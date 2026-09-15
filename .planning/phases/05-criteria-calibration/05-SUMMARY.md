---
phase: 05-criteria-calibration
status: complete
completed: "2026-08-12"
---

# Summary

Implemented policy `2026-08-12.1` and prepared offline human calibration.

## Policy changes

- `merchant_name` is now required and cannot be blank.
- Rolling degradation now triggers at 5 percentage points with at least 50
  transactions.
- Exact threshold comparisons tolerate only floating-point representation error.
- `NO_SUPPORTED_SIGNAL` remains the result for a valid payload with no signal.

## Calibration tooling

`scripts/evaluate_human_labels.py` compares independent labels with the same
production filter and reports coverage, abstentions, confusion matrix,
precision, recall, F1, balanced accuracy, Cohen's kappa and disagreements.

The included data is illustrative only. A real conclusion requires anonymized
cases, blinded independent reviewers and an adjudicated label set.

The LocalStack E2E now exercises three policy paths: an exact 5-point drop reaches
Hub, a stable event is acknowledged without publication, and an event without
`merchant_name` is retried three times and reaches the DLQ without persistence.

The live AWS figures from 2026-08-11 were generated with the prior policy. The
previous local AWS worker is stopped; `2026-08-12.1` requires an explicit start
or managed redeployment before any controlled live test.
