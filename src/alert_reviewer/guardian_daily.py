"""On-demand local Guardian execution against the reviewer's real Kipu archive."""

from __future__ import annotations

import base64
import binascii
import json
import re
import time
from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from pydantic import Field, SecretStr

from alert_reviewer.ai_review import AIReviewError, OpenAIAlertReviewer, OpenAIReviewConfig
from alert_reviewer.alert_filter import FilterPolicy
from alert_reviewer.datalake_history import TIMEZONE, HistoryError
from alert_reviewer.gemini_review import GeminiAlertReviewer, GeminiReviewConfig, _decode_json
from alert_reviewer.guardian import GuardianSettings
from alert_reviewer.manual_snapshot import filter_occurrence_snapshot
from alert_reviewer.occurrence_export import _deserialize_item

MAX_SCAN_PAGES = 100
MAX_RECORDS = 20_000
MAX_SCAN_BYTES = 20_000_000
MAX_SCAN_SECONDS = 120
AWS_CONFIG = Config(connect_timeout=5, read_timeout=15, retries={"total_max_attempts": 2})
MAX_EXTRACTOR_BYTES = 2_000_000
LAMBDA_CONFIG = Config(connect_timeout=5, read_timeout=75, retries={"total_max_attempts": 1})


class DailyReviewError(HistoryError):
    """Safe, user-facing daily review failures; never contains provider response bodies."""


class DailyReviewSettings(GuardianSettings):
    alerts_aws_profile: str = "ia-dev-payments-intelligence"
    alerts_aws_region: str = "us-east-1"
    alerts_stack_name: str = "pmt-intel-kipu-alert-reviewer-mvp"
    alerts_extractor_function: str = Field(
        default="kipu-alert-reviewer-manual-extractor",
        pattern=r"^[A-Za-z0-9_-]{1,64}$",
    )
    agent_execution_key: SecretStr = SecretStr("")
    ai_provider: str = "gemini"
    openai_api_key: SecretStr = SecretStr("")
    openai_model: str = "gpt-5.6-luna"
    openai_timeout_seconds: float = Field(default=30, ge=1, le=90)


def _source_error(error: BotoCoreError | ClientError) -> DailyReviewError:
    name = type(error).__name__
    code = error.response.get("Error", {}).get("Code", "") if isinstance(error, ClientError) else ""
    if "SSO" in name or "Token" in name or code in {"ExpiredToken", "ExpiredTokenException"}:
        return DailyReviewError("SSO_EXPIRED", "Renueva la sesión AWS del archivo de alertas.")
    if "AccessDenied" in code or "Unauthorized" in code:
        return DailyReviewError(
            "ALERT_SOURCE_ACCESS_DENIED", "No hay acceso al archivo de alertas."
        )
    return DailyReviewError("EXTRACTION_FAILED", "No se pudo leer el archivo de alertas Kipu.")


class ManualExtractorClient:
    """Invoke the existing authorized extractor, never assume its role or scan its table."""

    def __init__(self, settings: DailyReviewSettings, client: Any = None) -> None:
        self.settings = settings
        self.client = client

    def extract(self, day: str, mode: str) -> dict[str, Any]:
        settings = self.settings
        try:
            client = self.client
            if client is None:
                session = boto3.Session(
                    profile_name=settings.alerts_aws_profile,
                    region_name=settings.alerts_aws_region,
                )
                client = session.client("lambda", config=LAMBDA_CONFIG)
            key = settings.agent_execution_key.get_secret_value()
            if not key:
                # IAM-authorized configuration read of this exact function. Keep the
                # existing shared credential only in memory, never in reports or UI.
                config = client.get_function_configuration(
                    FunctionName=settings.alerts_extractor_function,
                )
                key = config.get("Environment", {}).get("Variables", {}).get("AGENT_EXECUTION_KEY")
            if not isinstance(key, str) or not key.strip():
                raise DailyReviewError(
                    "AGENT_CONFIGURATION_FAILED",
                    "Falta la autenticación del extractor existente.",
                )
            response = client.invoke(
                FunctionName=settings.alerts_extractor_function,
                InvocationType="RequestResponse",
                Payload=json.dumps(
                    {
                        "headers": {"x-agent-key": key},
                        "body": json.dumps({"date": day, "mode": mode}),
                    },
                    ensure_ascii=True,
                ).encode(),
            )
            stream = response.get("Payload")
            if stream is None:
                raise ValueError("Missing Lambda payload")
            try:
                raw = stream.read(MAX_EXTRACTOR_BYTES + 1)
            finally:
                stream.close()
            if len(raw) > MAX_EXTRACTOR_BYTES:
                raise DailyReviewError("AGENT_RESPONSE_TOO_LARGE", "La respuesta excede el límite.")
            if response.get("StatusCode") != 200 or response.get("FunctionError"):
                raise DailyReviewError("EXTRACTION_FAILED", "El extractor no completó la revisión.")
            envelope = _decode_json(raw)
            if not isinstance(envelope, dict) or type(envelope.get("statusCode")) is not int:
                raise ValueError("Invalid Lambda envelope")
            body = envelope.get("body")
            if not isinstance(body, str):
                raise ValueError("Invalid Lambda body")
            encoded = envelope.get("isBase64Encoded", False)
            if type(encoded) is not bool:
                raise ValueError("Invalid Lambda body encoding")
            if encoded:
                body = base64.b64decode(body, validate=True).decode("utf-8")
            snapshot = _decode_json(body)
            if not isinstance(snapshot, dict):
                raise ValueError("Invalid extraction result")
            if envelope["statusCode"] != 200:
                code = snapshot.get("error")
                # Never return arbitrary provider messages, tracebacks or credentials.
                messages = {
                    "INVALID_DATE": "Selecciona una fecha válida.",
                    "FUTURE_DATE": "Selecciona hoy o una fecha anterior.",
                    "INVALID_REVIEW_MODE": "Selecciona un método de revisión válido.",
                    "SNAPSHOT_NOT_FOUND": "No hay ocurrencias archivadas para esa fecha.",
                    "AI_NOT_CONFIGURED": "El modo IA del extractor no está configurado.",
                    "AI_REVIEW_FAILED": "La IA del extractor no completó la revisión.",
                    "AGENT_CONFIGURATION_FAILED": "El extractor requiere revisar su configuración.",
                    "EXTRACTION_FAILED": "El extractor no pudo consultar las alertas.",
                }
                if code == "UNAUTHORIZED" or envelope["statusCode"] in (401, 403):
                    raise DailyReviewError(
                        "EXTRACTOR_AUTH_FAILED",
                        "El extractor rechazó la autenticación configurada.",
                    )
                if not isinstance(code, str) or code not in messages:
                    code = "EXTRACTION_FAILED"
                raise DailyReviewError(code, messages[code])
            if (
                snapshot.get("business_date") != day
                or snapshot.get("review_mode") != mode
                or snapshot.get("schema_version") != "1.0"
                or snapshot.get("source") not in ("eventbridge_occurrences", "s3_snapshot")
            ):
                raise ValueError("Wrong extraction identity")
            return snapshot
        except (BotoCoreError, ClientError) as error:
            raise _source_error(error) from None
        except OSError:
            raise DailyReviewError(
                "EXTRACTION_FAILED",
                "La conexión con el extractor no se pudo completar.",
            ) from None
        except (
            ValueError,
            TypeError,
            KeyError,
            AttributeError,
            UnicodeError,
            binascii.Error,
            RecursionError,
        ):
            raise DailyReviewError(
                "INVALID_AGENT_RESPONSE",
                "El extractor devolvió una respuesta inválida.",
            ) from None


class OccurrenceReader:
    def __init__(self, settings: DailyReviewSettings) -> None:
        self.settings = settings

    def _client(self) -> tuple[Any, str]:
        # Reuse the archive and role established by export_hourly_alert_audit.py.
        settings = self.settings
        control = boto3.Session(
            profile_name=settings.alerts_aws_profile,
            region_name=settings.alerts_aws_region,
        )
        stack = control.client("cloudformation", config=AWS_CONFIG).describe_stacks(
            StackName=settings.alerts_stack_name,
        )
        outputs = {
            entry["OutputKey"]: entry["OutputValue"]
            for entry in stack["Stacks"][0].get("Outputs", [])
        }
        table, role = outputs.get("IdempotencyTableName"), outputs.get("LocalWorkerRoleArn")
        if not table or not role:
            raise DailyReviewError(
                "AGENT_CONFIGURATION_FAILED", "Falta configurar el archivo Kipu."
            )
        credentials = control.client("sts", config=AWS_CONFIG).assume_role(
            RoleArn=role,
            RoleSessionName="kipu-guardian-daily-review",
        )["Credentials"]
        runtime = boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=settings.alerts_aws_region,
        )
        return runtime.client("dynamodb", config=AWS_CONFIG), table

    def read(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        try:
            deadline = time.monotonic() + MAX_SCAN_SECONDS
            client, table = self._client()
            request: dict[str, Any] = {
                "TableName": table,
                "ConsistentRead": True,
                "FilterExpression": "begins_with(idempotency_key, :prefix)",
                "ExpressionAttributeValues": {":prefix": {"S": "occurrence#"}},
            }
            seen: set[str] = set()
            size = 0
            for _ in range(MAX_SCAN_PAGES):
                if time.monotonic() >= deadline:
                    break
                page = client.scan(**request)
                size += len(json.dumps(page, ensure_ascii=True).encode())
                items = page.get("Items", [])
                if (
                    size > MAX_SCAN_BYTES
                    or len(records) + len(items) > MAX_RECORDS
                    or time.monotonic() >= deadline
                ):
                    break
                records.extend(_deserialize_item(item) for item in items)
                cursor = page.get("LastEvaluatedKey")
                if not cursor:
                    return records
                identity = json.dumps(cursor, sort_keys=True)
                if identity in seen:
                    break
                seen.add(identity)
                request["ExclusiveStartKey"] = cursor
        except (BotoCoreError, ClientError) as error:
            raise _source_error(error) from None
        raise DailyReviewError(
            "EXTRACTION_LIMIT",
            "El archivo excede el límite de consulta; no se muestran datos parciales.",
        )


class GuardianDailyReview:
    def __init__(self, settings: DailyReviewSettings | None = None, reader: Any = None) -> None:
        self.settings = settings or DailyReviewSettings()
        # Direct archive readers are explicit offline/test dependencies only. The
        # dashboard's default always uses the existing Lambda and its own IAM role.
        self.reader = reader

    def run(self, day: str, mode: str) -> dict[str, Any]:
        try:
            if not isinstance(day, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
                raise ValueError("Invalid date")
            requested = date.fromisoformat(day)
        except ValueError:
            raise DailyReviewError("INVALID_DATE", "Selecciona una fecha válida.") from None
        if requested > datetime.now(ZoneInfo(TIMEZONE)).date():
            raise DailyReviewError("FUTURE_DATE", "Selecciona hoy o una fecha anterior en Ecuador.")
        if mode not in ("policy", "ai"):
            raise DailyReviewError(
                "INVALID_REVIEW_MODE", "Selecciona un método de revisión válido."
            )
        if self.reader is None:
            return ManualExtractorClient(self.settings).extract(day, mode)
        policy = FilterPolicy.load(self.settings.filter_policy_path)
        reviewer: GeminiAlertReviewer | OpenAIAlertReviewer | None = None
        provider = None
        if mode == "ai":
            provider = self.settings.ai_provider.strip().lower()
            if provider == "gemini":
                key = self.settings.gemini_api_key.get_secret_value()
                if not key:
                    raise DailyReviewError(
                        "AI_NOT_CONFIGURED", "Configura Gemini en el agente local."
                    )
                reviewer = GeminiAlertReviewer(
                    GeminiReviewConfig(
                        api_key=key,
                        model=self.settings.gemini_model,
                        timeout_seconds=self.settings.gemini_timeout_seconds,
                    ),
                    policy,
                )
            elif provider == "openai":
                key = self.settings.openai_api_key.get_secret_value()
                if not key:
                    raise DailyReviewError("AI_NOT_CONFIGURED", "La IA no está configurada.")
                reviewer = OpenAIAlertReviewer(
                    OpenAIReviewConfig(
                        api_key=key,
                        model=self.settings.openai_model,
                        timeout_seconds=self.settings.openai_timeout_seconds,
                    ),
                    policy,
                )
            else:
                raise DailyReviewError("AI_NOT_CONFIGURED", "Proveedor de IA no soportado.")
        try:
            accepted, evaluated, received, countries = filter_occurrence_snapshot(
                self.reader.read(),
                requested,
                policy=policy,
                timezone=TIMEZONE,
                ai_selector=reviewer.select if reviewer else None,
            )
        except AIReviewError:
            raise DailyReviewError(
                "AI_REVIEW_FAILED",
                "La IA no completó la revisión. No se guardó un resultado nuevo.",
            ) from None
        if received == 0:
            raise DailyReviewError(
                "SNAPSHOT_NOT_FOUND",
                "No hay ocurrencias archivadas para esa fecha; esto no prueba ausencia en Slack.",
            )
        return {
            "schema_version": "1.0",
            "business_date": day,
            "extracted_at": datetime.now(UTC).isoformat(),
            "source": "eventbridge_occurrences",
            "source_record_count": received,
            "evaluated_record_count": evaluated,
            "review_mode": mode,
            "review_provider": provider,
            "review_model": reviewer.config.model if reviewer else None,
            "policy_version": policy.version,
            "country_summary": countries,
            "accepted_alerts": accepted,
        }
