# Roadmap

## M1 — Payload-only Kipu integration

- [x] Discover Kipu producer contract from the local repository.
- [x] Define executable requirements and observable truths.
- [x] Move payload filtering into the production package.
- [x] Rewire SQS consumer around filter outcomes.
- [x] Publish accepted alerts back to EventBridge.
- [x] Remove Elastic/OpenAI construction from the worker.
- [x] Align CloudFormation rule and IAM documentation.
- [x] Add Kipu contract and failure-path tests.
- [x] Produce verification report with evidence.

## M2 — Real local integration

- [x] Reproduce EventBridge, SQS, DynamoDB and Hub delivery locally.
- [x] Execute accepted and rejected alerts through the complete topology.
- [x] Capture repeatable E2E evidence.

## M3 — AWS development MVP

- [x] Deploy reviewer capture infrastructure in development.
- [x] Configure a scoped, assumable local worker role.
- [x] Run the MVP worker against live Kipu events.
- [x] Verify persisted, validated and filtered live outcomes.

## M4 — Repository minimization and MVP hardening

- [x] Audit the transitive runtime, test and documentation dependencies.
- [x] Remove the legacy HTTP/evidence implementation and its tests.
- [x] Remove obsolete examples, documentation, Compose services and packages.
- [x] Extract the useful Kipu v1 contract and payload-filter policy.
- [x] Align worker configuration, README and operational documentation.
- [x] Preserve and validate the repository skill and all Harness artifacts.
- [x] Run the reduced test, lint, Compose and skill verification suite.

## M5 — Filter criteria hardening and human calibration

- [x] Require a non-empty merchant name in the filter and Kipu contract.
- [x] Change the rolling-average degradation threshold from 10 to 5 points.
- [x] Protect the exact 5-point boundary against floating-point error.
- [x] Align production, policy, skill, tests and documentation.
- [ ] Build an anonymized, human-labelled evaluation set.
- [ ] Measure reviewer agreement and human–filter precision/recall/F1.

## M6 — Critical-only high-confidence filtering

- [x] Approve the critical-only global gates and signal branches A–F.
- [x] Version the thresholds as policy `2026-08-13.1`.
- [x] Record the policy contract and verification plan in Harness artifacts.
- [x] Verify that the worker and repository skill execute the same policy.
- [x] Verify inclusive boundaries and rejection paths with automated tests.
- [x] Run pytest, Ruff, a clean Docker build and the LocalStack E2E.
- [x] Replay the preserved 2026-08-13 accepted-only report without overwriting it.
- [x] Recalibrate human-labelled examples against the critical-only policy.

## M7 — Historical Kipu v2 evidence prototype (superseded)

This milestone records locally verified prototype work. Kipu final
`origin/main@d692b1b` did not integrate the v2 publisher; none of these items
describe the active EventBridge route.

- [x] Publish `Anomaly Detected v2` from Kipu with structured detector evidence.
- [x] Preserve v1 compatibility and make versioned publication failures visible.
- [x] Add strict v2 parsing and independent multi-criteria verification.
- [x] Recompute priority components and criticality from payload evidence.
- [x] Route both versioned input types through SQS and LocalStack.
- [x] Align the repository skill, documentation and contract fixtures.
- [x] Pass producer, reviewer, lint, skill and end-to-end verification.

## M8 — Reviewer-owned occurrence archive

- [x] Capture each structurally valid event from the active Kipu v1 route in the
  reviewer table before its decision; retain historical v2 export compatibility.
- [x] Separate immutable `occurrence#…` evidence from event-idempotent
  `decision#…` state without changing Kipu.
- [x] Select publication days from EventBridge time; retain historical explicit
  v2 `observation_date` parsing without inferring it for v1.
- [x] Export `all`, `unique`, `valid` and `valid_unique` payload sets for manual
  review.
- [x] Align reviewer documentation and Harness requirements with the ownership
  boundary.
- [ ] Pass and record the final reviewer, exporter, Docker and LocalStack
  verification before marking Phase 08 complete.

## External rollout

Separate local-agent evolution: [Guardian analyst plan](guardian-analyst/PLAN.md)
(started 2026-09-09). It does not mark any external rollout below complete.

- [ ] Promote the local MVP worker to a managed ECS service.
- [ ] Change Hub development rule to consume `Anomaly Validated v1`.
- [ ] Run shadow comparison against current Hub delivery.
- [ ] Disable Hub subscription to original Kipu anomaly events.
- [ ] Promote the same topology to production.
