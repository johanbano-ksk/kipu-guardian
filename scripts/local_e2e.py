"""Run the real reviewer topology locally against LocalStack."""

from __future__ import annotations

import argparse
import copy
import json
import subprocess
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

PROJECT_ROOT = Path(__file__).resolve().parents[1]
COMPOSE_FILE = PROJECT_ROOT / "compose.local.yaml"
DEFAULT_EVENT = PROJECT_ROOT / "examples" / "kipu-event.json"

ENDPOINT_URL = "http://localhost:4566"
REGION = "us-east-1"
ACCOUNT_ID = "000000000000"
EVENT_BUS = "acceptance-intelligence-bus-dev"
INPUT_QUEUE = "kipu-alert-reviewer-input"
INPUT_DLQ = "kipu-alert-reviewer-dlq"
HUB_QUEUE = "kipu-alert-reviewer-local-hub"
IDEMPOTENCY_TABLE = "kipu-alert-reviewer-idempotency"
INPUT_RULE = "kipu-alert-reviewer-input-local"
HUB_RULE = "kipu-alert-reviewer-hub-local"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Exercise Kipu -> reviewer -> Hub with local AWS services."
    )
    parser.add_argument(
        "--event",
        type=Path,
        default=DEFAULT_EVENT,
        help="Kipu EventBridge event JSON to publish.",
    )
    parser.add_argument(
        "--no-start",
        action="store_true",
        help="Use an already-running compose.local.yaml stack.",
    )
    return parser.parse_args()


def start_localstack() -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "up",
            "-d",
            "localstack",
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )


def start_worker() -> None:
    subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(COMPOSE_FILE),
            "up",
            "-d",
            "--build",
            "local-worker",
        ],
        cwd=PROJECT_ROOT,
        check=True,
    )


def wait_for_localstack(timeout_seconds: int = 90) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(
                f"{ENDPOINT_URL}/_localstack/health",
                timeout=2,
            ) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(1)
    raise TimeoutError("LocalStack did not become ready")


def aws_clients() -> tuple[Any, Any, Any]:
    session = boto3.Session(
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name=REGION,
    )
    options = {"endpoint_url": ENDPOINT_URL, "region_name": REGION}
    return (
        session.client("events", **options),
        session.client("sqs", **options),
        session.client("dynamodb", **options),
    )


def ensure_topology(events: Any, sqs: Any, dynamodb: Any) -> dict[str, str]:
    try:
        events.create_event_bus(Name=EVENT_BUS)
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceAlreadyExistsException":
            raise

    dlq_url = sqs.create_queue(QueueName=INPUT_DLQ)["QueueUrl"]
    dlq_arn = queue_arn(sqs, dlq_url)
    input_url = sqs.create_queue(
        QueueName=INPUT_QUEUE,
        Attributes={
            "VisibilityTimeout": "2",
            "RedrivePolicy": json.dumps(
                {
                    "deadLetterTargetArn": dlq_arn,
                    "maxReceiveCount": 3,
                }
            ),
        },
    )["QueueUrl"]
    hub_url = sqs.create_queue(QueueName=HUB_QUEUE)["QueueUrl"]

    try:
        dynamodb.create_table(
            TableName=IDEMPOTENCY_TABLE,
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[
                {"AttributeName": "idempotency_key", "AttributeType": "S"}
            ],
            KeySchema=[{"AttributeName": "idempotency_key", "KeyType": "HASH"}],
        )
    except ClientError as exc:
        if exc.response["Error"]["Code"] != "ResourceInUseException":
            raise

    input_rule_arn = events.put_rule(
        Name=INPUT_RULE,
        EventBusName=EVENT_BUS,
        EventPattern=json.dumps(
            {
                "source": ["acceptance.kipu"],
                "detail-type": ["Anomaly Detected v1"],
            }
        ),
        State="ENABLED",
    )["RuleArn"]
    hub_rule_arn = events.put_rule(
        Name=HUB_RULE,
        EventBusName=EVENT_BUS,
        EventPattern=json.dumps(
            {
                "source": ["acceptance.reviewer"],
                "detail-type": ["Anomaly Validated v1"],
            }
        ),
        State="ENABLED",
    )["RuleArn"]

    input_arn = queue_arn(sqs, input_url)
    hub_arn = queue_arn(sqs, hub_url)
    allow_eventbridge(sqs, input_url, input_arn, input_rule_arn)
    allow_eventbridge(sqs, hub_url, hub_arn, hub_rule_arn)

    assert not events.put_targets(
        Rule=INPUT_RULE,
        EventBusName=EVENT_BUS,
        Targets=[{"Id": "reviewer-input", "Arn": input_arn}],
    )["FailedEntryCount"]
    assert not events.put_targets(
        Rule=HUB_RULE,
        EventBusName=EVENT_BUS,
        Targets=[{"Id": "local-hub", "Arn": hub_arn}],
    )["FailedEntryCount"]

    return {"input": input_url, "hub": hub_url, "dlq": dlq_url}


def queue_arn(sqs: Any, queue_url: str) -> str:
    response = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["QueueArn"],
    )
    return response["Attributes"]["QueueArn"]


def allow_eventbridge(
    sqs: Any,
    queue_url: str,
    queue_resource_arn: str,
    rule_arn: str,
) -> None:
    policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Sid": "AllowEventBridge",
                "Effect": "Allow",
                "Principal": {"Service": "events.amazonaws.com"},
                "Action": "sqs:SendMessage",
                "Resource": queue_resource_arn,
                "Condition": {"ArnEquals": {"aws:SourceArn": rule_arn}},
            }
        ],
    }
    sqs.set_queue_attributes(
        QueueUrl=queue_url,
        Attributes={"Policy": json.dumps(policy)},
    )


def drain_queue(sqs: Any, queue_url: str) -> None:
    while True:
        messages = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=0,
        ).get("Messages", [])
        if not messages:
            return
        for message in messages:
            sqs.delete_message(
                QueueUrl=queue_url,
                ReceiptHandle=message["ReceiptHandle"],
            )


def publish_kipu_event(events: Any, event: dict[str, Any]) -> str:
    entry = {
        "Source": event["source"],
        "DetailType": event["detail-type"],
        "Detail": json.dumps(event["detail"], ensure_ascii=False),
        "EventBusName": EVENT_BUS,
    }
    event_time = event.get("time")
    if isinstance(event_time, str):
        entry["Time"] = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
    response = events.put_events(
        Entries=[entry]
    )
    if response["FailedEntryCount"]:
        raise RuntimeError(f"Local EventBridge rejected the event: {response['Entries']}")
    return str(response["Entries"][0]["EventId"])


def wait_for_occurrence(
    dynamodb: Any,
    event_id: str,
    expected_alert: dict[str, Any],
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    occurrence_key = f"occurrence#event:{event_id}"
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        item = dynamodb.get_item(
            TableName=IDEMPOTENCY_TABLE,
            Key={"idempotency_key": {"S": occurrence_key}},
            ConsistentRead=True,
        ).get("Item")
        if item is not None:
            if item.get("record_type") != {"S": "ALERT_OCCURRENCE"}:
                raise RuntimeError("Captured item has an unexpected record type")
            if json.loads(item["raw_payload"]["S"]) != expected_alert:
                raise RuntimeError("Captured occurrence did not preserve the raw alert")
            return item
        time.sleep(0.5)
    raise TimeoutError(f"No occurrence found for EventBridge event {event_id}")


def wait_for_event(
    sqs: Any,
    queue_url: str,
    alert_id: str,
    timeout_seconds: int,
) -> dict[str, Any] | None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        messages = sqs.receive_message(
            QueueUrl=queue_url,
            MaxNumberOfMessages=10,
            WaitTimeSeconds=1,
        ).get("Messages", [])
        for message in messages:
            payload = json.loads(message["Body"])
            sqs.delete_message(
                QueueUrl=queue_url,
                ReceiptHandle=message["ReceiptHandle"],
            )
            if payload.get("detail", {}).get("alert_id") == alert_id:
                return payload
    return None


def wait_for_outcome(
    dynamodb: Any,
    decision_key: str,
    timeout_seconds: int = 30,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        response = dynamodb.get_item(
            TableName=IDEMPOTENCY_TABLE,
            Key={"idempotency_key": {"S": decision_key}},
            ConsistentRead=True,
        )
        item = response.get("Item")
        if item and item.get("status", {}).get("S") == "COMPLETED":
            return json.loads(item["outcome"]["S"])
        time.sleep(0.5)
    raise TimeoutError(f"No completed outcome found for {decision_key}")


def unique_event(event: dict[str, Any], suffix: str) -> dict[str, Any]:
    result = copy.deepcopy(event)
    result["detail"]["alert_id"] = f"local-{suffix}-{uuid.uuid4().hex[:10]}"
    return result


def reviewer_decision_key(event_id: str) -> str:
    return f"decision#event:{event_id}"


def main() -> None:
    args = parse_args()
    event = json.loads(args.event.resolve().read_text(encoding="utf-8"))
    if not args.no_start:
        start_localstack()
    wait_for_localstack()

    events, sqs, dynamodb = aws_clients()
    queues = ensure_topology(events, sqs, dynamodb)
    drain_queue(sqs, queues["input"])
    drain_queue(sqs, queues["hub"])
    drain_queue(sqs, queues["dlq"])
    if not args.no_start:
        start_worker()

    accepted_event = unique_event(event, "accepted")
    schema_version = accepted_event["detail"].get("schema_version")
    if schema_version != "1.0":
        raise ValueError(f"Unsupported example schema: {schema_version}")
    accepted_event["detail"].update(
        {
            "criticality": "Critica",
            "approval_rate": 0.20,
            "rolling_avg_approval_rate": 0.25,
            "total_transactions": 100,
            "declined_count": 80,
        }
    )
    accepted_id = accepted_event["detail"]["alert_id"]
    superseded_v1_result = None
    alta_before_critical_result = None
    if schema_version == "2.0":
        superseded_v1_event = json.loads(DEFAULT_EVENT.read_text(encoding="utf-8"))
        superseded_v1_event["detail"]["alert_id"] = accepted_id
        superseded_v1_event["detail"]["superseded_by_schema_version"] = "2.0"
        superseded_event_id = publish_kipu_event(events, superseded_v1_event)
        superseded_key = reviewer_decision_key(superseded_event_id)
        superseded_outcome = wait_for_outcome(dynamodb, superseded_key)
        if superseded_outcome["accepted"]:
            raise RuntimeError("Superseded v1 transition copy was unexpectedly accepted")
        if superseded_outcome["reason_codes"] != ["SUPERSEDED_BY_V2"]:
            raise RuntimeError(
                "Superseded v1 transition copy returned an unexpected decision"
            )
        if wait_for_event(sqs, queues["hub"], accepted_id, 2) is not None:
            raise RuntimeError("Superseded v1 transition copy unexpectedly reached Hub")
        wait_for_occurrence(
            dynamodb,
            superseded_event_id,
            superseded_v1_event["detail"],
        )
        superseded_v1_result = {
            "alert_id": accepted_id,
            "decision_key": superseded_key,
            "event_id": superseded_event_id,
            "accepted": False,
            "reason_codes": superseded_outcome["reason_codes"],
            "reached_hub": False,
        }

        alta_event = copy.deepcopy(accepted_event)
        alta_event["detail"].update(
            {
                "criticality": "Alta",
                "approval_rate": 0.60,
                "approved_count": 60,
                "declined_count": 38,
                "unknown_status_count": 2,
                "total_transactions": 100,
                "rolling_avg_approval_rate": 0.70,
                "historical_avg_approval_rate": 0.72,
                "total_approved_amount": 0.0,
                "signal_codes": [
                    "LOW_APPROVAL_RATE",
                    "APPROVAL_RATE_MAD_DROP",
                ],
                "criteria_count": 2,
                "zscore_ta": -4.0,
                "zscore_rechazos": 0.0,
                "rolling_avg_rejections": 20.0,
                "rolling_q95_rejections": 50.0,
                "rejection_change_pct": 90.0,
                "is_anomaly_merchant": False,
                "is_anomaly_global": False,
                "isolation_forest_merchant_score": 0.1,
                "isolation_forest_global_score": 0.2,
                "is_volume_spike": False,
                "volume_ratio": None,
                "baseline_weekday_avg": None,
                "oldest_weekday_activity": None,
                "priority_score": 55.0,
                "priority_components": {
                    "volume_score": 5.0,
                    "approval_rate_score": 30.0,
                    "anomaly_score": 20.0,
                },
            }
        )
        alta_event_id = publish_kipu_event(events, alta_event)
        alta_key = reviewer_decision_key(alta_event_id)
        alta_outcome = wait_for_outcome(dynamodb, alta_key)
        if alta_outcome["accepted"]:
            raise RuntimeError("Alta alert was unexpectedly accepted as critical")
        if alta_outcome["reason_codes"] != ["NON_CRITICAL_ALERT"]:
            raise RuntimeError("Alta alert returned an unexpected decision")
        if wait_for_event(sqs, queues["hub"], accepted_id, 2) is not None:
            raise RuntimeError("Alta alert unexpectedly reached Hub")
        wait_for_occurrence(dynamodb, alta_event_id, alta_event["detail"])
        alta_before_critical_result = {
            "alert_id": accepted_id,
            "decision_key": alta_key,
            "event_id": alta_event_id,
            "accepted": False,
            "reason_codes": alta_outcome["reason_codes"],
            "reached_hub": False,
        }

    accepted_event_id = publish_kipu_event(events, accepted_event)
    accepted_hub_event = wait_for_event(sqs, queues["hub"], accepted_id, 30)
    accepted_key = reviewer_decision_key(accepted_event_id)
    accepted_outcome = wait_for_outcome(dynamodb, accepted_key)
    if accepted_hub_event is None:
        raise RuntimeError("Accepted alert did not reach the local Hub queue")
    wait_for_occurrence(dynamodb, accepted_event_id, accepted_event["detail"])

    rejected_event = unique_event(event, "stable")
    if schema_version == "1.0":
        rejected_event["detail"].update(
            {
                "criticality": "Critica",
                "approval_rate": 0.60,
                "rolling_avg_approval_rate": 0.61,
                "total_transactions": 100,
                "declined_count": 40,
            }
        )
    else:
        rejected_event["detail"].update(
            {
                "criticality": "Critica",
                "approval_rate": 0.80,
                "approved_count": 400,
                "declined_count": 100,
                "unknown_status_count": 0,
                "total_transactions": 500,
                "total_approved_amount": 5000.0,
                "signal_codes": [],
                "criteria_count": 0,
                "zscore_ta": 0.0,
                "zscore_rechazos": 0.0,
                "rolling_avg_rejections": 100.0,
                "rolling_q95_rejections": 200.0,
                "rejection_change_pct": 0.0,
                "is_anomaly_merchant": False,
                "is_anomaly_global": False,
                "isolation_forest_merchant_score": 0.2,
                "isolation_forest_global_score": 0.1,
                "is_volume_spike": False,
                "volume_ratio": None,
                "baseline_weekday_avg": None,
                "oldest_weekday_activity": None,
                "priority_score": 15.0,
                "priority_components": {
                    "volume_score": 10.0,
                    "approval_rate_score": 5.0,
                    "anomaly_score": 0.0,
                },
            }
        )
    rejected_id = rejected_event["detail"]["alert_id"]
    rejected_event_id = publish_kipu_event(events, rejected_event)
    rejected_key = reviewer_decision_key(rejected_event_id)
    rejected_outcome = wait_for_outcome(dynamodb, rejected_key)
    rejected_hub_event = wait_for_event(sqs, queues["hub"], rejected_id, 5)
    if rejected_hub_event is not None:
        raise RuntimeError("Rejected alert unexpectedly reached the local Hub queue")
    wait_for_occurrence(dynamodb, rejected_event_id, rejected_event["detail"])

    invalid_event = unique_event(event, "missing-name")
    invalid_event["detail"].pop("merchant_name", None)
    invalid_id = invalid_event["detail"]["alert_id"]
    invalid_event_id = publish_kipu_event(events, invalid_event)
    invalid_dlq_event = wait_for_event(sqs, queues["dlq"], invalid_id, 15)
    if invalid_dlq_event is None:
        raise RuntimeError("Structurally invalid alert did not reach the DLQ")
    invalid_hub_event = wait_for_event(sqs, queues["hub"], invalid_id, 2)
    if invalid_hub_event is not None:
        raise RuntimeError("Structurally invalid alert unexpectedly reached Hub")
    invalid_record = dynamodb.get_item(
        TableName=IDEMPOTENCY_TABLE,
        Key={"idempotency_key": {"S": reviewer_decision_key(invalid_event_id)}},
        ConsistentRead=True,
    ).get("Item")
    if invalid_record is not None:
        raise RuntimeError("Structurally invalid alert was unexpectedly persisted")
    invalid_occurrence = dynamodb.get_item(
        TableName=IDEMPOTENCY_TABLE,
        Key={
            "idempotency_key": {
                "S": f"occurrence#event:{invalid_event_id}"
            }
        },
        ConsistentRead=True,
    ).get("Item")
    if invalid_occurrence is not None:
        raise RuntimeError("Structurally invalid alert was unexpectedly archived")

    print(
        json.dumps(
            {
                "topology": "Kipu EventBridge -> SQS -> worker -> EventBridge -> Hub SQS",
                "accepted": {
                    "alert_id": accepted_id,
                    "event_id": accepted_event_id,
                    "decision_key": accepted_key,
                    "accepted": accepted_outcome["accepted"],
                    "reason_codes": accepted_outcome["reason_codes"],
                    "hub_source": accepted_hub_event["source"],
                    "hub_detail_type": accepted_hub_event["detail-type"],
                },
                "superseded_v1": superseded_v1_result,
                "alta_before_critical": alta_before_critical_result,
                "rejected": {
                    "alert_id": rejected_id,
                    "event_id": rejected_event_id,
                    "decision_key": rejected_key,
                    "accepted": rejected_outcome["accepted"],
                    "reason_codes": rejected_outcome["reason_codes"],
                    "reached_hub": False,
                },
                "invalid": {
                    "alert_id": invalid_id,
                    "event_id": invalid_event_id,
                    "missing_field": "merchant_name",
                    "occurrence_captured": False,
                    "persisted": False,
                    "reached_hub": False,
                    "reached_dlq": True,
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
