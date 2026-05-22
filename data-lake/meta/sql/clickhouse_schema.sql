CREATE TABLE IF NOT EXISTS dart_report_metadata
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
