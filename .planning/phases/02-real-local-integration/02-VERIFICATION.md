---
phase: 02-real-local-integration
verified: "2026-07-30"
status: passed
score: "5/5 observable truths verified"
---

# Verification

| Truth | Result |
|---|---|
| Kipu v1 traverses EventBridge and SQS | Passed |
| Accepted alert is persisted and reaches Hub SQS | Passed |
| Stable alert is persisted and does not reach Hub | Passed |
| Worker uses DynamoDB idempotency | Passed |
| The complete run is reproducible with one command | Passed twice |

## Evidence

- `scripts/local_e2e.py` completed successfully.
- Accepted reason: `APPROVAL_DROP_FROM_ROLLING_AVERAGE`.
- Accepted output: `acceptance.reviewer / Anomaly Validated v1`.
- Rejected reason: `NO_SUPPORTED_SIGNAL`.
- Rejected output: no Hub message.
- Worker logs contain `queued_alert_validated` and `queued_alert_filtered`.
- Full pytest: 50 passed.
- Ruff: all checks passed.

## Historical AWS rollout boundary

The Kipu `.env` contains a temporary AWS session, but AWS returned
`InvalidClientTokenId`. No AWS resources were changed and no credential was
copied into this repository.

This was the boundary on 2026-07-30. Phase 03 later records the successful SSO
login, AWS deployment and live Kipu event processing.
