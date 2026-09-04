from __future__ import annotations

import argparse
from pathlib import Path
import re
import subprocess

import pandas as pd

from engine.core.paths import DATA_LAKE


SNAPSHOT_ROOT = DATA_LAKE.silver("dart", "normalized-snapshots")
DEFAULT_OUTPUT = DATA_LAKE.meta("kr_unit_scale_repair_audit.csv")
DEFAULT_TARGETS = DATA_LAKE.meta("kr_unit_scale_repair_targets.csv")
_FILE_RE = re.compile(
    r"kr_normalized_(?P<symbol>\d{6})_(?P<year>\d{4})[.](?P<month>\d{2})[.]debug[.]csv$"
)
_OLD_REASON = "report unit-scale repaired"


def _matching_paths(snapshot_root: Path) -> list[Path]:
    result = subprocess.run(
        ["rg", "-l", _OLD_REASON, str(snapshot_root), "-g", "*.debug.csv"],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode not in {0, 1}:
        raise RuntimeError(result.stderr.strip() or f"rg exited with {result.returncode}")
    return sorted(Path(line.strip()) for line in result.stdout.splitlines() if line.strip())


def audit_unit_scale_repairs(
    *,
    snapshot_root: str | Path = SNAPSHOT_ROOT,
    output_path: str | Path = DEFAULT_OUTPUT,
    targets_path: str | Path = DEFAULT_TARGETS,
    paths_from: str | Path | None = None,
) -> pd.DataFrame:
    snapshot_root = Path(snapshot_root)
    rows: list[dict[str, object]] = []
    if paths_from is None:
        paths = _matching_paths(snapshot_root)
    else:
        prior = pd.read_csv(paths_from, dtype=str, keep_default_na=False)
        paths = sorted(Path(value) for value in prior["path"].tolist() if value)
    print(f"[INFO] old report-scale debug files={len(paths)}", flush=True)
    for index, path in enumerate(paths, start=1):
        match = _FILE_RE.fullmatch(path.name)
        if match is None:
            continue
        frame = pd.read_csv(path, dtype=str, keep_default_na=False)
        reason = frame.get("reason", pd.Series("", index=frame.index)).astype(str)
        repaired = frame[reason.str.contains(_OLD_REASON, regex=False)]
        if repaired.empty:
            if index % 100 == 0 or index == len(paths):
                print(f"[PROGRESS] audited={index}/{len(paths)}", flush=True)
            continue
        units = pd.to_numeric(
            repaired.get("unit_factor", pd.Series("", index=repaired.index)),
            errors="coerce",
        )
        rows.append(
            {
                "symbol": match.group("symbol"),
                "year": int(match.group("year")),
                "month": int(match.group("month")),
                "path": str(path),
                "repaired_rows": len(repaired),
                "invalid_fractional_unit_rows": int((units < 1).sum()),
                "minimum_unit_factor": units.min() if units.notna().any() else "",
            }
        )
        if index % 100 == 0 or index == len(paths):
            print(f"[PROGRESS] audited={index}/{len(paths)}", flush=True)

    frame = pd.DataFrame(
        rows,
        columns=[
            "symbol",
            "year",
            "month",
            "path",
            "repaired_rows",
            "invalid_fractional_unit_rows",
            "minimum_unit_factor",
        ],
    ).sort_values(["symbol", "year", "month"])
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(output_path, index=False, encoding="utf-8-sig")

    targets = pd.DataFrame({"symbol": sorted(frame["symbol"].astype(str).unique())})
    targets_path = Path(targets_path)
    targets_path.parent.mkdir(parents=True, exist_ok=True)
    targets.to_csv(targets_path, index=False, encoding="utf-8-sig")
    print(
        f"[DONE] files={len(frame)}, symbols={len(targets)}, "
        f"fractional_unit_files={int((frame['invalid_fractional_unit_rows'] > 0).sum())}, "
        f"output={output_path}, targets={targets_path}",
        flush=True,
    )
    return frame


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Audit legacy report-wide KR unit-scale repairs."
    )
    parser.add_argument("--snapshot-root", type=Path, default=SNAPSHOT_ROOT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--targets", type=Path, default=DEFAULT_TARGETS)
    parser.add_argument("--paths-from", type=Path)
    args = parser.parse_args()
    audit_unit_scale_repairs(
        snapshot_root=args.snapshot_root,
        output_path=args.output,
        targets_path=args.targets,
        paths_from=args.paths_from,
    )


if __name__ == "__main__":
    main()
