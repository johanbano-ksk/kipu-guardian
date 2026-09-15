"""Daily local extraction uses the authoritative archive and never contacts AWS in tests."""

from copy import deepcopy
from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from boto3.dynamodb.types import TypeSerializer
from botocore.exceptions import ClientError, NoCredentialsError, UnauthorizedSSOTokenError

from alert_reviewer import guardian_daily
from alert_reviewer.gemini_review import GeminiReviewError
from alert_reviewer.guardian_daily import (
    DailyReviewError,
    DailyReviewSettings,
    GuardianDailyReview,
    OccurrenceReader,
)


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            # UTC is already September 8; Ecuador is still September 7.
            return datetime(2026, 9, 8, 2, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(guardian_daily, "datetime", FixedDateTime)
    monkeypatch.setattr(
        guardian_daily.boto3,
        "Session",
        Mock(side_effect=AssertionError("Tests must not initialize AWS sessions")),
    )
    monkeypatch.setattr(
        guardian_daily,
        "GeminiAlertReviewer",
        Mock(side_effect=AssertionError("Unexpected Gemini initialization")),
    )


def settings(**overrides):
    return DailyReviewSettings(
        _env_file=None,
        **{
            "gemini_api_key": "",
            "openai_api_key": "",
            "ai_provider": "gemini",
            **overrides,
        },
    )


def alert_payload(**changes):
    return {
        "schema_version": "1.0",
        "alert_id": "synthetic-alert",
        "merchant_code": "synthetic-mid",
        "merchant_name": "Synthetic merchant",
        "country": "Ecuador",
        "timestamp": "2026-09-07T12:00:00Z",
        "criticality": "Critica",
        "approval_rate": 0.1,
        "rolling_avg_approval_rate": 0.9,
        "total_transactions": 100,
        "declined_count": 90,
        **changes,
    }


def occurrence(alert=None, **changes):
    payload = alert_payload() if alert is None else alert
    return {
        "idempotency_key": "occurrence#event:synthetic-event",
        "record_type": "ALERT_OCCURRENCE",
        "event_source": "acceptance.kipu",
        "detail_type": "Anomaly Detected v1",
        "schema_version": "1.0",
        "alert_id": payload["alert_id"],
        "publication_timestamp": "2026-09-07T12:00:01Z",
        "recorded_at": "2026-09-07T12:00:02Z",
        "raw_payload": deepcopy(payload),
        **changes,
    }


def reviewer(records, **overrides):
    reader = SimpleNamespace(read=Mock(return_value=deepcopy(records)))
    return GuardianDailyReview(settings=settings(**overrides), reader=reader), reader


@pytest.mark.parametrize(
    "day",
    [None, 20260907, True, "", "20260907", "2026-9-7", "2026-09-07T00:00:00Z",
     "2026-09-07 ", "2026-02-30", "2026-09-07/../file", date(2026, 9, 7)],
)
def test_invalid_date_fails_before_source_io(day):
    agent, reader = reviewer([occurrence()])
    with pytest.raises(DailyReviewError) as error:
        agent.run(day, "policy")
    assert error.value.code == "INVALID_DATE"
    reader.read.assert_not_called()


def test_future_day_uses_ecuador_not_utc_and_does_not_read_source():
    agent, reader = reviewer([occurrence()])
    with pytest.raises(DailyReviewError) as error:
        agent.run("2026-09-08", "policy")
    assert error.value.code == "FUTURE_DATE"
    reader.read.assert_not_called()


@pytest.mark.parametrize("mode", [None, "", "gemini", "AI", True, {"mode": "policy"}])
def test_invalid_mode_fails_before_source_io(mode):
    agent, reader = reviewer([occurrence()])
    with pytest.raises(DailyReviewError) as error:
        agent.run("2026-09-07", mode)
    assert error.value.code == "INVALID_REVIEW_MODE"
    reader.read.assert_not_called()


def test_policy_execution_preserves_original_alert_context_without_gemini():
    original = alert_payload()
    agent, reader = reviewer([occurrence(original)])
    snapshot = agent.run("2026-09-07", "policy")
    assert snapshot["business_date"] == "2026-09-07"
    assert snapshot["source"] == "eventbridge_occurrences"
    assert snapshot["schema_version"] == "1.0"
    assert snapshot["review_mode"] == "policy"
    assert snapshot["review_provider"] is None
    assert snapshot["review_model"] is None
    assert snapshot["policy_version"]
    assert snapshot["source_record_count"] == snapshot["evaluated_record_count"] == 1
    assert snapshot["accepted_alerts"] == [
        {**original, "kipu_generated_at": original["timestamp"]}
    ]
    assert datetime.fromisoformat(snapshot["extracted_at"]).tzinfo is not None
    reader.read.assert_called_once_with()
    guardian_daily.GeminiAlertReviewer.assert_not_called()


def test_dates_are_not_replaced_by_previous_snapshot_or_generation_date():
    first = occurrence(
        alert_payload(alert_id="first-day", timestamp="2026-09-05T23:00:00Z"),
        publication_timestamp="2026-09-07T04:59:59Z",
    )
    second = occurrence(
        alert_payload(alert_id="second-day"),
        publication_timestamp="2026-09-07T05:00:00Z",
    )
    agent, reader = reviewer([first, second])
    previous = agent.run("2026-09-06", "policy")
    current = agent.run("2026-09-07", "policy")
    assert previous["business_date"] == "2026-09-06"
    assert current["business_date"] == "2026-09-07"
    assert [a["alert_id"] for a in previous["accepted_alerts"]] == ["first-day"]
    assert [a["alert_id"] for a in current["accepted_alerts"]] == ["second-day"]
    assert previous["accepted_alerts"][0]["kipu_generated_at"] == "2026-09-05T23:00:00Z"
    assert reader.read.call_count == 2


def test_latest_rejected_state_is_not_replaced_with_older_acceptance():
    accepted = occurrence()
    rejected = occurrence(
        alert_payload(approval_rate=0.99, declined_count=1),
        idempotency_key="occurrence#event:later",
        publication_timestamp="2026-09-07T13:00:01Z",
    )
    agent, _ = reviewer([accepted, rejected])
    snapshot = agent.run("2026-09-07", "policy")
    assert snapshot["source_record_count"] == 2
    assert snapshot["evaluated_record_count"] == 1
    assert snapshot["accepted_alerts"] == []
    assert snapshot["country_summary"] == [
        {"country": "Ecuador", "received_count": 2, "accepted_count": 0}
    ]


def test_country_with_only_rejected_alerts_remains_visible():
    colombia = occurrence(
        alert_payload(alert_id="colombia-small", country="Colombia",
                      total_transactions=10, declined_count=9)
    )
    agent, _ = reviewer([occurrence(), colombia])
    snapshot = agent.run("2026-09-07", "policy")
    assert snapshot["country_summary"] == [
        {"country": "Colombia", "received_count": 1, "accepted_count": 0},
        {"country": "Ecuador", "received_count": 1, "accepted_count": 1},
    ]


@pytest.mark.parametrize(
    "records", [[], [occurrence(publication_timestamp="2026-09-06T12:00:00Z")]]
)
def test_missing_day_is_explicit_not_stale_snapshot_or_source_fallback(records):
    agent, reader = reviewer(records)
    with pytest.raises(DailyReviewError) as error:
        agent.run("2026-09-07", "policy")
    assert error.value.code == "SNAPSHOT_NOT_FOUND"
    reader.read.assert_called_once_with()


def test_ai_requires_its_configured_provider_key_without_fallback():
    agent, _ = reviewer([occurrence()], openai_api_key="synthetic-other-key")
    with pytest.raises(DailyReviewError) as error:
        agent.run("2026-09-07", "ai")
    assert error.value.code == "AI_NOT_CONFIGURED"
    guardian_daily.GeminiAlertReviewer.assert_not_called()


def test_ai_cannot_broaden_policy_and_preserves_review_metadata(monkeypatch):
    ai = SimpleNamespace(config=SimpleNamespace(model="gemini-synthetic"),
                         select=Mock(return_value={0, 1}))
    factory = Mock(return_value=ai)
    monkeypatch.setattr(guardian_daily, "GeminiAlertReviewer", factory)
    agent, _ = reviewer(
        [occurrence(), occurrence(alert_payload(alert_id="small", total_transactions=10,
                                                declined_count=9))],
        gemini_api_key="synthetic-key", gemini_model="gemini-synthetic",
    )
    snapshot = agent.run("2026-09-07", "ai")
    assert [a["alert_id"] for a in snapshot["accepted_alerts"]] == ["synthetic-alert"]
    assert snapshot["review_mode"] == "ai"
    assert snapshot["review_provider"] == "gemini"
    assert snapshot["review_model"] == "gemini-synthetic"
    factory.assert_called_once()
    ai.select.assert_called_once()


def test_ai_failure_does_not_silently_fall_back_to_policy(monkeypatch):
    ai = SimpleNamespace(config=SimpleNamespace(model="gemini-synthetic"),
                         select=Mock(side_effect=GeminiReviewError("AI_PROVIDER_UNAVAILABLE")))
    monkeypatch.setattr(guardian_daily, "GeminiAlertReviewer", Mock(return_value=ai))
    agent, _ = reviewer([occurrence()], gemini_api_key="synthetic-key")
    with pytest.raises(DailyReviewError) as error:
        agent.run("2026-09-07", "ai")
    assert error.value.code == "AI_REVIEW_FAILED"


def dynamodb_item(**changes):
    value = occurrence()
    # DynamoDB numeric representation is Decimal, not float.
    value["raw_payload"] = {**value["raw_payload"], "approval_rate": Decimal("0.1"),
                            "rolling_avg_approval_rate": Decimal("0.9")}
    value.update(changes)
    serializer = TypeSerializer()
    return {key: serializer.serialize(item) for key, item in value.items()}


def occurrence_reader(monkeypatch, pages):
    client = SimpleNamespace(scan=Mock(side_effect=pages))
    reader = OccurrenceReader(settings())
    monkeypatch.setattr(reader, "_client", lambda: (client, "synthetic-table"))
    return reader, client


def test_occurrence_reader_follows_pagination_and_decodes_numeric_items(monkeypatch):
    key = {"idempotency_key": {"S": "occurrence#event:first"}}
    reader, client = occurrence_reader(monkeypatch, [
        {"Items": [dynamodb_item()], "LastEvaluatedKey": key},
        {"Items": [dynamodb_item(idempotency_key="occurrence#event:second")]},
    ])
    records = reader.read()
    assert len(records) == 2
    assert records[0]["raw_payload"]["approval_rate"] == 0.1
    assert records[0]["raw_payload"]["total_transactions"] == 100
    assert client.scan.call_args_list[0].kwargs["TableName"] == "synthetic-table"
    assert "ExclusiveStartKey" not in client.scan.call_args_list[0].kwargs
    assert client.scan.call_args_list[1].kwargs["ExclusiveStartKey"] == key


def test_occurrence_reader_detects_pagination_cycle_without_returning_partial_records(monkeypatch):
    key = {"idempotency_key": {"S": "occurrence#event:cycle"}}
    reader, client = occurrence_reader(monkeypatch, [
        {"Items": [dynamodb_item()], "LastEvaluatedKey": key},
        {"Items": [], "LastEvaluatedKey": key},
    ])
    with pytest.raises(DailyReviewError) as error:
        reader.read()
    assert error.value.code == "EXTRACTION_LIMIT"
    assert client.scan.call_count == 2


@pytest.mark.parametrize("limit_name,limit_value", [
    ("MAX_SCAN_PAGES", 1), ("MAX_RECORDS", 1), ("MAX_SCAN_BYTES", 1),
])
def test_occurrence_reader_limits_do_not_return_truncated_success(
    monkeypatch, limit_name, limit_value,
):
    monkeypatch.setattr(guardian_daily, limit_name, limit_value)
    key = {"idempotency_key": {"S": "occurrence#event:first"}}
    reader, _ = occurrence_reader(monkeypatch, [
        {"Items": [dynamodb_item(), dynamodb_item()], "LastEvaluatedKey": key},
        {"Items": []},
    ])
    with pytest.raises(DailyReviewError) as error:
        reader.read()
    assert error.value.code == "EXTRACTION_LIMIT"


@pytest.mark.parametrize("aws_code,expected", [
    ("AccessDeniedException", "ALERT_SOURCE_ACCESS_DENIED"),
    ("AccessDenied", "ALERT_SOURCE_ACCESS_DENIED"),
    ("ExpiredTokenException", "SSO_EXPIRED"),
    ("ExpiredToken", "SSO_EXPIRED"),
    ("InternalServerError", "EXTRACTION_FAILED"),
])
def test_occurrence_reader_maps_aws_failures_without_leaking_provider_body(
    monkeypatch, aws_code, expected,
):
    secret_marker = "synthetic-sensitive-provider-text"
    failure = ClientError({"Error": {"Code": aws_code, "Message": secret_marker}}, "Scan")
    reader, client = occurrence_reader(monkeypatch, failure)
    with pytest.raises(DailyReviewError) as error:
        reader.read()
    assert error.value.code == expected
    assert secret_marker not in str(error.value)
    client.scan.assert_called_once()


@pytest.mark.parametrize("failure,expected", [
    (UnauthorizedSSOTokenError(), "SSO_EXPIRED"),
    (NoCredentialsError(), "EXTRACTION_FAILED"),
])
def test_occurrence_reader_maps_missing_or_expired_session(monkeypatch, failure, expected):
    reader, _ = occurrence_reader(monkeypatch, failure)
    with pytest.raises(DailyReviewError) as error:
        reader.read()
    assert error.value.code == expected


def test_occurrence_reader_maps_session_failure_during_source_discovery(monkeypatch):
    reader = OccurrenceReader(settings())
    lookup = Mock(side_effect=UnauthorizedSSOTokenError())
    monkeypatch.setattr(reader, "_client", lookup)
    with pytest.raises(DailyReviewError) as error:
        reader.read()
    assert error.value.code == "SSO_EXPIRED"
    lookup.assert_called_once_with()


def test_occurrence_reader_deadline_never_returns_partial_records(monkeypatch):
    monkeypatch.setattr(guardian_daily.time, "monotonic", Mock(side_effect=[0, 0, 121]))
    reader, client = occurrence_reader(monkeypatch, [{"Items": [dynamodb_item()]}])
    with pytest.raises(DailyReviewError) as error:
        reader.read()
    assert error.value.code == "EXTRACTION_LIMIT"
    client.scan.assert_called_once()


def test_occurrence_reader_uses_known_archive_role_and_not_datalake_credentials(monkeypatch):
    cloudformation = SimpleNamespace(describe_stacks=Mock(return_value={
        "Stacks": [{"Outputs": [
            {"OutputKey": "IdempotencyTableName", "OutputValue": "reviewer-table"},
            {"OutputKey": "LocalWorkerRoleArn", "OutputValue": "synthetic-role"},
        ]}],
    }))
    sts = SimpleNamespace(assume_role=Mock(return_value={"Credentials": {
        "AccessKeyId": "synthetic-id", "SecretAccessKey": "synthetic-secret",
        "SessionToken": "synthetic-token",
    }}))
    dynamodb = object()
    control = SimpleNamespace(client=Mock(side_effect=lambda name, **_: {
        "cloudformation": cloudformation, "sts": sts,
    }[name]))
    runtime = SimpleNamespace(client=Mock(return_value=dynamodb))
    sessions = Mock(side_effect=[control, runtime])
    monkeypatch.setattr(guardian_daily.boto3, "Session", sessions)
    client, table = OccurrenceReader(settings())._client()
    assert client is dynamodb
    assert table == "reviewer-table"
    assert sessions.call_args_list[0].kwargs == {
        "profile_name": "ia-dev-payments-intelligence", "region_name": "us-east-1",
    }
    cloudformation.describe_stacks.assert_called_once_with(
        StackName="pmt-intel-kipu-alert-reviewer-mvp"
    )
    assert sts.assume_role.call_args.kwargs["RoleArn"] == "synthetic-role"
    assert sessions.call_args_list[1].kwargs["aws_access_key_id"] == "synthetic-id"
    assert runtime.client.call_args.args == ("dynamodb",)
