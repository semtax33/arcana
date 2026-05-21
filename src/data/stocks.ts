export type StockScore = {
  ticker: string;
  score: number;
};

export const topGrowthStocks: StockScore[] = [
  { ticker: 'LLY', score: 99.7 },
  { ticker: 'EXPE', score: 99.9 },
  { ticker: 'GOOGL', score: 99.6 },
  { ticker: 'WWD', score: 97.8 },
  { ticker: 'FIX', score: 96.1 },
  { ticker: 'APH', score: 94.3 },
  { ticker: 'PAHC', score: 98.7 },
  { ticker: 'MEDP', score: 89.6 },
  { ticker: 'AEM', score: 89.3 },
  { ticker: 'ENVA', score: 86.5 },
];
