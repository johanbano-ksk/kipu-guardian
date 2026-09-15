"""Export reviewer-captured EventBridge occurrences for one business day."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

import boto3

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from alert_reviewer.alert_filter import FilterPolicy  # noqa: E402
from alert_reviewer.occurrence_export import (  # noqa: E402
    DEFAULT_TIMEZONE,
    build_daily_export,
    load_dynamodb_occurrences,
    load_local_occurrences,
    write_daily_export,
)

DEFAULT_PROFILE = "ia-dev-payments-intelligence"
DEFAULT_REGION = "us-east-1"
DEFAULT_STACK = "pmt-intel-kipu-alert-reviewer-mvp"
DEFAULT_POLICY = PROJECT_ROOT / "config" / "filter_policy.yaml"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Export all occurrences, latest state, every policy-valid occurrence, "
            "and latest valid state from the reviewer's EventBridge archive."
        )
    )
    parser.add_argument("day", type=date.fromisoformat, help="Business day in YYYY-MM-DD")
    parser.add_argument(
        "--date-basis",
        choices=("publication", "observation"),
        default="publication",
        help=(
            "Publication uses EventBridge time in --timezone; observation uses only "
            "the explicit contractual observation_date."
        ),
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE, help="IANA business timezone")
    parser.add_argument("--profile", default=DEFAULT_PROFILE, help="AWS control-plane profile")
    parser.add_argument("--region", default=DEFAULT_REGION)
    parser.add_argument("--stack-name", default=DEFAULT_STACK)
    parser.add_argument("--table", help="Override the IdempotencyTableName stack output")
    parser.add_argument("--role-arn", help="Override the LocalWorkerRoleArn stack output")
    parser.add_argument(
        "--no-assume-role",
        action="store_true",
        help="Use current credentials directly; intended for LocalStack only.",
    )
    parser.add_argument("--endpoint-url", help="AWS endpoint override for local testing")
    parser.add_argument(
        "--source",
        type=Path,
        help="Read decoded occurrence JSON or DynamoDB scan JSON locally; never contacts AWS.",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "Destination for all.json, unique.json, valid.json, "
            "valid_unique.json, and summary.json."
        ),
    )
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    return parser.parse_args(argv)


def resolve_aws_source(args: argparse.Namespace) -> tuple[Any, str]:
    if args.endpoint_url and args.no_assume_role:
        control = boto3.Session(
            aws_access_key_id="test",
            aws_secret_access_key="test",
            region_name=args.region,
        )
    else:
        control = boto3.Session(profile_name=args.profile, region_name=args.region)
    table_name = args.table
    role_arn = args.role_arn
    if table_name is None or (role_arn is None and not args.no_assume_role):
        cloudformation = control.client(
            "cloudformation",
            endpoint_url=args.endpoint_url,
        )
        response = cloudformation.describe_stacks(StackName=args.stack_name)
        outputs = {
            item["OutputKey"]: item["OutputValue"]
            for item in response["Stacks"][0].get("Outputs", [])
        }
        table_name = table_name or outputs.get("IdempotencyTableName")
        role_arn = role_arn or outputs.get("LocalWorkerRoleArn")
    if not table_name:
        raise ValueError("Could not resolve the reviewer DynamoDB table name")

    runtime = control
    if not args.no_assume_role:
        if not role_arn:
            raise ValueError("Could not resolve the reviewer worker role ARN")
        sts = control.client("sts", endpoint_url=args.endpoint_url)
        credentials = sts.assume_role(
            RoleArn=role_arn,
            RoleSessionName="kipu-alert-reviewer-export",
        )["Credentials"]
        runtime = boto3.Session(
            aws_access_key_id=credentials["AccessKeyId"],
            aws_secret_access_key=credentials["SecretAccessKey"],
            aws_session_token=credentials["SessionToken"],
            region_name=args.region,
        )
    client = runtime.client("dynamodb", endpoint_url=args.endpoint_url)
    return client, table_name


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.source is not None:
        occurrences = load_local_occurrences(args.source.resolve())
        source_description = str(args.source.resolve())
    else:
        client, table_name = resolve_aws_source(args)
        occurrences = load_dynamodb_occurrences(client, table_name=table_name)
        source_description = f"dynamodb://{table_name}"

    policy = FilterPolicy.load(args.policy.resolve())
    (
        all_alerts,
        unique_alerts,
        valid_alerts,
        valid_unique_alerts,
        summary,
    ) = build_daily_export(
        occurrences,
        day=args.day,
        date_basis=args.date_basis,
        timezone=args.timezone,
        policy=policy,
    )
    output_dir = args.output_dir or (
        PROJECT_ROOT
        / "reports"
        / f"reviewer-occurrences-{args.date_basis}-{args.day.isoformat()}"
    )
    paths = write_daily_export(
        output_dir.resolve(),
        all_alerts=all_alerts,
        unique_alerts=unique_alerts,
        valid_alerts=valid_alerts,
        valid_unique_alerts=valid_unique_alerts,
        summary=summary,
    )
    print(
        json.dumps(
            {
                **summary,
                "source": source_description,
                "outputs": {name: str(path.resolve()) for name, path in paths.items()},
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
