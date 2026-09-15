"""Publish payload-validated Kipu alerts for Hub consumption."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Protocol

from alert_reviewer.config import Settings


class ValidatedAlertPublishError(RuntimeError):
    pass


class EventBridgeClient(Protocol):
    def put_events(self, **kwargs: Any) -> dict[str, Any]: ...


class EventBridgeValidatedAlertPublisher:
    def __init__(self, client: EventBridgeClient, settings: Settings) -> None:
        if not settings.eventbridge_output_bus_name:
            raise ValueError("EVENTBRIDGE_OUTPUT_BUS_NAME is required")
        self.client = client
        self.settings = settings

    async def publish(self, alert: dict[str, Any]) -> None:
        try:
            detail = json.dumps(
                alert,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise ValidatedAlertPublishError(
                "Validated alert is not strict JSON"
            ) from exc

        response = await asyncio.to_thread(
            self.client.put_events,
            Entries=[
                {
                    "Source": self.settings.eventbridge_output_source,
                    "DetailType": self.settings.eventbridge_output_detail_type,
                    "Detail": detail,
                    "EventBusName": self.settings.eventbridge_output_bus_name,
                }
            ],
        )
        if response.get("FailedEntryCount", 0):
            failed = response.get("Entries", [{}])[0]
            error_code = failed.get("ErrorCode", "Unknown")
            raise ValidatedAlertPublishError(
                f"EventBridge rejected validated alert: {error_code}"
            )
