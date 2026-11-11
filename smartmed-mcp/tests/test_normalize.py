"""Нормализация DTO MCP-сервера на обезличенных фикстурах."""

from __future__ import annotations

import unittest

from smartmed_mcp.service import (
    _appointment,
    _document,
    _patient,
    _patient_list,
    _prescription,
    _profile,
    _referral,
)


class ProfileDtoTest(unittest.TestCase):
    def test_profile_fields_and_phone_mask(self):
        profile = _profile(
            {
                "id": 123,
                "lastName": "Иванов",
                "firstName": "Иван",
                "patronymic": "Иванович",
                "birthday": "1990-01-02T00:00:00",
                "phone": "79990001122",
                "email": "user@example.org",
            }
        )
        self.assertEqual(profile["user_id"], "123")
        self.assertEqual(profile["middle_name"], "Иванович")
        self.assertEqual(profile["birthday"], "1990-01-02")
        self.assertEqual(profile["phone_masked"], "+7 *** *** ** 22")
        self.assertNotIn("email", profile)

    def test_empty_profile(self):
        self.assertEqual(_profile({})["user_id"], None)


class PatientDtoTest(unittest.TestCase):
    def test_nested_user_details(self):
        patient = _patient(
            {
                "isDefault": True,
                "userDetails": {
                    "userId": 42,
                    "lastName": "Иванов",
                    "firstName": "Иван",
                    "patronymic": "Иванович",
                    "birthday": "2015-05-09T00:00:00Z",
                    "ageLevel": 1,
                    "isClient": True,
                },
            }
        )
        self.assertEqual(patient["patient_id"], "42")
        self.assertEqual(patient["full_name"], "Иванов Иван Иванович")
        self.assertEqual(patient["age_level"], "child")
        self.assertTrue(patient["is_default"])

    def test_list_skips_items_without_id(self):
        items = _patient_list([{"userId": 1}, {"userDetails": {"firstName": "X"}}])
        self.assertEqual([item["patient_id"] for item in items], ["1"])

    def test_flat_item(self):
        patient = _patient({"userId": 7, "fio": "Петров Пётр", "ageLevel": 2})
        self.assertEqual(patient["full_name"], "Петров Пётр")
        self.assertEqual(patient["age_level"], "adult")


class AppointmentDtoTest(unittest.TestCase):
    def test_full_item(self):
        appointment = _appointment(
            {
                "appointment_date": "2026-12-01T09:30:00",
                "type": "offline_reception",
                "calendar_state": "upcoming",
                "user": {"user_id": 42},
                "reception": {
                    "id": "777",
                    "clinic": {"name": "Клиника"},
                    "specialty": {"name": "Терапевт"},
                    "doctor": {"nom": "Иванов", "prenom": "Иван"},
                },
            }
        )
        self.assertEqual(appointment["appointment_id"], "777")
        self.assertEqual(appointment["date"], "2026-12-01")
        self.assertEqual(appointment["time"], "09:30")
        self.assertEqual(appointment["doctor"], "Иванов Иван")
        self.assertEqual(appointment["clinic"], "Клиника")
        self.assertEqual(appointment["state_label"], "Предстоящий")
        self.assertEqual(appointment["patient_id"], "42")

    def test_empty_item(self):
        appointment = _appointment({})
        self.assertIsNone(appointment["appointment_id"])
        self.assertIsNone(appointment["date"])


class DocumentDtoTest(unittest.TestCase):
    def test_document(self):
        document = _document(
            {
                "file_id": "f-1",
                "file_name": "Анализ.pdf",
                "created_at": "2026-01-02T10:11:12",
                "doctor_name": "Петров П. П.",
                "extension": ".pdf",
                "size": 1000,
                "is_deletion_allowed": True,
                "tags": [{"name": "Кровь"}],
            }
        )
        self.assertEqual(document["file_id"], "f-1")
        self.assertEqual(document["created_at"], "2026-01-02")
        self.assertEqual(document["time"], "10:11")
        self.assertEqual(document["tags"], ["Кровь"])
        self.assertTrue(document["can_delete"])


class ReferralDtoTest(unittest.TestCase):
    def test_referral(self):
        referral = _referral(
            {
                "referralId": "r-1",
                "name": "МРТ",
                "type": "laboratory",
                "doctor": "Иванов И. И.",
                "state": {"code": "ok", "name": "Действует"},
                "isActive": True,
                "bookingAvailability": True,
            }
        )
        self.assertEqual(referral["referral_id"], "r-1")
        self.assertEqual(referral["state"], "Действует")
        self.assertTrue(referral["is_active"])
        self.assertTrue(referral["can_book"])


class PrescriptionDtoTest(unittest.TestCase):
    def test_prescription(self):
        prescription = _prescription(
            {
                "id": "p-1",
                "appointmentId": "a-1",
                "doctor": {"lastName": "Иванов", "firstName": "Иван", "position": "Терапевт"},
                "medications": [
                    {
                        "medicine": {"name": "Препарат", "code": "Препарат 10 мг"},
                        "medicationForm": {"name": "Таблетки"},
                        "dosage": {"text": "10 мг"},
                        "isRecipe": True,
                        "isActive": True,
                        "intakeSchedule": {"text": "1 раз в день"},
                    }
                ],
            }
        )
        self.assertEqual(prescription["prescription_id"], "p-1")
        self.assertEqual(prescription["doctor"], "Иванов Иван")
        self.assertEqual(prescription["medications"][0]["form"], "Таблетки")
        self.assertTrue(prescription["medications"][0]["is_recipe"])
        self.assertEqual(prescription["medications"][0]["schedule"], "1 раз в день")


if __name__ == "__main__":
    unittest.main()
