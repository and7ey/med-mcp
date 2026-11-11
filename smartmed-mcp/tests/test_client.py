"""Контракт HTTP-клиента: конверт, refresh, смена вида Authorization."""

from __future__ import annotations

import unittest

from smartmed.client import SmartMedClient
from smartmed.errors import ApiError, AuthRequired

SUCCESS = {"statusInfo": {"status": "SUCCESS", "infoMessages": []}, "data": {"value": 1}}
SESSION_ERROR = {
    "statusInfo": {
        "status": "ERROR",
        "infoMessages": [{"code": "SESSION_TOKEN_ERROR", "descr": "Invalid session token"}],
    },
    "data": None,
}
VERSION_ERROR = {
    "statusInfo": {
        "status": "ERROR",
        "infoMessages": [{"code": "0", "descr": "Ваше приложение устарело"}],
    },
    "data": None,
}


class FakeClient(SmartMedClient):
    def __init__(self, responses, *, refresh_result="ok", **kwargs):
        super().__init__(**kwargs)
        self.responses = list(responses)
        self.refresh_result = refresh_result
        self.refresh_calls = 0
        self.styles: list[str | None] = []

    def _send(self, method, url, *, params, json_body, patient_id, extra_headers, style):
        self.styles.append(style)
        payload = self.responses.pop(0)
        return payload, 200, None

    def refresh(self):
        self.refresh_calls += 1
        if self.refresh_result != "ok":
            raise AuthRequired(reason="refresh_rejected")
        self.access_token = "Bearer new"
        return {"ok": True, "refreshed": True}


class EnvelopeTest(unittest.TestCase):
    def test_success_returns_data(self):
        client = FakeClient([SUCCESS], access_token="Bearer a")
        self.assertEqual(client.request("GET", "/x"), {"value": 1})

    def test_version_error(self):
        client = FakeClient([VERSION_ERROR], access_token="Bearer a")
        with self.assertRaises(ApiError) as ctx:
            client.request("GET", "/x")
        self.assertEqual(ctx.exception.code, "app_version_rejected")

    def test_plain_payload_without_envelope(self):
        client = FakeClient([{"configs": []}], access_token="Bearer a")
        self.assertEqual(client.request("GET", "/x"), {"configs": []})


class RefreshTest(unittest.TestCase):
    def test_session_error_refreshes_and_retries(self):
        client = FakeClient([SESSION_ERROR, SUCCESS], access_token="Bearer a", refresh_token="r")
        self.assertEqual(client.request("GET", "/x"), {"value": 1})
        self.assertEqual(client.refresh_calls, 1)

    def test_refresh_rejected_raises_auth_required(self):
        client = FakeClient(
            [SESSION_ERROR],
            access_token="Bearer a",
            refresh_token="r",
            refresh_result="fail",
        )
        with self.assertRaises(AuthRequired):
            client.request("GET", "/x")

    def test_no_retry_for_write_requests(self):
        client = FakeClient([SESSION_ERROR], access_token="Bearer a", refresh_token="r")
        with self.assertRaises(AuthRequired):
            client.request("POST", "/x", read_only=False)
        self.assertEqual(client.refresh_calls, 0)


class AuthStyleTest(unittest.TestCase):
    def test_learns_raw_style_without_refresh(self):
        client = FakeClient([SESSION_ERROR, SUCCESS], access_token="Bearer a")
        self.assertEqual(client.request("GET", "/x"), {"value": 1})
        self.assertEqual(client.styles, ["auto", "raw"])
        self.assertEqual(client.auth_style, "raw")

    def test_raw_style_strips_bearer_prefix(self):
        client = SmartMedClient(access_token="Bearer a", auth_style="raw")
        self.assertEqual(client._auth_value(), "a")

    def test_bearer_style_adds_prefix(self):
        client = SmartMedClient(access_token="a", auth_style="bearer")
        self.assertEqual(client._auth_value(), "Bearer a")


if __name__ == "__main__":
    unittest.main()
