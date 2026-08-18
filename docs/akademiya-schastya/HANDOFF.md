# Памятка для агента на `landing` (pulseteam.online)

Живой домен крутится из **этого** репозитория: `mrsergeyesmirnov-svg/landing` (GitHub Pages, ветка `main`, корень).

Исходники живут в приватном `mrsergeyesmirnov-svg/-1`, ветка `cursor/akademiya-schastya-a3f9` (PR https://github.com/mrsergeyesmirnov-svg/-1/pull/27).

Агент только на `landing` **не видит** `-1`. Нужны оба репозитория в environment либо файлы из `docs/akademiya-schastya/` в чате.

Дизайн: Mulish, крем `#faf7f3`, акцент `#ff5a1f`. Не тёмная «академия 2010».

## Смысл витрины

- **Академия** = дом. Без «творить любовь». Задача — вход в продукт.
- **«Состояние смены»** = методичка 1.0 целиком: 8 этапов, 4 блока, цикл, матрица S–XL.
- Туры и база — соседние двери.
- Pulse / CRM / аудит не в публичном меню.

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
