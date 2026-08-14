# Памятка для агента на `landing` (pulseteam.online)

Живой домен крутится из **этого** репозитория: `mrsergeyesmirnov-svg/landing` (GitHub Pages, ветка `main`, корень).

Исходники, договор и калькулятор живут в **другом** репозитории: приватный `mrsergeyesmirnov-svg/-1`, ветка `cursor/akademiya-schastya-a3f9` (PR https://github.com/mrsergeyesmirnov-svg/-1/pull/27).

Агент, запущенный только на `landing`, **не видит** приватный `-1`: clone даёт `Repository not found`, даже если в GitHub App репозиторий галочкой стоит. Токен того агента выдан под `landing`.

Чтобы он увидел исходники, нужен **новый** Cloud Agent с двумя репозиториями в environment (`landing` + `-1`), либо прикрепить к чату файлы из `docs/akademiya-schastya/` (html/css/постеры).

Дизайн дома: тот же минимализм, что Pulse / «Состояние смены» (Mulish, крем `#faf7f3`, акцент `#ff5a1f`, много воздуха). Не тёмная «академия 2010», не серif-собор. Любовь — светом и пустотой, не бордовым бархатом.

## Что сделать в начале работы

```bash
git clone --depth 1 -b cursor/akademiya-schastya-a3f9 \
  https://github.com/mrsergeyesmirnov-svg/-1.git /tmp/src-1
```

Если clone 403 — у установки Cursor нет `-1`. Нужны оба репозитория: `landing` и `-1`.

## Карта файлов в `-1`

| Путь | Зачем |
|---|---|
| `docs/akademiya-schastya/` | Витрина дома: миссия «творить любовь», 3 входа |
| `docs/sostoyanie-smeny/` | Продукт «Состояние смены» (консалтинг + калькулятор), не старый Pulse |
| `docs/pulseteam-archive/` | Снимок старого pulseteam.online на 14.08.2026 |
| `docs/sostoyanie-smeny/partnership-agreement-draft.md` | Черновик ПТ, решения только единогласно |
| `docs/sostoyanie-smeny/partnership-memo.md` | НДС 174.1, УСН, кто мы |

## Как это должно стоять на `landing`

- `/` — Академия счастья  
- `/tury/` или `tours.html` — афиши  
- `/baza/` или `knowledge.html` — «скоро появится»  
- `/sostoyanie/` — **сайт продукта из `docs/sostoyanie-smeny/`**, не копия старого Pulse  
- архив Pulse — в `/docs/pulseteam-archive/` или `/pulse-archive/`  
- не трогать: `CNAME` (`www.pulseteam.online`), `.nojekyll`, `yandex_3ba19479d2e6410e.html`

## Смысл (не перепутать имена)

- **Кто мы:** соучредители Академии счастья — платформа осознанности и счастья с духовностью. Миссия: творить любовь.  
- **Состояние смены** — продукт академии, не имя дома.  
- **Pulse Team** целиком внутри этого контура (цифровой слой продукта).  
- Три блока: база знаний / духовные туры с мастер-классами для HoReCa / Состояние смены.  
- Решения в партнёрстве — только когда согласны оба; иначе рефлексия до компромисса.

## Агент, который уже выложил академию на домен

Коммит `52da97a` на `landing` (14.08.2026). Перед правками смотреть живой сайт: https://www.pulseteam.online/
