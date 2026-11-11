"""Высокоуровневые read-only операции MCP-сервера SmartMed.

Инструменты возвращают только нужные поля: без токенов, телефона, сырых
ответов API, URL с query и служебных идентификаторов, которых нет в DTO.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from smartmed.api import ALL_RECEPTION_TYPES, SmartMedApi
from smartmed.client import SmartMedClient
from smartmed.errors import ApiError
from smartmed.errors import AuthRequired as ClientAuthRequired

from .errors import AuthRequired, McpError
from .login import BrowserBootstrap
from .safe_log import SafeLog
from .state import StateStore, TokenStore, resolve_state_dir

MAX_LIMIT = 100
SESSION_TTL_SEC = 900
DOCUMENTS_DIR = "documents"
PENDING_TTL_SEC = 300
BOOKING_POLL_SEC = 3
BOOKING_POLL_TRIES = 5

_TYPE_LABELS = {
    "offline_reception": "Приём в клинике",
    "online_reception": "Онлайн-консультация",
    "diagnostic_reception": "Диагностика",
    "homecall": "Вызов на дом",
}

_STATE_LABELS = {
    "current": "Текущий",
    "upcoming": "Предстоящий",
    "done": "Завершён",
    "canceled": "Отменён",
}

_AGE_LEVELS = {1: "child", 2: "adult", 3: "any"}


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _date(value: Any) -> str | None:
    text = _text(value)
    return text[:10] if text else None


def _clock(value: Any) -> str | None:
    text = _text(value)
    return text[11:16] if text and len(text) >= 16 else None


def _clamp(limit: Any, default: int = 20) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        value = default
    return max(1, min(value, MAX_LIMIT))


def _is_canceled(status: Any) -> bool:
    text = str(status or "").strip().lower()
    return text == "canceled" or text.startswith("canceled_")


def _items(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("list", "items", "data"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _mask_phone(value: Any) -> str | None:
    digits = re.sub(r"\D", "", str(value or ""))
    if len(digits) < 4:
        return None
    tail = digits[-2:]
    return f"+{digits[0]} *** *** ** {tail}"


def _person_name(*parts: Any) -> str | None:
    text = " ".join(part for part in (str(p).strip() for p in parts if p) if part)
    return text or None


# --------------------------------------------------------------------------
# Нормализация DTO
# --------------------------------------------------------------------------


def _profile(data: dict[str, Any]) -> dict[str, Any]:
    data = data if isinstance(data, dict) else {}
    return {
        "user_id": _text(_first(data, "id", "userId", "user_id")),
        "last_name": _text(_first(data, "lastName", "last_name")),
        "first_name": _text(_first(data, "firstName", "first_name")),
        "middle_name": _text(_first(data, "patronymic", "middleName", "middle_name")),
        "birthday": _date(_first(data, "birthday", "birthDate")),
        "phone_masked": _mask_phone(_first(data, "phone")),
    }


def _patient(item: dict[str, Any]) -> dict[str, Any]:
    details = item.get("userDetails") if isinstance(item.get("userDetails"), dict) else item
    user_id = _first(details, "userId", "id", "user_id")
    full_name = _text(_first(details, "fio", "fullName")) or _person_name(
        _first(details, "lastName"),
        _first(details, "firstName"),
        _first(details, "patronymic"),
    )
    age_level = _first(details, "ageLevel")
    if isinstance(age_level, int):
        age_level = _AGE_LEVELS.get(age_level)
    return {
        "patient_id": _text(user_id),
        "full_name": full_name,
        "first_name": _text(_first(details, "firstName")),
        "last_name": _text(_first(details, "lastName")),
        "middle_name": _text(_first(details, "patronymic", "middleName")),
        "birthday": _date(_first(details, "birthday", "birthDate")),
        "age_level": _text(age_level),
        "is_default": bool(item.get("isDefault", details.get("isDefault", False))),
        "is_client": bool(_first(details, "isClient", "is_client")),
    }


def _patient_list(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in items:
        patient = _patient(item)
        if patient["patient_id"]:
            result.append(patient)
    return result


def _appointment(item: dict[str, Any]) -> dict[str, Any]:
    reception = item.get("reception") if isinstance(item.get("reception"), dict) else {}
    clinic = reception.get("clinic") if isinstance(reception.get("clinic"), dict) else {}
    specialty = reception.get("specialty") if isinstance(reception.get("specialty"), dict) else {}
    doctor = reception.get("doctor") if isinstance(reception.get("doctor"), dict) else {}
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    appointment_date = _first(item, "appointment_date", "date")
    cabinet = _first(reception, "cabinet", "office")
    if isinstance(cabinet, dict):
        cabinet = _first(cabinet, "name", "number")
    return {
        "appointment_id": _text(_first(reception, "id", "reception_id")),
        "date": _date(appointment_date),
        "time": _clock(appointment_date),
        "type": _text(item.get("type")),
        "type_label": _TYPE_LABELS.get(str(item.get("type") or ""), None),
        "state": _text(item.get("calendar_state")),
        "state_label": _STATE_LABELS.get(str(item.get("calendar_state") or ""), None),
        "specialty": _text(specialty.get("name")),
        "doctor": _person_name(doctor.get("nom"), doctor.get("prenom")),
        "clinic": _text(clinic.get("name")),
        "cabinet": _text(cabinet),
        "patient_id": _text(_first(user, "user_id", "userId")),
    }


def _document(item: dict[str, Any]) -> dict[str, Any]:
    tags = item.get("tags") if isinstance(item.get("tags"), list) else []
    return {
        "file_id": _text(_first(item, "file_id", "fileId", "id")),
        "name": _text(_first(item, "file_name", "name")),
        "created_at": _date(_first(item, "created_at", "createdAt")),
        "time": _clock(_first(item, "created_at", "createdAt")),
        "doctor": _text(_first(item, "doctor_name", "doctorName")),
        "extension": _text(item.get("extension")),
        "size": item.get("size") if isinstance(item.get("size"), int) else None,
        "tags": [
            _text(tag.get("name"))
            for tag in tags
            if isinstance(tag, dict) and _text(tag.get("name"))
        ],
        "can_delete": bool(item.get("is_deletion_allowed", False)),
    }


def _referral(item: dict[str, Any]) -> dict[str, Any]:
    state = item.get("state") if isinstance(item.get("state"), dict) else {}
    return {
        "referral_id": _text(_first(item, "referralId", "referral_id", "id")),
        "name": _text(_first(item, "name", "title")),
        "type": _text(item.get("type")),
        "doctor": _text(_first(item, "doctor")),
        "state": _text(_first(state, "name", "code")) or _text(item.get("referralState")),
        "is_active": bool(item.get("isActive", False)),
        "can_book": bool(item.get("bookingAvailability", False)),
    }


def _prescription(item: dict[str, Any]) -> dict[str, Any]:
    doctor = item.get("doctor") if isinstance(item.get("doctor"), dict) else {}
    medications = item.get("medications") if isinstance(item.get("medications"), list) else []
    result_meds = []
    for med in medications:
        if not isinstance(med, dict):
            continue
        medicine = med.get("medicine") if isinstance(med.get("medicine"), dict) else {}
        form = med.get("medicationForm") if isinstance(med.get("medicationForm"), dict) else {}
        dosage = med.get("dosage") if isinstance(med.get("dosage"), dict) else {}
        schedule = med.get("intakeSchedule") if isinstance(med.get("intakeSchedule"), dict) else {}
        result_meds.append(
            {
                "name": _text(_first(medicine, "name", "code")),
                "code": _text(medicine.get("code")),
                "latin_name": _text(medicine.get("latinName")),
                "form": _text(form.get("name")),
                "dosage": _text(_first(dosage, "text", "amount")),
                "is_recipe": bool(med.get("isRecipe", False)),
                "is_active": bool(med.get("isActive", False)),
                "schedule": _text(schedule.get("text")),
            }
        )
    return {
        "prescription_id": _text(_first(item, "id", "purpose_id")),
        "appointment_id": _text(item.get("appointmentId")),
        "doctor": _person_name(doctor.get("lastName"), doctor.get("firstName")),
        "doctor_position": _text(doctor.get("position")),
        "medications": result_meds,
    }


# --------------------------------------------------------------------------
# Сервис
# --------------------------------------------------------------------------


class SmartMedService:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = resolve_state_dir(directory)
        self.state = StateStore(self.directory)
        self.tokens = TokenStore(self.directory)
        self.log = SafeLog(self.directory)
        self.bootstrap = BrowserBootstrap(self.tokens, self.state, self.log, self.directory)
        self._lock = threading.RLock()
        self._client: SmartMedClient | None = None
        self._api: SmartMedApi | None = None

    # -------------------------------------------------------------- client

    def _client_instance(self) -> SmartMedClient:
        if self._client is None:
            tokens = self.tokens.load()
            self._client = SmartMedClient(
                access_token=tokens.get("access_token"),
                refresh_token=tokens.get("refresh_token"),
                auth_style=tokens.get("auth_style"),
                on_tokens=self._persist_tokens,
            )
        return self._client

    def _api_instance(self) -> SmartMedApi:
        if self._api is None:
            self._api = SmartMedApi(self._client_instance())
        return self._api

    def _persist_tokens(self, tokens: dict[str, Any]) -> None:
        self.tokens.save(tokens)
        self.state.update(tokens_updated_at=int(time.time()))

    def _reset_client(self) -> None:
        self._client = None
        self._api = None

    # ------------------------------------------------------------- session

    def status(self) -> dict[str, Any]:
        tokens = self.tokens.load()
        browser = self.bootstrap.snapshot()
        verified_at = int(self.state.load().get("session_verified_at") or 0)
        if browser["status"] in ("starting", "waiting"):
            status = "login_pending"
        elif not tokens:
            status = "bootstrap"
        elif verified_at and int(time.time()) - verified_at < SESSION_TTL_SEC:
            status = "ready"
        else:
            status = "quick_login"
        return {
            "ok": True,
            "status": status,
            "storage": self.tokens.storage_kind(),
            "browser": browser["browser"],
        }

    def open_bootstrap(self) -> dict[str, Any]:
        result = self.bootstrap.start()
        return {
            "ok": True,
            "status": result.get("status"),
            "message": (
                "Открыт Chrome на https://smartmed.pro/sign-in. Введите телефон и код "
                "из SMS на сайте SmartMed (не в чате). После входа вызовите "
                "bootstrap_status, затем quick_login."
            ),
        }

    def bootstrap_status(self) -> dict[str, Any]:
        return {"ok": True, **self.bootstrap.snapshot()}

    def close_bootstrap(self) -> dict[str, Any]:
        return {"ok": True, **self.bootstrap.close()}

    def quick_login(self) -> dict[str, Any]:
        tokens = self.tokens.load()
        if not tokens:
            raise AuthRequired(reason="no_tokens")
        with self._lock:
            try:
                patients = self._api_instance().patients()
            except ClientAuthRequired as exc:
                self._reset_client()
                raise AuthRequired(reason=exc.reason) from exc
            except ApiError as exc:
                raise _api_error(exc) from exc
        self.state.update(session_verified_at=int(time.time()))
        return {
            "ok": True,
            "status": "ready",
            "patients": len(patients),
        }

    def refresh_tokens(self) -> dict[str, Any]:
        if not self.tokens.load().get("refresh_token"):
            raise AuthRequired(reason="no_refresh_token")
        with self._lock:
            try:
                result = self._client_instance().refresh()
            except ClientAuthRequired as exc:
                self._reset_client()
                raise AuthRequired(reason=exc.reason) from exc
            except ApiError as exc:
                raise _api_error(exc) from exc
        return {"ok": True, "status": "refreshed", "refreshed": bool(result.get("refreshed"))}

    def logout(self) -> dict[str, Any]:
        self.tokens.clear()
        self.state.update(session_verified_at=0)
        self._reset_client()
        return {"ok": True, "status": "logged_out"}

    # ------------------------------------------------------------- helpers

    def _call(self, func, *args, **kwargs):
        with self._lock:
            try:
                return func(*args, **kwargs)
            except ClientAuthRequired as exc:
                self._on_client_auth_error(exc)
                raise AuthRequired(reason=exc.reason) from exc
            except ApiError as exc:
                raise _api_error(exc) from exc

    def _on_client_auth_error(self, exc: ClientAuthRequired) -> None:
        """Сессия недействительна: при отказе refresh токены больше не нужны."""
        self._reset_client()
        self.state.update(session_verified_at=0)
        if exc.reason == "refresh_rejected":
            self.tokens.clear()

    def _resolve_patient_id(self, patient_id: str | None) -> str:
        if patient_id:
            return str(patient_id)
        items = _patient_list(self._api_instance().patients())
        if not items:
            raise McpError("no_patients", "В аккаунте нет доступных пациентов.")
        chosen = next((item for item in items if item["is_default"]), items[0])
        return str(chosen["patient_id"])

    # --------------------------------------------------------------- data

    def profile(self) -> dict[str, Any]:
        if not self.tokens.load():
            raise AuthRequired(reason="no_tokens")
        data = self._call(self._api_instance().info)
        return {"ok": True, "profile": _profile(data)}

    def patients(self) -> dict[str, Any]:
        if not self.tokens.load():
            raise AuthRequired(reason="no_tokens")
        items = _patient_list(self._call(self._api_instance().patients))
        return {"ok": True, "patients": items}

    def appointments(
        self,
        patient_id: str | None = None,
        kind: str = "upcoming",
        limit: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        if not self.tokens.load():
            raise AuthRequired(reason="no_tokens")
        kind = (kind or "upcoming").strip().lower()
        states = {
            "upcoming": ["current", "upcoming"],
            "history": ["done", "canceled"],
            "all": ["current", "upcoming", "done", "canceled"],
        }.get(kind)
        if states is None:
            raise McpError("bad_argument", "kind должен быть upcoming, history или all.")
        resolved = self._resolve_patient_id(patient_id)
        data = self._call(
            self._api_instance().medical_card,
            patient_id=resolved,
            types=list(ALL_RECEPTION_TYPES),
            calendar_states=states,
            page=max(1, int(page)),
            size=_clamp(limit),
        )
        items = [_appointment(item) for item in _items(data)]
        return {
            "ok": True,
            "patient_id": resolved,
            "kind": kind,
            "appointments": items[: _clamp(limit)],
            "total": data.get("total") if isinstance(data.get("total"), int) else len(items),
        }

    def documents(
        self,
        patient_id: str | None = None,
        limit: int = 20,
        page: int = 0,
        search: str | None = None,
        tag_id: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
    ) -> dict[str, Any]:
        if not self.tokens.load():
            raise AuthRequired(reason="no_tokens")
        resolved = self._resolve_patient_id(patient_id)
        data = self._call(
            self._api_instance().documents,
            patient_id=resolved,
            page=max(0, int(page)),
            size=_clamp(limit),
            search=search,
            tag_id=tag_id,
            date_from=date_from,
            date_to=date_to,
        )
        items = [_document(item) for item in _items(data)]
        return {
            "ok": True,
            "patient_id": resolved,
            "documents": items[: _clamp(limit)],
        }

    def document_tags(self, patient_id: str | None = None) -> dict[str, Any]:
        if not self.tokens.load():
            raise AuthRequired(reason="no_tokens")
        resolved = self._resolve_patient_id(patient_id)
        items = self._call(self._api_instance().document_tags, patient_id=resolved)
        tags = [
            {"tag_id": _text(tag.get("id")), "name": _text(tag.get("name"))}
            for tag in items
            if _text(tag.get("name"))
        ]
        return {"ok": True, "patient_id": resolved, "tags": tags}

    def document_download(self, file_id: str, patient_id: str | None = None) -> dict[str, Any]:
        if not self.tokens.load():
            raise AuthRequired(reason="no_tokens")
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", str(file_id or ""))
        if not safe_id:
            raise McpError("bad_argument", "file_id пустой или содержит недопустимые символы.")
        resolved = self._resolve_patient_id(patient_id)
        content = self._call(
            self._api_instance().document_file, safe_id, patient_id=resolved
        )
        extension = _guess_extension(content)
        target_dir = self.directory / DOCUMENTS_DIR
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = target_dir / f"smartmed-document-{safe_id}.{extension}"
        fd = path.open("wb")
        try:
            os.fchmod(fd.fileno(), 0o600)
            fd.write(content)
        finally:
            fd.close()
        return {
            "ok": True,
            "patient_id": resolved,
            "path": str(path),
            "size": len(content),
            "format": extension,
        }

    def referrals(
        self,
        patient_id: str | None = None,
        limit: int = 25,
        page: int = 1,
        search: str | None = None,
        is_active: bool | None = None,
        sort: str = "date_desc",
    ) -> dict[str, Any]:
        if not self.tokens.load():
            raise AuthRequired(reason="no_tokens")
        resolved = self._resolve_patient_id(patient_id)
        data = self._call(
            self._api_instance().referrals,
            patient_id=resolved,
            page=max(1, int(page)),
            size=_clamp(limit, 25),
            search=search,
            is_active=is_active,
            sort=sort,
        )
        items = [_referral(item) for item in _items(data)]
        return {
            "ok": True,
            "patient_id": resolved,
            "referrals": items[: _clamp(limit, 25)],
        }

    def prescriptions(
        self,
        patient_id: str | None = None,
        scope: str = "active",
        limit: int = 20,
        page: int = 1,
    ) -> dict[str, Any]:
        if not self.tokens.load():
            raise AuthRequired(reason="no_tokens")
        scope = (scope or "active").strip().lower()
        if scope not in ("active", "archive", "all"):
            raise McpError("bad_argument", "scope должен быть active, archive или all.")
        resolved = self._resolve_patient_id(patient_id)
        data = self._call(
            self._api_instance().prescriptions,
            patient_id=resolved,
            scope=scope,
            page=max(1, int(page)),
            size=_clamp(limit),
        )
        items = [_prescription(item) for item in _items(data)]
        return {
            "ok": True,
            "patient_id": resolved,
            "scope": scope,
            "prescriptions": items[: _clamp(limit)],
        }


    # ------------------------------------------------- запись к врачу

    def _require_tokens(self) -> None:
        if not self.tokens.load():
            raise AuthRequired(reason="no_tokens")

    def _resolve_city_id(self, city_id: str | None) -> str:
        if city_id:
            return str(city_id)
        data = self._call(self._api_instance().user_city)
        for key in ("userLocation", "nearestCity"):
            node = data.get(key) if isinstance(data.get(key), dict) else {}
            city = node.get("city") if isinstance(node.get("city"), dict) else node
            value = _first(city, "id", "city_id") if isinstance(city, dict) else None
            if value not in (None, ""):
                return str(value)
        cities = _items(self._call(self._api_instance().cities))
        if cities:
            value = _first(cities[0], "id", "city_id")
            if value not in (None, ""):
                return str(value)
        raise McpError("city_not_found", "Не удалось определить город для записи.")

    def _resolve_age_level(self, patient_id: str) -> int | None:
        for item in _patient_list(self._call(self._api_instance().patients)):
            if item["patient_id"] == str(patient_id):
                return {"child": 1, "adult": 2, "any": 3}.get(item.get("age_level") or "")
        return None

    def cities(self, search: str | None = None) -> dict[str, Any]:
        self._require_tokens()
        data = self._call(self._api_instance().cities)
        result = []
        for item in _items(data):
            city_id = _text(_first(item, "id", "city_id"))
            name = _text(item.get("name"))
            if city_id and name:
                result.append({"city_id": city_id, "name": name})
        if search:
            needle = search.strip().lower()
            result = [item for item in result if needle in item["name"].lower()]
        return {"ok": True, "cities": result}

    def specialties(
        self,
        city_id: str | None = None,
        search: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self._require_tokens()
        resolved = self._resolve_city_id(city_id)
        data = self._call(
            self._api_instance().specialties,
            city_id=resolved,
            search=search,
            size=_clamp(limit, 50),
        )
        result = []
        for item in _items(data):
            specialty_id = _text(_first(item, "id", "specialty_id"))
            name = _text(item.get("name"))
            if specialty_id and name:
                result.append({"specialty_id": specialty_id, "name": name})
        return {"ok": True, "city_id": resolved, "specialties": result}

    def clinics(
        self,
        specialty_id: str,
        city_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_tokens()
        resolved = self._resolve_city_id(city_id)
        items = self._call(
            self._api_instance().clinics_by_specialty,
            city_id=resolved,
            specialty_ids=[str(specialty_id)],
        )
        result = []
        for item in items:
            clinic_id = _text(_first(item, "id", "clinic_id"))
            if not clinic_id:
                continue
            result.append(
                {
                    "clinic_id": clinic_id,
                    "clinic_code": _text(_first(item, "code", "clinic_code")),
                    "name": _text(item.get("name")),
                    "address": _text(item.get("address")),
                }
            )
        return {"ok": True, "city_id": resolved, "clinics": result}

    def doctors(
        self,
        specialty_id: str | None = None,
        clinic_id: str | None = None,
        city_id: str | None = None,
        search: str | None = None,
        online: bool = False,
        limit: int = 20,
    ) -> dict[str, Any]:
        self._require_tokens()
        resolved = self._resolve_city_id(city_id)
        data = self._call(
            self._api_instance().doctors,
            city_id=resolved,
            specialty_ids=[str(specialty_id)] if specialty_id else None,
            clinic_ids=[str(clinic_id)] if clinic_id else None,
            search=search,
            online=online,
            size=_clamp(limit),
        )
        result = []
        for item in _items(data):
            doctor_id = _text(_first(item, "id", "doctor_id"))
            if not doctor_id:
                continue
            specialties = item.get("specialties") if isinstance(item.get("specialties"), list) else []
            result.append(
                {
                    "doctor_id": doctor_id,
                    "name": _person_name(item.get("nom"), item.get("prenom")),
                    "specialties": [
                        _text(entry.get("name"))
                        for entry in specialties
                        if isinstance(entry, dict) and _text(entry.get("name"))
                    ],
                }
            )
        return {"ok": True, "city_id": resolved, "doctors": result}

    def available_dates(
        self,
        specialty_id: str,
        clinic_ids: list[str],
        patient_id: str | None = None,
    ) -> dict[str, Any]:
        self._require_tokens()
        resolved = self._resolve_patient_id(patient_id)
        age_level = self._resolve_age_level(resolved)
        items = self._call(
            self._api_instance().available_dates,
            specialty_id=str(specialty_id),
            clinic_ids=[str(item) for item in clinic_ids],
            patient_age_level=age_level,
            patient_id=resolved,
        )
        dates = [
            {
                "date": _date(item.get("date")),
                "is_available": bool(
                    item.get("is_available", item.get("isAvailable", False))
                ),
            }
            for item in items
            if _date(item.get("date"))
        ]
        return {"ok": True, "patient_id": resolved, "dates": dates}

    def slots(
        self,
        specialty_id: str,
        clinic_ids: list[str],
        date: str,
        patient_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        self._require_tokens()
        resolved = self._resolve_patient_id(patient_id)
        age_level = self._resolve_age_level(resolved)
        data = self._call(
            self._api_instance().schedules,
            specialty_id=str(specialty_id),
            clinic_ids=[str(item) for item in clinic_ids],
            date=date,
            size=_clamp(limit, 50),
            patient_age_level=age_level,
            patient_id=resolved,
        )
        slots = []
        for schedule in _items(data):
            slots.extend(_schedule_slots(schedule))
        return {
            "ok": True,
            "patient_id": resolved,
            "date": _date(date),
            "slots": slots[: _clamp(limit, 50)],
            "next_page_token": (data.get("paginate") or {}).get("token")
            if isinstance(data.get("paginate"), dict)
            else None,
        }

    def booking_prepare(
        self,
        schedule_id: str,
        reception_type_id: str,
        start_utcz: str,
        clinic_code: str,
        patient_id: str | None = None,
        summary: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self._require_tokens()
        resolved = self._resolve_patient_id(patient_id)
        check = self._call(
            self._api_instance().slot_is_free,
            schedule_id=str(schedule_id),
            reception_type_id=str(reception_type_id),
            start_utcz=start_utcz,
        )
        is_free = check.get("is_free", check.get("isFree"))
        if not is_free:
            raise McpError(
                "slot_taken",
                "Слот уже занят. Выберите другое время через slots.",
                extra={"is_free": False},
            )
        details = dict(summary or {})
        details.update(
            {
                "schedule_id": str(schedule_id),
                "reception_type_id": str(reception_type_id),
                "start_utcz": str(start_utcz),
                "clinic_code": str(clinic_code),
                "patient_id": resolved,
            }
        )
        fm_link = details.pop("fm_clink_patient_id", None)
        if isinstance(fm_link, str) and fm_link.strip().isdigit():
            fm_link = int(fm_link.strip())
        token = secrets.token_hex(16)
        pending = self._load_pending()
        pending[token] = {
            "kind": "booking",
            "created_at": int(time.time()),
            "payload": {
                "patient_id": resolved,
                "schedule_id": str(schedule_id),
                "reception_type_id": str(reception_type_id),
                "start_utcz": str(start_utcz),
                "clinic_code": str(clinic_code),
                "fm_clink_patient_id": fm_link,
            },
            "details": details,
        }
        self._save_pending(pending)
        self.log.event("booking_prepared")
        return {
            "ok": True,
            "confirm_token": token,
            "expires_in": PENDING_TTL_SEC,
            "is_free": True,
            "details": details,
        }

    def booking_execute(self, confirm_token: str) -> dict[str, Any]:
        self._require_tokens()
        pending = self._load_pending()
        entry = pending.pop(str(confirm_token or ""), None)
        if not entry or entry.get("kind") != "booking":
            raise McpError("bad_confirm_token", "Неизвестный или уже использованный confirm_token.")
        if int(time.time()) - int(entry.get("created_at") or 0) > PENDING_TTL_SEC:
            self._save_pending(pending)
            raise McpError("confirm_token_expired", "confirm_token истёк. Повторите booking_prepare.")
        self._save_pending(pending)

        payload = entry.get("payload") or {}
        # Перед записью убеждаемся, что сессия жива (read-only запрос, без повтора записи).
        self._call(self._api_instance().patients)
        try:
            created = self._call(
                self._api_instance().make_appointment,
                clinic_code=payload["clinic_code"],
                schedule_id=payload["schedule_id"],
                reception_type_id=payload["reception_type_id"],
                start_utcz=payload["start_utcz"],
                fm_clink_patient_id=payload.get("fm_clink_patient_id"),
                patient_id=payload.get("patient_id"),
            )
        except McpError as exc:
            # Явный отказ сервера (есть HTTP-статус) — записи нет. Сетевой сбой
            # или таймаут — исход неизвестен; повторять запись нельзя.
            exc.extra.setdefault("outcome", "rejected" if exc.http_status else "unknown")
            raise
        reception_id = _text(_first(created, "id", "reception_id"))
        if not reception_id:
            self.log.event("booking_unknown", reason="no_reception_id")
            return {
                "ok": False,
                "error": "unknown_outcome",
                "message": (
                    "Сервер не вернул id приёма. Не повторяйте запись: проверьте appointments."
                ),
            }
        final = self._wait_reception(reception_id, payload.get("patient_id"))
        status = _text(_first(final.get("status") or {}, "name")) if isinstance(final.get("status"), dict) else _text(final.get("status"))
        self.log.event("booking_created", status=status or "unknown")
        if status == "rejected":
            error_info = final.get("errorInfo") if isinstance(final.get("errorInfo"), dict) else {}
            return {
                "ok": False,
                "error": "rejected",
                "message": _text(error_info.get("description")) or "Клиника отклонила запись.",
                "appointment": _reception_dto(final),
            }
        return {
            "ok": status in (None, "approved", "reserved"),
            "status": status or "unknown",
            "appointment": _reception_dto(final),
            "message": (
                "Запись создана и подтверждена."
                if status == "approved"
                else "Запись отправлена в клинику и ещё не подтверждена (reserved). "
                "Проверьте appointments позже."
            ),
        }

    def cancel_prepare(
        self, appointment_id: str, patient_id: str | None = None
    ) -> dict[str, Any]:
        self._require_tokens()
        resolved = self._resolve_patient_id(patient_id)
        raw = self._call(
            self._api_instance().reception, str(appointment_id), patient_id=resolved
        )
        dto = _reception_dto(raw)
        status = _text(_first(raw.get("status") or {}, "name")) if isinstance(raw.get("status"), dict) else None
        if _is_canceled(status):
            return {"ok": True, "already_canceled": True, "appointment": dto}
        token = secrets.token_hex(16)
        pending = self._load_pending()
        pending[token] = {
            "kind": "cancel",
            "created_at": int(time.time()),
            "payload": {"appointment_id": str(appointment_id), "patient_id": resolved},
            "details": dto,
        }
        self._save_pending(pending)
        self.log.event("cancel_prepared")
        return {
            "ok": True,
            "confirm_token": token,
            "expires_in": PENDING_TTL_SEC,
            "appointment": dto,
        }

    def cancel_execute(self, confirm_token: str) -> dict[str, Any]:
        self._require_tokens()
        pending = self._load_pending()
        entry = pending.pop(str(confirm_token or ""), None)
        if not entry or entry.get("kind") != "cancel":
            raise McpError("bad_confirm_token", "Неизвестный или уже использованный confirm_token.")
        if int(time.time()) - int(entry.get("created_at") or 0) > PENDING_TTL_SEC:
            self._save_pending(pending)
            raise McpError("confirm_token_expired", "confirm_token истёк. Повторите cancel_prepare.")
        self._save_pending(pending)

        payload = entry.get("payload") or {}
        appointment_id = payload.get("appointment_id")
        patient_id = payload.get("patient_id")
        self._call(self._api_instance().patients)
        error: McpError | None = None
        try:
            self._call(
                self._api_instance().cancel_reception,
                str(appointment_id),
                patient_id=patient_id,
            )
        except McpError as exc:
            error = exc

        # Проверяем фактическое состояние, не полагаясь на ответ отмены.
        final = self._wait_canceled(str(appointment_id), patient_id)
        status = (
            _text(_first(final.get("status") or {}, "name"))
            if isinstance(final.get("status"), dict)
            else None
        )
        if _is_canceled(status):
            self.log.event("cancel_done")
            return {
                "ok": True,
                "status": status or "canceled",
                "appointment": _reception_dto(final),
            }
        if error is not None:
            error.extra.setdefault("outcome", "unknown")
            error.extra.setdefault("status", status)
            raise error
        return {
            "ok": False,
            "error": "cancel_unknown",
            "message": "Отмена отправлена, но статус не подтверждён. Проверьте appointments.",
            "status": status,
            "appointment": _reception_dto(final),
        }

    # ------------------------------------------------------- pending tokens

    def _load_pending(self) -> dict[str, Any]:
        data = self.state.load().get("pending")
        return dict(data) if isinstance(data, dict) else {}

    def _save_pending(self, pending: dict[str, Any]) -> None:
        now = int(time.time())
        clean = {
            key: value
            for key, value in pending.items()
            if isinstance(value, dict)
            and now - int(value.get("created_at") or 0) <= PENDING_TTL_SEC
        }
        self.state.update(pending=clean)

    def _wait_reception(self, reception_id: str, patient_id: str | None) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for attempt in range(BOOKING_POLL_TRIES):
            try:
                last = self._call(
                    self._api_instance().reception,
                    reception_id,
                    patient_id=patient_id,
                )
            except McpError:
                return last
            status = last.get("status") if isinstance(last.get("status"), dict) else {}
            name = _text(status.get("name"))
            if name != "reserved":
                return last
            if attempt < BOOKING_POLL_TRIES - 1:
                time.sleep(BOOKING_POLL_SEC)
        return last

    def _wait_canceled(self, appointment_id: str, patient_id: str | None) -> dict[str, Any]:
        last: dict[str, Any] = {}
        for attempt in range(BOOKING_POLL_TRIES):
            try:
                last = self._call(
                    self._api_instance().reception,
                    appointment_id,
                    patient_id=patient_id,
                )
            except McpError:
                return last
            status = last.get("status") if isinstance(last.get("status"), dict) else {}
            if _is_canceled(_text(status.get("name"))):
                return last
            if attempt < BOOKING_POLL_TRIES - 1:
                time.sleep(BOOKING_POLL_SEC)
        return last

def _schedule_slots(schedule: dict[str, Any]) -> list[dict[str, Any]]:
    doctor = schedule.get("doctor") if isinstance(schedule.get("doctor"), dict) else {}
    clinic = schedule.get("clinic") if isinstance(schedule.get("clinic"), dict) else {}
    raw_types = schedule.get("schedule_reception_types")
    types = raw_types if isinstance(raw_types, list) else []
    by_id: dict[str, dict[str, Any]] = {}
    for entry in types:
        if not isinstance(entry, dict):
            continue
        reception_type = entry.get("reception_type")
        if isinstance(reception_type, dict):
            value = _text(_first(reception_type, "id", "reception_type_id"))
            if value:
                by_id[value] = reception_type
    slots = schedule.get("slots") if isinstance(schedule.get("slots"), list) else []
    result = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        start = _first(slot, "start_local", "startLocal")
        reception_type_id = _text(_first(slot, "reception_type_id", "receptionTypeId"))
        if not reception_type_id and len(by_id) == 1:
            reception_type_id = next(iter(by_id))
        reception_type = by_id.get(str(reception_type_id), {}) if reception_type_id else {}
        price = reception_type.get("price") if isinstance(reception_type.get("price"), dict) else {}
        specialty = (
            reception_type.get("specialty")
            if isinstance(reception_type.get("specialty"), dict)
            else {}
        )
        result.append(
            {
                "slot_id": _text(_first(slot, "id", "slot_id")),
                "date": _date(start),
                "time": _clock(start),
                "start_local": _text(start),
                "start_utcz": _text(_first(slot, "start_utcz", "startUtcz")),
                "schedule_id": _text(_first(slot, "schedule_id", "scheduleId")) or _text(schedule.get("id")),
                "reception_type_id": reception_type_id,
                "clinic_code": _text(_first(clinic, "code", "clinic_code")),
                "clinic": _text(clinic.get("name")),
                "address": _text(_first(slot, "cabinet_address", "cabinetAddress"))
                or _text(clinic.get("address")),
                "cabinet": _text(_first(slot, "cabinet_address", "cabinetAddress")),
                "doctor_id": _text(_first(doctor, "id", "doctor_id")),
                "doctor": _person_name(doctor.get("nom"), doctor.get("prenom")),
                "specialty": _text(specialty.get("name")) or _text(schedule.get("name")),
                "reception_type": _text(_first(reception_type, "web_name", "name")),
                "price": price.get("price") if isinstance(price.get("price"), (int, float, str)) else None,
                "med_program_id": price.get("med_program_id"),
            }
        )
    return result


def _reception_dto(raw: dict[str, Any]) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    status = raw.get("status") if isinstance(raw.get("status"), dict) else {}
    doctor = raw.get("doctor") if isinstance(raw.get("doctor"), dict) else {}
    clinic = raw.get("clinic") if isinstance(raw.get("clinic"), dict) else {}
    specialty = raw.get("specialty") if isinstance(raw.get("specialty"), dict) else {}
    schedule_slot = (
        raw.get("scheduleSlot")
        if isinstance(raw.get("scheduleSlot"), dict)
        else raw.get("schedule_slot")
        if isinstance(raw.get("schedule_slot"), dict)
        else {}
    )
    start = _first(raw, "startLocal", "start_local", "appointment_date", "date")
    return {
        "appointment_id": _text(_first(raw, "id", "reception_id")),
        "status": _text(_first(status, "name", "value")),
        "date": _date(start),
        "time": _clock(start),
        "specialty": _text(specialty.get("name")),
        "doctor": _person_name(doctor.get("nom"), doctor.get("prenom")),
        "clinic": _text(clinic.get("name")),
        "address": _text(_first(schedule_slot, "cabinetAddress", "cabinet_address"))
        or _text(clinic.get("address")),
        "cabinet": _text(raw.get("cabinet")),
        "duration": raw.get("duration") if isinstance(raw.get("duration"), (int, float)) else None,
    }

def _guess_extension(content: bytes) -> str:
    if content.startswith(b"%PDF"):
        return "pdf"
    if content.startswith(b"\x89PNG"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return "bin"


def _api_error(exc: ApiError) -> McpError:
    if exc.code in {"rate_limited"}:
        message = "SmartMed ограничил частоту запросов. Подождите минуту."
    elif exc.code == "app_version_rejected":
        message = (
            "SmartMed отклонил версию клиента. Нужно обновить APP_VERSION "
            "в smartmed/client.py до версии официального веб-кабинета."
        )
    elif exc.retryable:
        message = "SmartMed временно недоступен. Повторите запрос позже."
    else:
        message = exc.message or "SmartMed вернул ошибку."
    return McpError(
        exc.code,
        message,
        http_status=exc.http_status,
    )
