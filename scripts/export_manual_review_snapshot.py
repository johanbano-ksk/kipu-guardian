"""Export today's Kipu alerts that pass the payload-only critical policy.

The source is Kipu's daily ``alerts.json`` object in S3. It retains Kipu's
minute-level ``notified_at`` value but not the EventBridge-generated alert id
or exact publication timestamp, so this script creates a local v1-compatible
replay and preserves the Kipu time separately as ``kipu_generated_at``. It
never consumes SQS or publishes an event.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

import boto3

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from alert_reviewer.manual_snapshot import project_eventbridge_v1  # noqa: E402

FILTER_CLI = (
    PROJECT_ROOT / ".codex" / "skills" / "filter-valid-alerts" / "scripts" / "filter_alerts.py"
)
DEFAULT_BUCKET = "pmt-intel-kipu-data-development"
DEFAULT_PROFILE = "ia-dev-payments-intelligence"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Read a Kipu daily snapshot, replay its EventBridge v1 details "
            "locally, and retain only policy-approved critical alerts."
        )
    )
    parser.add_argument("day", type=date.fromisoformat, help="Day in YYYY-MM-DD")
    parser.add_argument("--profile", default=DEFAULT_PROFILE)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument(
        "--source",
        type=Path,
        help="Read alerts.json locally instead of downloading it from S3.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Accepted-only JSON destination (defaults under reports/).",
    )
    parser.add_argument(
        "--guardian-history",
        action="store_true",
        help="Guardian consulta históricos por MID y añade conclusiones de Gemini en un sidecar.",
    )
    parser.add_argument("--history-days", type=int, choices=range(1, 32), default=7)
    parser.add_argument("--history-max-queries", type=int, choices=range(1, 21), default=5)
    parser.add_argument(
        "--guardian-output", type=Path, help="Reporte de Guardian separado del filtro"
    )
    return parser.parse_args()


def s3_key(day: date) -> str:
    return f"alerts/year={day:%Y}/month={day:%m}/day={day:%d}/alerts.json"


def read_source(args: argparse.Namespace) -> list[dict[str, Any]]:
    if args.source:
        raw = args.source.read_text(encoding="utf-8")
    else:
        session = boto3.Session(profile_name=args.profile)
        response = session.client("s3").get_object(
            Bucket=args.bucket,
            Key=s3_key(args.day),
        )
        raw = response["Body"].read().decode("utf-8")

    payload = json.loads(raw)
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("Kipu alerts.json must contain an array of objects")
    return payload


def run_filter(projected: list[dict[str, Any]]) -> tuple[str, list[Any]]:
    result = subprocess.run(
        [sys.executable, str(FILTER_CLI), "-"],
        cwd=PROJECT_ROOT,
        input=json.dumps(projected, ensure_ascii=False),
        text=True,
        capture_output=True,
        check=True,
    )
    accepted = json.loads(result.stdout)
    if not isinstance(accepted, list):
        raise ValueError("The filter skill must return a JSON array")

    projected_values = {json.dumps(item, ensure_ascii=False, sort_keys=True) for item in projected}
    if any(
        json.dumps(item, ensure_ascii=False, sort_keys=True) not in projected_values
        for item in accepted
    ):
        raise ValueError("The filter modified or invented an accepted payload")
    return result.stdout, accepted


def main() -> int:
    args = parse_args()
    output = args.output or (
        PROJECT_ROOT
        / "reports"
        / f"critical-alerts-{args.day.isoformat()}-policy-2026-08-13.1.json"
    )
    rows = read_source(args)
    projected = project_eventbridge_v1(rows, args.day)
    guardian_output = args.guardian_output or output.with_suffix(".guardian.json")
    if args.guardian_history:
        if len(projected) > 50:
            raise ValueError("Guardian admite hasta 50 alertas por lote; usa un --source acotado.")
        if guardian_output.resolve() == output.resolve() or (
            args.source and guardian_output.resolve() == args.source.resolve()
        ):
            raise ValueError("El reporte Guardian debe estar separado de las entradas y alertas.")
    rendered, accepted = run_filter(projected)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(rendered, encoding="utf-8")
    guardian_summary = {}
    incomplete = False
    if args.guardian_history:
        from alert_reviewer.guardian import GuardianAgent

        guardian_report = GuardianAgent.configured().review_alerts(
            projected,
            lookback_days=args.history_days,
            max_history_queries=args.history_max_queries,
            history_timestamp_field="kipu_generated_at",
        )
        if guardian_report["accepted_alerts"] != accepted:
            raise ValueError("La política cambió durante la ejecución; vuelve a ejecutar.")
        guardian_output.parent.mkdir(parents=True, exist_ok=True)
        guardian_output.write_text(
            json.dumps(guardian_report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        incomplete = any(
            r["analysis_status"] in {"skipped", "unavailable"} for r in guardian_report["reviews"]
        )
        guardian_summary = {
            "guardian_output": str(guardian_output.resolve()),
            "guardian_status": "partial" if incomplete else "completed",
            "history_query_count": guardian_report["history_query_count"],
        }
    print(
        json.dumps(
            {
                "snapshot_records": len(rows),
                "eventbridge_v1_replay_records": len(projected),
                "accepted_for_manual_review": len(accepted),
                "output": str(output.resolve()),
                **guardian_summary,
            },
            ensure_ascii=False,
        )
    )
    return int(incomplete)


if __name__ == "__main__":
    raise SystemExit(main())
