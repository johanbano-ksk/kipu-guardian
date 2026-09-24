from __future__ import annotations

import json
from pathlib import Path

from alert_reviewer.slack_bot import (
    SlackBotSettings,
    SlackEventStore,
    _is_kipu_message,
    format_slack_reply,
    parse_slack_kipu_alert,
)


def _event(text: str) -> dict:
    return {
        "type": "message",
        "subtype": "bot_message",
        "channel": "C-KIPU",
        "ts": "1720000000.000001",
        "username": "fraud-reporting",
        "text": text,
    }


def _alert() -> dict:
    return {
        "schema_version": "1.0",
        "alert_id": "alert-1",
        "merchant_code": "20000000100000000000",
        "merchant_name": "Example Merchant",
        "country": "Ecuador",
        "criticality": "Critica",
        "approval_rate": 0.2,
        "rolling_avg_approval_rate": 0.8,
        "total_transactions": 100,
        "declined_count": 80,
        "timestamp": "2026-09-24T07:40:12Z",
    }


def test_parser_extracts_json_detail_from_slack_code_block():
    payload = {"detail": _alert()}
    parsed = parse_slack_kipu_alert(_event(f"Nueva alerta\n```json\n{json.dumps(payload)}\n```"))

    assert parsed is not None
    assert parsed["alert_id"] == "alert-1"
    assert parsed["approval_rate"] == 0.2


def test_parser_rejects_incomplete_message():
    assert parse_slack_kipu_alert(_event("Alerta sin payload estructurado")) is None


def test_slack_filter_only_accepts_configured_kipu_bot_and_channel():
    settings = SlackBotSettings(
        _env_file=None,
        slack_alert_channel_id="C-KIPU",
        slack_alert_bot_name="fraud-reporting",
    )
    assert _is_kipu_message(_event("{}"), settings)
    assert not _is_kipu_message({**_event("{}"), "channel": "C-OTHER"}, settings)
    assert not _is_kipu_message({**_event("{}"), "username": "someone-else"}, settings)


def test_slack_event_store_deduplicates(tmp_path: Path):
    store = SlackEventStore(tmp_path / "events.json")
    assert not store.is_processed("event-1")
    store.mark_processed("event-1", {"alert_id": "alert-1", "verdict": "confirmed"})
    assert store.is_processed("event-1")


def test_slack_reply_exposes_only_operational_result():
    reply = format_slack_reply(_alert(), {"label": "Correcta", "reason": "Deterioro material."})

    assert "Correcta" in reply
    assert "20.0%" in reply
    assert "alert-1" not in reply
    assert "merchant_code" not in reply
