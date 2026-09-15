"""Local dashboard adapter over Guardian; JSON stdin/stdout, no HTTP server."""

from __future__ import annotations

import hashlib
import json
import re
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from alert_reviewer.datalake_history import HistoryError, HistoryQuery, historical_evidence
from alert_reviewer.gemini_review import (
    GeminiReviewError,
    _decode_json,
    _history_evidence,
    _validate_analysis,
)
from alert_reviewer.guardian import GuardianAgent, SavedHistoryReader, validate_history_report
from alert_reviewer.guardian_daily import DailyReviewError
from alert_reviewer.kipu_contract import KipuAlert

REPORTS = Path(__file__).resolve().parents[2] / "reports"
MAX_BYTES = 2_000_000


def _query(value: Any) -> HistoryQuery:
    if not isinstance(value, dict):
        raise ValueError("Invalid query")
    for key in ("date_from", "date_to"):
        if not isinstance(value.get(key), str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", value[key]
        ):
            raise ValueError("Invalid dates")
    if not isinstance(value.get("merchant_code"), str):
        raise ValueError("Invalid MID")
    return HistoryQuery(
        value["merchant_code"],
        date.fromisoformat(value["date_from"]),
        date.fromisoformat(value["date_to"]),
    )


def _public_report(report: Any) -> dict[str, Any]:
    if not isinstance(report, dict) or report.get("agent") != "kipu-guardian":
        raise ValueError("Not a Guardian history report")
    query = _query(report.get("query"))
    result = validate_history_report(report, query)
    source = result["source"]
    result["source"] = {
        key: source.get(key)
        for key in (
            "catalog",
            "database",
            "table",
            "query_execution_id",
            "retrieved_at",
            "data_scanned_bytes",
        )
    }
    status = report.get("analysis_status")
    if status not in {"completed", "unavailable", "no_data", "not_requested"}:
        raise ValueError("Unknown analysis state")
    conclusions = None
    if status == "completed":
        _, ids = _history_evidence(historical_evidence(result))
        conclusions = report.get("conclusions")
        if not isinstance(conclusions, dict):
            raise ValueError("Missing conclusions")
        _validate_analysis(conclusions, ids)
    elif report.get("conclusions") is not None:
        raise ValueError("Unexpected conclusions")
    model = report.get("analysis_model")
    if model is not None and (
        not isinstance(model, str) or not re.fullmatch(r"gemini-[a-z0-9._-]{1,100}", model)
    ):
        raise ValueError("Invalid model")
    created = report.get("analysis_created_at")
    if created is not None:
        parsed = datetime.fromisoformat(created)
        if parsed.tzinfo is None:
            raise ValueError("Invalid analysis time")
    return {
        **result,
        "agent": "kipu-guardian",
        "analysis_status": status,
        "analysis_model": model,
        "analysis_created_at": created,
        "conclusions": conclusions,
        "analysis_error": {
            "code": "AI_UNAVAILABLE",
            "message": "Gemini no pudo completar la conclusión. Las métricas siguen disponibles.",
        }
        if status == "unavailable"
        else None,
    }


class GuardianView:
    def __init__(self, reports: Path = REPORTS) -> None:
        self.reports = reports.resolve()

    def _alert_snapshot(self) -> dict[str, Any]:
        """Read only the explicitly imported local snapshot, never search arbitrary backups."""
        path = self.reports / "guardian-dashboard-snapshot.json"
        if path.is_symlink() or path.resolve().parent != self.reports:
            raise ValueError("Invalid snapshot path")
        with path.open("rb") as stream:
            raw = stream.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            raise ValueError("Snapshot too large")
        return self._validate_alert_snapshot(_decode_json(raw))

    @staticmethod
    def _validate_alert_snapshot(snapshot: Any) -> dict[str, Any]:
        if not isinstance(snapshot, dict):
            raise ValueError("Invalid snapshot")
        business_date = snapshot.get("business_date")
        if not isinstance(business_date, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}", business_date
        ):
            raise ValueError("Invalid snapshot date")
        date.fromisoformat(business_date)
        extracted = snapshot.get("extracted_at")
        if not isinstance(extracted, str) or "T" not in extracted:
            raise ValueError("Invalid extraction time")
        parsed = datetime.fromisoformat(extracted)
        if parsed.tzinfo is None or snapshot.get("review_mode") not in ("policy", "ai"):
            raise ValueError("Invalid snapshot metadata")
        policy_version = snapshot.get("policy_version")
        model = snapshot.get("review_model")
        if (
            not isinstance(policy_version, str)
            or not policy_version.strip()
            or not (model is None or isinstance(model, str))
        ):
            raise ValueError("Invalid review metadata")
        alerts = snapshot.get("accepted_alerts")
        if not isinstance(alerts, list) or len(alerts) > 500:
            raise ValueError("Invalid alerts")
        alert_ids: set[str] = set()
        accepted_countries: dict[str, int] = {}
        for alert in alerts:
            validated = KipuAlert.model_validate(alert)
            if (
                validated.alert_id in alert_ids
                or validated.alert_id.startswith("fallback-")
                or validated.declined_count > validated.total_transactions
                or round(validated.total_transactions * validated.approval_rate)
                + validated.declined_count
                > validated.total_transactions + 1
            ):
                raise ValueError("Invalid alert identity or counts")
            alert_ids.add(validated.alert_id)
            accepted_countries[validated.country] = accepted_countries.get(validated.country, 0) + 1
            generated = alert.get("kipu_generated_at")
            if generated is not None:
                if not isinstance(generated, str) or "T" not in generated:
                    raise ValueError("Invalid Kipu generation time")
                generated_at = datetime.fromisoformat(generated)
                if generated_at.tzinfo is None:
                    raise ValueError("Kipu generation requires a timezone")
        for field in ("source_record_count", "evaluated_record_count"):
            if type(snapshot.get(field)) is not int or snapshot[field] < len(alerts):
                raise ValueError("Invalid snapshot counts")
        if snapshot["evaluated_record_count"] > snapshot["source_record_count"]:
            raise ValueError("Invalid evaluated count")
        country_summary = snapshot.get("country_summary")
        if not isinstance(country_summary, list) or len(country_summary) > 100:
            raise ValueError("Invalid country summary")
        countries: set[str] = set()
        received_total = accepted_total = 0
        for row in country_summary:
            if not isinstance(row, dict):
                raise ValueError("Invalid country summary")
            country, received, accepted = (
                row.get("country"),
                row.get("received_count"),
                row.get("accepted_count"),
            )
            if (
                not isinstance(country, str)
                or not country.strip()
                or country in countries
                or type(received) is not int
                or type(accepted) is not int
                or not 0 <= accepted <= received
                or accepted != accepted_countries.get(country, 0)
            ):
                raise ValueError("Inconsistent country summary")
            countries.add(country)
            received_total += received
            accepted_total += accepted
        if received_total != snapshot["source_record_count"] or accepted_total != len(alerts):
            raise ValueError("Inconsistent country totals")
        return {"snapshot": snapshot, "source": "local_archive"}

    def _run_alerts(self, day: Any, mode: Any) -> dict[str, Any]:
        from alert_reviewer.guardian_conclusions import _save
        from alert_reviewer.guardian_daily import GuardianDailyReview

        snapshot = GuardianDailyReview().run(day, mode)
        self._validate_alert_snapshot(snapshot)
        if snapshot.get("business_date") != day or snapshot.get("review_mode") != mode:
            raise ValueError("Daily result does not match the requested date and mode")
        if len(json.dumps(snapshot, ensure_ascii=True).encode()) > MAX_BYTES - 1000:
            raise ValueError("Snapshot too large")
        self.reports.mkdir(parents=True, exist_ok=True)
        archive = self.reports / "guardian-dashboard-runs"
        if archive.is_symlink() or archive.resolve().parent != self.reports:
            raise ValueError("Invalid archive path")
        archive.mkdir(exist_ok=True)
        latest = self.reports / "guardian-dashboard-snapshot.json"
        if latest.is_symlink():
            raise ValueError("Invalid snapshot path")
        # Retain the previous successful snapshot before changing the local current view.
        if latest.exists():
            previous = self._alert_snapshot()["snapshot"]
            identity = hashlib.sha256(json.dumps(previous, sort_keys=True).encode()).hexdigest()
            previous_path = archive / f"{previous['business_date']}-{identity}.json"
            if not previous_path.exists():
                _save(previous_path, previous)
        _save(archive / f"{day}-{uuid.uuid4().hex}.json", snapshot)
        _save(latest, snapshot)
        return {"snapshot": snapshot, "source": "local_agent"}

    def _catalog(self) -> list[tuple[str, dict[str, Any]]]:
        if not self.reports.is_dir():
            return []
        # No recursive read: backups, arbitrary filenames and alert smoke tests are excluded.
        paths = sorted(self.reports.glob("guardian-*.json"), key=lambda p: p.name, reverse=True)
        items = []
        for path in paths[:200]:
            if path.is_symlink() or path.resolve().parent != self.reports or not path.is_file():
                continue
            try:
                with path.open("rb") as stream:
                    raw = stream.read(MAX_BYTES + 1)
                if len(raw) > MAX_BYTES:
                    continue
                report = _public_report(_decode_json(raw))
                identifier = hashlib.sha256(path.name.encode()).hexdigest()[:24]
                items.append((identifier, report))
            except (ValueError, TypeError, KeyError, OSError, HistoryError, GeminiReviewError):
                continue
        items.sort(
            key=lambda item: (
                item[1].get("analysis_created_at") or item[1]["source"].get("retrieved_at") or ""
            ),
            reverse=True,
        )
        return items[:30]

    def _load(self, identifier: Any) -> dict[str, Any]:
        if not isinstance(identifier, str) or not re.fullmatch(r"[a-f0-9]{24}", identifier):
            raise ValueError("Invalid report identifier")
        for candidate, report in self._catalog():
            if candidate == identifier:
                return report
        raise ValueError("Report not found")

    def handle(self, body: Any) -> dict[str, Any]:
        if not isinstance(body, dict):
            raise ValueError("Invalid request")
        action = body.get("action")
        fields = {
            "run_alerts": {"action", "date", "mode"},
            "alert_snapshot": {"action"},
            "conclude_alert": {"action", "alert"},
            "refresh_conclusion": {"action", "alert", "metric_profile", "expected_revision"},
            "list": {"action"},
            "load": {"action", "report_id"},
            "reanalyze": {"action", "report_id"},
            "history": {"action", "merchant_code", "date_from", "date_to"},
        }
        valid_fields = isinstance(action, str) and action in fields and set(body) == fields[action]
        if action == "conclude_alert" and set(body) == {"action", "alert", "metric_profile"}:
            valid_fields = True
        if not valid_fields:
            raise ValueError("Invalid operation")
        if action == "run_alerts":
            return self._run_alerts(body["date"], body["mode"])
        if action == "alert_snapshot":
            return self._alert_snapshot()
        if action in {"conclude_alert", "refresh_conclusion"}:
            from alert_reviewer.guardian_cases import GuardianCaseReview

            profile = body.get("metric_profile", "authorizations")
            if not isinstance(profile, str) or profile not in {"sales", "authorizations"}:
                raise ValueError("Invalid metric profile")
            kwargs = {"metric_profile": profile}
            if action == "refresh_conclusion":
                if not isinstance(body["expected_revision"], str) or not re.fullmatch(
                    r"[a-f0-9]{32}", body["expected_revision"]
                ):
                    raise ValueError("Invalid revision")
                kwargs.update(refresh=True, expected_revision=body["expected_revision"])
            return GuardianCaseReview(self.reports).conclude(body["alert"], **kwargs)
        if action == "list":
            items = self._catalog()
            return {
                "local_only": True,
                "reports": [
                    {
                        "id": identifier,
                        **report["query"],
                        "analysis_status": report["analysis_status"],
                        "analysis_created_at": report["analysis_created_at"],
                        "retrieved_at": report["source"].get("retrieved_at"),
                    }
                    for identifier, report in items
                ],
                "report": items[0][1] if items else None,
                "selected_id": items[0][0] if items else None,
            }
        if action == "load":
            return {"report": self._load(body["report_id"]), "selected_id": body["report_id"]}
        if action == "reanalyze":
            saved = self._load(body["report_id"])
            query = _query(saved["query"])
            agent = GuardianAgent.configured(reader=SavedHistoryReader(saved))
        else:
            query = _query(body)
            agent = GuardianAgent.configured()
        report = agent.investigate_merchant(query)
        report["analysis_created_at"] = datetime.now(UTC).isoformat()
        public = _public_report(report)
        self.reports.mkdir(parents=True, exist_ok=True)
        name = f"guardian-view-{datetime.now(UTC):%Y%m%dT%H%M%S}-{uuid.uuid4().hex}.json"
        # Never overwrite earlier evidence. No user-controlled output path.
        with (self.reports / name).open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
        return {"report": public, "selected_id": hashlib.sha256(name.encode()).hexdigest()[:24]}


def main() -> None:
    try:
        raw = sys.stdin.buffer.read(16_385)
        if len(raw) > 16_384:
            raise ValueError("Request too large")
        payload = GuardianView().handle(_decode_json(raw))
    except DailyReviewError as error:
        payload = {"error": error.code, "message": str(error)}
    except (HistoryError, GeminiReviewError) as error:
        messages = {
            "SSO_EXPIRED": "La sesión AWS venció. Inicia sesión en data-core y reintenta.",
            "EVIDENCE_INVALID": "La evidencia guardada requiere revisión; no se reemplazó.",
            "INVALID_HISTORY_SOURCE": (
                "El histórico usa otra versión o contiene datos inconsistentes. "
                "Conserva el archivo anterior y genera una nueva extracción."
            ),
            "REVISION_CONFLICT": (
                "La evidencia cambió. Carga la última revisión y vuelve a intentar."
            ),
        }
        payload = {
            "error": getattr(error, "code", "AGENT_UNAVAILABLE"),
            "message": messages.get(
                getattr(error, "code", ""),
                "Guardian no pudo completar la consulta o el análisis. Revisa el código de error.",
            ),
        }
    except (ValueError, TypeError, KeyError, OSError, OverflowError, RecursionError):
        payload = {"error": "INVALID_REQUEST", "message": "Revisa el MID, las fechas o el reporte."}
    print(json.dumps(payload, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
