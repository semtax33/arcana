import type {
  ComparisonOperator,
  FactorValueDirection,
  FilterDefinition,
  FactorBacktestRequest,
  FactorBacktestResponse,
  FilterGroup,
  IndustryOption,
  MarketOption,
  QuantScreenerColumn,
  QuantScreenerRequest,
  QuantScreenerResponse,
  ScreenedFactorValue,
  ScreenedStock,
} from "../types/quantScreener";

const apiBaseUrl = (import.meta.env.VITE_API_BASE_URL as string | undefined) ?? "";
const useMockApi = import.meta.env.VITE_USE_MOCK_API === "true";

export const defaultMarketOptions: MarketOption[] = [
  { id: "KR", label: "한국" },
  { id: "US", label: "미국" },
  { id: "JP", label: "일본" },
];

type SectorDto = {
  sector_code: string;
  sector_name: string;
  stock_count: number;
};

type FactorDto = {
  factor_id: string;
  factor_name: string;
  factor_type: string;
  factor_group: string;
  unit: string | null;
  value_direction: FactorValueDirection;
  description: string | null;
  is_active: boolean;
};

type MarketDto =
  | string
  | {
      id?: string | null;
      code?: string | null;
      country?: string | null;
      country_code?: string | null;
      market?: string | null;
      label?: string | null;
      name?: string | null;
      country_name?: string | null;
    };

type MarketCatalogDto =
  | MarketDto[]
  | {
      markets?: MarketDto[] | null;
      countries?: MarketDto[] | null;
      data?: MarketDto[] | null;
      results?: MarketDto[] | null;
    };

type FactorConditionDto = {
  factor_id: string;
  mode: "top_percent" | "threshold";
  top_percent?: number | null;
  rank_direction?: "catalog" | "higher" | "lower";
  operator?: string | null;
  value?: number | null;
  alias?: string | null;
};

type FactorScreenRequestDto = {
  conditions: FactorConditionDto[];
  sector_codes?: string[] | null;
  match_mode: "all" | "any";
  limit: number;
};

type FactorScreenColumnDto = {
  key: string;
  label: string;
  column_type: QuantScreenerColumn["columnType"];
  order: number;
  factor_id: string | null;
  factor_name: string | null;
  unit: string | null;
  value_direction: FactorValueDirection | null;
};

type FactorScreenValueDto = {
  factor_id: string;
  factor_name: string;
  condition_id: string;
  value: number | null;
  trade_date: string | null;
  unit: string | null;
  value_direction: FactorValueDirection | null;
};

type ScreenedStockRowDto = {
  rank: number;
  security_id: string;
  ticker: string | null;
  stock_name: string | null;
  country: string | null;
  market_cap: number | null;
  sector_code: string | null;
  percentile: number | null;
  matched_condition_count: number;
  matched_conditions: string[];
  latest_trade_date: string | null;
  factor_values: Record<string, FactorScreenValueDto>;
};

type FactorScreenResponseDto = {
  summary: {
    screening_result: "OK" | "EMPTY";
    total_count: number;
    displayed_count: number;
  };
  total_count: number;
  fixed_columns: FactorScreenColumnDto[];
  factor_columns: FactorScreenColumnDto[];
  rows: ScreenedStockRowDto[];
};

type FactorBacktestSummaryDto = {
  start_date: string;
  end_date: string;
  rebalance_frequency: string;
  cumulative_return: number | null;
  cagr: number | null;
  max_drawdown: number | null;
  volatility: number | null;
  sharpe: number | null;
  win_rate: number | null;
  rebalance_count: number | null;
};

type FactorBacktestEquityPointDto = {
  trade_date: string;
  strategy_nav: number | null;
  benchmark_navs?: Record<string, number | null> | null;
};

type FactorBacktestAnnualReturnDto = {
  year: number;
  strategy_return: number | null;
  benchmark_returns?: Record<string, number | null> | null;
  excess_returns?: Record<string, number | null> | null;
};

type FactorBacktestPositionDto = {
  security_id: string;
  ticker: string | null;
  stock_name: string | null;
  weight: number | null;
  score: number | null;
};

type FactorBacktestRebalanceDto = {
  rebalance_date: string;
  signal_date: string | null;
  positions: FactorBacktestPositionDto[];
};

type FactorBacktestResponseDto = {
  summary: FactorBacktestSummaryDto;
  equity_curve: FactorBacktestEquityPointDto[];
  annual_returns: FactorBacktestAnnualReturnDto[];
  rebalance_history: FactorBacktestRebalanceDto[];
  warnings?: string[] | null;
};

const mockIndustries: IndustryOption[] = [
  { id: "10", name: "Energy", count: 34 },
  { id: "15", name: "Materials", count: 91 },
  { id: "20", name: "Industrials", count: 184 },
  { id: "25", name: "Consumer Discretionary", count: 221 },
  { id: "30", name: "Consumer Staples", count: 74 },
  { id: "35", name: "Health Care", count: 128 },
  { id: "40", name: "Financials", count: 83 },
  { id: "45", name: "Information Technology", count: 312 },
];

const mockFilters: FilterDefinition[] = [
  {
    id: "roe",
    label: "ROE",
    field: "roe",
    description: "Return on equity",
    unit: "ratio",
    valueDirection: "HIGHER_BETTER",
    defaultOperator: ">=",
    defaultValue: 0,
    defaultInputMode: "percentile",
  },
  {
    id: "per",
    label: "PER",
    field: "per",
    description: "Price earnings ratio",
    unit: "times",
    valueDirection: "LOWER_BETTER",
    defaultOperator: "<=",
    defaultValue: 10,
    defaultInputMode: "percentile",
  },
  {
    id: "epr",
    label: "EPR",
    field: "epr",
    description: "Earnings to price ratio",
    unit: "ratio",
    valueDirection: "HIGHER_BETTER",
    defaultOperator: ">=",
    defaultValue: 0,
    defaultInputMode: "percentile",
  },
  {
    id: "bpr",
    label: "BPR",
    field: "bpr",
    description: "Book to price ratio",
    unit: "ratio",
    valueDirection: "HIGHER_BETTER",
    defaultOperator: ">=",
    defaultValue: 0,
    defaultInputMode: "percentile",
  },
  {
    id: "mcap_mil",
    label: "MCAP MIL",
    field: "mcap_mil",
    description: "Market capitalization in millions",
    unit: "krw",
    valueDirection: "HIGHER_BETTER",
    defaultOperator: ">=",
    defaultValue: 0,
    defaultInputMode: "percentile",
  },
];

const mockFilterCatalog: FilterGroup[] = [
  {
    id: "valuation",
    name: "Valuation",
    count: 4,
    children: [
      {
        id: "valuation:valuation",
        name: "Valuation",
        count: 4,
        filters: mockFilters.filter((filter) =>
          ["epr", "bpr", "per", "mcap_mil"].includes(filter.id),
        ),
      },
    ],
  },
  {
    id: "quality",
    name: "Quality",
    count: 1,
    children: [
      {
        id: "quality:quality",
        name: "Quality",
        count: 1,
        filters: mockFilters.filter((filter) => filter.id === "roe"),
      },
    ],
  },
];

const mockRows: ScreenedStock[] = [
  {
    rank: 1,
    securityId: "KR-036800",
    ticker: "036800",
    name: "나이스정보통신",
    market: "KR",
    marketCap: 240000,
    sectorCode: "45",
    percentile: null,
    matchedConditionCount: 2,
    matchedConditions: ["0:top_percent:roe", "1:top_percent:per"],
    latestTradeDate: "2026-05-21",
    factorValues: {
      roe_0: {
        factorId: "roe",
        factorName: "ROE",
        conditionId: "0:top_percent:roe",
        value: 0.18,
        tradeDate: "2026-05-21",
        unit: "ratio",
        valueDirection: "HIGHER_BETTER",
      },
      per_1: {
        factorId: "per",
        factorName: "PER",
        conditionId: "1:top_percent:per",
        value: 8.42,
        tradeDate: "2026-05-21",
        unit: "times",
        valueDirection: "LOWER_BETTER",
      },
    },
  },
  {
    rank: 2,
    securityId: "KR-005960",
    ticker: "005960",
    name: "동부건설",
    market: "KR",
    marketCap: 108000,
    sectorCode: "20",
    percentile: null,
    matchedConditionCount: 2,
    matchedConditions: ["0:top_percent:roe", "1:top_percent:per"],
    latestTradeDate: "2026-05-21",
    factorValues: {
      roe_0: {
        factorId: "roe",
        factorName: "ROE",
        conditionId: "0:top_percent:roe",
        value: 0.15,
        tradeDate: "2026-05-21",
        unit: "ratio",
        valueDirection: "HIGHER_BETTER",
      },
      per_1: {
        factorId: "per",
        factorName: "PER",
        conditionId: "1:top_percent:per",
        value: 6.37,
        tradeDate: "2026-05-21",
        unit: "times",
        valueDirection: "LOWER_BETTER",
      },
    },
  },
];

async function getJson<T>(path: string): Promise<T> {
  const response = await fetch(`${apiBaseUrl}${path}`);

  if (!response.ok) {
    throw new Error(`Request failed: ${path}`);
  }

  return (await response.json()) as T;
}

function formatGroupName(value: string) {
  return value
    .split(/[_-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function defaultOperatorFor(direction: FactorValueDirection): ComparisonOperator {
  return direction === "LOWER_BETTER" ? "<=" : ">=";
}

function defaultValueFor(unit: string | null) {
  if (unit === "times") {
    return 10;
  }

  if (unit === "score") {
    return 80;
  }

  return 0;
}

function mapSector(sector: SectorDto): IndustryOption {
  return {
    id: sector.sector_code,
    name: sector.sector_name,
    count: sector.stock_count,
  };
}

function mapMarket(market: MarketDto): MarketOption | null {
  if (typeof market === "string") {
    return { id: market, label: market };
  }

  const id = market.code ?? market.id ?? market.country_code ?? market.country ?? market.market;
  const label = market.label ?? market.name ?? market.country_name ?? market.country ?? id;

  if (!id || !label) {
    return null;
  }

  return { id, label };
}

function normalizeMarketCatalog(catalog: MarketCatalogDto): MarketDto[] {
  if (Array.isArray(catalog)) {
    return catalog;
  }

  return catalog.markets ?? catalog.countries ?? catalog.data ?? catalog.results ?? [];
}

function mapFactorsToFilterCatalog(factors: FactorDto[]): FilterGroup[] {
  const typeGroups = new Map<string, Map<string, FactorDto[]>>();

  for (const factor of factors) {
    const groupMap = typeGroups.get(factor.factor_type) ?? new Map<string, FactorDto[]>();
    const factorList = groupMap.get(factor.factor_group) ?? [];

    factorList.push(factor);
    groupMap.set(factor.factor_group, factorList);
    typeGroups.set(factor.factor_type, groupMap);
  }

  return [...typeGroups.entries()].map(([factorType, groupMap]) => {
    const children = [...groupMap.entries()].map(([factorGroup, groupFactors]) => ({
      id: `${factorType}:${factorGroup}`,
      name: formatGroupName(factorGroup),
      count: groupFactors.length,
      filters: groupFactors.map((factor) => ({
        id: factor.factor_id,
        label: factor.factor_name,
        field: factor.factor_id,
        description: factor.description ?? factor.factor_id,
        unit: factor.unit ?? undefined,
        valueDirection: factor.value_direction,
        defaultOperator: defaultOperatorFor(factor.value_direction),
        defaultValue: defaultValueFor(factor.unit),
        defaultInputMode: "percentile" as const,
      })),
    }));

    return {
      id: factorType,
      name: formatGroupName(factorType),
      count: children.reduce((sum, child) => sum + child.count, 0),
      children,
    };
  });
}

function mapConditionToDto(condition: QuantScreenerRequest["conditions"][number]): FactorConditionDto {
  if (condition.inputMode === "percentile") {
    return {
      factor_id: condition.filterId,
      mode: "top_percent",
      top_percent: condition.percentile,
      rank_direction: "catalog",
      alias: condition.filterId,
    };
  }

  return {
    factor_id: condition.filterId,
    mode: "threshold",
    operator: condition.operator,
    value: condition.value,
    alias: condition.filterId,
  };
}

function toPercent(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return null;
  }

  return value * 100;
}

function navToCumulativePercent(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return null;
  }

  return (value - 1) * 100;
}

function sortBenchmarkNames(names: string[]) {
  const preferredOrder = ["KOSPI200", "KOSPI 200", "KOSDAQ"];

  return [...new Set(names)].sort((left, right) => {
    const leftIndex = preferredOrder.indexOf(left);
    const rightIndex = preferredOrder.indexOf(right);

    if (leftIndex !== -1 || rightIndex !== -1) {
      return (leftIndex === -1 ? Number.MAX_SAFE_INTEGER : leftIndex) -
        (rightIndex === -1 ? Number.MAX_SAFE_INTEGER : rightIndex);
    }

    return left.localeCompare(right, "ko-KR", { numeric: true });
  });
}

function getBenchmarkNames(response: FactorBacktestResponseDto) {
  return sortBenchmarkNames([
    ...response.equity_curve.flatMap((point) => Object.keys(point.benchmark_navs ?? {})),
    ...response.annual_returns.flatMap((item) => Object.keys(item.benchmark_returns ?? {})),
  ]);
}

function mapBenchmarkValues(
  benchmarkValues: Record<string, number | null> | null | undefined,
  mapper: (value: number | null | undefined) => number | null,
): Record<string, number | null> {
  if (!benchmarkValues) {
    return {};
  }

  return Object.fromEntries(
    sortBenchmarkNames(Object.keys(benchmarkValues)).map((name) => [
      name,
      mapper(benchmarkValues[name]),
    ]),
  );
}

function mapBacktestResponse(response: FactorBacktestResponseDto): FactorBacktestResponse {
  const benchmarkNames = getBenchmarkNames(response);

  return {
    summary: {
      startDate: response.summary.start_date,
      endDate: response.summary.end_date,
      rebalanceFrequency: response.summary.rebalance_frequency,
      cumulativeReturn: toPercent(response.summary.cumulative_return),
      cagr: toPercent(response.summary.cagr),
      maxDrawdown: toPercent(response.summary.max_drawdown),
      volatility: toPercent(response.summary.volatility),
      sharpe: response.summary.sharpe,
      winRate: toPercent(response.summary.win_rate),
      rebalanceCount: response.summary.rebalance_count,
      benchmarkNames,
    },
    equityCurve: response.equity_curve.map((point) => ({
      date: point.trade_date,
      strategy: navToCumulativePercent(point.strategy_nav) ?? 0,
      benchmarks: mapBenchmarkValues(point.benchmark_navs, navToCumulativePercent),
      cash: null,
    })),
    annualReturns: response.annual_returns
      .map((item) => ({
        year: item.year,
        strategy: toPercent(item.strategy_return) ?? 0,
        benchmarkReturns: mapBenchmarkValues(item.benchmark_returns, toPercent),
        excessReturns: mapBenchmarkValues(item.excess_returns, toPercent),
      }))
      .sort((left, right) => right.year - left.year),
    rebalanceHistory: response.rebalance_history.map((rebalance) => ({
      rebalanceDate: rebalance.rebalance_date,
      signalDate: rebalance.signal_date,
      positions: rebalance.positions.map((position) => ({
        securityId: position.security_id,
        ticker: position.ticker ?? position.security_id,
        name: position.stock_name ?? position.security_id,
        weight: position.weight,
        score: position.score,
      })),
    })),
    warnings: response.warnings ?? [],
  };
}

function mapColumn(column: FactorScreenColumnDto): QuantScreenerColumn {
  return {
    key: column.key,
    label: column.label,
    columnType: column.column_type,
    order: column.order,
    factorId: column.factor_id,
    factorName: column.factor_name,
    unit: column.unit,
    valueDirection: column.value_direction,
  };
}

function mapFactorValue(value: FactorScreenValueDto): ScreenedFactorValue {
  return {
    factorId: value.factor_id,
    factorName: value.factor_name,
    conditionId: value.condition_id,
    value: value.value,
    tradeDate: value.trade_date,
    unit: value.unit,
    valueDirection: value.value_direction,
  };
}

function mapScreeningResponse(
  response: FactorScreenResponseDto,
  request: QuantScreenerRequest,
): QuantScreenerResponse {
  return {
    total: response.total_count,
    page: request.page,
    pageSize: request.pageSize,
    matchedConditionCounts: {},
    summary: {
      screeningResult: response.summary.screening_result,
      totalCount: response.summary.total_count,
      displayedCount: response.summary.displayed_count,
    },
    fixedColumns: response.fixed_columns.map(mapColumn),
    factorColumns: response.factor_columns.map(mapColumn),
    rows: response.rows.map((row) => ({
      rank: row.rank,
      securityId: row.security_id,
      ticker: row.ticker ?? row.security_id,
      name: row.stock_name ?? row.security_id,
      market: row.country ?? request.market,
      marketCap: row.market_cap,
      sectorCode: row.sector_code,
      percentile: row.percentile,
      matchedConditionCount: row.matched_condition_count,
      matchedConditions: row.matched_conditions,
      latestTradeDate: row.latest_trade_date,
      factorValues: Object.fromEntries(
        Object.entries(row.factor_values).map(([key, value]) => [key, mapFactorValue(value)]),
      ),
    })),
  };
}

function createMockScreeningResponse(request: QuantScreenerRequest): QuantScreenerResponse {
  return {
    total: mockRows.length,
    page: request.page,
    pageSize: request.pageSize,
    rows: mockRows.slice(0, request.pageSize),
    matchedConditionCounts: {},
    summary: {
      screeningResult: mockRows.length > 0 ? "OK" : "EMPTY",
      totalCount: mockRows.length,
      displayedCount: Math.min(mockRows.length, request.pageSize),
    },
    fixedColumns: [
      { key: "rank", label: "#", columnType: "rank", order: 1 },
      { key: "ticker", label: "티커", columnType: "ticker", order: 2 },
      { key: "stock_name", label: "종목명", columnType: "name", order: 3 },
      { key: "country", label: "국가", columnType: "country", order: 4 },
      { key: "market_cap", label: "시가총액", columnType: "market_cap", order: 5, unit: "mil" },
    ],
    factorColumns: [
      {
        key: "roe_0",
        label: "ROE",
        columnType: "factor",
        order: 100,
        factorId: "roe",
        factorName: "ROE",
        unit: "ratio",
        valueDirection: "HIGHER_BETTER",
      },
      {
        key: "per_1",
        label: "PER",
        columnType: "factor",
        order: 101,
        factorId: "per",
        factorName: "PER",
        unit: "times",
        valueDirection: "LOWER_BETTER",
      },
    ],
  };
}

export async function fetchMarketCatalog(): Promise<MarketOption[]> {
  if (useMockApi) {
    return defaultMarketOptions;
  }

  for (const path of ["/api/markets", "/api/countries"]) {
    try {
      const markets = await getJson<MarketCatalogDto>(path);
      const mappedMarkets = normalizeMarketCatalog(markets)
        .map(mapMarket)
        .filter((market): market is MarketOption => market !== null);

      if (mappedMarkets.length > 0) {
        return mappedMarkets;
      }
    } catch {
      // Market metadata is optional in older API versions.
    }
  }

  return defaultMarketOptions;
}

export async function fetchIndustryCatalog(_market: string): Promise<IndustryOption[]> {
  if (useMockApi) {
    return mockIndustries;
  }

  const sectors = await getJson<SectorDto[]>("/api/sectors");
  return sectors.map(mapSector);
}

export async function fetchFilterCatalog(): Promise<FilterGroup[]> {
  if (useMockApi) {
    return mockFilterCatalog;
  }

  const factors = await getJson<FactorDto[]>("/api/factors?active_only=true");
  return mapFactorsToFilterCatalog(factors);
}

export async function runQuantScreening(
  request: QuantScreenerRequest,
): Promise<QuantScreenerResponse> {
  if (useMockApi) {
    await new Promise((resolve) => window.setTimeout(resolve, 350));
    return createMockScreeningResponse(request);
  }

  const payload: FactorScreenRequestDto = {
    conditions: request.conditions.map(mapConditionToDto),
    sector_codes: request.industries.length > 0 ? request.industries : null,
    match_mode: "all",
    limit: request.pageSize,
  };

  const response = await fetch(`${apiBaseUrl}/api/factor-screen/screen`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(`Screening failed: ${response.status}`);
  }

  return mapScreeningResponse((await response.json()) as FactorScreenResponseDto, request);
}

export async function runFactorBacktest(
  request: FactorBacktestRequest,
): Promise<FactorBacktestResponse> {
  const payload = {
    start_date: request.startDate,
    end_date: request.endDate,
    rebalance_frequency: request.rebalanceFrequency,
    conditions: request.conditions.map(mapConditionToDto),
    sector_codes: request.industries.length > 0 ? request.industries : null,
    match_mode: "all",
    market: request.market,
  };

  const response = await fetch(`${apiBaseUrl}/api/backtests/factor`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });

  if (!response.ok) {
    throw new Error(`Backtest failed: ${response.status}`);
  }

  return mapBacktestResponse((await response.json()) as FactorBacktestResponseDto);
}
