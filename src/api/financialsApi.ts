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
  points: FinancialPoint[];
};

export type FinancialStatementData = {
  code: FinancialStatementCode;
  accounts: FinancialAccountSeries[];
};

const apiBaseUrl = ((import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "").replace(/\/+$/, "");
const apiPath = (path: string) => `${apiBaseUrl}${path}`;

const statementCodes: FinancialStatementCode[] = ["IS", "BS", "CF"];
const accountListKeys = ["accounts", "rows", "items", "line_items", "lineItems", "data"];
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
    const pointArray = pickArray(raw, pointListKeys);
    if (pointArray) {
      return pointArray.map((point, index) => normalizePoint(point, String(index + 1)));
    }

    return normalizePointMap(raw);
  }

  return [];
}

function inferStatement(raw: Record<string, unknown>, fallback: FinancialStatementCode) {
  const value = pickString(raw, [
    "statement",
    "statement_type",
    "statementType",
    "statement_code",
    "statementCode",
    "fs_div",
  ]);
  if (value === "IS" || value === "BS" || value === "CF") {
    return value;
  }

  if (/income/i.test(value ?? "")) {
    return "IS";
  }
  if (/balance/i.test(value ?? "")) {
    return "BS";
  }
  if (/cash/i.test(value ?? "")) {
    return "CF";
  }

  return fallback;
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
    pickString(raw, ["canonical_id", "canonicalId", "account_id", "accountId", "id"]) ?? fallbackId;
  const name =
    pickString(raw, ["account_name", "accountName", "name", "label", "title", "ko_name", "koName"]) ??
    canonicalId;
  const series = pickArray(raw, pointListKeys) ?? raw;

  return {
    canonicalId,
    name,
    statement: inferStatement(raw, fallbackStatement),
    points: normalizePoints(series).slice(-10),
  };
}

function normalizeAccounts(payload: unknown, code: FinancialStatementCode) {
  if (Array.isArray(payload)) {
    return payload.map((item, index) => normalizeAccount(item, code, `${code}_${index + 1}`));
  }

  if (!isRecord(payload)) {
    return [];
  }

  const accountArray = pickArray(payload, accountListKeys);
  if (accountArray) {
    return accountArray.map((item, index) => normalizeAccount(item, code, `${code}_${index + 1}`));
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

  return undefined;
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
