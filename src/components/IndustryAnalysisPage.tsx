import { useEffect, useMemo, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  BarChart3,
  Building2,
  CalendarDays,
  Globe2,
  LineChart,
  Percent,
  TrendingUp,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { fetchSectorLeaders } from "../api/sectorLeadersApi";
import type {
  IndustryAnalysisRow,
  IndustryMarket,
  IndustryMetricKey,
} from "../types/industryAnalysis";

type SortDirection = "asc" | "desc";

type MetricDefinition = {
  key: IndustryMetricKey;
  label: string;
  unit: "%" | "x";
  icon: LucideIcon;
};

const metrics: MetricDefinition[] = [
  {
    key: "strongStockRatio",
    label: "강세 종목 비율",
    unit: "%",
    icon: Percent,
  },
  {
    key: "expectedEpsGrowth",
    label: "EPS 예상 성장률",
    unit: "%",
    icon: TrendingUp,
  },
  {
    key: "dailyReturn",
    label: "1일 수익률",
    unit: "%",
    icon: LineChart,
  },
  {
    key: "weeklyReturn",
    label: "1주 수익률",
    unit: "%",
    icon: BarChart3,
  },
  {
    key: "roe",
    label: "ROE",
    unit: "%",
    icon: Building2,
  },
  {
    key: "per",
    label: "PER",
    unit: "x",
    icon: ArrowUpDown,
  },
  {
    key: "pbr",
    label: "PBR",
    unit: "x",
    icon: ArrowUpDown,
  },
];

const marketOptions: { value: IndustryMarket; label: string }[] = [
  { value: "KR", label: "한국" },
  { value: "US", label: "미국" },
];

const numberFormatter = new Intl.NumberFormat("ko-KR", {
  maximumFractionDigits: 1,
  minimumFractionDigits: 0,
});

const integerFormatter = new Intl.NumberFormat("ko-KR");

const dateFormatter = new Intl.DateTimeFormat("ko-KR", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

function formatMetricValue(
  value: number | null | undefined,
  unit: MetricDefinition["unit"],
) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "N/A";
  }

  return `${numberFormatter.format(value)}${unit}`;
}

function getMetricClass(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "na-value";
  }

  if (value > 0) {
    return "positive";
  }

  if (value < 0) {
    return "negative";
  }

  return undefined;
}

function SortStateIcon({
  active,
  direction,
}: {
  active: boolean;
  direction: SortDirection;
}) {
  if (!active) {
    return <ArrowUpDown size={13} />;
  }

  return direction === "asc" ? <ArrowUp size={13} /> : <ArrowDown size={13} />;
}

export function IndustryAnalysisPage() {
  const [market, setMarket] = useState<IndustryMarket>("KR");
  const [sortKey, setSortKey] = useState<IndustryMetricKey>("strongStockRatio");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [rows, setRows] = useState<IndustryAnalysisRow[]>([]);
  const [asOfDate, setAsOfDate] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [errorMessage, setErrorMessage] = useState("");

  const selectedMetric = metrics.find((metric) => metric.key === sortKey) ?? metrics[0];
  const selectedMarketLabel =
    marketOptions.find((option) => option.value === market)?.label ?? market;
  const today = asOfDate ?? dateFormatter.format(new Date());

  useEffect(() => {
    let ignore = false;

    setIsLoading(true);
    setErrorMessage("");
    setRows([]);
    setAsOfDate(null);
    void fetchSectorLeaders(market)
      .then((response) => {
        if (!ignore) {
          setRows(response.rows);
          setAsOfDate(response.asOfDate);
        }
      })
      .catch((error: Error) => {
        if (!ignore) {
          setRows([]);
          setAsOfDate(null);
          setErrorMessage(error.message);
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
  }, [market]);

  const sortedRows = useMemo(() => {
    return [...rows].sort((left, right) => {
      const leftValue = left[sortKey];
      const rightValue = right[sortKey];
      const leftMissing = leftValue === null || leftValue === undefined;
      const rightMissing = rightValue === null || rightValue === undefined;

      if (leftMissing && rightMissing) {
        return left.industryName.localeCompare(right.industryName, "ko-KR");
      }

      if (leftMissing) {
        return 1;
      }

      if (rightMissing) {
        return -1;
      }

      return sortDirection === "asc" ? leftValue - rightValue : rightValue - leftValue;
    });
  }, [rows, sortDirection, sortKey]);

  const topIndustry = sortedRows[0] ?? null;
  const availableStrongRatios = rows
    .map((row) => row.strongStockRatio)
    .filter((value): value is number => value !== null && value !== undefined);
  const averageStrongRatio =
    availableStrongRatios.length > 0
      ? availableStrongRatios.reduce((sum, value) => sum + value, 0) / availableStrongRatios.length
      : null;
  const totalStockCount = rows.reduce((sum, row) => sum + row.stockCount, 0);
  const totalStrongStockCount = rows.reduce((sum, row) => sum + row.strongStockCount, 0);
  const marketWeightedStrongRatio =
    totalStockCount > 0 ? (totalStrongStockCount / totalStockCount) * 100 : null;

  const handleMetricClick = (metricKey: IndustryMetricKey) => {
    if (metricKey === sortKey) {
      setSortDirection((current) => (current === "desc" ? "asc" : "desc"));
      return;
    }

    setSortKey(metricKey);
    setSortDirection("desc");
  };

  return (
    <section className="industry-analysis-page">
      <header className="industry-analysis-header">
        <div>
          <p className="industry-eyebrow">주도 산업군 랭킹</p>
          <h1>산업 분석</h1>
          <p>
            선택한 시장의 산업군별 강세, 성장성, 수익률, 밸류에이션을 비교합니다.
          </p>
        </div>
        <div className="industry-header-actions">
          <div className="industry-market-toggle" aria-label="분석 국가 선택">
            {marketOptions.map((option) => (
              <button
                className={option.value === market ? "active" : ""}
                key={option.value}
                type="button"
                onClick={() => setMarket(option.value)}
              >
                <Globe2 size={14} />
                <span>{option.label}</span>
              </button>
            ))}
          </div>
          <div className="industry-header-status">
            <span>
              <CalendarDays size={14} />
              {today}
            </span>
            <em>{selectedMarketLabel} · {isLoading ? "API 로딩 중" : "API 연동"}</em>
          </div>
        </div>
      </header>

      <main className="industry-analysis-scroll">
        {errorMessage && <p className="industry-api-note">{errorMessage}</p>}

        <section className="industry-summary-grid" aria-label="산업 분석 요약">
          <article>
            <span>현재 정렬 기준</span>
            <strong>{selectedMetric.label}</strong>
            <em>{sortDirection === "desc" ? "높은 순" : "낮은 순"}</em>
          </article>
          <article>
            <span>1위 산업군</span>
            <strong>{topIndustry?.industryName ?? "N/A"}</strong>
            <em>{topIndustry ? formatMetricValue(topIndustry[sortKey], selectedMetric.unit) : "N/A"}</em>
          </article>
          <article>
            <span>{selectedMarketLabel} 전체 강세 비율</span>
            <strong>{formatMetricValue(marketWeightedStrongRatio, "%")}</strong>
            <em>
              {isLoading
                ? "불러오는 중"
                : `${integerFormatter.format(totalStrongStockCount)} / ${integerFormatter.format(
                    totalStockCount,
                  )}개`}
            </em>
          </article>
          <article>
            <span>섹터 단순평균</span>
            <strong>{formatMetricValue(averageStrongRatio, "%")}</strong>
            <em>{rows.length > 0 ? `${rows.length}개 산업군 평균` : "산업군 평균"}</em>
          </article>
        </section>

        <section className="industry-sort-panel" aria-label="산업 분석 정렬 기준">
          <div className="industry-section-title">
            <h2>지표별 정렬</h2>
            <p>각 지표를 클릭하면 주도 산업군 순위를 바로 비교할 수 있습니다.</p>
          </div>

          <div className="industry-metric-tabs">
            {metrics.map((metric) => (
              <button
                className={metric.key === sortKey ? "active" : ""}
                key={metric.key}
                type="button"
                onClick={() => handleMetricClick(metric.key)}
              >
                <metric.icon size={14} />
                <span>{metric.label}</span>
                <SortStateIcon active={metric.key === sortKey} direction={sortDirection} />
              </button>
            ))}
          </div>
        </section>

        <section className="industry-table-panel">
          <div className="industry-section-title">
            <h2>산업군 순위</h2>
            <p>N/A 값은 데이터 미확보 항목이며 정렬 시 하단에 표시됩니다.</p>
          </div>

          <div className="industry-table-wrap">
            <table className="industry-table">
              <thead>
                <tr>
                  <th>순위</th>
                  <th>산업군</th>
                  <th>종목 수</th>
                  {metrics.map((metric) => (
                    <th key={metric.key}>
                      <button
                        className={`industry-sort-header ${
                          metric.key === sortKey ? "active" : ""
                        }`}
                        type="button"
                        onClick={() => handleMetricClick(metric.key)}
                        title={`${metric.label} 정렬`}
                      >
                        <span>{metric.label}</span>
                        <SortStateIcon
                          active={metric.key === sortKey}
                          direction={sortDirection}
                        />
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {isLoading ? (
                  <tr>
                    <td className="industry-table-state" colSpan={10}>
                      산업 분석 데이터를 불러오는 중...
                    </td>
                  </tr>
                ) : sortedRows.length === 0 ? (
                  <tr>
                    <td className="industry-table-state" colSpan={10}>
                      표시할 산업 분석 데이터가 없습니다.
                    </td>
                  </tr>
                ) : (
                  sortedRows.map((row, index) => (
                    <tr key={row.industryName}>
                      <td className="industry-rank">{index + 1}</td>
                      <td className="industry-name">{row.industryName}</td>
                      <td>{row.stockCount}</td>
                      {metrics.map((metric) => (
                        <td className={getMetricClass(row[metric.key])} key={metric.key}>
                          {formatMetricValue(row[metric.key], metric.unit)}
                        </td>
                      ))}
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </div>
        </section>
      </main>
    </section>
  );
}
