# Безопасность Казны

Финансовые данные. Цель — минимизировать утечки, несанкционированный доступ и потерю целостности.

## Модель угроз (кратко)

| Угроза | Защита сейчас |
|--------|----------------|
| Утечка сессии | HttpOnly cookie, SameSite=lax, Secure в prod, короткий TTL |
| Брутфорс входа | rate limit по IP+email |
| Доступ менеджера к платежам сети | `/api/payments` только office (фин/бухгалтер) |
| XSS в заявках | escape в UI |
| Утечка iiko apiLogin из БД | Fernet-шифрование через `KAZNA_SECRET`; лучше `IIKO_API_LOGIN` в env |
| Demo-пароли в проде | seed/UI только при `KAZNA_ALLOW_DEMO` или вне production |
| Сканирование API | `/docs` выключен в production |

## Обязательный чеклист Railway (продакшен)

1. **Postgres** — `DATABASE_URL` (не SQLite на эфемерном диске).
2. **`KAZNA_SECRET`** ≥ 32 символов (не менять без планового logout всех).
3. **`KAZNA_HTTPS=true`** (или auto в production).
4. **`KAZNA_ENV=production`** (или деплой на Railway — детектится сам).
5. **Не ставить** `KAZNA_ALLOW_DEMO=1` на боевом.
6. Создать первого финдира вручную (SQL/скрипт) или один раз локально с демо → сменить пароли → выключить демо.
7. **`IIKO_API_LOGIN`** в Railway Variables (предпочтительнее, чем хранение в БД).
8. Volume для `data/uploads` только если нужны файлы; иначе чистить uploads.
9. Ограничить кто в Railway project; 2FA на GitHub/Railway.
10. Опционально `KAZNA_HOSTS=kazna.up.railway.app` — Trusted Host.

### Создать секрет

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

## Следующий уровень (ещё не в коде)

- SSO / MFA для финдира
- Аудит-лог в БД (кто менял остатки, статусы, людей)
- WAF / Cloudflare перед Railway
- Отдельный staging с анонимизированными данными
- Шифрование диска БД (managed Postgres)
- Backup + restore drill
- Юридически: NDA, доступ по ролям, политика паролей компании

## Локальная разработка

По умолчанию demo-аккаунты разрешены. Для проверки «как в проде»:

```bash
export KAZNA_ENV=production
export KAZNA_SECRET="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export KAZNA_HTTPS=false   # локально без TLS
# без KAZNA_ALLOW_DEMO — демо-юзеры не создаются
```
