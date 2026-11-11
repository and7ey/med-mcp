"""Первичный вход через официальный веб-кабинет https://smartmed.pro.

Пользователь вводит телефон и код из SMS только на сайте SmartMed: секреты не
проходят через чат и не сохраняются. После успешного входа кабинет вызывает
``POST /personal/api/users/completelogon/v1`` и кладёт пару токенов в cookies
``_smed-at``/``_smed-rt``. Worker забирает токены из ответа (основной способ)
или из cookies (резервный), сохраняет их и закрывает браузер.

Playwright синхронный API используется строго в одном постоянном потоке
(``_run``), как требует руководство для банковских MCP-серверов.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from .safe_log import SafeLog
from .state import StateStore, TokenStore

LOGIN_URL = "https://smartmed.pro/sign-in"
LOGIN_HOST = "smartmed.pro"
COMPLETE_LOGON_MARKER = "/users/completelogon/v1"
ACCESS_COOKIE = "_smed-at"
REFRESH_COOKIE = "_smed-rt"

BROWSER_TIMEOUT_SEC = 600
POLL_INTERVAL_SEC = 1.0


class BrowserBootstrap:
    def __init__(
        self,
        tokens: TokenStore,
        state: StateStore,
        log: SafeLog,
        directory: Path,
    ) -> None:
        self.tokens = tokens
        self.state = state
        self.log = log
        self.directory = directory
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._captured: dict[str, Any] | None = None
        self._status = "idle"
        self._reason: str | None = None
        self._started_at: float | None = None

    # ------------------------------------------------------------------ API

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.snapshot()
            self._stop.clear()
            self._captured = None
            self._status = "starting"
            self._reason = None
            self._started_at = time.time()
            self._thread = threading.Thread(
                target=self._run, name="smartmed-browser-login", daemon=True
            )
            self._thread.start()
        self.log.event("browser_login_started")
        return self.snapshot()

    def close(self) -> dict[str, Any]:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=15)
        with self._lock:
            if self._status in ("starting", "waiting"):
                self._status = "closed"
        self.log.event("browser_login_closed")
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            running = bool(self._thread and self._thread.is_alive())
            data: dict[str, Any] = {
                "status": self._status,
                "browser": "running" if running else "stopped",
            }
            if self._started_at:
                data["elapsed_sec"] = int(time.time() - self._started_at)
            if self._reason:
                data["reason"] = self._reason
            return data

    # ------------------------------------------------------------- worker

    def _run(self) -> None:
        profile_dir = Path(tempfile.mkdtemp(prefix="smartmed-login-", dir=self.directory))
        context = None
        try:
            from playwright.sync_api import sync_playwright
        except Exception:
            self._finish("failed", "playwright_unavailable")
            return

        try:
            with sync_playwright() as playwright:
                try:
                    context = playwright.chromium.launch_persistent_context(
                        user_data_dir=str(profile_dir),
                        channel="chrome",
                        headless=False,
                        args=[
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--disable-background-networking",
                        ],
                    )
                except Exception:
                    self._finish("failed", "browser_unavailable")
                    return

                context.on("response", self._on_response)
                self._set_status("waiting")
                self.log.event("browser_started")
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    # Пользователь мог закрыть окно или сеть медленная.
                    pass

                deadline = time.time() + BROWSER_TIMEOUT_SEC
                while not self._stop.wait(POLL_INTERVAL_SEC):
                    captured = self._take_captured()
                    found = captured or self._read_cookies(context)
                    if found:
                        self._store(
                            found,
                            source="completelogon_response" if captured else "cookies",
                        )
                        return
                    if time.time() > deadline:
                        self._finish("timeout", "login_timeout")
                        self.log.event("browser_login_timeout")
                        return
                    if self._context_closed(context):
                        self._finish("closed", "browser_closed")
                        return
        except Exception:
            self._finish("failed", "browser_error")
            self.log.event("browser_login_failed", error="browser_error")
        finally:
            try:
                if context is not None:
                    context.close()
            except Exception:
                pass
            shutil.rmtree(profile_dir, ignore_errors=True)

    def _on_response(self, response: Any) -> None:
        """Перехватить токены из ответа completelogon (только в памяти)."""
        try:
            if COMPLETE_LOGON_MARKER not in (response.url or ""):
                return
            if response.status != 200:
                return
            payload = response.json()
        except Exception:
            return
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return
        access = str(data.get("token") or "").strip()
        refresh = str(data.get("refreshToken") or "").strip()
        if not access or not refresh:
            return
        tokens: dict[str, Any] = {
            "access_token": access,
            "refresh_token": refresh,
            "obtained_at": int(time.time()),
        }
        user_id = data.get("userId")
        if user_id not in (None, ""):
            tokens["user_id"] = str(user_id)
        with self._lock:
            self._captured = tokens

    def _take_captured(self) -> dict[str, Any] | None:
        with self._lock:
            return self._captured

    def _read_cookies(self, context: Any) -> dict[str, Any] | None:
        try:
            cookies = context.cookies()
        except Exception:
            return None
        access = ""
        refresh = ""
        for cookie in cookies:
            name = cookie.get("name")
            if name == ACCESS_COOKIE and not access:
                access = str(cookie.get("value") or "").strip()
            elif name == REFRESH_COOKIE and not refresh:
                refresh = str(cookie.get("value") or "").strip()
        if not access or not refresh:
            return None
        return {
            "access_token": access,
            "refresh_token": refresh,
            "obtained_at": int(time.time()),
        }

    def _store(self, tokens: dict[str, Any], *, source: str) -> None:
        self.tokens.save(tokens)
        self.state.update(last_login_at=int(time.time()), session_verified_at=0)
        self._finish("ready", None)
        self.log.event("tokens_captured", source=source)

    @staticmethod
    def _context_closed(context: Any) -> bool:
        try:
            _ = context.pages
            return False
        except Exception:
            return True

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status

    def _finish(self, status: str, reason: str | None) -> None:
        with self._lock:
            self._status = status
            self._reason = reason
