"""Юнит-тесты без сети: python -m unittest discover tests"""

from __future__ import annotations

import unittest

from emias.api import parse_slots
from emias.auth import build_login_url, parse_auth_code
from emias.client import EmiasClient


class FakeResponse:
    def __init__(self, status_code: int, payload):
        self.status_code = status_code
        self._payload = payload
        self.content = b"x"
        self.text = ""

    def json(self):
        return self._payload


class AuthHelpersTest(unittest.TestCase):
    def test_login_url(self):
        url = build_login_url()
        self.assertIn("client_id=lk.emias.mos.ru", url)
        self.assertIn("redirect_uri=https%3A%2F%2Flk.emias.mos.ru%2Fauth", url)
        self.assertIn("response_type=code", url)

    def test_parse_code_from_url(self):
        self.assertEqual(
            parse_auth_code("https://lk.emias.mos.ru/auth?code=ABC&state=1"), "ABC"
        )

    def test_parse_raw_code(self):
        self.assertEqual(parse_auth_code("ABC123"), "ABC123")

    def test_parse_code_error(self):
        with self.assertRaises(ValueError):
            parse_auth_code("https://lk.emias.mos.ru/auth?state=1")


class SlotsTest(unittest.TestCase):
    def test_flat_slots(self):
        payload = {
            "data": {
                "availableResources": [
                    {
                        "id": 1,
                        "muName": "ГП №5",
                        "schedule": [
                            {
                                "date": "2026-11-12",
                                "slots": [
                                    {
                                        "startTime": "2026-11-12T09:00:00",
                                        "endTime": "2026-11-12T09:20:00",
                                        "isBusy": False,
                                    },
                                    {
                                        "startTime": "2026-11-12T09:20:00",
                                        "endTime": "2026-11-12T09:40:00",
                                        "isBusy": True,
                                    },
                                ],
                            }
                        ],
                    }
                ]
            }
        }
        slots = parse_slots(payload)
        self.assertEqual(len(slots), 1)
        self.assertEqual(slots[0].resource_id, 1)
        self.assertEqual(slots[0].date, "2026-11-12")

    def test_complex_slots(self):
        payload = {
            "availableResources": [
                {
                    "id": 2,
                    "schedule": [
                        {
                            "date": "2026-11-13",
                            "complexResource": [{"id": 77, "room": {"number": "12"}}],
                            "complexResourceSchedule": [
                                {
                                    "complexResourceId": 77,
                                    "slots": [
                                        {
                                            "startTime": "2026-11-13T10:00:00",
                                            "endTime": "2026-11-13T10:30:00",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                }
            ]
        }
        slots = parse_slots(payload)
        self.assertEqual(slots[0].complex_resource_id, 77)
        self.assertEqual(slots[0].room, "12")


class PathTest(unittest.TestCase):
    def setUp(self):
        self.client = EmiasClient(session_path="/tmp/emias-test-session.json")
        self.client.session.access_token = "token"
        self.calls = []

        def fake_request(method, url, params=None, json=None, headers=None, timeout=None):
            self.calls.append({"method": method, "url": url, "params": params, "headers": headers})
            return FakeResponse(200, {"ok": True})

        self.client.http.request = fake_request  # type: ignore[assignment]

    def test_version_prefix(self):
        self.client.request("GET", "/documents/sections", query={"ehrId": "1", "type": "X"})
        call = self.calls[-1]
        self.assertEqual(
            call["url"], "https://lk.emias.mos.ru/api/1/documents/sections"
        )
        self.assertEqual(call["headers"]["X-Access-JWT"], "token")

    def test_custom_version(self):
        self.client.request("GET", "/receipt", version="/2", query={"ehrId": "1"})
        self.assertEqual(self.calls[-1]["url"], "https://lk.emias.mos.ru/api/2/receipt")

    def test_array_query(self):
        self.client.request("GET", "/x", query={"testCodes": ["A", "B"]})
        self.assertEqual(self.calls[-1]["params"], [("testCodes", "A"), ("testCodes", "B")])


class SessionImportTest(unittest.TestCase):
    def test_pick_from_localstorage_dump(self):
        from emias.cli import _pick

        dump = {
            "patient.web.v2.accessToken": "acc",
            "patient.web.v2.refreshToken": "ref",
            "patient.web.v2.isLoggedIn": "isLoggedIn",
        }
        self.assertEqual(_pick(dump, "access_token", "accessToken", "patient.web.v2.accessToken"), "acc")
        self.assertEqual(_pick(dump, "refresh_token", "patient.web.v2.refreshToken"), "ref")
        self.assertIsNone(_pick(dump, "absent"))


class InterviewTest(unittest.TestCase):
    def test_build_tag_survey_follows_branch(self):
        from emias.interview import build_tag_survey

        tag = {
            "tagCode": 3,
            "tagTitle": "Головокружение",
            "questionsForTag": [
                {
                    "code": 30,
                    "title": "Что провоцирует?",
                    "type": "MULTIPLE_CHOICE",
                    "stepNumber": 1,
                    "answers": [
                        {"code": 3001, "title": "Быстрый подъём", "nextQuestionCode": 3010}
                    ],
                },
                {
                    "code": 3010,
                    "title": "Как давно?",
                    "type": "TEXT_ANSWER",
                    "stepNumber": 2,
                    "answerToDoctor": "* Головокружение длится: <data>",
                },
            ],
        }
        answers = iter(["1", "два дня"])
        lines: list[str] = []
        entry = build_tag_survey(
            tag,
            input_fn=lambda prompt: next(answers),
            out=lines.append,
        )
        self.assertEqual(entry["tagCode"], 3)
        self.assertEqual(len(entry["patientAnswer"]), 2)
        first, second = entry["patientAnswer"]
        self.assertEqual(first["answerCode"], "3001")
        self.assertEqual(first["answerTitle"], "Быстрый подъём")
        self.assertEqual(second["answerInputText"], "два дня")
        self.assertEqual(second["answerToDoctor"], "* Головокружение длится: два дня")

    def test_unique_tags_drops_duplicates(self):
        from emias.interview import unique_tags

        tags = [
            {"tagCode": 5, "tagTitle": "A", "syntheticTagCode": None},
            {"tagCode": 5, "tagTitle": "A", "syntheticTagCode": 999},
            {"tagCode": 6, "tagTitle": "B"},
        ]
        self.assertEqual([t["tagCode"] for t in unique_tags(tags)], [5, 6])


if __name__ == "__main__":
    unittest.main()
