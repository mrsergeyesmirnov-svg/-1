"""Fast, source-grounded grading for open menu-training answers.

Objective list questions (ingredients/allergens) are checked locally. Narrative answers
(preparation/serving/sales) are judged semantically by AI when available, with a forgiving
source-grounded fallback so short correct paraphrases are not punished for wording.
"""
from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher
from typing import Any

import menu_training

_INSTALLED = False
_ORIGINAL_GRADE = None

_STOPWORDS = {
    "это", "как", "для", "при", "или", "его", "ее", "её", "они", "она", "оно",
    "что", "где", "когда", "который", "которая", "которые", "блюдо", "блюда",
    "соус", "соусом", "подается", "подаётся", "входит", "есть", "имеет", "имеются",
    "также", "нужно", "надо", "можно", "должен", "должна", "должно", "должны",
    "the", "and", "with", "from", "that", "this", "dish",
}

_RU_ENDINGS = (
    "иями", "ями", "ами", "ого", "ему", "ому", "ыми", "ими", "ее", "ие", "ые", "ое",
    "ей", "ий", "ый", "ой", "ая", "яя", "ую", "юю", "ов", "ев", "ом", "ем", "ах", "ях",
    "ам", "ям", "а", "я", "ы", "и", "у", "ю", "е", "о",
)


def _norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _stem(token: str) -> str:
    token = _norm(token)
    if len(token) < 5:
        return token
    for ending in _RU_ENDINGS:
        if token.endswith(ending) and len(token) - len(ending) >= 4:
            return token[:-len(ending)]
    return token


def _tokens(text: str) -> set[str]:
    return {
        t for t in _norm(text).split()
        if len(t) >= 3 and t not in _STOPWORDS
    }


def _token_match(left: str, right: str) -> bool:
    left_n = _norm(left)
    right_n = _norm(right)
    if not left_n or not right_n:
        return False
    if left_n == right_n:
        return True
    ls = _stem(left_n)
    rs = _stem(right_n)
    if ls and rs and (ls == rs or (min(len(ls), len(rs)) >= 5 and ls[:5] == rs[:5])):
        return True
    if min(len(left_n), len(right_n)) >= 5 and SequenceMatcher(None, left_n, right_n).ratio() >= 0.78:
        return True
    return False


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
    hits = sum(1 for token in ft if any(_token_match(token, other) for other in at))
    overlap = hits / max(1, len(ft))
    return overlap >= (0.67 if len(ft) >= 2 else 1.0)


def _semantic_stats(answer: str, reference: str) -> tuple[float, float, int]:
    """Return answer precision, reference coverage and number of matched answer concepts."""
    ans = list(_tokens(answer))
    ref = list(_tokens(reference))
    if not ans or not ref:
        return 0.0, 0.0, 0
    supported = sum(1 for token in ans if any(_token_match(token, other) for other in ref))
    covered = sum(1 for token in ref if any(_token_match(token, other) for other in ans))
    return supported / len(ans), covered / len(ref), supported


def _narrative_local_score(answer: str, reference: str) -> float:
    """Forgiving score for concise semantic paraphrases of preparation/serving facts.

    We care more about whether what the trainee *did say* is supported by the TTK than
    whether they repeated every word of a long reference paragraph.
    """
    precision, coverage, hits = _semantic_stats(answer, reference)
    if hits <= 0:
        return 0.0
    # A concise answer that captures 2–3 core ideas should land around 7–9 even when the
    # reference is much longer. Coverage adds bonus for fuller answers, not a harsh penalty.
    base = 2.5 + 5.0 * precision + 2.5 * min(1.0, coverage * 3.0)
    if hits == 1:
        base = min(base, 5.5)
    elif hits >= 3 and precision >= 0.55:
        base = max(base, 7.0)
    return round(min(10.0, max(0.0, base)), 1)


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

    if dimension in {"preparation", "serving"}:
        score = _narrative_local_score(answer, reference)
        correct = score >= 6.0
        suffix = " ИИ не успел ответить, поэтому это быстрая смысловая проверка по ТТК." if ai_timeout else ""
        feedback = (
            "Смысл ответа совпадает с ключевыми фактами ТТК."
            if correct
            else "Главная идея пока передана не полностью — добавь ключевую технологию или особенность подачи из ТТК."
        ) + suffix
        return {
            "score": score,
            "correct": correct,
            "feedback": feedback,
            "ideal_answer": model_answer,
        }

    ref_tokens = _tokens(reference)
    ans_tokens = _tokens(answer)
    overlap = sum(1 for token in ans_tokens if any(_token_match(token, other) for other in ref_tokens))
    ratio = overlap / max(1, min(len(ref_tokens), 14))

    if dimension == "sales":
        # Sales answers should still be coached on quality, not only keyword coverage.
        base = 4.0 if len(answer_norm) >= 35 else 2.5
        score = min(10.0, base + min(6.0, ratio * 10.0))
        correct = score >= 6.5
        suffix = " ИИ не успел ответить, поэтому это быстрая проверка по фактам ТТК." if ai_timeout else ""
        feedback = (
            "Ответ опирается на факты из ТТК и достаточно содержательный."
            if correct
            else "В ответе мало опорных фактов из ТТК — добавь идею блюда, вкус/сочетание или особенность подачи, если они есть в карточке."
        ) + suffix
        return {
            "score": round(score, 1),
            "correct": correct,
            "feedback": feedback,
            "ideal_answer": model_answer,
        }

    score = _narrative_local_score(answer, reference)
    correct = score >= 6.0
    return {
        "score": score,
        "correct": correct,
        "feedback": "Смысл ответа совпадает с ТТК." if correct else "Ответ стоит дополнить ключевыми фактами из ТТК.",
        "ideal_answer": model_answer,
    }


def _merge_with_semantic_floor(q: dict[str, Any], answer: str, grade: dict[str, Any]) -> dict[str, Any]:
    """Protect against an AI grader being overly literal on concise factual narratives."""
    dimension = str(q.get("dimension") or "")
    if dimension not in {"preparation", "serving"}:
        return grade
    local = _local_grade(q, answer)
    ai_score = float(grade.get("score") or 0)
    local_score = float(local.get("score") or 0)
    # Local matching is only a safety floor. AI may still award more for a strong paraphrase.
    if local_score >= 6.0 and ai_score < local_score - 1.0:
        merged = dict(grade)
        merged["score"] = local_score
        merged["correct"] = local_score >= 6.0
        if ai_score <= 4.0:
            merged["feedback"] = "Ответ короткий, но по смыслу передаёт ключевые факты из ТТК."
        return merged
    return grade


async def _hybrid_grade(q: dict[str, Any], answer: str) -> dict[str, Any]:
    dimension = str(q.get("dimension") or "")

    # Objective lists are reliable and instant locally.
    if dimension in {"ingredients", "allergens"}:
        return _local_grade(q, answer)

    # Narrative answers need semantic judgement. Try AI first; never block UX forever.
    if _ORIGINAL_GRADE is not None:
        try:
            grade = await asyncio.wait_for(_ORIGINAL_GRADE(q, answer), timeout=6.0)
            return _merge_with_semantic_floor(q, answer, grade)
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
    print("[menu-training] semantic open-answer grading enabled")
