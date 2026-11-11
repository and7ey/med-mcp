"""Аутентификация в личном кабинете ЕМИАС (lk.emias.mos.ru).

Схема (снята с web-клиента lk.emias.mos.ru):

1. Открываем OAuth-страницу mos.ru (SUDIR/ЕСИА)::

       https://login.mos.ru/sps/oauth/ae
           ?client_id=lk.emias.mos.ru
           &scope=openid+profile+contacts
           &redirect_uri=https://lk.emias.mos.ru/auth
           &response_type=code&access_type=offline

2. Пользователь входит (телефон/пароль mos.ru либо ЕСИА, у mos.ru может
   спросить код из СМС). Браузер уходит на
   ``https://lk.emias.mos.ru/auth?code=...``.

3. Код меняется на токены::

       POST /api/auth/1/login   {"code": "...", "redirectUrl": "https://lk.emias.mos.ru/auth"}

   Ответ содержит ``attempt_token`` и ``delay_timeout`` — это второй фактор.
   Если в ответе сразу есть ``access_token`` (бывает при некоторых сценариях),
   шаг 4 пропускается.

4. Подтверждаем одноразовый код (СМС/пуш)::

       POST /api/auth/1/confirm {"otp_code": "12345", "attempt_token": "..."}

   Ответ: ``{"access_token": "...", "refresh_token": "...", "show_onboarding": bool}``

5. Обновление токена::

       POST /api/auth/1/refresh {"refresh_token": "..."}
       -> {"access_token": "...", "refresh_token": "..."}

Все запросы к данным идут с заголовком ``X-Access-JWT: <access_token>``.
Для неавторизованных (шаги 3–5) вместо него ставится ``X-Environment: prod``.
"""

from __future__ import annotations

import json
import os
import stat
import time
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse

SUDIR_HOST = "https://login.mos.ru"
CLIENT_ID = "lk.emias.mos.ru"
REDIRECT_URI = "https://lk.emias.mos.ru/auth"

DEFAULT_SESSION_PATH = Path(
    os.environ.get(
        "EMIAS_SESSION_FILE",
        Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
        / "emias"
        / "session.json",
    )
)


def build_login_url(scope: str = "openid profile contacts") -> str:
    """Собрать URL страницы входа mos.ru."""
    query = {
        "client_id": CLIENT_ID,
        "scope": scope,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "access_type": "offline",
    }
    return f"{SUDIR_HOST}/sps/oauth/ae?{urlencode(query)}"


def parse_auth_code(value: str) -> str:
    """Достать ``code`` из вставленного URL или вернуть строку как есть."""
    value = value.strip().strip('"').strip("'")
    if value.startswith("http://") or value.startswith("https://"):
        qs = parse_qs(urlparse(value).query)
        code = qs.get("code", [None])[0]
        if not code:
            raise ValueError("В URL нет параметра ?code=...")
        return code
    # Пользователь мог вставить "code=abc&state=..." без схемы
    if "code=" in value:
        qs = parse_qs(value.split("?", 1)[-1])
        code = qs.get("code", [None])[0]
        if code:
            return code
    return value


@dataclass
class Session:
    """Состояние сессии: токены + служебные поля."""

    access_token: str = ""
    refresh_token: str = ""
    obtained_at: float = field(default_factory=time.time)
    login_variant: str = "regular"

    @property
    def is_logged_in(self) -> bool:
        return bool(self.access_token)

    def to_dict(self) -> dict[str, Any]:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "obtained_at": self.obtained_at,
            "login_variant": self.login_variant,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Session":
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            obtained_at=float(data.get("obtained_at") or 0),
            login_variant=data.get("login_variant", "regular"),
        )


def load_session(path: Path | str = DEFAULT_SESSION_PATH) -> Session:
    path = Path(path)
    if not path.exists():
        return Session()
    try:
        return Session.from_dict(json.loads(path.read_text("utf-8")))
    except (ValueError, OSError):
        return Session()


def save_session(session: Session, path: Path | str = DEFAULT_SESSION_PATH) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(session.to_dict(), ensure_ascii=False, indent=2), "utf-8")
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 0600: в файле токены
    except OSError:
        pass
    return path


def open_login_page(url: str | None = None) -> str:
    """Открыть страницу входа в браузере и вернуть URL."""
    url = url or build_login_url()
    try:
        webbrowser.open(url)
    except Exception:  # pragma: no cover - браузера может не быть
        pass
    return url
