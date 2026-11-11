"""ЕМИАС: клиент личного кабинета и медкарты."""

from .api import Emias, Slot, parse_slots
from .auth import Session, build_login_url, load_session, save_session
from .client import EmiasClient
from .errors import AuthRequired, EmiasError, NotFound

__all__ = [
    "Emias",
    "EmiasClient",
    "Slot",
    "parse_slots",
    "Session",
    "build_login_url",
    "load_session",
    "save_session",
    "EmiasError",
    "AuthRequired",
    "NotFound",
]

__version__ = "0.1.0"
