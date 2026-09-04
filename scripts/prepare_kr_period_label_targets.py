from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE


DEFAULT_AUDIT_PATH = DATA_LAKE.meta("kr_dart_period_column_audit.csv")
DEFAULT_TARGET_PATH = DATA_LAKE.meta("kr_dart_period_column_factor_targets.csv")
DEFAULT_COUNTS_PATH = DATA_LAKE.meta("kr_dart_period_column_predelete_counts.csv")
FACTOR_TABLES = ("fact_daily_factors", "fact_daily_factor_snapshot")
SCORE_TABLES = ("fact_daily_factor_score", "fact_daily_style_score")


def prepare_targets(
    *, audit_path: Path, target_path: Path, counts_path: Path
) -> pd.DataFrame:
    audit = pd.read_csv(audit_path, dtype={"symbol": str})
    errors = audit["error"].fillna("").astype(str).str.strip()
    if errors.astype(bool).any():
        raise RuntimeError(f"KR audit has {int(errors.astype(bool).sum())} unresolved errors")
    affected = audit["affected"].astype(str).str.strip().str.lower().isin({"1", "true"})
    symbols = sorted(
        {
            str(value).strip().zfill(6)
            for value in audit.loc[affected, "symbol"].dropna()
            if str(value).strip()
        }
    )
    if not symbols:
        raise ValueError("KR audit contains no affected symbols")
    candidate_ids = [f"SEC_KR_{symbol}" for symbol in symbols]

    client = get_clickhouse_client(send_receive_timeout=3_600)
    try:
        priced_ids = {
            str(row[0])
            for row in client.query(
                """
SELECT DISTINCT security_id
FROM price_daily
WHERE security_id IN {security_ids:Array(String)}
""".strip(),
                parameters={"security_ids": candidate_ids},
            ).result_rows
        }
        targets = pd.DataFrame(
            [
                {"symbol": security_id.removeprefix("SEC_KR_"), "security_id": security_id}
                for security_id in candidate_ids
                if security_id in priced_ids
            ]
        )
        if targets.empty:
            raise RuntimeError("no affected KR symbols have price_daily coverage")
        security_ids = targets["security_id"].tolist()
        params = {"security_ids": security_ids}
        count_rows: list[dict[str, object]] = []
        for table in FACTOR_TABLES:
            rows = client.query(
                f"""
SELECT financial_basis, count()
FROM {table}
WHERE security_id IN {{security_ids:Array(String)}}
GROUP BY financial_basis
ORDER BY financial_basis
""".strip(),
                parameters=params,
            ).result_rows
            count_rows.extend(
                {"table": table, "financial_basis": basis, "row_count": int(count)}
                for basis, count in rows
            )
        for table in SCORE_TABLES:
            count = client.query(
                f"SELECT count() FROM {table} "
                "WHERE security_id IN {security_ids:Array(String)}",
                parameters=params,
            ).first_item
            if isinstance(count, dict):
                count = next(iter(count.values()))
            count_rows.append(
                {"table": table, "financial_basis": "all", "row_count": int(count)}
            )
    finally:
        client.close()

    target_path.parent.mkdir(parents=True, exist_ok=True)
    targets.to_csv(target_path, index=False, encoding="utf-8-sig")
    pd.DataFrame(count_rows).to_csv(counts_path, index=False, encoding="utf-8-sig")
    print(
        f"[DONE] KR targets affected={len(symbols)}, priced={len(targets)}, "
        f"without_price={len(symbols) - len(targets)}, targets={target_path}, "
        f"predelete_counts={counts_path}",
        flush=True,
    )
    return targets


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build exact KR factor targets and capture affected-row predelete counts."
    )
    parser.add_argument("--audit", type=Path, default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGET_PATH)
    parser.add_argument("--counts", type=Path, default=DEFAULT_COUNTS_PATH)
    args = parser.parse_args()
    prepare_targets(
        audit_path=args.audit,
        target_path=args.targets,
        counts_path=args.counts,
    )


if __name__ == "__main__":
    main()
