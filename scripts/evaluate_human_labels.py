#!/usr/bin/env python3
"""Compare independent human labels with the payload-only filter policy."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from itertools import combinations
from pathlib import Path
from typing import Any

import yaml

from alert_reviewer.alert_filter import FilterPolicy, evaluate_alert

ACCEPT = "ACCEPT"
REJECT = "REJECT"
ABSTAIN = "ABSTAIN"
DECISIONS = {ACCEPT, REJECT, ABSTAIN}
ADJUDICATED_REVIEWER = "adjudicated"


@dataclass(frozen=True)
class HumanLabel:
    case_id: str
    reviewer_id: str
    decision: str
    reason_code: str | None = None
    confidence: float | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Measure agreement between human labels and the Kipu filter policy."
    )
    parser.add_argument("cases", type=Path, help="JSONL with case_id and alert")
    parser.add_argument("labels", type=Path, help="CSV with human decisions")
    parser.add_argument(
        "--policy",
        type=Path,
        default=Path("config/filter_policy.yaml"),
        help="Filter policy YAML",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON report path")
    return parser.parse_args()


def load_cases(path: Path) -> dict[str, dict[str, Any]]:
    cases: dict[str, dict[str, Any]] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON on cases line {line_number}") from exc
        if not isinstance(item, dict):
            raise ValueError(f"Cases line {line_number} must be a JSON object")
        case_id = item.get("case_id")
        alert = item.get("alert")
        if not isinstance(case_id, str) or not case_id.strip():
            raise ValueError(f"Cases line {line_number} has no valid case_id")
        if not isinstance(alert, dict):
            raise ValueError(f"Case {case_id!r} has no alert object")
        if case_id in cases:
            raise ValueError(f"Duplicate case_id: {case_id}")
        cases[case_id] = alert
    if not cases:
        raise ValueError("Cases file is empty")
    return cases


def load_labels(path: Path, case_ids: set[str]) -> list[HumanLabel]:
    labels: list[HumanLabel] = []
    seen: set[tuple[str, str]] = set()
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        required = {"case_id", "reviewer_id", "decision"}
        if reader.fieldnames is None or not required.issubset(reader.fieldnames):
            raise ValueError("Labels CSV requires case_id, reviewer_id and decision")
        for line_number, row in enumerate(reader, 2):
            case_id = (row.get("case_id") or "").strip()
            reviewer_id = (row.get("reviewer_id") or "").strip()
            decision = (row.get("decision") or "").strip().upper()
            if case_id not in case_ids:
                raise ValueError(f"Unknown case_id on labels line {line_number}: {case_id}")
            if not reviewer_id:
                raise ValueError(f"Missing reviewer_id on labels line {line_number}")
            if decision not in DECISIONS:
                raise ValueError(f"Invalid decision on labels line {line_number}: {decision}")
            key = (case_id, reviewer_id)
            if key in seen:
                raise ValueError(f"Duplicate label for {case_id}/{reviewer_id}")
            seen.add(key)
            raw_confidence = (row.get("confidence") or "").strip()
            confidence = float(raw_confidence) if raw_confidence else None
            if confidence is not None and not 1 <= confidence <= 5:
                raise ValueError(f"Confidence must be between 1 and 5 on line {line_number}")
            labels.append(
                HumanLabel(
                    case_id=case_id,
                    reviewer_id=reviewer_id,
                    decision=decision,
                    reason_code=(row.get("reason_code") or "").strip() or None,
                    confidence=confidence,
                )
            )
    if not labels:
        raise ValueError("Labels file is empty")
    return labels


def _rounded(value: float | None) -> float | None:
    return round(value, 4) if value is not None else None


def binary_metrics(predictions: list[str], references: list[str]) -> dict[str, Any]:
    if len(predictions) != len(references):
        raise ValueError("Predictions and references must have equal length")
    pairs = list(zip(predictions, references, strict=True))
    tp = sum(p == ACCEPT and r == ACCEPT for p, r in pairs)
    fp = sum(p == ACCEPT and r == REJECT for p, r in pairs)
    fn = sum(p == REJECT and r == ACCEPT for p, r in pairs)
    tn = sum(p == REJECT and r == REJECT for p, r in pairs)
    total = tp + fp + fn + tn
    precision = tp / (tp + fp) if tp + fp else None
    recall = tp / (tp + fn) if tp + fn else None
    specificity = tn / (tn + fp) if tn + fp else None
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall
        else None
    )
    balanced_accuracy = (
        (recall + specificity) / 2
        if recall is not None and specificity is not None
        else None
    )
    return {
        "compared": total,
        "confusion_matrix": {"tp": tp, "fp": fp, "fn": fn, "tn": tn},
        "agreement": _rounded((tp + tn) / total if total else None),
        "precision_accept": _rounded(precision),
        "recall_accept": _rounded(recall),
        "f1_accept": _rounded(f1),
        "balanced_accuracy": _rounded(balanced_accuracy),
    }


def cohens_kappa(left: list[str], right: list[str]) -> float | None:
    if not left or len(left) != len(right):
        return None
    total = len(left)
    observed = sum(a == b for a, b in zip(left, right, strict=True)) / total
    left_accept = sum(value == ACCEPT for value in left) / total
    right_accept = sum(value == ACCEPT for value in right) / total
    expected = left_accept * right_accept + (1 - left_accept) * (1 - right_accept)
    if expected == 1:
        return 1.0 if observed == 1 else None
    return _rounded((observed - expected) / (1 - expected))


def build_report(
    cases: dict[str, dict[str, Any]],
    labels: list[HumanLabel],
    policy: FilterPolicy,
    *,
    policy_version: str,
) -> dict[str, Any]:
    outcomes = {case_id: evaluate_alert(alert, policy) for case_id, alert in cases.items()}
    filter_decisions = {
        case_id: ACCEPT if outcome.accepted else REJECT
        for case_id, outcome in outcomes.items()
    }
    by_reviewer: dict[str, dict[str, HumanLabel]] = defaultdict(dict)
    for label in labels:
        by_reviewer[label.reviewer_id][label.case_id] = label

    reviewer_reports: dict[str, Any] = {}
    disagreements: list[dict[str, Any]] = []
    for reviewer_id, reviewer_labels in sorted(by_reviewer.items()):
        decided = [label for label in reviewer_labels.values() if label.decision != ABSTAIN]
        predictions = [label.decision for label in decided]
        references = [filter_decisions[label.case_id] for label in decided]
        confidences = [
            label.confidence for label in reviewer_labels.values() if label.confidence is not None
        ]
        reviewer_reports[reviewer_id] = {
            "labels": len(reviewer_labels),
            "accept": sum(label.decision == ACCEPT for label in reviewer_labels.values()),
            "reject": sum(label.decision == REJECT for label in reviewer_labels.values()),
            "abstain": sum(label.decision == ABSTAIN for label in reviewer_labels.values()),
            "coverage": _rounded(len(decided) / len(cases)),
            "mean_confidence": _rounded(sum(confidences) / len(confidences))
            if confidences
            else None,
            "comparison_to_filter_policy": binary_metrics(predictions, references),
        }
        if reviewer_id != ADJUDICATED_REVIEWER:
            for label in decided:
                filter_decision = filter_decisions[label.case_id]
                if label.decision != filter_decision:
                    disagreements.append(
                        {
                            "case_id": label.case_id,
                            "reviewer_id": reviewer_id,
                            "human_decision": label.decision,
                            "human_reason_code": label.reason_code,
                            "filter_decision": filter_decision,
                            "filter_reason_codes": [
                                str(reason) for reason in outcomes[label.case_id].reason_codes
                            ],
                        }
                    )

    pairwise: list[dict[str, Any]] = []
    human_reviewers = sorted(
        reviewer for reviewer in by_reviewer if reviewer != ADJUDICATED_REVIEWER
    )
    for left_id, right_id in combinations(human_reviewers, 2):
        shared = sorted(set(by_reviewer[left_id]) & set(by_reviewer[right_id]))
        shared = [
            case_id
            for case_id in shared
            if by_reviewer[left_id][case_id].decision != ABSTAIN
            and by_reviewer[right_id][case_id].decision != ABSTAIN
        ]
        left = [by_reviewer[left_id][case_id].decision for case_id in shared]
        right = [by_reviewer[right_id][case_id].decision for case_id in shared]
        pairwise.append(
            {
                "reviewers": [left_id, right_id],
                "compared": len(shared),
                "agreement": _rounded(
                    sum(a == b for a, b in zip(left, right, strict=True))
                    / len(shared)
                    if shared
                    else None
                ),
                "cohens_kappa": cohens_kappa(left, right),
            }
        )

    adjudicated = None
    truth_labels = by_reviewer.get(ADJUDICATED_REVIEWER, {})
    truth_cases = sorted(
        case_id
        for case_id, label in truth_labels.items()
        if label.decision != ABSTAIN
    )
    if truth_cases:
        adjudicated = {
            "filter_vs_adjudicated": binary_metrics(
                [filter_decisions[case_id] for case_id in truth_cases],
                [truth_labels[case_id].decision for case_id in truth_cases],
            )
        }

    return {
        "policy_version": policy_version,
        "policy": asdict(policy),
        "interpretation": (
            "Agreement measures consistency with the payload-only policy, not proof "
            "that an operational incident occurred."
        ),
        "cases": {
            "total": len(cases),
            "filter_accept": sum(value == ACCEPT for value in filter_decisions.values()),
            "filter_reject": sum(value == REJECT for value in filter_decisions.values()),
        },
        "reviewers": reviewer_reports,
        "pairwise_human_agreement": pairwise,
        "adjudicated_evaluation": adjudicated,
        "disagreements": disagreements,
    }


def main() -> int:
    args = parse_args()
    cases = load_cases(args.cases)
    labels = load_labels(args.labels, set(cases))
    policy_document = _load_yaml(args.policy)
    policy = FilterPolicy.load(args.policy)
    report = build_report(
        cases,
        labels,
        policy,
        policy_version=str(policy_document.get("version", "unversioned")),
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


def _load_yaml(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as file:
        document = yaml.safe_load(file)
    if not isinstance(document, dict):
        raise ValueError("Policy YAML must contain an object")
    return document


if __name__ == "__main__":
    raise SystemExit(main())
