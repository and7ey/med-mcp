"""Нормализация DTO MCP-сервера на обезличенных фикстурах."""

from __future__ import annotations

import unittest

from emias_mcp.service import (
    _analyze,
    _appointment,
    _patient_list,
    _referral,
    _vaccination,
    _visit,
)


class PatientDtoTest(unittest.TestCase):
    def test_profile_and_relatives(self):
        payload = {
            "profile": {
                "profileId": "uuid-1",
                "lastName": "Иванов",
                "firstName": "Иван",
                "middleName": "Иванович",
                "birthDate": "1990-01-02T00:00:00",
            },
            "relatives": [
                {"profileId": "uuid-2", "lastName": "Иванова", "firstName": "Анна",
                 "permissionType": "CHILD"}
            ],
        }
        patients = _patient_list(payload)
        self.assertEqual([p["patient_id"] for p in patients], ["uuid-1", "uuid-2"])
        self.assertEqual(patients[0]["full_name"], "Иванов Иван Иванович")
        self.assertEqual(patients[0]["permission_type"], "PERSONAL")
        self.assertEqual(patients[1]["permission_type"], "CHILD")

    def test_empty_payload(self):
        self.assertEqual(_patient_list(None), [])


class DocumentDtoTest(unittest.TestCase):
    def test_visit_v4_wrapped(self):
        item = {
            "documentType": "INSPECTION",
            "date": "2024-05-01T10:00:00+03:00",
            "document": {
                "documentId": "doc-1",
                "date": "2024-05-01T10:00:00+03:00",
                "title": "Осмотр",
                "doctorSpecialization": "Терапевт",
                "doctorName": "Иванов И. И.",
                "organisation": "ГП № 1",
            },
        }
        visit = _visit(item)
        self.assertEqual(visit["document_id"], "doc-1")
        self.assertEqual(visit["date"], "2024-05-01")
        self.assertEqual(visit["time"], "10:00")
        self.assertEqual(visit["speciality"], "Терапевт")

    def test_visit_v3_flat(self):
        visit = _visit({"documentId": "d", "date": "2024-05-01T09:30:00",
                        "title": "Приём", "doctorName": "Петров П. П."})
        self.assertEqual(visit["time"], "09:30")
        self.assertEqual(visit["doctor"], "Петров П. П.")

    def test_analyze(self):
        analyze = _analyze({"documentId": "a", "date": "2024-04-01T08:00:00",
                            "code": "BLD", "title": "Кровь", "isCovid": True})
        self.assertEqual(analyze["code"], "BLD")
        self.assertTrue(analyze["is_covid"])

    def test_vaccination(self):
        item = {
            "documentId": "v", "dateVaccination": "2021-09-08",
            "vaccinationName": "Вакцина", "dosage": "0.5 мл",
            "infectionList": [{"infectionName": "COVID-19"}],
        }
        vacc = _vaccination(item)
        self.assertEqual(vacc["infections"], ["COVID-19"])
        self.assertEqual(vacc["date"], "2021-09-08")

    def test_appointment(self):
        item = {
            "id": 1, "startTime": "2026-11-11T15:48:00+03:00",
            "endTime": "2026-11-11T16:00:00+03:00",
            "lpuName": "ГП № 1",
            "doctor": {"name": "Иванов И. И.", "specialityName": "Терапевт"},
        }
        appt = _appointment(item)
        self.assertEqual(appt["time"], "15:48")
        self.assertEqual(appt["speciality"], "Терапевт")
        self.assertEqual(appt["doctor"], "Иванов И. И.")

    def test_referral(self):
        item = {"id": 5, "number": "123", "dateFrom": "2026-01-01",
                "type": "CONSULTATION", "moName": "ГП № 1",
                "isPurposeRequired": True}
        ref = _referral(item)
        self.assertEqual(ref["number"], "123")
        self.assertTrue(ref["purpose_required"])


if __name__ == "__main__":
    unittest.main()


class SurveyBuildTest(unittest.TestCase):
    def setUp(self):
        self.tags = [
            {
                "tagCode": 3,
                "tagTitle": "Головокружение",
                "syntheticTagCode": None,
                "questionsForTag": [
                    {
                        "code": 30,
                        "title": "Что провоцирует?",
                        "type": "MULTIPLE_CHOICE",
                        "stepNumber": 1,
                        "answers": [
                            {"code": 3001, "title": "Резкий подъём",
                             "nextQuestionCode": 3010},
                        ],
                    },
                    {
                        "code": 3010,
                        "title": "Как давно?",
                        "type": "TEXT_ANSWER",
                        "stepNumber": 2,
                        "answerToDoctor": "* Длится: <data>",
                    },
                ],
            }
        ]

    def test_branching_and_text(self):
        from emias_mcp.survey import build_complaints

        survey, errors = build_complaints(
            self.tags,
            [{"tag_code": 3, "answers": [
                {"question_code": "30", "answer_code": "3001"},
                {"question_code": "3010", "text": "два дня"},
            ]}],
        )
        self.assertEqual(errors, [])
        answers = survey[0]["patientAnswer"]
        self.assertEqual(answers[0]["answerCode"], "3001")
        self.assertEqual(answers[1]["answerToDoctor"], "* Длится: два дня")

    def test_missing_answer_reports_error(self):
        from emias_mcp.survey import build_complaints

        _survey, errors = build_complaints(
            self.tags, [{"tag_code": 3, "answers": []}]
        )
        self.assertTrue(any("вопрос 30" in error for error in errors))

    def test_unknown_tag_reports_error(self):
        from emias_mcp.survey import build_complaints

        _survey, errors = build_complaints(self.tags, [{"tag_code": 999, "answers": []}])
        self.assertTrue(any("999" in error for error in errors))


class PendingTokenTest(unittest.TestCase):
    def setUp(self):
        import os
        import tempfile
        from emias_mcp.service import EmiasService

        self.tmp = tempfile.TemporaryDirectory()
        os.environ["EMIAS_MCP_DISABLE_KEYCHAIN"] = "1"
        os.environ["EMIAS_MCP_IMPORT_CLI"] = "0"
        self.service = EmiasService(self.tmp.name)

    def tearDown(self):
        import os

        os.environ.pop("EMIAS_MCP_DISABLE_KEYCHAIN", None)
        os.environ.pop("EMIAS_MCP_IMPORT_CLI", None)
        self.tmp.cleanup()

    def test_token_is_single_use(self):
        token = self.service._put_pending("booking", {"patient_id": "p"})
        self.assertEqual(self.service._peek_pending(token, "booking")["details"]["patient_id"], "p")
        self.service._take_pending(token, "booking")
        from emias_mcp.errors import McpError

        with self.assertRaises(McpError):
            self.service._take_pending(token, "booking")

    def test_wrong_kind_rejected(self):
        from emias_mcp.errors import McpError

        token = self.service._put_pending("booking", {})
        with self.assertRaises(McpError):
            self.service._peek_pending(token, "cancel")

    def test_expired_token_rejected(self):
        import time

        from emias_mcp.errors import McpError

        token = self.service._put_pending("booking", {})
        state = self.service.state.load()
        state["pending"][token]["expires_at"] = int(time.time()) - 1
        self.service.state.save(state)
        with self.assertRaises(McpError):
            self.service._peek_pending(token, "booking")
