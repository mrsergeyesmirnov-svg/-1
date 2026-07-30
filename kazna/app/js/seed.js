/** Demo numbers for fin director home. Later: API / iiko. */
window.KaznaSeed = {
  accountsTotal: 12_400_000,
  payToday: 3_100_000,
  afterPay: 9_300_000,
  freeTodayLabel: "9,3 млн свободно после сегодняшних оплат",
  accounts: [
    { name: "Организация «Невский»", kind: "р/с", balance: 2_400_000, due: 420_000, ok: true },
    { name: "Организация «Север»", kind: "2 счёта", balance: 4_100_000, due: 1_800_000, ok: true },
    { name: "Организация «Васильевский»", kind: "р/с", balance: 1_900_000, due: 880_000, ok: false },
  ],
  todayPays: [
    { title: "Поставщик продуктов", meta: "накладная · отсрочка", amount: 640_000, status: "plan" },
    { title: "Аренда", meta: "заявка управляющего", amount: 560_000, status: "plan" },
    { title: "Расходники", meta: "накладная", amount: 380_000, status: "ok" },
  ],
  requests: [
    { title: "Замена бойлера", meta: "Невский · высокий→средний", amount: 186_000, status: "new" },
    { title: "Доплата поставщику", meta: "из накладной iiko", amount: 92_000, status: "plan" },
  ],
  fmt(n) {
    if (n >= 1_000_000) return (n / 1_000_000).toLocaleString("ru-RU", { maximumFractionDigits: 1 }) + " млн";
    if (n >= 1_000) return Math.round(n / 1_000).toLocaleString("ru-RU") + " тыс.";
    return n.toLocaleString("ru-RU") + " ₽";
  },
};
