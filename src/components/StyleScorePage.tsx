import { ChangeEvent, FormEvent, useEffect, useMemo, useState } from "react";
import { AlertTriangle, CalendarDays, Database, Search, Star } from "lucide-react";
import {
  fetchStyleScoreComponentFactors,
  fetchStyleScoreComponents,
  fetchStyleScores,
} from "../api/styleScoresApi";
import type {
  StyleScoreDataSource,
  StyleScoreFactor,
  StyleScoreGroup,
  StyleScoreStock,
  StyleProfile,
} from "../types/styleScores";

const numberFormatter = new Intl.NumberFormat("ko-KR", {
  maximumFractionDigits: 3,
  minimumFractionDigits: 0,
});

const percentFormatter = new Intl.NumberFormat("ko-KR", {
  maximumFractionDigits: 1,
  minimumFractionDigits: 0,
});

const styleProfileOptions: { value: StyleProfile; label: string; description: string }[] = [
  {
    value: "DEFAULT",
    label: "Default",
    description: "밸류, 퀄리티, 성장, 모멘텀을 균형 있게 반영",
  },
  {
    value: "MINERVINI_ZWEIG",
    label: "Minervini/Zweig",
    description: "성장과 모멘텀 비중을 높인 공격형 스타일",
  },
  {
    value: "DIVIDEND_QUALITY",
    label: "Dividend Quality",
    description: "배당, 퀄리티, 밸류 비중을 높인 안정형 스타일",
  },
];

function formatScore(value: number | null | undefined) {
  return value === null || value === undefined || !Number.isFinite(value) ? "-" : value.toFixed(1);
}

function formatNumber(value: number | null | undefined) {
  return value === null || value === undefined || !Number.isFinite(value) ? "-" : numberFormatter.format(value);
}

function formatWeight(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "-";
  }

  return `${percentFormatter.format(value * 100)}%`;
}

function getScoreTone(value: number | null | undefined) {
  if (value === null || value === undefined || !Number.isFinite(value)) {
    return "neutral";
  }

  if (value >= 70) {
    return "strong";
  }

  if (value >= 40) {
    return "watch";
  }

  return "weak";
}

function sourceLabel(source: StyleScoreDataSource) {
  return source === "api" ? "API 연동" : "더미 데이터";
}

function StyleScoreCard({
  group,
  isActive,
  onClick,
}: {
  group: StyleScoreGroup;
  isActive: boolean;
  onClick: () => void;
}) {
  const score = group.score ?? 0;
  const tone = getScoreTone(group.score);

  return (
    <button
      className={`style-score-card ${tone} ${isActive ? "active" : ""}`}
      type="button"
      onClick={onClick}
    >
      <span>{group.label}</span>
      <strong>{formatScore(group.score)}</strong>
      <em>
        신뢰도 {formatScore(group.scoreConfidence === null ? null : (group.scoreConfidence ?? 0) * 100)} · 팩터{" "}
        {group.availableFactorCount ?? "-"} / {group.requiredFactorCount ?? "-"}
      </em>
      <div className="style-score-meter" aria-hidden="true">
        <i style={{ width: `${Math.max(0, Math.min(100, score))}%` }} />
      </div>
    </button>
  );
}

function DetailTable({
  group,
  factors,
  isLoading,
}: {
  group: StyleScoreGroup | null;
  factors: StyleScoreFactor[];
  isLoading: boolean;
}) {
  return (
    <section className="style-detail-panel">
      <div className="style-detail-title">
        <h2>{group ? `${group.label} 상세 분석` : "상세 분석"}</h2>
        <p>버튼 클릭 시 새 상세 API에서 내려주는 팩터 breakdown을 표시합니다.</p>
      </div>

      <div className="style-detail-table-wrap">
        <table className="style-detail-table style-factor-detail-table">
          <thead>
            <tr>
              <th>팩터명</th>
              <th>원값</th>
              <th>Winsorized</th>
              <th>Percentile</th>
              <th>Robust Z</th>
              <th>가중치</th>
              <th>Weighted Score</th>
              <th>Peer 수</th>
              <th>Fallback</th>
            </tr>
          </thead>
          <tbody>
            {isLoading ? (
              <tr>
                <td className="style-empty-row" colSpan={9}>
                  상세 팩터를 불러오는 중입니다.
                </td>
              </tr>
            ) : factors.length === 0 ? (
              <tr>
                <td className="style-empty-row" colSpan={9}>
                  표시할 구성 팩터가 없습니다.
                </td>
              </tr>
            ) : (
              factors.map((factor) => (
                <tr key={`${group?.componentKey ?? "component"}-${factor.factorId}`}>
                  <td>
                    <strong>{factor.label}</strong>
                    <span>{factor.factorId}</span>
                  </td>
                  <td>{formatNumber(factor.rawValue)}</td>
                  <td>{formatNumber(factor.winsorizedValue)}</td>
                  <td className={getScoreTone(factor.percentileScore)}>{formatScore(factor.percentileScore)}</td>
                  <td>{formatNumber(factor.robustZScore)}</td>
                  <td>{formatWeight(factor.weight)}</td>
                  <td className={getScoreTone(factor.weightedScore)}>{formatNumber(factor.weightedScore)}</td>
                  <td>{formatNumber(factor.peerCount)}</td>
                  <td>
                    <strong>{factor.fallbackLevel ?? "-"}</strong>
                    <span>{factor.fallbackCode ?? "-"}</span>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>
    </section>
  );
}

export function StyleScorePage() {
  const [rows, setRows] = useState<StyleScoreStock[]>([]);
  const [selectedStock, setSelectedStock] = useState<StyleScoreStock | null>(null);
  const [selectedGroupId, setSelectedGroupId] = useState("composite");
  const [selectedFactors, setSelectedFactors] = useState<StyleScoreFactor[]>([]);
  const [selectedStyleProfile, setSelectedStyleProfile] = useState<StyleProfile>("DEFAULT");
  const [inputValue, setInputValue] = useState("");
  const [source, setSource] = useState<StyleScoreDataSource>("api");
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isFactorLoading, setIsFactorLoading] = useState(false);

  const selectedGroup =
    selectedStock?.groups.find((group) => group.id === selectedGroupId) ?? selectedStock?.groups[0] ?? null;
  const selectedStyleProfileOption =
    styleProfileOptions.find((option) => option.value === selectedStyleProfile) ?? styleProfileOptions[0];

  const visibleRows = useMemo(() => {
    const selectedExists = selectedStock
      ? rows.some((row) => row.securityId === selectedStock.securityId)
      : true;

    return selectedStock && !selectedExists ? [selectedStock, ...rows] : rows;
  }, [rows, selectedStock]);

  const loadFactors = async (
    securityId: string,
    group: StyleScoreGroup,
    styleProfile: StyleProfile = selectedStyleProfile,
  ) => {
    setIsFactorLoading(true);
    const result = await fetchStyleScoreComponentFactors(securityId, group.componentKey, styleProfile);

    setSelectedFactors(result.data);
    setSource((current) => (result.source === "mock" ? "mock" : current));
    setStatusMessage(result.message);
    setIsFactorLoading(false);
  };

  const loadComponents = async (securityId: string, styleProfile: StyleProfile = selectedStyleProfile) => {
    setIsLoading(true);
    setIsFactorLoading(true);

    const result = await fetchStyleScoreComponents(securityId, styleProfile);
    const firstGroup = result.data.groups[0] ?? null;

    setSelectedStock(result.data);
    setInputValue(result.data.securityId);
    setSelectedGroupId(firstGroup?.id ?? "composite");
    setSource(result.source);
    setStatusMessage(result.message);
    setIsLoading(false);

    if (firstGroup) {
      const factorResult = await fetchStyleScoreComponentFactors(
        result.data.securityId,
        firstGroup.componentKey,
        styleProfile,
      );
      setSelectedFactors(factorResult.data);
      setSource((current) => (factorResult.source === "mock" ? "mock" : current));
      setStatusMessage(factorResult.message ?? result.message);
    } else {
      setSelectedFactors([]);
    }

    setIsFactorLoading(false);
  };

  const loadScores = async (styleProfile: StyleProfile, preferredSecurityId?: string) => {
    setIsLoading(true);
    setIsFactorLoading(true);
    const listResult = await fetchStyleScores(styleProfile);
    const nextSecurityId = preferredSecurityId || listResult.data[0]?.securityId;

    setRows(listResult.data);
    setSource(listResult.source);
    setStatusMessage(listResult.message);

    if (!nextSecurityId) {
      setSelectedStock(null);
      setSelectedFactors([]);
      setIsLoading(false);
      setIsFactorLoading(false);
      return;
    }

    await loadComponents(nextSecurityId, styleProfile);
  };

  useEffect(() => {
    let ignore = false;

    const loadInitial = async () => {
      setIsLoading(true);
      const listResult = await fetchStyleScores(selectedStyleProfile);

      if (ignore) {
        return;
      }

      const firstStock = listResult.data[0];
      setRows(listResult.data);
      setSource(listResult.source);
      setStatusMessage(listResult.message);

      if (!firstStock) {
        setSelectedStock(null);
        setSelectedFactors([]);
        setIsLoading(false);
        return;
      }

      await loadComponents(firstStock.securityId, selectedStyleProfile);
    };

    void loadInitial();

    return () => {
      ignore = true;
    };
  }, []);

  const handleSubmit = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    const securityId = inputValue.trim();

    if (securityId.length > 0) {
      void loadComponents(securityId, selectedStyleProfile);
    }
  };

  const handleStyleProfileChange = (event: ChangeEvent<HTMLSelectElement>) => {
    const nextStyleProfile = event.target.value as StyleProfile;

    setSelectedStyleProfile(nextStyleProfile);
    void loadScores(nextStyleProfile, selectedStock?.securityId);
  };

  const handleGroupClick = (group: StyleScoreGroup) => {
    setSelectedGroupId(group.id);

    if (selectedStock) {
      void loadFactors(selectedStock.securityId, group, selectedStyleProfile);
    }
  };

  return (
    <section className="style-score-page">
      <aside className="stock-analysis-aside">
        <div className="stock-panel-header">
          <div>
            <h1>스타일 스코어</h1>
            <p>종목별 팩터 점수</p>
          </div>
          <Star size={17} />
        </div>

        <form className="stock-search-box" onSubmit={handleSubmit}>
          <Search size={15} />
          <input
            aria-label="style_score_search"
            placeholder="security_id 검색"
            type="search"
            value={inputValue}
            onChange={(event) => setInputValue(event.target.value)}
          />
        </form>

        <form className="manual-stock-form" onSubmit={handleSubmit}>
          <span>직접 입력</span>
          <div>
            <input
              aria-label="security_id"
              value={inputValue}
              onChange={(event) => setInputValue(event.target.value)}
              placeholder="security_id"
            />
            <button type="submit">조회</button>
          </div>
        </form>

        <label className="style-profile-control">
          <span>스타일 프로필</span>
          <select
            aria-label="style_profile"
            value={selectedStyleProfile}
            onChange={handleStyleProfileChange}
          >
            {styleProfileOptions.map((option) => (
              <option key={option.value} value={option.value}>
                {option.label}
              </option>
            ))}
          </select>
          <em>{selectedStyleProfileOption.description}</em>
        </label>

        <div className="history-list">
          <div className="history-title">
            <span>종목 목록</span>
            <button
              type="button"
              onClick={() => {
                const securityId = inputValue || visibleRows[0]?.securityId;

                if (securityId) {
                  void loadScores(selectedStyleProfile, securityId);
                }
              }}
            >
              새로고침
            </button>
          </div>
          {visibleRows.map((row) => (
            <button
              className={row.securityId === selectedStock?.securityId ? "selected" : ""}
              key={row.securityId}
              type="button"
              onClick={() => void loadComponents(row.securityId, selectedStyleProfile)}
            >
              <strong>{row.name}</strong>
              <span>
                {row.ticker} · {row.country}
              </span>
            </button>
          ))}
        </div>
      </aside>

      <main className="style-score-main">
        <header className="style-score-header">
          <div>
            <p className="style-score-eyebrow">Cross-sectional style ranking</p>
            <h1>{selectedStock?.name ?? "스타일 스코어 분석"}</h1>
          </div>
          <div className="style-score-status">
            <span>
              <Star size={14} />
              {selectedStyleProfileOption.label}
            </span>
            <span>
              <CalendarDays size={14} />
              {selectedStock?.asOfDate ?? "-"}
            </span>
            <em className={source}>
              <Database size={14} />
              {sourceLabel(source)}
            </em>
          </div>
        </header>

        <div className="style-score-scroll">
          {statusMessage && (
            <p className="style-score-note">
              <AlertTriangle size={14} />
              {statusMessage}
            </p>
          )}

          <section className="style-score-summary">
            <article>
              <span>선택 종목</span>
              <strong>{selectedStock?.ticker ?? "-"}</strong>
              <em>{selectedStock?.securityId ?? "-"}</em>
            </article>
            <article>
              <span>Composite Score</span>
              <strong className={getScoreTone(selectedStock?.compositeScore)}>
                {formatScore(selectedStock?.compositeScore)}
              </strong>
              <em>{selectedStyleProfile}</em>
            </article>
            <article>
              <span>선택 컴포넌트</span>
              <strong>{selectedGroup?.label ?? "-"}</strong>
              <em>{selectedGroup?.componentKey ?? "-"}</em>
            </article>
            <article>
              <span>상태</span>
              <strong>{isLoading ? "로딩 중" : "표시 완료"}</strong>
              <em>{sourceLabel(source)}</em>
            </article>
          </section>

          <section className="style-score-grid" aria-label="스타일 스코어 컴포넌트">
            {(selectedStock?.groups ?? []).map((group) => (
              <StyleScoreCard
                group={group}
                isActive={group.id === selectedGroup?.id}
                key={group.id}
                onClick={() => handleGroupClick(group)}
              />
            ))}
          </section>

          <DetailTable group={selectedGroup} factors={selectedFactors} isLoading={isFactorLoading} />
        </div>
      </main>
    </section>
  );
}
