CREATE TABLE IF NOT EXISTS arcana.dart_report_metadata
(
    security_id String,
    stock_code FixedString(6),
    fiscal_year Int32,
    fiscal_month UInt8,
    period_end_date Date,
    report_date Date,
    rcept_no String,
    report_name String,
    source_type LowCardinality(String),
    source_url String,
    updated_at DateTime DEFAULT now()
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(report_date)
ORDER BY
(
    security_id,
    fiscal_year,
    fiscal_month,
    source_type,
    report_date,
    rcept_no
);

CREATE TABLE IF NOT EXISTS arcana.benchmark_price_daily
(
    benchmark_id LowCardinality(String),
    trade_date   Date,
    open         Nullable(Decimal(20, 6)),
    high         Nullable(Decimal(20, 6)),
    low          Nullable(Decimal(20, 6)),
    close        Nullable(Decimal(20, 6)),
    volume       Nullable(UInt64),
    currency     LowCardinality(String)      default 'KRW',
    updated_at   DateTime64(3, 'Asia/Seoul') default now64(3)
)
    engine = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(trade_date)
        ORDER BY (benchmark_id, trade_date)
        SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS benchmark_price_daily
(
    benchmark_id LowCardinality(String),
    trade_date   Date,
    open         Nullable(Decimal(20, 6)),
    high         Nullable(Decimal(20, 6)),
    low          Nullable(Decimal(20, 6)),
    close        Nullable(Decimal(20, 6)),
    volume       Nullable(UInt64),
    currency     LowCardinality(String)      default 'KRW',
    updated_at   DateTime64(3, 'Asia/Seoul') default now64(3)
)
    engine = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(trade_date)
        ORDER BY (benchmark_id, trade_date)
        SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS fact_canonical_statements
(
    corp_code    String,
    fs_year      UInt16,
    fs_quarter   LowCardinality(String),
    fs_type      LowCardinality(String)      default '',
    canonical_id String,
    amount       Nullable(Int64),
    updated_at   DateTime64(3, 'Asia/Seoul') default now64(3)
)
    engine = ReplacingMergeTree(updated_at)
        PARTITION BY fs_year
        ORDER BY (corp_code, fs_year, fs_quarter, canonical_id)
        SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS fact_canonical_statements
(
    corp_code    String,
    fs_year      UInt16,
    fs_quarter   LowCardinality(String),
    fs_type      LowCardinality(String)      default '',
    canonical_id String,
    amount       Nullable(Int64),
    updated_at   DateTime64(3, 'Asia/Seoul') default now64(3)
)
    engine = ReplacingMergeTree(updated_at)
        PARTITION BY fs_year
        ORDER BY (corp_code, fs_year, fs_quarter, canonical_id)
        SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS fact_canonical_statements
(
    corp_code    String,
    fs_year      UInt16,
    fs_quarter   LowCardinality(String),
    fs_type      LowCardinality(String)      default '',
    canonical_id String,
    amount       Nullable(Int64),
    updated_at   DateTime64(3, 'Asia/Seoul') default now64(3)
)
    engine = ReplacingMergeTree(updated_at)
        PARTITION BY fs_year
        ORDER BY (corp_code, fs_year, fs_quarter, canonical_id)
        SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS identifiers
(
    security_id String,
    id_type     LowCardinality(String),
    id_value    String,
    market_mic  LowCardinality(String)      default '',
    is_primary  Bool                        default true,
    created_at  DateTime64(3, 'Asia/Seoul') default now64(3),
    updated_at  DateTime64(3, 'Asia/Seoul') default now64(3)
)
    engine = ReplacingMergeTree(updated_at)
        ORDER BY (id_type, id_value, market_mic)
        SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS issuers
(
    issuer_id           String,
    legal_name_ko       String                      default '',
    legal_name_en       String                      default '',
    domicile_country    LowCardinality(String)      default '',
    region              LowCardinality(String)      default '',
    industry_schema     LowCardinality(String)      default '',
    sector_code         String                      default '',
    is_active           Bool                        default true,
    created_at          DateTime64(3, 'Asia/Seoul') default now64(3),
    updated_at          DateTime64(3, 'Asia/Seoul') default now64(3),
    industry_group_code LowCardinality(String)      default '',
    industry_group_name String                      default ''
)
    engine = ReplacingMergeTree(updated_at)
        ORDER BY issuer_id
        SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS arcana.price_daily
(
    security_id String,
    trade_date  Date,
    open        Nullable(Decimal(20, 6)),
    high        Nullable(Decimal(20, 6)),
    low         Nullable(Decimal(20, 6)),
    close       Nullable(Decimal(20, 6)),
    volume      Nullable(UInt64),
    adj_close   Nullable(Decimal(20, 6)),
    currency    LowCardinality(String)      default '',
    updated_at  DateTime64(3, 'Asia/Seoul') default now64(3)
)
    engine = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(trade_date)
        ORDER BY (security_id, trade_date)
        SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS arcana.security_master
(
    security_id   String,
    issuer_id     String                      default '',
    sec_type      LowCardinality(String)      default '',
    asset_subtype LowCardinality(String)      default '',
    share_class   LowCardinality(String)      default '',
    is_active     Bool                        default true,
    created_at    DateTime64(3, 'Asia/Seoul') default now64(3),
    updated_at    DateTime64(3, 'Asia/Seoul') default now64(3)
)
    engine = ReplacingMergeTree(updated_at)
        ORDER BY security_id
        SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS arcana.stock_dividend
(
    security_id      String,
    trade_date       Date,
    dividend         Nullable(Decimal(20, 6)),
    payout_ratio     Nullable(Float64),
    dividend_percent Nullable(Float64),
    currency         LowCardinality(String)      default '',
    updated_at       DateTime64(3, 'Asia/Seoul') default now64(3)
)
    engine = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(trade_date)
        ORDER BY (trade_date, security_id)
        SETTINGS index_granularity = 8192;

create table arcana.stock_shares
(
    security_id String,
    trade_date  Date,
    shares      Nullable(UInt64),
    market_cap  Nullable(Decimal(38, 4)),
    currency    LowCardinality(String)      default '',
    updated_at  DateTime64(3, 'Asia/Seoul') default now64(3)
)
    engine = ReplacingMergeTree(updated_at)
        PARTITION BY toYYYYMM(trade_date)
        ORDER BY (trade_date, security_id)
        SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS arcana.fact_daily_factor_score
(
    trade_date Date,
    security_id String,
    issuer_id String DEFAULT '',
    stock_code FixedString(6) DEFAULT '',
    company_name String DEFAULT '',
    industry_schema LowCardinality(String) DEFAULT '',
    industry_level LowCardinality(String) DEFAULT '',
    industry_code String DEFAULT '',
    industry_name String DEFAULT '',
    factor_id LowCardinality(String),
    style_group LowCardinality(String) DEFAULT '',
    factor_direction Int8,
    raw_factor_value Nullable(Float64),
    winsorized_value Nullable(Float64),
    percentile_score Nullable(Float64),
    robust_z_score Nullable(Float64),
    n_peers UInt32,
    score_method LowCardinality(String) DEFAULT 'INDUSTRY_PERCENTILE',
    fallback_level LowCardinality(String) DEFAULT '',
    is_valid Bool DEFAULT true,
    invalid_reason String DEFAULT '',
    is_winsorized Bool DEFAULT false,
    is_missing Bool DEFAULT false,
    score_confidence Float64 DEFAULT 1.0,
    source_trade_date Date,
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(trade_date)
ORDER BY
(
    trade_date,
    factor_id,
    industry_schema,
    industry_code,
    security_id
)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS arcana.fact_daily_style_score
(
    trade_date Date,
    security_id String,
    issuer_id String DEFAULT '',
    stock_code FixedString(6) DEFAULT '',
    company_name String DEFAULT '',
    industry_schema LowCardinality(String) DEFAULT '',
    industry_code String DEFAULT '',
    industry_name String DEFAULT '',
    style_profile LowCardinality(String),
    value_score Nullable(Float64),
    quality_score Nullable(Float64),
    growth_score Nullable(Float64),
    momentum_score Nullable(Float64),
    risk_score Nullable(Float64),
    dividend_score Nullable(Float64),
    total_score Nullable(Float64),
    total_score_sort Float64 DEFAULT -1,
    available_factor_count UInt16,
    required_factor_count UInt16,
    score_confidence Float64,
    missing_factor_ids Array(String) DEFAULT [],
    invalid_factor_ids Array(String) DEFAULT [],
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(trade_date)
ORDER BY
(
    trade_date,
    style_profile,
    total_score_sort,
    security_id
)
SETTINGS index_granularity = 8192;

CREATE TABLE IF NOT EXISTS arcana.industry_factor_daily_snapshot
(
    trade_date Date,
    industry_schema LowCardinality(String) DEFAULT '',
    industry_level LowCardinality(String) DEFAULT '',
    industry_code String DEFAULT '',
    industry_name String DEFAULT '',
    factor_id LowCardinality(String),
    n_companies UInt32,
    avg_value Nullable(Float64),
    median_value Nullable(Float64),
    p10_value Nullable(Float64),
    p25_value Nullable(Float64),
    p75_value Nullable(Float64),
    p90_value Nullable(Float64),
    winsor_avg_value Nullable(Float64),
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(trade_date)
ORDER BY
(
    trade_date,
    industry_schema,
    industry_level,
    industry_code,
    factor_id
)
SETTINGS index_granularity = 8192;
