---
phase: 08-hourly-alert-audit
status: partial
last_updated: "2026-08-19"
---

# Partial verification

Phase 08 remains open only for Docker and LocalStack runtime checks. Unit,
integration, export and static checks below are completed evidence.

## Requirements matrix

| Requirement | Required evidence | Status |
|---|---|---|
| KIPU-21 | Structurally valid v1/v2 events are captured with exact raw payload and SHA-256 before decision claim. | PASS |
| KIPU-22 | Publication uses EventBridge time/fallback correctly; observation uses only explicit v2 date. | PASS |
| REL-07 | `occurrence#…` and `decision#…` retries are idempotent; a same-key/different-payload collision fails closed. | PASS |
| REL-08 | Unit coverage proves capture failure blocks evaluation/ACK and malformed input creates no records; DLQ redrive still needs current LocalStack rerun. | PARTIAL |
| COMP-06 | Both date bases produce `all`, `unique`, `valid`, `valid_unique` and schema `1.1` summary with exact payloads. | PASS |
| MAINT-08 | Reviewer-only tests pass and Kipu is unchanged; current Docker/LocalStack checks remain pending. | PARTIAL |

## Evidence to record

| Check | Expected command or observation | Result |
|---|---|---|
| Reviewer suite | Complete collection with writable fixture override for sandbox ACL | `148/148` PASS |
| Static quality | `.venv/Scripts/python.exe -m ruff check src tests scripts .codex/skills/filter-valid-alerts/scripts` | PASS |
| Skill package | `quick_validate.py .codex/skills/filter-valid-alerts` | PASS |
| Local export | Example occurrence source creates all five files and schema `1.1` summary | PASS: `1/1/1/1`, exact payload |
| Capture ordering | Structurally valid accepted and rejected events create occurrence before decision | PASS |
| Malformed path | Invalid contract creates neither namespace; current DLQ E2E deferred | PARTIAL |
| Retry/collision | Same event+payload is idempotent; same identity+different payload fails | PASS |
| Date semantics | Guayaquil boundary and v2 observation selection pass; v1 is not inferred | PASS |
| LocalStack v1 | Accepted/rejected occurrences persist; accepted reaches Hub; invalid reaches DLQ | PENDING |
| LocalStack v2 | Transition, non-critical, accepted, rejected and invalid paths preserve the Phase 07 contract | PENDING |
| Docker | Current reviewer image builds and starts with the occurrence store | PENDING |
| Scope boundary | Read-only audit found no tracked Phase 08 Kipu source or commit and no deployment occurred | PASS |

## Review checks

Before changing this file to `passed`:

1. Insert the exact command results without replacing or rewriting Phase 07
   evidence.
2. Confirm `valid` evaluates all primary occurrences and `valid_unique` is
   selected from accepted occurrences rather than from `unique`.
3. Confirm a transition v1 remains archived but is excluded from all four alert
   sets and counted separately from unsupported contracts.
4. Confirm business rejections are archived and acknowledged, while malformed
   inputs remain unarchived and unacknowledged.
5. Confirm no AWS deployment occurred unless separately authorized and recorded.

No AWS resources have been deployed as part of this documentation correction.
