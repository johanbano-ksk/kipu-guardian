---
milestone: M8
milestone_name: Reviewer-owned occurrence archive
status: verified
last_updated: "2026-09-24"
stopped_at: "Phase 08 verified locally; AWS rollout remains separately authorized work"
progress:
  completed: 54
  total: 54
---

# Current state

> Active local Guardian work (2026-09-09): [Guardian analyst harness](guardian-analyst/STATE.md).
> This separate track preserves the historical M8 rollout status recorded below.

## Completed

1. Extracted the real producer contract from the local Kipu repository.
2. Moved payload filtering into `src/alert_reviewer/alert_filter.py`.
3. Made the project-local skill reuse the production implementation.
4. Rewired SQS processing around persisted filter outcomes.
5. Added EventBridge publisher for `Anomaly Validated v1`.
6. Removed external evidence construction from the worker.
7. Aligned CloudFormation, Docker, Compose, environment and IAM documentation.
8. Added real-fixture, contract, idempotency and failure-path tests.
9. Produced verification and summary artifacts.
10. Verified a minimal worker container.
11. Added a LocalStack EventBridge/SQS/DynamoDB topology.
12. Added a one-command end-to-end runner.
13. Verified accepted delivery to a queue representing Hub.
14. Verified stable alerts are persisted but never reach Hub.
15. Repeated the one-command run and captured worker logs.
16. Deployed the EventBridge/SQS/DLQ/DynamoDB stack in AWS development.
17. Added a scoped, assumable local MVP worker role.
18. Started the worker against the real Kipu event bus.
19. Processed and persisted 108 live Kipu alerts.
20. Verified 3 accepted, 105 filtered and zero processing failures.
21. Audited every repository file against the worker runtime and Harness scope.
22. Removed the HTTP, Elastic/OpenSearch and OpenAI legacy implementation.
23. Removed obsolete tests, examples, documentation and Compose configuration.
24. Extracted the strict Kipu v1 contract and worker-only settings.
25. Preserved the project skill and all prior Harness Engineering evidence.
26. Verified the reduced repository with 33 tests, a clean container build and E2E.
27. Made `merchant_name` a required, non-blank structural field.
28. Reduced the rolling-average degradation threshold from 10 to 5 points.
29. Protected the exact 5-point boundary against floating-point representation.
30. Made the repository skill load its defaults from the production policy.
31. Added an offline human-label evaluator with agreement and balanced metrics.
32. Approved critical-only global gates and observed/predictive branches A-F.
33. Versioned the critical-only thresholds as policy `2026-08-13.1`.
34. Added the Phase 06 Harness plan, draft summary and pending verification
    matrix without rewriting earlier phase evidence.
35. Applied policy `2026-08-13.1` across the worker, skill, fixtures and docs.
36. Verified global gates and all observed/predictive branch boundaries.
37. Preserved legacy DynamoDB outcome compatibility and added policy traceability.
38. Passed the complete pytest, Ruff and skill validation suite.
39. Built a clean worker image and passed accepted/rejected/invalid LocalStack E2E.
40. Replayed the preserved 2026-08-13 accepted-only sample: 11 retained, 39 rejected.
41. Recalibrated and executed the example human-label evaluation.
42. Added Kipu schema `2.0` with deterministic identity and structured detector evidence.
43. Preserved genuine v1 compatibility while suppressing the marked transition copy safely.
44. Added strict, independent v2 verification for signals, gate, score and criticality.
45. Corrected status-count, POS, rolling-history and volume-spike evidence at the producer.
46. Verified both versioned inputs through SQS, the worker, EventBridge and LocalStack.
47. Aligned the skill, examples, operational documentation and Harness requirements.
48. Passed 474 Kipu tests, 126 reviewer tests, Ruff, skill validation and both image smokes.
## In progress

- Revalidated the producer boundary against Kipu final `origin/main@d692b1b`
  and snapshot `3171c38`: the active producer contract is exclusively
  `Anomaly Detected v1` / `schema_version = 1.0`.
- Removed `Anomaly Detected v2` from the deployable EventBridge rule and local
  topology. The 2.0 evaluator remains only as historical/offline compatibility.
- Recorded the producer's final `alert_min_volume = 20` and baseline-drop guard;
  their non-published metrics are not inferred by the reviewer.

- The reviewer implementation captures each structurally valid versioned
  EventBridge occurrence before its filter decision.
- Occurrences and decisions use separate `occurrence#…` and `decision#…`
  namespaces based on EventBridge ID or a deterministic envelope hash.
- The exporter emits `all`, `unique`, `valid`, `valid_unique` and summary schema
  `1.1`; the reproducible example returned `1/1/1/1` with exact payloads.
- Phase 08 changes only this repository. It adds no Kipu metadata, snapshot,
  notification or deduplication behavior.
- The complete reviewer suite passed `148/148`; Ruff, skill package validation
  and the local exporter example also passed.
- Final local verification passed on 2026-09-24: 1214 tests passed, 7 skipped,
  Ruff passed, Docker image built, LocalStack E2E passed, and both export date
  bases were verified. No AWS deployment has been performed.

## Evidence log

| Date | Check | Result |
|---|---|---|
| 2026-07-30 | Kipu publisher inspection | Contract extracted from `src/notifications/eventbridge.py` |
| 2026-07-30 | Kipu contract tests | Source, detail type, schema and criticality confirmed |
| 2026-07-30 | Reviewer baseline | 35 tests passing before integration refactor |
| 2026-07-30 | Payload integration suite | 50 tests passing |
| 2026-07-30 | Kipu-specific contracts | 24 focused tests passing |
| 2026-07-30 | Static quality | Ruff and compileall passing |
| 2026-07-30 | Infrastructure | CloudFormation and Compose structure verified |
| 2026-07-30 | Package | Wheel contains filter, publisher, queue and worker entrypoint |
| 2026-07-30 | Skill | Validation passed; real Kipu fixture accepted |
| 2026-07-30 | Docker image | `kipu-alert-reviewer:harness-verified` built |
| 2026-07-30 | Container isolation | Worker present; external evidence modules absent |
| 2026-07-30 | Local accepted run | `Anomaly Validated v1` emitted |
| 2026-07-30 | Local filtered run | Stable alert filtered with `NO_SUPPORTED_SIGNAL` |
| 2026-07-30 | LocalStack topology | EventBridge → SQS → worker → EventBridge → Hub SQS passed |
| 2026-08-06 | AWS SSO | `ia-dev-payments-intelligence` authenticated |
| 2026-08-06 | AWS account | Account `010928226018`, region `us-east-1` confirmed |
| 2026-08-11 | AWS stack | `pmt-intel-kipu-alert-reviewer-mvp` reached `UPDATE_COMPLETE` |
| 2026-08-11 | Live Kipu queue | 108 real `Anomaly Detected v1` events received |
| 2026-08-11 | Live decisions | 3 validated, 105 filtered, 0 failed |
| 2026-08-11 | Repository cleanup | 16 obsolete source/config/test/doc files removed |
| 2026-08-11 | Reduced verification | 33 tests, Ruff and Compose passed |
| 2026-08-11 | Skill execution | Fixture accepted with routing fields preserved |
| 2026-08-11 | Clean Docker build | `kipu-alert-reviewer:mvp` built without cache |
| 2026-08-11 | Container isolation | Worker imports; five legacy packages absent |
| 2026-08-11 | Post-cleanup LocalStack E2E | Accepted reaches Hub; stable alert does not |
| 2026-08-11 | Local DLQ | 0 visible and 0 in-flight messages |
| 2026-08-12 | Policy version | `2026-08-12.1` requires merchant name and 5-point drop |
| 2026-08-12 | Criteria regression | 45 tests passed; Ruff passed |
| 2026-08-12 | Exact boundary | 5.00-point drop accepted despite float representation |
| 2026-08-12 | Missing merchant name | Skill output `[]`; queue contract rejects event |
| 2026-08-12 | Calibration tooling | Example labels produced agreement, F1, kappa and discrepancies |
| 2026-08-12 | Policy E2E accepted path | Exact 5-point drop reached Hub |
| 2026-08-12 | Policy E2E rejected path | Stable alert remained `NO_SUPPORTED_SIGNAL` |
| 2026-08-12 | Policy E2E invalid path | Missing merchant name reached DLQ after 3 receives |
| 2026-08-13 | Critical-only policy design | Global gates and branches A–F approved as policy `2026-08-13.1` |
| 2026-08-13 | Critical-only suite | 90 tests passed in 3.46s; gates, A–F, strict typing/timestamps and float boundaries covered |
| 2026-08-13 | Static and skill quality | Ruff passed; skill package valid; CLI parity passed |
| 2026-08-13 | Docker build | Clean `kipu-alert-reviewer:mvp` build succeeded on Engine 29.5.3 |
| 2026-08-13 | Critical-only LocalStack E2E | Accepted reached Hub; stable critical was filtered; invalid reached DLQ |
| 2026-08-13 | Historical replay | 11/50 retained, 39/50 filtered; source SHA-256 unchanged |
| 2026-08-13 | Calibration examples | 9 cases executed under `2026-08-13.1`; adjudicated TP=6, FP=0, FN=0, TN=3 |
| 2026-08-13 | Harness phase 06 | Plan, summary and verification marked complete |
| 2026-08-18 | Kipu producer suite | 474 tests passed in the final CodeBuild-shaped image |
| 2026-08-18 | Reviewer suite | 126 tests passed; Ruff passed |
| 2026-08-18 | Dual-version LocalStack E2E | v1 and v2 accepted paths reached Hub; stable and invalid controls behaved correctly |
| 2026-08-18 | Transition race | Marked v1 became `SUPERSEDED_BY_V2` without blocking the same-ID v2 event |
| 2026-08-18 | Production images | Both Docker paths loaded LightGBM 4.7.0 and `src.main` as non-root |
| 2026-08-18 | Mixed source schemas | POS filtering passed with legacy/current Parquet files in both orders |
| 2026-08-18 | Skill and contract | Skill package valid; producer-generated v2 fixture accepted unchanged |
| 2026-08-18 | Harness phase 07 | Plan, summary and verification marked complete |
| 2026-08-19 | Phase 08 scope correction | Kipu source has no tracked Phase 08 change; reviewer-only boundary documented |
| 2026-08-19 | Reviewer complete suite | 148/148 tests passed using a writable fixture override for sandbox ACL compatibility |
| 2026-08-19 | Static and skill quality | Ruff passed; skill package validation passed; v1/v2 skill examples accepted unchanged |
| 2026-08-19 | Reviewer occurrence export | Local example produced all/unique/valid/valid_unique = 1/1/1/1 with exact payload preservation |
| 2026-08-19 | Docker availability | Docker API unavailable; image build and LocalStack E2E deferred without claiming PASS |
| 2026-08-26 | Final Kipu contract audit | `origin/main@d692b1b` and snapshot `3171c38` publish only `Anomaly Detected v1` / schema `1.0` |
| 2026-08-26 | Runtime realignment | EventBridge, SQS parser and LocalStack topology reject unnegotiated v2 traffic; offline historical evaluator/export remain |
| 2026-08-26 | Reviewer verification | 148 tests passed; Ruff and skill v1 execution passed; Docker API unavailable |

## Decisions

- Supersede the former Phase 07 runtime assumption: Kipu final does not publish
  `Anomaly Detected v2`. Keep its evaluator and evidence only for historical
  reproducibility, outside the active EventBridge subscription.

- **Superseded 2026-08-26:** the earlier decision to subscribe to both v1 and
  v2 applied only to the unmerged Phase 07 prototype. Runtime now subscribes to
  exact `Anomaly Detected v1`/schema `1.0` only.
- Complete a marked v1 migration copy as `SUPERSEDED_BY_V2`; its EventBridge
  identity keeps its decision separate from the corresponding v2 event.
- Keep predictive detection on v1 until it can provide the complete v2
  descriptive-evidence contract.
- Republish validated events with a distinct source/type to prevent loops.
- Keep malformed messages for DLQ, but acknowledge legitimate filtered alerts.
- Keep the worker as the only supported runtime in this repository.
- Treat the 3 validated / 105 filtered AWS outcomes as historical evidence from
  the previous policy, not as validation of policy `2026-08-12.1`.
- Measure human consistency with precision/recall/F1 and inter-reviewer agreement;
  do not rely on accuracy alone in an imbalanced sample.
- A human–filter agreement study does not prove an operational incident occurred.
- Hub must stop consuming original Kipu events before filtering becomes enforcing.
- Treat criticality as a necessary business gate, never as sufficient evidence.
- Require at least 20 transactions and 20 declines before any signal can pass.
- Accept only the four observed or two predictive branches recorded in
  `KIPU-11` and `KIPU-12`; all thresholds are inclusive.
- Do not reconstruct missing rolling or predictive metrics from free text.
- Preserve reports and phase evidence produced by older policies. Any replay
  under `2026-08-13.1` must create a new versioned result rather than overwrite
  historical artifacts.
- Leave legacy DynamoDB records untouched until TTL expiry. After the updated
  worker starts, new evaluations use the `decision#…` namespace and do not
  rewrite the older alert-based keys.
- Keep reviewer output on `Anomaly Validated v1` until Hub explicitly negotiates
  a new output contract; accepted details remain unchanged.
- Treat Phase 07 as locally complete, not deployed: production account/bus routing,
  Hub shadow traffic and cutover require separate operational authorization.
- Capture only structurally valid EventBridge events and do so before the filter
  decision; malformed input remains outside the archive and follows DLQ redrive.
- Use EventBridge time as publication time and explicit v2 `observation_date` as
  the only observation-date source.
- Separate reviewer occurrence evidence from decision state by namespace and
  event identity; do not merge distinct publications by `alert_id`.
- Treat the reviewer archive as evidence of received EventBridge events, never
  as evidence of every Kipu execution, Slack delivery or external incident.
- Keep Phase 08 entirely inside the reviewer repository and deployment boundary.

## Phase 06 complete

Implementation and evidence are recorded in
`.planning/phases/06-critical-only-policy/06-VERIFICATION.md`.

## Phase 07 complete

Implementation and evidence are recorded in
`.planning/phases/07-kipu-main-v2-contract/07-VERIFICATION.md`.

## Phase 08 runtime verification pending

The reviewer-only implementation and partial verification matrix are recorded
in `.planning/phases/08-hourly-alert-audit/`. Do not mark this phase complete
until the current Docker image and both LocalStack paths pass.

## External rollout pending

1. Revoke and rotate the Slack webhook that existed in Git history, then replace
   its authorized Secrets Manager value.
2. Refresh the Bitbucket remote, review the local Kipu diff and merge it through
   the normal pull-request path.
3. Decide whether the reviewer runs in Kipu's production account or receives an
   explicitly authorized cross-account EventBridge forward from the prod bus.
4. Resolve Kipu's development-specific CDK resource/environment values before a
   production deployment.
5. Deploy the Phase 07 Kipu v2 publisher through its normal release process;
   Phase 08 requires no additional Kipu change.
6. After final Phase 08 verification, promote the reviewer worker with occurrence
   capture to a managed ECS service and add the Hub rule for validated events.
7. Run shadow comparison, then disable Hub's original Kipu subscription.
8. Define retention, access and capacity controls for reviewer-owned occurrence
   records in DynamoDB.
9. Collect 30–50 anonymized cases with two independent blinded labels and run the
   offline agreement/precision/recall evaluation before enforcing the cutover.
