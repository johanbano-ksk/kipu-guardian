"""Kushki Guardian Dashboard — FastAPI backend.

Serves a web dashboard for reviewing Kipu alerts with deterministic verdicts
and optional Gemini explanations.
"""

from __future__ import annotations

import json
import logging
import os
import traceback
from datetime import date, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# Guardian imports
from alert_reviewer.alert_filter import FilterPolicy, extract_alerts
from alert_reviewer.config import get_settings
from alert_reviewer.datalake_history import TIMEZONE, HistoryQuery
from alert_reviewer.deterministic_verdict import (
    APPROVAL_DROP_THRESHOLD_PP,
    MIN_OBSERVED_DAYS,
    MIN_TOTAL_TRANSACTIONS,
    DeterministicVerdict,
    deterministic_fallback_explanation,
    evaluate_verdict,
    extract_evidence_numbers,
    validate_output_text,
)
from alert_reviewer.gemini_review import GeminiReviewConfig, GeminiReviewError
from alert_reviewer.guardian import GuardianAgent, GuardianSettings, SavedHistoryReader
from alert_reviewer.guardian_analysis import _alert_evidence
from alert_reviewer.guardian_assurance import (
    ANALYSIS_VERSION,
    GeminiAssuredAnalyst,
    build_comparison_context,
)
from alert_reviewer.guardian_history import guardian_historical_evidence, summarize_guardian_rows
from alert_reviewer.kipu_contract import KipuAlert

logger = logging.getLogger(__name__)

DASHBOARD_DIR = Path(__file__).resolve().parent
STATIC_DIR = DASHBOARD_DIR / "static"
REPORTS_DIR = DASHBOARD_DIR.parent / "reports"

app = FastAPI(
    title="Kushki Guardian",
    description="Dashboard de revisión de alertas con veredictos determinísticos",
    version=ANALYSIS_VERSION,
)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    """Serve the main dashboard page."""
    html_path = STATIC_DIR / "index.html"
    return HTMLResponse(content=html_path.read_text(encoding="utf-8"))


@app.get("/api/health")
async def health():
    """Health check."""
    settings = _load_settings()
    return {
        "status": "ok",
        "version": ANALYSIS_VERSION,
        "gemini_configured": bool(settings.get("gemini_api_key")),
        "thresholds": {
            "approval_drop_pp": APPROVAL_DROP_THRESHOLD_PP,
            "min_observed_days": MIN_OBSERVED_DAYS,
            "min_total_transactions": MIN_TOTAL_TRANSACTIONS,
        },
    }


@app.get("/api/examples")
async def list_examples():
    """List available example alert files."""
    examples_dir = DASHBOARD_DIR.parent / "examples"
    files = []
    if examples_dir.exists():
        for f in sorted(examples_dir.glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8"))
                detail = data.get("detail", data)
                files.append({
                    "filename": f.name,
                    "merchant_code": detail.get("merchant_code", "—"),
                    "criticality": detail.get("criticality", "—"),
                    "approval_rate": detail.get("approval_rate"),
                    "total_transactions": detail.get("total_transactions"),
                })
            except Exception:
                files.append({"filename": f.name, "error": "Could not parse"})
    return {"examples": files}


@app.get("/api/example/{filename}")
async def load_example(filename: str):
    """Load a specific example alert file."""
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "Invalid filename")
    path = DASHBOARD_DIR.parent / "examples" / filename
    if not path.exists() or not path.suffix == ".json":
        raise HTTPException(404, "Example not found")
    data = json.loads(path.read_bytes()[:2_000_000])
    return data


@app.post("/api/analyze")
async def analyze_alert(request: Request):
    """Analyze an alert with the deterministic verdict evaluator.

    Accepts either:
    - A raw Kipu EventBridge envelope (with `detail`)
    - A bare alert payload (with `schema_version`)
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    # Extract alert from envelope or bare payload
    alert = body.get("detail", body) if isinstance(body, dict) else body
    if not isinstance(alert, dict) or alert.get("schema_version") != "1.0":
        raise HTTPException(400, "Unsupported alert schema. Requires schema_version=1.0")

    # Build safe alert evidence
    try:
        safe_alert = _alert_evidence(alert, policy_accepted=True)
    except Exception as e:
        raise HTTPException(400, f"Invalid alert payload: {e}")

    # Extract key metrics for standalone deterministic analysis
    result = _standalone_deterministic(safe_alert, alert)
    return JSONResponse(result)


@app.post("/api/analyze-with-history")
async def analyze_with_history(request: Request):
    """Full analysis with historical data + optional Gemini explanation.

    Expects JSON: { "alert": {...}, "history": {...} }
    where history is the output of a previous Guardian evidence run.
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    alert_raw = body.get("alert", body.get("detail", body))
    history_raw = body.get("history")

    if not isinstance(alert_raw, dict) or alert_raw.get("schema_version") != "1.0":
        raise HTTPException(400, "Unsupported alert schema")

    if not history_raw:
        # If no history, run standalone deterministic
        try:
            safe_alert = _alert_evidence(alert_raw, policy_accepted=True)
        except Exception as e:
            raise HTTPException(400, f"Invalid alert: {e}")
        return JSONResponse(_standalone_deterministic(safe_alert, alert_raw))

    try:
        settings = GuardianSettings()
        config = GeminiReviewConfig(
            api_key=settings.gemini_api_key.get_secret_value(),
            model=settings.gemini_model,
            timeout=settings.gemini_timeout_seconds,
        )
        analyst = GeminiAssuredAnalyst(config)
        context = build_comparison_context(alert_raw, history_raw)
        analysis = analyst.analyze(alert_raw, history_raw, True, context)

        return JSONResponse({
            "status": "completed",
            "mode": "full_analysis",
            "analysis": analysis,
            "comparison_context": context,
        })
    except GeminiReviewError as e:
        return JSONResponse({
            "status": "error",
            "mode": "full_analysis",
            "error": str(e),
        }, status_code=422)
    except Exception as e:
        logger.exception("Analysis failed")
        return JSONResponse({
            "status": "error",
            "error": f"Analysis failed: {type(e).__name__}",
            "detail": traceback.format_exc()[-500:],
        }, status_code=500)


@app.get("/api/reports")
async def list_reports():
    """List saved Guardian evidence reports."""
    evidence_dir = REPORTS_DIR / "guardian-evidence"
    reports = []
    if evidence_dir.exists():
        for case_dir in sorted(evidence_dir.iterdir(), reverse=True):
            if case_dir.is_dir():
                meta_files = list(case_dir.glob("*-meta.json"))
                for mf in meta_files:
                    try:
                        meta = json.loads(mf.read_text(encoding="utf-8"))
                        reports.append({
                            "case_id": case_dir.name,
                            "filename": mf.name,
                            "path": str(mf),
                            "analysis_version": meta.get("analysis_version"),
                            "verdict": meta.get("verdict"),
                            "created_at": meta.get("created_at"),
                        })
                    except Exception:
                        pass
    return {"reports": reports, "count": len(reports)}


@app.get("/api/reports/{case_id}")
async def load_report(case_id: str):
    """Load a specific Guardian evidence report."""
    if ".." in case_id:
        raise HTTPException(400, "Invalid case ID")
    case_dir = REPORTS_DIR / "guardian-evidence" / case_id
    if not case_dir.exists():
        raise HTTPException(404, "Report not found")

    files = {}
    for f in sorted(case_dir.glob("*.json")):
        try:
            files[f.name] = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            files[f.name] = {"error": "Could not parse"}
    return {"case_id": case_id, "files": files}


@app.get("/api/today")
async def today_date():
    """Return today's date in Ecuador timezone."""
    from datetime import datetime
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    return {"date": today.isoformat(), "timezone": TIMEZONE}


@app.post("/api/live-alerts")
async def fetch_live_alerts(request: Request):
    """Extrae alertas reales de Kipu y las analiza con Guardian."""

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "Invalid JSON")

    day = body.get("date")
    mode = body.get("mode", "policy")

    if not isinstance(day, str):
        raise HTTPException(400, "Missing 'date' field")

    try:
        from alert_reviewer.guardian_daily import GuardianDailyReview

        # 1. Mantener la extracción existente.
        review = GuardianDailyReview()
        snapshot = review.run(day, mode)

        alerts = snapshot.get("accepted_alerts", [])

        # 2. Analizar las alertas extraídas con el Guardian actual.
        #
        # GuardianAgent.configured() crea automáticamente:
        # - la política actual
        # - AthenaHistoryReader
        # - Gemini, si GEMINI_API_KEY está configurada
        guardian = GuardianAgent.configured()

        # Guardian admite máximo 50 alertas por llamada y por defecto
        # limita las consultas históricas a 5.
        guardian_result = guardian.review_alerts(
            alerts[:50],
            with_history=True,
            lookback_days=7,
            max_history_queries=5,
            history_timestamp_field="timestamp",
        )

        # 3. Indexar los análisis por alert_id para unirlos
        #    con las alertas originales.
        reviews_by_id = {
            item.get("alert_id"): item
            for item in guardian_result.get("reviews", [])
            if item.get("alert_id")
        }

        analyzed_alerts = []

        for alert in alerts:
            alert_id = alert.get("alert_id")
            analysis = reviews_by_id.get(alert_id)

            analyzed_alerts.append({
                **alert,
                "guardian_analysis": analysis,
            })

        # 4. Devolver extracción + análisis.
        return JSONResponse({
            "status": "completed",
            "business_date": snapshot.get("business_date"),
            "source": snapshot.get("source"),
            "review_mode": snapshot.get("review_mode"),
            "policy_version": guardian_result.get(
                "policy_version",
                snapshot.get("policy_version"),
            ),
            "total_received": snapshot.get("source_record_count", 0),
            "total_evaluated": snapshot.get("evaluated_record_count", 0),
            "total_accepted": len(alerts),
            "total_analyzed": len(guardian_result.get("reviews", [])),
            "history_query_count": guardian_result.get(
                "history_query_count",
                0,
            ),
            "country_summary": snapshot.get("country_summary", []),
            "alerts": analyzed_alerts,
        })

    except Exception as e:
        logger.exception("Live alert analysis failed")

        code = getattr(e, "code", "UNKNOWN_ERROR")
        msg = str(e)

        if "SSO" in code or "Token" in str(type(e)):
            msg = (
                "Sesión AWS expirada. Ejecuta: "
                "aws sso login --profile ia-dev-payments-intelligence"
            )
        elif "ACCESS_DENIED" in code:
            msg = "Sin acceso al archivo de alertas. Verifica permisos AWS."
        elif "SNAPSHOT_NOT_FOUND" in code:
            msg = "No hay alertas archivadas para esa fecha."
        elif "FUTURE_DATE" in code:
            msg = "Selecciona hoy o una fecha anterior."

        return JSONResponse(
            {
                "status": "error",
                "code": code,
                "message": msg,
            },
            status_code=422,
        )


# ── Helpers ──────────────────────────────────────────────────────────


def _load_settings() -> dict[str, Any]:
    try:
        settings = GuardianSettings()
        return {
            "gemini_api_key": bool(settings.gemini_api_key.get_secret_value()),
            "gemini_model": settings.gemini_model,
        }
    except Exception:
        return {"gemini_api_key": False, "gemini_model": "unknown"}


def _standalone_deterministic(safe_alert: dict, raw_alert: dict) -> dict:
    """Run deterministic verdict without historical data (coverage will be insufficient)."""
    alert_rate = safe_alert.get("approval_rate")
    alert_rate_pct = float(alert_rate) * 100 if alert_rate is not None else None

    verdict = DeterministicVerdict(
        verdict="requires_review",
        decision_reason="no_historical_data",
        signals=(),
        evidence_summary={
            "alert_approval_rate_pct": alert_rate_pct,
            "historical_approval_rate_pct": None,
            "historical_total_transactions": 0,
            "observed_days": 0,
            "change_percentage_points": None,
            "alert_total_transactions": safe_alert.get("total_transactions"),
            "alert_declined_count": safe_alert.get("declined_count"),
        },
    )

    # Derive signal analysis from alert data alone
    signals = []
    if alert_rate is not None and float(alert_rate) < 0.3:
        signals.append("low_approval_rate")
    declined = safe_alert.get("declined_count")
    total = safe_alert.get("total_transactions")
    if declined is not None and total is not None and total > 0:
        decline_ratio = float(declined) / float(total)
        if decline_ratio > 0.7:
            signals.append("high_decline_ratio")

    return {
        "status": "completed",
        "mode": "deterministic_standalone",
        "verdict": verdict.verdict,
        "decision_reason": verdict.decision_reason,
        "signals": signals,
        "evidence_summary": verdict.evidence_summary,
        "alert_metrics": {
            "approval_rate": safe_alert.get("approval_rate"),
            "approval_rate_pct": alert_rate_pct,
            "total_transactions": safe_alert.get("total_transactions"),
            "declined_count": safe_alert.get("declined_count"),
            "rolling_avg_approval_rate": safe_alert.get("rolling_avg_approval_rate"),
            "policy_accepted": safe_alert.get("policy_accepted"),
        },
        "thresholds": {
            "approval_drop_pp": APPROVAL_DROP_THRESHOLD_PP,
            "min_observed_days": MIN_OBSERVED_DAYS,
            "min_total_transactions": MIN_TOTAL_TRANSACTIONS,
        },
        "note": "Sin datos históricos disponibles. Suba evidencia histórica para un análisis completo.",
    }
