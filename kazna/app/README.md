# Казна · рабочий контур

FastAPI + SQLite/Postgres + iiko + Excel + UI.

См. также **[SECURITY.md](./SECURITY.md)** — чеклист для боевых финансов.

## Локально

```bash
cd kazna/app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn server.main:app --reload --port 8765
```

Открой http://127.0.0.1:8765/

В демо-режиме (по умолчанию вне production):
- финдир: `fin@kazna.local` / `kazna2026`
- управляющий: `manager@kazna.local` / `kazna2026`

## Railway (прод)

Root directory: `kazna/app`

**Обязательные переменные:**
- `KAZNA_SECRET` — ≥32 символов
- `KAZNA_HTTPS=true`
- `DATABASE_URL` — Postgres
- `KAZNA_ENV=production` (рекомендуется)

**Опционально:**
- `IIKO_API_LOGIN` — лучше env, чем поле в UI
- `KAZNA_HOSTS=your.domain`
- `KAZNA_SESSION_HOURS=12`
- `KAZNA_ALLOW_DEMO=1` — **только** для песочницы, не для боевых денег

## Данные

- Платежи: iiko sync + ручные + заявки
- Остатки р/с и кассы: Excel / ручной ввод на «Деньги»
