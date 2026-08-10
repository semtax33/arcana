from __future__ import annotations

import argparse
import base64
import html
import math
from dataclasses import dataclass
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd


TRADING_DAYS = 252
RETURN_WINDOW = 126
VOLATILITY_WINDOW = 252
HISTORY_WINDOW = 1260
BASELINE_MIN_PERIODS = 252
CASH_TICKER = "^IRX"

# ^IRX is the 13-week U.S. Treasury-bill yield used as the cash proxy. The
# country and sector ETFs below include a long-history core so that the chart
# can begin in 2000 while newer ETFs join the daily cross-section when listed.
ASSETS: dict[str, str] = {
    "SPY": "미국 대형주",
    "QQQ": "미국 기술주",
    "IWM": "미국 소형주",
    "EWA": "호주 주식",
    "EWC": "캐나다 주식",
    "EWD": "스웨덴 주식",
    "EWG": "독일 주식",
    "EWH": "홍콩 주식",
    "EWI": "이탈리아 주식",
    "EWL": "스위스 주식",
    "EWM": "말레이시아 주식",
    "EWP": "스페인 주식",
    "EWS": "싱가포르 주식",
    "EWU": "영국 주식",
    "EWW": "멕시코 주식",
    "EFA": "선진국 주식",
    "EEM": "신흥국 주식",
    "EWJ": "일본 주식",
    "EWY": "한국 주식",
    "EWZ": "브라질 주식",
    "INDA": "인도 주식",
    "HYG": "미국 하이일드",
    "LQD": "미국 투자등급 회사채",
    "EMB": "신흥국 국채",
    "TLT": "미국 장기국채",
    "IEF": "미국 중기국채",
    "GLD": "금",
    "SLV": "은",
    "DBC": "원자재",
    "VNQ": "미국 리츠",
    "XLK": "미국 기술 섹터",
    "XLF": "미국 금융 섹터",
    "XLE": "미국 에너지 섹터",
    "XLI": "미국 산업재 섹터",
    "XLU": "미국 유틸리티 섹터",
}

COUNTRY_TICKERS: dict[str, str] = {
    "미국": "SPY",
    "호주": "EWA",
    "캐나다": "EWC",
    "스웨덴": "EWD",
    "독일": "EWG",
    "홍콩": "EWH",
    "이탈리아": "EWI",
    "일본": "EWJ",
    "스위스": "EWL",
    "말레이시아": "EWM",
    "스페인": "EWP",
    "싱가포르": "EWS",
    "영국": "EWU",
    "멕시코": "EWW",
    "한국": "EWY",
    "브라질": "EWZ",
    "인도": "INDA",
}

# Direct market-risk context. Defensive bond/cash assets are intentionally
# omitted so realized volatility and drawdown are not diluted by BIL/Treasuries.
RISK_BASKET = [
    "SPY",
    "QQQ",
    "IWM",
    "EFA",
    "EEM",
    "EWJ",
    "EWY",
    "EWZ",
    "EWA",
    "EWC",
    "EWD",
    "EWG",
    "EWH",
    "EWI",
    "EWL",
    "EWM",
    "EWP",
    "EWS",
    "EWU",
    "EWW",
    "INDA",
    "HYG",
    "EMB",
    "DBC",
    "VNQ",
    "XLK",
    "XLF",
    "XLE",
    "XLI",
]


@dataclass(frozen=True)
class RegressionStats:
    intercept: float
    slope: float
    t_value: float
    r_squared: float
    n_assets: int


def _extract_close(download: pd.DataFrame, tickers: list[str]) -> pd.DataFrame:
    """Normalize yfinance output across single- and multi-ticker layouts."""
    if download.empty:
        raise RuntimeError("Yahoo Finance가 빈 데이터를 반환했습니다.")

    if isinstance(download.columns, pd.MultiIndex):
        first_level = download.columns.get_level_values(0)
        last_level = download.columns.get_level_values(-1)
        if "Close" in first_level:
            close = download["Close"]
        elif "Close" in last_level:
            close = download.xs("Close", axis=1, level=-1)
        else:
            raise RuntimeError("Yahoo Finance 결과에서 Close 열을 찾지 못했습니다.")
    elif "Close" in download.columns:
        close = download[["Close"]].rename(columns={"Close": tickers[0]})
    else:
        raise RuntimeError("Yahoo Finance 결과에서 Close 열을 찾지 못했습니다.")

    close = close.copy()
    close.columns = [str(column) for column in close.columns]
    return close.sort_index().apply(pd.to_numeric, errors="coerce")


def download_prices(start: str, end: str | None = None) -> pd.DataFrame:
    """Download adjusted daily closes for the dashboard universe."""
    import yfinance as yf

    tickers = [CASH_TICKER, *ASSETS]
    data = yf.download(
        tickers=tickers,
        start=start,
        end=end,
        auto_adjust=True,
        actions=False,
        progress=False,
        threads=True,
        group_by="column",
    )
    prices = _extract_close(data, tickers).reindex(columns=tickers)
    prices = prices.dropna(axis=1, how="all").ffill(limit=3)

    if CASH_TICKER not in prices:
        raise RuntimeError(f"현금금리 대용치 {CASH_TICKER} 데이터를 받지 못했습니다.")
    if len(prices.columns) < 16:
        raise RuntimeError(
            f"유효 자산이 {len(prices.columns)}개뿐입니다. 최소 16개가 필요합니다."
        )
    return prices


def load_prices(
    start: str,
    end: str | None,
    cache_path: Path,
    offline: bool,
) -> pd.DataFrame:
    if offline:
        if not cache_path.exists():
            raise FileNotFoundError(f"오프라인 가격 캐시가 없습니다: {cache_path}")
        cached = pd.read_parquet(cache_path)
        if CASH_TICKER not in cached:
            raise RuntimeError(
                f"기존 캐시에 {CASH_TICKER}가 없습니다. --offline 없이 한 번 갱신하세요."
            )
        return cached

    try:
        prices = download_prices(start, end)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        prices.to_parquet(cache_path)
        return prices
    except Exception:
        if cache_path.exists():
            print(f"WARN: 다운로드 실패. 기존 캐시를 사용합니다: {cache_path}")
            return pd.read_parquet(cache_path)
        raise


def _winsorize(series: pd.Series, lower: float = 0.025, upper: float = 0.975) -> pd.Series:
    low, high = series.quantile([lower, upper])
    return series.clip(lower=low, upper=high)


def _ols(x: pd.Series, y: pd.Series) -> RegressionStats:
    """Small OLS implementation; no statsmodels dependency is required."""
    frame = pd.concat([x.rename("x"), y.rename("y")], axis=1).dropna()
    x_values = frame["x"].to_numpy(dtype=float)
    y_values = frame["y"].to_numpy(dtype=float)
    n = len(frame)
    if n < 4:
        raise ValueError("회귀에 필요한 자산 수가 부족합니다.")

    x_centered = x_values - x_values.mean()
    y_centered = y_values - y_values.mean()
    ss_x = float(np.dot(x_centered, x_centered))
    if math.isclose(ss_x, 0.0):
        raise ValueError("위험도의 횡단면 분산이 0입니다.")

    slope = float(np.dot(x_centered, y_centered) / ss_x)
    intercept = float(y_values.mean() - slope * x_values.mean())
    fitted = intercept + slope * x_values
    residual = y_values - fitted
    rss = float(np.dot(residual, residual))
    tss = float(np.dot(y_centered, y_centered))
    r_squared = 1.0 - rss / tss if tss > 0 else np.nan
    residual_variance = rss / (n - 2)
    standard_error = math.sqrt(residual_variance / ss_x)
    t_value = slope / standard_error if standard_error > 0 else np.nan
    return RegressionStats(intercept, slope, t_value, r_squared, n)


def _rolling_last_percentile(series: pd.Series) -> pd.Series:
    return (
        series.rolling(HISTORY_WINDOW, min_periods=BASELINE_MIN_PERIODS)
        .rank(pct=True)
        .mul(100.0)
    )


def calculate_indicators(
    prices: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, RegressionStats, pd.DataFrame]:
    """Calculate CSFB-style appetite plus direct market-risk context."""
    prices = prices.sort_index().astype(float)
    asset_prices = prices.drop(columns=CASH_TICKER)
    asset_log_returns = np.log(asset_prices).diff()
    # Yahoo's ^IRX close is an annualized discount yield in percentage points.
    # Convert it to an approximate daily continuously compounded cash return.
    cash_yield = prices[CASH_TICKER].ffill().clip(lower=0.0)
    cash_log_returns = np.log1p(cash_yield.div(100.0 * 360.0))

    six_month_return = np.log(asset_prices).diff(RETURN_WINDOW)
    six_month_cash = cash_log_returns.rolling(RETURN_WINDOW).sum()
    six_month_excess = six_month_return.sub(six_month_cash, axis=0)
    annualized_volatility = (
        asset_log_returns.rolling(VOLATILITY_WINDOW).std(ddof=1) * math.sqrt(TRADING_DAYS)
    )

    rows: list[dict[str, float | int | pd.Timestamp]] = []
    residual_rows: list[pd.Series] = []
    for date in prices.index[VOLATILITY_WINDOW:]:
        cross_section = pd.concat(
            [
                annualized_volatility.loc[date].rename("risk"),
                six_month_excess.loc[date].rename("excess_return"),
            ],
            axis=1,
        ).replace([np.inf, -np.inf], np.nan).dropna()
        if len(cross_section) < 12:
            continue

        risk = _winsorize(cross_section["risk"])
        excess = _winsorize(cross_section["excess_return"])
        stats = _ols(risk, excess)
        expected_return = stats.intercept + stats.slope * cross_section["risk"]
        residual = (cross_section["excess_return"] - expected_return).rename(date)
        residual_rows.append(residual)
        rows.append(
            {
                "date": date,
                "rai_raw": stats.slope,
                "t_value": stats.t_value,
                "r_squared": stats.r_squared,
                "n_assets": stats.n_assets,
            }
        )

    if not rows:
        raise RuntimeError("위험선호 지수를 계산할 만큼 충분한 이력이 없습니다.")

    result = pd.DataFrame(rows).set_index("date")
    residuals = pd.DataFrame(residual_rows).sort_index()
    mean_5y = result["rai_raw"].rolling(
        HISTORY_WINDOW, min_periods=BASELINE_MIN_PERIODS
    ).mean()
    std_5y = result["rai_raw"].rolling(
        HISTORY_WINDOW, min_periods=BASELINE_MIN_PERIODS
    ).std(ddof=1)
    result["rai_z_5y"] = (result["rai_raw"] - mean_5y) / std_5y.replace(0.0, np.nan)
    result["rai_ewm20"] = result["rai_z_5y"].ewm(span=20, adjust=False).mean()
    appetite_percentile = _rolling_last_percentile(result["rai_ewm20"])
    result["stress_score"] = 100.0 - appetite_percentile

    available_risk_assets = [ticker for ticker in RISK_BASKET if ticker in prices]
    simple_returns = prices[available_risk_assets].pct_change(fill_method=None)
    equal_weight_return = simple_returns.mean(axis=1, skipna=True)
    equal_weight_index = (1.0 + equal_weight_return.fillna(0.0)).cumprod()
    result["realized_vol_20d"] = (
        equal_weight_return.rolling(20).std(ddof=1) * math.sqrt(TRADING_DAYS) * 100.0
    ).reindex(result.index)
    result["drawdown_3m"] = (
        (equal_weight_index / equal_weight_index.rolling(63).max() - 1.0) * 100.0
    ).reindex(result.index)
    result["breadth_120d"] = (
        (asset_prices > asset_prices.rolling(120).mean()).mean(axis=1) * 100.0
    ).reindex(result.index)
    result["volatility_stress"] = _rolling_last_percentile(result["realized_vol_20d"])
    result["drawdown_stress"] = _rolling_last_percentile(-result["drawdown_3m"])
    result["breadth_stress"] = _rolling_last_percentile(-result["breadth_120d"])
    # A transparent relative-risk composite. It prevents a single low-appetite
    # signal from being presented as an absolute loss-risk conclusion.
    result["market_risk_score"] = (
        0.35 * result["stress_score"]
        + 0.30 * result["volatility_stress"]
        + 0.20 * result["drawdown_stress"]
        + 0.15 * result["breadth_stress"]
    )

    valid_dates = result["market_risk_score"].dropna().index
    if valid_dates.empty:
        raise RuntimeError("스트레스 백분위를 계산하려면 약 4년 이상의 이력이 필요합니다.")
    latest_date = valid_dates[-1]
    cross_section = pd.concat(
        [
            annualized_volatility.loc[latest_date].rename("risk"),
            six_month_excess.loc[latest_date].rename("excess_return"),
        ],
        axis=1,
    ).replace([np.inf, -np.inf], np.nan).dropna()
    cross_section["name"] = [ASSETS.get(ticker, ticker) for ticker in cross_section.index]
    latest_stats = _ols(
        _winsorize(cross_section["risk"]),
        _winsorize(cross_section["excess_return"]),
    )
    return result, cross_section, latest_stats, residuals


def calculate_country_indicators(
    prices: pd.DataFrame,
    global_result: pd.DataFrame,
    residuals: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build country-relative risk scores from the global RAI diagnostics.

    The country score is not a default or crash probability. It combines the
    global environment with four country-specific historical percentiles from
    the perspective of a U.S.-dollar ETF investor.
    """
    available = {
        country: ticker
        for country, ticker in COUNTRY_TICKERS.items()
        if ticker in prices and ticker in residuals
    }
    if len(available) < 8:
        raise RuntimeError("국가별 위험을 계산할 수 있는 장기이력 ETF가 부족합니다.")

    tickers = list(available.values())
    country_prices = prices[tickers].astype(float)
    daily_returns = country_prices.pct_change(fill_method=None)
    cash_yield = prices[CASH_TICKER].ffill().clip(lower=0.0)
    cash_log_returns = np.log1p(cash_yield.div(100.0 * 360.0))
    cash_six_month = cash_log_returns.rolling(RETURN_WINDOW).sum()
    excess_six_month = np.log(country_prices).diff(RETURN_WINDOW).sub(
        cash_six_month, axis=0
    )
    volatility_20d = (
        daily_returns.rolling(20).std(ddof=1) * math.sqrt(TRADING_DAYS) * 100.0
    )
    drawdown_3m = (
        country_prices.div(country_prices.rolling(63).max()).sub(1.0).mul(100.0)
    )
    country_residuals = residuals.reindex(country_prices.index)[tickers]

    residual_stress = country_residuals.apply(
        lambda column: _rolling_last_percentile(-column)
    )
    volatility_stress = volatility_20d.apply(_rolling_last_percentile)
    drawdown_stress = drawdown_3m.apply(
        lambda column: _rolling_last_percentile(-column)
    )
    momentum_stress = excess_six_month.apply(
        lambda column: _rolling_last_percentile(-column)
    )
    global_risk = global_result["market_risk_score"].reindex(country_prices.index)

    country_frames: list[pd.DataFrame] = []
    for country, ticker in available.items():
        frame = pd.DataFrame(
            {
                "global_risk_score": global_risk,
                "residual_stress": residual_stress[ticker],
                "volatility_stress": volatility_stress[ticker],
                "drawdown_stress": drawdown_stress[ticker],
                "momentum_stress": momentum_stress[ticker],
                "realized_vol_20d": volatility_20d[ticker],
                "drawdown_3m": drawdown_3m[ticker],
                "excess_return_6m": excess_six_month[ticker] * 100.0,
                "rai_residual": country_residuals[ticker] * 100.0,
            }
        )
        frame["local_risk_score"] = (
            0.35 * frame["residual_stress"]
            + 0.25 * frame["volatility_stress"]
            + 0.25 * frame["drawdown_stress"]
            + 0.15 * frame["momentum_stress"]
        )
        frame["country_risk_score"] = (
            0.25 * frame["global_risk_score"]
            + 0.75 * frame["local_risk_score"]
        )
        frame["country"] = country
        frame["ticker"] = ticker
        frame.index.name = "date"
        country_frames.append(frame.reset_index())

    history = (
        pd.concat(country_frames, ignore_index=True)
        .sort_values(["date", "country"])
        .set_index(["date", "country"])
    )
    valid = history.dropna(subset=["country_risk_score"]).reset_index()
    snapshot = (
        valid.sort_values("date")
        .groupby("country", as_index=False)
        .tail(1)
        .set_index("country")
        .sort_values("country_risk_score", ascending=False)
    )
    snapshot["regime"] = snapshot["country_risk_score"].map(
        lambda value: market_risk_regime(float(value))[0]
    )
    return history, snapshot


def stress_regime(score: float) -> tuple[str, str]:
    if score < 20:
        return "낮음", "위험선호가 강한 편"
    if score < 40:
        return "보통", "평시 범위"
    if score < 60:
        return "주의", "위험회피가 다소 증가"
    if score < 80:
        return "경계", "위험회피가 뚜렷함"
    return "높음", "과거 대비 강한 위험회피"


def market_risk_regime(score: float) -> tuple[str, str]:
    if score < 20:
        return "낮음", "직접 위험지표도 대체로 안정"
    if score < 40:
        return "보통", "과거 평시 범위"
    if score < 60:
        return "주의", "일부 위험 신호가 상승"
    if score < 80:
        return "경계", "여러 위험 신호가 동반 상승"
    return "높음", "과거 대비 복합 위험이 매우 높음"


def _configure_korean_font() -> None:
    plt.rcParams["font.family"] = ["Malgun Gothic", "DejaVu Sans"]
    plt.rcParams["axes.unicode_minus"] = False


def create_dashboard_figure(
    result: pd.DataFrame,
    cross_section: pd.DataFrame,
    latest_stats: RegressionStats,
    destination: Path,
    chart_start: str,
) -> None:
    _configure_korean_font()
    latest = result.loc[result["market_risk_score"].dropna().index[-1]]
    latest_date = latest.name
    score = float(latest["market_risk_score"])
    regime, interpretation = market_risk_regime(score)

    colors = {
        "ink": "#17202a",
        "muted": "#5d6d7e",
        "line": "#d5d8dc",
        "blue": "#2463a6",
        "green": "#2e7d32",
        "yellow": "#d4a017",
        "orange": "#d2691e",
        "red": "#b3261e",
    }
    fig = plt.figure(figsize=(16, 12), facecolor="white", constrained_layout=True)
    grid = fig.add_gridspec(4, 2, height_ratios=[0.8, 2.1, 1.8, 1.9])

    summary = fig.add_subplot(grid[0, :])
    summary.axis("off")
    summary.text(
        0.0,
        0.92,
        f"시장 위험 대시보드  |  {latest_date:%Y-%m-%d}",
        fontsize=22,
        fontweight="bold",
        color=colors["ink"],
        va="top",
    )
    summary.text(
        0.0,
        0.48,
        f"종합 시장위험 {score:.0f}/100 · {regime} ({interpretation})",
        fontsize=16,
        color=colors["ink"],
        va="top",
    )
    summary.text(
        0.0,
        0.08,
        (
            f"위험회피 스트레스 {latest['stress_score']:.0f}/100  |  "
            f"위험선호 z {latest['rai_ewm20']:.2f}  |  "
            f"20일 실현변동성 {latest['realized_vol_20d']:.1f}%  |  "
            f"3개월 낙폭 {latest['drawdown_3m']:.1f}%  |  "
            f"120일선 상회 비율 {latest['breadth_120d']:.0f}%"
        ),
        fontsize=12,
        color=colors["muted"],
        va="bottom",
    )
    for left, right, color in [
        (0, 20, colors["green"]),
        (20, 40, "#7aa35a"),
        (40, 60, colors["yellow"]),
        (60, 80, colors["orange"]),
        (80, 100, colors["red"]),
    ]:
        summary.barh(0.3, right - left, left=left, height=0.09, color=color, alpha=0.9)
    summary.plot([score, score], [0.21, 0.39], color=colors["ink"], linewidth=3)
    summary.set_xlim(0, 100)
    summary.set_ylim(0, 1)

    stress_ax = fig.add_subplot(grid[1, :])
    history = result.loc[result.index >= pd.Timestamp(chart_start)]
    stress_ax.plot(
        history.index,
        history["market_risk_score"],
        color=colors["blue"],
        linewidth=2.0,
        label="종합 시장위험",
    )
    stress_ax.plot(
        history.index,
        history["stress_score"],
        color=colors["orange"],
        linewidth=1.2,
        alpha=0.72,
        label="위험회피 스트레스",
    )
    stress_ax.axhspan(80, 100, color=colors["red"], alpha=0.10)
    stress_ax.axhspan(60, 80, color=colors["orange"], alpha=0.08)
    stress_ax.axhspan(0, 20, color=colors["green"], alpha=0.08)
    stress_ax.scatter([latest_date], [score], color=colors["red"], s=55, zorder=3)
    stress_ax.annotate(
        f"현재 {score:.0f}",
        (latest_date, score),
        xytext=(-8, 12),
        textcoords="offset points",
        ha="right",
        fontsize=10,
        color=colors["ink"],
    )
    stress_ax.set_title("과거 5년 분포 대비 종합 시장위험", loc="left", fontweight="bold")
    stress_ax.set_ylabel("상대위험 점수 (0~100)")
    stress_ax.set_ylim(0, 100)
    stress_ax.grid(axis="y", color=colors["line"], linewidth=0.7)
    stress_ax.spines[["top", "right"]].set_visible(False)
    chart_years = max(1, history.index.max().year - history.index.min().year)
    stress_ax.xaxis.set_major_locator(mdates.YearLocator(2 if chart_years > 12 else 1))
    stress_ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    stress_ax.legend(loc="upper left", frameon=False, ncol=2)

    context_ax = fig.add_subplot(grid[2, 0])
    context = history.loc[history.index >= history.index.max() - pd.DateOffset(years=2)]
    context_ax.plot(
        context.index,
        context["realized_vol_20d"],
        color=colors["orange"],
        linewidth=1.8,
        label="20일 실현변동성",
    )
    context_ax.set_title("직접 시장위험: 변동성과 낙폭", loc="left", fontweight="bold")
    context_ax.set_ylabel("연율 변동성 (%)", color=colors["orange"])
    context_ax.tick_params(axis="y", labelcolor=colors["orange"])
    context_ax.grid(axis="y", color=colors["line"], linewidth=0.7)
    context_ax.spines[["top"]].set_visible(False)
    drawdown_ax = context_ax.twinx()
    drawdown_ax.fill_between(
        context.index,
        context["drawdown_3m"].to_numpy(dtype=float),
        0,
        color=colors["red"],
        alpha=0.15,
        label="3개월 낙폭",
    )
    drawdown_ax.set_ylabel("3개월 고점 대비 (%)", color=colors["red"])
    drawdown_ax.tick_params(axis="y", labelcolor=colors["red"])
    drawdown_ax.spines[["top"]].set_visible(False)
    context_ax.xaxis.set_major_locator(mdates.MonthLocator(interval=4))
    context_ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m"))
    context_ax.tick_params(axis="x", rotation=30)

    scatter_ax = fig.add_subplot(grid[2, 1])
    scatter_ax.scatter(
        cross_section["risk"] * 100.0,
        cross_section["excess_return"] * 100.0,
        color=colors["blue"],
        alpha=0.78,
        s=42,
    )
    x_line = np.linspace(cross_section["risk"].min(), cross_section["risk"].max(), 100)
    y_line = latest_stats.intercept + latest_stats.slope * x_line
    scatter_ax.plot(x_line * 100.0, y_line * 100.0, color=colors["red"], linewidth=1.8)
    label_candidates = pd.concat(
        [
            cross_section.nsmallest(2, "excess_return"),
            cross_section.nlargest(2, "excess_return"),
        ]
    ).drop_duplicates()
    for ticker, row in label_candidates.iterrows():
        scatter_ax.annotate(
            ticker,
            (row["risk"] * 100.0, row["excess_return"] * 100.0),
            xytext=(4, 5),
            textcoords="offset points",
            fontsize=9,
        )
    scatter_ax.axhline(0, color=colors["line"], linewidth=0.8)
    scatter_ax.set_title(
        f"최신 횡단면 회귀 (기울기 {latest_stats.slope:.2f}, R² {latest_stats.r_squared:.2f})",
        loc="left",
        fontweight="bold",
    )
    scatter_ax.set_xlabel("12개월 연율 변동성 (%)")
    scatter_ax.set_ylabel("6개월 현금초과수익률 (%)")
    scatter_ax.grid(color=colors["line"], linewidth=0.7)
    scatter_ax.spines[["top", "right"]].set_visible(False)

    bar_ax = fig.add_subplot(grid[3, :])
    ranked = cross_section.sort_values("excess_return")
    bar_colors = [colors["red"] if value < 0 else colors["green"] for value in ranked["excess_return"]]
    bar_ax.barh(ranked.index, ranked["excess_return"] * 100.0, color=bar_colors, alpha=0.82)
    bar_ax.axvline(0, color=colors["ink"], linewidth=0.8)
    bar_ax.set_title("자산별 6개월 현금초과수익률", loc="left", fontweight="bold")
    bar_ax.set_xlabel("초과수익률 (%)")
    bar_ax.grid(axis="x", color=colors["line"], linewidth=0.7)
    bar_ax.spines[["top", "right", "left"]].set_visible(False)

    fig.text(
        0.01,
        0.005,
        "주의: 2000년대 초 점수는 짧은 초기 기준기간과 더 적은 자산으로 계산됩니다. 이 점수는 손실확률·VaR·투자 권고가 아닙니다.",
        fontsize=9,
        color=colors["muted"],
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, dpi=155, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def create_country_dashboard_figure(
    country_history: pd.DataFrame,
    country_snapshot: pd.DataFrame,
    destination: Path,
    chart_start: str,
) -> None:
    _configure_korean_font()
    colors = {
        "ink": "#17202a",
        "muted": "#5d6d7e",
        "line": "#d5d8dc",
        "green": "#2e7d32",
        "yellow": "#d4a017",
        "orange": "#d2691e",
        "red": "#b3261e",
    }

    def bar_color(score: float) -> str:
        if score < 20:
            return colors["green"]
        if score < 40:
            return "#7aa35a"
        if score < 60:
            return colors["yellow"]
        if score < 80:
            return colors["orange"]
        return colors["red"]

    latest_date = pd.to_datetime(country_snapshot["date"]).max()
    ranked = country_snapshot.sort_values("country_risk_score")
    fig = plt.figure(figsize=(16, 11), facecolor="white", constrained_layout=True)
    grid = fig.add_gridspec(2, 1, height_ratios=[1.05, 1.8])

    rank_ax = fig.add_subplot(grid[0, 0])
    scores = ranked["country_risk_score"]
    rank_ax.barh(
        ranked.index,
        scores,
        color=[bar_color(float(score)) for score in scores],
        alpha=0.88,
    )
    for position, (country, score) in enumerate(scores.items()):
        rank_ax.text(
            min(float(score) + 1.0, 96.0),
            position,
            f"{score:.0f}",
            va="center",
            fontsize=9,
            color=colors["ink"],
        )
    for threshold in [20, 40, 60, 80]:
        rank_ax.axvline(threshold, color=colors["line"], linewidth=0.8)
    rank_ax.set_xlim(0, 100)
    rank_ax.set_xlabel("국가 상대위험 점수 (0~100)")
    rank_ax.set_title(
        f"국가별 상대위험 순위  |  {latest_date:%Y-%m-%d}",
        loc="left",
        fontweight="bold",
    )
    rank_ax.spines[["top", "right", "left"]].set_visible(False)
    rank_ax.grid(axis="x", color=colors["line"], linewidth=0.6)

    heat_ax = fig.add_subplot(grid[1, 0])
    panel = country_history["country_risk_score"].unstack("country")
    panel = panel.loc[panel.index >= pd.Timestamp(chart_start)]
    monthly = panel.resample("ME").last()
    country_order = country_snapshot.index.tolist()
    heat_data = monthly.reindex(columns=country_order).T
    color_map = plt.colormaps["RdYlGn_r"].copy()
    color_map.set_bad("#eceff1")
    image = heat_ax.imshow(
        np.ma.masked_invalid(heat_data.to_numpy(dtype=float)),
        aspect="auto",
        interpolation="nearest",
        cmap=color_map,
        vmin=0,
        vmax=100,
    )
    heat_ax.set_yticks(np.arange(len(country_order)), labels=country_order)
    tick_positions = np.arange(0, len(monthly), 24)
    heat_ax.set_xticks(
        tick_positions,
        labels=[monthly.index[position].strftime("%Y") for position in tick_positions],
    )
    heat_ax.set_title(
        "국가별 위험 히트맵 — 빨강은 해당 국가의 과거 대비 높은 상대위험",
        loc="left",
        fontweight="bold",
    )
    heat_ax.set_xlabel("연도")
    heat_ax.spines[:].set_visible(False)
    color_bar = fig.colorbar(image, ax=heat_ax, fraction=0.018, pad=0.015)
    color_bar.set_label("상대위험")

    fig.text(
        0.01,
        0.005,
        "국가점수 = 글로벌 환경 25% + 국가 고유위험 75%. USD 상장 ETF 기준이며 국가 부도·폭락확률이 아닙니다.",
        fontsize=9,
        color=colors["muted"],
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(destination, dpi=155, bbox_inches="tight", facecolor="white")
    plt.close(fig)


def create_html_report(
    result: pd.DataFrame,
    cross_section: pd.DataFrame,
    latest_stats: RegressionStats,
    image_path: Path,
    country_snapshot: pd.DataFrame,
    country_image_path: Path,
    destination: Path,
    chart_start: str,
) -> None:
    latest = result.loc[result["market_risk_score"].dropna().index[-1]]
    score = float(latest["market_risk_score"])
    regime, interpretation = market_risk_regime(score)
    appetite_regime, appetite_interpretation = stress_regime(float(latest["stress_score"]))
    image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    country_image_base64 = base64.b64encode(country_image_path.read_bytes()).decode("ascii")

    rows = []
    for ticker, row in cross_section.sort_values("risk", ascending=False).iterrows():
        rows.append(
            "<tr>"
            f"<td>{html.escape(ticker)}</td>"
            f"<td>{html.escape(str(row['name']))}</td>"
            f"<td>{row['risk'] * 100.0:.1f}%</td>"
            f"<td>{row['excess_return'] * 100.0:.1f}%</td>"
            "</tr>"
        )

    country_rows = []
    for country, row in country_snapshot.iterrows():
        country_rows.append(
            "<tr>"
            f"<td>{html.escape(country)}</td>"
            f"<td>{html.escape(str(row['ticker']))}</td>"
            f"<td><strong>{row['country_risk_score']:.0f}</strong></td>"
            f"<td>{html.escape(str(row['regime']))}</td>"
            f"<td>{row['local_risk_score']:.0f}</td>"
            f"<td>{row['residual_stress']:.0f}</td>"
            f"<td>{row['realized_vol_20d']:.1f}%</td>"
            f"<td>{row['drawdown_3m']:.1f}%</td>"
            f"<td>{row['excess_return_6m']:.1f}%</td>"
            "</tr>"
        )

    document = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>시장 위험 대시보드</title>
<style>
:root {{ --ink:#17202a; --muted:#5d6d7e; --line:#d5d8dc; --paper:#f7f8fa; --accent:#2463a6; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:"Malgun Gothic","Apple SD Gothic Neo",sans-serif; color:var(--ink); background:var(--paper); line-height:1.55; }}
main {{ max-width:1200px; margin:auto; padding:28px 22px 54px; }}
h1 {{ margin:0 0 6px; font-size:clamp(25px,4vw,40px); }}
.muted {{ color:var(--muted); }}
.cards {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:12px; margin:22px 0; }}
.card {{ background:white; border:1px solid var(--line); border-radius:12px; padding:15px; }}
.label {{ color:var(--muted); font-size:13px; }}
.value {{ margin-top:4px; font-size:24px; font-weight:700; }}
.chart {{ width:100%; height:auto; background:white; border:1px solid var(--line); border-radius:12px; }}
.note {{ margin:18px 0; padding:14px 16px; border-left:5px solid #b3261e; background:#fff4f2; }}
table {{ width:100%; border-collapse:collapse; background:white; font-size:14px; }}
th,td {{ padding:9px 10px; border-bottom:1px solid var(--line); text-align:left; }}
th:nth-child(n+3),td:nth-child(n+3) {{ text-align:right; }}
.table-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:12px; }}
code {{ background:#eef1f4; padding:2px 5px; border-radius:4px; }}
.button {{ display:inline-block; margin:10px 0 20px; padding:10px 14px; border-radius:8px; background:var(--accent); color:white; text-decoration:none; }}
@media (max-width:620px) {{
  main {{ padding:20px 12px 40px; }}
  .cards {{ grid-template-columns:1fr 1fr; gap:8px; }}
  .card {{ padding:11px; }}
  .value {{ font-size:19px; }}
  th,td {{ padding:8px; white-space:nowrap; }}
}}
@media (max-width:390px) {{ .cards {{ grid-template-columns:1fr; }} }}
</style>
</head>
<body><main>
<h1>시장 위험 대시보드</h1>
<div class="muted">표시기간 {html.escape(chart_start)}~{latest.name:%Y-%m-%d} · CSFB 스타일 위험선호 + 직접 시장위험 보조지표</div>
<section class="cards" aria-label="최신 위험 요약">
  <div class="card"><div class="label">종합 시장위험</div><div class="value">{score:.0f}/100 · {regime}</div><div class="muted">{interpretation}</div></div>
  <div class="card"><div class="label">위험회피 스트레스</div><div class="value">{latest['stress_score']:.0f}/100 · {appetite_regime}</div><div class="muted">{appetite_interpretation}</div></div>
  <div class="card"><div class="label">20일 실현변동성</div><div class="value">{latest['realized_vol_20d']:.1f}%</div><div class="muted">위험자산 바스켓, 연율</div></div>
  <div class="card"><div class="label">3개월 낙폭</div><div class="value">{latest['drawdown_3m']:.1f}%</div><div class="muted">최근 63거래일 고점 대비</div></div>
  <div class="card"><div class="label">시장 폭</div><div class="value">{latest['breadth_120d']:.0f}%</div><div class="muted">120일선 위 자산 비율</div></div>
</section>
<img class="chart" src="data:image/png;base64,{image_base64}" alt="시장 위험 시계열, 실현변동성, 낙폭, 횡단면 회귀와 자산별 초과수익률 차트">
<div class="note"><strong>해석 한계.</strong> 종합위험 100은 “손실확률 100%”가 아니라 네 지표가 최근 약 5년 분포의 위험 쪽 꼬리에 있다는 뜻입니다. 투자 판단에는 유동성, 신용스프레드, 포지션 규모와 손실한도도 함께 확인해야 합니다.</div>
<p class="muted">2000년대 초 값은 상장되어 있던 장기이력 ETF만 사용하며, 최소 1년의 초기 기준기간으로 계산됩니다. 이후 새 ETF는 데이터가 충분해지는 시점부터 횡단면에 합류합니다.</p>
<h2>국가별 상대위험</h2>
<p>글로벌 RAI 환경 25%와 국가 고유위험 75%를 결합합니다. 국가 고유위험은 RAI 회귀잔차, 20일 변동성, 3개월 낙폭, 6개월 모멘텀으로 구성됩니다.</p>
<img class="chart" src="data:image/png;base64,{country_image_base64}" alt="국가별 현재 상대위험 순위와 2000년 이후 국가 위험 히트맵">
<div class="table-wrap"><table>
<thead><tr><th>국가</th><th>ETF</th><th>종합</th><th>상태</th><th>국가고유</th><th>회귀잔차</th><th>20일 변동성</th><th>3개월 낙폭</th><th>6개월 초과수익</th></tr></thead>
<tbody>{''.join(country_rows)}</tbody>
</table></div>
<a class="button" href="rai_explainer_ko.html">RAI 계산법과 국가점수 설명 보기</a>
<a class="button" href="rai_allocation_timing_ko.html">채권·현금 배분과 타이밍 설명 보기</a>
<h2>최신 횡단면</h2>
<p class="muted">회귀 기울기 {latest_stats.slope:.3f}, t값 {latest_stats.t_value:.2f}, R² {latest_stats.r_squared:.2f}, 자산 {latest_stats.n_assets}개</p>
<div class="table-wrap"><table>
<thead><tr><th>티커</th><th>자산</th><th>연율 변동성</th><th>6개월 현금초과수익률</th></tr></thead>
<tbody>{''.join(rows)}</tbody>
</table></div>
<h2>점수 정의</h2>
<ul>
  <li><strong>위험회피 스트레스:</strong> 20일 평활 위험선호 z값의 최근 5년 백분위를 뒤집은 0~100 상대점수.</li>
  <li><strong>종합 시장위험:</strong> 위험회피 35%, 실현변동성 30%, 낙폭 20%, 시장 폭 악화 15%의 최근 5년 상대백분위 가중평균.</li>
  <li><strong>실현변동성:</strong> 위험자산 동일가중 바스켓의 최근 20거래일 표준편차를 연율화.</li>
  <li><strong>3개월 낙폭:</strong> 동일가중 바스켓의 최근 63거래일 고점 대비 하락률.</li>
  <li><strong>시장 폭:</strong> 현금 대용치를 제외한 자산 중 가격이 120일 이동평균 위에 있는 비율.</li>
</ul>
</main></body></html>"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")


def create_rai_explainer(destination: Path) -> None:
    countries = " · ".join(COUNTRY_TICKERS)
    document = f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RAI와 국가별 시장위험 이해하기</title>
<style>
:root {{ --ink:#17202a; --muted:#5d6d7e; --paper:#f4f6f8; --card:#fff; --line:#d5d8dc; --blue:#2463a6; --green:#2e7d32; --yellow:#d4a017; --orange:#d2691e; --red:#b3261e; }}
* {{ box-sizing:border-box; }}
body {{ margin:0; font-family:"Malgun Gothic","Apple SD Gothic Neo",sans-serif; background:var(--paper); color:var(--ink); line-height:1.65; }}
main {{ max-width:1060px; margin:auto; padding:38px 22px 70px; }}
h1 {{ margin:.15em 0; font-size:clamp(30px,5vw,52px); line-height:1.18; }}
h2 {{ margin-top:2.2em; border-bottom:1px solid var(--line); padding-bottom:.3em; }}
h3 {{ margin-bottom:.3em; }}
p {{ margin:.5em 0 1em; }}
a {{ color:var(--blue); }}
.kicker {{ color:var(--blue); font-weight:700; letter-spacing:.06em; }}
.lead {{ max-width:790px; font-size:18px; color:#34495e; }}
.grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; margin:20px 0; }}
.card {{ background:var(--card); border:1px solid var(--line); border-radius:12px; padding:17px; }}
.card strong {{ display:block; font-size:19px; margin-bottom:6px; }}
.muted {{ color:var(--muted); }}
.formula {{ overflow-x:auto; margin:18px 0; padding:18px; border-left:5px solid var(--blue); background:white; font-family:Cambria,"Times New Roman",serif; font-size:19px; text-align:center; }}
.flow {{ display:grid; grid-template-columns:repeat(4,1fr); gap:10px; align-items:stretch; margin:22px 0; }}
.step {{ position:relative; padding:16px; background:white; border:1px solid var(--line); border-radius:10px; }}
.step b {{ color:var(--blue); }}
.note {{ margin:18px 0; padding:16px 18px; border-left:5px solid var(--orange); background:#fff8e8; }}
.danger {{ border-left-color:var(--red); background:#fff4f2; }}
table {{ width:100%; border-collapse:collapse; background:white; font-size:14px; }}
th,td {{ padding:10px; border-bottom:1px solid var(--line); text-align:left; }}
th:nth-child(n+2),td:nth-child(n+2) {{ text-align:right; }}
.table-wrap {{ overflow-x:auto; border:1px solid var(--line); border-radius:12px; }}
.band {{ display:flex; height:18px; margin:14px 0 8px; overflow:hidden; border-radius:9px; }}
.band span {{ flex:1; }}
.back {{ display:inline-block; margin-top:24px; padding:11px 15px; background:var(--blue); color:white; border-radius:8px; text-decoration:none; }}
svg {{ display:block; width:100%; height:auto; max-width:760px; margin:18px auto; background:white; border:1px solid var(--line); border-radius:12px; }}
@media (max-width:720px) {{ .flow {{ grid-template-columns:1fr 1fr; }} }}
@media (max-width:430px) {{ .flow {{ grid-template-columns:1fr; }} main {{ padding-inline:15px; }} }}
</style>
</head>
<body><main>
<div class="kicker">RISK APPETITE INDEX · PRACTICAL GUIDE</div>
<h1>RAI와 국가별 시장위험 이해하기</h1>
<p class="lead">RAI는 시장이 위험자산에 더 많은 보상을 주는지 측정합니다. 그것은 중요한 환경 신호이지만, 특정 국가의 부도확률이나 다음 하락률을 직접 예측하는 숫자는 아닙니다.</p>

<section class="grid" aria-label="핵심 요약">
  <div class="card"><strong>RAI가 답하는 질문</strong>위험한 자산이 안전한 자산보다 더 높은 초과수익을 얻고 있는가?</div>
  <div class="card"><strong>높은 RAI</strong>변동성이 큰 자산일수록 보상이 커지는 위험선호 환경.</div>
  <div class="card"><strong>낮은 RAI</strong>고위험 자산이 충분한 보상을 받지 못하는 위험회피 환경.</div>
  <div class="card"><strong>RAI가 답하지 못하는 질문</strong>앞으로 며칠 안에 몇 퍼센트 하락하는가?</div>
</section>

<h2>1. CSFB 스타일 RAI 계산</h2>
<div class="flow">
  <div class="step"><b>① 위험 측정</b><br>각 자산의 최근 12개월 연율 변동성</div>
  <div class="step"><b>② 보상 측정</b><br>각 자산의 6개월 현금초과수익률</div>
  <div class="step"><b>③ 횡단면 회귀</b><br>같은 날짜의 모든 자산을 한 번에 비교</div>
  <div class="step"><b>④ 기울기 해석</b><br>양수는 위험선호, 음수는 위험회피</div>
</div>
<div class="formula">초과수익<sub>i,t</sub> = α<sub>t</sub> + RAI<sub>t</sub> × 변동성<sub>i,t</sub> + ε<sub>i,t</sub></div>
<svg viewBox="0 0 760 320" role="img" aria-label="양의 RAI와 음의 RAI 회귀 기울기 비교">
  <rect x="0" y="0" width="760" height="320" fill="#ffffff"/>
  <line x1="70" y1="260" x2="350" y2="260" stroke="#5d6d7e"/><line x1="70" y1="260" x2="70" y2="50" stroke="#5d6d7e"/>
  <line x1="410" y1="260" x2="690" y2="260" stroke="#5d6d7e"/><line x1="410" y1="260" x2="410" y2="50" stroke="#5d6d7e"/>
  <line x1="90" y1="230" x2="330" y2="75" stroke="#2e7d32" stroke-width="4"/>
  <line x1="430" y1="80" x2="670" y2="225" stroke="#b3261e" stroke-width="4"/>
  <g fill="#2463a6"><circle cx="110" cy="220" r="7"/><circle cx="145" cy="188" r="7"/><circle cx="190" cy="178" r="7"/><circle cx="235" cy="128" r="7"/><circle cx="300" cy="92" r="7"/></g>
  <g fill="#2463a6"><circle cx="450" cy="92" r="7"/><circle cx="490" cy="115" r="7"/><circle cx="540" cy="155" r="7"/><circle cx="595" cy="185" r="7"/><circle cx="650" cy="220" r="7"/></g>
  <text x="150" y="35" font-family="sans-serif" font-size="18" fill="#17202a">RAI 양수 · 위험선호</text>
  <text x="490" y="35" font-family="sans-serif" font-size="18" fill="#17202a">RAI 음수 · 위험회피</text>
  <text x="280" y="292" font-family="sans-serif" font-size="14" fill="#5d6d7e">위험 →</text>
  <text x="620" y="292" font-family="sans-serif" font-size="14" fill="#5d6d7e">위험 →</text>
</svg>
<p>원래 CSFB 지수는 선진국·신흥국의 주식 및 채권 64개 지수를 사용했습니다. 이 프로젝트는 공개 ETF를 이용한 근사치이며 2000년에는 장기이력 자산으로 시작하고 신규 ETF는 이력이 쌓인 뒤 합류합니다. <a href="https://www.bankofcanada.ca/wp-content/uploads/2012/01/fsr-0605-illing.pdf">Bank of Canada의 방법론 조사 원문</a></p>

<h2>2. 글로벌 RAI에서 국가위험으로 확장</h2>
<p>국가 하나의 ETF만으로 별도의 RAI 회귀를 만들 수는 없습니다. 대신 글로벌 회귀에서 각 국가가 예상보다 얼마나 부진했는지 나타내는 <strong>회귀잔차</strong>와 그 국가의 직접 위험지표를 결합합니다.</p>
<div class="formula">국가 고유위험 = 회귀잔차 35% + 변동성 25% + 낙폭 25% + 모멘텀 15%</div>
<div class="formula">국가 상대위험 = 글로벌 시장위험 25% + 국가 고유위험 75%</div>
<section class="grid">
  <div class="card"><strong>회귀잔차 스트레스 · 35%</strong>같은 위험도를 가진 다른 자산보다 해당 국가가 얼마나 부진한지 측정.</div>
  <div class="card"><strong>20일 변동성 · 25%</strong>최근 가격 움직임의 크기를 연율화하고 국가 자체 과거와 비교.</div>
  <div class="card"><strong>3개월 낙폭 · 25%</strong>최근 63거래일 고점에서 얼마나 내려왔는지 측정.</div>
  <div class="card"><strong>6개월 모멘텀 · 15%</strong>현금수익률을 차감한 중기 성과가 얼마나 약한지 측정.</div>
</section>
<p class="muted">대상 국가: {html.escape(countries)}</p>
<div class="note">국가 ETF는 미국 달러로 거래되므로 국가 주가와 환율 효과가 함께 반영됩니다. 따라서 이 점수는 현지통화 투자자의 위험과 정확히 같지 않습니다.</div>

<h2>3. 0~100 점수 읽는 법</h2>
<div class="band" aria-hidden="true"><span style="background:var(--green)"></span><span style="background:#7aa35a"></span><span style="background:var(--yellow)"></span><span style="background:var(--orange)"></span><span style="background:var(--red)"></span></div>
<div class="table-wrap"><table>
<thead><tr><th>점수</th><th>상태</th><th>의미</th></tr></thead>
<tbody>
<tr><td>0~19</td><td>낮음</td><td>해당 국가의 과거 대비 위험 신호가 낮음</td></tr>
<tr><td>20~39</td><td>보통</td><td>평시 범위</td></tr>
<tr><td>40~59</td><td>주의</td><td>일부 위험 신호 상승</td></tr>
<tr><td>60~79</td><td>경계</td><td>여러 위험 신호가 함께 상승</td></tr>
<tr><td>80~100</td><td>높음</td><td>최근 약 5년 분포의 위험 쪽 꼬리</td></tr>
</tbody></table></div>
<div class="note danger"><strong>80점은 폭락확률 80%가 아닙니다.</strong> 현재 관측치가 각 지표의 최근 5년 분포에서 위험한 쪽에 얼마나 가까운지를 나타내는 상대백분위입니다.</div>

<h2>4. 올바른 사용법</h2>
<div class="grid">
  <div class="card"><strong>좋은 용도</strong>국가 간 상대비교, 위험 급상승 감시, 포지션 점검 우선순위, 과거 위기와의 비교.</div>
  <div class="card"><strong>함께 볼 자료</strong>국채 CDS·스프레드, 외환보유액, 경상수지, 정책금리, 시장 유동성, 기업이익.</div>
  <div class="card"><strong>나쁜 용도</strong>단일 점수만으로 매수·매도, 국가 부도확률 산정, 정확한 하락 시점 예측.</div>
  <div class="card"><strong>확인할 신호</strong>높은 국가점수가 여러 주 지속되고 글로벌 위험도 동시에 상승하는지 확인.</div>
</div>

<h2>5. 모델의 주요 한계</h2>
<ul>
  <li>ETF 상장 이전 데이터가 없어 초기 국가는 표본 수가 다릅니다.</li>
  <li>현재 거래되는 ETF를 과거로 가져가므로 생존편향이 남습니다.</li>
  <li>배당, ETF 비용, 거래시간, 환율과 현지시장 휴장 차이가 결과에 영향을 줍니다.</li>
  <li>위험선호와 실제 경제·재정 위험은 같은 개념이 아닙니다.</li>
  <li>가중치는 설명 가능성을 위한 공개 근사 규칙이며 공식 CSFB 국가모형이 아닙니다.</li>
</ul>
<a class="back" href="risk_appetite_dashboard.html">대시보드로 돌아가기</a>
<a class="back" href="rai_allocation_timing_ko.html">채권·현금 배분과 타이밍</a>
</main></body></html>"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")


def create_allocation_timing_explainer(destination: Path) -> None:
    document = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>RAI를 이용한 채권·현금 배분과 투자 타이밍</title>
<style>
:root { --ink:#17202a; --muted:#5d6d7e; --paper:#f4f6f8; --card:#fff; --line:#d5d8dc; --blue:#2463a6; --green:#2e7d32; --yellow:#d4a017; --orange:#d2691e; --red:#b3261e; }
* { box-sizing:border-box; }
body { margin:0; font-family:"Malgun Gothic","Apple SD Gothic Neo",sans-serif; background:var(--paper); color:var(--ink); line-height:1.65; }
main { max-width:1080px; margin:auto; padding:38px 22px 70px; }
h1 { margin:.15em 0; font-size:clamp(29px,5vw,50px); line-height:1.2; }
h2 { margin-top:2.2em; padding-bottom:.3em; border-bottom:1px solid var(--line); }
p { margin:.5em 0 1em; }
a { color:var(--blue); }
.kicker { color:var(--blue); font-weight:700; letter-spacing:.06em; }
.lead { max-width:820px; font-size:18px; color:#34495e; }
.grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(220px,1fr)); gap:14px; margin:20px 0; }
.card { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:17px; }
.card strong { display:block; margin-bottom:6px; font-size:18px; }
.flow { display:grid; grid-template-columns:1fr auto 1fr; gap:12px; align-items:center; margin:22px 0; }
.step { height:100%; padding:18px; background:white; border:1px solid var(--line); border-radius:12px; }
.arrow { font-size:28px; color:var(--blue); }
.muted { color:var(--muted); }
.note { margin:18px 0; padding:16px 18px; border-left:5px solid var(--orange); background:#fff8e8; }
.danger { border-left-color:var(--red); background:#fff4f2; }
.good { border-left-color:var(--green); background:#f2f8f2; }
.table-wrap { overflow-x:auto; border:1px solid var(--line); border-radius:12px; }
table { width:100%; border-collapse:collapse; background:white; font-size:14px; }
th,td { padding:10px; border-bottom:1px solid var(--line); text-align:left; }
th:nth-child(n+3),td:nth-child(n+3) { text-align:right; }
.calculator { margin:22px 0; padding:20px; background:white; border:1px solid var(--line); border-radius:14px; }
.controls { display:grid; grid-template-columns:1fr 1fr; gap:18px; align-items:end; }
label { display:block; font-weight:700; margin-bottom:6px; }
input[type="range"] { width:100%; accent-color:var(--blue); }
select { width:100%; min-height:42px; padding:8px; border:1px solid #aeb6bf; border-radius:7px; background:white; color:var(--ink); }
.score-line { display:flex; justify-content:space-between; gap:12px; }
.allocation { display:grid; grid-template-columns:repeat(3,1fr); gap:12px; margin-top:18px; }
.allocation .value { font-size:28px; font-weight:700; }
.bar { height:14px; overflow:hidden; display:flex; margin-top:15px; border-radius:7px; background:#e8ebee; }
.bar span { display:block; height:100%; transition:width .2s ease; }
#stockBar { background:var(--blue); } #bondBar { background:var(--green); } #cashBar { background:var(--yellow); }
.legend { display:flex; flex-wrap:wrap; gap:14px; margin-top:8px; color:var(--muted); font-size:13px; }
.dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:5px; }
.timeline { display:grid; grid-template-columns:repeat(5,1fr); gap:8px; margin:20px 0; }
.phase { padding:14px; background:white; border-top:5px solid var(--blue); border-radius:8px; }
.phase:nth-child(2) { border-color:var(--yellow); }.phase:nth-child(3) { border-color:var(--orange); }.phase:nth-child(4) { border-color:var(--red); }.phase:nth-child(5) { border-color:var(--green); }
.back { display:inline-block; margin:24px 8px 0 0; padding:11px 15px; background:var(--blue); color:white; border-radius:8px; text-decoration:none; }
@media (max-width:760px) { .flow { grid-template-columns:1fr; } .arrow { transform:rotate(90deg); text-align:center; } .controls { grid-template-columns:1fr; } .timeline { grid-template-columns:1fr 1fr; } }
@media (max-width:460px) { main { padding-inline:14px; } .allocation { grid-template-columns:1fr; } .timeline { grid-template-columns:1fr; } }
</style>
</head>
<body><main>
<div class="kicker">RAI · ASSET ALLOCATION · TIMING</div>
<h1>RAI를 이용한 채권·현금 배분과 투자 타이밍</h1>
<p class="lead">RAI는 매수·매도 날짜를 직접 예측하는 지표가 아니라, 현재 포트폴리오가 감당할 위험의 크기를 조절하는 국면 신호입니다.</p>

<div class="flow">
  <div class="step"><strong>1단계 · RAI와 종합위험</strong><p>주식·위험자산과 방어자산 사이의 큰 비중을 결정합니다.</p></div>
  <div class="arrow" aria-hidden="true">→</div>
  <div class="step"><strong>2단계 · 물가와 금리</strong><p>방어자산 안에서 채권과 현금·단기채 비중을 결정합니다.</p></div>
</div>
<div class="note"><strong>핵심:</strong> RAI가 높다고 항상 장기채를 늘리는 것은 아닙니다. 경기침체형 위험회피에는 채권이 유리할 수 있지만, 인플레이션형 위험회피에는 현금·단기채가 더 적합할 수 있습니다.</div>

<h2>1. 종합위험에 따른 방어자산 범위</h2>
<div class="table-wrap"><table>
<thead><tr><th>종합위험</th><th>국면</th><th>방어자산 예시</th><th>의미</th></tr></thead>
<tbody>
<tr><td>0~19</td><td>위험선호 강함</td><td>15~25%</td><td>위험예산 확대 가능</td></tr>
<tr><td>20~39</td><td>위험선호</td><td>20~30%</td><td>정상 위험선호 국면</td></tr>
<tr><td>40~59</td><td>주의</td><td>30~45%</td><td>방어자산 점진 확대</td></tr>
<tr><td>60~79</td><td>위험회피</td><td>45~60%</td><td>위험노출 축소 검토</td></tr>
<tr><td>80~100</td><td>강한 스트레스</td><td>60~75%</td><td>자본보전 우선</td></tr>
</tbody></table></div>
<p class="muted">이 범위는 설명과 백테스트를 위한 예시이며 투자자의 목표수익률·기간·손실한도에 따라 달라집니다.</p>

<h2>2. 예시 배분 계산기</h2>
<div class="calculator">
  <div class="controls">
    <div>
      <div class="score-line"><label for="riskScore">종합 시장위험</label><output id="riskValue" for="riskScore">50</output></div>
      <input id="riskScore" type="range" min="0" max="100" step="1" value="50">
    </div>
    <div>
      <label for="rateRegime">물가·금리 국면</label>
      <select id="rateRegime">
        <option value="mixed">혼합·불명확</option>
        <option value="recession">경기둔화·물가하락·금리하락</option>
        <option value="inflation">인플레이션·금리상승</option>
      </select>
    </div>
  </div>
  <div class="allocation" role="status" aria-live="polite">
    <div class="card"><span class="muted">주식·위험자산</span><div class="value" id="stockValue">63%</div></div>
    <div class="card"><span class="muted">채권</span><div class="value" id="bondValue">19%</div></div>
    <div class="card"><span class="muted">현금·단기채</span><div class="value" id="cashValue">18%</div></div>
  </div>
  <div class="bar" aria-hidden="true"><span id="stockBar"></span><span id="bondBar"></span><span id="cashBar"></span></div>
  <div class="legend"><span><i class="dot" style="background:var(--blue)"></i>위험자산</span><span><i class="dot" style="background:var(--green)"></i>채권</span><span><i class="dot" style="background:var(--yellow)"></i>현금·단기채</span></div>
  <p id="calcNote" class="muted"></p>
</div>
<div class="note danger"><strong>계산기 주의:</strong> 위험구간별 방어자산 범위의 중간값을 사용한 교육용 예시입니다. 개인화된 투자 권고나 최적 포트폴리오가 아니며 실제 사용 전 거래비용을 포함한 백테스트가 필요합니다.</div>

<h2>3. 채권과 현금의 비율 결정</h2>
<div class="grid">
  <div class="card"><strong>경기침체형 위험회피</strong><p>RAI 하락, 물가 둔화, 정책금리 인하 기대, 장기금리 하락.</p><p><b>방어자산 내 채권 60~80% / 현금 20~40%</b></p></div>
  <div class="card"><strong>인플레이션형 위험회피</strong><p>RAI 하락, 물가 상승, 정책금리 인상, 장기금리 상승.</p><p><b>방어자산 내 채권 20~40% / 현금 60~80%</b></p></div>
  <div class="card"><strong>혼합·불명확</strong><p>물가와 성장 신호가 충돌하거나 금리 방향이 불분명.</p><p><b>방어자산 내 채권 50% / 현금 50%</b></p></div>
</div>

<h2>4. 투자 타이밍 해석</h2>
<div class="timeline">
  <div class="phase"><strong>위험선호</strong><br>RAI 양수, 스트레스 낮음</div>
  <div class="phase"><strong>약화</strong><br>RAI 하락, 시장 폭 둔화</div>
  <div class="phase"><strong>방어전환</strong><br>스트레스 60·80 상향 돌파</div>
  <div class="phase"><strong>투매</strong><br>변동성·낙폭 동반 상승</div>
  <div class="phase"><strong>재진입 관찰</strong><br>스트레스 고점 후 하락</div>
</div>

<h3>방어자산 확대 확인 신호</h3>
<ul>
  <li>RAI z점수가 0 아래로 내려가고 5~20거래일 지속</li>
  <li>위험회피 스트레스와 종합위험이 함께 상승</li>
  <li>변동성 상승, 시장 폭 악화, 신용스프레드 확대</li>
  <li>고위험주와 하이일드채권이 안전자산보다 부진</li>
</ul>

<h3>위험자산 재진입 확인 신호</h3>
<ul>
  <li>스트레스가 80~100에서 고점을 만들고 하락</li>
  <li>RAI z점수 반등과 실현변동성 둔화</li>
  <li>시장 폭 개선, 신용스프레드 축소</li>
  <li>한 번에 진입하지 않고 3~5회 분할</li>
</ul>
<div class="good note"><strong>중요:</strong> 스트레스가 90이라는 이유만으로 바로 매수하지 않습니다. 90→85→72처럼 위험회피가 완화되는 방향 전환을 확인하는 것이 핵심입니다.</div>

<h2>5. 버블 감시와 타이밍의 차이</h2>
<div class="table-wrap"><table>
<thead><tr><th>RAI 상태</th><th>주된 활용</th><th>확인할 보조지표</th></tr></thead>
<tbody>
<tr><td>높은 위험선호 장기 지속</td><td>과열·버블 가능성 감시</td><td>밸류에이션, 신용, 레버리지</td></tr>
<tr><td>높은 수준에서 급락</td><td>위험노출 축소 검토</td><td>시장 폭, 변동성, 스프레드</td></tr>
<tr><td>극단적인 위험회피</td><td>자본보전·바닥 형성 관찰</td><td>낙폭, 유동성, 투매 여부</td></tr>
<tr><td>위험회피 고점 후 반등</td><td>분할 재진입 검토</td><td>변동성 둔화, 가격 추세 회복</td></tr>
</tbody></table></div>

<h2>6. 사용 원칙</h2>
<ul>
  <li>RAI 하나만 사용하지 않고 종합위험·금리·물가·신용을 함께 확인합니다.</li>
  <li>절대 수준보다 방향 전환과 지속성을 더 중요하게 봅니다.</li>
  <li>신호가 바뀔 때 전량 매매하지 않고 목표비중을 단계적으로 조절합니다.</li>
  <li>리밸런싱 간격과 최소 변경폭을 정해 과도한 거래를 방지합니다.</li>
  <li>과거 성과에는 데이터 변경, 생존편향, 거래비용이 포함될 수 있음을 확인합니다.</li>
</ul>
<h2>7. 참고자료</h2>
<ul>
  <li><a href="https://www.bankofcanada.ca/wp-content/uploads/2012/01/fsr-0605-illing.pdf">Bank of Canada — A Brief Survey of Risk-Appetite Indexes</a></li>
  <li><a href="https://www.bankofengland.co.uk/working-paper/2005/measuring-investors-risk-appetite">Bank of England — Measuring investors' risk appetite</a></li>
  <li><a href="https://www.bis.org/publ/arpdf/ar2014e2.htm">BIS — Global financial markets under the spell of monetary policy</a></li>
</ul>
<a class="back" href="risk_appetite_dashboard.html">대시보드로 돌아가기</a>
<a class="back" href="rai_explainer_ko.html">RAI 산출 원리 보기</a>
</main>
<script>
(function () {
  const riskInput = document.getElementById('riskScore');
  const regimeInput = document.getElementById('rateRegime');
  const riskOutput = document.getElementById('riskValue');
  const stockValue = document.getElementById('stockValue');
  const bondValue = document.getElementById('bondValue');
  const cashValue = document.getElementById('cashValue');
  const stockBar = document.getElementById('stockBar');
  const bondBar = document.getElementById('bondBar');
  const cashBar = document.getElementById('cashBar');
  const calcNote = document.getElementById('calcNote');

  function defensiveWeight(score) {
    if (score < 20) return 20;
    if (score < 40) return 25;
    if (score < 60) return 37.5;
    if (score < 80) return 52.5;
    return 67.5;
  }

  function update() {
    const score = Number(riskInput.value);
    const regime = regimeInput.value;
    const defensive = defensiveWeight(score);
    const bondShare = regime === 'recession' ? 0.70 : regime === 'inflation' ? 0.30 : 0.50;
    const stock = 100 - defensive;
    const bond = defensive * bondShare;
    const cash = defensive - bond;
    const label = regime === 'recession' ? '경기침체형: 채권 비중을 높인 예시' : regime === 'inflation' ? '인플레이션형: 현금·단기채 비중을 높인 예시' : '혼합형: 채권과 현금을 균형 배분한 예시';

    riskOutput.value = score;
    riskOutput.textContent = score + '/100';
    stockValue.textContent = Math.round(stock) + '%';
    bondValue.textContent = Math.round(bond) + '%';
    cashValue.textContent = Math.round(cash) + '%';
    stockBar.style.width = stock + '%';
    bondBar.style.width = bond + '%';
    cashBar.style.width = cash + '%';
    calcNote.textContent = label + ' · 반올림 전 합계 100%';
  }

  riskInput.addEventListener('input', update);
  regimeInput.addEventListener('change', update);
  update();
}());
</script>
</body></html>"""
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(document, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CSFB 스타일 위험선호와 직접 시장위험을 시각화합니다."
    )
    parser.add_argument(
        "--start",
        default="1996-01-01",
        help="지표 워밍업용 가격 시작일 (기본 1996-01-01)",
    )
    parser.add_argument(
        "--chart-start",
        default="2000-01-01",
        help="대시보드 표시 시작일 (기본 2000-01-01)",
    )
    parser.add_argument("--end", default=None, help="가격 종료일; yfinance에서는 해당 날짜 미포함")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("output/risk_appetite"),
        help="결과 저장 폴더",
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="네트워크를 쓰지 않고 output-dir의 market_prices.parquet 사용",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    cache_path = output_dir / "market_prices.parquet"

    prices = load_prices(args.start, args.end, cache_path, args.offline)
    result, cross_section, latest_stats, residuals = calculate_indicators(prices)
    country_history, country_snapshot = calculate_country_indicators(
        prices, result, residuals
    )
    result_path = output_dir / "risk_appetite_timeseries.parquet"
    snapshot_path = output_dir / "risk_appetite_snapshot.csv"
    figure_path = output_dir / "risk_appetite_dashboard.png"
    country_history_path = output_dir / "country_risk_timeseries.parquet"
    country_snapshot_path = output_dir / "country_risk_snapshot.csv"
    country_figure_path = output_dir / "country_risk_dashboard.png"
    html_path = output_dir / "risk_appetite_dashboard.html"
    explainer_path = output_dir / "rai_explainer_ko.html"
    allocation_explainer_path = output_dir / "rai_allocation_timing_ko.html"

    result.to_parquet(result_path)
    cross_section.to_csv(snapshot_path, encoding="utf-8-sig")
    country_history.to_parquet(country_history_path)
    country_snapshot.to_csv(country_snapshot_path, encoding="utf-8-sig")
    create_dashboard_figure(
        result, cross_section, latest_stats, figure_path, args.chart_start
    )
    create_country_dashboard_figure(
        country_history, country_snapshot, country_figure_path, args.chart_start
    )
    create_html_report(
        result,
        cross_section,
        latest_stats,
        figure_path,
        country_snapshot,
        country_figure_path,
        html_path,
        args.chart_start,
    )
    create_rai_explainer(explainer_path)
    create_allocation_timing_explainer(allocation_explainer_path)

    latest = result.loc[result["market_risk_score"].dropna().index[-1]]
    regime, interpretation = market_risk_regime(float(latest["market_risk_score"]))
    print(f"기준일: {latest.name:%Y-%m-%d}")
    print(
        f"종합 시장위험: {latest['market_risk_score']:.0f}/100 · "
        f"{regime} ({interpretation})"
    )
    print(f"위험회피 스트레스: {latest['stress_score']:.0f}/100")
    print(f"20일 실현변동성: {latest['realized_vol_20d']:.1f}%")
    print(f"3개월 낙폭: {latest['drawdown_3m']:.1f}%")
    print("국가위험 상위 5개:")
    for country, row in country_snapshot.head(5).iterrows():
        print(f"  {country}: {row['country_risk_score']:.0f}/100 · {row['regime']}")
    print(f"HTML: {html_path}")
    print(f"RAI 설명: {explainer_path}")
    print(f"배분·타이밍 설명: {allocation_explainer_path}")


if __name__ == "__main__":
    main()
