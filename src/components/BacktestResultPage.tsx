import { useEffect, useRef, type CSSProperties } from "react";
import { observer } from "mobx-react-lite";
import {
  ArrowLeft,
  Calendar,
  Database,
  Loader2,
  Play,
  Save,
  X,
} from "lucide-react";
import {
  AreaSeries,
  ColorType,
  HistogramSeries,
  LineSeries,
  createChart,
  type Time,
} from "lightweight-charts";
import type { QuantScreenerStore } from "../stores/quantScreenerStore";
import type {
  BacktestAnnualReturn,
  BacktestEquityPoint,
  BacktestPosition,
  BacktestRebalance,
  BacktestRebalanceFrequency,
  FactorBacktestResponse,
} from "../types/quantScreener";

type BacktestResultPageProps = {
  store: QuantScreenerStore;
};

type MetricRow = {
  label: string;
  strategy: number | null;
  benchmark: number | null;
  diff: number | null;
  signed?: boolean;
};

const percentFormatter = new Intl.NumberFormat("ko-KR", {
  maximumFractionDigits: 1,
  minimumFractionDigits: 1,
});

const numberFormatter = new Intl.NumberFormat("ko-KR", {
  maximumFractionDigits: 2,
});

const tabs = ["성과통계", "팩터", "거래 통계", "연도별"];
const rebalanceLabels: Record<string, string> = {
  monthly: "월간",
  quarterly: "분기",
  semiannual: "반기",
  annual: "연간",
};
const rebalanceOptions: Array<{
  label: string;
  frequency: BacktestRebalanceFrequency;
}> = [
  { label: "연간", frequency: "annual" },
  { label: "반기", frequency: "semiannual" },
  { label: "분기", frequency: "quarterly" },
];
const benchmarkColors = ["#ef6067", "#7fa7ff", "#f5c84c", "#c084fc"];

function formatSignedPercent(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }

  const prefix = value > 0 ? "+" : "";
  return `${prefix}${percentFormatter.format(value)}%`;
}

function formatMetric(value: number | null | undefined, signed = true) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }

  if (!signed) {
    return numberFormatter.format(value);
  }

  return formatSignedPercent(value);
}

function getMetricClass(value: number | null | undefined) {
  if (value === null || value === undefined) {
    return "";
  }

  if (value > 0) {
    return "positive";
  }

  if (value < 0) {
    return "negative";
  }

  return "";
}

function formatBenchmarkName(name: string) {
  return name === "KOSPI200" ? "KOSPI 200" : name;
}

function getBenchmarkValue(
  annualReturns: BacktestAnnualReturn[],
  benchmarkName: string | null,
) {
  if (!benchmarkName) {
    return null;
  }

  const values = annualReturns
    .map((item) => item.benchmarkReturns[benchmarkName])
    .filter((value): value is number => value !== null && value !== undefined);

  if (values.length === 0) {
    return null;
  }

  return values.reduce((product, value) => product * (1 + value / 100), 1) - 1;
}

function getMetricSections(
  result: FactorBacktestResponse | null,
): Array<{ title: string; rows: MetricRow[] }> {
  const summary = result?.summary;
  const primaryBenchmarkName = summary?.benchmarkNames[0] ?? null;
  const benchmarkReturn = result
    ? getBenchmarkValue(result.annualReturns, primaryBenchmarkName)
    : null;
  const benchmarkPercent =
    benchmarkReturn === null ? null : benchmarkReturn * 100;

  return [
    {
      title: "수익률",
      rows: [
        {
          label: "누적 수익률",
          strategy: summary?.cumulativeReturn ?? null,
          benchmark: benchmarkPercent,
          diff:
            summary?.cumulativeReturn !== null &&
            summary?.cumulativeReturn !== undefined &&
            benchmarkPercent !== null
              ? summary.cumulativeReturn - benchmarkPercent
              : null,
        },
        {
          label: "CAGR",
          strategy: summary?.cagr ?? null,
          benchmark: null,
          diff: null,
        },
        {
          label: "MDD",
          strategy: summary?.maxDrawdown ?? null,
          benchmark: null,
          diff: null,
        },
      ],
    },
    {
      title: "리스크 지표",
      rows: [
        {
          label: "샤프",
          strategy: summary?.sharpe ?? null,
          benchmark: null,
          diff: null,
          signed: false,
        },
        {
          label: "변동성",
          strategy: summary?.volatility ?? null,
          benchmark: null,
          diff: null,
        },
        {
          label: "승률",
          strategy: summary?.winRate ?? null,
          benchmark: null,
          diff: null,
        },
      ],
    },
  ];
}

function getWinCount(annualReturns: BacktestAnnualReturn[]) {
  return annualReturns.filter((item) => item.strategy > 0).length;
}

function formatDateLabel(value: string | null | undefined) {
  return value ? value.replaceAll("-", ".") : "-";
}
type YearlyPositionChange = {
  year: number;
  rebalances: BacktestRebalance[];
  latestPositions: BacktestPosition[];
  enteredCount: number;
  exitedCount: number;
};

function formatPositionWeight(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "비중 -";
  }

  return `비중 ${percentFormatter.format(value * 100)}%`;
}

function buildYearlyPositionChanges(
  rebalanceHistory: BacktestRebalance[],
): YearlyPositionChange[] {
  const byYear = new Map<number, YearlyPositionChange>();

  for (const rebalance of rebalanceHistory) {
    const year = Number(rebalance.rebalanceDate.slice(0, 4));

    if (!Number.isFinite(year)) {
      continue;
    }

    const group = byYear.get(year) ?? {
      year,
      rebalances: [],
      latestPositions: [],
      enteredCount: 0,
      exitedCount: 0,
    };

    group.rebalances.push(rebalance);
    group.latestPositions = rebalance.positions;
    group.enteredCount += rebalance.enteredPositions.length;
    group.exitedCount += rebalance.exitedPositions.length;
    byYear.set(year, group);
  }

  return [...byYear.values()].sort((left, right) => right.year - left.year);
}

function PositionChip({
  position,
  tone,
}: {
  position: BacktestPosition;
  tone?: "entered" | "exited" | "held";
}) {
  return (
    <span className={`position-chip ${tone ?? ""}`}>
      <strong>{position.ticker}</strong>
      <em>{position.name}</em>
      <small>{formatPositionWeight(position.weight)}</small>
    </span>
  );
}

function CumulativeReturnChart({ data }: { data: BacktestEquityPoint[] }) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = containerRef.current;

    if (!container || data.length === 0) {
      return undefined;
    }

    const chart = createChart(container, {
      width: container.clientWidth,
      height: 430,
      layout: {
        background: { type: ColorType.Solid, color: "#0d1524" },
        textColor: "#8fa0b8",
        fontFamily:
          "Inter, Pretendard, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
      },
      grid: {
        vertLines: { color: "rgba(45, 61, 86, 0.55)" },
        horzLines: { color: "rgba(45, 61, 86, 0.55)" },
      },
      rightPriceScale: {
        borderColor: "#26364d",
      },
      timeScale: {
        borderColor: "#26364d",
      },
    });

    const strategySeries = chart.addSeries(AreaSeries, {
      lineColor: "#22d39b",
      topColor: "rgba(34, 211, 155, 0.32)",
      bottomColor: "rgba(34, 211, 155, 0.02)",
      lineWidth: 3,
      priceFormat: { type: "percent" },
    });
    strategySeries.setData(
      data.map((point) => ({
        time: point.date as Time,
        value: point.strategy,
      })),
    );

    const benchmarkNames = [
      ...new Set(data.flatMap((point) => Object.keys(point.benchmarks))),
    ];

    benchmarkNames.forEach((benchmarkName, index) => {
      const benchmarkRows = data
        .filter((point) => point.benchmarks[benchmarkName] !== null)
        .map((point) => ({
          time: point.date as Time,
          value: point.benchmarks[benchmarkName] as number,
        }));

      if (benchmarkRows.length === 0) {
        return;
      }

      const benchmarkSeries = chart.addSeries(LineSeries, {
        color: benchmarkColors[index % benchmarkColors.length],
        lineWidth: 2,
        priceFormat: { type: "percent" },
      });
      benchmarkSeries.setData(benchmarkRows);
    });

    const cashRows = data
      .filter((point) => point.cash !== null)
      .map((point) => ({
        time: point.date as Time,
        value: point.cash as number,
      }));

    if (cashRows.length > 0) {
      const cashSeries = chart.addSeries(LineSeries, {
        color: "#9ba5ff",
        lineWidth: 1,
        priceFormat: { type: "percent" },
        lastValueVisible: false,
        priceLineVisible: false,
      });
      cashSeries.setData(cashRows);
    }

    chart.timeScale().fitContent();
    const observer = new ResizeObserver(([entry]) => {
      chart.applyOptions({
        width: Math.floor(entry.contentRect.width),
      });
    });
    observer.observe(container);

    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, [data]);

  return <div className="backtest-chart-canvas" ref={containerRef} />;
}

function AnnualReturnChart({ data }: { data: BacktestAnnualReturn[] }) {
  const containerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = containerRef.current;

    if (!container || data.length === 0) {
      return undefined;
    }

    const chart = createChart(container, {
      width: container.clientWidth,
      height: 238,
      layout: {
        background: { type: ColorType.Solid, color: "#0d1524" },
        textColor: "#8fa0b8",
        fontFamily:
          "Inter, Pretendard, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
      },
      grid: {
        vertLines: { color: "rgba(45, 61, 86, 0.36)" },
        horzLines: { color: "rgba(45, 61, 86, 0.52)" },
      },
      rightPriceScale: {
        borderColor: "#26364d",
      },
      timeScale: {
        borderColor: "#26364d",
      },
    });

    const series = chart.addSeries(HistogramSeries, {
      priceFormat: { type: "percent" },
      base: 0,
    });
    series.setData(
      data
        .slice()
        .reverse()
        .map((point) => ({
          time: `${point.year}-01-01` as Time,
          value: point.strategy,
          color: point.strategy >= 0 ? "#22d39b" : "#ef6067",
        })),
    );

    chart.timeScale().fitContent();
    const observer = new ResizeObserver(([entry]) => {
      chart.applyOptions({
        width: Math.floor(entry.contentRect.width),
      });
    });
    observer.observe(container);

    return () => {
      observer.disconnect();
      chart.remove();
    };
  }, [data]);

  return <div className="backtest-annual-chart" ref={containerRef} />;
}

export const BacktestResultPage = observer(
  ({ store }: BacktestResultPageProps) => {
    const result = store.backtestResult;
    const summary = result?.summary;
    const annualReturns = result?.annualReturns ?? [];
    const equityCurve = result?.equityCurve ?? [];
    const rebalanceHistory = result?.rebalanceHistory ?? [];
    const latestHoldingPositions = rebalanceHistory.at(-1)?.positions ?? [];
    const yearlyPositionChanges = buildYearlyPositionChanges(rebalanceHistory);
    const benchmarkNames = summary?.benchmarkNames ?? [];
    const primaryBenchmarkName = benchmarkNames[0] ?? null;
    const selectedLabels = store.selectedConditions.map(
      (condition) => condition.label,
    );
    const metricSections = getMetricSections(result);
    const winCount = getWinCount(annualReturns);
    const winRate =
      summary?.winRate ??
      (annualReturns.length > 0
        ? (winCount / annualReturns.length) * 100
        : null);
    const latestPositions = result
      ? latestHoldingPositions.length
      : store.result?.total ?? 0;
    const averageExcess =
      annualReturns.length > 0 && primaryBenchmarkName
        ? annualReturns.reduce(
            (sum, item) =>
              sum + (item.excessReturns[primaryBenchmarkName] ?? 0),
            0,
          ) / annualReturns.length
        : null;
    const isInitialLoading = store.isBacktesting && !result;

    useEffect(() => {
      if (
        store.viewMode === "backtest" &&
        !store.backtestResult &&
        !store.isBacktesting &&
        store.selectedConditionCount > 0
      ) {
        void store.runBacktest();
      }
    }, [
      store,
      store.viewMode,
      store.backtestResult,
      store.isBacktesting,
      store.selectedConditionCount,
    ]);

    return (
      <section className="quant-page result-view backtest-page">
        <header className="backtest-header">
          <div>
            <span>STRATEGY BACKTEST</span>
            <h1>전략 성과 분석</h1>
            <p>
              {formatDateLabel(summary?.startDate)} -{" "}
              {formatDateLabel(summary?.endDate)} - {store.market} ·{" "}
              {rebalanceLabels[
                summary?.rebalanceFrequency ?? store.backtestRebalanceFrequency
              ] ??
                summary?.rebalanceFrequency ??
                store.backtestRebalanceFrequency}{" "}
              리밸런싱
            </p>
          </div>
          <div className="header-actions">
            <button
              className="back-button"
              type="button"
              onClick={() => store.backToBuilder()}
            >
              <ArrowLeft size={15} />
              조건 수정
            </button>
            <button className="green-button" type="button">
              <Save size={15} />
              전략 저장
            </button>
            <button
              className="icon-chip"
              type="button"
              aria-label="닫기"
              onClick={() => store.backToBuilder()}
            >
              <X size={16} />
            </button>
          </div>
        </header>

        <div className="backtest-scroll">
          <section className="backtest-control-bar">
            <div className="backtest-control-group">
              <span>국가</span>
              {["KR", "US", "JP", "DE", "CN", "IN"].map((market) => (
                <button
                  className={market === store.market ? "selected" : ""}
                  key={market}
                  type="button"
                >
                  {market}
                </button>
              ))}
            </div>
            <div className="backtest-control-group">
              <span>리밸런싱</span>
              {rebalanceOptions.map(({ label, frequency }) => (
                <button
                  className={
                    frequency === store.backtestRebalanceFrequency
                      ? "selected"
                      : ""
                  }
                  key={frequency}
                  type="button"
                  onClick={() => store.setBacktestRebalanceFrequency(frequency)}
                >
                  {label}
                </button>
              ))}
            </div>
            <div className="backtest-date-range">
              <span>기간</span>
              <button type="button">
                {summary?.startDate ?? "2016-01-01"}
                <Calendar size={14} />
              </button>
              <button type="button">
                {summary?.endDate ?? "2026-04-26"}
                <Calendar size={14} />
              </button>
            </div>
            <button
              className="blue-button"
              type="button"
              onClick={() => void store.runBacktest()}
              disabled={store.isBacktesting}
            >
              {store.isBacktesting ? <Loader2 size={15} /> : <Play size={15} />}
              {store.isBacktesting ? "백테스트 실행 중" : "백테스트 재실행"}
            </button>
          </section>

          {store.backtestErrorMessage && (
            <p className="error-message">{store.backtestErrorMessage}</p>
          )}

          {isInitialLoading ? (
            <section className="backtest-card backtest-loading-panel">
              <Loader2 size={24} />
              <p>백테스트 결과를 계산하는 중...</p>
            </section>
          ) : (
            <>
              <section className="backtest-grid">
                <article className="backtest-card cumulative-card">
                  <div className="backtest-section-title">
                    <div>
                      <h2>누적 수익률 추이</h2>
                      <p>
                        {formatDateLabel(summary?.startDate)} -{" "}
                        {formatDateLabel(summary?.endDate)} ·{" "}
                        {benchmarkNames.length > 0
                          ? benchmarkNames.map(formatBenchmarkName).join(", ")
                          : "벤치마크"}{" "}
                        비교
                      </p>
                    </div>
                    <div className="backtest-legend">
                      <span className="strategy">전략</span>
                      {benchmarkNames.map((benchmarkName, index) => (
                        <span
                          className="benchmark"
                          key={benchmarkName}
                          style={
                            {
                              "--legend-color":
                                benchmarkColors[index % benchmarkColors.length],
                            } as CSSProperties
                          }
                        >
                          {formatBenchmarkName(benchmarkName)}
                        </span>
                      ))}
                    </div>
                  </div>
                  {equityCurve.length > 0 ? (
                    <CumulativeReturnChart data={equityCurve} />
                  ) : (
                    <div className="backtest-empty">
                      표시할 누적 수익률 데이터가 없습니다.
                    </div>
                  )}
                </article>

                <aside className="backtest-card metric-card">
                  <nav className="metric-tabs" aria-label="백테스트 결과 탭">
                    {tabs.map((tab, index) => (
                      <button
                        className={index === 0 ? "selected" : ""}
                        key={tab}
                        type="button"
                      >
                        {tab}
                      </button>
                    ))}
                  </nav>

                  <div className="metric-section-stack">
                    {metricSections.map((section) => (
                      <section className="metric-section" key={section.title}>
                        <h2>{section.title}</h2>
                        <div className="metric-table">
                          <div className="metric-table-head">
                            <span />
                            <span>전략</span>
                            <span>벤치마크</span>
                            <span>차이</span>
                          </div>
                          {section.rows.map((row) => (
                            <div className="metric-row" key={row.label}>
                              <span>{row.label}</span>
                              <strong className={getMetricClass(row.strategy)}>
                                {formatMetric(row.strategy, row.signed)}
                              </strong>
                              <strong className={getMetricClass(row.benchmark)}>
                                {formatMetric(row.benchmark, row.signed)}
                              </strong>
                              <strong className="blue-value">
                                {formatMetric(row.diff, row.signed)}
                              </strong>
                            </div>
                          ))}
                        </div>
                      </section>
                    ))}
                  </div>

                  <button
                    className="green-button full-width-button"
                    type="button"
                  >
                    <Database size={15} />
                    포트폴리오 편입 종목 ({latestPositions}개)
                  </button>
                </aside>
              </section>

              <section className="backtest-card position-change-card">
                <div className="backtest-section-title">
                  <div>
                    <h2>편입/편출 종목</h2>
                    <p>연도별 리밸런싱 변경 내역과 현재 보유 종목입니다.</p>
                  </div>
                </div>

                <div className="position-change-layout">
                  <section className="current-holdings-panel">
                    <div className="position-panel-heading">
                      <span>현재 보유</span>
                      <strong>{latestHoldingPositions.length}개</strong>
                    </div>
                    {latestHoldingPositions.length > 0 ? (
                      <div className="position-chip-list holdings">
                        {latestHoldingPositions.map((position) => (
                          <PositionChip
                            key={position.securityId}
                            position={position}
                            tone="held"
                          />
                        ))}
                      </div>
                    ) : (
                      <p className="position-empty-inline">보유 종목이 없습니다.</p>
                    )}
                  </section>

                  {yearlyPositionChanges.length > 0 ? (
                    <div className="position-timeline">
                      {yearlyPositionChanges.map((yearGroup) => (
                        <article className="position-year-block" key={yearGroup.year}>
                          <div className="position-year-header">
                            <strong>{yearGroup.year}</strong>
                            <span>
                              편입 {yearGroup.enteredCount} - 편출 {yearGroup.exitedCount} - 보유 {yearGroup.latestPositions.length}
                            </span>
                          </div>

                          {yearGroup.rebalances.map((rebalance) => (
                            <section
                              className="position-rebalance-row"
                              key={rebalance.rebalanceDate}
                            >
                              <div className="position-rebalance-heading">
                                <strong>{formatDateLabel(rebalance.rebalanceDate)}</strong>
                                <span>신호일 {formatDateLabel(rebalance.signalDate)}</span>
                              </div>
                              <div className="position-change-columns">
                                <div className="position-change-column entered">
                                  <h3>편입 {rebalance.enteredPositions.length}</h3>
                                  {rebalance.enteredPositions.length > 0 ? (
                                    <div className="position-chip-list compact">
                                      {rebalance.enteredPositions.map((position) => (
                                        <PositionChip
                                          key={position.securityId}
                                          position={position}
                                          tone="entered"
                                        />
                                      ))}
                                    </div>
                                  ) : (
                                    <p className="position-empty-inline">없음</p>
                                  )}
                                </div>
                                <div className="position-change-column exited">
                                  <h3>편출 {rebalance.exitedPositions.length}</h3>
                                  {rebalance.exitedPositions.length > 0 ? (
                                    <div className="position-chip-list compact">
                                      {rebalance.exitedPositions.map((position) => (
                                        <PositionChip
                                          key={position.securityId}
                                          position={position}
                                          tone="exited"
                                        />
                                      ))}
                                    </div>
                                  ) : (
                                    <p className="position-empty-inline">없음</p>
                                  )}
                                </div>
                                <div className="position-change-column held">
                                  <h3>보유 {rebalance.positions.length}</h3>
                                  {rebalance.positions.length > 0 ? (
                                    <div className="position-chip-list compact">
                                      {rebalance.positions.map((position) => (
                                        <PositionChip
                                          key={position.securityId}
                                          position={position}
                                          tone="held"
                                        />
                                      ))}
                                    </div>
                                  ) : (
                                    <p className="position-empty-inline">없음</p>
                                  )}
                                </div>
                              </div>
                            </section>
                          ))}
                        </article>
                      ))}
                    </div>
                  ) : (
                    <div className="backtest-empty position-empty">
                      리밸런싱 히스토리가 없습니다.
                    </div>
                  )}
                </div>
              </section>
              <section className="backtest-bottom-grid">
                <article className="backtest-card">
                  <div className="backtest-section-title">
                    <div>
                      <h2>연도별 수익률</h2>
                      <p>
                        연간 승률 {formatMetric(winRate)} ({winCount}/
                        {annualReturns.length}) - 평균 초과 수익률{" "}
                        {formatMetric(averageExcess)}
                      </p>
                    </div>
                  </div>
                  {annualReturns.length > 0 ? (
                    <>
                      <AnnualReturnChart data={annualReturns} />
                      <div className="annual-return-strip">
                        {annualReturns.map((item) => (
                          <article key={item.year}>
                            <span>{item.year}</span>
                            <strong className={getMetricClass(item.strategy)}>
                              {formatSignedPercent(item.strategy)}
                            </strong>
                            {benchmarkNames.length > 0 ? (
                              benchmarkNames.map((benchmarkName) => (
                                <em key={benchmarkName}>
                                  {formatBenchmarkName(benchmarkName)}{" "}
                                  {formatSignedPercent(
                                    item.benchmarkReturns[benchmarkName],
                                  )}
                                </em>
                              ))
                            ) : (
                              <em>BM -</em>
                            )}
                          </article>
                        ))}
                      </div>
                    </>
                  ) : (
                    <div className="backtest-empty">
                      표시할 연도별 수익률 데이터가 없습니다.
                    </div>
                  )}
                </article>

                <article className="backtest-card factor-summary-card">
                  <div className="backtest-section-title">
                    <div>
                      <h2>선택 팩터</h2>
                      <p>
                        {selectedLabels.length}개 조건으로 백테스트를
                        구성했습니다
                      </p>
                    </div>
                  </div>
                  <div className="factor-chip-list">
                    {store.selectedConditions.map((condition, index) => (
                      <span key={condition.filterId}>
                        {index + 1}. {condition.label} ·{" "}
                        {condition.inputMode === "percentile"
                          ? `상위 ${condition.percentile}%`
                          : `${condition.operator} ${condition.value}`}
                      </span>
                    ))}
                  </div>
                  {result && result.warnings.length > 0 && (
                    <div className="backtest-warning-list">
                      {result.warnings.map((warning) => (
                        <p key={warning}>{warning}</p>
                      ))}
                    </div>
                  )}
                </article>
              </section>
            </>
          )}
        </div>
      </section>
    );
  },
);
