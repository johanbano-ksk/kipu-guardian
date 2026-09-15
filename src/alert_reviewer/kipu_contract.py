"""Typed boundary for Kipu anomaly events consumed by the worker."""

from __future__ import annotations

import unicodedata
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Rate = Annotated[float, Field(ge=0.0, le=1.0)]


class Criticality(StrEnum):
    CRITICA = "Critica"
    CRITICA_ACCENTED = "Crítica"
    ALTA = "Alta"
    MEDIA = "Media"
    BAJA = "Baja"


class RejectionCause(BaseModel):
    reason: str
    count: int = Field(ge=0)


class _KipuAlertBase(BaseModel):
    """Fields shared by the two versioned Kipu contracts."""

    model_config = ConfigDict(allow_inf_nan=False, extra="ignore")

    alert_id: str = Field(min_length=1)
    merchant_code: str = Field(min_length=1)
    merchant_name: str = Field(min_length=1)
    country: str = Field(min_length=1)
    timestamp: datetime
    criticality: Criticality
    approval_rate: Rate
    total_transactions: int = Field(ge=0)
    declined_count: int = Field(ge=0)
    @field_validator("alert_id", "merchant_code", "merchant_name", "country")
    @classmethod
    def reject_blank_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Required text fields cannot be blank")
        return normalized

    @field_validator("criticality", mode="before")
    @classmethod
    def normalize_recognized_criticality(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        normalized = unicodedata.normalize("NFKD", value.strip().casefold())
        key = "".join(
            character
            for character in normalized
            if not unicodedata.combining(character)
        )
        return {
            "critica": Criticality.CRITICA.value,
            "alta": Criticality.ALTA.value,
            "media": Criticality.MEDIA.value,
            "baja": Criticality.BAJA.value,
        }.get(key, value)

    @field_validator("total_transactions", "declined_count", mode="before")
    @classmethod
    def reject_coerced_counts(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Counts must be JSON integers")
        return value

    @field_validator(
        "approval_rate",
        mode="before",
    )
    @classmethod
    def reject_coerced_numeric_metrics(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("Metrics must be JSON numbers")
        return value

    @field_validator("timestamp", mode="before")
    @classmethod
    def require_iso_datetime_string(cls, value: object) -> object:
        if isinstance(value, datetime):
            return value
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Timestamp must be an ISO 8601 JSON string")
        normalized = value.strip().replace("Z", "+00:00")
        if "T" not in normalized and " " not in normalized:
            raise ValueError("Timestamp must include a date and time")
        try:
            datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise ValueError("Timestamp must be ISO 8601") from exc
        return value

    @field_validator("timestamp")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)


class KipuAlert(_KipuAlertBase):
    """Required Kipu v1 fields plus the optional metrics used by its filter."""

    schema_version: Literal["1.0"]
    rolling_avg_approval_rate: Rate | None = None
    priority_score: float | None = Field(default=None, ge=0.0, le=100.0)
    alert_summary: str | None = None
    anomaly_type: str | None = None
    predicted_ar_q10: Rate | None = None
    predicted_ar_q50: Rate | None = None
    predicted_ar_q90: Rate | None = None
    predicted_dc_q90: float | None = Field(default=None, ge=0.0)
    top_rejections: str | list[RejectionCause] | None = None
    superseded_by_schema_version: Literal["2.0"] | None = None

    @field_validator(
        "rolling_avg_approval_rate",
        "priority_score",
        "predicted_ar_q10",
        "predicted_ar_q50",
        "predicted_ar_q90",
        "predicted_dc_q90",
        mode="before",
    )
    @classmethod
    def reject_coerced_optional_metrics(cls, value: object) -> object:
        return cls.reject_coerced_numeric_metrics(value)


class KipuSignalCode(StrEnum):
    LOW_APPROVAL_RATE = "LOW_APPROVAL_RATE"
    APPROVAL_RATE_MAD_DROP = "APPROVAL_RATE_MAD_DROP"
    DECLINES_ABOVE_P95 = "DECLINES_ABOVE_P95"
    DECLINES_DOUBLED = "DECLINES_DOUBLED"
    ML_MERCHANT_ANOMALY = "ML_MERCHANT_ANOMALY"
    ML_GLOBAL_ANOMALY = "ML_GLOBAL_ANOMALY"
    VOLUME_SPIKE = "VOLUME_SPIKE"


class KipuPolicyThresholds(BaseModel):
    """Producer settings that v2 exposes for drift detection."""

    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    ta_low_threshold: float
    ta_critical_threshold: float
    zscore_mad_threshold: float
    alert_min_criteria: int
    alert_min_volume: int
    rejection_doubling_min_mean: float
    rejection_doubling_multiplier: float
    volume_spike_baseline_weeks: int
    volume_spike_min_target: int
    volume_spike_min_avg_hist: float
    volume_spike_min_ratio: float
    tpv_low_threshold: float
    tpv_mid_threshold: float
    tpv_high_threshold: float
    criticality_critica_threshold: float
    criticality_alta_threshold: float
    criticality_media_threshold: float
    isolation_forest_decision_threshold: float

    @field_validator("alert_min_criteria", "alert_min_volume", mode="before")
    @classmethod
    def reject_coerced_integer_thresholds(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("Integer thresholds must be JSON integers")
        return value

    @field_validator(
        "volume_spike_baseline_weeks",
        "volume_spike_min_target",
        mode="before",
    )
    @classmethod
    def reject_coerced_volume_thresholds(cls, value: object) -> object:
        return cls.reject_coerced_integer_thresholds(value)

    @field_validator(
        "ta_low_threshold",
        "ta_critical_threshold",
        "zscore_mad_threshold",
        "rejection_doubling_min_mean",
        "rejection_doubling_multiplier",
        "volume_spike_min_avg_hist",
        "volume_spike_min_ratio",
        "tpv_low_threshold",
        "tpv_mid_threshold",
        "tpv_high_threshold",
        "criticality_critica_threshold",
        "criticality_alta_threshold",
        "criticality_media_threshold",
        "isolation_forest_decision_threshold",
        mode="before",
    )
    @classmethod
    def reject_coerced_numeric_thresholds(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("Thresholds must be JSON numbers")
        return value


class KipuPriorityComponents(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")

    volume_score: float = Field(ge=0.0, le=30.0)
    approval_rate_score: float = Field(ge=0.0, le=40.0)
    anomaly_score: float = Field(ge=0.0, le=30.0)

    @field_validator("volume_score", "approval_rate_score", "anomaly_score", mode="before")
    @classmethod
    def reject_coerced_component_scores(cls, value: object) -> object:
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise ValueError("Priority components must be JSON numbers")
        return value


class KipuAlertV2(_KipuAlertBase):
    """Evidence-bearing contract emitted by Kipu's descriptive detector."""

    schema_version: Literal["2.0"]
    observation_date: date
    published_at: datetime
    detection_engine: Literal["descriptive_v1"]
    policy_version: str = Field(min_length=1)
    batch_id: str | None = None
    cluster_profile: str | None = None

    approved_count: int = Field(ge=0)
    unknown_status_count: int = Field(ge=0)
    rolling_avg_approval_rate: Rate | None = None
    historical_avg_approval_rate: Rate | None = None
    total_approved_amount: float | None = Field(default=None, ge=0.0)

    signal_codes: list[KipuSignalCode]
    criteria_count: int = Field(ge=0, le=4)
    zscore_ta: float | None = None
    zscore_rechazos: float | None = None
    rolling_avg_rejections: float | None = Field(default=None, ge=0.0)
    rolling_q95_rejections: float | None = Field(default=None, ge=0.0)
    rejection_change_pct: float | None = None
    is_anomaly_merchant: bool
    is_anomaly_global: bool
    isolation_forest_merchant_score: float | None = None
    isolation_forest_global_score: float | None = None
    is_volume_spike: bool
    volume_ratio: float | None = Field(default=None, ge=0.0)
    baseline_weekday_avg: float | None = Field(default=None, ge=0.0)
    oldest_weekday_activity: int | None = Field(default=None, ge=0)
    adaptive_window: int = Field(ge=1)

    priority_score: float = Field(ge=0.0, le=100.0)
    priority_components: KipuPriorityComponents
    policy_thresholds: KipuPolicyThresholds

    alert_summary: str | None = None
    top_rejections: str | list[RejectionCause] | None = None

    @field_validator("policy_version")
    @classmethod
    def reject_blank_policy_version(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Policy version cannot be blank")
        return normalized

    @field_validator("batch_id", mode="before")
    @classmethod
    def validate_optional_batch_id(cls, value: object) -> object:
        if value is None:
            return value
        if not isinstance(value, str) or not value.strip():
            raise ValueError("Batch id must be a non-empty JSON string")
        return value.strip()

    @field_validator(
        "approved_count",
        "unknown_status_count",
        "criteria_count",
        "oldest_weekday_activity",
        "adaptive_window",
        mode="before",
    )
    @classmethod
    def reject_coerced_v2_counts(cls, value: object) -> object:
        if value is None:
            return value
        return cls.reject_coerced_counts(value)

    @field_validator("is_anomaly_merchant", "is_anomaly_global", "is_volume_spike", mode="before")
    @classmethod
    def reject_coerced_flags(cls, value: object) -> object:
        if not isinstance(value, bool):
            raise ValueError("Anomaly flags must be JSON booleans")
        return value

    @field_validator(
        "rolling_avg_approval_rate",
        "historical_avg_approval_rate",
        "total_approved_amount",
        "zscore_ta",
        "zscore_rechazos",
        "rolling_avg_rejections",
        "rolling_q95_rejections",
        "rejection_change_pct",
        "isolation_forest_merchant_score",
        "isolation_forest_global_score",
        "volume_ratio",
        "baseline_weekday_avg",
        "priority_score",
        mode="before",
    )
    @classmethod
    def reject_coerced_v2_metrics(cls, value: object) -> object:
        return cls.reject_coerced_numeric_metrics(value)

    @field_validator("signal_codes")
    @classmethod
    def require_unique_signal_codes(
        cls,
        value: list[KipuSignalCode],
    ) -> list[KipuSignalCode]:
        if len(value) != len(set(value)):
            raise ValueError("Signal codes must be unique")
        return value

    @field_validator("observation_date", mode="before")
    @classmethod
    def require_iso_observation_date(cls, value: object) -> object:
        if not isinstance(value, str):
            raise ValueError("Observation date must be an ISO date JSON string")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("Observation date must be ISO 8601") from exc
        return value

    @field_validator("published_at", mode="before")
    @classmethod
    def require_iso_published_at(cls, value: object) -> object:
        return cls.require_iso_datetime_string(value)

    @field_validator("published_at")
    @classmethod
    def normalize_published_at(cls, value: datetime) -> datetime:
        return cls.normalize_timestamp(value)
