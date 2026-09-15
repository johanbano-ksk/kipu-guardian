from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from alert_reviewer.kipu_contract import Criticality, KipuAlert, KipuAlertV2


@pytest.fixture
def alert() -> KipuAlert:
    return KipuAlert(
        schema_version="1.0",
        alert_id="alert-001",
        merchant_code="merchant-001",
        merchant_name="Example Merchant",
        country="Ecuador",
        timestamp=datetime(2026, 7, 29, 12, tzinfo=UTC),
        criticality=Criticality.CRITICA,
        approval_rate=0.20,
        total_transactions=100,
        declined_count=80,
        rolling_avg_approval_rate=0.25,
        alert_summary="TA fuera de intervalo",
        top_rejections="Fondos insuficientes (20 - 50.0%)",
    )


@pytest.fixture
def v2_alert() -> KipuAlertV2:
    published_at = datetime(2026, 8, 18, 12, tzinfo=UTC)
    return KipuAlertV2(
        schema_version="2.0",
        alert_id="v2-alert-001",
        merchant_code="merchant-001",
        merchant_name="Example Merchant",
        country="Ecuador",
        observation_date=date(2026, 8, 18).isoformat(),
        published_at=published_at.isoformat(),
        timestamp=published_at.isoformat(),
        detection_engine="descriptive_v1",
        policy_version="kipu-main-2026-08-13.1",
        criticality=Criticality.CRITICA,
        approval_rate=0.40,
        total_transactions=100,
        approved_count=40,
        declined_count=58,
        unknown_status_count=2,
        rolling_avg_approval_rate=0.60,
        historical_avg_approval_rate=0.62,
        total_approved_amount=0.0,
        signal_codes=[
            "LOW_APPROVAL_RATE",
            "APPROVAL_RATE_MAD_DROP",
            "DECLINES_ABOVE_P95",
            "DECLINES_DOUBLED",
        ],
        criteria_count=4,
        zscore_ta=-4.0,
        zscore_rechazos=4.0,
        rolling_avg_rejections=20.0,
        rolling_q95_rejections=50.0,
        rejection_change_pct=190.0,
        is_anomaly_merchant=False,
        is_anomaly_global=False,
        isolation_forest_merchant_score=0.1,
        isolation_forest_global_score=0.2,
        is_volume_spike=False,
        adaptive_window=30,
        priority_score=75.0,
        priority_components={
            "volume_score": 5.0,
            "approval_rate_score": 40.0,
            "anomaly_score": 30.0,
        },
        policy_thresholds={
            "ta_low_threshold": 0.65,
            "ta_critical_threshold": 0.55,
            "zscore_mad_threshold": 3.0,
            "alert_min_criteria": 2,
            "alert_min_volume": 5,
            "rejection_doubling_min_mean": 5.0,
            "rejection_doubling_multiplier": 2.0,
            "volume_spike_baseline_weeks": 4,
            "volume_spike_min_target": 50,
            "volume_spike_min_avg_hist": 10.0,
            "volume_spike_min_ratio": 20.0,
            "tpv_low_threshold": 1000.0,
            "tpv_mid_threshold": 10000.0,
            "tpv_high_threshold": 100000.0,
            "criticality_critica_threshold": 70.0,
            "criticality_alta_threshold": 50.0,
            "criticality_media_threshold": 30.0,
            "isolation_forest_decision_threshold": 0.0,
        },
    )
