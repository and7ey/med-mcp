"""HTTP-транспорт SmartMed.

Особенности протокола, подтверждённые по живому API и веб-кабинету:

* база веб-кабинета — ``https://gateway.smartmed.pro/personal``;
  ``/api/...`` — пользовательские методы, ``/proxyapi/...`` — медицинские;
* заголовки ``Application-Version``, ``OS-Type``, ``CurrentDate``,
  ``Timezone-Offset`` обязательны: без них сервер отвечает бизнес-ошибкой;
* каждый ответ обёрнут в ``{data, statusInfo}``, успех — ``statusInfo.status``
  со значением ``SUCCESS``/``OK``;
* протухшая сессия — код ``SESSION_TOKEN_ERROR``; пара токенов обновляется
  через ``POST /users/logonbytoken/v2``;
* access-токен передаётся в ``Authorization`` как есть: официальное
  мобильное приложение кладёт туда строку без префикса, а свежие токены
  могут уже содержать ``Bearer ``. Клиент сохраняет исходный вид и при
  необходимости один раз пробует второй вариант.

Модуль не печатает токены, телефон, тела ответов и URL с query.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Callable

import requests

from .errors import ApiError, AuthRequired

BASE_URL = "https://gateway.smartmed.pro"
API_BASE = BASE_URL + "/personal/api"
PROXY_BASE = BASE_URL + "/personal/proxyapi"
WEB_ORIGIN = "https://smartmed.pro"
#: Версия, которую присылает официальный веб-кабинет (patient@0.21.0).
APP_VERSION = "2.2.0"

REFRESH_PATH = "/users/logonbytoken/v2"

SUCCESS_STATUSES = {"SUCCESS", "OK"}
SESSION_ERROR = "SESSION_TOKEN_ERROR"
REFRESH_ERROR = "REFRESH_TOKEN_ERROR"
AUTH_ERRORS = {SESSION_ERROR, REFRESH_ERROR, "AUTH_ERROR", "ACCESS_DENY"}
#: Коды «приложение устарело» встречаются и как code, и как текст.
VERSION_ERROR_CODES = {"APPLICATION_VERSION_TOO_SMALL_ERROR", "0"}

DEFAULT_TIMEOUT = 30.0
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/141.0 Safari/537.36"
)

TokenUpdateCallback = Callable[[dict[str, Any]], None]


def _bool_env(name: str) -> bool:
    import os

    return os.getenv(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _now_headers() -> dict[str, str]:
    now = datetime.now().astimezone()
    offset = now.utcoffset()
    minutes = int(abs(offset.total_seconds()) // 60) if offset else 0
    return {
        "CurrentDate": now.isoformat(timespec="seconds"),
        "Timezone-Offset": str(minutes),
    }


def _first_message(payload: Any) -> tuple[str | None, str | None]:
    if not isinstance(payload, dict):
        return None, None
    info = (payload.get("statusInfo") or {}).get("infoMessages") or []
    if not isinstance(info, list) or not info:
        return None, None
    first = info[0] if isinstance(info[0], dict) else {}
    code = first.get("code")
    descr = first.get("descr")
    return (str(code) if code is not None else None, str(descr) if descr else None)


class SmartMedClient:
    """Тонкий клиент gateway.smartmed.pro с одним refresh и без ретраев записи."""

    def __init__(
        self,
        access_token: str | None = None,
        refresh_token: str | None = None,
        *,
        auth_style: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        user_agent: str | None = None,
        on_tokens: TokenUpdateCallback | None = None,
    ) -> None:
        self.access_token = (access_token or "").strip()
        self.refresh_token = (refresh_token or "").strip()
        # "auto" — отправлять токен как есть; "raw" — без префикса Bearer.
        self.auth_style = (auth_style or "auto").strip() or "auto"
        self.timeout = timeout
        self.on_tokens = on_tokens
        self._refresh_lock = False
        self.http = requests.Session()
        self.http.headers.update(
            {
                "User-Agent": user_agent or DEFAULT_UA,
                "Accept-Language": "ru-RU,ru;q=0.9",
                "Origin": WEB_ORIGIN,
                "Referer": WEB_ORIGIN + "/",
            }
        )
        self._configure_tls()

    # ------------------------------------------------------------------ TLS

    def _configure_tls(self) -> None:
        """Системное хранилище корней (если есть truststore), иначе certifi."""
        if _bool_env("SMARTMED_MCP_INSECURE"):
            raise RuntimeError(
                "Отключение проверки TLS запрещено правилами проекта: уберите "
                "SMARTMED_MCP_INSECURE и установите системные корневые сертификаты."
            )
        try:  # pragma: no cover - зависит от окружения
            import truststore

            truststore.inject_into_ssl()
        except Exception:
            try:
                import certifi

                self.http.verify = certifi.where()
            except Exception:
                pass

    # -------------------------------------------------------------- tokens

    @property
    def has_tokens(self) -> bool:
        return bool(self.access_token or self.refresh_token)

    def update_tokens(self, tokens: dict[str, Any]) -> None:
        if "access_token" in tokens:
            self.access_token = str(tokens.get("access_token") or "").strip()
        if "refresh_token" in tokens:
            self.refresh_token = str(tokens.get("refresh_token") or "").strip()
        if tokens.get("auth_style"):
            self.auth_style = str(tokens["auth_style"])

    def _auth_value(self, style: str | None = None) -> str | None:
        token = self.access_token.strip()
        if not token:
            return None
        style = style or self.auth_style
        is_bearer = token.lower().startswith("bearer ")
        if style == "raw":
            return token[7:].strip() if is_bearer else token
        if style == "bearer" and not is_bearer:
            return "Bearer " + token
        return token

    def _base_headers(self, *, with_auth: bool = True, style: str | None = None) -> dict[str, str]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Pragma": "no-cache",
            "Application-Version": APP_VERSION,
            "OS-Type": "Web",
        }
        headers.update(_now_headers())
        if with_auth:
            value = self._auth_value(style)
            if value:
                headers["Authorization"] = value
        return headers

    # ------------------------------------------------------------- requests

    def request(
        self,
        method: str,
        path: str,
        *,
        proxy: bool = False,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
        patient_id: str | None = None,
        extra_headers: dict[str, str] | None = None,
        read_only: bool = True,
    ) -> Any:
        """Выполнить запрос и вернуть ``data`` из конверта.

        Повтор разрешён только для read-only вызовов: один раз после refresh
        токена и один раз со вторым вариантом заголовка Authorization.
        """
        base = PROXY_BASE if proxy else API_BASE
        url = base + path
        if not self.access_token and self.refresh_token:
            self.refresh()

        styles: list[str] = [self.auth_style]
        if read_only:
            alt = "raw" if self.auth_style != "raw" else "bearer"
            styles.append(alt)

        refreshed = False
        for style in styles:
            try:
                payload, status, response = self._send(
                    method,
                    url,
                    params=params,
                    json_body=json_body,
                    patient_id=patient_id,
                    extra_headers=extra_headers,
                    style=style,
                )
                data = self._unwrap(payload, status, response, url)
            except ApiError as exc:
                if exc.code not in AUTH_ERRORS:
                    raise
                if read_only and not refreshed and self.refresh_token and not self._refresh_lock:
                    refreshed = True
                    self.refresh()
                    try:
                        payload, status, response = self._send(
                            method,
                            url,
                            params=params,
                            json_body=json_body,
                            patient_id=patient_id,
                            extra_headers=extra_headers,
                            style=self.auth_style,
                        )
                        data = self._unwrap(payload, status, response, url)
                    except ApiError as retry_exc:
                        if retry_exc.code not in AUTH_ERRORS:
                            raise
                        continue
                    else:
                        return data
                else:
                    continue
            else:
                if style != self.auth_style:
                    # Запоминаем рабочий вид заголовка, чтобы не перебирать его снова.
                    self.auth_style = style
                    if self.on_tokens:
                        self.on_tokens({"auth_style": style})
                return data

        raise AuthRequired(reason="server_rejected_token")

    def request_bytes(
        self,
        path: str,
        *,
        proxy: bool = False,
        params: dict[str, Any] | None = None,
        patient_id: str | None = None,
    ) -> bytes:
        """Скачать двоичный файл (например, документ медкарты).

        Токен и содержимое файла не логируются; ошибка разбирается по
        конверту, если сервер вернул JSON вместо файла. Скачивание read-only,
        поэтому допускается один refresh при истёкшей сессии.
        """
        if not self.access_token and self.refresh_token:
            self.refresh()
        try:
            return self._get_bytes(path, proxy=proxy, params=params, patient_id=patient_id)
        except AuthRequired:
            if not self.refresh_token or self._refresh_lock:
                raise
            self.refresh()
            return self._get_bytes(path, proxy=proxy, params=params, patient_id=patient_id)

    def _get_bytes(
        self,
        path: str,
        *,
        proxy: bool,
        params: dict[str, Any] | None,
        patient_id: str | None,
    ) -> bytes:
        base = PROXY_BASE if proxy else API_BASE
        headers = self._base_headers()
        headers["Accept"] = "*/*"
        if patient_id:
            headers["PatientId"] = str(patient_id)
        try:
            response = self.http.request(
                "GET", base + path, params=params, headers=headers, timeout=self.timeout
            )
        except requests.RequestException as exc:
            raise ApiError("network_error", "Сеть недоступна. Повторите позже.", retryable=True) from exc

        if response.status_code in (401, 403):
            raise AuthRequired(reason="http_unauthorized")
        if response.status_code != 200:
            raise ApiError(
                "http_error",
                "Сервер не отдал файл.",
                http_status=response.status_code,
                retryable=response.status_code >= 500,
            )
        content_type = response.headers.get("Content-Type", "")
        if "application/json" in content_type:
            try:
                payload = response.json()
            except ValueError:
                payload = {}
            code, _ = _first_message(payload)
            if code in AUTH_ERRORS:
                raise AuthRequired(reason="server_rejected_token")
            raise ApiError(code or "file_error", "Сервер вернул ошибку вместо файла.")
        if not response.content:
            raise ApiError("empty_file", "Сервер вернул пустой файл.")
        return response.content

    def _send(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None,
        json_body: Any | None,
        patient_id: str | None,
        extra_headers: dict[str, str] | None,
        style: str | None,
    ) -> tuple[Any, int, requests.Response]:
        headers = self._base_headers(style=style)
        if extra_headers:
            headers.update({str(k): str(v) for k, v in extra_headers.items()})
        if patient_id:
            headers["PatientId"] = str(patient_id)
        try:
            response = self.http.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ApiError(
                "network_error",
                "Сеть недоступна. Повторите позже.",
                retryable=True,
            ) from exc

        status = response.status_code
        if status in (401, 403):
            raise AuthRequired(reason="http_unauthorized")
        if status == 429:
            raise ApiError("rate_limited", "Слишком много запросов. Подождите минуту.", http_status=429)
        try:
            payload = response.json()
        except ValueError:
            if status >= 500:
                raise ApiError(
                    "http_error", "Сервер временно недоступен.", http_status=status, retryable=True
                )
            raise ApiError(
                "invalid_response",
                "Сервер вернул неожиданный ответ.",
                http_status=status,
            )
        return payload, status, response

    def _unwrap(
        self,
        payload: Any,
        status: int,
        response: requests.Response,
        url: str,
    ) -> Any:
        if not isinstance(payload, dict):
            return payload
        if "detail" in payload and "statusInfo" not in payload:
            raise ApiError(
                "validation_error",
                "Сервер отклонил запрос: неверные поля.",
                http_status=status,
            )
        status_info = payload.get("statusInfo")
        if not isinstance(status_info, dict):
            # Часть служебных методов отдаёт данные без конверта.
            return payload.get("data", payload)

        state = str(status_info.get("status") or "").upper()
        if state in SUCCESS_STATUSES:
            return payload.get("data")
        if not state and payload.get("data") is not None:
            return payload.get("data")

        code, descr = _first_message(payload)
        code = code or "api_error"
        if code in AUTH_ERRORS:
            raise ApiError(code, "Сессия SmartMed недействительна.", http_status=status)
        if code in VERSION_ERROR_CODES:
            raise ApiError(
                "app_version_rejected",
                "Сервер отклонил версию клиента. Обновите константу APP_VERSION в MCP-сервере.",
                http_status=status,
            )
        raise ApiError(
            code,
            _safe_descr(descr, code),
            http_status=status,
            retryable=status >= 500,
        )

    # -------------------------------------------------------------- refresh

    def refresh(self) -> dict[str, Any]:
        """Обновить пару токенов. Один запрос, без повторов и без Authorization."""
        if not self.refresh_token:
            raise AuthRequired(reason="no_refresh_token")
        if self._refresh_lock:
            raise AuthRequired(reason="refresh_in_progress")
        self._refresh_lock = True
        try:
            headers = self._base_headers(with_auth=False)
            try:
                response = self.http.post(
                    API_BASE + REFRESH_PATH,
                    json={"refreshToken": self.refresh_token},
                    headers=headers,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise ApiError(
                    "network_error", "Сеть недоступна. Повторите позже.", retryable=True
                ) from exc
            try:
                payload = response.json()
            except ValueError:
                raise ApiError(
                    "invalid_response",
                    "Сервер вернул неожиданный ответ при обновлении сессии.",
                    http_status=response.status_code,
                )
            code, _ = _first_message(payload)
            if code in AUTH_ERRORS or code in {"SESSION_TOKEN_ERROR", REFRESH_ERROR}:
                self._drop_tokens()
                raise AuthRequired(reason="refresh_rejected")
            data = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(data, dict) or not data.get("token"):
                self._drop_tokens()
                raise AuthRequired(reason="refresh_rejected")
            tokens = {
                "access_token": str(data.get("token") or ""),
                "refresh_token": str(data.get("refreshToken") or ""),
                "obtained_at": int(time.time()),
            }
            if not tokens["refresh_token"]:
                tokens.pop("refresh_token")
            self.update_tokens(tokens)
            if self.on_tokens:
                self.on_tokens(tokens)
            return {"ok": True, "refreshed": True}
        finally:
            self._refresh_lock = False

    def _drop_tokens(self) -> None:
        self.access_token = ""
        self.refresh_token = ""


def _safe_descr(descr: str | None, code: str) -> str:
    """Короткое безопасное описание без персональных данных."""
    if descr:
        text = descr.strip()
        if len(text) > 200:
            text = text[:200] + "…"
        return text
    return f"Сервер вернул ошибку {code}."
