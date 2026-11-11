"""Безопасные ошибки MCP-сервера SmartMed.

Наружу отдаются только код, короткое сообщение и HTTP-статус. URL, query,
тела ответов, телефон, SMS-коды и токены не попадают ни в ответы, ни в журнал.
"""

from __future__ import annotations

from typing import Any


class McpError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        http_status: int | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.extra = extra or {}

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "ok": False,
            "error": self.code,
            "message": self.message,
        }
        if self.http_status:
            data["http_status"] = self.http_status
        data.update(self.extra)
        return data


class AuthRequired(McpError):
    def __init__(
        self,
        message: str = "Нужен вход в SmartMed: вызовите open_bootstrap и войдите в браузере.",
        *,
        reason: str = "no_tokens",
    ) -> None:
        super().__init__("auth_required", message, extra={"reason": reason})
