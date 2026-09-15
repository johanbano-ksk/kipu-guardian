"""Durable EventBridge/SQS consumer for asynchronous alert reviews."""

from __future__ import annotations

import asyncio
import json
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any, Protocol

import structlog
from botocore.exceptions import ClientError
from pydantic import ValidationError

from alert_reviewer.alert_filter import AlertFilterOutcome, PayloadAlertFilter
from alert_reviewer.config import Settings
from alert_reviewer.kipu_contract import KipuAlert
from alert_reviewer.occurrence import AlertOccurrence, build_occurrence

logger = structlog.get_logger(__name__)

KIPU_EVENT_SOURCE = "acceptance.kipu"
KIPU_DETAIL_TYPE = "Anomaly Detected v1"
# Historical identifier used by the offline occurrence exporter. It is
# intentionally absent from KIPU_DETAIL_TYPES and from the deployable rule.
KIPU_DETAIL_TYPE_V2 = "Anomaly Detected v2"
KIPU_DETAIL_TYPES = {
    KIPU_DETAIL_TYPE: ("1.0", KipuAlert),
}


class InvalidQueueMessage(ValueError):
    """Raised when an SQS body does not contain a supported review request."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ClaimStatus(StrEnum):
    ACQUIRED = "ACQUIRED"
    READY = "READY"
    COMPLETED = "COMPLETED"
    BUSY = "BUSY"


@dataclass(frozen=True)
class IdempotencyClaim:
    status: ClaimStatus
    outcome: AlertFilterOutcome | None = None


@dataclass(frozen=True)
class QueueReview:
    raw_alert: dict[str, Any]
    idempotency_key: str
    occurrence: AlertOccurrence


class IdempotencyStore(Protocol):
    async def claim(self, key: str) -> IdempotencyClaim: ...

    async def store_result(self, key: str, outcome: AlertFilterOutcome) -> None: ...

    async def mark_completed(self, key: str) -> None: ...

    async def extend(self, key: str) -> None: ...

    async def release(self, key: str) -> None: ...


class OccurrenceStore(Protocol):
    async def capture(self, occurrence: AlertOccurrence) -> bool: ...


class SQSClient(Protocol):
    def receive_message(self, **kwargs: Any) -> dict[str, Any]: ...

    def delete_message(self, **kwargs: Any) -> dict[str, Any]: ...

    def change_message_visibility(self, **kwargs: Any) -> dict[str, Any]: ...


class ValidatedAlertPublisher(Protocol):
    async def publish(self, alert: dict[str, Any]) -> None: ...


class DynamoDBIdempotencyStore:
    """Claim alerts and persist filter outcomes before side effects.

    Keeping the serialized outcome in READY closes the gap where an accepted
    alert could be acknowledged without reaching EventBridge. A retry reuses the
    same outcome instead of evaluating the alert again.
    """

    def __init__(
        self,
        client: Any,
        table_name: str,
        *,
        ttl_hours: int,
        lock_seconds: int,
    ) -> None:
        self.client = client
        self.table_name = table_name
        self.ttl_hours = ttl_hours
        self.lock_seconds = lock_seconds

    async def claim(self, key: str) -> IdempotencyClaim:
        now = datetime.now(UTC)
        now_epoch = int(now.timestamp())
        item = {
            "idempotency_key": {"S": key},
            "status": {"S": ClaimStatus.ACQUIRED.value},
            "lock_until": {"N": str(now_epoch + self.lock_seconds)},
            "expires_at": {
                "N": str(int((now + timedelta(hours=self.ttl_hours)).timestamp()))
            },
            "updated_at": {"S": now.isoformat()},
        }
        try:
            await asyncio.to_thread(
                self.client.put_item,
                TableName=self.table_name,
                Item=item,
                ConditionExpression=(
                    "attribute_not_exists(idempotency_key) OR "
                    "#expires_at < :now OR "
                    "(#status = :processing AND #lock_until < :now)"
                ),
                ExpressionAttributeNames={
                    "#status": "status",
                    "#lock_until": "lock_until",
                    "#expires_at": "expires_at",
                },
                ExpressionAttributeValues={
                    ":processing": {"S": ClaimStatus.ACQUIRED.value},
                    ":now": {"N": str(now_epoch)},
                },
            )
            return IdempotencyClaim(ClaimStatus.ACQUIRED)
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise

        response = await asyncio.to_thread(
            self.client.get_item,
            TableName=self.table_name,
            Key={"idempotency_key": {"S": key}},
            ConsistentRead=True,
        )
        existing = response.get("Item")
        if not existing:
            return IdempotencyClaim(ClaimStatus.BUSY)

        status = existing.get("status", {}).get("S")
        if status == ClaimStatus.COMPLETED.value:
            return IdempotencyClaim(ClaimStatus.COMPLETED)
        if status == ClaimStatus.READY.value:
            serialized = existing.get("outcome", {}).get("S")
            if serialized:
                return IdempotencyClaim(
                    ClaimStatus.READY,
                    AlertFilterOutcome.model_validate_json(serialized),
                )
        return IdempotencyClaim(ClaimStatus.BUSY)

    async def store_result(self, key: str, outcome: AlertFilterOutcome) -> None:
        await asyncio.to_thread(
            self.client.update_item,
            TableName=self.table_name,
            Key={"idempotency_key": {"S": key}},
            UpdateExpression=(
                "SET #status = :ready, #outcome = :outcome, #accepted = :accepted, "
                "#updated_at = :updated_at REMOVE #lock_until"
            ),
            ConditionExpression="#status = :processing",
            ExpressionAttributeNames={
                "#status": "status",
                "#outcome": "outcome",
                "#accepted": "accepted",
                "#updated_at": "updated_at",
                "#lock_until": "lock_until",
            },
            ExpressionAttributeValues={
                ":ready": {"S": ClaimStatus.READY.value},
                ":processing": {"S": ClaimStatus.ACQUIRED.value},
                ":outcome": {"S": outcome.model_dump_json()},
                ":accepted": {"BOOL": outcome.accepted},
                ":updated_at": {"S": datetime.now(UTC).isoformat()},
            },
        )

    async def mark_completed(self, key: str) -> None:
        await asyncio.to_thread(
            self.client.update_item,
            TableName=self.table_name,
            Key={"idempotency_key": {"S": key}},
            UpdateExpression="SET #status = :completed, #updated_at = :updated_at",
            ConditionExpression="#status = :ready",
            ExpressionAttributeNames={
                "#status": "status",
                "#updated_at": "updated_at",
            },
            ExpressionAttributeValues={
                ":completed": {"S": ClaimStatus.COMPLETED.value},
                ":ready": {"S": ClaimStatus.READY.value},
                ":updated_at": {"S": datetime.now(UTC).isoformat()},
            },
        )

    async def extend(self, key: str) -> None:
        now = datetime.now(UTC)
        await asyncio.to_thread(
            self.client.update_item,
            TableName=self.table_name,
            Key={"idempotency_key": {"S": key}},
            UpdateExpression="SET #lock_until = :lock_until, #updated_at = :updated_at",
            ConditionExpression="#status = :processing",
            ExpressionAttributeNames={
                "#status": "status",
                "#lock_until": "lock_until",
                "#updated_at": "updated_at",
            },
            ExpressionAttributeValues={
                ":processing": {"S": ClaimStatus.ACQUIRED.value},
                ":lock_until": {
                    "N": str(int(now.timestamp()) + self.lock_seconds),
                },
                ":updated_at": {"S": now.isoformat()},
            },
        )

    async def release(self, key: str) -> None:
        try:
            await asyncio.to_thread(
                self.client.delete_item,
                TableName=self.table_name,
                Key={"idempotency_key": {"S": key}},
                ConditionExpression="#status = :processing",
                ExpressionAttributeNames={"#status": "status"},
                ExpressionAttributeValues={
                    ":processing": {"S": ClaimStatus.ACQUIRED.value},
                },
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ConditionalCheckFailedException":
                raise


def parse_queue_body(body: str) -> QueueReview:
    """Validate the versioned Kipu EventBridge envelope received through SQS."""

    try:
        payload = json.loads(body)
    except (TypeError, json.JSONDecodeError) as exc:
        raise InvalidQueueMessage(
            "INVALID_JSON",
            "SQS message body must be a JSON object",
        ) from exc
    if not isinstance(payload, dict):
        raise InvalidQueueMessage(
            "INVALID_ENVELOPE",
            "SQS message body must be a JSON object",
        )

    if payload.get("source") != KIPU_EVENT_SOURCE:
        raise InvalidQueueMessage("UNEXPECTED_SOURCE", "Unexpected EventBridge source")
    detail_type = payload.get("detail-type")
    if detail_type not in KIPU_DETAIL_TYPES:
        raise InvalidQueueMessage(
            "UNEXPECTED_DETAIL_TYPE",
            "Unexpected EventBridge detail type",
        )
    raw_alert = payload.get("detail")
    if not isinstance(raw_alert, dict):
        raise InvalidQueueMessage(
            "INVALID_DETAIL",
            "EventBridge detail must be a JSON object",
        )

    expected_schema, contract = KIPU_DETAIL_TYPES[detail_type]
    if raw_alert.get("schema_version") != expected_schema:
        raise InvalidQueueMessage(
            "SCHEMA_DETAIL_TYPE_MISMATCH",
            "Kipu schema version does not match EventBridge detail type",
        )

    try:
        alert = contract.model_validate(raw_alert)
    except ValidationError as exc:
        raise InvalidQueueMessage(
            "INVALID_ALERT_CONTRACT",
            f"Invalid Kipu v{expected_schema.removesuffix('.0')} alert",
        ) from exc

    occurrence = build_occurrence(
        payload,
        raw_alert=raw_alert,
        alert_timestamp=alert.timestamp,
        observation_date=None,
        serialized_event=body,
    )
    return QueueReview(
        raw_alert=raw_alert,
        idempotency_key=occurrence.decision_key,
        occurrence=occurrence,
    )


class SQSReviewConsumer:
    def __init__(
        self,
        *,
        sqs_client: SQSClient,
        alert_filter: PayloadAlertFilter,
        publisher: ValidatedAlertPublisher,
        idempotency_store: IdempotencyStore,
        settings: Settings,
        occurrence_store: OccurrenceStore | None = None,
    ) -> None:
        if settings.sqs_input_queue_url is None:
            raise ValueError("SQS input queue is required")
        self.sqs_client = sqs_client
        self.alert_filter = alert_filter
        self.publisher = publisher
        self.idempotency_store = idempotency_store
        self.occurrence_store = occurrence_store
        self.settings = settings

    async def run_forever(self) -> None:
        failures = 0
        while True:
            try:
                await self.run_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failures += 1
                delay = min(30, 2 ** min(failures, 5))
                logger.error(
                    "sqs_poll_failed",
                    error_type=type(exc).__name__,
                    retry_delay_seconds=delay,
                )
                await asyncio.sleep(delay)

    async def run_once(self) -> int:
        response = await asyncio.to_thread(
            self.sqs_client.receive_message,
            QueueUrl=self.settings.sqs_input_queue_url,
            MaxNumberOfMessages=self.settings.sqs_max_messages,
            WaitTimeSeconds=self.settings.sqs_wait_time_seconds,
            VisibilityTimeout=self.settings.sqs_visibility_timeout_seconds,
            AttributeNames=["ApproximateReceiveCount"],
        )
        messages = response.get("Messages", [])
        if messages:
            await asyncio.gather(*(self._process(message) for message in messages))
        return len(messages)

    async def _process(self, message: dict[str, Any]) -> None:
        message_id = str(message.get("MessageId", "unknown"))
        receipt_handle = message.get("ReceiptHandle")
        body = message.get("Body")
        if not isinstance(receipt_handle, str) or not isinstance(body, str):
            logger.warning("invalid_sqs_message", message_id=message_id)
            return

        try:
            queued = parse_queue_body(body)
        except InvalidQueueMessage as exc:
            logger.warning(
                "invalid_review_message",
                message_id=message_id,
                receive_count=_receive_count(message),
                validation_error=exc.code,
            )
            return

        key = queued.idempotency_key
        claim: IdempotencyClaim | None = None
        heartbeat: asyncio.Task[None] | None = None
        try:
            if self.occurrence_store is not None:
                await self.occurrence_store.capture(queued.occurrence)
            claim = await self.idempotency_store.claim(key)
            if claim.status == ClaimStatus.COMPLETED:
                await self._delete(receipt_handle)
                logger.info(
                    "duplicate_review_message",
                    alert_id=queued.raw_alert["alert_id"],
                    decision_key=key,
                    message_id=message_id,
                )
                return
            if claim.status == ClaimStatus.BUSY:
                logger.info(
                    "review_already_in_progress",
                    alert_id=queued.raw_alert["alert_id"],
                    decision_key=key,
                    message_id=message_id,
                )
                return

            heartbeat = asyncio.create_task(
                self._heartbeat(
                    receipt_handle,
                    key if claim.status == ClaimStatus.ACQUIRED else None,
                )
            )
            outcome = claim.outcome
            if claim.status == ClaimStatus.ACQUIRED:
                outcome = self.alert_filter.evaluate(queued.raw_alert)
                await self.idempotency_store.store_result(key, outcome)

            if outcome is None:
                raise RuntimeError("Idempotency record has no stored filter outcome")
            if outcome.accepted:
                await self.publisher.publish(outcome.alert)
            await self.idempotency_store.mark_completed(key)
            await self._delete(receipt_handle)
            event_name = (
                "queued_alert_validated" if outcome.accepted else "queued_alert_filtered"
            )
            logger.info(
                event_name,
                alert_id=queued.raw_alert["alert_id"],
                decision_key=key,
                message_id=message_id,
                reason_codes=outcome.reason_codes,
                policy_version=outcome.policy_version,
            )
        except Exception as exc:
            if claim is not None and claim.status == ClaimStatus.ACQUIRED:
                try:
                    await self.idempotency_store.release(key)
                except Exception:
                    logger.exception("idempotency_release_failed", decision_key=key)
            logger.warning(
                "queued_review_failed",
                alert_id=queued.raw_alert["alert_id"],
                decision_key=key,
                message_id=message_id,
                receive_count=_receive_count(message),
                error_type=type(exc).__name__,
            )
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with suppress(asyncio.CancelledError):
                    await heartbeat

    async def _delete(self, receipt_handle: str) -> None:
        await asyncio.to_thread(
            self.sqs_client.delete_message,
            QueueUrl=self.settings.sqs_input_queue_url,
            ReceiptHandle=receipt_handle,
        )

    async def _heartbeat(
        self,
        receipt_handle: str,
        idempotency_key: str | None = None,
    ) -> None:
        interval = max(1, self.settings.sqs_visibility_timeout_seconds // 3)
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.to_thread(
                    self.sqs_client.change_message_visibility,
                    QueueUrl=self.settings.sqs_input_queue_url,
                    ReceiptHandle=receipt_handle,
                    VisibilityTimeout=self.settings.sqs_visibility_timeout_seconds,
                )
            except Exception as exc:
                logger.warning(
                    "sqs_visibility_extension_failed",
                    error_type=type(exc).__name__,
                )
            if idempotency_key is not None:
                try:
                    await self.idempotency_store.extend(idempotency_key)
                except Exception as exc:
                    logger.warning(
                        "idempotency_lock_extension_failed",
                        decision_key=idempotency_key,
                        error_type=type(exc).__name__,
                    )


def _receive_count(message: dict[str, Any]) -> int | None:
    raw = message.get("Attributes", {}).get("ApproximateReceiveCount")
    try:
        return int(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None
