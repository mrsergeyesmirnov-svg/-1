# Казна на Railway — что сделать тебе

## 1. Аккаунт и CLI

1. Зайди на https://railway.app и войди через GitHub (`mrsergeyesmirnov-svg`).
2. На компе:

```bash
npm i -g @railway/cli
railway login
```

## 2. Новый проект из репо kazna

**Вариант A — через сайт (проще):**

1. Railway → **New Project** → **Deploy from GitHub repo**
2. Выбери репозиторий **`kazna`** (не `-1` / PulseTeam)
3. Если спросит Root Directory → укажи **`app`**
4. Deploy

**Вариант B — через CLI:**

```bash
git clone https://github.com/mrsergeyesmirnov-svg/kazna.git
cd kazna/app
railway init          # создаст проект, привяжет к папке
railway up            # задеплоит
railway domain        # выдаст https://….up.railway.app
```

## 3. Настройки сервиса (обязательно)

В Railway → твой сервис → **Variables**:

| Variable | Значение |
|---|---|
| `KAZNA_SECRET` | любая длинная случайная строка (пароль сессий) |
| `KAZNA_HTTPS` | `true` (после того как появится https-домен) |

**Start Command** (если сам не подхватил):

```bash
uvicorn server.main:app --host 0.0.0.0 --port $PORT
```

**Root Directory:** `app`

## 4. База данных (важно)

Сейчас по умолчанию SQLite внутри контейнера — при редеплое данные могут пропасть.

Сделай нормально:

1. В проекте Railway → **New** → **Database** → **PostgreSQL**
2. Postgres сам добавит `DATABASE_URL` в переменные
3. Перезапусти сервис Казны  
   Приложение уже умеет читать `DATABASE_URL`.

## 5. Проверка

1. Открой выданный домен Railway
2. Войди: `fin@kazna.local` / `kazna2026`
3. Вкладка **Импорт** → загрузи свой Excel с платежами
4. Обзор должен показать твои суммы

## 6. Свой домен (позже)

Railway → Service → **Settings** → **Domains** → Custom Domain  
Например `kazna.pulseteam.online` (CNAME на railway).

## Частые ошибки

- Задеплоил весь монорепо Pulse (`-1`) → не то. Нужен репо **`kazna`**, root = `app`
- Нет `KAZNA_SECRET` → сессии могут сбрасываться
- Только SQLite без Postgres → после редеплоя пустая база
- Excel не `.xlsx` / нет колонки «Сумма» → импорт ругнётся, поправь заголовки
