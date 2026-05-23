export type MarketCode = string;

export type MarketOption = {
  id: MarketCode;
  label: string;
};

export type ComparisonOperator = ">" | ">=" | "<" | "<=" | "=";

export type ConditionInputMode = "percentile" | "value";

export type IndustryOption = {
  id: string;
  name: string;
  count?: number;
  children?: IndustryOption[];
};

export type FactorValueDirection = "HIGHER_BETTER" | "LOWER_BETTER" | "NEUTRAL" | string;

export type FilterDefinition = {
  id: string;
  label: string;
  field: string;
  description: string;
  unit?: string;
  valueDirection?: FactorValueDirection;
  defaultOperator: ComparisonOperator;
  defaultValue: number;
  defaultInputMode?: ConditionInputMode;
};

export type FilterGroup = {
  id: string;
  name: string;
  count: number;
  children?: FilterGroup[];
  filters?: FilterDefinition[];
};

export type ScreenerCondition = {
  filterId: string;
  field: string;
  label: string;
  inputMode: ConditionInputMode;
  operator: ComparisonOperator;
  value: number;
  percentile: number;
  unit?: string;
};

export type QuantScreenerRequest = {
  market: MarketCode;
  industries: string[];
  conditions: ScreenerCondition[];
  page: number;
  pageSize: number;
};

export type ScreenedFactorValue = {
  factorId: string;
  factorName: string;
  conditionId: string;
  value: number | null;
  tradeDate: string | null;
  unit: string | null;
  valueDirection: FactorValueDirection | null;
};

export type ScreenedStock = {
  rank: number;
  securityId?: string;
  ticker: string;
  name: string;
  market: string;
  marketCap: number | null;
  sectorCode?: string | null;
  percentile: number | null;
  matchedConditionCount?: number;
  matchedConditions?: string[];
  latestTradeDate?: string | null;
  factorValues: Record<string, ScreenedFactorValue>;
};

export type QuantScreenerColumn = {
  key: string;
  label: string;
  columnType: "rank" | "ticker" | "name" | "country" | "market_cap" | "factor" | "percentile";
  order: number;
  factorId?: string | null;
  factorName?: string | null;
  unit?: string | null;
  valueDirection?: FactorValueDirection | null;
};

export type QuantScreenerSummary = {
  screeningResult: "OK" | "EMPTY";
  totalCount: number;
  displayedCount: number;
};

export type QuantScreenerResponse = {
  total: number;
  page: number;
  pageSize: number;
  rows: ScreenedStock[];
  matchedConditionCounts: Record<string, number>;
  summary?: QuantScreenerSummary;
  fixedColumns?: QuantScreenerColumn[];
  factorColumns?: QuantScreenerColumn[];
};

export type BacktestRebalanceFrequency = "quarterly" | "semiannual" | "annual" | "monthly" | string;

export type FactorBacktestRequest = {
  market: MarketCode;
  industries: string[];
  conditions: ScreenerCondition[];
  startDate: string;
  endDate: string;
  rebalanceFrequency: BacktestRebalanceFrequency;
};

export type BacktestEquityPoint = {
  date: string;
  strategy: number;
  benchmark: number | null;
  cash: number | null;
  benchmarkName: string | null;
};

export type BacktestAnnualReturn = {
  year: number;
  strategy: number;
  benchmark: number | null;
  excess: number | null;
};

export type BacktestPosition = {
  securityId: string;
  ticker: string;
  name: string;
  weight: number | null;
  score: number | null;
};

export type BacktestRebalance = {
  rebalanceDate: string;
  signalDate: string | null;
  positions: BacktestPosition[];
};

export type FactorBacktestSummary = {
  startDate: string;
  endDate: string;
  rebalanceFrequency: BacktestRebalanceFrequency;
  cumulativeReturn: number | null;
  cagr: number | null;
  maxDrawdown: number | null;
  volatility: number | null;
  sharpe: number | null;
  winRate: number | null;
  rebalanceCount: number | null;
  benchmarkName: string | null;
};

export type FactorBacktestResponse = {
  summary: FactorBacktestSummary;
  equityCurve: BacktestEquityPoint[];
  annualReturns: BacktestAnnualReturn[];
  rebalanceHistory: BacktestRebalance[];
  warnings: string[];
};
