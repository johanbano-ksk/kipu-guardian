"""Query one merchant's transaction history and optionally request Gemini conclusions."""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

from alert_reviewer.ai_review import AIReviewError
from alert_reviewer.datalake_history import (
    AthenaHistoryConfig,
    AthenaHistoryReader,
    HistoryError,
    HistoryQuery,
    historical_evidence,
)
from alert_reviewer.gemini_review import GeminiHistoricalAnalyst, GeminiReviewConfig
from alert_reviewer.guardian import GuardianSettings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("merchant_code", nargs="?")
    parser.add_argument("--from", dest="date_from", type=date.fromisoformat)
    parser.add_argument("--to", dest="date_to", type=date.fromisoformat)
    parser.add_argument(
        "--source", type=Path, help="Previously saved report; does not query Athena"
    )
    parser.add_argument("--analyze", action="store_true")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    settings = GuardianSettings()
    try:
        if args.source:
            report = json.loads(args.source.read_text(encoding="utf-8"))
            historical_evidence(report)  # Shape must be usable before asking the model.
        else:
            if not args.merchant_code or not args.date_from or not args.date_to:
                parser.error("MID, --from and --to are required unless --source is given")
            report = AthenaHistoryReader(
                AthenaHistoryConfig(
                    profile=settings.history_aws_profile,
                    workgroup=settings.history_athena_workgroup,
                    approved_statuses=tuple(settings.history_approved_statuses.split(",")),
                    declined_statuses=tuple(settings.history_declined_statuses.split(",")),
                )
            ).read(HistoryQuery(args.merchant_code, args.date_from, args.date_to))
        report.update(conclusions=None, analysis_status="not_requested", analysis_error=None)
        if args.analyze:
            if report["metrics"]["total_transactions"] == 0:
                report["analysis_status"] = "no_data"
            else:
                try:
                    analyst = GeminiHistoricalAnalyst(
                        GeminiReviewConfig(
                            api_key=settings.gemini_api_key.get_secret_value(),
                            model=settings.gemini_model,
                            timeout_seconds=settings.gemini_timeout_seconds,
                        )
                    )
                    report["conclusions"] = analyst.analyze(historical_evidence(report))
                    report["analysis_status"] = "completed"
                except (AIReviewError, ValueError) as error:
                    report["analysis_status"] = "unavailable"
                    report["analysis_error"] = {
                        "code": getattr(error, "code", "AI_NOT_CONFIGURED"),
                        "message": "Gemini no completó el análisis.",
                    }
        serialized = json.dumps(report, indent=2, ensure_ascii=False)
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(serialized + "\n", encoding="utf-8")
            print(
                json.dumps(
                    {
                        "output": str(args.output),
                        "analysis_status": report["analysis_status"],
                        "metrics": report["metrics"],
                        "analysis_error": report["analysis_error"],
                    }
                )
            )
        else:
            print(serialized)
        return int(report["analysis_status"] == "unavailable")
    except (HistoryError, ValueError, OSError, KeyError, TypeError) as error:
        print(
            json.dumps(
                {
                    "code": getattr(error, "code", "INVALID_REQUEST_OR_REPORT"),
                    "error": "No se pudo completar el histórico. Revisa parámetros y conexión.",
                }
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
