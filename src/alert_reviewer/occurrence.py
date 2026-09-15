"""Durable reviewer-owned archive of Kipu EventBridge occurrences."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

from botocore.exceptions import ClientError

OCCURRENCE_RECORD_TYPE = "ALERT_OCCURRENCE"
OCCURRENCE_SCHEMA_VERSION = "1.0"
OCCURRENCE_KEY_PREFIX = "occurrence#"
DECISION_KEY_PREFIX = "decision#"

# DynamoDB rejects items above 400 KiB.  This lower application limit includes
# the low-level attribute envelope and leaves headroom for service accounting.
MAX_OCCURRENCE_ITEM_BYTES = 350 * 1024


class OccurrenceItemTooLargeError(ValueError):
    """Raised before DynamoDB I/O when an occurrence cannot fit safely."""


@dataclass(frozen=True)
class AlertOccurrence:
    """One supported EventBridge event before reviewer decision deduplication."""

    occurrence_key: str
    decision_key: str
    event_id: str | None
    event_source: str
    detail_type: str
    schema_version: str
    alert_id: str
    event_time: datetime | None
    alert_timestamp: datetime
    publication_timestamp: datetime
    observation_date: date | None
    raw_alert: dict[str, Any]


def build_occurrence(
    event: dict[str, Any],
    *,
    raw_alert: dict[str, Any],
    alert_timestamp: datetime,
    observation_date: date | None,
    serialized_event: str,
) -> AlertOccurrence:
    """Build capture metadata while preserving the original alert object."""

    event_id_value = event.get("id")
    event_id = (
        event_id_value.strip()
        if isinstance(event_id_value, str) and event_id_value.strip()
        else None
    )
    if event_id is not None:
        identity_suffix = f"event:{event_id}"
    else:
        digest = hashlib.sha256(serialized_event.encode("utf-8")).hexdigest()
        identity_suffix = f"sha256:{digest}"

    normalized_alert_timestamp = _utc(alert_timestamp)
    event_time = _optional_aware_datetime(event.get("time"))
    publication_timestamp = event_time or normalized_alert_timestamp
    return AlertOccurrence(
        occurrence_key=f"{OCCURRENCE_KEY_PREFIX}{identity_suffix}",
        decision_key=f"{DECISION_KEY_PREFIX}{identity_suffix}",
        event_id=event_id,
        event_source=str(event["source"]),
        detail_type=str(event["detail-type"]),
        schema_version=str(raw_alert["schema_version"]),
        alert_id=str(raw_alert["alert_id"]),
        event_time=event_time,
        alert_timestamp=normalized_alert_timestamp,
        publication_timestamp=publication_timestamp,
        observation_date=observation_date,
        raw_alert=raw_alert,
    )


class DynamoDBOccurrenceStore:
    """Append each EventBridge occurrence once in the existing reviewer table."""

    def __init__(self, client: Any, table_name: str, *, ttl_hours: int) -> None:
        self.client = client
        self.table_name = table_name
        self.ttl_hours = ttl_hours

    async def capture(self, occurrence: AlertOccurrence) -> bool:
        """Return true for a new event and false for an already captured retry."""

        recorded_at = datetime.now(UTC)
        raw_payload = json.dumps(
            occurrence.raw_alert,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        raw_payload_sha256 = hashlib.sha256(raw_payload.encode("utf-8")).hexdigest()
        item = {
            "idempotency_key": {"S": occurrence.occurrence_key},
            "record_type": {"S": OCCURRENCE_RECORD_TYPE},
            "occurrence_schema_version": {"S": OCCURRENCE_SCHEMA_VERSION},
            "event_source": {"S": occurrence.event_source},
            "detail_type": {"S": occurrence.detail_type},
            "schema_version": {"S": occurrence.schema_version},
            "alert_id": {"S": occurrence.alert_id},
            "alert_timestamp": {"S": occurrence.alert_timestamp.isoformat()},
            "publication_timestamp": {
                "S": occurrence.publication_timestamp.isoformat()
            },
            "recorded_at": {"S": recorded_at.isoformat()},
            "raw_payload": {"S": raw_payload},
            "raw_payload_sha256": {
                "S": raw_payload_sha256
            },
            "expires_at": {
                "N": str(int((recorded_at + timedelta(hours=self.ttl_hours)).timestamp()))
            },
        }
        if occurrence.event_id is not None:
            item["event_id"] = {"S": occurrence.event_id}
        if occurrence.event_time is not None:
            item["event_time"] = {"S": occurrence.event_time.isoformat()}
        if occurrence.observation_date is not None:
            item["observation_date"] = {
                "S": occurrence.observation_date.isoformat()
            }

        estimated_item_bytes = len(
            json.dumps(
                item,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        )
        if estimated_item_bytes > MAX_OCCURRENCE_ITEM_BYTES:
            raise OccurrenceItemTooLargeError(
                "Occurrence item is too large for the reviewer archive: "
                f"estimated {estimated_item_bytes} bytes exceeds the "
                f"{MAX_OCCURRENCE_ITEM_BYTES}-byte application limit"
            )

        try:
            await asyncio.to_thread(
                self.client.put_item,
                TableName=self.table_name,
                Item=item,
                ConditionExpression="attribute_not_exists(idempotency_key)",
            )
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != (
                "ConditionalCheckFailedException"
            ):
                raise
        response = await asyncio.to_thread(
            self.client.get_item,
            TableName=self.table_name,
            Key={"idempotency_key": {"S": occurrence.occurrence_key}},
            ConsistentRead=True,
        )
        existing_digest = (
            response.get("Item", {}).get("raw_payload_sha256", {}).get("S")
        )
        if existing_digest == raw_payload_sha256:
            return False
        raise RuntimeError("Occurrence identity collision has a different payload")


def _optional_aware_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
