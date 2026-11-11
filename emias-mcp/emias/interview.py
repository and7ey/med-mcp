"""Опросник жалоб перед записью к врачу.

Без заполненных жалоб ``reservation-confirm`` отвечает
``422 NoComplaint`` — «Актуальные жалобы отсутствуют». Схема (снята с UI):

1. ``POST /api/1/interview/create-dialog`` ``{ehrId, specialityId}``
   -> ``{code, complaintId}``;
2. ``POST /api/1/self-appointment/interview/get-questions``
   ``{ehrId, tagCodes: [], isDrzNeeded}`` -> дерево жалоб и вопросов;
3. ``POST /api/1/self-appointment/interview/save-answers``
   ``{ehrId, hash: code, complaintsJson: {tagSurvey: [...]}}``.

Здесь реализован минимальный интерактивный проход по этому дереву:
пользователь выбирает жалобы и отвечает на вопросы, после чего жалобы
сохраняются и запись можно подтвердить.
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

from .errors import EmiasError

MAX_QUESTIONS = 60


def fetch_tags(client: Any, ehr_id: str, is_drz: bool = True) -> list[dict[str, Any]]:
    """Список жалоб (верхний уровень дерева)."""
    data = client.request(
        "POST",
        "/self-appointment/interview/get-questions",
        body={"ehrId": ehr_id, "tagCodes": [], "isDrzNeeded": is_drz},
    )
    tags = data.get("questions") if isinstance(data, dict) else None
    return [tag for tag in tags or [] if isinstance(tag, dict)]


def unique_tags(tags: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Убрать дубли tagCode (варианты для пола/возраста), оставив первый."""
    seen: set[int] = set()
    result: list[dict[str, Any]] = []
    for tag in tags:
        code = tag.get("tagCode")
        if code is None or code in seen:
            continue
        seen.add(int(code))
        result.append(tag)
    return result


def _answer_entry(
    question: dict[str, Any], answer: dict[str, Any] | None, text: str | None = None
) -> dict[str, Any]:
    template = question.get("answerToDoctor")
    answer_to_doctor = None
    if answer and answer.get("answerToDoctor"):
        answer_to_doctor = answer["answerToDoctor"]
    elif template and text:
        answer_to_doctor = str(template).replace("<data>", text)

    numeric = None
    if text and str(text).replace(".", "", 1).isdigit():
        numeric = float(text)

    return {
        "stepNo": question.get("stepNumber") or 0,
        "questionCode": str(question.get("code")),
        "questionTitle": question.get("title") or "",
        "isPtv": question.get("isPtv"),
        "isDrz": bool(question.get("isDrz")),
        "answerToDoctor": answer_to_doctor,
        "answerCode": str(answer["code"]) if answer and answer.get("code") is not None else None,
        "answerTitle": answer.get("title") if answer else None,
        "answerInputText": text if not answer else None,
        "answerNumeric": numeric if not answer else None,
        "answerBloodPressure": [],
    }


def _ask_question(
    question: dict[str, Any],
    input_fn: Callable[[str], str],
    out: Callable[[str], None],
) -> tuple[dict[str, Any], int | None]:
    """Задать один вопрос. Возвращает (ответ, nextQuestionCode)."""
    answers = question.get("answers") or []
    qtype = str(question.get("type") or "")
    out(f"\n{question.get('title')}")

    if answers:
        for index, answer in enumerate(answers, start=1):
            out(f"  {index}. {answer.get('title')}")
        while True:
            raw = input_fn("Выберите номер (Enter — пропустить): ").strip()
            if not raw:
                return _answer_entry(question, None), None
            if raw.isdigit() and 1 <= int(raw) <= len(answers):
                chosen = answers[int(raw) - 1]
                return _answer_entry(question, chosen), chosen.get("nextQuestionCode")
            out("Нет такого варианта.")

    text = input_fn("Ответ (Enter — пропустить): ").strip()
    if not text:
        return _answer_entry(question, None), None
    return _answer_entry(question, None, text), None


def build_tag_survey(
    tag: dict[str, Any],
    input_fn: Callable[[str], str],
    out: Callable[[str], None],
) -> dict[str, Any]:
    """Пройти вопросы одной жалобы и собрать элемент tagSurvey."""
    questions = [q for q in tag.get("questionsForTag") or [] if isinstance(q, dict)]
    by_code = {str(q.get("code")): q for q in questions}
    order = [str(q.get("code")) for q in questions]

    entry: dict[str, Any] = {
        "tagCode": tag.get("tagCode"),
        "tagTitle": tag.get("tagTitle") or "",
        "tagInputText": None,
        "childTagCode": None,
        "childTagTitle": None,
        "childTagInputText": None,
        "syntheticTagCode": tag.get("syntheticTagCode"),
        "patientAnswer": [],
    }

    if not order:
        text = input_fn(f"Уточните: {tag.get('tagTitle')} (Enter — без уточнения): ").strip()
        entry["tagInputText"] = text or None
        return entry

    asked = 0
    cursor = 0
    while cursor < len(order) and asked < MAX_QUESTIONS:
        code = order[cursor]
        question = by_code.get(code)
        if question is None:
            cursor += 1
            continue
        answer, next_code = _ask_question(question, input_fn, out)
        entry["patientAnswer"].append(answer)
        asked += 1

        if next_code is not None and str(next_code) in by_code:
            cursor = order.index(str(next_code))
        else:
            cursor += 1

    return entry


def collect_complaints(
    client: Any,
    ehr_id: str,
    speciality_id: int | str,
    *,
    input_fn: Callable[[str], str] = input,
    out: Callable[[str], None] = print,
) -> dict[str, Any] | None:
    """Интерактивно собрать и сохранить жалобы.

    Возвращает ``{"code", "complaintId"}`` или ``None``, если пользователь
    ничего не выбрал.
    """
    tags = unique_tags(fetch_tags(client, ehr_id))
    if not tags:
        raise EmiasError("Сервер не вернул список жалоб")

    out("Что беспокоит? Выберите номера через запятую (Enter — отмена):")
    for index, tag in enumerate(tags, start=1):
        out(f"  {index:>2}. {tag.get('tagTitle')}")

    raw = input_fn("Жалобы: ").strip()
    if not raw:
        return None
    selected: list[dict[str, Any]] = []
    for part in raw.replace(" ", ",").split(","):
        if not part:
            continue
        if not part.isdigit() or not 1 <= int(part) <= len(tags):
            out(f"Пропускаю «{part}»: нет такого номера.")
            continue
        selected.append(tags[int(part) - 1])
    if not selected:
        return None

    survey = [build_tag_survey(tag, input_fn, out) for tag in selected]

    dialog = client.request(
        "POST",
        "/interview/create-dialog",
        body={"ehrId": ehr_id, "specialityId": speciality_id},
    )
    code = dialog.get("code")
    complaint_id = dialog.get("complaintId")
    if not code:
        raise EmiasError(f"create-dialog не вернул code: {dialog}")

    client.request(
        "POST",
        "/self-appointment/interview/save-answers",
        body={
            "ehrId": ehr_id,
            "hash": code,
            "complaintsJson": {"tagSurvey": survey},
        },
    )
    return {"code": code, "complaintId": complaint_id}


def complaint_required(client: Any, ehr_id: str, speciality_id: int | str) -> bool:
    """Нужно ли заполнять жалобы для этой специальности."""
    data = client.request(
        "GET",
        "/interview/get-complaint",
        query={"ehrId": ehr_id, "specialityId": str(speciality_id)},
    )
    if isinstance(data, dict):
        return data.get("complaintId") is None
    return True
