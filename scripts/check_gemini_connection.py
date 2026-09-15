"""Probe Gemini with synthetic data only; never prints provider credentials."""

from __future__ import annotations

import json

from alert_reviewer.ai_review import AIReviewError
from alert_reviewer.datalake_history import HistoryQuery, historical_evidence, summarize_rows
from alert_reviewer.gemini_review import GeminiHistoricalAnalyst, GeminiReviewConfig
from alert_reviewer.guardian import GuardianSettings


def main() -> int:
    from datetime import date

    settings = GuardianSettings()
    if not settings.gemini_api_key.get_secret_value():
        print(json.dumps({"status": "not_configured"}))
        return 1
    query = HistoryQuery("synthetic", date(2026, 1, 1), date(2026, 1, 2))
    report = summarize_rows(
        [
            {
                "business_date": "2026-01-01",
                "total_transactions": "100",
                "approved_transactions": "90",
                "declined_transactions": "10",
                "other_transactions": "0",
            },
            {
                "business_date": "2026-01-02",
                "total_transactions": "100",
                "approved_transactions": "60",
                "declined_transactions": "40",
                "other_transactions": "0",
            },
        ],
        query,
        {},
    )
    analyst = GeminiHistoricalAnalyst(
        GeminiReviewConfig(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model,
            timeout_seconds=settings.gemini_timeout_seconds,
        )
    )
    try:
        result = analyst.analyze(historical_evidence(report))
    except AIReviewError as error:
        print(json.dumps({"status": "failed", "code": getattr(error, "code", "AI_REVIEW_FAILED")}))
        return 1
    print(
        json.dumps(
            {
                "status": "ok",
                "model": settings.gemini_model,
                "source": "synthetic_only",
                "result": result,
            },
            ensure_ascii=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
