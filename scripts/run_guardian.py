"""Run the Guardian agent without a dashboard or HTTP server."""

from alert_reviewer.guardian_cli import main

if __name__ == "__main__":
    raise SystemExit(main())
