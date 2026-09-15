#!/usr/bin/env python3
"""CLI wrapper around the reviewer's production payload filter."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[4]
SOURCE_ROOT = REPOSITORY_ROOT / "src"
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from alert_reviewer.alert_filter import FilterPolicy, filter_alerts  # noqa: E402

DEFAULT_POLICY_PATH = REPOSITORY_ROOT / "config" / "filter_policy.yaml"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Show only critical alerts supported by their own payload metrics."
    )
    parser.add_argument("input", help="JSON file path, or - to read stdin")
    parser.add_argument(
        "--policy",
        type=Path,
        default=DEFAULT_POLICY_PATH,
        help="Versioned policy YAML (defaults to the production policy)",
    )
    parser.add_argument("--compact", action="store_true")
    return parser


def main() -> int:
    args = _parser().parse_args()
    raw = (
        sys.stdin.read()
        if args.input == "-"
        else Path(args.input).read_text(encoding="utf-8")
    )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON: {exc}") from exc

    policy = FilterPolicy.load(args.policy)
    indent = None if args.compact else 2
    print(json.dumps(filter_alerts(payload, policy), ensure_ascii=False, indent=indent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
