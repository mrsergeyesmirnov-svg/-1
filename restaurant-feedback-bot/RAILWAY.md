# Деплой на Railway (Pulse / смены)

GitHub **не передаёт** секреты из `.env` — их нужно вручную добавить в Railway → сервис → **Variables**.

## Pulse (смены)

1. **Start Command**: `python bot.py`
2. **Variables**:
   - `BOT_TOKEN`
   - `ADMIN_IDS`
   - опционально `DATABASE_URL`
   - опционально `PULSE_DATA_DIR=/data` (если используете Volume)

## Volume (чтобы настройки не сбрасывались)

- Add Volume → mount `/data`
- В сервисе Pulse задайте `PULSE_DATA_DIR=/data`

## PostgreSQL

`DATABASE_URL` подключайте **Reference** из сервиса Postgres к сервису Pulse (`bot.py`).

## Если «не отвечает»

1. Railway → **Deployments** → последний деплой → **View logs**
   - `Crash` / `SystemExit` — нет токена или `ADMIN_IDS`
   - `Conflict` / `terminated by other getUpdates` — один и тот же токен запущен дважды
2. В Telegram откройте **того** бота, чей токен в этом сервисе.

## Безопасность

Не коммитьте `.env` в GitHub. Если уже закоммитили — смените токены в @BotFather и пароль БД.
