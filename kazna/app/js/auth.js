/** Legacy stub — real auth is server session via /js/api.js. */
(function (global) {
  global.KaznaAuth = {
    login: function () {
      return { ok: false, error: "Используйте форму входа (серверная сессия)" };
    },
    logout: function () {
      location.href = "/";
    },
    current: function () {
      return null;
    },
    requireRole: function () {
      location.href = "/";
      return null;
    },
    homeFor: function (role) {
      if (role === "manager") return "/manager.html";
      if (role === "fin_director" || role === "accountant") return "/home.html";
      return "/";
    },
  };
})(window);
