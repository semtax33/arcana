from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from engine.semantic.integrity import audit_debug_corpus, static_sign_policy_audit


NORMALIZED = PROJECT_ROOT / "data-lake" / "silver" / "dart" / "normalized"
SIGN_POLICY = PROJECT_ROOT / "data-lake" / "meta" / "rules" / "semantic_kr_v2.yaml"
OUTPUT = PROJECT_ROOT / "deliverables" / "semantic_value_integrity_audit.json"


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit unit/sign/cash-flow invariants")
    parser.add_argument("--normalized-dir", type=Path, default=NORMALIZED)
    parser.add_argument("--sign-policy", type=Path, default=SIGN_POLICY)
    parser.add_argument("--output", type=Path, default=OUTPUT)
    parser.add_argument("--max-files", type=int)
    args = parser.parse_args()

    report = {
        "static_sign_policy": static_sign_policy_audit(args.sign_policy),
        "observed_corpus": audit_debug_corpus(
            args.normalized_dir,
            max_files=args.max_files,
        ),
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    observed = report["observed_corpus"]
    print(
        json.dumps(
            {
                "output": str(args.output),
                "static_sign_policy_error_count": report["static_sign_policy"]["error_count"],
                "file_count": observed["file_count"],
                "row_count": observed["row_count"],
                "invalid_unit_factor_row_count": observed["invalid_unit_factor_row_count"],
                "source_scale_mismatch_row_count": observed["source_scale_mismatch_row_count"],
                "cash_effect_mismatch_row_count": observed["cash_effect_mismatch_row_count"],
                "canonical_direction_mismatch_row_count": observed[
                    "canonical_direction_mismatch_row_count"
                ],
            },
            ensure_ascii=True,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
