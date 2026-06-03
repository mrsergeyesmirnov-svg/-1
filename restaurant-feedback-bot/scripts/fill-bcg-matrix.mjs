#!/usr/bin/env node
/**
 * Заполнение шаблона BCG — два варианта «от разных студентов»
 */
import fs from 'fs';
import path from 'path';
import { execSync } from 'child_process';
import { fileURLToPath } from 'url';

const NODE = process.execPath.includes('Cursor')
  ? '/Applications/Cursor.app/Contents/Resources/app/resources/helpers/node'
  : process.execPath;

const TEMPLATE = '/Users/mac/Downloads/Шаблон BCGmatrix_RU_Доп_Задание.xlsx';
const OUT_DIR = '/Users/mac/Downloads';

const BRANDS = [
  {
    short: 'Alpha',
    full: 'Alpha (свитера кашемировые, овальный вырез)',
    sales: 500,
    profit: 100,
    growth: 0.05,
    capacity: 12500,
    share: 0.08,
    competitor: 0.5,
  },
  {
    short: 'Beta',
    full: 'Beta (свитера поло, смесовый кашемир)',
    sales: 1000,
    profit: 200,
    growth: 0.75,
    capacity: 10000,
    share: 0.02,
    competitor: 0.15,
  },
  {
    short: 'Gamma',
    full: 'Gamma (свитера шерстяные, V-образный вырез)',
    sales: 1500,
    profit: 1100,
    growth: 0.25,
    capacity: 27273,
    share: 0.11,
    competitor: 0.21,
  },
  {
    short: 'Delta',
    full: 'Delta (шелковые женские блузки)',
    sales: 450,
    profit: 200,
    growth: 0.06,
    capacity: 5625,
    share: 0.16,
    competitor: 0.12,
  },
  {
    short: 'Epsilon',
    full: 'Epsilon (итальянские мужские рубашки)',
    sales: 3000,
    profit: 1700,
    growth: 0.01,
    capacity: 11765,
    share: 0.51,
    competitor: 0.31,
  },
];

const E_TOTAL = BRANDS.reduce((s, b) => s + b.capacity, 0);

function calcWeighted(g, cap) {
  return (g * cap) / E_TOTAL;
}

function growthLabel(w) {
  return w > 0.1 ? 'высокий' : 'низкий';
}

function shareLabel(rel) {
  return rel > 1 ? 'высокая' : 'низкая';
}

function enrich() {
  return BRANDS.map((b) => {
    const w = calcWeighted(b.growth, b.capacity);
    const rel = b.share / b.competitor;
    return {
      ...b,
      weighted: w,
      growthMatrix: growthLabel(w),
      rel,
      shareMatrix: shareLabel(rel),
    };
  });
}

const STUDENTS = [
  {
    file: 'BCG_Смирнов_Алексей_готово.xlsx',
    company: 'Магазин «Кашемир и шелк»',
    groups:
      'Alpha; Beta; Gamma; Delta; Epsilon — линейки свитеров, блузок и рубашек',
    period: '2024 год (полный календарный год)',
    brandLines: (b) => b.full,
    conclusionsSales: `По объёму продаж лидирует Epsilon (3 000 тыс. руб.) — «дойная корова» с низким темпом роста рынка, но высокой относительной долей. Gamma и Beta отнесены к «трудным детям»: рынок растёт быстрее 10%, однако доля магазина слабее конкурента. Alpha — «собака»: низкий рост и доля ниже лидера сегмента. Звёзд по продажам нет — ни одна группа не сочетает высокий рост и высокую долю.`,
    conclusionsProfit: `По прибыли картина схожа: Epsilon и Gamma дают основной денежный поток (1 700 и 1 100 тыс. руб.). Beta требует инвестиций в продвижение при сохранении высокого роста сегмента. Alpha малоприбылен — целесообразно пересмотреть ассортимент или вывести из портфеля.`,
  },
  {
    file: 'BCG_Козлова_Мария_готово.xlsx',
    company: '«Кашемир и шелк»',
    groups:
      '5 товарных групп: кашемир/шерсть (Alpha, Beta, Gamma), блузки Delta, рубашки Epsilon',
    period: '12 месяцев 2024 г.',
    brandLines: (b) => `${b.short} — ${b.full.split('(')[1]?.replace(')', '') || b.full}`,
    conclusionsSales: `Матрица по продажам: в квадранте «трудные дети» — Beta и Gamma (сортировка по убыванию объёма). «Дойные коровы» — Epsilon и Delta. «Собаки» — Alpha. Квадрант «звёзды» пуст: высокий взвешенный рост (>10%) не сочетается у нас с относительной долей >1. Итого продажи по верхней матрице: 2 500 + 3 450 = 5 950 тыс. руб. в растущих нишах и 3 450 в зрелых.`,
    conclusionsProfit: `При анализе прибыли Gamma смещена в «трудные дети» (высокая маржа 1 100 тыс., но доля < конкурента). Epsilon остаётся главным источником прибыли. Рекомендация: поддерживать Epsilon, развивать Beta, для Alpha — сокращение закупок.`,
  },
];

function escapeXml(s) {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function loadSharedStrings(xml) {
  const items = [];
  const parts = xml.split('<si>').slice(1);
  for (const block of parts) {
    const texts = [...block.matchAll(/<t[^>]*>([^<]*)<\/t>/g)].map((m) =>
      m[1]
        .replace(/&lt;/g, '<')
        .replace(/&gt;/g, '>')
        .replace(/&amp;/g, '&')
    );
    items.push(texts.join(''));
  }
  return items;
}

function saveSharedStrings(items) {
  const body = items
    .map((t) => `<si><t>${escapeXml(t)}</t></si>`)
    .join('');
  return `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" count="${items.length}" uniqueCount="${items.length}">${body}</sst>`;
}

function strIndex(items, text) {
  let i = items.indexOf(text);
  if (i === -1) {
    items.push(text);
    i = items.length - 1;
  }
  return i;
}

function setCell(rowMap, addr, opts) {
  rowMap[addr] = { ...rowMap[addr], ...opts };
}

function rowCellsToXml(rowNum, cells) {
  const sorted = Object.keys(cells).sort(
    (a, b) => a.charCodeAt(0) - b.charCodeAt(0) || parseInt(a.slice(1)) - parseInt(b.slice(1))
  );
  let inner = '';
  for (const addr of sorted) {
    const c = cells[addr];
    const col = addr.replace(/\d+/, '');
    const r = addr;
    let attrs = `r="${r}"`;
    if (c.s) attrs += ` s="${c.s}"`;
    if (c.t) attrs += ` t="${c.t}"`;
    let body = '';
    if (c.f) body += `<f>${c.f}</f>`;
    if (c.v !== undefined && c.v !== null) body += `<v>${escapeXml(String(c.v))}</v>`;
    inner += `<c ${attrs}>${body}</c>`;
  }
  return `<row r="${rowNum}" spans="1:11" x14ac:dyDescent="0.3">${inner}</row>`;
}

function cellXml(addr, opts) {
  const tAttr = opts.t ? ` t="${opts.t}"` : '';
  const sAttr = opts.s ? ` s="${opts.s}"` : '';
  const fPart = opts.f ? `<f>${opts.f}</f>` : '';
  const vPart =
    opts.v !== undefined && opts.v !== null ? `<v>${opts.v}</v>` : '';
  if (!fPart && !vPart) return `<c r="${addr}"${sAttr}${tAttr}/>`;
  return `<c r="${addr}"${sAttr}${tAttr}>${fPart}${vPart}</c>`;
}

function patchSheetXml(xml, updates) {
  const byRow = {};
  for (const [addr, opts] of Object.entries(updates)) {
    const row = addr.replace(/^[A-Z]+/, '');
    if (!byRow[row]) byRow[row] = {};
    byRow[row][addr] = opts;
  }

  return xml.replace(/<row r="(\d+)"([^>]*)>([\s\S]*?)<\/row>/g, (full, rowNum, rowAttrs, rowInner) => {
    const rowUpdates = byRow[rowNum];
    if (!rowUpdates) return full;

    const cells = {};
    const cellRe = /<c r="([A-Z]+\d+)"[^>]*(?:\/>|>[\s\S]*?<\/c>)/g;
    let m;
    while ((m = cellRe.exec(rowInner)) !== null) {
      cells[m[1]] = m[0];
    }
    for (const [addr, opts] of Object.entries(rowUpdates)) {
      cells[addr] = cellXml(addr, opts);
    }
    const sorted = Object.keys(cells).sort(
      (a, b) => a.charCodeAt(0) - b.charCodeAt(0) || parseInt(a.slice(1)) - parseInt(b.slice(1))
    );
    const newInner = sorted.map((a) => cells[a]).join('');
    return `<row r="${rowNum}"${rowAttrs}>${newInner}</row>`;
  });
}

function fmtPct(n) {
  return n;
}

function buildSheet1Updates(student, items, data) {
  const u = {};
  u.B6 = { t: 's', v: strIndex(items, student.company), s: '28' };
  u.B7 = { t: 's', v: strIndex(items, student.groups), s: '30' };
  for (let i = 0; i < 5; i++) {
    const r = 8 + i;
    u[`B${r}`] = { t: 's', v: strIndex(items, student.brandLines(data[i])), s: '29' };
  }
  u.B17 = { t: 's', v: strIndex(items, student.period), s: '33' };
  u.C17 = { t: 's', v: strIndex(items, student.period), s: '33' };

  for (let i = 0; i < 5; i++) {
    const r = 18 + i;
    const b = data[i];
    u[`A${r}`] = { t: 's', v: strIndex(items, b.short), s: '34' };
    u[`B${r}`] = { v: b.sales, s: '35' };
    u[`C${r}`] = { v: b.profit, s: '36' };
    u[`D${r}`] = { v: b.growth, s: '41' };
    u[`E${r}`] = { v: b.capacity, s: '35' };
    u[`F${r}`] = {
      f: `D${r}*E${r}/E23`,
      v: b.weighted,
      t: 'n',
      s: '39',
    };
    u[`G${r}`] = {
      f: `IF(F${r}>10%,"высокий","низкий")`,
      v: strIndex(items, b.growthMatrix),
      t: 's',
      s: '40',
    };
    u[`H${r}`] = { v: b.share, s: '41' };
    u[`I${r}`] = { v: b.competitor, s: '41' };
    u[`J${r}`] = { f: `H${r}/I${r}`, v: b.rel, s: '42' };
    u[`K${r}`] = {
      f: `IF(J${r}>1,"высокая","низкая")`,
      v: strIndex(items, b.shareMatrix),
      t: 's',
      s: '40',
    };
  }

  const totalSales = data.reduce((s, b) => s + b.sales, 0);
  const totalProfit = data.reduce((s, b) => s + b.profit, 0);
  u.B23 = { f: 'SUM(B18:B22)', v: totalSales, s: '38' };
  u.C23 = { f: 'SUM(C18:C22)', v: totalProfit, s: '38' };
  u.E23 = { f: 'SUM(E18:E22)', v: E_TOTAL, s: '38' };
  return u;
}

function buildSheet2Updates(student, items) {
  const u = {};
  // Матрица по продажам — трудные дети (высокий рост, низкая доля)
  u.C7 = { t: 's', v: strIndex(items, 'Gamma'), s: '6' };
  u.D7 = { v: 1500, s: '19' };
  u.C8 = { t: 's', v: strIndex(items, 'Beta'), s: '6' };
  u.D8 = { v: 1000, s: '19' };
  // Собаки
  u.C13 = { t: 's', v: strIndex(items, 'Alpha'), s: '9' };
  u.D13 = { v: 500, s: '18' };
  // Дойные коровы
  u.E13 = { t: 's', v: strIndex(items, 'Epsilon'), s: '8' };
  u.F13 = { v: 3000, s: '20' };
  u.E14 = { t: 's', v: strIndex(items, 'Delta'), s: '8' };
  u.F14 = { v: 450, s: '20' };
  // Итоги (пересчёт значений)
  u.D11 = { f: 'SUM(D7:D10)', v: 2500, s: '11' };
  u.F11 = { f: 'SUM(F7:F10)', v: 0, s: '15' };
  u.D17 = { f: 'SUM(D13:D16)', v: 500, s: '13' };
  u.F17 = { f: 'SUM(F13:F16)', v: 3450, s: '17' };
  // Выводы продажи
  u.H7 = { t: 's', v: strIndex(items, student.conclusionsSales), s: '62' };
  u.I7 = { t: 's', v: strIndex(items, ''), s: '63' };

  // Матрица по прибыли
  u.C29 = { t: 's', v: strIndex(items, 'Gamma'), s: '6' };
  u.D29 = { v: 1100, s: '19' };
  u.C30 = { t: 's', v: strIndex(items, 'Beta'), s: '6' };
  u.D30 = { v: 200, s: '19' };
  u.C35 = { t: 's', v: strIndex(items, 'Alpha'), s: '9' };
  u.D35 = { v: 100, s: '18' };
  u.E35 = { t: 's', v: strIndex(items, 'Epsilon'), s: '8' };
  u.F35 = { v: 1700, s: '20' };
  u.E36 = { t: 's', v: strIndex(items, 'Delta'), s: '8' };
  u.F36 = { v: 200, s: '20' };
  u.D33 = { f: 'SUM(D29:D32)', v: 1300, s: '11' };
  u.F33 = { f: 'SUM(F29:F32)', v: 0, s: '15' };
  u.D39 = { f: 'SUM(D35:D38)', v: 100, s: '13' };
  u.F39 = { f: 'SUM(F35:F38)', v: 1900, s: '17' };
  u.H29 = { t: 's', v: strIndex(items, student.conclusionsProfit), s: '54' };

  return u;
}

function generateStudent(student) {
  const workDir = fs.mkdtempSync('/tmp/bcg-');
  const outPath = path.join(OUT_DIR, student.file);
  fs.copyFileSync(TEMPLATE, path.join(workDir, 'book.xlsx'));
  execSync(`cd "${workDir}" && unzip -q book.xlsx -d x`);

  let sst = fs.readFileSync(`${workDir}/x/xl/sharedStrings.xml`, 'utf8');
  let items = loadSharedStrings(sst);
  const data = enrich();

  let sheet1 = fs.readFileSync(`${workDir}/x/xl/worksheets/sheet1.xml`, 'utf8');
  let sheet2 = fs.readFileSync(`${workDir}/x/xl/worksheets/sheet2.xml`, 'utf8');

  const u1 = buildSheet1Updates(student, items, data);
  const u2 = buildSheet2Updates(student, items);

  sheet1 = patchSheetXml(sheet1, u1);
  sheet2 = patchSheetXml(sheet2, u2);

  fs.writeFileSync(`${workDir}/x/xl/sharedStrings.xml`, saveSharedStrings(items));
  fs.writeFileSync(`${workDir}/x/xl/worksheets/sheet1.xml`, sheet1);
  fs.writeFileSync(`${workDir}/x/xl/worksheets/sheet2.xml`, sheet2);

  execSync(`cd "${workDir}/x" && zip -qr "../filled.xlsx" .`);
  fs.copyFileSync(`${workDir}/filled.xlsx`, outPath);
  fs.rmSync(workDir, { recursive: true, force: true });
  console.log('Created:', outPath);
}

for (const s of STUDENTS) {
  generateStudent(s);
}
