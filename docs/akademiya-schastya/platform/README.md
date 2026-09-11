# Платформа для консультантов

Внутренний контур Академии счастья: CRM, Master Audit, калькулятор проекта, финансы, план туров.

Публичный вход — тихая ссылка в футере сайта: **«Платформа для консультантов»** → `/platform/`.

## Авторизация

Основной вход в хаб и Master Audit — по индивидуальному логину и паролю через Railway backend.

Пользователи при первом запуске/деплое создаются или обновляются из Railway-переменной:

`PLATFORM_BOOTSTRAP_USERS_JSON`

Пример:

```json
[
  {"username":"sergey","password":"CHANGE_ME_1","display_name":"Сергей","role":"admin"},
  {"username":"partner","password":"CHANGE_ME_2","display_name":"Партнёр","role":"partner"}
]
```

Пароль хэшируется PBKDF2-SHA256 и хранится в PostgreSQL только в виде хэша. После успешного первого деплоя bootstrap-переменную можно удалить, если пароли больше не нужно автоматически пересоздавать при старте.

Сессия входа хранится как случайный bearer token, в PostgreSQL сохраняется только SHA-256 token hash. Срок сессии — 30 дней.

Старые страницы CRM/финансов пока используют совместимый `sessionStorage`-флаг после входа через новый хаб. Их старый локальный gate нужно убрать отдельным проходом после проверки нового входа.

## Состав

| Путь | Статус |
|---|---|
| `platform/index.html` | Хаб с логином/паролем |
| `platform/crm.html` | CRM-стикеры клиентов |
| `platform/restaurant-audit.html` | Master Audit 150 пунктов: mobile-first, 0/1/2/N/A, Day 0/30/60, Red Flags, Excel |
| `platform/data/master-audit-01.csv` | Шаблон Master Audit, пункты 1–50 |
| `platform/data/master-audit-02.csv` | Шаблон Master Audit, пункты 51–100 |
| `platform/data/master-audit-03.csv` | Шаблон Master Audit, пункты 101–150 |
| `platform/audit.html` | Калькулятор проекта → счета/акты в CRM |
| `platform/finance.html` | Учёт: доходы/расходы, сметы, календарь, PDF, прибыль (факт), баланс |
| `platform/tours-plan.html` | Заготовка планирования туров |

## Master Audit

`restaurant-audit.html` использует CRM-карточку ресторана как привязку, но сами сессии и ответы хранятся в Railway PostgreSQL.

Основные таблицы:

- `platform_users`
- `platform_sessions`
- `audit_sessions`
- `audit_answers`
- `audit_baselines`

Каждый ответ 0/1/2/N/A и комментарий сохраняется сразу через `/api/platform/*`. В браузере остаётся только аварийная очередь на случай потери связи; после восстановления она досылается на сервер.

После аудита считаются общий weighted score и баллы по блокам, критические нули превращаются в Red Flags. Итог можно записать комментарием в карточку CRM и выгрузить в `.xlsx`. Для Day 60 можно зафиксировать Healthy Baseline для дальнейшей логики Pulse.

## Railway backend

`restaurant-feedback-bot/launcher.py` запускает существующий Telegram Mini App и новый platform API на одном Railway `PORT`.

`restaurant-feedback-bot/platform_api.py` использует тот же `DATABASE_URL`, что уже подключён к Pulse.

Обязательные переменные:

- `BOT_TOKEN`
- `ADMIN_IDS`
- `DATABASE_URL`
- `PLATFORM_BOOTSTRAP_USERS_JSON` — нужна хотя бы для первичного создания пользователей

Frontend по умолчанию обращается к `https://api.pulseteam.online`. При необходимости адрес можно переопределить через `window.AH_API_BASE`.

## Выкладка на pulseteam.online

Скопировать папку `docs/akademiya-schastya/platform/` в корень Pages как `/platform/`.

В футере публичных страниц академии добавить:

```html
<a href="/platform/" style="font-size:.78rem;font-weight:600;color:#a8a29e;text-decoration:none">Платформа для консультантов</a>
```

Не ставить в основное меню. Это не витрина.
