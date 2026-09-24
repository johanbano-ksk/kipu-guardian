"""Slack Socket Mode adapter for acceptance-rate Kipu alerts.

Slack is only an input/output adapter. Business decisions remain in the
acceptance-rate policy and deterministic Guardian evaluator.
"""

from __future__ import annotations

import json
import logging
import os
import re
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from alert_reviewer.alert_filter import FilterPolicy, PayloadAlertFilter
from alert_reviewer.datalake_history import (
    AthenaHistoryConfig,
    AthenaHistoryReader,
    HistoryError,
    HistoryQuery,
    historical_evidence,
)
from alert_reviewer.deterministic_verdict import evaluate_verdict
from alert_reviewer.kipu_contract import KipuAlert

logger = logging.getLogger(__name__)
TIMEZONE = "America/Guayaquil"
_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


class SlackBotSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    slack_bot_token: SecretStr = SecretStr("")
    slack_app_token: SecretStr = SecretStr("")
    slack_alert_channel_id: str | None = None
    slack_alert_bot_name: str = "fraud-reporting"
    slack_alert_bot_user_id: str | None = None
    slack_dry_run: bool = True
    slack_processed_events_path: Path = Path("state/slack_processed_events.json")
    slack_history_lookback_days: int = Field(default=7, ge=1, le=31)
    history_aws_profile: str = "data-core"
    history_athena_workgroup: str = "primary"
    history_athena_timeout_seconds: float = Field(default=180, ge=1, le=180)
    history_approved_statuses: str = "APPROVED"
    history_declined_statuses: str = "DECLINED"
    filter_policy_path: Path = Path("config/filter_policy.yaml")


class SlackEventStore:
    """Small local dedupe store for the first Slack dry-run integration."""

    def __init__(self, path: Path, max_records: int = 5000) -> None:
        self.path = path
        self.max_records = max_records

    def is_processed(self, key: str) -> bool:
        return key in self._load()

    def mark_processed(self, key: str, metadata: dict[str, Any]) -> None:
        values = self._load()
        values[key] = metadata
        values = dict(list(values.items())[-self.max_records :])
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, suffix=".tmp", delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(values, stream, ensure_ascii=False, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            if temporary is not None and temporary.exists():
                temporary.unlink()

    def _load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


def _event_text(event: dict[str, Any]) -> str:
    parts = [event.get("text") or ""]
    for block in event.get("blocks", []) or []:
        block_text = (block.get("text") or {}).get("text")
        if block_text:
            parts.append(block_text)
        for field in block.get("fields", []) or []:
            if field.get("text"):
                parts.append(field["text"])
    return "\n".join(str(part) for part in parts if part)


def _json_payload(text: str) -> dict[str, Any] | None:
    candidates = [match.group(1) for match in _JSON_BLOCK.finditer(text)]
    if not candidates:
        start, end = text.find("{"), text.rfind("}")
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1])
    for candidate in candidates:
        try:
            value = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            if isinstance(value.get("detail"), dict):
                value = value["detail"]
            if isinstance(value.get("alert"), dict):
                value = value["alert"]
            return value
    return None


def parse_slack_kipu_alert(event: dict[str, Any]) -> dict[str, Any] | None:
    """Extract a complete Kipu 1.0 alert from a Slack message JSON block."""
    payload = _json_payload(_event_text(event))
    if payload is None:
        return None
    try:
        return KipuAlert.model_validate(payload).model_dump(mode="json")
    except Exception:
        return None


def _event_key(event_id: str | None, event: dict[str, Any]) -> str:
    return event_id or f"message:{event.get('channel')}:{event.get('ts')}"


def _is_kipu_message(event: dict[str, Any], settings: SlackBotSettings) -> bool:
    if event.get("type") != "message" or event.get("subtype") not in {None, "bot_message"}:
        return False
    if event.get("thread_ts") and event.get("thread_ts") != event.get("ts"):
        return False
    if settings.slack_alert_channel_id and event.get("channel") != settings.slack_alert_channel_id:
        return False
    configured_ids = {settings.slack_alert_bot_user_id, event.get("bot_id")}
    if settings.slack_alert_bot_user_id and event.get("user") in configured_ids:
        return True
    names = {
        str(event.get("username") or "").lower(),
        str((event.get("bot_profile") or {}).get("name") or "").lower(),
        str((event.get("bot_profile") or {}).get("app_name") or "").lower(),
    }
    return settings.slack_alert_bot_name.lower() in names


def _history_query(alert: dict[str, Any], lookback_days: int) -> HistoryQuery:
    timestamp = datetime.fromisoformat(str(alert["timestamp"]).replace("Z", "+00:00"))
    local_day = timestamp.astimezone(ZoneInfo(TIMEZONE)).date()
    end = local_day - timedelta(days=1)
    return HistoryQuery(alert["merchant_code"], end - timedelta(days=lookback_days - 1), end)


class SlackAcceptanceAnalyzer:
    def __init__(self, settings: SlackBotSettings) -> None:
        self.settings = settings
        self.policy = FilterPolicy.load(settings.filter_policy_path)
        self.filter = PayloadAlertFilter(self.policy)
        self.reader = AthenaHistoryReader(
            AthenaHistoryConfig(
                profile=settings.history_aws_profile,
                workgroup=settings.history_athena_workgroup,
                timeout_seconds=settings.history_athena_timeout_seconds,
                approved_statuses=tuple(
                    item.strip().upper() for item in settings.history_approved_statuses.split(",")
                ),
                declined_statuses=tuple(
                    item.strip().upper() for item in settings.history_declined_statuses.split(",")
                ),
            )
        )

    def analyze(self, alert: dict[str, Any]) -> dict[str, Any]:
        outcome = self.filter.evaluate(alert)
        if not outcome.accepted:
            return {
                "label": "Falsa",
                "verdict": "requires_review",
                "reason": "La alerta no cumple la política de aceptación publicada por Kipu.",
                "policy_reasons": list(outcome.reason_codes),
            }
        try:
            query = _history_query(alert, self.settings.slack_history_lookback_days)
            history = self.reader.read(query)
            evidence = historical_evidence(history)
            if history["metrics"]["total_transactions"] == 0:
                return {
                    "label": "Requiere revisión · sin datos",
                    "verdict": "no_data",
                    "reason": (
                        "No hay transacciones históricas elegibles para comparar la tasa "
                        "de aceptación."
                    ),
                }
            result = evaluate_verdict(
                alert_evidence=alert,
                history_evidence=evidence,
                period={
                    "approval_rate_pct": history["metrics"]["approval_rate"] * 100,
                    "total_transactions": history["metrics"]["total_transactions"],
                },
                data_quality=evidence["data_quality"],
                comparison=evidence["comparison"],
            )
            return {
                "label": "Correcta" if result.verdict == "confirmed" else "Requiere revisión",
                "verdict": result.verdict,
                "reason": _reason_text(result.decision_reason),
                "summary": result.evidence_summary,
            }
        except (HistoryError, ValueError, KeyError, TypeError) as error:
            logger.warning(
                "Slack history review failed code=%s",
                getattr(error, "code", type(error).__name__),
            )
            return {
                "label": "Requiere revisión",
                "verdict": "requires_review",
                "reason": (
                    "No se pudo completar el histórico de aceptación dentro del tiempo "
                    "disponible."
                ),
                "error_code": getattr(error, "code", "HISTORY_FAILED"),
            }


def _reason_text(reason: str) -> str:
    return {
        "significant_approval_rate_drop": (
            "La tasa de aceptación presenta un deterioro material frente al histórico."
        ),
        "minor_drop_with_declining_trend": (
            "La tasa de aceptación cae y el histórico muestra una tendencia descendente."
        ),
        "minor_drop_without_convergence": (
            "La caída de aceptación no tiene suficiente evidencia convergente."
        ),
        "ambiguous_signals": (
            "La alerta y la tendencia histórica presentan señales que requieren validación."
        ),
        "no_confirmed_deterioration": (
            "No se confirmó un deterioro material con el histórico disponible."
        ),
    }.get(reason, "La evidencia disponible requiere validación manual.")


def format_slack_reply(alert: dict[str, Any], result: dict[str, Any]) -> str:
    return (
        f"*Kipu Alert Reviewer*\n"
        f"*Resultado:* {result['label']}\n"
        f"*Tasa actual:* {float(alert['approval_rate']) * 100:.1f}%\n"
        f"*Motivo:* {result['reason']}"
    )


class SlackAcceptanceBot:
    def __init__(self, settings: SlackBotSettings | None = None) -> None:
        self.settings = settings or SlackBotSettings()
        self.store = SlackEventStore(self.settings.slack_processed_events_path)
        self.analyzer = SlackAcceptanceAnalyzer(self.settings)

    def run(self) -> None:
        from slack_sdk import WebClient
        from slack_sdk.socket_mode import SocketModeClient
        from slack_sdk.socket_mode.request import SocketModeRequest
        from slack_sdk.socket_mode.response import SocketModeResponse

        bot_token = self.settings.slack_bot_token.get_secret_value()
        app_token = self.settings.slack_app_token.get_secret_value()
        if not bot_token or not app_token:
            raise RuntimeError(
                "Configura SLACK_BOT_TOKEN y SLACK_APP_TOKEN antes de iniciar Slack."
            )
        client = SocketModeClient(app_token=app_token, web_client=WebClient(token=bot_token))

        def handle(_client: Any, request: SocketModeRequest) -> None:
            if request.type != "events_api":
                return
            client.send_socket_mode_response(SocketModeResponse(envelope_id=request.envelope_id))
            payload = request.payload or {}
            event = payload.get("event") or {}
            event_id = payload.get("event_id")
            if not _is_kipu_message(event, self.settings):
                return
            key = _event_key(event_id, event)
            if self.store.is_processed(key):
                return
            alert = parse_slack_kipu_alert(event)
            if alert is None:
                logger.info("Ignoring Slack message without complete Kipu acceptance payload")
                return
            result = self.analyzer.analyze(alert)
            reply = format_slack_reply(alert, result)
            if self.settings.slack_dry_run:
                logger.info("Slack dry-run key=%s reply=%s", key, reply.replace("\n", " | "))
            else:
                client.web_client.chat_postMessage(
                    channel=event["channel"],
                    thread_ts=event.get("thread_ts") or event["ts"],
                    text=reply,
                )
            self.store.mark_processed(
                key, {"alert_id": alert["alert_id"], "verdict": result["verdict"]}
            )

        client.socket_mode_request_listeners.append(handle)
        client.connect()
        client.start()


def run() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    SlackAcceptanceBot().run()


if __name__ == "__main__":
    run()
