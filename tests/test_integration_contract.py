from __future__ import annotations

import inspect
import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from alert_reviewer import worker
from alert_reviewer.alert_filter import (
    FilterPolicy,
    FilterReason,
    evaluate_alert,
    filter_alerts,
)
from alert_reviewer.queue import InvalidQueueMessage, parse_queue_body

ROOT = Path(__file__).parents[1]
POLICY_PATH = ROOT / "config" / "filter_policy.yaml"
KIPU_EVENT_PATH = ROOT / "examples" / "kipu-event.json"
KIPU_EVENT_V2_PATH = ROOT / "examples" / "kipu-event-v2.json"
HISTORICAL_REPORT_PATH = ROOT / "reports" / "filtered-alerts-2026-08-13.json"
EXPECTED_CRITICAL_ALERT_IDS = [
    "11089acf-8f74-4b8a-a802-4b0d47845752",
    "370ef703-d082-4c55-8ba4-6408d3f38e7c",
    "caffbd00-ee83-4d68-9c6e-c281c530d1dc",
    "d4619cbb-28fe-454d-9a26-5949b4a6445d",
    "6d8ed885-6852-4373-a8a5-c23981439296",
    "a123f80a-9882-42e7-84d0-dc1b872e74c6",
    "fc1bdda7-df4b-4ff1-b9fa-04e3be6a6056",
    "64ceffd2-147a-4c42-a595-860a3aafb4a1",
    "ff79298e-9064-4eb9-8009-3da348a10a88",
    "1f2f0eda-4c46-4a6c-bfb8-25c73d650c5b",
    "5d727fb3-6d8b-4d77-adc3-3069bc286d54",
]


class CloudFormationLoader(yaml.SafeLoader):
    pass


def _construct_tag(loader, _suffix, node):
    if isinstance(node, yaml.ScalarNode):
        return loader.construct_scalar(node)
    if isinstance(node, yaml.SequenceNode):
        return loader.construct_sequence(node)
    return loader.construct_mapping(node)


CloudFormationLoader.add_multi_constructor("!", _construct_tag)


def test_real_kipu_fixture_matches_consumer_contract():
    event = json.loads(KIPU_EVENT_PATH.read_text(encoding="utf-8"))

    queued = parse_queue_body(json.dumps(event))

    assert queued.idempotency_key == "decision#event:example-event-001"
    assert queued.raw_alert["batch_id"] == "20260730_120000_abcd1234"
    assert queued.raw_alert["group_name"] == "world_cup"


def test_real_kipu_fixture_is_accepted_only_by_severe_deterioration():
    event = json.loads(KIPU_EVENT_PATH.read_text(encoding="utf-8"))
    policy = FilterPolicy.load(POLICY_PATH)

    outcome = evaluate_alert(event["detail"], policy)

    assert policy.version == "2026-08-13.1"
    assert outcome.accepted is True
    assert outcome.reason_codes == [FilterReason.SEVERE_APPROVAL_DETERIORATION]
    assert outcome.policy_version == policy.version


def test_runtime_rejects_unnegotiated_v2_contract(v2_alert):
    raw_alert = v2_alert.model_dump(mode="json")
    event = {
        "id": "event-v2-contract",
        "source": "acceptance.kipu",
        "detail-type": "Anomaly Detected v2",
        "detail": raw_alert,
    }
    with pytest.raises(InvalidQueueMessage) as exc_info:
        parse_queue_body(json.dumps(event))

    assert exc_info.value.code == "UNEXPECTED_DETAIL_TYPE"


def test_historical_v2_fixture_remains_reproducible_offline():
    event = json.loads(KIPU_EVENT_V2_PATH.read_text(encoding="utf-8"))
    policy = FilterPolicy.load(POLICY_PATH)

    outcome = evaluate_alert(event["detail"], policy)

    assert outcome.accepted is True
    assert outcome.reason_codes == [
        FilterReason.KIPU_LOW_APPROVAL_RATE,
        FilterReason.KIPU_APPROVAL_RATE_MAD_DROP,
        FilterReason.KIPU_DECLINES_ABOVE_P95,
        FilterReason.KIPU_DECLINES_DOUBLED,
    ]
    assert outcome.policy_version == "2026-08-18.1"


def test_real_kipu_v2_fixture_uses_producer_deterministic_alert_id():
    event = json.loads(KIPU_EVENT_V2_PATH.read_text(encoding="utf-8"))
    detail = event["detail"]
    namespace = uuid.uuid5(uuid.NAMESPACE_URL, "acceptance.kipu")
    identity = "|".join(
        (
            detail["merchant_code"],
            detail["country"],
            detail["observation_date"],
            detail["detection_engine"],
            detail["policy_version"],
        )
    )

    assert detail["alert_id"] == str(uuid.uuid5(namespace, identity))


def test_skill_cli_default_policy_returns_exact_original_detail():
    event = json.loads(KIPU_EVENT_PATH.read_text(encoding="utf-8"))
    script = ROOT / ".codex" / "skills" / "filter-valid-alerts" / "scripts" / "filter_alerts.py"

    completed = subprocess.run(
        [sys.executable, str(script), str(KIPU_EVENT_PATH)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [event["detail"]]


def test_skill_cli_v2_policy_returns_exact_original_detail():
    event = json.loads(KIPU_EVENT_V2_PATH.read_text(encoding="utf-8"))
    script = ROOT / ".codex" / "skills" / "filter-valid-alerts" / "scripts" / "filter_alerts.py"

    completed = subprocess.run(
        [sys.executable, str(script), str(KIPU_EVENT_V2_PATH)],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == [event["detail"]]


def test_critical_policy_backtest_keeps_exactly_expected_historical_alerts():
    historical_alerts = json.loads(HISTORICAL_REPORT_PATH.read_text(encoding="utf-8"))
    policy = FilterPolicy.load(POLICY_PATH)

    accepted = filter_alerts(historical_alerts, policy)

    assert len(historical_alerts) == 50
    assert len({alert["alert_id"] for alert in historical_alerts}) == 50
    assert [alert["alert_id"] for alert in accepted] == EXPECTED_CRITICAL_ALERT_IDS


def test_infrastructure_subscribes_only_to_versioned_kipu_events():
    template_path = ROOT / "infra" / "sqs-worker.yaml"
    template = yaml.load(
        template_path.read_text(encoding="utf-8"),
        Loader=CloudFormationLoader,
    )

    parameters = template["Parameters"]
    rule = template["Resources"]["ReviewEventRule"]["Properties"]
    target = rule["Targets"][0]

    assert parameters["EventBusName"]["Default"] == "acceptance-intelligence-bus-dev"
    assert parameters["EventSource"]["Default"] == "acceptance.kipu"
    assert parameters["EventDetailType"]["Default"] == "Anomaly Detected v1"
    assert "EventDetailTypeV2" not in parameters
    assert rule["EventPattern"] == {
        "source": ["EventSource"],
        "detail-type": ["EventDetailType"],
    }
    assert "DeadLetterConfig" in target
    assert "ReviewOutputQueue" not in template["Resources"]


def test_mvp_worker_role_is_scoped_to_reviewer_resources():
    template_path = ROOT / "infra" / "sqs-worker.yaml"
    template = yaml.load(
        template_path.read_text(encoding="utf-8"),
        Loader=CloudFormationLoader,
    )

    role = template["Resources"]["ReviewLocalWorkerRole"]["Properties"]
    assert role["AssumeRolePolicyDocument"]["Statement"][0]["Principal"] == {
        "AWS": "DeveloperPrincipalArn"
    }
    statements = role["Policies"][0]["PolicyDocument"]["Statement"]
    actions = {
        action
        for statement in statements
        for action in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    }
    assert actions == {
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueAttributes",
        "sqs:GetQueueUrl",
        "dynamodb:PutItem",
        "dynamodb:GetItem",
        "dynamodb:UpdateItem",
        "dynamodb:DeleteItem",
        "dynamodb:Scan",
        "events:PutEvents",
    }


def test_worker_source_has_no_external_evidence_dependencies():
    source = inspect.getsource(worker)

    assert "alert_reviewer.elastic" not in source
    assert "alert_reviewer.llm" not in source
    assert "AlertReviewer" not in source
    assert "build_search_client" not in source
