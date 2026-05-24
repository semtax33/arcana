export type StyleScoreDataSource = "api" | "mock";

export type StyleScoreFactor = {
  factorId: string;
  label: string;
  rawValue: number | null;
  winsorizedValue: number | null;
  percentileScore: number | null;
  robustZScore: number | null;
  weight: number;
  weightedScore: number | null;
  peerCount: number | null;
  fallbackLevel: string | null;
  fallbackCode: string | null;
};

export type StyleScoreGroup = {
  id: string;
  componentKey: string;
  label: string;
  score: number | null;
  scoreConfidence: number | null;
  availableFactorCount: number | null;
  requiredFactorCount: number | null;
  weight: number | null;
  factors: StyleScoreFactor[];
};

export type StyleScoreStock = {
  securityId: string;
  ticker: string;
  name: string;
  country: string;
  asOfDate: string | null;
  compositeScore: number | null;
  groups: StyleScoreGroup[];
};

export type StyleScoresResult<T> = {
  data: T;
  source: StyleScoreDataSource;
  message: string | null;
};
