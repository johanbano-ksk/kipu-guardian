---
phase: 04-repository-minimization
status: complete
completed: "2026-08-11"
---

# Summary

Reduced the repository to the asynchronous Kipu payload-validation MVP.

## Removed

- Legacy FastAPI entrypoint and HTTP review API.
- Elastic/OpenSearch evidence clients and OpenAI adjudication.
- Legacy domain/rule/reviewer modules and their tests.
- Legacy optional dependencies and console entrypoint.
- Obsolete review request fixture and OpenSearch access guide.
- Redundant Compose topology and generated caches/build artifacts.

## Retained and hardened

- Strict Kipu v1 contract.
- Deterministic payload-only filter and externalized thresholds.
- SQS consumer, DynamoDB idempotency and EventBridge publisher.
- CloudFormation, AWS worker controls and LocalStack E2E runner.
- Project-local `filter-valid-alerts` skill.
- All Harness Engineering project, phase and verification artifacts.

The Docker image was rebuilt without cache and the complete LocalStack topology
was executed after cleanup. The E2E runner now creates the queues and rules
before starting the worker, eliminating a harmless bootstrap polling error.

The active AWS worker and `.venv` were intentionally left running/in place; the
cleanup changes apply on its next controlled restart.
