---
phase: 01-kipu-payload-filter
type: execute
status: complete
requirements:
  - KIPU-01
  - KIPU-02
  - KIPU-03
  - KIPU-04
  - KIPU-05
  - KIPU-06
  - REL-01
  - REL-02
  - REL-03
  - REL-04
  - SEC-01
  - SEC-02
  - COMP-01
  - COMP-02
  - COMP-03
must_haves:
  truths:
    - "Worker starts without constructing Elastic or OpenAI clients"
    - "Kipu example event is parsed without losing optional routing fields"
    - "Only quantitatively supported alerts are republished"
    - "Business rejections are acknowledged and never published"
    - "Malformed JSON remains unacknowledged for DLQ redrive"
    - "Duplicate alert_id is not evaluated or published twice after completion"
    - "Infrastructure listens only to acceptance.kipu / Anomaly Detected v1"
    - "Validated output uses acceptance.reviewer / Anomaly Validated v1"
---

# Objective

Replace evidence-based review in the asynchronous Kipu path with a deterministic,
payload-only gate and wire accepted alerts back to Hub through EventBridge.

# Tasks

1. Create production `alert_filter` module and make the local skill reuse it.
2. Refactor idempotency records from `ReviewDecision` to filter outcomes.
3. Refactor `SQSReviewConsumer` to filter, acknowledge, or publish.
4. Add an EventBridge validated-alert publisher.
5. Rewire worker construction and settings.
6. Update CloudFormation, IAM documentation and environment example.
7. Add unit, contract and failure-path tests.
8. Run tests, lint, compile, YAML validation and package build.
9. Write `01-VERIFICATION.md` and update `STATE.md`.
