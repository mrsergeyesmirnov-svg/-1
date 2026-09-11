# Платформа для консультантов

Внутренний контур Академии счастья: CRM, Master Audit, калькулятор проекта, финансы, план туров.

Публичный вход — тихая ссылка в футере сайта: **«Платформа для консультантов»** → `/platform/`.

Пароль: `smena2026` (тот же, что у аудита).

## Состав

| Путь | Статус |
|---|---|
| `platform/index.html` | Хаб |
| `platform/crm.html` | CRM-стикеры клиентов |
| `platform/restaurant-audit.html` | Master Audit 150 пунктов: mobile-first, 0/1/2/N/A, Day 0/30/60, Red Flags, Excel |
| `platform/data/master-audit-01.csv` | Шаблон Master Audit, пункты 1–50 |
| `platform/data/master-audit-02.csv` | Шаблон Master Audit, пункты 51–100 |
| `platform/data/master-audit-03.csv` | Шаблон Master Audit, пункты 101–150 |
| `platform/audit.html` | Калькулятор проекта → счета/акты в CRM |
| `platform/finance.html` | Учёт: доходы/расходы, сметы, календарь, PDF, прибыль (факт), баланс |
| `platform/tours-plan.html` | Заготовка планирования туров |

## Master Audit

`restaurant-audit.html` использует ту же CRM в браузере (`crm-store.js`). Консультант выбирает ресторан, тип контрольной точки и заполняет 150 стандартов с телефона. Каждый ответ и комментарий сохраняются автоматически.

После аудита считаются общий балл и баллы по блокам, критические пункты превращаются в Red Flags. Итог можно записать комментарием в карточку клиента и выгрузить в `.xlsx`. Для Day 60 можно зафиксировать Healthy Baseline для дальнейшей логики Pulse.

Текущая beta хранит сами сессии Master Audit в `localStorage` устройства. Для командной production-версии следующий шаг — перенести `audit_sessions / audit_answers / audit_baselines` в серверную БД/API.

## Выкладка на pulseteam.online

Скопировать папку `docs/akademiya-schastya/platform/` в корень Pages как `/platform/`.

В футере публичных страниц академии добавить:

```html
<a href="/platform/" style="font-size:.78rem;font-weight:600;color:#a8a29e;text-decoration:none">Платформа для консультантов</a>
```

Не ставить в основное меню. Это не витрина.
