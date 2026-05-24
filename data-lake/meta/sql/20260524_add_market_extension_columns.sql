ALTER TABLE arcana.security_master
    ADD COLUMN IF NOT EXISTS country LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS primary_market_mic LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS currency LowCardinality(String) DEFAULT '';

ALTER TABLE arcana.fact_daily_factor_score
    ADD COLUMN IF NOT EXISTS country LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS market_mic LowCardinality(String) DEFAULT '';

ALTER TABLE arcana.fact_daily_style_score
    ADD COLUMN IF NOT EXISTS country LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS market_mic LowCardinality(String) DEFAULT '';

ALTER TABLE arcana.dart_report_metadata
    ADD COLUMN IF NOT EXISTS country LowCardinality(String) DEFAULT 'KR',
    ADD COLUMN IF NOT EXISTS market_mic LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS filing_system LowCardinality(String) DEFAULT 'DART';

ALTER TABLE arcana.benchmark_price_daily
    ADD COLUMN IF NOT EXISTS country LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS market_mic LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS benchmark_family LowCardinality(String) DEFAULT '';

ALTER TABLE benchmark_price_daily
    ADD COLUMN IF NOT EXISTS country LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS market_mic LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS benchmark_family LowCardinality(String) DEFAULT '';

