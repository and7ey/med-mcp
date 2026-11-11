"""Состояние MCP-сервера: метаданные и токены.

* `state.json` — только несекретные метаданные (время входа/refresh).
* Токены — в macOS Keychain (служба `emias-medcard-mcp`). Если Keychain
  недоступен (Linux/CI), используется `tokens.json` с правами 0600.
* Совместимость: при первом запуске можно подхватить токены из
  `~/.config/emias/session.json`, который пишет CLI (`python -m emias`).

Файлы состояния никогда не логируются и не возвращаются через MCP.
"""

from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_DIR_NAME = ".emias-mcp"
STATE_FILE_NAME = "state.json"
TOKENS_FILE_NAME = "tokens.json"
KEYCHAIN_SERVICE = "emias-medcard-mcp"
KEYCHAIN_ACCOUNT = "tokens"
_TOKEN_KEYS = ("access_token", "refresh_token", "obtained_at", "login_variant")


def resolve_state_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Каталог состояния: аргумент -> EMIAS_MCP_HOME -> ~/.emias-mcp."""
    if explicit:
        candidate = Path(explicit).expanduser()
    else:
        env = os.getenv("EMIAS_MCP_HOME")
        candidate = Path(env).expanduser() if env else Path.home() / DEFAULT_DIR_NAME
    candidate.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(candidate, 0o700)
    except OSError:
        pass
    return candidate


class StateStore:
    """Несекретные метаданные (JSON, 0600, атомарная запись)."""

    def __init__(self, directory: Path | str | None = None) -> None:
        self.directory = resolve_state_dir(directory)
        self.path = self.directory / STATE_FILE_NAME

    def load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def save(self, data: dict[str, Any]) -> None:
        _atomic_write(self.path, json.dumps(data, ensure_ascii=False, indent=2).encode())

    def update(self, **fields: Any) -> dict[str, Any]:
        data = self.load()
        data.update(fields)
        self.save(data)
        return data


def _atomic_write(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    tmp = Path(tmp_name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def _keychain_available() -> bool:
    if os.getenv("EMIAS_MCP_DISABLE_KEYCHAIN") == "1":
        return False
    return bool(shutil.which("security")) and platform.system() == "Darwin"


def _keychain_read() -> str | None:
    if not _keychain_available():
        return None
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _keychain_write(value: str) -> bool:
    if not _keychain_available():
        return False
    try:
        result = subprocess.run(
            [
                "security",
                "add-generic-password",
                "-U",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
                "-w",
                value,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def _keychain_delete() -> None:
    if not _keychain_available():
        return
    try:
        subprocess.run(
            [
                "security",
                "delete-generic-password",
                "-s",
                KEYCHAIN_SERVICE,
                "-a",
                KEYCHAIN_ACCOUNT,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


class TokenStore:
    """Токены: Keychain на macOS, файл 0600 как переносимый fallback."""

    def __init__(self, directory: Path | str | None = None, *, import_cli: bool = True) -> None:
        self.directory = resolve_state_dir(directory)
        self.file_path = self.directory / TOKENS_FILE_NAME
        self.import_cli = import_cli and os.getenv("EMIAS_MCP_IMPORT_CLI", "1") != "0"

    # ------------------------------------------------------------------ API

    def load(self) -> dict[str, Any]:
        return self._clean(self._read_keychain() or self._read_file())

    def save(self, tokens: dict[str, Any]) -> None:
        clean = self._clean(tokens)
        payload = json.dumps(clean, ensure_ascii=False)
        if _keychain_write(payload):
            self.file_path.unlink(missing_ok=True)
            return
        _atomic_write(self.file_path, payload.encode())

    def clear(self) -> None:
        _keychain_delete()
        self.file_path.unlink(missing_ok=True)

    def storage_kind(self) -> str:
        if _keychain_available():
            return "keychain" if self._read_keychain() is not None else "keychain-empty"
        return "file" if self.file_path.exists() else "file-empty"

    # ------------------------------------------------------------- private

    @staticmethod
    def _clean(tokens: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(tokens, dict):
            return {}
        return {key: tokens[key] for key in _TOKEN_KEYS if tokens.get(key) not in (None, "")}

    def _read_keychain(self) -> dict[str, Any]:
        raw = _keychain_read()
        if not raw:
            return {}
        try:
            data = json.loads(raw)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    def _read_file(self) -> dict[str, Any]:
        try:
            data = json.loads(self.file_path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def import_cli_session(self) -> dict[str, Any]:
        if not self.import_cli:
            return {}
        return self._clean(self._read_cli_session())

    @staticmethod
    def _read_cli_session() -> dict[str, Any]:
        path = Path(
            os.getenv("EMIAS_SESSION_FILE")
            or Path(os.getenv("XDG_CONFIG_HOME", Path.home() / ".config"))
            / "emias"
            / "session.json"
        ).expanduser()
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(data, dict) or not data.get("access_token"):
            return {}
        data.setdefault("obtained_at", int(time.time()))
        return data
