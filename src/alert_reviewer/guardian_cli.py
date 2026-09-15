"""Direct Guardian entry point: review alerts or investigate a merchant's history."""

from __future__ import annotations

import argparse
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alert_reviewer.alert_filter import FilterPolicy, extract_alerts
from alert_reviewer.config import get_settings
from alert_reviewer.datalake_history import TIMEZONE, HistoryError, HistoryQuery
from alert_reviewer.gemini_review import _decode_json
from alert_reviewer.guardian import GuardianAgent, SavedHistoryReader


def read_json(path: Path) -> Any:
    with path.open("rb") as stream:
        data = stream.read(2_000_001)
    if len(data) > 2_000_000:
        raise ValueError("Input exceeds 2 MB")
    return _decode_json(data)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    review = commands.add_parser("review", help="Revisar alertas Kipu con contexto histórico")
    review.add_argument("--source", type=Path, required=True, help="JSON Kipu o envelope")
    review.add_argument("--policy-only", action="store_true", help="No usar Athena ni Gemini")
    review.add_argument("--history-days", type=int, default=7)
    review.add_argument("--max-history-queries", type=int, default=5)
    review.add_argument("--history-source", type=Path, help="Reusar histórico sin consultar AWS")
    history = commands.add_parser("history", help="Investigar un MID desde Guardian")
    history.add_argument("merchant_code")
    history.add_argument("--days", type=int, default=7)
    history.add_argument("--end", type=date.fromisoformat, help="Último día inclusive")
    history.add_argument("--source", type=Path, help="Reusar histórico sin consultar AWS")
    for command in (review, history):
        command.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        saved = args.history_source if args.command == "review" else args.source
        if any(path and path.resolve() == args.output.resolve() for path in (args.source, saved)):
            raise ValueError("Keep source evidence separate from the Guardian output")
        reader = SavedHistoryReader(read_json(saved)) if saved else None
        if args.command == "review" and args.policy_only:
            agent = GuardianAgent(FilterPolicy.load(get_settings().filter_policy_path), reader=None)
        else:
            agent = GuardianAgent.configured(reader=reader)
        if args.command == "review":
            payload = read_json(args.source)
            alerts = extract_alerts(payload)
            if not alerts and payload not in ([], {"alerts": []}):
                raise ValueError("No Kipu alert found")
            result = agent.review_alerts(
                alerts,
                with_history=not args.policy_only,
                lookback_days=args.history_days,
                max_history_queries=args.max_history_queries,
            )
            incomplete = any(
                r["analysis_status"] in {"unavailable", "skipped"} for r in result["reviews"]
            )
        else:
            if not 1 <= args.days <= 31:
                raise ValueError("History days must be between 1 and 31")
            end = args.end or (datetime.now(ZoneInfo(TIMEZONE)).date() - timedelta(days=1))
            query = HistoryQuery(args.merchant_code, end - timedelta(days=args.days - 1), end)
            result = agent.investigate_merchant(query)
            incomplete = result["analysis_status"] == "unavailable"
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
            encoding="utf-8",
        )
        print(
            json.dumps(
                {
                    "agent": "kipu-guardian",
                    "command": args.command,
                    "status": "partial" if incomplete else "completed",
                    "output": str(args.output),
                }
            )
        )
        return int(incomplete)
    except (HistoryError, ValueError, TypeError, OSError, RecursionError, OverflowError) as error:
        print(
            json.dumps(
                {
                    "code": getattr(error, "code", "INVALID_REQUEST_OR_SOURCE"),
                    "error": "Guardian no completó la solicitud. Revisa entrada y configuración.",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
