from __future__ import annotations

import json
import math

import pytest

from alert_reviewer.config import Settings
from alert_reviewer.eventbridge import (
    EventBridgeValidatedAlertPublisher,
    ValidatedAlertPublishError,
)


class FakeEvents:
    def __init__(self, response=None):
        self.response = response or {"FailedEntryCount": 0, "Entries": [{"EventId": "1"}]}
        self.calls = []

    def put_events(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def _settings():
    return Settings(
        eventbridge_output_bus_name="acceptance-intelligence-bus-dev",
    )


async def test_publishes_validated_contract_without_mutating_kipu_detail(alert):
    client = FakeEvents()
    publisher = EventBridgeValidatedAlertPublisher(client, _settings())
    detail = alert.model_dump(mode="json")
    detail.update({"batch_id": "batch-001", "group_name": "world_cup"})

    await publisher.publish(detail)

    entry = client.calls[0]["Entries"][0]
    assert entry["Source"] == "acceptance.reviewer"
    assert entry["DetailType"] == "Anomaly Validated v1"
    assert entry["EventBusName"] == "acceptance-intelligence-bus-dev"
    assert json.loads(entry["Detail"]) == detail


async def test_publishes_v2_contract_with_versioned_detail_type(v2_alert):
    client = FakeEvents()
    publisher = EventBridgeValidatedAlertPublisher(client, _settings())
    detail = v2_alert.model_dump(mode="json")

    await publisher.publish(detail)

    entry = client.calls[0]["Entries"][0]
    assert entry["DetailType"] == "Anomaly Validated v1"
    assert json.loads(entry["Detail"]) == detail


async def test_eventbridge_failed_entry_raises_for_sqs_retry(alert):
    client = FakeEvents(
        {
            "FailedEntryCount": 1,
            "Entries": [{"ErrorCode": "InternalFailure"}],
        }
    )
    publisher = EventBridgeValidatedAlertPublisher(client, _settings())

    with pytest.raises(ValidatedAlertPublishError, match="InternalFailure"):
        await publisher.publish(alert.model_dump(mode="json"))


async def test_non_json_payload_raises_before_eventbridge_call(alert):
    client = FakeEvents()
    publisher = EventBridgeValidatedAlertPublisher(client, _settings())
    detail = alert.model_dump(mode="json")
    detail["unexpected_metric"] = math.nan

    with pytest.raises(ValidatedAlertPublishError, match="strict JSON"):
        await publisher.publish(detail)

    assert client.calls == []
