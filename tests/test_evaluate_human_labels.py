from __future__ import annotations

from alert_reviewer.alert_filter import FilterPolicy
from scripts.evaluate_human_labels import (
    ACCEPT,
    REJECT,
    HumanLabel,
    binary_metrics,
    build_report,
)


def _alert(alert_id: str, *, approval_rate: float, rolling_average: float):
    return {
        "schema_version": "1.0",
        "alert_id": alert_id,
        "merchant_code": "merchant-001",
        "merchant_name": "Example Merchant",
        "country": "Ecuador",
        "timestamp": "2026-08-12T12:00:00Z",
        "criticality": "Critica",
        "approval_rate": approval_rate,
        "rolling_avg_approval_rate": rolling_average,
        "total_transactions": 100,
        "declined_count": round(100 * (1 - approval_rate)),
    }


def test_binary_metrics_report_confusion_matrix_and_balanced_scores():
    metrics = binary_metrics(
        [ACCEPT, ACCEPT, REJECT, REJECT],
        [ACCEPT, REJECT, ACCEPT, REJECT],
    )

    assert metrics["confusion_matrix"] == {"tp": 1, "fp": 1, "fn": 1, "tn": 1}
    assert metrics["precision_accept"] == 0.5
    assert metrics["recall_accept"] == 0.5
    assert metrics["f1_accept"] == 0.5
    assert metrics["balanced_accuracy"] == 0.5


def test_report_compares_reviewers_filter_and_adjudicated_truth():
    cases = {
        "accepted": _alert("accepted", approval_rate=0.15, rolling_average=0.15),
        "rejected": _alert("rejected", approval_rate=0.60, rolling_average=0.61),
    }
    labels = [
        HumanLabel("accepted", "reviewer-a", ACCEPT),
        HumanLabel("rejected", "reviewer-a", REJECT),
        HumanLabel("accepted", "reviewer-b", REJECT),
        HumanLabel("rejected", "reviewer-b", REJECT),
        HumanLabel("accepted", "adjudicated", ACCEPT),
        HumanLabel("rejected", "adjudicated", REJECT),
    ]

    report = build_report(
        cases,
        labels,
        FilterPolicy(),
        policy_version="2026-08-13.1",
    )

    assert report["policy_version"] == "2026-08-13.1"
    assert report["cases"] == {"total": 2, "filter_accept": 1, "filter_reject": 1}
    assert report["reviewers"]["reviewer-a"]["comparison_to_filter_policy"][
        "agreement"
    ] == 1.0
    assert report["pairwise_human_agreement"] == [
        {
            "reviewers": ["reviewer-a", "reviewer-b"],
            "compared": 2,
            "agreement": 0.5,
            "cohens_kappa": 0.0,
        }
    ]
    assert report["adjudicated_evaluation"]["filter_vs_adjudicated"][
        "f1_accept"
    ] == 1.0
    assert report["disagreements"][0]["case_id"] == "accepted"
