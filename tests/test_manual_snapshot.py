import json
from datetime import UTC, date, datetime

import pytest

from alert_reviewer.alert_filter import FilterPolicy
from alert_reviewer.manual_snapshot import (
    filter_occurrence_snapshot,
    filter_snapshot,
    project_eventbridge_v1,
)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "date": "2026-08-27",
        "merchant_code": "20000000100043813000",
        "merchant_name": "BORDER CREDIT",
        "country": "Mexico",
        "criticality": "Critica",
        "cluster_profile": "Bajo Vol, Baja TA",
        "approval_rate": 0.02,
        "rolling_avg_approval_rate": 0.15,
        "total_transactions": 115,
        "declined_count": 112,
        "batch_id": "20260827_test",
        "notified_at": "2026-08-27-16:38",
    }
    row.update(overrides)
    return row


def test_filter_snapshot_preserves_mid_and_accepts_supported_alert() -> None:
    timestamp = datetime(2026, 8, 27, 12, tzinfo=UTC)

    accepted, projected_count, country_summary = filter_snapshot(
        [_row()],
        date(2026, 8, 27),
        policy=FilterPolicy(),
        replay_timestamp=timestamp,
    )

    assert projected_count == 1
    assert len(accepted) == 1
    assert accepted[0]["merchant_code"] == "20000000100043813000"
    assert accepted[0]["timestamp"] == timestamp.isoformat()
    assert accepted[0]["kipu_generated_at"] == "2026-08-27T16:38:00+00:00"
    assert country_summary == [
        {"country": "Mexico", "received_count": 1, "accepted_count": 1}
    ]


def test_filter_snapshot_returns_only_accepted_alerts() -> None:
    accepted, projected_count, country_summary = filter_snapshot(
        [
            _row(),
            _row(
                merchant_code="small",
                country="Colombia",
                total_transactions=10,
                declined_count=9,
            ),
        ],
        date(2026, 8, 27),
        policy=FilterPolicy(),
    )

    assert projected_count == 2
    assert [alert["merchant_code"] for alert in accepted] == ["20000000100043813000"]
    assert country_summary == [
        {"country": "Colombia", "received_count": 1, "accepted_count": 0},
        {"country": "Mexico", "received_count": 1, "accepted_count": 1},
    ]


def test_ai_selector_can_reject_but_cannot_broaden_policy() -> None:
    accepted, projected_count, country_summary = filter_snapshot(
        [_row(), _row(merchant_code="small", total_transactions=10, declined_count=9)],
        date(2026, 8, 27),
        policy=FilterPolicy(),
        ai_selector=lambda _alerts: {1},
    )

    assert projected_count == 2
    assert accepted == []
    assert sum(item["received_count"] for item in country_summary) == 2
    assert sum(item["accepted_count"] for item in country_summary) == 0


def test_project_eventbridge_v1_rejects_mixed_snapshot_dates() -> None:
    with pytest.raises(ValueError, match="outside 2026-08-27"):
        project_eventbridge_v1([_row(date="2026-08-26")], date(2026, 8, 27))


def test_project_eventbridge_v1_does_not_invent_missing_kipu_generation_time() -> None:
    projected = project_eventbridge_v1(
        [_row(notified_at="")],
        date(2026, 8, 27),
        replay_timestamp=datetime(2026, 8, 27, 18, tzinfo=UTC),
    )

    assert "kipu_generated_at" not in projected[0]


def test_project_eventbridge_v1_rejects_invalid_kipu_generation_time() -> None:
    with pytest.raises(ValueError, match="notified_at"):
        project_eventbridge_v1(
            [_row(notified_at="2026-08-27 16:38")],
            date(2026, 8, 27),
        )


def _occurrence(alert: dict[str, object], **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        "idempotency_key": "occurrence#event:event-001",
        "record_type": "ALERT_OCCURRENCE",
        "event_source": "acceptance.kipu",
        "detail_type": "Anomaly Detected v1",
        "schema_version": "1.0",
        "alert_id": alert["alert_id"],
        "publication_timestamp": "2026-08-28T00:30:01Z",
        "recorded_at": "2026-08-28T00:30:02Z",
        "raw_payload": json.dumps(alert),
    }
    record.update(overrides)
    return record


def test_occurrence_snapshot_uses_eventbridge_day_and_kipu_timestamp() -> None:
    alert = project_eventbridge_v1(
        [_row()],
        date(2026, 8, 27),
        replay_timestamp=datetime(2026, 8, 28, 0, 30, tzinfo=UTC),
    )[0]

    accepted, evaluated_count, source_count, country_summary = filter_occurrence_snapshot(
        [_occurrence(alert)],
        date(2026, 8, 27),
        policy=FilterPolicy(),
    )

    assert source_count == 1
    assert evaluated_count == 1
    assert accepted[0]["kipu_generated_at"] == alert["timestamp"]
    assert country_summary == [
        {"country": "Mexico", "received_count": 1, "accepted_count": 1}
    ]


def test_occurrence_snapshot_keeps_latest_occurrence_per_alert_id() -> None:
    first = project_eventbridge_v1(
        [_row()],
        date(2026, 8, 27),
        replay_timestamp=datetime(2026, 8, 27, 12, tzinfo=UTC),
    )[0]
    latest = {**first, "timestamp": "2026-08-27T13:00:00+00:00"}

    accepted, evaluated_count, source_count, country_summary = filter_occurrence_snapshot(
        [
            _occurrence(first, publication_timestamp="2026-08-27T12:00:01Z"),
            _occurrence(
                latest,
                idempotency_key="occurrence#event:event-002",
                publication_timestamp="2026-08-27T13:00:01Z",
            ),
        ],
        date(2026, 8, 27),
        policy=FilterPolicy(),
    )

    assert source_count == 2
    assert evaluated_count == 1
    assert accepted[0]["kipu_generated_at"] == latest["timestamp"]
    assert country_summary == [
        {"country": "Mexico", "received_count": 2, "accepted_count": 1}
    ]
