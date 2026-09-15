from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from alert_reviewer.config import Settings


def test_worker_defaults_are_safe_and_bounded():
    settings = Settings(_env_file=None)

    assert settings.aws_region == "us-east-1"
    assert settings.sqs_wait_time_seconds == 20
    assert settings.sqs_visibility_timeout_seconds == 120
    assert settings.sqs_max_messages == 10
    assert settings.alert_occurrence_ttl_hours == 720
    assert settings.filter_policy_path == Path("config/filter_policy.yaml")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("sqs_wait_time_seconds", 21),
        ("sqs_visibility_timeout_seconds", 0),
        ("sqs_max_messages", 11),
        ("sqs_idempotency_ttl_hours", 0),
        ("alert_occurrence_ttl_hours", 0),
    ],
)
def test_rejects_invalid_worker_bounds(field, value):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})


def test_empty_optional_env_values_are_ignored(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            [
                "SQS_INPUT_QUEUE_URL=",
                "SQS_IDEMPOTENCY_TABLE=",
                "EVENTBRIDGE_OUTPUT_BUS_NAME=",
            ]
        ),
        encoding="utf-8",
    )

    settings = Settings(_env_file=env_file)

    assert settings.sqs_input_queue_url is None
    assert settings.sqs_idempotency_table is None
    assert settings.eventbridge_output_bus_name is None


def test_example_env_file_is_loadable():
    env_file = Path(__file__).parents[1] / ".env.example"

    settings = Settings(_env_file=env_file)

    assert settings.aws_region == "us-east-1"
    assert settings.eventbridge_output_source == "acceptance.reviewer"
    assert settings.sqs_input_queue_url is None
