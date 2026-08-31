(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    try {
      tg.setHeaderColor("secondary_bg_color");
    } catch (_) {}
  }

  const roleLine = document.getElementById("roleLine");
  const screensEl = document.getElementById("screens");
  const noteEl = document.getElementById("feedbackNote");
  const locWrap = document.getElementById("locations");
  const locList = document.getElementById("locList");
  const btnBot = document.getElementById("btnBot");
  const btnClose = document.getElementById("btnClose");

  const STATUS_RU = {
    ready: "в боте",
    bot: "через бота",
    soon: "скоро",
  };

  const ACTION_HINT = {
    tests: "Тесты появятся здесь. Пока материалы — в обучении.",
    training: "Откройте бота → «📚 Материалы» / «📚 Обучение».",
    feedback_bot: "Оценка смены только в боте: кнопка из группового чата «в личку».",
    reports: "В боте: «📊 Аналитика» → «Отчёт».",
    signals: "В боте: «Горящие вопросы».",
    materials: "В боте: «📚 Материалы» — папки и загрузка файлов.",
    access: "В боте: «⚙️ Ещё» → «Подключить доступ».",
    ai_audit: "В боте: «🧠 ИИ-аудит» на главном меню.",
    network_summary: "В боте: «📁 Сводки» (админ) или отчёты по точкам.",
    billing: "Оплаты и тарифы — в следующем релизе mini app.",
    stop: "В боте: стоп-лист в меню шефа.",
  };

  let profile = null;

  function apiBase() {
    return (window.MINIAPP_API_BASE || "").replace(/\/$/, "");
  }

  async function loadProfile() {
    const initData = (tg && tg.initData) || "";
    const headers = {};
    if (initData) headers.Authorization = `tma ${initData}`;

    // демо без Telegram (браузер)
    if (!initData) {
      return {
        ok: true,
        demo: true,
        user: { first_name: "Демо" },
        role: "manager",
        role_label: "Управляющий (демо)",
        bot_username: "smena_feedback_bot",
        feedback_in_bot: true,
        note: "Оценку и отзыв о смене линейка по-прежнему оставляет в боте.",
        screens: [
          { id: "reports", title: "Отчёты", blurb: "Сводка смены и недели", status: "ready" },
          { id: "signals", title: "Горящие вопросы", blurb: "Проблемы смены", status: "ready" },
          { id: "materials", title: "Материалы", blurb: "Загрузка и обучение", status: "ready" },
          { id: "access", title: "Доступы", blurb: "Роли точки", status: "ready" },
          { id: "feedback_bot", title: "Отзыв линейки", blurb: "Только через бота", status: "bot" },
        ],
        locations: [{ id: "1", title: "Демо-точка" }],
      };
    }

    const res = await fetch(`${apiBase()}/api/miniapp/me`, { headers });
    if (!res.ok) throw new Error("auth");
    return res.json();
  }

  function render(data) {
    profile = data;
    const name = (data.user && data.user.first_name) || "";
    roleLine.textContent = `${data.role_label}${name ? " · " + name : ""}${data.demo ? " · демо" : ""}`;
    noteEl.hidden = false;
    if (data.note) {
      noteEl.querySelector("span").textContent = data.note;
    }

    screensEl.innerHTML = "";
    (data.screens || []).forEach((s) => {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "card";
      btn.innerHTML = `
        <h3>${escapeHtml(s.title)}</h3>
        <span class="badge ${escapeHtml(s.status)}">${STATUS_RU[s.status] || s.status}</span>
        <p>${escapeHtml(s.blurb || "")}</p>
      `;
      btn.addEventListener("click", () => onScreen(s));
      screensEl.appendChild(btn);
    });

    const locs = data.locations || [];
    if (locs.length) {
      locWrap.hidden = false;
      locList.innerHTML = locs
        .map((l) => `<li>${escapeHtml(l.title)}</li>`)
        .join("");
    }
  }

  function onScreen(s) {
    const hint = ACTION_HINT[s.id] || s.blurb || "";
    if (tg && tg.showPopup) {
      tg.showPopup({
        title: s.title,
        message: hint,
        buttons: [
          { id: "bot", type: "default", text: "Открыть бота" },
          { type: "close" },
        ],
      }, (id) => {
        if (id === "bot") openBot();
      });
    } else {
      alert(`${s.title}\n\n${hint}`);
    }
  }

  function openBot() {
    const un = (profile && profile.bot_username) || "smena_feedback_bot";
    const url = `https://t.me/${un}`;
    if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
    else window.open(url, "_blank");
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  btnBot.addEventListener("click", openBot);
  btnClose.addEventListener("click", () => {
    if (tg) tg.close();
    else window.close();
  });

  loadProfile()
    .then(render)
    .catch(() => {
      roleLine.textContent = "Не удалось войти";
      screensEl.innerHTML =
        '<div class="error">Откройте mini app из меню бота Telegram. Отзыв о смене по-прежнему только в боте.</div>';
    });
})();
