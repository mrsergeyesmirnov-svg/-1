# Казна · рабочий контур

FastAPI + SQLite + Excel-импорт + UI.

## Локально

```bash
cd kazna/app
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn server.main:app --reload --port 8765
```

Открой http://127.0.0.1:8765/

- финдир: `fin@kazna.local` / `kazna2026`
- управляющий: `manager@kazna.local` / `kazna2026`

На вкладке **Импорт** загрузи свой `.xlsx` с платежами.

## Railway

Root directory сервиса: `kazna/app`

```bash
railway login
cd kazna/app
railway init
railway up
railway domain
```

Переменные:
- `KAZNA_SECRET` — длинная строка
- `KAZNA_HTTPS=true` — после выдачи https-домена

## Excel

Платежки: колонки `Дата`, `Сумма`, желательно `Контрагент`, `Назначение`, `Счёт`, `Статус`.  
Остатки: `Счёт/Организация` + `Остаток`.
