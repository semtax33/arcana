from __future__ import annotations

from typing import Any, Callable, Iterable, Mapping


def _canonical(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if text in {"", "UNMAPPED", "ABSTAIN", "None"} else text


class GoldenCorpusEvaluator:
    """Evaluate semantic emissions without conflating abstention and accuracy."""

    def evaluate(
        self,
        cases: Iterable[Mapping[str, Any]],
        predictor: Callable[[Mapping[str, Any]], object],
    ) -> dict[str, Any]:
        rows = [dict(case) for case in cases]
        true_positive = false_positive = false_negative = true_abstain = 0
        abstain = exact = 0
        errors = []
        for case in rows:
            expected = _canonical(case.get("expected_canonical_id"))
            predicted = _canonical(predictor(case))
            if predicted is None:
                abstain += 1
            if expected == predicted:
                exact += 1
                if expected is None:
                    true_abstain += 1
                else:
                    true_positive += 1
                continue
            if predicted is not None:
                false_positive += 1
            if expected is not None:
                false_negative += 1
            if len(errors) < 200:
                errors.append(
                    {
                        "case_id": case.get("case_id"),
                        "expected_canonical_id": expected,
                        "predicted_canonical_id": predicted,
                    }
                )

        count = len(rows)
        precision_denominator = true_positive + false_positive
        recall_denominator = true_positive + false_negative
        return {
            "case_count": count,
            "true_positive_count": true_positive,
            "false_positive_count": false_positive,
            "false_negative_count": false_negative,
            "true_abstain_count": true_abstain,
            "abstain_count": abstain,
            "precision_pct": (
                100.0 * true_positive / precision_denominator
                if precision_denominator
                else None
            ),
            "recall_pct": (
                100.0 * true_positive / recall_denominator
                if recall_denominator
                else None
            ),
            "abstain_pct": 100.0 * abstain / count if count else 0.0,
            "exact_match_pct": 100.0 * exact / count if count else 0.0,
            "errors": errors,
        }
