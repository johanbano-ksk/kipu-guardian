import hashlib
import json
from copy import deepcopy
from datetime import date
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from alert_reviewer import guardian_view as view
from alert_reviewer.datalake_history import HistoryError, HistoryQuery, summarize_rows
from alert_reviewer.gemini_review import GeminiReviewError
from alert_reviewer.guardian import SavedHistoryReader

QUERY = HistoryQuery("synthetic-mid", date(2026, 8, 1), date(2026, 8, 7))


def report():
    result = summarize_rows(
        [
            {
                "business_date": "2026-08-01",
                "total_transactions": "100",
                "approved_transactions": "100",
                "declined_transactions": "0",
                "other_transactions": "0",
            }
        ],
        QUERY,
        {
            "catalog": "s3tablescatalog/datalake-prod",
            "database": "odl",
            "table": "card_transaction",
            "query_execution_id": "synthetic-query",
            "retrieved_at": "2026-08-08T10:00:00+00:00",
            "data_scanned_bytes": 100,
        },
    )
    return {
        **result,
        "agent": "kipu-guardian",
        "analysis_status": "unavailable",
        "analysis_error": {"message": "sensitive-provider-body"},
        "analysis_model": "gemini-3.1-flash-lite",
        "conclusions": None,
    }


def seed(tmp_path):
    path = tmp_path / "guardian-real.json"
    path.write_text(json.dumps(report()), encoding="utf-8")
    return view.GuardianView(tmp_path), path


def test_list_and_load_use_saved_guardian_results_without_providers(tmp_path, monkeypatch):
    dashboard, _ = seed(tmp_path)
    configured = Mock(side_effect=AssertionError("No external calls for read"))
    monkeypatch.setattr(view.GuardianAgent, "configured", configured)
    result = dashboard.handle({"action": "list"})
    assert len(result["reports"]) == 1
    assert result["report"]["metrics"]["total_transactions"] == 100
    assert result["report"]["analysis_status"] == "unavailable"
    assert result["report"]["conclusions"] is None
    assert "sensitive-provider-body" not in json.dumps(result)
    assert result["report"]["analysis_created_at"] is None
    loaded = dashboard.handle({"action": "load", "report_id": result["selected_id"]})
    assert loaded["report"] == result["report"]
    configured.assert_not_called()


def test_ticket_history_cannot_be_listed_or_reanalyzed_as_current(tmp_path, monkeypatch):
    saved = report()
    saved["schema_version"] = "1.0"
    path = tmp_path / "guardian-ticket-history.json"
    path.write_text(json.dumps(saved), encoding="utf-8")
    original = path.read_bytes()
    configured = Mock(side_effect=AssertionError("No provider for old history"))
    monkeypatch.setattr(view.GuardianAgent, "configured", configured)
    dashboard = view.GuardianView(tmp_path)
    assert dashboard.handle({"action": "list"})["reports"] == []
    identifier = hashlib.sha256(path.name.encode()).hexdigest()[:24]
    with pytest.raises(ValueError, match="Report not found"):
        dashboard.handle({"action": "reanalyze", "report_id": identifier})
    assert path.read_bytes() == original
    configured.assert_not_called()


@pytest.mark.parametrize("code", ["EVIDENCE_INVALID", "INVALID_HISTORY_SOURCE", "SSO_EXPIRED"])
def test_evidence_errors_do_not_incorrectly_request_aws_login(monkeypatch, capsys, code):
    monkeypatch.setattr(view.sys, "stdin", SimpleNamespace(buffer=BytesIO(b'{"action":"list"}')))
    monkeypatch.setattr(
        view.GuardianView, "handle", Mock(side_effect=HistoryError(code, "PRIVATE_PROVIDER_BODY"))
    )
    view.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"] == code
    assert "PRIVATE_PROVIDER_BODY" not in payload["message"]
    assert ("sesión AWS" in payload["message"]) == (code == "SSO_EXPIRED")


def test_catalog_excludes_demo_alert_replay_legacy_and_corrupt_files(tmp_path):
    dashboard, _ = seed(tmp_path)
    (tmp_path / "guardian-policy-smoke.json").write_text(
        json.dumps(
            {
                "agent": "kipu-guardian",
                "reviews": [{"merchant_code": "demo"}],
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "history-old.json").write_text(json.dumps(report()), encoding="utf-8")
    (tmp_path / "guardian-corrupt.json").write_text('{"x":NaN}', encoding="utf-8")
    (tmp_path / "guardian-large.json").write_bytes(b" " * (view.MAX_BYTES + 1))
    assert len(dashboard.handle({"action": "list"})["reports"]) == 1


@pytest.mark.parametrize("identifier", ["../.env", "C:/secrets", "a" * 24, 123, None])
def test_report_ids_never_resolve_user_paths(tmp_path, identifier):
    dashboard, _ = seed(tmp_path)
    with pytest.raises(ValueError):
        dashboard.handle({"action": "load", "report_id": identifier})


@pytest.mark.parametrize(
    "changes",
    [
        {"merchant_code": "mid' OR 1=1"},
        {"merchant_code": 1},
        {"date_from": 0},
        {"date_from": True},
        {"date_to": "2099-01-01"},
        {"date_to": "2026-09-07"},
        {"date_from": "2026-08-01T00:00:00Z"},
    ],
)
def test_invalid_filters_fail_before_configuring_providers(tmp_path, monkeypatch, changes):
    configured = Mock(side_effect=AssertionError("No provider for invalid parameters"))
    monkeypatch.setattr(view.GuardianAgent, "configured", configured)
    with pytest.raises(ValueError):
        view.GuardianView(tmp_path).handle(
            {
                "action": "history",
                "merchant_code": "mid",
                "date_from": "2026-08-01",
                "date_to": "2026-08-07",
                **changes,
            }
        )
    configured.assert_not_called()


def test_reanalysis_uses_saved_reader_and_does_not_overwrite_evidence(tmp_path, monkeypatch):
    dashboard, path = seed(tmp_path)
    original = path.read_bytes()
    identifier = dashboard.handle({"action": "list"})["selected_id"]

    def configured(*, reader):
        assert isinstance(reader, SavedHistoryReader)
        assert reader.read(QUERY)["metrics"]["total_transactions"] == 100
        return Mock(investigate_merchant=Mock(return_value=deepcopy(report())))

    monkeypatch.setattr(view.GuardianAgent, "configured", configured)
    result = dashboard.handle({"action": "reanalyze", "report_id": identifier})
    assert result["report"]["analysis_created_at"]
    assert result["selected_id"] != identifier
    assert path.read_bytes() == original
    assert len(list(tmp_path.glob("guardian-*.json"))) == 2


def test_manual_history_calls_guardian_with_exact_requested_range(tmp_path, monkeypatch):
    agent = Mock(investigate_merchant=Mock(return_value=report()))
    monkeypatch.setattr(view.GuardianAgent, "configured", Mock(return_value=agent))
    result = view.GuardianView(tmp_path).handle(
        {
            "action": "history",
            "merchant_code": QUERY.merchant_code,
            "date_from": "2026-08-01",
            "date_to": "2026-08-07",
        }
    )
    agent.investigate_merchant.assert_called_once_with(QUERY)
    assert result["report"]["metrics"]["total_transactions"] == 100


@pytest.mark.parametrize(
    "body", [{"action": "sql", "query": "SELECT *"}, {"action": "list", "path": ".env"}, [], None]
)
def test_unknown_actions_and_fields_fail_closed(tmp_path, body):
    with pytest.raises(ValueError):
        view.GuardianView(tmp_path).handle(body)


def test_inconsistent_conclusion_state_is_not_displayed(tmp_path):
    bad = report()
    bad["conclusions"] = {"summary": "Must not show this as successful"}
    (tmp_path / "guardian-invalid.json").write_text(json.dumps(bad), encoding="utf-8")
    assert view.GuardianView(tmp_path).handle({"action": "list"})["report"] is None


def snapshot_alert(**changes):
    return {
        "schema_version": "1.0",
        "alert_id": "synthetic-alert",
        "timestamp": "2026-08-10T12:00:00Z",
        "kipu_generated_at": "2026-08-10T12:00:00Z",
        "merchant_code": "synthetic-mid",
        "merchant_name": "Synthetic merchant",
        "country": "Ecuador",
        "criticality": "Critica",
        "approval_rate": 0.1,
        "rolling_avg_approval_rate": None,
        "predicted_ar_q10": 0.6,
        "predicted_dc_q90": 50.5,
        "total_transactions": 100,
        "declined_count": 90,
        **changes,
    }


def alert_snapshot(**changes):
    return {
        "business_date": "2026-08-10",
        "extracted_at": "2026-08-10T13:00:00Z",
        "source_record_count": 3,
        "evaluated_record_count": 2,
        "review_mode": "policy",
        "review_model": None,
        "policy_version": "synthetic-policy",
        "country_summary": [
            {"country": "Ecuador", "received_count": 2, "accepted_count": 1},
            {"country": "Colombia", "received_count": 1, "accepted_count": 0},
        ],
        "accepted_alerts": [snapshot_alert()],
        **changes,
    }


def save_alert_snapshot(tmp_path, snapshot):
    path = tmp_path / "guardian-dashboard-snapshot.json"
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    return path


def test_conclude_alert_dispatches_exact_raw_payload_to_agent_service(tmp_path, monkeypatch):
    result = {"report": {"analysis_status": "completed"}, "reused": False}
    service = Mock(conclude=Mock(return_value=result))
    factory = Mock(return_value=service)
    monkeypatch.setattr("alert_reviewer.guardian_cases.GuardianCaseReview", factory)
    raw = snapshot_alert()

    response = view.GuardianView(tmp_path).handle({"action": "conclude_alert", "alert": raw})

    assert response is result
    factory.assert_called_once_with(tmp_path.resolve())
    service.conclude.assert_called_once_with(raw, metric_profile="authorizations")
    assert service.conclude.call_args.args[0] is raw
    assert raw["approval_rate"] == 0.1
    assert raw["rolling_avg_approval_rate"] is None


@pytest.mark.parametrize("profile", ["sales", "authorizations"])
@pytest.mark.parametrize("refresh", [False, True])
def test_inline_profile_and_revision_are_forwarded_exactly(tmp_path, monkeypatch, profile, refresh):
    result = {"report": {}, "reused": not refresh, "revisions": []}
    service = Mock(conclude=Mock(return_value=result))
    monkeypatch.setattr(
        "alert_reviewer.guardian_cases.GuardianCaseReview", Mock(return_value=service)
    )
    raw = snapshot_alert()
    body = {
        "action": "refresh_conclusion" if refresh else "conclude_alert",
        "alert": raw,
        "metric_profile": profile,
    }
    expected = {"metric_profile": profile}
    if refresh:
        body["expected_revision"] = "a" * 32
        expected.update(refresh=True, expected_revision="a" * 32)
    assert view.GuardianView(tmp_path).handle(body) is result
    service.conclude.assert_called_once_with(raw, **expected)


@pytest.mark.parametrize("profile", [None, True, 1, [], {}, "", "auto", "SALE"])
def test_inline_invalid_profiles_fail_before_service_configuration(tmp_path, monkeypatch, profile):
    factory = Mock(side_effect=AssertionError("Must not configure Guardian"))
    monkeypatch.setattr("alert_reviewer.guardian_cases.GuardianCaseReview", factory)
    with pytest.raises(ValueError):
        view.GuardianView(tmp_path).handle(
            {"action": "conclude_alert", "alert": {}, "metric_profile": profile}
        )
    factory.assert_not_called()


@pytest.mark.parametrize("revision", [None, True, [], "", "../private", "a" * 31, "A" * 32])
def test_inline_invalid_revision_fails_before_service_configuration(
    tmp_path, monkeypatch, revision
):
    factory = Mock(side_effect=AssertionError("Must not configure Guardian"))
    monkeypatch.setattr("alert_reviewer.guardian_cases.GuardianCaseReview", factory)
    with pytest.raises(ValueError):
        view.GuardianView(tmp_path).handle(
            {
                "action": "refresh_conclusion",
                "alert": {},
                "metric_profile": "sales",
                "expected_revision": revision,
            }
        )
    factory.assert_not_called()


@pytest.mark.parametrize(
    "body",
    [
        {"action": "conclude_alert"},
        {"action": "conclude_alert", "alert": {}, "lookback_days": 31},
        {"action": "conclude_alert", "alert": {}, "date_from": "2026-08-01"},
        {"action": "conclude_alert", "alert": {}, "merchant_code": "another-mid"},
        {"action": "conclude_alert", "alert": {}, "model": "another-model"},
        {"action": "conclude_alert", "alert": {}, "sql": "SELECT *"},
        {"action": "conclude_alert", "alert": {}, "expected_revision": "a" * 32},
        {"action": "refresh_conclusion", "alert": {}, "metric_profile": "sales"},
        {"action": "refresh_conclusion", "alert": {}, "expected_revision": "a" * 32},
        {"action": "alert_snapshot", "path": "../private.json"},
        {"action": "alert_snapshot", "report_id": "anything"},
        {"action": "alert_snapshot", "merchant_code": "synthetic-mid"},
    ],
)
def test_inline_actions_reject_extra_fields_before_dispatch(tmp_path, monkeypatch, body):
    factory = Mock(side_effect=AssertionError("Invalid action must not configure Guardian"))
    monkeypatch.setattr("alert_reviewer.guardian_cases.GuardianCaseReview", factory)
    with pytest.raises(ValueError):
        view.GuardianView(tmp_path).handle(body)
    factory.assert_not_called()


def test_alert_snapshot_returns_fixed_archive_without_normalizing_raw_payload(
    tmp_path, monkeypatch
):
    snapshot = alert_snapshot()
    path = save_alert_snapshot(tmp_path, snapshot)
    original = path.read_bytes()
    configured = Mock(side_effect=AssertionError("Reading archive must not query AWS or Gemini"))
    monkeypatch.setattr(view.GuardianAgent, "configured", configured)

    result = view.GuardianView(tmp_path).handle({"action": "alert_snapshot"})

    assert result == {"snapshot": snapshot, "source": "local_archive"}
    assert result["snapshot"]["accepted_alerts"][0]["predicted_dc_q90"] == 50.5
    assert result["snapshot"]["country_summary"][1]["country"] == "Colombia"
    assert path.read_bytes() == original
    configured.assert_not_called()


def test_missing_imported_snapshot_does_not_search_other_reports(tmp_path):
    for name in ("guardian-backup.json", "critical-alerts-latest.json"):
        (tmp_path / name).write_text(json.dumps(alert_snapshot()), encoding="utf-8")
    with pytest.raises(FileNotFoundError):
        view.GuardianView(tmp_path).handle({"action": "alert_snapshot"})


def test_snapshot_symlink_is_rejected_before_opening(tmp_path, monkeypatch):
    path = save_alert_snapshot(tmp_path, alert_snapshot())
    original_is_symlink = Path.is_symlink

    def is_symlink(candidate):
        return candidate == path or original_is_symlink(candidate)

    monkeypatch.setattr(Path, "is_symlink", is_symlink)
    with pytest.raises(ValueError, match="snapshot path"):
        view.GuardianView(tmp_path).handle({"action": "alert_snapshot"})


def test_oversized_imported_snapshot_is_rejected(tmp_path):
    (tmp_path / "guardian-dashboard-snapshot.json").write_bytes(b" " * (view.MAX_BYTES + 1))
    with pytest.raises(ValueError, match="too large"):
        view.GuardianView(tmp_path).handle({"action": "alert_snapshot"})


@pytest.mark.parametrize("raw", ["[]", "null", '{"x":NaN}', '{"x":1,"x":2}', "{broken"])
def test_malformed_snapshot_json_is_rejected(tmp_path, raw):
    (tmp_path / "guardian-dashboard-snapshot.json").write_text(raw, encoding="utf-8")
    with pytest.raises((ValueError, GeminiReviewError)):
        view.GuardianView(tmp_path).handle({"action": "alert_snapshot"})


@pytest.mark.parametrize(
    "changes",
    [
        {"business_date": "20260810"},
        {"business_date": "2026-02-30"},
        {"business_date": 20260810},
        {"extracted_at": "2026-08-10T13:00:00"},
        {"extracted_at": "2026-08-10"},
        {"extracted_at": None},
        {"review_mode": "unknown"},
        {"review_mode": {}},
        {"review_model": {}},
        {"policy_version": " "},
        {"policy_version": None},
        {"source_record_count": -1},
        {"source_record_count": True},
        {"source_record_count": 0},
        {"evaluated_record_count": False},
        {"evaluated_record_count": 0},
        {"evaluated_record_count": 4},
        {"country_summary": None},
        {"country_summary": []},
        {"accepted_alerts": None},
    ],
)
def test_invalid_snapshot_metadata_is_rejected(tmp_path, changes):
    save_alert_snapshot(tmp_path, alert_snapshot(**changes))
    with pytest.raises(ValueError):
        view.GuardianView(tmp_path).handle({"action": "alert_snapshot"})


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "2.0"},
        {"merchant_code": ""},
        {"alert_id": "fallback-01"},
        {"approval_rate": 10},
        {"approval_rate": 0.8},
        {"declined_count": 101},
        {"total_transactions": True},
        {"kipu_generated_at": "2026-08-10T13:00:00"},
        {"kipu_generated_at": "not-a-timestamp"},
        {"kipu_generated_at": 123},
    ],
)
def test_invalid_imported_alerts_are_rejected(tmp_path, changes):
    save_alert_snapshot(tmp_path, alert_snapshot(accepted_alerts=[snapshot_alert(**changes)]))
    with pytest.raises(ValueError):
        view.GuardianView(tmp_path).handle({"action": "alert_snapshot"})


def test_legacy_snapshot_without_generation_is_preserved_without_guessing(tmp_path):
    raw = snapshot_alert()
    raw.pop("kipu_generated_at")
    save_alert_snapshot(tmp_path, alert_snapshot(accepted_alerts=[raw]))
    result = view.GuardianView(tmp_path).handle({"action": "alert_snapshot"})
    assert "kipu_generated_at" not in result["snapshot"]["accepted_alerts"][0]


def test_duplicate_imported_alert_ids_are_rejected(tmp_path):
    save_alert_snapshot(
        tmp_path, alert_snapshot(accepted_alerts=[snapshot_alert(), snapshot_alert()])
    )
    with pytest.raises(ValueError, match="identity"):
        view.GuardianView(tmp_path).handle({"action": "alert_snapshot"})


def test_alert_snapshot_rejects_more_than_500_alerts(tmp_path):
    rows = [snapshot_alert(alert_id=f"alert-{index}") for index in range(501)]
    save_alert_snapshot(tmp_path, alert_snapshot(accepted_alerts=rows))
    with pytest.raises(ValueError, match="alerts"):
        view.GuardianView(tmp_path).handle({"action": "alert_snapshot"})


@pytest.mark.parametrize(
    "summary",
    [
        [None],
        [{"country": "", "received_count": 3, "accepted_count": 1}],
        [{"country": "Ecuador", "received_count": True, "accepted_count": 1}],
        [{"country": "Ecuador", "received_count": 3, "accepted_count": True}],
        [{"country": "Ecuador", "received_count": -1, "accepted_count": 1}],
        [{"country": "Ecuador", "received_count": 0, "accepted_count": 1}],
        [{"country": "Colombia", "received_count": 3, "accepted_count": 1}],
        [{"country": "Ecuador", "received_count": 2, "accepted_count": 1}],
        [
            {"country": "Ecuador", "received_count": 2, "accepted_count": 1},
            {"country": "Ecuador", "received_count": 1, "accepted_count": 1},
        ],
    ],
)
def test_imported_country_summary_must_match_actual_alerts_and_totals(tmp_path, summary):
    save_alert_snapshot(tmp_path, alert_snapshot(country_summary=summary))
    with pytest.raises(ValueError):
        view.GuardianView(tmp_path).handle({"action": "alert_snapshot"})


def test_empty_valid_snapshot_is_not_replaced_by_demo_alerts(tmp_path):
    empty = alert_snapshot(
        accepted_alerts=[], source_record_count=0, evaluated_record_count=0, country_summary=[]
    )
    save_alert_snapshot(tmp_path, empty)
    assert view.GuardianView(tmp_path).handle({"action": "alert_snapshot"}) == {
        "snapshot": empty,
        "source": "local_archive",
    }


def daily_snapshot(day="2026-09-07", **changes):
    timestamp = f"{day}T12:00:00Z"
    return alert_snapshot(
        **{
            "business_date": day,
            "extracted_at": f"{day}T15:00:00Z",
            "schema_version": "1.0",
            "source": "eventbridge_occurrences",
            "review_provider": None,
            "accepted_alerts": [snapshot_alert(timestamp=timestamp, kipu_generated_at=timestamp)],
            **changes,
        }
    )


def mock_daily_runner(monkeypatch, *, snapshot=None, error=None):
    runner = Mock(run=Mock(return_value=deepcopy(snapshot), side_effect=error))
    factory = Mock(return_value=runner)
    monkeypatch.setattr("alert_reviewer.guardian_daily.GuardianDailyReview", factory)
    return runner, factory


@pytest.mark.parametrize("mode", ["policy", "ai"])
def test_run_alerts_dispatches_exact_date_and_mode_and_persists_result(
    tmp_path,
    monkeypatch,
    mode,
):
    snapshot = daily_snapshot(review_mode=mode)
    runner, factory = mock_daily_runner(monkeypatch, snapshot=snapshot)
    dashboard = view.GuardianView(tmp_path)

    result = dashboard.handle({"action": "run_alerts", "date": "2026-09-07", "mode": mode})

    runner.run.assert_called_once_with("2026-09-07", mode)
    factory.assert_called_once_with()
    assert result == {"snapshot": snapshot, "source": "local_agent"}
    assert dashboard.handle({"action": "alert_snapshot"}) == {
        "snapshot": snapshot,
        "source": "local_archive",
    }
    archives = list((tmp_path / "guardian-dashboard-runs").glob("2026-09-07-*.json"))
    assert len(archives) == 1
    assert json.loads(archives[0].read_text(encoding="utf-8")) == snapshot
    assert not list(tmp_path.rglob("*.tmp"))


def test_successful_run_archives_previous_august_snapshot_without_overwriting_evidence(
    tmp_path,
    monkeypatch,
):
    previous = daily_snapshot("2026-08-31")
    latest = save_alert_snapshot(tmp_path, previous)
    evidence = tmp_path / "guardian-alerts" / "synthetic-conclusion.json"
    evidence.parent.mkdir()
    evidence.write_text('{"evidence":"do-not-change"}', encoding="utf-8")
    evidence_bytes = evidence.read_bytes()
    current = daily_snapshot()
    mock_daily_runner(monkeypatch, snapshot=current)

    result = view.GuardianView(tmp_path).handle(
        {
            "action": "run_alerts",
            "date": "2026-09-07",
            "mode": "policy",
        }
    )

    assert result["snapshot"] == current
    assert json.loads(latest.read_text(encoding="utf-8")) == current
    previous_archives = list((tmp_path / "guardian-dashboard-runs").glob("2026-08-31-*.json"))
    assert len(previous_archives) == 1
    assert json.loads(previous_archives[0].read_text(encoding="utf-8")) == previous
    assert evidence.read_bytes() == evidence_bytes
    assert len(list((tmp_path / "guardian-dashboard-runs").glob("*.json"))) == 2


@pytest.mark.parametrize(
    "body",
    [
        {"action": "run_alerts", "date": "2026-09-07"},
        {"action": "run_alerts", "mode": "policy"},
        {"action": "run_alerts", "date": "2026-09-07", "mode": "policy", "path": "../file"},
        {"action": "run_alerts", "date": "2026-09-07", "mode": "policy", "source": "s3"},
        {"action": "run_alerts", "date": "2026-09-07", "mode": "policy", "profile": "other"},
    ],
)
def test_run_alerts_rejects_missing_and_extra_fields_before_agent_initialization(
    tmp_path,
    monkeypatch,
    body,
):
    runner, factory = mock_daily_runner(monkeypatch, snapshot=daily_snapshot())
    with pytest.raises(ValueError):
        view.GuardianView(tmp_path).handle(body)
    factory.assert_not_called()
    runner.run.assert_not_called()
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "error_code",
    [
        "AI_REVIEW_FAILED",
        "SNAPSHOT_NOT_FOUND",
        "ALERT_SOURCE_ACCESS_DENIED",
        "SSO_EXPIRED",
    ],
)
def test_failed_execution_does_not_replace_previous_snapshot_or_create_run_archive(
    tmp_path,
    monkeypatch,
    error_code,
):
    from alert_reviewer.guardian_daily import DailyReviewError

    previous = save_alert_snapshot(tmp_path, daily_snapshot("2026-08-31"))
    original = previous.read_bytes()
    failure = DailyReviewError(error_code, "Synthetic safe failure")
    mock_daily_runner(monkeypatch, error=failure)

    with pytest.raises(DailyReviewError) as error:
        view.GuardianView(tmp_path).handle(
            {
                "action": "run_alerts",
                "date": "2026-09-07",
                "mode": "policy",
            }
        )

    assert error.value.code == error_code
    assert previous.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == [previous.name]


@pytest.mark.parametrize(
    "invalid",
    [
        None,
        [],
        {},
        daily_snapshot(evaluated_record_count=4),
        daily_snapshot(country_summary=[]),
    ],
)
def test_invalid_execution_snapshot_is_rejected_before_replacing_previous_data(
    tmp_path,
    monkeypatch,
    invalid,
):
    previous = save_alert_snapshot(tmp_path, daily_snapshot("2026-08-31"))
    original = previous.read_bytes()
    mock_daily_runner(monkeypatch, snapshot=invalid)

    with pytest.raises(ValueError):
        view.GuardianView(tmp_path).handle(
            {
                "action": "run_alerts",
                "date": "2026-09-07",
                "mode": "policy",
            }
        )

    assert previous.read_bytes() == original
    assert sorted(path.name for path in tmp_path.iterdir()) == [previous.name]


@pytest.mark.parametrize(
    "returned_snapshot",
    [
        daily_snapshot("2026-09-06"),
        daily_snapshot(review_mode="ai"),
    ],
)
def test_execution_for_different_returned_date_or_mode_cannot_replace_selected_request(
    tmp_path,
    monkeypatch,
    returned_snapshot,
):
    previous = save_alert_snapshot(tmp_path, daily_snapshot("2026-08-31"))
    original = previous.read_bytes()
    mock_daily_runner(monkeypatch, snapshot=returned_snapshot)

    with pytest.raises(ValueError):
        view.GuardianView(tmp_path).handle(
            {
                "action": "run_alerts",
                "date": "2026-09-07",
                "mode": "policy",
            }
        )

    assert previous.read_bytes() == original
    assert not (tmp_path / "guardian-dashboard-runs").exists()


def test_latest_snapshot_replace_failure_preserves_readable_previous_result(
    tmp_path,
    monkeypatch,
):
    from alert_reviewer import guardian_conclusions

    previous_snapshot = daily_snapshot("2026-08-31")
    latest = save_alert_snapshot(tmp_path, previous_snapshot)
    original = latest.read_bytes()
    snapshot = daily_snapshot()
    mock_daily_runner(monkeypatch, snapshot=snapshot)
    replace = guardian_conclusions.os.replace

    def fail_latest_replace(source, destination):
        if Path(destination) == latest:
            raise OSError("Synthetic atomic replacement failure")
        return replace(source, destination)

    monkeypatch.setattr(guardian_conclusions.os, "replace", fail_latest_replace)
    dashboard = view.GuardianView(tmp_path)
    with pytest.raises(OSError, match="atomic replacement"):
        dashboard.handle({"action": "run_alerts", "date": "2026-09-07", "mode": "policy"})

    assert latest.read_bytes() == original
    assert dashboard.handle({"action": "alert_snapshot"})["snapshot"] == previous_snapshot
    assert not list(tmp_path.rglob("*.tmp"))


def test_successful_execution_with_no_accepted_alerts_replaces_previous_nonempty_snapshot(
    tmp_path,
    monkeypatch,
):
    save_alert_snapshot(tmp_path, daily_snapshot("2026-08-31"))
    snapshot = daily_snapshot(
        accepted_alerts=[],
        country_summary=[
            {"country": "Ecuador", "received_count": 2, "accepted_count": 0},
            {"country": "Colombia", "received_count": 1, "accepted_count": 0},
        ],
    )
    mock_daily_runner(monkeypatch, snapshot=snapshot)
    dashboard = view.GuardianView(tmp_path)

    result = dashboard.handle(
        {
            "action": "run_alerts",
            "date": "2026-09-07",
            "mode": "policy",
        }
    )

    assert result["snapshot"]["accepted_alerts"] == []
    assert result["snapshot"]["source_record_count"] == 3
    assert dashboard.handle({"action": "alert_snapshot"})["snapshot"] == snapshot


def test_oversized_execution_result_does_not_change_previous_snapshot(tmp_path, monkeypatch):
    previous = save_alert_snapshot(tmp_path, daily_snapshot("2026-08-31"))
    original = previous.read_bytes()
    mock_daily_runner(monkeypatch, snapshot=daily_snapshot(padding="x" * view.MAX_BYTES))

    with pytest.raises(ValueError, match="too large"):
        view.GuardianView(tmp_path).handle(
            {
                "action": "run_alerts",
                "date": "2026-09-07",
                "mode": "policy",
            }
        )

    assert previous.read_bytes() == original
    assert not (tmp_path / "guardian-dashboard-runs").exists()
