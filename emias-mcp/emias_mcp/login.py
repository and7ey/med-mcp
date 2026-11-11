"""Первичный вход через официальную страницу mos.ru в отдельном Chrome.

Пользователь вводит телефон/пароль/код только на сайте mos.ru — секреты не
проходят через чат и не сохраняются. После успешного входа SPA медкарты
кладёт токены в localStorage; worker забирает их, сохраняет и закрывает браузер.

Playwright синхронный API используется строго в одном постоянном потоке
(`_run`), как требует руководство для банковских MCP-серверов.
"""

from __future__ import annotations

import shutil
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

from emias.auth import build_login_url

from .safe_log import SafeLog
from .state import StateStore, TokenStore

BROWSER_TIMEOUT_SEC = 600
POLL_INTERVAL_SEC = 1.0
ACCESS_KEY = "patient.web.v2.accessToken"
REFRESH_KEY = "patient.web.v2.refreshToken"
LOGIN_VARIANT_KEY = "patient.web.v2.loginVariant"


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
        self._status = "idle"
        self._reason: str | None = None
        self._started_at: float | None = None

    # ------------------------------------------------------------------ API

    def start(self) -> dict[str, Any]:
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.snapshot()
            self._stop.clear()
            self._status = "starting"
            self._reason = None
            self._started_at = time.time()
            self._thread = threading.Thread(
                target=self._run, name="emias-browser-login", daemon=True
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
        profile_dir = Path(tempfile.mkdtemp(prefix="emias-login-", dir=self.directory))
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

                self._set_status("waiting")
                self.log.event("browser_started")
                page = context.pages[0] if context.pages else context.new_page()
                try:
                    page.goto(build_login_url(), wait_until="domcontentloaded", timeout=60000)
                except Exception:
                    # Пользователь мог успеть закрыть окно — не считаем это фатальным.
                    pass

                deadline = time.time() + BROWSER_TIMEOUT_SEC
                while not self._stop.wait(POLL_INTERVAL_SEC):
                    found = self._read_tokens(context)
                    if found:
                        self.tokens.save(found)
                        self.state.update(last_login_at=int(time.time()))
                        self._finish("ready", None)
                        self.log.event("tokens_captured", source="localStorage")
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

    @staticmethod
    def _context_closed(context: Any) -> bool:
        try:
            _ = context.pages
            return False
        except Exception:
            return True

    def _read_tokens(self, context: Any) -> dict[str, Any] | None:
        try:
            pages = list(context.pages)
        except Exception:
            return None
        for page in pages:
            try:
                url = page.url or ""
            except Exception:
                continue
            if "lk.emias.mos.ru" not in url:
                continue
            try:
                raw = page.evaluate(
                    """() => ({
                        access: localStorage.getItem('patient.web.v2.accessToken'),
                        refresh: localStorage.getItem('patient.web.v2.refreshToken'),
                        variant: localStorage.getItem('patient.web.v2.loginVariant'),
                    })"""
                )
            except Exception:
                continue
            if not isinstance(raw, dict):
                continue
            access = str(raw.get("access") or "")
            refresh = str(raw.get("refresh") or "")
            if not access or not refresh:
                continue
            return {
                "access_token": access,
                "refresh_token": refresh,
                "login_variant": str(raw.get("variant") or "regular"),
                "obtained_at": int(time.time()),
            }
        return None

    # ------------------------------------------------------------- helpers

    def _set_status(self, status: str) -> None:
        with self._lock:
            self._status = status

    def _finish(self, status: str, reason: str | None) -> None:
        with self._lock:
            self._status = status
            self._reason = reason
