"""Неофициальный клиент личного кабинета SmartMed (gateway.smartmed.pro).

Токены выдаёт официальный веб-кабинет https://smartmed.pro после входа по
телефону и SMS-коду. Клиент не хранит пароль и SMS-код и не обходит капчу.
"""

from __future__ import annotations

from .client import APP_VERSION, SmartMedClient
from .errors import ApiError, AuthRequired, SmartMedError

__all__ = [
    "APP_VERSION",
    "ApiError",
    "AuthRequired",
    "SmartMedClient",
    "SmartMedError",
]

__version__ = "0.2.0"
