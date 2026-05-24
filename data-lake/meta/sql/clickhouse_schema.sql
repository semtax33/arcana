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

