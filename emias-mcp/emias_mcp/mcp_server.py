"""MCP-сервер (stdio) для медкарты ЕМИАС.

Инструменты возвращают нормализованные DTO и никогда — токены, пароли,
SMS-коды, номера полисов, сырые ответы API или URL с query.

Версия 0.2.0: read-only данные и запись/отмена к врачу через
prepare → явное подтверждение → execute.
"""

from __future__ import annotations

import atexit
import signal

from mcp.server.fastmcp import FastMCP

from .errors import McpError
from .service import EmiasService

INSTRUCTIONS = """\
Сервер медкарты ЕМИАС (lk.emias.mos.ru).

Порядок работы:
1. session_status — узнать состояние: bootstrap (нужен вход), quick_login или ready.
2. Если нужен вход — open_bootstrap, войдите в открывшемся Chrome на сайте mos.ru
   (телефон/пароль/код). Секреты вводятся только на mos.ru, не в чате.
3. bootstrap_status — проверить, пойманы ли токены; затем quick_login.
4. patients — кто доступен; далее visits, analyzes, vaccinations, prescriptions,
   referrals, appointments, specialities, purposes, slots.

Все инструменты только читают, кроме записи/отмены. Браузер используется ТОЛЬКО для
первичного входа.

Запись к врачу — строго два шага:
1. slots → booking_prepare (ничего не меняет) → покажите пользователю дату, время,
   врача и кабинет.
2. Только после отдельного явного подтверждения — booking_execute(confirm_token).
   Токен одноразовый, живёт 5 минут. Если booking_prepare вернул needs_complaints,
   сначала complaint_tags → complaint_questions, задайте вопросы пользователю и
   передайте ответы в booking_execute(complaints=[...]). Жалобы попадают в медкарту:
   не придумывайте симптомы.

Отмена: cancel_prepare → показать детали → отдельное подтверждение → cancel_execute.
При неизвестном исходе (сеть, таймаут) НЕ повторяйте execute: проверьте appointments.

В ответах нет токенов, полиса, СНИЛС и сырых JSON. Никогда не просите
пользователя прислать токен или код из SMS в чат.
"""

mcp = FastMCP("emias", instructions=INSTRUCTIONS, log_level="WARNING")

_service: EmiasService | None = None


def _service_instance() -> EmiasService:
    global _service
    if _service is None:
        _service = EmiasService()
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
    """Открыть Chrome на официальной странице входа mos.ru.

    Пользователь вводит телефон, пароль и код только на сайте mos.ru; сервер
    забирает токены из localStorage после входа. Верните пользователю сообщение
    и попросите завершить вход в браузере.
    """
    return _safe(_service_instance().open_bootstrap)


@mcp.tool()
def bootstrap_status() -> dict:
    """Проверить, завершён ли вход в браузере (waiting / ready / timeout)."""
    return _safe(_service_instance().bootstrap_status)


@mcp.tool()
def close_bootstrap() -> dict:
    """Закрыть окно входа, если оно больше не нужно."""
    return _safe(_service_instance().close_bootstrap)


@mcp.tool()
def quick_login() -> dict:
    """Проверить сохранённые токены read-only запросом профиля.

    При истёкшем access_token выполняется один refresh. Возвращает только
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
def patients() -> dict:
    """Список доступных пациентов: patient_id, ФИО, дата рождения, тип доступа.

    patient_id — UUID для остальных инструментов.
    """
    return _safe(_service_instance().patients)


@mcp.tool()
def visits(
    patient_id: str | None = None,
    period: str = "half_year",
    limit: int = 50,
) -> dict:
    """Посещения врача (осмотры), read-only.

    period: half_year | year | all_time. patient_id по умолчанию — свой профиль.
    Возвращает дату, время, специальность, врача, организацию, document_id.
    """
    return _safe(_service_instance().visits, patient_id, period, limit)


@mcp.tool()
def analyzes(
    patient_id: str | None = None,
    period: str = "half_year",
    limit: int = 50,
) -> dict:
    """Анализы, read-only: дата, код, название, признак COVID, document_id."""
    return _safe(_service_instance().analyzes, patient_id, period, limit)


@mcp.tool()
def vaccinations(patient_id: str | None = None, limit: int = 100) -> dict:
    """Прививки: дата, инфекции, вакцина, доза, статус."""
    return _safe(_service_instance().vaccinations, patient_id, limit)


@mcp.tool()
def prescriptions(
    patient_id: str | None = None,
    period: str = "half_year",
    limit: int = 50,
) -> dict:
    """Лекарственные назначения/рецепты: дата, препарат, статус, id."""
    return _safe(_service_instance().prescriptions, patient_id, period, limit)


@mcp.tool()
def referrals(patient_id: str | None = None, limit: int = 50) -> dict:
    """Направления: дата действия, тип, организация, врач."""
    return _safe(_service_instance().referrals, patient_id, limit)


@mcp.tool()
def appointments(patient_id: str | None = None) -> dict:
    """Текущие записи к врачу: дата, время, специальность, врач, место."""
    return _safe(_service_instance().appointments, patient_id)


@mcp.tool()
def specialities(patient_id: str | None = None) -> dict:
    """Специальности, доступные для записи: id, название, платно/бесплатно."""
    return _safe(_service_instance().specialities, patient_id)


@mcp.tool()
def purposes(patient_id: str | None = None) -> dict:
    """Поводы обращения для записи, сгруппированные по категориям."""
    return _safe(_service_instance().purposes, patient_id)


@mcp.tool()
def slots(
    speciality_id: str,
    purpose_id: str,
    patient_id: str | None = None,
    lpu_id: str | None = None,
    referral_id: str | None = None,
    limit: int = 50,
) -> dict:
    """Свободное время для записи, read-only.

    Обязательны speciality_id и purpose_id (см. specialities и purposes).
    Возвращает дату, время, врача, кабинет и resource_id. Запись не выполняется.
    """
    return _safe(
        _service_instance().slots,
        speciality_id,
        purpose_id,
        patient_id,
        lpu_id,
        referral_id,
        limit,
    )


# --------------------------------------------------------------------------
# Запись к врачу (prepare -> подтверждение -> execute)
# --------------------------------------------------------------------------


@mcp.tool()
def complaint_tags(patient_id: str | None = None) -> dict:
    """Список жалоб для опросника (read-only): tag_code и название.

    Используется только если booking_prepare вернул needs_complaints=true.
    """
    return _safe(_service_instance().complaint_tags, patient_id)


@mcp.tool()
def complaint_questions(
    tag_codes: list[int | str], patient_id: str | None = None
) -> dict:
    """Вопросы и варианты ответов по выбранным жалобам (read-only).

    Задайте пользователю вопросы из ответа и передайте его ответы в
    booking_execute(complaints=[...]).
    """
    return _safe(_service_instance().complaint_questions, tag_codes, patient_id)


@mcp.tool()
def booking_prepare(
    resource_id: str,
    start_time: str,
    end_time: str,
    purpose_id: str,
    speciality_id: str,
    patient_id: str | None = None,
    complex_resource_id: str | None = None,
) -> dict:
    """Подготовить запись: проверить слот и вернуть детали + confirm_token.

    Ничего не меняет. Возьмите resource_id/start_time/end_time из slots.
    Покажите пользователю дату, врача, кабинет и вызовите booking_execute
    только после отдельного явного подтверждения.
    """
    return _safe(
        _service_instance().booking_prepare,
        resource_id,
        start_time,
        end_time,
        purpose_id,
        speciality_id,
        patient_id,
        complex_resource_id,
    )


@mcp.tool()
def booking_execute(
    confirm_token: str,
    complaints: list[dict] | None = None,
) -> dict:
    """Создать запись по confirm_token из booking_prepare.

    Вызывайте только после явного подтверждения пользователя. Токен
    одноразовый, действует 5 минут. Если нужны жалобы — сначала
    complaint_tags → complaint_questions, затем передайте:
    [{"tag_code": 3, "answers": [{"question_code": "30",
      "answer_code": "3001"}, {"question_code": "31", "text": "..."}]}].

    При неизвестном исходе (сеть) НЕ повторяйте: проверьте appointments.
    """
    return _safe(_service_instance().booking_execute, confirm_token, complaints)


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

    При неизвестном исходе не повторяйте: проверьте appointments.
    """
    return _safe(_service_instance().cancel_execute, confirm_token)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
