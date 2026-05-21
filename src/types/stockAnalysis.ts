export type StockChartMode = "line" | "area" | "candle";

export type StockChartRange = "1M" | "3M" | "6M" | "1Y" | "2Y" | "5Y" | "MAX";

export type StockAnalysisPoint = {
  date: string;
  open: number;
  high: number;
  low: number;
  close: number;
  volume: number;
  ma5: number;
  ma20: number;
  ema12: number;
  ema26: number;
  momentum: string;
  rsi: string;
  bollinger: string;
  trend: string;
  macd: string;
  volumeSignal: string;
  volatility: string;
  gap: string;
  weeklyReturn: number;
};

export type StyleScore = {
  label: string;
  value: number;
};

export type StockAnalysisSummary = {
  stockCode: string;
  market: "KR" | "US";
  name: string;
  industryLabel: string;
  latestDate: string;
  latestPrice: number;
  priceChange: number;
  priceChangeRate: number;
  styleScore: number;
  scores: StyleScore[];
};

export type StockOverview = {
  marketCap: number | null;
  per: number | null;
  dividendYield: number | null;
  week52Low: number | null;
  week52High: number | null;
  week52ChangeRate: number | null;
  companyDescription: string;
  gicsIndustries: string[];
};

export type StockAnalysisResponse = {
  summary: StockAnalysisSummary;
  overview: StockOverview;
  chart: StockAnalysisPoint[];
  recentData: StockAnalysisPoint[];
};
