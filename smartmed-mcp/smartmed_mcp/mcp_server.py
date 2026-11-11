"""MCP-сервер (stdio) личного кабинета SmartMed (smartmed.pro).

Инструменты возвращают нормализованные DTO и никогда — токены, телефон,
SMS-коды, сырые ответы API или URL с query.

Версия 0.2.0: read-only данные и запись/отмена к врачу через
prepare → явное подтверждение → execute. Вход — через официальный
веб-кабинет в открытом Chrome.
"""

from __future__ import annotations

import atexit
import signal

from mcp.server.fastmcp import FastMCP

from .errors import McpError
from .service import SmartMedService

INSTRUCTIONS = """\
Сервер личного кабинета SmartMed (smartmed.pro).

Порядок работы:
1. session_status — узнать состояние: bootstrap (нужен вход), quick_login или ready.
2. Если нужен вход — open_bootstrap, войдите в открывшемся Chrome на сайте
   smartmed.pro (телефон и код из SMS). Секреты вводятся только на сайте,
   никогда — в чате.
3. bootstrap_status — проверить, пойманы ли токены; затем quick_login.
4. profile, patients — кто доступен; далее appointments, documents,
   document_download, document_tags, referrals, prescriptions.

Все инструменты только читают, кроме записи/отмены. Браузер используется
ТОЛЬКО для входа.

Запись к врачу — управляемый сценарий:
cities → specialties → clinics → available_dates → slots → booking_prepare
(ничего не меняет) → покажите врача, клинику, дату/время и цену → отдельное
явное подтверждение пользователя → booking_execute(confirm_token) →
проверьте appointments. Токен одноразовый, живёт 5 минут. Статус reserved
значит, что клиника ещё не подтвердила запись.

Отмена: cancel_prepare → показать детали → подтверждение → cancel_execute.
При неизвестном исходе (сеть, таймаут) НЕ повторяйте execute — проверьте
appointments.

Никогда не просите пользователя прислать токен или код из SMS в чат.
В ответах нет токенов, телефона, сырых JSON и URL с query.
"""

mcp = FastMCP("smartmed", instructions=INSTRUCTIONS, log_level="WARNING")

_service: SmartMedService | None = None


def _service_instance() -> SmartMedService:
    global _service
    if _service is None:
        _service = SmartMedService()
    return _service


def _safe(call, *args, **kwargs) -> dict:
    try:
        service = _service_instance()
    except Exception:
        return {
            "ok": False,
            "error": "internal_error",
            "message": "Не удалось инициализировать сервер. Проверьте права на каталог состояния.",
        }
    try:
        return call(*args, **kwargs)
    except McpError as exc:
        service.log.event("tool_error", error=exc.code, http_status=exc.http_status)
        return exc.to_dict()
    except Exception as exc:
        service.log.event("tool_error", error=type(exc).__name__)
        return {
            "ok": False,
            "error": "internal_error",
            "message": "Внутренняя ошибка сервера. Подробности записаны в безопасный журнал.",
        }


def _shutdown() -> None:
    global _service
    if _service is not None:
        try:
            _service.bootstrap.close()
        except Exception:
            pass
        _service = None


def _handle_signal(signum, frame) -> None:
    _shutdown()
    raise SystemExit(0)


atexit.register(_shutdown)
for _sig in (signal.SIGINT, signal.SIGTERM):
    try:
        signal.signal(_sig, _handle_signal)
    except (ValueError, OSError):
        pass


# --------------------------------------------------------------------------
# Сессия и вход
# --------------------------------------------------------------------------


@mcp.tool()
def session_status() -> dict:
    """Показать состояние входа: bootstrap / login_pending / quick_login / ready.

    Не возвращает токены и телефон. Вызывайте первым.
    """
    return _safe(_service_instance().status)


@mcp.tool()
def open_bootstrap() -> dict:
    """Открыть Chrome на официальной странице входа smartmed.pro.

    Пользователь вводит телефон и код из SMS на сайте SmartMed; сервер
    забирает токены после входа. Верните пользователю сообщение и попросите
    завершить вход в браузере.
    """
    return _safe(_service_instance().open_bootstrap)


@mcp.tool()
def bootstrap_status() -> dict:
    """Проверить, завершён ли вход в браузере (waiting / ready / timeout / closed)."""
    return _safe(_service_instance().bootstrap_status)


@mcp.tool()
def close_bootstrap() -> dict:
    """Закрыть окно входа, если оно больше не нужно."""
    return _safe(_service_instance().close_bootstrap)


@mcp.tool()
def quick_login() -> dict:
    """Проверить сохранённые токены read-only запросом списка пациентов.

    При истёкшем access-токене выполняется один refresh. Возвращает только
    статус и количество доступных пациентов.
    """
    return _safe(_service_instance().quick_login)


@mcp.tool()
def refresh_tokens() -> dict:
    """Обновить access/refresh токены. При отказе потребуется open_bootstrap."""
    return _safe(_service_instance().refresh_tokens)


@mcp.tool()
def logout() -> dict:
    """Удалить сохранённые токены из Keychain/файла состояния."""
    return _safe(_service_instance().logout)


# --------------------------------------------------------------------------
# Read-only данные
# --------------------------------------------------------------------------


@mcp.tool()
def profile() -> dict:
    """Профиль владельца аккаунта: user_id, ФИО, дата рождения, маска телефона."""
    return _safe(_service_instance().profile)


@mcp.tool()
def patients() -> dict:
    """Список доступных пациентов: patient_id, ФИО, дата рождения, тип доступа.

    patient_id — числовой id пользователя для остальных инструментов.
    """
    return _safe(_service_instance().patients)


@mcp.tool()
def appointments(
    patient_id: str | None = None,
    kind: str = "upcoming",
    limit: int = 20,
    page: int = 1,
) -> dict:
    """Приёмы из медкарты, read-only.

    kind: upcoming (текущие/предстоящие) | history (завершённые и отменённые) |
    all. Возвращает дату, время, тип, состояние, специальность, врача, клинику
    и кабинет. patient_id по умолчанию — пациент по умолчанию в аккаунте.
    """
    return _safe(_service_instance().appointments, patient_id, kind, limit, page)


@mcp.tool()
def documents(
    patient_id: str | None = None,
    limit: int = 20,
    page: int = 0,
    search: str | None = None,
    tag_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> dict:
    """Документы медкарты, read-only: file_id, название, дата, врач, теги.

    date_from/date_to — ISO-дата (YYYY-MM-DD). tag_id — из document_tags.
    """
    return _safe(
        _service_instance().documents,
        patient_id,
        limit,
        page,
        search,
        tag_id,
        date_from,
        date_to,
    )


@mcp.tool()
def document_tags(patient_id: str | None = None) -> dict:
    """Теги документов медкарты (read-only): tag_id и название."""
    return _safe(_service_instance().document_tags, patient_id)


@mcp.tool()
def document_download(file_id: str, patient_id: str | None = None) -> dict:
    """Скачать документ медкарты в локальный каталог состояния (права 0600).

    file_id — из documents. Возвращает локальный путь, размер и формат;
    содержимое файла в чат не выводится.
    """
    return _safe(_service_instance().document_download, file_id, patient_id)


@mcp.tool()
def referrals(
    patient_id: str | None = None,
    limit: int = 25,
    page: int = 1,
    search: str | None = None,
    active_only: bool | None = None,
    sort: str = "date_desc",
) -> dict:
    """Направления, read-only: название, тип, врач, состояние, активность.

    active_only=True — только активные, False — только архивные, None — все.
    sort: date_desc | date_asc.
    """
    return _safe(
        _service_instance().referrals,
        patient_id,
        limit,
        page,
        search,
        active_only,
        sort,
    )


@mcp.tool()
def prescriptions(
    patient_id: str | None = None,
    scope: str = "active",
    limit: int = 20,
    page: int = 1,
) -> dict:
    """Назначения врача, read-only: препарат, форма, дозировка, курс, рецепт.

    scope: active | archive | all.
    """
    return _safe(_service_instance().prescriptions, patient_id, scope, limit, page)


# --------------------------------------------------------------------------
# Запись к врачу (prepare -> подтверждение -> execute)
# --------------------------------------------------------------------------


@mcp.tool()
def cities(search: str | None = None) -> dict:
    """Города, где доступна запись (read-only): city_id и название."""
    return _safe(_service_instance().cities, search)


@mcp.tool()
def specialties(
    city_id: str | None = None,
    search: str | None = None,
    limit: int = 50,
) -> dict:
    """Специальности для записи в городе (read-only): specialty_id и название.

    city_id по умолчанию — город пользователя.
    """
    return _safe(_service_instance().specialties, city_id, search, limit)


@mcp.tool()
def clinics(specialty_id: str, city_id: str | None = None) -> dict:
    """Клиники, где принимает специальность (read-only): clinic_id, code, адрес.

    clinic_code из ответа нужен для booking_prepare.
    """
    return _safe(_service_instance().clinics, specialty_id, city_id)


@mcp.tool()
def doctors(
    specialty_id: str | None = None,
    clinic_id: str | None = None,
    city_id: str | None = None,
    search: str | None = None,
    online: bool = False,
    limit: int = 20,
) -> dict:
    """Врачи по специальности/клинике (read-only): doctor_id, ФИО, специальности."""
    return _safe(
        _service_instance().doctors,
        specialty_id,
        clinic_id,
        city_id,
        search,
        online,
        limit,
    )


@mcp.tool()
def available_dates(
    specialty_id: str,
    clinic_ids: list[str],
    patient_id: str | None = None,
) -> dict:
    """Даты, когда есть свободное время (read-only): date и is_available."""
    return _safe(_service_instance().available_dates, specialty_id, clinic_ids, patient_id)


@mcp.tool()
def slots(
    specialty_id: str,
    clinic_ids: list[str],
    date: str,
    patient_id: str | None = None,
    limit: int = 50,
) -> dict:
    """Свободное время на дату (read-only, запись не создаётся).

    date — YYYY-MM-DD из available_dates. Возвращает врача, клинику, кабинет,
    цену и поля schedule_id / reception_type_id / start_utcz / clinic_code
    для booking_prepare.
    """
    return _safe(
        _service_instance().slots,
        specialty_id,
        clinic_ids,
        date,
        patient_id,
        limit,
    )


@mcp.tool()
def booking_prepare(
    schedule_id: str,
    reception_type_id: str,
    start_utcz: str,
    clinic_code: str,
    patient_id: str | None = None,
    doctor: str | None = None,
    clinic: str | None = None,
    address: str | None = None,
    specialty: str | None = None,
    date: str | None = None,
    time: str | None = None,
    price: float | None = None,
    med_program_id: str | None = None,
) -> dict:
    """Подготовить запись: проверить слот и вернуть детали + confirm_token.

    Ничего не меняет. Возьмите поля из slots. Покажите пользователю врача,
    клинику, дату/время и цену и вызывайте booking_execute только после
    отдельного явного подтверждения.
    """
    summary = {
        key: value
        for key, value in {
            "doctor": doctor,
            "clinic": clinic,
            "address": address,
            "specialty": specialty,
            "date": date,
            "time": time,
            "price": price,
            "fm_clink_patient_id": med_program_id,
        }.items()
        if value is not None
    }
    return _safe(
        _service_instance().booking_prepare,
        schedule_id,
        reception_type_id,
        start_utcz,
        clinic_code,
        patient_id,
        summary,
    )


@mcp.tool()
def booking_execute(confirm_token: str) -> dict:
    """Создать запись по confirm_token из booking_prepare.

    Вызывайте только после явного подтверждения пользователя. Токен
    одноразовый, действует 5 минут. При неизвестном исходе НЕ повторяйте:
    проверьте appointments. Статус reserved означает, что клиника ещё не
    подтвердила запись.
    """
    return _safe(_service_instance().booking_execute, confirm_token)


@mcp.tool()
def cancel_prepare(appointment_id: str, patient_id: str | None = None) -> dict:
    """Подготовить отмену записи: показать детали и вернуть confirm_token.

    Ничего не меняет. Вызывайте cancel_execute только после отдельного
    подтверждения пользователя.
    """
    return _safe(_service_instance().cancel_prepare, appointment_id, patient_id)


@mcp.tool()
def cancel_execute(confirm_token: str) -> dict:
    """Отменить запись по confirm_token из cancel_prepare (один раз).

    После отмены сервер перепроверяет статус. При неизвестном исходе не
    повторяйте: проверьте appointments.
    """
    return _safe(_service_instance().cancel_execute, confirm_token)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
