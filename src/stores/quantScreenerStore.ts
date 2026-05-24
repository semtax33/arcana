import { makeAutoObservable, runInAction } from "mobx";
import {
  defaultMarketOptions,
  fetchFilterCatalog,
  fetchIndustryCatalog,
  fetchMarketCatalog,
  runFactorBacktest,
  runQuantScreening,
} from "../api/quantScreenerApi";
import type {
  ComparisonOperator,
  ConditionInputMode,
  FilterDefinition,
  FilterGroup,
  FactorBacktestResponse,
  BacktestRebalanceFrequency,
  IndustryOption,
  MarketCode,
  QuantScreenerResponse,
  ScreenerCondition,
  ScreenedStock,
  QuantScreenerColumn,
} from "../types/quantScreener";

type ScreenerViewMode = "builder" | "loading" | "result" | "backtest";
type SortDirection = "asc" | "desc";

const maxResultRows = 5000;
const defaultBacktestStartDate = "2016-01-01";
const defaultBacktestEndDate = "2026-04-26";
const defaultRebalanceFrequency: BacktestRebalanceFrequency = "annual";

const defaultIndustryIds: string[] = [];
const defaultExpandedGroups: string[] = [];
const defaultFactorIds = ["roe", "per", "epr", "bpr"];

function findFilter(
  groups: FilterGroup[],
  filterId: string,
): FilterDefinition | undefined {
  for (const group of groups) {
    const directMatch = group.filters?.find((filter) => filter.id === filterId);

    if (directMatch) {
      return directMatch;
    }

    const childMatch = group.children
      ? findFilter(group.children, filterId)
      : undefined;

    if (childMatch) {
      return childMatch;
    }
  }

  return undefined;
}

export class QuantScreenerStore {
  marketOptions = defaultMarketOptions;
  market: MarketCode = "KR";
  industryCatalog: IndustryOption[] = [];
  selectedIndustries = defaultIndustryIds;
  filterCatalog: FilterGroup[] = [];
  selectedConditions: ScreenerCondition[] = [];
  expandedGroupIds = defaultExpandedGroups;
  isIndustryOpen = true;
  viewMode: ScreenerViewMode = "builder";
  result: QuantScreenerResponse | null = null;
  sortKey = "rank";
  sortDirection: SortDirection = "asc";
  backtestResult: FactorBacktestResponse | null = null;
  backtestStartDate = defaultBacktestStartDate;
  backtestEndDate = defaultBacktestEndDate;
  backtestRebalanceFrequency: BacktestRebalanceFrequency =
    defaultRebalanceFrequency;
  isBacktesting = false;
  backtestRequestId = 0;
  isCatalogLoading = false;
  isScreening = false;
  errorMessage = "";
  backtestErrorMessage = "";

  constructor() {
    makeAutoObservable(this);
  }

  get selectedConditionCount() {
    return this.selectedConditions.length;
  }

  get marketCount() {
    return this.marketOptions.length;
  }

  get factorCount() {
    return countUniqueFilters(this.filterCatalog);
  }

  get hasResult() {
    return this.result !== null;
  }

  get sortedRows() {
    if (!this.result) {
      return [];
    }

    return [...this.result.rows].sort((left, right) => {
      const leftValue = this.getSortValue(left, this.sortKey);
      const rightValue = this.getSortValue(right, this.sortKey);
      const leftEmpty = isEmptySortValue(leftValue);
      const rightEmpty = isEmptySortValue(rightValue);

      if (leftEmpty && rightEmpty) {
        return 0;
      }

      if (leftEmpty) {
        return 1;
      }

      if (rightEmpty) {
        return -1;
      }

      const comparison = compareSortValues(leftValue, rightValue);

      return this.sortDirection === "asc" ? comparison : -comparison;
    });
  }

  get selectedIndustryLabels() {
    return this.industryCatalog
      .filter((industry) => this.selectedIndustries.includes(industry.id))
      .map((industry) => industry.name);
  }

  async loadCatalog() {
    if (this.filterCatalog.length > 0 || this.isCatalogLoading) {
      return;
    }

    this.isCatalogLoading = true;
    this.errorMessage = "";

    try {
      const markets = await fetchMarketCatalog();
      const selectedMarket = markets.some((market) => market.id === this.market)
        ? this.market
        : (markets[0]?.id ?? this.market);
      const [industries, filters] = await Promise.all([
        fetchIndustryCatalog(selectedMarket),
        fetchFilterCatalog(),
      ]);

      runInAction(() => {
        this.marketOptions = markets;
        this.market = selectedMarket;
        this.industryCatalog = industries;
        this.filterCatalog = filters;
        this.expandedGroupIds = this.getInitialExpandedGroupIds(filters);
        this.addDefaultConditions();
      });
    } catch {
      runInAction(() => {
        this.errorMessage = "스크리너 카탈로그를 불러오지 못했습니다.";
      });
    } finally {
      runInAction(() => {
        this.isCatalogLoading = false;
      });
    }
  }

  async reloadIndustries() {
    try {
      const industries = await fetchIndustryCatalog(this.market);
      runInAction(() => {
        this.industryCatalog = industries;
      });
    } catch {
      runInAction(() => {
        this.errorMessage = "산업 목록을 불러오지 못했습니다.";
      });
    }
  }

  addDefaultConditions() {
    if (this.selectedConditions.length > 0) {
      return;
    }

    for (const factorId of defaultFactorIds) {
      const filter = findFilter(this.filterCatalog, factorId);

      if (filter) {
        this.addCondition(filter);
      }
    }

    if (this.selectedConditions.length === 0) {
      const firstFilters = this.filterCatalog
        .flatMap((group) => group.children ?? [])
        .flatMap((group) => group.filters ?? [])
        .slice(0, 2);

      for (const filter of firstFilters) {
        this.addCondition(filter);
      }
    }
  }

  getInitialExpandedGroupIds(groups: FilterGroup[]) {
    return groups.flatMap((group) => [
      group.id,
      ...(group.children?.slice(0, 2).map((child) => child.id) ?? []),
    ]);
  }

  setMarket(market: MarketCode) {
    this.market = market;
    void this.reloadIndustries();
  }

  toggleIndustryMenu() {
    this.isIndustryOpen = !this.isIndustryOpen;
  }

  toggleIndustry(industryId: string) {
    if (this.selectedIndustries.includes(industryId)) {
      this.selectedIndustries = this.selectedIndustries.filter(
        (item) => item !== industryId,
      );
      return;
    }

    this.selectedIndustries = [...this.selectedIndustries, industryId];
  }

  toggleGroup(groupId: string) {
    if (this.expandedGroupIds.includes(groupId)) {
      this.expandedGroupIds = this.expandedGroupIds.filter(
        (item) => item !== groupId,
      );
      return;
    }

    this.expandedGroupIds = [...this.expandedGroupIds, groupId];
  }

  isGroupExpanded(groupId: string) {
    return this.expandedGroupIds.includes(groupId);
  }

  addCondition(filter: FilterDefinition) {
    if (
      this.selectedConditions.some(
        (condition) => condition.filterId === filter.id,
      )
    ) {
      return;
    }

    this.selectedConditions = [
      ...this.selectedConditions,
      {
        filterId: filter.id,
        field: filter.field,
        label: filter.label,
        inputMode: filter.defaultInputMode ?? "percentile",
        operator: filter.defaultOperator,
        value: filter.defaultValue,
        percentile: 30,
        unit: filter.unit,
      },
    ];
  }

  removeCondition(filterId: string) {
    this.selectedConditions = this.selectedConditions.filter(
      (condition) => condition.filterId !== filterId,
    );
  }

  updateConditionOperator(filterId: string, operator: ComparisonOperator) {
    this.selectedConditions = this.selectedConditions.map((condition) =>
      condition.filterId === filterId ? { ...condition, operator } : condition,
    );
  }

  updateConditionMode(filterId: string, inputMode: ConditionInputMode) {
    this.selectedConditions = this.selectedConditions.map((condition) =>
      condition.filterId === filterId ? { ...condition, inputMode } : condition,
    );
  }

  updateConditionValue(filterId: string, value: number) {
    this.selectedConditions = this.selectedConditions.map((condition) =>
      condition.filterId === filterId ? { ...condition, value } : condition,
    );
  }

  updateConditionPercentile(filterId: string, percentile: number) {
    this.selectedConditions = this.selectedConditions.map((condition) =>
      condition.filterId === filterId
        ? { ...condition, percentile }
        : condition,
    );
  }

  resetConditions() {
    this.selectedConditions = [];
    this.addDefaultConditions();
  }

  backToBuilder() {
    if (this.viewMode === "backtest") {
      this.backtestRequestId += 1;
      this.isBacktesting = false;
    }

    this.viewMode = "builder";
  }

  openBacktest() {
    if (this.selectedConditions.length === 0) {
      return;
    }

    this.viewMode = "backtest";
    this.backtestResult = null;
    this.backtestRebalanceFrequency = defaultRebalanceFrequency;
    this.backtestErrorMessage = "";
  }

  setBacktestRebalanceFrequency(frequency: BacktestRebalanceFrequency) {
    this.backtestRebalanceFrequency = frequency;
  }

  async runBacktest() {
    if (this.selectedConditions.length === 0 || this.isBacktesting) {
      return;
    }

    const requestId = this.backtestRequestId + 1;
    const conditions = this.selectedConditions.map((condition) => ({
      ...condition,
    }));
    const industries = [...this.selectedIndustries];
    const market = this.market;
    const startDate = this.backtestStartDate;
    const endDate = this.backtestEndDate;
    const rebalanceFrequency = this.backtestRebalanceFrequency;

    this.backtestRequestId = requestId;
    this.isBacktesting = true;
    this.backtestErrorMessage = "";

    try {
      const result = await runFactorBacktest({
        market,
        industries,
        conditions,
        startDate,
        endDate,
        rebalanceFrequency,
      });

      runInAction(() => {
        if (requestId !== this.backtestRequestId) {
          return;
        }

        this.backtestResult = result;
      });
    } catch {
      runInAction(() => {
        if (requestId !== this.backtestRequestId) {
          return;
        }

        this.backtestErrorMessage = "백테스트 결과를 불러오지 못했습니다.";
      });
    } finally {
      runInAction(() => {
        if (requestId !== this.backtestRequestId) {
          return;
        }

        this.isBacktesting = false;
      });
    }
  }

  toggleSort(column: QuantScreenerColumn) {
    if (this.sortKey === column.key) {
      this.sortDirection = this.sortDirection === "asc" ? "desc" : "asc";
      return;
    }

    this.sortKey = column.key;
    this.sortDirection = column.columnType === "rank" ? "asc" : "desc";
  }

  private getSortValue(row: ScreenedStock, columnKey: string) {
    switch (columnKey) {
      case "rank":
        return row.rank;
      case "ticker":
        return row.ticker;
      case "stock_name":
        return row.name;
      case "country":
        return row.market;
      case "market_cap":
        return row.marketCap;
      case "percentile":
        return row.percentile;
      default:
        return row.factorValues[columnKey]?.value ?? null;
    }
  }

  async runScreening() {
    this.isScreening = true;
    this.viewMode = "loading";
    this.result = null;
    this.errorMessage = "";

    try {
      const result = await runQuantScreening({
        market: this.market,
        industries: this.selectedIndustries,
        conditions: this.selectedConditions,
        page: 1,
        pageSize: maxResultRows,
      });

      runInAction(() => {
        this.result = result;
        this.sortKey = "rank";
        this.sortDirection = "asc";
        this.viewMode = "result";
      });
    } catch {
      runInAction(() => {
        this.errorMessage = "스크리닝 실행 중 오류가 발생했습니다.";
        this.viewMode = "builder";
      });
    } finally {
      runInAction(() => {
        this.isScreening = false;
      });
    }
  }
}

export const quantScreenerStore = new QuantScreenerStore();

function countUniqueFilters(groups: FilterGroup[]) {
  const filterIds = new Set<string>();

  collectFilterIds(groups, filterIds);

  return filterIds.size;
}

function collectFilterIds(groups: FilterGroup[], filterIds: Set<string>) {
  for (const group of groups) {
    group.filters?.forEach((filter) => filterIds.add(filter.id));

    if (group.children) {
      collectFilterIds(group.children, filterIds);
    }
  }
}

function compareSortValues(
  left: string | number | null | undefined,
  right: string | number | null | undefined,
) {
  if (typeof left === "number" && typeof right === "number") {
    return left - right;
  }

  return String(left).localeCompare(String(right), "ko-KR", {
    numeric: true,
    sensitivity: "base",
  });
}

function isEmptySortValue(value: string | number | null | undefined) {
  return value === null || value === undefined || value === "";
}
