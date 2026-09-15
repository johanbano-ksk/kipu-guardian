from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "export_hourly_alert_audit.py"
SPEC = importlib.util.spec_from_file_location("export_hourly_alert_audit", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


class _FakeCloudFormation:
    def describe_stacks(self, **kwargs):
        assert kwargs == {"StackName": "pmt-intel-kipu-alert-reviewer-mvp"}
        return {
            "Stacks": [
                {
                    "Outputs": [
                        {
                            "OutputKey": "IdempotencyTableName",
                            "OutputValue": "reviewer-table",
                        },
                        {
                            "OutputKey": "LocalWorkerRoleArn",
                            "OutputValue": "arn:aws:iam::123456789012:role/reviewer",
                        },
                    ]
                }
            ]
        }


class _FakeSTS:
    def assume_role(self, **kwargs):
        assert kwargs["RoleArn"] == "arn:aws:iam::123456789012:role/reviewer"
        return {
            "Credentials": {
                "AccessKeyId": "assumed-key",
                "SecretAccessKey": "assumed-secret",
                "SessionToken": "assumed-token",
            }
        }


class _FakeDynamoDB:
    pass


class _FakeSession:
    def __init__(self, factory, kwargs):
        self.factory = factory
        self.kwargs = kwargs

    def client(self, service, **kwargs):
        self.factory.client_calls.append((self.kwargs, service, kwargs))
        return {
            "cloudformation": self.factory.cloudformation,
            "sts": self.factory.sts,
            "dynamodb": self.factory.dynamodb,
        }[service]


class _SessionFactory:
    def __init__(self):
        self.calls = []
        self.client_calls = []
        self.cloudformation = _FakeCloudFormation()
        self.sts = _FakeSTS()
        self.dynamodb = _FakeDynamoDB()

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeSession(self, kwargs)


def test_real_aws_source_resolves_stack_and_assumes_worker_role(monkeypatch):
    factory = _SessionFactory()
    monkeypatch.setattr(cli.boto3, "Session", factory)
    args = cli.parse_args(["2026-08-18"])

    client, table_name = cli.resolve_aws_source(args)

    assert client is factory.dynamodb
    assert table_name == "reviewer-table"
    assert factory.calls[0] == {
        "profile_name": "ia-dev-payments-intelligence",
        "region_name": "us-east-1",
    }
    assert factory.calls[1]["aws_access_key_id"] == "assumed-key"
    assert [service for _session, service, _kwargs in factory.client_calls] == [
        "cloudformation",
        "sts",
        "dynamodb",
    ]


def test_localstack_override_skips_stack_and_role(monkeypatch):
    factory = _SessionFactory()
    monkeypatch.setattr(cli.boto3, "Session", factory)
    args = cli.parse_args(
        [
            "2026-08-18",
            "--table",
            "local-table",
            "--no-assume-role",
            "--endpoint-url",
            "http://localhost:4566",
        ]
    )

    client, table_name = cli.resolve_aws_source(args)

    assert client is factory.dynamodb
    assert table_name == "local-table"
    assert len(factory.calls) == 1
    assert factory.calls[0] == {
        "aws_access_key_id": "test",
        "aws_secret_access_key": "test",
        "region_name": "us-east-1",
    }
    assert [service for _session, service, _kwargs in factory.client_calls] == [
        "dynamodb"
    ]
