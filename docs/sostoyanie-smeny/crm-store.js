/*! CRM store — localStorage for Состояние смены auditor */
(function (global) {
  var KEY = "ss_crm_v1";

  var STATUSES = [
    { id: "lead", label: "Касание" },
    { id: "brief", label: "Квалификация" },
    { id: "sale", label: "Продажа" },
    { id: "diagnose", label: "Диагноз / baseline" },
    { id: "change", label: "Изменение 30/60/90" },
    { id: "control", label: "Контроль" },
    { id: "sub", label: "Подписка" },
    { id: "pause", label: "Пауза" },
    { id: "lost", label: "Отказ" }
  ];

  function uid() {
    return "c" + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  }

  function load() {
    try {
      var raw = localStorage.getItem(KEY);
      if (!raw) return { clients: [] };
      var data = JSON.parse(raw);
      if (!data || !Array.isArray(data.clients)) return { clients: [] };
      return data;
    } catch (e) {
      return { clients: [] };
    }
  }

  function save(data) {
    localStorage.setItem(KEY, JSON.stringify(data));
  }

  function statusLabel(id) {
    for (var i = 0; i < STATUSES.length; i++) {
      if (STATUSES[i].id === id) return STATUSES[i].label;
    }
    return id || "—";
  }

  function listClients() {
    return load().clients.slice().sort(function (a, b) {
      return (b.updatedAt || 0) - (a.updatedAt || 0);
    });
  }

  function getClient(id) {
    var clients = load().clients;
    for (var i = 0; i < clients.length; i++) {
      if (clients[i].id === id) return clients[i];
    }
    return null;
  }

  function findByName(name) {
    var n = (name || "").trim().toLowerCase();
    if (!n) return null;
    var clients = load().clients;
    for (var i = 0; i < clients.length; i++) {
      if ((clients[i].name || "").trim().toLowerCase() === n) return clients[i];
    }
    return null;
  }

  function upsertClient(patch) {
    var data = load();
    var now = Date.now();
    var c = null;
    if (patch.id) {
      for (var i = 0; i < data.clients.length; i++) {
        if (data.clients[i].id === patch.id) {
          c = data.clients[i];
          break;
        }
      }
    }
    if (!c) {
      c = {
        id: uid(),
        name: "",
        city: "",
        contact: "",
        telegram: "",
        status: "lead",
        notes: "",
        comments: [],
        docs: [],
        createdAt: now,
        updatedAt: now
      };
      data.clients.push(c);
    }
    Object.keys(patch).forEach(function (k) {
      if (k === "id" || k === "comments" || k === "docs") return;
      if (patch[k] !== undefined) c[k] = patch[k];
    });
    c.updatedAt = now;
    save(data);
    return c;
  }

  function addComment(clientId, text) {
    var data = load();
    for (var i = 0; i < data.clients.length; i++) {
      if (data.clients[i].id === clientId) {
        data.clients[i].comments = data.clients[i].comments || [];
        data.clients[i].comments.unshift({
          id: uid(),
          text: text,
          at: Date.now()
        });
        data.clients[i].updatedAt = Date.now();
        save(data);
        return data.clients[i];
      }
    }
    return null;
  }

  function attachDoc(clientId, doc) {
    var data = load();
    var c = null;
    for (var i = 0; i < data.clients.length; i++) {
      if (data.clients[i].id === clientId) {
        c = data.clients[i];
        break;
      }
    }
    if (!c && doc && doc.clientName) {
      c = findByName(doc.clientName);
      if (c) {
        /* found by name in memory from find — need ref from data */
        for (i = 0; i < data.clients.length; i++) {
          if (data.clients[i].id === c.id) {
            c = data.clients[i];
            break;
          }
        }
      }
    }
    if (!c && doc && doc.clientName) {
      c = {
        id: uid(),
        name: doc.clientName,
        city: doc.city || "",
        contact: doc.contact || "",
        telegram: "",
        status: "sale",
        notes: "",
        comments: [],
        docs: [],
        createdAt: Date.now(),
        updatedAt: Date.now()
      };
      data.clients.push(c);
    }
    if (!c) return null;
    c.docs = c.docs || [];
    c.docs.unshift({
      id: uid(),
      kind: doc.kind,
      number: doc.number,
      date: doc.date,
      sum: doc.sum,
      title: doc.title,
      html: doc.html || "",
      snapshot: doc.snapshot || null,
      at: Date.now()
    });
    c.updatedAt = Date.now();
    if (doc.city && !c.city) c.city = doc.city;
    if (doc.contact && !c.contact) c.contact = doc.contact;
    save(data);
    return c;
  }

  function removeClient(id) {
    var data = load();
    data.clients = data.clients.filter(function (c) { return c.id !== id; });
    save(data);
  }

  function setActiveClient(id) {
    if (id) localStorage.setItem("ss_crm_active", id);
    else localStorage.removeItem("ss_crm_active");
  }

  function getActiveClientId() {
    return localStorage.getItem("ss_crm_active") || "";
  }

  global.SSCrm = {
    KEY: KEY,
    STATUSES: STATUSES,
    statusLabel: statusLabel,
    listClients: listClients,
    getClient: getClient,
    findByName: findByName,
    upsertClient: upsertClient,
    addComment: addComment,
    attachDoc: attachDoc,
    removeClient: removeClient,
    setActiveClient: setActiveClient,
    getActiveClientId: getActiveClientId
  };
})(window);
