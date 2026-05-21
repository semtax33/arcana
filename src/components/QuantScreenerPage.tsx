import { useEffect } from "react";
import { observer } from "mobx-react-lite";
import {
  ArrowLeft,
  ArrowRight,
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Check,
  ChevronDown,
  ChevronRight,
  Search,
  X,
} from "lucide-react";
import type { QuantScreenerStore } from "../stores/quantScreenerStore";
import type {
  ComparisonOperator,
  FilterGroup,
  QuantScreenerColumn,
  ScreenedStock,
} from "../types/quantScreener";

type QuantScreenerPageProps = {
  store: QuantScreenerStore;
};

const operators: ComparisonOperator[] = [">", "<", ">=", "<=", "="];
const numberFormatter = new Intl.NumberFormat("ko-KR", {
  maximumFractionDigits: 2,
});

function formatValue(value: number | null | undefined, unit?: string | null) {
  if (value === null || value === undefined) {
    return "-";
  }

  if (unit === "percent") {
    return `${numberFormatter.format(value)}%`;
  }

  return unit ? `${numberFormatter.format(value)} ${unit}` : numberFormatter.format(value);
}

function renderFixedCell(row: ScreenedStock, column: QuantScreenerColumn) {
  switch (column.columnType) {
    case "rank":
      return row.rank;
    case "ticker":
      return row.ticker;
    case "name":
      return row.name;
    case "country":
      return <span className="country-pill">{row.market}</span>;
    case "market_cap":
      return formatValue(row.marketCap, "mil");
    case "percentile":
      return formatValue(row.percentile, "percent");
    default:
      return "-";
  }
}

function SortIcon({
  active,
  direction,
}: {
  active: boolean;
  direction: "asc" | "desc";
}) {
  if (!active) {
    return <ArrowUpDown size={13} />;
  }

  return direction === "asc" ? <ArrowUp size={13} /> : <ArrowDown size={13} />;
}

export const QuantScreenerPage = observer(({ store }: QuantScreenerPageProps) => {
  useEffect(() => {
    void store.loadCatalog();
  }, [store]);

  const renderFilterGroup = (group: FilterGroup, depth = 0) => {
    const expanded = store.isGroupExpanded(group.id);
    const hasChildren = Boolean(group.children?.length);
    const hasFilters = Boolean(group.filters?.length);

    return (
      <div className={`filter-tree-node depth-${depth}`} key={group.id}>
        <button
          className="filter-tree-title"
          type="button"
          onClick={() => store.toggleGroup(group.id)}
        >
          {expanded ? <ChevronDown size={15} /> : <ChevronRight size={15} />}
          <span>{group.name}</span>
          <em>{group.count}개</em>
        </button>

        {expanded && (
          <div className="filter-tree-children">
            {hasChildren && group.children?.map((child) => renderFilterGroup(child, depth + 1))}
            {hasFilters &&
              group.filters?.map((filter) => {
                const selected = store.selectedConditions.some(
                  (condition) => condition.filterId === filter.id,
                );

                return (
                  <button
                    className={`factor-row ${selected ? "selected" : ""}`}
                    key={filter.id}
                    type="button"
                    onClick={() =>
                      selected ? store.removeCondition(filter.id) : store.addCondition(filter)
                    }
                  >
                    <span className="checkbox-mark">{selected && <Check size={12} />}</span>
                    <span>{filter.label}</span>
                  </button>
                );
              })}
          </div>
        )}
      </div>
    );
  };

  if (store.viewMode === "loading") {
    return (
      <section className="quant-page loading-screen">
        <div className="screening-loader" aria-label="스크리닝 진행률">
          <span>5%</span>
        </div>
        <p>스크리닝 중...</p>
      </section>
    );
  }

  if (store.viewMode === "result" && store.result) {
    const fixedColumns = [...(store.result.fixedColumns ?? [])].sort((a, b) => a.order - b.order);
    const factorColumns = [...(store.result.factorColumns ?? [])].sort((a, b) => a.order - b.order);
    const displayedFixedColumns: QuantScreenerColumn[] =
      fixedColumns.length > 0
        ? fixedColumns
        : [
            { key: "rank", label: "#", columnType: "rank", order: 1 },
            { key: "ticker", label: "티커", columnType: "ticker", order: 2 },
            { key: "stock_name", label: "종목명", columnType: "name", order: 3 },
            { key: "country", label: "국가", columnType: "country", order: 4 },
            { key: "market_cap", label: "시가총액", columnType: "market_cap", order: 5 },
          ];

    return (
      <section className="quant-page result-view">
        <header className="quant-header">
          <div>
            <h1>퀀트 스크리너</h1>
            <p>스크리닝 확인</p>
          </div>
          <div className="header-actions">
            <button className="ghost-button" type="button">
              전략 불러오기
            </button>
            <button className="icon-chip" type="button" aria-label="닫기">
              <X size={16} />
            </button>
          </div>
        </header>

        <section className="result-panel result-only">
          <div className="result-summary">
            <div>
              <p>스크리닝 결과</p>
              <strong>{store.result.summary?.screeningResult ?? "OK"}</strong>
            </div>
            <div>
              <strong>{store.result.total}</strong>
              <span>종목</span>
            </div>
          </div>

          <div className="table-header">
            <button className="back-button" type="button" onClick={() => store.backToBuilder()}>
              <ArrowLeft size={15} />
              조건 설정
            </button>
            <button className="purple-button" type="button">
              백테스팅
              <ArrowRight size={15} />
            </button>
          </div>

          <div className="stock-table-wrap">
            <table className="stock-table">
              <thead>
                <tr>
                  {displayedFixedColumns.map((column) => (
                    <th key={column.key}>
                      <button
                        className={`sort-header ${store.sortKey === column.key ? "active" : ""}`}
                        type="button"
                        onClick={() => store.toggleSort(column)}
                        title={`${column.label} 정렬`}
                      >
                        <span>{column.label}</span>
                        <SortIcon
                          active={store.sortKey === column.key}
                          direction={store.sortDirection}
                        />
                      </button>
                    </th>
                  ))}
                  {factorColumns.map((column) => (
                    <th key={column.key}>
                      <button
                        className={`sort-header ${store.sortKey === column.key ? "active" : ""}`}
                        type="button"
                        onClick={() => store.toggleSort(column)}
                        title={`${column.label} 정렬`}
                      >
                        <span>{column.label}</span>
                        <SortIcon
                          active={store.sortKey === column.key}
                          direction={store.sortDirection}
                        />
                      </button>
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {store.sortedRows.map((row) => (
                  <tr key={row.securityId ?? row.ticker}>
                    {displayedFixedColumns.map((column) => (
                      <td
                        className={
                          column.columnType === "ticker"
                            ? "ticker"
                            : column.columnType === "percentile"
                              ? "blue-value"
                              : undefined
                        }
                        key={column.key}
                      >
                        {renderFixedCell(row, column)}
                      </td>
                    ))}
                    {factorColumns.map((column) => {
                      const factorValue = row.factorValues[column.key];

                      return (
                        <td className="positive" key={column.key}>
                          {formatValue(factorValue?.value, factorValue?.unit ?? column.unit)}
                        </td>
                      );
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>
      </section>
    );
  }

  return (
    <section className="quant-page">
      <header className="quant-header">
        <div>
          <h1>퀀트 스크리너</h1>
          <p>
            {store.marketCount}개국 ·{" "}
            {store.isCatalogLoading && store.factorCount === 0
              ? "팩터 로딩 중"
              : `${store.factorCount}개 팩터`}
          </p>
        </div>
        <div className="header-actions">
          <button className="ghost-button" type="button">
            전략 불러오기
          </button>
          <button className="icon-chip" type="button" aria-label="닫기">
            <X size={16} />
          </button>
        </div>
      </header>

      <div className="quant-layout">
        <aside className="setup-panel">
          <h2>기본 설정</h2>
          <label className="field-label" htmlFor="market">
            투자국가
          </label>
          <select
            id="market"
            value={store.market}
            onChange={(event) => store.setMarket(event.target.value)}
          >
            {store.marketOptions.map((market) => (
              <option key={market.id} value={market.id}>
                {market.label}
              </option>
            ))}
          </select>

          <label className="field-label">산업 선택</label>
          <div className="industry-box">
            <button
              className="select-trigger"
              type="button"
              onClick={() => store.toggleIndustryMenu()}
            >
              <span>
                {store.selectedIndustries.length > 0
                  ? `${store.selectedIndustries.length}개 산업 선택`
                  : "전체 산업"}
              </span>
              {store.isIndustryOpen ? <ChevronDown size={16} /> : <ChevronRight size={16} />}
            </button>

            {store.isIndustryOpen && (
              <div className="industry-list">
                {store.industryCatalog.map((industry) => (
                  <button
                    className={store.selectedIndustries.includes(industry.id) ? "checked" : ""}
                    key={industry.id}
                    type="button"
                    onClick={() => store.toggleIndustry(industry.id)}
                  >
                    <span className="checkbox-mark">
                      {store.selectedIndustries.includes(industry.id) && <Check size={12} />}
                    </span>
                    <span>{industry.name}</span>
                  </button>
                ))}
              </div>
            )}
          </div>
        </aside>

        <section className="filter-panel">
          <div className="panel-title-row">
            <div>
              <h2>필터 선택</h2>
              <p>{store.selectedConditionCount}개 선택됨</p>
            </div>
            <button className="reset-button" type="button" onClick={() => store.resetConditions()}>
              초기화
            </button>
          </div>

          <label className="filter-search">
            <Search size={16} />
            <input placeholder="팩터 검색..." />
          </label>

          <div className="filter-groups">
            {store.isCatalogLoading ? (
              <p className="catalog-loading">카탈로그를 불러오는 중...</p>
            ) : (
              store.filterCatalog.map((group) => renderFilterGroup(group))
            )}
          </div>
        </section>

        <section className="condition-panel">
          <div className="condition-actions">
            <button className="purple-button" type="button" disabled={store.isScreening}>
              백테스팅
              <ArrowRight size={15} />
            </button>
            <button
              className="green-button"
              type="button"
              onClick={() => void store.runScreening()}
              disabled={store.isScreening || store.selectedConditionCount === 0}
            >
              종목찾기
              <ArrowRight size={15} />
            </button>
          </div>

          <div className="condition-title">
            <h2>조건 설정</h2>
            <p>{store.selectedConditionCount}개 팩터의 스크리닝 조건을 설정하세요</p>
          </div>

          <div className="condition-list">
            {store.selectedConditions.map((condition, index) => (
              <article className="condition-card" key={condition.filterId}>
                <button
                  className="remove-condition"
                  type="button"
                  onClick={() => store.removeCondition(condition.filterId)}
                  aria-label={`${condition.label} 조건 삭제`}
                >
                  <X size={16} />
                </button>
                <div className="condition-card-title">
                  <span>{index + 1}</span>
                  <div>
                    <strong>{condition.label}</strong>
                    <p>{condition.field}</p>
                  </div>
                </div>

                <div className="condition-mode-tabs" aria-label="조건 입력 방식">
                  <button
                    className={condition.inputMode === "percentile" ? "selected" : ""}
                    type="button"
                    onClick={() => store.updateConditionMode(condition.filterId, "percentile")}
                  >
                    비율 변경
                  </button>
                  <button
                    className={condition.inputMode === "value" ? "selected" : ""}
                    type="button"
                    onClick={() => store.updateConditionMode(condition.filterId, "value")}
                  >
                    값 변경
                  </button>
                </div>

                {condition.inputMode === "percentile" ? (
                  <div className="range-preview">
                    <strong>상위 0% - {condition.percentile}%</strong>
                    <input
                      type="range"
                      min="1"
                      max="100"
                      value={condition.percentile}
                      onChange={(event) =>
                        store.updateConditionPercentile(
                          condition.filterId,
                          Number(event.target.value),
                        )
                      }
                    />
                  </div>
                ) : (
                  <>
                    <p className="condition-subtitle">연산자</p>
                    <div className="operator-row" aria-label="연산자">
                      {operators.map((operator) => (
                        <button
                          className={condition.operator === operator ? "selected" : ""}
                          key={operator}
                          type="button"
                          onClick={() =>
                            store.updateConditionOperator(condition.filterId, operator)
                          }
                        >
                          {operator}
                        </button>
                      ))}
                    </div>
                    <label className="value-input">
                      <span>값</span>
                      <input
                        type="number"
                        value={condition.value}
                        onChange={(event) =>
                          store.updateConditionValue(condition.filterId, Number(event.target.value))
                        }
                      />
                    </label>
                  </>
                )}
              </article>
            ))}
          </div>
        </section>
      </div>

      {store.errorMessage && <p className="error-message">{store.errorMessage}</p>}
    </section>
  );
});
