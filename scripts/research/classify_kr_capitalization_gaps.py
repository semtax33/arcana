"""Classify read-only market-factor gaps against retained source quarantine rows."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

KEYS = ["security_id", "trade_date"]


def checked(path, expected):
    raw = path.read_bytes()
    if sha256(raw).hexdigest() != expected:
        raise ValueError(f"Evidence changed: {path}")
    return pd.read_parquet(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--quarantine", type=Path, required=True)
    parser.add_argument("--quarantine-sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2] / "data-lake"
    if not args.output.resolve().is_relative_to((root / "silver").resolve()):
        raise ValueError("Classification evidence must remain in silver")
    preparation_bytes = args.preparation.read_bytes()
    preparation = json.loads(preparation_bytes)
    quarantine = checked(args.quarantine, args.quarantine_sha256)
    quarantine["trade_date"] = pd.to_datetime(quarantine.trade_date).astype("datetime64[ns]")
    if quarantine.duplicated(KEYS).any():
        raise ValueError("Ambiguous quarantine keys")
    args.output.mkdir(parents=True, exist_ok=False)
    (args.output / "preparation_checkpoint.json").write_bytes(preparation_bytes)
    months, evidence = [], {}
    for month in preparation["months"]:
        folder = args.preparation.parent / month["month"]
        result = {"month": month["month"]}
        for name in ("price_without_source", "native_outside_prepared"):
            path = folder / (name + ".parquet")
            frame = checked(path, month["hashes"][path.name])
            evidence[str(path.resolve())] = month["hashes"][path.name]
            result[name] = len(frame)
            if frame.empty:
                continue
            frame["trade_date"] = pd.to_datetime(frame.trade_date).astype("datetime64[ns]")
            joined = frame.merge(quarantine, on=KEYS, how="left", validate="many_to_one", indicator=True)
            matched = joined._merge.eq("both")
            joined["matches_quarantined_key"] = matched
            joined["disposition"] = "unmatched_requires_review"
            joined.loc[matched, "disposition"] = "quarantined_original_requires_review"
            if name == "price_without_source":
                original = pd.to_numeric(joined.raw_close, errors="coerce")
                current = pd.to_numeric(joined.close, errors="coerce")
                joined["close_matches_original"] = matched & np.isclose(current, original, rtol=1e-10, atol=1e-6, equal_nan=True)
                joined["missing_original_close_stored_as_zero"] = matched & original.isna() & current.eq(0)
                joined["original_has_positive_close"] = matched & original.gt(0)
                result["close_matches_original"] = int(joined.close_matches_original.sum())
                result["missing_original_close_stored_as_zero"] = int(joined.missing_original_close_stored_as_zero.sum())
            else:
                values = pd.to_numeric(joined.factor_value, errors="coerce")
                shares = pd.to_numeric(joined.shares, errors="coerce")
                supported_shares = matched & joined.factor_id.eq("csho") & shares.gt(0) & np.isclose(values, shares, rtol=1e-10, atol=1e-6)
                joined["shares_match_quarantined_source"] = supported_shares
                joined.loc[supported_shares, "disposition"] = "reported_shares_match_but_price_observation_remains_quarantined"
                result["shares_match_quarantined_source"] = int(supported_shares.sum())
                result["other_native_rows_require_review"] = int((~supported_shares).sum())
            result[name + "_quarantined"] = int(matched.sum())
            result[name + "_unmatched"] = int((~matched).sum())
            target = args.output / month["month"] / path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            joined.drop(columns="_merge").to_parquet(target, index=False)
        months.append(result)
    totals = {key: sum(item.get(key, 0) for item in months) for key in sorted({k for m in months for k in m} - {"month"})}
    result = dict(status="classified_requires_review", production_changed=False, coverage_complete=False,
        preparation_status=preparation["status"], classified_months=len(months), requested_months=len(preparation["requested_months"]),
        preparation_path=str(args.preparation.resolve()), preparation_checkpoint_sha256=sha256(preparation_bytes).hexdigest(),
        quarantine_path=str(args.quarantine.resolve()), quarantine_sha256=args.quarantine_sha256,
        implementation_sha256=sha256(Path(__file__).read_bytes()).hexdigest(),
        policy="A quarantine key match explains a gap; it does not approve prices, revoke reported shares, or complete lifecycle coverage.",
        totals=totals, months=months, input_hashes=evidence)
    (args.output / "summary.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(json.dumps({key: result[key] for key in ("status", "classified_months", "requested_months", "totals")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
