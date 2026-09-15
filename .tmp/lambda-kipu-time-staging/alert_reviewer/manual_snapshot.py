"""Manual Kipu snapshot extraction using the runtime filter policy."""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

from alert_reviewer.alert_filter import FilterPolicy, PayloadAlertFilter

PUBLISHABLE_CRITICALITIES = {"Critica", "Alta"}
REPLAY_NAMESPACE = uuid.UUID("d50b609d-4e01-55e7-8386-b3a65172ff74")


def snapshot_key(day: date) -> str:
    return f"alerts/year={day:%Y}/month={day:%m}/day={day:%d}/alerts.json"


def sanitize(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, dict):
        return {key: sanitize(item) for key, item in value.items()}
    return value


def replay_alert_id(day: date, row: dict[str, Any]) -> str:
    identity = "|".join(
        (
            day.isoformat(),
            str(row.get("merchant_code", "")),
            str(row.get("country", "")),
            str(row.get("batch_id", "")),
        )
    )
    return str(uuid.uuid5(REPLAY_NAMESPACE, identity))


def kipu_generated_at(row: dict[str, Any]) -> str | None:
    """Normalize Kipu's persisted notification time without inventing one."""
    value = row.get("notified_at")
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise ValueError("Kipu notified_at must be a string")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d-%H:%M").replace(tzinfo=UTC)
    except ValueError as exc:
        raise ValueError("Kipu notified_at must use YYYY-MM-DD-HH:MM") from exc
    return parsed.isoformat()


def project_eventbridge_v1(
    rows: list[dict[str, Any]],
    day: date,
    *,
    replay_timestamp: datetime | None = None,
) -> list[dict[str, Any]]:
    expected_day = day.isoformat()
    unexpected_dates = {
        str(row.get("date", ""))[:10]
        for row in rows
        if str(row.get("date", ""))[:10] != expected_day
    }
    if unexpected_dates:
        raise ValueError(
            f"Snapshot contains dates outside {expected_day}: {sorted(unexpected_dates)}"
        )

    timestamp = (replay_timestamp or datetime.now(UTC)).isoformat()
    projected: list[dict[str, Any]] = []
    for row in rows:
        if row.get("criticality") not in PUBLISHABLE_CRITICALITIES:
            continue

        detail = {
            "schema_version": "1.0",
            "alert_id": replay_alert_id(day, row),
            "merchant_code": row.get("merchant_code"),
            "merchant_name": row.get("merchant_name"),
            "country": row.get("country"),
            "criticality": row.get("criticality", "unknown"),
            "anomaly_type": row.get("cluster_profile", "unknown"),
            "approval_rate": row.get("approval_rate"),
            "rolling_avg_approval_rate": row.get("rolling_avg_approval_rate"),
            "total_transactions": row.get("total_transactions"),
            "declined_count": row.get("declined_count"),
            "top_rejections": row.get("top_rejections"),
            "alert_summary": row.get("alert_summary"),
            "timestamp": timestamp,
        }
        generated_at = kipu_generated_at(row)
        if generated_at is not None:
            detail["kipu_generated_at"] = generated_at
        if row.get("batch_id"):
            detail["batch_id"] = row["batch_id"]
        if row.get("group_name"):
            detail["group_name"] = row["group_name"]
        projected.append(sanitize(detail))
    return projected


def filter_snapshot(
    rows: list[dict[str, Any]],
    day: date,
    *,
    policy: FilterPolicy,
    replay_timestamp: datetime | None = None,
    ai_selector: Callable[[list[dict[str, Any]]], set[int]] | None = None,
) -> tuple[list[dict[str, Any]], int]:
    projected = project_eventbridge_v1(rows, day, replay_timestamp=replay_timestamp)
    alert_filter = PayloadAlertFilter(policy)
    policy_accepted_indices = {
        index for index, alert in enumerate(projected) if alert_filter.evaluate(alert).accepted
    }
    if ai_selector is None:
        accepted_indices = policy_accepted_indices
    else:
        accepted_indices = policy_accepted_indices & ai_selector(projected)
    accepted = [alert for index, alert in enumerate(projected) if index in accepted_indices]
    return accepted, len(projected)
