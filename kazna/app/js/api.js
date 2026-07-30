/** API helper for Казна backend */
(function (global) {
  async function req(path, options) {
    const opts = options || {};
    const headers = Object.assign({}, opts.headers || {});
    if (opts.json !== undefined) {
      headers["Content-Type"] = "application/json";
      opts.body = JSON.stringify(opts.json);
      delete opts.json;
    }
    const res = await fetch(path, Object.assign({ credentials: "same-origin" }, opts, { headers: headers }));
    if (res.status === 401) {
      if (!location.pathname.endsWith("index.html") && location.pathname !== "/") {
        location.href = "/";
      }
      throw new Error("Unauthorized");
    }
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok) {
      var detail = data.detail;
      if (Array.isArray(detail)) {
        detail = detail.map(function (d) { return d.msg || JSON.stringify(d); }).join("; ");
      }
      throw new Error(detail || data.message || "Ошибка запроса");
    }
    return data;
  }

  async function upload(path, file, replace) {
    const fd = new FormData();
    fd.append("file", file);
    const q = replace === false ? "?replace=false" : "?replace=true";
    const res = await fetch(path + q, { method: "POST", body: fd, credentials: "same-origin" });
    const data = await res.json().catch(function () { return {}; });
    if (!res.ok) throw new Error(data.detail || "Не удалось импортировать");
    return data;
  }

  global.KaznaAPI = {
    login: function (email, password) {
      return req("/api/login", { method: "POST", json: { email: email, password: password } });
    },
    logout: function () {
      return req("/api/logout", { method: "POST", json: {} });
    },
    me: function () {
      return req("/api/me");
    },
    overview: function () {
      return req("/api/overview");
    },
    payments: function () {
      return req("/api/payments");
    },
    people: function () {
      return req("/api/people");
    },
    requests: function () {
      return req("/api/requests");
    },
    createRequest: function (payload) {
      return req("/api/requests", { method: "POST", json: payload });
    },
    patchRequest: function (id, payload) {
      return req("/api/requests/" + id, { method: "PATCH", json: payload });
    },
    importExcel: function (file, replace) {
      return upload("/api/import/excel", file, replace);
    },
    importIiko: function (file, replace) {
      return upload("/api/import/iiko", file, replace);
    },
    inspectImport: async function (file) {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch("/api/import/inspect", { method: "POST", body: fd, credentials: "same-origin" });
      const data = await res.json().catch(function () { return {}; });
      if (!res.ok) throw new Error(data.detail || "Не удалось разобрать заголовки");
      return data;
    },
    saveIikoConfig: function (payload) {
      return req("/api/import/iiko/config", { method: "POST", json: payload });
    },
  };
})(window);
