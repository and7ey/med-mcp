"""Высокоуровневые операции MCP-сервера и нормализация DTO.

Инструменты возвращают только нужные поля: без токенов, сырых ответов ЕМИАС,
URL с query и служебных идентификаторов, которых нет в DTO.
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from emias.api import Emias, Slot, find_key, parse_slots
from emias.auth import Session
from emias.errors import AuthRequired as ApiAuthRequired
from emias.errors import EmiasError, NotFound
from emias.interview import fetch_tags

from .errors import AuthRequired, McpError
from .login import BrowserBootstrap
from .safe_log import SafeLog
from .state import StateStore, TokenStore, resolve_state_dir
from .survey import build_complaints, questions_payload, tags_payload

PERIODS = {"half_year", "year", "all_time"}
MAX_LIMIT = 200
PENDING_TTL_SEC = 300


class _Client(Emias):
    """Клиент ЕМИАС, который сохраняет обновлённые токены в TokenStore."""

    def __init__(self, session: Session, on_save) -> None:
        super().__init__(session=session)
        self._on_save = on_save

    def save(self):  # type: ignore[override]
        self._on_save(self.session)
        return self.session_path


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _date(value: Any) -> str | None:
    text = _text(value)
    return text[:10] if text else None


def _clock(value: Any) -> str | None:
    text = _text(value)
    return text[11:16] if text and len(text) >= 16 else None


def _first(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _clamp(limit: int) -> int:
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return 50
    return max(1, min(value, MAX_LIMIT))


class EmiasService:
    def __init__(self, directory: str | Path | None = None) -> None:
        self.directory = resolve_state_dir(directory)
        self.state = StateStore(self.directory)
        self.tokens = TokenStore(self.directory)
        self.log = SafeLog(self.directory)
        self.bootstrap = BrowserBootstrap(
            self.tokens, self.state, self.log, self.directory
        )
        self._verified_at: float | None = None
        self._pending_lock = threading.Lock()
        self._import_cli_once()

    # ------------------------------------------------------------- сессия

    def _import_cli_once(self) -> None:
        """Один раз подхватить сессию CLI, чтобы не входить повторно.

        Флаг в state.json не даёт импортировать старые токены после logout().
        """
        state = self.state.load()
        if state.get("cli_import_checked") or self.tokens.load():
            return
        tokens = self.tokens.import_cli_session()
        if tokens:
            self.tokens.save(tokens)
        self.state.update(cli_import_checked=True)

    def status(self) -> dict[str, Any]:
        tokens = self.tokens.load()
        browser = self.bootstrap.snapshot()
        if browser["status"] in ("starting", "waiting"):
            status = "login_pending"
        elif tokens.get("access_token") or tokens.get("refresh_token"):
            status = "ready" if self._verified_at else "quick_login"
        else:
            status = "bootstrap"
        return {
            "ok": True,
            "status": status,
            "signed_in": bool(tokens.get("access_token")),
            "browser": browser,
            "next_action": {
                "bootstrap": "open_bootstrap",
                "login_pending": "bootstrap_status",
                "quick_login": "quick_login",
                "ready": None,
            }[status],
        }

    def open_bootstrap(self) -> dict[str, Any]:
        tokens = self.tokens.load()
        if tokens.get("access_token"):
            return {
                "ok": True,
                "status": "already_signed_in",
                "message": "Токены уже есть. Для повторного входа вызовите logout().",
            }
        snapshot = self.bootstrap.start()
        return {
            "ok": True,
            "status": "browser_opened",
            "message": (
                "Открыт Chrome на странице входа mos.ru. Войдите в нём "
                "(телефон/пароль/код) и вернитесь в чат — токены подхватятся "
                "автоматически. Секреты вводятся только на сайте mos.ru."
            ),
            "browser": snapshot,
        }

    def bootstrap_status(self) -> dict[str, Any]:
        snapshot = self.bootstrap.snapshot()
        tokens = self.tokens.load()
        result = {"ok": True, "browser": snapshot, "signed_in": bool(tokens.get("access_token"))}
        if snapshot["status"] == "ready":
            result["message"] = "Вход выполнен. Вызовите quick_login."
        elif snapshot["status"] == "timeout":
            result["message"] = "Время входа истекло. Вызовите open_bootstrap заново."
        return result

    def close_bootstrap(self) -> dict[str, Any]:
        return {"ok": True, "browser": self.bootstrap.close()}

    def quick_login(self) -> dict[str, Any]:
        tokens = self.tokens.load()
        if not tokens.get("access_token"):
            raise AuthRequired()
        client = self._client(tokens)
        try:
            data = client.profile()
        except ApiAuthRequired:
            try:
                client.refresh()
            except EmiasError as exc:
                self._on_auth_failure(exc)
                raise AuthRequired(
                    "Сессия истекла, обновите вход через open_bootstrap.",
                    reason="refresh_failed",
                ) from None
            data = client.profile()
        self._verified_at = time.time()
        patients = _patient_list(data)
        return {
            "ok": True,
            "status": "ready",
            "patients_count": len(patients),
            "message": "Сессия рабочая. Доступны read-only инструменты.",
        }

    def refresh_tokens(self) -> dict[str, Any]:
        tokens = self.tokens.load()
        if not tokens.get("refresh_token"):
            raise AuthRequired(reason="no_refresh_token")
        client = self._client(tokens)
        try:
            client.refresh()
            client.profile()
        except EmiasError as exc:
            self._on_auth_failure(exc)
            raise AuthRequired(
                "Не удалось обновить сессию. Нужен повторный вход.",
                reason="refresh_failed",
            ) from None
        self._verified_at = time.time()
        self.state.update(last_refresh_at=int(time.time()))
        return {"ok": True, "status": "ready"}

    def logout(self) -> dict[str, Any]:
        self.bootstrap.close()
        self.tokens.clear()
        self._verified_at = None
        self.log.event("tokens_cleared")
        return {"ok": True, "status": "bootstrap"}

    # ------------------------------------------------------------- данные

    def patients(self) -> dict[str, Any]:
        data = self._client().profile()
        return {"ok": True, "patients": _patient_list(data)}

    def visits(
        self, patient_id: str | None = None, period: str = "half_year", limit: int = 50
    ) -> dict[str, Any]:
        ehr, client = self._patient_client(patient_id)
        payload = client.visits(ehr, self._period(period))
        items = [_visit(item) for item in _documents(payload)]
        return self._page("visits", items, limit, patient_id=ehr)

    def analyzes(
        self, patient_id: str | None = None, period: str = "half_year", limit: int = 50
    ) -> dict[str, Any]:
        ehr, client = self._patient_client(patient_id)
        payload = client.analyzes(ehr, self._period(period))
        items = [_analyze(item) for item in _documents(payload)]
        return self._page("analyzes", items, limit, patient_id=ehr)

    def vaccinations(
        self, patient_id: str | None = None, limit: int = 100
    ) -> dict[str, Any]:
        ehr, client = self._patient_client(patient_id)
        payload = client.vaccinations(ehr)
        done = [_vaccination(item) for item in _list(payload, "doneList")]
        tub = [_first(item, "infectionName", "name") for item in _list(payload, "tubList")]
        return {
            "ok": True,
            "patient_id": ehr,
            "count": len(done),
            "vaccinations": done[:_clamp(limit)],
            "tub_count": len(tub),
            "scans_count": len(_list(payload, "scans")),
        }

    def prescriptions(
        self, patient_id: str | None = None, period: str = "half_year", limit: int = 50
    ) -> dict[str, Any]:
        ehr, client = self._patient_client(patient_id)
        payload = client.prescriptions(ehr, self._period(period))
        receipts = [_receipt(item) for item in _list(payload, "receipts")]
        return {
            "ok": True,
            "patient_id": ehr,
            "count": len(receipts),
            "prescriptions": receipts[:_clamp(limit)],
        }

    def referrals(self, patient_id: str | None = None, limit: int = 50) -> dict[str, Any]:
        ehr, client = self._patient_client(patient_id)
        payload = client.referrals(ehr)
        items = [_referral(item) for item in _list(payload, "referrals")]
        return {"ok": True, "patient_id": ehr, "count": len(items),
                "referrals": items[:_clamp(limit)]}

    def appointments(self, patient_id: str | None = None) -> dict[str, Any]:
        ehr, client = self._patient_client(patient_id)
        payload = client.appointments(ehr)
        items = [_appointment(item) for item in _list(payload, "appointments")]
        return {"ok": True, "patient_id": ehr, "count": len(items), "appointments": items}

    def specialities(self, patient_id: str | None = None) -> dict[str, Any]:
        ehr, client = self._patient_client(patient_id)
        items = [
            {
                "id": _text(_first(item, "code", "specialityId", "id")),
                "name": _text(_first(item, "name", "specialityName")),
                "is_paid": bool(item.get("isPaid")),
            }
            for item in _list(client.specialities(ehr), "specialities")
        ]
        return {"ok": True, "patient_id": ehr, "specialities": items}

    def purposes(self, patient_id: str | None = None) -> dict[str, Any]:
        ehr, client = self._patient_client(patient_id)
        categories = []
        for category in _list(client.purposes(ehr), "categories"):
            purposes = []
            for item in category.get("purposes") or []:
                if not isinstance(item, dict):
                    continue
                purposes.append(
                    {
                        "id": _text(_first(item, "inquiryPurposeId", "id")),
                        "code": _text(item.get("code")),
                        "name": _text(item.get("name")),
                        "description": _text(item.get("description")),
                    }
                )
            categories.append({"title": _text(category.get("title")), "purposes": purposes})
        return {"ok": True, "patient_id": ehr, "categories": categories}

    def slots(
        self,
        speciality_id: str,
        purpose_id: str,
        patient_id: str | None = None,
        lpu_id: str | None = None,
        referral_id: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        if not speciality_id or not purpose_id:
            raise McpError(
                "invalid_request",
                "Нужны speciality_id и purpose_id: их можно получить "
                "инструментами specialities и purposes.",
            )
        ehr, client = self._patient_client(patient_id)
        payload = client.available_resources(
            ehr,
            speciality_id=speciality_id,
            lpu_id=lpu_id,
            referral_id=referral_id,
            inquiry_purpose_id=purpose_id,
        )
        slots = sorted(parse_slots(payload), key=lambda slot: (slot.date, slot.start_time))
        items = [
            {
                "date": slot.date,
                "start_time": slot.start_time,
                "end_time": slot.end_time,
                "speciality": slot.speciality,
                "doctor": slot.doctor,
                "room": slot.room,
                "resource_id": _text(slot.resource_id),
                "complex_resource_id": _text(slot.complex_resource_id),
            }
            for slot in slots
        ]
        return {
            "ok": True,
            "patient_id": ehr,
            "count": len(items),
            "slots": items[:_clamp(limit)],
        }

    # ------------------------------------------------------------- запись

    def complaint_tags(self, patient_id: str | None = None) -> dict[str, Any]:
        """Список жалоб для опросника (read-only)."""
        ehr, client = self._patient_client(patient_id)
        tags = tags_payload(fetch_tags(client, ehr))
        return {"ok": True, "patient_id": ehr, "tags": tags}

    def complaint_questions(
        self, tag_codes: list[int | str], patient_id: str | None = None
    ) -> dict[str, Any]:
        """Вопросы и варианты ответов по выбранным жалобам (read-only)."""
        if not tag_codes:
            raise McpError("invalid_request", "Передайте tag_codes из complaint_tags.")
        ehr, client = self._patient_client(patient_id)
        tags = fetch_tags(client, ehr)
        return {
            "ok": True,
            "patient_id": ehr,
            "tags": questions_payload(tags, tag_codes),
        }

    def booking_prepare(
        self,
        resource_id: str,
        start_time: str,
        end_time: str,
        purpose_id: str,
        speciality_id: str,
        patient_id: str | None = None,
        complex_resource_id: str | None = None,
    ) -> dict[str, Any]:
        """Проверить слот и вернуть детали + одноразовый confirm_token.

        Ничего не меняет. Слот перепроверяется read-only запросом, чтобы не
        записать пациента на устаревшее время.
        """
        if not (resource_id and start_time and end_time and purpose_id and speciality_id):
            raise McpError(
                "invalid_request",
                "Нужны resource_id, start_time, end_time, purpose_id и speciality_id.",
            )
        ehr, client = self._patient_client(patient_id)
        payload = client.available_resources(
            ehr,
            speciality_id=speciality_id,
            inquiry_purpose_id=purpose_id,
            with_schedule=True,
        )
        match = None
        for slot in parse_slots(payload):
            if str(slot.resource_id) != str(resource_id):
                continue
            if slot.start_time != start_time:
                continue
            if end_time and slot.end_time != end_time:
                continue
            if complex_resource_id not in (None, "", "0") and str(
                slot.complex_resource_id
            ) != str(complex_resource_id):
                continue
            match = slot
            break
        if match is None:
            raise McpError(
                "slot_unavailable",
                "Слот уже недоступен. Обновите список через slots.",
            )

        complaint = client.request(
            "GET",
            "/interview/get-complaint",
            query={"ehrId": ehr, "specialityId": str(speciality_id)},
        )
        needs_complaints = not (
            isinstance(complaint, dict) and complaint.get("complaintId")
        )

        details = {
            "patient_id": ehr,
            "resource_id": str(match.resource_id),
            "complex_resource_id": _text(match.complex_resource_id),
            "start_time": match.start_time,
            "end_time": match.end_time,
            "speciality_id": str(speciality_id),
            "purpose_id": str(purpose_id),
            "needs_complaints": needs_complaints,
        }
        token = self._put_pending("booking", details)
        return {
            "ok": True,
            "confirm_token": token,
            "expires_in_sec": PENDING_TTL_SEC,
            "booking": {
                "patient_id": ehr,
                "date": match.date,
                "start_time": match.start_time,
                "end_time": match.end_time,
                "speciality": match.speciality,
                "doctor": match.doctor,
                "room": match.room,
                "needs_complaints": needs_complaints,
            },
            "message": (
                "Покажите детали пользователю и вызывайте booking_execute "
                "только после отдельного явного подтверждения. "
                + (
                    "Перед этим заполните жалобы: complaint_tags → "
                    "complaint_questions → booking_execute(complaints=[...])."
                    if needs_complaints
                    else ""
                )
            ),
        }

    def booking_execute(
        self,
        confirm_token: str,
        complaints: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Создать запись по подтверждённому слоту. Один раз, без повторов."""
        pending = self._peek_pending(confirm_token, "booking")
        details = pending["details"]

        survey: list[dict[str, Any]] | None = None
        if details.get("needs_complaints"):
            if not complaints:
                raise McpError(
                    "complaints_required",
                    "Для этой специальности нужны жалобы. Вызовите complaint_tags, "
                    "затем complaint_questions и передайте ответы в complaints.",
                )
            ehr = str(details["patient_id"])
            client = self._client()
            tags = fetch_tags(client, ehr)
            survey, errors = build_complaints(tags, complaints)
            if errors:
                raise McpError(
                    "invalid_complaints",
                    "Ответы неполные или не соответствуют опроснику.",
                    extra={"errors": errors[:20]},
                )
        elif complaints:
            raise McpError(
                "complaints_not_needed",
                "Этой специальности жалобы не требуются; уберите complaints.",
            )

        # Токен одноразовый: изымаем только когда входные данные валидны.
        self._take_pending(confirm_token, "booking")

        ehr = str(details["patient_id"])
        client = self._client()

        complaint_id = None
        if survey is not None:
            try:
                dialog = client.request(
                    "POST",
                    "/interview/create-dialog",
                    body={"ehrId": ehr, "specialityId": details["speciality_id"]},
                )
                code = dialog.get("code") if isinstance(dialog, dict) else None
                complaint_id = dialog.get("complaintId") if isinstance(dialog, dict) else None
                if not code:
                    raise McpError(
                        "complaints_save_failed",
                        "ЕМИАС не начал диалог жалоб. Запись не создана.",
                    )
                client.request(
                    "POST",
                    "/self-appointment/interview/save-answers",
                    body={
                        "ehrId": ehr,
                        "hash": code,
                        "complaintsJson": {"tagSurvey": survey},
                    },
                )
            except EmiasError as exc:
                self._log_api_error("complaints_save", exc)
                return {
                    "ok": False,
                    "status": "not_booked",
                    "stage": "complaints",
                    "error": "complaints_save_failed",
                    "message": "Не удалось сохранить жалобы. Запись не создана.",
                    "api_code": exc.code,
                }

        slot = Slot(
            resource_id=details["resource_id"],
            complex_resource_id=details.get("complex_resource_id"),
            date=str(details["start_time"])[:10],
            start_time=details["start_time"],
            end_time=details["end_time"],
        )

        try:
            reservation = client.reserve(
                ehr,
                slot,
                inquiry_purpose_id=details["purpose_id"],
                complaint_id=complaint_id,
            )
        except EmiasError as exc:
            self._log_api_error("reserve", exc)
            if exc.status is None:
                return {
                    "ok": False,
                    "status": "unknown_outcome",
                    "stage": "reserve",
                    "error": "unknown_outcome",
                    "message": (
                        "Сеть оборвалась на бронировании. Не повторяйте; "
                        "бронь могла создаться. Проверьте appointments позже."
                    ),
                }
            return {
                "ok": False,
                "status": "not_booked",
                "stage": "reserve",
                "error": "reserve_failed",
                "message": "Слот не удалось забронировать. Запись не создана.",
                "api_code": exc.code,
            }

        reservation_id = find_key(reservation, {"reservationId", "reservation_id", "id"})
        if reservation_id is None:
            return {
                "ok": False,
                "status": "unknown_outcome",
                "stage": "reserve",
                "error": "unknown_outcome",
                "message": (
                    "Бронь создана без reservationId — состояние неизвестно. "
                    "Не повторяйте; проверьте appointments."
                ),
            }

        try:
            confirmed = client.confirm_reservation(
                ehr,
                reservation_id,
                slot,
                speciality_id=details["speciality_id"],
                inquiry_purpose_id=details["purpose_id"],
            )
        except EmiasError as exc:
            self._log_api_error("confirm", exc)
            if exc.status is not None:
                # Ответ сервера получен: запись точно не создана, снимаем бронь.
                try:
                    client.cancel_reservation(ehr, reservation_id)
                except EmiasError:
                    pass
                return {
                    "ok": False,
                    "status": "not_booked",
                    "stage": "confirm",
                    "error": "confirm_failed",
                    "api_code": exc.code,
                    "message": "ЕМИАС отклонил подтверждение. Запись не создана.",
                }
            return {
                "ok": False,
                "status": "unknown_outcome",
                "stage": "confirm",
                "error": "unknown_outcome",
                "message": (
                    "Сеть оборвалась на подтверждении. Не повторяйте запись; "
                    "проверьте appointments."
                ),
            }

        appointment_id = find_key(confirmed, {"appointmentId", "id"})
        verified = self._appointment_exists(client, ehr, appointment_id)
        self.log.event("appointment_created", verified=verified)
        return {
            "ok": True,
            "status": "booked",
            "appointment_id": _text(appointment_id),
            "date": details["start_time"][:10],
            "start_time": details["start_time"],
            "end_time": details["end_time"],
            "verified": verified,
        }

    def cancel_prepare(
        self, appointment_id: str, patient_id: str | None = None
    ) -> dict[str, Any]:
        """Найти запись и вернуть детали + confirm_token. Ничего не меняет."""
        ehr, client = self._patient_client(patient_id)
        items = _list(client.appointments(ehr), "appointments")
        match = next(
            (item for item in items if str(_first(item, "id", "appointmentId")) == str(appointment_id)),
            None,
        )
        if match is None:
            raise McpError(
                "appointment_not_found",
                "Запись с таким id не найдена в текущих записях.",
            )
        appt = _appointment(match)
        token = self._put_pending(
            "cancel",
            {"patient_id": ehr, "appointment_id": str(appointment_id)},
        )
        return {
            "ok": True,
            "confirm_token": token,
            "expires_in_sec": PENDING_TTL_SEC,
            "appointment": appt,
            "message": (
                "Покажите детали пользователю и вызывайте cancel_execute только "
                "после отдельного явного подтверждения."
            ),
        }

    def cancel_execute(self, confirm_token: str) -> dict[str, Any]:
        """Отменить запись по подтверждённому cancel_prepare. Один раз."""
        pending = self._peek_pending(confirm_token, "cancel")
        details = pending["details"]
        self._take_pending(confirm_token, "cancel")

        ehr = str(details["patient_id"])
        appointment_id = str(details["appointment_id"])
        client = self._client()
        try:
            client.cancel_appointment(ehr, appointment_id)
        except EmiasError as exc:
            self._log_api_error("cancel_appointment", exc)
            if exc.status is None:
                return {
                    "ok": False,
                    "status": "unknown_outcome",
                    "error": "unknown_outcome",
                    "message": (
                        "Сеть оборвалась при отмене. Не повторяйте; проверьте "
                        "appointments."
                    ),
                }
            return {
                "ok": False,
                "status": "not_cancelled",
                "error": "cancel_failed",
                "api_code": exc.code,
                "message": "ЕМИАС отклонил отмену.",
            }

        still_there = self._appointment_exists(client, ehr, appointment_id)
        self.log.event("appointment_cancelled", still_present=still_there)
        return {
            "ok": not still_there,
            "status": "cancelled" if not still_there else "unknown_outcome",
            "appointment_id": appointment_id,
            "verified": not still_there,
        }

    # ------------------------------------------------------------- helpers

    def _client(self, tokens: dict[str, Any] | None = None) -> _Client:
        tokens = tokens if tokens is not None else self.tokens.load()
        if not tokens.get("access_token"):
            raise AuthRequired()
        session = Session(
            access_token=str(tokens.get("access_token") or ""),
            refresh_token=str(tokens.get("refresh_token") or ""),
            obtained_at=float(tokens.get("obtained_at") or 0),
            login_variant=str(tokens.get("login_variant") or "regular"),
        )

        def on_save(updated: Session) -> None:
            self.tokens.save(updated.to_dict())

        return _Client(session, on_save)

    # ------------------------------------------------------------ документы

    def document_download(
        self,
        document_id: str,
        patient_id: str | None = None,
        variant: str = "pdf",
        official_document_pdf_id: str | None = None,
    ) -> dict[str, Any]:
        """Скачать документ медкарты в локальный каталог (0600).

        variant: pdf (обычный PDF), scan (скан), official (официальный PDF),
        archive (архив). Для official/archive нужен officialDocumentPDFId;
        если он не передан, берётся из карточки документа.
        """
        variant = (variant or "pdf").strip().lower()
        if variant not in {"pdf", "scan", "official", "archive"}:
            raise McpError(
                "bad_argument",
                "variant должен быть pdf, scan, official или archive.",
            )
        raw_id = str(document_id or "").strip()
        safe_id = re.sub(r"[^A-Za-z0-9_-]", "", raw_id)
        if not safe_id or safe_id != raw_id:
            raise McpError("bad_argument", "document_id пустой или содержит недопустимые символы.")

        ehr, client = self._patient_client(patient_id)
        official_id = str(official_document_pdf_id or "").strip()
        if variant in {"official", "archive"} and not official_id:
            try:
                info = client.document_info(ehr, safe_id)
            except NotFound as exc:
                raise McpError("document_not_found", "Документ не найден.") from exc
            except EmiasError as exc:
                self._log_api_error("document_info", exc)
                raise McpError("download_failed", "ЕМИАС не отдал карточку документа.") from exc
            official_id = str(info.get("officialDocumentPDFId") or "").strip()
            if not official_id:
                raise McpError(
                    "no_official_pdf",
                    "У этого документа нет официального PDF или архива.",
                )

        try:
            if variant == "pdf":
                content = client.document_pdf(ehr, safe_id)
            elif variant == "scan":
                content = client.document_scan(ehr, safe_id)
            elif variant == "official":
                content = client.document_official_pdf(ehr, official_id)
            else:
                content = client.document_archive(ehr, official_id)
        except NotFound as exc:
            raise McpError("document_not_found", "Документ или файл не найден.") from exc
        except EmiasError as exc:
            self._log_api_error(f"document_{variant}", exc)
            if exc.status == 520:
                raise McpError(
                    "file_unavailable",
                    "Для этого документа нет файла в выбранном виде. "
                    "Попробуйте другой variant.",
                ) from exc
            raise McpError("download_failed", "Не удалось скачать документ.") from exc

        extension = _guess_extension(content)
        target_dir = self.directory / "documents"
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = target_dir / f"emias-document-{safe_id}.{extension}"
        with path.open("wb") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(content)
        self.log.event("document_downloaded", variant=variant, size=len(content))
        return {
            "ok": True,
            "document_id": safe_id,
            "variant": variant,
            "path": str(path),
            "size": len(content),
            "format": extension,
        }

    def _patient_client(self, patient_id: str | None) -> tuple[str, _Client]:
        client = self._client()
        ehr = patient_id or self._default_patient(client)
        return ehr, client

    @staticmethod
    def _default_patient(client: _Client) -> str:
        data = client.profile()
        patients = _patient_list(data)
        if not patients:
            raise McpError("no_patients", "В профиле нет доступных пациентов.")
        return str(patients[0]["patient_id"])

    @staticmethod
    def _period(period: str) -> str:
        if period not in PERIODS:
            raise McpError(
                "invalid_period",
                "period должен быть half_year, year или all_time "
                "(month сервер ЕМИАС не принимает).",
            )
        return period

    @staticmethod
    def _page(
        name: str, items: list[dict[str, Any]], limit: int, *, patient_id: str
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "patient_id": patient_id,
            "count": len(items),
            name: items[:_clamp(limit)],
        }

    def _on_auth_failure(self, exc: EmiasError) -> None:
        self.log.event("auth_failed", error=exc.code or "unknown", http_status=exc.status)

    def _log_api_error(self, stage: str, exc: EmiasError) -> None:
        self.log.event(
            "write_failed",
            stage=stage,
            error=exc.code or "unknown",
            http_status=exc.status,
        )

    def _appointment_exists(
        self, client: _Client, ehr: str, appointment_id: Any
    ) -> bool:
        if appointment_id is None:
            return False
        try:
            items = _list(client.appointments(ehr), "appointments")
        except EmiasError:
            return False
        return any(
            str(_first(item, "id", "appointmentId")) == str(appointment_id)
            for item in items
        )

    # ------------------------------------------------------ pending tokens

    def _prune_pending(self) -> dict[str, Any]:
        state = self.state.load()
        pending = state.get("pending")
        if not isinstance(pending, dict):
            pending = {}
        now = time.time()
        pending = {
            key: value
            for key, value in pending.items()
            if isinstance(value, dict) and float(value.get("expires_at") or 0) > now
        }
        state["pending"] = pending
        self.state.save(state)
        return pending

    def _put_pending(self, kind: str, details: dict[str, Any]) -> str:
        with self._pending_lock:
            pending = self._prune_pending()
            token = secrets.token_hex(16)
            pending[token] = {
                "kind": kind,
                "expires_at": int(time.time()) + PENDING_TTL_SEC,
                "details": details,
            }
            state = self.state.load()
            state["pending"] = pending
            self.state.save(state)
            return token

    def _peek_pending(self, token: str, kind: str) -> dict[str, Any]:
        with self._pending_lock:
            pending = self._prune_pending()
            item = pending.get(str(token or ""))
            if not item or item.get("kind") != kind:
                raise McpError(
                    "confirm_token_invalid",
                    "confirm_token неизвестен, истёк или уже использован. "
                    "Вызовите prepare заново.",
                )
            return item

    def _take_pending(self, token: str, kind: str) -> dict[str, Any]:
        with self._pending_lock:
            pending = self._prune_pending()
            item = pending.pop(str(token or ""), None)
            if not item or item.get("kind") != kind:
                raise McpError(
                    "confirm_token_invalid",
                    "confirm_token неизвестен, истёк или уже использован. "
                    "Вызовите prepare заново.",
                )
            state = self.state.load()
            state["pending"] = pending
            self.state.save(state)
            return item


# --------------------------------------------------------------------------
# Нормализация
# --------------------------------------------------------------------------


def _patient_list(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    raw = []
    if isinstance(payload.get("profile"), dict):
        raw.append(payload["profile"])
    raw.extend(item for item in payload.get("relatives") or [] if isinstance(item, dict))
    result = []
    for item in raw:
        name = " ".join(
            part
            for part in (
                _text(item.get("lastName")),
                _text(item.get("firstName")),
                _text(item.get("middleName")),
            )
            if part
        )
        result.append(
            {
                "patient_id": _text(_first(item, "profileId", "ehrId")),
                "full_name": name or None,
                "birth_date": _date(item.get("birthDate")),
                "permission_type": _text(item.get("permissionType")) or "PERSONAL",
                "gender": _text(item.get("gender")),
            }
        )
    return result


def _documents(payload: Any) -> list[dict[str, Any]]:
    return _list(payload, "documents")


def _list(payload: Any, key: str) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    value = payload.get(key)
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _visit(item: dict[str, Any]) -> dict[str, Any]:
    document = item.get("document")
    if not isinstance(document, dict):
        document = item
    date = _first(document, "date") or _first(item, "date")
    return {
        "document_id": _text(_first(document, "documentId") or _first(item, "documentId")),
        "date": _date(date),
        "time": _clock(date),
        "title": _text(document.get("title")),
        "speciality": _text(_first(document, "doctorSpecialization", "specialityName")),
        "doctor": _text(_first(document, "doctorName", "doctorFio")),
        "organization": _text(_first(document, "organisation", "lpuName")),
        "type": _text(item.get("documentType")),
    }


def _analyze(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "document_id": _text(item.get("documentId")),
        "date": _date(item.get("date")),
        "code": _text(item.get("code")),
        "title": _text(_first(item, "title", "name")),
        "is_covid": bool(item.get("isCovid")),
    }


def _vaccination(item: dict[str, Any]) -> dict[str, Any]:
    infections = []
    for infection in item.get("infectionList") or []:
        if isinstance(infection, dict):
            name = _text(infection.get("infectionName"))
            if name:
                infections.append(name)
    return {
        "document_id": _text(item.get("documentId")),
        "date": _date(item.get("dateVaccination")),
        "infections": infections,
        "vaccine": _text(item.get("vaccinationName")),
        "dosage": _text(item.get("dosage")),
        "status": _text(item.get("status")),
    }


def _receipt(item: dict[str, Any]) -> dict[str, Any]:
    date = _first(item, "date", "prescriptionDate", "createdAt")
    return {
        "id": _text(_first(item, "prescriptionNumber", "documentId", "id")),
        "date": _date(date),
        "title": _text(_first(item, "title", "drugName", "medicineName", "name", "mnnName")),
        "status": _text(_first(item, "status", "state", "prescriptionStatus")),
    }


def _referral(item: dict[str, Any]) -> dict[str, Any]:
    doctor = item.get("doctor")
    doctor_name = None
    if isinstance(doctor, dict):
        doctor_name = _text(_first(doctor, "fio", "name", "fullName"))
    return {
        "id": _text(_first(item, "id", "number")),
        "number": _text(item.get("number")),
        "date_from": _date(item.get("dateFrom")),
        "date_to": _date(item.get("dateTo")),
        "type": _text(item.get("type")),
        "organization": _text(_first(item, "moName", "lpuName")),
        "doctor": doctor_name,
        "purpose_required": bool(item.get("isPurposeRequired")),
    }


def _appointment(item: dict[str, Any]) -> dict[str, Any]:
    doctor = item.get("doctor")
    if not isinstance(doctor, dict):
        doctor = {}
    start = _first(item, "startTime", "date")
    return {
        "id": _text(_first(item, "id", "appointmentId")),
        "date": _date(start),
        "time": _clock(start),
        "end_time": _clock(item.get("endTime")),
        "speciality": _text(_first(item, "specialityName") or doctor.get("specialityName")),
        "doctor": _text(_first(item, "doctorName") or doctor.get("name")),
        "place": _text(_first(item, "lpuName", "muName", "address")),
        "type": _text(item.get("type")),
    }


def _guess_extension(content: bytes) -> str:
    if content.startswith(b"%PDF"):
        return "pdf"
    if content.startswith(b"\x89PNG"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpg"
    return "bin"
