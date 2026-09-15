---
phase: 05-criteria-calibration
type: execute
status: complete
started: "2026-08-12"
completed: "2026-08-12"
requirements:
  - KIPU-07
  - KIPU-08
must_haves:
  truths:
    - "Missing or blank merchant_name cannot pass the filter or queue contract"
    - "A 5.00-point rolling drop passes with at least 50 transactions"
    - "A drop below 5 points or volume below 50 does not trigger that signal"
    - "Skill and worker use the same versioned policy"
    - "Human decisions can be evaluated offline without changing AWS"
---

# Objective

Apply the revised business criteria consistently and make the project ready for
a blinded human-versus-filter calibration study.

# Tasks

1. Require non-empty merchant identity fields, including `merchant_name`.
2. Configure the rolling-average drop at 5 percentage points and volume 50.
3. Test inclusive and exclusive boundary cases, including float representation.
4. Make the skill load defaults from the production policy.
5. Add an offline evaluator for human labels and an executable example dataset.
6. Align README, operational docs, requirements and state.
7. Run the complete regression and skill validation.
