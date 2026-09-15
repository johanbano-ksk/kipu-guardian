---
phase: 06-critical-only-policy
policy_version: "2026-08-13.1"
verified: "2026-08-13"
status: passed
---

# Verification

Policy `2026-08-13.1` passed its implementation, boundary, skill, replay and
topology checks. All evidence below was captured from the repository state on
2026-08-13; no AWS queue was consumed during this verification.

## Policy and implementation parity

| Check | Result |
|---|---|
| Policy version | `FilterPolicy.load(config/filter_policy.yaml)` returned `2026-08-13.1`. |
| Production loader | The loaded dataclass equals the complete default policy, including global gates and A–F thresholds. |
| Repository skill | Integration test and direct CLI execution used the production implementation and default policy file. |
| Skill package | `quick_validate.py` returned `Skill is valid!`. |
| Independent skill test | From a three-alert batch, the skill returned only the accented critical alert that met branch A. |
| Accepted payload | Integration and unit tests confirmed that output equals the original `detail`, without added fields. |
| Evidence boundary | Static regression confirmed the worker has no Elastic, OpenSearch, LLM or external-evidence dependency. |
| Persisted trace | New outcomes include optional `policy_version`; legacy outcomes without it still deserialize. |
| Contract typing | Numeric strings, booleans and non-ISO/date-only timestamps are rejected before filtering, preserving retry/DLQ behavior for malformed payloads. |
| Criticality parity | SQS contract and direct filter normalize case/accent identically. |

## Boundary evidence

The automated suite exercises the following inclusive borders and negative
controls:

| Area | Passed cases |
|---|---|
| Global gates | `Critica` and `Crítica`; non-critical `Alta`, `Media`, `Baja`; invalid severity; transaction 19/20; decline 19/20; inconsistent counts. |
| A — mass impact | `n=100`, AR `0.15` accepts; `n=99` and AR `0.1501` reject without another branch. |
| B — severe deterioration | `n=50`, AR `0.20`, exact 5pp drop accepts; `n=49`, AR above limit, drop below limit and absent rolling value reject. |
| C — extreme low rate | `n=20`, AR `0`, declines 20 and exact 10pp drop accepts; exact AR `0.10` accepts at coherent volume; rate/drop just outside reject. |
| D — high-volume collapse | `n=100`, AR `0.50`, exact 10pp drop accepts; volume/rate/drop just outside reject. |
| E — predictive approval | `n=50` and exact 5pp q10 gap accepts; low volume and sub-threshold gap reject. |
| F — predictive declines | Exact excess 5 and exact high-volume 5% excess accept; values immediately below either margin reject. |
| Optional data | Missing rolling/predictive data never creates a signal; invalid metrics and disordered quantiles reject. |
| Multiple branches | A+B+C+D are emitted once each in deterministic order. |
| Compatibility | Historical `APPROVAL_DROP_FROM_ROLLING_AVERAGE` outcomes still deserialize with `policy_version=None`. |
| Float representation | A–D rate ceilings and all drop/margin floors accept machine-level representations of their exact limits. |

The branch C minimum-volume case uses AR zero because `n=20`, 20 declines and
AR `0.10` would be internally inconsistent. Its exact rate boundary is tested
separately at coherent volume.

## Automated quality

| Command | Observed result |
|---|---|
| `.\.venv\Scripts\python.exe -m pytest -p no:cacheprovider` | 90 passed in 3.46s. |
| `.\.venv\Scripts\python.exe -m ruff check src tests scripts .codex/skills/filter-valid-alerts/scripts` | `All checks passed!` |
| `.\.venv\Scripts\python.exe -m compileall -q src scripts .codex/skills/filter-valid-alerts/scripts` | Completed with no errors. |
| Skill CLI over `examples/kipu-event.json` | One unchanged alert returned; accepted only by `SEVERE_APPROVAL_DETERIORATION`. |
| Offline evaluator examples | 9 cases; 6 accepted, 3 rejected; policy version `2026-08-13.1`; adjudicated TP=6, FP=0, FN=0, TN=3. |

## Container and topology

Docker Engine `29.5.3` was available. `docker compose -f compose.local.yaml
config --quiet` passed and `docker build --no-cache -t
kipu-alert-reviewer:mvp .` completed successfully.

The LocalStack E2E then verified:

| Path | Observed result |
|---|---|
| Critical accepted | `SEVERE_APPROVAL_DETERIORATION`; published as `acceptance.reviewer / Anomaly Validated v1`. |
| Critical without material signal | `NO_CRITICAL_SIGNAL`; persisted/acknowledged and absent from Hub. |
| Missing `merchant_name` | Not persisted or published; reached DLQ after redrive. |

The Compose topology was stopped and removed after the successful run.

## Preserved 2026-08-13 replay

Input: `reports/filtered-alerts-2026-08-13.json`, the 50 alerts accepted by the
previous policy.

| Check | Observed result |
|---|---|
| Source count | 50 unique alert IDs. |
| Critical-only retained | 11 (22%). |
| Rejected from prior accepted set | 39 (78%). |
| Source SHA-256 before/after | `9ddc32945b2c5860a2664d0d59eda601fb774e4129514451fdb7c886ae45f10b`; unchanged. |
| Versioned result | `reports/critical-alerts-2026-08-13-policy-2026-08-13.1.md`. |
| Regression | All 11 IDs are fixed in the integration test and reevaluated by the production policy. |

The 11 comprise A=2, B=3, C=8 and D=1 when branches overlap. `BORDER CREDIT`
meets A+B+C+D. E/F did not participate because the historical payloads lack the
predictive fields. This is a selectivity replay over the 50 previously accepted
alerts, not the unavailable complete original batch of 393.

## Final verdict

**PASSED.** The worker, project skill, fixtures, tests and operational docs share
policy `2026-08-13.1`. Local execution supports all global gates and branches
A–F, and the policy reduced the preserved accepted-only sample from 50 to 11.

AWS activation remains an explicit rollout step: restart/redeploy the worker to
load the YAML. Existing DynamoDB `READY`/`COMPLETED` outcomes retain their prior
decisions to preserve idempotency during the TTL.
