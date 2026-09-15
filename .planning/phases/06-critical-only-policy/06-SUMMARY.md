---
phase: 06-critical-only-policy
status: complete
policy_version: "2026-08-13.1"
completed: "2026-08-13"
---

# Summary

Policy `2026-08-13.1` replaces broad anomaly acceptance with a critical-only,
high-confidence contract based exclusively on structured Kipu payload fields.
The production filter, repository skill and examples now share that policy.

## Policy change

Every accepted alert must now be critical, contain at least 20 transactions and
20 declines, and satisfy at least one of six material signals:

- mass impact at 100 or more transactions and at most 15% approval;
- severe deterioration at 50 or more transactions, at most 20% approval and a
  rolling drop of at least 5 percentage points;
- an extreme rate at 20 or more transactions, at most 10% approval and a rolling
  drop of at least 10 percentage points;
- a high-volume collapse at 100 or more transactions, at most 50% approval and a
  rolling drop of at least 10 percentage points;
- an approval rate at least 5 points below `predicted_ar_q10`, with at least 50
  transactions;
- declines exceeding `predicted_dc_q90` by at least the greater of five or 5% of
  transaction volume, with at least 50 transactions.

All comparisons are inclusive. Historical and predictive branches can run only
when their structured inputs exist.

## Behavioral difference from policy 2026-08-12.1

- `approval_rate <= 0.30` is no longer sufficient.
- A rolling drop of 5 points is no longer sufficient by itself.
- `Alta`, `Media` and `Baja` remain structurally valid but are filtered as
  non-critical business outcomes.
- Predictive values must exceed material margins; merely crossing q10 or q90 is
  no longer sufficient.
- Low-volume events and events with fewer than 20 declines cannot pass any
  signal.

`KIPU-08` and the complete Phase 05 evidence remain in the repository as the
historical record of policy `2026-08-12.1`; they are not rewritten as if the new
rules had existed earlier.

## Safety boundary

The policy still verifies internal support in the payload. It does not establish
that an operational incident occurred and does not infer root cause. No external
evidence source is part of the decision.

## Verification result

The boundary/integration suite, Ruff, skill validation, direct skill execution,
clean Docker build, LocalStack E2E and offline evaluator all passed. The replay
retained 11 of the 50 alerts accepted by the prior policy and preserved the
source hash. Full commands and results are in `06-VERIFICATION.md`.

Activating the policy in AWS is deliberately outside this local phase: the
worker must be restarted/redeployed, and existing idempotency outcomes retain
their original decisions during the TTL.
