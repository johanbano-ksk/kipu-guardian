import copy
import json
from datetime import UTC, date, datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from alert_reviewer import guardian_cli
from alert_reviewer.alert_filter import FilterPolicy
from alert_reviewer.datalake_history import TIMEZONE, HistoryError, HistoryQuery, summarize_rows
from alert_reviewer.guardian import SavedHistoryReader

MID = "20000000101108097000"


def test_calendar_underflow_is_reported_without_traceback(tmp_path, capsys):
    assert (
        guardian_cli.main(
            [
                "history",
                MID,
                "--end",
                "0001-01-01",
                "--days",
                "7",
                "--output",
                str(tmp_path / "underflow.json"),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["code"] == "INVALID_REQUEST_OR_SOURCE"
    assert not (tmp_path / "underflow.json").exists()


def _alert(identifier="alert-one"):
    return {
        "alert_id": identifier,
        "schema_version": "1.0",
        "merchant_code": MID,
        "merchant_name": "Comercio de prueba",
        "country": "Ecuador",
        "timestamp": "2020-01-09T12:00:00Z",
        "criticality": "Critica",
        "approval_rate": 0.1,
        "total_transactions": 100,
        "declined_count": 90,
    }


def _report():
    query = HistoryQuery(MID, date(2020, 1, 2), date(2020, 1, 8))
    return summarize_rows(
        [
            {
                "business_date": "2020-01-03",
                "total_transactions": "201",
                "approved_transactions": "201",
                "declined_transactions": "0",
                "other_transactions": "0",
            }
        ],
        query,
        {"query_execution_id": "synthetic-query-id"},
    )


def _write_input(tmp_path, payload, name="source.json"):
    path = tmp_path / name
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _configured_fake(monkeypatch, *, review_result=None, history_status="completed"):
    captured = {}

    def configured(**kwargs):
        reader = kwargs.get("reader")
        captured["reader"] = reader

        def review_alerts(alerts, **options):
            captured["alerts"] = copy.deepcopy(alerts)
            captured["options"] = options
            return copy.deepcopy(
                review_result
                if review_result is not None
                else {
                    "agent": "kipu-guardian",
                    "accepted_alerts": alerts,
                    "reviews": [{"analysis_status": "not_requested"} for _ in alerts],
                    "history_query_count": 0,
                }
            )

        def investigate_merchant(query):
            captured["query"] = query
            report = reader.read(query) if reader is not None else _report()
            return {
                "agent": "kipu-guardian",
                **report,
                "analysis_status": history_status,
                "conclusions": None,
                "analysis_error": (
                    {"code": "AI_QUOTA_EXCEEDED", "message": "Análisis no disponible."}
                    if history_status == "unavailable"
                    else None
                ),
            }

        return SimpleNamespace(
            review_alerts=review_alerts, investigate_merchant=investigate_merchant
        )

    class FakeAgent:
        def __init__(self, _policy, *, reader):
            self.__dict__.update(configured(reader=reader).__dict__)
            captured["policy_only_constructor"] = True

        @staticmethod
        def configured(**kwargs):
            captured["configured_called"] = True
            return configured(**kwargs)

    monkeypatch.setattr(guardian_cli, "GuardianAgent", FakeAgent)
    monkeypatch.setattr(
        guardian_cli, "get_settings", lambda: SimpleNamespace(filter_policy_path=None)
    )
    monkeypatch.setattr(guardian_cli.FilterPolicy, "load", lambda _path: FilterPolicy())
    monkeypatch.setattr(
        "alert_reviewer.datalake_history.boto3.Session",
        lambda *_args, **_kwargs: pytest.fail("Unexpected AWS access"),
    )
    monkeypatch.setattr(
        "alert_reviewer.gemini_review.urlopen",
        lambda *_args, **_kwargs: pytest.fail("Unexpected Gemini access"),
    )
    return captured


@pytest.mark.parametrize("wrapper", ["list", "alerts", "eventbridge", "sqs"])
def test_review_source_batch_policy_only_passes_flags_and_writes_json(
    monkeypatch, tmp_path, capsys, wrapper
):
    alerts = [_alert("one"), _alert("two")]
    payload = {
        "list": alerts,
        "alerts": {"alerts": alerts},
        "eventbridge": {"detail": {"alerts": alerts}},
        "sqs": {"Records": [{"body": json.dumps({"alerts": alerts})}]},
    }[wrapper]
    source = _write_input(tmp_path, payload)
    before = source.read_bytes()
    output = tmp_path / "nested" / "guardian-review.json"
    captured = _configured_fake(monkeypatch)
    result = guardian_cli.main(
        ["review", "--source", str(source), "--policy-only", "--output", str(output)]
    )
    assert result == 0
    assert captured["alerts"] == alerts
    assert captured["policy_only_constructor"] is True
    assert "configured_called" not in captured
    assert captured["options"] == {
        "with_history": False,
        "lookback_days": 7,
        "max_history_queries": 5,
    }
    assert source.read_bytes() == before
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["accepted_alerts"] == alerts
    assert written["history_query_count"] == 0
    assert output.read_text(encoding="utf-8").endswith("\n")
    assert "Comercio de prueba" in output.read_text(encoding="utf-8")
    assert json.loads(capsys.readouterr().out) == {
        "agent": "kipu-guardian",
        "command": "review",
        "status": "completed",
        "output": str(output),
    }


def test_review_history_passes_explicit_bounds(monkeypatch, tmp_path):
    source = _write_input(tmp_path, _alert())
    captured = _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            [
                "review",
                "--source",
                str(source),
                "--history-days",
                "14",
                "--max-history-queries",
                "2",
                "--output",
                str(tmp_path / "output.json"),
            ]
        )
        == 0
    )
    assert captured["options"] == {
        "with_history": True,
        "lookback_days": 14,
        "max_history_queries": 2,
    }


@pytest.mark.parametrize("payload", [[], {"alerts": []}])
def test_empty_alert_batch_is_an_explicit_success(monkeypatch, tmp_path, payload):
    source = _write_input(tmp_path, payload)
    captured = _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            [
                "review",
                "--source",
                str(source),
                "--policy-only",
                "--output",
                str(tmp_path / "output.json"),
            ]
        )
        == 0
    )
    assert captured["alerts"] == []


def test_history_defaults_to_last_seven_complete_days_in_ecuador(monkeypatch, tmp_path):
    captured = _configured_fake(monkeypatch)

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            assert tz == ZoneInfo(TIMEZONE)
            # UTC January 10 is still January 9 in Ecuador.
            return datetime(2020, 1, 10, 2, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(guardian_cli, "datetime", FrozenDatetime)
    assert guardian_cli.main(["history", MID, "--output", str(tmp_path / "history.json")]) == 0
    assert captured["query"] == HistoryQuery(MID, date(2020, 1, 2), date(2020, 1, 8))


def test_history_explicit_end_is_inclusive(monkeypatch, tmp_path):
    captured = _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            [
                "history",
                MID,
                "--days",
                "3",
                "--end",
                "2020-01-08",
                "--output",
                str(tmp_path / "history.json"),
            ]
        )
        == 0
    )
    assert captured["query"] == HistoryQuery(MID, date(2020, 1, 6), date(2020, 1, 8))


def test_saved_history_matches_mid_range_and_never_uses_aws(monkeypatch, tmp_path, capsys):
    report = _report()
    source = _write_input(tmp_path, report)
    output = tmp_path / "history.json"
    captured = _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            [
                "history",
                MID,
                "--end",
                "2020-01-08",
                "--source",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert isinstance(captured["reader"], SavedHistoryReader)
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["metrics"] == report["metrics"]
    assert written["daily"] == report["daily"]
    assert written["query"] == report["query"]
    assert json.loads(capsys.readouterr().out)["status"] == "completed"


@pytest.mark.parametrize(
    ("mid", "end", "days"),
    [("another-mid", "2020-01-08", "7"), (MID, "2020-01-09", "7"), (MID, "2020-01-08", "6")],
)
def test_saved_history_mismatch_fails_without_aws_fallback(
    monkeypatch, tmp_path, capsys, mid, end, days
):
    source = _write_input(tmp_path, _report())
    output = tmp_path / "output.json"
    _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            [
                "history",
                mid,
                "--end",
                end,
                "--days",
                days,
                "--source",
                str(source),
                "--output",
                str(output),
            ]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["code"] == "INVALID_HISTORY_SOURCE"
    assert not output.exists()


def test_review_can_pass_saved_history_to_agent(monkeypatch, tmp_path):
    source = _write_input(tmp_path, _alert())
    history_source = _write_input(tmp_path, _report(), "history-source.json")
    captured = _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            [
                "review",
                "--source",
                str(source),
                "--history-source",
                str(history_source),
                "--output",
                str(tmp_path / "output.json"),
            ]
        )
        == 0
    )
    assert isinstance(captured["reader"], SavedHistoryReader)
    assert (
        captured["reader"].read(HistoryQuery(MID, date(2020, 1, 2), date(2020, 1, 8)))["metrics"]
        == _report()["metrics"]
    )


@pytest.mark.parametrize("command", ["review", "history"])
@pytest.mark.parametrize(
    "raw",
    [b'{"secret":"KEY_NEVER_ECHO",', b'"KEY_NEVER_ECHO"' * 150_000, b"\xffKEY_NEVER_ECHO"],
    ids=["malformed-json", "oversize-json", "invalid-encoding"],
)
def test_bad_json_input_returns_safe_error_without_source_content(
    monkeypatch, tmp_path, capsys, command, raw
):
    source = tmp_path / "source.json"
    source.write_bytes(raw)
    output = tmp_path / "output.json"
    _configured_fake(monkeypatch)
    args = [command]
    if command == "history":
        args.append(MID)
    assert guardian_cli.main([*args, "--source", str(source), "--output", str(output)]) == 1
    captured = capsys.readouterr()
    assert "KEY_NEVER_ECHO" not in captured.out + captured.err
    assert json.loads(captured.out)["code"] == "INVALID_REQUEST_OR_SOURCE"
    assert not output.exists()


@pytest.mark.parametrize("payload", [None, 3, "SECRET_IGNORE_INSTRUCTIONS", {}, {"unrelated": []}])
def test_non_alert_source_is_not_silently_successful(monkeypatch, tmp_path, capsys, payload):
    source = _write_input(tmp_path, payload)
    _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            ["review", "--source", str(source), "--output", str(tmp_path / "output.json")]
        )
        == 1
    )
    captured = capsys.readouterr()
    assert "SECRET_IGNORE_INSTRUCTIONS" not in captured.out + captured.err
    assert json.loads(captured.out)["code"] == "INVALID_REQUEST_OR_SOURCE"


@pytest.mark.parametrize("days", ["0", "-1", "32"])
def test_history_rejects_out_of_range_days_before_investigation(
    monkeypatch, tmp_path, capsys, days
):
    captured = _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            ["history", MID, "--days", days, "--output", str(tmp_path / "output.json")]
        )
        == 1
    )
    assert "query" not in captured
    assert json.loads(capsys.readouterr().out)["code"] == "INVALID_REQUEST_OR_SOURCE"


def test_unavailable_history_analysis_preserves_metrics_and_returns_partial(
    monkeypatch, tmp_path, capsys
):
    source = _write_input(tmp_path, _report())
    output = tmp_path / "output.json"
    _configured_fake(monkeypatch, history_status="unavailable")
    assert (
        guardian_cli.main(
            [
                "history",
                MID,
                "--source",
                str(source),
                "--end",
                "2020-01-08",
                "--output",
                str(output),
            ]
        )
        == 1
    )
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["metrics"]["total_transactions"] == 201
    assert written["metrics"]["approval_rate"] == 1.0
    assert written["conclusions"] is None
    assert written["analysis_error"]["code"] == "AI_QUOTA_EXCEEDED"
    assert json.loads(capsys.readouterr().out)["status"] == "partial"


@pytest.mark.parametrize(
    ("status", "exit_code"), [("unavailable", 1), ("skipped", 1), ("no_data", 0)]
)
def test_review_analysis_status_controls_partial_without_losing_policy_result(
    monkeypatch, tmp_path, capsys, status, exit_code
):
    source = _write_input(tmp_path, _alert())
    output = tmp_path / "output.json"
    result = {
        "accepted_alerts": [_alert()],
        "reviews": [
            {
                "analysis_status": status,
                "policy": {"accepted": True},
                "history": _report(),
                "conclusions": None,
            }
        ],
    }
    _configured_fake(monkeypatch, review_result=result)
    assert (
        guardian_cli.main(["review", "--source", str(source), "--output", str(output)]) == exit_code
    )
    assert json.loads(output.read_text(encoding="utf-8")) == result
    assert json.loads(capsys.readouterr().out)["status"] == (
        "partial" if exit_code else "completed"
    )


@pytest.mark.parametrize(
    "error",
    [
        ValueError("SECRET_GEMINI_KEY"),
        OSError("SECRET_FILESYSTEM_PATH"),
        HistoryError("SSO_EXPIRED", "SECRET_PROVIDER_BODY"),
    ],
)
def test_configuration_errors_do_not_echo_secret_messages(monkeypatch, tmp_path, capsys, error):
    def configured(**_kwargs):
        raise error

    monkeypatch.setattr(guardian_cli.GuardianAgent, "configured", configured)
    assert guardian_cli.main(["history", MID, "--output", str(tmp_path / "output.json")]) == 1
    captured = capsys.readouterr()
    assert "SECRET_" not in captured.out + captured.err
    assert json.loads(captured.out)["code"] == getattr(error, "code", "INVALID_REQUEST_OR_SOURCE")


def test_missing_source_returns_safe_failure(monkeypatch, tmp_path, capsys):
    _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            [
                "review",
                "--source",
                str(tmp_path / "SECRET_MISSING_FILE.json"),
                "--output",
                str(tmp_path / "output.json"),
            ]
        )
        == 1
    )
    assert "SECRET_MISSING_FILE" not in capsys.readouterr().out


def test_write_failure_returns_safe_error(monkeypatch, tmp_path, capsys):
    source = _write_input(tmp_path, _alert())
    not_a_directory = tmp_path / "SECRET_EXISTING_FILE"
    not_a_directory.write_text("preserve-me", encoding="utf-8")
    _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            [
                "review",
                "--source",
                str(source),
                "--output",
                str(not_a_directory / "output.json"),
            ]
        )
        == 1
    )
    assert not_a_directory.read_text(encoding="utf-8") == "preserve-me"
    assert "SECRET_EXISTING_FILE" not in capsys.readouterr().out


@pytest.mark.parametrize(
    "raw",
    [
        '{"alert_id":"one","approval_rate":NaN}',
        '{"alert_id":"one","approval_rate":Infinity}',
        '{"alert_id":"one","approval_rate":-Infinity}',
        '{"alert_id":"one","approval_rate":0.1,"approval_rate":0.9}',
        '{"alerts":[{"alert_id":"one","nested":{"a":1,"a":2}}]}',
    ],
)
def test_nonstandard_or_ambiguous_json_is_rejected(monkeypatch, tmp_path, capsys, raw):
    source = tmp_path / "source.json"
    source.write_text(raw, encoding="utf-8")
    _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            ["review", "--source", str(source), "--output", str(tmp_path / "output.json")]
        )
        == 1
    )
    assert json.loads(capsys.readouterr().out)["code"] == "INVALID_REQUEST_OR_SOURCE"


@pytest.mark.parametrize("command", ["review", "history"])
def test_output_cannot_overwrite_source_evidence(monkeypatch, tmp_path, capsys, command):
    source = _write_input(tmp_path, _alert() if command == "review" else _report())
    original = source.read_bytes()
    _configured_fake(monkeypatch)
    args = [command] + ([MID] if command == "history" else [])
    assert guardian_cli.main([*args, "--source", str(source), "--output", str(source)]) == 1
    assert source.read_bytes() == original
    assert json.loads(capsys.readouterr().out)["code"] == "INVALID_REQUEST_OR_SOURCE"


def test_output_cannot_overwrite_saved_review_history(monkeypatch, tmp_path, capsys):
    source = _write_input(tmp_path, _alert())
    history_source = _write_input(tmp_path, _report(), "history.json")
    original = history_source.read_bytes()
    _configured_fake(monkeypatch)
    assert (
        guardian_cli.main(
            [
                "review",
                "--source",
                str(source),
                "--history-source",
                str(history_source),
                "--output",
                str(history_source),
            ]
        )
        == 1
    )
    assert history_source.read_bytes() == original
    assert json.loads(capsys.readouterr().out)["code"] == "INVALID_REQUEST_OR_SOURCE"


def test_policy_only_does_not_configure_gemini_or_aws(monkeypatch, tmp_path):
    source = _write_input(tmp_path, _alert())
    output = tmp_path / "policy-only.json"
    monkeypatch.setenv("GEMINI_MODEL", "INVALID_SECRET_MODEL")
    monkeypatch.setenv("GEMINI_API_KEY", "INVALID\nSECRET_KEY")
    monkeypatch.setenv("HISTORY_AWS_PROFILE", "profile-does-not-exist")
    monkeypatch.setattr(
        guardian_cli.GuardianAgent,
        "configured",
        lambda **_kwargs: pytest.fail("Should not configure providers in policy-only mode"),
    )
    monkeypatch.setattr(
        guardian_cli, "get_settings", lambda: SimpleNamespace(filter_policy_path=None)
    )
    monkeypatch.setattr(guardian_cli.FilterPolicy, "load", lambda _path: FilterPolicy())
    monkeypatch.setattr(
        "alert_reviewer.datalake_history.boto3.Session",
        lambda *_args, **_kwargs: pytest.fail("Unexpected AWS access"),
    )
    monkeypatch.setattr(
        "alert_reviewer.gemini_review.urlopen",
        lambda *_args, **_kwargs: pytest.fail("Unexpected Gemini access"),
    )
    assert (
        guardian_cli.main(
            ["review", "--source", str(source), "--policy-only", "--output", str(output)]
        )
        == 0
    )
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["reviews"][0]["policy"]["accepted"] is True
    assert report["reviews"][0]["history_status"] == "not_requested"
    assert report["reviews"][0]["analysis_status"] == "not_requested"
    assert report["history_query_count"] == 0
