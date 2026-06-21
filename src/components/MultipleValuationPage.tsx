import { useEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  BarChart3,
  Calculator,
  CircleDollarSign,
  Landmark,
  LineChart,
  ShieldCheck,
} from "lucide-react";
import {
  fetchMultipleValuationBands,
  type MultipleValuationApiComparisonRow,
  type MultipleValuationApiFactor,
  type MultipleValuationApiResponse,
} from "../api/multipleValuationApi";
import type { StockAnalysisResponse } from "../types/stockAnalysis";

type BenchmarkMode = "history" | "industry" | "market";
type FactorDirection = "lower" | "higher";
type PriceModel = "multiple" | "yield" | "compareOnly";

type MultipleFactor = {
  id: string;
  label: string;
  description: string;
  current: number | null;
  benchmark?: number | null;
  historyMedian: number | null;
  historyP75: number | null;
  industryAvg: number | null;
  marketAvg: number | null;
  nasdaqAvg: number | null;
  buyPrice?: number | null;
  fairPrice?: number | null;
  sellPrice?: number | null;
  targetSource?: string;
  direction: FactorDirection;
  priceModel: PriceModel;
  history: number[];
};

type MultipleValuationPageProps = {
  stockCode: string;
  data: StockAnalysisResponse | null;
  isLoading: boolean;
  initialApiData?: MultipleValuationApiResponse | null;
  isInitialApiLoading?: boolean;
  initialApiError?: string;
};

const priceFormatter = new Intl.NumberFormat("ko-KR", {
  maximumFractionDigits: 0,
});

const numberFormatter = new Intl.NumberFormat("ko-KR", {
  maximumFractionDigits: 2,
  minimumFractionDigits: 0,
});

const percentFormatter = new Intl.NumberFormat("ko-KR", {
  maximumFractionDigits: 1,
  minimumFractionDigits: 0,
});

const adjustmentMin = 0;
const adjustmentMax = 60;
const adjustmentStep = 1;

const benchmarkOptions: Array<{
  id: BenchmarkMode;
  label: string;
  description: string;
}> = [
  {
    id: "history",
    label: "3Y 히스토리",
    description: "동일 종목의 과거 멀티플 중앙값을 기준으로 계산",
  },
  {
    id: "industry",
    label: "산업 평균",
    description: "속한 산업 내 평균 멀티플과 비교",
  },
  {
    id: "market",
    label: "시장 평균",
    description: "시장 또는 대표지수 평균 멀티플과 비교",
  },
];

const factorTemplates: MultipleFactor[] = [
  {
    id: "ev_nopat",
    label: "EV/NOPAT",
    description: "영업세후이익 대비 기업가치",
    current: 12.4,
    historyMedian: 10.8,
    historyP75: 13.2,
    industryAvg: 9.6,
    marketAvg: 18.5,
    nasdaqAvg: 21.2,
    direction: "lower",
    priceModel: "multiple",
    history: [8.9, 9.6, 10.2, 11.1, 12.7, 11.4, 10.8, 13.2, 12.4],
  },
  {
    id: "ev_ebitda",
    label: "EV/EBITDA",
    description: "감가상각 전 영업현금흐름 대비 기업가치",
    current: 16.0,
    historyMedian: 3.5,
    historyP75: 3.7,
    industryAvg: 3.3,
    marketAvg: 18.8,
    nasdaqAvg: 22.4,
    direction: "lower",
    priceModel: "multiple",
    history: [2.8, 3.2, 3.4, 3.5, 3.8, 4.2, 6.6, 10.8, 16.0],
  },
  {
    id: "per",
    label: "PER",
    description: "주가수익비율",
    current: 5.6,
    historyMedian: 7.0,
    historyP75: 8.1,
    industryAvg: 7.4,
    marketAvg: 24.7,
    nasdaqAvg: 36.9,
    direction: "lower",
    priceModel: "multiple",
    history: [6.8, 7.6, 8.1, 7.3, 6.9, 6.2, 5.9, 5.7, 5.6],
  },
  {
    id: "pbr",
    label: "PBR",
    description: "주가순자산비율",
    current: 0.7,
    historyMedian: 0.8,
    historyP75: 0.8,
    industryAvg: 0.7,
    marketAvg: 4.8,
    nasdaqAvg: 9.8,
    direction: "lower",
    priceModel: "multiple",
    history: [0.9, 0.8, 0.8, 0.7, 0.7, 0.8, 0.7, 0.7, 0.7],
  },
  {
    id: "eps_yoy",
    label: "EPS YoY",
    description: "주당순이익 전년 대비 성장률",
    current: -1.5,
    historyMedian: 7.8,
    historyP75: 13.6,
    industryAvg: 4.2,
    marketAvg: 8.6,
    nasdaqAvg: 11.7,
    direction: "higher",
    priceModel: "compareOnly",
    history: [12.4, 9.8, 8.1, 4.6, 2.3, -0.4, -1.2, -2.7, -1.5],
  },
  {
    id: "fcfpr",
    label: "FCFPR",
    description: "잉여현금흐름 기반 가격배수",
    current: 9.2,
    historyMedian: 8.4,
    historyP75: 10.6,
    industryAvg: 8.8,
    marketAvg: 21.0,
    nasdaqAvg: 28.3,
    direction: "lower",
    priceModel: "multiple",
    history: [7.1, 7.8, 8.4, 8.9, 9.5, 10.6, 8.8, 9.1, 9.2],
  },
  {
    id: "rd_market_cap",
    label: "R&D / Market Cap",
    description: "시가총액 대비 연구개발 투자 강도",
    current: 4.1,
    historyMedian: 3.6,
    historyP75: 4.4,
    industryAvg: 5.2,
    marketAvg: 2.8,
    nasdaqAvg: 6.1,
    direction: "higher",
    priceModel: "compareOnly",
    history: [2.9, 3.1, 3.3, 3.6, 3.9, 4.4, 4.2, 4.0, 4.1],
  },
  {
    id: "rpr",
    label: "RPR",
    description: "매출/수익성 조합 배수",
    current: 6.3,
    historyMedian: 5.8,
    historyP75: 6.6,
    industryAvg: 5.2,
    marketAvg: 9.4,
    nasdaqAvg: 11.0,
    direction: "lower",
    priceModel: "multiple",
    history: [5.1, 5.4, 5.8, 6.1, 6.6, 6.9, 6.4, 6.2, 6.3],
  },
  {
    id: "fcf_ev_yield",
    label: "FCF/EV Yield",
    description: "기업가치 대비 잉여현금흐름 수익률",
    current: 5.8,
    historyMedian: 6.4,
    historyP75: 7.1,
    industryAvg: 6.0,
    marketAvg: 4.2,
    nasdaqAvg: 3.6,
    direction: "higher",
    priceModel: "yield",
    history: [7.4, 7.1, 6.8, 6.4, 6.1, 5.9, 5.7, 5.6, 5.8],
  },
  {
    id: "peg",
    label: "PEG",
    description: "성장률 보정 PER",
    current: 0.62,
    historyMedian: 0.82,
    historyP75: 1.05,
    industryAvg: 0.92,
    marketAvg: 1.44,
    nasdaqAvg: 1.68,
    direction: "lower",
    priceModel: "multiple",
    history: [0.88, 0.96, 1.05, 0.9, 0.82, 0.74, 0.68, 0.64, 0.62],
  },
  {
    id: "psr",
    label: "PSR",
    description: "주가매출비율",
    current: 1.1,
    historyMedian: 1.1,
    historyP75: 1.1,
    industryAvg: 0.4,
    marketAvg: 3.8,
    nasdaqAvg: 7.3,
    direction: "lower",
    priceModel: "multiple",
    history: [1.2, 1.1, 1.0, 1.0, 1.1, 1.2, 1.1, 1.1, 1.1],
  },
  {
    id: "pcr",
    label: "PCR",
    description: "주가현금흐름비율",
    current: 4.4,
    historyMedian: 5.1,
    historyP75: 5.8,
    industryAvg: 4.8,
    marketAvg: 13.2,
    nasdaqAvg: 18.6,
    direction: "lower",
    priceModel: "multiple",
    history: [5.8, 5.3, 5.1, 4.8, 4.6, 4.4, 4.5, 4.3, 4.4],
  },
];

const comparisonColumns = [
  { id: "per", label: "PER", suffix: "x" },
  { id: "pbr", label: "PBR", suffix: "x" },
  { id: "psr", label: "PSR", suffix: "x" },
  { id: "ev_ebitda", label: "EV/EBITDA", suffix: "x" },
  { id: "fcf_ev_yield", label: "FCF/EV Yield", suffix: "%" },
  { id: "peg", label: "PEG", suffix: "x" },
];

const factorIdAliases: Record<string, string[]> = {
  ev_nopat: ["ev_nopat", "evnopat", "ev_to_nopat", "ev/nopat"],
  ev_ebitda: ["ev_ebitda", "evebitda", "ev_to_ebitda", "ev/ebitda"],
  per: ["per", "pe", "p_e", "price_earnings", "price_to_earnings"],
  pbr: ["pbr", "pb", "p_b", "price_book", "price_to_book"],
  eps_yoy: ["eps_yoy", "eps_yoy_pct", "eps_growth_yoy", "eps_growth", "epsYoY"],
  fcfpr: ["fcfpr", "fcf_pr", "fcf_price_ratio"],
  rd_market_cap: [
    "rd_market_cap",
    "rnd_to_market_cap",
    "r_d_market_cap",
    "r_and_d_market_cap",
    "rd_to_market_cap",
  ],
  rpr: ["rpr"],
  fcf_ev_yield: [
    "fcf_ev_yield",
    "fcfevyield",
    "fcf_to_ev_yield",
    "fcf/ev_yield",
  ],
  peg: ["peg"],
  psr: ["psr", "ps", "p_s", "price_sales", "price_to_sales"],
  pcr: ["pcr", "pc", "p_c", "price_cash_flow", "price_to_cash_flow"],
};

function formatPrice(value: number | null) {
  if (value === null || !Number.isFinite(value)) {
    return "N/A";
  }

  return `${priceFormatter.format(Math.round(value))}원`;
}

function formatFactor(value: number | null | undefined, suffix = "x") {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "N/A";
  }

  return `${numberFormatter.format(value)}${suffix}`;
}

function formatOptionalFactor(value: number | null | undefined, suffix = "x") {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "N/A";
  }

  return formatFactor(value, suffix);
}

function clampAdjustment(value: number) {
  if (!Number.isFinite(value)) {
    return adjustmentMin;
  }

  return Math.min(adjustmentMax, Math.max(adjustmentMin, Math.round(value)));
}

function factorSuffix(factorId: string) {
  return factorId === "eps_yoy" ||
    factorId === "rd_market_cap" ||
    factorId === "fcf_ev_yield"
    ? "%"
    : "x";
}

function normalizeComparableId(value: string) {
  return value
    .trim()
    .toLowerCase()
    .replace(/&/g, "")
    .replace(/\+/g, "")
    .replace(/[/-]+/g, "_")
    .replace(/\s+/g, "_")
    .replace(/__+/g, "_");
}

function p75(values: number[]) {
  if (values.length === 0) {
    return null;
  }

  const sorted = [...values].sort((left, right) => left - right);
  const index = Math.min(
    sorted.length - 1,
    Math.ceil(sorted.length * 0.75) - 1,
  );

  return sorted[index];
}

function apiFactorLookupKey(factor: MultipleValuationApiFactor) {
  const candidates = [factor.id, factor.label ?? ""]
    .filter(Boolean)
    .map(normalizeComparableId);

  for (const [templateId, aliases] of Object.entries(factorIdAliases)) {
    if (
      aliases
        .map(normalizeComparableId)
        .some((alias) => candidates.includes(alias))
    ) {
      return templateId;
    }
  }

  return candidates[0] ?? factor.id;
}

function mergeApiFactor(
  template: MultipleFactor,
  apiFactor: MultipleValuationApiFactor | undefined,
): MultipleFactor {
  if (!apiFactor) {
    return template;
  }

  const apiHistory =
    apiFactor.history && apiFactor.history.length > 0
      ? apiFactor.history
      : undefined;
  const apiMedian = apiHistory ? median(apiHistory) : null;
  const apiP75 = apiHistory ? p75(apiHistory) : null;

  return {
    ...template,
    label: apiFactor.label ?? template.label,
    description: apiFactor.description ?? template.description,
    current:
      apiFactor.current === undefined ? template.current : apiFactor.current,
    benchmark:
      apiFactor.benchmark === undefined
        ? template.benchmark
        : apiFactor.benchmark,
    historyMedian:
      apiFactor.historyMedian === undefined
        ? (apiMedian ?? template.historyMedian)
        : apiFactor.historyMedian,
    historyP75:
      apiFactor.historyP75 === undefined
        ? (apiP75 ?? template.historyP75)
        : apiFactor.historyP75,
    industryAvg:
      apiFactor.industryAvg === undefined
        ? template.industryAvg
        : apiFactor.industryAvg,
    marketAvg:
      apiFactor.marketAvg === undefined
        ? template.marketAvg
        : apiFactor.marketAvg,
    nasdaqAvg:
      apiFactor.nasdaqAvg === undefined
        ? template.nasdaqAvg
        : apiFactor.nasdaqAvg,
    buyPrice:
      apiFactor.buyPrice === undefined ? template.buyPrice : apiFactor.buyPrice,
    fairPrice:
      apiFactor.fairPrice === undefined
        ? template.fairPrice
        : apiFactor.fairPrice,
    sellPrice:
      apiFactor.sellPrice === undefined
        ? template.sellPrice
        : apiFactor.sellPrice,
    targetSource: apiFactor.targetSource ?? template.targetSource,
    direction: apiFactor.direction ?? template.direction,
    priceModel: apiFactor.priceModel ?? template.priceModel,
    history: apiHistory ?? template.history,
  };
}

function createApiOnlyFactor(
  apiFactor: MultipleValuationApiFactor,
): MultipleFactor {
  const id = apiFactorLookupKey(apiFactor);
  const history =
    apiFactor.history && apiFactor.history.length > 0 ? apiFactor.history : [];
  const fallbackMedian =
    history.length > 0
      ? median(history)
      : (apiFactor.benchmark ?? apiFactor.current ?? null);
  const label = apiFactor.label ?? apiFactor.id.toUpperCase();
  const isYield = /yield|yoy|r&d|rd/i.test(label);

  return {
    id,
    label,
    description: apiFactor.description ?? label,
    current: apiFactor.current ?? null,
    benchmark: apiFactor.benchmark,
    historyMedian: apiFactor.historyMedian ?? fallbackMedian,
    historyP75: apiFactor.historyP75 ?? p75(history) ?? fallbackMedian,
    industryAvg: apiFactor.industryAvg ?? apiFactor.benchmark ?? fallbackMedian,
    marketAvg: apiFactor.marketAvg ?? apiFactor.benchmark ?? fallbackMedian,
    nasdaqAvg:
      apiFactor.nasdaqAvg ??
      apiFactor.marketAvg ??
      apiFactor.benchmark ??
      fallbackMedian,
    buyPrice: apiFactor.buyPrice,
    fairPrice: apiFactor.fairPrice,
    sellPrice: apiFactor.sellPrice,
    targetSource: apiFactor.targetSource,
    direction: apiFactor.direction ?? (isYield ? "higher" : "lower"),
    priceModel: apiFactor.priceModel ?? (isYield ? "yield" : "multiple"),
    history: history.length > 0 ? history : [],
  };
}

function getBenchmarkValue(factor: MultipleFactor, mode: BenchmarkMode) {
  if (mode === "industry") {
    return factor.industryAvg ?? factor.benchmark ?? factor.historyMedian;
  }

  if (mode === "market") {
    return (
      factor.nasdaqAvg ??
      factor.marketAvg ??
      factor.benchmark ??
      factor.historyMedian
    );
  }

  return (
    factor.historyMedian ??
    factor.benchmark ??
    factor.industryAvg ??
    factor.marketAvg
  );
}

function apiBandBasis(
  mode: BenchmarkMode,
): "historical" | "industry" | "market" {
  return mode === "history" ? "historical" : mode;
}

function isApiFairPriceForMode(factor: MultipleFactor, mode: BenchmarkMode) {
  if (factor.fairPrice === undefined) {
    return false;
  }

  if (!factor.targetSource) {
    return false;
  }

  const source = normalizeComparableId(factor.targetSource);
  const expected = apiBandBasis(mode);

  return (
    source === expected ||
    (mode === "history" && source === "historical_median")
  );
}

function calculateFairPrice(
  currentPrice: number,
  factor: MultipleFactor,
  benchmark: number | null | undefined,
) {
  if (
    factor.priceModel === "compareOnly" ||
    factor.current === null ||
    benchmark === null ||
    benchmark === undefined ||
    factor.current <= 0 ||
    benchmark <= 0 ||
    currentPrice <= 0
  ) {
    return null;
  }

  if (factor.priceModel === "yield") {
    return currentPrice * (factor.current / benchmark);
  }

  return currentPrice * (benchmark / factor.current);
}

function median(values: number[]) {
  const sorted = [...values].sort((left, right) => left - right);
  const middle = Math.floor(sorted.length / 2);

  return sorted.length % 2 === 0
    ? (sorted[middle - 1] + sorted[middle]) / 2
    : sorted[middle];
}

function getFactorTone(
  factor: MultipleFactor,
  benchmark: number | null | undefined,
) {
  if (
    factor.current === null ||
    benchmark === null ||
    benchmark === undefined
  ) {
    return "neutral";
  }

  const diff =
    factor.direction === "lower"
      ? (benchmark - factor.current) / Math.abs(benchmark || 1)
      : (factor.current - benchmark) / Math.abs(benchmark || 1);

  if (diff >= 0.15) {
    return "discount";
  }

  if (diff <= -0.15) {
    return "premium";
  }

  return "neutral";
}

function toneLabel(tone: string) {
  if (tone === "discount") {
    return "Discount";
  }

  if (tone === "premium") {
    return "Premium";
  }

  return "Fair";
}

function Sparkline({ values }: { values: number[] }) {
  if (values.length === 0) {
    return <div className="valuation-sparkline empty">N/A</div>;
  }

  const min = Math.min(...values);
  const max = Math.max(...values);
  const range = Math.max(max - min, 0.01);

  return (
    <div className="valuation-sparkline" aria-hidden="true">
      {values.map((value, index) => (
        <span
          key={`${value}-${index}`}
          style={{ height: `${22 + ((value - min) / range) * 54}px` }}
        />
      ))}
    </div>
  );
}

export function MultipleValuationPage({
  stockCode,
  data,
  isLoading,
  initialApiData = null,
  isInitialApiLoading = false,
  initialApiError = "",
}: MultipleValuationPageProps) {
  const [benchmarkMode, setBenchmarkMode] = useState<BenchmarkMode>("history");
  const [safetyMargin, setSafetyMargin] = useState(20);
  const [sellPremium, setSellPremium] = useState(15);
  const [selectedFactorId, setSelectedFactorId] = useState("per");
  const [apiData, setApiData] = useState<MultipleValuationApiResponse | null>(
    initialApiData,
  );
  const [apiError, setApiError] = useState(initialApiError);
  const [isApiLoading, setIsApiLoading] = useState(isInitialApiLoading);

  useEffect(() => {
    let ignore = false;

    if (
      benchmarkMode === "history" &&
      (isInitialApiLoading ||
        initialApiError ||
        initialApiData?.stockCode === stockCode)
    ) {
      setApiData(initialApiData);
      setApiError(initialApiError);
      setIsApiLoading(isInitialApiLoading);
      return () => {
        ignore = true;
      };
    }

    setIsApiLoading(true);
    setApiError("");
    void fetchMultipleValuationBands(stockCode, {
      bandBasis: apiBandBasis(benchmarkMode),
    })
      .then((response) => {
        if (!ignore) {
          setApiData(response);
        }
      })
      .catch((error: Error) => {
        if (!ignore) {
          setApiData(null);
          setApiError(error.message);
        }
      })
      .finally(() => {
        if (!ignore) {
          setIsApiLoading(false);
        }
      });

    return () => {
      ignore = true;
    };
  }, [
    benchmarkMode,
    initialApiData,
    initialApiError,
    isInitialApiLoading,
    stockCode,
  ]);

  const apiFactorsById = useMemo(() => {
    const entries = (apiData?.factors ?? []).map(
      (factor) => [apiFactorLookupKey(factor), factor] as const,
    );

    return new Map(entries);
  }, [apiData?.factors]);
  const multipleFactors = useMemo(() => {
    const templateIds = new Set(factorTemplates.map((template) => template.id));
    const mergedTemplates = factorTemplates.map((template) =>
      mergeApiFactor(template, apiFactorsById.get(template.id)),
    );
    const apiOnlyFactors = [...apiFactorsById.entries()]
      .filter(([id]) => !templateIds.has(id))
      .map(([, factor]) => createApiOnlyFactor(factor));

    return [...mergedTemplates, ...apiOnlyFactors];
  }, [apiFactorsById]);
  const latestPrice =
    apiData?.currentPrice ?? data?.summary.latestPrice ?? 32700;
  const latestDate =
    apiData?.asOfDate ?? data?.summary.latestDate ?? "2026-01-09";
  const stockName = apiData?.stockName ?? data?.summary.name ?? stockCode;
  const industryLabel =
    apiData?.industryLabel ??
    data?.summary.industryLabel ??
    "Industry peer group";
  const selectedBenchmark = benchmarkOptions.find(
    (option) => option.id === benchmarkMode,
  );
  const updateSafetyMargin = (value: number) =>
    setSafetyMargin(clampAdjustment(value));
  const updateSellPremium = (value: number) =>
    setSellPremium(clampAdjustment(value));

  const factorRows = useMemo(() => {
    return multipleFactors.map((factor) => {
      const benchmark = getBenchmarkValue(factor, benchmarkMode);
      const calculatedFairPrice = calculateFairPrice(
        latestPrice,
        factor,
        benchmark,
      );
      const fairPrice = isApiFairPriceForMode(factor, benchmarkMode)
        ? factor.fairPrice === undefined
          ? calculatedFairPrice
          : factor.fairPrice
        : calculatedFairPrice;

      return {
        ...factor,
        benchmark,
        fairPrice,
        buyPrice:
          fairPrice === null ? null : fairPrice * (1 - safetyMargin / 100),
        sellPrice:
          fairPrice === null ? null : fairPrice * (1 + sellPremium / 100),
        tone: getFactorTone(factor, benchmark),
      };
    });
  }, [benchmarkMode, latestPrice, multipleFactors, safetyMargin, sellPremium]);

  const pricedRows = factorRows.filter(
    (factor): factor is (typeof factorRows)[number] & { fairPrice: number } =>
      typeof factor.fairPrice === "number" && Number.isFinite(factor.fairPrice),
  );
  const fallbackFairValue =
    pricedRows.length > 0
      ? median(pricedRows.map((row) => row.fairPrice))
      : null;
  const apiCentralBand = apiData?.centralBand;
  const fairValue =
    typeof apiCentralBand?.fairPrice === "number" &&
    Number.isFinite(apiCentralBand.fairPrice)
      ? apiCentralBand.fairPrice
      : fallbackFairValue;
  const buyValue =
    fairValue === null ? null : fairValue * (1 - safetyMargin / 100);
  const sellValue =
    fairValue === null ? null : fairValue * (1 + sellPremium / 100);
  const selectedFactor =
    factorRows.find((factor) => factor.id === selectedFactorId) ??
    factorRows[0];
  const upside =
    fairValue === null ? null : ((fairValue - latestPrice) / latestPrice) * 100;
  const benchmarkLabel = selectedBenchmark?.label ?? "Benchmark";
  const comparisonRows = useMemo<MultipleValuationApiComparisonRow[]>(() => {
    if ((apiData?.comparisonRows ?? []).length > 0) {
      return apiData?.comparisonRows ?? [];
    }

    const byId = new Map(factorRows.map((factor) => [factor.id, factor]));

    return [
      {
        label: "US Market Avg",
        values: {
          per: byId.get("per")?.marketAvg ?? null,
          pbr: byId.get("pbr")?.marketAvg ?? null,
          psr: byId.get("psr")?.marketAvg ?? null,
          ev_ebitda: byId.get("ev_ebitda")?.marketAvg ?? null,
          fcf_ev_yield: byId.get("fcf_ev_yield")?.marketAvg ?? null,
          peg: byId.get("peg")?.marketAvg ?? null,
        },
      },
      {
        label: "Nasdaq 100",
        values: {
          per: byId.get("per")?.nasdaqAvg ?? null,
          pbr: byId.get("pbr")?.nasdaqAvg ?? null,
          psr: byId.get("psr")?.nasdaqAvg ?? null,
          ev_ebitda: byId.get("ev_ebitda")?.nasdaqAvg ?? null,
          fcf_ev_yield: byId.get("fcf_ev_yield")?.nasdaqAvg ?? null,
          peg: byId.get("peg")?.nasdaqAvg ?? null,
        },
      },
      {
        label: "Industry Avg",
        values: {
          per: byId.get("per")?.industryAvg ?? null,
          pbr: byId.get("pbr")?.industryAvg ?? null,
          psr: byId.get("psr")?.industryAvg ?? null,
          ev_ebitda: byId.get("ev_ebitda")?.industryAvg ?? null,
          fcf_ev_yield: byId.get("fcf_ev_yield")?.industryAvg ?? null,
          peg: byId.get("peg")?.industryAvg ?? null,
        },
      },
      {
        label: stockCode,
        values: {
          per: byId.get("per")?.current ?? null,
          pbr: byId.get("pbr")?.current ?? null,
          psr: byId.get("psr")?.current ?? null,
          ev_ebitda: byId.get("ev_ebitda")?.current ?? null,
          fcf_ev_yield: byId.get("fcf_ev_yield")?.current ?? null,
          peg: byId.get("peg")?.current ?? null,
        },
      },
    ];
  }, [apiData?.comparisonRows, factorRows, stockCode]);

  return (
    <section className="valuation-page">
      <header className="valuation-header">
        <div>
          <p className="valuation-eyebrow">Multiple valuation band</p>
          <h1>
            {stockName} <span>({stockCode})</span>
          </h1>
          <p>
            {industryLabel} 기준으로 time series와 cross section 멀티플을 함께
            비교합니다.
          </p>
        </div>
        <div className="valuation-header-metrics">
          <article>
            <span>현재가</span>
            <strong>{formatPrice(latestPrice)}</strong>
            <em>{latestDate}</em>
          </article>
          <article>
            <span>중앙 적정가</span>
            <strong>{formatPrice(fairValue)}</strong>
            <em>
              {upside === null
                ? "N/A"
                : `${upside >= 0 ? "+" : ""}${percentFormatter.format(upside)}%`}
            </em>
          </article>
        </div>
      </header>

      {(isApiLoading || isInitialApiLoading || (isLoading && !data)) && (
        <p className="valuation-note">
          종목 데이터를 불러오는 동안 예시 팩터로 밴드를 계산합니다.
        </p>
      )}
      {apiError && (
        <p className="valuation-note">
          {apiError} 현재 화면은 폴백 데이터로 표시합니다.
        </p>
      )}

      <section className="valuation-control-bar">
        <div className="valuation-control-group">
          <span>비교 기준</span>
          <div className="valuation-segmented" aria-label="멀티플 비교 기준">
            {benchmarkOptions.map((option) => (
              <button
                className={option.id === benchmarkMode ? "active" : ""}
                key={option.id}
                type="button"
                onClick={() => setBenchmarkMode(option.id)}
              >
                {option.label}
              </button>
            ))}
          </div>
          <em>{selectedBenchmark?.description}</em>
        </div>

        <div className="valuation-slider">
          <span>
            안전마진 <strong>{safetyMargin}%</strong>
          </span>
          <input
            aria-label="안전마진 슬라이더"
            max={adjustmentMax}
            min={adjustmentMin}
            step={adjustmentStep}
            type="range"
            value={safetyMargin}
            onChange={(event) => updateSafetyMargin(Number(event.target.value))}
          />
        </div>

        <div className="valuation-slider">
          <span>
            매도 프리미엄 <strong>{sellPremium}%</strong>
          </span>
          <input
            aria-label="매도 프리미엄 슬라이더"
            max={adjustmentMax}
            min={adjustmentMin}
            step={adjustmentStep}
            type="range"
            value={sellPremium}
            onChange={(event) => updateSellPremium(Number(event.target.value))}
          />
        </div>
      </section>

      <section className="valuation-band-grid">
        <article className="valuation-band-card buy">
          <ShieldCheck size={18} />
          <span>매수 밴드</span>
          <strong>{formatPrice(buyValue)}</strong>
          <em>적정가에서 {safetyMargin}% 안전마진 적용</em>
        </article>
        <article className="valuation-band-card fair">
          <CircleDollarSign size={18} />
          <span>적정가 기준</span>
          <strong>{formatPrice(fairValue)}</strong>
          <em>{benchmarkLabel} 가격 환산 중앙값</em>
        </article>
        <article className="valuation-band-card sell">
          <Calculator size={18} />
          <span>매도 밴드</span>
          <strong>{formatPrice(sellValue)}</strong>
          <em>적정가에서 {sellPremium}% 프리미엄 적용</em>
        </article>
      </section>

      <section className="valuation-section">
        <div className="valuation-section-title">
          <BarChart3 size={17} />
          <div>
            <h2>핵심 멀티플 비교</h2>
            <p>
              현재 멀티플을 선택한 벤치마크와 비교하고 가격 밴드를 계산합니다.
            </p>
          </div>
        </div>
        <div className="valuation-table-wrap">
          <table className="valuation-table">
            <thead>
              <tr>
                <th>지표</th>
                <th>현재</th>
                <th>{benchmarkLabel}</th>
                <th>평가</th>
                <th>매수</th>
                <th>적정</th>
                <th>매도</th>
              </tr>
            </thead>
            <tbody>
              {factorRows.map((factor) => (
                <tr
                  className={factor.id === selectedFactorId ? "selected" : ""}
                  key={factor.id}
                  onClick={() => setSelectedFactorId(factor.id)}
                >
                  <td>
                    <strong>{factor.label}</strong>
                    <span>{factor.description}</span>
                  </td>
                  <td>
                    {formatFactor(factor.current, factorSuffix(factor.id))}
                  </td>
                  <td>
                    {formatFactor(factor.benchmark, factorSuffix(factor.id))}
                  </td>
                  <td>
                    <span className={`valuation-pill ${factor.tone}`}>
                      {factor.tone === "discount" && <ArrowDown size={12} />}
                      {factor.tone === "premium" && <ArrowUp size={12} />}
                      {toneLabel(factor.tone)}
                    </span>
                  </td>
                  <td>{formatPrice(factor.buyPrice)}</td>
                  <td>{formatPrice(factor.fairPrice)}</td>
                  <td>{formatPrice(factor.sellPrice)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="valuation-section">
        <div className="valuation-section-title">
          <Landmark size={17} />
          <div>
            <h2>시장/산업 평균과의 상대 비교</h2>
            <p>
              시장 평균, 대표지수, 산업 평균, 현재 종목을 같은 열에서
              비교합니다.
            </p>
          </div>
        </div>
        <div className="valuation-table-wrap">
          <table className="valuation-table valuation-cross-table">
            <thead>
              <tr>
                <th>Index / Market</th>
                {comparisonColumns.map((column) => (
                  <th key={column.id}>{column.label}</th>
                ))}
              </tr>
            </thead>
            <tbody>
              {comparisonRows.map((row) => (
                <tr key={row.label}>
                  <td>
                    <strong>{row.label}</strong>
                  </td>
                  {comparisonColumns.map((column) => (
                    <td key={`${row.label}-${column.id}`}>
                      {formatOptionalFactor(
                        row.values[column.id],
                        column.suffix,
                      )}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </section>

      <section className="valuation-detail-grid">
        <article className="valuation-section valuation-detail-card">
          <div className="valuation-section-title">
            <LineChart size={17} />
            <div>
              <h2>동일 종목 히스토리 비교</h2>
              <p>{selectedFactor.label} 최근 3년 분포와 현재 위치입니다.</p>
            </div>
          </div>
          <Sparkline values={selectedFactor.history} />
          <div className="valuation-history-summary">
            <span>현재 {formatFactor(selectedFactor.current)}</span>
            <span>3Y 중앙값 {formatFactor(selectedFactor.historyMedian)}</span>
            <span>P75 {formatFactor(selectedFactor.historyP75)}</span>
          </div>
        </article>

        <article className="valuation-section valuation-detail-card">
          <div className="valuation-section-title">
            <Calculator size={17} />
            <div>
              <h2>밴드 해석 요약</h2>
              <p>팩터별 가격 환산 결과의 중앙값을 최종 밴드로 사용합니다.</p>
            </div>
          </div>
          <div className="valuation-summary-list">
            <p>
              낮을수록 좋은 멀티플은 목표 멀티플 / 현재 멀티플로 가격을
              환산합니다.
            </p>
            <p>
              수익률형 지표는 현재 수익률 / 목표 수익률로 가격을 환산합니다.
            </p>
            <p>
              EPS YoY, R&D / Market Cap처럼 가격 환산이 직접적이지 않은 지표는
              비교 신호로만 사용합니다.
            </p>
          </div>
        </article>
      </section>
    </section>
  );
}
