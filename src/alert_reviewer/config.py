"""Environment configuration for the asynchronous Kipu worker."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        case_sensitive=False,
        extra="ignore",
    )

    log_level: str = "INFO"
    aws_region: str = "us-east-1"

    sqs_input_queue_url: str | None = None
    sqs_idempotency_table: str | None = None
    sqs_wait_time_seconds: int = Field(default=20, ge=0, le=20)
    sqs_visibility_timeout_seconds: int = Field(default=120, ge=1, le=43_200)
    sqs_max_messages: int = Field(default=10, ge=1, le=10)
    sqs_idempotency_ttl_hours: int = Field(default=168, ge=1)
    alert_occurrence_ttl_hours: int = Field(default=720, ge=1)

    eventbridge_output_bus_name: str | None = None
    eventbridge_output_source: str = "acceptance.reviewer"
    eventbridge_output_detail_type: str = "Anomaly Validated v1"

    filter_policy_path: Path = Path("config/filter_policy.yaml")


@lru_cache
def get_settings() -> Settings:
    return Settings()
