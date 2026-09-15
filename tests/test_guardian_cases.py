"""Harness G1 integration: profiles, assurance, refresh and historical invariants."""

import json
from copy import deepcopy
from unittest.mock import Mock

import pytest

from alert_reviewer.alert_filter import FilterPolicy
from alert_reviewer.datalake_history import HistoryError
from alert_reviewer.gemini_review import GeminiReviewError
from alert_reviewer.guardian_cases import GuardianCaseReview
from alert_reviewer.guardian_history import (
    DIAGNOSTIC_COUNTERS,
    OPERATION_COUNTERS,
    summarize_guardian_rows,
)


def alert(**changes):
    return {
        "schema_version": "1.0",
        "alert_id": "synthetic-case",
        "merchant_code": "synthetic-mid",
        "merchant_name": "Synthetic merchant",
        "country": "Ecuador",
        "timestamp": "2026-09-09T12:00:00Z",
        "kipu_generated_at": "2026-09-09T12:00:00Z",
        "criticality": "Critica",
        "approval_rate": 0.1,
        "total_transactions": 100,
        "declined_count": 90,
        "rolling_avg_approval_rate": 0.9,
        **changes,
    }


def conclusion(verdict="requires_review"):
    return {
        "verdict": verdict,
        "verdict_evidence_ids": ["alert_0", "day_0"],
        "summary": "La alerta tiene 10% de aprobación; el histórico no verifica su ventana.",
        "findings": [
            {
                "observation": "Las ventanas requieren validación.",
                "evidence_ids": ["alert_0", "day_0"],
            }
        ],
        "limitations": ["Kipu no publica una ventana observada verificable."],
        "next_steps": ["Solicitar la ventana y el criterio original de Kipu."],
    }


class Sources:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.empty = False

    def factory(self, profile):
        def read(query):
            self.calls.append((profile, query))
            if self.failure:
                raise self.failure
            row = dict.fromkeys((*DIAGNOSTIC_COUNTERS, *OPERATION_COUNTERS), "0")
            row.update(
                business_date=query.date_from.isoformat(),
                last_ingested_at="",
                source_cdc_rows="55",
                current_transactions="55",
                preauthorizations_approved="10",
                preauthorizations_declined="40",
                captures_approved="5",
            )
            return summarize_guardian_rows(
                [] if self.empty else [row],
                query,
                {
                    "query_execution_id": f"synthetic-{len(self.calls)}",
                    "retrieved_at": "2026-09-09T15:00:00+00:00",
                },
                metric_profile=profile,
            )

        return Mock(read=read)


@pytest.fixture(autouse=True)
def no_external_calls(monkeypatch):
    monkeypatch.setattr(
        "alert_reviewer.datalake_history.boto3.Session", Mock(side_effect=AssertionError("No AWS"))
    )
    monkeypatch.setattr(
        "alert_reviewer.gemini_review.urlopen", Mock(side_effect=AssertionError("No Gemini"))
    )


def setup_service(tmp_path, analyst=True):
    sources = Sources()
    provider = Mock(analyze=Mock(return_value=conclusion())) if analyst else None
    service = GuardianCaseReview(
        tmp_path,
        policy=FilterPolicy(),
        reader_factory=sources.factory,
        analyst=provider,
        model="gemini-3.1-flash-lite" if provider else None,
    )
    return service, sources, provider


def test_preauthorizations_are_analyzed_without_captures_and_with_abstention(tmp_path):
    service, sources, provider = setup_service(tmp_path)
    raw = alert()
    original = deepcopy(raw)
    result = service.conclude(raw)
    report = result["report"]
    assert raw == original
    assert report["history"]["metrics"]["total_transactions"] == 50
    assert report["history"]["metrics"]["approval_rate"] == 0.2
    assert report["history"]["diagnostics"]["excluded_type_transactions"] == 5
    assert report["comparison_context"]["allowed_verdicts"] == ["confirmed", "not_supported", "requires_review"]
    assert report["comparison_context"]["alert_window"] is None
    assert report["analysis_status"] == "completed"
    assert report["policy"]["accepted"] is True
    assert report["analysis_model"] and report["analysis_created_at"]
    assert "original_alert" not in report
    assert len(sources.calls) == 1
    provider.analyze.assert_called_once()


def test_sales_profile_reports_excluded_activity_without_attributing_ai(tmp_path):
    service, sources, provider = setup_service(tmp_path)
    result = service.conclude(alert(), metric_profile="sales")
    report = result["report"]
    assert report["analysis_status"] == "no_data"
    assert report["history"]["diagnostics"]["status"] == "outside_scope"
    assert report["history"]["diagnostics"]["totals"]["current_transactions"] == 55
    assert report["conclusions"] is None
    for field in ("analysis_model", "analysis_attempted_at", "analysis_created_at"):
        assert report[field] is None
    provider.analyze.assert_not_called()
    cached = service.conclude(alert(), metric_profile="sales")
    assert cached["reused"] and cached["report"] == report
    assert len(sources.calls) == 1


def test_profile_change_gets_different_case_and_does_not_relabel_old_history(tmp_path):
    service, sources, _ = setup_service(tmp_path)
    first = service.conclude(alert(), metric_profile="sales")["report"]
    second = service.conclude(alert(), metric_profile="authorizations")["report"]
    assert first["case_id"] != second["case_id"]
    assert first["analysis_status"] == "no_data" and second["analysis_status"] == "completed"
    assert len(sources.calls) == 2


def test_refresh_preserves_old_bytes_and_conflicting_retry_does_not_query(tmp_path):
    service, sources, provider = setup_service(tmp_path)
    first = service.conclude(alert())["report"]
    path = tmp_path / "guardian-evidence" / first["case_id"] / f"{first['revision_id']}.json"
    original = path.read_bytes()
    second = service.conclude(alert(), refresh=True, expected_revision=first["revision_id"])
    report = second["report"]
    assert not second["reused"]
    assert report["revision_id"] != first["revision_id"]
    assert report["previous_revision_id"] == first["revision_id"]
    assert report["evidence_digest"] != first["evidence_digest"]
    assert len(second["revisions"]) == 2
    assert path.read_bytes() == original
    with pytest.raises(HistoryError) as error:
        service.conclude(alert(), refresh=True, expected_revision=first["revision_id"])
    assert error.value.code == "REVISION_CONFLICT"
    assert len(sources.calls) == provider.analyze.call_count == 2
    loaded = service.conclude(alert())
    assert loaded["reused"] and loaded["report"] == report


def test_failed_refresh_keeps_previous_success_and_no_new_revision(tmp_path):
    service, sources, _ = setup_service(tmp_path)
    first = service.conclude(alert())["report"]
    sources.failure = HistoryError("SSO_EXPIRED", "Safe error")
    with pytest.raises(HistoryError, match="Safe error"):
        service.conclude(alert(), refresh=True, expected_revision=first["revision_id"])
    result = service.conclude(alert())
    assert result["report"] == first and len(result["revisions"]) == 1


def test_empty_evidence_can_be_explicitly_refreshed_after_late_data(tmp_path):
    service, sources, _ = setup_service(tmp_path)
    sources.empty = True
    first = service.conclude(alert())["report"]
    assert first["analysis_status"] == "no_data"
    assert first["history"]["diagnostics"]["status"] == "no_source_rows"
    sources.empty = False
    cached = service.conclude(alert())["report"]
    assert cached == first
    fresh = service.conclude(alert(), refresh=True, expected_revision=first["revision_id"])
    assert fresh["report"]["analysis_status"] == "completed"
    assert len(fresh["revisions"]) == len(sources.calls) == 2


def test_ai_retry_uses_same_history_and_revision(tmp_path):
    service, sources, provider = setup_service(tmp_path)
    provider.analyze.side_effect = GeminiReviewError("SECRET_PROVIDER_BODY")
    first = service.conclude(alert())["report"]
    assert first["analysis_status"] == "unavailable"
    assert first["analysis_created_at"] is None
    assert first["analysis_attempted_at"] is not None
    assert "SECRET_PROVIDER_BODY" not in json.dumps(first)
    provider.analyze.side_effect = None
    second = service.conclude(alert())["report"]
    assert second["analysis_status"] == "completed"
    for field in ("revision_id", "evidence_digest", "history"):
        assert second[field] == first[field]
    assert len(sources.calls) == 1


@pytest.mark.parametrize("verdict", ["confirmed", "not_supported"])
def test_service_rejects_definitive_dictamen_even_if_injected_provider_allows_it(tmp_path, verdict):
    service, _, provider = setup_service(tmp_path)
    provider.analyze.return_value = conclusion(verdict)
    result = service.conclude(alert())["report"]
    assert result["analysis_status"] == "completed"
    assert result["conclusions"] is not None and result["conclusions"]["verdict"] == verdict
    assert result["policy"]["accepted"] is True


def test_missing_ai_configuration_has_no_fake_execution_time_or_model(tmp_path):
    service, _, _ = setup_service(tmp_path, analyst=False)
    result = service.conclude(alert())["report"]
    assert result["analysis_status"] == "unavailable"
    assert result["analysis_model"] is None and result["analysis_attempted_at"] is None


@pytest.mark.parametrize(
    "kwargs",
    [
        {"metric_profile": "capture"},
        {"metric_profile": None},
        {"refresh": True},
        {"refresh": True, "expected_revision": "../secret"},
        {"refresh": "yes"},
        {"expected_revision": "a" * 32},
    ],
)
def test_invalid_requests_do_not_query(tmp_path, kwargs):
    service, sources, provider = setup_service(tmp_path)
    with pytest.raises(ValueError):
        service.conclude(alert(), **kwargs)
    assert not sources.calls
    provider.analyze.assert_not_called()


def test_legacy_files_untouched_and_not_reused(tmp_path):
    old = tmp_path / "guardian-alerts"
    old.mkdir()
    file = old / "old.json"
    file.write_text('{"protocol":"guardian-alert-card-7d-v2"}', encoding="utf-8")
    before = file.read_bytes()
    service, sources, _ = setup_service(tmp_path)
    assert not service.conclude(alert())["reused"]
    assert len(sources.calls) == 1 and file.read_bytes() == before


def _revision_path(tmp_path, report):
    return tmp_path / "guardian-evidence" / report["case_id"] / f"{report['revision_id']}.json"


def _change_revision(path, change):
    saved = json.loads(path.read_text(encoding="utf-8"))
    change(saved)
    path.write_text(json.dumps(saved, allow_nan=False), encoding="utf-8")


@pytest.mark.parametrize(
    "change",
    [
        lambda saved: saved.update(provider_raw_body="SENSITIVE_BODY_MUST_NOT_REACH_UI"),
        lambda saved: saved.pop("analysis_attempted_at"),
        lambda saved: saved.update(analysis_model="not a model"),
        lambda saved: saved.update(analysis_model=True),
        lambda saved: saved.update(analysis_model=["gemini-3.1-flash-lite"]),
        lambda saved: saved.update(analysis_created_at="not a timestamp"),
        lambda saved: saved.update(analysis_created_at="2026-09-09T12:00:00"),
        lambda saved: saved.update(analysis_created_at="2026-09-09T12:00:00-05:00"),
        lambda saved: saved.update(analysis_created_at="2099-09-09T12:00:00+00:00"),
        lambda saved: saved.update(analysis_created_at="2000-01-01T00:00:00+00:00"),
        lambda saved: saved.update(analysis_created_at=None),
        lambda saved: saved.update(analysis_attempted_at=None),
        lambda saved: saved.update(analysis_attempted_at="2000-01-01T00:00:00+00:00"),
        lambda saved: saved.update(analysis_error={"body": "SENSITIVE_BODY_MUST_NOT_REACH_UI"}),
        lambda saved: saved["policy"].update(accepted=1),
        lambda saved: saved["original_alert"].update(unknown_provenance="UNTRUSTED_TEXT"),
        lambda saved: saved.update(analysis_status="pending"),
    ],
)
@pytest.mark.parametrize("refresh", [False, True])
def test_corrupt_completed_provenance_fails_closed_before_reuse_or_refresh(
    tmp_path, change, refresh
):
    service, sources, provider = setup_service(tmp_path)
    report = service.conclude(alert())["report"]
    path = _revision_path(tmp_path, report)
    _change_revision(path, change)
    corrupted = path.read_bytes()
    kwargs = {"refresh": True, "expected_revision": report["revision_id"]} if refresh else {}
    with pytest.raises(HistoryError) as error:
        service.conclude(alert(), **kwargs)
    assert error.value.code == "EVIDENCE_INVALID"
    assert len(sources.calls) == provider.analyze.call_count == 1
    assert path.read_bytes() == corrupted


@pytest.mark.parametrize(
    "change",
    [
        lambda saved: saved.update(analysis_error={"code": "AI_UNAVAILABLE", "body": "SECRET"}),
        lambda saved: saved["analysis_error"].update(message="SECRET_PROVIDER_RESPONSE"),
        lambda saved: saved.update(analysis_attempted_at=None),
        lambda saved: saved.update(analysis_model=None),
        lambda saved: saved.update(analysis_created_at=saved["analysis_attempted_at"]),
        lambda saved: saved.update(conclusions=conclusion()),
    ],
)
def test_corrupt_failed_attempt_cannot_be_replayed_or_leak_provider_fields(tmp_path, change):
    service, sources, provider = setup_service(tmp_path)
    provider.analyze.side_effect = GeminiReviewError("PRIVATE_PROVIDER_RESPONSE")
    report = service.conclude(alert())["report"]
    path = _revision_path(tmp_path, report)
    _change_revision(path, change)
    with pytest.raises(HistoryError) as error:
        service.conclude(alert())
    assert error.value.code == "EVIDENCE_INVALID"
    assert len(sources.calls) == provider.analyze.call_count == 1


@pytest.mark.parametrize(
    "change",
    [
        lambda saved: saved.update(analysis_error={"code": "AI_NOT_CONFIGURED", "message": "x"}),
        lambda saved: saved.update(analysis_model="gemini-3.1-flash-lite"),
        lambda saved: saved.update(analysis_attempted_at=saved["revision_created_at"]),
        lambda saved: saved.update(analysis_created_at=saved["revision_created_at"]),
        lambda saved: saved.update(conclusions=conclusion()),
    ],
)
def test_empty_history_never_acquires_fake_ai_provenance_from_cache(tmp_path, change):
    service, sources, provider = setup_service(tmp_path)
    report = service.conclude(alert(), metric_profile="sales")["report"]
    _change_revision(_revision_path(tmp_path, report), change)
    with pytest.raises(HistoryError) as error:
        service.conclude(alert(), metric_profile="sales")
    assert error.value.code == "EVIDENCE_INVALID"
    assert len(sources.calls) == 1
    provider.analyze.assert_not_called()


@pytest.mark.parametrize(
    "change",
    [
        lambda saved: saved["history"]["metrics"].update(approved_transactions=999),
        lambda saved: saved.update(evidence_digest="f" * 64),
        lambda saved: saved["policy"].update(accepted=1),
        lambda saved: saved.update(analysis_created_at="not a timestamp"),
        lambda saved: saved.update(provider_raw_body="SECRET_FROM_PREVIOUS_REVISION"),
    ],
)
@pytest.mark.parametrize("refresh", [False, True])
def test_semantic_corruption_in_previous_revision_blocks_current_case_and_refresh(
    tmp_path, change, refresh
):
    service, sources, provider = setup_service(tmp_path)
    first = service.conclude(alert())["report"]
    current = service.conclude(alert(), refresh=True, expected_revision=first["revision_id"])[
        "report"
    ]
    _change_revision(_revision_path(tmp_path, first), change)
    current_bytes = _revision_path(tmp_path, current).read_bytes()
    kwargs = {"refresh": True, "expected_revision": current["revision_id"]} if refresh else {}
    with pytest.raises(HistoryError) as error:
        service.conclude(alert(), **kwargs)
    assert error.value.code == "EVIDENCE_INVALID"
    assert len(sources.calls) == provider.analyze.call_count == 2
    assert _revision_path(tmp_path, current).read_bytes() == current_bytes


def test_provider_crash_leaves_valid_pending_evidence_for_a_same_revision_retry(tmp_path):
    service, sources, provider = setup_service(tmp_path)
    provider.analyze.side_effect = RuntimeError("Process interrupted")
    with pytest.raises(RuntimeError, match="Process interrupted"):
        service.conclude(alert())
    case_id = service._context(alert(), "authorizations")[3]
    with service.store.locked(case_id) as case:
        pending = case.load_latest()
    assert pending["analysis_status"] == "pending"
    assert all(
        pending[field] is None
        for field in (
            "analysis_model",
            "analysis_created_at",
            "analysis_attempted_at",
            "analysis_error",
        )
    )
    provider.analyze.side_effect = None
    result = service.conclude(alert())["report"]
    assert result["revision_id"] == pending["revision_id"]
    assert result["analysis_status"] == "completed"
    assert len(sources.calls) == 1


def test_disabling_ai_after_a_failed_attempt_does_not_attribute_a_new_execution(tmp_path):
    service, sources, provider = setup_service(tmp_path)
    provider.analyze.side_effect = GeminiReviewError("AI_QUOTA_EXCEEDED")
    failed = service.conclude(alert())["report"]
    assert failed["analysis_attempted_at"] is not None
    service.analyst = None
    result = service.conclude(alert())["report"]
    assert result["analysis_status"] == "unavailable"
    assert result["analysis_error"] == {
        "code": "AI_NOT_CONFIGURED",
        "message": "No se configuró Gemini.",
    }
    assert all(
        result[field] is None
        for field in (
            "analysis_model",
            "analysis_attempted_at",
            "analysis_created_at",
            "conclusions",
        )
    )
    assert len(sources.calls) == provider.analyze.call_count == 1


def test_public_response_has_only_documented_fields_and_no_original_payload(tmp_path):
    service, _, _ = setup_service(tmp_path)
    result = service.conclude(alert())
    assert set(result) == {"report", "reused", "revisions"}
    assert set(result["report"]) == {
        "report_id",
        "case_id",
        "revision_id",
        "previous_revision_id",
        "revision_created_at",
        "protocol",
        "history",
        "metric_profile",
        "policy",
        "comparison_context",
        "evidence_digest",
        "analysis_version",
        "analysis_status",
        "analysis_model",
        "analysis_attempted_at",
        "analysis_created_at",
        "analysis_error",
        "conclusions",
        "alert",
    }
    assert set(result["report"]["alert"]) == {
        "alert_id",
        "merchant_code",
        "merchant_name",
        "country",
        "kipu_generated_at",
    }


@pytest.mark.parametrize("model", [None, "not a model", True, "gemini-BAD", "gemini-x/secret"])
def test_invalid_injected_model_is_rejected_before_any_provider_or_source_call(tmp_path, model):
    sources = Sources()
    provider = Mock(analyze=Mock(return_value=conclusion()))
    with pytest.raises(ValueError, match="valid Gemini model"):
        GuardianCaseReview(
            tmp_path,
            policy=FilterPolicy(),
            reader_factory=sources.factory,
            analyst=provider,
            model=model,
        )
    assert not sources.calls
    provider.analyze.assert_not_called()
