"""Inline Guardian conclusions freeze evidence and retain their alert association."""

import json
from copy import deepcopy
from dataclasses import replace
from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from alert_reviewer import guardian_conclusions as module
from alert_reviewer.alert_filter import FilterPolicy
from alert_reviewer.datalake_history import HistoryError, HistoryQuery, summarize_rows
from alert_reviewer.gemini_review import GeminiReviewError
from alert_reviewer.guardian import GuardianAgent
from alert_reviewer.guardian_analysis import ANALYSIS_VERSION
from alert_reviewer.guardian_conclusions import GuardianConclusions


def alert(**changes):
    return {
        "schema_version": "1.0",
        "alert_id": "synthetic-alert",
        "merchant_code": "synthetic-mid",
        "merchant_name": "Synthetic merchant",
        "country": "Ecuador",
        "timestamp": "2026-09-07T12:00:00Z",
        "kipu_generated_at": "2026-08-10T12:00:00Z",
        "criticality": "Critica",
        "approval_rate": 0.1,
        "total_transactions": 100,
        "declined_count": 90,
        "rolling_avg_approval_rate": 0.9,
        **changes,
    }


def conclusions():
    return {
        "verdict": "requires_review",
        "verdict_evidence_ids": ["alert_0", "day_0"],
        "summary": "La alerta y el histórico requieren contrastar ventanas distintas.",
        "findings": [
            {
                "observation": "La alerta registra aprobación de 10%.",
                "evidence_ids": ["alert_0"],
            },
            {
                "observation": "El día observado del histórico registra aprobación de 90%.",
                "evidence_ids": ["day_0"],
            },
        ],
        "limitations": ["No está acreditada la equivalencia de ventanas y universos."],
        "next_steps": ["Contrastar la ventana original de la alerta."],
    }


class FakeReader:
    def __init__(self, *, empty=False):
        self.calls = []
        self.empty = empty

    def read(self, query):
        self.calls.append(query)
        rows = (
            []
            if self.empty
            else [
                {
                    "business_date": query.date_from.isoformat(),
                    "total_transactions": "100",
                    "approved_transactions": "90",
                    "declined_transactions": "10",
                    "other_transactions": "0",
                }
            ]
        )
        return summarize_rows(
            rows,
            query,
            {
                "catalog": "s3tablescatalog/datalake-prod",
                "database": "odl",
                "table": "card_transaction",
                "query_execution_id": "synthetic-query",
                "data_scanned_bytes": 100,
                "retrieved_at": "2026-09-07T12:00:00+00:00",
            },
        )


def fake_agent(*, empty=False, policy=None):
    reader = FakeReader(empty=empty)
    analyst = SimpleNamespace(analyze=Mock(return_value=conclusions()))
    return GuardianAgent(
        policy or FilterPolicy(),
        reader,
        alert_analyst=analyst,
        model="gemini-3.1-flash-lite",
    )


@pytest.fixture(autouse=True)
def prevent_real_providers(monkeypatch):
    monkeypatch.setattr(
        "alert_reviewer.gemini_review.urlopen",
        Mock(side_effect=AssertionError("No network is allowed in these tests")),
    )
    monkeypatch.setattr(
        "alert_reviewer.datalake_history.boto3.Session",
        Mock(side_effect=AssertionError("No AWS session is allowed in these tests")),
    )


@pytest.mark.parametrize(
    ("generated", "start", "end"),
    [
        ("2026-08-10T12:00:00Z", date(2026, 8, 3), date(2026, 8, 9)),
        ("2026-08-10T02:00:00Z", date(2026, 8, 2), date(2026, 8, 8)),
        ("2026-08-09T21:00:00-05:00", date(2026, 8, 2), date(2026, 8, 8)),
    ],
)
def test_inline_conclusion_uses_kipu_generation_and_fixed_seven_days(
    tmp_path, generated, start, end
):
    agent = fake_agent()
    raw = alert(kipu_generated_at=generated)
    original = deepcopy(raw)

    result = GuardianConclusions(tmp_path, agent=agent).conclude(raw)

    assert raw == original
    assert agent.reader.calls == [HistoryQuery("synthetic-mid", start, end)]
    report = result["report"]
    assert result["reused"] is False
    assert report["protocol"]
    assert report["report_id"]
    assert report["evidence_digest"]
    assert report["analysis_status"] == "completed"
    assert report["analysis_version"] == ANALYSIS_VERSION
    assert report["conclusions"] == conclusions()
    assert report["alert"]["alert_id"] == raw["alert_id"]
    assert report["alert"]["merchant_code"] == raw["merchant_code"]
    assert report["alert"]["merchant_name"] == raw["merchant_name"]
    assert report["alert"]["country"] == raw["country"]
    assert report["history"]["query"]["timezone"] == "America/Guayaquil"
    assert report["history"]["source"]["query_execution_id"] == "synthetic-query"
    assert report["policy"]["accepted"] is True
    assert report["analysis_created_at"]
    assert not list((tmp_path / "guardian-alerts").glob("*.lock"))


def test_success_is_reused_across_instances_without_providers(tmp_path):
    first_agent = fake_agent()
    first = GuardianConclusions(tmp_path, agent=first_agent).conclude(alert())
    second_agent = fake_agent()
    second_agent.reader.read = Mock(side_effect=AssertionError("Evidence must be frozen"))
    second_agent.alert_analyst.analyze.side_effect = AssertionError("Reuse exact conclusion")

    second = GuardianConclusions(tmp_path, agent=second_agent).conclude(alert())

    assert second["reused"] is True
    assert second["report"] == first["report"]
    second_agent.reader.read.assert_not_called()
    second_agent.alert_analyst.analyze.assert_not_called()
    assert len(list((tmp_path / "guardian-alerts").glob("*.json"))) == 1


@pytest.mark.parametrize("status", ["completed", "pending", "unavailable", "no_data"])
def test_old_extraction_cache_is_preserved_and_replaced_by_a_new_query(
    tmp_path, monkeypatch, status
):
    assert module.PROTOCOL == "guardian-alert-card-7d-v2"
    assert ANALYSIS_VERSION == "guardian-analyst-verdict-v2"
    # Seed the previous extraction namespace, not merely a previous AI response
    # version under the current namespace.
    with monkeypatch.context() as old_protocol:
        old_protocol.setattr(module, "PROTOCOL", "guardian-alert-card-7d-v1")
        previous = GuardianConclusions(
            tmp_path, agent=fake_agent(empty=status == "no_data")
        ).conclude(alert())
    old_id = previous["report"]["report_id"]
    old_path = tmp_path / "guardian-alerts" / f"{old_id}.json"
    saved = json.loads(old_path.read_text(encoding="utf-8"))
    saved["history"]["schema_version"] = "1.0"
    saved["history"]["source"]["query_execution_id"] = "synthetic-ticket-query-v1"
    saved["evidence_digest"] = module._digest(saved["history"])
    saved["analysis_version"] = "guardian-analyst-verdict-v1"
    saved["analysis_status"] = status
    if status != "completed":
        saved["conclusions"] = None
    old_path.write_text(json.dumps(saved), encoding="utf-8")
    original_bytes = old_path.read_bytes()

    agent = fake_agent()
    read = agent.reader.read

    def new_history(query):
        report = read(query)
        report["source"]["query_execution_id"] = "synthetic-transaction-query-v2"
        return report

    agent.reader.read = Mock(side_effect=new_history)
    result = GuardianConclusions(tmp_path, agent=agent).conclude(alert())

    report = result["report"]
    assert result["reused"] is False
    assert report["report_id"] != old_id
    assert report["protocol"] == "guardian-alert-card-7d-v2"
    assert report["history"]["schema_version"] == "2.0"
    assert report["history"]["source"]["query_execution_id"] == "synthetic-transaction-query-v2"
    assert report["analysis_status"] == "completed"
    assert report["analysis_version"] == "guardian-analyst-verdict-v2"
    assert old_path.read_bytes() == original_bytes
    assert len(list(old_path.parent.glob("*.json"))) == 2
    agent.reader.read.assert_called_once()
    agent.alert_analyst.analyze.assert_called_once()


@pytest.mark.parametrize("empty", [False, True])
def test_old_history_under_current_cache_key_fails_without_relabeling_or_provider_calls(
    tmp_path, empty
):
    first = GuardianConclusions(tmp_path, agent=fake_agent(empty=empty)).conclude(alert())
    path = tmp_path / "guardian-alerts" / f"{first['report']['report_id']}.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    saved["history"]["schema_version"] = "1.0"
    # Recompute the digest to isolate the semantic version check from hash checks.
    # These all-final counts alone cannot establish the new extraction contract.
    saved["evidence_digest"] = module._digest(saved["history"])
    path.write_text(json.dumps(saved), encoding="utf-8")
    original_bytes = path.read_bytes()
    agent = _frozen_agent()

    with pytest.raises(HistoryError) as caught:
        GuardianConclusions(tmp_path, agent=agent).conclude(alert())

    assert caught.value.code == "EVIDENCE_INVALID"
    assert path.read_bytes() == original_bytes
    assert len(list(path.parent.glob("*.json"))) == 1
    agent.reader.read.assert_not_called()
    agent.alert_analyst.analyze.assert_not_called()


def test_live_legacy_history_is_rejected_before_persistence_and_gemini(tmp_path):
    agent = fake_agent()
    read = agent.reader.read

    def old_history(query):
        report = read(query)
        report["schema_version"] = "1.0"
        return report

    agent.reader.read = Mock(side_effect=old_history)

    with pytest.raises(HistoryError) as caught:
        GuardianConclusions(tmp_path, agent=agent).conclude(alert())

    assert caught.value.code == "INVALID_HISTORY_SOURCE"
    assert not list((tmp_path / "guardian-alerts").glob("*.json"))
    assert not list((tmp_path / "guardian-alerts").glob("*.lock"))
    agent.reader.read.assert_called_once()
    agent.alert_analyst.analyze.assert_not_called()


def test_replay_timestamp_change_does_not_change_evidence_identity(tmp_path):
    agent = fake_agent()
    service = GuardianConclusions(tmp_path, agent=agent)
    first = service.conclude(alert(timestamp="2026-09-06T12:00:00Z"))
    second = service.conclude(alert(timestamp="2026-09-07T12:00:00Z"))

    assert second["reused"] is True
    assert second["report"] == first["report"]
    assert len(agent.reader.calls) == 1
    agent.alert_analyst.analyze.assert_called_once()


@pytest.mark.parametrize(
    "changes",
    [
        {"merchant_code": "another-mid"},
        {"kipu_generated_at": "2026-08-11T12:00:00Z"},
        {"approval_rate": 0.09},
        {"rolling_avg_approval_rate": 0.85},
        {"predicted_ar_q10": 0.8},
        {"predicted_dc_q90": 50.5},
    ],
)
def test_changed_alert_evidence_cannot_reuse_previous_result(tmp_path, changes):
    agent = fake_agent()
    service = GuardianConclusions(tmp_path, agent=agent)
    first = service.conclude(alert())
    second = service.conclude(alert(**changes))

    assert second["reused"] is False
    assert second["report"]["report_id"] != first["report"]["report_id"]
    assert len(agent.reader.calls) == 2
    assert agent.alert_analyst.analyze.call_count == 2


def test_policy_content_change_even_with_same_version_changes_cache_key(tmp_path):
    policy = FilterPolicy()
    first_agent = fake_agent(policy=policy)
    first = GuardianConclusions(tmp_path, agent=first_agent).conclude(alert())
    changed_agent = fake_agent(policy=replace(policy, minimum_transactions=21))

    second = GuardianConclusions(tmp_path, agent=changed_agent).conclude(alert())

    assert second["reused"] is False
    assert second["report"]["report_id"] != first["report"]["report_id"]
    assert len(changed_agent.reader.calls) == 1


def test_evidence_is_persisted_before_gemini_runs(tmp_path):
    agent = fake_agent()

    def analyze(*_args, **_kwargs):
        stored = list((tmp_path / "guardian-alerts").glob("*.json"))
        assert len(stored) == 1
        persisted = stored[0].read_text(encoding="utf-8")
        assert "synthetic-query" in persisted
        assert "total_transactions" in persisted
        return conclusions()

    agent.alert_analyst.analyze.side_effect = analyze
    result = GuardianConclusions(tmp_path, agent=agent).conclude(alert())
    assert result["report"]["analysis_status"] == "completed"


def test_failed_gemini_retry_uses_persisted_evidence_not_athena(tmp_path):
    first_agent = fake_agent()
    first_agent.alert_analyst.analyze.side_effect = GeminiReviewError("AI_UNAVAILABLE")
    first = GuardianConclusions(tmp_path, agent=first_agent).conclude(alert())
    assert first["report"]["analysis_status"] == "unavailable"
    assert first["report"]["conclusions"] is None
    assert first["report"]["history"]["metrics"]["total_transactions"] == 100

    next_agent = fake_agent()
    next_agent.reader.read = Mock(side_effect=AssertionError("Do not query Athena again"))
    retried = GuardianConclusions(tmp_path, agent=next_agent).conclude(alert())

    next_agent.reader.read.assert_not_called()
    next_agent.alert_analyst.analyze.assert_called_once()
    assert retried["report"]["analysis_status"] == "completed"
    assert retried["report"]["history"] == first["report"]["history"]
    assert retried["report"]["evidence_digest"] == first["report"]["evidence_digest"]
    assert retried["report"]["report_id"] == first["report"]["report_id"]


def test_empty_history_is_retained_without_gemini_or_repeated_athena(tmp_path):
    agent = fake_agent(empty=True)
    service = GuardianConclusions(tmp_path, agent=agent)
    first = service.conclude(alert())
    second = service.conclude(alert())

    assert first["report"]["analysis_status"] == "no_data"
    assert first["report"]["conclusions"] is None
    assert first["report"]["history"]["metrics"]["approval_rate"] is None
    assert second["reused"] is True
    assert len(agent.reader.calls) == 1
    agent.alert_analyst.analyze.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "2.0"},
        {"schema_version": None},
        {"alert_id": "fallback-01"},
        {"alert_id": ""},
        {"merchant_code": "mid' OR 1=1 --"},
        {"merchant_code": 123},
        {"kipu_generated_at": None},
        {"kipu_generated_at": "2026-08-10T12:00:00"},
        {"kipu_generated_at": "2099-08-10T12:00:00Z"},
        {"kipu_generated_at": "not-a-time"},
        {"approval_rate": 10},
        {"total_transactions": True},
        {"declined_count": 101},
    ],
)
def test_invalid_alerts_fail_before_either_provider(tmp_path, changes):
    agent = fake_agent()
    with pytest.raises(ValueError):
        GuardianConclusions(tmp_path, agent=agent).conclude(alert(**changes))
    assert agent.reader.calls == []
    agent.alert_analyst.analyze.assert_not_called()


@pytest.mark.parametrize("raw", [None, [], "MID", {}, {"merchant_code": "synthetic-mid"}])
def test_presentation_or_mid_only_inputs_are_not_reconstructed(tmp_path, raw):
    agent = fake_agent()
    with pytest.raises(ValueError):
        GuardianConclusions(tmp_path, agent=agent).conclude(raw)
    assert agent.reader.calls == []
    agent.alert_analyst.analyze.assert_not_called()


def test_corrupt_evidence_fails_closed_instead_of_querying_again(tmp_path):
    agent = fake_agent()
    service = GuardianConclusions(tmp_path, agent=agent)
    service.conclude(alert())
    path = next((tmp_path / "guardian-alerts").glob("*.json"))
    path.write_text("{not-json", encoding="utf-8")

    with pytest.raises(HistoryError) as raised:
        service.conclude(alert())

    assert raised.value.code == "EVIDENCE_INVALID"
    assert len(agent.reader.calls) == 1
    agent.alert_analyst.analyze.assert_called_once()


def test_active_lock_prevents_a_duplicate_extraction(tmp_path):
    agent = fake_agent()
    service = GuardianConclusions(tmp_path, agent=agent)
    service.conclude(alert())
    path = next((tmp_path / "guardian-alerts").glob("*.json"))
    # Simulate another process holding this key before its first evidence write.
    path.with_suffix(".lock").write_text("active", encoding="utf-8")
    path.unlink()

    with pytest.raises(HistoryError) as raised:
        service.conclude(alert())

    assert raised.value.code == "AGENT_BUSY"
    assert len(agent.reader.calls) == 1
    agent.alert_analyst.analyze.assert_called_once()


def test_returned_mutation_cannot_change_frozen_evidence(tmp_path):
    agent = fake_agent()
    service = GuardianConclusions(tmp_path, agent=agent)
    first = service.conclude(alert())
    expected = deepcopy(first["report"])
    first["report"]["history"]["metrics"]["total_transactions"] = 99999
    first["report"]["conclusions"]["summary"] = "Tampered returned result"

    second = service.conclude(alert())

    assert second["report"] == expected
    assert len(agent.reader.calls) == 1


def test_cached_provider_metadata_does_not_expose_error_body(tmp_path):
    agent = fake_agent()
    agent.alert_analyst.analyze.side_effect = GeminiReviewError("AI_INVALID_ANALYSIS")
    result = GuardianConclusions(tmp_path, agent=agent).conclude(alert())
    encoded = json.dumps(result)
    assert "api_key" not in encoded
    assert '"authorization"' not in encoded.lower()
    assert result["report"]["analysis_error"]["code"] == "AI_UNAVAILABLE"
    assert result["report"]["policy"]["accepted"] is True


def test_default_factory_pins_status_taxonomy_despite_ambient_overrides(tmp_path, monkeypatch):
    settings = SimpleNamespace(
        history_aws_profile="data-core",
        history_athena_workgroup="primary",
        history_approved_statuses="SETTLED",
        history_declined_statuses="CANCELLED",
    )
    configured = Mock(return_value=fake_agent())
    monkeypatch.setattr(module, "GuardianSettings", Mock(return_value=settings))
    monkeypatch.setattr(module.GuardianAgent, "configured", configured)

    GuardianConclusions(tmp_path)

    configured.assert_called_once()
    reader = configured.call_args.kwargs["reader"]
    assert reader.config.approved_statuses == ("APPROVED",)
    assert reader.config.declined_statuses == ("DECLINED",)
    assert reader.config.profile == "data-core"


def test_null_and_optional_alert_metrics_keep_raw_units(tmp_path):
    agent = fake_agent()
    GuardianConclusions(tmp_path, agent=agent).conclude(
        alert(rolling_avg_approval_rate=None, predicted_ar_q10=0.6, predicted_dc_q90=50.5)
    )
    passed_alert, evidence, accepted = agent.alert_analyst.analyze.call_args.args
    assert passed_alert["approval_rate"] == 0.1
    assert passed_alert["rolling_avg_approval_rate"] is None
    assert passed_alert["predicted_ar_q10"] == 0.6
    assert passed_alert["predicted_dc_q90"] == 50.5
    assert evidence["daily"][0]["approval_rate_pct"] == 90.0
    assert accepted is True


def test_unexpected_analysis_failure_leaves_recoverable_pending_evidence(tmp_path):
    failed_agent = fake_agent()
    failed_agent.alert_analyst.analyze.side_effect = RuntimeError("Synthetic interrupted analysis")
    with pytest.raises(RuntimeError, match="Synthetic interrupted"):
        GuardianConclusions(tmp_path, agent=failed_agent).conclude(alert())
    assert len(list((tmp_path / "guardian-alerts").glob("*.json"))) == 1
    assert not list((tmp_path / "guardian-alerts").glob("*.lock"))

    fresh_agent = fake_agent()
    fresh_agent.reader.read = Mock(side_effect=AssertionError("Recover existing evidence only"))
    result = GuardianConclusions(tmp_path, agent=fresh_agent).conclude(alert())

    assert result["report"]["analysis_status"] == "completed"
    fresh_agent.reader.read.assert_not_called()
    fresh_agent.alert_analyst.analyze.assert_called_once()


@pytest.mark.parametrize(
    "tamper", ["metric", "citation", "alert_identity", "verdict", "verdict_citation"]
)
def test_structurally_valid_but_tampered_cache_fails_closed(tmp_path, tamper):
    agent = fake_agent()
    service = GuardianConclusions(tmp_path, agent=agent)
    service.conclude(alert())
    path = next((tmp_path / "guardian-alerts").glob("*.json"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    if tamper == "metric":
        saved["history"]["metrics"]["approved_transactions"] = 99
    elif tamper == "citation":
        saved["conclusions"]["findings"][0]["evidence_ids"] = ["another_alert_0"]
    elif tamper == "verdict":
        saved["conclusions"]["verdict"] = "false_positive"
    elif tamper == "verdict_citation":
        saved["conclusions"]["verdict_evidence_ids"] = ["alert_0", "another_day_0"]
    else:
        saved["original_alert"]["merchant_code"] = "another-mid"
    path.write_text(json.dumps(saved), encoding="utf-8")

    with pytest.raises(HistoryError) as raised:
        service.conclude(alert())

    assert raised.value.code == "EVIDENCE_INVALID"
    assert len(agent.reader.calls) == 1
    agent.alert_analyst.analyze.assert_called_once()


def _legacy_cache(tmp_path):
    """Reproduce the pre-verdict on-disk contract without any provider calls."""
    agent = fake_agent()
    GuardianConclusions(tmp_path, agent=agent).conclude(alert())
    path = next((tmp_path / "guardian-alerts").glob("*.json"))
    saved = json.loads(path.read_text(encoding="utf-8"))
    del saved["analysis_version"]
    del saved["conclusions"]["verdict"]
    del saved["conclusions"]["verdict_evidence_ids"]
    # A valid old response may exceed the new concise contract.
    saved["conclusions"]["summary"] = "L" * 700
    saved["conclusions"]["next_steps"].append("Verificar el último estado CDC.")
    saved["analysis_model"] = "synthetic-legacy-model"
    saved["analysis_created_at"] = "2026-09-06T12:00:00+00:00"
    path.write_text(json.dumps(saved), encoding="utf-8")
    return path, saved


def _frozen_agent():
    agent = fake_agent()
    agent.reader.read = Mock(
        side_effect=AssertionError("Reuse frozen evidence; never query Athena")
    )
    return agent


def _expected_previous(saved):
    return {
        name: saved.get(name)
        for name in (
            "analysis_version",
            "analysis_status",
            "analysis_model",
            "analysis_created_at",
            "conclusions",
        )
    }


def test_legacy_conclusion_refreshes_only_gemini_preserving_evidence_and_private_audit(tmp_path):
    path, legacy = _legacy_cache(tmp_path)
    agent = _frozen_agent()

    def analyze(*_args, **_kwargs):
        pending = json.loads(path.read_text(encoding="utf-8"))
        assert pending["analysis_version"] == ANALYSIS_VERSION
        assert pending["analysis_status"] == "pending"
        assert pending["conclusions"] is None
        assert pending["analysis_model"] is None
        assert pending["analysis_created_at"] is None
        assert pending["previous_analysis"] == _expected_previous(legacy)
        assert pending["history"] == legacy["history"]
        return conclusions()

    agent.alert_analyst.analyze.side_effect = analyze
    result = GuardianConclusions(tmp_path, agent=agent).conclude(alert())

    assert result["reused"] is True
    assert result["report"]["analysis_version"] == ANALYSIS_VERSION
    assert result["report"]["analysis_status"] == "completed"
    assert result["report"]["conclusions"] == conclusions()
    for name in ("report_id", "protocol", "evidence_digest", "history", "policy"):
        assert result["report"][name] == legacy[name]
    assert "previous_analysis" not in result["report"]
    assert "synthetic-legacy-model" not in json.dumps(result)
    refreshed = json.loads(path.read_text(encoding="utf-8"))
    assert refreshed["previous_analysis"] == _expected_previous(legacy)
    assert refreshed["original_alert"] == legacy["original_alert"]
    assert refreshed["history"]["source"]["query_execution_id"] == "synthetic-query"
    assert len(list(path.parent.glob("*.json"))) == 1
    agent.reader.read.assert_not_called()
    agent.alert_analyst.analyze.assert_called_once()

    # The new contract is reusable; neither the legacy audit nor analysis repeats.
    cached_agent = _frozen_agent()
    cached_agent.alert_analyst.analyze.side_effect = AssertionError("Reuse current conclusion")
    cached = GuardianConclusions(tmp_path, agent=cached_agent).conclude(alert())
    assert cached == result
    cached_agent.reader.read.assert_not_called()
    cached_agent.alert_analyst.analyze.assert_not_called()
    assert json.loads(path.read_text(encoding="utf-8"))["previous_analysis"] == _expected_previous(
        legacy
    )


def test_failed_legacy_upgrade_retries_same_evidence_without_losing_previous_analysis(tmp_path):
    path, legacy = _legacy_cache(tmp_path)
    failed_agent = _frozen_agent()
    failed_agent.alert_analyst.analyze.side_effect = GeminiReviewError("AI_UNAVAILABLE")

    failed = GuardianConclusions(tmp_path, agent=failed_agent).conclude(alert())

    assert failed["report"]["analysis_status"] == "unavailable"
    assert failed["report"]["analysis_version"] == ANALYSIS_VERSION
    assert failed["report"]["conclusions"] is None
    assert "previous_analysis" not in failed["report"]
    saved_failure = json.loads(path.read_text(encoding="utf-8"))
    assert saved_failure["previous_analysis"] == _expected_previous(legacy)
    assert saved_failure["history"] == legacy["history"]
    failed_agent.reader.read.assert_not_called()

    retry_agent = _frozen_agent()
    retried = GuardianConclusions(tmp_path, agent=retry_agent).conclude(alert())

    assert retried["report"]["analysis_status"] == "completed"
    assert retried["report"]["report_id"] == legacy["report_id"]
    assert retried["report"]["evidence_digest"] == legacy["evidence_digest"]
    assert retried["report"]["history"] == legacy["history"]
    assert json.loads(path.read_text(encoding="utf-8"))["previous_analysis"] == _expected_previous(
        legacy
    )
    retry_agent.reader.read.assert_not_called()
    retry_agent.alert_analyst.analyze.assert_called_once()


def test_interrupted_legacy_upgrade_leaves_new_version_pending_and_recovers_without_athena(
    tmp_path,
):
    path, legacy = _legacy_cache(tmp_path)
    failed_agent = _frozen_agent()
    failed_agent.alert_analyst.analyze.side_effect = RuntimeError("Synthetic upgrade interrupted")

    with pytest.raises(RuntimeError, match="Synthetic upgrade interrupted"):
        GuardianConclusions(tmp_path, agent=failed_agent).conclude(alert())

    pending = json.loads(path.read_text(encoding="utf-8"))
    assert pending["analysis_status"] == "pending"
    assert pending["analysis_version"] == ANALYSIS_VERSION
    assert pending["conclusions"] is None
    assert pending["history"] == legacy["history"]
    assert pending["previous_analysis"] == _expected_previous(legacy)
    assert not list(path.parent.glob("*.lock"))

    recovered_agent = _frozen_agent()
    recovered = GuardianConclusions(tmp_path, agent=recovered_agent).conclude(alert())

    assert recovered["report"]["analysis_status"] == "completed"
    assert recovered["report"]["evidence_digest"] == legacy["evidence_digest"]
    failed_agent.reader.read.assert_not_called()
    recovered_agent.reader.read.assert_not_called()
    recovered_agent.alert_analyst.analyze.assert_called_once()


@pytest.mark.parametrize("version", ["guardian-analyst-verdict-v99", "", False, 1, [], {}])
def test_unknown_cached_analysis_version_fails_closed_before_providers(tmp_path, version):
    path, legacy = _legacy_cache(tmp_path)
    legacy["analysis_version"] = version
    path.write_text(json.dumps(legacy), encoding="utf-8")
    agent = _frozen_agent()

    with pytest.raises(HistoryError) as raised:
        GuardianConclusions(tmp_path, agent=agent).conclude(alert())

    assert raised.value.code == "EVIDENCE_INVALID"
    assert json.loads(path.read_text(encoding="utf-8")) == legacy
    agent.reader.read.assert_not_called()
    agent.alert_analyst.analyze.assert_not_called()


@pytest.mark.parametrize("tamper", ["history", "citation", "schema"])
def test_invalid_legacy_evidence_or_conclusion_cannot_trigger_migration(tmp_path, tamper):
    path, legacy = _legacy_cache(tmp_path)
    if tamper == "history":
        legacy["history"]["metrics"]["total_transactions"] = 999
    elif tamper == "citation":
        legacy["conclusions"]["findings"][0]["evidence_ids"] = ["unknown_alert"]
    else:
        legacy["conclusions"]["verdict"] = "requires_review"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    agent = _frozen_agent()

    with pytest.raises(HistoryError) as raised:
        GuardianConclusions(tmp_path, agent=agent).conclude(alert())

    assert raised.value.code == "EVIDENCE_INVALID"
    assert json.loads(path.read_text(encoding="utf-8")) == legacy
    agent.reader.read.assert_not_called()
    agent.alert_analyst.analyze.assert_not_called()


def test_current_version_cache_without_verdict_is_not_silently_treated_as_legacy(tmp_path):
    path, legacy = _legacy_cache(tmp_path)
    legacy["analysis_version"] = ANALYSIS_VERSION
    path.write_text(json.dumps(legacy), encoding="utf-8")
    agent = _frozen_agent()

    with pytest.raises(HistoryError) as raised:
        GuardianConclusions(tmp_path, agent=agent).conclude(alert())

    assert raised.value.code == "EVIDENCE_INVALID"
    agent.reader.read.assert_not_called()
    agent.alert_analyst.analyze.assert_not_called()
