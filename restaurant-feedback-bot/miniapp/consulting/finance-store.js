/*! Finance store — Академия счастья platform */
(function (global) {
  var KEY = "ss_fin_v1";

  var INCOME_CATS = [
    { id: "project", label: "Проект / консалтинг" },
    { id: "sub", label: "Подписка мониторинга" },
    { id: "tour", label: "Тур / мастер-класс" },
    { id: "other", label: "Прочее" }
  ];

  var EXPENSE_CATS = [
    { id: "contractor", label: "Подрядчики" },
    { id: "ads", label: "Реклама" },
    { id: "travel", label: "Командировки" },
    { id: "venue", label: "Площадка / тур" },
    { id: "tools", label: "Сервисы / инфраструктура" },
    { id: "tax", label: "Налоги и взносы" },
    { id: "bank", label: "Банк / эквайринг" },
    { id: "other", label: "Прочее" }
  ];

  function uid(prefix) {
    return (prefix || "f") + Date.now().toString(36) + Math.random().toString(36).slice(2, 7);
  }

  function empty() {
    return {
      incomes: [],
      expenses: [],
      estimates: [],
      events: [],
      comments: [],
      docs: [],
      openingBalance: 0,
      updatedAt: Date.now()
    };
  }

  function load() {
    try {
      var raw = localStorage.getItem(KEY);
      if (!raw) return empty();
      var data = JSON.parse(raw);
      if (!data || typeof data !== "object") return empty();
      data.incomes = Array.isArray(data.incomes) ? data.incomes : [];
      data.expenses = Array.isArray(data.expenses) ? data.expenses : [];
      data.estimates = Array.isArray(data.estimates) ? data.estimates : [];
      data.events = Array.isArray(data.events) ? data.events : [];
      data.comments = Array.isArray(data.comments) ? data.comments : [];
      data.docs = Array.isArray(data.docs) ? data.docs : [];
      data.openingBalance = Number(data.openingBalance || 0);
      return data;
    } catch (e) {
      return empty();
    }
  }

  function save(data) {
    data.updatedAt = Date.now();
    localStorage.setItem(KEY, JSON.stringify(data));
  }

  function catLabel(list, id) {
    for (var i = 0; i < list.length; i++) if (list[i].id === id) return list[i].label;
    return id || "—";
  }

  function monthKey(iso) {
    if (!iso) return "";
    return String(iso).slice(0, 7);
  }

  function inMonth(iso, ym) {
    if (!ym || ym === "all") return true;
    return monthKey(iso) === ym;
  }

  function addIncome(item) {
    var data = load();
    var row = {
      id: uid("in"),
      date: item.date || new Date().toISOString().slice(0, 10),
      amount: Number(item.amount || 0),
      category: item.category || "project",
      client: item.client || "",
      title: item.title || "",
      note: item.note || "",
      docId: item.docId || "",
      createdAt: Date.now()
    };
    data.incomes.unshift(row);
    save(data);
    return row;
  }

  function addExpense(item) {
    var data = load();
    var row = {
      id: uid("ex"),
      date: item.date || new Date().toISOString().slice(0, 10),
      amount: Number(item.amount || 0),
      category: item.category || "other",
      title: item.title || "",
      note: item.note || "",
      estimateId: item.estimateId || "",
      docId: item.docId || "",
      createdAt: Date.now()
    };
    data.expenses.unshift(row);
    save(data);
    return row;
  }

  function removeItem(kind, id) {
    var data = load();
    var key = kind === "income" ? "incomes"
      : kind === "expense" ? "expenses"
      : kind === "estimate" ? "estimates"
      : kind === "event" ? "events"
      : kind === "comment" ? "comments"
      : kind === "doc" ? "docs" : null;
    if (!key) return;
    data[key] = data[key].filter(function (x) { return x.id !== id; });
    save(data);
  }

  function addEstimate(item) {
    var data = load();
    var row = {
      id: uid("es"),
      title: item.title || "Смета",
      period: item.period || new Date().toISOString().slice(0, 7),
      budgetIncome: Number(item.budgetIncome || 0),
      budgetExpense: Number(item.budgetExpense || 0),
      lines: Array.isArray(item.lines) ? item.lines : [],
      note: item.note || "",
      createdAt: Date.now()
    };
    data.estimates.unshift(row);
    save(data);
    return row;
  }

  function addEvent(item) {
    var data = load();
    var row = {
      id: uid("ev"),
      date: item.date || new Date().toISOString().slice(0, 10),
      title: item.title || "",
      kind: item.kind || "plan",
      amount: Number(item.amount || 0),
      note: item.note || "",
      createdAt: Date.now()
    };
    data.events.unshift(row);
    save(data);
    return row;
  }

  function addComment(text) {
    var data = load();
    var row = { id: uid("cm"), text: text, at: Date.now() };
    data.comments.unshift(row);
    save(data);
    return row;
  }

  function addDoc(item) {
    var data = load();
    var row = {
      id: uid("dc"),
      name: item.name || "document.pdf",
      kind: item.kind || "invoice",
      date: item.date || new Date().toISOString().slice(0, 10),
      client: item.client || "",
      amount: Number(item.amount || 0),
      mime: item.mime || "application/pdf",
      dataUrl: item.dataUrl || "",
      size: Number(item.size || 0),
      note: item.note || "",
      createdAt: Date.now()
    };
    data.docs.unshift(row);
    save(data);
    return row;
  }

  function setOpeningBalance(n) {
    var data = load();
    data.openingBalance = Number(n || 0);
    save(data);
  }

  function importFromCrm() {
    if (!global.SSCrm || !SSCrm.listClients) return { added: 0 };
    var data = load();
    var known = {};
    data.incomes.forEach(function (i) {
      if (i.docId) known[i.docId] = true;
    });
    data.docs.forEach(function (d) {
      if (d.crmDocId) known[d.crmDocId] = true;
    });
    var added = 0;
    SSCrm.listClients().forEach(function (c) {
      (c.docs || []).forEach(function (d) {
        var key = "crm:" + d.id;
        if (known[key]) return;
        if (d.kind === "invoice" || d.kind === "act") {
          addIncome({
            date: (d.date || "").split(".").reverse().join("-") || new Date().toISOString().slice(0, 10),
            amount: Number(d.sum || 0),
            category: "project",
            client: c.name || "",
            title: (d.kind === "invoice" ? "Счёт " : "Акт ") + (d.number || ""),
            note: "Из CRM",
            docId: key
          });
          if (d.html) {
            var blob = "data:text/html;charset=utf-8," + encodeURIComponent(
              "<!DOCTYPE html><html><head><meta charset='utf-8'><title>" +
              (d.number || "doc") + "</title></head><body>" + d.html + "</body></html>"
            );
            var docRow = addDoc({
              name: (d.kind === "invoice" ? "schet-" : "akt-") + (d.number || d.id) + ".html",
              kind: d.kind,
              date: new Date().toISOString().slice(0, 10),
              client: c.name || "",
              amount: Number(d.sum || 0),
              mime: "text/html",
              dataUrl: blob,
              size: blob.length,
              note: "Импорт из CRM " + (d.number || "")
            });
            // mark crm id on last doc
            var fresh = load();
            for (var i = 0; i < fresh.docs.length; i++) {
              if (fresh.docs[i].id === docRow.id) {
                fresh.docs[i].crmDocId = key;
                break;
              }
            }
            save(fresh);
          }
          added++;
        }
      });
    });
    return { added: added };
  }

  function summary(ym) {
    var data = load();
    var income = 0;
    var expense = 0;
    var tax = 0;
    data.incomes.forEach(function (i) {
      if (!inMonth(i.date, ym)) return;
      income += Number(i.amount || 0);
    });
    data.expenses.forEach(function (e) {
      if (!inMonth(e.date, ym)) return;
      var a = Number(e.amount || 0);
      expense += a;
      if (e.category === "tax") tax += a;
    });
    var profit = income - expense;
    var allIncome = 0;
    var allExpense = 0;
    data.incomes.forEach(function (i) { allIncome += Number(i.amount || 0); });
    data.expenses.forEach(function (e) { allExpense += Number(e.amount || 0); });
    var balance = Number(data.openingBalance || 0) + allIncome - allExpense;

    var budgetIncome = 0;
    var budgetExpense = 0;
    data.estimates.forEach(function (es) {
      if (ym && ym !== "all" && es.period !== ym) return;
      budgetIncome += Number(es.budgetIncome || 0);
      budgetExpense += Number(es.budgetExpense || 0);
    });

    return {
      income: income,
      expense: expense,
      tax: tax,
      expenseNoTax: expense - tax,
      profit: profit,
      balance: balance,
      budgetIncome: budgetIncome,
      budgetExpense: budgetExpense,
      budgetProfit: budgetIncome - budgetExpense,
      openingBalance: Number(data.openingBalance || 0)
    };
  }

  global.SSFin = {
    KEY: KEY,
    INCOME_CATS: INCOME_CATS,
    EXPENSE_CATS: EXPENSE_CATS,
    load: load,
    save: save,
    catLabel: catLabel,
    addIncome: addIncome,
    addExpense: addExpense,
    addEstimate: addEstimate,
    addEvent: addEvent,
    addComment: addComment,
    addDoc: addDoc,
    removeItem: removeItem,
    setOpeningBalance: setOpeningBalance,
    importFromCrm: importFromCrm,
    summary: summary,
    monthKey: monthKey
  };
})(window);
