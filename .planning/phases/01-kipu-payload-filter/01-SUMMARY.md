---
phase: 01-kipu-payload-filter
status: complete
completed: "2026-07-30"
---

# Summary

Implemented the payload-only enforcement path between Kipu and Hub.

## Delivered

- Production filter in `src/alert_reviewer/alert_filter.py`.
- Skill CLI backed by the production filter.
- Strict Kipu v1 EventBridge parser.
- DynamoDB-persisted filter outcomes.
- Business rejection ACK without downstream publication.
- EventBridge validated-alert publisher.
- Worker with no external evidence construction.
- CloudFormation rule for `acceptance.kipu / Anomaly Detected v1`.
- EventBridge target DLQ plus SQS processing DLQ.
- Minimal worker Docker/Compose runtime.
- Exact Kipu event fixture and executable integration contracts.

The legacy HTTP evidence implementation initially remained isolated. It was
removed completely in phase 04 because it was not part of Kipu → Hub.

## Output contract

```text
Source: acceptance.reviewer
DetailType: Anomaly Validated v1
EventBusName: acceptance-intelligence-bus-dev
Detail: original accepted Kipu payload
```

## Reliability behavior

- Accepted: persist → publish → complete → ACK.
- Business rejected: persist → complete → ACK.
- Malformed: no ACK → retry/DLQ.
- Technical failure: no ACK → retry/DLQ.
- Completed duplicate: ACK without evaluation/publication.
