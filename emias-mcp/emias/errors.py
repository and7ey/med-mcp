"""Ошибки API ЕМИАС."""

from __future__ import annotations

from typing import Any


class EmiasError(Exception):
    """Ошибка, вернувшаяся от API (или сетевая).

    API отвечает JSON-объектом вида::

        {"code": "Unauthorized", "title": null,
         "message": "X-Access-JWT header is not found", "payload": null}
    """

    def __init__(
        self,
        message: str,
        *,
        code: str | None = None,
        status: int | None = None,
        payload: Any = None,
        method: str | None = None,
        url: str | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status
        self.payload = payload
        self.method = method
        self.url = url

    def __str__(self) -> str:  # pragma: no cover - косметика
        parts = [self.message]
        if self.code:
            parts.append(f"code={self.code}")
        if self.status:
            parts.append(f"http={self.status}")
        return " | ".join(parts)


class AuthRequired(EmiasError):
    """Нет токена или он истёк и обновить не удалось."""


class NotFound(EmiasError):
    """404."""
