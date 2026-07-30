/** Demo auth for Казна shell. Replace with real backend later. */
(function (global) {
  const KEY = "kazna_session_v1";

  const USERS = [
    {
      id: "u_fin",
      email: "fin@kazna.local",
      password: "kazna2026",
      name: "Анна Финдир",
      role: "fin_director",
      roleLabel: "Финансовый директор",
    },
    {
      id: "u_mgr",
      email: "manager@kazna.local",
      password: "kazna2026",
      name: "Игорь Управляющий",
      role: "manager",
      roleLabel: "Управляющий · Невский",
      site: "Невский",
    },
  ];

  function save(session) {
    localStorage.setItem(KEY, JSON.stringify(session));
  }

  function clear() {
    localStorage.removeItem(KEY);
  }

  function current() {
    try {
      const raw = localStorage.getItem(KEY);
      return raw ? JSON.parse(raw) : null;
    } catch {
      return null;
    }
  }

  function login(email, password) {
    const user = USERS.find(
      (u) => u.email.toLowerCase() === String(email).trim().toLowerCase() && u.password === password
    );
    if (!user) return { ok: false, error: "Неверный логин или пароль" };
    const session = {
      id: user.id,
      email: user.email,
      name: user.name,
      role: user.role,
      roleLabel: user.roleLabel,
      site: user.site || null,
      at: Date.now(),
    };
    save(session);
    return { ok: true, session };
  }

  function requireRole(roles, redirectTo) {
    const s = current();
    if (!s || (roles && roles.length && !roles.includes(s.role))) {
      location.href = redirectTo || "index.html";
      return null;
    }
    return s;
  }

  function logout() {
    clear();
    location.href = "index.html";
  }

  function homeFor(role) {
    if (role === "fin_director") return "home.html";
    if (role === "manager") return "manager.html";
    return "index.html";
  }

  global.KaznaAuth = { login, logout, current, requireRole, homeFor, USERS };
})(window);
