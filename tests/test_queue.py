from __future__ import annotations

import asyncio
import json

import pytest
from botocore.exceptions import ClientError

from alert_reviewer import queue as queue_module
from alert_reviewer.alert_filter import AlertFilterOutcome, PayloadAlertFilter
from alert_reviewer.config import Settings
from alert_reviewer.queue import (
    ClaimStatus,
    DynamoDBIdempotencyStore,
    IdempotencyClaim,
    InvalidQueueMessage,
    SQSReviewConsumer,
    parse_queue_body,
)


class CountingFilter:
    def __init__(self):
        self.calls = 0
        self.delegate = PayloadAlertFilter()

    def evaluate(self, alert):
        self.calls += 1
        return self.delegate.evaluate(alert)


class FakePublisher:
    def __init__(self, *, failures=0):
        self.failures = failures
        self.published = []

    async def publish(self, alert):
        if self.failures:
            self.failures -= 1
            raise RuntimeError("EventBridge unavailable")
        self.published.append(alert)


class FakeIdempotencyStore:
    def __init__(self):
        self.status = None
        self.outcome = None
        self.extensions = 0

    async def claim(self, _key):
        if self.status is None:
            self.status = ClaimStatus.ACQUIRED
            return IdempotencyClaim(ClaimStatus.ACQUIRED)
        if self.status == ClaimStatus.READY:
            return IdempotencyClaim(self.status, self.outcome)
        return IdempotencyClaim(self.status)

    async def store_result(self, _key, outcome):
        self.status = ClaimStatus.READY
        self.outcome = outcome

    async def mark_completed(self, _key):
        self.status = ClaimStatus.COMPLETED

    async def extend(self, _key):
        self.extensions += 1

    async def release(self, _key):
        if self.status == ClaimStatus.ACQUIRED:
            self.status = None


class KeyedFakeIdempotencyStore:
    def __init__(self):
        self.records = {}
        self.claimed_keys = []

    async def claim(self, key):
        self.claimed_keys.append(key)
        record = self.records.get(key)
        if record is None:
            self.records[key] = {"status": ClaimStatus.ACQUIRED, "outcome": None}
            return IdempotencyClaim(ClaimStatus.ACQUIRED)
        if record["status"] == ClaimStatus.READY:
            return IdempotencyClaim(ClaimStatus.READY, record["outcome"])
        return IdempotencyClaim(record["status"])

    async def store_result(self, key, outcome):
        self.records[key] = {"status": ClaimStatus.READY, "outcome": outcome}

    async def mark_completed(self, key):
        self.records[key]["status"] = ClaimStatus.COMPLETED

    async def extend(self, _key):
        return None

    async def release(self, key):
        if self.records.get(key, {}).get("status") == ClaimStatus.ACQUIRED:
            self.records.pop(key)


class FakeOccurrenceStore:
    def __init__(self, *, failure=False, order=None):
        self.failure = failure
        self.order = order
        self.calls = []
        self.inserted_keys = set()

    async def capture(self, occurrence):
        if self.order is not None:
            self.order.append("capture")
        self.calls.append(occurrence)
        if self.failure:
            raise RuntimeError("DynamoDB occurrence capture unavailable")
        is_new = occurrence.occurrence_key not in self.inserted_keys
        self.inserted_keys.add(occurrence.occurrence_key)
        return is_new


class FakeSQS:
    def __init__(self, messages):
        self.messages = list(messages)
        self.receive_kwargs = []
        self.deleted = []
        self.visibility_changes = []

    def receive_message(self, **kwargs):
        self.receive_kwargs.append(kwargs)
        if not self.messages:
            return {}
        return {"Messages": [self.messages.pop(0)]}

    def delete_message(self, **kwargs):
        self.deleted.append(kwargs)
        return {}

    def change_message_visibility(self, **kwargs):
        self.visibility_changes.append(kwargs)
        return {}


class RecordingLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, event, **fields):
        self.warnings.append((event, fields))


def _message(body, *, message_id="message-001", receipt="receipt-001"):
    return {
        "MessageId": message_id,
        "ReceiptHandle": receipt,
        "Body": json.dumps(body),
        "Attributes": {"ApproximateReceiveCount": "1"},
    }


def _settings(**overrides):
    values = {
        "aws_region": "us-east-1",
        "sqs_input_queue_url": "https://sqs.us-east-1.amazonaws.com/123/input",
        "sqs_idempotency_table": "alert-review-idempotency",
        "eventbridge_output_bus_name": "acceptance-intelligence-bus-dev",
        "sqs_wait_time_seconds": 0,
    }
    values.update(overrides)
    return Settings(**values)


def _kipu_event(alert, *, event_id="event-001"):
    return {
        "version": "0",
        "id": event_id,
        "source": "acceptance.kipu",
        "detail-type": "Anomaly Detected v1",
        "detail": alert,
    }


def _kipu_event_v2(alert, *, event_id="event-v2-001"):
    return {
        "version": "0",
        "id": event_id,
        "source": "acceptance.kipu",
        "detail-type": "Anomaly Detected v2",
        "detail": alert,
    }


def test_parses_real_kipu_envelope_and_preserves_routing_fields(alert):
    raw_alert = alert.model_dump(mode="json")
    raw_alert.update({"batch_id": "batch-001", "group_name": "world_cup"})

    queued = parse_queue_body(json.dumps(_kipu_event(raw_alert)))

    assert queued.idempotency_key == "decision#event:event-001"
    assert queued.raw_alert["batch_id"] == "batch-001"
    assert queued.raw_alert["group_name"] == "world_cup"


def test_v1_transition_copy_uses_its_occurrence_decision_key(alert):
    raw_alert = alert.model_dump(mode="json")
    raw_alert["superseded_by_schema_version"] = "2.0"

    queued = parse_queue_body(json.dumps(_kipu_event(raw_alert)))

    assert queued.raw_alert["superseded_by_schema_version"] == "2.0"
    assert queued.idempotency_key == "decision#event:event-001"


def test_rejects_unknown_v1_supersession_marker_at_contract_boundary(alert):
    raw_alert = alert.model_dump(mode="json")
    raw_alert["superseded_by_schema_version"] = "3.0"

    with pytest.raises(InvalidQueueMessage, match="Invalid Kipu v1 alert"):
        parse_queue_body(json.dumps(_kipu_event(raw_alert)))


def test_rejects_unnegotiated_v2_envelope(v2_alert):
    raw_alert = v2_alert.model_dump(mode="json")

    with pytest.raises(InvalidQueueMessage) as exc_info:
        parse_queue_body(json.dumps(_kipu_event_v2(raw_alert)))

    assert exc_info.value.code == "UNEXPECTED_DETAIL_TYPE"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("unknown_status_count", "2"),
        ("is_anomaly_global", 0),
        ("isolation_forest_global_score", "0.2"),
        ("criteria_count", 4.0),
    ],
)
def test_unnegotiated_v2_is_rejected_before_evidence_validation(v2_alert, field, value):
    raw_alert = v2_alert.model_dump(mode="json")
    raw_alert[field] = value

    with pytest.raises(InvalidQueueMessage, match="Unexpected EventBridge detail type"):
        parse_queue_body(json.dumps(_kipu_event_v2(raw_alert)))


def test_rejects_schema_and_detail_type_mismatch(v2_alert):
    raw_alert = v2_alert.model_dump(mode="json")

    with pytest.raises(InvalidQueueMessage) as exc_info:
        parse_queue_body(json.dumps(_kipu_event(raw_alert)))

    assert exc_info.value.code == "SCHEMA_DETAIL_TYPE_MISMATCH"


@pytest.mark.parametrize("criticality", ["Crítica", "critica", "CRITICA", " CRÍTICA "])
def test_normalizes_criticality_like_the_payload_filter(alert, criticality):
    raw_alert = alert.model_dump(mode="json")
    raw_alert["criticality"] = criticality

    queued = parse_queue_body(json.dumps(_kipu_event(raw_alert)))

    assert queued.raw_alert["criticality"] == criticality


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("approval_rate", "0.2"),
        ("total_transactions", "100"),
        ("declined_count", "80"),
        ("rolling_avg_approval_rate", "0.25"),
        ("predicted_dc_q90", "75"),
    ],
)
def test_rejects_coerced_numeric_fields_as_invalid_contract(alert, field, value):
    raw_alert = alert.model_dump(mode="json")
    raw_alert[field] = value

    with pytest.raises(InvalidQueueMessage, match="Invalid Kipu v1 alert"):
        parse_queue_body(json.dumps(_kipu_event(raw_alert)))


@pytest.mark.parametrize(
    "timestamp",
    [1723550400, True, "2026-08-13", "not-a-timestamp"],
)
def test_rejects_non_iso_datetime_timestamp_as_invalid_contract(alert, timestamp):
    raw_alert = alert.model_dump(mode="json")
    raw_alert["timestamp"] = timestamp

    with pytest.raises(InvalidQueueMessage, match="Invalid Kipu v1 alert"):
        parse_queue_body(json.dumps(_kipu_event(raw_alert)))


def test_rejects_kipu_event_without_merchant_name(alert):
    raw_alert = alert.model_dump(mode="json")
    raw_alert.pop("merchant_name")

    with pytest.raises(InvalidQueueMessage, match="Invalid Kipu v1 alert"):
        parse_queue_body(json.dumps(_kipu_event(raw_alert)))


@pytest.mark.parametrize("merchant_name", [None, "", "   "])
def test_rejects_kipu_event_with_invalid_merchant_name(alert, merchant_name):
    raw_alert = alert.model_dump(mode="json")
    raw_alert["merchant_name"] = merchant_name

    with pytest.raises(InvalidQueueMessage, match="Invalid Kipu v1 alert"):
        parse_queue_body(json.dumps(_kipu_event(raw_alert)))


@pytest.mark.parametrize(
    ("event", "expected_code"),
    [
        (
            {
                "source": "other.producer",
                "detail-type": "Anomaly Detected v1",
                "detail": {},
            },
            "UNEXPECTED_SOURCE",
        ),
        (
            {
                "source": "acceptance.kipu",
                "detail-type": "Anomaly Detected",
                "detail": {},
            },
            "UNEXPECTED_DETAIL_TYPE",
        ),
        (
            {
                "schema_version": "2.0",
                "alert_id": "alert-001",
            },
            "UNEXPECTED_SOURCE",
        ),
    ],
)
def test_rejects_unexpected_event_contract(event, expected_code):
    with pytest.raises(InvalidQueueMessage) as exc_info:
        parse_queue_body(json.dumps(event))

    assert exc_info.value.code == expected_code


async def test_accepted_alert_is_published_then_acknowledged(alert):
    raw_alert = alert.model_dump(mode="json")
    raw_alert.update({"batch_id": "batch-001", "group_name": "world_cup"})
    sqs = FakeSQS([_message(_kipu_event(raw_alert))])
    alert_filter = CountingFilter()
    publisher = FakePublisher()
    store = FakeIdempotencyStore()
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=alert_filter,
        publisher=publisher,
        idempotency_store=store,
        settings=_settings(),
    )

    count = await consumer.run_once()

    assert count == 1
    assert alert_filter.calls == 1
    assert store.status == ClaimStatus.COMPLETED
    assert publisher.published == [raw_alert]
    assert sqs.deleted[0]["ReceiptHandle"] == "receipt-001"


async def test_same_event_retry_captures_one_occurrence_before_decision_dedup(alert):
    body = _kipu_event(alert.model_dump(mode="json"))
    sqs = FakeSQS(
        [
            _message(body, message_id="first", receipt="first-receipt"),
            _message(body, message_id="retry", receipt="retry-receipt"),
        ]
    )
    occurrence_store = FakeOccurrenceStore()
    alert_filter = CountingFilter()
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=alert_filter,
        publisher=FakePublisher(),
        idempotency_store=FakeIdempotencyStore(),
        occurrence_store=occurrence_store,
        settings=_settings(),
    )

    await consumer.run_once()
    await consumer.run_once()

    assert len(occurrence_store.calls) == 2
    assert len(occurrence_store.inserted_keys) == 1
    assert alert_filter.calls == 1
    assert len(sqs.deleted) == 2


async def test_occurrence_capture_failure_blocks_decision_and_ack(alert):
    sqs = FakeSQS([_message(_kipu_event(alert.model_dump(mode="json")))])
    alert_filter = CountingFilter()
    idempotency_store = FakeIdempotencyStore()
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=alert_filter,
        publisher=FakePublisher(),
        idempotency_store=idempotency_store,
        occurrence_store=FakeOccurrenceStore(failure=True),
        settings=_settings(),
    )

    await consumer.run_once()

    assert idempotency_store.status is None
    assert alert_filter.calls == 0
    assert sqs.deleted == []


async def test_unnegotiated_v2_is_not_evaluated_published_or_acknowledged(v2_alert):
    raw_alert = v2_alert.model_dump(mode="json")
    sqs = FakeSQS([_message(_kipu_event_v2(raw_alert))])
    alert_filter = CountingFilter()
    publisher = FakePublisher()
    store = FakeIdempotencyStore()
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=alert_filter,
        publisher=publisher,
        idempotency_store=store,
        settings=_settings(),
    )

    await consumer.run_once()

    assert alert_filter.calls == 0
    assert store.status is None
    assert publisher.published == []
    assert sqs.deleted == []


async def test_historical_transition_v1_is_handled_but_v2_remains_unacknowledged(
    alert,
    v2_alert,
):
    shared_alert_id = "shared-v1-v2-alert"
    raw_v1 = alert.model_dump(mode="json")
    raw_v1.update(
        {
            "alert_id": shared_alert_id,
            "superseded_by_schema_version": "2.0",
        }
    )
    raw_v2 = v2_alert.model_dump(mode="json")
    raw_v2["alert_id"] = shared_alert_id
    sqs = FakeSQS(
        [
            _message(_kipu_event(raw_v1), message_id="v1", receipt="v1-receipt"),
            _message(_kipu_event_v2(raw_v2), message_id="v2", receipt="v2-receipt"),
        ]
    )
    publisher = FakePublisher()
    store = KeyedFakeIdempotencyStore()
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=CountingFilter(),
        publisher=publisher,
        idempotency_store=store,
        settings=_settings(),
    )

    await consumer.run_once()
    await consumer.run_once()

    superseded_key = "decision#event:event-001"
    assert store.claimed_keys == [superseded_key]
    assert store.records[superseded_key]["outcome"].reason_codes == [
        "SUPERSEDED_BY_V2"
    ]
    assert store.records[superseded_key]["status"] == ClaimStatus.COMPLETED
    assert publisher.published == []
    assert {item["ReceiptHandle"] for item in sqs.deleted} == {"v1-receipt"}


async def test_unnegotiated_v2_events_create_no_decisions(v2_alert):
    shared_alert_id = "same-alert-upgraded-to-critical"
    alta = v2_alert.model_dump(mode="json")
    alta.update(
        {
            "alert_id": shared_alert_id,
            "criticality": "Alta",
            "approval_rate": 0.60,
            "approved_count": 60,
            "declined_count": 38,
            "unknown_status_count": 2,
            "signal_codes": ["LOW_APPROVAL_RATE", "APPROVAL_RATE_MAD_DROP"],
            "criteria_count": 2,
            "zscore_ta": -4.0,
            "zscore_rechazos": 0.0,
            "rolling_avg_rejections": 20.0,
            "rolling_q95_rejections": 50.0,
            "rejection_change_pct": 90.0,
            "priority_score": 55.0,
            "priority_components": {
                "volume_score": 5.0,
                "approval_rate_score": 30.0,
                "anomaly_score": 20.0,
            },
        }
    )
    critical = v2_alert.model_dump(mode="json")
    critical["alert_id"] = shared_alert_id
    sqs = FakeSQS(
        [
            _message(
                _kipu_event_v2(alta, event_id="event-v2-alta"),
                message_id="alta",
                receipt="alta-receipt",
            ),
            _message(
                _kipu_event_v2(critical, event_id="event-v2-critical"),
                message_id="critica",
                receipt="critica-receipt",
            ),
        ]
    )
    publisher = FakePublisher()
    store = KeyedFakeIdempotencyStore()
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=CountingFilter(),
        publisher=publisher,
        idempotency_store=store,
        settings=_settings(),
    )

    await consumer.run_once()
    await consumer.run_once()

    assert store.claimed_keys == []
    assert publisher.published == []
    assert sqs.deleted == []


async def test_same_alert_and_criticality_is_reevaluated_for_new_event_occurrence(alert):
    rejected = alert.model_copy(
        update={
            "approval_rate": 0.79,
            "declined_count": 21,
            "rolling_avg_approval_rate": 0.80,
        }
    ).model_dump(mode="json")
    accepted = alert.model_dump(mode="json")
    sqs = FakeSQS(
        [
            _message(
                _kipu_event(rejected, event_id="event-first"),
                message_id="first",
                receipt="first-receipt",
            ),
            _message(
                _kipu_event(accepted, event_id="event-second"),
                message_id="second",
                receipt="second-receipt",
            ),
        ]
    )
    alert_filter = CountingFilter()
    publisher = FakePublisher()
    store = KeyedFakeIdempotencyStore()
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=alert_filter,
        publisher=publisher,
        idempotency_store=store,
        settings=_settings(),
    )

    await consumer.run_once()
    await consumer.run_once()

    assert store.claimed_keys == [
        "decision#event:event-first",
        "decision#event:event-second",
    ]
    assert alert_filter.calls == 2
    assert publisher.published == [accepted]


async def test_business_rejection_is_acknowledged_without_publish(alert):
    raw_alert = alert.model_copy(
        update={
            "approval_rate": 0.79,
            "declined_count": 21,
            "rolling_avg_approval_rate": 0.80,
        }
    ).model_dump(mode="json")
    sqs = FakeSQS([_message(_kipu_event(raw_alert))])
    publisher = FakePublisher()
    store = FakeIdempotencyStore()
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=CountingFilter(),
        publisher=publisher,
        idempotency_store=store,
        settings=_settings(),
    )

    await consumer.run_once()

    assert store.outcome.accepted is False
    assert store.status == ClaimStatus.COMPLETED
    assert publisher.published == []
    assert len(sqs.deleted) == 1


async def test_publish_retry_reuses_stored_outcome(alert):
    body = _kipu_event(alert.model_dump(mode="json"))
    sqs = FakeSQS(
        [
            _message(body, message_id="message-001", receipt="receipt-001"),
            _message(body, message_id="message-002", receipt="receipt-002"),
        ]
    )
    alert_filter = CountingFilter()
    publisher = FakePublisher(failures=1)
    store = FakeIdempotencyStore()
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=alert_filter,
        publisher=publisher,
        idempotency_store=store,
        settings=_settings(),
    )

    await consumer.run_once()
    assert store.status == ClaimStatus.READY
    assert sqs.deleted == []

    await consumer.run_once()

    assert alert_filter.calls == 1
    assert store.status == ClaimStatus.COMPLETED
    assert len(publisher.published) == 1
    assert sqs.deleted[0]["ReceiptHandle"] == "receipt-002"


async def test_completed_duplicate_is_deleted_without_reprocessing(alert):
    sqs = FakeSQS([_message(_kipu_event(alert.model_dump(mode="json")))])
    alert_filter = CountingFilter()
    publisher = FakePublisher()
    store = FakeIdempotencyStore()
    store.status = ClaimStatus.COMPLETED
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=alert_filter,
        publisher=publisher,
        idempotency_store=store,
        settings=_settings(),
    )

    await consumer.run_once()

    assert alert_filter.calls == 0
    assert publisher.published == []
    assert len(sqs.deleted) == 1


async def test_invalid_message_is_left_for_dlq_redrive():
    sqs = FakeSQS([_message({"unexpected": True})])
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=CountingFilter(),
        publisher=FakePublisher(),
        idempotency_store=FakeIdempotencyStore(),
        settings=_settings(),
    )

    await consumer.run_once()

    assert sqs.deleted == []


async def test_invalid_message_logs_stable_validation_code(monkeypatch):
    recorder = RecordingLogger()
    monkeypatch.setattr(queue_module, "logger", recorder)
    sqs = FakeSQS([_message({"unexpected": True})])
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=CountingFilter(),
        publisher=FakePublisher(),
        idempotency_store=FakeIdempotencyStore(),
        settings=_settings(),
    )

    await consumer.run_once()

    assert recorder.warnings == [
        (
            "invalid_review_message",
            {
                "message_id": "message-001",
                "receive_count": 1,
                "validation_error": "UNEXPECTED_SOURCE",
            },
        )
    ]


async def test_coerced_numeric_message_is_left_for_dlq_redrive(alert):
    raw_alert = alert.model_dump(mode="json")
    raw_alert["total_transactions"] = "100"
    sqs = FakeSQS([_message(_kipu_event(raw_alert))])
    alert_filter = CountingFilter()
    store = FakeIdempotencyStore()
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=alert_filter,
        publisher=FakePublisher(),
        idempotency_store=store,
        settings=_settings(),
    )

    await consumer.run_once()

    assert alert_filter.calls == 0
    assert store.status is None
    assert sqs.deleted == []


async def test_visibility_and_lock_are_extended_during_long_processing():
    sqs = FakeSQS([])
    store = FakeIdempotencyStore()
    consumer = SQSReviewConsumer(
        sqs_client=sqs,
        alert_filter=CountingFilter(),
        publisher=FakePublisher(),
        idempotency_store=store,
        settings=_settings(sqs_visibility_timeout_seconds=1),
    )

    heartbeat = asyncio.create_task(consumer._heartbeat("receipt", "alert-001"))
    await asyncio.sleep(1.05)
    heartbeat.cancel()
    with pytest.raises(asyncio.CancelledError):
        await heartbeat

    assert sqs.visibility_changes[0]["VisibilityTimeout"] == 1
    assert store.extensions == 1


class FakeDynamoDB:
    def __init__(self, *, existing=None):
        self.existing = existing
        self.calls = []

    def put_item(self, **kwargs):
        self.calls.append(("put_item", kwargs))
        if self.existing is not None:
            raise ClientError(
                {
                    "Error": {
                        "Code": "ConditionalCheckFailedException",
                        "Message": "exists",
                    }
                },
                "PutItem",
            )
        return {}

    def get_item(self, **kwargs):
        self.calls.append(("get_item", kwargs))
        return {"Item": self.existing}

    def update_item(self, **kwargs):
        self.calls.append(("update_item", kwargs))
        return {}

    def delete_item(self, **kwargs):
        self.calls.append(("delete_item", kwargs))
        return {}


def _outcome(alert_id="alert-001"):
    return AlertFilterOutcome(
        alert_id=alert_id,
        accepted=True,
        reason_codes=["APPROVAL_DROP_FROM_ROLLING_AVERAGE"],
        alert={"alert_id": alert_id},
    )


async def test_dynamodb_store_persists_outcome_before_completion():
    client = FakeDynamoDB()
    store = DynamoDBIdempotencyStore(
        client,
        "idempotency",
        ttl_hours=24,
        lock_seconds=120,
    )

    claim = await store.claim("alert-001")
    await store.extend("alert-001")
    await store.store_result("alert-001", _outcome())
    await store.mark_completed("alert-001")

    assert claim.status == ClaimStatus.ACQUIRED
    operations = [operation for operation, _kwargs in client.calls]
    assert operations == ["put_item", "update_item", "update_item", "update_item"]
    result_update = client.calls[2][1]["ExpressionAttributeValues"]
    assert ":outcome" in result_update
    assert result_update[":accepted"] == {"BOOL": True}


async def test_dynamodb_store_recovers_outcome_ready_to_publish():
    outcome = _outcome()
    client = FakeDynamoDB(
        existing={
            "idempotency_key": {"S": "alert-001"},
            "status": {"S": ClaimStatus.READY.value},
            "outcome": {"S": outcome.model_dump_json()},
        }
    )
    store = DynamoDBIdempotencyStore(
        client,
        "idempotency",
        ttl_hours=24,
        lock_seconds=120,
    )

    claim = await store.claim("alert-001")

    assert claim.status == ClaimStatus.READY
    assert claim.outcome == outcome
