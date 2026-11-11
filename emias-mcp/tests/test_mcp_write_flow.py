"""Сценарии write-инструментов на фейковом клиенте: без реальных записей."""

from __future__ import annotations

import os
import tempfile
import unittest

from emias.errors import EmiasError
from emias_mcp.service import EmiasService


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.cancelled = False
        self.fail_confirm: EmiasError | None = None
        self.appointments_left: list[dict] = [{"id": 7}]

    def request(self, method, endpoint, **kwargs):
        self.calls.append(("request", endpoint))
        if endpoint == "/interview/create-dialog":
            return {"code": "hash", "complaintId": 11}
        if endpoint.endswith("/save-answers"):
            return {}
        raise AssertionError(endpoint)

    def reserve(self, ehr, slot, **kwargs):
        self.calls.append(("reserve", str(slot.resource_id)))
        return {"reservationId": 5}

    def confirm_reservation(self, ehr, reservation_id, slot, **kwargs):
        self.calls.append(("confirm", reservation_id))
        if self.fail_confirm:
            raise self.fail_confirm
        return {"appointmentId": 7}

    def cancel_reservation(self, ehr, reservation_id):
        self.cancelled = True
        self.calls.append(("cancel_reservation", reservation_id))

    def appointments(self, ehr):
        return {"appointments": self.appointments_left}

    def cancel_appointment(self, ehr, appointment_id):
        self.calls.append(("cancel_appointment", appointment_id))
        self.appointments_left = []


class WriteFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["EMIAS_MCP_DISABLE_KEYCHAIN"] = "1"
        os.environ["EMIAS_MCP_IMPORT_CLI"] = "0"
        self.service = EmiasService(self.tmp.name)

    def tearDown(self):
        os.environ.pop("EMIAS_MCP_DISABLE_KEYCHAIN", None)
        os.environ.pop("EMIAS_MCP_IMPORT_CLI", None)
        self.tmp.cleanup()

    def _pending(self, needs_complaints=False):
        details = {
            "patient_id": "patient",
            "resource_id": "100",
            "complex_resource_id": "200",
            "start_time": "2026-11-24T16:48:00+03:00",
            "end_time": "2026-11-24T17:00:00+03:00",
            "speciality_id": "2",
            "purpose_id": "71",
            "needs_complaints": needs_complaints,
        }
        return self.service._put_pending("booking", details)

    def test_booked_and_verified(self):
        fake = FakeClient()
        self.service._client = lambda tokens=None: fake
        token = self._pending()
        result = self.service.booking_execute(token)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "booked")
        self.assertTrue(result["verified"])
        self.assertEqual(result["appointment_id"], "7")
        self.assertFalse(fake.cancelled)

    def test_definite_confirm_failure_cancels_reservation(self):
        fake = FakeClient()
        fake.fail_confirm = EmiasError("no", code="NoComplaint", status=422)
        self.service._client = lambda tokens=None: fake
        result = self.service.booking_execute(self._pending())
        self.assertEqual(result["status"], "not_booked")
        self.assertEqual(result["stage"], "confirm")
        self.assertTrue(fake.cancelled)

    def test_network_failure_is_unknown_and_not_retried(self):
        fake = FakeClient()
        fake.fail_confirm = EmiasError("timeout")
        self.service._client = lambda tokens=None: fake
        result = self.service.booking_execute(self._pending())
        self.assertEqual(result["status"], "unknown_outcome")
        self.assertFalse(fake.cancelled)
        self.assertEqual([call for call in fake.calls if call[0] == "confirm"], [("confirm", 5)])

    def test_complaints_required_does_not_consume_token(self):
        self.service._client = lambda tokens=None: FakeClient()
        token = self._pending(needs_complaints=True)
        from emias_mcp.errors import McpError

        with self.assertRaises(McpError) as ctx:
            self.service.booking_execute(token)
        self.assertEqual(ctx.exception.code, "complaints_required")
        # Токен всё ещё можно использовать после добавления жалоб.
        self.service._peek_pending(token, "booking")

    def test_cancel_flow(self):
        fake = FakeClient()
        self.service._client = lambda tokens=None: fake
        token = self.service._put_pending(
            "cancel", {"patient_id": "patient", "appointment_id": "7"}
        )
        result = self.service.cancel_execute(token)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "cancelled")
        self.assertIn(("cancel_appointment", "7"), fake.calls)


if __name__ == "__main__":
    unittest.main()
