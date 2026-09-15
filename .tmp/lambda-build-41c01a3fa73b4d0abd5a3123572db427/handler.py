"""AWS Lambda entry point for on-demand dashboard extraction."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.exceptions import ClientError

from alert_reviewer.ai_review import AIReviewError, OpenAIAlertReviewer, OpenAIReviewConfig
from alert_reviewer.alert_filter import FilterPolicy
from alert_reviewer.manual_snapshot import (
    filter_occurrence_snapshot,
    filter_snapshot,
    snapshot_key,
)

S3_BUCKET = os.environ["KIPU_ALERTS_BUCKET"]
OCCURRENCE_TABLE = os.environ.get("REVIEW_OCCURRENCE_TABLE", "")
DASHBOARD_TIMEZONE = os.environ.get("DASHBOARD_TIMEZONE", "America/Guayaquil")
SHARED_SECRET = os.environ["AGENT_EXECUTION_KEY"]
POLICY_PATH = os.environ.get("FILTER_POLICY_PATH", "/var/task/config/filter_policy.yaml")
logger = logging.getLogger(__name__)
REVIEW_MODES = {"policy", "ai"}


def _occurrence_records() -> list[dict[str, Any]]:
    table = boto3.resource("dynamodb").Table(OCCURRENCE_TABLE)
    records: list[dict[str, Any]] = []
    request: dict[str, Any] = {
        "FilterExpression": "begins_with(idempotency_key, :prefix)",
        "ExpressionAttributeValues": {":prefix": "occurrence#"},
    }
    while True:
        response = table.scan(**request)
        records.extend(response.get("Items", []))
        key = response.get("LastEvaluatedKey")
        if not key:
            return records
        request["ExclusiveStartKey"] = key


def _response(status: int, payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json; charset=utf-8"},
        "body": json.dumps(payload, ensure_ascii=False),
    }


def _request_body(event: dict[str, Any]) -> dict[str, Any]:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("Request body must be a JSON object")
    return payload


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    headers = {str(key).lower(): str(value) for key, value in (event.get("headers") or {}).items()}
    provided_secret = headers.get("x-agent-key", "")
    if not provided_secret or not hmac.compare_digest(provided_secret, SHARED_SECRET):
        return _response(401, {"error": "UNAUTHORIZED"})

    try:
        request_payload = _request_body(event)
        requested_day = date.fromisoformat(str(request_payload.get("date", "")))
    except (ValueError, TypeError, json.JSONDecodeError):
        return _response(400, {"error": "INVALID_DATE"})
    if requested_day > datetime.now(UTC).date():
        return _response(400, {"error": "FUTURE_DATE"})

    review_mode = str(request_payload.get("mode", "policy"))
    if review_mode not in REVIEW_MODES:
        return _response(400, {"error": "INVALID_REVIEW_MODE"})

    try:
        policy = FilterPolicy.load(Path(POLICY_PATH))
        ai_reviewer: OpenAIAlertReviewer | None = None
        if review_mode == "ai":
            api_key = os.environ.get("OPENAI_API_KEY", "")
            if not api_key:
                return _response(503, {"error": "AI_NOT_CONFIGURED"})
            ai_reviewer = OpenAIAlertReviewer(
                OpenAIReviewConfig(
                    api_key=api_key,
                    model=os.environ.get("OPENAI_MODEL", "gpt-5.6-luna"),
                    timeout_seconds=float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "30")),
                ),
                policy,
            )
    except (OSError, TypeError, ValueError):
        logger.exception("manual_snapshot_configuration_failed")
        return _response(500, {"error": "AGENT_CONFIGURATION_FAILED"})

    try:
        if OCCURRENCE_TABLE:
            records = _occurrence_records()
            accepted, projected_count, source_count, country_summary = filter_occurrence_snapshot(
                records,
                requested_day,
                policy=policy,
                timezone=DASHBOARD_TIMEZONE,
                ai_selector=ai_reviewer.select if ai_reviewer else None,
            )
            if source_count == 0:
                return _response(404, {"error": "SNAPSHOT_NOT_FOUND"})
            extraction_source = "eventbridge_occurrences"
        else:
            response = boto3.client("s3").get_object(
                Bucket=S3_BUCKET,
                Key=snapshot_key(requested_day),
            )
            rows = json.loads(response["Body"].read().decode("utf-8"))
            if not isinstance(rows, list) or not all(isinstance(item, dict) for item in rows):
                raise ValueError("Kipu alerts.json must contain an array of objects")
            accepted, projected_count, country_summary = filter_snapshot(
                rows,
                requested_day,
                policy=policy,
                ai_selector=ai_reviewer.select if ai_reviewer else None,
            )
            source_count = len(rows)
            extraction_source = "s3_snapshot"
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "UNKNOWN")
        if error_code in {"NoSuchKey", "404"}:
            return _response(404, {"error": "SNAPSHOT_NOT_FOUND"})
        logger.exception("manual_snapshot_s3_failed", extra={"error_code": error_code})
        return _response(500, {"error": "EXTRACTION_FAILED"})
    except AIReviewError:
        logger.exception("manual_snapshot_ai_review_failed")
        return _response(502, {"error": "AI_REVIEW_FAILED"})
    except Exception:
        logger.exception("manual_snapshot_extraction_failed")
        return _response(500, {"error": "EXTRACTION_FAILED"})

    return _response(
        200,
        {
            "schema_version": "1.0",
            "business_date": requested_day.isoformat(),
            "extracted_at": datetime.now(UTC).isoformat(),
            "source": extraction_source,
            "source_record_count": source_count,
            "evaluated_record_count": projected_count,
            "review_mode": review_mode,
            "review_model": ai_reviewer.config.model if ai_reviewer else None,
            "policy_version": policy.version,
            "country_summary": country_summary,
            "accepted_alerts": accepted,
        },
    )
