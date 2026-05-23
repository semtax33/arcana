ALTER TABLE issuers
    RENAME COLUMN industry_code TO sector_code;

ALTER TABLE issuers
    ADD COLUMN IF NOT EXISTS industry_group_code LowCardinality(String) DEFAULT '',
    ADD COLUMN IF NOT EXISTS industry_group_name String DEFAULT '';
