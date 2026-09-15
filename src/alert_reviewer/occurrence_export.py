"""Export reviewer-owned DynamoDB occurrences for manual alert review."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from boto3.dynamodb.types import TypeDeserializer

from alert_reviewer.alert_filter import FilterPolicy, evaluate_alert
from alert_reviewer.occurrence import (
    OCCURRENCE_KEY_PREFIX,
    OCCURRENCE_RECORD_TYPE,
    OCCURRENCE_SCHEMA_VERSION,
)
from alert_reviewer.queue import (
    KIPU_DETAIL_TYPE,
    KIPU_DETAIL_TYPE_V2,
    KIPU_EVENT_SOURCE,
)

DEFAULT_TIMEZONE = "America/Guayaquil"
DateBasis = Literal["publication", "observation"]


@dataclass(frozen=True)
class StoredOccurrence:
    occurrence_key: str
    event_source: str
    detail_type: str
    schema_version: str
    alert_id: str
    event_time: datetime | None
    alert_timestamp: datetime
    publication_timestamp: datetime
    observation_date: date | None
    recorded_at: datetime
    raw_alert: dict[str, Any]

    @property
    def tie_break(self) -> tuple[datetime, datetime, str]:
        return self.publication_timestamp, self.recorded_at, self.occurrence_key


def parse_occurrence_document(document: Any, *, source: str) -> StoredOccurrence:
    """Validate one decoded DynamoDB occurrence without changing its payload."""

    if not isinstance(document, dict):
        raise ValueError(f"Occurrence {source} must be a JSON object")
    if document.get("record_type") != OCCURRENCE_RECORD_TYPE:
        raise ValueError(f"Occurrence {source} has an unsupported record type")
    if document.get("occurrence_schema_version") != OCCURRENCE_SCHEMA_VERSION:
        raise ValueError(f"Occurrence {source} has an unsupported schema")

    occurrence_key = _nonempty_text(document.get("idempotency_key"), "key", source)
    if not occurrence_key.startswith(OCCURRENCE_KEY_PREFIX):
        raise ValueError(f"Occurrence {source} key has an invalid namespace")
    event_source = _nonempty_text(document.get("event_source"), "event_source", source)
    detail_type = _nonempty_text(document.get("detail_type"), "detail_type", source)
    schema_version = _nonempty_text(
        document.get("schema_version"),
        "schema_version",
        source,
    )
    alert_id = _nonempty_text(document.get("alert_id"), "alert_id", source)
    alert_timestamp = _aware_datetime(
        document.get("alert_timestamp"),
        "alert_timestamp",
        source,
    )
    publication_timestamp = _aware_datetime(
        document.get("publication_timestamp"),
        "publication_timestamp",
        source,
    )
    recorded_at = _aware_datetime(document.get("recorded_at"), "recorded_at", source)
    event_time = _optional_aware_datetime(document.get("event_time"), "event_time", source)
    if event_time is not None and publication_timestamp != event_time:
        raise ValueError(f"Occurrence {source} publication does not match EventBridge time")
    if event_time is None and publication_timestamp != alert_timestamp:
        raise ValueError(f"Occurrence {source} publication fallback is inconsistent")
    observation_date = _optional_iso_date(
        document.get("observation_date"),
        "observation_date",
        source,
    )
    raw_value = document.get("raw_payload")
    if isinstance(raw_value, str):
        raw_serialized = raw_value
        try:
            raw_alert = json.loads(raw_serialized)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Occurrence {source} raw_payload is invalid JSON") from exc
    elif isinstance(raw_value, dict):
        raw_alert = raw_value
        raw_serialized = json.dumps(
            raw_alert,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
    else:
        raise ValueError(f"Occurrence {source} raw_payload must be JSON object data")
    if not isinstance(raw_alert, dict):
        raise ValueError(f"Occurrence {source} raw_payload must decode to an object")

    expected_digest = document.get("raw_payload_sha256")
    if expected_digest is not None:
        digest = hashlib.sha256(raw_serialized.encode("utf-8")).hexdigest()
        if expected_digest != digest:
            raise ValueError(f"Occurrence {source} raw payload checksum is inconsistent")
    if raw_alert.get("schema_version") != schema_version:
        raise ValueError(f"Occurrence {source} raw schema is inconsistent")
    if raw_alert.get("alert_id") != alert_id:
        raise ValueError(f"Occurrence {source} raw alert id is inconsistent")
    if schema_version in {"1.0", "2.0"}:
        contractual_field = "timestamp" if schema_version == "1.0" else "published_at"
        contractual_timestamp = _contract_datetime(
            raw_alert.get(contractual_field),
            f"raw_payload.{contractual_field}",
            source,
        )
        if contractual_timestamp != alert_timestamp:
            raise ValueError(f"Occurrence {source} contractual timestamp is inconsistent")

    return StoredOccurrence(
        occurrence_key=occurrence_key,
        event_source=event_source,
        detail_type=detail_type,
        schema_version=schema_version,
        alert_id=alert_id,
        event_time=event_time,
        alert_timestamp=alert_timestamp,
        publication_timestamp=publication_timestamp,
        observation_date=observation_date,
        recorded_at=recorded_at,
        raw_alert=raw_alert,
    )


def load_dynamodb_occurrences(client: Any, *, table_name: str) -> list[StoredOccurrence]:
    """Scan every reviewer occurrence, following DynamoDB pagination."""

    documents: list[tuple[dict[str, Any], str]] = []
    exclusive_start_key: dict[str, Any] | None = None
    page_number = 0
    while True:
        page_number += 1
        request: dict[str, Any] = {
            "TableName": table_name,
            "ConsistentRead": True,
            "FilterExpression": "#record_type = :occurrence",
            "ExpressionAttributeNames": {"#record_type": "record_type"},
            "ExpressionAttributeValues": {
                ":occurrence": {"S": OCCURRENCE_RECORD_TYPE}
            },
        }
        if exclusive_start_key is not None:
            request["ExclusiveStartKey"] = exclusive_start_key
        response = client.scan(**request)
        for index, item in enumerate(response.get("Items", [])):
            documents.append(
                (
                    _deserialize_item(item),
                    f"dynamodb://{table_name}/page-{page_number}/item-{index}",
                )
            )
        exclusive_start_key = response.get("LastEvaluatedKey")
        if not exclusive_start_key:
            break
    return [parse_occurrence_document(item, source=source) for item, source in documents]


def load_local_occurrences(source: Path) -> list[StoredOccurrence]:
    """Load one decoded record, an array, or DynamoDB ``Items`` scan output."""

    paths = [source] if source.is_file() else sorted(source.rglob("*.json"))
    if not paths:
        raise ValueError(f"No JSON occurrence files found under {source}")
    documents: list[tuple[dict[str, Any], str]] = []
    for path in paths:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            if "Items" in payload:
                values = payload["Items"]
                if not isinstance(values, list):
                    raise ValueError(
                        f"Occurrence source {path} Items must be an array"
                    )
            else:
                values = [payload]
        else:
            values = payload
        if not isinstance(values, list):
            raise ValueError(
                f"Occurrence source {path} must contain an object, array, or Items"
            )
        for index, value in enumerate(values):
            if not isinstance(value, dict):
                raise ValueError(f"Occurrence source {path} item {index} must be an object")
            document = _deserialize_item(value) if _looks_like_dynamodb_item(value) else value
            documents.append((document, f"{path.resolve()}#{index}"))
    return [parse_occurrence_document(item, source=name) for item, name in documents]


def build_daily_export(
    occurrences: list[StoredOccurrence],
    *,
    day: date,
    date_basis: DateBasis,
    policy: FilterPolicy,
    timezone: str = DEFAULT_TIMEZONE,
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
]:
    """Build all, latest-state, all-valid, and latest-valid alert views."""
    if date_basis not in {"publication", "observation"}:
        raise ValueError(f"Unsupported date basis: {date_basis}")
    business_timezone = _load_timezone(timezone)
    missing_observation = 0
    selected: list[StoredOccurrence] = []
    for occurrence in occurrences:
        if date_basis == "publication":
            occurrence_day = occurrence.publication_timestamp.astimezone(
                business_timezone
            ).date()
        elif occurrence.observation_date is None:
            missing_observation += 1
            continue
        else:
            occurrence_day = occurrence.observation_date
        if occurrence_day == day:
            selected.append(occurrence)

    selected.sort(key=lambda occurrence: occurrence.tie_break)
    primary: list[StoredOccurrence] = []
    excluded_unsupported = 0
    excluded_transition = 0
    for occurrence in selected:
        supported_contract = (
            occurrence.event_source == KIPU_EVENT_SOURCE
            and (
                (
                    occurrence.schema_version == "1.0"
                    and occurrence.detail_type == KIPU_DETAIL_TYPE
                )
                or (
                    occurrence.schema_version == "2.0"
                    and occurrence.detail_type == KIPU_DETAIL_TYPE_V2
                )
            )
        )
        if not supported_contract:
            excluded_unsupported += 1
            continue
        if (
            occurrence.schema_version == "1.0"
            and occurrence.raw_alert.get("superseded_by_schema_version") == "2.0"
        ):
            excluded_transition += 1
            continue
        primary.append(occurrence)

    all_alerts = [occurrence.raw_alert for occurrence in primary]
    unique_occurrences = _latest_by_alert_id(primary)
    unique_alerts = [occurrence.raw_alert for occurrence in unique_occurrences]
    valid_occurrences = [
        occurrence
        for occurrence in primary
        if evaluate_alert(occurrence.raw_alert, policy).accepted
    ]
    valid_alerts = [occurrence.raw_alert for occurrence in valid_occurrences]
    valid_unique_occurrences = _latest_by_alert_id(valid_occurrences)
    valid_unique_alerts = [
        occurrence.raw_alert for occurrence in valid_unique_occurrences
    ]
    summary = {
        "export_schema_version": "1.1",
        "day": day.isoformat(),
        "date_basis": date_basis,
        "timezone": timezone,
        "source_occurrence_count": len(occurrences),
        "selected_occurrence_count": len(selected),
        "missing_observation_date_count": missing_observation,
        "excluded_transition_count": excluded_transition,
        "excluded_unsupported_count": excluded_unsupported,
        "all_occurrence_count": len(all_alerts),
        "unique_alert_count": len(unique_alerts),
        "duplicate_occurrence_count": len(all_alerts) - len(unique_alerts),
        "valid_occurrence_count": len(valid_alerts),
        "valid_unique_alert_count": len(valid_unique_alerts),
        "reviewer_policy_versions": {
            "v1": policy.version,
            "v2": policy.v2_version,
            "producer_v2": policy.v2_producer_policy_version,
        },
    }
    return all_alerts, unique_alerts, valid_alerts, valid_unique_alerts, summary


def write_daily_export(
    output_dir: Path,
    *,
    all_alerts: list[dict[str, Any]],
    unique_alerts: list[dict[str, Any]],
    valid_alerts: list[dict[str, Any]],
    valid_unique_alerts: list[dict[str, Any]],
    summary: dict[str, Any],
) -> dict[str, Path]:
    """Write the four alert views and their summary as separate JSON files."""

    output_dir.mkdir(parents=True, exist_ok=True)
    documents = {
        "all": all_alerts,
        "unique": unique_alerts,
        "valid": valid_alerts,
        "valid_unique": valid_unique_alerts,
        "summary": summary,
    }
    paths: dict[str, Path] = {}
    for name, document in documents.items():
        path = output_dir / f"{name}.json"
        path.write_text(
            json.dumps(document, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        paths[name] = path
    return paths


def _latest_by_alert_id(
    occurrences: list[StoredOccurrence],
) -> list[StoredOccurrence]:
    latest: dict[str, StoredOccurrence] = {}
    for occurrence in occurrences:
        current = latest.get(occurrence.alert_id)
        if current is None or occurrence.tie_break > current.tie_break:
            latest[occurrence.alert_id] = occurrence
    return sorted(
        latest.values(),
        key=lambda occurrence: (
            occurrence.publication_timestamp,
            occurrence.alert_id,
            occurrence.occurrence_key,
        ),
    )


def _deserialize_item(item: dict[str, Any]) -> dict[str, Any]:
    deserializer = TypeDeserializer()
    return {
        key: _json_number(deserializer.deserialize(value))
        for key, value in item.items()
    }


def _json_number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    if isinstance(value, list):
        return [_json_number(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_number(item) for key, item in value.items()}
    return value


def _looks_like_dynamodb_item(value: dict[str, Any]) -> bool:
    return bool(value) and all(
        isinstance(attribute, dict)
        and len(attribute) == 1
        and next(iter(attribute)) in {"S", "N", "B", "BOOL", "NULL", "M", "L", "SS", "NS", "BS"}
        for attribute in value.values()
    )


def _nonempty_text(value: Any, field: str, source: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Occurrence {source} {field} must be non-empty text")
    return value.strip()


def _aware_datetime(value: Any, field: str, source: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Occurrence {source} {field} must be an ISO datetime")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Occurrence {source} {field} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"Occurrence {source} {field} must include a UTC offset")
    return parsed.astimezone(UTC)


def _contract_datetime(value: Any, field: str, source: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Occurrence {source} {field} must be an ISO datetime")
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Occurrence {source} {field} must be ISO 8601") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _optional_aware_datetime(value: Any, field: str, source: str) -> datetime | None:
    return None if value is None else _aware_datetime(value, field, source)


def _optional_iso_date(value: Any, field: str, source: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"Occurrence {source} {field} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Occurrence {source} {field} must be an ISO date") from exc


def _load_timezone(timezone: str) -> ZoneInfo:
    if not isinstance(timezone, str) or not timezone.strip():
        raise ValueError("Timezone must be a non-empty IANA name")
    try:
        return ZoneInfo(timezone.strip())
    except ZoneInfoNotFoundError as exc:
        raise ValueError(f"Unknown IANA timezone: {timezone}") from exc
