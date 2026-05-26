const fs = require("fs");
const path = require("path");
const readline = require("readline");

const ROOT = path.resolve(__dirname, "..");
const FINANCIAL_DIR = path.join(
  ROOT,
  "data-lake",
  "silver",
  "dart",
  "normalized",
);
const PRICE_PATH = path.join(
  ROOT,
  "data-lake",
  "silver",
  "krx",
  "price",
  "kr_normalized_price.csv",
);
const SHARES_PATH = path.join(
  ROOT,
  "data-lake",
  "silver",
  "krx",
  "shares",
  "kr_normalized_shares.csv",
);
const DIVIDEND_PATH = path.join(
  ROOT,
  "data-lake",
  "silver",
  "dart",
  "dividend",
  "kr_dividend_normalized.csv",
);
const DIVIDEND_DIR = path.join(ROOT, "data-lake", "bronze", "dart", "dividend");
const OUT_DIR = path.join(ROOT, "data-lake", "gold", "factor_coverage");
const OUT_CSV = path.join(OUT_DIR, "kr_factor_coverage_all_stocks.csv");
const OUT_SUMMARY = path.join(OUT_DIR, "factor_coverage_summary.json");

function todaySeoulText() {
  const parts = new Intl.DateTimeFormat("en", {
    timeZone: "Asia/Seoul",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).formatToParts(new Date());
  const part = (type) => parts.find((item) => item.type === type).value;
  return `${part("year")}-${part("month")}-${part("day")}`;
}

const TODAY_TEXT = process.env.FACTOR_COVERAGE_TODAY || todaySeoulText();
const TODAY = Date.parse(`${TODAY_TEXT}T00:00:00+09:00`);

const FACTORS = [
  "at",
  "seq",
  "ceq",
  "ppent",
  "act",
  "lct",
  "invt",
  "rect",
  "ap",
  "dltt",
  "dlc",
  "che",
  "retained_earnings",
  "sale",
  "ni",
  "ni_parent",
  "oiadp",
  "oibdp",
  "cogs",
  "dp",
  "xrd",
  "xint",
  "oancf",
  "capx",
  "fcf",
  "fcff",
  "fcfe",
  "ffo",
  "dvpsp",
  "dvpsx",
  "sstk",
  "prstkc",
  "eps",
  "bps",
  "sps",
  "cps",
  "csho",
  "mcap_mil",
  "gpm",
  "opm",
  "ebitda_margin",
  "npm",
  "tax_rate",
  "nopat",
  "roe",
  "avg_parent_equity",
  "roa",
  "iroe",
  "roic_financial",
  "roic_operational",
  "asset_turnover",
  "receivables_turnover",
  "inventory_turnover",
  "inv_days",
  "ar_days",
  "ap_days",
  "ccc",
  "working_capital",
  "wc_to_sales_pct",
  "working_capital_turnover",
  "sales_yoy_pct",
  "op_yoy_pct",
  "sales_change_mil",
  "op_change_mil",
  "rdsr_pct",
  "eps_yoy_pct",
  "asset_yoy_pct",
  "cfo_yoy_pct",
  "fcf_yoy_pct",
  "ffo_yoy_pct",
  "peg",
  "epr",
  "bpr",
  "tpr",
  "spr",
  "cpr",
  "fcfpr",
  "npr",
  "rpr",
  "ebitda_to_ev",
  "ev_to_ebitda",
  "ev_to_nopat",
  "na_5",
  "na_20",
  "na_50",
  "na_150",
  "na_200",
  "tr_12_1",
  "tr_6_1",
  "tr_3_1",
  "ret_1m",
  "high52w_gap_pct",
  "risk_adj_mom",
  "vol_12_1_ann",
  "mdd1yr_12_1_pct",
  "adturn_pct_12_1",
  "net_debt_to_ebitda",
  "net_debt_to_ocf",
  "fc_to_ndr",
  "icr_times",
  "interest_coverage",
  "current_ratio",
  "debt_to_equity",
  "cash_to_debt",
  "sharehold_div_yield",
  "sharehold_net_buyback_yield",
  "sharehold_return",
  "tdpr",
  "per",
  "pbr",
  "pcr",
  "psr",
  "roce",
  "total_interest_coverage",
  "debt_ratio",
  "dividend_yield",
  "payout_ratio",
  "altman_z_score",
  "beneish_m_score",
  "f_score",
];

function normCode(code) {
  return String(code ?? "")
    .trim()
    .padStart(6, "0");
}

function securityId(code) {
  return `SEC_KR_${normCode(code)}`;
}

function parseCsvLine(line) {
  const out = [];
  let value = "";
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (ch === '"') {
      if (inQuotes && line[i + 1] === '"') {
        value += '"';
        i++;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (ch === "," && !inQuotes) {
      out.push(value);
      value = "";
    } else {
      value += ch;
    }
  }
  out.push(value);
  return out;
}

function readCsv(filePath) {
  const text = fs.readFileSync(filePath, "utf8").replace(/^\uFEFF/, "");
  const records = [];
  let record = [];
  let value = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i++) {
    const ch = text[i];
    if (ch === '"') {
      if (inQuotes && text[i + 1] === '"') {
        value += '"';
        i++;
      } else {
        inQuotes = !inQuotes;
      }
    } else if (ch === "," && !inQuotes) {
      record.push(value);
      value = "";
    } else if ((ch === "\n" || ch === "\r") && !inQuotes) {
      if (ch === "\r" && text[i + 1] === "\n") i++;
      record.push(value);
      value = "";
      if (record.length > 1 || record[0] !== "") records.push(record);
      record = [];
    } else {
      value += ch;
    }
  }
  if (value.length || record.length) {
    record.push(value);
    if (record.length > 1 || record[0] !== "") records.push(record);
  }
  if (records.length === 0) return [];
  const headers = records[0];
  return records.slice(1).map((cells) => {
    const row = {};
    headers.forEach((h, i) => {
      row[h] = cells[i] ?? "";
    });
    return row;
  });
}

function num(value) {
  if (value === null || value === undefined || value === "") return null;
  const n = Number(String(value).replace(/,/g, ""));
  return Number.isNaN(n) ? null : n;
}

function isCovered(value) {
  return value !== null && value !== undefined && !Number.isNaN(value);
}

function add(a, b) {
  return isCovered(a) && isCovered(b) ? a + b : null;
}

function sub(a, b) {
  return isCovered(a) && isCovered(b) ? a - b : null;
}

function mul(a, b) {
  return isCovered(a) && isCovered(b) ? a * b : null;
}

function div(a, b) {
  if (!isCovered(a) || !isCovered(b)) return null;
  const v = a / b;
  return Number.isNaN(v) ? null : v;
}

function pctYoy(curr, prev) {
  const r = div(curr, prev);
  return isCovered(r) ? (r - 1) * 100 : null;
}

function first(row, ...cols) {
  for (const col of cols) {
    if (isCovered(row[col])) return row[col];
  }
  return null;
}

function fill0(value) {
  return isCovered(value) ? value : 0;
}

function pickLargestAbs(values) {
  let best = null;
  for (const value of values) {
    if (!isCovered(value)) continue;
    if (!isCovered(best) || Math.abs(value) > Math.abs(best)) best = value;
  }
  return best;
}

function extractAmountByName(
  rows,
  patterns,
  statementTypes,
  absoluteValue = false,
) {
  const values = [];
  for (const row of rows) {
    const name = row.original_account_name ?? "";
    const statementType = row.statement_type ?? "";
    if (statementTypes && !statementTypes.includes(statementType)) continue;
    if (!patterns.some((pattern) => pattern.test(name))) continue;
    const value = num(row.normalized_amount);
    if (isCovered(value)) values.push(absoluteValue ? Math.abs(value) : value);
  }
  return pickLargestAbs(values);
}

function financialFileMeta(name) {
  const consolidated = name.match(/^kr_normalized_(\d{6})\.csv$/);
  if (consolidated) {
    return {
      stockCode: consolidated[1],
      year: null,
      month: null,
      consolidated: true,
    };
  }
  const match = name.match(/^normalized_(\d{6})_(\d{4})\.(\d{2})\.csv$/);
  if (!match) return null;
  return {
    stockCode: match[1],
    year: Number(match[2]),
    month: Number(match[3]),
    consolidated: false,
  };
}

function discoverAnnualFiles() {
  const byStock = new Map();
  for (const name of fs.readdirSync(FINANCIAL_DIR)) {
    if (name.includes(".debug") || name.includes(".validation")) continue;
    const meta = financialFileMeta(name);
    if (!meta || (!meta.consolidated && meta.month !== 12)) continue;
    if (!byStock.has(meta.stockCode)) byStock.set(meta.stockCode, []);
    byStock
      .get(meta.stockCode)
      .push({ ...meta, path: path.join(FINANCIAL_DIR, name) });
  }
  for (const files of byStock.values()) files.sort((a, b) => a.year - b.year);
  return byStock;
}

function readAnnualFinancials(stockCode, files) {
  const rows = [];
  for (const file of files) {
    const csvRows = readCsv(file.path);
    if (csvRows.length === 0) continue;
    const annualRowsByYear = new Map();
    for (const row of csvRows) {
      const year = file.consolidated ? Number(row.fiscal_year) : file.year;
      const month = file.consolidated ? Number(row.fiscal_month) : file.month;
      if (!year || month !== 12) continue;
      if (!annualRowsByYear.has(year)) annualRowsByYear.set(year, []);
      annualRowsByYear.get(year).push(row);
    }
    for (const [year, annualRows] of annualRowsByYear.entries()) {
      const grouped = new Map();
      for (const row of annualRows) {
        const account = row.canonical_account_id;
        if (!account || account === "UNMAPPED") continue;
        if (!grouped.has(account)) grouped.set(account, []);
        grouped.get(account).push(num(row.normalized_amount));
      }
      const values = {};
      for (const [account, accountValues] of grouped.entries()) {
        values[account] = pickLargestAbs(accountValues);
      }
      values.stock_code = stockCode;
      values.security_id = securityId(stockCode);
      values.fiscal_year = year;
      values.financial_period_ts = Date.parse(
        `${year}-12-31T00:00:00+09:00`,
      );
      rows.push(values);
    }
  }
  return addAnnualFinancialFactors(
    rows.sort((a, b) => a.financial_period_ts - b.financial_period_ts),
  );
}
function addAnnualFinancialFactors(rows) {
  const result = [];
  for (let i = 0; i < rows.length; i++) {
    const source = rows[i];
    const prev = i > 0 ? result[i - 1] : {};
    const r = { ...source };
    r.at = source.TOTAL_ASSETS ?? null;
    r.seq = source.TOTAL_EQUITY ?? null;
    r.ceq = first(source, "EAOP", "TOTAL_EQUITY");
    r.ppent = source.PPE ?? null;
    r.act = source.CURRENT_ASSETS ?? null;
    r.lct = source.CURRENT_LIABILITIES ?? null;
    r.invt = source.INVENTORIES ?? null;
    r.rect = first(
      source,
      "TRADE_RECEIVABLES",
      "TRADE_AND_OTHER_RECEIVABLES",
      "OTHER_RECEIVABLES",
    );
    r.ap = first(
      source,
      "TRADE_PAYABLES",
      "TRADE_AND_OTHER_PAYABLES",
      "OTHER_PAYABLES",
    );
    r.dltt = first(source, "LONG_TERM_DEBT", "LONG_TERM_DEBT_FALLBACK");
    r.dlc = source.SHORT_TERM_DEBT ?? null;
    r.che =
      fill0(source.CASH_AND_EQUIVALENTS) +
      fill0(source.SHORT_TERM_FINANCIAL_ASSETS);
    r.sale = source.REVENUE ?? null;
    r.ni = source.NET_INCOME ?? null;
    r.ni_parent = first(source, "NET_INCOME_PARENT", "NET_INCOME");
    r.oiadp = source.OPERATING_INCOME ?? null;
    r.cogs = source.COGS ?? null;
    r.xrd = source.RND ?? null;
    r.xint = first(
      source,
      "INTEREST_EXPENSE_FALLBACK",
      "INT_PAID",
      "INTEREST_PAID_FALLBACK",
      "FINANCE_COST_FALLBACK",
    );
    r.tax_expense = source.TAX_EXPENSE ?? null;
    r.pbt = source.PBT ?? null;
    r.gross_profit = first(source, "GROSS_PROFIT");
    if (!isCovered(r.gross_profit)) r.gross_profit = sub(r.sale, r.cogs);
    r.dp = first(source, "DNA_IS");
    if (!isCovered(r.dp))
      r.dp = fill0(source.DEPRECIATION_EXPENSE) + fill0(source.AMORTIZATION);
    r.oibdp = first(source, "EBITDA");
    if (!isCovered(r.oibdp)) r.oibdp = add(r.oiadp, fill0(r.dp));
    r.oancf = source.CFO ?? null;
    r.capx = isCovered(source.CAPEX_PPE) ? Math.abs(source.CAPEX_PPE) : null;
    r.fcf = sub(r.oancf, r.capx);
    r.ffo = add(r.ni, fill0(r.dp));
    r.sstk = source.EQ_ISSUE ?? null;
    r.prstkc = source.BUYBACK ?? null;
    r.debt_issue = source.DEBT_ISSUE ?? null;
    r.debt_repay = source.DEBT_REPAY ?? null;

    r.avg_assets =
      isCovered(r.at) && isCovered(prev.at) ? (r.at + prev.at) / 2 : null;
    r.avg_equity =
      isCovered(r.seq) && isCovered(prev.seq) ? (r.seq + prev.seq) / 2 : null;
    r.avg_inventory =
      isCovered(r.invt) && isCovered(prev.invt)
        ? (r.invt + prev.invt) / 2
        : null;
    r.avg_receivables =
      isCovered(r.rect) && isCovered(prev.rect)
        ? (r.rect + prev.rect) / 2
        : null;
    r.avg_payables =
      isCovered(r.ap) && isCovered(prev.ap) ? (r.ap + prev.ap) / 2 : null;

    r.gpm = div(r.gross_profit, r.sale);
    r.opm = div(r.oiadp, r.sale);
    r.ebitda_margin = div(r.oibdp, r.sale);
    r.npm = div(r.ni, r.sale);
    r.tax_rate = div(r.tax_expense, r.pbt);
    if (isCovered(r.tax_rate) && (r.tax_rate < 0 || r.tax_rate > 1))
      r.tax_rate = null;
    r.nopat = mul(r.oiadp, isCovered(r.tax_rate) ? 1 - r.tax_rate : null);
    r.avg_parent_equity =
      isCovered(r.ceq) && isCovered(prev.ceq) ? (r.ceq + prev.ceq) / 2 : null;
    r.roe = div(r.ni_parent, r.avg_parent_equity);
    r.roa = div(r.ni, r.avg_assets);
    r.iroe = div(
      add(r.ni_parent, fill0(r.xrd) * (1 - fill0(r.tax_rate))),
      r.avg_parent_equity,
    );
    r.debt = fill0(r.dltt) + fill0(r.dlc);
    r.net_debt = r.debt - fill0(r.che);
    r.invested_capital_financial = isCovered(r.seq)
      ? r.seq + r.debt - fill0(r.che)
      : null;
    r.invested_capital_operational =
      fill0(r.rect) +
      fill0(r.invt) -
      fill0(r.ap) +
      fill0(r.ppent) +
      fill0(source.INTANGIBLE_ASSETS);
    r.avg_ic_financial =
      isCovered(r.invested_capital_financial) &&
      isCovered(prev.invested_capital_financial)
        ? (r.invested_capital_financial + prev.invested_capital_financial) / 2
        : null;
    r.avg_ic_operational =
      isCovered(r.invested_capital_operational) &&
      isCovered(prev.invested_capital_operational)
        ? (r.invested_capital_operational + prev.invested_capital_operational) /
          2
        : null;
    r.roic_financial = div(r.nopat, r.avg_ic_financial);
    r.roic_operational = div(r.nopat, r.avg_ic_operational);
    r.asset_turnover = div(r.sale, r.avg_assets);
    r.receivables_turnover = div(r.sale, r.avg_receivables);
    r.inventory_turnover = div(r.cogs, r.avg_inventory);
    r.inv_days = mul(div(r.avg_inventory, r.cogs), 365);
    r.ar_days = mul(div(r.avg_receivables, r.sale), 365);
    r.ap_days = mul(div(r.avg_payables, r.cogs), 365);
    r.ccc =
      isCovered(r.inv_days) && isCovered(r.ar_days) && isCovered(r.ap_days)
        ? r.inv_days + r.ar_days - r.ap_days
        : null;
    r.working_capital = sub(r.act, r.lct);
    r.wc_to_sales_pct = mul(div(r.working_capital, r.sale), 100);
    r.working_capital_turnover = div(r.sale, r.working_capital);
    const wcChange =
      isCovered(r.working_capital) && isCovered(prev.working_capital)
        ? r.working_capital - prev.working_capital
        : 0;
    r.fcff = isCovered(r.nopat)
      ? r.nopat + fill0(r.dp) - fill0(r.capx) - wcChange
      : null;
    r.fcfe = isCovered(r.fcf)
      ? r.fcf +
        fill0(r.debt_issue) -
        fill0(r.debt_repay) +
        fill0(r.sstk) -
        fill0(r.prstkc)
      : null;
    r.sales_yoy_pct = pctYoy(r.sale, prev.sale);
    r.op_yoy_pct = pctYoy(r.oiadp, prev.oiadp);
    r.sales_change_mil =
      isCovered(r.sale) && isCovered(prev.sale)
        ? (r.sale - prev.sale) / 1_000_000
        : null;
    r.op_change_mil =
      isCovered(r.oiadp) && isCovered(prev.oiadp)
        ? (r.oiadp - prev.oiadp) / 1_000_000
        : null;
    r.rdsr_pct = mul(div(r.xrd, r.sale), 100);
    r.eps = first(source, "BASIC_EPS", "DILUTED_EPS");
    if (!isCovered(r.eps)) r.eps = div(r.ni_parent, source.shares ?? null);
    r.eps_yoy_pct = pctYoy(r.eps, prev.eps);
    r.asset_yoy_pct = pctYoy(r.at, prev.at);
    r.cfo_yoy_pct = pctYoy(r.oancf, prev.oancf);
    r.fcf_yoy_pct = pctYoy(r.fcf, prev.fcf);
    r.ffo_yoy_pct = pctYoy(r.ffo, prev.ffo);
    r.net_debt_to_ebitda = div(r.net_debt, r.oibdp);
    r.fc_to_ndr = div(r.fcf, r.net_debt);
    r.icr_times = div(r.oancf, isCovered(r.xint) ? Math.abs(r.xint) : null);
    r.interest_coverage = div(
      r.oiadp,
      isCovered(r.xint) ? Math.abs(r.xint) : null,
    );
    r.current_ratio = div(r.act, r.lct);
    r.debt_to_equity = div(r.debt, r.seq);
    r.cash_to_debt = div(r.che, r.debt);
    r.retained_earnings = first(
      source,
      "RETAINED_EARNINGS",
      "RETAINED_EARNINGS_FALLBACK",
    );
    r.altman_z_score = add(
      add(
        add(
          mul(1.2, div(sub(r.act, r.lct), r.at)),
          mul(1.4, div(r.retained_earnings, r.at)),
        ),
        mul(3.3, div(r.oiadp, r.at)),
      ),
      div(r.sale, r.at),
    );
    r.beneish_m_score = beneish(r, prev, source.SGNA ?? null);
    r.f_score = piotroski(r, prev);
    result.push(r);
  }
  return result;
}

function beneish(r, prev, sgna) {
  const dsri = div(div(r.rect, r.sale), div(prev.rect, prev.sale));
  const gmi = div(
    div(sub(prev.sale, prev.cogs), prev.sale),
    div(sub(r.sale, r.cogs), r.sale),
  );
  const aqi = div(
    sub(1, div(add(r.act, r.ppent), r.at)),
    sub(1, div(add(prev.act, prev.ppent), prev.at)),
  );
  const sgi = div(r.sale, prev.sale);
  const depi = div(
    div(prev.dp, add(prev.ppent, prev.dp)),
    div(r.dp, add(r.ppent, r.dp)),
  );
  const sgai = div(div(sgna, r.sale), div(prev.SGNA ?? null, prev.sale));
  const lvgi = div(
    div(add(r.dltt, r.dlc), r.at),
    div(add(prev.dltt, prev.dlc), prev.at),
  );
  const tata = div(sub(r.ni, r.oancf), r.at);
  const parts = [
    -4.84,
    mul(0.92, dsri),
    mul(0.528, gmi),
    mul(0.404, aqi),
    mul(0.892, sgi),
    mul(0.115, depi),
    mul(-0.172, sgai),
    mul(4.679, tata),
    mul(-0.327, lvgi),
  ];
  return parts.every(isCovered) ? parts.reduce((a, b) => a + b, 0) : null;
}

function piotroski(r, prev) {
  let score = 0;
  score += r.roa > 0 ? 1 : 0;
  score += r.oancf > 0 ? 1 : 0;
  score += isCovered(r.roa) && isCovered(prev.roa) && r.roa > prev.roa ? 1 : 0;
  score += isCovered(r.oancf) && isCovered(r.ni) && r.oancf > r.ni ? 1 : 0;
  score +=
    isCovered(r.debt_to_equity) &&
    isCovered(prev.debt_to_equity) &&
    r.debt_to_equity < prev.debt_to_equity
      ? 1
      : 0;
  score +=
    isCovered(r.current_ratio) &&
    isCovered(prev.current_ratio) &&
    r.current_ratio > prev.current_ratio
      ? 1
      : 0;
  score += fill0(r.sstk) <= 0 ? 1 : 0;
  score += isCovered(r.gpm) && isCovered(prev.gpm) && r.gpm > prev.gpm ? 1 : 0;
  score +=
    isCovered(r.asset_turnover) &&
    isCovered(prev.asset_turnover) &&
    r.asset_turnover > prev.asset_turnover
      ? 1
      : 0;
  return score;
}

// Dividend coverage is read from silver/dart/dividend/kr_dividend_normalized.csv.
// Legacy bronze dividend JSON helpers were removed from this calculation path.
async function loadGroupedCsv(filePath, wantedSecurities, columns) {
  const grouped = new Map();
  const stream = fs.createReadStream(filePath, { encoding: "utf8" });
  const rl = readline.createInterface({ input: stream, crlfDelay: Infinity });
  let headers = null;
  let lineNo = 0;
  for await (const line of rl) {
    if (!line) continue;
    lineNo++;
    if (headers === null) {
      headers = parseCsvLine(line.replace(/^\uFEFF/, ""));
      continue;
    }
    const cells = parseCsvLine(line);
    const row = {};
    headers.forEach((h, i) => {
      if (!columns || columns.includes(h)) row[h] = cells[i] ?? "";
    });
    const sid = row.security_id;
    if (!wantedSecurities.has(sid)) continue;
    if (!grouped.has(sid)) grouped.set(sid, []);
    grouped.get(sid).push(row);
    if (lineNo % 1_000_000 === 0) {
      process.stderr.write(
        `[INFO] read ${path.basename(filePath)} lines=${lineNo.toLocaleString()}\n`,
      );
    }
  }
  return grouped;
}

function maxDrawdown(returns) {
  let wealth = 1;
  let peak = 1;
  let mdd = 0;
  for (const ret of returns) {
    wealth *= 1 + (isCovered(ret) ? ret : 0);
    if (wealth > peak) peak = wealth;
    const dd = wealth / peak - 1;
    if (dd < mdd) mdd = dd;
  }
  return mdd;
}

function rollingMean(values, end, window, minPeriods = 1) {
  let sum = 0;
  let count = 0;
  const start = Math.max(0, end - window + 1);
  for (let i = start; i <= end; i++) {
    if (isCovered(values[i])) {
      sum += values[i];
      count++;
    }
  }
  return count >= minPeriods ? sum / count : null;
}

function rollingMax(values, end, window, minPeriods) {
  let max = null;
  let count = 0;
  const start = Math.max(0, end - window + 1);
  for (let i = start; i <= end; i++) {
    if (isCovered(values[i])) {
      max = isCovered(max) ? Math.max(max, values[i]) : values[i];
      count++;
    }
  }
  return count >= minPeriods ? max : null;
}

function rollingStd(values, end, window, minPeriods) {
  const xs = [];
  const start = Math.max(0, end - window + 1);
  for (let i = start; i <= end; i++)
    if (isCovered(values[i])) xs.push(values[i]);
  if (xs.length < minPeriods) return null;
  const mean = xs.reduce((a, b) => a + b, 0) / xs.length;
  const variance =
    xs.reduce((a, b) => a + (b - mean) ** 2, 0) / (xs.length - 1);
  return xs.length > 1 ? Math.sqrt(variance) : null;
}

function addDailyFactors(rows) {
  const close = rows.map((r) => r.close);
  const volume = rows.map((r) => r.volume);
  const shares = rows.map((r) => r.shares);
  const ret = close.map((c, i) => (i === 0 ? null : div(c, close[i - 1]) - 1));
  const shiftedRet = ret.map((_, idx) => (idx >= 21 ? ret[idx - 21] : null));
  const turnoverShifted = volume.map((_, idx) =>
    idx >= 21 ? mul(div(volume[idx - 21], shares[idx - 21]), 100) : null,
  );
  for (let i = 0; i < rows.length; i++) {
    const r = rows[i];
    r.mcap_mil = div(r.market_cap, 1_000_000);
    r.trading_value = mul(r.close, r.volume);
    r.csho = r.shares;
    if (!isCovered(r.eps)) r.eps = div(r.ni_parent, r.shares);
    if (!isCovered(r.eps)) r.eps = 0;
    r.bps = div(r.ceq, r.shares);
    if (!isCovered(r.bps)) r.bps = 0;
    r.sps = div(r.sale, r.shares);
    if (!isCovered(r.sps)) r.sps = 0;
    r.cps = div(r.oancf, r.shares);
    if (!isCovered(r.cps)) r.cps = 0;
    if (!isCovered(r.fcff)) r.fcff = 0;
    if (!isCovered(r.fcfe)) r.fcfe = 0;
    if (isCovered(r.altman_z_score)) {
      r.altman_z_score = add(
        r.altman_z_score,
        mul(0.6, div(r.market_cap, r.TOTAL_LIABILITIES ?? null)),
      );
    }
    r.epr = div(r.eps, r.close);
    r.bpr = div(r.bps, r.close);
    r.tpr = div(r.ppent, r.market_cap);
    r.spr = div(r.sps, r.close);
    r.cpr = div(r.cps, r.close);
    r.fcfpr = div(r.fcfe, r.market_cap);
    r.npr = div(sub(r.che, r.debt), r.market_cap);
    r.rpr = div(r.xrd, r.market_cap);
    r.enterprise_value = isCovered(r.market_cap)
      ? r.market_cap + fill0(r.debt) - fill0(r.che)
      : null;
    r.ebitda_to_ev = div(r.oibdp, r.enterprise_value);
    r.ev_to_ebitda = div(r.enterprise_value, r.oibdp);
    r.ev_to_nopat = div(r.enterprise_value, r.nopat);
    r.net_debt_to_ocf = div(r.net_debt, r.oancf);
    r.per = div(r.close, r.eps);
    r.pbr = div(r.close, r.bps);
    r.pcr = div(r.close, r.cps);
    r.psr = div(r.close, r.sps);
    r.roce = div(r.oiadp, sub(r.at, r.lct));
    r.total_interest_coverage = r.interest_coverage;
    r.debt_ratio = div(r.debt, r.at);
    r.dividend_yield = r.sharehold_div_yield;
    r.payout_ratio = r.tdpr;
    r.peg = div(r.per, r.eps_yoy_pct);
    if (isCovered(r.eps_yoy_pct) && r.eps_yoy_pct <= 0) r.peg = null;
    r.sharehold_net_buyback_yield = mul(
      div(fill0(r.prstkc) - fill0(r.sstk), r.market_cap),
      100,
    );
    r.sharehold_return =
      fill0(r.sharehold_div_yield) + fill0(r.sharehold_net_buyback_yield);

    r.na_5 = rollingMean(close, i, 5);
    r.na_20 = rollingMean(close, i, 20);
    r.na_50 = rollingMean(close, i, 50);
    r.na_150 = rollingMean(close, i, 150);
    r.na_200 = rollingMean(close, i, 200);
    r.tr_12_1 = i >= 252 ? div(close[i - 21], close[i - 252]) - 1 : null;
    r.tr_6_1 = i >= 126 ? div(close[i - 21], close[i - 126]) - 1 : null;
    r.tr_3_1 = i >= 63 ? div(close[i - 21], close[i - 63]) - 1 : null;
    r.ret_1m = i >= 21 ? div(close[i], close[i - 21]) - 1 : null;
    const high52 = rollingMax(close, i, 252, 20);
    r.high52w_gap_pct = mul(sub(div(close[i], high52), 1), 100);
    const vol = rollingStd(shiftedRet, i, 231, 60);
    r.vol_12_1_ann = isCovered(vol) ? vol * Math.sqrt(252) : null;
    r.risk_adj_mom = div(r.tr_12_1, r.vol_12_1_ann);
    const windowReturns = [];
    for (let j = Math.max(0, i - 231 + 1); j <= i; j++)
      windowReturns.push(shiftedRet[j]);
    const validReturns = windowReturns.filter(isCovered);
    r.mdd1yr_12_1_pct =
      validReturns.length >= 60 ? maxDrawdown(windowReturns) * 100 : null;
    r.adturn_pct_12_1 = rollingMean(turnoverShifted, i, 231, 60);
  }
}

function asofIndex(rows, ts, startIndex) {
  let idx = startIndex;
  while (idx + 1 < rows.length && rows[idx + 1]._ts <= ts) idx++;
  return idx >= 0 && rows[idx]._ts <= ts ? idx : -1;
}

function mergeStockRows(
  stockCode,
  priceRows,
  sharesRows,
  dividendRows,
  financialRows,
) {
  priceRows.sort((a, b) => a._ts - b._ts);
  sharesRows.sort((a, b) => a._ts - b._ts);
  dividendRows.sort((a, b) => a._ts - b._ts);
  financialRows.sort((a, b) => a.financial_period_ts - b.financial_period_ts);
  const rows = [];
  let shareIdx = -1;
  let dividendIdx = -1;
  let finIdx = -1;
  const financialAsofRows = financialRows.map((r) => ({
    ...r,
    _ts: r.financial_period_ts,
  }));
  for (const price of priceRows) {
    if (price._ts > TODAY) continue;
    shareIdx = asofIndex(sharesRows, price._ts, shareIdx);
    dividendIdx = asofIndex(dividendRows, price._ts, dividendIdx);
    finIdx = asofIndex(financialAsofRows, price._ts, finIdx);
    const share = shareIdx >= 0 ? sharesRows[shareIdx] : {};
    const dividend = dividendIdx >= 0 ? dividendRows[dividendIdx] : {};
    const fin = finIdx >= 0 ? financialRows[finIdx] : {};
    const row = { ...fin };
    row.security_id = securityId(stockCode);
    row.stock_code = stockCode;
    row.trade_date = price.trade_date;
    row.close = price.close;
    row.volume = price.volume;
    row.shares = share.shares ?? null;
    row.market_cap = share.market_cap ?? null;
    row.dvpsx = dividend.dividend ?? null;
    row.dvpsp = null;
    row.sharehold_div_yield = dividend.dividend_percent ?? null;
    if (
      isCovered(row.sharehold_div_yield) &&
      (row.sharehold_div_yield < 0 || row.sharehold_div_yield > 100)
    ) {
      row.sharehold_div_yield = null;
    }
    row.tdpr = isCovered(dividend.payout_ratio)
      ? dividend.payout_ratio * 100
      : null;
    rows.push(row);
  }
  addDailyFactors(rows);
  return rows;
}

function preparePriceRows(rows) {
  return rows
    .map((row) => ({
      security_id: row.security_id,
      trade_date: row.trade_date,
      _ts: Date.parse(`${row.trade_date}T00:00:00+09:00`),
      close: num(row.close),
      volume: num(row.volume),
    }))
    .filter((row) => row.security_id && isCovered(row._ts));
}

function prepareShareRows(rows) {
  return rows
    .map((row) => ({
      security_id: row.security_id,
      trade_date: row.trade_date,
      _ts: Date.parse(`${row.trade_date}T00:00:00+09:00`),
      shares: num(row.shares),
      market_cap: num(row.market_cap),
    }))
    .filter((row) => row.security_id && isCovered(row._ts));
}

function prepareDividendRows(rows) {
  return rows
    .map((row) => ({
      security_id: row.security_id,
      trade_date: row.trade_date,
      _ts: Date.parse(`${row.trade_date}T00:00:00+09:00`),
      dividend: num(row.dividend),
      payout_ratio: num(row.payout_ratio),
      dividend_percent: num(row.dividend_percent),
    }))
    .filter((row) => row.security_id && isCovered(row._ts));
}

function csvEscape(value) {
  const text = String(value ?? "");
  return /[",\n\r]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
}

async function main() {
  fs.mkdirSync(OUT_DIR, { recursive: true });
  const financialByStock = discoverAnnualFiles();
  const wantedSecurities = new Set(
    [...financialByStock.keys()].map(securityId),
  );
  process.stderr.write(
    `[INFO] annual-financial stocks=${financialByStock.size.toLocaleString()}\n`,
  );

  const [priceGroupedRaw, shareGroupedRaw, dividendGroupedRaw] = await Promise.all([
    loadGroupedCsv(PRICE_PATH, wantedSecurities, [
      "security_id",
      "trade_date",
      "close",
      "volume",
    ]),
    loadGroupedCsv(SHARES_PATH, wantedSecurities, [
      "security_id",
      "trade_date",
      "shares",
      "market_cap",
    ]),
    loadGroupedCsv(DIVIDEND_PATH, wantedSecurities, [
      "security_id",
      "trade_date",
      "dividend",
      "payout_ratio",
      "dividend_percent",
    ]),
  ]);

  const coverage = new Map(FACTORS.map((factor) => [factor, 0]));
  let rowCount = 0;
  let processedStocks = 0;
  let skippedNoPrice = 0;
  const errors = [];

  for (const [stockCode, files] of financialByStock.entries()) {
    const sid = securityId(stockCode);
    const priceRows = preparePriceRows(priceGroupedRaw.get(sid) ?? []);
    if (priceRows.length === 0) {
      skippedNoPrice++;
      continue;
    }
    try {
      const shareRows = prepareShareRows(shareGroupedRaw.get(sid) ?? []);
      const dividendRows = prepareDividendRows(dividendGroupedRaw.get(sid) ?? []);
      const financialRows = readAnnualFinancials(stockCode, files);
      const rows = mergeStockRows(
        stockCode,
        priceRows,
        shareRows,
        dividendRows,
        financialRows,
      );
      for (const row of rows) {
        rowCount++;
        for (const factor of FACTORS) {
          if (isCovered(row[factor]))
            coverage.set(factor, coverage.get(factor) + 1);
        }
      }
      processedStocks++;
      if (processedStocks % 100 === 0) {
        process.stderr.write(
          `[INFO] processed stocks=${processedStocks.toLocaleString()} rows=${rowCount.toLocaleString()}\n`,
        );
      }
    } catch (error) {
      errors.push({
        stockCode,
        error: String(error && error.stack ? error.stack : error),
      });
    }
  }

  const rows = FACTORS.map((factor) => {
    const covered = coverage.get(factor);
    const missing = rowCount - covered;
    const ratio = rowCount ? covered / rowCount : 0;
    return {
      factor,
      row_count: rowCount,
      covered_count: covered,
      missing_count: missing,
      coverage_ratio: ratio,
      coverage_pct: ratio * 100,
    };
  }).sort(
    (a, b) =>
      a.coverage_ratio - b.coverage_ratio || a.factor.localeCompare(b.factor),
  );

  const totalCells = rowCount * FACTORS.length;
  const coveredCells = rows.reduce((sum, row) => sum + row.covered_count, 0);
  const summary = {
    generated_at: new Date().toISOString(),
    as_of_date: TODAY_TEXT,
    row_count: rowCount,
    processed_stock_count: processedStocks,
    skipped_no_price_count: skippedNoPrice,
    error_count: errors.length,
    factor_count: FACTORS.length,
    total_cells: totalCells,
    covered_cells: coveredCells,
    missing_cells: totalCells - coveredCells,
    coverage_ratio: totalCells ? coveredCells / totalCells : 0,
    coverage_pct: totalCells ? (coveredCells / totalCells) * 100 : 0,
    errors: errors.slice(0, 20),
  };

  const csv =
    [
      "factor,row_count,covered_count,missing_count,coverage_ratio,coverage_pct",
      ...rows.map((row) =>
        [
          row.factor,
          row.row_count,
          row.covered_count,
          row.missing_count,
          row.coverage_ratio,
          row.coverage_pct,
        ]
          .map(csvEscape)
          .join(","),
      ),
    ].join("\n") + "\n";

  fs.writeFileSync(OUT_CSV, csv, "utf8");
  fs.writeFileSync(OUT_SUMMARY, JSON.stringify(summary, null, 2), "utf8");
  process.stdout.write(
    JSON.stringify(
      {
        summary,
        csv_path: OUT_CSV,
        summary_path: OUT_SUMMARY,
        lowest: rows.slice(0, 15),
        highest: rows.slice(-15).reverse(),
      },
      null,
      2,
    ),
  );
}

main().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});


