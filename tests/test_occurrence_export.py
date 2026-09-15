from __future__ import annotations

import copy
import hashlib
import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest
from boto3.dynamodb.types import TypeSerializer

from alert_reviewer.alert_filter import FilterPolicy
from alert_reviewer.occurrence_export import (
    build_daily_export,
    load_dynamodb_occurrences,
    load_local_occurrences,
    parse_occurrence_document,
    write_daily_export,
)

ROOT = Path(__file__).parents[1]
POLICY_PATH = ROOT / "config" / "filter_policy.yaml"


def _alta(raw_v2: dict) -> dict:
    detail = copy.deepcopy(raw_v2)
    detail.update(
        {
            "criticality": "Alta",
            "approval_rate": 0.60,
            "approved_count": 60,
            "declined_count": 38,
            "unknown_status_count": 2,
            "total_transactions": 100,
            "rolling_avg_approval_rate": 0.70,
            "historical_avg_approval_rate": 0.72,
            "total_approved_amount": 0.0,
            "signal_codes": ["LOW_APPROVAL_RATE", "APPROVAL_RATE_MAD_DROP"],
            "criteria_count": 2,
            "zscore_ta": -4.0,
            "zscore_rechazos": 0.0,
            "rolling_avg_rejections": 20.0,
            "rolling_q95_rejections": 50.0,
            "rejection_change_pct": 90.0,
            "is_anomaly_merchant": False,
            "is_anomaly_global": False,
            "isolation_forest_merchant_score": 0.1,
            "isolation_forest_global_score": 0.2,
            "is_volume_spike": False,
            "volume_ratio": None,
            "baseline_weekday_avg": None,
            "oldest_weekday_activity": None,
            "priority_score": 55.0,
            "priority_components": {
                "volume_score": 5.0,
                "approval_rate_score": 30.0,
                "anomaly_score": 20.0,
            },
        }
    )
    return detail


def _document(
    raw_alert: dict,
    *,
    event_id: str,
    event_time: str,
    recorded_at: str,
    observation_date: str | None = None,
    source: str = "acceptance.kipu",
    detail_type: str | None = None,
) -> dict:
    schema_version = str(raw_alert["schema_version"])
    alert_timestamp = str(
        raw_alert.get("published_at")
        if schema_version == "2.0"
        else raw_alert.get("timestamp")
    )
    raw_payload = json.dumps(
        raw_alert,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    document = {
        "idempotency_key": f"occurrence#event:{event_id}",
        "record_type": "ALERT_OCCURRENCE",
        "occurrence_schema_version": "1.0",
        "event_id": event_id,
        "event_source": source,
        "detail_type": detail_type
        or ("Anomaly Detected v2" if schema_version == "2.0" else "Anomaly Detected v1"),
        "schema_version": schema_version,
        "alert_id": raw_alert["alert_id"],
        "event_time": event_time,
        "alert_timestamp": alert_timestamp,
        "publication_timestamp": event_time,
        "recorded_at": recorded_at,
        "raw_payload": raw_payload,
        "raw_payload_sha256": hashlib.sha256(raw_payload.encode()).hexdigest(),
        "expires_at": 1_800_000_000,
    }
    if observation_date is not None:
        document["observation_date"] = observation_date
    return document


def _stored(raw_alert: dict, **kwargs):
    return parse_occurrence_document(
        _document(raw_alert, **kwargs),
        source=kwargs["event_id"],
    )


def test_parse_occurrence_preserves_payload_and_checks_checksum(v2_alert):
    raw_alert = v2_alert.model_dump(mode="json")
    document = _document(
        raw_alert,
        event_id="event-v2",
        event_time="2026-08-18T12:30:00Z",
        recorded_at="2026-08-18T12:31:00Z",
        observation_date="2026-08-18",
    )

    occurrence = parse_occurrence_document(document, source="fixture")

    assert occurrence.raw_alert == raw_alert
    assert occurrence.observation_date == date(2026, 8, 18)
    assert occurrence.publication_timestamp.isoformat() == "2026-08-18T12:30:00+00:00"

    document["raw_payload_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="checksum"):
        parse_occurrence_document(document, source="fixture")


class _FakeDynamoDB:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def scan(self, **kwargs):
        self.calls.append(kwargs)
        return self.pages.pop(0)


def _dynamodb_item(document: dict) -> dict:
    serializer = TypeSerializer()
    return {key: serializer.serialize(value) for key, value in document.items()}


def test_dynamodb_loader_scans_all_pages(v2_alert):
    raw_alert = v2_alert.model_dump(mode="json")
    first = _document(
        raw_alert,
        event_id="first",
        event_time="2026-08-18T12:30:00Z",
        recorded_at="2026-08-18T12:31:00Z",
        observation_date="2026-08-18",
    )
    second = _document(
        {**raw_alert, "alert_id": "second-alert"},
        event_id="second",
        event_time="2026-08-18T13:30:00Z",
        recorded_at="2026-08-18T13:31:00Z",
        observation_date="2026-08-18",
    )
    client = _FakeDynamoDB(
        [
            {
                "Items": [_dynamodb_item(first)],
                "LastEvaluatedKey": {"idempotency_key": {"S": "next"}},
            },
            {"Items": [_dynamodb_item(second)]},
        ]
    )

    occurrences = load_dynamodb_occurrences(client, table_name="reviewer-table")

    assert [item.alert_id for item in occurrences] == ["v2-alert-001", "second-alert"]
    assert len(client.calls) == 2
    assert "ExclusiveStartKey" not in client.calls[0]
    assert client.calls[1]["ExclusiveStartKey"] == {
        "idempotency_key": {"S": "next"}
    }
    assert client.calls[0]["FilterExpression"] == "#record_type = :occurrence"
    assert all(call["ConsistentRead"] is True for call in client.calls)


def test_local_loader_accepts_dynamodb_scan_json(tmp_path, v2_alert):
    document = _document(
        v2_alert.model_dump(mode="json"),
        event_id="local",
        event_time="2026-08-18T12:30:00Z",
        recorded_at="2026-08-18T12:31:00Z",
        observation_date="2026-08-18",
    )
    source = tmp_path / "scan.json"
    source.write_text(
        json.dumps({"Items": [_dynamodb_item(document)]}),
        encoding="utf-8",
    )

    occurrences = load_local_occurrences(source)

    assert len(occurrences) == 1
    assert occurrences[0].raw_alert == v2_alert.model_dump(mode="json")


def test_local_loader_accepts_one_decoded_occurrence_object(tmp_path, v2_alert):
    document = _document(
        v2_alert.model_dump(mode="json"),
        event_id="single",
        event_time="2026-08-18T12:30:00Z",
        recorded_at="2026-08-18T12:31:00Z",
        observation_date="2026-08-18",
    )
    source = tmp_path / "single.json"
    source.write_text(json.dumps(document), encoding="utf-8")

    occurrences = load_local_occurrences(source)

    assert len(occurrences) == 1
    assert occurrences[0].occurrence_key == "occurrence#event:single"


def test_local_loader_rejects_unknown_object_wrapper(tmp_path):
    source = tmp_path / "unknown.json"
    source.write_text(json.dumps({"alerts": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="unsupported record type"):
        load_local_occurrences(source)


def test_local_loader_rejects_non_array_items(tmp_path):
    source = tmp_path / "invalid-scan.json"
    source.write_text(json.dumps({"Items": {}}), encoding="utf-8")

    with pytest.raises(ValueError, match="Items must be an array"):
        load_local_occurrences(source)


def test_daily_export_uses_event_time_and_keeps_v1_v2_primary(alert, v2_alert, tmp_path):
    base_v2 = v2_alert.model_dump(mode="json")
    old_critical = {**base_v2, "alert_id": "shared-alert"}
    latest_alta = _alta({**base_v2, "alert_id": "shared-alert"})
    other_critical = {**base_v2, "alert_id": "valid-alert"}
    predictive_v1 = alert.model_dump(mode="json")
    predictive_v1["alert_id"] = "predictive-v1"
    transition_v1 = {**predictive_v1, "alert_id": "transition"}
    transition_v1["superseded_by_schema_version"] = "2.0"
    unsupported = {**predictive_v1, "schema_version": "legacy", "alert_id": "legacy"}

    occurrences = [
        _stored(
            old_critical,
            event_id="old",
            event_time="2026-08-19T01:00:00Z",
            recorded_at="2026-08-19T01:01:00Z",
            observation_date="2026-08-18",
        ),
        _stored(
            latest_alta,
            event_id="alta",
            event_time="2026-08-19T02:00:00Z",
            recorded_at="2026-08-19T02:01:00Z",
            observation_date="2026-08-18",
        ),
        _stored(
            other_critical,
            event_id="critical",
            event_time="2026-08-19T03:00:00Z",
            recorded_at="2026-08-19T03:01:00Z",
            observation_date="2026-08-18",
        ),
        _stored(
            predictive_v1,
            event_id="v1",
            event_time="2026-08-19T04:30:00Z",
            recorded_at="2026-08-19T04:31:00Z",
        ),
        _stored(
            transition_v1,
            event_id="transition",
            event_time="2026-08-19T04:40:00Z",
            recorded_at="2026-08-19T04:41:00Z",
        ),
        _stored(
            unsupported,
            event_id="legacy",
            event_time="2026-08-19T04:50:00Z",
            recorded_at="2026-08-19T04:51:00Z",
            detail_type="Legacy Alert",
        ),
    ]

    (
        all_alerts,
        unique_alerts,
        valid_alerts,
        valid_unique_alerts,
        summary,
    ) = build_daily_export(
        occurrences,
        day=date(2026, 8, 18),
        date_basis="publication",
        policy=FilterPolicy.load(POLICY_PATH),
    )

    assert all_alerts == [
        old_critical,
        latest_alta,
        other_critical,
        predictive_v1,
    ]
    assert unique_alerts == [
        latest_alta,
        other_critical,
        predictive_v1,
    ]
    assert valid_alerts == [old_critical, other_critical, predictive_v1]
    assert valid_unique_alerts == [old_critical, other_critical, predictive_v1]
    assert summary["export_schema_version"] == "1.1"
    assert summary["excluded_transition_count"] == 1
    assert summary["excluded_unsupported_count"] == 1
    assert summary["all_occurrence_count"] == 4
    assert summary["unique_alert_count"] == 3
    assert summary["valid_occurrence_count"] == 3
    assert summary["valid_unique_alert_count"] == 3

    paths = write_daily_export(
        tmp_path / "output",
        all_alerts=all_alerts,
        unique_alerts=unique_alerts,
        valid_alerts=valid_alerts,
        valid_unique_alerts=valid_unique_alerts,
        summary=summary,
    )
    assert json.loads(paths["all"].read_text(encoding="utf-8")) == all_alerts
    assert json.loads(paths["valid"].read_text(encoding="utf-8")) == valid_alerts
    assert json.loads(paths["valid_unique"].read_text(encoding="utf-8")) == (
        valid_unique_alerts
    )


def test_observation_basis_never_infers_v1_date(alert, v2_alert):
    v1 = _stored(
        alert.model_dump(mode="json"),
        event_id="v1",
        event_time="2026-08-19T04:30:00Z",
        recorded_at="2026-08-19T04:31:00Z",
    )
    v2 = _stored(
        v2_alert.model_dump(mode="json"),
        event_id="v2",
        event_time="2026-08-19T05:30:00Z",
        recorded_at="2026-08-19T05:31:00Z",
        observation_date="2026-08-18",
    )

    (
        all_alerts,
        unique_alerts,
        valid_alerts,
        valid_unique_alerts,
        summary,
    ) = build_daily_export(
        [v1, v2],
        day=date(2026, 8, 18),
        date_basis="observation",
        policy=FilterPolicy.load(POLICY_PATH),
    )

    assert all_alerts == [v2.raw_alert]
    assert unique_alerts == [v2.raw_alert]
    assert valid_alerts == [v2.raw_alert]
    assert valid_unique_alerts == [v2.raw_alert]
    assert summary["missing_observation_date_count"] == 1


def test_cli_local_source_never_requires_aws(tmp_path, v2_alert):
    document = _document(
        v2_alert.model_dump(mode="json"),
        event_id="cli",
        event_time="2026-08-18T12:30:00Z",
        recorded_at="2026-08-18T12:31:00Z",
        observation_date="2026-08-18",
    )
    source = tmp_path / "occurrences.json"
    source.write_text(json.dumps([document]), encoding="utf-8")
    output_dir = tmp_path / "export"

    completed = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "export_hourly_alert_audit.py"),
            "2026-08-18",
            "--source",
            str(source),
            "--output-dir",
            str(output_dir),
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    result = json.loads(completed.stdout)
    assert result["source_occurrence_count"] == 1
    assert result["valid_occurrence_count"] == 1
    assert result["valid_unique_alert_count"] == 1
    assert json.loads((output_dir / "valid.json").read_text(encoding="utf-8")) == [
        v2_alert.model_dump(mode="json")
    ]
    assert json.loads(
        (output_dir / "valid_unique.json").read_text(encoding="utf-8")
    ) == [v2_alert.model_dump(mode="json")]
