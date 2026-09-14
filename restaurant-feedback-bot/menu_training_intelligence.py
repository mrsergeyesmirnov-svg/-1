"""Smarter menu parsing, question generation and sales-answer coaching.

This layer keeps the training source-grounded but makes it understand restaurant language:
- normalises glued/OCR-ish ingredient names ("соустомям" -> "соус том ям");
- removes service/production labels that are not ingredients;
- asks AI to compose educational, non-trivial questions from the approved menu context;
- creates richer guest-facing selling descriptions without inventing menu facts;
- improves matching of equivalent spellings for fast local grading.

Privacy invariant: this module only sees approved menu/TTK data and pseudonymous mastery.
It never reads shift feedback or exposes learner identity to managers.
"""
from __future__ import annotations

import json
import random
import re
from datetime import datetime, timezone
from typing import Any

import menu_training
import menu_training_fastgrade

_INSTALLED = False

# Things that commonly leak from technical cards / PDF extraction but are not edible
# components worth memorising as ingredients.
_NON_INGREDIENT_PATTERNS = (
    r"\bнабор\s*(?:д|для)?\s*/?\s*суши\b",
    r"\bнабор\s*(?:д|для)?\s*/?\s*ролл",
    r"\bупаковк",
    r"\bконтейнер",
    r"\bсалфет",
    r"\bпалочк(?:и|а)?\b",
    r"\bперчат",
    r"\bпакет\b",
)

# High-confidence restaurant/OCR repairs. We deliberately keep this short: anything
# uncertain is left to the source-aware AI parser instead of being guessed locally.
_CANONICAL_COMPACT = {
    "соустомям": "соус том ям",
    "соустомyam": "соус том ям",
    "ристомям": "рис том ям",
    "рисдлясуши": "рис для суши",
    "кунжутчерный": "кунжут чёрный",
    "кунжутчерныйжареный": "кунжут чёрный жареный",
    "сырчеддер": "сыр чеддер",
    "сырсливочный": "сыр сливочный",
    "соусюдзу": "соус юдзу",
    "соусвасаби": "соус васаби",
    "микрозелень": "микрозелень",
}

_GENERIC_INGREDIENTS = {
    "соль", "перец", "вода", "масло", "сахар",
}


def _norm(text: str) -> str:
    text = (text or "").lower().replace("ё", "е")
    text = re.sub(r"[^a-zа-я0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _compact(text: str) -> str:
    return re.sub(r"\s+", "", _norm(text))


def _is_non_ingredient(text: str) -> bool:
    n = _norm(text)
    if not n:
        return True
    return any(re.search(pattern, n, flags=re.I) for pattern in _NON_INGREDIENT_PATTERNS)


def canonical_ingredient(text: str) -> str:
    raw = re.sub(r"\s+", " ", (text or "").strip(" ,;:.-"))
    if not raw or _is_non_ingredient(raw):
        return ""
    compact = _compact(raw)
    if compact in _CANONICAL_COMPACT:
        return _CANONICAL_COMPACT[compact]
    # Normalise common compact prefixes without pretending we can solve arbitrary OCR.
    raw = re.sub(r"(?i)^соус(?=[а-яa-z])", "соус ", raw)
    raw = re.sub(r"(?i)^сыр(?=(?:чеддер|сливоч))", "сыр ", raw)
    raw = re.sub(r"(?i)^рисдлясуши$", "рис для суши", raw)
    raw = re.sub(r"(?i)^кунжутчерный", "кунжут чёрный", raw)
    return re.sub(r"\s+", " ", raw).strip()


def _dedupe(values: list[str], *, ingredients: bool = False) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for value in values:
        cleaned = canonical_ingredient(value) if ingredients else re.sub(r"\s+", " ", str(value).strip())
        if not cleaned:
            continue
        key = _compact(cleaned)
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(cleaned)
    return out


def normalize_dish(dish: dict[str, Any]) -> dict[str, Any]:
    row = dict(dish)
    name = re.sub(r"\s+", " ", str(row.get("name") or "").strip())
    row["name"] = name
    row["category"] = re.sub(r"\s+", " ", str(row.get("category") or "").strip())

    ingredients = _dedupe(
        [str(x) for x in (row.get("ingredients") or [])],
        ingredients=True,
    )
    # A parsing artefact sometimes repeats the dish itself as an ingredient.
    dish_key = _compact(name)
    ingredients = [x for x in ingredients if _compact(x) != dish_key]
    row["ingredients"] = ingredients
    row["allergens"] = _dedupe([str(x) for x in (row.get("allergens") or [])])
    row["important_facts"] = _dedupe([str(x) for x in (row.get("important_facts") or [])])
    for field in ("preparation", "serving", "weight", "sales_description"):
        row[field] = re.sub(r"\s+", " ", str(row.get(field) or "").strip())
    row["key"] = str(row.get("key") or menu_training._dish_key(name))
    return row


async def _smart_analyse_text(file_name: str, source: str) -> dict[str, Any]:
    prompt = f"""
Ты разбираешь ТТК/технологические карты ресторана для обучения официантов и кухни.
Нужно понять смысл документа, а не механически копировать строки PDF.

ВАЖНО ПРО КАЧЕСТВО РАСПОЗНАВАНИЯ:
1. Исправляй очевидно склеенные слова и OCR/PDF-разрывы, только когда смысл однозначен.
   Примеры: «Соустомям» = «соус том ям», «Рисдлясуши» = «рис для суши»,
   «Кунжутчерный» = «кунжут чёрный», «Сырчеддер» = «сыр чеддер».
2. Одинаковые сущности с разным написанием объединяй в одну каноническую форму.
   «соус томям», «соустомям», «соус том ям» — это один компонент «соус том ям».
3. ingredients — только реальные съедобные компоненты блюда, которые сотруднику имеет смысл знать.
   Не записывай туда служебные строки, упаковку, инвентарь, названия заготовок верхнего уровня,
   группы/наборы и технические маркеры. Например «Набор д/суши» НЕ является ингредиентом.
4. Не превращай название блюда, категорию, единицу измерения или техкарту в ингредиент.
5. Не додумывай отсутствующие компоненты.

АЛЛЕРГЕНЫ:
- allergens заполняй ТОЛЬКО если аллерген прямо указан в документе;
- не выводи аллерген из состава самостоятельно;
- если явной информации нет: allergens=[] и allergens_confirmed=false.

ПРОДАЮЩЕЕ ОПИСАНИЕ:
Для каждого блюда sales_description — 2–3 естественных предложения, которыми сильный официант
может описать блюдо гостю. Оно должно быть живым, аппетитным и погружать в блюдо: показать
главную идею, ключевые компоненты, сочетание/контраст и особенности подачи. Можно красиво
переформулировать факты и очевидные гастрономические свойства компонентов, но НЕЛЬЗЯ придумывать
ингредиенты, способ приготовления, остроту, происхождение, граммовку или другие факты,
которых нет в источнике. Избегай пустых слов вроде «невероятно вкусный».

preparation, serving, weight, important_facts — только по источнику.
Если документ не про меню/ТТК/карточки блюд: is_menu_source=false, dishes=[].

Имя файла: {file_name}
<source>
{source}
</source>
"""
    response = await menu_training._ai().responses.create(
        model=menu_training.MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "Ты шеф-технолог и тренер ресторанной команды. Понимай ресторанный контекст, "
                    "исправляй только очевидные артефакты извлечения текста и оставайся строго в фактах ТТК."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "menu_analysis",
                "schema": menu_training.MENU_ANALYSIS_SCHEMA,
                "strict": True,
            }
        },
    )
    result = json.loads(response.output_text)
    result["dishes"] = [normalize_dish(d) for d in (result.get("dishes") or []) if d.get("name")]
    return result


QUESTION_SET_SCHEMA = {
    "type": "object",
    "properties": {
        "questions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "dish_key": {"type": "string"},
                    "dish_name": {"type": "string"},
                    "dimension": {
                        "type": "string",
                        "enum": ["ingredients", "allergens", "sales", "preparation", "serving", "recognition"],
                    },
                    "type": {"type": "string", "enum": ["mcq", "open"]},
                    "question": {"type": "string"},
                    "options": {"type": "array", "items": {"type": "string"}},
                    "correct_index": {"type": "integer"},
                    "model_answer": {"type": "string"},
                    "facts": {"type": "array", "items": {"type": "string"}},
                },
                "required": [
                    "dish_key", "dish_name", "dimension", "type", "question", "options",
                    "correct_index", "model_answer", "facts",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["questions"],
    "additionalProperties": False,
}


def _dimension_options(dish: dict[str, Any], department: str) -> list[str]:
    dims: list[str] = []
    if dish.get("ingredients"):
        dims.append("ingredients")
    if dish.get("allergens_confirmed"):
        dims.append("allergens")
    if department == "kitchen":
        if dish.get("preparation"):
            dims.append("preparation")
        if dish.get("serving"):
            dims.append("serving")
    else:
        dims.append("sales")
    return dims or ["recognition"]


async def _rank_focus_dishes(
    learner: str,
    chat_id: int,
    dishes: list[dict[str, Any]],
    department: str,
    target: int,
) -> list[dict[str, Any]]:
    mastery = await menu_training._mastery_map(learner, chat_id)
    now = datetime.now(timezone.utc)
    ranked: list[tuple[float, dict[str, Any]]] = []
    for dish in dishes:
        dims = _dimension_options(dish, department)
        scores: list[float] = []
        for dim in dims:
            row = mastery.get((str(dish.get("key")), dim), {})
            m = float(row.get("mastery") or 0)
            due = row.get("next_due_at")
            due_bonus = 0.25 if due is None or (getattr(due, "tzinfo", None) and due <= now) else 0.0
            scores.append(1.0 - m + due_bonus)
        priority = (sum(scores) / max(1, len(scores))) + random.random() * 0.12
        ranked.append((priority, dish))
    ranked.sort(key=lambda item: item[0], reverse=True)
    want = min(len(ranked), max(4, min(target + 2, 14)))
    return [d for _, d in ranked[:want]]


def _question_payload(dish: dict[str, Any]) -> dict[str, Any]:
    d = normalize_dish(dish)
    return {
        "key": d.get("key"),
        "name": d.get("name"),
        "category": d.get("category"),
        "ingredients": d.get("ingredients") or [],
        "allergens": d.get("allergens") or [],
        "allergens_confirmed": bool(d.get("allergens_confirmed")),
        "preparation": d.get("preparation") or "",
        "serving": d.get("serving") or "",
        "sales_description": d.get("sales_description") or "",
        "important_facts": d.get("important_facts") or [],
    }


async def _ai_questions(
    dishes: list[dict[str, Any]],
    department: str,
    mode: str,
    target: int,
) -> list[dict[str, Any]]:
    role = "кухни" if department == "kitchen" else "зала"
    if mode == "one":
        mix = "Сделай один действительно полезный вопрос; выбирай между MCQ и открытым по смыслу."
    elif mode == "full":
        mix = "Около 65–70% вопросов MCQ и 30–35% открытых. Покрой разные блюда и навыки."
    else:
        mix = "Смешай примерно 4 MCQ и 3 открытых вопроса; не ставь подряд однотипные задания."

    payload = [_question_payload(d) for d in dishes]
    prompt = f"""
Создай ровно {target} вопросов для обучения сотрудника {role} по утверждённой базе меню ниже.
{mix}

Главное требование: вопросы должны быть УМНЫМИ и похожими на реальную аттестацию ресторана,
а не на механический тест по словам.

ПРАВИЛА:
- Понимай смысл ингредиентов и ресторанный контекст. «Набор д/суши», упаковка, инвентарь,
  технические названия строк — не ингредиенты и не должны становиться ответами.
- Считай варианты написания одной сущностью: «соустомям»/«соус томям»/«соус том ям» — одно и то же.
- Не задавай абсурдно очевидные вопросы. Не спрашивай, например, идёт ли рис в бургер,
  если это проверяет только здравый смысл, а не знание конкретного меню.
- Для MCQ все 4 варианта должны быть правдоподобны и одного смыслового класса:
  соус против соусов, сыр против сыров, рыба против рыбы и т.п. Лучше сравнивать близкие блюда
  той же категории. Один правильный ответ, три реально возможных, но неверных по этой ТТК.
- Не используй случайные ингредиенты из совершенно другого раздела как дешёвые отвлекающие варианты.
- Открытые вопросы должны проверять понимание: состав, отличия, аллергены (только подтверждённые),
  технологию/подачу для кухни, умение продать блюдо для зала.
- Для sales вопроса model_answer должен быть сильным гостевым описанием на 2–3 предложения:
  образным, аппетитным, конкретным, с главной идеей блюда и сочетанием компонентов. Без выдуманных фактов.
- facts должны содержать канонические факты, которыми можно проверять ответ. Не дублируй одно и то же
  из-за пробелов/склейки слов.
- Для open: options=[], correct_index=-1.
- Для mcq: ровно 4 options и correct_index 0..3.
- Не повторяй формулировки и не задавай два вопроса об одном и том же факте в одной тренировке.

УТВЕРЖДЁННЫЕ КАРТОЧКИ:
{json.dumps(payload, ensure_ascii=False)}
"""
    response = await menu_training._ai().responses.create(
        model=menu_training.MODEL,
        input=[
            {
                "role": "system",
                "content": (
                    "Ты сильный тренер ресторанной команды и экзаменатор. Проверяй знание конкретного меню, "
                    "а не общую эрудицию. Вопрос должен быть содержательным, правдоподобным и source-grounded."
                ),
            },
            {"role": "user", "content": prompt},
        ],
        text={
            "format": {
                "type": "json_schema",
                "name": "menu_question_set",
                "schema": QUESTION_SET_SCHEMA,
                "strict": True,
            }
        },
    )
    raw = json.loads(response.output_text).get("questions") or []
    allowed_keys = {str(d.get("key")) for d in dishes}
    out: list[dict[str, Any]] = []
    seen_questions: set[str] = set()
    for q in raw:
        if str(q.get("dish_key")) not in allowed_keys:
            continue
        question_key = _norm(str(q.get("question") or ""))
        if not question_key or question_key in seen_questions:
            continue
        if q.get("type") == "mcq":
            options = [str(x).strip() for x in (q.get("options") or [])]
            if len(options) != 4 or len({_compact(x) for x in options}) != 4:
                continue
            if int(q.get("correct_index", -1)) not in range(4):
                continue
        else:
            q["options"] = []
            q["correct_index"] = -1
        q["facts"] = _dedupe([str(x) for x in (q.get("facts") or [])], ingredients=q.get("dimension") == "ingredients")
        seen_questions.add(question_key)
        out.append(q)
        if len(out) >= target:
            break
    return out


def _classify_ingredient(value: str) -> str:
    n = _norm(value)
    groups = {
        "fish": ("лосос", "тунец", "угор", "кревет", "кальмар", "краб", "гребеш", "рыб"),
        "cheese": ("сыр", "чеддер", "моцарел", "пармез", "сливочн"),
        "sauce": ("соус", "майонез", "юдзу", "васаби", "терияки", "спайси", "том ям"),
        "veg": ("авокад", "огур", "томат", "лук", "перец", "зелень", "салат"),
        "carb": ("рис", "лапш", "булоч", "хлеб", "картоф", "тортиль"),
        "meat": ("говя", "куриц", "индей", "бекон", "свинин", "мяс"),
    }
    for group, needles in groups.items():
        if any(x in n for x in needles):
            return group
    return "other"


def _local_sales_answer(d: dict[str, Any]) -> str:
    desc = str(d.get("sales_description") or "").strip()
    ingredients = [x for x in (d.get("ingredients") or []) if _norm(x) not in _GENERIC_INGREDIENTS]
    facts = [str(x) for x in (d.get("important_facts") or []) if str(x).strip()]
    if len(desc) >= 100:
        return desc
    lead = ", ".join(ingredients[:4])
    second = facts[0] if facts else str(d.get("serving") or "").strip()
    parts = []
    if lead:
        parts.append(f"В основе блюда — {lead}.")
    if desc:
        parts.append(desc.rstrip(".") + ".")
    if second and _norm(second) not in _norm(" ".join(parts)):
        parts.append(second.rstrip(".") + ".")
    return " ".join(parts) or str(d.get("name") or "")


def _local_candidates(dishes: list[dict[str, Any]], department: str) -> list[dict[str, Any]]:
    """Reasonable fallback if AI question generation is temporarily unavailable."""
    dishes = [normalize_dish(d) for d in dishes]
    out: list[dict[str, Any]] = []
    for d in dishes:
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        key = str(d.get("key"))
        ingredients = [x for x in (d.get("ingredients") or []) if _norm(x) not in _GENERIC_INGREDIENTS]
        if len(ingredients) >= 2:
            out.append({
                "dish_key": key, "dish_name": name, "dimension": "ingredients", "type": "open",
                "question": f"Назови ключевые ингредиенты блюда «{name}».",
                "options": [], "correct_index": -1,
                "model_answer": ", ".join(ingredients), "facts": ingredients,
            })
            # Build a hard MCQ only when we can find same-class distractors from nearby menu cards.
            correct_pool = [x for x in ingredients if _classify_ingredient(x) != "other"] or ingredients
            correct = random.choice(correct_pool)
            group = _classify_ingredient(correct)
            distractors: list[str] = []
            for other in dishes:
                if other.get("key") == key:
                    continue
                if d.get("category") and other.get("category") != d.get("category"):
                    continue
                for item in other.get("ingredients") or []:
                    item = canonical_ingredient(str(item))
                    if item and item not in ingredients and _classify_ingredient(item) == group:
                        distractors.append(item)
            distractors = _dedupe(distractors, ingredients=True)
            if len(distractors) >= 3:
                options = random.sample(distractors, 3) + [correct]
                random.shuffle(options)
                out.append({
                    "dish_key": key, "dish_name": name, "dimension": "ingredients", "type": "mcq",
                    "question": f"Какой из этих компонентов действительно входит в «{name}»?",
                    "options": options, "correct_index": options.index(correct),
                    "model_answer": correct, "facts": [correct],
                })
        if d.get("allergens_confirmed"):
            allergens = list(d.get("allergens") or [])
            out.append({
                "dish_key": key, "dish_name": name, "dimension": "allergens", "type": "open",
                "question": f"Какие аллергены прямо указаны в ТТК для «{name}»?",
                "options": [], "correct_index": -1,
                "model_answer": ", ".join(allergens) if allergens else "Явно указано отсутствие аллергенов",
                "facts": allergens,
            })
        if department == "kitchen":
            if d.get("preparation"):
                out.append({
                    "dish_key": key, "dish_name": name, "dimension": "preparation", "type": "open",
                    "question": f"Объясни ключевую технологию приготовления «{name}» так, как объяснил бы новому повару.",
                    "options": [], "correct_index": -1,
                    "model_answer": str(d.get("preparation")), "facts": [str(d.get("preparation"))],
                })
        else:
            out.append({
                "dish_key": key, "dish_name": name, "dimension": "sales", "type": "open",
                "question": f"Гость заинтересовался «{name}». Опиши блюдо так, чтобы захотелось заказать: 2–3 живых предложения, только по фактам меню.",
                "options": [], "correct_index": -1,
                "model_answer": _local_sales_answer(d),
                "facts": ingredients + list(d.get("important_facts") or []),
            })
    random.shuffle(out)
    return out


async def _smart_select_questions(
    learner: str,
    chat_id: int,
    dishes: list[dict[str, Any]],
    department: str,
    mode: str,
) -> list[dict[str, Any]]:
    cleaned = [normalize_dish(d) for d in dishes if isinstance(d, dict) and d.get("name")]
    if not cleaned:
        return []
    target = 1 if mode == "one" else (min(30, max(12, len(cleaned) * 2)) if mode == "full" else min(7, max(1, len(cleaned) * 2)))
    focus = await _rank_focus_dishes(learner, chat_id, cleaned, department, target)

    # Give the model some neighbouring cards as distractor/context material without dumping a huge menu.
    context = list(focus)
    focus_categories = {str(d.get("category") or "") for d in focus}
    extras = [d for d in cleaned if d not in focus and (not focus_categories or str(d.get("category") or "") in focus_categories)]
    random.shuffle(extras)
    context.extend(extras[: max(0, 18 - len(context))])

    try:
        generated = await _ai_questions(context, department, mode, target)
        if len(generated) >= max(1, min(target, 3)):
            if len(generated) < target:
                fallback = _local_candidates(focus, department)
                used = {_norm(str(q.get("question") or "")) for q in generated}
                generated.extend(q for q in fallback if _norm(str(q.get("question") or "")) not in used)
            return generated[:target]
    except Exception as exc:
        print(f"[menu-training intelligence] AI question generation fallback: {exc!r}")

    fallback = _local_candidates(focus, department)
    return fallback[:target]


async def _smart_grade_open(q: dict[str, Any], answer: str) -> dict[str, Any]:
    dimension = str(q.get("dimension") or "")
    payload = {
        "question": q.get("question"),
        "dimension": dimension,
        "reference_answer": q.get("model_answer"),
        "source_facts": q.get("facts") or [],
        "learner_answer": answer,
    }
    if dimension == "sales":
        system = (
            "Ты тренер сильного официанта. Оцени продающее описание по шкале 0–10: "
            "фактическая точность по ТТК — до 4 баллов; живой гастрономический образ и конкретика — до 2; "
            "умение объяснить гостю, в чём идея/сочетание блюда — до 2; естественная уверенная речь — до 2. "
            "Любой выдуманный ингредиент, технология или свойство снижает оценку. "
            "9–10 — это текст, который реально хочется услышать от сильного официанта, а не перечень состава. "
            "ideal_answer дай как мощное, но естественное описание в 2–3 предложениях, строго из source_facts/reference."
        )
    else:
        system = (
            "Ты проверяешь знание меню ресторана. Оцени только по reference/source_facts, учитывая нормальные "
            "варианты написания и словоформы. Не требуй дословного совпадения и не добавляй факты от себя."
        )
    response = await menu_training._ai().responses.create(
        model=menu_training.MODEL,
        input=[
            {"role": "system", "content": system},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        text={"format": {"type": "json_schema", "name": "open_grade", "schema": menu_training.OPEN_GRADE_SCHEMA, "strict": True}},
    )
    return json.loads(response.output_text)


def _smart_fact_hit(answer_norm: str, fact: str) -> bool:
    """Equivalent spellings/spacing should not be counted as separate knowledge errors."""
    fact_clean = canonical_ingredient(fact)
    if not fact_clean:
        # Noise such as "Набор д/суши" should not count as a required fact at all.
        return True
    f_norm = _norm(fact_clean)
    a_norm = _norm(answer_norm)
    if not f_norm:
        return True
    if f_norm in a_norm:
        return True
    # Handles "соустомям" vs "соус том ям", "сырчеддер" vs "сыр чеддер".
    f_compact = _compact(f_norm)
    a_compact = _compact(a_norm)
    if len(f_compact) >= 5 and f_compact in a_compact:
        return True

    ft = [t for t in f_norm.split() if len(t) >= 3]
    at = [t for t in a_norm.split() if len(t) >= 3]
    if not ft:
        return False

    def token_match(left: str, right: str) -> bool:
        if left == right:
            return True
        # light Russian morphology tolerance: лосось/лосося, креветка/креветки, etc.
        return min(len(left), len(right)) >= 5 and left[:5] == right[:5]

    hits = sum(1 for token in ft if any(token_match(token, other) for other in at))
    return hits / max(1, len(ft)) >= (0.67 if len(ft) >= 2 else 1.0)


def install() -> None:
    global _INSTALLED
    if _INSTALLED:
        return
    menu_training._analyse_text = _smart_analyse_text
    menu_training._select_questions = _smart_select_questions
    menu_training._question_candidates = _local_candidates
    menu_training._grade_open = _smart_grade_open
    # fastgrade calls this global dynamically, so replacing it improves old and newly analysed menus.
    menu_training_fastgrade._fact_hit = _smart_fact_hit
    _INSTALLED = True
    print("[menu-training] semantic menu intelligence enabled")
