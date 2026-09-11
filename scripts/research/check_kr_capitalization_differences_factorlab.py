"""Observe original-vs-native discrepancies through the actual FactorLab query."""
import argparse
import json
from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from api.repository.factor_lab_query import compile_factor_lab_graph
from engine.core.clickhouse import get_clickhouse_client
from engine.core.paths import DATA_LAKE
from engine.core.source_storage import sha256_file
from engine.core.serving_storage import export_json

KEYS = ["security_id", "trade_date", "factor_id", "financial_basis"]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preparation", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Consumer evidence must remain in silver")
    args.output.mkdir(parents=True, exist_ok=False)
    report = json.loads(args.preparation.read_bytes())
    evidence_hashes = {str(args.preparation.resolve()): sha256_file(args.preparation), str(Path(__file__).resolve()): sha256_file(Path(__file__))}
    frames = []
    for month in report["months"]:
        if month["differing_native_values"]:
            path = args.preparation.parent / month["month"] / "native_value_discrepancies.parquet"
            expected = month["hashes"][path.name]
            if sha256_file(path) != expected:
                raise ValueError("Original/native comparison changed")
            evidence_hashes[str(path.resolve())] = expected
            frames.append(pd.read_parquet(path))
    originals = pd.concat(frames, ignore_index=True)
    originals.trade_date = pd.to_datetime(originals.trade_date).astype("datetime64[ns]")
    output = dict(status="running", production_changed=False, recipe_or_holdout_changed=False,
        expected_rows=len(originals), input_hashes=evidence_hashes, groups=[])
    client = get_clickhouse_client(connect_timeout=5, send_receive_timeout=40)
    details = []
    try:
        for (factor, basis), expected in originals.groupby(["factor_id", "financial_basis"], sort=True):
            dates = sorted(expected.trade_date.dt.strftime("%Y-%m-%d").unique())
            graph = {"version": 2, "experiment": {"name": "native_capitalization_differences", "market": "KR", "start_date": dates[0], "end_date": dates[-1]},
                "nodes": [{"id": "input", "type": "factor_input", "version": 1,
                    "config": {"factor_id": factor, "financial_basis": basis, "missing_policy": "drop"}}],
                "edges": [], "outputs": {"final_node_id": "input", "evaluation_node_ids": []}}
            compiled = compile_factor_lab_graph(graph, known_factor_ids={factor}, trade_dates=dates,
                factor_table="fact_daily_factors", listing_table="security_listing_episodes")
            query = compiled.query + "\nSETTINGS max_execution_time=30,max_threads=2"
            folder = args.output / f"{factor}_{basis}"
            folder.mkdir()
            (folder / "query.sql").write_text(query, "utf-8")
            export_json(folder / "graph.json", graph)
            export_json(folder / "parameters.json", compiled.parameters)
            actual = client.query_df(query, parameters=compiled.parameters)
            if actual.empty:
                actual = pd.DataFrame(columns=["security_id", "trade_date", "value", "is_valid"])
            actual.trade_date = pd.to_datetime(actual.trade_date).astype("datetime64[ns]")
            actual.to_parquet(folder / "factorlab_output.parquet", index=False)
            if actual.duplicated(KEYS[:2]).any():
                raise ValueError("FactorLab returned ambiguous security-date values")
            joined = expected.merge(actual[["security_id", "trade_date", "value", "is_valid"]], on=KEYS[:2],
                how="left", validate="one_to_one", indicator=True)
            joined["visible_in_factorlab"] = joined._merge.eq("both")
            joined["matches_original"] = joined.visible_in_factorlab & np.isclose(
                joined.value.astype(float), joined.factor_value_expected.astype(float), rtol=1e-10, atol=1e-8, equal_nan=False)
            joined["matches_previous_native"] = joined.visible_in_factorlab & np.isclose(
                joined.value.astype(float), joined.factor_value.astype(float), rtol=1e-10, atol=1e-8, equal_nan=False)
            joined.drop(columns="_merge").to_parquet(folder / "comparison.parquet", index=False)
            details.append(joined.drop(columns="_merge"))
            record = dict(factor_id=factor, financial_basis=basis, expected_rows=len(joined),
                visible_rows=int(joined.visible_in_factorlab.sum()), matching_original_rows=int(joined.matches_original.sum()),
                matching_previous_native_rows=int(joined.matches_previous_native.sum()))
            output["groups"].append(record)
            export_json(args.output / "summary.json", output)
            print(record, flush=True)
    finally:
        client.close()
    all_rows = pd.concat(details, ignore_index=True)
    all_rows.to_parquet(args.output / "comparison.parquet", index=False)
    for name, expected in evidence_hashes.items():
        if sha256_file(Path(name)) != expected:
            raise ValueError("Consumer reference changed")
    output.update(status="visible_values_match_originals" if all_rows.loc[all_rows.visible_in_factorlab, "matches_original"].all() else "original_value_discrepancies_observed",
        visible_rows=int(all_rows.visible_in_factorlab.sum()), missing_rows=int((~all_rows.visible_in_factorlab).sum()),
        matching_original_rows=int(all_rows.matches_original.sum()), comparison_sha256=sha256_file(args.output / "comparison.parquet"),
        coverage_complete=False, policy="Missing dated-universe members remain unverified; visible-value agreement alone does not complete lifecycle coverage.")
    export_json(args.output / "summary.json", output)
    print({k: output[k] for k in ("status", "visible_rows", "missing_rows", "matching_original_rows")}, flush=True)


if __name__ == "__main__":
    main()
