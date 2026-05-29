import type {
  IndustryAnalysisRow,
  IndustryMarket,
  SectorLeadersResponse,
} from "../types/industryAnalysis";

const apiBaseUrl = ((import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "").replace(/\/+$/, "");
const apiPath = (path: string) => `${apiBaseUrl}${path}`;

const rowListKeys = ["rows", "data", "items", "results", "sectors", "sector_leaders", "sectorLeaders"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "" || value === "N/A") {
    return null;
  }

  if (isRecord(value)) {
    return pickNumber(value, ["value", "displayValue", "display_value"]);
  }

  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }

  if (typeof value === "string") {
    const normalized = value.replace(/,/g, "").replace(/%$/, "").replace(/x$/i, "").trim();
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

function pickPercent(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const rawValue = record[key];
    const value = toNumber(rawValue);

    if (value !== null) {
      return isRecord(rawValue) || (typeof rawValue === "string" && rawValue.includes("%"))
        ? value
        : normalizePercent(value);
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

function normalizePercent(value: number | null) {
  if (value === null) {
    return null;
  }

  return Math.abs(value) <= 1 ? value * 100 : value;
}

function normalizeSectorLeaderRow(raw: unknown): IndustryAnalysisRow | null {
  if (!isRecord(raw)) {
    return null;
  }

  const industryName = pickString(raw, [
    "industryName",
    "industry_name",
    "sectorName",
    "sector_name",
    "name",
    "label",
  ]);

  if (!industryName) {
    return null;
  }

  return {
    industryName,
    stockCount:
      pickNumber(raw, ["stockCount", "stock_count", "totalStocks", "total_stocks", "count"]) ?? 0,
    strongStockCount:
      pickNumber(raw, [
        "strongStockCount",
        "strong_stock_count",
        "newHighCount",
        "new_high_count",
        "nearHighCount",
        "near_high_count",
        "momentumCount",
        "momentum_count",
      ]) ?? 0,
    strongStockRatio: pickPercent(raw, [
      "strongStockRatio",
      "strong_stock_ratio",
      "newHighRatio",
      "new_high_ratio",
      "nearHighRatio",
      "near_high_ratio",
      "momentumRatio",
      "momentum_ratio",
    ]),
    expectedEpsGrowth: pickPercent(raw, [
      "expectedEpsGrowth",
      "expected_eps_growth",
      "epsExpectedGrowth",
      "eps_expected_growth",
      "expectedEpsGrowthRate",
      "expected_eps_growth_rate",
      "epsGrowth",
      "eps_growth",
      "epsGrowthRate",
      "eps_growth_rate",
    ]),
    dailyReturn: pickPercent(raw, [
      "dailyReturn",
      "daily_return",
      "oneDayReturn",
      "one_day_return",
      "return1d",
      "return_1d",
      "return1D",
    ]),
    weeklyReturn: pickPercent(raw, [
      "weeklyReturn",
      "weekly_return",
      "oneWeekReturn",
      "one_week_return",
      "return1w",
      "return_1w",
      "return1W",
    ]),
    roe: pickPercent(raw, ["roe", "returnOnEquity", "return_on_equity"]),
    per: pickNumber(raw, ["per", "avgPer", "avg_per", "averagePer", "average_per"]),
    pbr: pickNumber(raw, ["pbr", "avgPbr", "avg_pbr", "averagePbr", "average_pbr"]),
  };
}

function normalizeMarket(value: unknown, fallback: IndustryMarket): IndustryMarket {
  return typeof value === "string" && value.toUpperCase() === "US" ? "US" : fallback;
}

function normalizeSectorLeadersPayload(
  payload: unknown,
  fallbackMarket: IndustryMarket,
): SectorLeadersResponse {
  const rowsPayload = Array.isArray(payload)
    ? payload
    : isRecord(payload)
      ? pickArray(payload, rowListKeys) ?? []
      : [];

  const asOfDate = isRecord(payload)
    ? (pickString(payload, ["asOfDate", "as_of_date", "date", "tradeDate", "trade_date"]) ?? null)
    : null;

  return {
    asOfDate,
    market: isRecord(payload) ? normalizeMarket(payload.market, fallbackMarket) : fallbackMarket,
    rows: rowsPayload
      .map(normalizeSectorLeaderRow)
      .filter((row): row is IndustryAnalysisRow => row !== null),
  };
}

async function readJson(response: Response) {
  const contentType = response.headers.get("content-type") ?? "";

  if (!contentType.includes("json")) {
    throw new Error("산업 분석 API가 JSON을 반환하지 않았습니다.");
  }

  return response.json() as Promise<unknown>;
}

export async function fetchSectorLeaders(
  market: IndustryMarket = "KR",
): Promise<SectorLeadersResponse> {
  const searchParams = new URLSearchParams({ market });
  const response = await fetch(apiPath(`/api/sector-leaders?${searchParams.toString()}`));

  if (!response.ok) {
    throw new Error("산업 분석 데이터를 불러오지 못했습니다.");
  }

  return normalizeSectorLeadersPayload(await readJson(response), market);
}
