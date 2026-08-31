# Памятка для агента на `landing` (pulseteam.online)

Живой домен крутится из **этого** репозитория: `mrsergeyesmirnov-svg/landing` (GitHub Pages, ветка `main`, корень).

Исходники живут в приватном `mrsergeyesmirnov-svg/-1`, ветка `cursor/akademiya-schastya-a3f9` (PR https://github.com/mrsergeyesmirnov-svg/-1/pull/27).

Агент только на `landing` **не видит** `-1`. Нужны оба репозитория в environment либо файлы из `docs/akademiya-schastya/` в чате.

Дизайн: Mulish, крем `#faf7f3`, акцент `#ff5a1f`. Не тёмная «академия 2010».

## Смысл витрины

Архитектура из 10 пунктов брифа «Сайт Академия Счастья».
Главный продукт — **Состояние смены** (зачем бизнесу). Туры только «планируются». Без «методичка 1.0».

```bash
git clone --depth 1 -b cursor/akademiya-schastya-a3f9 \
  https://github.com/mrsergeyesmirnov-svg/-1.git /tmp/src-1
```

| Путь | Зачем |
|---|---|
| `docs/akademiya-schastya/` | Витрина дома: прижимает к продукту |
| `docs/akademiya-schastya/tours.html` | Первый выезд шефов |
| `docs/sostoyanie-smeny/` | Полная страница продукта |
| `docs/sostoyanie-smeny/metodika-sostoyanie-smeny-1.0.pdf` | Методичка 1.0 |
| `docs/akademiya-schastya/platform/` | CRM/финансы → live `/platform/` |

В футере публичных страниц: тихая ссылка `Платформа для консультантов` → `/platform/` (пароль `smena2026`).

Живой сайт сейчас: https://www.pulseteam.online/ — после этой переписи нужно заново выложить html/css с `-1`.

## Mini App (август 2026)

**Важно:** UI и API должны быть на одном HTTPS (или API явно прописан).

| Что | Где |
|---|---|
| Статика + `GET /api/miniapp/me` | Railway-бот (`MINIAPP_HTTP=1`, порт `PORT`) |
| BotFather Menu Button URL | тот же Railway HTTPS, напр. `https://….up.railway.app/` |
| Env | `MINIAPP_URL=https://….up.railway.app/` |

Pages `/sostoyanie/app/` — только демо/зеркало. Если меню ведёт на Pages без  
`window.MINIAPP_API_BASE` → в приложении будет «нет связи с API».

Исходники UI: `docs/sostoyanie-smeny/app/` · копия: `restaurant-feedback-bot/miniapp/`.

Отзыв линейки о смене — только в боте.
