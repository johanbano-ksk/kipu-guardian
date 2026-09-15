---
phase: 07-kipu-main-v2-contract
type: execute
status: complete
started: "2026-08-18"
completed: "2026-08-18"
requirements:
  - KIPU-14
  - KIPU-15
  - KIPU-16
  - KIPU-17
  - KIPU-18
  - KIPU-19
  - KIPU-20
  - REL-05
  - REL-06
  - SEC-05
  - COMP-04
  - COMP-05
  - MAINT-06
  - MAINT-07
must_haves:
  truths:
    - "Kipu emits a versioned v2 payload with enough numeric evidence to audit its detector"
    - "The reviewer keeps the v1 fallback and validates v2 through an independent policy path"
    - "Producer labels and free text never replace numeric evidence"
    - "Priority and criticality are recomputed before a v2 alert can pass"
    - "EventBridge and SQS route both versioned input contracts without consuming legacy duplicates"
    - "Predictive alerts stay on v1 and marked descriptive v1 copies cannot race their v2 event"
    - "Kipu counts, POS exclusion and production image dependencies support the published evidence"
---

# Objective

Align the payload-only reviewer with the detector promoted to Kipu `main`, while
keeping the existing v1 policy stable during a controlled contract migration.

# Tasks

1. Add `Anomaly Detected v2` publication in Kipu with a deterministic alert ID,
   observation time, detector/policy identifiers, atomic signal metrics,
   canonical thresholds and priority components.
2. Preserve v1 and unversioned publication temporarily, but fail the Kipu
   notification path when the required versioned publication fails.
3. Preserve raw volume-spike evidence and score components through the Kipu
   pipeline instead of reconstructing them from human-readable reasons.
4. Add strict schema v2 parsing and a separate reviewer evaluation path that
   recomputes Kipu C1-C4, ML-assisted/critical bypasses and standalone volume
   spike rules from payload data.
5. Compare producer thresholds and policy identity against the reviewer policy;
   reject drift and incomplete declared signals.
6. Subscribe the SQS rule and LocalStack topology to both v1 and v2 inputs.
7. Update the repository skill, examples and operational documentation without
   changing the payload-only safety boundary.
8. Run producer tests, consumer tests, lint, skill validation, contract fixtures
   and the LocalStack end-to-end path.
9. Remove webhook material from versioned CDK inputs, verify the production
   image can load LightGBM, and record the required external credential rotation.

# Constraints

- No Elastic, OpenSearch, Kibana, API, browser or other external evidence may
  influence a filter decision.
- Do not derive signals or scores from `alert_summary`, `anomaly_type` or other
  display text.
- Do not weaken the v1 policy to imitate fields that exist only in v2.
- Do not deploy into or mutate AWS production as part of this local phase.
- Preserve all prior Harness evidence as immutable historical records.

# Completion criteria

Complete only after producer and consumer suites pass, a real Kipu-generated v2
fixture passes the reviewer contract, skill parity is validated, and remaining
deployment/security decisions are recorded in `07-VERIFICATION.md`.
