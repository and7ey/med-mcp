"""Командная строка: python -m emias ..."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .api import Emias, Slot, find_key
from .client import iter_nodes
from .errors import AuthRequired, EmiasError
from .interview import collect_complaints

CACHE_DIR = Path(
    os.environ.get("EMIAS_CACHE_DIR", Path.home() / ".cache" / "emias")
)
SLOTS_CACHE = CACHE_DIR / "last_slots.json"


# --------------------------------------------------------------- вывод

def dump(data: Any, out: str | None = None) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if out:
        Path(out).expanduser().write_text(text, encoding="utf-8")
        print(f"Сохранено в {out} ({len(text)} байт)")
        return
    print(text)


def print_patients(patients: list[dict[str, Any]]) -> None:
    if not patients:
        print("Пациентов не найдено")
        return
    print(f"{'ehrId':<14} {'ФИО':<40} {'дата рожд.':<12} {'доступ'}")
    print("-" * 84)
    for p in patients:
        fio = " ".join(
            filter(
                None,
                [p.get("lastName"), p.get("firstName"), p.get("middleName")],
            )
        )
        ehr = str(p.get("profileId") or p.get("ehrId") or "")
        print(
            f"{ehr:<14} {fio[:40]:<40} {str(p.get('birthDate') or '')[:10]:<12} "
            f"{p.get('permissionType') or ''}"
        )


def _first_list(data: Any, *keys: str) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
    return []


def _first_value(item: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = item.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _cut(value: Any, width: int) -> str:
    text = " ".join(str(value).split()) if value is not None else ""
    return text if len(text) <= width else text[: width - 1] + "…"


def _day(value: Any) -> str:
    return str(value or "")[:10]


def _clock(value: Any) -> str:
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else ""


def _visit_fields(item: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    document = item.get("document")
    if not isinstance(document, dict):
        document = {}
    return item, document


def print_visits(data: Any, limit: int = 50) -> None:
    items = _first_list(data, "documents", "inspections")
    if not items:
        print("Посещений нет")
        return
    print(f"{'дата':<11} {'время':<6} {'специальность':<30} {'врач':<26} где")
    print("-" * 104)
    for item in items[:limit]:
        wrapper, document = _visit_fields(item)
        date = _first_value(document, "date") or _first_value(wrapper, "date")
        speciality = _first_value(document, "doctorSpecialization", "specialityName")
        doctor = _first_value(document, "doctorName", "doctorFio")
        place = _first_value(document, "organisation", "lpuName")
        print(
            f"{_day(date):<11} {_clock(date):<6} "
            f"{_cut(speciality, 30):<30} {_cut(doctor, 26):<26} {_cut(place, 32)}"
        )
    if len(items) > limit:
        print(f"... ещё {len(items) - limit} (--limit/--json)")


def print_analyzes(data: Any, limit: int = 50) -> None:
    items = _first_list(data, "documents")
    if not items:
        print("Анализов нет")
        return
    print(f"{'дата':<11} {'код':<10} {'название':<48} covid")
    print("-" * 80)
    for item in items[:limit]:
        print(
            f"{_day(item.get('date')):<11} {_cut(item.get('code'), 10):<10} "
            f"{_cut(item.get('title', item.get('name')), 48):<48} "
            f"{'да' if item.get('isCovid') else ''}"
        )
    if len(items) > limit:
        print(f"... ещё {len(items) - limit} (--limit/--json)")


def print_vaccinations(data: Any, limit: int = 50) -> None:
    items = _first_list(data, "doneList")
    tub = _first_list(data, "tubList")
    scans = _first_list(data, "scans")
    if not items and not tub and not scans:
        print("Прививок нет")
        return
    if items:
        print(f"{'дата':<11} {'инфекция':<30} {'вакцина':<34} {'доза':<8} статус")
        print("-" * 104)
        for item in items[:limit]:
            infections = item.get("infectionList") or []
            names = ", ".join(
                str(x.get("infectionName"))
                for x in infections
                if isinstance(x, dict) and x.get("infectionName")
            )
            print(
                f"{_day(item.get('dateVaccination')):<11} {_cut(names, 30):<30} "
                f"{_cut(item.get('vaccinationName'), 34):<34} "
                f"{_cut(item.get('dosage'), 8):<8} {_cut(item.get('status'), 20)}"
            )
        if len(items) > limit:
            print(f"... ещё {len(items) - limit} (--limit/--json)")
    if tub:
        print(f"\nРеакции Манту/Диаскинтест: {len(tub)}")
    if scans:
        print(f"Загруженные пакеты: {len(scans)}")


def print_prescriptions(data: Any, limit: int = 50) -> None:
    items = _first_list(data, "receipts", "prescriptions", "documents")
    if not items:
        print("Назначений нет (или за выбранный период пусто)")
        return
    print(f"{'дата':<11} {'препарат':<44} {'статус':<14} id")
    print("-" * 96)
    for item in items[:limit]:
        date = _first_value(item, "date", "prescriptionDate", "createdAt")
        title = _first_value(
            item, "title", "drugName", "medicineName", "name", "mnnName"
        )
        status = _first_value(item, "status", "state", "prescriptionStatus")
        ident = _first_value(item, "prescriptionNumber", "documentId", "id")
        print(f"{_day(date):<11} {_cut(title, 44):<44} {_cut(status, 14):<14} {ident or ''}")
    if len(items) > limit:
        print(f"... ещё {len(items) - limit} (--limit/--json)")


def print_slots(slots: list[Slot], limit: int = 40) -> None:
    if not slots:
        print("Свободных слотов не найдено")
        return
    print(f"{'#':<3} {'дата':<11} {'время':<12} {'специальность':<28} {'врач':<24} где")
    print("-" * 104)
    for index, slot in enumerate(slots[:limit], start=1):
        time_range = f"{slot.start_time[11:16]}-{slot.end_time[11:16]}"
        where = ", ".join(
            part
            for part in (
                slot.lpu or "",
                f"каб. {slot.room}" if slot.room else "",
                slot.address or "",
            )
            if part
        )
        print(
            f"{index:<3} {slot.date[:10]:<11} {time_range:<12} "
            f"{_cut(slot.speciality, 28):<28} {_cut(slot.doctor, 24):<24} {_cut(where, 34)}"
        )
    if len(slots) > limit:
        print(f"... ещё {len(slots) - limit} слотов (--limit чтобы показать больше)")


def extract_records(payload: Any, keys: set[str]) -> list[dict[str, Any]]:
    """Найти в ответе список записей по известным ключам."""
    for node in iter_nodes(payload):
        if isinstance(node, dict):
            for key in keys:
                value = node.get(key)
                if isinstance(value, list) and value and isinstance(value[0], dict):
                    return value
    return []


# --------------------------------------------------------------- команды

def cmd_login(args: argparse.Namespace) -> int:
    client = Emias()
    session = client.login_interactive(code=args.code, open_browser=not args.no_browser)
    client.profile()  # проверка, что токен рабочий
    print("Профиль доступен.")
    return 0


def cmd_import_session(args: argparse.Namespace) -> int:
    """Импорт токенов, которые уже есть в браузере (localStorage)."""
    access = args.access
    refresh = args.refresh
    if not (access or refresh):
        raw = sys.stdin.read()
        if not raw.strip():
            print(
                "Пусто: передайте JSON с токенами (см. подсказку ниже).",
                file=sys.stderr,
            )
            return 2
        try:
            payload = json.loads(raw)
        except ValueError:
            # Могли вставить просто «access_token\nrefresh_token»
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            if len(lines) >= 2:
                access, refresh = lines[0], lines[1]
            elif lines:
                access = lines[0]
            else:
                print("Не удалось разобрать входные данные.", file=sys.stderr)
                return 2
        else:
            if not isinstance(payload, dict):
                print("Ожидался JSON-объект.", file=sys.stderr)
                return 2
            access = access or _pick(
                payload,
                "access_token",
                "accessToken",
                "patient.web.v2.accessToken",
                "token",
            )
            refresh = refresh or _pick(
                payload,
                "refresh_token",
                "refreshToken",
                "patient.web.v2.refreshToken",
            )

    if not access:
        print("Не найден access_token.", file=sys.stderr)
        return 2

    client = Emias()
    client.session.access_token = access
    client.session.refresh_token = refresh or ""
    client.save()
    print(f"Токены сохранены в {client.session_path}")

    try:
        data = client.profile()
    except EmiasError as exc:
        print(
            "Токены сохранены, но профиль не открылся: "
            f"{exc}\n"
            "Если access_token истёк — нужен refresh_token (или новый вход).",
            file=sys.stderr,
        )
        return 1

    profile = data.get("profile") if isinstance(data, dict) else None
    if isinstance(profile, dict):
        fio = " ".join(
            filter(None, [profile.get("lastName"), profile.get("firstName"), profile.get("middleName")])
        )
        print(f"Профиль доступен: {fio or profile.get('profileId') or 'без имени'}")
    else:
        print("Профиль доступен.")
    print("Дальше: python -m emias patients")
    return 0


def _pick(payload: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    return None


def cmd_logout(args: argparse.Namespace) -> int:
    Emias().logout()
    print("Токены удалены")
    return 0


def cmd_whoami(args: argparse.Namespace) -> int:
    client = Emias()
    data = client.profile()
    dump(data, args.out)
    return 0


def resolve_patient(client: Emias, ehr_id: str | None) -> str:
    if ehr_id:
        return ehr_id
    patient = client.default_patient()
    return str(patient.get("profileId") or patient.get("ehrId"))


def cmd_patients(args: argparse.Namespace) -> int:
    client = Emias()
    patients = client.patients()
    if args.json or args.out:
        dump(patients, args.out)
    else:
        print_patients(patients)
    return 0


def _doc_command(path: str):
    printers = {
        "visits": print_visits,
        "analyzes": print_analyzes,
        "vaccinations": print_vaccinations,
        "prescriptions": print_prescriptions,
    }

    def run(args: argparse.Namespace) -> int:
        client = Emias()
        ehr_id = resolve_patient(client, args.patient)
        method = getattr(client, path)
        if path in {"visits", "analyzes", "prescriptions"}:
            data = method(ehr_id, args.period)
        else:
            data = method(ehr_id)

        if args.json or args.out:
            dump(data, args.out)
            return 0

        printers[path](data, args.limit)
        return 0

    return run


def print_specialities(data: Any) -> None:
    items = _first_list(data, "specialities", "items")
    if not items:
        print("Специальностей не найдено")
        return
    print(f"{'id':<8} {'название':<44} платно")
    print("-" * 62)
    for item in items:
        print(
            f"{str(item.get('code') or ''):<8} {_cut(item.get('name'), 44):<44} "
            f"{'да' if item.get('isPaid') else ''}"
        )


def print_purposes(data: Any) -> None:
    categories = _first_list(data, "categories")
    if not categories:
        print("Поводов обращения не найдено")
        return
    for category in categories:
        print(f"\n{category.get('title')}")
        for item in category.get("purposes") or []:
            if not isinstance(item, dict):
                continue
            print(
                f"  id={str(item.get('inquiryPurposeId') or ''):<8} "
                f"code={str(item.get('code') or ''):<6} {_cut(item.get('name'), 56)}"
            )
            description = item.get("description")
            if description:
                print(f"           {_cut(description, 72)}")


def cmd_specialities(args: argparse.Namespace) -> int:
    client = Emias()
    ehr_id = resolve_patient(client, args.patient)
    data = client.specialities(ehr_id)
    if args.json or args.out:
        dump(data, args.out)
    else:
        print_specialities(data)
    return 0


def cmd_purposes(args: argparse.Namespace) -> int:
    client = Emias()
    ehr_id = resolve_patient(client, args.patient)
    data = client.purposes(ehr_id)
    if args.json or args.out:
        dump(data, args.out)
    else:
        print_purposes(data)
    return 0


def cmd_slots(args: argparse.Namespace) -> int:
    client = Emias()
    ehr_id = resolve_patient(client, args.patient)
    slots = client.slots(
        ehr_id,
        speciality_id=args.speciality,
        lpu_id=args.lpu,
        inquiry_purpose_id=args.purpose,
        referral_id=args.referral,
    )
    slots.sort(key=lambda s: (s.date, s.start_time))
    if args.purpose:
        for slot in slots:
            slot.inquiry_purpose_id = args.purpose
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    SLOTS_CACHE.write_text(
        json.dumps([s.to_dict() for s in slots], ensure_ascii=False),
        encoding="utf-8",
    )
    if args.json or args.out:
        dump([s.to_dict() for s in slots], args.out)
    else:
        print_slots(slots, args.limit)
        if slots:
            print(f"\nСписок сохранён: {SLOTS_CACHE}. Запись: --slot N")
    return 0


def load_cached_slot(index: int) -> Slot:
    if not SLOTS_CACHE.exists():
        raise SystemExit("Нет сохранённых слотов — сначала выполните `slots`")
    raw = json.loads(SLOTS_CACHE.read_text("utf-8"))
    if not 1 <= index <= len(raw):
        raise SystemExit(f"Слот #{index} вне диапазона 1..{len(raw)}")
    return Slot(**raw[index - 1])


def cmd_book(args: argparse.Namespace) -> int:
    client = Emias()
    ehr_id = resolve_patient(client, args.patient)
    slot = load_cached_slot(args.slot)

    print("Будет создана запись:")
    print(f"  пациент:        {ehr_id}")
    print(
        f"  дата/время:     {slot.date} "
        f"{_clock(slot.start_time)}-{_clock(slot.end_time)}"
    )
    print(f"  специальность:  {slot.speciality or '—'}")
    print(f"  врач:           {slot.doctor or '—'}")
    print(f"  кабинет:        {slot.room or '—'}")
    if slot.address:
        print(f"  адрес:          {slot.address}")
    if not args.yes:
        print("\nЭто изменяет данные. Повторите с --yes для подтверждения.")
        print("Если сервер потребует жалобы — будет короткий опросник.")
        return 2

    speciality_id = args.speciality or slot.speciality_id
    purpose_id = args.purpose or slot.inquiry_purpose_id

    def make_booking() -> dict[str, Any]:
        return client.book(
            ehr_id,
            slot,
            speciality_id=speciality_id,
            reception_type_id=args.reception_type,
            inquiry_purpose_id=purpose_id,
            referral_id=args.referral,
        )

    try:
        result = make_booking()
    except EmiasError as exc:
        if exc.code != "NoComplaint" or not speciality_id:
            raise
        print(
            "\nДля записи нужно заполнить жалобы (опросник перед приёмом).\n"
            "Ответьте на вопросы — это те же данные, что спрашивает сайт."
        )
        info = collect_complaints(client, ehr_id, speciality_id)
        if not info:
            print("Жалобы не заполнены — запись отменена.")
            return 2
        print("\nЖалобы сохранены, повторяю запись...")
        result = make_booking()

    appointment = result.get("appointment") if isinstance(result, dict) else None
    appointment_id = find_key(appointment, {"appointmentId", "id"}) if appointment else None
    if appointment_id:
        print(f"\nГотово: запись создана, appointmentId={appointment_id}")
    dump(result, args.out)
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    client = Emias()
    ehr_id = resolve_patient(client, args.patient)
    if not args.yes:
        print(f"Отмена записи {args.id}. Повторите с --yes.")
        return 2
    dump(client.cancel_appointment(ehr_id, args.id), args.out)
    return 0


def cmd_appointments(args: argparse.Namespace) -> int:
    client = Emias()
    ehr_id = resolve_patient(client, args.patient)
    data = client.appointments(ehr_id)
    if args.json or args.out:
        dump(data, args.out)
        return 0
    records = extract_records(data, {"appointments", "items", "records"})
    if not records:
        print("Активных записей нет")
        return 0
    print(f"{'дата':<11} {'время':<13} {'специальность':<30} {'врач':<24} где")
    print("-" * 108)
    for item in records:
        doctor = item.get("doctor") if isinstance(item.get("doctor"), dict) else {}
        start = item.get("startTime") or item.get("date") or ""
        end = item.get("endTime") or ""
        clock = _clock(start)
        if end:
            clock = f"{clock}-{_clock(end)}"
        speciality = (
            item.get("specialityName")
            or item.get("speciality")
            or doctor.get("specialityName")
        )
        doc_name = item.get("doctorName") or doctor.get("name") or item.get("employeeFio")
        place = item.get("lpuName") or item.get("muName") or item.get("address")
        print(
            f"{_day(start):<11} {clock:<13} {_cut(speciality, 30):<30} "
            f"{_cut(doc_name, 24):<24} {_cut(place, 30)}"
        )
    return 0


def cmd_raw(args: argparse.Namespace) -> int:
    client = Emias()
    query: dict[str, Any] = {}
    for item in args.query or []:
        key, _, value = item.partition("=")
        query[key] = value
    body = json.loads(args.body) if args.body else None
    data = client.request(args.method.upper(), args.path, version=args.version, query=query, body=body)
    dump(data, args.out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="emias",
        description="Клиент медкарты ЕМИАС (lk.emias.mos.ru)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_common(p: argparse.ArgumentParser, with_period: bool = False) -> None:
        p.add_argument("--patient", "-p", help="ehrId пациента (по умолчанию свой)")
        p.add_argument("--out", help="сохранить JSON в файл")
        p.add_argument("--json", action="store_true", help="вывести сырой JSON")
        p.add_argument("--limit", type=int, default=50, help="сколько строк показать")
        if with_period:
            p.add_argument(
                "--period",
                default="half_year",
                choices=["half_year", "year", "all_time"],
                help="период (по умолчанию half_year)",
            )

    p = sub.add_parser("login", help="войти через mos.ru")
    p.add_argument("--code", help="OAuth-код из URL после входа (необязательно)")
    p.add_argument("--no-browser", action="store_true", help="не открывать браузер")
    p.set_defaults(func=cmd_login)

    p = sub.add_parser(
        "import-session",
        help="сохранить токены, которые уже есть в браузере (localStorage)",
    )
    p.add_argument(
        "--tokens",
        help="JSON с токенами; если не указан, читается stdin (удобно с pbpaste)",
    )
    p.add_argument("--access", help="access_token вручную")
    p.add_argument("--refresh", help="refresh_token вручную")
    p.set_defaults(func=cmd_import_session)

    p = sub.add_parser("logout", help="удалить сохранённые токены")
    p.set_defaults(func=cmd_logout)

    p = sub.add_parser("whoami", help="сырой профиль из API")
    p.add_argument("--out")
    p.set_defaults(func=cmd_whoami)

    p = sub.add_parser("patients", help="список пациентов")
    p.add_argument("--json", action="store_true")
    p.add_argument("--out")
    p.set_defaults(func=cmd_patients)

    p = sub.add_parser("visits", help="посещения врача")
    add_common(p, with_period=True)
    p.set_defaults(func=_doc_command("visits"))

    p = sub.add_parser("analyzes", help="анализы")
    add_common(p, with_period=True)
    p.set_defaults(func=_doc_command("analyzes"))

    p = sub.add_parser("vaccinations", help="прививки")
    add_common(p)
    p.set_defaults(func=_doc_command("vaccinations"))

    p = sub.add_parser("prescriptions", help="назначения/рецепты")
    add_common(p, with_period=True)
    p.set_defaults(func=_doc_command("prescriptions"))

    p = sub.add_parser("referrals", help="направления")
    add_common(p)
    p.set_defaults(func=_doc_command("referrals"))

    p = sub.add_parser("appointments", help="текущие записи к врачу")
    add_common(p)
    p.set_defaults(func=cmd_appointments)

    p = sub.add_parser("specialities", help="специальности для записи")
    add_common(p)
    p.set_defaults(func=cmd_specialities)

    p = sub.add_parser("purposes", help="поводы обращения для записи")
    add_common(p)
    p.set_defaults(func=cmd_purposes)

    p = sub.add_parser("slots", help="свободное время для записи")
    add_common(p)
    p.add_argument("--speciality", help="id специальности")
    p.add_argument("--lpu", help="id поликлиники")
    p.add_argument("--purpose", help="id повода обращения")
    p.add_argument("--referral", help="id направления")
    p.set_defaults(func=cmd_slots)

    p = sub.add_parser("book", help="записаться на слот из последнего `slots`")
    p.add_argument("--slot", type=int, required=True, help="номер слота из `slots`")
    p.add_argument("--patient", "-p")
    p.add_argument("--speciality")
    p.add_argument("--purpose")
    p.add_argument("--referral")
    p.add_argument("--reception-type")
    p.add_argument("--out")
    p.add_argument("--yes", action="store_true", help="подтвердить запись")
    p.set_defaults(func=cmd_book)

    p = sub.add_parser("cancel", help="отменить запись")
    p.add_argument("--id", required=True, help="id приёма")
    p.add_argument("--patient", "-p")
    p.add_argument("--out")
    p.add_argument("--yes", action="store_true")
    p.set_defaults(func=cmd_cancel)

    p = sub.add_parser("raw", help="произвольный запрос к API")
    p.add_argument("method", choices=["get", "post", "put", "patch", "delete"])
    p.add_argument("path", help="например /documents/sections")
    p.add_argument("--version", default="/1")
    p.add_argument("--query", action="append", help="k=v, можно несколько")
    p.add_argument("--body", help="JSON-тело")
    p.add_argument("--out")
    p.set_defaults(func=cmd_raw)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\nПрервано", file=sys.stderr)
        return 130
    except AuthRequired as exc:
        print(f"Нужен вход: {exc}\nВыполните: python -m emias login", file=sys.stderr)
        return 1
    except EmiasError as exc:
        print(f"Ошибка API: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
