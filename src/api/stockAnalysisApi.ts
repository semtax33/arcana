import type {
  StockAnalysisPoint,
  StockAnalysisResponse,
  StockAnalysisSummary,
  StockChartRange,
  StockOverview,
  StyleScore,
} from "../types/stockAnalysis";

const apiBaseUrl = ((import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "").replace(/\/+$/, "");
const mockApiPreference = import.meta.env.VITE_STOCK_ANALYSIS_USE_MOCK_API as string | undefined;
const useMockApi = mockApiPreference === "true";
const apiPath = (path: string) => `${apiBaseUrl}${path}`;

type ChartApiRange = Exclude<StockChartRange, "2Y">;

type StockChartPointDto = {
  time: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  ma5?: number | null;
  ma20?: number | null;
};

type RecentStockChartRowDto = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  monthly_return?: number | null;
  continuity?: string | null;
  volume_signal?: string | null;
  rsi?: string | number | null;
  bollinger_band?: string | number | Record<string, unknown> | null;
  trend?: string | number | null;
  macd?: string | number | Record<string, unknown> | null;
};

type StockChartResponseDto = {
  stock: {
    stock_code: string;
    stock_name?: string | null;
    country?: string | null;
  };
  to_date: string;
  chart: StockChartPointDto[];
  recent: RecentStockChartRowDto[];
};

type IntroductionBusinessArea = {
  sector_name?: string | null;
  name?: string | null;
  schema?: string | null;
};

type StatementStockIntroductionResponse = {
  stock?: {
    stock_code?: string | null;
    security_id?: string | null;
    stock_name?: string | null;
    stock_name_en?: string | null;
    country?: string | null;
    currency?: string | null;
  };
  stock_code?: string | null;
  stock_name?: string | null;
  market?: string | null;
  currency?: string | null;
  market_cap?: number | null;
  trailing_per?: number | null;
  dividend_yield?: number | null;
  week_52_low?: number | null;
  week_52_high?: number | null;
  week_52_change_rate?: number | null;
  company_description?: string | null;
  gsic_sectors?: Array<string | IntroductionBusinessArea>;
  sections?: {
    valuation?: {
      market_cap?: number | null;
      trailing_per?: number | null;
      dividend_yield?: number | null;
      week_52_low?: number | null;
      week_52_high?: number | null;
      week_52_change_rate?: number | null;
    };
    company?: {
      description?: string | null;
    };
    business_areas?: Array<string | IntroductionBusinessArea>;
  };
  metrics?: {
    market_cap?: number | null;
    trailing_per?: number | null;
    dividend_yield?: number | null;
    fifty_two_week_low?: number | null;
    fifty_two_week_high?: number | null;
    fifty_two_week_range_pct?: number | null;
    latest_close?: number | null;
    latest_trade_date?: string | null;
  };
  company?: {
    description?: string | null;
  };
  business_areas?: Array<IntroductionBusinessArea>;
  factor_source?: string;
};

type KnownStockProfile = {
  name: string;
  basePrice: number;
  market: "KR" | "US";
  industryLabel: string;
};

const knownStocks: Record<string, KnownStockProfile> = {
  "236200": {
    name: "슈프리마",
    basePrice: 32700,
    market: "KR",
    industryLabel: "Technology Hardware & Equipment",
  },
  "005710": {
    name: "대원산업",
    basePrice: 7100,
    market: "KR",
    industryLabel: "Automobiles & Components",
  },
  "440110": {
    name: "파두",
    basePrice: 18200,
    market: "KR",
    industryLabel: "Semiconductors",
  },
  "019540": {
    name: "일지테크",
    basePrice: 5300,
    market: "KR",
    industryLabel: "Automobile Components",
  },
  "019180": {
    name: "티에이치엔",
    basePrice: 4200,
    market: "KR",
    industryLabel: "Automobile Components",
  },
  "005930": {
    name: "삼성전자",
    basePrice: 72000,
    market: "KR",
    industryLabel: "Semiconductors",
  },
  "003230": {
    name: "삼양식품",
    basePrice: 511000,
    market: "KR",
    industryLabel: "Food Products",
  },
};

const rangeSize: Record<StockChartRange, number> = {
  "1M": 22,
  "3M": 66,
  "6M": 132,
  "1Y": 252,
  "2Y": 504,
  "5Y": 900,
  MAX: 1100,
};

function hashStockCode(stockCode: string) {
  return stockCode.split("").reduce((hash, char) => hash + char.charCodeAt(0), 0);
}

function average(values: number[]) {
  return values.reduce((sum, value) => sum + value, 0) / Math.max(values.length, 1);
}

function movingAverage(points: StockAnalysisPoint[], index: number, size: number) {
  const start = Math.max(0, index - size + 1);
  return average(points.slice(start, index + 1).map((point) => point.close));
}

function exponentialAverage(previous: number, value: number, period: number) {
  const multiplier = 2 / (period + 1);
  return value * multiplier + previous * (1 - multiplier);
}

function normalizeNumber(value: number | null | undefined, fallback = 0) {
  return Number.isFinite(value) ? Number(value) : fallback;
}

function createUnknownProfile(stockCode: string, basePrice: number): KnownStockProfile {
  return {
    name: `${stockCode} 종목`,
    basePrice,
    market: "KR",
    industryLabel: "Unclassified",
  };
}

function calculateWeek52Range(rows: StockAnalysisPoint[]) {
  const recentRows = rows.slice(-252);
  const lows = recentRows.map((row) => row.low).filter(Number.isFinite);
  const highs = recentRows.map((row) => row.high).filter(Number.isFinite);
  const week52Low = lows.length > 0 ? Math.min(...lows) : null;
  const week52High = highs.length > 0 ? Math.max(...highs) : null;

  return {
    week52Low,
    week52High,
    week52ChangeRate:
      week52Low !== null && week52High !== null && week52Low > 0
        ? Number((((week52High - week52Low) / week52Low) * 100).toFixed(2))
        : null,
  };
}

function finiteNumber(value: unknown) {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function stringifySignal(value: unknown, fallback = "No_Data") {
  if (value === null || value === undefined) {
    return fallback;
  }

  if (typeof value === "number") {
    return Number.isFinite(value) ? Number(value.toFixed(2)).toString() : fallback;
  }

  if (typeof value === "string") {
    return value;
  }

  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const signal = record.signal ?? record.trend ?? record.label;
    return typeof signal === "string" ? signal : fallback;
  }

  return fallback;
}

function macdSignal(value: RecentStockChartRowDto["macd"]) {
  if (typeof value === "object" && value !== null) {
    const macd = finiteNumber(value.macd);
    const signal = finiteNumber(value.signal);

    if (macd !== null && signal !== null) {
      return macd >= signal ? "Bullish" : "Bearish";
    }
  }

  return stringifySignal(value, "No_Data");
}

function createSignalLabels(close: number, open: number, ma5: number, ma20: number, volume: number) {
  const changeRate = open === 0 ? 0 : ((close - open) / open) * 100;
  const volumeSignal =
    volume > 100000 ? "High_Volume" : volume > 62000 ? "Above_Average_Volume" : "Normal_Volume";
  const trend = ma5 > ma20 * 1.02 ? "Strong_Uptrend" : ma5 > ma20 ? "Uptrend" : "Sideways";
  const momentum =
    changeRate > 2 ? "Bullish_Momentum" : changeRate < -2 ? "Bearish_Momentum" : "Neutral";
  const rsi = close > ma20 * 1.08 ? "Overbought" : close < ma20 * 0.94 ? "Oversold" : "Neutral";
  const bollinger =
    close > ma20 * 1.08 ? "Above_Upper_Band" : close > ma20 * 1.03 ? "Near_Upper_Band" : "Neutral";
  const macd = ma5 > ma20 ? "Bullish" : "Bearish";
  const volatility = Math.abs(changeRate) > 4 ? "Wide_Range" : "Normal_Gap";

  return { bollinger, macd, momentum, rsi, trend, volumeSignal, volatility };
}

function createMockChart(stockCode: string, range: StockChartRange) {
  const seed = hashStockCode(stockCode);
  const profile = knownStocks[stockCode] ?? createUnknownProfile(stockCode, 18000 + seed * 72);
  const days = rangeSize[range];
  const start = new Date("2025-05-01T00:00:00+09:00");
  const points: StockAnalysisPoint[] = [];
  let previousClose = profile.basePrice;
  let ema12 = previousClose;
  let ema26 = previousClose;

  for (let index = 0; index < days; index += 1) {
    const date = new Date(start);
    date.setDate(start.getDate() + index);

    const cycle = Math.sin((index + seed) / 18) * 0.018;
    const longCycle = Math.cos((index + seed * 2) / 64) * 0.012;
    const drift = 0.0009 + (seed % 7) * 0.00008;
    const eventBoost = index > days * 0.67 ? 0.0017 : 0;
    const dailyMove = cycle + longCycle + drift + eventBoost;
    const open = previousClose * (1 + Math.sin(index / 7 + seed) * 0.006);
    const close = Math.max(900, previousClose * (1 + dailyMove));
    const high = Math.max(open, close) * (1 + 0.012 + Math.abs(Math.sin(index / 11)) * 0.01);
    const low = Math.min(open, close) * (1 - 0.011 - Math.abs(Math.cos(index / 13)) * 0.008);
    const volume =
      26000 +
      Math.round(Math.abs(Math.sin(index / 9 + seed)) * 56000) +
      (index % 57 === 0 ? 128000 : 0);

    ema12 = exponentialAverage(ema12, close, 12);
    ema26 = exponentialAverage(ema26, close, 26);

    points.push({
      date: date.toISOString().slice(0, 10),
      open: Math.round(open / 10) * 10,
      high: Math.round(high / 10) * 10,
      low: Math.round(low / 10) * 10,
      close: Math.round(close / 10) * 10,
      volume,
      ma5: 0,
      ma20: 0,
      ema12: Math.round(ema12),
      ema26: Math.round(ema26),
      momentum: "Neutral",
      rsi: "Neutral",
      bollinger: "Neutral",
      trend: "Sideways",
      macd: "Neutral",
      volumeSignal: "Normal_Volume",
      volatility: "Normal_Gap",
      gap: "Normal_Gap",
      weeklyReturn: 0,
    });

    previousClose = close;
  }

  return points.map((point, index) => {
    const ma5 = movingAverage(points, index, 5);
    const ma20 = movingAverage(points, index, 20);
    const weeklyBase = points[Math.max(0, index - 5)].close;
    const labels = createSignalLabels(point.close, point.open, ma5, ma20, point.volume);

    return {
      ...point,
      ma5: Math.round(ma5),
      ma20: Math.round(ma20),
      weeklyReturn: Number((((point.close - weeklyBase) / weeklyBase) * 100).toFixed(2)),
      gap: labels.volatility,
      ...labels,
    };
  });
}

function createStyleScoresFromRows(rows: StockAnalysisPoint[]): StyleScore[] {
  const latest = rows[rows.length - 1];
  const recent = rows.slice(-30);
  const monthlyReturn = latest?.weeklyReturn ?? 0;
  const positiveDays = recent.filter((row) => row.close >= row.open).length;
  const volumeMomentum = latest ? latest.volume / Math.max(average(recent.map((row) => row.volume)), 1) : 1;
  const maSpread = latest?.ma20 ? ((latest.ma5 - latest.ma20) / latest.ma20) * 100 : 0;

  return [
    { label: "Growth", value: Math.max(0, Math.min(100, 55 + monthlyReturn * 2)) },
    {
      label: "Profitability",
      value: Math.max(0, Math.min(100, 45 + (positiveDays / Math.max(recent.length, 1)) * 55)),
    },
    { label: "Stability", value: Math.max(0, Math.min(100, 80 - Math.abs(monthlyReturn) * 1.4)) },
    { label: "Valuation", value: Math.max(0, Math.min(100, 58 - Math.max(maSpread, -20))) },
    { label: "Shareholder Return", value: 62 },
    { label: "Momentum", value: Math.max(0, Math.min(100, 50 + maSpread * 4 + volumeMomentum * 4)) },
  ].map((score) => ({ ...score, value: Math.round(score.value) }));
}

async function fetchStockIntroduction(stockCode: string): Promise<StatementStockIntroductionResponse | null> {
  try {
    const response = await fetch(apiPath(`/api/introduction/${encodeURIComponent(stockCode)}`));

    if (!response.ok) {
      return null;
    }

    return (await response.json()) as StatementStockIntroductionResponse;
  } catch {
    return null;
  }
}

function getIntroductionStockCode(introduction: StatementStockIntroductionResponse | null) {
  return introduction?.stock?.stock_code ?? introduction?.stock_code ?? undefined;
}

function getIntroductionStockName(introduction: StatementStockIntroductionResponse | null) {
  return introduction?.stock?.stock_name ?? introduction?.stock_name ?? undefined;
}

function getIntroductionMarket(introduction: StatementStockIntroductionResponse | null): "KR" | "US" | undefined {
  const market = introduction?.stock?.country ?? introduction?.market;
  return market === "US" ? "US" : market === "KR" ? "KR" : undefined;
}

function firstNumber(...values: Array<number | null | undefined>) {
  for (const value of values) {
    if (typeof value === "number" && Number.isFinite(value)) {
      return value;
    }
  }
  return null;
}

function sectorLabel(sector: string | IntroductionBusinessArea) {
  if (typeof sector === "string") {
    return sector;
  }
  return sector.sector_name ?? sector.name ?? "";
}

function overviewFromIntroduction(
  introduction: StatementStockIntroductionResponse | null,
  chart: StockAnalysisPoint[],
): StockOverview {
  const week52Range = calculateWeek52Range(chart);
  const valuation = introduction?.sections?.valuation;
  const sectors = [
    ...(introduction?.business_areas ?? []),
    ...(introduction?.gsic_sectors ?? []),
    ...(introduction?.sections?.business_areas ?? []),
  ];

  return {
    marketCap: firstNumber(introduction?.metrics?.market_cap, introduction?.market_cap, valuation?.market_cap),
    per: firstNumber(introduction?.metrics?.trailing_per, introduction?.trailing_per, valuation?.trailing_per),
    dividendYield: firstNumber(
      introduction?.metrics?.dividend_yield,
      introduction?.dividend_yield,
      valuation?.dividend_yield,
    ),
    week52Low:
      firstNumber(introduction?.metrics?.fifty_two_week_low, introduction?.week_52_low, valuation?.week_52_low) ??
      week52Range.week52Low,
    week52High:
      firstNumber(introduction?.metrics?.fifty_two_week_high, introduction?.week_52_high, valuation?.week_52_high) ??
      week52Range.week52High,
    week52ChangeRate:
      firstNumber(
        introduction?.metrics?.fifty_two_week_range_pct,
        introduction?.week_52_change_rate,
        valuation?.week_52_change_rate,
      ) ?? week52Range.week52ChangeRate,
    companyDescription:
      introduction?.company?.description ?? introduction?.company_description ?? introduction?.sections?.company?.description ?? "",
    gicsIndustries: sectors.map(sectorLabel).filter((name) => name.trim().length > 0),
  };
}

function createMockResponse(
  stockCode: string,
  range: StockChartRange,
  introduction: StatementStockIntroductionResponse | null,
): StockAnalysisResponse {
  const chart = createMockChart(stockCode, range);
  const latest = chart[chart.length - 1];
  const previous = chart[chart.length - 2] ?? latest;
  const profile = knownStocks[stockCode] ?? createUnknownProfile(stockCode, latest.close);
  const scores = createStyleScoresFromRows(chart);
  const overview = overviewFromIntroduction(introduction, chart);
  const summary: StockAnalysisSummary = {
    stockCode: getIntroductionStockCode(introduction) ?? stockCode,
    market: getIntroductionMarket(introduction) ?? profile.market,
    name: getIntroductionStockName(introduction) ?? profile.name,
    industryLabel: overview.gicsIndustries.join(" / ") || profile.industryLabel,
    latestDate: latest.date,
    latestPrice: latest.close,
    priceChange: latest.close - previous.close,
    priceChangeRate: Number((((latest.close - previous.close) / previous.close) * 100).toFixed(2)),
    styleScore: Math.round(average(scores.map((score) => score.value))),
    scores,
  };

  return {
    summary,
    overview,
    chart,
    recentData: [...chart].slice(-30).reverse(),
  };
}

function mapChartPoint(point: StockChartPointDto): StockAnalysisPoint {
  const ma5 = normalizeNumber(point.ma5, point.close);
  const ma20 = normalizeNumber(point.ma20, ma5);
  const labels = createSignalLabels(point.close, point.open, ma5, ma20, point.volume);

  return {
    date: point.time,
    open: point.open,
    high: point.high,
    low: point.low,
    close: point.close,
    volume: point.volume,
    ma5,
    ma20,
    ema12: ma5,
    ema26: ma20,
    momentum: labels.momentum,
    rsi: labels.rsi,
    bollinger: labels.bollinger,
    trend: labels.trend,
    macd: labels.macd,
    volumeSignal: labels.volumeSignal,
    volatility: labels.volatility,
    gap: labels.volatility,
    weeklyReturn: 0,
  };
}

function mapRecentRow(row: RecentStockChartRowDto, chartPoint?: StockAnalysisPoint): StockAnalysisPoint {
  const ma5 = chartPoint?.ma5 ?? row.close;
  const ma20 = chartPoint?.ma20 ?? ma5;
  const monthlyReturn = normalizeNumber(row.monthly_return);

  return {
    date: row.date,
    open: row.open,
    high: row.high,
    low: row.low,
    close: row.close,
    volume: row.volume,
    ma5,
    ma20,
    ema12: chartPoint?.ema12 ?? ma5,
    ema26: chartPoint?.ema26 ?? ma20,
    momentum:
      row.monthly_return === null || row.monthly_return === undefined
        ? "Neutral"
        : monthlyReturn > 0
          ? "Bullish_Momentum"
          : monthlyReturn < 0
            ? "Bearish_Momentum"
            : "Neutral",
    rsi: stringifySignal(row.rsi, "No_Data"),
    bollinger: stringifySignal(row.bollinger_band, "No_Data"),
    trend: stringifySignal(row.trend, "No_Data"),
    macd: macdSignal(row.macd),
    volumeSignal: stringifySignal(row.volume_signal, "Normal_Volume"),
    volatility: stringifySignal(row.continuity, "Normal_Gap"),
    gap: stringifySignal(row.continuity, "Normal_Gap"),
    weeklyReturn: monthlyReturn,
  };
}

function twoYearsAgoFrom(value: string) {
  const date = new Date(`${value.slice(0, 10)}T00:00:00+09:00`);
  date.setFullYear(date.getFullYear() - 2);
  return date.toISOString().slice(0, 10);
}

function mapChartResponse(
  response: StockChartResponseDto,
  requestedRange: StockChartRange,
  introduction: StatementStockIntroductionResponse | null,
): StockAnalysisResponse {
  const mappedChart = response.chart.map(mapChartPoint);
  const chart =
    requestedRange === "2Y"
      ? mappedChart.filter((point) => point.date >= twoYearsAgoFrom(response.to_date))
      : mappedChart;
  const chartByDate = new Map(chart.map((point) => [point.date, point]));
  const recentData = response.recent.map((row) => mapRecentRow(row, chartByDate.get(row.date)));
  const latest = chart[chart.length - 1] ?? recentData[0];
  const previous = chart[chart.length - 2] ?? latest;
  const scores = createStyleScoresFromRows(chart.length > 0 ? chart : recentData);
  const profile =
    knownStocks[response.stock.stock_code] ??
    createUnknownProfile(response.stock.stock_code, latest?.close ?? 0);
  const overview = overviewFromIntroduction(introduction, chart);

  return {
    summary: {
      stockCode: response.stock.stock_code ?? getIntroductionStockCode(introduction) ?? "",
      market: getIntroductionMarket(introduction) ?? (response.stock.country === "US" ? "US" : "KR"),
      name: response.stock.stock_name ?? getIntroductionStockName(introduction) ?? response.stock.stock_code,
      industryLabel: overview.gicsIndustries.join(" / ") || profile.industryLabel,
      latestDate: introduction?.metrics?.latest_trade_date ?? response.to_date,
      latestPrice: introduction?.metrics?.latest_close ?? latest?.close ?? 0,
      priceChange: latest && previous ? latest.close - previous.close : 0,
      priceChangeRate:
        latest && previous && previous.close !== 0
          ? Number((((latest.close - previous.close) / previous.close) * 100).toFixed(2))
          : 0,
      styleScore: Math.round(average(scores.map((score) => score.value))),
      scores,
    },
    overview,
    chart,
    recentData,
  };
}

function toChartApiRange(range: StockChartRange): ChartApiRange {
  return range === "2Y" ? "5Y" : range;
}

export async function fetchStockAnalysis(
  stockCode: string,
  range: StockChartRange,
): Promise<StockAnalysisResponse> {
  const introduction = await fetchStockIntroduction(stockCode);

  if (useMockApi) {
    await new Promise((resolve) => window.setTimeout(resolve, 220));
    return createMockResponse(stockCode, range, introduction);
  }

  const apiRange = toChartApiRange(range);
  const response = await fetch(
    apiPath(`/api/chart/${encodeURIComponent(stockCode)}?range=${encodeURIComponent(apiRange)}`),
  );

  if (!response.ok) {
    const message =
      response.status === 404
        ? `${stockCode} 종목의 차트 데이터가 없습니다.`
        : "종목 차트 데이터를 불러오지 못했습니다.";
    throw new Error(message);
  }

  return mapChartResponse((await response.json()) as StockChartResponseDto, range, introduction);
}
