/** Shared payment row renderer for Казна lists */
(function (global) {
  var statusMap = { new: "новая", plan: "в плане", ok: "ок", done: "оплачено" };

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function payMeta(p) {
    var bits = [];
    bits.push(p.counterparty ? ("кому: " + p.counterparty) : "кому: не указано");
    if (p.purpose && p.purpose !== p.counterparty) bits.push("за что: " + p.purpose);
    bits.push(p.account ? ("юрлицо/р/с: " + p.account) : "юрлицо/р/с: не указано");
    bits.push(p.dateLabel || "без даты");
    if (p.note) bits.push("коммент: " + p.note);
    bits.push(p.source || "");
    return bits.join(" · ");
  }

  function payRow(p) {
    var head = p.counterparty || p.title || "Платёж";
    var sub = payMeta(p);
    return (
      '<div class="row pay">' +
      "<div><strong>" + esc(head) + "</strong>" +
      (p.purpose && p.purpose !== head ? '<div class="pay-purpose">' + esc(p.purpose) + "</div>" : "") +
      '<div class="muted">' + esc(sub) + "</div></div>" +
      '<div class="amount">' + esc(p.amountLabel) + "</div>" +
      '<div class="muted">' + esc(p.dateLabel || "без даты") + "</div>" +
      '<div><span class="status ' + esc(p.status) + '">' +
      esc(statusMap[p.status] || p.status) + "</span></div>" +
      "<div></div></div>"
    );
  }

  global.KaznaPayRow = { render: payRow, meta: payMeta };
})(window);
