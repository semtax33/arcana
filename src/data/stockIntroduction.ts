export type StockIntroductionSector = {
  code: string;
  name: string;
};

export type StockIntroductionResponse = {
  stock_code: string;
  stock_name: string;
  market: "KR" | "US";
  currency: "KRW" | "USD";
  as_of_date: string;
  market_cap: number | null;
  trailing_per: number | null;
  dividend_yield: number | null;
  week_52_low: number | null;
  week_52_high: number | null;
  week_52_change_rate: number | null;
  company_description: string;
  gsic_sectors: StockIntroductionSector[];
  sections: {
    valuation: {
      market_cap: number | null;
      trailing_per: number | null;
      dividend_yield: number | null;
      week_52_low: number | null;
      week_52_high: number | null;
      week_52_change_rate: number | null;
    };
    company: {
      description: string;
    };
    business_areas: StockIntroductionSector[];
  };
};

type StockIntroductionProfile = {
  stockName: string;
  market: "KR" | "US";
  currency: "KRW" | "USD";
  basePrice: number;
  marketCap: number | null;
  trailingPer: number | null;
  dividendYield: number | null;
  companyDescription: string;
  sectors: StockIntroductionSector[];
};

const stockIntroductionProfiles: Record<string, StockIntroductionProfile> = {
  "236200": {
    stockName: "슈프리마",
    market: "KR",
    currency: "KRW",
    basePrice: 32700,
    marketCap: 262_000_000_000,
    trailingPer: 10.98,
    dividendYield: 0,
    companyDescription:
      "슈프리마는 바이오인식 기반 출입통제, 근태관리, 지문인식 알고리즘과 관련 단말 및 모듈을 공급하는 통합 보안 솔루션 기업입니다.",
    sectors: [
      { code: "4520", name: "Technology Hardware & Equipment" },
      { code: "452030", name: "Electronic Equipment, Instruments & Components" },
    ],
  },
  "005710": {
    stockName: "대원산업",
    market: "KR",
    currency: "KRW",
    basePrice: 7100,
    marketCap: 142_000_000_000,
    trailingPer: 6.42,
    dividendYield: 2.14,
    companyDescription: "대원산업은 자동차 시트와 관련 부품을 생산해 완성차 업체에 공급하는 자동차 부품 기업입니다.",
    sectors: [
      { code: "2510", name: "Automobiles & Components" },
      { code: "251010", name: "Automobile Components" },
    ],
  },
  "440110": {
    stockName: "파두",
    market: "KR",
    currency: "KRW",
    basePrice: 18200,
    marketCap: 912_000_000_000,
    trailingPer: null,
    dividendYield: 0,
    companyDescription:
      "파두는 데이터센터용 반도체 설계와 스토리지 컨트롤러 솔루션을 중심으로 사업을 전개하는 팹리스 반도체 기업입니다.",
    sectors: [
      { code: "4530", name: "Semiconductors & Semiconductor Equipment" },
      { code: "453010", name: "Semiconductors" },
    ],
  },
  "019540": {
    stockName: "일지테크",
    market: "KR",
    currency: "KRW",
    basePrice: 5300,
    marketCap: 72_000_000_000,
    trailingPer: 5.8,
    dividendYield: 1.35,
    companyDescription: "일지테크는 자동차 차체 부품과 관련 금형을 생산하는 자동차 부품 업체입니다.",
    sectors: [
      { code: "2510", name: "Automobiles & Components" },
      { code: "251010", name: "Automobile Components" },
    ],
  },
  "019180": {
    stockName: "티에이치엔",
    market: "KR",
    currency: "KRW",
    basePrice: 4200,
    marketCap: 81_000_000_000,
    trailingPer: 4.74,
    dividendYield: 1.62,
    companyDescription: "티에이치엔은 자동차 전장 하네스와 관련 부품을 생산해 국내외 완성차 업체에 공급합니다.",
    sectors: [
      { code: "2510", name: "Automobiles & Components" },
      { code: "251010", name: "Automobile Components" },
    ],
  },
  "005930": {
    stockName: "삼성전자",
    market: "KR",
    currency: "KRW",
    basePrice: 72000,
    marketCap: 430_000_000_000_000,
    trailingPer: 18.7,
    dividendYield: 1.95,
    companyDescription:
      "삼성전자는 반도체, 모바일, 디스플레이, 가전 등 전자 산업 전반에 걸쳐 사업을 영위하는 글로벌 기업입니다.",
    sectors: [
      { code: "4530", name: "Semiconductors & Semiconductor Equipment" },
      { code: "4520", name: "Technology Hardware & Equipment" },
    ],
  },
  "003230": {
    stockName: "삼양식품",
    market: "KR",
    currency: "KRW",
    basePrice: 511000,
    marketCap: 3_850_000_000_000,
    trailingPer: 21.3,
    dividendYield: 0.42,
    companyDescription: "삼양식품은 라면과 스낵 등 식품을 제조하며 국내외 식품 시장에서 판매망을 확장하고 있습니다.",
    sectors: [
      { code: "3020", name: "Food, Beverage & Tobacco" },
      { code: "302020", name: "Food Products" },
    ],
  },
};

function normalizeStockCode(stockCode: string) {
  return stockCode.trim().toUpperCase();
}

function createUnknownProfile(stockCode: string): StockIntroductionProfile {
  const seed = hashStockCode(stockCode);

  return {
    stockName: `${stockCode} 종목`,
    market: "KR",
    currency: "KRW",
    basePrice: 12000 + seed * 43,
    marketCap: null,
    trailingPer: null,
    dividendYield: null,
    companyDescription: "",
    sectors: [],
  };
}

function hashStockCode(stockCode: string) {
  return stockCode.split("").reduce((hash, char) => hash + char.charCodeAt(0), 0);
}

function toDateKey(date: Date) {
  return date.toISOString().slice(0, 10);
}

function createAnnualPriceRange(stockCode: string, basePrice: number, asOfDate: Date) {
  const seed = hashStockCode(stockCode);
  let high: number | null = null;
  let low: number | null = null;

  for (let index = 0; index < 365; index += 1) {
    const date = new Date(asOfDate);
    date.setDate(asOfDate.getDate() - (364 - index));

    const cycle = Math.sin((index + seed) / 17) * 0.045;
    const longCycle = Math.cos((index + seed * 2) / 59) * 0.075;
    const drift = (index / 364) * 0.16 - 0.08;
    const close = Math.max(500, basePrice * (1 + cycle + longCycle + drift));
    const dayHigh = close * (1.012 + Math.abs(Math.sin(index / 11)) * 0.018);
    const dayLow = close * (0.988 - Math.abs(Math.cos(index / 13)) * 0.015);

    high = high === null ? dayHigh : Math.max(high, dayHigh);
    low = low === null ? dayLow : Math.min(low, dayLow);
  }

  return {
    high: high === null ? null : Math.round(high / 10) * 10,
    low: low === null ? null : Math.round(low / 10) * 10,
  };
}

function calculateChangeRate(low: number | null, high: number | null) {
  if (low === null || high === null || low <= 0) {
    return null;
  }

  return Number((((high - low) / low) * 100).toFixed(2));
}

export function buildStockIntroduction(
  stockCode: string,
  asOfDate: Date = new Date(),
): StockIntroductionResponse {
  const normalizedStockCode = normalizeStockCode(stockCode);
  const profile = stockIntroductionProfiles[normalizedStockCode] ?? createUnknownProfile(normalizedStockCode);
  const week52Range = createAnnualPriceRange(normalizedStockCode, profile.basePrice, asOfDate);
  const week52ChangeRate = calculateChangeRate(week52Range.low, week52Range.high);

  return {
    stock_code: normalizedStockCode,
    stock_name: profile.stockName,
    market: profile.market,
    currency: profile.currency,
    as_of_date: toDateKey(asOfDate),
    market_cap: profile.marketCap,
    trailing_per: profile.trailingPer,
    dividend_yield: profile.dividendYield,
    week_52_low: week52Range.low,
    week_52_high: week52Range.high,
    week_52_change_rate: week52ChangeRate,
    company_description: profile.companyDescription,
    gsic_sectors: profile.sectors,
    sections: {
      valuation: {
        market_cap: profile.marketCap,
        trailing_per: profile.trailingPer,
        dividend_yield: profile.dividendYield,
        week_52_low: week52Range.low,
        week_52_high: week52Range.high,
        week_52_change_rate: week52ChangeRate,
      },
      company: {
        description: profile.companyDescription,
      },
      business_areas: profile.sectors,
    },
  };
}
