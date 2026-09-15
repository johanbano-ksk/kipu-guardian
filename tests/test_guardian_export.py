import importlib.util
import json
from pathlib import Path
from unittest.mock import Mock

import pytest

from alert_reviewer.guardian import GuardianAgent

SPEC = importlib.util.spec_from_file_location(
    "guardian_manual_export", Path(__file__).parents[1] / "scripts/export_manual_review_snapshot.py"
)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


def setup(monkeypatch, tmp_path, *, status="completed", history=True):
    source = tmp_path / "source.json"
    source.write_text(
        json.dumps(
            [
                {
                    "date": "2026-08-01",
                    "notified_at": "2026-08-01-12:00",
                    "criticality": "Critica",
                    "merchant_code": "synthetic-mid",
                    "merchant_name": "Synthetic",
                    "country": "Ecuador",
                    "approval_rate": 0.1,
                    "rolling_avg_approval_rate": 0.5,
                    "total_transactions": 100,
                    "declined_count": 90,
                }
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "accepted.json"
    argv = ["export", "2026-08-01", "--source", str(source), "--output", str(output)]
    if history:
        argv.append("--guardian-history")
    monkeypatch.setattr("sys.argv", argv)
    mocked = Mock()

    def review(alerts, **_kwargs):
        return {
            "accepted_alerts": alerts,
            "reviews": [{"analysis_status": status}],
            "history_query_count": 1,
        }

    mocked.review_alerts.side_effect = review
    factory = Mock(return_value=mocked)
    monkeypatch.setattr(GuardianAgent, "configured", factory)
    return output, mocked, factory, argv


def test_existing_export_default_does_not_invoke_history(monkeypatch, tmp_path):
    output, _agent, factory, _argv = setup(monkeypatch, tmp_path, history=False)
    assert cli.main() == 0
    assert len(json.loads(output.read_text(encoding="utf-8"))) == 1
    assert not output.with_suffix(".guardian.json").exists()
    factory.assert_not_called()


@pytest.mark.parametrize("status,exit_code", [("completed", 0), ("unavailable", 1)])
def test_export_adds_separate_guardian_report_and_real_kipu_anchor(
    monkeypatch, tmp_path, status, exit_code
):
    output, agent, _factory, _argv = setup(monkeypatch, tmp_path, status=status)
    assert cli.main() == exit_code
    accepted = json.loads(output.read_text(encoding="utf-8"))
    report = json.loads(output.with_suffix(".guardian.json").read_text(encoding="utf-8"))
    assert report["accepted_alerts"] == accepted
    assert accepted[0]["kipu_generated_at"] == "2026-08-01T12:00:00+00:00"
    assert agent.review_alerts.call_args.kwargs == {
        "lookback_days": 7,
        "max_history_queries": 5,
        "history_timestamp_field": "kipu_generated_at",
    }


def test_export_rejects_overlapping_output_before_gemini(monkeypatch, tmp_path):
    output, _agent, factory, argv = setup(monkeypatch, tmp_path)
    argv.extend(["--guardian-output", str(output)])
    with pytest.raises(ValueError, match="separado"):
        cli.main()
    assert not output.exists()
    factory.assert_not_called()
