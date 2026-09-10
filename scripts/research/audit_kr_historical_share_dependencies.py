"""Audit every factor contract before/after restored historical share inputs.

This command does not modify provider inputs or ClickHouse. Changes are
counterfactual candidates, not automatic approval of their financial inputs.
"""
import argparse
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import json
import multiprocessing
from pathlib import Path
import shutil
import sys
import threading
from time import monotonic
import traceback
import warnings

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from engine.core.paths import DATA_LAKE
from engine.core.serving_storage import export_json
from engine.core.source_storage import SourceRefreshLock
from engine.transformers.factors import FactorMarketDataCache, create_stock_factor_dataframe, preferred_factor_columns, read_restored_market_shares
from engine.transformers.share_input_history import file_digest

BASES = ("annual", "quarterly", "ttm")


def configure_warnings():
    warnings.filterwarnings("ignore", category=pd.errors.PerformanceWarning)
    warnings.filterwarnings("ignore", category=FutureWarning, message="Downcasting behavior in `replace` is deprecated.*")


class CounterfactualShares:
    """Use the old default shares while retaining the actual source precedence."""
    def __init__(self, current, old_path, end_date):
        self.current = current
        self.old = FactorMarketDataCache(market="kr", shares_path=old_path, end_date=end_date)

    def shares(self, security_id):
        restored = read_restored_market_shares(security_id.removeprefix("SEC_KR_"), "kr")
        return restored if restored is not None else self.old.shares(security_id)

    def __getattr__(self, name):
        return getattr(self.current, name)


class ReadEvidence:
    """Record files actually opened, and prevent this audit from writing inputs."""
    def __init__(self, output):
        self.output = output.resolve()
        self.root = DATA_LAKE.root.resolve()
        self.inputs = {}
        self.lock = threading.Lock()
        self.enabled = True

    def hook(self, event, args):
        if not self.enabled or event != "open" or not isinstance(args[0], (str, bytes)):
            return
        path = Path(args[0].decode() if isinstance(args[0], bytes) else args[0]).resolve()
        if not path.is_relative_to(self.root) or path.is_relative_to(self.output):
            return
        mode = args[1]
        if isinstance(mode, str) and any(letter in mode for letter in "wax+"):
            # The market lock belongs to the command's own context manager.
            if path.name.endswith(".lock"):
                return
            raise RuntimeError(f"Read-only audit attempted to write an input: {path}")
        try:
            stat = path.stat()
        except FileNotFoundError:
            return
        if path.is_file():
            with self.lock:
                previous = self.inputs.setdefault(str(path), dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns))
                if previous != dict(size=stat.st_size, mtime_ns=stat.st_mtime_ns):
                    raise RuntimeError(f"Calculation input changed during audit: {path}")

    def snapshot(self):
        with self.lock:
            return dict(self.inputs)


def compare_security(sid, start, observation_rows, before_cache, after_cache, *, end_date, output, factors):
    symbol = sid.removeprefix("SEC_KR_")
    folder = output / "securities" / symbol
    folder.mkdir(parents=True)
    record = dict(security_id=sid, symbol=symbol, from_date=start, added_observations=observation_rows, bases={})
    try:
        for basis in BASES:
            kwargs = dict(start_date=start, end_date=end_date, financial_basis=basis, market="kr",
                use_edgartools=False, require_report_metadata=True, wacc_online_backfill=False)
            before = create_stock_factor_dataframe(symbol, market_data_cache=before_cache, **kwargs)
            after = create_stock_factor_dataframe(symbol, market_data_cache=after_cache, **kwargs)
            if before.empty or after.empty:
                if before.empty != after.empty:
                    raise ValueError("Share-only counterfactual changed the available price history")
                record["bases"][basis] = dict(status="no_price_history", rows=0, changes={}, missing_contracts=factors)
                continue
            if before.trade_date.tolist() != after.trade_date.tolist() or set(after.security_id) != {sid}:
                raise ValueError("Share-only counterfactual changed security/date identities")
            changes, parts = {}, []
            for factor in factors:
                left = pd.to_numeric(before[factor], errors="coerce").to_numpy(float) if factor in before else np.full(len(before), np.nan)
                right = pd.to_numeric(after[factor], errors="coerce").to_numpy(float) if factor in after else np.full(len(after), np.nan)
                mask = ~np.isclose(left, right, rtol=1e-12, atol=0, equal_nan=True)
                if not mask.any():
                    continue
                columns = [c for c in ("security_id", "trade_date", "fiscal_year", "financial_period", "report_date", "financial_available_date", "currency") if c in after]
                part = after.loc[mask, columns].copy()
                part["financial_basis"] = basis
                part["factor_id"] = factor
                part["before_value"] = left[mask]
                part["after_value"] = right[mask]
                parts.append(part)
                changes[factor] = dict(rows=int(mask.sum()), from_date=str(part.trade_date.min().date()),
                    through_date=str(part.trade_date.max().date()), new_finite=int((~np.isfinite(left) & np.isfinite(right)).sum()),
                    withdrawn=int((np.isfinite(left) & ~np.isfinite(right)).sum()))
            delta = pd.concat(parts, ignore_index=True) if parts else pd.DataFrame(columns=["security_id", "trade_date", "financial_basis", "factor_id", "before_value", "after_value"])
            target = folder / f"{basis}_changes.parquet"
            delta.to_parquet(target, index=False)
            record["bases"][basis] = dict(status="compared", rows=len(after), changes=changes,
                produced_contracts=len(set(factors) & set(after.columns)), missing_contracts=sorted(set(factors) - set(after.columns)),
                delta_path=str(target.resolve()), delta_sha256=file_digest(target))
        record["status"] = "compared" if all(item["status"] == "compared" for item in record["bases"].values()) else "no_price_history"
    except Exception as error:
        record.update(status="calculation_failed", error_type=type(error).__name__, error=str(error))
        (folder / "error.txt").write_text(traceback.format_exc(), "utf-8")
    export_json(folder / "summary.json", record)
    return record


def initialize_worker(old, end_date, output, factors):
    global WORKER_BEFORE, WORKER_AFTER, WORKER_EVIDENCE, WORKER_CONFIG, WORKER_REPORTED_INPUTS
    configure_warnings()
    WORKER_CONFIG = dict(end_date=end_date, output=Path(output), factors=factors)
    WORKER_EVIDENCE = ReadEvidence(Path(output))
    sys.addaudithook(WORKER_EVIDENCE.hook)
    WORKER_AFTER = FactorMarketDataCache(market="kr", end_date=end_date)
    WORKER_BEFORE = CounterfactualShares(WORKER_AFTER, Path(old), end_date)
    WORKER_REPORTED_INPUTS = set()


def calculate_in_process(item):
    config = dict(WORKER_CONFIG)
    if item.get("execution_validation"):
        config["output"] = config["output"] / "execution_validation"
    result = compare_security(item["security_id"], str(item["from_date"].date()), item["added_rows"],
        WORKER_BEFORE, WORKER_AFTER, **config)
    inputs = WORKER_EVIDENCE.snapshot()
    new = {name: value for name, value in inputs.items() if name not in WORKER_REPORTED_INPUTS}
    WORKER_REPORTED_INPUTS.update(new)
    return result, new, item.get("execution_validation", False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--share-publication", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--end-date", default="2026-09-04")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--execution", choices=("process", "thread"), default="process")
    parser.add_argument("--reuse-completed", type=Path)
    args = parser.parse_args()
    if not args.output.resolve().is_relative_to((DATA_LAKE.root / "silver").resolve()):
        raise ValueError("Counterfactual evidence must be stored in silver")
    args.output.mkdir(parents=True, exist_ok=False)
    configure_warnings()
    publication = json.loads(args.share_publication.read_text("utf-8"))
    old = args.share_publication.parent / "default_before.csv"
    new = Path(publication["default_input_path"])
    added = args.share_publication.parent / "added_observations.parquet"
    if file_digest(new) != publication["default_input_sha256"] or file_digest(added) != publication["added_observations_sha256"]:
        raise ValueError("Historical share publication no longer matches the live input or its delta")
    source_audit = json.loads(Path(publication["source_audit_path"]).read_text("utf-8"))
    if file_digest(old) != source_audit["default_share_input_sha256"]:
        raise ValueError("Counterfactual preimage does not match the original observation audit")
    observations = pd.read_parquet(added)
    scope = observations.groupby("security_id").agg(from_date=("trade_date", "min"), added_rows=("trade_date", "size"), priority_cap=("market_cap", "max"))
    scope = scope.sort_values("priority_cap", ascending=False).reset_index()
    scope.to_parquet(args.output / "scope.parquet", index=False)
    factors = preferred_factor_columns()
    assert len(factors) == 293 and not any(name.startswith("lab_") for name in factors)
    report = dict(status="running", started_at=datetime.now(timezone.utc).isoformat(),
        source_cutoff="2026-09-10", calculation_end=args.end_date, scope_securities=len(scope), scope_observations=len(observations),
        factor_contracts=factors, bases=list(BASES), before_sha256=file_digest(old), after_sha256=file_digest(new),
        share_publication_path=str(args.share_publication.resolve()), share_publication_sha256=file_digest(args.share_publication),
        completed=0, failures=0, no_price_history=0, by_factor={}, results=[], native_published=False,
        snapshots_published=False, whole_market_survivorship_complete=False,
        policy="Counterfactual changes from the restored default shares only; actual restored issuer-panel precedence retained. Missing financial inputs remain missing. No new issuer/listing approval or strategy evaluation.")
    report.update(execution=args.execution, workers=args.workers)
    code = {}
    for module in list(sys.modules.values()):
        file = getattr(module, "__file__", None)
        if file and Path(file).suffix == ".py" and Path(file).resolve().is_relative_to(ROOT / "engine"):
            path = Path(file).resolve()
            destination = args.output / "implementation" / path.relative_to(ROOT)
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, destination)
            code[str(path)] = file_digest(path)
    shutil.copyfile(__file__, args.output / "implementation" / Path(__file__).name)
    report["implementation_sha256"] = code
    export_json(args.output / "summary.json", report)
    evidence = ReadEvidence(args.output)
    started = monotonic()
    with SourceRefreshLock("kr", data_lake_root=DATA_LAKE.root):
        remaining = scope.to_dict("records")
        def accept(result):
            report["completed"] += 1
            report["failures"] += int(result["status"] == "calculation_failed")
            report["no_price_history"] += int(result["status"] == "no_price_history")
            report["results"].append(dict(symbol=result["symbol"], status=result["status"],
                summary_sha256=file_digest(args.output / "securities" / result["symbol"] / "summary.json")))
            for basis, item in result["bases"].items():
                for factor, change in item["changes"].items():
                    total = report["by_factor"].setdefault(factor, {}).setdefault(basis, dict(rows=0, securities=0, new_finite=0, withdrawn=0, from_date=change["from_date"], through_date=change["through_date"]))
                    for name in ("rows", "new_finite", "withdrawn"):
                        total[name] += change[name]
                    total["securities"] += 1
                    total["from_date"] = min(total["from_date"], change["from_date"])
                    total["through_date"] = max(total["through_date"], change["through_date"])
            if report["completed"] == 1 or report["completed"] % 5 == 0:
                report["seconds"] = round(monotonic()-started, 1)
                export_json(args.output / "summary.json", report)
                export_json(args.output / "input_stats.json", evidence.snapshot())
                print(json.dumps({name: report[name] for name in ("completed", "scope_securities", "failures", "no_price_history", "seconds")}), flush=True)
        reused = {}
        if args.reuse_completed:
            prior = json.loads((args.reuse_completed / "summary.json").read_text("utf-8"))
            if prior["status"] != "stopped_for_process_parallelism" or any(prior[key] != report[key] for key in ("before_sha256", "after_sha256", "calculation_end", "factor_contracts", "bases", "implementation_sha256")):
                raise ValueError("Prior completed results have incompatible inputs or calculation code")
            evidence.inputs.update(json.loads((args.reuse_completed / "input_stats.json").read_text("utf-8")))
            for name, stat in evidence.inputs.items():
                current = Path(name).stat()
                if (current.st_size, current.st_mtime_ns) != (stat["size"], stat["mtime_ns"]):
                    raise ValueError(f"A prior calculation input changed: {name}")
            for item in prior["results"]:
                if item["status"] != "compared":
                    continue
                folder = args.reuse_completed / "securities" / item["symbol"]
                if file_digest(folder / "summary.json") != item["summary_sha256"]:
                    raise ValueError("Prior security summary changed")
                result = json.loads((folder / "summary.json").read_text("utf-8"))
                for entry in result["bases"].values():
                    if file_digest(entry["delta_path"]) != entry["delta_sha256"]:
                        raise ValueError("Prior counterfactual values changed")
                shutil.copytree(folder, args.output / "securities" / item["symbol"])
                reused[item["symbol"]] = result
                accept(result)
            report["reused_completed"] = len(reused)
            report["reuse_source"] = str(args.reuse_completed.resolve())
            remaining = [item for item in remaining if item["security_id"].removeprefix("SEC_KR_") not in reused]
        if args.execution == "process":
            with ProcessPoolExecutor(max_workers=max(1, args.workers), mp_context=multiprocessing.get_context("spawn"),
                initializer=initialize_worker, initargs=(str(old), args.end_date, str(args.output), factors)) as pool:
                jobs = remaining
                if reused:
                    validate_sid = "SEC_KR_" + next(iter(reused))
                    validation = scope.loc[scope.security_id.eq(validate_sid)].iloc[0].to_dict()
                    jobs = [dict(**validation, execution_validation=True)] + jobs
                futures = [pool.submit(calculate_in_process, item) for item in jobs]
                for future in as_completed(futures):
                    result, new_inputs, validation = future.result()
                    for name, value in new_inputs.items():
                        if evidence.inputs.setdefault(name, value) != value:
                            raise ValueError(f"Process workers read different input versions: {name}")
                    if validation:
                        if result["status"] != "compared" or any(result["bases"][basis]["delta_sha256"] != reused[result["symbol"]]["bases"][basis]["delta_sha256"] for basis in BASES):
                            raise ValueError("Process execution changed counterfactual values")
                        report["process_execution_equivalence"] = dict(symbol=result["symbol"], bases=list(BASES), all_delta_hashes_identical=True)
                        export_json(args.output / "execution_validation.json", report["process_execution_equivalence"])
                    else:
                        accept(result)
        else:
            sys.addaudithook(evidence.hook)
            after_cache = FactorMarketDataCache(market="kr", end_date=args.end_date)
            before_cache = CounterfactualShares(after_cache, old, args.end_date)
            def calculate(item):
                return compare_security(item["security_id"], str(item["from_date"].date()), item["added_rows"],
                    before_cache, after_cache, end_date=args.end_date, output=args.output, factors=factors)
            if remaining:
                accept(calculate(remaining[0]))
                with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                    futures = [pool.submit(calculate, item) for item in remaining[1:]]
                    for future in as_completed(futures):
                        accept(future.result())
        evidence.enabled = False
        inputs = evidence.snapshot()
        for name, item in inputs.items():
            path = Path(name)
            stat = path.stat()
            if (stat.st_size, stat.st_mtime_ns) != (item["size"], item["mtime_ns"]):
                raise RuntimeError(f"Audited input changed: {path}")
            item["sha256"] = file_digest(path)
        if any(file_digest(name) != digest for name, digest in code.items()):
            raise RuntimeError("Calculation implementation changed during audit")
        export_json(args.output / "input_inventory.json", inputs)
        report.update(status="compared_all_with_unresolved_sources" if report["failures"] or report["no_price_history"] else "compared_all_not_published",
            seconds=round(monotonic()-started, 1), input_files=len(inputs), input_inventory_sha256=file_digest(args.output / "input_inventory.json"))
        export_json(args.output / "summary.json", report)
        print(json.dumps({name: report[name] for name in ("status", "completed", "scope_securities", "failures", "no_price_history", "seconds")}), flush=True)


if __name__ == "__main__":
    main()
