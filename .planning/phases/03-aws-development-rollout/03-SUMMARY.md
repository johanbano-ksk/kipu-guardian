---
phase: 03-aws-development-rollout
status: mvp_live
completed: "2026-08-11"
---

# Summary

The MVP now consumes real Kipu events from AWS development.

## Deployed

- Stack: `pmt-intel-kipu-alert-reviewer-mvp`
- Bus: `acceptance-intelligence-bus-dev`
- Rule: `pmt-intel-kipu-alert-reviewer-mvp-events`
- Input: `pmt-intel-kipu-alert-reviewer-mvp-input`
- DLQ: `pmt-intel-kipu-alert-reviewer-mvp-dlq`
- DynamoDB: `pmt-intel-kipu-alert-reviewer-mvp-idempotency`
- Runtime role: `pmt-intel-kipu-alert-reviewer-mvp-local-worker-role`

## First live batch

- Received: 108
- Persisted: 108
- Validated: 3
- Filtered: 105
- Processing failures: 0

The existing direct Kipu to Hub rule was not changed.
