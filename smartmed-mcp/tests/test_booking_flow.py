"""Сценарий записи и отмены на фейковом API (без реальных вызовов)."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from smartmed.errors import ApiError
from smartmed_mcp.errors import McpError
from smartmed_mcp.service import SmartMedService, _schedule_slots


class FakeApi:
    def __init__(self) -> None:
        self.slot_free = True
        self.make_calls = 0
        self.cancel_calls = 0
        self.make_error: Exception | None = None
        self.cancel_error: Exception | None = None
        self.status = "approved"
        self.canceled = False
        self.canceled_status = "canceled"
        self.receptions: dict[str, dict] = {}

    def patients(self):
        return [{"userDetails": {"userId": 42, "firstName": "Иван"}, "isDefault": True}]

    def slot_is_free(self, **kwargs):
        return {"isFree": self.slot_free}

    def make_appointment(self, **kwargs):
        self.make_calls += 1
        if self.make_error:
            raise self.make_error
        self.receptions["rec-1"] = self.reception("rec-1")
        return {"id": "rec-1"}

    def reception(self, reception_id, *, patient_id=None):
        if self.canceled:
            return self._raw(reception_id, self.canceled_status)
        return self._raw(reception_id, self.status)

    def cancel_reception(self, reception_id, *, patient_id=None):
        self.cancel_calls += 1
        if self.cancel_error:
            raise self.cancel_error
        self.canceled = True
        return self._raw(reception_id, self.canceled_status)

    @staticmethod
    def _raw(reception_id: str, status: str) -> dict:
        return {
            "id": reception_id,
            "status": {"name": status, "value": 1},
            "startLocal": "2026-12-01T10:30:00",
            "specialty": {"name": "Терапевт"},
            "doctor": {"nom": "Иванов", "prenom": "Иван"},
            "clinic": {"name": "Клиника", "address": "ул. Тестовая, 1"},
            "scheduleSlot": {"cabinetAddress": "каб. 5"},
        }


class BookingFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["SMARTMED_MCP_DISABLE_KEYCHAIN"] = "1"
        self.service = SmartMedService(self.tmp.name)
        self.service.tokens.save({"access_token": "a", "refresh_token": "r"})
        self.api = FakeApi()
        self.service._api = self.api
        self.sleep = mock.patch("smartmed_mcp.service.time.sleep", return_value=None)
        self.sleep.start()

    def tearDown(self):
        self.sleep.stop()
        os.environ.pop("SMARTMED_MCP_DISABLE_KEYCHAIN", None)
        self.tmp.cleanup()

    def _prepare(self, **overrides):
        args = dict(
            schedule_id="sch-1",
            reception_type_id="rt-1",
            start_utcz="1764570600",
            clinic_code="CL01",
            summary={"doctor": "Иванов Иван", "price": 1000},
        )
        args.update(overrides)
        return self.service.booking_prepare(**args)

    def test_prepare_does_not_book(self):
        result = self._prepare()
        self.assertTrue(result["ok"])
        self.assertTrue(result["confirm_token"])
        self.assertEqual(self.api.make_calls, 0)

    def test_slot_taken(self):
        self.api.slot_free = False
        with self.assertRaises(McpError) as ctx:
            self._prepare()
        self.assertEqual(ctx.exception.code, "slot_taken")
        self.assertEqual(self.api.make_calls, 0)

    def test_execute_books_once_and_token_is_single_use(self):
        token = self._prepare()["confirm_token"]
        result = self.service.booking_execute(token)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "approved")
        self.assertEqual(result["appointment"]["appointment_id"], "rec-1")
        self.assertEqual(self.api.make_calls, 1)
        with self.assertRaises(McpError) as ctx:
            self.service.booking_execute(token)
        self.assertEqual(ctx.exception.code, "bad_confirm_token")

    def test_network_error_is_unknown_and_not_retried(self):
        self.api.make_error = ApiError("network_error", "Сеть недоступна.", retryable=True)
        token = self._prepare()["confirm_token"]
        with self.assertRaises(McpError) as ctx:
            self.service.booking_execute(token)
        self.assertEqual(ctx.exception.extra["outcome"], "unknown")
        self.assertEqual(self.api.make_calls, 1)

    def test_server_rejection_is_rejected(self):
        self.api.make_error = McpError("AlreadyHaveReception", "Уже есть запись", http_status=409)
        token = self._prepare()["confirm_token"]
        with self.assertRaises(McpError) as ctx:
            self.service.booking_execute(token)
        self.assertEqual(ctx.exception.extra["outcome"], "rejected")
        self.assertEqual(self.api.make_calls, 1)

    def test_expired_token(self):
        token = self._prepare()["confirm_token"]
        data = self.service.state.load()
        data["pending"][token]["created_at"] -= 10_000
        self.service.state.save(data)
        with self.assertRaises(McpError) as ctx:
            self.service.booking_execute(token)
        self.assertEqual(ctx.exception.code, "confirm_token_expired")
        self.assertEqual(self.api.make_calls, 0)

    def test_cancel_flow_verifies_status(self):
        token = self._prepare()["confirm_token"]
        appointment_id = self.service.booking_execute(token)["appointment"]["appointment_id"]
        prepared = self.service.cancel_prepare(appointment_id)
        self.assertTrue(prepared["ok"])
        self.assertFalse(prepared.get("already_canceled"))
        result = self.service.cancel_execute(prepared["confirm_token"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "canceled")
        self.assertEqual(self.api.cancel_calls, 1)

    def test_cancel_accepts_canceled_by_client(self):
        token = self._prepare()["confirm_token"]
        appointment_id = self.service.booking_execute(token)["appointment"]["appointment_id"]
        self.api.canceled_status = "canceled_by_client"
        prepared = self.service.cancel_prepare(appointment_id)
        result = self.service.cancel_execute(prepared["confirm_token"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "canceled_by_client")

    def test_cancel_after_already_canceled_error(self):
        prepared = self.service.cancel_prepare("rec-1")
        self.assertFalse(prepared.get("already_canceled"))
        # Сервер ответил ошибкой отмены, но статус уже canceled — это успех.
        self.api.canceled = True
        self.api.cancel_error = McpError("AlreadyCancelledError", "Уже отменено", http_status=418)
        result = self.service.cancel_execute(prepared["confirm_token"])
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "canceled")


class ScheduleSlotsTest(unittest.TestCase):
    def test_flatten_schedule(self):
        schedule = {
            "id": "sch-1",
            "clinic": {"code": "CL01", "name": "Клиника", "address": "ул. 1"},
            "doctor": {"id": "doc-1", "nom": "Иванов", "prenom": "Иван"},
            "schedule_reception_types": [
                {
                    "reception_type": {
                        "id": "rt-1",
                        "web_name": "Первичный приём",
                        "price": {"price": 1500, "med_program_id": "mp-1"},
                        "specialty": {"name": "Терапевт"},
                    }
                }
            ],
            "slots": [
                {
                    "id": "slot-1",
                    "start_local": "2026-12-01T10:30:00",
                    "start_utcz": "1764570600",
                    "reception_type_id": "rt-1",
                    "cabinet_address": "каб. 5",
                }
            ],
        }
        slots = _schedule_slots(schedule)
        self.assertEqual(len(slots), 1)
        slot = slots[0]
        self.assertEqual(slot["schedule_id"], "sch-1")
        self.assertEqual(slot["clinic_code"], "CL01")
        self.assertEqual(slot["date"], "2026-12-01")
        self.assertEqual(slot["time"], "10:30")
        self.assertEqual(slot["price"], 1500)
        self.assertEqual(slot["med_program_id"], "mp-1")
        self.assertEqual(slot["doctor"], "Иванов Иван")


if __name__ == "__main__":
    unittest.main()
