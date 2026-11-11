"""Доменные методы медкарты и записи к врачу."""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Any, Iterable, Mapping

from .client import EmiasClient, iter_nodes
from .errors import AuthRequired


@dataclass
class Slot:
    """Свободный слот приёма."""

    resource_id: int | str
    complex_resource_id: int | str | None
    date: str
    start_time: str
    end_time: str
    lpu: str | None = None
    address: str | None = None
    speciality: str | None = None
    doctor: str | None = None
    room: str | None = None
    speciality_id: int | str | None = None
    reception_type_id: int | str | None = None
    inquiry_purpose_id: int | str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class Emias(EmiasClient):
    """Клиент с методами разделов медкарты."""

    # ------------------------------------------------------------- профиль

    def profile(self, ehr_id: str | None = None) -> dict[str, Any]:
        """Данные профиля: сам пациент + родственники.

        ``POST /api/1/get-profile`` -> ``{"profile": {...}, "relatives": [...]}``
        Резервный вариант: ``GET /api/2/profile``.
        """
        try:
            data = self.request(
                "POST",
                "/get-profile",
                body={"permissionTypes": ["PERSONAL", "CHILD", "RELATIVE"]},
            )
            if data:
                return data
        except AuthRequired:
            raise
        except Exception:
            pass
        query = {"ehrId": ehr_id} if ehr_id else None
        return self.request("GET", "/profile", version="/2", query=query)

    def patients(self) -> list[dict[str, Any]]:
        """Плоский список пациентов, к чьим картам есть доступ."""
        data = self.profile() or {}
        result: list[dict[str, Any]] = []
        if isinstance(data.get("profile"), dict):
            result.append(data["profile"])
        result.extend(data.get("relatives") or [])
        return result

    def default_patient(self) -> dict[str, Any]:
        data = self.profile() or {}
        if isinstance(data.get("profile"), dict):
            return data["profile"]
        relatives = data.get("relatives") or []
        if relatives:
            return relatives[0]
        raise RuntimeError("В профиле нет доступных пациентов")

    # -------------------------------------------------------------- медкарта

    def section_counts(
        self, ehr_id: str, types: Iterable[str] | None = None
    ) -> dict[str, Any]:
        """Счётчики документов по разделам.

        ``GET /api/1/documents/sections?ehrId=&type=``
        """
        types = list(types or [])
        result: dict[str, Any] = {}
        for doc_type in types:
            try:
                result[doc_type] = self.request(
                    "GET", "/documents/sections", query={"ehrId": ehr_id, "type": doc_type}
                )
            except AuthRequired:
                raise
            except Exception as exc:  # раздел может быть недоступен
                result[doc_type] = {"error": str(exc)}
        return result

    # ------------------------------------------------------------ документы

    def document_info(self, ehr_id: str, document_id: str) -> dict[str, Any]:
        """Детали документа: ``GET /api/3/document``.

        Содержит HTML документа, ``officialDocumentPDFId``, список сканов и
        признаки ``isDownload``/``isArchiveDownloadAllowed``.
        """
        data = self.request(
            "GET",
            "/document",
            version="/3",
            query={"ehrId": ehr_id, "documentId": document_id, "extendedScansType": "YES"},
        )
        if isinstance(data, dict) and isinstance(data.get("data"), dict):
            return data["data"]
        return data if isinstance(data, dict) else {}

    def document_pdf(self, ehr_id: str, document_id: str, *, timeout: float = 120.0) -> bytes:
        """PDF документа: ``GET /api/1/document/pdf``."""
        return self.request(
            "GET",
            "/document/pdf",
            query={"ehrId": ehr_id, "documentId": document_id},
            binary=True,
            timeout=timeout,
        )

    def document_scan(self, ehr_id: str, document_id: str, *, timeout: float = 120.0) -> bytes:
        """Скан документа: ``GET /api/1/document/scan``."""
        return self.request(
            "GET",
            "/document/scan",
            query={"ehrId": ehr_id, "documentId": document_id},
            binary=True,
            timeout=timeout,
        )

    def document_official_pdf(
        self, ehr_id: str, official_document_pdf_id: str, *, timeout: float = 120.0
    ) -> bytes:
        """Официальный PDF: ``GET /api/1/document/officialPDF``."""
        return self.request(
            "GET",
            "/document/officialPDF",
            query={"ehrId": ehr_id, "officialDocumentPDFId": official_document_pdf_id},
            binary=True,
            timeout=timeout,
        )

    def document_archive(
        self, ehr_id: str, official_document_pdf_id: str, *, timeout: float = 120.0
    ) -> bytes:
        """Архив официального PDF: ``GET /api/1/document/official-archive``."""
        return self.request(
            "GET",
            "/document/official-archive",
            query={"ehrId": ehr_id, "officialDocumentPDFId": official_document_pdf_id},
            binary=True,
            timeout=timeout,
        )

    def visits(self, ehr_id: str, period: str = "half_year") -> Any:
        """Посещения врача / осмотры.

        ``GET /api/4/documents/inspections`` (fallback ``/3``, ``/1``).
        """
        return self._with_version_fallback(
            "GET",
            "/documents/inspections",
            versions=["/4", "/3", "/1"],
            query={"ehrId": ehr_id, "shortDateFilter": period},
        )

    def analyzes(self, ehr_id: str, period: str = "half_year", covid: bool = True) -> Any:
        """Анализы (документы).

        ``GET /api/1/documents/analyzes``
        """
        return self.request(
            "GET",
            "/documents/analyzes",
            query={
                "ehrId": ehr_id,
                "shortDateFilter": period,
                "includeCovidTests": "YES" if covid else "NO",
            },
        )

    def analysis_assignments(self, ehr_id: str, period: str = "half_year") -> Any:
        """Назначения на анализы (CLS). ``GET /api/1/analysis/assignments``."""
        return self.request(
            "GET",
            "/analysis/assignments",
            query={"ehrId": ehr_id, "dateFilter": period},
        )

    def analysis_details(
        self,
        ehr_id: str,
        *,
        results_doc_ids: Iterable[str] = (),
        assignments_doc_ids: Iterable[str] = (),
    ) -> Any:
        """Детализация анализов. ``POST /api/1/analysis/details``."""
        return self.request(
            "POST",
            "/analysis/details",
            body={
                "ehrId": ehr_id,
                "resultsDocId": sorted(results_doc_ids),
                "assignmentsDocId": sorted(assignments_doc_ids),
            },
        )

    def analysis_results(
        self,
        ehr_id: str,
        *,
        test_codes: Iterable[str],
        start_date: str,
        end_date: str,
        date_filter: str = "all_time",
    ) -> Any:
        """Динамика показателей. ``POST /api/1/analysis/results``."""
        return self.request(
            "POST",
            "/analysis/results",
            body={
                "ehrId": ehr_id,
                "testCodes": list(test_codes),
                "startDate": start_date,
                "endDate": end_date,
                "dateFilter": date_filter,
            },
        )

    def vaccinations(self, ehr_id: str, immunodiagnostics: bool = True) -> Any:
        """Прививки. ``GET /api/3/vaccinations`` (fallback ``/1``).

        UI запрашивает именно ``/3`` с ``isImmunodiagEnabled``.
        Ответ: ``{"doneList": [...], "tubList": [...], "scans": [...], "count": n}``.
        """
        return self._with_version_fallback(
            "GET",
            "/vaccinations",
            versions=["/3", "/1"],
            query={"ehrId": ehr_id, "isImmunodiagEnabled": immunodiagnostics},
        )

    def vaccination_statuses(self, ehr_id: str, package_ids: Iterable[str] = ()) -> Any:
        """Статусы загруженных пакетов прививок."""
        return self.request(
            "GET",
            "/vaccinations/get-package-statuses",
            query={"ehrId": ehr_id, "vaccinationPackageIds": list(package_ids)},
        )

    # --------------------------------------------------------- назначения

    def prescriptions(self, ehr_id: str, period: str = "half_year") -> Any:
        """Лекарственные назначения / рецепты. ``GET /api/2/receipt``."""
        return self.request(
            "GET",
            "/receipt",
            version="/2",
            query={"ehrId": ehr_id, "shortDateFilter": period},
        )

    def prescription_details(self, ehr_id: str, prescription_number: str) -> Any:
        """Детали рецепта. ``GET /api/3/receipt/details``."""
        return self.request(
            "GET",
            "/receipt/details",
            version="/3",
            query={"ehrId": ehr_id, "prescriptionNumber": prescription_number},
        )

    def referrals(self, ehr_id: str) -> Any:
        """Направления. ``GET /api/1/self-appointment/referrals``."""
        return self.request(
            "GET",
            "/self-appointment/referrals",
            query={"ehrId": ehr_id},
        )

    # -------------------------------------------------------- записи к врачу

    def appointments(self, ehr_id: str) -> Any:
        """Текущие записи к врачу. ``GET /api/1/self-appointment/appointments``."""
        return self.request(
            "GET",
            "/self-appointment/appointments",
            query={"ehrId": ehr_id},
        )

    def specialities(self, ehr_id: str) -> Any:
        """Доступные специальности. ``GET /api/1/self-appointment/specialities``."""
        return self.request(
            "GET", "/self-appointment/specialities", query={"ehrId": ehr_id}
        )

    def lpus(self, ehr_id: str) -> Any:
        """Поликлиники, доступные для записи."""
        return self.request("GET", "/self-appointment/lpu", query={"ehrId": ehr_id})

    def purposes(self, ehr_id: str, is_referral: bool = False) -> Any:
        """Поводы обращения. ``POST /api/1/self-appointment/purposes``."""
        return self.request(
            "POST",
            "/self-appointment/purposes",
            body={"ehrId": ehr_id, "isReferral": is_referral},
        )

    def available_resources(
        self,
        ehr_id: str,
        *,
        speciality_id: int | str | None = None,
        lpu_id: int | str | None = None,
        referral_id: int | str | None = None,
        inquiry_purpose_id: int | str | None = None,
        appointment_id: int | str | None = None,
        with_schedule: bool = True,
        version: str = "/2",
    ) -> Any:
        """Свободное время. ``POST /api/{2}/self-appointment/available-resources``."""
        body = {
            "ehrId": ehr_id,
            "appointmentId": appointment_id,
            "withSchedule": with_schedule,
            "specialityId": _num(speciality_id),
            "referralId": _num(referral_id),
            "lpuId": _num(lpu_id),
            "inquiryPurposeId": _num(inquiry_purpose_id),
        }
        body = {k: v for k, v in body.items() if v is not None}
        return self.request(
            "POST", "/self-appointment/available-resources", version=version, body=body
        )

    def slots(self, ehr_id: str, **kwargs: Any) -> list[Slot]:
        """Удобное представление свободных слотов."""
        data = self.available_resources(ehr_id, **kwargs)
        return parse_slots(data)

    def reserve(
        self,
        ehr_id: str,
        slot: Slot,
        *,
        referral_id: int | str | None = None,
        reception_type_id: int | str | None = None,
        inquiry_purpose_id: int | str | None = None,
        inquiry_purpose_code: str | None = None,
        complaint_id: int | str | None = None,
    ) -> Any:
        """Шаг 1 записи: забронировать слот.

        ``POST /api/2/self-appointment/reservation``
        """
        body = {
            "ehrId": ehr_id,
            "availableResourceId": _num(slot.resource_id),
            "complexResourceId": _num(slot.complex_resource_id) or 0,
            "startTime": slot.start_time,
            "endTime": slot.end_time,
            "referralId": _num(referral_id),
            "receptionTypeId": _num(reception_type_id),
            "inquiryPurposeId": _num(inquiry_purpose_id),
            "inquiryPurposeCode": inquiry_purpose_code,
            "complaintId": _num(complaint_id),
        }
        body = {k: v for k, v in body.items() if v is not None}
        return self.request(
            "POST", "/self-appointment/reservation", version="/2", body=body
        )

    def confirm_reservation(
        self,
        ehr_id: str,
        reservation_id: int | str,
        slot: Slot,
        *,
        speciality_id: int | str | None = None,
        reception_type_id: int | str | None = None,
        inquiry_purpose_id: int | str | None = None,
    ) -> Any:
        """Шаг 2 записи: подтвердить бронь -> создаётся приём."""
        body = {
            "ehrId": ehr_id,
            "reservationId": _num(reservation_id),
            "availableResourceId": _num(slot.resource_id),
            "specialityId": _num(speciality_id),
            "receptionTypeId": _num(reception_type_id),
            "inquiryPurposeId": _num(inquiry_purpose_id),
        }
        body = {k: v for k, v in body.items() if v is not None}
        return self.request(
            "POST", "/self-appointment/reservation-confirm", body=body
        )

    def cancel_reservation(
        self,
        ehr_id: str,
        reservation_id: int | str,
        slot: Slot | None = None,
        *,
        reception_type_id: int | str | None = None,
        inquiry_purpose_id: int | str | None = None,
        speciality_id: int | str | None = None,
    ) -> Any:
        body = {
            "ehrId": ehr_id,
            "reservationId": _num(reservation_id),
            "availableResourceId": _num(slot.resource_id) if slot else None,
            "specialityId": _num(speciality_id),
            "receptionTypeId": _num(reception_type_id),
            "inquiryPurposeId": _num(inquiry_purpose_id),
        }
        body = {k: v for k, v in body.items() if v is not None}
        return self.request(
            "POST", "/self-appointment/reservation-cancel", body=body
        )

    def book(
        self,
        ehr_id: str,
        slot: Slot,
        *,
        speciality_id: int | str | None = None,
        reception_type_id: int | str | None = None,
        inquiry_purpose_id: int | str | None = None,
        referral_id: int | str | None = None,
    ) -> dict[str, Any]:
        """Записаться на слот: reserve + confirm."""
        # Значения из самого слота — чтобы не требовать их от пользователя.
        if speciality_id is None:
            speciality_id = slot.speciality_id
        if reception_type_id is None:
            reception_type_id = slot.reception_type_id
        if inquiry_purpose_id is None:
            inquiry_purpose_id = slot.inquiry_purpose_id
        reservation = self.reserve(
            ehr_id,
            slot,
            referral_id=referral_id,
            reception_type_id=reception_type_id,
            inquiry_purpose_id=inquiry_purpose_id,
        )
        reservation_id = find_key(reservation, {"reservationId", "reservation_id", "id"})
        if reservation_id is None:
            raise RuntimeError(f"Сервер не вернул reservationId: {reservation}")
        # Если id не передали руками — пробуем достать из ответа на бронь.
        if speciality_id is None:
            speciality_id = find_key(reservation, {"specialityId", "speciality_id"})
        if reception_type_id is None:
            reception_type_id = find_key(reservation, {"receptionTypeId", "reception_type_id"})
        if inquiry_purpose_id is None:
            inquiry_purpose_id = find_key(reservation, {"inquiryPurposeId", "inquiry_purpose_id"})
        try:
            confirmed = self.confirm_reservation(
                ehr_id,
                reservation_id,
                slot,
                speciality_id=speciality_id,
                reception_type_id=reception_type_id,
                inquiry_purpose_id=inquiry_purpose_id,
            )
        except Exception:
            # Не оставляем «висящую» бронь, если подтвердить не удалось.
            try:
                self.cancel_reservation(ehr_id, reservation_id)
            except Exception:
                pass
            raise
        return {"reservation": reservation, "appointment": confirmed}

    def cancel_appointment(self, ehr_id: str, appointment_id: int | str) -> Any:
        """Отменить запись. ``POST /api/1/self-appointment/cancel``."""
        return self.request(
            "POST",
            "/self-appointment/cancel",
            body={"ehrId": ehr_id, "appointmentId": _num(appointment_id)},
        )

    # -------------------------------------------------------------- утилиты

    def _with_version_fallback(
        self,
        method: str,
        endpoint: str,
        *,
        versions: list[str],
        query: Mapping[str, Any] | None = None,
        body: Any = None,
    ) -> Any:
        last: Exception | None = None
        for version in versions:
            try:
                return self.request(
                    method, endpoint, version=version, query=query, body=body
                )
            except AuthRequired:
                raise
            except Exception as exc:  # пробуем следующую версию
                last = exc
        assert last is not None
        raise last


def _num(value: Any) -> Any:
    """Строковые id API любит числами."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return value


def find_key(payload: Any, names: set[str]) -> Any:
    """Найти первое значение по имени ключа в произвольном JSON."""
    for node in iter_nodes(payload):
        if isinstance(node, dict):
            for name in names:
                if name in node and node[name] is not None:
                    return node[name]
    return None


def _doctor_name(doctor: Mapping[str, Any]) -> str | None:
    """«Кочетков Д. В.» из mainDoctor."""
    last = str(doctor.get("lastName") or "").strip()
    if not last:
        return None
    initials = "".join(
        f"{str(part)[:1]}." for part in (doctor.get("firstName"), doctor.get("secondName")) if part
    )
    return f"{last} {initials}".strip()


def parse_slots(payload: Any) -> list[Slot]:
    """Разобрать ответ available-resources в плоский список слотов."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    resources = (
        data.get("availableResources")
        or data.get("resources")
        or (data if isinstance(data, list) else [])
    )
    if isinstance(resources, dict):
        resources = [resources]

    slots: list[Slot] = []
    for resource in resources or []:
        if not isinstance(resource, dict):
            continue
        resource_id = resource.get("id") or resource.get("availableResourceId")
        lpu = resource.get("muName") or resource.get("lpuName")
        address = resource.get("address")
        doctor = None
        speciality = resource.get("specialityName")
        main_doctor = resource.get("mainDoctor")
        if isinstance(main_doctor, dict):
            doctor = _doctor_name(main_doctor)
        referral = resource.get("referralType") or {}
        if isinstance(referral, dict):
            consultation = referral.get("consultation") or {}
            if isinstance(consultation, dict):
                doctor = consultation.get("doctorFullName") or doctor
                speciality = consultation.get("doctorPosition") or speciality

        for schedule in resource.get("schedule") or []:
            if not isinstance(schedule, dict):
                continue
            schedule_date = schedule.get("date")
            rooms = {
                item.get("id"): (item.get("room") or {}).get("number")
                for item in schedule.get("complexResource") or []
                if isinstance(item, dict)
            }

            groups = []
            if schedule.get("slots"):
                groups.append((None, schedule["slots"], None))
            for complex_schedule in schedule.get("complexResourceSchedule") or []:
                groups.append(
                    (
                        complex_schedule.get("complexResourceId"),
                        complex_schedule.get("slots") or [],
                        complex_schedule.get("roomNumber"),
                    )
                )

            for complex_id, group, room_number in groups:
                for item in group:
                    if not isinstance(item, dict) or item.get("isBusy"):
                        continue
                    room = item.get("room") or {}
                    start_time = item.get("startTime", "")
                    slots.append(
                        Slot(
                            resource_id=resource_id,
                            complex_resource_id=complex_id,
                            date=(start_time[:10] if start_time else schedule_date or ""),
                            start_time=start_time,
                            end_time=item.get("endTime", ""),
                            lpu=lpu,
                            address=address,
                            speciality=speciality,
                            doctor=doctor,
                            room=room_number
                            or rooms.get(complex_id)
                            or (room.get("number") if isinstance(room, dict) else None),
                            speciality_id=resource.get("specialityId"),
                            reception_type_id=next(
                                (
                                    value
                                    for value in resource.get("receptionTypeIds") or []
                                    if value is not None
                                ),
                                None,
                            ),
                        )
                    )
    return slots
