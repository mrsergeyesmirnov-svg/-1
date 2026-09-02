"""
ИИ-аудитор операционного здоровья точки.

Сессия: голосовые/аудио/файлы кусками → Whisper → GPT (индекс 0–100) → PDF.
"""
from __future__ import annotations

import json
import os
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import ai_advisor

OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_CHUNKS = 40
MAX_FILE_BYTES = 24 * 1024 * 1024  # Whisper ~25MB
HISTORY_KEEP = 80

BLOCK_KEYS = ("people", "processes", "guest", "finance_ops")
BLOCK_TITLES_RU = {
    "people": "Люди и команда",
    "processes": "Процессы и смена",
    "guest": "Гость и сервис",
    "finance_ops": "Финансы и операционка",
}

AUDIT_SYSTEM = """\
Ты — ИИ-аудитор ресторанной операционки (Pulse Team).
По расшифровке разговора с управляющим/менеджером по счастью оцени операционное здоровье точки.

Верни ТОЛЬКО JSON (без markdown):
{
  "overall_index": 0-100,
  "summary": "2–4 предложения по-русски",
  "blocks": {
    "people": {"score": 0-100, "findings": ["..."], "risks": ["..."], "quick_wins": ["..."]},
    "processes": {"score": 0-100, "findings": ["..."], "risks": ["..."], "quick_wins": ["..."]},
    "guest": {"score": 0-100, "findings": ["..."], "risks": ["..."], "quick_wins": ["..."]},
    "finance_ops": {"score": 0-100, "findings": ["..."], "risks": ["..."], "quick_wins": ["..."]}
  },
  "top_priorities": ["до 5 коротких приоритетов"],
  "quotes": ["до 5 коротких цитат/формулировок из речи, без имён"]
}

Правила:
- overall_index ≈ среднее scores блоков, с поправкой на критические риски.
- Если по блоку мало данных — score ближе к 50, findings: «мало данных в разговоре».
- Не выдумывай факты, которых нет в тексте.
- Язык ответа — русский.
"""


def _data_root() -> Path:
    env = os.getenv("PULSE_DATA_DIR", "").strip()
    if env:
        return Path(env)
    return Path(__file__).resolve().parent


def sessions_path() -> Path:
    return _data_root() / "audit_sessions.json"


def reports_dir() -> Path:
    d = _data_root() / "audit_reports"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _default_store() -> dict[str, Any]:
    return {"active": {}, "history": []}


def load_store() -> dict[str, Any]:
    path = sessions_path()
    if not path.exists():
        return _default_store()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return _default_store()
    if not isinstance(raw, dict):
        return _default_store()
    raw.setdefault("active", {})
    raw.setdefault("history", [])
    if not isinstance(raw["active"], dict):
        raw["active"] = {}
    if not isinstance(raw["history"], list):
        raw["history"] = []
    return raw


def save_store(store: dict[str, Any]) -> None:
    path = sessions_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")


def get_active(user_id: int) -> dict[str, Any] | None:
    store = load_store()
    sess = store.get("active", {}).get(str(user_id))
    return sess if isinstance(sess, dict) else None


def start_session(
    user_id: int,
    *,
    restaurant_id: str,
    restaurant_title: str,
    organization_id: str | None = None,
) -> dict[str, Any]:
    store = load_store()
    oid = organization_id or restaurant_id
    sess = {
        "restaurant_id": str(restaurant_id),
        "restaurant_title": str(restaurant_title),
        "organization_id": str(oid),
        "chunks": [],
        "started_at": datetime.now(timezone.utc).isoformat(),
    }
    store.setdefault("active", {})[str(user_id)] = sess
    save_store(store)
    return sess


def cancel_session(user_id: int) -> bool:
    store = load_store()
    existed = str(user_id) in store.get("active", {})
    store.get("active", {}).pop(str(user_id), None)
    if existed:
        save_store(store)
    return existed


def add_chunk(
    user_id: int,
    *,
    kind: str,
    file_id: str,
    file_unique_id: str = "",
    filename: str = "",
    mime: str = "",
    size: int | None = None,
) -> tuple[dict[str, Any] | None, str | None]:
    store = load_store()
    sess = store.get("active", {}).get(str(user_id))
    if not isinstance(sess, dict):
        return None, "Нет активной сессии аудита. Нажмите «ИИ-аудит»."
    chunks = sess.setdefault("chunks", [])
    if not isinstance(chunks, list):
        chunks = []
        sess["chunks"] = chunks
    if len(chunks) >= MAX_CHUNKS:
        return sess, f"Уже {MAX_CHUNKS} фрагментов — завершите анализ или отмените."
    if size is not None and size > MAX_FILE_BYTES:
        return sess, (
            f"Файл слишком большой ({size // (1024 * 1024)} МБ). "
            "Лимит Whisper ~25 МБ — пришлите кусками поменьше."
        )
    chunks.append(
        {
            "kind": kind,
            "file_id": file_id,
            "file_unique_id": file_unique_id,
            "filename": filename or f"{kind}.bin",
            "mime": mime or "",
            "size": size,
            "added_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    store["active"][str(user_id)] = sess
    save_store(store)
    return sess, None


def _clamp_score(v: Any, default: int = 50) -> int:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, n))


def _normalize_block(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        raw = {}
    findings = [str(x).strip() for x in (raw.get("findings") or []) if str(x).strip()]
    risks = [str(x).strip() for x in (raw.get("risks") or []) if str(x).strip()]
    wins = [str(x).strip() for x in (raw.get("quick_wins") or []) if str(x).strip()]
    return {
        "score": _clamp_score(raw.get("score"), 50),
        "findings": findings[:8] or ["Мало данных в разговоре по этому блоку."],
        "risks": risks[:6],
        "quick_wins": wins[:6],
    }


def normalize_analysis(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    blocks_in = raw.get("blocks") if isinstance(raw.get("blocks"), dict) else {}
    blocks = {k: _normalize_block(blocks_in.get(k)) for k in BLOCK_KEYS}
    scores = [blocks[k]["score"] for k in BLOCK_KEYS]
    avg = int(round(sum(scores) / len(scores))) if scores else 50
    overall = _clamp_score(raw.get("overall_index"), avg)
    priorities = [str(x).strip() for x in (raw.get("top_priorities") or []) if str(x).strip()]
    quotes = [str(x).strip() for x in (raw.get("quotes") or []) if str(x).strip()]
    summary = str(raw.get("summary") or "").strip() or "Индекс рассчитан по расшифровке аудита."
    return {
        "overall_index": overall,
        "summary": summary[:1200],
        "blocks": blocks,
        "top_priorities": priorities[:5],
        "quotes": quotes[:5],
    }


def _extract_json(text: str) -> dict[str, Any] | None:
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{[\s\S]*\}", text)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None


async def analyze_transcript(
    transcript: str, *, restaurant_title: str = ""
) -> dict[str, Any] | None:
    client = ai_advisor._client_or_none()
    if client is None:
        return None
    body = (transcript or "").strip()
    if not body:
        return None
    # ограничиваем промпт
    if len(body) > 60000:
        body = body[:60000] + "\n…[обрезано]"
    user = (
        f"Точка: {restaurant_title or '—'}\n\n"
        f"Расшифровка аудита:\n{body}"
    )
    try:
        resp = await client.chat.completions.create(
            model=OPENAI_MODEL,
            temperature=0.3,
            messages=[
                {"role": "system", "content": AUDIT_SYSTEM},
                {"role": "user", "content": user},
            ],
        )
        raw_text = (resp.choices[0].message.content or "").strip()
        parsed = _extract_json(raw_text)
        if not parsed:
            print(f"[ai-auditor] bad json: {raw_text[:400]}")
            return None
        return normalize_analysis(parsed)
    except Exception as e:
        print(f"[ai-auditor] analyze: {e}")
        return None


async def process_session(
    user_id: int,
    *,
    download_bytes,
) -> tuple[dict[str, Any] | None, str | None]:
    """
    download_bytes(file_id) -> bytes
    Returns (history_record, error_message)
    """
    store = load_store()
    sess = store.get("active", {}).get(str(user_id))
    if not isinstance(sess, dict):
        return None, "Нет активной сессии."
    chunks = sess.get("chunks") or []
    if not chunks:
        return None, "Добавьте хотя бы одно голосовое или файл, затем завершите анализ."

    if ai_advisor._client_or_none() is None:
        return None, "Нужен OPENAI_API_KEY для Whisper и анализа."

    transcripts: list[str] = []
    for i, ch in enumerate(chunks, start=1):
        if not isinstance(ch, dict):
            continue
        if ch.get("kind") == "text":
            note = str(ch.get("text") or "").strip()
            if note:
                transcripts.append(f"[Заметка {i}]\n{note}")
            continue
        fid = ch.get("file_id")
        if not fid:
            continue
        filename = str(ch.get("filename") or f"chunk_{i}.ogg")
        try:
            raw = await download_bytes(str(fid))
        except Exception as e:
            print(f"[ai-auditor] download chunk {i}: {e}")
            return None, f"Не удалось скачать фрагмент {i}. Попробуйте ещё раз."
        if not raw:
            return None, f"Пустой файл в фрагменте {i}."
        if len(raw) > MAX_FILE_BYTES:
            return None, (
                f"Фрагмент {i} слишком большой после скачивания. "
                "Пришлите кусками до 20 МБ."
            )
        text = await ai_advisor.transcribe_voice(raw, filename=filename)
        if not text:
            return None, f"Не удалось расшифровать фрагмент {i} ({filename})."
        transcripts.append(f"[Фрагмент {i}]\n{text.strip()}")

    full = "\n\n".join(transcripts).strip()
    if not full:
        return None, "Расшифровка пустая."

    analysis = await analyze_transcript(
        full, restaurant_title=str(sess.get("restaurant_title") or "")
    )
    if not analysis:
        return None, "Не удалось построить индекс здоровья. Попробуйте позже."

    audit_id = "aud_" + secrets.token_hex(5)
    record = {
        "id": audit_id,
        "user_id": user_id,
        "restaurant_id": str(sess.get("restaurant_id") or ""),
        "restaurant_title": str(sess.get("restaurant_title") or ""),
        "started_at": sess.get("started_at"),
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "chunk_count": len(chunks),
        "transcript_chars": len(full),
        "analysis": analysis,
        "pdf_path": "",
    }

    try:
        import audit_pdf

        pdf_bytes = audit_pdf.build_audit_pdf_bytes(
            analysis,
            location=str(sess.get("restaurant_title") or ""),
            audit_id=audit_id,
            completed_at=str(record["completed_at"]),
        )
        pdf_name = f"{audit_id}.pdf"
        pdf_path = reports_dir() / pdf_name
        pdf_path.write_bytes(pdf_bytes)
        record["pdf_path"] = str(pdf_path)
    except Exception as e:
        print(f"[ai-auditor] pdf: {e}")
        return None, f"Анализ готов, но PDF не собрался: {e}"

    store.get("active", {}).pop(str(user_id), None)
    hist = store.setdefault("history", [])
    if not isinstance(hist, list):
        hist = []
        store["history"] = hist
    hist.insert(0, record)
    del hist[HISTORY_KEEP:]
    save_store(store)
    return record, None


def index_label(score: int) -> str:
    if score >= 80:
        return "сильное"
    if score >= 65:
        return "устойчивое"
    if score >= 50:
        return "среднее"
    if score >= 35:
        return "слабое"
    return "критичное"


def tg_summary_html(record: dict[str, Any]) -> str:
    from html import escape

    a = record.get("analysis") or {}
    idx = int(a.get("overall_index") or 0)
    title = escape(str(record.get("restaurant_title") or "Точка"))
    lines = [
        f"<b>🧠 ИИ-аудит готов</b> · {title}",
        f"Индекс здоровья: <b>{idx}/100</b> ({escape(index_label(idx))})",
        "",
        escape(str(a.get("summary") or "")),
        "",
        "<b>Блоки</b>",
    ]
    blocks = a.get("blocks") or {}
    for key in BLOCK_KEYS:
        b = blocks.get(key) or {}
        sc = int(b.get("score") or 0)
        lines.append(f"· {escape(BLOCK_TITLES_RU.get(key, key))}: <b>{sc}</b>")
    pri = a.get("top_priorities") or []
    if pri:
        lines.append("")
        lines.append("<b>Приоритеты</b>")
        for p in pri[:5]:
            lines.append(f"· {escape(str(p))}")
    lines.append("")
    lines.append(f"<i>ID: {escape(str(record.get('id') or ''))}</i>")
    return "\n".join(lines)
