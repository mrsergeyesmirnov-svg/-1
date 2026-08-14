# Академия счастья — сайт

Публичная витрина платформы. Миссия: **творить любовь**.

«Состояние смены» — продукт академии, не «кто мы». Pulse Team целиком входит в этот контур; старая витрина лежит в `docs/pulseteam-archive/`.

Живой домен **pulseteam.online** крутится из репозитория [`mrsergeyesmirnov-svg/landing`](https://github.com/mrsergeyesmirnov-svg/landing). Cursor-боту туда пока нельзя пушить (403). Чтобы академия открылась по той же ссылке: GitHub → Settings → Applications → Cursor → Repository access → добавить **landing**, затем написать агенту «залей».

## Страницы

| Файл | Назначение |
|---|---|
| `index.html` | Главная: миссия + три входа |
| `knowledge.html` | База знаний — «скоро появится» |
| `tours.html` | Туры и афиши |
| `../sostoyanie-smeny/` | Проект «Состояние смены» |

## Локально

Из корня репозитория, чтобы работала ссылка на «Состояние смены»:

```bash
cd docs && python3 -m http.server 8767
```

Открыть: http://127.0.0.1:8767/akademiya-schastya/
