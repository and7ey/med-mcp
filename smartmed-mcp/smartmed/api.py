"""Методы личного кабинета SmartMed (read-only).

Пути и тела запросов восстановлены по официальному веб-кабинету
https://smartmed.pro (patient@0.21.0): ``/personal/api`` для пользовательских
методов и ``/personal/proxyapi`` для медицинских.
"""

from __future__ import annotations

from typing import Any

from .client import SmartMedClient

#: Типы приёмов, которые кабинет передаёт в фильтре медкарты.
RECEPTION_TYPES = {
    "offline": "offline_reception",
    "online": "online_reception",
    "diagnostic": "diagnostic_reception",
    "homecall": "homecall",
}
#: Кабинет запрашивает медкарту ровно этими тремя типами; вызов на дом —
#: отдельный раздел и в общий фильтр не входит.
ALL_RECEPTION_TYPES = [
    RECEPTION_TYPES["offline"],
    RECEPTION_TYPES["online"],
    RECEPTION_TYPES["diagnostic"],
]

#: Состояния приёма в календаре медкарты.
CALENDAR_STATES = ("current", "upcoming", "done", "canceled")

#: Область действия назначений в веб-кабинете.
PRESCRIPTION_SCOPE = {"active": 1, "archive": 2, "all": 3}

MAX_PAGE_SIZE = 100


def _page_size(value: int) -> int:
    try:
        return max(1, min(int(value), MAX_PAGE_SIZE))
    except (TypeError, ValueError):
        return 20


def _patient_ref(patient_id: str) -> Any:
    """Кабинет передаёт userId числом; строку из MCP приводим к int."""
    text = str(patient_id).strip()
    return int(text) if text.isdigit() else text


class SmartMedApi:
    def __init__(self, client: SmartMedClient) -> None:
        self.client = client

    # ------------------------------------------------------------- профиль

    def info(self) -> dict[str, Any]:
        data = self.client.request("GET", "/users/info/v3")
        return data if isinstance(data, dict) else {}

    def patients(self) -> list[dict[str, Any]]:
        data = self.client.request("GET", "/users/getpatients")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            for key in ("items", "patients", "list"):
                items = data.get(key)
                if isinstance(items, list):
                    return [item for item in items if isinstance(item, dict)]
        return []

    # ------------------------------------------------------------- приёмы

    def medical_card(
        self,
        *,
        patient_id: str,
        types: list[str] | None = None,
        calendar_states: list[str] | None = None,
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        body = {
            "filters": {
                "types": types or ALL_RECEPTION_TYPES,
                "calendar_states": calendar_states or ["current", "upcoming"],
                "patients": [_patient_ref(patient_id)],
            },
            "paginate": {"page": max(1, page), "size": max(1, min(size, MAX_PAGE_SIZE))},
        }
        data = self.client.request(
            "POST",
            "/reception-line/v3/receptions/medicalcard/list",
            proxy=True,
            json_body=body,
            patient_id=patient_id,
        )
        return data if isinstance(data, dict) else {}

    def reception(self, reception_id: str, *, patient_id: str | None = None) -> dict[str, Any]:
        data = self.client.request(
            "GET",
            f"/core-reception/v3/receptions/{reception_id}",
            proxy=True,
            patient_id=patient_id,
        )
        return data if isinstance(data, dict) else {}

    # --------------------------------------------------------- документы

    def documents(
        self,
        *,
        patient_id: str,
        page: int = 0,
        size: int = 20,
        search: str | None = None,
        tag_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": max(0, page),
            "size": max(1, min(size, MAX_PAGE_SIZE)),
        }
        if search:
            params["search"] = search
        if tag_id:
            params["tag_id"] = tag_id
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to
        data = self.client.request(
            "GET",
            "/documents_manager/v1/documents/",
            proxy=True,
            params=params,
            patient_id=patient_id,
        )
        return data if isinstance(data, dict) else {}

    def document_tags(self, *, patient_id: str) -> list[dict[str, Any]]:
        data = self.client.request(
            "GET",
            "/documents_manager/v1/tags/",
            proxy=True,
            patient_id=patient_id,
        )
        if isinstance(data, dict):
            items = data.get("items")
            if isinstance(items, list):
                return [item for item in items if isinstance(item, dict)]
        return []

    def document_file(self, file_id: str, *, patient_id: str) -> bytes:
        return self.client.request_bytes(
            f"/file/storage/v2/{file_id}",
            patient_id=patient_id,
        )

    # -------------------------------------------------------- направления

    def referrals(
        self,
        *,
        patient_id: str,
        page: int = 1,
        size: int = 25,
        search: str | None = None,
        is_active: bool | None = None,
        sort: str = "date_desc",
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "page": max(1, page),
            "size": max(1, min(size, MAX_PAGE_SIZE)),
            "sort": "date_asc" if sort == "date_asc" else "date_desc",
        }
        if search:
            params["search"] = search
        if is_active is not None:
            params["isActive"] = bool(is_active)
        if date_from:
            params["dateFrom"] = date_from
        if date_to:
            params["dateTo"] = date_to
        data = self.client.request(
            "GET",
            "/referral/v2/referrals",
            proxy=True,
            params=params,
            patient_id=patient_id,
        )
        return data if isinstance(data, dict) else {}

    # ---------------------------------------------------------- назначения

    def prescriptions(
        self,
        *,
        patient_id: str,
        scope: str = "active",
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        body = {"scope": PRESCRIPTION_SCOPE.get(scope, PRESCRIPTION_SCOPE["active"])}
        params = {"page": max(1, page), "size": max(1, min(size, MAX_PAGE_SIZE))}
        data = self.client.request(
            "POST",
            "/connector/web/purpose/",
            proxy=True,
            params=params,
            json_body=body,
            patient_id=patient_id,
        )
        return data if isinstance(data, dict) else {}

    # ------------------------------------------------------- запись к врачу

    def cities(self, *, page: int = 1, size: int = 50) -> dict[str, Any]:
        data = self.client.request(
            "GET",
            "/clinics/clinics/v1/cities/",
            proxy=True,
            params={"page": max(1, page), "size": _page_size(size), "with_clinic": True, "context": "reception"},
        )
        return data if isinstance(data, dict) else {}

    def user_city(self) -> dict[str, Any]:
        data = self.client.request(
            "GET",
            "/clinics/clinics/v1/cities/city/user",
            proxy=True,
            params={"with_clinic": True},
        )
        return data if isinstance(data, dict) else {}

    def specialties(
        self,
        *,
        city_id: str,
        search: str | None = None,
        page: int = 1,
        size: int = 50,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {"city_id": _patient_ref(city_id), "online": False}
        if search:
            filters["search"] = search
        data = self.client.request(
            "POST",
            "/core-reception/v2/specialties/search",
            proxy=True,
            json_body={
                "paginate": {"page": max(1, page), "size": _page_size(size)},
                "filters": filters,
            },
        )
        return data if isinstance(data, dict) else {}

    def clinics_by_specialty(
        self, *, city_id: str, specialty_ids: list[str]
    ) -> list[dict[str, Any]]:
        data = self.client.request(
            "POST",
            "/core-reception/v2/clinic-specialties",
            proxy=True,
            json_body={
                "city_id": _patient_ref(city_id),
                "specialty_ids": [_patient_ref(item) for item in specialty_ids],
            },
        )
        if isinstance(data, list):
            items = data
        elif isinstance(data, dict):
            items = data.get("items") or data.get("collection") or []
        else:
            items = []
        return [item for item in items if isinstance(item, dict)]

    def doctors(
        self,
        *,
        city_id: str,
        specialty_ids: list[str] | None = None,
        clinic_ids: list[str] | None = None,
        search: str | None = None,
        online: bool = False,
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        filters: dict[str, Any] = {
            "city_id": _patient_ref(city_id),
            "online": bool(online),
        }
        if search:
            filters["search"] = search
        if specialty_ids:
            filters["specialty_ids"] = [_patient_ref(item) for item in specialty_ids]
        if clinic_ids:
            filters["clinic_ids"] = [_patient_ref(item) for item in clinic_ids]
        data = self.client.request(
            "POST",
            "/core-reception/v2/doctor-specialties/search",
            proxy=True,
            json_body={
                "paginate": {"page": max(1, page), "size": _page_size(size)},
                "filters": filters,
            },
            extra_headers={"PatientId": "-1"},
        )
        return data if isinstance(data, dict) else {}

    def available_dates(
        self,
        *,
        specialty_id: str,
        clinic_ids: list[str],
        patient_age_level: int | None = None,
        patient_id: str | None = None,
    ) -> list[dict[str, Any]]:
        body = {
            "clinic_ids": [_patient_ref(item) for item in clinic_ids],
            "specialties_ids": [_patient_ref(specialty_id)],
            "online": False,
        }
        headers: dict[str, str] = {}
        if patient_age_level:
            headers["PatientAgeLevel"] = str(patient_age_level)
        if patient_id:
            headers["PatientId"] = str(patient_id)
        data = self.client.request(
            "POST",
            "/core-reception/v2/calendar-schedules",
            proxy=True,
            json_body=body,
            extra_headers=headers,
        )
        if isinstance(data, dict):
            items = data.get("items") or data.get("data") or []
        elif isinstance(data, list):
            items = data
        else:
            items = []
        return [item for item in items if isinstance(item, dict)]

    def schedules(
        self,
        *,
        specialty_id: str,
        clinic_ids: list[str],
        date: str,
        page_token: str | None = None,
        size: int = 20,
        patient_age_level: int | None = None,
        patient_id: str | None = None,
    ) -> dict[str, Any]:
        filters = {
            "clinic_ids": [_patient_ref(item) for item in clinic_ids],
            "specialty_ids": [_patient_ref(specialty_id)],
            "online": False,
            "date_visit": str(date)[:10],
        }
        headers: dict[str, str] = {}
        if patient_age_level:
            headers["PatientAgeLevel"] = str(patient_age_level)
        if patient_id:
            headers["PatientId"] = str(patient_id)
        data = self.client.request(
            "POST",
            "/core-reception/v2/schedules",
            proxy=True,
            json_body={"filters": filters, "paginate": {"token": page_token, "size": _page_size(size)}},
            extra_headers=headers,
        )
        return data if isinstance(data, dict) else {}

    def slot_is_free(
        self, *, schedule_id: str, reception_type_id: str, start_utcz: str | int
    ) -> dict[str, Any]:
        data = self.client.request(
            "POST",
            "/core-reception/v1/slots/by-ids",
            proxy=True,
            json_body={
                "schedule_id": _patient_ref(schedule_id),
                "reception_type_id": _patient_ref(reception_type_id),
                "start_utcz": start_utcz,
            },
        )
        return data if isinstance(data, dict) else {}

    def make_appointment(
        self,
        *,
        clinic_code: str,
        schedule_id: str,
        reception_type_id: str,
        start_utcz: str | int,
        fm_clink_patient_id: str | None = None,
        referral_id: str | None = None,
        patient_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "clinic_code": clinic_code,
            "schedule_id": _patient_ref(schedule_id),
            "reception_type_id": _patient_ref(reception_type_id),
            "start_utcz": start_utcz,
            "source_client": "web",
        }
        if fm_clink_patient_id:
            body["fm_clink_patient_id"] = fm_clink_patient_id
        if referral_id:
            body["referral_id"] = referral_id
        data = self.client.request(
            "POST",
            "/core-reception/v3/receptions",
            proxy=True,
            json_body=body,
            patient_id=patient_id,
            read_only=False,
        )
        return data if isinstance(data, dict) else {}

    def reception(self, reception_id: str, *, patient_id: str | None = None) -> dict[str, Any]:
        data = self.client.request(
            "GET",
            f"/core-reception/v3/receptions/{reception_id}",
            proxy=True,
            patient_id=patient_id,
        )
        return data if isinstance(data, dict) else {}

    def cancel_reception(self, reception_id: str, *, patient_id: str | None = None) -> dict[str, Any]:
        data = self.client.request(
            "POST",
            "/core-reception/v2/receptions/cancel",
            proxy=True,
            json_body={"reception_id": reception_id},
            patient_id=patient_id,
            read_only=False,
        )
        return data if isinstance(data, dict) else {}
