import type {
  StyleScoreFactor,
  StyleScoreGroup,
  StyleScoreStock,
  StyleScoresResult,
  StyleProfile,
} from "../types/styleScores";

const apiBaseUrl = ((import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "").replace(/\/+$/, "");
const apiPath = (path: string) => `${apiBaseUrl}${path}`;

const listKeys = ["rows", "data", "items", "results", "style_scores", "styleScores"];
const componentListKeys = ["components", "rows", "data", "items", "results", "scores", "style_scores", "styleScores"];
const factorListKeys = ["factors", "rows", "data", "items", "results", "breakdown", "details"];

const defaultStyleProfile: StyleProfile = "DEFAULT";

function buildStyleProfileQuery(styleProfile: StyleProfile = defaultStyleProfile) {
  return `style_profile=${encodeURIComponent(styleProfile)}`;
}

type ComponentDefinition = {
  id: string;
  componentKey: string;
  label: string;
  aliases: string[];
};

const componentDefinitions: ComponentDefinition[] = [
  {
    id: "composite",
    componentKey: "COMPOSITE",
    label: "Composite",
    aliases: ["COMPOSITE", "Composite", "composite", "composite_score", "total_score", "score"],
  },
  {
    id: "value",
    componentKey: "VALUE",
    label: "Value",
    aliases: ["VALUE", "Value", "value", "value_score", "UNDERVALUED", "undervalued"],
  },
  {
    id: "quality",
    componentKey: "QUALITY",
    label: "Quality",
    aliases: ["QUALITY", "Quality", "quality", "quality_score", "PROFITABILITY", "profitability"],
  },
  {
    id: "growth",
    componentKey: "GROWTH",
    label: "Growth",
    aliases: ["GROWTH", "Growth", "growth", "growth_score"],
  },
  {
    id: "momentum",
    componentKey: "MOMENTUM",
    label: "Momentum",
    aliases: ["MOMENTUM", "Momentum", "momentum", "momentum_score"],
  },
  {
    id: "risk",
    componentKey: "RISK",
    label: "Risk",
    aliases: ["RISK", "Risk", "risk", "risk_score", "FINANCIAL_STABILITY", "financial_stability", "stability"],
  },
  {
    id: "dividend",
    componentKey: "DIVIDEND",
    label: "Dividend",
    aliases: ["DIVIDEND", "Dividend", "dividend", "dividend_score", "SHAREHOLDER_RETURN_RATE", "shareholder_return_rate"],
  },
];

const factorTemplates: Record<string, StyleScoreFactor[]> = {
  COMPOSITE: [
    createMockFactor("VALUE", "Value", 63.2, 63.2, 63.2, 0.42, 0.25, 15.8, 42, "INDUSTRY_GROUP"),
    createMockFactor("QUALITY", "Quality", 71.8, 71.8, 71.8, 0.76, 0.25, 17.95, 42, "INDUSTRY_GROUP"),
    createMockFactor("GROWTH", "Growth", 58.4, 58.4, 58.4, 0.19, 0.2, 11.68, 42, "INDUSTRY_GROUP"),
    createMockFactor("MOMENTUM", "Momentum", 82.1, 82.1, 82.1, 1.12, 0.2, 16.42, 42, "INDUSTRY_GROUP"),
    createMockFactor("RISK", "Risk", 76.5, 76.5, 76.5, 0.91, 0.1, 7.65, 42, "INDUSTRY_GROUP"),
  ],
  VALUE: [
    createMockFactor("EARNINGS_YIELD", "Earnings Yield", 0.052, 0.052, 61.3, 0.34, 0.25, 15.33, 42, "INDUSTRY_GROUP"),
    createMockFactor("EBITDA_YIELD", "EBITDA Yield", 0.071, 0.071, 66.4, 0.48, 0.2, 13.28, 42, "INDUSTRY_GROUP"),
    createMockFactor("FCF_YIELD", "FCF Yield", 0.041, 0.041, 55.8, 0.12, 0.25, 13.95, 42, "INDUSTRY_GROUP"),
    createMockFactor("BOOK_TO_MARKET", "Book to Market", 0.29, 0.29, 65.1, 0.51, 0.2, 13.02, 42, "INDUSTRY_GROUP"),
    createMockFactor("SALES_YIELD", "Sales Yield", 0.73, 0.73, 56.2, 0.16, 0.1, 5.62, 42, "INDUSTRY_GROUP"),
  ],
  QUALITY: [
    createMockFactor("ROE", "ROE", 0.128, 0.128, 73.5, 0.83, 0.2, 14.7, 42, "INDUSTRY_GROUP"),
    createMockFactor("ROIC", "ROIC", 0.116, 0.116, 76.1, 0.91, 0.2, 15.22, 42, "INDUSTRY_GROUP"),
    createMockFactor("OPERATING_MARGIN", "Operating Margin", 0.207, 0.207, 78.2, 0.97, 0.15, 11.73, 42, "INDUSTRY_GROUP"),
    createMockFactor("CFO_TO_NET_INCOME", "CFO / Net Income", 1.18, 1.18, 70.4, 0.66, 0.15, 10.56, 42, "INDUSTRY_GROUP"),
    createMockFactor("ASSET_TURNOVER", "Asset Turnover", 0.44, 0.44, 62.5, 0.24, 0.1, 6.25, 42, "INDUSTRY_GROUP"),
    createMockFactor("DEBT_TO_EQUITY", "Debt / Equity", 0.31, 0.31, 82.4, 1.08, 0.1, 8.24, 42, "INDUSTRY_GROUP"),
    createMockFactor("FCF_TO_NET_INCOME", "FCF / Net Income", 1.06, 1.06, 69.8, 0.62, 0.1, 6.98, 42, "INDUSTRY_GROUP"),
  ],
  GROWTH: [
    createMockFactor("SALES_GROWTH_YOY", "Sales Growth YoY", 0.034, 0.034, 47.2, -0.09, 0.25, 11.8, 42, "INDUSTRY_GROUP"),
    createMockFactor("SALES_CAGR_3Y", "Sales CAGR 3Y", 0.085, 0.085, 59.6, 0.22, 0.2, 11.92, 42, "INDUSTRY_GROUP"),
    createMockFactor("OPERATING_PROFIT_GROWTH_YOY", "Operating Profit Growth YoY", 0.064, 0.064, 54.4, 0.11, 0.25, 13.6, 42, "INDUSTRY_GROUP"),
    createMockFactor("EPS_GROWTH_YOY", "EPS Growth YoY", -0.024, -0.024, 41.8, -0.31, 0.15, 6.27, 42, "INDUSTRY_GROUP"),
    createMockFactor("CFO_GROWTH_YOY", "CFO Growth YoY", 0.047, 0.047, 50.7, 0.02, 0.15, 7.61, 42, "INDUSTRY_GROUP"),
  ],
  MOMENTUM: [
    createMockFactor("MOM_12M_1M", "Momentum 12M ex 1M", 0.42, 0.42, 88.2, 1.45, 0.35, 30.87, 42, "INDUSTRY_GROUP"),
    createMockFactor("MOM_6M", "Momentum 6M", 0.21, 0.21, 82.1, 1.12, 0.25, 20.53, 42, "INDUSTRY_GROUP"),
    createMockFactor("MOM_3M", "Momentum 3M", 0.08, 0.08, 70.4, 0.63, 0.15, 10.56, 42, "INDUSTRY_GROUP"),
    createMockFactor("HIGH_52W_PROXIMITY", "52W High Proximity", 0.91, 0.91, 83.5, 1.18, 0.15, 12.53, 42, "INDUSTRY_GROUP"),
    createMockFactor("VOLUME_ACCELERATION", "Volume Acceleration", 1.14, 1.14, 76.2, 0.89, 0.1, 7.62, 42, "INDUSTRY_GROUP"),
  ],
  RISK: [
    createMockFactor("DEBT_TO_EQUITY", "Debt / Equity", 0.31, 0.31, 82.4, 1.08, 0.2, 16.48, 42, "INDUSTRY_GROUP"),
    createMockFactor("NET_DEBT_TO_EBITDA", "Net Debt / EBITDA", 0.71, 0.71, 78.3, 0.96, 0.2, 15.66, 42, "INDUSTRY_GROUP"),
    createMockFactor("INTEREST_COVERAGE", "Interest Coverage", 31.22, 31.22, 85.9, 1.24, 0.2, 17.18, 42, "INDUSTRY_GROUP"),
    createMockFactor("VOLATILITY_1Y", "Volatility 1Y", 0.19, 0.19, 70.4, 0.53, 0.2, 14.08, 42, "INDUSTRY_GROUP"),
    createMockFactor("MDD_1Y", "MDD 1Y", -0.147, -0.147, 65.5, 0.37, 0.2, 13.1, 42, "INDUSTRY_GROUP"),
  ],
  DIVIDEND: [
    createMockFactor("DIVIDEND_YIELD", "Dividend Yield", 0.011, 0.011, 37.3, -0.42, 0.4, 14.92, 42, "INDUSTRY_GROUP"),
    createMockFactor("PAYOUT_RATIO", "Payout Ratio", 0.284, 0.284, 44.8, -0.12, 0.25, 11.2, 42, "INDUSTRY_GROUP"),
    createMockFactor("BUYBACK_YIELD", "Buyback Yield", 0.006, 0.006, 52.4, 0.04, 0.2, 10.48, 42, "INDUSTRY_GROUP"),
    createMockFactor("DIVIDEND_GROWTH_3Y", "Dividend Growth 3Y", 0.063, 0.063, 55.9, 0.16, 0.15, 8.39, 42, "INDUSTRY_GROUP"),
  ],
};

const mockRows: StyleScoreStock[] = [
  createMockStock("FR-DSY", "DSY", "DASSAULT SYSTEMES SE", "FR", "2026-05-24", [24.9, 25.5, 83.1, 45.2, 21.8, 91.1, 47.0]),
  createMockStock("KR-005930", "005930", "Samsung Electronics", "KR", "2026-05-24", [70.4, 63.2, 71.8, 58.4, 82.1, 76.5, 48.6]),
  createMockStock("KR-035420", "035420", "NAVER", "KR", "2026-05-24", [64.8, 42.8, 69.3, 73.5, 68.1, 72.2, 32.5]),
];

function createMockFactor(
  factorId: string,
  label: string,
  rawValue: number,
  winsorizedValue: number,
  percentileScore: number,
  robustZScore: number,
  weight: number,
  weightedScore: number,
  peerCount: number,
  fallbackLevel: string,
): StyleScoreFactor {
  return {
    factorId,
    label,
    rawValue,
    winsorizedValue,
    percentileScore,
    robustZScore,
    weight,
    weightedScore,
    peerCount,
    fallbackLevel,
    fallbackCode: "4530",
  };
}

function createMockStock(
  securityId: string,
  ticker: string,
  name: string,
  country: string,
  asOfDate: string,
  scores: number[],
): StyleScoreStock {
  const groups = componentDefinitions.map((definition, index) => ({
    id: definition.id,
    componentKey: definition.componentKey,
    label: definition.label,
    score: scores[index] ?? null,
    scoreConfidence: definition.id === "dividend" ? 0.62 : 0.86,
    availableFactorCount: factorTemplates[definition.componentKey]?.length ?? 0,
    requiredFactorCount: factorTemplates[definition.componentKey]?.length ?? 0,
    weight: definition.id === "composite" ? 1 : [0.25, 0.25, 0.2, 0.2, 0.1, 0.05][Math.max(index - 1, 0)] ?? null,
    factors: factorTemplates[definition.componentKey] ?? [],
  }));

  return {
    securityId,
    ticker,
    name,
    country,
    styleProfile: defaultStyleProfile,
    asOfDate,
    compositeScore: scores[0] ?? null,
    groups,
  };
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function toNumber(value: unknown): number | null {
  if (value === null || value === undefined || value === "" || value === "N/A") {
    return null;
  }

  if (isRecord(value)) {
    return pickNumber(value, ["value", "score", "displayValue", "display_value"]);
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

function pickArray(record: Record<string, unknown>, keys: string[]) {
  for (const key of keys) {
    const value = record[key];

    if (Array.isArray(value)) {
      return value;
    }
  }

  return undefined;
}

function unwrapRows(payload: unknown, keys: string[]) {
  if (Array.isArray(payload)) {
    return payload;
  }

  if (!isRecord(payload)) {
    return [];
  }

  return pickArray(payload, keys) ?? [];
}

function canonicalKey(value: string) {
  return value.replace(/[_\-\s]+/g, "").toLowerCase();
}

function scoreSafe(value: number | null) {
  return value === null ? null : Math.max(0, Math.min(100, Number(value.toFixed(1))));
}

function normalizeSecurityId(value: string | undefined, fallback: string) {
  return (value ?? fallback).trim().replace(/\s+/g, "");
}

function matchDefinition(value: string | undefined, fallbackIndex = 0) {
  const normalized = value ? canonicalKey(value) : "";
  return (
    componentDefinitions.find((definition) =>
      definition.aliases.some((alias) => canonicalKey(alias) === normalized),
    ) ?? componentDefinitions[fallbackIndex]
  );
}

function normalizeFactor(raw: unknown, index: number): StyleScoreFactor | null {
  if (!isRecord(raw)) {
    return null;
  }

  const factorId =
    pickString(raw, ["factorId", "factor_id", "metricId", "metric_id", "id", "key"]) ?? `factor_${index + 1}`;
  const percentileScore = pickNumber(raw, [
    "percentileScore",
    "percentile_score",
    "score",
    "factorScore",
    "factor_score",
  ]);
  const weight = pickNumber(raw, ["weight", "factorWeight", "factor_weight"]) ?? 0;

  return {
    factorId,
    label:
      pickString(raw, ["displayLabel", "display_label", "label", "name", "factorName", "factor_name"]) ?? factorId,
    rawValue: pickNumber(raw, ["rawValue", "raw_value", "factorValue", "factor_value", "value"]),
    winsorizedValue: pickNumber(raw, ["winsorizedValue", "winsorized_value"]),
    percentileScore: scoreSafe(percentileScore),
    robustZScore: pickNumber(raw, ["robustZScore", "robust_z_score", "robustZ", "robust_z"]),
    weight,
    weightedScore:
      pickNumber(raw, ["weightedScore", "weighted_score", "contribution", "weightedContribution"]) ??
      (percentileScore === null ? null : Number((percentileScore * weight).toFixed(2))),
    peerCount: pickNumber(raw, ["peerCount", "peer_count", "nPeers", "n_peers"]),
    fallbackLevel: pickString(raw, ["fallbackLevel", "fallback_level", "industryLevel", "industry_level"]) ?? null,
    fallbackCode: pickString(raw, ["fallbackCode", "fallback_code", "industryCode", "industry_code"]) ?? null,
  };
}

function normalizeGroup(raw: unknown, index: number): StyleScoreGroup | null {
  if (!isRecord(raw)) {
    return null;
  }

  const rawKey = pickString(raw, ["componentKey", "component_key", "styleGroup", "style_group", "group", "key", "id"]);
  const definition = matchDefinition(rawKey, index);

  if (!definition) {
    return null;
  }

  const factors = unwrapRows(raw, factorListKeys)
    .map(normalizeFactor)
    .filter((factor): factor is StyleScoreFactor => factor !== null);

  return {
    id: definition.id,
    componentKey: definition.componentKey,
    label: pickString(raw, ["displayLabel", "display_label", "label", "name"]) ?? definition.label,
    score: scoreSafe(pickNumber(raw, ["score", "value", "styleScore", "style_score", "totalScore", "total_score"])),
    scoreConfidence: pickNumber(raw, ["scoreConfidence", "score_confidence", "confidence"]),
    availableFactorCount: pickNumber(raw, ["availableFactorCount", "available_factor_count", "availableFactors", "available_factors"]),
    requiredFactorCount: pickNumber(raw, ["requiredFactorCount", "required_factor_count", "requiredFactors", "required_factors"]),
    weight: pickNumber(raw, ["weight", "componentWeight", "component_weight"]),
    factors: factors.length > 0 ? factors : factorTemplates[definition.componentKey] ?? [],
  };
}

function orderGroups(groups: StyleScoreGroup[]) {
  const byId = new Map(groups.map((group) => [group.id, group]));

  return componentDefinitions.map((definition) => {
    return (
      byId.get(definition.id) ?? {
        id: definition.id,
        componentKey: definition.componentKey,
        label: definition.label,
        score: null,
        scoreConfidence: null,
        availableFactorCount: null,
        requiredFactorCount: null,
        weight: null,
        factors: factorTemplates[definition.componentKey] ?? [],
      }
    );
  });
}

function groupsFromRecord(record: Record<string, unknown>) {
  const componentRows = unwrapRows(record, componentListKeys);
  return groupsFromRows(componentRows, record);
}

function groupsFromRows(componentRows: unknown[], fallbackRecord?: Record<string, unknown>) {
  const groups = componentRows
    .map(normalizeGroup)
    .filter((group): group is StyleScoreGroup => group !== null);

  if (groups.length > 0) {
    return orderGroups(groups);
  }

  const record = fallbackRecord ?? {};

  return orderGroups(
    componentDefinitions.map((definition) => ({
      id: definition.id,
      componentKey: definition.componentKey,
      label: definition.label,
      score: scoreSafe(pickNumber(record, definition.aliases)),
      scoreConfidence: pickNumber(record, [`${definition.id}Confidence`, `${definition.componentKey.toLowerCase()}_confidence`]),
      availableFactorCount: null,
      requiredFactorCount: null,
      weight: null,
      factors: factorTemplates[definition.componentKey] ?? [],
    })),
  );
}

function normalizeStock(raw: unknown, fallbackIndex: number): StyleScoreStock | null {
  if (!isRecord(raw)) {
    return null;
  }

  const securityId = normalizeSecurityId(
    pickString(raw, ["securityId", "security_id", "id", "ticker", "stockCode", "stock_code"]),
    `MOCK-${fallbackIndex + 1}`,
  );
  const groups = groupsFromRecord(raw);

  return {
    securityId,
    ticker: pickString(raw, ["ticker", "stockCode", "stock_code", "symbol"]) ?? securityId,
    name:
      pickString(raw, ["name", "stockName", "stock_name", "companyName", "company_name", "company_name_ko"]) ??
      securityId,
    country: pickString(raw, ["country", "market", "domicileCountry", "domicile_country"]) ?? "KR",
    styleProfile: pickString(raw, ["styleProfile", "style_profile"]) ?? null,
    asOfDate: pickString(raw, ["asOfDate", "as_of_date", "tradeDate", "trade_date", "date"]) ?? null,
    compositeScore: groups.find((group) => group.id === "composite")?.score ?? null,
    groups,
  };
}

function normalizeStockList(payload: unknown) {
  const rows = unwrapRows(payload, listKeys)
    .map(normalizeStock)
    .filter((row): row is StyleScoreStock => row !== null);

  if (rows.length > 0) {
    return rows;
  }

  const single = normalizeStock(payload, 0);
  return single ? [single] : [];
}

function normalizeComponentsPayload(securityId: string, payload: unknown): StyleScoreStock | null {
  const baseRecord = isRecord(payload) ? payload : {};
  const groups = Array.isArray(payload) ? groupsFromRows(payload) : groupsFromRecord(baseRecord);
  const fallback = mockRows.find((row) => row.securityId === securityId || row.ticker === securityId) ?? mockRows[0];
  const resolvedSecurityId = pickString(baseRecord, ["securityId", "security_id", "id"]) ?? securityId;
  const resolvedTicker =
    pickString(baseRecord, ["ticker", "stockCode", "stock_code", "symbol"]) ??
    (resolvedSecurityId.includes("-") ? (resolvedSecurityId.split("-").at(-1) ?? resolvedSecurityId) : resolvedSecurityId);
  const resolvedCountry =
    pickString(baseRecord, ["country", "market", "domicileCountry", "domicile_country"]) ??
    (resolvedTicker.length === 6 && /^\d+$/.test(resolvedTicker) ? "KR" : fallback?.country ?? "KR");

  return {
    securityId: resolvedSecurityId,
    ticker: resolvedTicker,
    name:
      pickString(baseRecord, ["name", "stockName", "stock_name", "companyName", "company_name", "company_name_ko"]) ??
      `${resolvedTicker} 스타일 스코어`,
    country: resolvedCountry,
    styleProfile: pickString(baseRecord, ["styleProfile", "style_profile"]) ?? null,
    asOfDate: pickString(baseRecord, ["asOfDate", "as_of_date", "tradeDate", "trade_date", "date"]) ?? fallback?.asOfDate ?? null,
    compositeScore: groups.find((group) => group.id === "composite")?.score ?? null,
    groups,
  };
}

function normalizeFactorsPayload(payload: unknown, componentKey: string) {
  const rows = unwrapRows(payload, factorListKeys)
    .map(normalizeFactor)
    .filter((factor): factor is StyleScoreFactor => factor !== null);

  return rows.length > 0 ? rows : factorTemplates[componentKey] ?? [];
}

function mockList(message: string | null): StyleScoresResult<StyleScoreStock[]> {
  return {
    data: mockRows,
    source: "mock",
    message,
  };
}

function mockDetail(
  securityId: string,
  message: string | null,
  styleProfile: StyleProfile = defaultStyleProfile,
  baseDetail?: StyleScoreStock | null,
): StyleScoresResult<StyleScoreStock> {
  const template =
    mockRows.find((item) => item.securityId === securityId || item.ticker === securityId) ??
    mockRows[0];
  const resolvedSecurityId = baseDetail?.securityId || securityId;
  const resolvedTicker =
    baseDetail?.ticker ||
    (resolvedSecurityId.includes("-") ? resolvedSecurityId.split("-").at(-1) : resolvedSecurityId) ||
    securityId;

  return {
    data: {
      ...template,
      securityId: resolvedSecurityId,
      ticker: resolvedTicker,
      name:
        baseDetail?.name && baseDetail.name !== baseDetail.securityId
          ? baseDetail.name
          : `${resolvedTicker} 스타일 스코어`,
      country: baseDetail?.country ?? template.country,
      styleProfile: baseDetail?.styleProfile ?? styleProfile,
      asOfDate: baseDetail?.asOfDate ?? template.asOfDate,
    },
    source: "mock",
    message,
  };
}

function mockFactors(componentKey: string, message: string | null): StyleScoresResult<StyleScoreFactor[]> {
  return {
    data: factorTemplates[componentKey] ?? factorTemplates.COMPOSITE,
    source: "mock",
    message,
  };
}

function hasUsableScore(detail: StyleScoreStock) {
  return detail.groups.some((group) => {
    const hasScore = typeof group.score === "number" && Number.isFinite(group.score);
    const hasFactors = (group.availableFactorCount ?? 0) > 0;

    return hasScore && hasFactors;
  });
}

function hasUsableFactors(factors: StyleScoreFactor[]) {
  return factors.some((factor) => {
    return (
      factor.rawValue !== null ||
      factor.winsorizedValue !== null ||
      factor.percentileScore !== null ||
      factor.weightedScore !== null ||
      (factor.peerCount ?? 0) > 0
    );
  });
}

async function getJson(path: string) {
  const response = await fetch(apiPath(path));

  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }

  return response.json() as Promise<unknown>;
}

export async function fetchStyleScores(
  styleProfile: StyleProfile = defaultStyleProfile,
): Promise<StyleScoresResult<StyleScoreStock[]>> {
  try {
    const rows = normalizeStockList(await getJson(`/api/style-scores?${buildStyleProfileQuery(styleProfile)}`));

    if (rows.length === 0) {
      return mockList("스타일 스코어 목록 응답이 비어 있어 더미 데이터를 표시합니다.");
    }

    return {
      data: rows,
      source: "api",
      message: null,
    };
  } catch (error) {
    const message =
      error instanceof Error
        ? `스타일 스코어 목록 API 호출에 실패해 더미 데이터를 표시합니다. (${error.message})`
        : "스타일 스코어 목록 API 호출에 실패해 더미 데이터를 표시합니다.";
    return mockList(message);
  }
}

export async function fetchStyleScoreComponents(
  securityId: string,
  styleProfile: StyleProfile = defaultStyleProfile,
): Promise<StyleScoresResult<StyleScoreStock>> {
  try {
    const detail = normalizeComponentsPayload(
      securityId,
      await getJson(
        `/api/style-scores/${encodeURIComponent(securityId)}/components?${buildStyleProfileQuery(styleProfile)}`,
      ),
    );

    if (!detail) {
      return mockDetail(securityId, "스타일 스코어 컴포넌트 응답이 비어 있어 더미 데이터를 표시합니다.", styleProfile);
    }

    if (!hasUsableScore(detail)) {
      return mockDetail(securityId, "스타일 스코어 컴포넌트 값이 없어 더미 데이터를 표시합니다.", styleProfile, detail);
    }

    if (!hasUsableScore(detail)) {
      return mockDetail(securityId, "스타일 스코어 컴포넌트 값이 없어 더미 데이터를 표시합니다.", styleProfile);
    }

    return {
      data: detail,
      source: "api",
      message: null,
    };
  } catch (error) {
    const message =
      error instanceof Error
        ? `스타일 스코어 컴포넌트 API 호출에 실패해 더미 데이터를 표시합니다. (${error.message})`
        : "스타일 스코어 컴포넌트 API 호출에 실패해 더미 데이터를 표시합니다.";
    return mockDetail(securityId, message, styleProfile);
  }
}

export async function fetchStyleScoreComponentFactors(
  securityId: string,
  componentKey: string,
  styleProfile: StyleProfile = defaultStyleProfile,
): Promise<StyleScoresResult<StyleScoreFactor[]>> {
  try {
    const factors = normalizeFactorsPayload(
      await getJson(
        `/api/style-scores/${encodeURIComponent(securityId)}/components/${encodeURIComponent(componentKey)}?${buildStyleProfileQuery(styleProfile)}`,
      ),
      componentKey,
    );

    if (factors.length === 0) {
      return mockFactors(componentKey, "스타일 스코어 팩터 상세 응답이 비어 있어 더미 데이터를 표시합니다.");
    }

    if (!hasUsableFactors(factors)) {
      return mockFactors(componentKey, "스타일 스코어 팩터 값이 없어 더미 데이터를 표시합니다.");
    }

    return {
      data: factors,
      source: "api",
      message: null,
    };
  } catch (error) {
    const message =
      error instanceof Error
        ? `스타일 스코어 팩터 상세 API 호출에 실패해 더미 데이터를 표시합니다. (${error.message})`
        : "스타일 스코어 팩터 상세 API 호출에 실패해 더미 데이터를 표시합니다.";
    return mockFactors(componentKey, message);
  }
}
