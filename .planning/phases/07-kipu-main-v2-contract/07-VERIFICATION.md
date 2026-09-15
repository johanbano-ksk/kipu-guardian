---
phase: 07-kipu-main-v2-contract
policy_version: "2026-08-18.1"
verified: "2026-08-18"
status: passed
---

# Verification

## Executed evidence

| Area | Command/check | Result |
|---|---|---|
| Reviewer behavior | `.venv/Scripts/python.exe -m pytest -o addopts='' -q` | 126 passed |
| Reviewer static quality | `.venv/Scripts/python.exe -m ruff check src tests scripts` | Passed |
| Skill package | `quick_validate.py .codex/skills/filter-valid-alerts` | `Skill is valid!` |
| Skill parity v1/v2 | `filter_alerts.py` against both repository fixtures | One accepted payload, preserved exactly, for each fixture |
| Kipu focused producer/pipeline suite | Docker Python 3.12 over eventbridge, dedup, pipeline and secret-hygiene tests | 173 passed; one pre-existing S3-region warning |
| Kipu full suite | Final CodeBuild-shaped Python 3.12 image over the current tree | 474 passed; same pre-existing warning |
| Kipu primary production image | `src/Dockerfile` build plus non-root import smoke | `sha256:bd2c401550b2...`; LightGBM 4.7.0 and `src.main` import; per-file POS fix present |
| Kipu CodeBuild image | `infrastructure/Dockerfile` from the buildspec-shaped parent context | `sha256:3edfd746c004...`; built and passed the same non-root smoke |
| Mixed Parquet schemas | Legacy/current files with and without `channel`, in both orders | 2 passed; current POS rows excluded without dropping legacy files |
| Contract fixture | Kipu `_build_v2_detail` with fixed time → queue parser → reviewer filter | Deterministic UUIDv5; accepted with C1-C4 |
| LocalStack v1 | `scripts/local_e2e.py --event examples/kipu-event.json` | Accepted reached Hub; stable rejected; invalid reached DLQ |
| LocalStack v2/race | `scripts/local_e2e.py --event examples/kipu-event-v2.json --no-start` | Marked v1 became `SUPERSEDED_BY_V2`; v2 reached Hub; stable rejected; invalid reached DLQ |
| Output compatibility | Inspect LocalStack Hub event | Both accepted input versions emitted `Anomaly Validated v1` with original detail |
| Secret hygiene | Kipu `tests/test_secret_hygiene.py` | 2 passed; no webhook in current CDK context or context-copy code |
| Patch integrity | `git diff --check` in both repositories | Passed; only Git CRLF conversion notices |
| Local Docker entry points | README build command plus `docker compose config --quiet` | Parent-context paths resolve to the production Dockerfile |

The sole Kipu warning comes from the pre-existing Slack/TAPS catalog read trying
to infer an S3 region. It is unrelated to alert decisions and did not fail any
test.

## Requirements matrix

| Requirement | Evidence |
|---|---|
| KIPU-14–18 | Dual parser/evaluator tests, strict v2 invariants, priority recomputation and LocalStack v1/v2 |
| KIPU-19 | Predictive publisher tests show v1 plus legacy only, with quantiles preserved |
| KIPU-20 | POS, legacy-channel, Mongo unknown-count and count-invariant tests |
| REL-05 | UUIDv5, composite dedup and partial PutEvents failure tests |
| REL-06 | Same-ID v1-before-v2 worker regression and LocalStack race exercise |
| SEC-05 | Current-tree secret-hygiene tests; external rotation explicitly pending |
| COMP-04/05 | Dual EventBridge rule, exact version pairing and unchanged output detail tests |
| MAINT-06/07 | Producer-generated fixture, cross-contract run, full suites and image import smoke |

## External items not verified or performed

- No AWS development or production stack was updated in this phase.
- The Bitbucket remote could not be refreshed non-interactively; the source
  baseline is the locally tracked `origin/main` SHA recorded in the summary.
- The exposed Slack webhook cannot be made safe by a code change. It must be
  revoked/rotated by an authorized Slack owner and its Secrets Manager value
  replaced.
- Production bus/account topology, Hub shadow traffic and final cutover remain
  pending operational work.
