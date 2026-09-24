"""Deterministic verdict calculation for Guardian alerts.

The code decides; the LLM only explains.  Verdicts are computed from
numerical thresholds on alert metrics and historical evidence, without
any AI involvement.  This mirrors the MAX pattern where RuleEvaluator
owns all business-critical decisions.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ── Thresholds (approved by the team) ────────────────────────────────
APPROVAL_DROP_THRESHOLD_PP = 10  # puntos porcentuales
MIN_OBSERVED_DAYS = 7
MIN_TOTAL_TRANSACTIONS = 100
TREND_DECLINE_THRESHOLD_PP = 5  # negative change in historical comparison


@dataclass(frozen=True)
class DeterministicVerdict:
    """Result of the deterministic rule evaluation."""

    verdict: str  # "confirmed" | "no_data" | "requires_review"
    decision_reason: str
    signals: tuple[str, ...]
    evidence_summary: dict[str, Any]


def evaluate_verdict(
    alert_evidence: dict[str, Any],
    history_evidence: dict[str, Any],
    period: dict[str, Any],
    data_quality: dict[str, Any],
    comparison: dict[str, Any],
) -> DeterministicVerdict:
    """Evaluate alert and historical metrics to produce a deterministic verdict.

    Parameters
    ----------
    alert_evidence : dict
        Safe alert metrics (``approval_rate`` in [0, 1]).
    history_evidence : dict
        Safe history dict with ``daily``, ``comparison``, ``data_quality``.
    period : dict
        Aggregated period stats with ``approval_rate_pct`` in [0, 100].
    data_quality : dict
        Coverage information with ``observed_days`` and ``total_transactions``.
    comparison : dict
        Half-period comparison with ``change_percentage_points``.
    """
    signals: list[str] = []

    # ── Coverage check ───────────────────────────────────────────────
    observed_days = _safe_int(data_quality.get("observed_days"))
    total_hist_trx = _safe_int(data_quality.get("total_transactions"))

    if observed_days == 0 or total_hist_trx == 0:
        return DeterministicVerdict(
            verdict="no_data",
            decision_reason="no_historical_data",
            signals=(),
            evidence_summary=_build_summary(
                alert_evidence, period, data_quality, comparison
            ),
        )

    if observed_days < MIN_OBSERVED_DAYS or total_hist_trx < MIN_TOTAL_TRANSACTIONS:
        return DeterministicVerdict(
            verdict="requires_review",
            decision_reason="insufficient_coverage",
            signals=(),
            evidence_summary=_build_summary(
                alert_evidence, period, data_quality, comparison
            ),
        )

    # ── Extract key metrics ──────────────────────────────────────────
    alert_rate_raw = alert_evidence.get("approval_rate")
    if alert_rate_raw is None:
        return DeterministicVerdict(
            verdict="requires_review",
            decision_reason="missing_alert_approval_rate",
            signals=(),
            evidence_summary=_build_summary(
                alert_evidence, period, data_quality, comparison
            ),
        )

    alert_rate_pct = float(alert_rate_raw) * 100
    hist_rate_pct = period.get("approval_rate_pct")

    if hist_rate_pct is None:
        return DeterministicVerdict(
            verdict="no_data",
            decision_reason="missing_historical_approval_rate",
            signals=(),
            evidence_summary=_build_summary(
                alert_evidence, period, data_quality, comparison
            ),
        )

    hist_rate_pct = float(hist_rate_pct)
    rate_drop_pp = hist_rate_pct - alert_rate_pct

    # ── Signal detection ─────────────────────────────────────────────
    change_pp = comparison.get("change_percentage_points")
    if change_pp is not None:
        change_pp = float(change_pp)

    if rate_drop_pp >= APPROVAL_DROP_THRESHOLD_PP:
        signals.append("significant_approval_drop")

    if change_pp is not None and change_pp < -TREND_DECLINE_THRESHOLD_PP:
        signals.append("declining_historical_trend")

    # High volume of declines relative to historical daily average
    alert_declined = alert_evidence.get("declined_count")
    daily_rows = history_evidence.get("daily", [])
    if alert_declined is not None and daily_rows:
        avg_daily_declined = sum(
            d.get("declined_count", 0) for d in daily_rows
        ) / len(daily_rows)
        if avg_daily_declined > 0 and float(alert_declined) > avg_daily_declined * 2:
            signals.append("high_decline_volume")

    # ── Verdict decision ─────────────────────────────────────────────
    summary = _build_summary(alert_evidence, period, data_quality, comparison)

    if rate_drop_pp >= APPROVAL_DROP_THRESHOLD_PP:
        return DeterministicVerdict(
            verdict="confirmed",
            decision_reason="significant_approval_rate_drop",
            signals=tuple(signals),
            evidence_summary=summary,
        )

    if 0 < rate_drop_pp < APPROVAL_DROP_THRESHOLD_PP:
        # Minor drop: only confirm if there is a converging declining trend.
        if "declining_historical_trend" in signals:
            return DeterministicVerdict(
                verdict="confirmed",
                decision_reason="minor_drop_with_declining_trend",
                signals=tuple(signals),
                evidence_summary=summary,
            )
        return DeterministicVerdict(
            verdict="requires_review",
            decision_reason="minor_drop_without_convergence",
            signals=tuple(signals),
            evidence_summary=summary,
        )

    # No confirmed deterioration is not a rejection of the alert. It remains
    # operationally reviewable because this contract has no false-positive category.
    return DeterministicVerdict(
        verdict="requires_review",
        decision_reason=(
            "ambiguous_signals" if change_pp is not None and change_pp < 0
            else "no_confirmed_deterioration"
        ),
        signals=tuple(signals),
        evidence_summary=summary,
    )


# ── Output validation ────────────────────────────────────────────────

_NUM_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


def extract_evidence_numbers(payload: dict[str, Any]) -> set[str]:
    """Extract all numeric values present in the evidence payload."""
    numbers: set[str] = set()
    _collect_numbers(payload, numbers)
    return numbers


def _collect_numbers(obj: Any, out: set[str]) -> None:
    if isinstance(obj, dict):
        for value in obj.values():
            _collect_numbers(value, out)
    elif isinstance(obj, list):
        for item in obj:
            _collect_numbers(item, out)
    elif isinstance(obj, (int, float)) and obj is not True and obj is not False:
        out.add(str(obj))
        if isinstance(obj, float):
            out.add(f"{obj:.0f}")
            out.add(f"{obj:.1f}")
            out.add(f"{obj:.2f}")
        else:
            out.add(f"{obj}.0")


def validate_output_text(
    analysis: dict[str, Any],
    evidence_numbers: set[str],
) -> str | None:
    """Return a rejection reason if the output contains unsupported claims.

    Returns ``None`` when the output is acceptable.
    """
    all_text = _all_text_from_analysis(analysis)

    found_numbers = _NUM_RE.findall(all_text)
    for num in found_numbers:
        if num not in evidence_numbers and not _is_trivial_number(num):
            return f"unsupported_numeric_claim:{num}"

    return None


def _all_text_from_analysis(analysis: dict[str, Any]) -> str:
    parts: list[str] = []
    if isinstance(analysis.get("summary"), str):
        parts.append(analysis["summary"])
    for finding in analysis.get("findings", []):
        if isinstance(finding, dict) and isinstance(finding.get("observation"), str):
            parts.append(finding["observation"])
    for field_name in ("limitations", "next_steps"):
        for item in analysis.get(field_name, []):
            if isinstance(item, str):
                parts.append(item)
    return " ".join(parts)


def _is_trivial_number(num_str: str) -> bool:
    """Numbers like 0, 1, 2, 100, etc. appear naturally in language."""
    try:
        value = float(num_str)
        return value <= 10 or value == 100
    except ValueError:
        return False


# ── Fallback explanation ─────────────────────────────────────────────


def deterministic_fallback_explanation(
    verdict: DeterministicVerdict,
    history_ids: set[str],
) -> dict[str, Any]:
    """Generate a deterministic explanation when Gemini fails after retries."""
    s = verdict.evidence_summary
    alert_rate = s.get("alert_approval_rate_pct")
    hist_rate = s.get("historical_approval_rate_pct")

    day_ids = sorted(eid for eid in history_ids if eid.startswith("day_"))
    cite_day = day_ids[0] if day_ids else "quality_0"

    if verdict.verdict == "confirmed":
        drop = _safe_drop(hist_rate, alert_rate)
        summary = (
            f"La tasa de aceptación de la alerta ({_fmt(alert_rate)}%) muestra una "
            f"caída de {_fmt(drop)} puntos porcentuales respecto al histórico "
            f"({_fmt(hist_rate)}%), con {s.get('observed_days', 0)} días observados y "
            f"{s.get('historical_total_transactions', 0)} transacciones históricas."
        )
        findings = [
            {
                "observation": (
                    f"Caída de aceptación: alerta {_fmt(alert_rate)}% "
                    f"vs histórico {_fmt(hist_rate)}%."
                ),
                "evidence_ids": ["alert_0", cite_day],
            }
        ]
    elif verdict.verdict == "no_data":
        summary = (
            "No hay datos históricos elegibles suficientes para evaluar la tasa de "
            f"aceptación de la alerta. Cobertura: {s.get('observed_days', 0)} días, "
            f"{s.get('historical_total_transactions', 0)} transacciones."
        )
        findings = [
            {
                "observation": (
                    "No se obtuvo una tasa histórica de aceptación comparable."
                ),
                "evidence_ids": ["alert_0", "quality_0"],
            }
        ]
    else:
        summary = (
            f"Evidencia insuficiente para confirmar o descartar la alerta. "
            f"Cobertura: {s.get('observed_days', 0)} días, "
            f"{s.get('historical_total_transactions', 0)} transacciones."
        )
        findings = [
            {
                "observation": "Cobertura o señales insuficientes para decidir.",
                "evidence_ids": ["alert_0", cite_day],
            }
        ]

    return {
        "verdict": verdict.verdict,
        "verdict_evidence_ids": ["alert_0", cite_day],
        "summary": summary,
        "findings": findings,
        "limitations": [
            "Explicación generada de forma determinística sin análisis de lenguaje natural."
        ],
        "next_steps": [
            "Validar manualmente con el equipo de operaciones."
        ],
    }


# ── Helpers ──────────────────────────────────────────────────────────


def _build_summary(
    alert_evidence: dict[str, Any],
    period: dict[str, Any],
    data_quality: dict[str, Any],
    comparison: dict[str, Any],
) -> dict[str, Any]:
    alert_rate = alert_evidence.get("approval_rate")
    return {
        "alert_approval_rate_pct": (
            round(float(alert_rate) * 100, 2) if alert_rate is not None else None
        ),
        "historical_approval_rate_pct": period.get("approval_rate_pct"),
        "historical_total_transactions": period.get("total_transactions"),
        "observed_days": data_quality.get("observed_days"),
        "change_percentage_points": comparison.get("change_percentage_points"),
        "alert_total_transactions": alert_evidence.get("total_transactions"),
        "alert_declined_count": alert_evidence.get("declined_count"),
    }


def _safe_int(value: Any) -> int:
    if value is None:
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _fmt(value: float | None) -> str:
    if value is None:
        return "N/A"
    return f"{value:.1f}"


def _safe_drop(hist: float | None, alert: float | None) -> float | None:
    if hist is None or alert is None:
        return None
    return float(hist) - float(alert)
