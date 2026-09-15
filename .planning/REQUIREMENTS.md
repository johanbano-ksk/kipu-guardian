# Requirements

## Functional

- **KIPU-01** — Consume Kipu `Anomaly Detected v1` events from its custom
  EventBridge bus through SQS.
- **KIPU-02** — Validate alerts exclusively from the structured `detail` payload.
- **KIPU-03** — Publish only accepted alerts as `Anomaly Validated v1`.
- **KIPU-04** — Preserve all original alert fields in accepted output.
- **KIPU-05** — Acknowledge business rejections without publishing them.
- **KIPU-06** — Leave malformed messages and technical failures unacknowledged
  for retry and DLQ redrive.
- **KIPU-07** — Require non-empty `alert_id`, `merchant_code`, `merchant_name`
  and `country` before evaluating signals.
- **KIPU-08** — Accept a rolling-average degradation signal at a drop of at least
  5 percentage points when `total_transactions >= 50`.
- **KIPU-09** — Under policy `2026-08-13.1`, accept only alerts whose normalized
  `criticality` is `Critica`; other recognized severities remain structurally
  valid business rejections.
- **KIPU-10** — Require `total_transactions >= 20` and `declined_count >= 20`
  before evaluating any critical signal.
- **KIPU-11** — Accept an observed critical signal only when at least one of the
  following inclusive branches is true:
  - A: `total_transactions >= 100` and `approval_rate <= 0.15`;
  - B: `total_transactions >= 50`, `approval_rate <= 0.20` and rolling-average
    drop `>= 5` percentage points;
  - C: `total_transactions >= 20`, `approval_rate <= 0.10` and rolling-average
    drop `>= 10` percentage points;
  - D: `total_transactions >= 100`, `approval_rate <= 0.50` and rolling-average
    drop `>= 10` percentage points.
- **KIPU-12** — Accept a predictive critical signal only with
  `total_transactions >= 50` and at least one inclusive material margin:
  - E: `predicted_ar_q10 - approval_rate >= 5` percentage points;
  - F: `declined_count - predicted_dc_q90 >= max(5, total_transactions * 0.05)`.
- **KIPU-13** — Never infer missing rolling or predictive metrics from
  descriptions; an alert may use only branches whose structured fields exist.
- **KIPU-14** — Under reviewer policy `2026-08-18.1`, retain the conservative
  schema `1.0` decision path and add a distinct schema `2.0` path aligned with
  Kipu's descriptive multi-criteria detector.
- **KIPU-15** — For schema `2.0`, recompute C1-C4 and the Kipu alert gate only
  from typed numeric evidence in the payload; `signal_codes`, summaries,
  anomaly types and producer criticality are never sufficient proof.
- **KIPU-16** — Verify a declared volume spike only when target volume,
  weekday baseline, ratio and oldest-week activity satisfy the canonical Kipu
  thresholds carried by the versioned reviewer policy.
- **KIPU-17** — Require Kipu's schema `2.0` policy identifiers and declared
  thresholds to match the producer version supported by the reviewer. Contract
  drift is a rejection, not an invitation to trust producer-provided limits.
- **KIPU-18** — Recompute schema `2.0` priority components and require a
  coherent score of at least 70 before accepting `Critica`.
- **KIPU-19** — Emit schema `2.0` only for Kipu's `descriptive_v1` detector;
  predictive alerts remain on schema `1.0` with their structured quantiles
  until a dedicated predictive contract exists.
- **KIPU-20** — Preserve coherent approved, declined, unknown and total counts
  for every supported data source, and exclude normalized POS rows before both
  transaction and rejection aggregation.
- **KIPU-21** — Capture every structurally valid versioned Kipu EventBridge
  event in the reviewer's DynamoDB table before filter evaluation or decision
  idempotency. Preserve the original detail and its SHA-256 without mutating the
  producer payload.
- **KIPU-22** — Derive publication time from the EventBridge envelope, falling
  back to the contractual alert timestamp only when envelope time is absent.
  Use transaction observation date only when schema `2.0` explicitly provides
  `observation_date`; never infer it for `1.0`.
- **KIPU-23** — The deployable EventBridge rule must match the final producer
  boundary exactly: `acceptance.kipu / Anomaly Detected v1` with
  `detail.schema_version = 1.0`. A new detail type requires a newly negotiated
  producer fixture and shadow rollout.

`KIPU-08` is retained as historical traceability for policy `2026-08-12.1`.
Starting with `2026-08-13.1`, a five-point rolling drop is not sufficient by
itself and must satisfy the global gates and branch B.

`KIPU-14` through `KIPU-20` record the historical schema 2.0 proposal reviewed
on 2026-08-18. Kipu final `origin/main@d692b1b` did not integrate that publisher;
those requirements are reproducibility evidence, not active runtime scope.

## Reliability

- **REL-01** — Deduplicate reviewer decisions by EventBridge event ID, with a
  deterministic SHA-256 envelope fallback, using DynamoDB.
- **REL-02** — Persist the filter outcome before publishing or acknowledging.
- **REL-03** — Reuse the persisted outcome on retry.
- **REL-04** — Renew both SQS visibility and the DynamoDB processing lock.
- **REL-05** — Kipu producer retries must reuse a deterministic `alert_id` and
  deduplicate by merchant, country, observation date and producer policy; a
  partial versioned EventBridge failure must not be reported as success.
- **REL-06** — A schema `1.0` transition copy marked as superseded by `2.0`
  must be acknowledged under its own event-based decision identity and must
  never block or reach Hub ahead of the corresponding `2.0` event.
- **REL-07** — Keep received-event evidence and decision state in independent
  key namespaces. Use `occurrence#event:<id>` / `decision#event:<id>` when the
  EventBridge ID exists and a deterministic SHA-256 envelope identity otherwise.
  A retry must reuse both records; a reused identity with different payload must
  fail without overwriting evidence.
- **REL-08** — Capture must succeed before decision claim, filter evaluation or
  SQS acknowledgement. Malformed JSON, envelopes or versioned contracts create
  neither occurrence nor decision records and remain eligible for DLQ redrive.

`REL-05` and `REL-06` belong to the same historical 2.0 proposal and are not
claims about the final producer runtime.

## Security and privacy

- **SEC-01** — Do not initialize or call Elastic/OpenSearch in the worker.
- **SEC-02** — Do not log raw alert payloads.
- **SEC-03** — Use workload IAM credentials, never static AWS keys.
- **SEC-04** — Keep Elastic, OpenSearch and OpenAI clients out of the worker
  distribution, container and repository.
- **SEC-05** — Never place Slack webhook credentials in CDK context or generated
  templates; provision them outside CDK through Secrets Manager and rotate any
  value that has appeared in Git history.

## Compatibility

- **COMP-01** — Accept the exact Kipu schema version `1.0`.
- **COMP-02** — Preserve `batch_id` and `group_name` for Hub routing/threading.
- **COMP-03** — Ignore legacy `Anomaly Detected` to avoid dual-publish duplicates.
- **COMP-04** — Subscribe only to `Anomaly Detected v1` and continue ignoring
  both the unversioned legacy duplicate and the unnegotiated v2 type.
- **COMP-05** — Preserve accepted schema `1.0` details byte-for-byte at the
  filter boundary; the existing `Anomaly Validated v1` downstream type remains
  unchanged until Hub adopts a versioned output contract.
- **COMP-06** — Export reviewer-owned occurrences by publication date or
  explicit observation date into `all`, latest `unique`, all policy-`valid` and
  latest `valid_unique` sets. Preserve payloads unchanged; a later rejected
  occurrence must not erase the latest earlier accepted occurrence.

## Maintainability

- **MAINT-01** — Keep only files used by the Kipu MVP or its Harness Engineering
  traceability.
- **MAINT-02** — Keep HTTP, Elastic/OpenSearch and OpenAI legacy code and
  dependencies out of the repository.
- **MAINT-03** — Keep runtime configuration, documentation and tests aligned
  with the worker-only architecture.
- **MAINT-04** — Repository cleanup must not interrupt the active AWS MVP worker
  or delete its local virtual environment.
- **MAINT-05** — Keep the production worker, repository skill, examples,
  evaluation tooling and documentation aligned with the same versioned filter
  policy.
- **MAINT-06** — Maintain a producer-to-consumer v1 fixture and automated tests
  covering queue parsing, filter decisions and exact EventBridge routing. Keep
  former v2 fixtures clearly labelled as historical/offline.
- **MAINT-07** — The Kipu production image must load the descriptive and
  predictive runtimes, including LightGBM's native OpenMP dependency.
- **MAINT-08** — Cover capture-before-decision ordering, retry identity,
  collision failure, malformed exclusion, DynamoDB pagination, timezone
  boundaries, historical-version exclusion and all four export sets with automated
  reviewer tests. Phase 08 must not require producer changes.
