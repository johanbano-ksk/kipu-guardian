---
phase: 08-hourly-alert-audit
type: execute
status: passed
started: "2026-08-18"
requirements:
  - KIPU-21
  - KIPU-22
  - REL-07
  - REL-08
  - COMP-06
  - MAINT-08
must_haves:
  truths:
    - "Every structurally valid versioned EventBridge event is captured by the reviewer before its decision"
    - "Malformed input creates neither occurrence nor decision state and remains eligible for DLQ redrive"
    - "Event retries reuse independent occurrence and decision identities without overwriting evidence"
    - "Publication date comes from EventBridge time and observation date only from explicit v2 data"
    - "Daily exports separate all, unique, valid and valid-unique views"
    - "Phase 08 requires no Kipu code, payload, storage, schedule or deduplication change"
---

# Objective

Make the hourly stream observable from the reviewer's own integration boundary.
Preserve each structurally valid Kipu v1/v2 EventBridge occurrence before the
payload-only decision, without claiming visibility into producer executions
that never reach the reviewer.

# Tasks

1. Build an occurrence from the validated EventBridge envelope and original
   alert detail without enriching the producer payload.
2. Insert the occurrence conditionally in the existing reviewer DynamoDB table
   under `occurrence#event:<id>` or a deterministic SHA-256 fallback.
3. Move decision idempotency to the matching `decision#event:<id>` or
   `decision#sha256:<hash>` namespace so evidence and outcome cannot collide.
4. Capture before decision claim, evaluation or SQS acknowledgement; fail closed
   on persistence errors and leave malformed messages outside both namespaces.
5. Use EventBridge `time` for publication-day selection, with contractual
   timestamp fallback only when absent. Use only explicit v2 `observation_date`
   for observation-day selection.
6. Export `all`, `unique`, `valid`, `valid_unique` and `summary` from reviewer
   occurrences while preserving each raw alert unchanged.
7. Align README, operator documentation, the repository skill and Harness
   records, then run final reviewer, exporter, Docker and LocalStack checks.

# Constraints

- Slack, descriptions and external systems never provide filter evidence.
- Preserve the complete Phase 07 v1/v2 contract and `Anomaly Validated v1`
  output compatibility.
- Do not add fields, snapshots, state or deduplication behavior to Kipu.
- `valid` must evaluate every primary occurrence; `valid_unique` must select the
  latest accepted occurrence so a later rejection cannot erase it.
- An occurrence proves EventBridge receipt only, not an internal Kipu run,
  notification acknowledgement or external incident.
- Do not deploy AWS during this local phase.

# Completion criteria

Keep this phase in progress until final evidence proves capture ordering, retry
identity, collision handling, malformed exclusion, pagination, both date bases,
all five output files, exact payload preservation and LocalStack behavior for
v1 and v2. Record results in `08-VERIFICATION.md` before changing status.
