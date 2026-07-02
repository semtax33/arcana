CREATE TABLE IF NOT EXISTS arcana.dart_report_metadata
(
    security_id String,
    stock_code FixedString(64),
    country LowCardinality(String) DEFAULT 'KR',
    market_mic LowCardinality(String) DEFAULT '',
    filing_system LowCardinality(String) DEFAULT 'DART',
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
    country      LowCardinality(String)      default '',
    market_mic   LowCardinality(String)      default '',
    benchmark_family LowCardinality(String)  default '',
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
    country      LowCardinality(String)      default '',
    market_mic   LowCardinality(String)      default '',
    benchmark_family LowCardinality(String)  default '',
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
    country       LowCardinality(String)      default '',
    primary_market_mic LowCardinality(String) default '',
    currency      LowCardinality(String)      default '',
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
    stock_code FixedString(64) DEFAULT '',
    country LowCardinality(String) DEFAULT '',
    market_mic LowCardinality(String) DEFAULT '',
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

CREATE TABLE IF NOT EXISTS business_operating_metric_raw
(
    security_id String,
    stock_code String,
    fiscal_year UInt16,
    fiscal_month UInt8,
    period_end_date Date,
    report_date Date,
    rcept_no String,
    source_url String,
    section_key LowCardinality(String),
    section_title String,
    table_id String,
    table_kind LowCardinality(String),
    row_idx Int32,
    col_idx Int32,
    raw_label String,
    raw_value String,
    raw_unit String,
    row_text String,
    header_value_map_json String,
    metric_candidate LowCardinality(String),
    product_candidate String,
    segment_candidate String,
    parsed_value Nullable(Float64),
    parsed_unit LowCardinality(String),
    parser_rule_id LowCardinality(String),
    confidence Nullable(Float64),
    created_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY fiscal_year
ORDER BY (stock_code, fiscal_year, fiscal_month, table_id, row_idx, col_idx);

CREATE TABLE IF NOT EXISTS business_operating_metric
(
    security_id String,
    stock_code String,
    fiscal_year UInt16,
    fiscal_month UInt8,
    period_end_date Date,
    business_domain LowCardinality(String),
    segment_id String,
    segment_name String,
    product_id String,
    product_name String,
    metric_id LowCardinality(String),
    metric_name String,
    metric_value Nullable(Float64),
    metric_unit LowCardinality(String),
    value_type LowCardinality(String),
    source_type LowCardinality(String),
    source_table_id String,
    source_row_idx Int32,
    source_url String,
    confidence Nullable(Float64),
    quality_flags String,
    model_version LowCardinality(String),
    created_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY fiscal_year
ORDER BY (stock_code, fiscal_year, fiscal_month, product_id, metric_id);

CREATE TABLE IF NOT EXISTS business_unit_economics
(
    security_id String,
    stock_code String,
    fiscal_year UInt16,
    fiscal_month UInt8,
    period_end_date Date,
    business_domain LowCardinality(String),
    segment_id String,
    segment_name String,
    product_id String,
    product_name String,
    revenue Nullable(Float64),
    quantity Nullable(Float64),
    quantity_unit LowCardinality(String),
    p Nullable(Float64),
    asp Nullable(Float64),
    revenue_source LowCardinality(String),
    quantity_source LowCardinality(String),
    revenue_coverage_ratio Nullable(Float64),
    confidence Nullable(Float64),
    quality_flags String,
    model_version LowCardinality(String),
    created_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3),
    c Nullable(Float64),
    gross_profit Nullable(Float64),
    gross_margin Nullable(Float64),
    cogs_source LowCardinality(String),
    cogs_allocation_method LowCardinality(String)
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY fiscal_year
ORDER BY (stock_code, fiscal_year, fiscal_month, product_id);

CREATE TABLE IF NOT EXISTS business_unit_economics_driver
(
    security_id String,
    stock_code String,
    fiscal_year UInt16,
    fiscal_month UInt8,
    period_end_date Date,
    business_domain LowCardinality(String),
    segment_id String,
    segment_name String,
    product_id String,
    product_name String,
    q_yoy_pct Nullable(Float64),
    asp_yoy_pct Nullable(Float64),
    unit_cost_yoy_pct Nullable(Float64),
    revenue_yoy_pct Nullable(Float64),
    gross_margin_change_pctp Nullable(Float64),
    model_version LowCardinality(String),
    created_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(created_at)
PARTITION BY fiscal_year
ORDER BY (stock_code, fiscal_year, fiscal_month, product_id);

CREATE TABLE IF NOT EXISTS arcana_estimate_component
(
    security_id String,
    stock_code String,
    target_period LowCardinality(String),
    metric_id LowCardinality(String),
    model_id LowCardinality(String),
    scenario LowCardinality(String),
    estimate_value Nullable(Float64),
    currency LowCardinality(String),
    source_actual_period LowCardinality(String),
    assumptions_json String,
    confidence Nullable(Float64),
    quality_flags String,
    as_of_date Date
)
ENGINE = ReplacingMergeTree(as_of_date)
ORDER BY (stock_code, target_period, metric_id, model_id, scenario);

CREATE TABLE IF NOT EXISTS arcana_estimate_consensus
(
    security_id String,
    stock_code String,
    target_period LowCardinality(String),
    metric_id LowCardinality(String),
    scenario LowCardinality(String),
    consensus_mean Nullable(Float64),
    consensus_median Nullable(Float64),
    consensus_low Nullable(Float64),
    consensus_high Nullable(Float64),
    model_count UInt16,
    confidence Nullable(Float64),
    dispersion Nullable(Float64),
    currency LowCardinality(String),
    as_of_date Date
)
ENGINE = ReplacingMergeTree(as_of_date)
ORDER BY (stock_code, target_period, metric_id, scenario);

CREATE TABLE IF NOT EXISTS arcana_estimate_consensus_history
(
    security_id String,
    stock_code String,
    target_period LowCardinality(String),
    metric_id LowCardinality(String),
    scenario LowCardinality(String),
    consensus_mean Nullable(Float64),
    consensus_median Nullable(Float64),
    consensus_low Nullable(Float64),
    consensus_high Nullable(Float64),
    model_count UInt16,
    confidence Nullable(Float64),
    dispersion Nullable(Float64),
    currency LowCardinality(String),
    as_of_date Date,
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(as_of_date)
ORDER BY (as_of_date, stock_code, target_period, metric_id, scenario);

CREATE TABLE IF NOT EXISTS real_consensus_reports
(
    security_id String,
    stock_code String,
    file_register_date Date DEFAULT toDate('1970-01-01'),
    file_year UInt16 DEFAULT 0,
    report_idx UInt64 DEFAULT 0,
    publish_code LowCardinality(String),
    office_name String,
    business_code String,
    business_name String,
    industry_code String,
    industry_name String,
    market_type LowCardinality(String),
    report_type LowCardinality(String),
    report_title String,
    report_writer String,
    report_content String,
    report_filepath String,
    report_filename String,
    report_date Nullable(Date),
    grade_code LowCardinality(String),
    grade_value LowCardinality(String),
    old_grade_code LowCardinality(String),
    old_grade_value LowCardinality(String),
    opinion_end_prices Nullable(Float64),
    target_stock_prices Nullable(Float64),
    old_target_stock_prices Nullable(Float64),
    change_stock_prices Nullable(Float64),
    stock_settlement_day1 String,
    stock_eps1 Nullable(Float64),
    stock_settlement_day2 String,
    stock_eps2 Nullable(Float64),
    stock_settlement_day3 String,
    stock_eps3 Nullable(Float64),
    stock_old_eps Nullable(Float64),
    stock_net_profit1 Nullable(Float64),
    stock_net_profit2 Nullable(Float64),
    stock_net_profit3 Nullable(Float64),
    stock_settlement_day String,
    stock_expected_sales Nullable(Float64),
    stock_pre_operating_profit Nullable(Float64),
    stock_pre_net_income Nullable(Float64),
    stock_pre_eps Nullable(Float64),
    stock_pre_per Nullable(Float64),
    stock_pre_pbr Nullable(Float64),
    stock_pre_ev Nullable(Float64),
    stock_pre_roe Nullable(Float64),
    register_date Nullable(Date),
    update_date Nullable(Date),
    quality_flags String,
    payload_json String,
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (stock_code, file_register_date, report_idx);

CREATE TABLE IF NOT EXISTS real_consensus_estimates
(
    security_id String,
    stock_code String,
    file_register_date Date DEFAULT toDate('1970-01-01'),
    file_year UInt16 DEFAULT 0,
    report_idx UInt64 DEFAULT 0,
    broker_code LowCardinality(String),
    broker_name String,
    analyst_name String,
    as_of_date Date DEFAULT toDate('1970-01-01'),
    target_period LowCardinality(String),
    metric_id LowCardinality(String),
    estimate_value Nullable(Float64),
    currency LowCardinality(String),
    source_field LowCardinality(String),
    source_provider LowCardinality(String),
    quality_flags String,
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY (stock_code, target_period, metric_id, broker_code, as_of_date, report_idx, source_field);

CREATE TABLE IF NOT EXISTS real_consensus_daily
(
    security_id String,
    stock_code String,
    as_of_date Date,
    target_period LowCardinality(String),
    metric_id LowCardinality(String),
    consensus_mean Nullable(Float64),
    consensus_median Nullable(Float64),
    consensus_low Nullable(Float64),
    consensus_high Nullable(Float64),
    report_count UInt32,
    broker_count UInt32,
    currency LowCardinality(String),
    source_provider LowCardinality(String),
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(as_of_date)
ORDER BY (stock_code, as_of_date, target_period, metric_id);

CREATE TABLE IF NOT EXISTS arcana.fact_daily_style_score
(
    trade_date Date,
    security_id String,
    issuer_id String DEFAULT '',
    stock_code FixedString(64) DEFAULT '',
    country LowCardinality(String) DEFAULT '',
    market_mic LowCardinality(String) DEFAULT '',
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

CREATE TABLE IF NOT EXISTS factor_lab_experiment
(
    experiment_id UUID,
    name String,
    graph_json String,
    final_node_id String,
    market LowCardinality(String),
    created_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3),
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
ORDER BY experiment_id;

CREATE TABLE IF NOT EXISTS factor_lab_run
(
    run_id UUID,
    experiment_id Nullable(UUID),
    graph_hash String,
    status LowCardinality(String),
    start_date Date,
    end_date Date,
    error String,
    started_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3),
    finished_at Nullable(DateTime64(3, 'Asia/Seoul'))
)
ENGINE = ReplacingMergeTree(started_at)
ORDER BY run_id;

CREATE TABLE IF NOT EXISTS factor_lab_node_cache
(
    run_id UUID,
    node_id String,
    trade_date Date,
    security_id String,
    value Nullable(Float64),
    is_valid Bool,
    invalid_reason String,
    created_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = MergeTree
PARTITION BY toYYYYMM(trade_date)
ORDER BY (run_id, node_id, trade_date, security_id);

CREATE TABLE IF NOT EXISTS factor_lab_values
(
    security_id String,
    trade_date Date,
    factor_id LowCardinality(String),
    financial_basis LowCardinality(String) DEFAULT 'lab',
    factor_value Nullable(Float64),
    fiscal_year Nullable(UInt16),
    financial_period Nullable(Date),
    currency LowCardinality(String) DEFAULT '',
    run_id UUID,
    node_id String,
    is_valid Bool,
    invalid_reason String,
    updated_at DateTime64(3, 'Asia/Seoul') DEFAULT now64(3)
)
ENGINE = ReplacingMergeTree(updated_at)
PARTITION BY toYYYYMM(trade_date)
ORDER BY (factor_id, trade_date, security_id);
