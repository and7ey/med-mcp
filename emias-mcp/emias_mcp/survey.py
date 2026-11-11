"""Представление опросника жалоб для MCP и сборка complaintsJson.

MCP не задаёт вопросы сам: агент показывает пользователю теги и вопросы,
затем передаёт ответы в структурированном виде. Здесь они валидируются и
превращаются в формат ЕМИАС.
"""

from __future__ import annotations

from typing import Any, Iterable

from emias.interview import _answer_entry, fetch_tags, unique_tags


def tags_payload(tags: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "tag_code": tag.get("tagCode"),
            "title": tag.get("tagTitle") or "",
            "has_questions": bool(tag.get("questionsForTag")),
        }
        for tag in unique_tags(tags)
    ]


def questions_payload(
    tags: Iterable[dict[str, Any]], tag_codes: Iterable[int | str]
) -> list[dict[str, Any]]:
    wanted = {int(code) for code in tag_codes}
    result = []
    for tag in unique_tags(tags):
        if tag.get("tagCode") not in wanted:
            continue
        questions = []
        for question in tag.get("questionsForTag") or []:
            if not isinstance(question, dict):
                continue
            questions.append(
                {
                    "question_code": str(question.get("code")),
                    "title": question.get("title") or "",
                    "type": question.get("type"),
                    "step": question.get("stepNumber"),
                    "answers": [
                        {
                            "answer_code": str(answer.get("code")),
                            "title": answer.get("title") or "",
                        }
                        for answer in question.get("answers") or []
                        if isinstance(answer, dict)
                    ],
                }
            )
        result.append(
            {
                "tag_code": tag.get("tagCode"),
                "title": tag.get("tagTitle") or "",
                "questions": questions,
                "needs_text": not questions,
            }
        )
    return result


def build_complaints(
    tags: Iterable[dict[str, Any]],
    complaints: Iterable[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Собрать tagSurvey из ответов агента.

    ``complaints``: ``[{"tag_code": 3, "tag_input_text": null,
    "answers": [{"question_code": "30", "answer_code": "3001"},
                {"question_code": "31", "text": "два дня"}]}]``

    Возвращает (survey, errors). При ошибках survey неполный и отправлять его
    нельзя.
    """
    by_code = {int(tag["tagCode"]): tag for tag in unique_tags(tags) if tag.get("tagCode") is not None}
    survey: list[dict[str, Any]] = []
    errors: list[str] = []

    for complaint in complaints:
        if not isinstance(complaint, dict):
            errors.append("complaint должен быть объектом")
            continue
        try:
            tag_code = int(complaint.get("tag_code"))
        except (TypeError, ValueError):
            errors.append("tag_code обязателен")
            continue
        tag = by_code.get(tag_code)
        if tag is None:
            errors.append(f"tag_code {tag_code} отсутствует в опроснике")
            continue

        raw_answers = {
            str(answer.get("question_code")): answer
            for answer in complaint.get("answers") or []
            if isinstance(answer, dict) and answer.get("question_code") is not None
        }

        entry: dict[str, Any] = {
            "tagCode": tag_code,
            "tagTitle": tag.get("tagTitle") or "",
            "tagInputText": _text_or_none(complaint.get("tag_input_text")),
            "childTagCode": None,
            "childTagTitle": None,
            "childTagInputText": None,
            "syntheticTagCode": tag.get("syntheticTagCode"),
            "patientAnswer": [],
        }

        questions = [q for q in tag.get("questionsForTag") or [] if isinstance(q, dict)]
        order = [str(q.get("code")) for q in questions]
        by_question = {str(q.get("code")): q for q in questions}
        cursor = 0
        asked = 0
        while cursor < len(order) and asked < 60:
            code = order[cursor]
            question = by_question[code]
            answer = raw_answers.get(code)
            if answer is None:
                errors.append(
                    f"tag {tag_code}: нет ответа на вопрос {code} "
                    f"«{question.get('title')}»"
                )
                break

            options = [a for a in question.get("answers") or [] if isinstance(a, dict)]
            chosen = None
            text = _text_or_none(answer.get("text"))
            answer_code = answer.get("answer_code")
            if options:
                chosen = next(
                    (a for a in options if str(a.get("code")) == str(answer_code)), None
                )
                if chosen is None:
                    allowed = ", ".join(str(a.get("code")) for a in options)
                    errors.append(
                        f"tag {tag_code}, вопрос {code}: answer_code "
                        f"{answer_code!r} не из списка ({allowed})"
                    )
                    break
            elif not text:
                errors.append(
                    f"tag {tag_code}, вопрос {code}: нужен текстовый ответ (text)"
                )
                break

            entry["patientAnswer"].append(_answer_entry(question, chosen, None if chosen else text))
            asked += 1
            next_code = chosen.get("nextQuestionCode") if chosen else None
            if next_code is not None and str(next_code) in by_question:
                cursor = order.index(str(next_code))
            else:
                cursor += 1

        survey.append(entry)

    return survey, errors


def _text_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
