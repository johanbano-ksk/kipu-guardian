from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from botocore.exceptions import ClientError

from alert_reviewer.occurrence import (
    MAX_OCCURRENCE_ITEM_BYTES,
    DynamoDBOccurrenceStore,
    OccurrenceItemTooLargeError,
)
from alert_reviewer.queue import parse_queue_body


def _event(detail, *, event_id="event-001", event_time="2026-08-18T12:30:00Z"):
    return {
        "version": "0",
        "id": event_id,
        "time": event_time,
        "source": "acceptance.kipu",
        "detail-type": "Anomaly Detected v1",
        "detail": detail,
    }


def test_queue_occurrence_prefers_eventbridge_time_and_preserves_raw_v1(alert):
    raw_alert = alert.model_dump(mode="json")
    raw_alert["producer_extra"] = {"kept": True}
    event = _event(raw_alert)

    queued = parse_queue_body(json.dumps(event))
    occurrence = queued.occurrence

    assert occurrence.occurrence_key == "occurrence#event:event-001"
    assert occurrence.decision_key == "decision#event:event-001"
    assert occurrence.event_id == "event-001"
    assert occurrence.event_time == datetime(2026, 8, 18, 12, 30, tzinfo=UTC)
    assert occurrence.alert_timestamp == alert.timestamp
    assert occurrence.publication_timestamp == occurrence.event_time
    assert occurrence.observation_date is None
    assert occurrence.raw_alert is queued.raw_alert
    assert occurrence.raw_alert == raw_alert


def test_queue_occurrence_falls_back_to_contract_timestamp_and_hash(alert):
    raw_alert = alert.model_dump(mode="json")
    event = _event(raw_alert, event_id="", event_time=None)
    body = json.dumps(event)

    first = parse_queue_body(body).occurrence
    second = parse_queue_body(body).occurrence

    assert first.event_id is None
    assert first.event_time is None
    assert first.publication_timestamp == alert.timestamp
    assert first.observation_date is None
    assert first.occurrence_key.startswith("occurrence#sha256:")
    assert first.decision_key == first.occurrence_key.replace(
        "occurrence#",
        "decision#",
        1,
    )
    assert second.occurrence_key == first.occurrence_key
    assert second.decision_key == first.decision_key


class _CaptureDynamoDB:
    def __init__(self, *, error_code=None):
        self.error_code = error_code
        self.items = []
        self.keys = set()
        self.stored = {}

    def put_item(self, **kwargs):
        self.items.append(kwargs)
        key = kwargs["Item"]["idempotency_key"]["S"]
        if self.error_code is not None or key in self.keys:
            raise ClientError(
                {
                    "Error": {
                        "Code": self.error_code or "ConditionalCheckFailedException",
                        "Message": "put failed",
                    }
                },
                "PutItem",
            )
        self.keys.add(key)
        self.stored[key] = kwargs["Item"]
        return {}

    def get_item(self, **kwargs):
        key = kwargs["Key"]["idempotency_key"]["S"]
        item = self.stored.get(key)
        return {"Item": item} if item is not None else {}


async def test_dynamodb_occurrence_capture_is_append_only_and_retry_idempotent(alert):
    raw_alert = alert.model_dump(mode="json")
    occurrence = parse_queue_body(json.dumps(_event(raw_alert))).occurrence
    client = _CaptureDynamoDB()
    store = DynamoDBOccurrenceStore(client, "reviewer-table", ttl_hours=720)

    assert await store.capture(occurrence) is True
    assert await store.capture(occurrence) is False

    first_call = client.items[0]
    assert first_call["ConditionExpression"] == "attribute_not_exists(idempotency_key)"
    item = first_call["Item"]
    assert item["record_type"] == {"S": "ALERT_OCCURRENCE"}
    assert json.loads(item["raw_payload"]["S"]) == raw_alert
    assert item["event_time"] == {"S": "2026-08-18T12:30:00+00:00"}
    assert item["publication_timestamp"] == item["event_time"]
    assert item["alert_timestamp"] == {"S": alert.timestamp.isoformat()}
    recorded_at = datetime.fromisoformat(item["recorded_at"]["S"])
    expires_at = int(item["expires_at"]["N"])
    assert expires_at > int(recorded_at.timestamp())


async def test_dynamodb_occurrence_capture_fails_closed_on_nonconditional_error(alert):
    occurrence = parse_queue_body(
        json.dumps(_event(alert.model_dump(mode="json")))
    ).occurrence
    store = DynamoDBOccurrenceStore(
        _CaptureDynamoDB(error_code="AccessDeniedException"),
        "reviewer-table",
        ttl_hours=720,
    )

    with pytest.raises(ClientError):
        await store.capture(occurrence)


async def test_dynamodb_occurrence_capture_rejects_reused_event_id(alert):
    first_raw = alert.model_dump(mode="json")
    second_raw = {**first_raw, "alert_id": "different-alert"}
    first = parse_queue_body(json.dumps(_event(first_raw))).occurrence
    second = parse_queue_body(json.dumps(_event(second_raw))).occurrence
    store = DynamoDBOccurrenceStore(_CaptureDynamoDB(), "reviewer-table", ttl_hours=720)

    assert await store.capture(first) is True
    with pytest.raises(RuntimeError, match="collision"):
        await store.capture(second)


async def test_dynamodb_occurrence_capture_rejects_oversized_item_before_put(alert):
    raw_alert = alert.model_dump(mode="json")
    raw_alert["untrusted_extra"] = "x" * MAX_OCCURRENCE_ITEM_BYTES
    occurrence = parse_queue_body(json.dumps(_event(raw_alert))).occurrence
    client = _CaptureDynamoDB()
    store = DynamoDBOccurrenceStore(client, "reviewer-table", ttl_hours=720)

    with pytest.raises(OccurrenceItemTooLargeError, match="application limit"):
        await store.capture(occurrence)

    assert client.items == []
