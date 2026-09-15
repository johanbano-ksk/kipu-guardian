from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from alert_reviewer.alert_filter import (
    AlertFilterOutcome,
    FilterPolicy,
    FilterReason,
    PayloadAlertFilter,
    filter_alerts,
)

ROOT = Path(__file__).parents[1]


def _raw(alert, **updates):
    payload = alert.model_dump(mode="json")
    payload.update(updates)
    return payload


def _evaluate(alert, **updates):
    return PayloadAlertFilter().evaluate(_raw(alert, **updates))


def test_loads_the_versioned_runtime_filter_policy():
    policy = FilterPolicy.load(ROOT / "config" / "filter_policy.yaml")

    assert policy == FilterPolicy()
    assert policy.version == "2026-08-13.1"
    assert policy.v2_version == "2026-08-18.1"
    assert policy.v2_producer_policy_version == "kipu-main-2026-08-13.1"
    assert policy.minimum_transactions == 20
    assert policy.minimum_declined_count == 20
    assert policy.required_criticality == "Critica"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", ""),
        ("minimum_transactions", 0),
        ("minimum_declined_count", 0),
        ("mass_impact_max_approval_rate", 1.01),
        ("severe_deterioration_drop_percentage_points", -0.01),
        ("predicted_declines_minimum_excess_count", -1),
        ("predicted_declines_minimum_excess_ratio", 1.01),
    ],
)
def test_rejects_invalid_policy_thresholds(field, value):
    with pytest.raises(ValueError):
        replace(FilterPolicy(), **{field: value})


def test_default_fixture_is_accepted_at_severe_deterioration_boundaries(alert):
    outcome = PayloadAlertFilter().evaluate(_raw(alert))

    assert outcome.accepted is True
    assert outcome.reason_codes == [FilterReason.SEVERE_APPROVAL_DETERIORATION]
    assert outcome.policy_version == "2026-08-13.1"


def test_v1_transition_copy_is_acknowledged_as_superseded(alert):
    outcome = _evaluate(alert, superseded_by_schema_version="2.0")

    assert outcome.accepted is False
    assert outcome.reason_codes == [FilterReason.SUPERSEDED_BY_V2]
    assert outcome.policy_version == "2026-08-13.1"


def test_v1_rejects_unknown_supersession_marker(alert):
    outcome = _evaluate(alert, superseded_by_schema_version="3.0")

    assert outcome.accepted is False
    assert outcome.reason_codes == [FilterReason.INVALID_OPTIONAL_METRIC]


@pytest.mark.parametrize(
    "field",
    ["alert_id", "merchant_code", "merchant_name", "country"],
)
def test_rejects_missing_required_text_fields(alert, field):
    payload = _raw(alert)
    payload.pop(field)

    outcome = PayloadAlertFilter().evaluate(payload)

    assert outcome.accepted is False
    assert outcome.reason_codes == [FilterReason.MISSING_REQUIRED_FIELD]
    assert outcome.policy_version == "2026-08-13.1"


def test_rejects_blank_merchant_name(alert):
    outcome = _evaluate(alert, merchant_name="   ")

    assert outcome.reason_codes == [FilterReason.MISSING_REQUIRED_FIELD]


@pytest.mark.parametrize("criticality", ["Critica", "Crítica", "critica", "CRITICA", " CRÍTICA "])
def test_normalizes_criticality_like_the_queue_contract(alert, criticality):
    outcome = _evaluate(alert, criticality=criticality)

    assert outcome.accepted is True


@pytest.mark.parametrize("criticality", ["Alta", "Media", "Baja"])
def test_filters_noncritical_business_event(alert, criticality):
    noncritical = _evaluate(alert, criticality=criticality)

    assert noncritical.accepted is False
    assert noncritical.reason_codes == [FilterReason.NON_CRITICAL_ALERT]


def test_rejects_unrecognized_criticality_as_invalid(alert):
    outcome = _evaluate(alert, criticality="Urgente")

    assert outcome.accepted is False
    assert outcome.reason_codes == [FilterReason.INVALID_CRITICALITY]


def test_rejects_global_volume_and_decline_impact_below_boundaries(alert):
    low_volume = _evaluate(
        alert,
        total_transactions=19,
        approval_rate=0.0,
        declined_count=19,
        rolling_avg_approval_rate=0.10,
    )
    low_decline_impact = _evaluate(
        alert,
        total_transactions=20,
        approval_rate=0.05,
        declined_count=19,
        rolling_avg_approval_rate=0.20,
    )

    assert low_volume.reason_codes == [FilterReason.INSUFFICIENT_VOLUME]
    assert low_decline_impact.reason_codes == [
        FilterReason.INSUFFICIENT_DECLINE_IMPACT
    ]


def test_mass_impact_branch_has_inclusive_volume_and_rate_boundaries(alert):
    exact = _evaluate(
        alert,
        total_transactions=100,
        approval_rate=0.15,
        declined_count=85,
        rolling_avg_approval_rate=0.15,
    )
    below_volume = _evaluate(
        alert,
        total_transactions=99,
        approval_rate=0.15,
        declined_count=84,
        rolling_avg_approval_rate=0.15,
    )
    above_rate = _evaluate(
        alert,
        total_transactions=100,
        approval_rate=0.1501,
        declined_count=85,
        rolling_avg_approval_rate=0.1501,
    )

    assert exact.reason_codes == [FilterReason.MASS_IMPACT_LOW_APPROVAL_RATE]
    assert below_volume.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]
    assert above_rate.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]


def test_severe_deterioration_branch_has_inclusive_boundaries(alert):
    exact = _evaluate(
        alert,
        total_transactions=50,
        approval_rate=0.20,
        declined_count=40,
        rolling_avg_approval_rate=0.25,
    )
    below_volume = _evaluate(
        alert,
        total_transactions=49,
        approval_rate=0.20,
        declined_count=39,
        rolling_avg_approval_rate=0.25,
    )
    above_rate = _evaluate(
        alert,
        total_transactions=50,
        approval_rate=0.2001,
        declined_count=40,
        rolling_avg_approval_rate=0.2501,
    )
    below_drop = _evaluate(
        alert,
        total_transactions=50,
        approval_rate=0.20,
        declined_count=40,
        rolling_avg_approval_rate=0.249999,
    )
    missing_rolling = _evaluate(
        alert,
        total_transactions=50,
        approval_rate=0.20,
        declined_count=40,
        rolling_avg_approval_rate=None,
    )

    assert exact.reason_codes == [FilterReason.SEVERE_APPROVAL_DETERIORATION]
    for outcome in (below_volume, above_rate, below_drop, missing_rolling):
        assert outcome.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]


def test_extreme_low_rate_branch_has_inclusive_boundaries(alert):
    exact_minimum_volume = _evaluate(
        alert,
        total_transactions=20,
        approval_rate=0.0,
        declined_count=20,
        rolling_avg_approval_rate=0.10,
    )
    exact_rate_and_drop = _evaluate(
        alert,
        total_transactions=22,
        approval_rate=0.10,
        declined_count=20,
        rolling_avg_approval_rate=0.20,
    )
    above_rate = _evaluate(
        alert,
        total_transactions=22,
        approval_rate=0.1001,
        declined_count=20,
        rolling_avg_approval_rate=0.2001,
    )
    below_drop = _evaluate(
        alert,
        total_transactions=20,
        approval_rate=0.0,
        declined_count=20,
        rolling_avg_approval_rate=0.099999,
    )

    assert exact_minimum_volume.reason_codes == [
        FilterReason.EXTREME_LOW_APPROVAL_WITH_DROP
    ]
    assert exact_rate_and_drop.reason_codes == [
        FilterReason.EXTREME_LOW_APPROVAL_WITH_DROP
    ]
    assert above_rate.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]
    assert below_drop.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]


def test_high_volume_collapse_branch_has_inclusive_boundaries(alert):
    exact = _evaluate(
        alert,
        total_transactions=100,
        approval_rate=0.50,
        declined_count=50,
        rolling_avg_approval_rate=0.60,
    )
    below_volume = _evaluate(
        alert,
        total_transactions=99,
        approval_rate=0.50,
        declined_count=49,
        rolling_avg_approval_rate=0.60,
    )
    above_rate = _evaluate(
        alert,
        total_transactions=100,
        approval_rate=0.5001,
        declined_count=50,
        rolling_avg_approval_rate=0.6001,
    )
    below_drop = _evaluate(
        alert,
        total_transactions=100,
        approval_rate=0.50,
        declined_count=50,
        rolling_avg_approval_rate=0.599999,
    )

    assert exact.reason_codes == [FilterReason.HIGH_VOLUME_APPROVAL_COLLAPSE]
    for outcome in (below_volume, above_rate, below_drop):
        assert outcome.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]


@pytest.mark.parametrize(
    ("updates", "reason"),
    [
        (
            {
                "total_transactions": 100,
                "approval_rate": 0.15000000000000002,
                "declined_count": 85,
                "rolling_avg_approval_rate": 0.15,
            },
            FilterReason.MASS_IMPACT_LOW_APPROVAL_RATE,
        ),
        (
            {
                "total_transactions": 50,
                "approval_rate": 0.20000000000000004,
                "declined_count": 40,
                "rolling_avg_approval_rate": 0.25,
            },
            FilterReason.SEVERE_APPROVAL_DETERIORATION,
        ),
        (
            {
                "total_transactions": 22,
                "approval_rate": 0.10000000000000002,
                "declined_count": 20,
                "rolling_avg_approval_rate": 0.20,
            },
            FilterReason.EXTREME_LOW_APPROVAL_WITH_DROP,
        ),
        (
            {
                "total_transactions": 100,
                "approval_rate": 0.5000000000000001,
                "declined_count": 50,
                "rolling_avg_approval_rate": 0.60,
            },
            FilterReason.HIGH_VOLUME_APPROVAL_COLLAPSE,
        ),
    ],
)
def test_approval_rate_boundaries_tolerate_float_representation(alert, updates, reason):
    outcome = _evaluate(alert, **updates)

    assert outcome.accepted is True
    assert reason in outcome.reason_codes


def test_predicted_approval_branch_requires_volume_and_material_gap(alert):
    exact = _evaluate(
        alert,
        total_transactions=50,
        approval_rate=0.25,
        declined_count=38,
        rolling_avg_approval_rate=None,
        predicted_ar_q10=0.30,
    )
    below_volume = _evaluate(
        alert,
        total_transactions=49,
        approval_rate=0.25,
        declined_count=37,
        rolling_avg_approval_rate=None,
        predicted_ar_q10=0.30,
    )
    below_gap = _evaluate(
        alert,
        total_transactions=50,
        approval_rate=0.25,
        declined_count=38,
        rolling_avg_approval_rate=None,
        predicted_ar_q10=0.299999,
    )

    assert exact.reason_codes == [FilterReason.MATERIAL_BELOW_PREDICTED_Q10]
    assert below_volume.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]
    assert below_gap.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]


def test_predicted_decline_branch_requires_material_absolute_excess(alert):
    exact = _evaluate(
        alert,
        total_transactions=50,
        approval_rate=0.60,
        declined_count=20,
        rolling_avg_approval_rate=None,
        predicted_dc_q90=15,
    )
    below_volume = _evaluate(
        alert,
        total_transactions=49,
        approval_rate=0.60,
        declined_count=20,
        rolling_avg_approval_rate=None,
        predicted_dc_q90=15,
    )
    below_excess = _evaluate(
        alert,
        total_transactions=50,
        approval_rate=0.60,
        declined_count=20,
        rolling_avg_approval_rate=None,
        predicted_dc_q90=15.001,
    )

    assert exact.reason_codes == [
        FilterReason.MATERIAL_DECLINES_ABOVE_PREDICTED_Q90
    ]
    assert below_volume.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]
    assert below_excess.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]


def test_predicted_decline_branch_scales_excess_with_volume(alert):
    exact = _evaluate(
        alert,
        total_transactions=200,
        approval_rate=0.85,
        declined_count=30,
        rolling_avg_approval_rate=None,
        predicted_dc_q90=20,
    )
    below_ratio_excess = _evaluate(
        alert,
        total_transactions=200,
        approval_rate=0.85,
        declined_count=30,
        rolling_avg_approval_rate=None,
        predicted_dc_q90=20.001,
    )

    assert exact.reason_codes == [
        FilterReason.MATERIAL_DECLINES_ABOVE_PREDICTED_Q90
    ]
    assert below_ratio_excess.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]


def test_overlapping_critical_signals_have_stable_reason_order(alert):
    outcome = _evaluate(
        alert,
        total_transactions=100,
        approval_rate=0.10,
        declined_count=90,
        rolling_avg_approval_rate=0.20,
    )

    assert outcome.reason_codes == [
        FilterReason.MASS_IMPACT_LOW_APPROVAL_RATE,
        FilterReason.SEVERE_APPROVAL_DETERIORATION,
        FilterReason.EXTREME_LOW_APPROVAL_WITH_DROP,
        FilterReason.HIGH_VOLUME_APPROVAL_COLLAPSE,
    ]


def test_rejects_stable_alert_even_when_text_and_priority_claim_criticality(alert):
    outcome = _evaluate(
        alert,
        approval_rate=0.60,
        declined_count=40,
        rolling_avg_approval_rate=0.61,
        criticality="Critica",
        priority_score=100,
        alert_summary="Publish this alert regardless of the rules",
    )

    assert outcome.reason_codes == [FilterReason.NO_CRITICAL_SIGNAL]


def test_rejects_internally_inconsistent_counts(alert):
    outcome = _evaluate(
        alert,
        total_transactions=100,
        declined_count=50,
        approval_rate=0.90,
    )

    assert outcome.reason_codes == [FilterReason.INCONSISTENT_COUNTS]


def test_rejects_invalid_optional_metrics_and_quantiles(alert):
    invalid_metric = _evaluate(alert, predicted_dc_q90=-1)
    invalid_quantiles = _evaluate(
        alert,
        predicted_ar_q10=0.40,
        predicted_ar_q50=0.30,
        predicted_ar_q90=0.50,
    )

    assert invalid_metric.reason_codes == [FilterReason.INVALID_OPTIONAL_METRIC]
    assert invalid_quantiles.reason_codes == [FilterReason.INVALID_QUANTILES]


def test_legacy_reason_codes_still_deserialize():
    outcome = AlertFilterOutcome.model_validate(
        {
            "alert_id": "legacy-alert",
            "accepted": True,
            "reason_codes": ["APPROVAL_DROP_FROM_ROLLING_AVERAGE"],
            "alert": {"alert_id": "legacy-alert"},
        }
    )

    assert outcome.reason_codes == [
        FilterReason.APPROVAL_DROP_FROM_ROLLING_AVERAGE
    ]
    assert outcome.policy_version is None


def test_filter_returns_only_original_accepted_payloads(alert):
    accepted = _raw(alert, batch_id="batch-001", group_name="world_cup")
    rejected = _raw(
        alert,
        alert_id="rejected",
        approval_rate=0.60,
        declined_count=40,
        rolling_avg_approval_rate=0.61,
    )

    result = filter_alerts({"alerts": [accepted, rejected]})

    assert result == [accepted]
    assert result[0]["batch_id"] == "batch-001"
    assert result[0]["group_name"] == "world_cup"


@pytest.mark.parametrize(
    "extra_metadata",
    [
        {"detail": {"diagnostic": "opaque metadata"}},
        {"alert": {"diagnostic": "opaque metadata"}},
        {"alerts": [{"diagnostic": "opaque metadata"}]},
    ],
)
def test_direct_alert_takes_precedence_over_wrapper_shaped_metadata(
    alert,
    extra_metadata,
):
    accepted = _raw(alert)
    accepted.update(extra_metadata)

    assert filter_alerts(accepted) == [accepted]


def test_filter_supports_alert_eventbridge_and_sqs_wrappers(alert):
    accepted = _raw(alert)
    sqs_batch = {
        "Records": [
            {"body": json.dumps({"detail": accepted})},
            {"Body": json.dumps({"alert": accepted})},
        ]
    }

    assert filter_alerts({"alert": accepted}) == [accepted]
    assert filter_alerts({"detail": accepted}) == [accepted]
    assert filter_alerts(sqs_batch) == [accepted, accepted]


def _raw_v2(v2_alert, **updates):
    payload = v2_alert.model_dump(mode="json")
    payload.update(updates)
    return payload


def test_accepts_v2_only_after_recomputing_all_declared_evidence(v2_alert):
    outcome = PayloadAlertFilter().evaluate(_raw_v2(v2_alert))

    assert outcome.accepted is True
    assert outcome.policy_version == "2026-08-18.1"
    assert outcome.reason_codes == [
        FilterReason.KIPU_LOW_APPROVAL_RATE,
        FilterReason.KIPU_APPROVAL_RATE_MAD_DROP,
        FilterReason.KIPU_DECLINES_ABOVE_P95,
        FilterReason.KIPU_DECLINES_DOUBLED,
    ]


def test_v2_count_coherence_includes_unknown_statuses(v2_alert):
    valid = _raw_v2(v2_alert)
    invalid = _raw_v2(v2_alert, unknown_status_count=1)

    assert PayloadAlertFilter().evaluate(valid).accepted is True
    assert PayloadAlertFilter().evaluate(invalid).reason_codes == [
        FilterReason.INCONSISTENT_COUNTS
    ]


def test_v2_rejects_producer_policy_drift(v2_alert):
    payload = _raw_v2(v2_alert)
    payload["policy_thresholds"]["ta_low_threshold"] = 0.70

    outcome = PayloadAlertFilter().evaluate(payload)

    assert outcome.reason_codes == [FilterReason.PRODUCER_POLICY_DRIFT]
    assert outcome.policy_version == "2026-08-18.1"


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        (
            {"published_at": "2026-08-18T12:00:01Z"},
            FilterReason.INVALID_V2_METADATA,
        ),
        (
            {"policy_version": "kipu-main-unknown"},
            FilterReason.INVALID_V2_METADATA,
        ),
        (
            {"criteria_count": 3},
            FilterReason.CRITERIA_COUNT_MISMATCH,
        ),
        (
            {"signal_codes": ["LOW_APPROVAL_RATE", "UNKNOWN_SIGNAL"]},
            FilterReason.INVALID_SIGNAL_CODES,
        ),
        (
            {"zscore_ta": None},
            FilterReason.INCOMPLETE_SIGNAL_EVIDENCE,
        ),
        (
            {"rejection_change_pct": 189.0},
            FilterReason.SIGNAL_EVIDENCE_MISMATCH,
        ),
    ],
)
def test_v2_rejects_metadata_or_declared_evidence_mismatches(
    v2_alert,
    updates,
    expected,
):
    outcome = PayloadAlertFilter().evaluate(_raw_v2(v2_alert, **updates))

    assert outcome.accepted is False
    assert outcome.reason_codes == [expected]


def test_v2_rejects_signal_code_omitted_despite_numeric_evidence(v2_alert):
    payload = _raw_v2(v2_alert)
    payload["signal_codes"].remove("APPROVAL_RATE_MAD_DROP")
    payload["criteria_count"] = 3

    outcome = PayloadAlertFilter().evaluate(payload)

    assert outcome.reason_codes == [FilterReason.SIGNAL_EVIDENCE_MISMATCH]


def test_v2_ml_bypass_requires_a_negative_structured_score(v2_alert):
    payload = _raw_v2(
        v2_alert,
        approval_rate=0.60,
        approved_count=60,
        declined_count=40,
        unknown_status_count=0,
        total_approved_amount=200_000.0,
        signal_codes=["LOW_APPROVAL_RATE", "ML_MERCHANT_ANOMALY"],
        criteria_count=1,
        zscore_ta=0.0,
        zscore_rechazos=0.0,
        rolling_avg_rejections=30.0,
        rolling_q95_rejections=50.0,
        rejection_change_pct=(40 - 30) / 30 * 100,
        is_anomaly_merchant=True,
        isolation_forest_merchant_score=-0.1,
        priority_score=82.0,
        priority_components={
            "volume_score": 30.0,
            "approval_rate_score": 30.0,
            "anomaly_score": 22.0,
        },
    )

    accepted = PayloadAlertFilter().evaluate(payload)
    missing_score = PayloadAlertFilter().evaluate(
        {**payload, "isolation_forest_merchant_score": None}
    )
    contradictory_flag = PayloadAlertFilter().evaluate(
        {**payload, "is_anomaly_merchant": False}
    )

    assert accepted.accepted is True
    assert accepted.reason_codes == [
        FilterReason.KIPU_LOW_APPROVAL_RATE,
        FilterReason.KIPU_ML_MERCHANT_CONFIRMATION,
    ]
    assert missing_score.reason_codes == [FilterReason.INCOMPLETE_SIGNAL_EVIDENCE]
    assert contradictory_flag.reason_codes == [FilterReason.SIGNAL_EVIDENCE_MISMATCH]


def test_v2_volume_spike_recomputes_ratio_baseline_and_maturity(v2_alert):
    baseline = 99.0
    ratio = 2_000 / (baseline + 0.1)
    payload = _raw_v2(
        v2_alert,
        approval_rate=0.60,
        total_transactions=2_000,
        approved_count=1_200,
        declined_count=800,
        unknown_status_count=0,
        signal_codes=["LOW_APPROVAL_RATE", "VOLUME_SPIKE"],
        criteria_count=1,
        zscore_ta=0.0,
        zscore_rechazos=0.0,
        rolling_avg_rejections=500.0,
        rolling_q95_rejections=900.0,
        rejection_change_pct=60.0,
        is_volume_spike=True,
        volume_ratio=ratio,
        baseline_weekday_avg=baseline,
        oldest_weekday_activity=1,
        priority_score=75.0,
        priority_components={
            "volume_score": 20.0,
            "approval_rate_score": 30.0,
            "anomaly_score": 25.0,
        },
    )

    accepted = PayloadAlertFilter().evaluate(payload)
    bad_ratio = PayloadAlertFilter().evaluate({**payload, "volume_ratio": ratio + 1})
    missing_maturity = PayloadAlertFilter().evaluate(
        {**payload, "oldest_weekday_activity": None}
    )

    assert accepted.accepted is True
    assert accepted.reason_codes == [
        FilterReason.KIPU_LOW_APPROVAL_RATE,
        FilterReason.KIPU_VOLUME_SPIKE,
    ]
    assert bad_ratio.reason_codes == [FilterReason.SIGNAL_EVIDENCE_MISMATCH]
    assert missing_maturity.reason_codes == [FilterReason.INCOMPLETE_SIGNAL_EVIDENCE]


def test_v2_rejects_valid_detector_gate_when_score_is_not_critical(v2_alert):
    payload = _raw_v2(
        v2_alert,
        approval_rate=0.60,
        approved_count=60,
        declined_count=40,
        unknown_status_count=0,
        signal_codes=["LOW_APPROVAL_RATE", "APPROVAL_RATE_MAD_DROP"],
        criteria_count=2,
        zscore_ta=-4.0,
        zscore_rechazos=0.0,
        rolling_avg_rejections=30.0,
        rolling_q95_rejections=50.0,
        rejection_change_pct=(40 - 30) / 30 * 100,
        priority_score=55.0,
        priority_components={
            "volume_score": 5.0,
            "approval_rate_score": 30.0,
            "anomaly_score": 20.0,
        },
    )

    outcome = PayloadAlertFilter().evaluate(payload)

    assert outcome.reason_codes == [FilterReason.PRIORITY_BELOW_CRITICAL_THRESHOLD]


def test_v2_rejects_recomputed_priority_component_mismatch(v2_alert):
    payload = _raw_v2(v2_alert)
    payload["priority_components"]["volume_score"] = 10.0

    outcome = PayloadAlertFilter().evaluate(payload)

    assert outcome.reason_codes == [FilterReason.PRIORITY_COMPONENTS_MISMATCH]
