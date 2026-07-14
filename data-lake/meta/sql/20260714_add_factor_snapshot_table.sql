CREATE TABLE IF NOT EXISTS arcana.fact_daily_factor_snapshot
(
    trade_date Date,
    security_id String,
    factor_id LowCardinality(String),
    financial_basis LowCardinality(String) DEFAULT 'annual',
    factor_value Nullable(Float64),
    source_trade_date Date,
    fiscal_year Nullable(UInt16),
    financial_period Nullable(Date),
    currency LowCardinality(String) DEFAULT 'KRW',
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(trade_date)
ORDER BY (trade_date, factor_id, financial_basis, security_id)
SETTINGS index_granularity = 8192;

