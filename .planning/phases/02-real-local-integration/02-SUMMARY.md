---
phase: 02-real-local-integration
status: complete
completed: "2026-07-30"
---

# Summary

Implemented and executed the real reviewer topology locally.

## Delivered

- Pinned LocalStack runtime for EventBridge, SQS and DynamoDB.
- Dockerized reviewer worker connected through AWS-compatible endpoints.
- Input rule for `acceptance.kipu / Anomaly Detected v1`.
- Output rule and SQS queue representing Hub.
- Durable DynamoDB outcomes for both accepted and rejected alerts.
- One-command runner at `scripts/local_e2e.py`.

## Verified flow

```text
Kipu event
  -> EventBridge
  -> reviewer input SQS
  -> worker
  -> DynamoDB outcome
  -> EventBridge (accepted only)
  -> local Hub SQS
```

An accepted alert reached Hub as `Anomaly Validated v1`. A stable alert was
completed with `NO_SUPPORTED_SIGNAL` and did not reach Hub.

