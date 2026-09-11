"""Record exact native corrections and preserved unpriced source observations."""
import argparse
from hashlib import sha256
import json
from pathlib import Path

import numpy as np
import pandas as pd

KEYS = ["security_id", "trade_date", "factor_id", "financial_basis"]
NATIVE = KEYS + ["factor_value", "fiscal_year", "financial_period", "currency", "updated_at"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--classification", type=Path, required=True)
    parser.add_argument("--public-input-audit", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    silver = Path(__file__).resolve().parents[2] / "data-lake/silver"
    if not args.output.resolve().is_relative_to(silver.resolve()):
        raise ValueError("Review artifacts must be in silver")
    hashes = {}

    def check(path, expected=None):
        path = Path(path).resolve()
        raw = path.read_bytes()
        digest = sha256(raw).hexdigest()
        if expected is not None and digest != expected:
            raise ValueError(f"Changed evidence: {path}")
        hashes[str(path)] = digest
        return raw

    prep = json.loads(check(args.preparation))
    classified = json.loads(check(args.classification))
    public = json.loads(check(args.public_input_audit))
    prep_sha = hashes[str(args.preparation.resolve())]
    if (classified["preparation_checkpoint_sha256"] != prep_sha
            or classified["classified_months"] != classified["requested_months"]
            or public["input_hashes"].get(str(args.preparation.resolve())) != prep_sha
            or public["status"] != "current_public_market_inputs_verified"
            or public["verified_rows"] != prep["totals"]["differing_native_values"]):
        raise ValueError("Complete public-input and gap evidence is required")
    if any(prep["totals"][key] for key in ("source_without_price", "price_discrepancies", "price_ambiguities", "native_ambiguities")):
        raise ValueError("Unresolved source or native ambiguity")
    for name, expected in public["input_hashes"].items():
        path = Path(name)
        if expected is None:
            if path.exists():
                raise ValueError("A previously absent public input appeared after verification")
            hashes[str(path.resolve())] = None
        else:
            check(path, expected)
    quarantine_path = Path(classified["quarantine_path"])
    check(quarantine_path, classified["quarantine_sha256"])
    quarantine = pd.read_parquet(quarantine_path)
    quarantine.trade_date = pd.to_datetime(quarantine.trade_date).astype("datetime64[ns]")
    if quarantine.duplicated(KEYS[:2]).any():
        raise ValueError("Ambiguous retained quarantine keys")
    for source in quarantine[["source_path", "source_sha256"]].drop_duplicates().to_dict("records"):
        source_path = silver.parent / source["source_path"]
        if not source_path.resolve().is_relative_to((silver.parent / "bronze").resolve()):
            raise ValueError("Original quarantine provenance must remain in bronze")
        check(source_path, source["source_sha256"])
    path = args.public_input_audit.parent / "all_discrepancies.parquet"
    check(path, public["output_sha256"])
    evidence = pd.read_parquet(path)
    if evidence.duplicated(KEYS).any() or not evidence[["public_matches_original", "prepared_matches_original", "close_matches_original", "shares_match_original"]].all().all():
        raise ValueError("Unverified public calculation rows")
    corrections, preserved, exclusions = [], [], []
    for month in prep["months"]:
        folder = args.preparation.parent / month["month"]
        path = folder / "native_value_discrepancies.parquet"
        check(path, month["hashes"][path.name])
        correction = pd.read_parquet(path)
        if len(correction):
            compared = evidence.loc[evidence.trade_date.dt.strftime("%Y-%m").eq(month["month"])]
            columns = NATIVE + ["factor_value_expected"]
            pd.testing.assert_frame_equal(correction[columns].sort_values(KEYS).reset_index(drop=True),
                compared[columns].sort_values(KEYS).reset_index(drop=True), check_exact=True, check_dtype=False)
            corrections.append(correction[columns])
        for name in ("native_outside_prepared", "price_without_source"):
            if not month[name]:
                continue
            path = folder / (name + ".parquet")
            check(path, month["hashes"][path.name])
            before = pd.read_parquet(path)
            before.trade_date = pd.to_datetime(before.trade_date).astype("datetime64[ns]")
            proof = before.merge(quarantine, on=KEYS[:2], how="left", validate="many_to_one", indicator=True)
            if not proof._merge.eq("both").all():
                raise ValueError("Unexplained observations cannot be preserved by this review")
            if name.startswith("native"):
                expected = np.where(proof.factor_id.eq("csho"), proof.shares, proof.market_cap / 1e6)
                if not np.isclose(proof.factor_value, expected, rtol=1e-10, atol=1e-8).all():
                    raise ValueError("Existing unpriced-observation factors differ from original reported values")
                preserved.append(before[NATIVE])
            else:
                if (not np.isclose(pd.to_numeric(proof.close), proof.raw_close, rtol=1e-10, atol=1e-6, equal_nan=True).all()
                        or not np.isclose(pd.to_numeric(proof.volume), proof.raw_volume, rtol=1e-10, atol=1e-6, equal_nan=True).all()):
                    raise ValueError("An unexplained source-price change remains")
                exclusions.append(before)
    frames = dict(corrections=pd.concat(corrections, ignore_index=True),
        preserved_unpriced_native=pd.concat(preserved, ignore_index=True),
        excluded_price_observations=pd.concat(exclusions, ignore_index=True))
    expected_counts = (prep["totals"]["differing_native_values"], prep["totals"]["native_outside_prepared"], prep["totals"]["price_without_source"])
    if tuple(map(len, frames.values())) != expected_counts:
        raise ValueError("Review scope is incomplete")
    check(Path(__file__))
    args.output.mkdir(parents=True, exist_ok=False)
    artifacts = {}
    for name, frame in frames.items():
        path = args.output / (name + ".parquet")
        frame.to_parquet(path, index=False)
        artifacts[name] = dict(path=str(path.resolve()), sha256=sha256(path.read_bytes()).hexdigest(), rows=len(frame))
    record = dict(status="native_dispositions_verified", preparation_sha256=prep_sha, artifacts=artifacts,
        input_hashes=hashes, native_published=False, coverage_complete=False,
        policy="Correct only the exact differing market values verified against current public inputs and dated originals. Preserve original reported shares/capitalization on unpriced observations without declaring their prices tradable. All other native values, metadata and timestamps must remain unchanged.")
    (args.output / "review.json").write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", "utf-8")
    print(dict(status=record["status"], counts={name: item["rows"] for name, item in artifacts.items()}), flush=True)


if __name__ == "__main__":
    main()
