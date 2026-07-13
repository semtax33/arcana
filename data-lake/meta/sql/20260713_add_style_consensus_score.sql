ALTER TABLE arcana.fact_daily_style_score
    ADD COLUMN IF NOT EXISTS consensus_score Nullable(Float64) AFTER growth_score;
