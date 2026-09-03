(() => {
  const tg = window.Telegram && window.Telegram.WebApp;
  if (tg) {
    tg.ready();
    tg.expand();
    try { tg.setHeaderColor("#f2f0ec"); } catch (_) {}
    try { tg.setBackgroundColor("#f2f0ec"); } catch (_) {}
  }

  const roleLine = document.getElementById("roleLine");
  const locBar = document.getElementById("locBar");
  const locSelect = document.getElementById("locSelect");
  const viewEl = document.getElementById("view");
  const tabbar = document.getElementById("tabbar");
  const btnPeriod = document.getElementById("btnPeriod");

  const PERIODS = [
    { id: "shift", label: "Смена" },
    { id: "week", label: "Неделя" },
    { id: "month", label: "3 нед." },
  ];
  const PRIMARY = ["home", "signals", "mentor", "report"];
  const MORE_ORDER = ["access", "ai_audit", "reviews", "engagement", "consulting"];

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
  let auditState = null;
  let lastAuditReport = null;
  let lastMentor = null;

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
  function authHeaders(json = true) {
    const initData = (tg && tg.initData) || "";
    if (!initData) throw new Error("no_telegram");
    const h = { Authorization: `tma ${initData}` };
    if (json) h["Content-Type"] = "application/json";
    return h;
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
    return profile && ["owner", "manager", "network", "happiness"].includes(profile.role);
  }
  function screenIds() {
    return ((profile && profile.screens) || []).map((s) => s.id);
  }
  function hasScreen(id) {
    return screenIds().includes(id);
  }

  function metric(k, v, h) {
    return `<div class="metric"><div class="k">${escapeHtml(k)}</div><div class="v">${v}</div>${h ? `<div class="h">${escapeHtml(h)}</div>` : ""}</div>`;
  }
  function deptPanel(d) {
    if (!d) return "";
    const avg = d.avg_rating != null ? String(d.avg_rating).replace(".", ",") : "—";
    const tops = (d.top_blockers || []).slice(0, 3).map((b) => `<li>${escapeHtml(b.label)} · ${b.count}</li>`).join("") || "<li>пока тихо</li>";
    return `<div class="panel"><h3>${escapeHtml(d.title)}</h3><div class="big">${avg}</div><div class="muted" style="font-size:0.78rem">${d.ratings_count} оценок</div><ul>${tops}</ul></div>`;
  }

  function renderTabbar() {
    if (!managerLike()) {
      tabbar.hidden = true;
      return;
    }
    tabbar.hidden = false;
    const primary = PRIMARY.filter((id) => hasScreen(id) || id === "home");
    const buttons = tabbar.querySelectorAll("button");
    buttons.forEach((b) => {
      const id = b.getAttribute("data-tab");
      if (id === "more") {
        b.classList.toggle("on", tab === "more" || MORE_ORDER.includes(tab));
        return;
      }
      b.hidden = !primary.includes(id) && id !== "home";
      b.classList.toggle("on", tab === id);
    });
  }

  function openMoreSheet() {
    const ids = MORE_ORDER.filter((id) => hasScreen(id));
    if (!ids.length) return;
    const labels = {
      access: ["Доступы", "Команда и QR"],
      ai_audit: ["ИИ-аудит", "Индекс здоровья"],
      reviews: ["Отзывы", "Зал и кухня"],
      engagement: ["Вовлечённость", "Кто отвечает"],
      consulting: ["Консалтинг", "Платформа Академии"],
    };
    const sheet = document.createElement("div");
    sheet.className = "sheet";
    sheet.innerHTML = `<div class="sheet-card"><div class="sheet-handle"></div>${ids
      .map((id) => {
        const [t, h] = labels[id] || [id, ""];
        const cls = id === "consulting" ? "sheet-item consult" : "sheet-item";
        return `<button type="button" class="${cls}" data-go="${id}"><span>${escapeHtml(t)}<div class="hint">${escapeHtml(h)}</div></span><span>›</span></button>`;
      })
      .join("")}<button type="button" class="sheet-item" data-go="close"><span>Закрыть</span></button></div>`;
    sheet.addEventListener("click", (e) => {
      if (e.target === sheet) sheet.remove();
    });
    sheet.querySelectorAll("[data-go]").forEach((b) => {
      b.addEventListener("click", () => {
        const go = b.getAttribute("data-go");
        sheet.remove();
        if (!go || go === "close") return;
        tab = go;
        renderTabbar();
        renderView();
      });
    });
    document.body.appendChild(sheet);
  }

  function renderHome() {
    if (!dash || dash.empty) {
      viewEl.innerHTML = `<div class="block"><h2>Нет точек</h2><p>${escapeHtml((dash && dash.message) || "Подключите чат и роль менеджера.")}</p></div>`;
      return;
    }
    const e = dash.engagement || {};
    const avg = e.avg_rating != null ? String(e.avg_rating).replace(".", ",") : "—";
    const pct = e.pct != null ? `${e.pct}%` : "—";
    const hot = (dash.hot_problems || []).slice(0, 3).map((p) => {
      const st = p.status === "new" ? "hot" : p.status === "in_progress" ? "warn" : "ok";
      return `<div class="item tap" data-pid="${escapeHtml(p.id)}"><div class="t">${escapeHtml(p.title)}</div><div class="m">${p.mentions || 0} отметок</div><span class="tag ${st}">${escapeHtml(p.status_ru || p.status)}</span></div>`;
    }).join("");
    viewEl.innerHTML = `
      <div class="metrics">
        ${metric("Средняя", avg, dash.period_label || "")}
        ${metric("Вовлечённость", pct, `${e.raters || 0} из ${e.baseline_30d || "—"}`)}
        ${metric("Оценок", String(e.ratings || 0), "за период")}
        ${metric("Горящие", String(dash.hot_count || 0), "активных")}
      </div>
      <div class="split">${deptPanel(dash.floor)}${deptPanel(dash.kitchen)}</div>
      <div class="block"><h2>Горящие сейчас</h2><p>Откройте вкладку «Горящие» или карточку — там статус и совет наставника.</p></div>
      <div class="list">${hot || `<div class="item"><div class="m">Тихо — активных тем нет</div></div>`}</div>
    `;
    viewEl.querySelectorAll("[data-pid]").forEach((el) => {
      el.addEventListener("click", () => {
        tab = "signals";
        renderTabbar();
        renderSignals(el.getAttribute("data-pid"));
      });
    });
  }

  function renderReviews() {
    if (!dash || dash.empty) { renderHome(); return; }
    const blocks = [dash.floor, dash.kitchen].map((d) => {
      const comments = (d.comments || []).map((c) => `<div class="item"><div class="m">${escapeHtml(c.text)}</div></div>`).join("")
        || `<div class="item"><div class="m">Комментариев пока нет</div></div>`;
      return `<div class="block"><h2>${escapeHtml(d.title)}</h2><p>Средняя ${d.avg_rating != null ? String(d.avg_rating).replace(".", ",") : "—"} · ${d.ratings_count} оценок</p></div><div class="list">${comments}</div>`;
    });
    viewEl.innerHTML = blocks.join("");
  }

  function renderEngagement() {
    if (!dash || dash.empty) { renderHome(); return; }
    const e = dash.engagement || {};
    viewEl.innerHTML = `
      <div class="metrics">
        ${metric("Ответили", String(e.raters || 0), "уникальных")}
        ${metric("База 30д", String(e.baseline_30d || 0), "писали раньше")}
        ${metric("Доля", e.pct != null ? e.pct + "%" : "—", "вовлечённость")}
        ${metric("Оценок", String(e.ratings || 0), dash.period_label || "")}
      </div>
      <div class="block"><h2>Как читать</h2><p>Низкая доля — напомните кнопку в группах зала и кухни.</p></div>
    `;
  }

  async function renderSignals(focusId) {
    viewEl.innerHTML = `<p class="muted center">Обновляем горящие…</p>`;
    try {
      const q = new URLSearchParams({ sync: "1", view: "active" });
      if (chatId) q.set("chat_id", chatId);
      const data = await apiGet(`/api/miniapp/problems?${q}`);
      const items = (data.problems || []).map((p) => {
        const st = p.status === "new" ? "hot" : p.status === "in_progress" ? "warn" : "ok";
        const open = focusId && focusId === p.id ? " data-open=1" : "";
        return `<div class="item tap" data-pid="${escapeHtml(p.id)}"${open}>
          <div class="t">${escapeHtml(p.title)}</div>
          <div class="m">${p.mentions || 0} отметок · ${escapeHtml(p.card_text || "").slice(0, 120)}</div>
          <span class="tag ${st}">${escapeHtml(p.status_ru || p.status)}</span>
        </div>`;
      }).join("");
      viewEl.innerHTML = `
        <div class="block">
          <h2>Горящие вопросы</h2>
          <p>${escapeHtml(data.title || "")}${data.sync_note ? " · " + escapeHtml(data.sync_note) : ""}</p>
        </div>
        <div class="list">${items || `<div class="item"><div class="m">Активных тем нет. Нажмите обновить после смены.</div></div>`}</div>
        <div class="actions"><button type="button" class="secondary" id="btnSyncHot">Обновить из отзывов</button></div>
      `;
      document.getElementById("btnSyncHot").addEventListener("click", () => renderSignals());
      viewEl.querySelectorAll("[data-pid]").forEach((el) => {
        el.addEventListener("click", () => showProblemCard(el.getAttribute("data-pid")));
      });
      const auto = viewEl.querySelector("[data-open]");
      if (auto) showProblemCard(auto.getAttribute("data-pid"));
    } catch (e) {
      viewEl.innerHTML = `<div class="error">${escapeHtml(e.message || "Ошибка")}</div>`;
    }
  }

  async function showProblemCard(pid) {
    viewEl.innerHTML = `<p class="muted center">Карточка…</p>`;
    try {
      const q = new URLSearchParams({ sync: "0", view: "active" });
      if (chatId) q.set("chat_id", chatId);
      const data = await apiGet(`/api/miniapp/problems?${q}`);
      const p = (data.problems || []).find((x) => x.id === pid);
      if (!p) {
        viewEl.innerHTML = `<div class="error">Тема не найдена</div>`;
        return;
      }
      viewEl.innerHTML = `
        <div class="block">
          <h2>${escapeHtml(p.title)}</h2>
          <p><span class="tag ${p.status === "new" ? "hot" : "warn"}">${escapeHtml(p.status_ru)}</span> · ${p.mentions || 0} отметок</p>
          <div class="m" style="margin-top:10px;white-space:pre-wrap;color:var(--muted);font-size:0.88rem">${escapeHtml(p.card_text || "")}</div>
        </div>
        <div class="actions">
          <button type="button" data-st="in_progress">В работу</button>
          <button type="button" class="secondary" data-st="resolved">Решена</button>
          <button type="button" class="secondary" data-st="ignored">Игнорировать</button>
          <button type="button" id="btnMentorP">✦ Совет наставника</button>
          <button type="button" class="secondary" id="btnBackHot">← К списку</button>
        </div>
      `;
      viewEl.querySelectorAll("[data-st]").forEach((b) => {
        b.addEventListener("click", async () => {
          try {
            await apiPost("/api/miniapp/problems/status", { problem_id: pid, status: b.getAttribute("data-st") });
            showProblemCard(pid);
          } catch (e) {
            alert(e.message || "Не удалось");
          }
        });
      });
      document.getElementById("btnMentorP").addEventListener("click", () => {
        tab = "mentor";
        renderTabbar();
        renderMentor(pid);
      });
      document.getElementById("btnBackHot").addEventListener("click", () => renderSignals());
    } catch (e) {
      viewEl.innerHTML = `<div class="error">${escapeHtml(e.message || "Ошибка")}</div>`;
    }
  }

  async function renderMentor(problemId) {
    viewEl.innerHTML = `<p class="muted center">Наставник разбирает…</p>`;
    try {
      let data;
      if (problemId) {
        data = await apiPost("/api/miniapp/mentor", { problem_id: problemId });
      } else {
        data = await apiPost("/api/miniapp/mentor", { chat_id: chatId || undefined, period });
      }
      lastMentor = data;
      const learn = data.learn
        ? `<div class="learn"><b>${escapeHtml(data.learn.title || "Подробнее")}</b><br>${escapeHtml(data.learn.blurb || "")}</div>`
        : "";
      viewEl.innerHTML = `
        <div class="mentor-card">
          <h2>Отчёт наставника</h2>
          <div class="muted" style="color:#a7f3d0;font-size:0.8rem;margin-bottom:8px">${escapeHtml(data.restaurant_title || "")} · ${escapeHtml(data.problem_title || "")}${data.template ? " · черновик" : ""}</div>
          <div class="body">${escapeHtml(data.text || "")}</div>
          ${learn}
        </div>
        <div class="actions">
          <button type="button" id="btnMentorAgain">Ещё раз по точке</button>
          <button type="button" class="secondary" id="btnToHot">К горящим</button>
        </div>
      `;
      document.getElementById("btnMentorAgain").addEventListener("click", () => renderMentor());
      document.getElementById("btnToHot").addEventListener("click", () => {
        tab = "signals";
        renderTabbar();
        renderSignals();
      });
    } catch (e) {
      viewEl.innerHTML = `
        <div class="block"><h2>Наставник</h2><p>${escapeHtml(e.message || "Нет совета")}</p></div>
        <div class="actions"><button type="button" id="btnMentorRetry">Повторить</button><button type="button" class="secondary" id="btnToHot2">К горящим</button></div>
      `;
      document.getElementById("btnMentorRetry").addEventListener("click", () => renderMentor(problemId));
      document.getElementById("btnToHot2").addEventListener("click", () => {
        tab = "signals";
        renderTabbar();
        renderSignals();
      });
    }
  }

  async function renderReport() {
    viewEl.innerHTML = `<p class="muted center">Собираем отчёт…</p>`;
    try {
      const q = new URLSearchParams({ period, department: "all" });
      if (chatId) q.set("chat_id", chatId);
      const data = await apiGet(`/api/miniapp/report?${q}`);
      const blocks = (data.blocks || []).map((b) => `<div class="report-block">${escapeHtml(b.text || "")}</div>`).join("");
      viewEl.innerHTML = `
        <div class="block">
          <h2>Отчёт · ${escapeHtml(data.title || "")}</h2>
          <p>Тот же текст, что уходит в бот и в PDF. Период: ${escapeHtml(PERIODS.find((p) => p.id === period)?.label || period)}.</p>
        </div>
        ${blocks || `<div class="item"><div class="m">Пока пусто за период</div></div>`}
      `;
    } catch (e) {
      viewEl.innerHTML = `<div class="error">${escapeHtml(e.message || "Ошибка отчёта")}</div>`;
    }
  }

  async function ensureAccessOpts() {
    if (accessOpts) return accessOpts;
    accessOpts = await apiGet("/api/miniapp/access/options");
    return accessOpts;
  }

  async function renderAccess() {
    if (!canAccessTab()) {
      viewEl.innerHTML = `<div class="block"><p>Доступы выдаёт управляющий точки или сети.</p></div>`;
      return;
    }
    viewEl.innerHTML = `<p class="muted center">Доступы…</p>`;
    try {
      const opts = await ensureAccessOpts();
      const orgsPack = await apiGet("/api/miniapp/orgs").catch(() => ({ orgs: [], can_create: false, unlinked_chats: [] }));
      if (!opts.can_manage) {
        viewEl.innerHTML = `<div class="block"><p>Нет прав выдавать доступы.</p></div>`;
        return;
      }
      const locs = opts.locations || [];
      // одна точка — из верхнего селектора приложения
      if (chatId) accessChatId = chatId;
      if (!accessChatId && locs[0]) accessChatId = locs[0].id;
      const loc = locs.find((l) => String(l.id) === String(accessChatId)) || locs[0];
      if (loc) {
        accessChatId = loc.id;
        accessOrgId = loc.org_id;
        if (!chatId) chatId = loc.id;
      }
      const roles = opts.roles || [];
      if (!accessRole && roles[0]) accessRole = roles[0];
      const placeTitle = (loc && loc.title) || accessChatId || "точка";

      let staffHtml = `<div class="item"><div class="m">Выберите точку сверху</div></div>`;
      if (accessChatId) {
        const staff = await apiGet(`/api/miniapp/access/staff?chat_id=${encodeURIComponent(accessChatId)}`);
        staffHtml = (staff.staff || []).map((s) => `
          <div class="item">
            <div class="t">${escapeHtml(s.role_label)}</div>
            <div class="m">ID ${s.user_id}${s.network ? " · сеть" : ""}</div>
            <div class="actions row" style="margin-top:8px">
              <button type="button" class="danger" data-rm="${s.user_id}" data-code="${escapeHtml(s.role_code)}" data-org="${escapeHtml(staff.org_id || accessOrgId)}" data-net="${s.network ? "1" : "0"}">Отозвать</button>
            </div>
          </div>
        `).join("") || `<div class="item"><div class="m">Пока никого нет на точке</div></div>`;
      }

      const orgList = (orgsPack.orgs || []).map((o) => {
        const chats = (o.chats || []).map((c) => escapeHtml(c.title)).join(", ") || "нет чатов";
        return `<div class="item"><div class="t">${escapeHtml(o.name)}</div><div class="m"><code>${escapeHtml(o.id)}</code> · ${chats}</div></div>`;
      }).join("") || `<div class="item"><div class="m">Организаций пока нет</div></div>`;

      const unlinked = (orgsPack.unlinked_chats || []).map((c) => `<option value="${escapeHtml(c.id)}">${escapeHtml(c.title)}</option>`).join("");
      const orgOpts = (orgsPack.orgs || []).map((o) => `<option value="${escapeHtml(o.id)}">${escapeHtml(o.name)}</option>`).join("");
      const roleBtns = roles.map((r) => `<button type="button" data-code="${escapeHtml(r.code)}" class="${accessRole && accessRole.code === r.code ? "on" : ""}">${escapeHtml(r.label)}</button>`).join("");
      const cmdHtml = (orgsPack.commands || []).map((c) => `
        <div class="item">
          <div class="t">${escapeHtml(c.name)}</div>
          <div class="m"><code>${escapeHtml(c.floor)}</code><br><code>${escapeHtml(c.kitchen)}</code></div>
        </div>`).join("");

      viewEl.innerHTML = `
        <div class="block">
          <h2>Команда · ${escapeHtml(placeTitle)}</h2>
          <p>Уже выданные доступы и новые через QR / инвайт. Точку меняйте селектором сверху.</p>
        </div>
        <div class="list">${staffHtml}</div>
        <div class="block">
          <h2>Выдать доступ</h2>
          <div class="role-grid" id="roleGrid">${roleBtns}</div>
          <div class="actions">
            <button type="button" id="btnScan">Сканировать QR</button>
            <button type="button" class="secondary" id="btnInvite">Инвайт-ссылка</button>
          </div>
          <input class="field" id="manualId" placeholder="@username или Telegram ID" />
          <div class="actions"><button type="button" class="secondary" id="btnManual">Выдать по @ или ID</button></div>
          <div id="inviteOut">${lastInviteLink ? `<div class="invite-box">${escapeHtml(lastInviteLink)}</div>` : ""}</div>
        </div>
        <div class="block"><h2>Организации</h2><p>Сети и уже привязанные чаты зала/кухни.</p></div>
        <div class="list">${orgList}</div>
        ${orgsPack.can_link ? `
        <div class="block">
          <h2>Подключить чат · зал / кухня</h2>
          <p>1) Добавьте бота в группу. 2) Выберите организацию, чат и тип. Кухня сама свяжется с залом сети, если он один.</p>
          ${orgsPack.add_bot_url ? `<div class="actions"><a class="btn secondary" href="${escapeHtml(orgsPack.add_bot_url)}" target="_blank" rel="noopener">Добавить бота в группу</a></div>` : ""}
          <select id="existOrg" class="field">${orgOpts || '<option value="">Нет организации</option>'}</select>
          <select id="existChat" class="field">${unlinked || '<option value="">Нет свободных чатов — сначала добавьте бота в группу</option>'}</select>
          <select id="existDept" class="field">
            <option value="floor">Зал</option>
            <option value="kitchen">Кухня</option>
          </select>
          <div class="actions"><button type="button" id="btnLinkChat">Привязать</button></div>
        </div>
        <div class="block"><h2>Команды в группу</h2><p>Можно вставить в чат вручную:</p></div>
        <div class="list">${cmdHtml || `<div class="item"><div class="m">Нет организаций в доступе</div></div>`}</div>
        ` : ""}
        ${orgsPack.can_create ? `
        <div class="block">
          <h2>Новая организация</h2>
          <p>Только владелец Pulse. Можно сразу привязать чат.</p>
          <input class="field" id="orgName" placeholder="Название сети / точки" />
          <select id="linkChat" class="field"><option value="">Без чата пока</option>${unlinked}</select>
          <select id="linkDept" class="field">
            <option value="floor">Зал</option>
            <option value="kitchen">Кухня</option>
          </select>
          <div class="actions"><button type="button" id="btnCreateOrg">Создать</button></div>
        </div>` : ""}
      `;

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
      viewEl.querySelectorAll("[data-rm]").forEach((b) => {
        b.addEventListener("click", async () => {
          if (!confirm("Отозвать доступ?")) return;
          try {
            await apiPost("/api/miniapp/access/remove", {
              user_id: Number(b.getAttribute("data-rm")),
              role_code: b.getAttribute("data-code"),
              org_id: b.getAttribute("data-org"),
              chat_id: b.getAttribute("data-net") === "1" ? null : accessChatId,
            });
            renderAccess();
          } catch (e) {
            alert(e.message || "Не удалось");
          }
        });
      });
      const createBtn = document.getElementById("btnCreateOrg");
      if (createBtn) {
        createBtn.addEventListener("click", async () => {
          try {
            const name = document.getElementById("orgName").value.trim();
            const chat_id = document.getElementById("linkChat").value || null;
            const department = document.getElementById("linkDept").value || null;
            const res = await apiPost("/api/miniapp/orgs/create", { name, chat_id, department });
            alert(`Организация ${res.org_id} создана${res.linked ? " и привязана" : ""}`);
            accessOpts = null;
            renderAccess();
          } catch (e) {
            alert(e.message || "Ошибка");
          }
        });
      }
      const linkBtn = document.getElementById("btnLinkChat");
      if (linkBtn) {
        linkBtn.addEventListener("click", async () => {
          try {
            const res = await apiPost("/api/miniapp/orgs/link", {
              org_id: document.getElementById("existOrg").value,
              chat_id: document.getElementById("existChat").value,
              department: document.getElementById("existDept").value,
            });
            alert(res.note || `Готово: ${res.title} · ${res.department === "kitchen" ? "кухня" : "зал"}`);
            accessOpts = null;
            renderAccess();
          } catch (e) {
            alert(e.message || "Ошибка");
          }
        });
      }
    } catch (e) {
      viewEl.innerHTML = `<div class="error">${escapeHtml(e.message || "Ошибка")}</div>`;
    }
  }

  function selectedAccessPayload() {
    if (!accessRole) throw new Error("Выберите роль");
    return { role_code: accessRole.code, chat_id: accessChatId, org_id: accessOrgId };
  }
  async function grantIdentity(text) {
    try {
      const res = await apiPost("/api/miniapp/access/grant", { ...selectedAccessPayload(), qr: text });
      if (tg && tg.showAlert) tg.showAlert(`Готово: ${res.role_label} · ${res.place}`);
      else alert(`Готово: ${res.role_label}`);
      renderAccess();
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
      const res = await apiPost("/api/miniapp/access/invite", selectedAccessPayload());
      lastInviteLink = res.link;
      renderAccess();
    } catch (e) {
      alert((e && e.message) || "Не удалось создать инвайт");
    }
  }

  async function loadAudit() {
    auditState = await apiGet("/api/miniapp/audit/orgs");
    return auditState;
  }
  function renderAudit() {
    viewEl.innerHTML = `<p class="muted center">ИИ-аудит…</p>`;
    loadAudit().then((st) => {
      const sess = st.session;
      const orgs = st.orgs || [];
      if (lastAuditReport) {
        const a = lastAuditReport;
        viewEl.innerHTML = `
          <div class="block"><h2>Индекс ${a.overall_index} · ${escapeHtml(a.index_label || "")}</h2><p>${escapeHtml(a.summary || "")}</p></div>
          <div class="actions">
            ${a.pdf_name ? `<button type="button" id="btnPdf">Скачать PDF</button>` : ""}
            <button type="button" class="secondary" id="btnAuditNew">Новый аудит</button>
          </div>`;
        const pdfBtn = document.getElementById("btnPdf");
        if (pdfBtn && a.pdf_name) pdfBtn.addEventListener("click", () => downloadAuditPdf(a.pdf_name));
        document.getElementById("btnAuditNew").addEventListener("click", () => { lastAuditReport = null; renderAudit(); });
        return;
      }
      if (sess) {
        viewEl.innerHTML = `
          <div class="block">
            <h2>${escapeHtml(sess.restaurant_title || "Аудит")}</h2>
            <p>Фрагментов: <b>${sess.chunk_count || 0}</b></p>
            <input type="file" id="auditFile" accept="audio/*,video/*,.ogg,.mp3,.m4a,.wav" class="field" />
            <textarea id="auditNote" class="field" rows="3" placeholder="Текстовая заметка"></textarea>
            <div class="actions">
              <button type="button" id="btnAuditUpload">Загрузить</button>
              <button type="button" class="secondary" id="btnAuditNote">Заметка</button>
              <button type="button" id="btnAuditFinish">Завершить</button>
              <button type="button" class="secondary" id="btnAuditCancel">Отменить</button>
            </div>
          </div>`;
        document.getElementById("btnAuditUpload").addEventListener("click", uploadAuditFile);
        document.getElementById("btnAuditNote").addEventListener("click", async () => {
          try {
            await apiPost("/api/miniapp/audit/note", { text: document.getElementById("auditNote").value });
            renderAudit();
          } catch (e) { alert(e.message || "Ошибка"); }
        });
        document.getElementById("btnAuditFinish").addEventListener("click", finishAudit);
        document.getElementById("btnAuditCancel").addEventListener("click", async () => {
          await apiPost("/api/miniapp/audit/cancel", {});
          renderAudit();
        });
        return;
      }
      if (!orgs.length) {
        viewEl.innerHTML = `<div class="block"><h2>ИИ-аудит</h2><p>Нет организаций — создайте во вкладке Доступы.</p></div>`;
        return;
      }
      const opts = orgs.map((o) => `<option value="${escapeHtml(o.id)}">${escapeHtml(o.title)}</option>`).join("");
      viewEl.innerHTML = `<div class="block"><h2>ИИ-аудит</h2><p>Голос или файлы → индекс 0–100 и PDF.</p><select id="auditOrg" class="field">${opts}</select><div class="actions"><button type="button" id="btnAuditStart">Начать</button></div></div>`;
      document.getElementById("btnAuditStart").addEventListener("click", async () => {
        try {
          await apiPost("/api/miniapp/audit/start", { org_id: document.getElementById("auditOrg").value });
          renderAudit();
        } catch (e) { alert(e.message || "Не удалось"); }
      });
    }).catch((e) => {
      viewEl.innerHTML = `<div class="error">${escapeHtml(e.message || "Нет доступа")}</div>`;
    });
  }
  async function uploadAuditFile() {
    const input = document.getElementById("auditFile");
    const file = input && input.files && input.files[0];
    if (!file) { alert("Выберите файл"); return; }
    const fd = new FormData();
    fd.append("file", file, file.name);
    viewEl.innerHTML = `<p class="muted center">Загружаем…</p>`;
    try {
      const res = await fetch(`${apiBase()}/api/miniapp/audit/chunk`, {
        method: "POST",
        headers: { Authorization: `tma ${(tg && tg.initData) || ""}` },
        body: fd,
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok || data.ok === false) throw new Error(data.error || "upload_fail");
      renderAudit();
    } catch (e) {
      alert(e.message || "Ошибка");
      renderAudit();
    }
  }
  async function finishAudit() {
    viewEl.innerHTML = `<p class="muted center">Анализируем…</p>`;
    try {
      const data = await apiPost("/api/miniapp/audit/finish", {});
      lastAuditReport = data.report;
      renderAudit();
    } catch (e) {
      alert(e.message || "Не удалось");
      renderAudit();
    }
  }
  async function downloadAuditPdf(name) {
    const res = await fetch(`${apiBase()}/api/miniapp/audit/pdf/${encodeURIComponent(name)}`, {
      headers: { Authorization: `tma ${(tg && tg.initData) || ""}` },
    });
    if (!res.ok) { alert("PDF недоступен"); return; }
    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    if (tg && tg.openLink) tg.openLink(url);
    else window.open(url, "_blank");
  }

  async function openConsulting() {
    viewEl.innerHTML = `<p class="muted center">Открываем платформу…</p>`;
    try {
      const data = await apiGet("/api/miniapp/consulting/token");
      location.href = data.url || `/consulting/?pulse_token=${data.token}`;
    } catch (e) {
      viewEl.innerHTML = `<div class="error">Консалтинг только для владельца Pulse.<br>${escapeHtml(e.message || "")}</div>`;
    }
  }

  function renderStaffHome() {
    viewEl.innerHTML = `<div class="block"><h2>Привет</h2><p>Отзыв о смене — кнопка из рабочей группы в боте.</p></div>`;
  }

  function renderView() {
    viewEl.scrollTop = 0;
    if (!managerLike()) {
      renderStaffHome();
      return;
    }
    if (tab === "more") {
      openMoreSheet();
      tab = PRIMARY.includes(tab) ? tab : "home";
      renderTabbar();
      return;
    }
    if (tab === "reviews") renderReviews();
    else if (tab === "engagement") renderEngagement();
    else if (tab === "signals") renderSignals();
    else if (tab === "mentor") renderMentor();
    else if (tab === "report") renderReport();
    else if (tab === "ai_audit") renderAudit();
    else if (tab === "consulting") openConsulting();
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
    if (["home", "reviews", "engagement"].includes(tab)) {
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
      if (tg && tg.showAlert) tg.showAlert(`Доступ выдан: ${res.role_label}`);
      profile = await apiGet("/api/miniapp/me");
      renderTabbar();
    } catch (e) {
      if (tg && tg.showAlert) tg.showAlert(e.message || "Инвайт не сработал");
    }
  }

  function showError(kind) {
    let detail = "Не удалось войти в приложение.";
    if (kind === "no_telegram") detail = "Откройте Mini App из меню бота в Telegram.";
    else if (kind === "unauthorized") detail = "Подпись Telegram не принята. Проверьте BOT_TOKEN на Railway.";
    else if (kind === "forbidden") detail = "Этот раздел для управляющих.";
    viewEl.innerHTML = `<div class="error">${detail}</div>`;
  }

  tabbar.querySelectorAll("button").forEach((b) => {
    b.addEventListener("click", () => {
      const id = b.getAttribute("data-tab");
      if (id === "more") {
        openMoreSheet();
        return;
      }
      tab = id;
      renderTabbar();
      renderView();
    });
  });

  locSelect.addEventListener("change", () => {
    chatId = locSelect.value;
    loadDashboard().catch((e) => showError(e.message));
  });
  btnPeriod.addEventListener("click", () => {
    const i = PERIODS.findIndex((p) => p.id === period);
    period = PERIODS[(i + 1) % PERIODS.length].id;
    loadDashboard().catch((e) => showError(e.message));
  });

  apiGet("/api/miniapp/me")
    .then(async (data) => {
      profile = data;
      const name = (data.user && data.user.first_name) || "";
      roleLine.textContent = `${data.role_label}${name ? " · " + name : ""}`;
      if (data.locations && data.locations[0]) chatId = data.locations[0].id;
      const start =
        data.start_param ||
        (tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param) ||
        "";
      if (start) await redeemStartParam(start);
      renderTabbar();
      return loadDashboard();
    })
    .catch((e) => showError((e && e.message) || "network"));
})();
