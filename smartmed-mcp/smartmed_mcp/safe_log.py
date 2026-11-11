"""Безопасный журнал: только технические события без секретов и ПДн."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

LOG_FILE_NAME = "bootstrap-debug.log"


class SafeLog:
    def __init__(self, directory: Path | str) -> None:
        self.path = Path(directory) / LOG_FILE_NAME

    def event(self, name: str, **fields: Any) -> None:
        payload = {"ts": int(time.time()), "event": str(name)}
        for key, value in fields.items():
            if value is None:
                continue
            if isinstance(value, (int, float, bool)):
                payload[str(key)] = value
            else:
                # Только короткие технические коды, без свободного текста и URL.
                text = str(value)
                payload[str(key)] = text[:80]
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        try:
            fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)
            os.chmod(self.path, 0o600)
        except OSError:
            pass
