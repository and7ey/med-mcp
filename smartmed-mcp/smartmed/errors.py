"""Безопасные ошибки клиента SmartMed.

Наружу отдаются только код, короткое сообщение и HTTP-статус. Значения
токенов, SMS-кодов, телефонов и тел ответов сюда не попадают.
"""

from __future__ import annotations


class SmartMedError(Exception):
    """Базовая ошибка клиента."""

    def __init__(self, code: str, message: str, *, http_status: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class AuthRequired(SmartMedError):
    """Нет действующей сессии либо сервер её не принял."""

    def __init__(
        self,
        message: str = "Нужен вход в SmartMed: вызовите open_bootstrap и войдите в браузере.",
        *,
        reason: str = "no_tokens",
    ) -> None:
        super().__init__("auth_required", message)
        self.reason = reason


class ApiError(SmartMedError):
    """Ответ сервера с бизнес-ошибкой или сетевой сбой."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(code, message, http_status=http_status)
        self.retryable = retryable
