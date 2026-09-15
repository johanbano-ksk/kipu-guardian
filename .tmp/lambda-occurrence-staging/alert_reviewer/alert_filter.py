"""Deterministic alert filtering based exclusively on the Kipu event payload."""

from __future__ import annotations

import json
import math
import unicodedata
from dataclasses import dataclass, fields
from datetime import UTC, date, datetime
from enum import StrEnum
from itertools import pairwise
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class FilterPolicy:
    version: str = "2026-08-13.1"
    v2_version: str = "2026-08-18.1"
    v2_producer_policy_version: str = "kipu-main-2026-08-13.1"
    v2_detection_engine: str = "descriptive_v1"
    minimum_transactions: int = 20
    minimum_declined_count: int = 20
    required_criticality: str = "Critica"
    mass_impact_minimum_transactions: int = 100
    mass_impact_max_approval_rate: float = 0.15
    severe_deterioration_minimum_transactions: int = 50
    severe_deterioration_max_approval_rate: float = 0.20
    severe_deterioration_drop_percentage_points: float = 5.0
    extreme_low_rate_minimum_transactions: int = 20
    extreme_low_rate_max_approval_rate: float = 0.10
    extreme_low_rate_drop_percentage_points: float = 10.0
    high_volume_collapse_minimum_transactions: int = 100
    high_volume_collapse_max_approval_rate: float = 0.50
    high_volume_collapse_drop_percentage_points: float = 10.0
    predictive_minimum_transactions: int = 50
    predicted_ar_minimum_gap_percentage_points: float = 5.0
    predicted_declines_minimum_excess_count: int = 5
    predicted_declines_minimum_excess_ratio: float = 0.05
    v2_ta_low_threshold: float = 0.65
    v2_ta_critical_threshold: float = 0.55
    v2_zscore_mad_threshold: float = 3.0
    v2_alert_minimum_criteria: int = 2
    v2_alert_minimum_volume: int = 5
    v2_rejection_doubling_minimum_mean: float = 5.0
    v2_rejection_doubling_multiplier: float = 2.0
    v2_numeric_tolerance: float = 1e-6
    v2_volume_spike_baseline_weeks: int = 4
    v2_volume_spike_minimum_transactions: int = 50
    v2_volume_spike_minimum_baseline: float = 10.0
    v2_volume_spike_minimum_ratio: float = 20.0
    v2_volume_spike_minimum_oldest_activity: int = 1
    v2_tpv_low_threshold: float = 1_000.0
    v2_tpv_mid_threshold: float = 10_000.0
    v2_tpv_high_threshold: float = 100_000.0
    v2_critical_priority_score: float = 70.0
    v2_high_priority_score: float = 50.0
    v2_medium_priority_score: float = 30.0
    v2_isolation_forest_decision_threshold: float = 0.0

    def __post_init__(self) -> None:
        if not self.version.strip() or not self.v2_version.strip():
            raise ValueError("Policy versions cannot be blank")
        if not self.v2_producer_policy_version.strip():
            raise ValueError("Producer policy version cannot be blank")
        if self.v2_detection_engine != "descriptive_v1":
            raise ValueError("Unsupported v2 detection engine")
        if _normalize_criticality(self.required_criticality) != "critica":
            raise ValueError("Critical-only policy requires Critica criticality")

        volume_thresholds = (
            self.minimum_transactions,
            self.minimum_declined_count,
            self.mass_impact_minimum_transactions,
            self.severe_deterioration_minimum_transactions,
            self.extreme_low_rate_minimum_transactions,
            self.high_volume_collapse_minimum_transactions,
            self.predictive_minimum_transactions,
            self.v2_alert_minimum_criteria,
            self.v2_alert_minimum_volume,
            self.v2_volume_spike_baseline_weeks,
            self.v2_volume_spike_minimum_transactions,
            self.v2_volume_spike_minimum_oldest_activity,
        )
        if any(value < 1 for value in volume_thresholds):
            raise ValueError("Volume thresholds must be positive")

        rate_thresholds = (
            self.mass_impact_max_approval_rate,
            self.severe_deterioration_max_approval_rate,
            self.extreme_low_rate_max_approval_rate,
            self.high_volume_collapse_max_approval_rate,
            self.predicted_declines_minimum_excess_ratio,
            self.v2_ta_low_threshold,
            self.v2_ta_critical_threshold,
        )
        if any(not 0 <= value <= 1 for value in rate_thresholds):
            raise ValueError("Rate thresholds must be between 0 and 1")

        drop_thresholds = (
            self.severe_deterioration_drop_percentage_points,
            self.extreme_low_rate_drop_percentage_points,
            self.high_volume_collapse_drop_percentage_points,
            self.predicted_ar_minimum_gap_percentage_points,
        )
        if any(value < 0 for value in drop_thresholds):
            raise ValueError("Approval drop threshold cannot be negative")
        if self.predicted_declines_minimum_excess_count < 0:
            raise ValueError("Predicted decline excess cannot be negative")

        positive_v2_thresholds = (
            self.v2_zscore_mad_threshold,
            self.v2_rejection_doubling_minimum_mean,
            self.v2_rejection_doubling_multiplier,
            self.v2_numeric_tolerance,
            self.v2_volume_spike_minimum_baseline,
            self.v2_volume_spike_minimum_ratio,
            self.v2_tpv_low_threshold,
            self.v2_tpv_mid_threshold,
            self.v2_tpv_high_threshold,
            self.v2_critical_priority_score,
            self.v2_high_priority_score,
            self.v2_medium_priority_score,
        )
        if any(value <= 0 for value in positive_v2_thresholds):
            raise ValueError("Kipu v2 thresholds must be positive")
        if not (
            self.v2_tpv_low_threshold
            < self.v2_tpv_mid_threshold
            < self.v2_tpv_high_threshold
        ):
            raise ValueError("Kipu v2 TPV thresholds must be ordered")
        if self.v2_critical_priority_score > 100:
            raise ValueError("Critical priority score cannot exceed 100")
        if not (
            self.v2_medium_priority_score
            < self.v2_high_priority_score
            < self.v2_critical_priority_score
        ):
            raise ValueError("Kipu v2 criticality thresholds must be ordered")

    @classmethod
    def load(cls, path: Path) -> FilterPolicy:
        with path.open(encoding="utf-8") as file:
            values = yaml.safe_load(file)
        if not isinstance(values, dict):
            raise ValueError("Filter policy YAML must contain an object")
        return cls(**{field.name: values[field.name] for field in fields(cls)})


class FilterReason(StrEnum):
    MISSING_REQUIRED_FIELD = "MISSING_REQUIRED_FIELD"
    UNSUPPORTED_SCHEMA = "UNSUPPORTED_SCHEMA"
    INVALID_TIMESTAMP = "INVALID_TIMESTAMP"
    INVALID_CRITICALITY = "INVALID_CRITICALITY"
    INVALID_RATE = "INVALID_RATE"
    INVALID_COUNTS = "INVALID_COUNTS"
    INSUFFICIENT_VOLUME = "INSUFFICIENT_VOLUME"
    INCONSISTENT_COUNTS = "INCONSISTENT_COUNTS"
    INVALID_OPTIONAL_METRIC = "INVALID_OPTIONAL_METRIC"
    INVALID_QUANTILES = "INVALID_QUANTILES"
    NON_CRITICAL_ALERT = "NON_CRITICAL_ALERT"
    INSUFFICIENT_DECLINE_IMPACT = "INSUFFICIENT_DECLINE_IMPACT"
    NO_CRITICAL_SIGNAL = "NO_CRITICAL_SIGNAL"
    MASS_IMPACT_LOW_APPROVAL_RATE = "MASS_IMPACT_LOW_APPROVAL_RATE"
    SEVERE_APPROVAL_DETERIORATION = "SEVERE_APPROVAL_DETERIORATION"
    EXTREME_LOW_APPROVAL_WITH_DROP = "EXTREME_LOW_APPROVAL_WITH_DROP"
    HIGH_VOLUME_APPROVAL_COLLAPSE = "HIGH_VOLUME_APPROVAL_COLLAPSE"
    MATERIAL_BELOW_PREDICTED_Q10 = "MATERIAL_BELOW_PREDICTED_Q10"
    MATERIAL_DECLINES_ABOVE_PREDICTED_Q90 = (
        "MATERIAL_DECLINES_ABOVE_PREDICTED_Q90"
    )
    SUPERSEDED_BY_V2 = "SUPERSEDED_BY_V2"

    INVALID_V2_METADATA = "INVALID_V2_METADATA"
    PRODUCER_POLICY_DRIFT = "PRODUCER_POLICY_DRIFT"
    INVALID_SIGNAL_CODES = "INVALID_SIGNAL_CODES"
    INCOMPLETE_SIGNAL_EVIDENCE = "INCOMPLETE_SIGNAL_EVIDENCE"
    SIGNAL_EVIDENCE_MISMATCH = "SIGNAL_EVIDENCE_MISMATCH"
    CRITERIA_COUNT_MISMATCH = "CRITERIA_COUNT_MISMATCH"
    KIPU_DETECTION_GATE_NOT_MET = "KIPU_DETECTION_GATE_NOT_MET"
    PRIORITY_BELOW_CRITICAL_THRESHOLD = "PRIORITY_BELOW_CRITICAL_THRESHOLD"
    INVALID_PRIORITY_COMPONENTS = "INVALID_PRIORITY_COMPONENTS"
    PRIORITY_COMPONENTS_MISMATCH = "PRIORITY_COMPONENTS_MISMATCH"
    KIPU_LOW_APPROVAL_RATE = "KIPU_LOW_APPROVAL_RATE"
    KIPU_APPROVAL_RATE_MAD_DROP = "KIPU_APPROVAL_RATE_MAD_DROP"
    KIPU_DECLINES_ABOVE_P95 = "KIPU_DECLINES_ABOVE_P95"
    KIPU_DECLINES_DOUBLED = "KIPU_DECLINES_DOUBLED"
    KIPU_ML_MERCHANT_CONFIRMATION = "KIPU_ML_MERCHANT_CONFIRMATION"
    KIPU_ML_GLOBAL_CONFIRMATION = "KIPU_ML_GLOBAL_CONFIRMATION"
    KIPU_VOLUME_SPIKE = "KIPU_VOLUME_SPIKE"

    # Preserve persisted outcomes created by policy versions still inside the TTL.
    NO_SUPPORTED_SIGNAL = "NO_SUPPORTED_SIGNAL"
    CATASTROPHIC_APPROVAL_RATE = "CATASTROPHIC_APPROVAL_RATE"
    BELOW_PREDICTED_Q10 = "BELOW_PREDICTED_Q10"
    APPROVAL_DROP_FROM_ROLLING_AVERAGE = "APPROVAL_DROP_FROM_ROLLING_AVERAGE"
    DECLINES_ABOVE_PREDICTED_Q90 = "DECLINES_ABOVE_PREDICTED_Q90"


class AlertFilterOutcome(BaseModel):
    alert_id: str
    accepted: bool
    reason_codes: list[FilterReason] = Field(default_factory=list)
    alert: dict[str, Any]
    policy_version: str | None = None


REQUIRED_TEXT_FIELDS = ("alert_id", "merchant_code", "merchant_name", "country")
RATE_FIELDS = (
    "approval_rate",
    "rolling_avg_approval_rate",
    "predicted_ar_q10",
    "predicted_ar_q50",
    "predicted_ar_q90",
)
CRITICALITIES = {"critica", "alta", "media", "baja"}
V2_CRITERIA_CODES = {
    "LOW_APPROVAL_RATE",
    "APPROVAL_RATE_MAD_DROP",
    "DECLINES_ABOVE_P95",
    "DECLINES_DOUBLED",
}
V2_ML_CODES = {"ML_MERCHANT_ANOMALY", "ML_GLOBAL_ANOMALY"}
V2_SIGNAL_CODES = V2_CRITERIA_CODES | V2_ML_CODES | {"VOLUME_SPIKE"}


class PayloadAlertFilter:
    def __init__(self, policy: FilterPolicy | None = None) -> None:
        self.policy = policy or FilterPolicy()

    def evaluate(self, alert: dict[str, Any]) -> AlertFilterOutcome:
        return evaluate_alert(alert, self.policy)


def evaluate_alert(
    alert: dict[str, Any],
    policy: FilterPolicy | None = None,
) -> AlertFilterOutcome:
    """Return a persisted decision without consulting any external source."""

    resolved_policy = policy or FilterPolicy()
    alert_id = str(alert.get("alert_id") or "")

    def reject(reason: FilterReason) -> AlertFilterOutcome:
        return _rejected(
            alert_id,
            alert,
            reason,
            policy_version=resolved_policy.version,
        )

    if alert.get("schema_version") == "2.0":
        return _evaluate_v2(alert, resolved_policy)

    if any(not _nonempty_text(alert.get(field)) for field in REQUIRED_TEXT_FIELDS):
        return reject(FilterReason.MISSING_REQUIRED_FIELD)
    if alert.get("schema_version") != "1.0":
        return reject(FilterReason.UNSUPPORTED_SCHEMA)
    if not _valid_timestamp(alert.get("timestamp")):
        return reject(FilterReason.INVALID_TIMESTAMP)

    superseded_by = alert.get("superseded_by_schema_version")
    if superseded_by is not None:
        if superseded_by != "2.0":
            return reject(FilterReason.INVALID_OPTIONAL_METRIC)
        return reject(FilterReason.SUPERSEDED_BY_V2)

    criticality = alert.get("criticality")
    normalized_criticality = (
        _normalize_criticality(criticality) if isinstance(criticality, str) else ""
    )
    if normalized_criticality not in CRITICALITIES:
        return reject(FilterReason.INVALID_CRITICALITY)

    total = _integer(alert.get("total_transactions"))
    declined = _integer(alert.get("declined_count"))
    approval_rate = _rate(alert.get("approval_rate"))
    if approval_rate is None:
        return reject(FilterReason.INVALID_RATE)
    if total is None or declined is None or total < 0 or declined < 0 or declined > total:
        return reject(FilterReason.INVALID_COUNTS)
    if total < resolved_policy.minimum_transactions:
        return reject(FilterReason.INSUFFICIENT_VOLUME)

    approved_estimate = round(total * approval_rate)
    if approved_estimate + declined > total + 1:
        return reject(FilterReason.INCONSISTENT_COUNTS)

    rates: dict[str, float | None] = {}
    for field in RATE_FIELDS:
        raw = alert.get(field)
        if raw is None:
            rates[field] = None
            continue
        parsed = _rate(raw)
        if parsed is None:
            return reject(FilterReason.INVALID_OPTIONAL_METRIC)
        rates[field] = parsed

    priority_score = alert.get("priority_score")
    if priority_score is not None:
        priority = _finite_number(priority_score)
        if priority is None or not 0 <= priority <= 100:
            return reject(FilterReason.INVALID_OPTIONAL_METRIC)

    predicted_declines = alert.get("predicted_dc_q90")
    if predicted_declines is not None:
        predicted_declines = _finite_number(predicted_declines)
        if predicted_declines is None or predicted_declines < 0:
            return reject(FilterReason.INVALID_OPTIONAL_METRIC)

    quantiles = [
        rates[field]
        for field in ("predicted_ar_q10", "predicted_ar_q50", "predicted_ar_q90")
        if rates[field] is not None
    ]
    if any(left > right for left, right in pairwise(quantiles)):
        return reject(FilterReason.INVALID_QUANTILES)

    if normalized_criticality != _normalize_criticality(
        resolved_policy.required_criticality
    ):
        return reject(FilterReason.NON_CRITICAL_ALERT)
    if declined < resolved_policy.minimum_declined_count:
        return reject(FilterReason.INSUFFICIENT_DECLINE_IMPACT)

    signals: list[FilterReason] = []
    rolling_average = rates["rolling_avg_approval_rate"]
    drop_pp = (
        (rolling_average - approval_rate) * 100
        if rolling_average is not None
        else None
    )

    if (
        total >= resolved_policy.mass_impact_minimum_transactions
        and _at_most(
            approval_rate,
            resolved_policy.mass_impact_max_approval_rate,
        )
    ):
        signals.append(FilterReason.MASS_IMPACT_LOW_APPROVAL_RATE)

    if (
        drop_pp is not None
        and total >= resolved_policy.severe_deterioration_minimum_transactions
        and _at_most(
            approval_rate,
            resolved_policy.severe_deterioration_max_approval_rate,
        )
        and _at_least(
            drop_pp,
            resolved_policy.severe_deterioration_drop_percentage_points,
        )
    ):
        signals.append(FilterReason.SEVERE_APPROVAL_DETERIORATION)

    if (
        drop_pp is not None
        and total >= resolved_policy.extreme_low_rate_minimum_transactions
        and _at_most(
            approval_rate,
            resolved_policy.extreme_low_rate_max_approval_rate,
        )
        and _at_least(
            drop_pp,
            resolved_policy.extreme_low_rate_drop_percentage_points,
        )
    ):
        signals.append(FilterReason.EXTREME_LOW_APPROVAL_WITH_DROP)

    if (
        drop_pp is not None
        and total >= resolved_policy.high_volume_collapse_minimum_transactions
        and _at_most(
            approval_rate,
            resolved_policy.high_volume_collapse_max_approval_rate,
        )
        and _at_least(
            drop_pp,
            resolved_policy.high_volume_collapse_drop_percentage_points,
        )
    ):
        signals.append(FilterReason.HIGH_VOLUME_APPROVAL_COLLAPSE)

    q10 = rates["predicted_ar_q10"]
    if q10 is not None and total >= resolved_policy.predictive_minimum_transactions:
        q10_gap_pp = (q10 - approval_rate) * 100
        if _at_least(
            q10_gap_pp,
            resolved_policy.predicted_ar_minimum_gap_percentage_points,
        ):
            signals.append(FilterReason.MATERIAL_BELOW_PREDICTED_Q10)

    if (
        predicted_declines is not None
        and total >= resolved_policy.predictive_minimum_transactions
    ):
        minimum_excess = max(
            resolved_policy.predicted_declines_minimum_excess_count,
            total * resolved_policy.predicted_declines_minimum_excess_ratio,
        )
        if _at_least(declined - predicted_declines, minimum_excess):
            signals.append(FilterReason.MATERIAL_DECLINES_ABOVE_PREDICTED_Q90)

    if not signals:
        return reject(FilterReason.NO_CRITICAL_SIGNAL)
    return AlertFilterOutcome(
        alert_id=alert_id,
        accepted=True,
        reason_codes=signals,
        alert=alert,
        policy_version=resolved_policy.version,
    )


def _evaluate_v2(alert: dict[str, Any], policy: FilterPolicy) -> AlertFilterOutcome:
    """Validate Kipu v2 by recomputing its structured detector evidence."""

    alert_id = str(alert.get("alert_id") or "")

    def reject(reason: FilterReason) -> AlertFilterOutcome:
        return _rejected(
            alert_id,
            alert,
            reason,
            policy_version=policy.v2_version,
        )

    if any(not _nonempty_text(alert.get(field)) for field in REQUIRED_TEXT_FIELDS):
        return reject(FilterReason.MISSING_REQUIRED_FIELD)

    timestamp = _parse_datetime(alert.get("timestamp"))
    published_at = _parse_datetime(alert.get("published_at"))
    observation_date = _parse_date(alert.get("observation_date"))
    if timestamp is None or published_at is None:
        return reject(FilterReason.INVALID_TIMESTAMP)
    if observation_date is None or not _same_instant(timestamp, published_at):
        return reject(FilterReason.INVALID_V2_METADATA)
    if (
        alert.get("detection_engine") != policy.v2_detection_engine
        or alert.get("policy_version") != policy.v2_producer_policy_version
    ):
        return reject(FilterReason.INVALID_V2_METADATA)

    batch_id = alert.get("batch_id")
    if batch_id is not None and not _nonempty_text(batch_id):
        return reject(FilterReason.INVALID_V2_METADATA)

    threshold_error = _v2_policy_threshold_error(alert.get("policy_thresholds"), policy)
    if threshold_error is not None:
        return reject(threshold_error)

    adaptive_window = _integer(alert.get("adaptive_window"))
    if adaptive_window is None or adaptive_window < 1:
        return reject(FilterReason.INVALID_V2_METADATA)

    criticality = alert.get("criticality")
    normalized_criticality = (
        _normalize_criticality(criticality) if isinstance(criticality, str) else ""
    )
    if normalized_criticality not in CRITICALITIES:
        return reject(FilterReason.INVALID_CRITICALITY)
    if normalized_criticality != "critica":
        return reject(FilterReason.NON_CRITICAL_ALERT)

    approval_rate = _rate(alert.get("approval_rate"))
    if approval_rate is None:
        return reject(FilterReason.INVALID_RATE)
    total = _integer(alert.get("total_transactions"))
    approved = _integer(alert.get("approved_count"))
    declined = _integer(alert.get("declined_count"))
    unknown_status = _integer(alert.get("unknown_status_count"))
    if (
        total is None
        or approved is None
        or declined is None
        or unknown_status is None
        or min(total, approved, declined, unknown_status) < 0
        or approved > total
        or declined > total
        or unknown_status > total
    ):
        return reject(FilterReason.INVALID_COUNTS)
    if approved + declined + unknown_status != total:
        return reject(FilterReason.INCONSISTENT_COUNTS)
    if total == 0 or not math.isclose(
        approval_rate,
        approved / total,
        rel_tol=0.0,
        abs_tol=policy.v2_numeric_tolerance,
    ):
        return reject(FilterReason.INCONSISTENT_COUNTS)

    for rate_field in ("rolling_avg_approval_rate", "historical_avg_approval_rate"):
        raw_rate = alert.get(rate_field)
        if raw_rate is not None and _rate(raw_rate) is None:
            return reject(FilterReason.INVALID_OPTIONAL_METRIC)

    total_approved_amount = alert.get("total_approved_amount")
    if total_approved_amount is not None:
        total_approved_amount = _finite_number(total_approved_amount)
        if total_approved_amount is None or total_approved_amount < 0:
            return reject(FilterReason.INVALID_OPTIONAL_METRIC)

    raw_codes = alert.get("signal_codes")
    if (
        not isinstance(raw_codes, list)
        or any(not isinstance(code, str) for code in raw_codes)
        or len(raw_codes) != len(set(raw_codes))
        or any(code not in V2_SIGNAL_CODES for code in raw_codes)
    ):
        return reject(FilterReason.INVALID_SIGNAL_CODES)
    declared_codes = set(raw_codes)

    criteria_count = _integer(alert.get("criteria_count"))
    if criteria_count is None or not 0 <= criteria_count <= len(V2_CRITERIA_CODES):
        return reject(FilterReason.INVALID_COUNTS)

    flags: dict[str, bool] = {}
    for field in ("is_anomaly_merchant", "is_anomaly_global", "is_volume_spike"):
        raw_flag = alert.get(field)
        if not isinstance(raw_flag, bool):
            return reject(FilterReason.INVALID_OPTIONAL_METRIC)
        flags[field] = raw_flag

    optional_metrics: dict[str, float | None] = {}
    for field in (
        "zscore_ta",
        "zscore_rechazos",
        "rolling_avg_rejections",
        "rolling_q95_rejections",
        "rejection_change_pct",
        "volume_ratio",
        "baseline_weekday_avg",
        "isolation_forest_merchant_score",
        "isolation_forest_global_score",
    ):
        raw_metric = alert.get(field)
        if raw_metric is None:
            optional_metrics[field] = None
            continue
        metric = _finite_number(raw_metric)
        if metric is None:
            return reject(FilterReason.INVALID_OPTIONAL_METRIC)
        if field in {
            "rolling_avg_rejections",
            "rolling_q95_rejections",
            "volume_ratio",
            "baseline_weekday_avg",
        } and metric < 0:
            return reject(FilterReason.INVALID_OPTIONAL_METRIC)
        optional_metrics[field] = metric

    oldest_activity = alert.get("oldest_weekday_activity")
    if oldest_activity is not None:
        oldest_activity = _integer(oldest_activity)
        if oldest_activity is None or oldest_activity < 0:
            return reject(FilterReason.INVALID_OPTIONAL_METRIC)

    zscore_ta = optional_metrics["zscore_ta"]
    zscore_rejections = optional_metrics["zscore_rechazos"]
    rolling_rejections = optional_metrics["rolling_avg_rejections"]
    q95_rejections = optional_metrics["rolling_q95_rejections"]
    rejection_change = optional_metrics["rejection_change_pct"]
    merchant_ml_score = optional_metrics["isolation_forest_merchant_score"]
    global_ml_score = optional_metrics["isolation_forest_global_score"]

    if "APPROVAL_RATE_MAD_DROP" in declared_codes and zscore_ta is None:
        return reject(FilterReason.INCOMPLETE_SIGNAL_EVIDENCE)
    if "DECLINES_ABOVE_P95" in declared_codes and (
        zscore_rejections is None or q95_rejections is None
    ):
        return reject(FilterReason.INCOMPLETE_SIGNAL_EVIDENCE)
    if "DECLINES_DOUBLED" in declared_codes and rolling_rejections is None:
        return reject(FilterReason.INCOMPLETE_SIGNAL_EVIDENCE)

    if rolling_rejections is None:
        if rejection_change is not None:
            return reject(FilterReason.INCOMPLETE_SIGNAL_EVIDENCE)
    elif rolling_rejections > policy.v2_rejection_doubling_minimum_mean:
        if rejection_change is None:
            return reject(FilterReason.INCOMPLETE_SIGNAL_EVIDENCE)
        expected_change = (declined - rolling_rejections) / rolling_rejections * 100
        if not math.isclose(
            rejection_change,
            expected_change,
            rel_tol=0.0,
            abs_tol=policy.v2_numeric_tolerance,
        ):
            return reject(FilterReason.SIGNAL_EVIDENCE_MISMATCH)
    elif rejection_change is not None:
        return reject(FilterReason.SIGNAL_EVIDENCE_MISMATCH)

    actual_codes: set[str] = set()
    if approval_rate < policy.v2_ta_low_threshold:
        actual_codes.add("LOW_APPROVAL_RATE")
    if zscore_ta is not None and zscore_ta < -policy.v2_zscore_mad_threshold:
        actual_codes.add("APPROVAL_RATE_MAD_DROP")
    if (
        zscore_rejections is not None
        and q95_rejections is not None
        and zscore_rejections > policy.v2_zscore_mad_threshold
        and declined > q95_rejections
    ):
        actual_codes.add("DECLINES_ABOVE_P95")
    if (
        rolling_rejections is not None
        and rolling_rejections > policy.v2_rejection_doubling_minimum_mean
        and declined >= policy.v2_rejection_doubling_multiplier * rolling_rejections
    ):
        actual_codes.add("DECLINES_DOUBLED")

    if merchant_ml_score is None:
        if flags["is_anomaly_merchant"] or "ML_MERCHANT_ANOMALY" in declared_codes:
            return reject(FilterReason.INCOMPLETE_SIGNAL_EVIDENCE)
        merchant_ml_supported = False
    else:
        merchant_ml_supported = (
            merchant_ml_score < policy.v2_isolation_forest_decision_threshold
        )
        if flags["is_anomaly_merchant"] != merchant_ml_supported:
            return reject(FilterReason.SIGNAL_EVIDENCE_MISMATCH)
    if global_ml_score is None:
        if flags["is_anomaly_global"] or "ML_GLOBAL_ANOMALY" in declared_codes:
            return reject(FilterReason.INCOMPLETE_SIGNAL_EVIDENCE)
        global_ml_supported = False
    else:
        global_ml_supported = (
            global_ml_score < policy.v2_isolation_forest_decision_threshold
        )
        if flags["is_anomaly_global"] != global_ml_supported:
            return reject(FilterReason.SIGNAL_EVIDENCE_MISMATCH)
    if merchant_ml_supported:
        actual_codes.add("ML_MERCHANT_ANOMALY")
    if global_ml_supported:
        actual_codes.add("ML_GLOBAL_ANOMALY")

    spike_fields_present = any(
        value is not None
        for value in (
            optional_metrics["volume_ratio"],
            optional_metrics["baseline_weekday_avg"],
            oldest_activity,
        )
    )
    spike_declared = "VOLUME_SPIKE" in declared_codes or flags["is_volume_spike"]
    if spike_declared or spike_fields_present:
        volume_ratio = optional_metrics["volume_ratio"]
        baseline = optional_metrics["baseline_weekday_avg"]
        if volume_ratio is None or baseline is None or oldest_activity is None:
            return reject(FilterReason.INCOMPLETE_SIGNAL_EVIDENCE)
        expected_ratio = total / (baseline + 0.1)
        if not math.isclose(
            volume_ratio,
            expected_ratio,
            rel_tol=0.0,
            abs_tol=policy.v2_numeric_tolerance,
        ):
            return reject(FilterReason.SIGNAL_EVIDENCE_MISMATCH)
        spike_supported = (
            total >= policy.v2_volume_spike_minimum_transactions
            and baseline >= policy.v2_volume_spike_minimum_baseline
            and volume_ratio >= policy.v2_volume_spike_minimum_ratio
            and oldest_activity >= policy.v2_volume_spike_minimum_oldest_activity
        )
        if spike_supported:
            actual_codes.add("VOLUME_SPIKE")

    if flags["is_volume_spike"] != ("VOLUME_SPIKE" in actual_codes):
        return reject(FilterReason.SIGNAL_EVIDENCE_MISMATCH)
    if declared_codes != actual_codes:
        return reject(FilterReason.SIGNAL_EVIDENCE_MISMATCH)

    actual_criteria_count = len(actual_codes & V2_CRITERIA_CODES)
    if criteria_count != actual_criteria_count:
        return reject(FilterReason.CRITERIA_COUNT_MISMATCH)

    ml_count = len(actual_codes & V2_ML_CODES)
    meets_detection_gate = "VOLUME_SPIKE" in actual_codes or (
        total >= policy.v2_alert_minimum_volume
        and (
            actual_criteria_count >= policy.v2_alert_minimum_criteria
            or (actual_criteria_count >= 1 and ml_count >= 1)
            or (
                actual_criteria_count >= 1
                and approval_rate < policy.v2_ta_critical_threshold
            )
        )
    )
    if not meets_detection_gate:
        return reject(FilterReason.KIPU_DETECTION_GATE_NOT_MET)

    priority_score = _finite_number(alert.get("priority_score"))
    if priority_score is None or not 0 <= priority_score <= 100:
        return reject(FilterReason.INVALID_OPTIONAL_METRIC)

    expected_components = _v2_priority_components(
        total=total,
        approval_rate=approval_rate,
        total_approved_amount=total_approved_amount,
        signal_codes=actual_codes,
        policy=policy,
    )
    expected_priority = round(sum(expected_components.values()), 1)
    if not math.isclose(
        priority_score,
        expected_priority,
        rel_tol=0.0,
        abs_tol=policy.v2_numeric_tolerance,
    ):
        return reject(FilterReason.PRIORITY_COMPONENTS_MISMATCH)

    raw_components = alert.get("priority_components")
    if not isinstance(raw_components, dict) or set(raw_components) != set(
        expected_components
    ):
        return reject(FilterReason.INVALID_PRIORITY_COMPONENTS)
    parsed_components: dict[str, float] = {}
    for name, expected in expected_components.items():
        value = _finite_number(raw_components.get(name))
        if value is None:
            return reject(FilterReason.INVALID_PRIORITY_COMPONENTS)
        parsed_components[name] = value
        if not math.isclose(
            value,
            expected,
            rel_tol=0.0,
            abs_tol=policy.v2_numeric_tolerance,
        ):
            return reject(FilterReason.PRIORITY_COMPONENTS_MISMATCH)
    if not math.isclose(
        sum(parsed_components.values()),
        priority_score,
        rel_tol=0.0,
        abs_tol=policy.v2_numeric_tolerance,
    ):
        return reject(FilterReason.PRIORITY_COMPONENTS_MISMATCH)

    if priority_score < policy.v2_critical_priority_score:
        return reject(FilterReason.PRIORITY_BELOW_CRITICAL_THRESHOLD)

    reason_by_code = {
        "LOW_APPROVAL_RATE": FilterReason.KIPU_LOW_APPROVAL_RATE,
        "APPROVAL_RATE_MAD_DROP": FilterReason.KIPU_APPROVAL_RATE_MAD_DROP,
        "DECLINES_ABOVE_P95": FilterReason.KIPU_DECLINES_ABOVE_P95,
        "DECLINES_DOUBLED": FilterReason.KIPU_DECLINES_DOUBLED,
        "ML_MERCHANT_ANOMALY": FilterReason.KIPU_ML_MERCHANT_CONFIRMATION,
        "ML_GLOBAL_ANOMALY": FilterReason.KIPU_ML_GLOBAL_CONFIRMATION,
        "VOLUME_SPIKE": FilterReason.KIPU_VOLUME_SPIKE,
    }
    ordered_codes = [
        "LOW_APPROVAL_RATE",
        "APPROVAL_RATE_MAD_DROP",
        "DECLINES_ABOVE_P95",
        "DECLINES_DOUBLED",
        "ML_MERCHANT_ANOMALY",
        "ML_GLOBAL_ANOMALY",
        "VOLUME_SPIKE",
    ]
    return AlertFilterOutcome(
        alert_id=alert_id,
        accepted=True,
        reason_codes=[reason_by_code[code] for code in ordered_codes if code in actual_codes],
        alert=alert,
        policy_version=policy.v2_version,
    )


def _v2_policy_threshold_error(
    raw_thresholds: Any,
    policy: FilterPolicy,
) -> FilterReason | None:
    if not isinstance(raw_thresholds, dict):
        return FilterReason.INVALID_V2_METADATA

    expected: dict[str, int | float] = {
        "ta_low_threshold": policy.v2_ta_low_threshold,
        "ta_critical_threshold": policy.v2_ta_critical_threshold,
        "zscore_mad_threshold": policy.v2_zscore_mad_threshold,
        "alert_min_criteria": policy.v2_alert_minimum_criteria,
        "alert_min_volume": policy.v2_alert_minimum_volume,
        "rejection_doubling_min_mean": policy.v2_rejection_doubling_minimum_mean,
        "rejection_doubling_multiplier": policy.v2_rejection_doubling_multiplier,
        "volume_spike_baseline_weeks": policy.v2_volume_spike_baseline_weeks,
        "volume_spike_min_target": policy.v2_volume_spike_minimum_transactions,
        "volume_spike_min_avg_hist": policy.v2_volume_spike_minimum_baseline,
        "volume_spike_min_ratio": policy.v2_volume_spike_minimum_ratio,
        "tpv_low_threshold": policy.v2_tpv_low_threshold,
        "tpv_mid_threshold": policy.v2_tpv_mid_threshold,
        "tpv_high_threshold": policy.v2_tpv_high_threshold,
        "criticality_critica_threshold": policy.v2_critical_priority_score,
        "criticality_alta_threshold": policy.v2_high_priority_score,
        "criticality_media_threshold": policy.v2_medium_priority_score,
        "isolation_forest_decision_threshold": (
            policy.v2_isolation_forest_decision_threshold
        ),
    }
    if set(raw_thresholds) != set(expected):
        return FilterReason.PRODUCER_POLICY_DRIFT

    integer_fields = {
        "alert_min_criteria",
        "alert_min_volume",
        "volume_spike_baseline_weeks",
        "volume_spike_min_target",
    }
    for name, expected_value in expected.items():
        raw_value = raw_thresholds[name]
        if name in integer_fields:
            parsed: int | float | None = _integer(raw_value)
        else:
            parsed = _finite_number(raw_value)
        if parsed is None:
            return FilterReason.INVALID_V2_METADATA
        if not math.isclose(
            float(parsed),
            float(expected_value),
            rel_tol=0.0,
            abs_tol=policy.v2_numeric_tolerance,
        ):
            return FilterReason.PRODUCER_POLICY_DRIFT
    return None


def _v2_priority_components(
    *,
    total: int,
    approval_rate: float,
    total_approved_amount: float | None,
    signal_codes: set[str],
    policy: FilterPolicy,
) -> dict[str, float]:
    count_score = 5.0
    if total > 10_000:
        count_score = 30.0
    elif total > 1_000:
        count_score = 20.0
    elif total > 100:
        count_score = 10.0

    volume_score = count_score
    if total_approved_amount is not None:
        tpv_score = 5.0
        if total_approved_amount > policy.v2_tpv_high_threshold:
            tpv_score = 30.0
        elif total_approved_amount > policy.v2_tpv_mid_threshold:
            tpv_score = 20.0
        elif total_approved_amount > policy.v2_tpv_low_threshold:
            tpv_score = 10.0
        volume_score = max(volume_score, tpv_score)

    if approval_rate < 0.50:
        approval_score = 40.0
    elif approval_rate < 0.65:
        approval_score = 30.0
    elif approval_rate < 0.75:
        approval_score = 15.0
    else:
        approval_score = 5.0

    anomaly_score = 0.0
    if "DECLINES_ABOVE_P95" in signal_codes:
        anomaly_score += 15.0
    if "LOW_APPROVAL_RATE" in signal_codes:
        anomaly_score += 10.0
    if "APPROVAL_RATE_MAD_DROP" in signal_codes:
        anomaly_score += 10.0
    if approval_rate < policy.v2_ta_critical_threshold:
        anomaly_score += 15.0
    if "ML_MERCHANT_ANOMALY" in signal_codes:
        anomaly_score += 12.0
    if "ML_GLOBAL_ANOMALY" in signal_codes:
        anomaly_score += 8.0
    if "VOLUME_SPIKE" in signal_codes:
        anomaly_score += 15.0

    return {
        "volume_score": volume_score,
        "approval_rate_score": approval_score,
        "anomaly_score": min(anomaly_score, 30.0),
    }


def filter_alerts(
    payload: Any,
    policy: FilterPolicy | None = None,
) -> list[dict[str, Any]]:
    """Return only accepted alert objects, preserving their original fields."""

    return [
        alert
        for alert in extract_alerts(payload)
        if evaluate_alert(alert, policy).accepted
    ]


def extract_alerts(payload: Any) -> list[dict[str, Any]]:
    """Normalize direct alerts, EventBridge/SQS envelopes, and alert batches."""

    if isinstance(payload, list):
        result: list[dict[str, Any]] = []
        for item in payload:
            result.extend(extract_alerts(item))
        return result
    if not isinstance(payload, dict):
        return []

    # Direct alerts take precedence over generic wrapper field names. Kipu
    # preserves producer-specific fields, so an alert may legitimately include
    # keys such as ``detail``, ``alert`` or ``alerts`` as opaque metadata.
    if any(field in payload for field in REQUIRED_TEXT_FIELDS):
        return [payload]

    if isinstance(payload.get("Records"), list):
        result = []
        for record in payload["Records"]:
            if not isinstance(record, dict):
                continue
            body = record.get("body", record.get("Body"))
            if isinstance(body, str):
                try:
                    result.extend(extract_alerts(json.loads(body)))
                except json.JSONDecodeError:
                    continue
            else:
                result.extend(extract_alerts(body))
        return result

    if isinstance(payload.get("alerts"), list):
        return extract_alerts(payload["alerts"])
    if isinstance(payload.get("detail"), dict):
        return extract_alerts(payload["detail"])
    if isinstance(payload.get("alert"), dict):
        return [payload["alert"]]
    return []


def _rejected(
    alert_id: str,
    alert: dict[str, Any],
    reason: FilterReason,
    *,
    policy_version: str | None = None,
) -> AlertFilterOutcome:
    return AlertFilterOutcome(
        alert_id=alert_id,
        accepted=False,
        reason_codes=[reason],
        alert=alert,
        policy_version=policy_version,
    )


def _nonempty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _normalize_criticality(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", value.strip().casefold())
    return "".join(
        character
        for character in normalized
        if not unicodedata.combining(character)
    )


def _at_least(actual: float, threshold: float) -> bool:
    return actual >= threshold or math.isclose(
        actual,
        threshold,
        rel_tol=0.0,
        abs_tol=1e-9,
    )


def _at_most(actual: float, threshold: float) -> bool:
    return actual <= threshold or math.isclose(
        actual,
        threshold,
        rel_tol=0.0,
        abs_tol=1e-9,
    )


def _valid_timestamp(value: Any) -> bool:
    if isinstance(value, datetime):
        return True
    if not isinstance(value, str) or not value.strip():
        return False
    normalized = value.strip().replace("Z", "+00:00")
    if "T" not in normalized and " " not in normalized:
        return False
    try:
        datetime.fromisoformat(normalized)
    except ValueError:
        return False
    return True


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        normalized = value.strip().replace("Z", "+00:00")
        if "T" not in normalized and " " not in normalized:
            return None
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        return None


def _same_instant(left: datetime, right: datetime) -> bool:
    return math.isclose(
        left.timestamp(),
        right.timestamp(),
        rel_tol=0.0,
        abs_tol=1e-6,
    )


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _rate(value: Any) -> float | None:
    parsed = _finite_number(value)
    if parsed is None or not 0 <= parsed <= 1:
        return None
    return parsed
