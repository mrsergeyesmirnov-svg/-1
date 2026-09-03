(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("#faf7f3"); } catch (_) {}
    try { tg.setBackgroundColor("#faf7f3"); } catch (_) {}
  }

  const roleLine = document.getElementById("roleLine");
  const noteEl = document.getElementById("feedbackNote");
  const noteText = document.getElementById("noteText");
  const locBar = document.getElementById("locBar");
  const locSelect = document.getElementById("locSelect");
  const tabsEl = document.getElementById("tabs");
  const viewEl = document.getElementById("view");
  const btnPeriod = document.getElementById("btnPeriod");
  const btnBot = document.getElementById("btnBot");
  const btnClose = document.getElementById("btnClose");

  const PERIODS = [
    { id: "shift", label: "Смена" },
    { id: "week", label: "Неделя" },
    { id: "month", label: "3 нед." },
  ];

  let profile = null;
  let dash = null;
  let accessOpts = null;
  let tab = "home";
  let period = "week";
  let chatId = "";
  let accessRole = null;
  let accessChatId = "";
  let accessOrgId = "";
  let lastInviteLink = "";

  function apiBase() {
    return (window.MINIAPP_API_BASE || "").replace(/\/$/, "");
  }

  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function authHeaders() {
    const initData = (tg && tg.initData) || "";
    if (!initData) throw new Error("no_telegram");
    return { Authorization: `tma ${initData}`, "Content-Type": "application/json" };
  }

  async function apiGet(path) {
    const res = await fetch(`${apiBase()}${path}`, { headers: authHeaders() });
    if (res.status === 401) throw new Error("unauthorized");
    if (res.status === 403) throw new Error("forbidden");
    if (!res.ok) throw new Error(`http_${res.status}`);
    return res.json();
  }

  async function apiPost(path, body) {
    const res = await fetch(`${apiBase()}${path}`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify(body || {}),
    });
    const data = await res.json().catch(() => ({}));
    if (res.status === 401) throw new Error("unauthorized");
    if (!res.ok || data.ok === false) {
      const err = new Error(data.error || `http_${res.status}`);
      err.payload = data;
      throw err;
    }
    return data;
  }

  function managerLike() {
    return profile && ["owner", "manager", "network", "happiness", "chef"].includes(profile.role);
  }

  function canAccessTab() {
    return profile && ["owner", "manager", "network"].includes(profile.role);
  }

  function renderTabs() {
    const screens = (profile && profile.screens) || [];
    const ids = screens.map((s) => s.id).filter((id) =>
      ["home", "reviews", "engagement", "signals", "ai", "access"].includes(id)
    );
    if (!ids.length) {
      tabsEl.hidden = true;
      return;
    }
    tabsEl.hidden = false;
    if (!ids.includes(tab)) tab = ids[0];
    tabsEl.innerHTML = ids
      .map((id) => {
        const s = screens.find((x) => x.id === id);
        return `<button type="button" data-tab="${id}" class="${id === tab ? "on" : ""}">${escapeHtml(s.title)}</button>`;
      })
      .join("");
    tabsEl.querySelectorAll("button").forEach((b) => {
      b.addEventListener("click", () => {
        tab = b.getAttribute("data-tab");
        renderTabs();
        renderView();
      });
    });
  }

  function metric(k, v, h) {
    return `<div class="metric"><div class="k">${escapeHtml(k)}</div><div class="v">${v}</div>${h ? `<div class="h">${escapeHtml(h)}</div>` : ""}</div>`;
  }

  function deptPanel(d) {
    if (!d) return "";
    const avg = d.avg_rating != null ? String(d.avg_rating).replace(".", ",") : "—";
    const tops = (d.top_blockers || [])
      .slice(0, 3)
      .map((b) => `<li>${escapeHtml(b.label)} · ${b.count}</li>`)
      .join("") || "<li>пока тихо</li>";
    return `<div class="panel"><h3>${escapeHtml(d.title)}</h3><div class="big">${avg}</div><div class="muted" style="font-size:0.78rem">${d.ratings_count} оценок</div><ul>${tops}</ul></div>`;
  }

  function renderHome() {
    if (!dash || dash.empty) {
      viewEl.innerHTML = `<div class="block"><h2>Нет точек</h2><p>${escapeHtml((dash && dash.message) || "Подключите чат и роль менеджера.")}</p></div>`;
      return;
    }
    const e = dash.engagement || {};
    const avg = e.avg_rating != null ? String(e.avg_rating).replace(".", ",") : "—";
    const pct = e.pct != null ? `${e.pct}%` : "—";
    viewEl.innerHTML = `
      <div class="metrics">
        ${metric("Средняя", avg, dash.period_label || "")}
        ${metric("Вовлечённость", pct, `${e.raters || 0} из ${e.baseline_30d || "—"}`)}
        ${metric("Оценок", String(e.ratings || 0), "за период")}
        ${metric("Горящие", String(dash.hot_count || 0), "активных")}
      </div>
      <div class="split">
        ${deptPanel(dash.floor)}
        ${deptPanel(dash.kitchen)}
      </div>
      <div class="block">
        <h2>${escapeHtml((dash.ai && dash.ai.title) || "ИИ")}</h2>
        <p>${escapeHtml((dash.ai && dash.ai.blurb) || "")}</p>
      </div>
    `;
  }

  function renderReviews() {
    if (!dash || dash.empty) { renderHome(); return; }
    const blocks = [dash.floor, dash.kitchen].map((d) => {
      const comments = (d.comments || [])
        .map((c) => `<div class="item"><div class="m">${escapeHtml(c.text)}</div></div>`)
        .join("") || `<div class="item"><div class="m">Комментариев пока нет</div></div>`;
      const tops = (d.top_blockers || [])
        .map((b) => `<span class="tag">${escapeHtml(b.label)} · ${b.count}</span> `)
        .join("");
      return `<div class="block"><h2>${escapeHtml(d.title)}</h2><p>Средняя ${d.avg_rating != null ? String(d.avg_rating).replace(".", ",") : "—"} · ${d.ratings_count} оценок</p><div style="margin:8px 0">${tops}</div><div class="list">${comments}</div></div>`;
    });
    viewEl.innerHTML = blocks.join("");
  }

  function renderEngagement() {
    if (!dash || dash.empty) { renderHome(); return; }
    const e = dash.engagement || {};
    viewEl.innerHTML = `
      <div class="metrics">
        ${metric("Ответили", String(e.raters || 0), "уникальных за период")}
        ${metric("База 30д", String(e.baseline_30d || 0), "писали раньше")}
        ${metric("Доля", e.pct != null ? e.pct + "%" : "—", "вовлечённость")}
        ${metric("Оценок", String(e.ratings || 0), dash.period_label || "")}
      </div>
      <div class="block"><h2>Как читать</h2><p>Низкая доля — напомните кнопку в группах зала и кухни.</p></div>
    `;
  }

  function renderSignals() {
    if (!dash || dash.empty) { renderHome(); return; }
    const items = (dash.hot_problems || [])
      .map((p) => {
        const st = p.status === "new" ? "hot" : "ok";
        return `<div class="item"><div class="t">${escapeHtml(p.title)}</div><div class="m">${p.mentions || 0} упоминаний</div><span class="tag ${st}">${escapeHtml(p.status_ru || p.status)}</span></div>`;
      })
      .join("");
    viewEl.innerHTML = items
      ? `<div class="list">${items}</div>`
      : `<div class="block"><h2>Тихо</h2><p>Активных горящих вопросов нет.</p></div>`;
  }

  function renderAi() {
    if (!dash || dash.empty) { renderHome(); return; }
    const tips = ((dash.ai && dash.ai.tips) || [])
      .map((t) => `<div class="tip">${escapeHtml(t)}</div>`)
      .join("");
    viewEl.innerHTML = `
      <div class="block"><h2>${escapeHtml((dash.ai && dash.ai.title) || "ИИ-советы")}</h2><p>${escapeHtml((dash.ai && dash.ai.blurb) || "")}</p></div>
      <div class="tips">${tips}</div>
    `;
  }

  function renderStaffHome() {
    viewEl.innerHTML = `
      <div class="block"><h2>Привет</h2><p>Здесь обучение и материалы. Отзыв о смене — кнопка из рабочей группы в боте.</p></div>
    `;
  }

  async function ensureAccessOpts() {
    if (accessOpts) return accessOpts;
    accessOpts = await apiGet("/api/miniapp/access/options");
    return accessOpts;
  }

  function renderAccess() {
    if (!canAccessTab()) {
      viewEl.innerHTML = `<div class="block"><p>Доступы выдаёт управляющий точки или сети.</p></div>`;
      return;
    }
    viewEl.innerHTML = `<p class="muted center">Загрузка доступов…</p>`;
    ensureAccessOpts()
      .then((opts) => {
        if (!opts.can_manage) {
          viewEl.innerHTML = `<div class="block"><p>Нет прав выдавать доступы.</p></div>`;
          return;
        }
        const locs = opts.locations || [];
        if (!accessChatId && locs[0]) {
          accessChatId = locs[0].id;
          accessOrgId = locs[0].org_id;
        }
        const loc = locs.find((l) => l.id === accessChatId) || locs[0];
        if (loc) {
          accessChatId = loc.id;
          accessOrgId = loc.org_id;
        }
        const roles = opts.roles || [];
        if (!accessRole && roles[0]) accessRole = roles[0];

        const locOptions = locs
          .map((l) => `<option value="${escapeHtml(l.id)}" data-org="${escapeHtml(l.org_id)}" ${l.id === accessChatId ? "selected" : ""}>${escapeHtml(l.title)}</option>`)
          .join("");
        const roleBtns = roles
          .map((r) => `<button type="button" data-code="${escapeHtml(r.code)}" class="${accessRole && accessRole.code === r.code ? "on" : ""}">${escapeHtml(r.label)}</button>`)
          .join("");

        viewEl.innerHTML = `
          <div class="block">
            <h2>Выдать доступ</h2>
            <p>${escapeHtml(opts.hint || "")}</p>
            <label class="muted" style="display:block;margin-top:10px;font-size:0.78rem;font-weight:700">ТОЧКА</label>
            <select id="accessLoc" class="field">${locOptions}</select>
            <label class="muted" style="display:block;margin-top:10px;font-size:0.78rem;font-weight:700">РОЛЬ</label>
            <div class="role-grid" id="roleGrid">${roleBtns}</div>
            <div class="actions">
              <button type="button" id="btnScan">Сканировать QR Telegram</button>
              <button type="button" class="secondary" id="btnInvite">Создать инвайт-QR / ссылку</button>
            </div>
            <input class="field" id="manualId" placeholder="@username или Telegram ID" />
            <div class="actions"><button type="button" class="secondary" id="btnManual">Выдать по @ или ID</button></div>
            <div id="inviteOut">${lastInviteLink ? `<div class="invite-box">${escapeHtml(lastInviteLink)}</div><p class="muted" style="margin-top:8px;font-size:0.82rem">Покажите QR этой ссылки или перешлите сотруднику. Он открывает — доступ сам.</p>` : ""}</div>
          </div>
        `;

        document.getElementById("accessLoc").addEventListener("change", (e) => {
          const opt = e.target.selectedOptions[0];
          accessChatId = e.target.value;
          accessOrgId = opt.getAttribute("data-org");
        });
        document.getElementById("roleGrid").querySelectorAll("button").forEach((b) => {
          b.addEventListener("click", () => {
            accessRole = roles.find((r) => r.code === b.getAttribute("data-code"));
            renderAccess();
          });
        });
        document.getElementById("btnScan").addEventListener("click", scanAndGrant);
        document.getElementById("btnInvite").addEventListener("click", createInvite);
        document.getElementById("btnManual").addEventListener("click", () => {
          const v = document.getElementById("manualId").value.trim();
          if (v) grantIdentity(v);
        });
      })
      .catch((e) => {
        viewEl.innerHTML = `<div class="error">${escapeHtml(e.message || "Ошибка")}</div>`;
      });
  }

  function selectedAccessPayload() {
    if (!accessRole) throw new Error("Выберите роль");
    return {
      role_code: accessRole.code,
      chat_id: accessChatId,
      org_id: accessOrgId,
    };
  }

  async function grantIdentity(text) {
    try {
      const body = { ...selectedAccessPayload(), qr: text };
      const res = await apiPost("/api/miniapp/access/grant", body);
      if (tg && tg.showAlert) {
        tg.showAlert(`Готово: ${res.role_label} · ${res.place}`);
      } else {
        alert(`Готово: ${res.role_label}`);
      }
    } catch (e) {
      const msg = (e && e.message) || "Не удалось выдать";
      if (tg && tg.showAlert) tg.showAlert(msg);
      else alert(msg);
    }
  }

  function scanAndGrant() {
    if (!tg || !tg.showScanQrPopup) {
      alert("Сканер QR доступен только внутри Telegram Mini App.");
      return;
    }
    tg.showScanQrPopup({ text: "QR человека в Telegram" }, (code) => {
      if (!code) return false;
      try { tg.closeScanQrPopup(); } catch (_) {}
      grantIdentity(code);
      return true;
    });
  }

  async function createInvite() {
    try {
      const body = selectedAccessPayload();
      const res = await apiPost("/api/miniapp/access/invite", body);
      lastInviteLink = res.link;
      renderAccess();
      if (tg && tg.openLink && navigator.share) {
        /* keep link on screen */
      }
      if (tg && tg.showPopup) {
        tg.showPopup({
          title: "Инвайт готов",
          message: "Перешлите ссылку сотруднику или покажите QR экрана. Он откроет приложение — доступ выдастся сам.",
          buttons: [{ type: "close" }],
        });
      }
    } catch (e) {
      const msg = (e && e.message) || "Не удалось создать инвайт";
      if (tg && tg.showAlert) tg.showAlert(msg);
      else alert(msg);
    }
  }

  function renderView() {
    if (!managerLike()) {
      renderStaffHome();
      return;
    }
    if (tab === "reviews") renderReviews();
    else if (tab === "engagement") renderEngagement();
    else if (tab === "signals") renderSignals();
    else if (tab === "ai") renderAi();
    else if (tab === "access") renderAccess();
    else renderHome();
  }

  function fillLocations() {
    const locs = (dash && dash.locations) || (profile && profile.locations) || [];
    if (!locs.length || !managerLike()) {
      locBar.hidden = true;
      return;
    }
    locBar.hidden = false;
    if (!chatId) chatId = locs[0].id;
    locSelect.innerHTML = locs
      .map((l) => `<option value="${escapeHtml(l.id)}" ${l.id === chatId ? "selected" : ""}>${escapeHtml(l.title)}</option>`)
      .join("");
  }

  async function loadDashboard() {
    if (!managerLike()) {
      dash = null;
      renderView();
      return;
    }
    btnPeriod.hidden = false;
    btnPeriod.textContent = PERIODS.find((p) => p.id === period)?.label || "Неделя";
    const q = new URLSearchParams({ period });
    if (chatId) q.set("chat_id", chatId);
    if (tab !== "access") {
      viewEl.innerHTML = `<p class="muted center">Собираем пульс…</p>`;
    }
    dash = await apiGet(`/api/miniapp/dashboard?${q}`);
    if (dash.chat_id) chatId = dash.chat_id;
    fillLocations();
    renderView();
  }

  async function redeemStartParam(token) {
    if (!token || !String(token).startsWith("inv_")) return;
    try {
      const res = await apiPost("/api/miniapp/access/redeem", { token });
      if (tg && tg.showAlert) {
        tg.showAlert(`Доступ выдан: ${res.role_label}`);
      }
      profile = await apiGet("/api/miniapp/me");
      renderTabs();
    } catch (e) {
      if (tg && tg.showAlert) tg.showAlert(e.message || "Инвайт не сработал");
    }
  }

  function openBot() {
    const un = (profile && profile.bot_username) || "smena_feedback_bot";
    const url = `https://t.me/${un}`;
    if (tg && tg.openTelegramLink) tg.openTelegramLink(url);
    else window.open(url, "_blank");
  }

  function showError(kind) {
    noteEl.hidden = false;
    let detail = "Не удалось войти в приложение.";
    if (kind === "no_telegram") detail = "Откройте Mini App из меню бота в Telegram.";
    else if (kind === "unauthorized") detail = "Подпись Telegram не принята. Проверьте BOT_TOKEN на Railway.";
    else if (kind === "forbidden") detail = "Этот раздел для управляющих.";
    viewEl.innerHTML = `<div class="error">${detail}</div>`;
  }

  locSelect.addEventListener("change", () => {
    chatId = locSelect.value;
    loadDashboard().catch((e) => showError(e.message));
  });

  btnPeriod.addEventListener("click", () => {
    const i = PERIODS.findIndex((p) => p.id === period);
    period = PERIODS[(i + 1) % PERIODS.length].id;
    loadDashboard().catch((e) => showError(e.message));
  });

  btnBot.addEventListener("click", openBot);
  btnClose.addEventListener("click", () => {
    if (tg) tg.close();
    else window.close();
  });

  apiGet("/api/miniapp/me")
    .then(async (data) => {
      profile = data;
      const name = (data.user && data.user.first_name) || "";
      roleLine.textContent = `${data.role_label}${name ? " · " + name : ""}`;
      noteEl.hidden = false;
      if (data.note) noteText.textContent = data.note;
      if (data.locations && data.locations[0]) chatId = data.locations[0].id;
      const start =
        data.start_param ||
        (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) ||
        "";
      if (start) await redeemStartParam(start);
      renderTabs();
      return loadDashboard();
    })
    .catch((e) => showError((e && e.message) || "network"));
})();
