"""Fast grading for open menu-training answers.

Factual questions are checked locally against the approved TTK facts. Sales-description
questions still try the AI grader, but fall back to a source-only local check after a few
seconds so the trainee is never asked to resend the same answer.
"""
from __future__ import annotations

import asyncio
import re
from typing import Any

import menu_training

_INSTALLED = False
_ORIGINAL_GRADE = None

_STOPWORDS = {
    "это", "как", "для", "при", "или", "его", "ее", "её", "они", "она", "оно",
    "что", "где", "когда", "который", "которая", "которые", "блюдо", "блюда",
    "соус", "соусом", "подается", "подаётся", "входит", "есть", "имеет", "имеются",
    "the", "and", "with", "from", "that", "this", "dish",
}


def _norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _tokens(text: str) -> set[str]:
    return {
        t for t in _norm(text).split()
        if len(t) >= 3 and t not in _STOPWORDS
    }


def _fact_hit(answer_norm: str, fact: str) -> bool:
    f_norm = _norm(fact)
    if not f_norm:
        return False
    if f_norm in answer_norm:
        return True
    ft = _tokens(fact)
    at = _tokens(answer_norm)
    if not ft:
        return False
    # Multi-word ingredients / phrases may be paraphrased slightly. Requiring 2/3 of
    # meaningful tokens is strict enough for a fallback while staying source-grounded.
    overlap = len(ft & at) / max(1, len(ft))
    return overlap >= (0.67 if len(ft) >= 2 else 1.0)


def _local_grade(q: dict[str, Any], answer: str, *, ai_timeout: bool = False) -> dict[str, Any]:
    answer_norm = _norm(answer)
    dimension = str(q.get("dimension") or "")
    facts = [str(x).strip() for x in (q.get("facts") or []) if str(x).strip()]
    model_answer = str(q.get("model_answer") or "").strip()

    if dimension in {"ingredients", "allergens"} and facts:
        hits = sum(1 for fact in facts if _fact_hit(answer_norm, fact))
        coverage = hits / max(1, len(facts))
        score = round(coverage * 10)
        correct = coverage >= 0.70
        missing = [f for f in facts if not _fact_hit(answer_norm, f)]
        if correct:
            feedback = f"По ТТК названо {hits} из {len(facts)} ключевых пунктов."
        else:
            tail = ", ".join(missing[:4])
            feedback = f"По ТТК совпало {hits} из {len(facts)}. Стоит добавить: {tail}." if tail else "Ответ неполный по ТТК."
        return {
            "score": float(score),
            "correct": correct,
            "feedback": feedback,
            "ideal_answer": model_answer,
        }

    reference = " ".join(facts + ([model_answer] if model_answer else []))
    ref_tokens = _tokens(reference)
    ans_tokens = _tokens(answer)
    overlap = len(ref_tokens & ans_tokens)
    ratio = overlap / max(1, min(len(ref_tokens), 14))

    if dimension == "sales":
        # Selling answers can be legitimately paraphrased, so use a forgiving factual
        # coverage estimate for the fallback. AI remains the preferred grader.
        base = 4.0 if len(answer_norm) >= 35 else 2.5
        score = min(10.0, base + min(6.0, ratio * 10.0))
        correct = score >= 6.5
        suffix = " ИИ не успел ответить, поэтому это быстрая проверка по фактам ТТК." if ai_timeout else ""
        feedback = (
            "Ответ опирается на факты из ТТК и достаточно содержательный."
            if correct
            else "В ответе мало опорных фактов из ТТК — добавь состав, вкус или особенности подачи, которые есть в карточке."
        ) + suffix
        return {
            "score": round(score, 1),
            "correct": correct,
            "feedback": feedback,
            "ideal_answer": model_answer,
        }

    # Technology / serving / any other open factual answer.
    score = min(10.0, ratio * 12.0)
    correct = score >= 6.0
    return {
        "score": round(score, 1),
        "correct": correct,
        "feedback": (
            "Ключевые факты из ТТК отражены."
            if correct
            else "Ответ стоит дополнить фактами из утверждённой ТТК."
        ),
        "ideal_answer": model_answer,
    }


async def _hybrid_grade(q: dict[str, Any], answer: str) -> dict[str, Any]:
    dimension = str(q.get("dimension") or "")

    # These are objective source-fact questions. No reason to spend an AI round-trip.
    if dimension in {"ingredients", "allergens", "preparation", "serving"}:
        return _local_grade(q, answer)

    # Sales wording benefits from AI judgement, but UX must not block on model latency.
    if _ORIGINAL_GRADE is not None:
        try:
            return await asyncio.wait_for(_ORIGINAL_GRADE(q, answer), timeout=6.0)
        except asyncio.TimeoutError:
            return _local_grade(q, answer, ai_timeout=True)
        except Exception as exc:
            print(f"[menu-training fast-grade] AI fallback: {exc!r}")
            return _local_grade(q, answer, ai_timeout=True)

    return _local_grade(q, answer, ai_timeout=True)


def install() -> None:
    global _INSTALLED, _ORIGINAL_GRADE
    if _INSTALLED:
        return
    _ORIGINAL_GRADE = menu_training._grade_open
    menu_training._grade_open = _hybrid_grade
    _INSTALLED = True
    print("[menu-training] fast open-answer grading enabled")
