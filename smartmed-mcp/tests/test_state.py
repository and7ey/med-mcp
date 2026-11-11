"""Тесты состояния MCP: файловый fallback, права 0600, отсутствие секретов в state."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from smartmed_mcp.state import StateStore, TokenStore


class TokenStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        os.environ["SMARTMED_MCP_DISABLE_KEYCHAIN"] = "1"

    def tearDown(self):
        os.environ.pop("SMARTMED_MCP_DISABLE_KEYCHAIN", None)
        self.tmp.cleanup()

    def test_save_load_roundtrip_file_mode(self):
        store = TokenStore(self.dir)
        store.save({"access_token": "a", "refresh_token": "r", "obtained_at": 1})
        loaded = store.load()
        self.assertEqual(loaded["access_token"], "a")
        self.assertEqual(loaded["refresh_token"], "r")

    def test_file_permissions_0600(self):
        store = TokenStore(self.dir)
        store.save({"access_token": "a", "refresh_token": "r"})
        mode = stat.S_IMODE(store.file_path.stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_clear_removes_tokens(self):
        store = TokenStore(self.dir)
        store.save({"access_token": "a", "refresh_token": "r"})
        store.clear()
        self.assertFalse(store.file_path.exists())
        self.assertEqual(store.load(), {})

    def test_unknown_fields_are_dropped(self):
        store = TokenStore(self.dir)
        store.save({"access_token": "a", "phone": "+70000000000", "cookie": "x"})
        loaded = store.load()
        self.assertEqual(set(loaded), {"access_token"})

    def test_partial_save_keeps_refresh_token(self):
        store = TokenStore(self.dir)
        store.save({"access_token": "a", "refresh_token": "r"})
        store.save({"auth_style": "raw"})
        loaded = store.load()
        self.assertEqual(loaded["access_token"], "a")
        self.assertEqual(loaded["auth_style"], "raw")


class StateStoreTest(unittest.TestCase):
    def test_state_has_no_tokens(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = StateStore(tmp)
            state.update(last_login_at=123)
            path = Path(tmp) / "state.json"
            text = path.read_text("utf-8")
            self.assertNotIn("access_token", text)
            self.assertNotIn("refresh_token", text)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


if __name__ == "__main__":
    unittest.main()
