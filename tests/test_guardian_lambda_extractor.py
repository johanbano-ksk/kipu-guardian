"""Regression coverage for the authorized Lambda transport; all AWS calls are mocked."""

import base64
import json
from copy import deepcopy
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError, NoCredentialsError, UnauthorizedSSOTokenError

from alert_reviewer import guardian_daily
from alert_reviewer.guardian_daily import DailyReviewError, DailyReviewSettings, GuardianDailyReview

DAY = "2026-09-07"
SYNTHETIC_KEY = "synthetic-extractor-key-do-not-log"


@pytest.fixture(autouse=True)
def isolated_runtime(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 8, 2, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(guardian_daily, "datetime", FixedDateTime)
    monkeypatch.setattr(
        guardian_daily.boto3,
        "Session",
        Mock(side_effect=AssertionError("Tests must not initialize live AWS sessions")),
    )
    monkeypatch.setattr(
        guardian_daily,
        "OccurrenceReader",
        Mock(side_effect=AssertionError("The default path must never scan DynamoDB")),
    )
    monkeypatch.setattr(
        guardian_daily,
        "GeminiAlertReviewer",
        Mock(side_effect=AssertionError("The extractor owns daily AI review")),
    )


def settings(**changes):
    return DailyReviewSettings(
        _env_file=None,
        **{
            "agent_execution_key": SYNTHETIC_KEY,
            "gemini_api_key": "",
            "openai_api_key": "",
            "alerts_aws_profile": "ia-dev-payments-intelligence",
            "alerts_aws_region": "us-east-1",
            "alerts_extractor_function": "kipu-alert-reviewer-manual-extractor",
            **changes,
        },
    )


def snapshot(mode="policy", **changes):
    return {
        "schema_version": "1.0",
        "business_date": DAY,
        "extracted_at": "2026-09-07T20:00:00Z",
        "source": "eventbridge_occurrences",
        "source_record_count": 2,
        "evaluated_record_count": 1,
        "review_mode": mode,
        "review_provider": "gemini" if mode == "ai" else None,
        "review_model": "gemini-synthetic-model" if mode == "ai" else None,
        "policy_version": "synthetic-policy-1",
        "country_summary": [
            {"country": "Colombia", "received_count": 1, "accepted_count": 0},
            {"country": "Ecuador", "received_count": 1, "accepted_count": 1},
        ],
        "accepted_alerts": [{
            "schema_version": "1.0",
            "alert_id": "synthetic-alert",
            "merchant_code": "synthetic-mid",
            "merchant_name": "Synthetic merchant",
            "country": "Ecuador",
            "timestamp": "2026-09-07T20:00:00Z",
            "kipu_generated_at": "2026-09-07T12:00:00Z",
            "criticality": "Critica",
            "approval_rate": 0.1,
            "rolling_avg_approval_rate": 0.9,
            "total_transactions": 100,
            "declined_count": 90,
            "extra_structured_evidence": {"source": "synthetic-preserve-verbatim"},
        }],
        **changes,
    }


def response(body=None, *, envelope_changes=None, aws_changes=None, encoded=False):
    body = snapshot() if body is None else body
    body_text = json.dumps(body, ensure_ascii=False)
    if encoded:
        body_text = base64.b64encode(body_text.encode()).decode()
    envelope = {"statusCode": 200, "body": body_text, "isBase64Encoded": encoded}
    envelope.update(envelope_changes or {})
    stream = BytesIO(json.dumps(envelope, ensure_ascii=False).encode())
    reply = {"StatusCode": 200, "Payload": stream, **(aws_changes or {})}
    return reply, stream


def extractor(reply=None, *, config=None, settings_changes=None):
    reply = response()[0] if reply is None else reply
    client = SimpleNamespace(
        invoke=Mock(return_value=reply),
        get_function_configuration=Mock(return_value=config or {
            "Environment": {"Variables": {"AGENT_EXECUTION_KEY": SYNTHETIC_KEY}},
        }),
    )
    instance = guardian_daily.ManualExtractorClient(
        settings(**(settings_changes or {})), client=client,
    )
    return instance, client


@pytest.mark.parametrize("mode", ["policy", "ai"])
def test_invoke_preserves_requested_date_mode_and_raw_alert_metadata(mode):
    expected = snapshot(mode)
    original = deepcopy(expected)
    reply, stream = response(expected)
    instance, client = extractor(reply)
    result = instance.extract(DAY, mode)
    assert result == original
    assert expected == original
    client.get_function_configuration.assert_not_called()
    request = client.invoke.call_args.kwargs
    assert request.keys() == {"FunctionName", "InvocationType", "Payload"}
    assert request["FunctionName"] == "kipu-alert-reviewer-manual-extractor"
    assert request["InvocationType"] == "RequestResponse"
    assert isinstance(request["Payload"], bytes)
    event = json.loads(request["Payload"])
    assert event["headers"] == {"x-agent-key": SYNTHETIC_KEY}
    assert json.loads(event["body"]) == {"date": DAY, "mode": mode}
    assert stream.closed


def test_missing_explicit_key_uses_existing_extractor_key_only_in_memory():
    instance, client = extractor(settings_changes={"agent_execution_key": ""})
    instance.extract(DAY, "policy")
    client.get_function_configuration.assert_called_once_with(
        FunctionName="kipu-alert-reviewer-manual-extractor",
    )
    event = json.loads(client.invoke.call_args.kwargs["Payload"])
    assert event["headers"]["x-agent-key"] == SYNTHETIC_KEY
    assert SYNTHETIC_KEY not in repr(instance)


@pytest.mark.parametrize("config", [
    {"Environment": {}},
    {"Environment": {"Variables": {}}},
    {"Environment": {"Variables": {"AGENT_EXECUTION_KEY": ""}}},
])
def test_unconfigured_extractor_does_not_invoke_or_use_another_role(config):
    instance, client = extractor(config=config, settings_changes={"agent_execution_key": ""})
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == "AGENT_CONFIGURATION_FAILED"
    client.invoke.assert_not_called()
    guardian_daily.OccurrenceReader.assert_not_called()


def test_default_transport_uses_only_lambda_with_existing_profile(monkeypatch):
    reply, _ = response()
    lambda_client = SimpleNamespace(invoke=Mock(return_value=reply))
    session = SimpleNamespace(client=Mock(return_value=lambda_client))
    sessions = Mock(return_value=session)
    monkeypatch.setattr(guardian_daily.boto3, "Session", sessions)
    result = GuardianDailyReview(settings()).run(DAY, "policy")
    assert result == snapshot()
    sessions.assert_called_once_with(
        profile_name="ia-dev-payments-intelligence", region_name="us-east-1",
    )
    session.client.assert_called_once()
    assert session.client.call_args.args == ("lambda",)
    guardian_daily.OccurrenceReader.assert_not_called()


@pytest.mark.parametrize("mode", ["policy", "ai"])
def test_daily_default_delegates_to_extractor_without_local_gemini(monkeypatch, mode):
    client = SimpleNamespace(extract=Mock(return_value=snapshot(mode)))
    factory = Mock(return_value=client)
    monkeypatch.setattr(guardian_daily, "ManualExtractorClient", factory)
    agent_settings = settings()
    assert GuardianDailyReview(agent_settings).run(DAY, mode) == snapshot(mode)
    factory.assert_called_once_with(agent_settings)
    client.extract.assert_called_once_with(DAY, mode)
    guardian_daily.GeminiAlertReviewer.assert_not_called()
    guardian_daily.OccurrenceReader.assert_not_called()


@pytest.mark.parametrize("day,mode,expected", [
    ("2026-09-08", "policy", "FUTURE_DATE"),
    ("2026-9-7", "policy", "INVALID_DATE"),
    ("2026-02-30", "policy", "INVALID_DATE"),
    (DAY, "gemini", "INVALID_REVIEW_MODE"),
])
def test_default_validates_before_constructing_extractor(monkeypatch, day, mode, expected):
    factory = Mock(side_effect=AssertionError("Invalid requests must not initialize extraction"))
    monkeypatch.setattr(guardian_daily, "ManualExtractorClient", factory)
    with pytest.raises(DailyReviewError) as error:
        GuardianDailyReview(settings()).run(day, mode)
    assert error.value.code == expected
    factory.assert_not_called()


def test_base64_lambda_body_is_decoded_and_preserves_snapshot():
    reply, stream = response(encoded=True)
    instance, _ = extractor(reply)
    assert instance.extract(DAY, "policy") == snapshot()
    assert stream.closed


@pytest.mark.parametrize("changes", [
    {"business_date": "2026-09-06"},
    {"review_mode": "ai"},
    {"schema_version": "2.0"},
    {"source": "synthetic_unknown_source"},
])
def test_snapshot_identity_mismatch_is_never_returned(changes):
    reply, stream = response(snapshot(**changes))
    instance, _ = extractor(reply)
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == "INVALID_AGENT_RESPONSE"
    assert stream.closed


@pytest.mark.parametrize("body", [[], "synthetic", 1, True])
def test_non_object_snapshot_is_rejected(body):
    reply, stream = response(body)
    instance, _ = extractor(reply)
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == "INVALID_AGENT_RESPONSE"
    assert stream.closed


@pytest.mark.parametrize("body", [
    '{"business_date":"2026-09-07","business_date":"2026-09-06"}',
    '{"source_record_count":NaN}',
    '{"source_record_count":Infinity}',
    "not-json",
])
def test_strict_body_json_rejects_ambiguous_or_nonfinite_data(body):
    reply, stream = response(envelope_changes={"body": body})
    instance, _ = extractor(reply)
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == "INVALID_AGENT_RESPONSE"
    assert stream.closed


@pytest.mark.parametrize("payload,expected", [
    (b'not-json', "INVALID_AGENT_RESPONSE"),
    (b'{"statusCode":200,"statusCode":500,"body":"{}"}', "INVALID_AGENT_RESPONSE"),
    (b'{"statusCode":NaN,"body":"{}"}', "INVALID_AGENT_RESPONSE"),
    (b'[]', "INVALID_AGENT_RESPONSE"),
    (b'\xff', "INVALID_AGENT_RESPONSE"),
    pytest.param(b'x' * 2_000_001, "AGENT_RESPONSE_TOO_LARGE", id="oversized"),
])
def test_invalid_or_oversized_transport_body_is_closed_and_rejected(payload, expected):
    stream = BytesIO(payload)
    instance, _ = extractor({"StatusCode": 200, "Payload": stream})
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == expected
    assert stream.closed


def test_reads_response_with_explicit_limit_instead_of_unbounded_streaming():
    reply, original = response()
    stream = SimpleNamespace(read=Mock(return_value=original.getvalue()), close=Mock())
    reply["Payload"] = stream
    instance, _ = extractor(reply)
    assert instance.extract(DAY, "policy") == snapshot()
    stream.read.assert_called_once_with(2_000_001)
    stream.close.assert_called_once_with()


def test_stream_read_error_closes_response_and_redacts_details():
    stream = SimpleNamespace(read=Mock(side_effect=OSError(SYNTHETIC_KEY)), close=Mock())
    instance, _ = extractor({"StatusCode": 200, "Payload": stream})
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == "EXTRACTION_FAILED"
    assert SYNTHETIC_KEY not in str(error.value)
    stream.close.assert_called_once_with()


@pytest.mark.parametrize("envelope_changes", [
    {"isBase64Encoded": "false"},
    {"isBase64Encoded": True, "body": "%%%invalid-base64%%%"},
    {"statusCode": "200"},
    {"statusCode": True},
    {"body": {}},
])
def test_invalid_envelope_fields_are_rejected(envelope_changes):
    reply, stream = response(envelope_changes=envelope_changes)
    instance, _ = extractor(reply)
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == "INVALID_AGENT_RESPONSE"
    assert stream.closed


def test_unsuccessful_aws_status_never_returns_snapshot():
    reply, stream = response(aws_changes={"StatusCode": 202})
    instance, _ = extractor(reply)
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == "EXTRACTION_FAILED"
    assert stream.closed


@pytest.mark.parametrize("code", ["SNAPSHOT_NOT_FOUND", "AI_NOT_CONFIGURED", "AI_REVIEW_FAILED"])
def test_known_extractor_failure_remains_explicit_without_changing_mode(code):
    reply, stream = response(
        {"error": code, "message": SYNTHETIC_KEY}, envelope_changes={"statusCode": 500},
    )
    instance, client = extractor(reply)
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "ai")
    assert error.value.code == code
    assert SYNTHETIC_KEY not in str(error.value)
    assert stream.closed
    client.invoke.assert_called_once()


def test_s3_snapshot_from_existing_extractor_is_not_reinterpreted():
    expected = snapshot(source="s3_snapshot")
    reply, stream = response(expected)
    instance, _ = extractor(reply)
    assert instance.extract(DAY, "policy") == expected
    assert stream.closed


@pytest.mark.parametrize("phase", ["invoke", "get_function_configuration"])
@pytest.mark.parametrize("aws_code,expected", [
    ("AccessDeniedException", "ALERT_SOURCE_ACCESS_DENIED"),
    ("ExpiredTokenException", "SSO_EXPIRED"),
    ("InternalServerError", "EXTRACTION_FAILED"),
])
def test_aws_failure_is_redacted_without_falling_back(phase, aws_code, expected, caplog):
    instance, client = extractor(settings_changes={
        "agent_execution_key": "" if phase == "get_function_configuration" else SYNTHETIC_KEY,
    })
    failure = ClientError({"Error": {"Code": aws_code, "Message": SYNTHETIC_KEY}}, phase)
    getattr(client, phase).side_effect = failure
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == expected
    assert SYNTHETIC_KEY not in str(error.value)
    assert SYNTHETIC_KEY not in caplog.text
    assert error.value.__suppress_context__
    guardian_daily.OccurrenceReader.assert_not_called()


@pytest.mark.parametrize("failure,expected", [
    (UnauthorizedSSOTokenError(), "SSO_EXPIRED"),
    (NoCredentialsError(), "EXTRACTION_FAILED"),
])
def test_missing_or_expired_session_has_no_archive_role_fallback(failure, expected):
    instance, client = extractor()
    client.invoke.side_effect = failure
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == expected
    guardian_daily.OccurrenceReader.assert_not_called()


@pytest.mark.parametrize("status,body,expected", [
    (401, {"error": "UNAUTHORIZED", "message": SYNTHETIC_KEY}, "EXTRACTOR_AUTH_FAILED"),
    (500, {"error": "UNKNOWN", "message": SYNTHETIC_KEY}, "EXTRACTION_FAILED"),
])
def test_lambda_errors_expose_only_safe_codes(status, body, expected, caplog):
    reply, stream = response(body, envelope_changes={"statusCode": status})
    instance, _ = extractor(reply)
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == expected
    assert SYNTHETIC_KEY not in str(error.value)
    assert SYNTHETIC_KEY not in caplog.text
    assert stream.closed


def test_function_error_does_not_return_or_log_runtime_details(caplog):
    reply, stream = response(
        {"errorMessage": SYNTHETIC_KEY}, aws_changes={"FunctionError": "Unhandled"},
    )
    instance, _ = extractor(reply)
    with pytest.raises(DailyReviewError) as error:
        instance.extract(DAY, "policy")
    assert error.value.code == "EXTRACTION_FAILED"
    assert SYNTHETIC_KEY not in str(error.value)
    assert SYNTHETIC_KEY not in caplog.text
    assert stream.closed
