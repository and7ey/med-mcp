"""HTTP-клиент API личного кабинета ЕМИАС."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

import requests

from .auth import (
    DEFAULT_SESSION_PATH,
    Session,
    build_login_url,
    load_session,
    parse_auth_code,
    save_session,
)
from .errors import AuthRequired, EmiasError, NotFound

BASE_URL = "https://lk.emias.mos.ru"
API_PREFIX = "/api"
DEFAULT_VERSION = "/1"

#: Коды, после которых имеет смысл обновить access_token и повторить запрос.
REFRESHABLE_CODES = {
    "AuthTokenExpired",
    "TokenExpired",
    "AccessTokenExpired",
    "Unauthorized",
    "AuthTokenInvalid",
}

#: Значения shortDateFilter, которые понимает большинство разделов медкарты.
#: ``month`` сервер отвергает с ValidationError (400), хотя в UI он когда-то был.
PERIODS = {
    "half_year": "half_year",
    "year": "year",
    "all_time": "all_time",
}


class EmiasClient:
    """Клиент ``lk.emias.mos.ru/api``.

    Пример::

        client = EmiasClient()
        client.login_interactive()          # один раз, токены лягут в файл
        for p in client.patients():
            print(p["ehrId"], p["firstName"])
        print(client.visits(p["ehrId"]))
    """

    def __init__(
        self,
        session: Session | None = None,
        *,
        session_path: Path | str = DEFAULT_SESSION_PATH,
        timeout: float = 30.0,
        user_agent: str | None = None,
        auto_refresh: bool = True,
    ) -> None:
        self.base_url = BASE_URL
        self.session_path = Path(session_path)
        self.timeout = timeout
        self.auto_refresh = auto_refresh
        self.session = session if session is not None else load_session(self.session_path)
        self.http = requests.Session()
        self.http.headers.update(
            {
                "User-Agent": user_agent
                or "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36",
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Origin": BASE_URL,
                "Referer": BASE_URL + "/",
            }
        )

    # ------------------------------------------------------------------ auth

    @property
    def access_token(self) -> str:
        return self.session.access_token

    def is_logged_in(self) -> bool:
        return self.session.is_logged_in

    def login_url(self) -> str:
        return build_login_url()

    def login(self, code: str) -> dict[str, Any]:
        """Шаг 3: обменять OAuth-код на attempt_token (или сразу на токены)."""
        payload = {
            "code": parse_auth_code(code),
            "redirectUrl": "https://lk.emias.mos.ru/auth",
        }
        data = self._request(
            "POST",
            "/login",
            version="/1",
            body=payload,
            auth_flow=True,
            use_token=False,
            retry=False,
        )
        if isinstance(data, dict) and data.get("access_token"):
            self._store_tokens(data)
        return data

    def confirm(self, otp_code: str, attempt_token: str) -> dict[str, Any]:
        """Шаг 4: подтвердить одноразовый код и получить токены."""
        data = self._request(
            "POST",
            "/confirm",
            version="/1",
            body={"otp_code": otp_code, "attempt_token": attempt_token},
            auth_flow=True,
            use_token=False,
            retry=False,
        )
        if isinstance(data, dict) and data.get("access_token"):
            self._store_tokens(data)
        return data

    def resend(self, attempt_token: str) -> dict[str, Any]:
        """Отправить код повторно."""
        return self._request(
            "POST",
            "/resend",
            version="/1",
            body={"attempt_token": attempt_token},
            auth_flow=True,
            use_token=False,
            retry=False,
        )

    def login_interactive(
        self,
        *,
        code: str | None = None,
        otp_code: str | None = None,
        open_browser: bool = True,
        out=print,
    ) -> Session:
        """Полный интерактивный вход.

        Пароль вводится только на сайте mos.ru — сюда он не попадает.
        """
        if not code:
            url = self.login_url()
            if open_browser:
                try:
                    import webbrowser

                    webbrowser.open(url)
                except Exception:  # pragma: no cover
                    pass
            out("Откройте ссылку и войдите на mos.ru:\n" + url + "\n")
            code = input(
                "После входа браузер перейдёт на https://lk.emias.mos.ru/auth?code=...\n"
                "Вставьте сюда адрес целиком или только значение code: "
            ).strip()
            if not code:
                raise AuthRequired("Код авторизации не введён")

        data = self.login(code)

        if not data.get("access_token"):
            attempt_token = data.get("attempt_token")
            if not attempt_token:
                raise AuthRequired(
                    "Сервер не вернул ни access_token, ни attempt_token. "
                    f"Ответ: {json.dumps(data, ensure_ascii=False)[:500]}"
                )
            delay = data.get("delay_timeout")
            if delay:
                out(f"Код отправлен. Повторная отправка возможна через {delay} c.")
            if not otp_code:
                otp_code = input("Введите код подтверждения (СМС/пуш): ").strip()
            confirm = self.confirm(otp_code, attempt_token)
            if not confirm.get("access_token"):
                raise AuthRequired(f"Не удалось подтвердить код: {confirm}")

        self.save()
        out("Вход выполнен, токены сохранены в " + str(self.session_path))
        return self.session

    def refresh(self) -> Session:
        """Шаг 5: обновить access_token по refresh_token."""
        if not self.session.refresh_token:
            raise AuthRequired("Нет refresh_token — выполните вход заново")
        data = self._request(
            "POST",
            "/refresh",
            version="/1",
            body={"refresh_token": self.session.refresh_token},
            auth_flow=True,
            use_token=False,
            retry=False,
        )
        self._store_tokens(data)
        self.save()
        return self.session

    def logout(self) -> None:
        try:
            self._request("POST", "/logout", version="/1", body=None)
        except EmiasError:
            pass
        self.session = Session()
        try:
            self.session_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _store_tokens(self, data: Mapping[str, Any]) -> None:
        self.session.access_token = data.get("access_token") or data.get("accessToken") or ""
        self.session.refresh_token = (
            data.get("refresh_token") or data.get("refreshToken") or self.session.refresh_token
        )
        self.session.obtained_at = time.time()

    def save(self) -> Path:
        return save_session(self.session, self.session_path)

    # --------------------------------------------------------------- request

    def request(
        self,
        method: str,
        endpoint: str,
        *,
        version: str | None = DEFAULT_VERSION,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
        binary: bool = False,
        timeout: float | None = None,
    ) -> Any:
        """Публичный запрос к API с авторизацией и автообновлением токена."""
        return self._request(
            method,
            endpoint,
            version=version,
            query=query,
            body=body,
            binary=binary,
            timeout=timeout,
        )

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        version: str | None = DEFAULT_VERSION,
        query: Mapping[str, Any] | None = None,
        body: Any = None,
        binary: bool = False,
        timeout: float | None = None,
        auth_flow: bool = False,
        use_token: bool = True,
        retry: bool = True,
    ) -> Any:
        path = f"{API_PREFIX}{'/auth' if auth_flow else ''}{(version or '')}{endpoint}"
        url = self.base_url + path

        headers = {
            "X-System-Name": "LK",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if use_token:
            if not self.session.access_token:
                raise AuthRequired("Нет access_token — вызовите login_interactive()")
            headers["X-Access-JWT"] = self.session.access_token
        else:
            headers["X-Environment"] = "prod"

        params: list[tuple[str, str]] = []
        for key, value in (query or {}).items():
            if value is None:
                continue
            if isinstance(value, (list, tuple)):
                params.extend((key, str(item)) for item in value)
            elif isinstance(value, bool):
                params.append((key, "true" if value else "false"))
            else:
                params.append((key, str(value)))

        try:
            response = self.http.request(
                method.upper(),
                url,
                params=params or None,
                json=body,
                headers=headers,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise EmiasError(f"Сетевая ошибка: {exc}", method=method, url=url) from exc

        if response.status_code >= 400:
            code, message, payload = self._parse_error(response)
            if (
                retry
                and self.auto_refresh
                and response.status_code == 401
                and code in REFRESHABLE_CODES
                and self.session.refresh_token
                and use_token
            ):
                try:
                    self.refresh()
                except EmiasError:
                    raise AuthRequired(message, code=code, status=response.status_code)
                return self._request(
                    method,
                    endpoint,
                    version=version,
                    query=query,
                    body=body,
                    binary=binary,
                    timeout=timeout,
                    auth_flow=auth_flow,
                    use_token=use_token,
                    retry=False,
                )
            if response.status_code == 404:
                raise NotFound(message, code=code, status=404, payload=payload, method=method, url=url)
            raise EmiasError(
                message,
                code=code,
                status=response.status_code,
                payload=payload,
                method=method,
                url=url,
            )

        if binary:
            return response.content
        if not response.content:
            return None
        try:
            return response.json()
        except ValueError:
            return response.text

    @staticmethod
    def _parse_error(response: requests.Response) -> tuple[str | None, str, Any]:
        try:
            data = response.json()
        except ValueError:
            return None, f"HTTP {response.status_code}: {response.text[:300]}", None
        if isinstance(data, dict):
            code = data.get("code")
            message = data.get("message") or data.get("title") or f"HTTP {response.status_code}"
            return code, str(message), data.get("payload")
        return None, str(data), data

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _unwrap(data: Any) -> Any:
        """Многие ручки заворачивают ответ в ``{"data": ...}``."""
        if isinstance(data, dict) and set(data.keys()) <= {"data"} and "data" in data:
            return data["data"]
        return data


def iter_nodes(value: Any) -> Iterable[Any]:
    """Рекурсивно обойти JSON и вернуть все словари/списки."""
    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from iter_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_nodes(item)
