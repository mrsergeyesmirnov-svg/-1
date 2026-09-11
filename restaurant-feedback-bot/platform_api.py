from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

import asyncpg
from aiohttp import web

_pool: asyncpg.Pool | None = None
_schema_ready = False
PBKDF2_ROUNDS = 260_000
SESSION_DAYS = 30

DDL = [
    """
    CREATE TABLE IF NOT EXISTS platform_users (
        id BIGSERIAL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        display_name TEXT NOT NULL DEFAULT '',
        role TEXT NOT NULL DEFAULT 'consultant',
        password_hash TEXT NOT NULL,
        active BOOLEAN NOT NULL DEFAULT TRUE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS platform_sessions (
        token_hash TEXT PRIMARY KEY,
        user_id BIGINT NOT NULL REFERENCES platform_users(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        expires_at TIMESTAMPTZ NOT NULL,
        last_seen_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_sessions (
        id TEXT PRIMARY KEY,
        restaurant_key TEXT NOT NULL,
        restaurant_title TEXT NOT NULL,
        audit_type TEXT NOT NULL,
        template_version TEXT NOT NULL DEFAULT 'AH-AUDIT-1.0',
        status TEXT NOT NULL DEFAULT 'in_progress',
        auditor_user_id BIGINT REFERENCES platform_users(id),
        started_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        completed_at TIMESTAMPTZ,
        overall_score DOUBLE PRECISION,
        red_flags_count INT NOT NULL DEFAULT 0,
        block_scores JSONB NOT NULL DEFAULT '{}'::jsonb,
        is_healthy_baseline BOOLEAN NOT NULL DEFAULT FALSE,
        notes TEXT NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_answers (
        session_id TEXT NOT NULL REFERENCES audit_sessions(id) ON DELETE CASCADE,
        item_id INT NOT NULL,
        item_code TEXT NOT NULL DEFAULT '',
        section TEXT NOT NULL DEFAULT '',
        weight DOUBLE PRECISION NOT NULL DEFAULT 0,
        critical BOOLEAN NOT NULL DEFAULT FALSE,
        standard TEXT NOT NULL DEFAULT '',
        evidence_hint TEXT NOT NULL DEFAULT '',
        score SMALLINT,
        is_na BOOLEAN NOT NULL DEFAULT FALSE,
        comment TEXT NOT NULL DEFAULT '',
        updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
        PRIMARY KEY (session_id, item_id),
        CHECK (score IS NULL OR score BETWEEN 0 AND 2)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS audit_baselines (
        restaurant_key TEXT PRIMARY KEY,
        source_session_id TEXT NOT NULL REFERENCES audit_sessions(id),
        overall_score DOUBLE PRECISION NOT NULL,
        red_flags_count INT NOT NULL DEFAULT 0,
        block_scores JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_by BIGINT REFERENCES platform_users(id),
        created_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_audit_sessions_restaurant_time ON audit_sessions (restaurant_key, started_at DESC)",
    "CREATE INDEX IF NOT EXISTS idx_audit_answers_session ON audit_answers (session_id, item_id)",
    "CREATE INDEX IF NOT EXISTS idx_platform_sessions_expiry ON platform_sessions (expires_at)",
]


def _dsn() -> str:
    value = os.getenv("DATABASE_URL", "").strip()
    if value.startswith("postgres://"):
        value = "postgresql://" + value[len("postgres://") :]
    return value


def _password_hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return f"pbkdf2_sha256${PBKDF2_ROUNDS}${salt.hex()}${digest.hex()}"


def _password_ok(password: str, stored: str) -> bool:
    try:
        algo, rounds_s, salt_hex, digest_hex = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        rounds = int(rounds_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(digest_hex)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, rounds)
        return hmac.compare_digest(actual, expected)
    except Exception:
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _clean_username(value: str) -> str:
    return re.sub(r"[^a-z0-9_.-]", "", (value or "").strip().lower())[:64]


async def _bootstrap_users(conn: asyncpg.Connection) -> None:
    raw = os.getenv("PLATFORM_BOOTSTRAP_USERS_JSON", "").strip()
    if not raw:
        return
    try:
        rows = json.loads(raw)
    except json.JSONDecodeError:
        print("[platform-auth] PLATFORM_BOOTSTRAP_USERS_JSON: invalid JSON")
        return
    if not isinstance(rows, list):
        print("[platform-auth] PLATFORM_BOOTSTRAP_USERS_JSON must be a JSON array")
        return
    for item in rows[:20]:
        if not isinstance(item, dict):
            continue
        username = _clean_username(str(item.get("username") or ""))
        password = str(item.get("password") or "")
        display_name = str(item.get("display_name") or username).strip()[:120]
        role = str(item.get("role") or "consultant").strip()[:32]
        if len(username) < 3 or len(password) < 8:
            print(f"[platform-auth] skip bootstrap user {username!r}: username>=3, password>=8")
            continue
        await conn.execute(
            """
            INSERT INTO platform_users (username, display_name, role, password_hash, active, updated_at)
            VALUES ($1, $2, $3, $4, TRUE, now())
            ON CONFLICT (username) DO UPDATE SET
                display_name = EXCLUDED.display_name,
                role = EXCLUDED.role,
                password_hash = EXCLUDED.password_hash,
                active = TRUE,
                updated_at = now()
            """,
            username,
            display_name,
            role,
            _password_hash(password),
        )


async def ensure_schema() -> asyncpg.Pool:
    global _pool, _schema_ready
    if _pool is None:
        dsn = _dsn()
        if not dsn:
            raise web.HTTPServiceUnavailable(text="DATABASE_URL is not configured")
        _pool = await asyncpg.create_pool(dsn, min_size=1, max_size=4, command_timeout=60)
    if not _schema_ready:
        async with _pool.acquire() as conn:
            for stmt in DDL:
                await conn.execute(stmt)
            await conn.execute("DELETE FROM platform_sessions WHERE expires_at <= now()")
            await _bootstrap_users(conn)
        _schema_ready = True
        print("[platform-api] Postgres schema ready")
    return _pool


async def close_pool() -> None:
    global _pool, _schema_ready
    if _pool is not None:
        await _pool.close()
    _pool = None
    _schema_ready = False


async def _json(request: web.Request) -> dict[str, Any]:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}


def _public_user(row: Any) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "username": row["username"],
        "display_name": row["display_name"],
        "role": row["role"],
    }


async def _current_user(request: web.Request) -> dict[str, Any] | None:
    auth = (request.headers.get("Authorization") or "").strip()
    if not auth.lower().startswith("bearer "):
        return None
    token = auth[7:].strip()
    if not token:
        return None
    pool = await ensure_schema()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT u.id, u.username, u.display_name, u.role
            FROM platform_sessions s
            JOIN platform_users u ON u.id = s.user_id
            WHERE s.token_hash = $1 AND s.expires_at > now() AND u.active = TRUE
            """,
            _token_hash(token),
        )
        if row:
            await conn.execute(
                "UPDATE platform_sessions SET last_seen_at = now() WHERE token_hash = $1",
                _token_hash(token),
            )
    return _public_user(row) if row else None


async def _require_user(request: web.Request) -> dict[str, Any]:
    user = await _current_user(request)
    if not user:
        raise web.HTTPUnauthorized(text="unauthorized")
    return user


async def _login(request: web.Request) -> web.Response:
    body = await _json(request)
    username = _clean_username(str(body.get("username") or ""))
    password = str(body.get("password") or "")
    if not username or not password:
        return web.json_response({"ok": False, "error": "Введите логин и пароль"}, status=400)
    pool = await ensure_schema()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT id, username, display_name, role, password_hash FROM platform_users WHERE username=$1 AND active=TRUE",
            username,
        )
        if not row or not _password_ok(password, row["password_hash"]):
            return web.json_response({"ok": False, "error": "Неверный логин или пароль"}, status=401)
        token = secrets.token_urlsafe(36)
        expires = datetime.now(timezone.utc) + timedelta(days=SESSION_DAYS)
        await conn.execute(
            "INSERT INTO platform_sessions (token_hash, user_id, expires_at) VALUES ($1, $2, $3)",
            _token_hash(token),
            int(row["id"]),
            expires,
        )
    return web.json_response({"ok": True, "token": token, "expires_at": expires.isoformat(), "user": _public_user(row)})


async def _logout(request: web.Request) -> web.Response:
    auth = (request.headers.get("Authorization") or "").strip()
    if auth.lower().startswith("bearer "):
        pool = await ensure_schema()
        async with pool.acquire() as conn:
            await conn.execute("DELETE FROM platform_sessions WHERE token_hash=$1", _token_hash(auth[7:].strip()))
    return web.json_response({"ok": True})


async def _me(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "user": await _require_user(request)})


def _audit_public(row: Any) -> dict[str, Any]:
    block_scores = row["block_scores"] if isinstance(row["block_scores"], dict) else json.loads(row["block_scores"] or "{}")
    return {
        "id": row["id"],
        "restaurant_key": row["restaurant_key"],
        "restaurant_title": row["restaurant_title"],
        "audit_type": row["audit_type"],
        "template_version": row["template_version"],
        "status": row["status"],
        "started_at": row["started_at"].isoformat() if row["started_at"] else None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
        "overall_score": row["overall_score"],
        "red_flags_count": int(row["red_flags_count"] or 0),
        "block_scores": block_scores or {},
        "is_healthy_baseline": bool(row["is_healthy_baseline"]),
        "notes": row["notes"] or "",
    }


async def _list_audits(request: web.Request) -> web.Response:
    await _require_user(request)
    restaurant_key = (request.query.get("restaurant_key") or "").strip()
    try:
        limit = max(1, min(100, int(request.query.get("limit") or 30)))
    except ValueError:
        limit = 30
    pool = await ensure_schema()
    async with pool.acquire() as conn:
        if restaurant_key:
            rows = await conn.fetch(
                "SELECT * FROM audit_sessions WHERE restaurant_key=$1 ORDER BY started_at DESC LIMIT $2",
                restaurant_key,
                limit,
            )
        else:
            rows = await conn.fetch("SELECT * FROM audit_sessions ORDER BY started_at DESC LIMIT $1", limit)
    return web.json_response({"ok": True, "audits": [_audit_public(r) for r in rows]})


async def _start_audit(request: web.Request) -> web.Response:
    user = await _require_user(request)
    body = await _json(request)
    restaurant_key = str(body.get("restaurant_key") or "").strip()[:160]
    restaurant_title = str(body.get("restaurant_title") or "").strip()[:180]
    audit_type = str(body.get("audit_type") or "day0").strip().lower()
    if audit_type not in {"day0", "day30", "day60", "extra"}:
        audit_type = "extra"
    if not restaurant_key or not restaurant_title:
        return web.json_response({"ok": False, "error": "Укажите ресторан"}, status=400)
    audit_id = "ma_" + secrets.token_hex(8)
    pool = await ensure_schema()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            INSERT INTO audit_sessions (id, restaurant_key, restaurant_title, audit_type, template_version, auditor_user_id)
            VALUES ($1,$2,$3,$4,$5,$6) RETURNING *
            """,
            audit_id,
            restaurant_key,
            restaurant_title,
            audit_type,
            str(body.get("template_version") or "AH-AUDIT-1.0")[:40],
            int(user["id"]),
        )
    return web.json_response({"ok": True, "audit": _audit_public(row)})


async def _get_audit(request: web.Request, audit_id: str) -> web.Response:
    await _require_user(request)
    pool = await ensure_schema()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM audit_sessions WHERE id=$1", audit_id)
        if not row:
            return web.json_response({"ok": False, "error": "Аудит не найден"}, status=404)
        answers = await conn.fetch("SELECT * FROM audit_answers WHERE session_id=$1 ORDER BY item_id", audit_id)
        baseline = await conn.fetchrow("SELECT * FROM audit_baselines WHERE restaurant_key=$1", row["restaurant_key"])
    payload_answers = []
    for a in answers:
        payload_answers.append({
            "item_id": int(a["item_id"]),
            "item_code": a["item_code"],
            "section": a["section"],
            "weight": float(a["weight"] or 0),
            "critical": bool(a["critical"]),
            "standard": a["standard"],
            "evidence_hint": a["evidence_hint"],
            "score": a["score"],
            "is_na": bool(a["is_na"]),
            "comment": a["comment"] or "",
            "updated_at": a["updated_at"].isoformat() if a["updated_at"] else None,
        })
    baseline_payload = None
    if baseline:
        bs = baseline["block_scores"] if isinstance(baseline["block_scores"], dict) else json.loads(baseline["block_scores"] or "{}")
        baseline_payload = {
            "source_session_id": baseline["source_session_id"],
            "overall_score": float(baseline["overall_score"]),
            "red_flags_count": int(baseline["red_flags_count"] or 0),
            "block_scores": bs or {},
            "created_at": baseline["created_at"].isoformat(),
        }
    return web.json_response({"ok": True, "audit": _audit_public(row), "answers": payload_answers, "baseline": baseline_payload})


async def _save_answer(request: web.Request, audit_id: str) -> web.Response:
    await _require_user(request)
    body = await _json(request)
    try:
        item_id = int(body.get("item_id"))
    except (TypeError, ValueError):
        return web.json_response({"ok": False, "error": "bad_item_id"}, status=400)
    is_na = bool(body.get("is_na"))
    score = None if is_na else body.get("score")
    if score is not None:
        try:
            score = int(score)
        except (TypeError, ValueError):
            return web.json_response({"ok": False, "error": "bad_score"}, status=400)
        if score not in (0, 1, 2):
            return web.json_response({"ok": False, "error": "bad_score"}, status=400)
    try:
        weight = float(body.get("weight") or 0)
    except (TypeError, ValueError):
        weight = 0.0
    pool = await ensure_schema()
    async with pool.acquire() as conn:
        exists = await conn.fetchval("SELECT 1 FROM audit_sessions WHERE id=$1 AND status='in_progress'", audit_id)
        if not exists:
            return web.json_response({"ok": False, "error": "Аудит не найден или уже завершён"}, status=409)
        await conn.execute(
            """
            INSERT INTO audit_answers (
                session_id,item_id,item_code,section,weight,critical,standard,evidence_hint,score,is_na,comment,updated_at
            ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,now())
            ON CONFLICT (session_id,item_id) DO UPDATE SET
                item_code=EXCLUDED.item_code, section=EXCLUDED.section, weight=EXCLUDED.weight,
                critical=EXCLUDED.critical, standard=EXCLUDED.standard, evidence_hint=EXCLUDED.evidence_hint,
                score=EXCLUDED.score, is_na=EXCLUDED.is_na, comment=EXCLUDED.comment, updated_at=now()
            """,
            audit_id,
            item_id,
            str(body.get("item_code") or "")[:30],
            str(body.get("section") or "")[:180],
            weight,
            bool(body.get("critical")),
            str(body.get("standard") or "")[:1200],
            str(body.get("evidence_hint") or "")[:1200],
            score,
            is_na,
            str(body.get("comment") or "")[:4000],
        )
    return web.json_response({"ok": True, "saved": True, "item_id": item_id})


async def _calculate(conn: asyncpg.Connection, audit_id: str) -> dict[str, Any]:
    rows = await conn.fetch(
        "SELECT item_id,section,weight,critical,score,is_na FROM audit_answers WHERE session_id=$1 ORDER BY item_id",
        audit_id,
    )
    answered = len(rows)
    active = [r for r in rows if not r["is_na"] and r["score"] is not None]
    denom = sum(float(r["weight"] or 0) for r in active)
    weighted = sum((int(r["score"]) / 2.0) * float(r["weight"] or 0) for r in active)
    overall = round((weighted / denom * 100.0), 1) if denom > 0 else None
    red_flags = sum(1 for r in active if r["critical"] and int(r["score"]) == 0)
    blocks: dict[str, dict[str, float]] = {}
    for r in active:
        section = str(r["section"] or "Без блока")
        b = blocks.setdefault(section, {"weighted": 0.0, "weight": 0.0})
        w = float(r["weight"] or 0)
        b["weight"] += w
        b["weighted"] += (int(r["score"]) / 2.0) * w
    block_scores = {k: round(v["weighted"] / v["weight"] * 100.0, 1) if v["weight"] else None for k, v in blocks.items()}
    return {"overall_score": overall, "red_flags_count": red_flags, "block_scores": block_scores, "answered_count": answered}


async def _complete_audit(request: web.Request, audit_id: str) -> web.Response:
    await _require_user(request)
    pool = await ensure_schema()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM audit_sessions WHERE id=$1", audit_id)
        if not row:
            return web.json_response({"ok": False, "error": "Аудит не найден"}, status=404)
        summary = await _calculate(conn, audit_id)
        if summary["answered_count"] < 150:
            return web.json_response({"ok": False, "error": f"Заполнено {summary['answered_count']} из 150 пунктов"}, status=409)
        updated = await conn.fetchrow(
            """
            UPDATE audit_sessions SET status='completed', completed_at=COALESCE(completed_at, now()),
                overall_score=$2, red_flags_count=$3, block_scores=$4::jsonb
            WHERE id=$1 RETURNING *
            """,
            audit_id,
            summary["overall_score"],
            summary["red_flags_count"],
            json.dumps(summary["block_scores"], ensure_ascii=False),
        )
    return web.json_response({"ok": True, "audit": _audit_public(updated), **summary})


async def _set_baseline(request: web.Request, audit_id: str) -> web.Response:
    user = await _require_user(request)
    pool = await ensure_schema()
    async with pool.acquire() as conn:
        row = await conn.fetchrow("SELECT * FROM audit_sessions WHERE id=$1", audit_id)
        if not row or row["status"] != "completed" or row["overall_score"] is None:
            return web.json_response({"ok": False, "error": "Сначала завершите аудит"}, status=409)
        await conn.execute("UPDATE audit_sessions SET is_healthy_baseline=FALSE WHERE restaurant_key=$1", row["restaurant_key"])
        await conn.execute("UPDATE audit_sessions SET is_healthy_baseline=TRUE WHERE id=$1", audit_id)
        await conn.execute(
            """
            INSERT INTO audit_baselines (restaurant_key, source_session_id, overall_score, red_flags_count, block_scores, created_by, created_at)
            VALUES ($1,$2,$3,$4,$5::jsonb,$6,now())
            ON CONFLICT (restaurant_key) DO UPDATE SET
                source_session_id=EXCLUDED.source_session_id, overall_score=EXCLUDED.overall_score,
                red_flags_count=EXCLUDED.red_flags_count, block_scores=EXCLUDED.block_scores,
                created_by=EXCLUDED.created_by, created_at=now()
            """,
            row["restaurant_key"], audit_id, float(row["overall_score"]), int(row["red_flags_count"] or 0),
            json.dumps(row["block_scores"] if isinstance(row["block_scores"], dict) else json.loads(row["block_scores"] or "{}"), ensure_ascii=False),
            int(user["id"]),
        )
    return web.json_response({"ok": True, "baseline": True, "audit_id": audit_id})


async def _delete_draft(request: web.Request, audit_id: str) -> web.Response:
    await _require_user(request)
    pool = await ensure_schema()
    async with pool.acquire() as conn:
        status = await conn.fetchval("SELECT status FROM audit_sessions WHERE id=$1", audit_id)
        if status != "in_progress":
            return web.json_response({"ok": False, "error": "Можно удалить только черновик"}, status=409)
        await conn.execute("DELETE FROM audit_sessions WHERE id=$1", audit_id)
    return web.json_response({"ok": True})


def make_middleware():
    @web.middleware
    async def platform_middleware(request: web.Request, handler):
        path = request.path
        if not path.startswith("/api/platform/"):
            return await handler(request)
        if request.method == "OPTIONS":
            response = web.Response(status=204)
        else:
            try:
                if path == "/api/platform/health" and request.method == "GET":
                    await ensure_schema()
                    response = web.json_response({"ok": True, "service": "platform", "postgres": True})
                elif path == "/api/platform/login" and request.method == "POST":
                    response = await _login(request)
                elif path == "/api/platform/logout" and request.method == "POST":
                    response = await _logout(request)
                elif path == "/api/platform/me" and request.method == "GET":
                    response = await _me(request)
                elif path == "/api/platform/audits" and request.method == "GET":
                    response = await _list_audits(request)
                elif path == "/api/platform/audits/start" and request.method == "POST":
                    response = await _start_audit(request)
                else:
                    m = re.fullmatch(r"/api/platform/audits/([A-Za-z0-9_-]+)(?:/(answer|complete|baseline))?", path)
                    if not m:
                        response = web.json_response({"ok": False, "error": "not_found"}, status=404)
                    else:
                        audit_id, action = m.group(1), m.group(2)
                        if not action and request.method == "GET":
                            response = await _get_audit(request, audit_id)
                        elif action == "answer" and request.method == "POST":
                            response = await _save_answer(request, audit_id)
                        elif action == "complete" and request.method == "POST":
                            response = await _complete_audit(request, audit_id)
                        elif action == "baseline" and request.method == "POST":
                            response = await _set_baseline(request, audit_id)
                        elif not action and request.method == "DELETE":
                            response = await _delete_draft(request, audit_id)
                        else:
                            response = web.json_response({"ok": False, "error": "method_not_allowed"}, status=405)
            except web.HTTPException as exc:
                response = web.json_response({"ok": False, "error": exc.text or exc.reason}, status=exc.status)
            except Exception as exc:
                print("[platform-api]", type(exc).__name__, repr(exc))
                response = web.json_response({"ok": False, "error": "server_error"}, status=500)
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, DELETE, OPTIONS"
        response.headers["Cache-Control"] = "no-store"
        return response
    return platform_middleware
