---
phase: 01-kipu-payload-filter
verified: "2026-07-30"
status: passed
score: "9/9 observable truths verified"
---

# Verification report

## Observable truths

| # | Truth | Status | Evidence |
|---|---|---|---|
| 1 | Worker starts without Elastic/OpenAI construction | Verified | `tests/test_integration_contract.py::test_worker_source_has_no_external_evidence_dependencies`; negative `rg` check |
| 2 | Real Kipu v1 fixture parses without routing-field loss | Verified | `tests/test_integration_contract.py::test_real_kipu_fixture_matches_consumer_contract` |
| 3 | Only quantitatively supported alerts publish | Verified | `tests/test_alert_filter.py`; `tests/test_queue.py::test_accepted_alert_is_published_then_acknowledged` |
| 4 | Business rejection is ACKed and not published | Verified | `tests/test_queue.py::test_business_rejection_is_acknowledged_without_publish` |
| 5 | Malformed messages remain for DLQ | Verified | `tests/test_queue.py::test_invalid_message_is_left_for_dlq_redrive` |
| 6 | Completed duplicates are not re-evaluated | Verified | `tests/test_queue.py::test_completed_duplicate_is_deleted_without_reprocessing` |
| 7 | Infrastructure listens only to Kipu v1 | Verified | `tests/test_integration_contract.py::test_infrastructure_subscribes_only_to_versioned_kipu_events` |
| 8 | Accepted output uses reviewer source/type and preserves detail | Verified | `tests/test_eventbridge.py::test_publishes_validated_contract_without_mutating_kipu_detail` |
| 9 | Default worker image excludes legacy evidence clients | Verified | Container imports worker and entrypoint; `elasticsearch`, `opensearchpy` and `openai` module specs are absent |

## Automated evidence

| Check | Result |
|---|---|
| Full pytest | 50 passed |
| Focused contract/failure suite | 24 passed |
| Ruff | All checks passed |
| compileall | Passed |
| Skill quick validation | Passed |
| Policy and CloudFormation YAML | Valid |
| Compose structure | Valid |
| Wheel build/content contract | Passed |
| Docker image build | `kipu-alert-reviewer:harness-verified` built successfully |
| Container dependency isolation | Worker/entrypoint present; legacy evidence clients absent |
| Local positive execution | Kipu fixture accepted and captured EventBridge publication |
| Local negative execution | Stable alert filtered with no publication |
| Real Kipu fixture through skill CLI | Accepted with `batch_id` and `group_name` preserved |
| Obsolete async-contract search | No matches |

## Rollout follow-up

The AWS capture stack and scoped runtime credentials were subsequently verified
in phase 03. Managed ECS deployment, Hub shadow comparison and Hub cutover remain
external rollout work.

Docker Desktop was started and the worker image was built and inspected locally.
No AWS or Hub mutations were attempted.
