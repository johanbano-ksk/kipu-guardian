"""Manual Kipu snapshot extraction using the runtime filter policy."""

from __future__ import annotations

import json
import math
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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


def filter_occurrence_snapshot(
    records: list[dict[str, Any]],
    day: date,
    *,
    policy: FilterPolicy,
    timezone: str = "America/Guayaquil",
    ai_selector: Callable[[list[dict[str, Any]]], set[int]] | None = None,
) -> tuple[list[dict[str, Any]], int, int]:
    """Filter the latest Kipu EventBridge occurrence for each alert id."""
    try:
        business_timezone = ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown dashboard timezone: {timezone}") from exc

    source_count = 0
    latest: dict[str, tuple[tuple[datetime, datetime, str], dict[str, Any]]] = {}
    for record in records:
        publication_timestamp = _aware_datetime(
            record.get("publication_timestamp"),
            "publication_timestamp",
        )
        if publication_timestamp.astimezone(business_timezone).date() != day:
            continue
        source_count += 1
        if (
            record.get("record_type") != "ALERT_OCCURRENCE"
            or record.get("event_source") != "acceptance.kipu"
            or record.get("detail_type") != "Anomaly Detected v1"
            or record.get("schema_version") != "1.0"
        ):
            continue

        raw_payload = record.get("raw_payload")
        try:
            alert = json.loads(raw_payload) if isinstance(raw_payload, str) else raw_payload
        except json.JSONDecodeError as exc:
            raise ValueError("Occurrence raw_payload must be valid JSON") from exc
        if not isinstance(alert, dict):
            raise ValueError("Occurrence raw_payload must be a JSON object")
        alert_id = str(alert.get("alert_id") or "")
        if not alert_id or alert_id != record.get("alert_id"):
            raise ValueError("Occurrence alert_id is inconsistent with raw_payload")
        if alert.get("schema_version") != record.get("schema_version"):
            raise ValueError("Occurrence schema_version is inconsistent with raw_payload")

        recorded_at = _aware_datetime(
            record.get("recorded_at", publication_timestamp.isoformat()),
            "recorded_at",
        )
        occurrence_key = str(record.get("idempotency_key") or "")
        tie_break = publication_timestamp, recorded_at, occurrence_key
        current = latest.get(alert_id)
        if current is None or tie_break > current[0]:
            latest[alert_id] = tie_break, sanitize(alert)

    candidates = [entry[1] for entry in sorted(latest.values(), key=lambda item: item[0])]
    alert_filter = PayloadAlertFilter(policy)
    policy_accepted_indices = {
        index
        for index, alert in enumerate(candidates)
        if alert_filter.evaluate(alert).accepted
    }
    if ai_selector is None:
        accepted_indices = policy_accepted_indices
    else:
        accepted_indices = policy_accepted_indices & ai_selector(candidates)

    accepted: list[dict[str, Any]] = []
    for index, alert in enumerate(candidates):
        if index not in accepted_indices:
            continue
        dashboard_alert = dict(alert)
        dashboard_alert["kipu_generated_at"] = alert["timestamp"]
        accepted.append(dashboard_alert)
    return accepted, len(candidates), source_count


def _aware_datetime(value: Any, field: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Occurrence {field} must be an ISO datetime")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Occurrence {field} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Occurrence {field} must include a UTC offset")
    return parsed.astimezone(UTC)
