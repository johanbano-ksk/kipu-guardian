---
phase: 08-hourly-alert-audit
status: passed
started: "2026-08-18"
last_updated: "2026-09-24"
---

# Implementation summary

Phase 08 has been reformulated as a reviewer-owned occurrence archive. It does
not modify Kipu. The reviewer captures each structurally valid versioned
EventBridge event in its existing DynamoDB table before filter evaluation and
decision idempotency. JSON, envelope and contract failures remain outside the
archive and follow the existing retry/DLQ path.

Occurrence evidence uses `occurrence#event:<event-id>` or, when the EventBridge
ID is unavailable, `occurrence#sha256:<hash-del-envelope>`. Decision state uses
the same event identity under `decision#…`. Conditional writes make retries
idempotent and reject identity reuse with a different raw payload instead of
overwriting it.

The stored record preserves the original alert and its SHA-256. Publication
selection is based on EventBridge `time`, falling back to the alert's contractual
timestamp only when envelope time is absent. Observation selection uses only the
explicit `observation_date` available in schema `2.0`; v1 dates are never
inferred.

`scripts/export_hourly_alert_audit.py` reads reviewer occurrences from DynamoDB
or a local JSON source and produces:

- `all.json`: every supported primary occurrence in the selected cut;
- `unique.json`: latest occurrence per `alert_id`, regardless of decision;
- `valid.json`: every occurrence in `all` accepted by the payload-only policy;
- `valid_unique.json`: latest accepted occurrence per `alert_id`, selected from
  `valid` so a later rejection cannot hide an earlier acceptance;
- `summary.json`: export schema `1.1`, temporal basis, versions and counts.

A transition v1 event marked as superseded remains archived but is excluded
from all four alert sets and reported through `excluded_transition_count`.
Unsupported sources, detail types or schema/type pairs are reported separately
through `excluded_unsupported_count` rather than treated as valid Kipu alerts.

## Operational boundaries

- The archive contains only EventBridge events delivered to this reviewer. It
  is not a record of every internal Kipu execution or Slack notification.
- Capture begins only after the updated worker is deployed; historical events
  cannot be reconstructed.
- Occurrence retention uses the reviewer table TTL and must be sized for manual
  review needs.
- Output remains `acceptance.reviewer / Anomaly Validated v1` for Hub
  compatibility, including accepted schema `2.0` payloads.
- No AWS resource or worker deployment has been performed for Phase 08.

## Verification state

The final reviewer suite passed `1214/1214` executed tests with 7 documented
skips, Ruff passed, the Docker image built successfully, and LocalStack E2E
passed for accepted, rejected and malformed runtime paths. The local occurrence
example produced `all/unique/valid/valid_unique = 1/1/1/1` while preserving its
alert object exactly. Publication and observation exports both passed. No AWS
deployment occurred.
