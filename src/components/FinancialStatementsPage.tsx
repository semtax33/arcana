import { useEffect, useMemo, useState } from "react";
import {
  BarChart3,
  FileSpreadsheet,
  LineChart,
  LoaderCircle,
  Percent,
  ReceiptText,
  WalletCards,
  X,
} from "lucide-react";
import {
  fetchFinancialAccount,
  fetchFinancialRatios,
  fetchFinancialStatements,
  type FinancialAccountSeries,
  type FinancialApiPeriod,
  type FinancialPoint,
  type FinancialStatementCode,
  type FinancialStatementData,
} from "../api/financialsApi";

type StatementView = {
  code: FinancialStatementCode;
  sectionKey?: string;
  title: string;
  icon: typeof ReceiptText;
};

type FinancialSubTab = "statements" | "ratios";
type RatioUnit = "percent" | "multiple" | "days" | "number" | "currency";

type SelectedMetric = {
  statement: StatementView;
  account: FinancialAccountSeries;
  labels: string[];
  values: Array<number | null>;
  displayValues: string[];
  growthRates: Array<number | null>;
  displayGrowthRates: string[];
  isLoading: boolean;
  errorMessage: string;
};

type FinancialStatementsPageProps = {
  stockCode: string;
};

type RatioDefinition = {
  canonicalId: string;
  name: string;
  statement: FinancialStatementCode;
  unit?: RatioUnit;
  calculate: (index: number, source: RatioSource) => number | null;
};

type NormalizedSeries = {
  labels: string[];
  values: Array<number | null>;
};

type RatioSource = Record<string, NormalizedSeries>;

const statementViews: Record<FinancialStatementCode, StatementView> = {
  IS: {
    code: "IS",
    title: "손익계산서",
    icon: BarChart3,
  },
  BS: {
    code: "BS",
    title: "재무상태표",
    icon: WalletCards,
  },
  CF: {
    code: "CF",
    title: "현금흐름표",
    icon: FileSpreadsheet,
  },
};

const periodLabels: Record<FinancialApiPeriod, string> = {
  annual: "연간",
  quarter: "분기",
  ttm: "TTM",
};

const fallbackLabels: Record<FinancialApiPeriod, string[]> = {
  annual: [
    "2016-12-31",
    "2017-12-31",
    "2018-12-31",
    "2019-12-31",
    "2020-12-31",
    "2021-12-31",
    "2022-12-31",
    "2023-12-31",
    "2024-12-31",
    "2025-12-31",
  ],
  quarter: [
    "2023-09-30",
    "2023-12-31",
    "2024-03-31",
    "2024-06-30",
    "2024-09-30",
    "2024-12-31",
    "2025-03-31",
    "2025-06-30",
    "2025-09-30",
    "2025-12-31",
  ],
  ttm: [
    "2016 TTM",
    "2017 TTM",
    "2018 TTM",
    "2019 TTM",
    "2020 TTM",
    "2021 TTM",
    "2022 TTM",
    "2023 TTM",
    "2024 TTM",
    "2025 TTM",
  ],
};

const sampleFinancials: FinancialStatementData[] = [
  {
    code: "IS",
    accounts: [
      createSampleAccount("revenue", "매출액", "IS", [42.2, 47.1, 52.8, 72.1, 57.8, 72.6, 89.4, 94.6, 108.2, 137.3]),
      createSampleAccount("cogs", "매출원가", "IS", [21.4, 22.0, 24.2, 25.6, 23.8, 29.4, 34.8, 36.5, 37.7, 46.5]),
      createSampleAccount("gross_profit", "매출총이익", "IS", [20.7, 25.0, 28.6, 46.5, 34.0, 43.1, 54.6, 58.1, 70.5, 90.8]),
      createSampleAccount("rnd", "R&D", "IS", [3.9, 4.8, 5.5, 6.5, 6.6, 7.5, 9.8, 10.2, 11.7, 12.0]),
      createSampleAccount("sga", "판관비", "IS", [3.9, 4.7, 5.2, 8.0, 7.9, 8.5, 12.6, 13.7, 16.3, 20.6]),
      createSampleAccount("operating_income", "영업이익", "IS", [10.8, 12.4, 8.5, 24.5, 10.1, 15.8, 17.2, 16.4, 22.1, 32.7]),
      createSampleAccount("net_income", "순이익", "IS", [11.1, 9.1, 11.4, 25.9, 9.4, 22.7, 17.9, 23.0, 32.5, 32.4]),
    ],
  },
  {
    code: "BS",
    accounts: [
      createSampleAccount("total_assets", "총자산", "BS", [94.7, 103.3, 115.9, 148.6, 153.8, 175.7, 193.8, 218.4, 260.1, 299.9]),
      createSampleAccount("cash_equivalents", "현금성자산", "BS", [44.4, 48.7, 62.4, 63.5, 57.6, 52.9, 79.2, 85.3, 68.2, 127.9]),
      createSampleAccount("inventory", "재고자산", "BS", [5.3, 6.5, 9.3, 12.7, 12.5, 15.1, 21.0, 19.6, 20.8, 21.5]),
      createSampleAccount("receivables", "매출채권", "BS", [7.8, 8.3, 8.7, 10.5, 8.4, 12.8, 14.3, 18.3, 19.8, 23.8]),
      createSampleAccount("ppe", "유형자산", "BS", [5.2, 5.0, 5.4, 5.6, 8.9, 10.2, 11.0, 10.7, 14.9, 19.5]),
      createSampleAccount("total_liabilities", "총부채", "BS", [6.4, 6.1, 7.3, 12.2, 8.0, 9.7, 12.0, 14.3, 23.5, 28.8]),
      createSampleAccount("financial_debt", "금융부채", "BS", [0, 0, 0.1, 0.16, 0.07, 0.17, 0.41, 0.15, 1.6, 2.4]),
      createSampleAccount("total_equity", "총자본", "BS", [88.2, 97.2, 108.6, 136.4, 145.9, 166.1, 181.8, 204.1, 236.7, 271.1]),
    ],
  },
  {
    code: "CF",
    accounts: [
      createSampleAccount("operating_cf", "영업현금흐름", "CF", [17.1, 15.0, 18.7, 28.6, 16.8, 23.4, 29.8, 32.7, 41.0, 44.8]),
      createSampleAccount("investing_cf", "투자현금흐름", "CF", [4.8, -9.7, -34.1, -25.2, 3.5, -13.1, -15.1, -14.2, -31.2, -33.1]),
      createSampleAccount("financing_cf", "재무현금흐름", "CF", [0, 0.01, 0.01, 1.6, -0.22, -2.8, -2.5, -1.3, -0.17, 1.2]),
      createSampleAccount("free_cf", "잉여현금흐름", "CF", [10.0, 7.3, 12.3, 22.9, 8.4, 11.8, 14.9, 19.3, 28.8, 27.2]),
      createSampleAccount("capex", "설비투자", "CF", [7.1, 7.7, 6.4, 5.7, 8.4, 11.6, 14.9, 13.4, 12.2, 17.6]),
      createSampleAccount("cash_change", "현금증감", "CF", [19.1, -0.69, -15.8, 3.2, 16.7, 1.2, 0.77, 7.7, -3.7, 5.5]),
    ],
  },
];

const accountMatchers = {
  revenue: ["revenue", "sales", "매출액", "매출"],
  grossProfit: ["gross_profit", "grossprofit", "gross profit", "매출총이익"],
  operatingIncome: ["operating_income", "operatingincome", "operating income", "영업이익"],
  netIncome: ["net_income", "netincome", "net income", "당기순이익", "순이익"],
  rnd: ["rnd", "r&d", "research", "개발비", "연구개발"],
  sga: ["sga", "selling", "판관비", "판매비", "관리비"],
  totalAssets: ["total_assets", "totalassets", "assets", "자산총계", "총자산"],
  cash: ["cash_equivalents", "cash", "현금성자산", "현금"],
  inventory: ["inventory", "재고자산", "재고"],
  receivables: ["receivables", "accounts_receivable", "매출채권"],
  totalLiabilities: ["total_liabilities", "totalliabilities", "liabilities", "부채총계", "총부채"],
  financialDebt: ["financial_debt", "borrowings", "interest-bearing", "금융부채", "차입금"],
  totalEquity: ["total_equity", "totalequity", "equity", "자본총계", "총자본", "자기자본"],
  operatingCf: ["operating_cf", "operatingcashflow", "operating cash", "영업현금흐름", "영업활동현금흐름"],
  freeCf: ["free_cf", "freecashflow", "fcf", "free cash", "잉여현금흐름"],
  capex: ["capex", "capital expenditure", "설비투자", "유형자산취득"],
} as const;

const ratioDefinitions: RatioDefinition[] = [
  {
    canonicalId: "ratio_revenue_growth",
    name: "매출 성장률",
    statement: "IS",
    calculate: (index, source) => calculateGrowth(at(source.revenue, index), at(source.revenue, index - 1)),
  },
  {
    canonicalId: "ratio_gross_margin",
    name: "매출총이익률",
    statement: "IS",
    calculate: (index, source) => percent(at(source.grossProfit, index), at(source.revenue, index)),
  },
  {
    canonicalId: "ratio_operating_margin",
    name: "영업이익률",
    statement: "IS",
    calculate: (index, source) => percent(at(source.operatingIncome, index), at(source.revenue, index)),
  },
  {
    canonicalId: "ratio_net_margin",
    name: "순이익률",
    statement: "IS",
    calculate: (index, source) => percent(at(source.netIncome, index), at(source.revenue, index)),
  },
  {
    canonicalId: "ratio_rnd_to_sales",
    name: "R&D 비율",
    statement: "IS",
    calculate: (index, source) => percent(at(source.rnd, index), at(source.revenue, index)),
  },
  {
    canonicalId: "ratio_sga_to_sales",
    name: "판관비율",
    statement: "IS",
    calculate: (index, source) => percent(at(source.sga, index), at(source.revenue, index)),
  },
  {
    canonicalId: "ratio_debt_to_equity",
    name: "부채비율",
    statement: "BS",
    calculate: (index, source) => percent(at(source.totalLiabilities, index), at(source.totalEquity, index)),
  },
  {
    canonicalId: "ratio_equity_to_assets",
    name: "자기자본비율",
    statement: "BS",
    calculate: (index, source) => percent(at(source.totalEquity, index), at(source.totalAssets, index)),
  },
  {
    canonicalId: "ratio_cash_to_assets",
    name: "현금성자산 비중",
    statement: "BS",
    calculate: (index, source) => percent(at(source.cash, index), at(source.totalAssets, index)),
  },
  {
    canonicalId: "ratio_inventory_to_sales",
    name: "재고자산 비중",
    statement: "BS",
    calculate: (index, source) => percent(at(source.inventory, index), at(source.revenue, index)),
  },
  {
    canonicalId: "ratio_receivables_turnover_days",
    name: "매출채권 회전일수",
    statement: "BS",
    unit: "days",
    calculate: (index, source) => days(at(source.receivables, index), at(source.revenue, index)),
  },
  {
    canonicalId: "ratio_asset_turnover",
    name: "총자산회전율",
    statement: "BS",
    unit: "multiple",
    calculate: (index, source) => divide(at(source.revenue, index), at(source.totalAssets, index)),
  },
  {
    canonicalId: "ratio_operating_cf_margin",
    name: "영업현금흐름률",
    statement: "CF",
    calculate: (index, source) => percent(at(source.operatingCf, index), at(source.revenue, index)),
  },
  {
    canonicalId: "ratio_fcf_margin",
    name: "FCF 마진",
    statement: "CF",
    calculate: (index, source) => percent(at(source.freeCf, index), at(source.revenue, index)),
  },
  {
    canonicalId: "ratio_fcf_growth",
    name: "FCF 성장률",
    statement: "CF",
    calculate: (index, source) => calculateGrowth(at(source.freeCf, index), at(source.freeCf, index - 1)),
  },
  {
    canonicalId: "ratio_capex_to_sales",
    name: "CAPEX 비율",
    statement: "CF",
    calculate: (index, source) => percent(abs(at(source.capex, index)), at(source.revenue, index)),
  },
  {
    canonicalId: "ratio_cash_conversion",
    name: "현금전환율",
    statement: "CF",
    calculate: (index, source) => percent(at(source.operatingCf, index), at(source.netIncome, index)),
  },
  {
    canonicalId: "ratio_cf_to_debt",
    name: "현금흐름 대비 부채",
    statement: "CF",
    calculate: (index, source) => percent(at(source.operatingCf, index), at(source.totalLiabilities, index)),
  },
];

function createSampleAccount(
  canonicalId: string,
  name: string,
  statement: FinancialStatementCode,
  values: Array<number | null>,
): FinancialAccountSeries {
  return {
    canonicalId,
    name,
    statement,
    points: values.map((value, index) => ({
      label: fallbackLabels.annual[index],
      value,
      displayValue: formatStatementValue(value),
      growthRate: calculateGrowth(value, values[index - 1] ?? null),
    })),
  };
}

function calculateGrowth(current: number | null, previous: number | null) {
  if (current === null || previous === null || previous === 0) {
    return null;
  }

  return ((current - previous) / Math.abs(previous)) * 100;
}

function roundMetric(value: number | null, digits = 2) {
  return value === null || !Number.isFinite(value) ? null : Number(value.toFixed(digits));
}

function divide(numerator: number | null, denominator: number | null) {
  if (numerator === null || denominator === null || denominator === 0) {
    return null;
  }

  return numerator / denominator;
}

function percent(numerator: number | null, denominator: number | null) {
  const value = divide(numerator, denominator);
  return value === null ? null : value * 100;
}

function days(numerator: number | null, denominator: number | null) {
  const value = divide(numerator, denominator);
  return value === null ? null : value * 365;
}

function abs(value: number | null) {
  return value === null ? null : Math.abs(value);
}

function at(series: NormalizedSeries, index: number) {
  return series.values[index] ?? null;
}

function transformSamplePoint(
  point: FinancialPoint,
  period: FinancialApiPeriod,
  index: number,
  statement: FinancialStatementCode,
): FinancialPoint {
  if (period === "annual") {
    return point;
  }

  if (point.value === null) {
    return { ...point, label: fallbackLabels[period][index] };
  }

  const factor =
    period === "ttm"
      ? 0.96 + index * 0.006 + Math.sin(index) * 0.012
      : statement === "BS"
        ? 0.92 + index * 0.018 + Math.sin(index * 0.9) * 0.02
        : 0.21 + (index % 4) * 0.025 + Math.sin(index) * 0.01;
  const value = Number((point.value * factor).toFixed(2));

  return {
    label: fallbackLabels[period][index],
    value,
    displayValue: formatStatementValue(value),
    growthRate: null,
  };
}

function getSampleFinancials(period: FinancialApiPeriod): FinancialStatementData[] {
  return sampleFinancials.map((statement) => ({
    ...statement,
    accounts: statement.accounts.map((account) => {
      const points = account.points
        .slice(0, 10)
        .map((point, index) => transformSamplePoint(point, period, index, statement.code));

      return {
        ...account,
        points: points.map((point, index) => ({
          ...point,
          growthRate: calculateGrowth(point.value, points[index - 1]?.value ?? null),
        })),
      };
    }),
  }));
}

function getPeriodKey(label: string, period: FinancialApiPeriod) {
  const normalized = label.trim();

  if (period === "annual") {
    const match = normalized.match(/^(\d{4})(?:-\d{2}-\d{2})?$/);
    return match?.[1] ?? normalized;
  }

  if (period === "ttm") {
    const match = normalized.match(/^(\d{4})(?:\s*TTM)?$/i);
    return match ? `${match[1]} TTM` : normalized.toUpperCase();
  }

  const quarterMatch = normalized.match(/^(\d{4})[-\s]?Q([1-4])$/i);
  if (quarterMatch) {
    return `${quarterMatch[1]} Q${quarterMatch[2]}`;
  }

  const dateMatch = normalized.match(/^(\d{4})-(\d{2})-\d{2}$/);
  if (dateMatch) {
    const quarter = Math.ceil(Number(dateMatch[2]) / 3);
    return `${dateMatch[1]} Q${quarter}`;
  }

  return normalized;
}

function dedupePeriodPoints(points: FinancialPoint[], period: FinancialApiPeriod) {
  const deduped = new Map<string, FinancialPoint>();

  points.forEach((point) => {
    const key = getPeriodKey(point.label, period);

    if (deduped.has(key)) {
      deduped.delete(key);
    }
    deduped.set(key, point);
  });

  return Array.from(deduped.values());
}

function normalizeTablePoints(account: FinancialAccountSeries, period: FinancialApiPeriod) {
  const points = dedupePeriodPoints(account.points, period).slice(-10);
  const labels = fallbackLabels[period].map((fallback, index) => points[index]?.label || fallback);
  const values = labels.map((_, index) => points[index]?.value ?? null);
  const displayValues = labels.map((_, index) => points[index]?.displayValue ?? formatMetricDisplay(account, values[index]));
  const growthRates = labels.map((_, index) => points[index]?.growthRate ?? calculateGrowth(values[index], values[index - 1] ?? null));
  const displayGrowthRates = labels.map((_, index) => points[index]?.displayGrowthRate ?? formatGrowth(growthRates[index]));

  return { labels, values, displayValues, growthRates, displayGrowthRates };
}

function formatStatementValue(value: number | null) {
  if (value === null) {
    return "N/A";
  }

  return new Intl.NumberFormat("ko-KR", {
    maximumFractionDigits: Math.abs(value) >= 100 ? 0 : 1,
  }).format(value);
}

function getRatioUnit(account: FinancialAccountSeries): RatioUnit {
  if (/percent|percentage|%/i.test(account.unit ?? "")) {
    return "percent";
  }

  if (/day/i.test(account.unit ?? "")) {
    return "days";
  }

  if (/krw|usd|currency/i.test(account.unit ?? "")) {
    return "currency";
  }

  if (/multiple|times|x/i.test(account.unit ?? "")) {
    return "multiple";
  }

  if (/ratio|score/i.test(account.unit ?? "")) {
    return "number";
  }

  if (account.canonicalId.includes("turnover")) {
    return "multiple";
  }

  if (account.canonicalId.includes("days")) {
    return "days";
  }

  return "percent";
}

function formatRatioValue(value: number | null, unit: RatioUnit = "percent") {
  if (value === null) {
    return "N/A";
  }

  if (unit === "multiple") {
    return `${value.toFixed(2)}x`;
  }

  if (unit === "days") {
    return `${value.toFixed(0)}일`;
  }

  if (unit === "number") {
    return value.toFixed(Math.abs(value) >= 100 ? 1 : 2);
  }

  if (unit === "currency") {
    return formatStatementValue(value);
  }

  return `${value.toFixed(1)}%`;
}

function formatMetricDisplay(account: FinancialAccountSeries, value: number | null) {
  if (account.canonicalId.startsWith("ratio_")) {
    return formatRatioValue(value, getRatioUnit(account));
  }

  return formatStatementValue(value);
}

function formatGrowth(value: number | null) {
  if (value === null) {
    return "";
  }

  const prefix = value > 0 ? "+" : "";
  return `(${prefix}${Math.round(value)}%)`;
}

function valuesOnly(values: Array<number | null>) {
  return values.filter((value): value is number => value !== null && Number.isFinite(value));
}

function emptySeries(period: FinancialApiPeriod): NormalizedSeries {
  return {
    labels: fallbackLabels[period],
    values: fallbackLabels[period].map(() => null),
  };
}

function matchesAccount(account: FinancialAccountSeries, terms: readonly string[]) {
  const haystack = `${account.canonicalId} ${account.name}`.toLowerCase();
  return terms.some((term) => haystack.includes(term.toLowerCase()));
}

function findAccount(
  financialData: FinancialStatementData[],
  statementCode: FinancialStatementCode,
  terms: readonly string[],
) {
  const section = financialData.find((statement) => statement.code === statementCode);
  return section?.accounts.find((account) => matchesAccount(account, terms));
}

function readSourceSeries(
  financialData: FinancialStatementData[],
  period: FinancialApiPeriod,
  statementCode: FinancialStatementCode,
  terms: readonly string[],
) {
  const account = findAccount(financialData, statementCode, terms);

  if (!account) {
    return emptySeries(period);
  }

  const tablePoints = normalizeTablePoints(account, period);
  return {
    labels: tablePoints.labels,
    values: tablePoints.values,
  };
}

function buildRatioSource(financialData: FinancialStatementData[], period: FinancialApiPeriod): RatioSource {
  return {
    revenue: readSourceSeries(financialData, period, "IS", accountMatchers.revenue),
    grossProfit: readSourceSeries(financialData, period, "IS", accountMatchers.grossProfit),
    operatingIncome: readSourceSeries(financialData, period, "IS", accountMatchers.operatingIncome),
    netIncome: readSourceSeries(financialData, period, "IS", accountMatchers.netIncome),
    rnd: readSourceSeries(financialData, period, "IS", accountMatchers.rnd),
    sga: readSourceSeries(financialData, period, "IS", accountMatchers.sga),
    totalAssets: readSourceSeries(financialData, period, "BS", accountMatchers.totalAssets),
    cash: readSourceSeries(financialData, period, "BS", accountMatchers.cash),
    inventory: readSourceSeries(financialData, period, "BS", accountMatchers.inventory),
    receivables: readSourceSeries(financialData, period, "BS", accountMatchers.receivables),
    totalLiabilities: readSourceSeries(financialData, period, "BS", accountMatchers.totalLiabilities),
    financialDebt: readSourceSeries(financialData, period, "BS", accountMatchers.financialDebt),
    totalEquity: readSourceSeries(financialData, period, "BS", accountMatchers.totalEquity),
    operatingCf: readSourceSeries(financialData, period, "CF", accountMatchers.operatingCf),
    freeCf: readSourceSeries(financialData, period, "CF", accountMatchers.freeCf),
    capex: readSourceSeries(financialData, period, "CF", accountMatchers.capex),
  };
}

function buildFinancialRatios(
  financialData: FinancialStatementData[],
  period: FinancialApiPeriod,
): FinancialStatementData[] {
  const source = buildRatioSource(financialData, period);
  const labels = source.revenue.labels;
  const accounts = ratioDefinitions.map((definition) => {
    const values = labels.map((_, index) => roundMetric(definition.calculate(index, source)));

    return {
      canonicalId: definition.canonicalId,
      name: definition.name,
      statement: definition.statement,
      unit: definition.unit ?? "percent",
      points: labels.map((label, index) => ({
        label,
        value: values[index],
        displayValue: formatRatioValue(values[index], definition.unit),
        growthRate: calculateGrowth(values[index], values[index - 1] ?? null),
      })),
    };
  });

  return (["IS", "BS", "CF"] as FinancialStatementCode[]).map((code) => ({
    code,
    accounts: accounts.filter((account) => account.statement === code),
  }));
}

function getRatioGroupTitle(account: FinancialAccountSeries) {
  return account.groupName || statementViews[account.statement].title;
}

function getRatioGroupKey(account: FinancialAccountSeries) {
  return account.groupKey || getRatioGroupTitle(account);
}

function groupRatioSections(ratioStatements: FinancialStatementData[]) {
  const groups = new Map<string, StatementView & { accounts: FinancialAccountSeries[] }>();

  ratioStatements.forEach((statement) => {
    statement.accounts.forEach((account) => {
      const groupKey = getRatioGroupKey(account);
      const sectionKey = `${statement.code}-${groupKey}`;

      if (!groups.has(sectionKey)) {
        groups.set(sectionKey, {
          code: statement.code,
          sectionKey,
          title: getRatioGroupTitle(account),
          icon: Percent,
          accounts: [],
        });
      }

      groups.get(sectionKey)?.accounts.push(account);
    });
  });

  return Array.from(groups.values());
}

function Sparkline({ values }: { values: Array<number | null> }) {
  const points = valuesOnly(values);

  if (points.length < 2) {
    return <span className="financial-empty-trend">-</span>;
  }

  const min = Math.min(...points);
  const max = Math.max(...points);
  const spread = max - min || 1;
  const path = values
    .map((value, index) => {
      const x = (index / Math.max(values.length - 1, 1)) * 68;
      const normalized = value === null ? min : value;
      const y = 28 - ((normalized - min) / spread) * 24;
      return `${index === 0 ? "M" : "L"} ${x.toFixed(1)} ${y.toFixed(1)}`;
    })
    .join(" ");
  const color = points[points.length - 1] >= points[0] ? "#27d38c" : "#f0525c";

  return (
    <svg className="financial-sparkline" viewBox="0 0 68 32" aria-hidden="true">
      <path d={path} fill="none" stroke={color} strokeLinecap="round" strokeWidth="2.2" />
    </svg>
  );
}

function MetricValueCell({
  displayValue,
  growth,
  displayGrowth,
}: {
  displayValue: string;
  growth: number | null;
  displayGrowth: string;
}) {
  const className =
    growth === null ? "" : growth >= 0 ? "financial-positive" : "financial-negative";

  return (
    <td className={className}>
      {displayValue}
      <span>{displayGrowth === "N/A" ? "" : displayGrowth}</span>
    </td>
  );
}

function TrendBars({ values, labels }: { values: Array<number | null>; labels: string[] }) {
  const points = valuesOnly(values);
  const max = Math.max(...points.map((value) => Math.abs(value)), 1);

  return (
    <div className="financial-trend-bars">
      {values.map((value, index) => {
        const height = value === null ? 0 : Math.max(10, (Math.abs(value) / max) * 100);

        return (
          <div className="financial-trend-bar-item" key={`${labels[index]}-${index}`}>
            <span
              className={value !== null && value < 0 ? "negative" : ""}
              style={{ height: `${height}%` }}
            />
            <em>{labels[index]}</em>
          </div>
        );
      })}
    </div>
  );
}

function GrowthBars({ values, labels }: { values: Array<number | null>; labels: string[] }) {
  const growth = values.map((value, index) => calculateGrowth(value, values[index - 1] ?? null));
  const max = Math.max(...growth.map((value) => Math.abs(value ?? 0)), 1);

  return (
    <div className="financial-growth-bars">
      {growth.map((value, index) => {
        const height = value === null ? 0 : Math.max(8, (Math.abs(value) / max) * 100);

        return (
          <div className="financial-growth-bar-item" key={`${labels[index]}-${index}`}>
            <strong>{value === null ? "-" : `${Math.round(value)}%`}</strong>
            <span
              className={value !== null && value < 0 ? "negative" : ""}
              style={{ height: `${height}%` }}
            />
            <em>{labels[index].replace("-12-31", "")}</em>
          </div>
        );
      })}
    </div>
  );
}

function MetricModal({
  metric,
  onClose,
}: {
  metric: SelectedMetric;
  onClose: () => void;
}) {
  const points = valuesOnly(metric.values);
  const latest = points[points.length - 1] ?? null;
  const max = points.length > 0 ? Math.max(...points) : null;
  const min = points.length > 0 ? Math.min(...points) : null;
  const average =
    points.length > 0
      ? points.reduce((sum, value) => sum + value, 0) / points.length
      : null;

  return (
    <div className="financial-modal-backdrop" role="presentation" onClick={onClose}>
      <section
        className="financial-modal"
        role="dialog"
        aria-modal="true"
        aria-label={`${metric.account.name} 재무 추세`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="financial-modal-header">
          <div>
            <span>{metric.statement.title}</span>
            <h2>{metric.account.name}</h2>
          </div>
          <button type="button" aria-label="닫기" onClick={onClose}>
            <X size={18} />
          </button>
        </header>

        {metric.isLoading && (
          <div className="financial-detail-loading">
            <LoaderCircle size={15} />
            상세 계정 데이터를 불러오는 중
          </div>
        )}
        {metric.errorMessage && <p className="financial-api-note">{metric.errorMessage}</p>}

        <div className="financial-modal-stats">
          <article>
            <span>최신값</span>
            <strong>{metric.displayValues[metric.displayValues.length - 1] ?? formatMetricDisplay(metric.account, latest)}</strong>
          </article>
          <article>
            <span>최대값</span>
            <strong>{formatMetricDisplay(metric.account, max)}</strong>
          </article>
          <article>
            <span>최소값</span>
            <strong>{formatMetricDisplay(metric.account, min)}</strong>
          </article>
          <article>
            <span>평균값</span>
            <strong>{formatMetricDisplay(metric.account, average)}</strong>
          </article>
        </div>

        <article className="financial-modal-chart">
          <div className="financial-chart-title">
            <LineChart size={16} />
            <h3>추세</h3>
          </div>
          <TrendBars values={metric.values} labels={metric.labels} />
        </article>

        <article className="financial-modal-chart">
          <div className="financial-chart-title">
            <BarChart3 size={16} />
            <h3>전년 대비 성장률</h3>
          </div>
          <GrowthBars values={metric.values} labels={metric.labels} />
        </article>
      </section>
    </div>
  );
}

function FinancialMetricSections({
  sections,
  period,
  onMetricSelect,
}: {
  sections: Array<StatementView & { accounts: FinancialAccountSeries[] }>;
  period: FinancialApiPeriod;
  onMetricSelect: (statement: StatementView, account: FinancialAccountSeries) => void;
}) {
  return (
    <div className="financial-statement-stack">
      {sections.map((statement) => {
        const firstAccount = statement.accounts[0];
        const labels = firstAccount
          ? normalizeTablePoints(firstAccount, period).labels
          : fallbackLabels[period];

        return (
            <section className="financial-statement-card" key={statement.sectionKey ?? statement.code}>
            <div className="financial-statement-title">
              <statement.icon size={18} />
              <h2>{statement.title}</h2>
            </div>

            <div className="financial-table-wrap">
              <table className="financial-table">
                <thead>
                  <tr>
                    <th>지표명</th>
                    <th>트렌드</th>
                    {labels.map((label) => (
                      <th key={label}>{label}</th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {statement.accounts.map((account) => {
                    const tablePoints = normalizeTablePoints(account, period);

                    return (
                      <tr
                        key={account.canonicalId}
                        onClick={() => onMetricSelect(statement, account)}
                      >
                        <th scope="row">{account.name}</th>
                        <td>
                          <Sparkline values={tablePoints.values} />
                        </td>
                        {tablePoints.labels.map((label, index) => (
                          <MetricValueCell
                            displayValue={tablePoints.displayValues[index]}
                            growth={tablePoints.growthRates[index] ?? null}
                            displayGrowth={tablePoints.displayGrowthRates[index] ?? ""}
                            key={`${account.canonicalId}-${label}`}
                          />
                        ))}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          </section>
        );
      })}
    </div>
  );
}

export function FinancialStatementsPage({ stockCode }: FinancialStatementsPageProps) {
  const [activeSubTab, setActiveSubTab] = useState<FinancialSubTab>("statements");
  const [statementPeriod, setStatementPeriod] = useState<FinancialApiPeriod>("annual");
  const [ratioPeriod, setRatioPeriod] = useState<Extract<FinancialApiPeriod, "annual" | "quarter">>("annual");
  const [financialData, setFinancialData] = useState<FinancialStatementData[]>(() =>
    getSampleFinancials("annual"),
  );
  const [ratioData, setRatioData] = useState<FinancialStatementData[] | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [ratioIsLoading, setRatioIsLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");
  const [ratioErrorMessage, setRatioErrorMessage] = useState("");
  const [selectedMetric, setSelectedMetric] = useState<SelectedMetric | null>(null);
  const period = activeSubTab === "ratios" ? ratioPeriod : statementPeriod;
  const visiblePeriodOptions: FinancialApiPeriod[] =
    activeSubTab === "ratios" ? ["annual", "quarter"] : ["annual", "quarter", "ttm"];
  const displayIsLoading = activeSubTab === "ratios" ? ratioIsLoading || isLoading : isLoading;
  const displayErrorMessage = activeSubTab === "ratios" ? ratioErrorMessage : errorMessage;

  useEffect(() => {
    let ignore = false;

    setIsLoading(true);
    setErrorMessage("");
    void fetchFinancialStatements(stockCode, period)
      .then((response) => {
        if (!ignore) {
          setFinancialData(response.some((statement) => statement.accounts.length > 0) ? response : getSampleFinancials(period));
        }
      })
      .catch((error: Error) => {
        if (!ignore) {
          setFinancialData(getSampleFinancials(period));
          setErrorMessage(`${error.message} 샘플 데이터로 화면을 표시합니다.`);
        }
      })
      .finally(() => {
        if (!ignore) {
          setIsLoading(false);
        }
      });

    return () => {
      ignore = true;
    };
  }, [period, stockCode]);

  useEffect(() => {
    let ignore = false;

    setRatioIsLoading(true);
    setRatioErrorMessage("");
    void fetchFinancialRatios(stockCode, ratioPeriod)
      .then((response) => {
        if (!ignore) {
          setRatioData(response.some((statement) => statement.accounts.length > 0) ? response : null);
        }
      })
      .catch((error: Error) => {
        if (!ignore) {
          setRatioData(null);
          setRatioErrorMessage(`${error.message} 재무제표 기준 계산값으로 표시합니다.`);
        }
      })
      .finally(() => {
        if (!ignore) {
          setRatioIsLoading(false);
        }
      });

    return () => {
      ignore = true;
    };
  }, [ratioPeriod, stockCode]);

  const statementSections = useMemo(
    () =>
      (["IS", "BS", "CF"] as FinancialStatementCode[]).map((code) => ({
        ...statementViews[code],
        accounts: financialData.find((statement) => statement.code === code)?.accounts ?? [],
      })),
    [financialData],
  );

  const ratioSections = useMemo(
    () => groupRatioSections(ratioData ?? buildFinancialRatios(financialData, ratioPeriod)),
    [financialData, ratioData, ratioPeriod],
  );

  const handleStatementMetricSelect = (statement: StatementView, account: FinancialAccountSeries) => {
    const tablePoints = normalizeTablePoints(account, period);

    setSelectedMetric({
      statement,
      account,
      labels: tablePoints.labels,
      values: tablePoints.values,
      displayValues: tablePoints.displayValues,
      growthRates: tablePoints.growthRates,
      displayGrowthRates: tablePoints.displayGrowthRates,
      isLoading: true,
      errorMessage: "",
    });

    void fetchFinancialAccount(stockCode, account.canonicalId, period)
      .then((response) => {
        const detailPoints = normalizeTablePoints(response, period);
        setSelectedMetric((current) =>
          current?.account.canonicalId === account.canonicalId
            ? {
                ...current,
                account: response,
                labels: detailPoints.labels,
                values: detailPoints.values,
                displayValues: detailPoints.displayValues,
                growthRates: detailPoints.growthRates,
                displayGrowthRates: detailPoints.displayGrowthRates,
                isLoading: false,
                errorMessage: "",
              }
            : current,
        );
      })
      .catch((error: Error) => {
        setSelectedMetric((current) =>
          current?.account.canonicalId === account.canonicalId
            ? {
                ...current,
                isLoading: false,
                errorMessage: `${error.message} 현재 표 데이터를 기준으로 표시합니다.`,
              }
            : current,
        );
      });
  };

  const handleRatioMetricSelect = (statement: StatementView, account: FinancialAccountSeries) => {
    const tablePoints = normalizeTablePoints(account, period);

    setSelectedMetric({
      statement,
      account,
      labels: tablePoints.labels,
      values: tablePoints.values,
      displayValues: tablePoints.displayValues,
      growthRates: tablePoints.growthRates,
      displayGrowthRates: tablePoints.displayGrowthRates,
      isLoading: false,
      errorMessage: "",
    });
  };

  return (
    <section className="financial-page">
      <nav className="financial-subnav" aria-label="재무 하위 메뉴">
        <button
          className={activeSubTab === "statements" ? "active" : ""}
          type="button"
          onClick={() => setActiveSubTab("statements")}
        >
          <ReceiptText size={15} />
          재무제표
        </button>
        <button
          className={activeSubTab === "ratios" ? "active" : ""}
          type="button"
          onClick={() => setActiveSubTab("ratios")}
        >
          <Percent size={15} />
          재무비율
        </button>
        {["적정가치", "실적", "애널리스트", "배당", "공시"].map((item) => (
          <button type="button" key={item}>
            {item}
          </button>
        ))}
      </nav>

      <div className="financial-period-row">
        <span>기간:</span>
        {visiblePeriodOptions.map((item) => (
          <button
            className={period === item ? "selected" : ""}
            key={item}
            type="button"
            onClick={() => {
              if (activeSubTab === "ratios") {
                setRatioPeriod(item as Extract<FinancialApiPeriod, "annual" | "quarter">);
              } else {
                setStatementPeriod(item);
              }
            }}
          >
            {periodLabels[item]}
          </button>
        ))}
        {displayIsLoading && (
          <em>
            <LoaderCircle size={14} />
            API 로딩
          </em>
        )}
      </div>

      {displayErrorMessage && <p className="financial-api-note">{displayErrorMessage}</p>}

      {activeSubTab === "ratios" ? (
        <FinancialMetricSections
          sections={ratioSections}
          period={period}
          onMetricSelect={handleRatioMetricSelect}
        />
      ) : (
        <FinancialMetricSections
          sections={statementSections}
          period={period}
          onMetricSelect={handleStatementMetricSelect}
        />
      )}

      {selectedMetric && (
        <MetricModal metric={selectedMetric} onClose={() => setSelectedMetric(null)} />
      )}
    </section>
  );
}
