from __future__ import annotations

from pathlib import Path
import json
import sys

import pytest
import yaml

from engine.semantic.manifest import RuleBundleIntegrityError
from engine.transformers import sec_filings as public_sec_filings
from engine.semantic.us_dsl import load_us_semantic_rules, parse_us_semantic_rules
from engine.transformers._internal.sec_filings import (
    US_MAPPING_RULE_PATH,
    _fact_matches_label_rule,
    load_us_mapping_rules,
)
from engine.us_mapping_coverage_validator import (
    DEFAULT_RULE_PATH as COVERAGE_RULE_PATH,
    load_mapping_rules as load_coverage_mapping_rules,
)
from engine.workflows._internal.normalize_workflow import (
    US_MAPPING_RULE_PATH as WORKFLOW_RULE_PATH,
)
from engine.workflows._internal import normalize_workflow


def test_hmrb_style_us_rule_preserves_semantic_evidence_and_legacy_shape() -> None:
    ruleset = parse_us_semantic_rules(
        r'''
ruleset "semantic_us_v2" {
  schema = "arcana.sec-semantic/v2"
  version = 2
  authority = ["FILING_XBRL", "COMPANYFACTS", "FINANCIAL_STATEMENT_NOTES"]
}

rule "us_total_assets" {
  version = 2
  applies {
    source = ["FILING_XBRL", "COMPANYFACTS"]
    form = ["10-K", "10-Q", "10-K/A", "10-Q/A"]
    accounting = "US_GAAP"
  }
  match fact {
    concept.primary = ["us-gaap:Assets"]
    concept.alternate = ["ifrs-full:Assets"]
    label.matches = ["(?i)\\btotal\\s+assets\\b"]
    label.excludes = ["(?i)\\bunder\\s+management\\b"]
  }
  capture {
    value = "fact.numeric_value"
    period = "context.period"
    dimensions = "context.dimensions"
  }
  constraint {
    statement = "BS"
    entity = "filing.entity"
    dimensions = "CONSOLIDATED_OR_NONE"
  }
  emit {
    canonical = "TOTAL_ASSETS"
    amount = "as_reported"
    cash_direction = ""
  }
  legacy {
    group = "companyfacts_rules"
    index = 0
  }
}
'''
    )

    assert ruleset.name == "semantic_us_v2"
    assert ruleset.schema == "arcana.sec-semantic/v2"
    assert ruleset.authority == (
        "FILING_XBRL",
        "COMPANYFACTS",
        "FINANCIAL_STATEMENT_NOTES",
    )
    rule = ruleset.rules[0]
    assert rule.sources == ("FILING_XBRL", "COMPANYFACTS")
    assert rule.primary_concepts == ("us-gaap:Assets",)
    assert rule.captures["dimensions"] == "context.dimensions"
    assert rule.constraints["dimensions"] == "CONSOLIDATED_OR_NONE"

    assert ruleset.to_legacy_rule_groups() == {
        "companyfacts_rules": [
            {
                "canonical_id": "TOTAL_ASSETS",
                "fs_type": "BS",
                "primary_tags": ["us-gaap:Assets"],
                "alternate_tags": ["ifrs-full:Assets"],
                "label_patterns": [r"(?i)\btotal\s+assets\b"],
                "label_exclude_patterns": [r"(?i)\bunder\s+management\b"],
            }
        ],
        "notes_rules": [],
        "edgartools_fallback_rules": [],
    }


def test_archived_us_v2_dsl_is_a_lossless_migration_of_every_v1_rule() -> None:
    legacy_path = Path("data-lake/meta/rules/us_mapping.yaml")
    expected = yaml.safe_load(legacy_path.read_text(encoding="utf-8"))
    actual = load_us_mapping_rules(Path("data-lake/meta/rules/semantic_us_v2.arcana"))

    assert US_MAPPING_RULE_PATH.name == "semantic_us_rule_manifest.json"
    for group, expected_rules in expected.items():
        assert len(actual[group]) == len(expected_rules)
        for expected_rule, actual_rule in zip(expected_rules, actual[group], strict=True):
            for field, expected_value in expected_rule.items():
                assert actual_rule[field] == expected_value
    assert {key: len(value) for key, value in expected.items()} == {
        "companyfacts_rules": 75,
        "notes_rules": 13,
        "edgartools_fallback_rules": 16,
    }


def test_v2_short_term_debt_rejects_interest_rate_extension_labels() -> None:
    rules = load_us_mapping_rules()["companyfacts_rules"]
    rule = next(item for item in rules if item["canonical_id"] == "SHORT_TERM_DEBT")

    assert not _fact_matches_label_rule(
        namespace="invest",
        tag="InvestmentInterestRateRangeEnd",
        fact={"label": "Investment Interest Rate Range End"},
        rule=rule,
    )


def test_all_us_rule_consumers_prefer_the_v2_dsl() -> None:
    expected = load_us_mapping_rules()

    assert WORKFLOW_RULE_PATH.name == "semantic_us_rule_manifest.json"
    assert COVERAGE_RULE_PATH.name == "semantic_us_rule_manifest.json"
    assert load_coverage_mapping_rules(COVERAGE_RULE_PATH) == expected


def test_us_rule_manifest_resolves_and_verifies_the_hmrb_bundle() -> None:
    ruleset = load_us_semantic_rules(
        Path("data-lake/meta/rules/semantic_us_rule_manifest.json")
    )

    manifest = json.loads(US_MAPPING_RULE_PATH.read_text(encoding="utf-8"))
    assert ruleset.name == Path(manifest["active_bundle"]).stem
    assert ruleset.version == manifest["version"]
    assert ruleset.schema == "arcana.sec-semantic/v2"
    assert len(ruleset.rules) == 104


def test_us_rule_manifest_rejects_a_modified_bundle(tmp_path: Path) -> None:
    source_root = Path("data-lake/meta/rules")
    manifest = json.loads(
        (source_root / "semantic_us_rule_manifest.json").read_text(encoding="utf-8")
    )
    bundle_name = manifest["active_bundle"]
    (tmp_path / bundle_name).write_bytes(
        (source_root / bundle_name).read_bytes() + b"\n# modified after publication\n"
    )
    manifest_path = tmp_path / "semantic_us_rule_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(RuleBundleIntegrityError, match="checksum mismatch"):
        load_us_semantic_rules(manifest_path)


def test_us_normalize_cli_keeps_live_edgartools_opt_in(monkeypatch) -> None:
    called: dict[str, object] = {}

    def normalize(**kwargs):
        called.update(kwargs)
        return []

    monkeypatch.setattr(public_sec_filings, "normalize_us_sec_filings", normalize)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "prog",
            "--market",
            "us",
            "--start-year",
            "2025",
            "--end-year",
            "2025",
            "--target",
            "statements",
        ],
    )

    normalize_workflow.main()

    assert called["use_edgartools"] is False
