ALTER TABLE arcana.security_master
    ADD COLUMN IF NOT EXISTS exchange_code LowCardinality(String) DEFAULT '';
