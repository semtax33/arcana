import { FormEvent, useEffect, useMemo, useRef, useState } from "react";
import {
  Activity,
  BarChart3,
  Bot,
  BriefcaseBusiness,
  Building2,
  FileText,
  LineChart,
  Search,
  Sparkles,
  Star,
  TrendingUp,
} from "lucide-react";
import type { LucideIcon } from "lucide-react";
import {
  AreaSeries,
  CandlestickSeries,
  ColorType,
  CrosshairMode,
  HistogramSeries,
  LineSeries,
  createChart,
  type Time,
} from "lightweight-charts";
import { fetchStockAnalysis } from "../api/stockAnalysisApi";
import type {
  StockAnalysisPoint,
  StockAnalysisResponse,
  StockChartMode,
  StockChartRange,
  StockOverview,
  StyleScore,
} from "../types/stockAnalysis";

const numberFormatter = new Intl.NumberFormat("ko-KR");
const compactFormatter = new Intl.NumberFormat("ko-KR", {
  notation: "compact",
});

const historyStocks = [
  { stockCode: "236200", name: "슈프리마" },
  { stockCode: "005710", name: "대원산업" },
  { stockCode: "440110", name: "파두" },
  { stockCode: "019540", name: "일지테크" },
  { stockCode: "019180", name: "티에이치엔" },
  { stockCode: "005930", name: "삼성전자" },
  { stockCode: "003230", name: "삼양식품" },
];

const topTabs = [
  "차트",
  "개요",
  "재무",
  "애널리스트 전망",
  "스타일 스코어",
  "뉴스",
  "투자자",
  "피어그룹",
  "이벤트",
  "구루 평가",
] as const;

type StockTopTab = (typeof topTabs)[number];

const ranges: StockChartRange[] = ["1M", "3M", "6M", "1Y", "2Y", "5Y", "MAX"];

const chartModes: Array<{
  id: StockChartMode;
  label: string;
  icon: LucideIcon;
}> = [
  { id: "line", label: "라인", icon: LineChart },
  { id: "area", label: "영역", icon: Activity },
  { id: "candle", label: "캔들", icon: BarChart3 },
];

type ChartProps = {
  data: StockAnalysisPoint[];
  mode: StockChartMode;
  name: string;
};

type StockAnalysisPageProps = {
  initialStockCode?: string;
};

function formatPrice(value: number) {
  return numberFormatter.format(Math.round(value));
}

function formatNullableNumber(value: number | null | undefined, digits = 2) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }

  return numberFormatter.format(Number(value.toFixed(digits)));
}

function formatMarketCap(
  value: number | null | undefined,
  market: "KR" | "US" = "KR",
) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }

  if (market === "US") {
    if (value >= 1_000_000_000_000) {
      return `$${(value / 1_000_000_000_000).toFixed(2)}T`;
    }

    if (value >= 1_000_000_000) {
      return `$${(value / 1_000_000_000).toFixed(1)}B`;
    }

    return `$${(value / 1_000_000).toFixed(0)}M`;
  }

  if (value >= 1_0000_0000_0000) {
    return `${Number((value / 1_0000_0000_0000).toFixed(1)).toLocaleString("ko-KR")}조원`;
  }

  if (value >= 1_0000_0000) {
    return `${Number((value / 1_0000_0000).toFixed(0)).toLocaleString("ko-KR")}억원`;
  }

  return `${numberFormatter.format(Math.round(value))}원`;
}

function formatSigned(value: number) {
  const prefix = value > 0 ? "+" : "";
  return `${prefix}${numberFormatter.format(value)}`;
}

function getStatusClass(value: string) {
  if (/Bullish|Strong|Overbought|Above|Uptrend|Buy|Momentum/.test(value)) {
    return "positive";
  }

  if (/Bearish|Oversold|Below|Down|Sell/.test(value)) {
    return "negative";
  }

  return "neutral";
}

function StockPriceChart({ data, mode, name }: ChartProps) {
  const chartContainerRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const container = chartContainerRef.current;

    if (!container || data.length === 0) {
      return undefined;
    }

    const chart = createChart(container, {
      width: container.clientWidth,
      height: 384,
      layout: {
        background: { type: ColorType.Solid, color: "#111b2a" },
        textColor: "#93a4b8",
        fontFamily:
          "Inter, Pretendard, -apple-system, BlinkMacSystemFont, Segoe UI, sans-serif",
      },
      grid: {
        vertLines: { color: "#223044" },
        horzLines: { color: "#223044" },
      },
      crosshair: {
        mode: CrosshairMode.MagnetOHLC,
        vertLine: { color: "#64748b", labelBackgroundColor: "#1d2939" },
        horzLine: { color: "#64748b", labelBackgroundColor: "#1d2939" },
      },
      rightPriceScale: {
        borderColor: "#25364d",
        scaleMargins: { top: 0.08, bottom: 0.24 },
      },
      timeScale: {
        borderColor: "#25364d",
        timeVisible: true,
        secondsVisible: false,
      },
    });

    const chartRows = data.map((point) => ({
      time: point.date as Time,
      open: point.open,
      high: point.high,
      low: point.low,
      close: point.close,
      value: point.close,
    }));

    if (mode === "candle") {
      const priceSeries = chart.addSeries(CandlestickSeries, {
        upColor: "#19c687",
        downColor: "#ef5d62",
        borderUpColor: "#19c687",
        borderDownColor: "#ef5d62",
        wickUpColor: "#19c687",
        wickDownColor: "#ef5d62",
        priceFormat: { type: "price", precision: 0, minMove: 1 },
      });
      priceSeries.setData(chartRows);
    } else if (mode === "area") {
      const priceSeries = chart.addSeries(AreaSeries, {
        lineColor: "#1bd098",
        topColor: "rgba(27, 208, 152, 0.38)",
        bottomColor: "rgba(27, 208, 152, 0.03)",
        lineWidth: 2,
        priceFormat: { type: "price", precision: 0, minMove: 1 },
      });
      priceSeries.setData(chartRows);
    } else {
      const priceSeries = chart.addSeries(LineSeries, {
        color: "#1bd098",
        lineWidth: 2,
        priceFormat: { type: "price", precision: 0, minMove: 1 },
      });
      priceSeries.setData(chartRows);
    }

    const ma5Series = chart.addSeries(LineSeries, {
      color: "#77e3ff",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      title: "MA5",
    });
    ma5Series.setData(
      data.map((point) => ({ time: point.date as Time, value: point.ma5 })),
    );

    const ma20Series = chart.addSeries(LineSeries, {
      color: "#f5c84c",
      lineWidth: 1,
      priceLineVisible: false,
      lastValueVisible: false,
      title: "MA20",
    });
    ma20Series.setData(
      data.map((point) => ({ time: point.date as Time, value: point.ma20 })),
    );

    const volumeSeries = chart.addSeries(HistogramSeries, {
      priceScaleId: "",
      priceFormat: { type: "volume" },
      priceLineVisible: false,
      lastValueVisible: false,
    });
    volumeSeries.setData(
      data.map((point) => ({
        time: point.date as Time,
        value: point.volume,
        color:
          point.close >= point.open
            ? "rgba(25, 198, 135, 0.48)"
            : "rgba(239, 93, 98, 0.48)",
      })),
    );

    chart.priceScale("").applyOptions({
      scaleMargins: { top: 0.78, bottom: 0 },
    });
    chart.timeScale().fitContent();

    const resizeObserver = new ResizeObserver(([entry]) => {
      chart.applyOptions({
        width: Math.floor(entry.contentRect.width),
      });
    });
    resizeObserver.observe(container);

    return () => {
      resizeObserver.disconnect();
      chart.remove();
    };
  }, [data, mode]);

  return (
    <figure className="stock-chart-frame" aria-label={`${name} 주가 차트`}>
      <div className="stock-chart-canvas" ref={chartContainerRef} />
    </figure>
  );
}

function StyleRadar({ scores }: { scores: StyleScore[] }) {
  const center = 92;
  const radius = 68;
  const points = scores.map((score, index) => {
    const angle = -Math.PI / 2 + (index / scores.length) * Math.PI * 2;
    const valueRadius = radius * (score.value / 100);

    return {
      axisX: center + Math.cos(angle) * radius,
      axisY: center + Math.sin(angle) * radius,
      labelX: center + Math.cos(angle) * (radius + 18),
      labelY: center + Math.sin(angle) * (radius + 18),
      valueX: center + Math.cos(angle) * valueRadius,
      valueY: center + Math.sin(angle) * valueRadius,
      label: score.label,
    };
  });
  const polygon = points
    .map((point) => `${point.valueX},${point.valueY}`)
    .join(" ");

  return (
    <svg
      className="style-radar"
      viewBox="0 0 184 184"
      role="img"
      aria-label="스타일 스코어"
    >
      {[0.35, 0.65, 1].map((scale) => (
        <polygon
          className="radar-grid"
          key={scale}
          points={points
            .map(
              (point) =>
                `${center + (point.axisX - center) * scale},${center + (point.axisY - center) * scale}`,
            )
            .join(" ")}
        />
      ))}
      {points.map((point) => (
        <g key={point.label}>
          <line
            className="radar-axis"
            x1={center}
            x2={point.axisX}
            y1={center}
            y2={point.axisY}
          />
          <text className="radar-label" x={point.labelX} y={point.labelY}>
            {point.label}
          </text>
        </g>
      ))}
      <polygon className="radar-value" points={polygon} />
    </svg>
  );
}

function formatWeek52Spread(overview?: StockOverview | null) {
  const low = overview?.week52Low;
  const high = overview?.week52High;

  if (!low || !high || low <= 0) {
    return {
      range: "-",
      spread: "-",
    };
  }

  return {
    range: `${formatPrice(low)} ~ ${formatPrice(high)}`,
    spread: `${Number((overview?.week52ChangeRate ?? ((high - low) / low) * 100).toFixed(1)).toLocaleString("ko-KR")}%`,
  };
}

function StockOverviewPanel({
  data,
  isLoading,
  stockCode,
}: {
  data: StockAnalysisResponse | null;
  isLoading: boolean;
  stockCode: string;
}) {
  const summary = data?.summary;
  const overview = data?.overview;
  const week52 = formatWeek52Spread(overview);
  const metricCards = [
    {
      label: "시가총액",
      value: formatMarketCap(overview?.marketCap, summary?.market),
      detail: summary?.market === "US" ? "USD" : "KRW",
    },
    {
      label: "PER",
      value: formatNullableNumber(overview?.per),
      detail: "Trailing",
    },
    {
      label: "배당수익률",
      value:
        overview?.dividendYield === null ||
        overview?.dividendYield === undefined
          ? "-"
          : `${formatNullableNumber(overview.dividendYield)}%`,
      detail: "시가배당률",
    },
    {
      label: "52주 변동폭",
      value: week52.spread,
      detail: week52.range,
    },
  ];

  if (isLoading && !data) {
    return <div className="overview-loading">개요 데이터를 불러오는 중...</div>;
  }

  return (
    <div className="stock-overview">
      <section className="overview-hero-panel">
        <div className="overview-title-block">
          <h1>
            {summary?.name ?? "종목"}{" "}
            <span>({summary?.stockCode ?? stockCode})</span>
          </h1>
          <p>{summary?.industryLabel ?? ""}</p>
        </div>
        <div className="overview-price-block">
          <strong>{summary ? formatPrice(summary.latestPrice) : "-"}</strong>
          <span>52주 {week52.range}</span>
        </div>
        <div className="overview-metric-grid">
          {metricCards.map((metric) => (
            <article className="overview-metric-card" key={metric.label}>
              <span>{metric.label}</span>
              <strong>{metric.value}</strong>
              <em>{metric.detail}</em>
            </article>
          ))}
        </div>
      </section>

      <section className="overview-section">
        <div className="overview-section-title">
          <Building2 size={18} />
          <h2>회사 소개</h2>
        </div>
        <p className="company-description">
          {overview?.companyDescription ?? ""}
        </p>
      </section>

      <section className="overview-section">
        <div className="overview-section-title">
          <BriefcaseBusiness size={18} />
          <h2>사업 영역</h2>
        </div>
        <div className="gics-badge-row">
          {(overview?.gicsIndustries ?? []).map((industry) => (
            <span className="gics-badge" key={industry}>
              {industry}
            </span>
          ))}
        </div>
      </section>
    </div>
  );
}

export function StockAnalysisPage({
  initialStockCode = "236200",
}: StockAnalysisPageProps) {
  const [stockCode, setStockCode] = useState(initialStockCode);
  const [inputValue, setInputValue] = useState(initialStockCode);
  const [activeTab, setActiveTab] = useState<StockTopTab>("차트");
  const [range, setRange] = useState<StockChartRange>("1Y");
  const [chartMode, setChartMode] = useState<StockChartMode>("candle");
  const [data, setData] = useState<StockAnalysisResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [errorMessage, setErrorMessage] = useState("");

  useEffect(() => {
    let ignore = false;

    setIsLoading(true);
    setErrorMessage("");
    void fetchStockAnalysis(stockCode, range)
      .then((response) => {
        if (!ignore) {
          setData(response);
        }
      })
      .catch((error: Error) => {
        if (!ignore) {
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
  }, [stockCode, range]);

  const latest = data?.summary;
  const recentRows = data?.recentData ?? [];
  const currentHistory = useMemo(
    () =>
      historyStocks.some((stock) => stock.stockCode === stockCode)
        ? historyStocks
        : [
            { stockCode, name: latest?.name ?? `${stockCode} 종목` },
            ...historyStocks,
          ],
    [latest?.name, stockCode],
  );

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const nextStockCode = inputValue.trim();

    if (nextStockCode.length > 0) {
      setStockCode(nextStockCode);
    }
  };

  return (
    <section className="stock-analysis-page">
      <aside className="stock-analysis-aside">
        <div className="stock-panel-header">
          <div>
            <h1>종목 분석</h1>
            <p>차트와 기술 지표</p>
          </div>
          <Sparkles size={17} />
        </div>

        <form className="stock-search-box" onSubmit={handleSubmit}>
          <Search size={15} />
          <input
            aria-label="stock_search"
            placeholder="종목 검색.."
            type="search"
            value={inputValue}
            onChange={(event) => setInputValue(event.target.value)}
          />
        </form>

        <form className="manual-stock-form" onSubmit={handleSubmit}>
          <span>직접 입력</span>
          <div>
            <input
              aria-label="stock_code"
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              placeholder="stock_code"
            />
            <button type="submit">KR</button>
          </div>
        </form>

        <div className="history-list">
          <div className="history-title">
            <span>검색 히스토리</span>
            <button type="button">전체 삭제</button>
          </div>
          {currentHistory.map((stock) => (
            <button
              className={stock.stockCode === stockCode ? "selected" : ""}
              key={stock.stockCode}
              type="button"
              onClick={() => {
                setInputValue(stock.stockCode);
                setStockCode(stock.stockCode);
              }}
            >
              <strong>{stock.name}</strong>
              <span>{stock.stockCode} · KR</span>
            </button>
          ))}
        </div>
      </aside>

      <main className="stock-analysis-main">
        <header className="stock-topbar">
          <nav className="stock-tab-row" aria-label="종목 분석 메뉴">
            {topTabs.map((tab) => (
              <button
                className={tab === activeTab ? "active" : ""}
                key={tab}
                type="button"
                onClick={() => setActiveTab(tab)}
              >
                {tab === "차트" && <TrendingUp size={15} />}
                {tab === "개요" && <FileText size={15} />}
                {tab === "스타일 스코어" && <Star size={15} />}
                <span>{tab}</span>
              </button>
            ))}
          </nav>
          <div className="stock-top-actions">
            <strong>{latest?.name ?? "종목"}</strong>
            <span>{latest?.stockCode ?? stockCode}</span>
            <button type="button">
              <FileText size={14} />
              PDF
            </button>
          </div>
        </header>

        <div className="stock-analysis-scroll">
          {errorMessage && <p className="stock-error">{errorMessage}</p>}

          {activeTab === "개요" ? (
            <StockOverviewPanel
              data={data}
              isLoading={isLoading}
              stockCode={stockCode}
            />
          ) : (
            <>
              <section className="chart-toolbar">
                <div className="chart-toolbar-row">
                  <div className="segmented-control">
                    <button className="selected" type="button">
                      기본 차트
                    </button>
                    <button type="button">새로고침</button>
                  </div>

                  <div className="range-tabs" aria-label="차트 기간">
                    {ranges.map((item) => (
                      <button
                        className={range === item ? "selected" : ""}
                        key={item}
                        type="button"
                        onClick={() => setRange(item)}
                      >
                        {item}
                      </button>
                    ))}
                  </div>

                  <div className="chart-mode-tabs" aria-label="차트 종류">
                    {chartModes.map((modeItem) => (
                      <button
                        className={chartMode === modeItem.id ? "selected" : ""}
                        key={modeItem.id}
                        title={`${modeItem.label} 차트`}
                        type="button"
                        onClick={() => setChartMode(modeItem.id)}
                      >
                        <modeItem.icon size={14} />
                        <span>{modeItem.label}</span>
                      </button>
                    ))}
                  </div>

                  <button className="ai-button" type="button">
                    <Bot size={15} />
                    AI 분석
                  </button>
                </div>

                <div className="indicator-row">
                  {["MA5", "MA20", "EMA12", "EMA26", "매수"].map(
                    (indicator) => (
                      <span key={indicator}>{indicator}</span>
                    ),
                  )}
                </div>
              </section>

              <section className="chart-content-grid">
                <article className="style-score-panel">
                  <h2>스타일 스코어</h2>
                  <StyleRadar scores={latest?.scores ?? []} />
                  <p>
                    <span>스타일 스코어</span>
                    <strong>{latest?.styleScore ?? 0}</strong>
                    <em>/100</em>
                  </p>
                </article>

                <article className="price-chart-panel">
                  <div className="price-summary">
                    <span>{latest?.latestDate ?? "-"}</span>
                    <strong>
                      {latest ? formatPrice(latest.latestPrice) : "-"}
                    </strong>
                    <em
                      className={
                        (latest?.priceChange ?? 0) >= 0
                          ? "positive"
                          : "negative"
                      }
                    >
                      {latest
                        ? `${formatSigned(latest.priceChange)} (${formatSigned(latest.priceChangeRate)}%)`
                        : "-"}
                    </em>
                  </div>

                  {isLoading || !data ? (
                    <div className="chart-loading">
                      차트 데이터를 불러오는 중...
                    </div>
                  ) : (
                    <StockPriceChart
                      data={data.chart}
                      mode={chartMode}
                      name={data.summary.name}
                    />
                  )}
                </article>
              </section>

              <section className="recent-data-panel">
                <div className="recent-data-title">
                  <h2>최근 데이터 (최신 30일)</h2>
                  <p>모멘텀, RSI, 볼린저밴드, 추세, MACD, 거래량 신호</p>
                </div>

                <div className="recent-table-wrap">
                  <table className="recent-data-table">
                    <thead>
                      <tr>
                        <th>날짜</th>
                        <th>시가</th>
                        <th>고가</th>
                        <th>저가</th>
                        <th>종가</th>
                        <th>거래량</th>
                        <th>월간 수익률</th>
                        <th>모멘텀</th>
                        <th>RSI</th>
                        <th>볼린저</th>
                        <th>추세</th>
                        <th>MACD</th>
                        <th>거래량 신호</th>
                      </tr>
                    </thead>
                    <tbody>
                      {recentRows.map((row) => (
                        <tr key={row.date}>
                          <td>{row.date}</td>
                          <td>{formatPrice(row.open)}</td>
                          <td>{formatPrice(row.high)}</td>
                          <td>{formatPrice(row.low)}</td>
                          <td>{formatPrice(row.close)}</td>
                          <td>{compactFormatter.format(row.volume)}</td>
                          <td
                            className={
                              row.weeklyReturn >= 0 ? "positive" : "negative"
                            }
                          >
                            {formatSigned(row.weeklyReturn)}%
                          </td>
                          <td className={getStatusClass(row.momentum)}>
                            {row.momentum}
                          </td>
                          <td className={getStatusClass(row.rsi)}>{row.rsi}</td>
                          <td className={getStatusClass(row.bollinger)}>
                            {row.bollinger}
                          </td>
                          <td className={getStatusClass(row.trend)}>
                            {row.trend}
                          </td>
                          <td className={getStatusClass(row.macd)}>
                            {row.macd}
                          </td>
                          <td className={getStatusClass(row.volumeSignal)}>
                            {row.volumeSignal}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </section>
            </>
          )}
        </div>
      </main>
    </section>
  );
}
