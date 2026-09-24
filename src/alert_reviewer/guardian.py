"""Guardian's bounded alert-review workflow, independent of any UI or web server."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr, ValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from alert_reviewer.ai_review import AIReviewError
from alert_reviewer.alert_filter import FilterPolicy, PayloadAlertFilter
from alert_reviewer.datalake_history import (
    HISTORY_SCHEMA_VERSION,
    TIMEZONE,
    AthenaHistoryConfig,
    AthenaHistoryReader,
    HistoryError,
    HistoryQuery,
    historical_evidence,
    summarize_rows,
)
from alert_reviewer.gemini_review import (
    DEFAULT_GEMINI_MODEL,
    GeminiHistoricalAnalyst,
    GeminiReviewConfig,
)
from alert_reviewer.guardian_analysis import GeminiGuardianAnalyst
from alert_reviewer.kipu_contract import KipuAlert

ROOT = Path(__file__).resolve().parents[2]


class GuardianSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    gemini_api_key: SecretStr = SecretStr("")
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_timeout_seconds: float = Field(default=45, ge=1, le=90)
    history_aws_profile: str = "data-core"
    history_athena_workgroup: str = "primary"
    history_athena_timeout_seconds: float = Field(default=180, ge=1, le=180)
    history_approved_statuses: str = "APPROVED"
    history_declined_statuses: str = "DECLINED"
    filter_policy_path: Path = ROOT / "config" / "filter_policy.yaml"


def validate_history_report(report: Any, query: HistoryQuery) -> dict[str, Any]:
    """Check saved/live evidence belongs to this MID/range and recalculate all metrics."""
    try:
        # Ticket-based evidence cannot be relabelled as transaction-based evidence.
        if report["schema_version"] != HISTORY_SCHEMA_VERSION or report["query"] != {
            "merchant_code": query.merchant_code,
            "date_from": query.date_from.isoformat(),
            "date_to": query.date_to.isoformat(),
            "timezone": TIMEZONE,
        }:
            raise ValueError("Query mismatch")
        rows = []
        for daily in report["daily"]:
            counts = {}
            for field in (
                "total_transactions",
                "approved_transactions",
                "declined_transactions",
                "other_transactions",
            ):
                if type(daily[field]) is not int:
                    raise ValueError("Invalid count")
                counts[field] = str(daily[field])
            rows.append({"business_date": daily["date"], **counts})
        rebuilt = summarize_rows(rows, query, report["source"])
        for field in ("daily", "metrics", "comparison"):
            if report[field] != rebuilt[field]:
                raise ValueError("Inconsistent metrics")
        if not isinstance(report["source"], dict):
            raise ValueError("Missing source")
        return deepcopy(rebuilt)
    except (ValueError, TypeError, KeyError, HistoryError):
        raise HistoryError(
            "INVALID_HISTORY_SOURCE",
            "El histórico usa otra versión, no corresponde al MID/rango o es inconsistente. "
            "La evidencia anterior debe conservarse; genera una nueva extracción.",
        ) from None


class SavedHistoryReader:
    """Explicit replay source: never falls back to AWS for another merchant or range."""

    def __init__(self, report: dict[str, Any]) -> None:
        self.report = deepcopy(report)

    def read(self, query: HistoryQuery) -> dict[str, Any]:
        return validate_history_report(self.report, query)


class GuardianAgent:
    def __init__(
        self,
        policy: FilterPolicy,
        reader: Any,
        alert_analyst: Any = None,
        history_analyst: Any = None,
        *,
        model: str | None = None,
    ) -> None:
        self.policy = policy
        self.filter = PayloadAlertFilter(policy)
        self.reader = reader
        self.alert_analyst = alert_analyst
        self.history_analyst = history_analyst
        self.model = model

    @classmethod
    def configured(
        cls, settings: GuardianSettings | None = None, *, reader: Any = None
    ) -> GuardianAgent:
        settings = settings or GuardianSettings()
        policy = FilterPolicy.load(settings.filter_policy_path)
        if reader is None:
            reader = AthenaHistoryReader(
                AthenaHistoryConfig(
                    profile=settings.history_aws_profile,
                    workgroup=settings.history_athena_workgroup,
                    timeout_seconds=settings.history_athena_timeout_seconds,
                    approved_statuses=tuple(
                        s.strip().upper() for s in settings.history_approved_statuses.split(",")
                    ),
                    declined_statuses=tuple(
                        s.strip().upper() for s in settings.history_declined_statuses.split(",")
                    ),
                )
            )
        config = None
        if settings.gemini_api_key.get_secret_value():
            config = GeminiReviewConfig(
                api_key=settings.gemini_api_key.get_secret_value(),
                model=settings.gemini_model,
                timeout_seconds=settings.gemini_timeout_seconds,
            )
        return cls(
            policy,
            reader,
            GeminiGuardianAnalyst(config, policy) if config else None,
            GeminiHistoricalAnalyst(config) if config else None,
            model=settings.gemini_model if config else None,
        )

    def _analyze(
        self,
        report: dict[str, Any],
        *,
        alert: dict[str, Any] | None = None,
        accepted: bool = False,
    ) -> dict[str, Any]:
        result: dict[str, Any] = {
            "analysis_status": "not_requested",
            "analysis_error": None,
            "analysis_model": self.model,
            "conclusions": None,
        }
        analyst = self.history_analyst if alert is None else self.alert_analyst
        if report["metrics"]["total_transactions"] == 0:
            result["analysis_status"] = "no_data"
        elif analyst is None:
            result.update(
                analysis_status="unavailable",
                analysis_error={
                    "code": "AI_NOT_CONFIGURED",
                    "message": "Configura Gemini en el agente Guardian.",
                },
            )
        else:
            try:
                evidence = historical_evidence(report)
                result["conclusions"] = (
                    analyst.analyze(evidence)
                    if alert is None
                    else analyst.analyze(alert, evidence, accepted)
                )
                result["analysis_status"] = "completed"
            except AIReviewError as error:
                result.update(
                    analysis_status="unavailable",
                    analysis_error={
                        "code": getattr(error, "code", "AI_REVIEW_FAILED"),
                        "message": "Gemini no completó la conclusión; se conserva la revisión.",
                    },
                )
        return result

    def investigate_merchant(self, query: HistoryQuery) -> dict[str, Any]:
        """The agent's historical investigation capability, usable without an alert."""
        report = validate_history_report(self.reader.read(query), query)
        return {"agent": "kipu-guardian", **report, **self._analyze(report)}

    def review_alerts(
        self,
        alerts: list[dict[str, Any]],
        *,
        with_history: bool = True,
        lookback_days: int = 7,
        max_history_queries: int = 5,
        history_timestamp_field: str = "timestamp",
    ) -> dict[str, Any]:
        """Policy decision followed by bounded historical investigation for valid v1 alerts.

        The lookback is anchored to each alert, never the machine's current date. No
        queue ACK or EventBridge publication occurs here; accepted payloads are unchanged.
        """
        if (
            not isinstance(alerts, list)
            or len(alerts) > 50
            or not all(isinstance(alert, dict) for alert in alerts)
        ):
            raise ValueError("Guardian admite lotes de hasta 50 alertas JSON.")
        if type(with_history) is not bool:
            raise ValueError("with_history debe ser booleano.")
        if type(lookback_days) is not int or not 1 <= lookback_days <= 31:
            raise ValueError("El histórico debe abarcar entre 1 y 31 días.")
        if type(max_history_queries) is not int or not 1 <= max_history_queries <= 20:
            raise ValueError("El límite de consultas debe estar entre 1 y 20.")
        if history_timestamp_field not in {"timestamp", "kipu_generated_at"}:
            raise ValueError("Usa una fecha de generación contractual, no la hora del replay.")
        cache: dict[HistoryQuery, dict[str, Any] | HistoryError] = {}
        reviews = []
        accepted_alerts = []
        for alert in alerts:
            outcome = self.filter.evaluate(alert)
            if outcome.accepted:
                accepted_alerts.append(deepcopy(alert))
            review: dict[str, Any] = {
                "alert_id": outcome.alert_id,
                "merchant_code": alert.get("merchant_code"),
                "policy": outcome.model_dump(mode="json", exclude={"alert"}),
                "history_status": "not_requested",
                "history": None,
                "analysis_status": "not_requested",
                "conclusions": None,
                "analysis_error": None,
                "analysis_model": self.model,
                "history_anchor_field": history_timestamp_field,
            }
            reviews.append(review)
            if not with_history:
                continue
            try:
                KipuAlert.model_validate(alert)
                if any(
                    str(reason).startswith("INVALID_")
                    or str(reason)
                    in {
                        "MISSING_REQUIRED_FIELD",
                        "UNSUPPORTED_SCHEMA",
                        "INCONSISTENT_COUNTS",
                        "SUPERSEDED_BY_V2",
                    }
                    for reason in outcome.reason_codes
                ):
                    raise ValueError("Invalid policy evidence")
                if alert["declined_count"] > alert["total_transactions"] or (
                    round(alert["total_transactions"] * alert["approval_rate"])
                    + alert["declined_count"]
                    > alert["total_transactions"] + 1
                ):
                    raise ValueError("Inconsistent counts")
                timestamp = datetime.fromisoformat(
                    alert[history_timestamp_field].replace("Z", "+00:00")
                )
                if timestamp.tzinfo is None or timestamp > datetime.now(UTC):
                    raise ValueError("Ambiguous or future alert time")
                end = timestamp.astimezone(ZoneInfo(TIMEZONE)).date() - timedelta(days=1)
                query = HistoryQuery(
                    alert["merchant_code"], end - timedelta(days=lookback_days - 1), end
                )
                review["history_anchor_at"] = timestamp.isoformat()
            except (
                ValueError,
                TypeError,
                KeyError,
                ValidationError,
                AttributeError,
                OverflowError,
            ):
                review.update(
                    history_status="skipped",
                    analysis_status="skipped",
                    analysis_error={
                        "code": "INVALID_ALERT_FOR_HISTORY",
                        "message": "Requiere alerta 1.0 válida, MID y hora con zona.",
                    },
                )
                continue
            if query not in cache:
                if len(cache) >= max_history_queries:
                    review.update(
                        history_status="skipped",
                        analysis_status="skipped",
                        analysis_error={
                            "code": "HISTORY_QUERY_LIMIT",
                            "message": "Límite de consultas del lote alcanzado.",
                        },
                    )
                    continue
                try:
                    cache[query] = validate_history_report(self.reader.read(query), query)
                except HistoryError as error:
                    cache[query] = error
            history = cache[query]
            if isinstance(history, HistoryError):
                review.update(
                    history_status="unavailable",
                    analysis_status="unavailable",
                    analysis_error={"code": history.code, "message": str(history)},
                )
                continue
            review.update(history_status="completed", history=deepcopy(history))
            review.update(self._analyze(history, alert=alert, accepted=outcome.accepted))
        return {
            "agent": "kipu-guardian",
            "schema_version": "1.0",
            "reviewed_at": datetime.now(UTC).isoformat(),
            "policy_version": self.policy.version,
            "accepted_alerts": accepted_alerts,
            "reviews": reviews,
            "history_query_count": len(cache),
            "note": "Las conclusiones históricas no sustituyen la política ni prueban incidentes.",
        }
