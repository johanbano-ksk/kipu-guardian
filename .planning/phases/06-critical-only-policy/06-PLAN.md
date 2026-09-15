---
phase: 06-critical-only-policy
type: execute
status: complete
started: "2026-08-13"
completed: "2026-08-13"
requirements:
  - KIPU-09
  - KIPU-10
  - KIPU-11
  - KIPU-12
  - KIPU-13
  - MAINT-05
must_haves:
  truths:
    - "Only structurally valid alerts with critical severity can pass"
    - "Every accepted alert has at least 20 transactions and 20 declines"
    - "Every accepted alert satisfies at least one observed or predictive branch A-F"
    - "All policy boundaries are inclusive and protected from representation noise"
    - "Missing historical or predictive fields never get reconstructed from text"
    - "The worker and repository skill load policy 2026-08-13.1"
    - "Artifacts produced under earlier policies remain immutable historical evidence"
---

# Objective

Narrow the payload-only reviewer to critical alerts with high-confidence
quantitative support, while preserving the Kipu contract, queue reliability and
all evidence produced by earlier policy versions.

# Policy contract

An alert is accepted only when it is structurally valid and all global gates are
true:

- normalized `criticality` is `Critica` or its accented equivalent;
- `total_transactions >= 20`;
- `declined_count >= 20`.

It must then satisfy at least one inclusive branch:

| Branch | Condition |
|---|---|
| A — mass impact | `total_transactions >= 100` and `approval_rate <= 0.15` |
| B — severe deterioration | `total_transactions >= 50`, `approval_rate <= 0.20` and rolling drop `>= 5pp` |
| C — extreme low rate | `total_transactions >= 20`, `approval_rate <= 0.10` and rolling drop `>= 10pp` |
| D — high-volume collapse | `total_transactions >= 100`, `approval_rate <= 0.50` and rolling drop `>= 10pp` |
| E — predictive approval gap | `total_transactions >= 50` and `predicted_ar_q10 - approval_rate >= 5pp` |
| F — predictive decline excess | `total_transactions >= 50` and `declined_count - predicted_dc_q90 >= max(5, total_transactions * 0.05)` |

# Tasks

1. Version all critical-only thresholds in `config/filter_policy.yaml` as
   `2026-08-13.1`.
2. Apply the global gates and branches A-F in the production payload filter.
3. Keep structural validation ahead of business gates and preserve accepted
   alert payloads without adding fields.
4. Make the repository skill execute the same versioned production policy.
5. Replace fixtures and examples whose acceptance depended only on the previous
   30% or rolling-drop rules.
6. Add unit coverage for every global gate, branch boundary, predictive margin,
   missing optional metric and float boundary.
7. Exercise accepted, business-rejected and malformed paths through the complete
   LocalStack topology.
8. Replay the preserved 2026-08-13 accepted-only report into a new versioned
   output without modifying the source artifact.
9. Run the offline human-label evaluator with examples aligned to the new policy.
10. Record exact pytest, Ruff, Docker, E2E and replay evidence in
    `06-VERIFICATION.md` before completing the phase.

# Constraints

- Decisions remain payload-only: no Elastic, OpenSearch, Kibana, dashboards,
  browsers, APIs or databases may provide evidence to the filter.
- `Alta`, `Media` and `Baja` remain recognized contract values but cannot pass
  the critical-only business gate.
- Missing rolling or predictive values disable only the dependent branch; their
  values must not be inferred from `alert_summary` or other text.
- Business rejections are acknowledged without publication and do not go to the
  DLQ. Malformed contracts and technical failures retain retry/redrive behavior.
- Phase 01-05 plans, summaries, verifications and prior report files must not be
  rewritten to describe the new policy.

# Completion criteria

Completed on 2026-08-13. `06-VERIFICATION.md` contains reproducible evidence,
the preserved replay has a versioned result artifact, and worker/skill policy
parity passed.
