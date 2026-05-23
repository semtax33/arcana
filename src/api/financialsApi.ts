export type FinancialApiPeriod = "annual" | "quarter" | "ttm";
export type FinancialStatementCode = "IS" | "BS" | "CF";

export type FinancialPoint = {
  label: string;
  value: number | null;
  displayValue?: string;
  growthRate?: number | null;
  displayGrowthRate?: string;
};

export type FinancialAccountSeries = {
  canonicalId: string;
  name: string;
  statement: FinancialStatementCode;
  unit?: string;
  groupKey?: string;
  groupName?: string;
  points: FinancialPoint[];
};

export type FinancialStatementData = {
  code: FinancialStatementCode;
  accounts: FinancialAccountSeries[];
};

const apiBaseUrl = ((import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "").replace(/\/+$/, "");
const apiPath = (path: string) => `${apiBaseUrl}${path}`;

const statementCodes: FinancialStatementCode[] = ["IS", "BS", "CF"];
const accountListKeys = ["accounts", "rows", "items", "line_items", "lineItems", "data", "metrics", "ratios"];
const groupListKeys = ["groups", "categories", "children"];
const pointListKeys = ["points", "values", "series", "periods", "history", "data"];
const labelKeys = [
  "label",
  "date",
  "period",
  "period_key",
  "periodKey",
  "period_end",
  "periodEnd",
  "report_date",
  "reportDate",
  "fiscal_date",
  "fiscalDate",
  "period_key",
  "periodKey",
  "year",
  "quarter",
];
const valueKeys = ["value", "amount", "display_value", "displayValue", "raw_value", "rawValue"];
const growthKeys = [
  "growth",
  "growth_rate",
  "growthRate",
  "yoy",
  "yoy_rate",
  "yoyRate",
  "change_rate",
  "changeRate",
];
const ratioPayloadKeys = ["ratios", "financial_ratios", "financialRatios", "ratio"];
const ratioNameMap: Record<string, string> = {
  gpm: "매출총이익률",
  opm: "영업이익률",
  ebitda_margin: "EBITDA 마진",
  npm: "순이익률",
  tax_rate: "법인세율",
  roe: "자기자본이익률 (ROE)",
  roa: "총자산이익률 (ROA)",
  iroe: "이익잉여금 수익률",
  roic_financial: "투하자본수익률 (재무)",
  roic_operational: "투하자본수익률 (영업)",
  roce: "사용자본수익률 (ROCE)",
  sales_yoy_pct: "매출 성장률",
  op_yoy_pct: "영업이익 성장률",
  eps_yoy_pct: "EPS 성장률",
  sales_change_mil: "매출 증감",
  op_change_mil: "영업이익 증감",
  eps: "주당순이익 (EPS)",
  sps: "주당매출액 (SPS)",
  per: "PER",
  psr: "PSR",
  epr: "이익수익률",
  current_ratio: "유동비율",
  cash_to_debt: "현금/부채 비율",
  working_capital_turnover: "운전자본회전율",
  wc_to_sales_pct: "운전자본/매출 비율",
  debt_to_equity: "부채자본비율",
  debt_ratio: "부채비율",
  net_debt_to_ebitda: "순차입금/EBITDA",
  net_debt_to_ocf: "순차입금/영업현금흐름",
  icr_times: "이자보상배율",
  interest_coverage: "이자보상비율",
  total_interest_coverage: "총이자보상비율",
  altman_z_score: "알트만 Z-Score",
  beneish_m_score: "베니시 M-Score",
  f_score: "피오트로스키 F-Score",
  asset_turnover: "총자산회전율",
  receivables_turnover: "매출채권회전율",
  inventory_turnover: "재고자산회전율",
  inv_days: "재고 회전일수",
  ar_days: "매출채권 회전일수",
  ap_days: "매입채무 회전일수",
  ccc: "현금전환주기",
  asset_yoy_pct: "자산 성장률",
  bps: "주당순자산 (BPS)",
  pbr: "PBR",
  bpr: "BPR",
  mcap_mil: "시가총액",
  cfo_yoy_pct: "영업현금흐름 성장률",
  fcf_yoy_pct: "FCF 성장률",
  ffo_yoy_pct: "FFO 성장률",
  fc_to_ndr: "금융비용/순차입금 비율",
  pcr: "PCR",
  cpr: "현금흐름수익률",
  fcfpr: "FCF 수익률",
  dividend_yield: "배당수익률",
  payout_ratio: "배당성향",
  sharehold_div_yield: "주주 배당수익률",
  sharehold_net_buyback_yield: "순자사주매입 수익률",
  sharehold_return: "주주환원율",
  tdpr: "총배당성향",
  dvpsx: "주당배당금",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

async function readJson(response: Response) {
  const contentType = response.headers.get("content-type") ?? "";

  if (!contentType.includes("json")) {
    throw new Error("재무 API가 JSON을 반환하지 않았습니다.");
  }

  return response.json() as Promise<unknown>;
}

function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "" || value === "N/A") {
    return null;
  }

  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }

  if (typeof value === "string") {
    const normalized = value.replace(/,/g, "").replace(/%$/, "").trim();
    const parsed = Number(normalized);
    return Number.isFinite(parsed) ? parsed : null;
  }

  return null;
}

function pickString(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = record[key];
    if (typeof value === "string" && value.trim().length > 0) {
      return value;
    }
    if (typeof value === "number" && Number.isFinite(value)) {
      return String(value);
    }
  }

  return undefined;
}

function pickNumber(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = toNumber(record[key]);
    if (value !== null) {
      return value;
    }
  }

  return null;
}

function pickArray(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = record[key];
    if (Array.isArray(value)) {
      return value;
    }
  }

  return undefined;
}

function pickSeries(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = record[key];
    if (Array.isArray(value) || isRecord(value)) {
      return value;
    }
  }

  return undefined;
}

function normalizePoint(raw: unknown, fallbackLabel: string): FinancialPoint {
  if (isRecord(raw)) {
    return {
      label: pickString(raw, labelKeys) ?? fallbackLabel,
      value: pickNumber(raw, valueKeys),
      displayValue: pickString(raw, ["display_value", "displayValue"]),
      growthRate: pickNumber(raw, growthKeys),
      displayGrowthRate: pickString(raw, ["display_growth_rate", "displayGrowthRate"]),
    };
  }

  return {
    label: fallbackLabel,
    value: toNumber(raw),
    growthRate: null,
  };
}

function normalizePointMap(record: Record<string, unknown>) {
  const ignoredKeys = new Set([
    "account",
    "accounts",
    "canonical_id",
    "canonicalId",
    "account_id",
    "accountId",
    "account_name",
    "accountName",
    "columns",
    "currency",
    "description",
    "formula",
    "growth_chart",
    "groups",
    "history",
    "id",
    "is_derived",
    "isDerived",
    "ko_name",
    "koName",
    "label",
    "name",
    "period",
    "periods",
    "rows",
    "sections",
    "security_id",
    "series",
    "source",
    "statement",
    "statement_code",
    "statement_type",
    "statementCode",
    "statistics",
    "stock",
    "title",
    "trend",
    "unit",
    "values",
  ]);

  return Object.entries(record)
    .filter(([key, value]) => !ignoredKeys.has(key) && (typeof value === "number" || value === null || isRecord(value)))
    .map(([key, value]) => normalizePoint(value, key));
}

function normalizePoints(raw: unknown) {
  if (Array.isArray(raw)) {
    return raw.map((point, index) => normalizePoint(point, String(index + 1)));
  }

  if (isRecord(raw)) {
    const pointSeries = pickSeries(raw, pointListKeys);
    if (Array.isArray(pointSeries)) {
      return pointSeries.map((point, index) => normalizePoint(point, String(index + 1)));
    }
    if (isRecord(pointSeries)) {
      return normalizePointMap(pointSeries);
    }

    return normalizePointMap(raw);
  }

  return [];
}

function readStatementCode(raw: Record<string, unknown>) {
  const value = pickString(raw, [
    "statement",
    "statement_type",
    "statementType",
    "statement_code",
    "statementCode",
    "fs_div",
    "section",
    "group",
    "category",
    "type",
  ]);

  return statementCodeFromText(value);
}

function readGroupStatementCode(raw: Record<string, unknown>) {
  const value = pickString(raw, [
    "statement",
    "statement_type",
    "statementType",
    "statement_code",
    "statementCode",
    "fs_div",
    "section",
    "group",
    "category",
    "type",
    "title",
    "name",
    "label",
  ]);

  return statementCodeFromText(value);
}


function statementCodeFromText(value: string | undefined) {
  const normalized = value?.trim().toLowerCase();

  if (!normalized) {
    return undefined;
  }

  if (normalized === "is" || /income|profit|손익/.test(normalized)) {
    return "IS";
  }
  if (normalized === "bs" || /balance|financial position|재무상태|대차/.test(normalized)) {
    return "BS";
  }
  if (normalized === "cf" || /cash|현금흐름/.test(normalized)) {
    return "CF";
  }

  return undefined;
}

function inferStatement(raw: Record<string, unknown>, fallback: FinancialStatementCode) {
  const statement = readStatementCode(raw);

  if (statement) {
    return statement;
  }

  return fallback;
}

function pickGroupKey(raw: Record<string, unknown>) {
  return pickString(raw, ["group_key", "groupKey", "category_key", "categoryKey", "group_id", "groupId"]);
}

function pickGroupName(raw: Record<string, unknown>) {
  return pickString(raw, ["group_name", "groupName", "category_name", "categoryName", "group_title", "groupTitle"]);
}

function withParentGroupMetadata(child: unknown, parent: Record<string, unknown>) {
  if (!isRecord(child)) {
    return child;
  }

  const groupKey = pickGroupKey(child) ?? pickGroupKey(parent);
  const groupName = pickGroupName(child) ?? pickGroupName(parent) ?? pickString(parent, ["title", "name", "label", "title_en"]);

  return {
    ...child,
    ...(groupKey ? { group_key: groupKey } : {}),
    ...(groupName ? { group_name: groupName } : {}),
  };
}

function flattenAccountItems(payload: unknown): unknown[] {
  if (Array.isArray(payload)) {
    return payload.flatMap((item) => flattenAccountItems(item));
  }

  if (!isRecord(payload)) {
    return [payload];
  }

  const accountArray = pickArray(payload, accountListKeys);
  if (accountArray) {
    return flattenAccountItems(accountArray.map((item) => withParentGroupMetadata(item, payload)));
  }

  const groupArray = pickArray(payload, groupListKeys);
  if (groupArray) {
    return flattenAccountItems(groupArray);
  }

  return [payload];
}

function normalizeAccount(raw: unknown, fallbackStatement: FinancialStatementCode, fallbackId: string) {
  if (!isRecord(raw)) {
    return {
      canonicalId: fallbackId,
      name: fallbackId,
      statement: fallbackStatement,
      points: normalizePoints(raw),
    };
  }

  const canonicalId =
    pickString(raw, ["factor_id", "factorId", "ratio_id", "ratioId", "canonical_id", "canonicalId", "account_id", "accountId", "id"]) ?? fallbackId;
  const name =
    ratioNameMap[canonicalId] ??
    pickString(raw, ["factor_name", "factorName", "ratio_name", "ratioName", "account_name", "accountName", "name", "label", "title", "ko_name", "koName"]) ??
    canonicalId;
  const series = pickSeries(raw, pointListKeys) ?? raw;

  return {
    canonicalId,
    name,
    statement: inferStatement(raw, fallbackStatement),
    unit: pickString(raw, ["unit", "value_unit", "valueUnit"]),
    groupKey: pickGroupKey(raw),
    groupName: pickGroupName(raw),
    points: normalizePoints(series).slice(-10),
  };
}

function normalizeAccounts(payload: unknown, code: FinancialStatementCode) {
  if (Array.isArray(payload)) {
    return flattenAccountItems(payload).map((item, index) => normalizeAccount(item, code, `${code}_${index + 1}`));
  }

  if (!isRecord(payload)) {
    return [];
  }

  const accountArray = pickArray(payload, accountListKeys);
  if (accountArray) {
    return flattenAccountItems(accountArray).map((item, index) => normalizeAccount(item, code, `${code}_${index + 1}`));
  }

  const groupArray = pickArray(payload, groupListKeys);
  if (groupArray) {
    return flattenAccountItems(groupArray).map((item, index) => normalizeAccount(item, code, `${code}_${index + 1}`));
  }

  return Object.entries(payload)
    .filter(([, value]) => Array.isArray(value) || isRecord(value))
    .map(([key, value]) => normalizeAccount(value, code, key));
}

function getStatementPayload(json: unknown, code: FinancialStatementCode) {
  if (!isRecord(json)) {
    return undefined;
  }

  if (json[code]) {
    return json[code];
  }

  const statements = json.statements;
  if (isRecord(statements) && statements[code]) {
    return statements[code];
  }

  if (Array.isArray(statements)) {
    return statements.find((item) => isRecord(item) && inferStatement(item, code) === code);
  }

  const sections = json.sections;
  if (isRecord(sections) && sections[code]) {
    return sections[code];
  }

  if (Array.isArray(sections)) {
    return sections.find((item) => isRecord(item) && inferStatement(item, code) === code);
  }

  const groups = json.groups;
  if (Array.isArray(groups)) {
    const matches = groups.filter((item) => isRecord(item) && readGroupStatementCode(item) === code);
    return matches.length > 0 ? { groups: matches } : undefined;
  }

  if (isRecord(groups)) {
    if (groups[code]) {
      return groups[code];
    }

    const matches = Object.entries(groups)
      .filter(([key, value]) => statementCodeFromText(key) === code || (isRecord(value) && readGroupStatementCode(value) === code))
      .map(([, value]) => value);
    return matches.length > 0 ? { groups: matches } : undefined;
  }

  return undefined;
}

function getRatioPayload(json: unknown) {
  if (!isRecord(json)) {
    return json;
  }

  for (const key of ratioPayloadKeys) {
    if (json[key]) {
      return json[key];
    }
  }

  return json;
}

function normalizeFlatRows(json: unknown) {
  if (!isRecord(json)) {
    return [];
  }

  const rows = pickArray(json, ["rows", "accounts", "items"]);
  if (!rows) {
    return [];
  }

  return statementCodes.map((code) => ({
    code,
    accounts: rows
      .map((row, index) => normalizeAccount(row, code, `${code}_${index + 1}`))
      .filter((account) => account.statement === code && account.points.length > 0),
  }));
}

export function normalizeFinancialStatements(json: unknown): FinancialStatementData[] {
  const normalized = statementCodes.map((code) => ({
    code,
    accounts: normalizeAccounts(getStatementPayload(json, code), code).filter(
      (account) => account.points.length > 0,
    ),
  }));

  if (normalized.some((statement) => statement.accounts.length > 0)) {
    return normalized;
  }

  return normalizeFlatRows(json);
}

export function normalizeFinancialRatios(json: unknown): FinancialStatementData[] {
  const payload = getRatioPayload(json);
  return normalizeFinancialStatements(Array.isArray(payload) ? { groups: payload } : payload);
}

export function normalizeFinancialAccount(
  json: unknown,
  canonicalId: string,
  fallbackStatement: FinancialStatementCode = "IS",
): FinancialAccountSeries {
  if (isRecord(json) && json.account) {
    return normalizeAccount(json.account, inferStatement(json, fallbackStatement), canonicalId);
  }

  return normalizeAccount(json, fallbackStatement, canonicalId);
}

export async function fetchFinancialStatements(
  stockCode: string,
  period: FinancialApiPeriod,
): Promise<FinancialStatementData[]> {
  const response = await fetch(
    apiPath(
      `/api/financials/${encodeURIComponent(stockCode)}?period=${encodeURIComponent(period)}&statement=all`,
    ),
  );

  if (!response.ok) {
    throw new Error("재무제표 데이터를 불러오지 못했습니다.");
  }

  return normalizeFinancialStatements(await readJson(response));
}

export async function fetchFinancialRatios(
  stockCode: string,
  period: Extract<FinancialApiPeriod, "annual" | "quarter">,
): Promise<FinancialStatementData[]> {
  const response = await fetch(
    apiPath(
      `/api/financials/${encodeURIComponent(stockCode)}/ratios?period=${encodeURIComponent(period)}`,
    ),
  );

  if (!response.ok) {
    throw new Error("재무비율 데이터를 불러오지 못했습니다.");
  }

  return normalizeFinancialRatios(await readJson(response));
}

export async function fetchFinancialAccount(
  stockCode: string,
  canonicalId: string,
  period: FinancialApiPeriod,
): Promise<FinancialAccountSeries> {
  const response = await fetch(
    apiPath(
      `/api/financials/${encodeURIComponent(stockCode)}/accounts/${encodeURIComponent(
        canonicalId,
      )}?period=${encodeURIComponent(period)}`,
    ),
  );

  if (!response.ok) {
    throw new Error("계정과목 상세 데이터를 불러오지 못했습니다.");
  }

  return normalizeFinancialAccount(await readJson(response), canonicalId);
}
