---
phase: 02-real-local-integration
status: complete
started: "2026-07-30"
completed: "2026-07-30"
---

# Plan

Build and execute the complete Kipu reviewer topology locally before AWS rollout.

## Observable truths

1. A Kipu v1 event reaches the worker through EventBridge and SQS.
2. An accepted alert is persisted and reaches the simulated Hub queue.
3. A valid alert without a supported signal is persisted but does not reach Hub.
4. The worker uses DynamoDB idempotency in the local execution.
5. The entire verification is reproducible with one command.
