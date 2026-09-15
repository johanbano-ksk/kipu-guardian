"""Agent-level orchestration checks: no network, real policy, explicit evidence."""

from copy import deepcopy
from datetime import UTC, date, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from alert_reviewer import datalake_history, guardian
from alert_reviewer.alert_filter import FilterPolicy, PayloadAlertFilter
from alert_reviewer.datalake_history import HistoryError, HistoryQuery, summarize_rows
from alert_reviewer.gemini_review import GeminiReviewError
from alert_reviewer.guardian import GuardianAgent, SavedHistoryReader, validate_history_report

QUERY = HistoryQuery("synthetic-mid", date(2026, 8, 3), date(2026, 8, 9))
CONCLUSIONS = {
    "summary": "El histórico requiere contrastar ventanas y poblaciones.",
    "findings": [{"observation": "Hay actividad registrada.", "evidence_ids": ["day_0"]}],
    "limitations": ["No acredita por sí solo un incidente."],
    "next_steps": ["Contrastar la ventana de la alerta."],
}


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime(2026, 9, 7, 15, tzinfo=UTC).astimezone(tz)

    monkeypatch.setattr(guardian, "datetime", FixedDateTime)
    monkeypatch.setattr(datalake_history, "datetime", FixedDateTime)


def alert_payload(**changes):
    return {
        "schema_version": "1.0",
        "alert_id": "synthetic-alert",
        "merchant_code": "synthetic-mid",
        "merchant_name": "Synthetic merchant",
        "country": "Ecuador",
        "timestamp": "2026-08-10T12:00:00Z",
        "criticality": "Critica",
        "approval_rate": 0.1,
        "total_transactions": 100,
        "declined_count": 90,
        "rolling_avg_approval_rate": 0.9,
        **changes,
    }


def history_report(query=QUERY, *, empty=False):
    rows = (
        []
        if empty
        else [
            {
                "business_date": day.isoformat(),
                "total_transactions": "100",
                "approved_transactions": "90",
                "declined_transactions": "10",
                "other_transactions": "0",
            }
            for day in dict.fromkeys((query.date_from, query.date_to))
        ]
    )
    return summarize_rows(rows, query, {"query_execution_id": "synthetic-query"})


class FakeReader:
    def __init__(self, *, error=None, empty=False, report=None):
        self.calls = []
        self.error = error
        self.empty = empty
        self.report = report

    def read(self, query):
        self.calls.append(query)
        if self.error:
            raise self.error
        return self.report if self.report is not None else history_report(query, empty=self.empty)


def agent_with(reader=None, *, alert_error=None, history_error=None):
    alert_analyst = SimpleNamespace(
        analyze=Mock(return_value=deepcopy(CONCLUSIONS), side_effect=alert_error)
    )
    history_analyst = SimpleNamespace(
        analyze=Mock(return_value=deepcopy(CONCLUSIONS), side_effect=history_error)
    )
    agent = GuardianAgent(
        FilterPolicy(),
        reader or FakeReader(),
        alert_analyst,
        history_analyst,
        model="synthetic-model",
    )
    return agent, alert_analyst, history_analyst


@pytest.mark.parametrize(
    ("timestamp", "start", "end"),
    [
        ("2026-08-10T12:00:00Z", date(2026, 8, 3), date(2026, 8, 9)),
        ("2026-08-10T02:00:00Z", date(2026, 8, 2), date(2026, 8, 8)),
        ("2026-08-09T21:00:00-05:00", date(2026, 8, 2), date(2026, 8, 8)),
    ],
)
def test_history_window_uses_seven_complete_ecuador_days_before_alert(timestamp, start, end):
    agent, analyst, history_analyst = agent_with()
    alert = alert_payload(timestamp=timestamp)

    result = agent.review_alerts([alert])

    assert agent.reader.calls == [HistoryQuery("synthetic-mid", start, end)]
    assert result["accepted_alerts"] == [alert]
    review = result["reviews"][0]
    assert review["history_status"] == "completed"
    assert review["analysis_status"] == "completed"
    assert review["conclusions"] == CONCLUSIONS
    assert review["analysis_model"] == "synthetic-model"
    analyst.analyze.assert_called_once()
    passed_alert, evidence, accepted = analyst.analyze.call_args.args
    assert passed_alert == alert
    assert accepted is True
    assert evidence["data_quality"]["requested_days"] == 7
    assert "merchant_code" not in evidence
    history_analyst.analyze.assert_not_called()


def test_lookback_can_be_bounded_to_one_complete_day():
    agent, _, _ = agent_with()
    agent.review_alerts([alert_payload()], lookback_days=1)
    assert agent.reader.calls == [HistoryQuery("synthetic-mid", date(2026, 8, 9), date(2026, 8, 9))]


@pytest.mark.parametrize("mid", [None, 123, "", " ", "mid' OR 1=1 --", "mid\n", "ñ", "m" * 101])
def test_unsafe_or_missing_mid_skips_history_without_network(mid):
    agent, analyst, _ = agent_with()
    result = agent.review_alerts([alert_payload(merchant_code=mid)])
    review = result["reviews"][0]
    assert review["history_status"] == "skipped"
    assert review["analysis_error"]["code"] == "INVALID_ALERT_FOR_HISTORY"
    assert review["history"] is None
    assert review["conclusions"] is None
    assert agent.reader.calls == []
    analyst.analyze.assert_not_called()


@pytest.mark.parametrize(
    "timestamp", ["2026-09-07T15:00:01Z", "2026-08-10T12:00:00", "not-a-date", None]
)
def test_future_ambiguous_or_invalid_alert_time_does_not_scan(timestamp):
    agent, analyst, _ = agent_with()
    result = agent.review_alerts([alert_payload(timestamp=timestamp)])
    assert result["reviews"][0]["history_status"] == "skipped"
    assert result["history_query_count"] == 0
    analyst.analyze.assert_not_called()


@pytest.mark.parametrize(
    "changes",
    [
        {"schema_version": "3.0"},
        {"merchant_name": None},
        {"total_transactions": True},
        {"total_transactions": "100"},
        {"approval_rate": "0.1"},
        {"declined_count": 101},
        {"approval_rate": 0.9},
        {"rolling_avg_approval_rate": "0.9"},
        {"predicted_ar_q10": 0.9, "predicted_ar_q50": 0.5},
    ],
)
def test_invalid_payloads_do_not_receive_historical_analysis(changes):
    agent, analyst, _ = agent_with()
    result = agent.review_alerts([alert_payload(**changes)])
    assert result["accepted_alerts"] == []
    assert result["reviews"][0]["history_status"] == "skipped"
    assert agent.reader.calls == []
    analyst.analyze.assert_not_called()


def test_v2_payload_preserves_existing_policy_but_never_queries_history(v2_alert):
    agent, analyst, _ = agent_with()
    alert = v2_alert.model_dump(mode="json")
    expected = PayloadAlertFilter().evaluate(alert)
    result = agent.review_alerts([alert])
    assert result["reviews"][0]["policy"]["accepted"] == expected.accepted
    assert result["accepted_alerts"] == ([alert] if expected.accepted else [])
    assert result["reviews"][0]["history_status"] == "skipped"
    assert agent.reader.calls == []
    analyst.analyze.assert_not_called()


def test_rejected_noncritical_alert_can_be_advised_without_overriding_policy():
    agent, analyst, _ = agent_with()
    alert = alert_payload(criticality="Alta")
    before = deepcopy(alert)
    result = agent.review_alerts([alert])
    review = result["reviews"][0]
    assert result["accepted_alerts"] == []
    assert review["policy"]["accepted"] is False
    assert "NON_CRITICAL_ALERT" in review["policy"]["reason_codes"]
    assert review["conclusions"] == CONCLUSIONS
    assert analyst.analyze.call_args.args[2] is False
    assert alert == before


def test_cache_uses_mid_and_local_day_range_but_each_alert_gets_its_own_analysis():
    agent, analyst, _ = agent_with()
    alerts = [
        alert_payload(alert_id="first"),
        alert_payload(alert_id="same-local-day", timestamp="2026-08-11T03:00:00Z"),
        alert_payload(alert_id="other-mid", merchant_code="another-mid"),
        alert_payload(alert_id="other-day", timestamp="2026-08-11T12:00:00Z"),
    ]
    result = agent.review_alerts(alerts)
    assert len(agent.reader.calls) == result["history_query_count"] == 3
    assert analyst.analyze.call_count == 4
    assert result["accepted_alerts"] == alerts
    result["reviews"][0]["history"]["daily"][0]["total_transactions"] = 999
    assert result["reviews"][1]["history"]["daily"][0]["total_transactions"] == 100


def test_query_budget_skips_new_ranges_but_allows_an_existing_cached_range():
    agent, analyst, _ = agent_with()
    alerts = [
        alert_payload(alert_id="first"),
        alert_payload(alert_id="not-queried", merchant_code="another-mid"),
        alert_payload(alert_id="cached"),
    ]
    result = agent.review_alerts(alerts, max_history_queries=1)
    assert result["accepted_alerts"] == alerts
    assert result["history_query_count"] == len(agent.reader.calls) == 1
    assert [r["history_status"] for r in result["reviews"]] == ["completed", "skipped", "completed"]
    assert result["reviews"][1]["analysis_error"]["code"] == "HISTORY_QUERY_LIMIT"
    assert result["reviews"][1]["history"] is None
    assert analyst.analyze.call_count == 2


def test_athena_failure_is_cached_and_preserves_policy_without_fabricating_evidence():
    reader = FakeReader(error=HistoryError("SSO_EXPIRED", "Renueva la sesión AWS."))
    agent, analyst, _ = agent_with(reader)
    alerts = [alert_payload(alert_id="first"), alert_payload(alert_id="second")]
    result = agent.review_alerts(alerts)
    assert result["accepted_alerts"] == alerts
    assert result["history_query_count"] == len(reader.calls) == 1
    for review in result["reviews"]:
        assert review["history_status"] == review["analysis_status"] == "unavailable"
        assert review["analysis_error"]["code"] == "SSO_EXPIRED"
        assert review["history"] is None
        assert review["conclusions"] is None
    analyst.analyze.assert_not_called()


def test_inconsistent_history_is_rejected_without_losing_policy():
    report = history_report()
    report["metrics"]["total_transactions"] += 1
    agent, analyst, _ = agent_with(FakeReader(report=report))
    result = agent.review_alerts([alert_payload()])
    assert result["accepted_alerts"] == [alert_payload()]
    review = result["reviews"][0]
    assert review["history_status"] == "unavailable"
    assert review["analysis_error"]["code"] == "INVALID_HISTORY_SOURCE"
    assert review["history"] is None
    analyst.analyze.assert_not_called()


def test_gemini_failure_keeps_actual_metrics_and_deterministic_decision():
    agent, _, _ = agent_with(alert_error=GeminiReviewError("PROVIDER_UNAVAILABLE"))
    result = agent.review_alerts([alert_payload()])
    assert result["accepted_alerts"] == [alert_payload()]
    review = result["reviews"][0]
    assert review["history_status"] == "completed"
    assert review["history"]["metrics"]["total_transactions"] == 200
    assert review["analysis_status"] == "unavailable"
    assert review["analysis_error"]["code"] == "PROVIDER_UNAVAILABLE"
    assert review["conclusions"] is None


def test_policy_only_has_no_athena_or_gemini_calls_and_returns_original_payloads():
    agent, alert_analyst, history_analyst = agent_with()
    alerts = [alert_payload(), alert_payload(alert_id="noncritical", criticality="Baja")]
    before = deepcopy(alerts)
    result = agent.review_alerts(alerts, with_history=False)
    assert alerts == before
    assert result["accepted_alerts"] == [alerts[0]]
    assert result["accepted_alerts"][0] is not alerts[0]
    assert result["history_query_count"] == 0
    assert all(r["history_status"] == "not_requested" for r in result["reviews"])
    assert agent.reader.calls == []
    alert_analyst.analyze.assert_not_called()
    history_analyst.analyze.assert_not_called()


def test_empty_history_stays_unknown_and_skips_gemini():
    agent, analyst, _ = agent_with(FakeReader(empty=True))
    result = agent.review_alerts([alert_payload()])
    review = result["reviews"][0]
    assert review["history_status"] == "completed"
    assert review["analysis_status"] == "no_data"
    assert review["history"]["metrics"]["approval_rate"] is None
    assert review["history"]["daily"] == []
    assert review["conclusions"] is None
    assert result["accepted_alerts"] == [alert_payload()]
    analyst.analyze.assert_not_called()


def test_missing_gemini_configuration_does_not_hide_history():
    agent = GuardianAgent(FilterPolicy(), FakeReader())
    result = agent.review_alerts([alert_payload()])
    review = result["reviews"][0]
    assert review["history_status"] == "completed"
    assert review["analysis_status"] == "unavailable"
    assert review["analysis_error"]["code"] == "AI_NOT_CONFIGURED"
    assert review["conclusions"] is None


def test_merchant_investigation_uses_historical_analyst_without_an_alert():
    agent, alert_analyst, history_analyst = agent_with()
    result = agent.investigate_merchant(QUERY)
    assert agent.reader.calls == [QUERY]
    assert result["agent"] == "kipu-guardian"
    assert result["query"]["merchant_code"] == QUERY.merchant_code
    assert result["analysis_status"] == "completed"
    assert result["conclusions"] == CONCLUSIONS
    history_analyst.analyze.assert_called_once()
    assert len(history_analyst.analyze.call_args.args) == 1
    assert history_analyst.analyze.call_args.args[0]["data_quality"]["requested_days"] == 7
    alert_analyst.analyze.assert_not_called()


def test_merchant_investigation_retains_report_if_gemini_is_unavailable():
    agent, _, _ = agent_with(history_error=GeminiReviewError("RATE_LIMITED"))
    result = agent.investigate_merchant(QUERY)
    assert result["metrics"]["total_transactions"] == 200
    assert result["analysis_status"] == "unavailable"
    assert result["analysis_error"]["code"] == "RATE_LIMITED"
    assert result["conclusions"] is None


@pytest.mark.parametrize(
    "query",
    [
        HistoryQuery("different-mid", QUERY.date_from, QUERY.date_to),
        HistoryQuery(QUERY.merchant_code, date(2026, 8, 2), QUERY.date_to),
        HistoryQuery(QUERY.merchant_code, QUERY.date_from, date(2026, 8, 8)),
    ],
)
def test_saved_reader_rejects_wrong_mid_or_range_and_never_falls_back(query, monkeypatch):
    aws_read = Mock(side_effect=AssertionError("Replay must never fall back to Athena"))
    monkeypatch.setattr(guardian.AthenaHistoryReader, "read", aws_read)
    reader = SavedHistoryReader(history_report())
    with pytest.raises(HistoryError) as caught:
        reader.read(query)
    assert caught.value.code == "INVALID_HISTORY_SOURCE"
    aws_read.assert_not_called()


@pytest.mark.parametrize("empty", [False, True])
def test_old_history_schema_never_replays_or_falls_back_to_athena(monkeypatch, empty):
    saved = history_report(empty=empty)
    saved["schema_version"] = "1.0"
    original = deepcopy(saved)
    # Nonempty legacy aggregates also satisfy A + D = total. That does not prove
    # which transaction identity or query contract produced the numbers.
    assert saved["metrics"]["other_transactions"] == 0
    assert saved["metrics"]["approved_transactions"] + saved["metrics"][
        "declined_transactions"
    ] == saved["metrics"]["total_transactions"]
    aws_read = Mock(side_effect=AssertionError("Do not silently replace old evidence"))
    monkeypatch.setattr(guardian.AthenaHistoryReader, "read", aws_read)
    agent, alert_analyst, history_analyst = agent_with(SavedHistoryReader(saved))

    with pytest.raises(HistoryError) as caught:
        agent.investigate_merchant(QUERY)

    assert caught.value.code == "INVALID_HISTORY_SOURCE"
    assert saved == original
    aws_read.assert_not_called()
    alert_analyst.analyze.assert_not_called()
    history_analyst.analyze.assert_not_called()


def test_legacy_live_reader_response_is_rejected_before_ai_without_changing_policy():
    saved = history_report()
    saved["schema_version"] = "1.0"
    reader = FakeReader(report=saved)
    agent, alert_analyst, history_analyst = agent_with(reader)
    raw = alert_payload()
    expected = PayloadAlertFilter(agent.policy).evaluate(raw)

    result = agent.review_alerts([raw])

    assert reader.calls == [QUERY]
    review = result["reviews"][0]
    assert review["history_status"] == review["analysis_status"] == "unavailable"
    assert review["history"] is None
    assert review["conclusions"] is None
    assert review["analysis_error"]["code"] == "INVALID_HISTORY_SOURCE"
    assert review["policy"] == expected.model_dump(mode="json", exclude={"alert"})
    assert result["accepted_alerts"] == [raw]
    alert_analyst.analyze.assert_not_called()
    history_analyst.analyze.assert_not_called()


def test_saved_report_and_replayed_results_are_independent():
    saved = history_report()
    saved["conclusions"] = {"summary": "Old conclusions must not be reused."}
    original = deepcopy(saved)
    reader = SavedHistoryReader(saved)
    agent, _, _ = agent_with(reader)
    result = agent.investigate_merchant(QUERY)
    assert saved == original
    assert result["conclusions"] == CONCLUSIONS
    saved["daily"][0]["total_transactions"] = 999
    result["source"]["query_execution_id"] = "mutated-by-consumer"
    result["daily"][0]["total_transactions"] = 777
    replayed = reader.read(QUERY)
    assert replayed["source"]["query_execution_id"] == "synthetic-query"
    assert replayed["daily"][0]["total_transactions"] == 100


@pytest.mark.parametrize("field", ["metrics", "comparison", "daily"])
def test_validate_history_recomputes_all_aggregates(field):
    report = history_report()
    if field == "metrics":
        report[field]["approval_rate"] = 1.0
    elif field == "comparison":
        report[field]["change_percentage_points"] = 10
    else:
        report[field][0]["total_transactions"] = True
    with pytest.raises(HistoryError) as caught:
        validate_history_report(report, QUERY)
    assert caught.value.code == "INVALID_HISTORY_SOURCE"


@pytest.mark.parametrize(
    "arguments",
    [
        {"with_history": "false"},
        {"lookback_days": True},
        {"lookback_days": 0},
        {"lookback_days": 32},
        {"max_history_queries": False},
        {"max_history_queries": 0},
        {"max_history_queries": 21},
        {"history_timestamp_field": "extracted_at"},
        {"history_timestamp_field": None},
    ],
)
def test_invalid_controls_are_rejected_before_any_external_calls(arguments):
    agent, analyst, _ = agent_with()
    with pytest.raises(ValueError):
        agent.review_alerts([alert_payload()], **arguments)
    assert agent.reader.calls == []
    analyst.analyze.assert_not_called()


@pytest.mark.parametrize("alerts", [None, {}, [None], [alert_payload()] * 51])
def test_invalid_or_oversized_batches_are_rejected_before_external_calls(alerts):
    agent, analyst, _ = agent_with()
    with pytest.raises(ValueError):
        agent.review_alerts(alerts)
    assert agent.reader.calls == []
    analyst.analyze.assert_not_called()


def test_empty_batch_has_no_queries():
    agent, analyst, _ = agent_with()
    result = agent.review_alerts([])
    assert result["accepted_alerts"] == result["reviews"] == []
    assert result["history_query_count"] == 0
    analyst.analyze.assert_not_called()


def test_manual_replay_uses_original_kipu_generation_not_replay_timestamp():
    agent, _, _ = agent_with()
    alert = alert_payload(
        timestamp="2026-09-07T12:00:00Z",
        kipu_generated_at="2026-08-10T02:00:00Z",
    )
    result = agent.review_alerts([alert], history_timestamp_field="kipu_generated_at")
    assert agent.reader.calls == [HistoryQuery("synthetic-mid", date(2026, 8, 2), date(2026, 8, 8))]
    review = result["reviews"][0]
    assert review["history_anchor_field"] == "kipu_generated_at"
    assert review["history_anchor_at"] == "2026-08-10T02:00:00+00:00"
    assert result["accepted_alerts"] == [alert]


@pytest.mark.parametrize(
    "original_time", [None, "", "invalid", "2026-08-10T02:00:00", "2026-09-08T02:00:00Z"]
)
def test_manual_replay_invalid_kipu_time_skips_without_timestamp_fallback(original_time):
    agent, analyst, _ = agent_with()
    alert = alert_payload(kipu_generated_at=original_time)
    result = agent.review_alerts([alert], history_timestamp_field="kipu_generated_at")
    review = result["reviews"][0]
    assert review["history_status"] == "skipped"
    assert review["analysis_error"]["code"] == "INVALID_ALERT_FOR_HISTORY"
    assert review["history_anchor_field"] == "kipu_generated_at"
    assert "history_anchor_at" not in review
    assert agent.reader.calls == []
    analyst.analyze.assert_not_called()


def test_manual_replay_missing_kipu_time_does_not_use_valid_timestamp():
    agent, analyst, _ = agent_with()
    result = agent.review_alerts([alert_payload()], history_timestamp_field="kipu_generated_at")
    assert result["reviews"][0]["history_status"] == "skipped"
    assert result["history_query_count"] == 0
    assert agent.reader.calls == []
    analyst.analyze.assert_not_called()
