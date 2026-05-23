export type IndustryMetricKey =
  | "strongStockRatio"
  | "expectedEpsGrowth"
  | "dailyReturn"
  | "weeklyReturn"
  | "roe"
  | "per"
  | "pbr";

export type IndustryAnalysisRow = {
  industryName: string;
  stockCount: number;
  strongStockCount: number;
  strongStockRatio: number | null;
  expectedEpsGrowth: number | null;
  dailyReturn: number | null;
  weeklyReturn: number | null;
  roe: number | null;
  per: number | null;
  pbr: number | null;
};

export type SectorLeadersResponse = {
  asOfDate: string | null;
  rows: IndustryAnalysisRow[];
};
