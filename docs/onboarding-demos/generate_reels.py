#!/usr/bin/env python3
"""Generate manager onboarding demo reels (self-contained HTML + shared CSS)."""
from __future__ import annotations

import json
from pathlib import Path

OUT = Path(__file__).resolve().parent

# Each reel: id, title, eyebrow, captions[{h,p}], phone_html, timeline JS steps, next/prev links
# Timeline is a list of {t: ms, do: "cap|on|off|tap|text|hi|press|cls"} described in player.

REELS: list[dict] = [
    {
        "id": "demo-manager-menu",
        "file": "demo-manager-menu.html",
        "title": "Главное меню менеджера",
        "eyebrow": "Онбординг · карта меню",
        "nav_next": "demo-manager-report.html",
        "nav_next_label": "Отчёт →",
        "captions": [
            ("Четыре папки — вся работа", "Не ищите команды. Кнопки внизу ведут по смыслу."),
            ("Аналитика", "Отчёт и «Горящие вопросы» — сюда смотрите утром и после пика."),
            ("Смена", "День, стоп, кухня, план — операционка точки без чатов «куда писать»."),
            ("Ещё", "Доступ, материалы, поддержка, подключение точки."),
            ("Запомните маршрут", "Аналитика → сигнал. Смена → день. Ещё → доступ."),
        ],
        "phone_sub": "бот · менеджер",
        "body": """
            <div class="daychip" data-el="day">сегодня · онбординг</div>
            <div class="bubble" data-el="hello">
              <strong>Меню менеджера</strong><br />
              <span class="soft">Бистро на Невском</span><br /><br />
              Выберите папку ниже. Все действия — из лички с ботом.
            </div>
            <div class="replybar" id="bar">
              <div class="row">
                <div class="chip" data-el="c1">📊 Аналитика</div>
                <div class="chip" data-el="c2">📋 Смена</div>
              </div>
              <div class="row">
                <div class="chip" data-el="c3">⚙️ Ещё</div>
              </div>
            </div>
        """,
        "timeline": [
            {"t": 600, "cap": 0, "on": ["day", "hello", "c1", "c2", "c3"]},
            {"t": 2200, "cap": 1, "hi": "c1", "tap": "c1"},
            {"t": 4000, "cap": 2, "hi": "c2", "unhi": "c1", "tap": "c2"},
            {"t": 5800, "cap": 3, "hi": "c3", "unhi": "c2", "tap": "c3"},
            {"t": 7600, "cap": 4, "unhi": "c3"},
            {"t": 11000, "loop": True},
        ],
    },
    {
        "id": "demo-manager-report",
        "file": "demo-manager-report.html",
        "title": "Отчёт по точке",
        "eyebrow": "Онбординг · Аналитика",
        "nav_next": "demo-manager-signals.html",
        "nav_next_label": "Сигналы →",
        "captions": [
            ("Аналитика → Отчёт", "Сначала точка, потом зал / кухня / всё, потом период."),
            ("Сводка без разбора чатов", "Оценка, дисциплина смен, что мешало чаще всего."),
            ("Готовые выводы", "Не «средняя 3,8» — что проверить на планёрке."),
            ("PDF одной кнопкой", "Собственнику или сети — без копипаста из Telegram."),
        ],
        "phone_sub": "бот · аналитика",
        "body": """
            <div class="daychip" data-el="day">понедельник · 09:12</div>
            <div class="bubble" data-el="pick">
              <strong>Отчёт</strong><br />
              <span class="soft">Выберите точку → отдел → период</span>
              <div class="kbd" style="margin-top:8px">
                <div class="kbtn primary" data-el="b1">Бистро на Невском</div>
                <div class="kbtn" data-el="b2">🍽 Зал · 👨‍🍳 Кухня · 🏠 Всё</div>
                <div class="kbtn" data-el="b3">Неделя</div>
              </div>
            </div>
            <div class="bubble" data-el="rep">
              <strong>📊 Сводка</strong><br />
              <span class="soft">Неделя · кухня</span>
              <div class="metric">
                <span data-el="m1">оценка <b>3,6</b></span>
                <span data-el="m2">без закрытия <b>1</b></span>
              </div>
              <div class="quote" data-el="q1">Вывод: перегруз кухни в пик · проверить отдачу</div>
              <div class="kbd" style="margin-top:8px">
                <div class="kbtn primary" data-el="pdf">📄 Выгрузить PDF</div>
              </div>
            </div>
            <div class="toast" data-el="toast">PDF готов · можно переслать сети</div>
        """,
        "timeline": [
            {"t": 500, "cap": 0, "on": ["day", "pick", "b1", "b2", "b3"]},
            {"t": 1800, "tap": "b1", "press": "b1"},
            {"t": 2800, "cap": 1, "on": ["rep", "m1", "m2"]},
            {"t": 4200, "cap": 2, "on": ["q1"]},
            {"t": 5600, "cap": 3, "on": ["pdf"], "tap": "pdf", "press": "pdf"},
            {"t": 6800, "on": ["toast"]},
            {"t": 10500, "loop": True},
        ],
    },
    {
        "id": "demo-manager-signals",
        "file": "demo-manager-signals.html",
        "title": "Горящие вопросы",
        "eyebrow": "Онбординг · Аналитика",
        "nav_next": "demo-manager-alert.html",
        "nav_next_label": "Push-алерт →",
        "captions": [
            ("Список тем, не чат", "Тема всплывает, когда несколько отметили одно и то же."),
            ("Статусы общие", "Новая → В работе → Решена. Все менеджеры точки видят одно."),
            ("Комментарий для команды", "Пишете коротко — в группу уходит смысл, без ваших имён."),
            ("Не дублируйте работу", "Если коллега уже взял — статус уже «В работе»."),
        ],
        "phone_sub": "бот · сигналы",
        "body": """
            <div class="daychip" data-el="day">суббота · 11:18</div>
            <div class="bubble" data-el="board">
              <strong>🔥 Горящие вопросы</strong><br />
              <span class="soft">Бистро на Невском</span>
              <div style="margin-top:8px">
                <span class="status-pill work">🟡 В работе</span><br />
                <strong>Нехватка персонала</strong><br />
                <span class="soft">12 отметок · 7 дней</span>
              </div>
              <div style="margin-top:10px" data-el="card2">
                <span class="status-pill new">🔴 Новая</span><br />
                <strong>Медленная кухня</strong><br />
                <span class="soft">9 отметок · 7 дней</span>
              </div>
              <div class="kbd" style="margin-top:10px">
                <div class="kbtn primary" data-el="bw">🟡 В работу</div>
                <div class="kbtn" data-el="br">🟢 Решена</div>
                <div class="kbtn" data-el="bc">✍️ Комментарий</div>
              </div>
            </div>
            <div class="bubble peer" data-el="peer">
              👤 <strong>коллега</strong> видит обновление<br />
              🟡 Медленная кухня → <strong>В работе</strong>
            </div>
            <div class="toast" data-el="toast">В группу смены — без имён менеджеров</div>
        """,
        "timeline": [
            {"t": 500, "cap": 0, "on": ["day", "board", "card2"]},
            {"t": 2000, "cap": 1, "on": ["bw", "br", "bc"]},
            {"t": 3400, "cap": 2, "tap": "bw", "press": "bw", "text": ["bw", "✓ В работе"]},
            {"t": 4800, "cap": 3, "on": ["peer"]},
            {"t": 6000, "on": ["toast"]},
            {"t": 10000, "loop": True},
        ],
    },
    {
        "id": "demo-manager-day",
        "file": "demo-manager-day.html",
        "title": "День: план и чек-листы",
        "eyebrow": "Онбординг · Смена → День",
        "nav_next": "demo-manager-close.html",
        "nav_next_label": "Закрытие →",
        "captions": [
            ("Смена → День", "План дня, чек-листы, сообщение шефу — в одной папке."),
            ("Сначала галочки открытия", "Пока чек-лист не закрыт — план в группу не уйдёт."),
            ("План уходит в смену", "Команда видит фокус дня: усиление, банкет, стоп."),
            ("📣 Шефу и менеджерам", "Короткий оперсигнал без отдельного «рабочего чата»."),
        ],
        "phone_sub": "бот · смена",
        "body": """
            <div class="daychip" data-el="day">суббота · 10:40</div>
            <div class="bubble" data-el="cl">
              <strong>☀️ Открытие смены</strong><br />
              <span class="soft">Бистро на Невском</span>
              <ul>
                <li class="done" data-el="i1">☑️ Зал и санузлы</li>
                <li class="done" data-el="i2">☑️ Касса и терминалы</li>
                <li data-el="i3">☐ Стоп согласован с кухней</li>
              </ul>
            </div>
            <div class="replybar">
              <div class="row">
                <div class="chip" data-el="p1">☀️ План дня</div>
                <div class="chip" data-el="p2">📋 Чек-листы</div>
              </div>
              <div class="row">
                <div class="chip" data-el="p3">📣 Шефу и менеджерам</div>
              </div>
            </div>
            <div class="bubble" data-el="plan">
              📋 <strong>План на субботу</strong><br />
              Усиление зала с 19:00 · стол №4 · стоп: тунец до 15:00
            </div>
            <div class="toast" data-el="toast">Опубликовано в группу смены</div>
        """,
        "timeline": [
            {"t": 500, "cap": 0, "on": ["day", "cl", "i1", "i2", "i3", "p1", "p2", "p3"]},
            {"t": 2000, "cap": 1, "tap": "i3", "text": ["i3", "☑️ Стоп согласован с кухней"], "cls": ["i3", "done"]},
            {"t": 3600, "cap": 2, "hi": "p1", "tap": "p1", "on": ["plan"]},
            {"t": 5200, "on": ["toast"]},
            {"t": 6600, "cap": 3, "hi": "p3", "unhi": "p1", "tap": "p3"},
            {"t": 10000, "loop": True},
        ],
    },
    {
        "id": "demo-manager-close",
        "file": "demo-manager-close.html",
        "title": "Закрытие дня",
        "eyebrow": "Онбординг · Смена → День",
        "nav_next": "demo-manager-stop.html",
        "nav_next_label": "Стоп-лист →",
        "captions": [
            ("Вечер — закрытие", "Чек-лист закрытия + итоги дня. Дисциплина видна в отчёте."),
            ("Без «забыли закрыть»", "Дни без закрытия копятся в сводке — сеть это видит."),
            ("Закрыть за дату", "Если вчера уехали без закрытия — догоняете из «План»."),
            ("Привычка смены", "Открытие утром · закрытие вечером · линия отвечает после смены."),
        ],
        "phone_sub": "бот · закрытие",
        "body": """
            <div class="daychip" data-el="day">суббота · 23:55</div>
            <div class="bubble" data-el="cl">
              <strong>🌙 Закрытие смены</strong><br />
              <span class="soft">чек-лист · 4 пункта</span>
              <ul>
                <li class="done" data-el="i1">☑️ Касса сверена</li>
                <li class="done" data-el="i2">☑️ Зал сдан</li>
                <li data-el="i3">☐ Итоги дня заполнены</li>
              </ul>
            </div>
            <div class="bubble" data-el="sum">
              <strong>Закрытие дня</strong><br />
              Выручка · метрика · короткий комментарий<br />
              <span class="soft">После галочек уходит в учёт точки</span>
            </div>
            <div class="kbd">
              <div class="kbtn primary" data-el="btn">✅ Закрыть день</div>
              <div class="kbtn" data-el="past">Закрыть за дату</div>
            </div>
            <div class="toast" data-el="toast">День закрыт · дисциплина +1</div>
        """,
        "timeline": [
            {"t": 500, "cap": 0, "on": ["day", "cl", "i1", "i2", "i3"]},
            {"t": 2000, "cap": 1, "on": ["sum", "btn", "past"]},
            {"t": 3400, "tap": "i3", "text": ["i3", "☑️ Итоги дня заполнены"], "cls": ["i3", "done"]},
            {"t": 4600, "cap": 2, "tap": "btn", "press": "btn"},
            {"t": 5600, "cap": 3, "on": ["toast"]},
            {"t": 9500, "loop": True},
        ],
    },
    {
        "id": "demo-manager-stop",
        "file": "demo-manager-stop.html",
        "title": "Стоп-лист",
        "eyebrow": "Онбординг · Смена → Стоп",
        "nav_next": "demo-manager-kitchen.html",
        "nav_next_label": "Кухня →",
        "captions": [
            ("Стоп в одном месте", "Менеджер или шеф пишут позиции — бот публикует в группу."),
            ("Актуальный стоп", "Всегда можно подтянуть текущий список, не искать в истории."),
            ("Добавить позицию", "Дописали — смена видит обновление без отдельного пинга."),
            ("В отчёте — частота", "Что чаще всего в стопе за период — видно управляющему."),
        ],
        "phone_sub": "бот · стоп",
        "body": """
            <div class="daychip" data-el="day">суббота · 11:40</div>
            <div class="bubble" data-el="ask">
              🛑 <strong>Стоп-лист на смену</strong><br />
              Напишите позиции или откройте актуальный стоп.
            </div>
            <div class="bubble mine" data-el="out">
              тунец до 15:00 · ризотто с морепродуктами
            </div>
            <div class="bubble" data-el="ok">
              Стоп опубликован в группу смены ✅
            </div>
            <div class="replybar">
              <div class="row">
                <div class="chip" data-el="s1">🛑 Стоп-лист</div>
                <div class="chip" data-el="s2">➕ В стоп-лист</div>
              </div>
              <div class="row">
                <div class="chip" data-el="s3">Актуальный стоп</div>
              </div>
            </div>
        """,
        "timeline": [
            {"t": 500, "cap": 0, "on": ["day", "ask", "s1", "s2", "s3"]},
            {"t": 1800, "cap": 1, "hi": "s3", "tap": "s3"},
            {"t": 3200, "cap": 2, "hi": "s2", "unhi": "s3", "tap": "s2", "on": ["out"]},
            {"t": 4600, "on": ["ok"]},
            {"t": 6000, "cap": 3},
            {"t": 9500, "loop": True},
        ],
    },
    {
        "id": "demo-manager-kitchen",
        "file": "demo-manager-kitchen.html",
        "title": "Кухня: оценка смены",
        "eyebrow": "Онбординг · Смена → Кухня",
        "nav_next": "demo-manager-plan.html",
        "nav_next_label": "План →",
        "captions": [
            ("Шеф и менеджер — в личке", "Оценка смены кухни не смешивается с опросом зала."),
            ("Что мешало кухне", "Забирали еду, посуда, коммуникация — темы для разбора."),
            ("Стоп рядом", "Из папки «Кухня» и «Стоп» — один контур с шефом."),
            ("В сводке отдельно", "Отчёт можно фильтровать: только кухня."),
        ],
        "phone_sub": "бот · кухня",
        "body": """
            <div class="daychip" data-el="day">суббота · 23:50</div>
            <div class="bubble" data-el="q">
              <strong>Как прошла смена на кухне?</strong><br />
              Оценка 1–5, затем что мешало.
            </div>
            <div class="kbd">
              <div class="kbtn" data-el="stars">1 ⭐  2 ⭐  3 ⭐  4 ⭐  5 ⭐</div>
            </div>
            <div class="bubble" data-el="thanks">
              Оценка <strong>3</strong> сохранена<br />
              <span class="soft">Что мешало на кухне?</span>
            </div>
            <div class="kbd">
              <div class="kbtn" data-el="k1">⏱ Долго забирали еду</div>
              <div class="kbtn" data-el="k2">🍽 Долго несли посуду</div>
              <div class="kbtn" data-el="k3">💬 Коммуникация</div>
            </div>
            <div class="toast" data-el="toast">Попадёт в отчёт «Кухня»</div>
        """,
        "timeline": [
            {"t": 500, "cap": 0, "on": ["day", "q", "stars"]},
            {"t": 2000, "cap": 1, "tap": "stars", "press": "stars", "on": ["thanks", "k1", "k2", "k3"]},
            {"t": 3600, "tap": "k1", "press": "k1"},
            {"t": 4800, "cap": 2, "on": ["toast"]},
            {"t": 6200, "cap": 3},
            {"t": 9500, "loop": True},
        ],
    },
    {
        "id": "demo-manager-plan",
        "file": "demo-manager-plan.html",
        "title": "План: задания и кадры",
        "eyebrow": "Онбординг · Смена → План",
        "nav_next": "demo-manager-access.html",
        "nav_next_label": "Доступ →",
        "captions": [
            ("План на месяц и задания", "Не только день — задачи, которые живут дольше смены."),
            ("Уход кадра", "Фиксируете уход — не теряется в личных переписках."),
            ("Задания точке", "Список задач с статусами — видно менеджерам точки."),
            ("Догнать закрытие", "«Закрыть за дату» — если вчера забыли."),
        ],
        "phone_sub": "бот · план",
        "body": """
            <div class="daychip" data-el="day">папка · План</div>
            <div class="bubble" data-el="hello">
              <strong>📅 План</strong><br />
              Месяц, задания, уход кадра, закрытие за дату.
            </div>
            <div class="replybar">
              <div class="row">
                <div class="chip" data-el="a1">План на месяц</div>
                <div class="chip" data-el="a2">Уход кадра</div>
              </div>
              <div class="row">
                <div class="chip" data-el="a3">Задания</div>
              </div>
              <div class="row">
                <div class="chip" data-el="a4">Закрыть за дату</div>
              </div>
            </div>
            <div class="bubble" data-el="tasks">
              <strong>Задания</strong><br />
              ☐ Разобрать отдачу с шефом<br />
              ☑ Усилить пятницу +1 в зале<br />
              <span class="soft">Статусы общие для менеджеров точки</span>
            </div>
        """,
        "timeline": [
            {"t": 500, "cap": 0, "on": ["day", "hello", "a1", "a2", "a3", "a4"]},
            {"t": 2000, "cap": 1, "hi": "a2", "tap": "a2"},
            {"t": 3600, "cap": 2, "hi": "a3", "unhi": "a2", "tap": "a3", "on": ["tasks"]},
            {"t": 5400, "cap": 3, "hi": "a4", "unhi": "a3", "tap": "a4"},
            {"t": 9000, "loop": True},
        ],
    },
    {
        "id": "demo-manager-access",
        "file": "demo-manager-access.html",
        "title": "Доступ и подключение",
        "eyebrow": "Онбординг · Ещё",
        "nav_next": "demo-manager-materials.html",
        "nav_next_label": "Материалы →",
        "captions": [
            ("Ещё → доступ", "Подключить менеджера или отозвать — без правки чатов руками."),
            ("Как подключить точку", "Группа смены + бот + привязка организации — короткий чеклист."),
            ("Роли", "Менеджер точки, старший, шеф — разные кнопки, одна точка."),
            ("Поддержка рядом", "Если застряли — «Поддержка» в той же папке."),
        ],
        "phone_sub": "бот · ещё",
        "body": """
            <div class="daychip" data-el="day">настройка доступа</div>
            <div class="bubble" data-el="more">
              <strong>⚙️ Ещё</strong><br />
              Доступ, материалы, подписка, поддержка, подключение.
            </div>
            <div class="replybar">
              <div class="row">
                <div class="chip" data-el="a1">Подключить доступ</div>
                <div class="chip" data-el="a2">Отозвать доступ</div>
              </div>
              <div class="row">
                <div class="chip" data-el="a3">📚 Материалы</div>
              </div>
              <div class="row">
                <div class="chip" data-el="a4">Как подключить точку</div>
              </div>
              <div class="row">
                <div class="chip" data-el="a5">Подписка/статус</div>
                <div class="chip" data-el="a6">Поддержка</div>
              </div>
            </div>
            <div class="bubble" data-el="conn">
              <strong>Как подключить точку</strong><br />
              1. Группа смены<br />
              2. Добавить бота<br />
              3. Привязка к организации<br />
              4. Время напоминания<br />
              <span class="soft">Оценки — только в личке по кнопке из группы</span>
            </div>
        """,
        "timeline": [
            {"t": 500, "cap": 0, "on": ["day", "more", "a1", "a2", "a3", "a4", "a5", "a6"]},
            {"t": 2000, "hi": "a1", "tap": "a1"},
            {"t": 3400, "cap": 1, "hi": "a4", "unhi": "a1", "tap": "a4", "on": ["conn"]},
            {"t": 5200, "cap": 2},
            {"t": 6800, "cap": 3, "hi": "a6", "unhi": "a4", "tap": "a6"},
            {"t": 10000, "loop": True},
        ],
    },
    {
        "id": "demo-manager-materials",
        "file": "demo-manager-materials.html",
        "title": "Материалы и обучение",
        "eyebrow": "Онбординг · Ещё → Материалы",
        "nav_next": "demo-waiter-checkin.html",
        "nav_next_label": "Линия →",
        "captions": [
            ("📚 Материалы — ваша шпаргалка", "Ролики и памятки лежат в боте, не в переписке с Сергеем."),
            ("Папки по ролям", "Менеджер / шеф / линия — открываете своё."),
            ("Сначала карта меню", "Потом алерт, сигналы, день — по одному ролику."),
            ("Вернитесь сюда", "Если запутались на смене — снова «Ещё → Материалы»."),
        ],
        "phone_sub": "бот · материалы",
        "body": """
            <div class="daychip" data-el="day">📚 обучение</div>
            <div class="bubble" data-el="pack">
              <strong>Материалы · менеджер</strong><br />
              <span class="soft">Бистро на Невском</span>
              <ul style="margin-top:8px">
                <li data-el="f1">📁 01 · Карта меню</li>
                <li data-el="f2">📁 02 · Аналитика и сигналы</li>
                <li data-el="f3">📁 03 · Смена (день / стоп / кухня)</li>
                <li data-el="f4">📁 04 · Доступ и точка</li>
              </ul>
            </div>
            <div class="kbd">
              <div class="kbtn primary" data-el="open">Открыть · Карта меню</div>
            </div>
            <div class="toast" data-el="toast">Ролик играет в Telegram / браузере</div>
        """,
        "timeline": [
            {"t": 500, "cap": 0, "on": ["day", "pack", "f1", "f2", "f3", "f4"]},
            {"t": 2000, "cap": 1},
            {"t": 3400, "cap": 2, "on": ["open"], "tap": "open", "press": "open"},
            {"t": 4800, "cap": 3, "on": ["toast"]},
            {"t": 9000, "loop": True},
        ],
    },
    {
        "id": "demo-manager-broadcast",
        "file": "demo-manager-broadcast.html",
        "title": "Сообщение шефу и менеджерам",
        "eyebrow": "Онбординг · Смена → День",
        "nav_next": "demo-manager-day.html",
        "nav_next_label": "День →",
        "captions": [
            ("📣 Не в общий чат линии", "Оперсигнал уходит шефу и менеджерам точки."),
            ("Коротко и по делу", "Пик, банкет, поломка — без флуда в смену."),
            ("Рядом с планом дня", "Та же папка «День» — не искать команду."),
            ("Линия не видит", "Официанты не получают внутренний опершум."),
        ],
        "phone_sub": "бот · оперсвязь",
        "body": """
            <div class="daychip" data-el="day">суббота · 18:10</div>
            <div class="bubble" data-el="ask">
              📣 <strong>Шефу и менеджерам</strong><br />
              Напишите сообщение — уйдёт только управленческому контуру точки.
            </div>
            <div class="bubble mine" data-el="out">
              С 19:00 полный зал · кухня держит отдачу · стоп тунец
            </div>
            <div class="bubble" data-el="ok">
              Отправлено: шеф + 2 менеджера ✅
            </div>
            <div class="toast" data-el="toast">В группу смены не публиковалось</div>
        """,
        "timeline": [
            {"t": 500, "cap": 0, "on": ["day", "ask"]},
            {"t": 2000, "cap": 1, "on": ["out"]},
            {"t": 3600, "cap": 2, "on": ["ok"]},
            {"t": 5000, "cap": 3, "on": ["toast"]},
            {"t": 9000, "loop": True},
        ],
    },
]


SHELL = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
  <title>PulseTeam · {title}</title>
  <link rel="stylesheet" href="assets/reel.css" />
</head>
<body>
  <div class="stage">
    <div class="eyebrow" id="eyebrow">{eyebrow}</div>
    <div class="brand" id="brand">Pulse<span>Team</span></div>
    <div class="caption" id="caption">
{captions_html}
    </div>
    <div class="phone-wrap" id="phoneWrap">
      <div class="phone">
        <div class="screen">
          <div class="tg-top">
            <div class="avatar">PT</div>
            <div>
              <div class="name">PulseTeam</div>
              <div class="sub">{phone_sub}</div>
            </div>
          </div>
          <div class="chat" id="chat">
{body}
            <div class="finger" id="finger"></div>
          </div>
        </div>
      </div>
    </div>
    <div class="progress" id="progress">{dots}</div>
    <p class="hint">Онбординг · полный ролик · не для публичного сайта</p>
    <div class="controls" id="controls">
      <button type="button" id="replay">↻ Ещё раз</button>
      <a class="ghost" href="{nav_next}">{nav_next_label}</a>
      <a class="ghost" href="index.html">Все ролики</a>
    </div>
  </div>
  <script>
    const TIMELINE = {timeline_json};
    const $ = (id) => document.getElementById(id);
    const el = (name) => document.querySelector('[data-el="' + name + '"]');
    const slides = [...document.querySelectorAll(".caption .slide")];
    const dots = [...document.querySelectorAll("#progress i")];
    let timers = [];
    const at = (ms, fn) => timers.push(setTimeout(fn, ms));
    function clear() {{ timers.forEach(clearTimeout); timers = []; }}
    function cap(i) {{
      slides.forEach((s) => s.classList.toggle("on", Number(s.dataset.i) === i));
      dots.forEach((d, n) => d.classList.toggle("on", n === i));
    }}
    function reset() {{
      document.querySelectorAll("[data-el]").forEach((n) => {{
        n.classList.remove("on", "pressed", "hi", "done", "flash");
        if (n.dataset.orig) n.innerHTML = n.dataset.orig;
      }});
      document.querySelectorAll("[data-el]").forEach((n) => {{
        if (!n.dataset.orig) n.dataset.orig = n.innerHTML;
      }});
      $("finger").classList.remove("flash");
    }}
    function tapOn(name) {{
      const node = el(name);
      if (!node) return;
      const f = $("finger");
      const chat = $("chat").getBoundingClientRect();
      const r = node.getBoundingClientRect();
      f.style.left = (r.left - chat.left + r.width * 0.55) + "px";
      f.style.top = (r.top - chat.top + r.height * 0.5) + "px";
      f.classList.remove("flash");
      void f.offsetWidth;
      f.classList.add("flash");
    }}
    function apply(step) {{
      if (step.cap !== undefined) cap(step.cap);
      (step.on || []).forEach((n) => el(n)?.classList.add("on"));
      (step.off || []).forEach((n) => el(n)?.classList.remove("on"));
      if (step.hi) {{
        document.querySelectorAll(".chip.hi,.cell.hi").forEach((n) => n.classList.remove("hi"));
        el(step.hi)?.classList.add("hi");
      }}
      if (step.unhi) el(step.unhi)?.classList.remove("hi");
      if (step.press) el(step.press)?.classList.add("pressed");
      if (step.tap) tapOn(step.tap);
      if (step.text) {{
        const [name, html] = step.text;
        const n = el(name);
        if (n) n.innerHTML = html;
      }}
      if (step.cls) {{
        const [name, c] = step.cls;
        el(name)?.classList.add(c);
      }}
      if (step.loop) play();
    }}
    function play() {{
      clear();
      reset();
      cap(0);
      ["eyebrow","brand","phoneWrap","controls"].forEach((id) => $(id).classList.add("on"));
      TIMELINE.forEach((step) => at(step.t, () => apply(step)));
    }}
    document.querySelectorAll("[data-el]").forEach((n) => {{ n.dataset.orig = n.innerHTML; }});
    if (new URLSearchParams(location.search).has("rec")) {{
      document.body.classList.add("rec");
    }}
    $("replay").addEventListener("click", play);
    requestAnimationFrame(() => play());
  </script>
</body>
</html>
"""


def captions_html(caps: list[tuple[str, str]]) -> str:
    parts = []
    for i, (h, p) in enumerate(caps):
        on = " on" if i == 0 else ""
        parts.append(
            f'      <div class="slide{on}" data-i="{i}">\n'
            f"        <h1>{h}</h1>\n"
            f"        <p>{p}</p>\n"
            f"      </div>"
        )
    return "\n".join(parts)


def main() -> None:
    for reel in REELS:
        html = SHELL.format(
            title=reel["title"],
            eyebrow=reel["eyebrow"],
            captions_html=captions_html(reel["captions"]),
            phone_sub=reel["phone_sub"],
            body=reel["body"].rstrip(),
            dots="".join("<i></i>" for _ in reel["captions"]),
            nav_next=reel["nav_next"],
            nav_next_label=reel["nav_next_label"],
            timeline_json=json.dumps(reel["timeline"], ensure_ascii=False),
        )
        path = OUT / reel["file"]
        path.write_text(html, encoding="utf-8")
        print("wrote", path.name)


if __name__ == "__main__":
    main()
