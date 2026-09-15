"""Versioned per-alert advice with durable evidence, independent of the dashboard."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from alert_reviewer.datalake_history import (
    TIMEZONE,
    AthenaHistoryConfig,
    AthenaHistoryReader,
    HistoryError,
    HistoryQuery,
    historical_evidence,
)
from alert_reviewer.gemini_review import (
    GeminiReviewError,
    _decode_json,
    _history_evidence,
    _validate_analysis,
)
from alert_reviewer.guardian import GuardianAgent, GuardianSettings, validate_history_report
from alert_reviewer.guardian_analysis import (
    ANALYSIS_VERSION,
    _alert_evidence,
    _validate_guardian_analysis,
)
from alert_reviewer.kipu_contract import KipuAlert

# Extraction changes invalidate evidence; analyst changes reuse that frozen evidence.
# Bump ANALYSIS_VERSION separately when instructions or the response contract change.
PROTOCOL = "guardian-alert-card-7d-v2"
MAX_BYTES = 2_000_000


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(raw.encode()).hexdigest()


def _save(path: Path, value: Any) -> None:
    """Atomic replacement only of this request's derived cache path."""
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=path.parent, suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


class GuardianConclusions:
    def __init__(self, reports: Path, agent: GuardianAgent | None = None) -> None:
        self.reports = reports.resolve()
        if agent is None:
            settings = GuardianSettings()
            # Status taxonomy cannot be altered by the browser or ambient status overrides.
            reader = AthenaHistoryReader(
                AthenaHistoryConfig(
                    profile=settings.history_aws_profile,
                    workgroup=settings.history_athena_workgroup,
                    approved_statuses=("APPROVED",),
                    declined_statuses=("DECLINED",),
                )
            )
            agent = GuardianAgent.configured(settings, reader=reader)
        self.agent = agent

    def _context(self, alert: Any) -> tuple[dict[str, Any], HistoryQuery, dict[str, Any], str]:
        if not isinstance(alert, dict) or str(alert.get("alert_id", "")).startswith("fallback-"):
            raise ValueError("A real Kipu payload is required")
        parsed = KipuAlert.model_validate(alert)
        generated = alert.get("kipu_generated_at")
        if not isinstance(generated, str) or "T" not in generated:
            raise ValueError("Kipu generation time is required")
        anchor = datetime.fromisoformat(generated.replace("Z", "+00:00"))
        if anchor.tzinfo is None or anchor > datetime.now(UTC):
            raise ValueError("Kipu generation time must be zoned and in the past")
        normalized = parsed.model_dump(mode="json")
        normalized["kipu_generated_at"] = anchor.astimezone(UTC).isoformat()
        outcome = self.agent.filter.evaluate(alert)
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
            raise ValueError("Invalid alert evidence")
        _alert_evidence(alert, outcome.accepted)
        end = anchor.astimezone(ZoneInfo(TIMEZONE)).date() - timedelta(days=1)
        query = HistoryQuery(parsed.merchant_code, end - timedelta(days=6), end)
        # Replay time/free text are not evidence. All policy metrics, including nulls, remain.
        stable = {
            k: v
            for k, v in normalized.items()
            if k
            not in {
                "timestamp",
                "alert_summary",
                "top_rejections",
            }
        }
        key = _digest({"protocol": PROTOCOL, "alert": stable, "policy": asdict(self.agent.policy)})
        policy = outcome.model_dump(mode="json", exclude={"alert"})
        return normalized, query, policy, key

    def _validate_saved(self, saved: Any, query: HistoryQuery, key: str) -> dict[str, Any]:
        try:
            if saved["protocol"] != PROTOCOL or saved["report_id"] != key:
                raise ValueError("Cache identity mismatch")
            history = validate_history_report(saved["history"], query)
            if saved["evidence_digest"] != _digest(history):
                raise ValueError("Evidence mismatch")
            _, _, policy, saved_key = self._context(saved["original_alert"])
            if saved_key != key or saved["policy"] != policy:
                raise ValueError("Alert mismatch")
            if saved["analysis_status"] not in {"pending", "completed", "unavailable", "no_data"}:
                raise ValueError("Invalid state")
            version = saved.get("analysis_version")
            if version not in (None, ANALYSIS_VERSION):
                raise ValueError("Unknown analyst version")
            if saved["analysis_status"] == "completed":
                _, ids = _history_evidence(historical_evidence(history))
                validator = _validate_guardian_analysis if version else _validate_analysis
                validator(saved["conclusions"], ids | {"alert_0"})
                cited = {i for f in saved["conclusions"]["findings"] for i in f["evidence_ids"]}
                if "alert_0" not in cited or not cited.intersection(ids):
                    raise ValueError("Missing alert and historical citations")
            elif saved["conclusions"] is not None:
                raise ValueError("Unexpected conclusion")
            if (saved["analysis_status"] == "no_data") != (
                history["metrics"]["total_transactions"] == 0
            ):
                # Pending evidence is also allowed before the first no-data decision.
                if saved["analysis_status"] != "pending":
                    raise ValueError("Inconsistent data state")
            return saved
        except (ValueError, TypeError, KeyError, HistoryError, GeminiReviewError) as error:
            # Corrupt evidence never triggers a silent, different Athena extraction.
            raise HistoryError(
                "EVIDENCE_INVALID", "La evidencia guardada requiere revisión."
            ) from error

    @staticmethod
    def _public(saved: dict[str, Any], *, reused: bool) -> dict[str, Any]:
        raw = saved["original_alert"]
        report = {
            key: value
            for key, value in saved.items()
            if key not in {"original_alert", "previous_analysis"}
        }
        report["alert"] = {
            key: raw[key]
            for key in (
                "alert_id",
                "merchant_code",
                "merchant_name",
                "country",
                "kipu_generated_at",
            )
        }
        if report.get("analysis_error"):
            report["analysis_error"] = {
                "code": "AI_UNAVAILABLE",
                "message": ("Gemini no completó la conclusión. Reintenta con la misma evidencia."),
            }
        return {"report": report, "reused": reused}

    def conclude(self, alert: Any) -> dict[str, Any]:
        normalized, query, policy, key = self._context(alert)
        directory = self.reports / "guardian-alerts"
        if directory.is_symlink():
            raise ValueError("Invalid evidence directory")
        directory.mkdir(parents=True, exist_ok=True)
        if directory.resolve().parent != self.reports:
            raise ValueError("Invalid evidence directory")
        path, lock = directory / f"{key}.json", directory / f"{key}.lock"
        try:
            lock_handle = lock.open("x", encoding="utf-8")
        except FileExistsError:
            raise HistoryError("AGENT_BUSY", "Esta alerta ya está siendo analizada.") from None
        try:
            saved = None
            if path.is_symlink():
                raise HistoryError("EVIDENCE_INVALID", "Ruta de evidencia inválida.")
            if path.exists():
                try:
                    with path.open("rb") as stream:
                        raw = stream.read(MAX_BYTES + 1)
                    if len(raw) > MAX_BYTES:
                        raise ValueError("Oversized evidence")
                    saved = self._validate_saved(_decode_json(raw), query, key)
                except (ValueError, TypeError, KeyError) as error:
                    raise HistoryError(
                        "EVIDENCE_INVALID", "La evidencia requiere revisión."
                    ) from error
            reused = saved is not None
            if saved and (
                saved["analysis_status"] == "no_data"
                or (
                    saved["analysis_status"] == "completed"
                    and saved.get("analysis_version") == ANALYSIS_VERSION
                )
            ):
                return self._public(saved, reused=True)
            if saved and saved.get("analysis_version") != ANALYSIS_VERSION:
                # Preserve the previous advice locally for audit. A wording/contract
                # update must not re-extract CDC data or overwrite the original evidence.
                saved["previous_analysis"] = {
                    name: saved.get(name)
                    for name in (
                        "analysis_version",
                        "analysis_status",
                        "analysis_model",
                        "analysis_created_at",
                        "conclusions",
                    )
                }
                saved.update(
                    analysis_version=ANALYSIS_VERSION,
                    analysis_status="pending",
                    analysis_model=None,
                    analysis_error=None,
                    analysis_created_at=None,
                    conclusions=None,
                )
                _save(path, saved)
            if saved is None:
                history = validate_history_report(self.agent.reader.read(query), query)
                saved = {
                    "report_id": key,
                    "protocol": PROTOCOL,
                    "original_alert": normalized,
                    "history": history,
                    "policy": policy,
                    "evidence_digest": _digest(history),
                    "analysis_version": ANALYSIS_VERSION,
                    "analysis_status": "pending",
                    "analysis_model": None,
                    "analysis_error": None,
                    "conclusions": None,
                    "analysis_created_at": None,
                }
                # This commit precedes Gemini, including a timeout or terminated process.
                _save(path, saved)
            saved.update(
                self.agent._analyze(
                    saved["history"],
                    alert=normalized,
                    accepted=policy["accepted"],
                )
            )
            saved["analysis_created_at"] = datetime.now(UTC).isoformat()
            self._validate_saved(saved, query, key)
            _save(path, saved)
            return self._public(saved, reused=reused)
        finally:
            lock_handle.close()
            lock.unlink()
