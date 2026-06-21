const apiBaseUrl = ((import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "").replace(/\/+$/, "");
const apiPath = (path: string) => `${apiBaseUrl}${path}`;

export type ApiFactorDirection = "lower" | "higher";
export type ApiPriceModel = "multiple" | "yield" | "compareOnly";

export type MultipleValuationApiFactor = {
  id: string;
  label?: string;
  description?: string;
  current?: number | null;
  benchmark?: number | null;
  historyMedian?: number | null;
  historyP75?: number | null;
  industryAvg?: number | null;
  marketAvg?: number | null;
  nasdaqAvg?: number | null;
  buyPrice?: number | null;
  fairPrice?: number | null;
  sellPrice?: number | null;
  targetSource?: string;
  direction?: ApiFactorDirection;
  priceModel?: ApiPriceModel;
  history?: number[];
};

export type MultipleValuationApiComparisonRow = {
  label: string;
  values: Record<string, number | null>;
};

export type MultipleValuationApiCentralBand = {
  fairPrice?: number | null;
  buyPrice?: number | null;
  sellPrice?: number | null;
  validFactorCount?: number | null;
  excludedFactorIds?: string[];
};

export type MultipleValuationApiResponse = {
  stockCode: string;
  stockName?: string;
  industryLabel?: string;
  asOfDate?: string;
  currentPrice?: number | null;
  factors: MultipleValuationApiFactor[];
  comparisonRows: MultipleValuationApiComparisonRow[];
  centralBand?: MultipleValuationApiCentralBand | null;
};

const factorListKeys = [
  "factors",
  "rows",
  "items",
  "results",
  "multiples",
  "multiple_factors",
  "multipleFactors",
  "bands",
  "factor_bands",
  "factorBands",
  "multiple_bands",
  "multipleBands",
  "valuation_bands",
  "valuationBands",
  "core_multiples",
  "coreMultiples",
  "factor_comparisons",
  "factorComparisons",
  "multiple_comparisons",
  "multipleComparisons",
  "metrics",
];
const comparisonListKeys = [
  "comparisonRows",
  "comparison_rows",
  "comparisons",
  "market_comparisons",
  "marketComparisons",
  "benchmarks",
  "cross_section",
  "crossSection",
  "relative_comparisons",
  "relativeComparisons",
];
const nestedPayloadKeys = ["data", "result", "payload", "response", "valuation", "multipleBands", "multiple_bands"];

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "" || value === "N/A" || value === "-") {
    return null;
  }

  if (isRecord(value)) {
    return pickNumber(value, ["value", "raw", "rawValue", "raw_value"]);
  }

  if (typeof value === "number") {
    return Number.isFinite(value) ? value : null;
  }

  if (typeof value === "string") {
    const parsed = Number(value.replace(/,/g, "").replace(/%$/, "").replace(/x$/i, "").trim());
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

function pickMetricValue(
  record: Record<string, unknown>,
  keys: string[],
  fallback?: number | null,
) {
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(record, key)) {
      return metricNumber(record[key]);
    }
  }

  return fallback;
}

function recordValues(value: unknown) {
  if (!isRecord(value)) {
    return [];
  }

  return Object.entries(value).map(([entryKey, entryValue]) =>
    isRecord(entryValue) ? { __entryKey: entryKey, ...entryValue } : entryValue,
  );
}

function pickCollection(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = record[key];

    if (Array.isArray(value)) {
      return value;
    }

    if (isRecord(value)) {
      return recordValues(value);
    }
  }

  return undefined;
}

function pickRecord(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = record[key];

    if (isRecord(value)) {
      return value;
    }
  }

  return undefined;
}

function unwrapRecord(payload: unknown): Record<string, unknown> {
  let current = isRecord(payload) ? payload : {};

  for (let index = 0; index < 4; index += 1) {
    const next = pickRecord(current, nestedPayloadKeys);

    if (!next) {
      break;
    }

    current = next;
  }

  return current;
}

function findNestedArray(payload: unknown, keys: string[]): unknown[] {
  if (Array.isArray(payload)) {
    return payload;
  }

  if (!isRecord(payload)) {
    return [];
  }

  const direct = pickCollection(payload, keys);

  if (direct) {
    return direct;
  }

  for (const key of nestedPayloadKeys) {
    const nested = payload[key];
    const nestedArray = findNestedArray(nested, keys);

    if (nestedArray.length > 0) {
      return nestedArray;
    }
  }

  return [];
}

function normalizeFactorId(value: string) {
  return value
    .trim()
    .toLowerCase()
    .replace(/&/g, "")
    .replace(/\+/g, "")
    .replace(/[/-]+/g, "_")
    .replace(/\s+/g, "_")
    .replace(/__+/g, "_");
}

function normalizeDirection(value: unknown): ApiFactorDirection | undefined {
  if (typeof value !== "string") {
    return undefined;
  }

  const normalized = value.toLowerCase();

  if (normalized.includes("higher") || normalized.includes("high")) {
    return "higher";
  }

  if (normalized.includes("lower") || normalized.includes("low")) {
    return "lower";
  }

  return undefined;
}

function normalizePriceModel(value: unknown): ApiPriceModel | undefined {
  if (typeof value !== "string") {
    return undefined;
  }

  const normalized = value.toLowerCase();

  if (normalized.includes("yield")) {
    return "yield";
  }

  if (normalized.includes("compare")) {
    return "compareOnly";
  }

  if (normalized.includes("multiple")) {
    return "multiple";
  }

  return undefined;
}

function normalizeHistory(raw: unknown) {
  if (!Array.isArray(raw)) {
    return undefined;
  }

  const values = raw
    .map((item) => (isRecord(item) ? pickNumber(item, ["value", "multiple", "factor_value", "factorValue"]) : toNumber(item)))
    .filter((value): value is number => value !== null);

  return values.length > 0 ? values : undefined;
}

function metricNumber(value: unknown): number | null {
  return toNumber(value);
}

function pickBenchmarkValue(raw: Record<string, unknown>, keys: string[]) {
  const direct = pickNumber(raw, keys);

  if (direct !== null) {
    return direct;
  }

  const benchmark = raw.benchmark ?? raw.benchmarks;

  if (!isRecord(benchmark)) {
    return null;
  }

  return pickNumber(benchmark, keys);
}

function normalizeFactor(raw: unknown): MultipleValuationApiFactor | null {
  if (!isRecord(raw)) {
    return null;
  }

  const label = pickString(raw, ["label", "name", "factorName", "factor_name", "metric", "metric_name"]);
  const id =
    pickString(raw, ["id", "factorId", "factor_id", "key", "metricId", "metric_id", "__entryKey"]) ??
    (label ? normalizeFactorId(label) : undefined);

  if (!id) {
    return null;
  }

  return {
    id: normalizeFactorId(id),
    label,
    description: pickString(raw, ["description", "desc"]),
    current: pickNumber(raw, [
      "current",
      "currentValue",
      "current_value",
      "currentMultiple",
      "current_multiple",
      "current_metric",
      "currentMetric",
      "currentFactor",
      "current_factor",
      "value",
      "factorValue",
      "factor_value",
      "multiple",
    ]),
    benchmark: pickBenchmarkValue(raw, [
      "benchmark",
      "benchmarkValue",
      "benchmark_value",
      "targetMultiple",
      "target_multiple",
      "targetMetric",
      "target_metric",
      "referenceMultiple",
      "reference_multiple",
    ]),
    historyMedian: pickBenchmarkValue(raw, [
      "historyMedian",
      "history_median",
      "historicalMedian",
      "historical_median",
      "threeYearMedian",
      "three_year_median",
      "threeYearAvg",
      "three_year_avg",
      "threeYearAverage",
      "three_year_average",
      "avg3y",
      "avg_3y",
      "median3y",
      "median_3y",
    ]),
    historyP75: pickBenchmarkValue(raw, [
      "historyP75",
      "history_p75",
      "historicalP75",
      "historical_p75",
      "threeYearP75",
      "three_year_p75",
      "p75",
      "percentile75",
      "percentile_75",
    ]),
    industryAvg: pickBenchmarkValue(raw, [
      "industryAvg",
      "industry_avg",
      "industryAverage",
      "industry_average",
      "industryMean",
      "industry_mean",
      "peerAvg",
      "peer_avg",
      "peerAverage",
      "peer_average",
    ]),
    marketAvg: pickBenchmarkValue(raw, [
      "marketAvg",
      "market_avg",
      "marketAverage",
      "market_average",
      "marketMean",
      "market_mean",
      "usMarketAvg",
      "us_market_avg",
      "usMarketAverage",
      "us_market_average",
    ]),
    nasdaqAvg: pickBenchmarkValue(raw, [
      "nasdaqAvg",
      "nasdaq_avg",
      "nasdaq100Avg",
      "nasdaq_100_avg",
      "nasdaq100Average",
      "nasdaq_100_average",
      "indexAvg",
      "index_avg",
      "indexAverage",
      "index_average",
    ]),
    buyPrice: pickNumber(raw, [
      "buyPrice",
      "buy_price",
      "buyBand",
      "buy_band",
      "buyBandPrice",
      "buy_band_price",
      "marginOfSafetyPrice",
      "margin_of_safety_price",
    ]),
    fairPrice: pickNumber(raw, [
      "fairPrice",
      "fair_price",
      "fairValue",
      "fair_value",
      "fairValuePrice",
      "fair_value_price",
      "targetPrice",
      "target_price",
      "intrinsicValue",
      "intrinsic_value",
      "valuationPrice",
      "valuation_price",
    ]),
    sellPrice: pickNumber(raw, [
      "sellPrice",
      "sell_price",
      "sellBand",
      "sell_band",
      "sellBandPrice",
      "sell_band_price",
      "premiumPrice",
      "premium_price",
      "takeProfitPrice",
      "take_profit_price",
    ]),
    direction: normalizeDirection(raw.direction ?? raw.valueDirection ?? raw.value_direction),
    priceModel: normalizePriceModel(raw.priceModel ?? raw.price_model ?? raw.valuationModel ?? raw.valuation_model),
    history: normalizeHistory(raw.history ?? raw.historyValues ?? raw.history_values ?? raw.timeSeries ?? raw.time_series),
  };
}

function comparisonBenchmarkValue(
  comparison: MultipleValuationApiFactor,
  rawComparison: unknown,
  benchmarkKey: string,
) {
  if (!isRecord(rawComparison)) {
    return comparison;
  }

  const value = metricNumber(rawComparison.value);

  if (value === null) {
    return comparison;
  }

  if (benchmarkKey === "historical_median") {
    return { ...comparison, historyMedian: value };
  }

  if (benchmarkKey === "historical_avg" && comparison.historyMedian === undefined) {
    return { ...comparison, historyMedian: value };
  }

  if (benchmarkKey === "industry_avg") {
    return { ...comparison, industryAvg: value };
  }

  if (benchmarkKey === "market_avg") {
    return { ...comparison, marketAvg: value, nasdaqAvg: comparison.nasdaqAvg ?? value };
  }

  return comparison;
}

function normalizeFactorComparison(raw: unknown): MultipleValuationApiFactor | null {
  if (!isRecord(raw)) {
    return null;
  }

  const base = normalizeFactor(raw);

  if (!base) {
    return null;
  }

  let result: MultipleValuationApiFactor = {
    ...base,
    current: metricNumber(raw.current) ?? base.current,
  };

  const comparisons = Array.isArray(raw.comparisons) ? raw.comparisons : [];

  for (const comparison of comparisons) {
    if (!isRecord(comparison)) {
      continue;
    }

    const benchmarkKey = pickString(comparison, ["benchmark_key", "benchmarkKey", "key"]) ?? "";
    result = comparisonBenchmarkValue(result, comparison, benchmarkKey);
  }

  return result;
}

function normalizeBand(raw: unknown): MultipleValuationApiFactor | null {
  if (!isRecord(raw)) {
    return null;
  }

  const base = normalizeFactor(raw);

  if (!base) {
    return null;
  }

  return {
    ...base,
    current: pickMetricValue(raw, ["current_multiple", "currentMultiple"], base.current),
    benchmark: pickMetricValue(raw, ["target_multiple", "targetMultiple"], base.benchmark),
    buyPrice: pickMetricValue(
      raw,
      ["buy_below_price", "buyBelowPrice", "buy_price"],
      base.buyPrice,
    ),
    fairPrice: pickMetricValue(
      raw,
      ["fair_price", "fairPrice", "target_price"],
      base.fairPrice,
    ),
    sellPrice: pickMetricValue(
      raw,
      ["sell_above_price", "sellAbovePrice", "sell_price"],
      base.sellPrice,
    ),
    targetSource:
      pickString(raw, ["target_source", "targetSource", "benchmark_key", "benchmarkKey"]) ??
      base.targetSource,
  };
}

function normalizeCentralBand(raw: unknown): MultipleValuationApiCentralBand | null {
  if (!isRecord(raw)) {
    return null;
  }

  const excluded = raw.excluded_factor_ids ?? raw.excludedFactorIds;

  return {
    fairPrice: pickMetricValue(raw, ["fair_price", "fairPrice", "target_price"]),
    buyPrice: pickMetricValue(raw, ["buy_below_price", "buyBelowPrice", "buy_price"]),
    sellPrice: pickMetricValue(raw, ["sell_above_price", "sellAbovePrice", "sell_price"]),
    validFactorCount: pickNumber(raw, ["valid_factor_count", "validFactorCount"]),
    excludedFactorIds: Array.isArray(excluded)
      ? excluded.filter((value): value is string => typeof value === "string")
      : undefined,
  };
}

function normalizeHistoryFactors(rawItems: unknown[]): MultipleValuationApiFactor[] {
  const grouped = new Map<string, number[]>();

  for (const item of rawItems) {
    if (!isRecord(item)) {
      continue;
    }

    const rawId = pickString(item, ["factor_id", "factorId", "id", "key", "metric_id", "metricId"]);
    const value = metricNumber(item.value ?? item.factor_value ?? item.factorValue);

    if (!rawId || value === null) {
      continue;
    }

    const id = normalizeFactorId(rawId);
    const values = grouped.get(id) ?? [];
    values.push(value);
    grouped.set(id, values);
  }

  return [...grouped.entries()].map(([id, history]) => ({ id, history }));
}

function mergeApiFactors(
  factors: MultipleValuationApiFactor[],
  nextFactors: MultipleValuationApiFactor[],
) {
  const byId = new Map(factors.map((factor) => [normalizeFactorId(factor.id), factor]));

  for (const factor of nextFactors) {
    const id = normalizeFactorId(factor.id);
    const existing = byId.get(id);

    if (!existing) {
      byId.set(id, factor);
      continue;
    }

    const merged: MultipleValuationApiFactor = { ...existing };

    for (const [key, value] of Object.entries(factor) as Array<
      [keyof MultipleValuationApiFactor, MultipleValuationApiFactor[keyof MultipleValuationApiFactor]]
    >) {
      if (value !== undefined && (value !== null || merged[key] === undefined)) {
        (merged as Record<string, unknown>)[key] = value;
      }
    }

    byId.set(id, merged);
  }

  return [...byId.values()];
}

function normalizeComparisonRow(raw: unknown): MultipleValuationApiComparisonRow | null {
  if (!isRecord(raw)) {
    return null;
  }

  const label = pickString(raw, ["label", "name", "market", "benchmark", "index", "indexName", "index_name", "__entryKey"]);

  if (!label) {
    return null;
  }

  const nestedValues = isRecord(raw.values) ? raw.values : raw;
  const values: Record<string, number | null> = {};

  for (const key of ["per", "pbr", "psr", "ev_ebitda", "evEbitda", "fcf_ev_yield", "fcfEvYield", "peg"]) {
    const value = toNumber(nestedValues[key]);

    if (value !== null) {
      values[normalizeFactorId(key)] = value;
    }
  }

  return { label, values };
}

function normalizePayload(payload: unknown, stockCode: string): MultipleValuationApiResponse {
  const record = unwrapRecord(payload);
  const factorsPayload = findNestedArray(payload, factorListKeys);
  const factorComparisonPayload = findNestedArray(payload, ["comparisons"]);
  const bandPayload = findNestedArray(payload, ["bands"]);
  const historyPayload = findNestedArray(payload, ["history", "time_series", "timeSeries"]);
  const comparisonPayload = findNestedArray(payload, comparisonListKeys);
  const normalizedFactors = factorsPayload
    .map(normalizeFactor)
    .filter((factor): factor is MultipleValuationApiFactor => factor !== null);
  const normalizedComparisons = factorComparisonPayload
    .map(normalizeFactorComparison)
    .filter((factor): factor is MultipleValuationApiFactor => factor !== null);
  const normalizedBands = bandPayload
    .map(normalizeBand)
    .filter((factor): factor is MultipleValuationApiFactor => factor !== null);
  const normalizedHistory = normalizeHistoryFactors(historyPayload);

  return {
    stockCode:
      pickString(record, ["stockCode", "stock_code", "ticker", "securityId", "security_id"]) ?? stockCode,
    stockName: pickString(record, ["stockName", "stock_name", "name", "companyName", "company_name"]),
    industryLabel: pickString(record, ["industryLabel", "industry_label", "industry", "sector", "peerGroup", "peer_group"]),
    asOfDate: pickString(record, ["asOfDate", "as_of_date", "date", "tradeDate", "trade_date"]),
    currentPrice:
      metricNumber(record.current_price) ??
      metricNumber(record.currentPrice) ??
      pickNumber(record, ["latestPrice", "latest_price", "close", "latestClose", "latest_close"]),
    factors: mergeApiFactors(
      mergeApiFactors(
        mergeApiFactors(normalizedFactors, normalizedComparisons),
        normalizedBands,
      ),
      normalizedHistory,
    ),
    comparisonRows: comparisonPayload
      .map(normalizeComparisonRow)
      .filter((row): row is MultipleValuationApiComparisonRow => row !== null),
    centralBand: normalizeCentralBand(record.central_band ?? record.centralBand),
  };
}

async function readJson(response: Response) {
  const contentType = response.headers.get("content-type") ?? "";

  if (!contentType.includes("json")) {
    throw new Error("멀티플 밴드 API가 JSON을 반환하지 않았습니다.");
  }

  return response.json() as Promise<unknown>;
}

export async function fetchMultipleValuationBands(
  stockCode: string,
  options: { bandBasis?: "historical" | "industry" | "market" | "blend" } = {},
) {
  const searchParams = new URLSearchParams();

  if (options.bandBasis) {
    searchParams.set("band_basis", options.bandBasis);
  }

  const query = searchParams.toString();
  const response = await fetch(
    apiPath(
      `/api/valuations/${encodeURIComponent(stockCode)}/multiple-bands${query ? `?${query}` : ""}`,
    ),
  );

  if (!response.ok) {
    throw new Error("멀티플 밴드 데이터를 불러오지 못했습니다.");
  }

  return normalizePayload(await readJson(response), stockCode);
}
