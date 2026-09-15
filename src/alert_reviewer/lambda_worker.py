"""AWS Lambda SQS consumer for continuously processing Kipu alerts."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import boto3
import structlog

from alert_reviewer.alert_filter import FilterPolicy, PayloadAlertFilter
from alert_reviewer.config import get_settings
from alert_reviewer.eventbridge import EventBridgeValidatedAlertPublisher
from alert_reviewer.logging import configure_logging
from alert_reviewer.occurrence import DynamoDBOccurrenceStore
from alert_reviewer.queue import (
    ClaimStatus,
    DynamoDBIdempotencyStore,
    IdempotencyClaim,
    IdempotencyStore,
    OccurrenceStore,
    ValidatedAlertPublisher,
    parse_queue_body,
)

logger = structlog.get_logger(__name__)


@dataclass(frozen=True)
class LambdaWorkerDependencies:
    alert_filter: PayloadAlertFilter
    publisher: ValidatedAlertPublisher
    idempotency_store: IdempotencyStore
    occurrence_store: OccurrenceStore


@lru_cache
def _dependencies() -> LambdaWorkerDependencies:
    settings = get_settings()
    configure_logging(settings.log_level)
    if not settings.sqs_idempotency_table or not settings.eventbridge_output_bus_name:
        raise RuntimeError(
            "Configure SQS_IDEMPOTENCY_TABLE and EVENTBRIDGE_OUTPUT_BUS_NAME"
        )
    session = boto3.Session(region_name=settings.aws_region)
    dynamodb = session.client("dynamodb")
    return LambdaWorkerDependencies(
        alert_filter=PayloadAlertFilter(FilterPolicy.load(settings.filter_policy_path)),
        publisher=EventBridgeValidatedAlertPublisher(session.client("events"), settings),
        idempotency_store=DynamoDBIdempotencyStore(
            dynamodb,
            settings.sqs_idempotency_table,
            ttl_hours=settings.sqs_idempotency_ttl_hours,
            lock_seconds=settings.sqs_visibility_timeout_seconds,
        ),
        occurrence_store=DynamoDBOccurrenceStore(
            dynamodb,
            settings.sqs_idempotency_table,
            ttl_hours=settings.alert_occurrence_ttl_hours,
        ),
    )


async def process_record(
    record: dict[str, Any],
    dependencies: LambdaWorkerDependencies,
) -> None:
    message_id = str(record.get("messageId") or "unknown")
    body = record.get("body")
    if not isinstance(body, str):
        raise ValueError("SQS record body must be text")
    queued = parse_queue_body(body)
    claim: IdempotencyClaim | None = None
    try:
        await dependencies.occurrence_store.capture(queued.occurrence)
        claim = await dependencies.idempotency_store.claim(queued.idempotency_key)
        if claim.status == ClaimStatus.COMPLETED:
            logger.info(
                "duplicate_lambda_review_message",
                message_id=message_id,
                decision_key=queued.idempotency_key,
            )
            return
        if claim.status == ClaimStatus.BUSY:
            raise RuntimeError("Review is already in progress")

        outcome = claim.outcome
        if claim.status == ClaimStatus.ACQUIRED:
            outcome = dependencies.alert_filter.evaluate(queued.raw_alert)
            await dependencies.idempotency_store.store_result(
                queued.idempotency_key,
                outcome,
            )
        if outcome is None:
            raise RuntimeError("Idempotency record has no stored filter outcome")
        if outcome.accepted:
            await dependencies.publisher.publish(outcome.alert)
        await dependencies.idempotency_store.mark_completed(queued.idempotency_key)
        logger.info(
            "lambda_alert_validated" if outcome.accepted else "lambda_alert_filtered",
            message_id=message_id,
            alert_id=queued.raw_alert["alert_id"],
            decision_key=queued.idempotency_key,
            policy_version=outcome.policy_version,
        )
    except Exception:
        if claim is not None and claim.status == ClaimStatus.ACQUIRED:
            try:
                await dependencies.idempotency_store.release(queued.idempotency_key)
            except Exception:
                logger.exception(
                    "lambda_idempotency_release_failed",
                    decision_key=queued.idempotency_key,
                )
        raise


async def _process_batch(
    records: list[dict[str, Any]],
    dependencies: LambdaWorkerDependencies,
) -> list[dict[str, str]]:
    results = await asyncio.gather(
        *(process_record(record, dependencies) for record in records),
        return_exceptions=True,
    )
    failures: list[dict[str, str]] = []
    for record, result in zip(records, results, strict=True):
        if isinstance(result, BaseException):
            message_id = str(record.get("messageId") or "unknown")
            logger.warning(
                "lambda_review_failed",
                message_id=message_id,
                error_type=type(result).__name__,
            )
            failures.append({"itemIdentifier": message_id})
    return failures


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    records = event.get("Records")
    if not isinstance(records, list) or not all(isinstance(item, dict) for item in records):
        raise ValueError("Lambda event must contain SQS Records")
    failures = asyncio.run(_process_batch(records, _dependencies()))
    return {"batchItemFailures": failures}
