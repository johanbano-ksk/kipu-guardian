---
phase: 05-criteria-calibration
verified: "2026-08-12"
status: passed_for_calibration
---

# Verification

| Check | Result |
|---|---|
| Complete pytest suite | 45 passed |
| Ruff | All checks passed |
| Skill structure | Valid |
| Exact 5.00-point drop | Accepted with rolling-average reason |
| Drop below 5 points | `NO_SUPPORTED_SIGNAL` |
| Exact minimum volume 50 | Accepted |
| Volume 49 | `NO_SUPPORTED_SIGNAL` for rolling signal |
| Missing/blank/null merchant name | Rejected |
| Example human evaluation | Report generated with version `2026-08-12.1` |
| Container E2E: exact 5-point drop | Published to local Hub |
| Container E2E: stable alert | Filtered without Hub publication |
| Container E2E: missing merchant name | Not persisted; redriven to DLQ |
| Invalid-event receive counts | 1, 2 and 3 observed in worker logs |

## Interpretation boundary

The evaluator measures consistency with the payload-only policy. It does not
establish operational ground truth. Real calibration remains pending until
independent reviewers label a representative anonymized sample.

The fresh container execution traversed EventBridge, SQS, worker, DynamoDB and
the EventBridge output consumed by the local Hub queue. The invalid contract path
was also verified through SQS redrive and DLQ.
