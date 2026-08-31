(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("#faf7f3"); } catch (_) {}
    try { tg.setBackgroundColor("#faf7f3"); } catch (_) {}
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

  function tgUser() {
    const u = tg && tg.initDataUnsafe && tg.initDataUnsafe.user;
    if (!u) return { first_name: "" };
    return {
      id: u.id,
      first_name: u.first_name || "",
      username: u.username || "",
    };
  }

  async function loadProfile() {
    const initData = (tg && tg.initData) || "";
    if (!initData) {
      throw new Error("no_telegram");
    }

    const headers = { Authorization: `tma ${initData}` };
    const url = `${apiBase()}/api/miniapp/me`;
    let res;
    try {
      res = await fetch(url, { headers });
    } catch (e) {
      const err = new Error("network");
      err.cause = e;
      throw err;
    }
    if (res.status === 401) throw new Error("unauthorized");
    if (!res.ok) throw new Error(`http_${res.status}`);
    const data = await res.json();
    if (!data || data.ok === false) throw new Error("bad_payload");
    return data;
  }

  function render(data) {
    profile = data;
    const name = (data.user && data.user.first_name) || "";
    roleLine.textContent = `${data.role_label}${name ? " · " + name : ""}`;
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

  function renderApiError(kind) {
    const user = tgUser();
    roleLine.textContent = user.first_name
      ? `${user.first_name} · нет связи с API`
      : "Нет связи с API ролей";

    noteEl.hidden = false;
    noteEl.querySelector("span").textContent =
      "Отзыв о смене по-прежнему только в боте. Ниже — что нужно для входа в приложение.";

    let detail;
    if (kind === "no_telegram") {
      detail =
        "Откройте Mini App <b>из меню бота</b> в Telegram (не из браузера).";
    } else if (kind === "unauthorized") {
      detail =
        "Подпись Telegram не принята сервером. Проверьте, что API крутится с тем же <code>BOT_TOKEN</code>, что и бот.";
    } else {
      detail =
        "UI открыт с сайта, а <b>/api/miniapp/me</b> там нет.<br><br>" +
        "<b>Как должно быть:</b> BotFather → Menu Button URL = HTTPS адрес <b>Railway-бота</b> " +
        "(там и статика Mini App, и API), либо на Pages задать " +
        "<code>window.MINIAPP_API_BASE = \"https://….up.railway.app\"</code>.<br><br>" +
        "Сейчас в меню, скорее всего, стоит только Pages без API — поэтому «нет доступа».";
    }

    screensEl.innerHTML = `<div class="error">${detail}</div>`;
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
    .catch((e) => renderApiError((e && e.message) || "network"));
})();
