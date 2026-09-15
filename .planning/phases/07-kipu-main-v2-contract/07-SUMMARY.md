---
phase: 07-kipu-main-v2-contract
status: complete
policy_version: "2026-08-18.1"
started: "2026-08-18"
completed: "2026-08-18"
---

# Summary

The local implementation and its final CodeBuild-image verification are
complete. It is based on the tree tracked locally as Kipu
`origin/main` at `6a1326925fde8aaaf52924bfab686aa05d223fc9`; the checked-out
`release/development` commit has the same tree before these changes. No branch,
commit or AWS production resource was changed.

## Producer result

Kipu now emits an evidence-bearing `Anomaly Detected v2` event for
`descriptive_v1`. The detail carries deterministic UUIDv5 identity, observation
and publication time, coherent status counts, raw C1-C4 metrics, Isolation
Forest scores, complete volume-spike inputs, the active thresholds and the
three priority components. The publisher recomputes the signals, gate, score
and criticality before calling EventBridge and treats any failed versioned
entry as a real failure.

For migration, a descriptive alert also emits a marked `1.0` copy and the
unversioned legacy copy. The marked v1 is not another decision candidate. A
predictive run does not fabricate descriptive evidence: it remains on the real
v1 contract and includes its predictive quantiles.

Kipu deduplication now uses merchant, country, observation date and producer
policy/engine. It reads merchant-only keys for one 24-hour migration window,
writes only composite keys and marks only alerts that the selected notification
path actually reports as sent.

## Evidence correctness fixes

Dynamic testing uncovered and fixed issues that materially affected alert
conclusions:

- POS rows are excluded before both transaction and rejection aggregation,
  while legacy files without `channel` remain readable.
- S3 and MongoDB now satisfy
  `approved + declined + unknown = total` explicitly.
- null rolling metrics no longer nullify `criteria_count` or block a standalone
  volume spike.
- the ETL end-of-day `datetime` is normalized to the daily date before the
  weekday spike comparison.
- `is_volume_spike` remains an explicit boolean when the feature is disabled or
  no spike fires.
- ML flags keep boolean dtype and publish their numeric decision scores.
- prior-history approval rate excludes the observed day.
- both supported production Docker paths load LightGBM's OpenMP runtime and
  fail their build smoke check if the application cannot import.

## Reviewer and migration result

The reviewer accepts exact v1 and v2 envelopes through separate policy paths.
V1 remains on `2026-08-13.1`. V2 uses reviewer policy `2026-08-18.1`, requires
producer policy `kipu-main-2026-08-13.1` and independently verifies thresholds,
counts, C1-C4, ML evidence, volume evidence, gate, score components and the
critical score floor. Text and producer labels never replace numeric evidence.

The v1 transition copy carries `superseded_by_schema_version = 2.0`. It is
completed as `SUPERSEDED_BY_V2` under
`<alert_id>#v1-superseded-by-v2`; the matching v2 retains the normal key. This
removes the nondeterministic EventBridge/SQS race without weakening genuine
legacy v1 handling. Accepted v1 and v2 details are still published unchanged as
`acceptance.reviewer / Anomaly Validated v1` until Hub negotiates a new output
contract.

## Security and rollout boundary

The Slack webhook was removed from current CDK context and the CDK custom
resource that copied context into Secrets Manager was removed. The value has
existed in Git history, so revocation/rotation in Slack and replacement in
Secrets Manager remain mandatory external actions; source cleanup alone cannot
invalidate it.

This phase does not deploy AWS. Development defaults still target
`acceptance-intelligence-bus-dev`. Production needs either a reviewer deployment
in the Kipu production account/bus or an explicitly authorized cross-account
EventBridge/SQS forwarding design. Hub cutover and the later output-contract
version remain separate rollout decisions.
