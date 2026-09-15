"""Console launcher for the EventBridge/SQS review worker."""

from __future__ import annotations

import asyncio

import boto3

from alert_reviewer.alert_filter import FilterPolicy, PayloadAlertFilter
from alert_reviewer.config import get_settings
from alert_reviewer.eventbridge import EventBridgeValidatedAlertPublisher
from alert_reviewer.logging import configure_logging
from alert_reviewer.occurrence import DynamoDBOccurrenceStore
from alert_reviewer.queue import DynamoDBIdempotencyStore, SQSReviewConsumer


async def serve() -> None:
    settings = get_settings()
    configure_logging(settings.log_level)
    if (
        settings.sqs_input_queue_url is None
        or settings.sqs_idempotency_table is None
        or settings.eventbridge_output_bus_name is None
    ):
        raise RuntimeError(
            "Configure SQS_INPUT_QUEUE_URL, SQS_IDEMPOTENCY_TABLE, and "
            "EVENTBRIDGE_OUTPUT_BUS_NAME before starting the worker"
        )

    alert_filter = PayloadAlertFilter(FilterPolicy.load(settings.filter_policy_path))

    aws = boto3.Session(region_name=settings.aws_region)
    sqs_client = aws.client("sqs")
    dynamodb_client = aws.client("dynamodb")
    events_client = aws.client("events")
    idempotency_store = DynamoDBIdempotencyStore(
        dynamodb_client,
        settings.sqs_idempotency_table,
        ttl_hours=settings.sqs_idempotency_ttl_hours,
        lock_seconds=settings.sqs_visibility_timeout_seconds,
    )
    occurrence_store = DynamoDBOccurrenceStore(
        dynamodb_client,
        settings.sqs_idempotency_table,
        ttl_hours=settings.alert_occurrence_ttl_hours,
    )
    publisher = EventBridgeValidatedAlertPublisher(events_client, settings)
    consumer = SQSReviewConsumer(
        sqs_client=sqs_client,
        alert_filter=alert_filter,
        publisher=publisher,
        idempotency_store=idempotency_store,
        occurrence_store=occurrence_store,
        settings=settings,
    )
    await consumer.run_forever()


def run() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    run()
