---
phase: 03-aws-development-rollout
status: mvp_live
started: "2026-08-06"
---

# Plan

Deploy the locally verified reviewer topology into AWS development without
interrupting the current Kipu to Hub path.

## Confirmed environment

- Account: `010928226018`
- Region: `us-east-1`
- CLI profile alias: `ia-dev-payments-intelligence`
- Assigned SSO role: `ia-dev-developer`
- Event bus: `acceptance-intelligence-bus-dev`
- ECS cluster: `pmt-intel-kipu-development`
- Current direct Hub rule: `kipu-anomaly-to-hub-dev`

## Rollout sequence

1. Create a dedicated ECR repository and publish the reviewer image.
2. Deploy the input rule, SQS/DLQ and DynamoDB stack.
3. Deploy a long-running Fargate service with least-privilege roles.
4. Add validated-event consumption in Hub without removing the direct rule.
5. Run a shadow comparison and inspect DLQs/metrics.
6. Disable direct Kipu delivery only after acceptance.

The capture stack is live and the local MVP worker is processing real Kipu
events. Managed ECS deployment and Hub cutover remain pending.
