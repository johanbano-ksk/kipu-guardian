---
phase: 08-hourly-alert-audit
status: passed
last_updated: "2026-09-24"
---

# Verification complete

Phase 08 is complete within the reviewer repository boundary. Unit, integration,
export, static, Docker and LocalStack checks passed. Runtime remains subscribed
only to Kipu's final `Anomaly Detected v1` / `schema_version=1.0` contract;
2.0 remains historical/offline support.

## Requirements matrix

| Requirement | Required evidence | Status |
|---|---|---|
| KIPU-21 | Structurally valid v1/v2 events are captured with exact raw payload and SHA-256 before decision claim. | PASS |
| KIPU-22 | Publication uses EventBridge time/fallback correctly; observation uses only explicit v2 date. | PASS |
| REL-07 | `occurrence#…` and `decision#…` retries are idempotent; a same-key/different-payload collision fails closed. | PASS |
| REL-08 | Unit coverage proves capture failure blocks evaluation/ACK and malformed input creates no records; LocalStack confirms DLQ redrive. | PASS |
| COMP-06 | Both date bases produce `all`, `unique`, `valid`, `valid_unique` and schema `1.1` summary with exact payloads. | PASS |
| MAINT-08 | Reviewer-only tests pass and Kipu is unchanged; Docker/LocalStack runtime checks pass. | PASS |

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
| LocalStack v1 | Accepted/rejected occurrences persist; accepted reaches Hub; invalid reaches DLQ | PASS: `scripts/local_e2e.py` |
| Runtime v2 boundary | Unnegotiated v2 is rejected by the active runtime; historical v2 tests remain offline | PASS: `test_integration_contract.py` |
| Docker | Current reviewer image builds and starts with the occurrence store | PASS: `docker build --no-cache -t kipu-alert-reviewer:mvp .` |
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

## Final runtime evidence — 2026-09-24

- `python -m pytest`: **1214 passed, 7 skipped** in 56.72s.
- Ruff: **passed** for `src`, `tests`, `scripts` and the repository skill.
- Docker image: **built successfully** as `kipu-alert-reviewer:mvp`.
- LocalStack E2E: **passed** for accepted, business-rejected and malformed
  messages. The accepted event reached Hub as `acceptance.reviewer /
  Anomaly Validated v1`; the rejected event did not reach Hub; the malformed
  event created no occurrence and reached the DLQ after redrive.
- Local occurrence export: **passed** with five output files and schema `1.1`;
  publication basis returned `all/unique/valid/valid_unique = 1/1/1/1` while
  preserving the raw payload exactly.
- Observation-basis export: **passed** and correctly excluded the v1 example
  because it has no explicit `observation_date`.
