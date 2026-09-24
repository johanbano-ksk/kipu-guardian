"""Local per-alert investigation with explicit scope and versioned evidence.

The original payload-only reviewer and v2 historical CLI remain independent.
"""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alert_reviewer.ai_review import AIReviewError
from alert_reviewer.alert_filter import FilterPolicy
from alert_reviewer.datalake_history import AthenaHistoryConfig, HistoryError
from alert_reviewer.gemini_review import GeminiReviewConfig, _history_evidence
from alert_reviewer.guardian import GuardianAgent, GuardianSettings
from alert_reviewer.guardian_analysis import _validate_guardian_analysis
from alert_reviewer.guardian_assurance import (
    ANALYSIS_VERSION,
    GeminiAssuredAnalyst,
    build_comparison_context,
)
from alert_reviewer.guardian_conclusions import GuardianConclusions, _digest
from alert_reviewer.guardian_evidence_store import GuardianEvidenceStore
from alert_reviewer.guardian_history import (
    GuardianHistoryReader,
    guardian_historical_evidence,
    validate_guardian_history,
)

PROTOCOL = "guardian-alert-context-7d-v3"
_AUTO = object()
_REPORT_FIELDS = frozenset(
    {
        "report_id",
        "case_id",
        "revision_id",
        "previous_revision_id",
        "revision_created_at",
        "protocol",
        "original_alert",
        "history",
        "metric_profile",
        "policy",
        "comparison_context",
        "evidence_digest",
        "analysis_version",
        "analysis_status",
        "analysis_model",
        "analysis_attempted_at",
        "analysis_created_at",
        "analysis_error",
        "conclusions",
    }
)
_PUBLIC_FIELDS = tuple(sorted(_REPORT_FIELDS - {"original_alert"}))
_MODEL = re.compile(r"gemini-[a-z0-9][a-z0-9._-]{0,98}\Z")
_ANALYSIS_ERRORS = {
    "AI_NOT_CONFIGURED": "No se configuró Gemini.",
    "AI_UNAVAILABLE": ("Gemini no entregó una conclusión admisible; la evidencia se conserva."),
}


def _canonical(value: Any) -> str:
    """Unlike dict equality, JSON does not conflate booleans and numeric values."""
    return json.dumps(value, sort_keys=True, allow_nan=False)


def _utc_time(value: Any) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise ValueError("Invalid provenance timestamp")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
        raise ValueError("Provenance timestamps must be UTC")
    if parsed > datetime.now(UTC):
        raise ValueError("Provenance timestamp is in the future")
    return parsed


def _error(code: str) -> dict[str, str]:
    return {"code": code, "message": _ANALYSIS_ERRORS[code]}


class GuardianCaseReview:
    def __init__(
        self,
        reports: Path,
        *,
        settings: GuardianSettings | None = None,
        policy: FilterPolicy | None = None,
        reader_factory: Any = None,
        analyst: Any = _AUTO,
        model: str | None = None,
    ) -> None:
        settings = settings or GuardianSettings()
        self.policy = policy or FilterPolicy.load(settings.filter_policy_path)
        # Reuse the established payload, policy and original-generation validation.
        self.context = GuardianConclusions(reports, agent=GuardianAgent(self.policy, reader=None))
        config = AthenaHistoryConfig(
            profile=settings.history_aws_profile,
            workgroup=settings.history_athena_workgroup,
            timeout_seconds=settings.history_athena_timeout_seconds,
        )
        self.reader_factory = reader_factory or (
            lambda profile: GuardianHistoryReader(config, metric_profile=profile)
        )
        self.store = GuardianEvidenceStore(reports)
        if analyst is _AUTO:
            analyst = None
            if settings.gemini_api_key.get_secret_value():
                analyst = GeminiAssuredAnalyst(
                    GeminiReviewConfig(
                        api_key=settings.gemini_api_key.get_secret_value(),
                        model=settings.gemini_model,
                        timeout_seconds=settings.gemini_timeout_seconds,
                    )
                )
                model = settings.gemini_model
        if analyst is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model)):
            raise ValueError("An analyst requires a valid Gemini model identifier")
        self.analyst, self.model = analyst, model

    def _context(self, alert: Any, metric_profile: str):
        if metric_profile not in ("sales", "authorizations"):
            raise ValueError("Unsupported metric profile")
        normalized, query, policy, identity = self.context._context(alert)
        case_id = _digest(
            {"protocol": PROTOCOL, "metric_profile": metric_profile, "alert_identity": identity}
        )
        return normalized, query, policy, case_id

    def _validate(self, saved: dict[str, Any], query, profile: str, case_id: str) -> None:
        try:
            if not isinstance(saved, dict) or set(saved) != _REPORT_FIELDS:
                raise ValueError("Invalid evidence fields")
            if (
                saved["protocol"] != PROTOCOL
                or saved["report_id"] != case_id
                or saved["case_id"] != case_id
                or saved["analysis_version"] != ANALYSIS_VERSION
            ):
                raise ValueError("Version or identity mismatch")
            history = validate_guardian_history(saved["history"], query, metric_profile=profile)
            if _canonical(saved["metric_profile"]) != _canonical(history["metric_profile"]):
                raise ValueError("Profile mismatch")
            if saved["evidence_digest"] != _digest(history):
                raise ValueError("Evidence mismatch")
            original, original_query, policy, original_case = self._context(
                saved["original_alert"], profile
            )
            if (
                original_query != query
                or original_case != case_id
                or _canonical(saved["policy"]) != _canonical(policy)
                or _canonical(saved["original_alert"]) != _canonical(original)
            ):
                raise ValueError("Alert mismatch")
            if _canonical(saved["comparison_context"]) != _canonical(
                build_comparison_context(saved["original_alert"], history)
            ):
                raise ValueError("Comparison mismatch")
            revision_at = _utc_time(saved["revision_created_at"])
            model = saved["analysis_model"]
            attempted = saved["analysis_attempted_at"]
            created = saved["analysis_created_at"]
            if model is not None and (not isinstance(model, str) or not _MODEL.fullmatch(model)):
                raise ValueError("Invalid model provenance")
            attempted_at = _utc_time(attempted) if attempted is not None else None
            created_at = _utc_time(created) if created is not None else None
            if (model is None) != (attempted_at is None):
                raise ValueError("Model and execution timestamp must agree")
            if attempted_at is not None and attempted_at < revision_at:
                raise ValueError("Analysis precedes its evidence revision")
            if created_at is not None and (attempted_at is None or created_at < attempted_at):
                raise ValueError("Conclusion precedes its execution")
            state = saved["analysis_status"]
            if state not in ("pending", "no_data", "unavailable", "completed"):
                raise ValueError("Unknown analysis state")
            empty = history["metrics"]["total_transactions"] == 0
            if state != "pending" and (state == "no_data") != empty:
                raise ValueError("Inconsistent data state")
            if state == "completed":
                _, ids = _history_evidence(guardian_historical_evidence(history))
                _validate_guardian_analysis(saved["conclusions"], ids | {"alert_0"})
                if (
                    saved["conclusions"]["verdict"]
                    not in saved["comparison_context"]["allowed_verdicts"]
                ):
                    raise ValueError("Unsupported definitive verdict")
                if model is None or attempted_at is None or created_at is None:
                    raise ValueError("Missing analysis provenance")
                if saved["analysis_error"] is not None:
                    raise ValueError("Completed analysis cannot contain an error")
            elif saved["conclusions"] is not None or created_at is not None:
                raise ValueError("Unexpected conclusion")
            if state in ("pending", "no_data"):
                if (
                    model is not None
                    or attempted_at is not None
                    or saved["analysis_error"] is not None
                ):
                    raise ValueError("AI was not executed")
            elif state == "unavailable":
                expected_code = "AI_NOT_CONFIGURED" if attempted_at is None else "AI_UNAVAILABLE"
                if _canonical(saved["analysis_error"]) != _canonical(_error(expected_code)):
                    raise ValueError("Invalid unavailable-analysis provenance")
        except (KeyError, ValueError, TypeError, HistoryError, AIReviewError, OverflowError) as exc:
            raise HistoryError(
                "EVIDENCE_INVALID", "La evidencia guardada requiere revisión."
            ) from exc

    def _analyze(self, saved: dict[str, Any]) -> dict[str, Any]:
        result = dict(saved)
        if not result["history"]["metrics"]["total_transactions"]:
            result["analysis_status"] = "no_data"
            return result
        if self.analyst is None:
            result.update(
                analysis_status="unavailable",
                analysis_model=None,
                analysis_attempted_at=None,
                analysis_created_at=None,
                conclusions=None,
                analysis_error=_error("AI_NOT_CONFIGURED"),
            )
            return result
        result.update(
            analysis_model=self.model,
            analysis_attempted_at=datetime.now(UTC).isoformat(),
            analysis_created_at=None,
            analysis_error=None,
            conclusions=None,
        )
        try:
            conclusion = self.analyst.analyze(
                result["original_alert"],
                result["history"],
                result["policy"]["accepted"],
                result["comparison_context"],
            )
            _, ids = _history_evidence(guardian_historical_evidence(result["history"]))
            _validate_guardian_analysis(conclusion, ids | {"alert_0"})
            if conclusion["verdict"] not in result["comparison_context"]["allowed_verdicts"]:
                raise AIReviewError("AI_UNSUPPORTED_VERDICT")
            result.update(
                analysis_status="completed",
                conclusions=conclusion,
                analysis_created_at=datetime.now(UTC).isoformat(),
            )
        except AIReviewError:
            result.update(
                analysis_status="unavailable",
                analysis_error=_error("AI_UNAVAILABLE"),
            )
        return result

    def conclude(
        self,
        alert: Any,
        *,
        metric_profile: str = "authorizations",
        refresh: bool = False,
        expected_revision: str | None = None,
    ) -> dict[str, Any]:
        if type(refresh) is not bool or (not refresh and expected_revision is not None):
            raise ValueError("Invalid refresh request")
        if refresh and (
            not isinstance(expected_revision, str)
            or not re.fullmatch(r"[a-f0-9]{32}", expected_revision)
        ):
            raise ValueError("Expected revision is required")
        normalized, query, policy, case_id = self._context(alert, metric_profile)
        with self.store.locked(
            case_id,
            validator=lambda saved: self._validate(saved, query, metric_profile, case_id),
        ) as case:
            saved = case.load_latest()
            if saved is not None:
                self._validate(saved, query, metric_profile, case_id)
            if refresh and (saved is None or saved["revision_id"] != expected_revision):
                raise HistoryError(
                    "REVISION_CONFLICT",
                    "La evidencia cambió. Carga la última revisión antes de actualizar.",
                )
            reused = saved is not None and not refresh
            if saved is None or refresh:
                history = validate_guardian_history(
                    self.reader_factory(metric_profile).read(query),
                    query,
                    metric_profile=metric_profile,
                )
                saved = case.create(
                    {
                        "report_id": case_id,
                        "protocol": PROTOCOL,
                        "original_alert": normalized,
                        "history": history,
                        "metric_profile": history["metric_profile"],
                        "policy": policy,
                        "comparison_context": build_comparison_context(normalized, history),
                        "evidence_digest": _digest(history),
                        "analysis_version": ANALYSIS_VERSION,
                        "analysis_status": "pending",
                        "analysis_model": None,
                        "analysis_attempted_at": None,
                        "analysis_created_at": None,
                        "analysis_error": None,
                        "conclusions": None,
                    }
                )
            if saved["analysis_status"] not in ("completed", "no_data"):
                updated = self._analyze(saved)
                self._validate(updated, query, metric_profile, case_id)
                saved = case.update(updated)
            public = {key: deepcopy(saved[key]) for key in _PUBLIC_FIELDS}
            if saved["analysis_error"] is not None:
                public["analysis_error"] = _error(saved["analysis_error"]["code"])
            public["alert"] = {
                k: saved["original_alert"][k]
                for k in (
                    "alert_id",
                    "merchant_code",
                    "merchant_name",
                    "country",
                    "kipu_generated_at",
                )
            }
            return {"report": public, "reused": reused, "revisions": case.list_revisions()}
