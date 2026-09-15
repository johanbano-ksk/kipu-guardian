import json

import pytest

from alert_reviewer.alert_filter import PayloadAlertFilter
from alert_reviewer.lambda_worker import LambdaWorkerDependencies, process_record
from alert_reviewer.queue import ClaimStatus, IdempotencyClaim


class FakeIdempotencyStore:
    def __init__(self, status: ClaimStatus = ClaimStatus.ACQUIRED) -> None:
        self.status = status
        self.stored = []
        self.completed = []
        self.released = []

    async def claim(self, _key):
        return IdempotencyClaim(self.status)

    async def store_result(self, key, outcome):
        self.stored.append((key, outcome))

    async def mark_completed(self, key):
        self.completed.append(key)

    async def extend(self, _key):
        return None

    async def release(self, key):
        self.released.append(key)


class FakeOccurrenceStore:
    def __init__(self) -> None:
        self.captured = []

    async def capture(self, occurrence):
        self.captured.append(occurrence)
        return True


class FakePublisher:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.published = []

    async def publish(self, alert):
        if self.fail:
            raise RuntimeError("publish failed")
        self.published.append(alert)


def _record(alert) -> dict:
    event = {
        "version": "0",
        "id": "event-001",
        "detail-type": "Anomaly Detected v1",
        "source": "acceptance.kipu",
        "time": "2026-08-27T12:00:01Z",
        "detail": alert.model_dump(mode="json"),
    }
    return {"messageId": "message-001", "body": json.dumps(event)}


def _dependencies(*, status=ClaimStatus.ACQUIRED, publish_fails=False):
    return LambdaWorkerDependencies(
        alert_filter=PayloadAlertFilter(),
        publisher=FakePublisher(fail=publish_fails),
        idempotency_store=FakeIdempotencyStore(status),
        occurrence_store=FakeOccurrenceStore(),
    )


@pytest.mark.asyncio
async def test_lambda_processor_captures_filters_publishes_and_completes(alert):
    dependencies = _dependencies()

    await process_record(_record(alert), dependencies)

    assert len(dependencies.occurrence_store.captured) == 1
    assert len(dependencies.idempotency_store.stored) == 1
    assert dependencies.publisher.published == [alert.model_dump(mode="json")]
    assert len(dependencies.idempotency_store.completed) == 1
    assert dependencies.idempotency_store.released == []


@pytest.mark.asyncio
async def test_lambda_processor_skips_completed_duplicate(alert):
    dependencies = _dependencies(status=ClaimStatus.COMPLETED)

    await process_record(_record(alert), dependencies)

    assert len(dependencies.occurrence_store.captured) == 1
    assert dependencies.publisher.published == []
    assert dependencies.idempotency_store.completed == []


@pytest.mark.asyncio
async def test_lambda_processor_releases_new_claim_when_publish_fails(alert):
    dependencies = _dependencies(publish_fails=True)

    with pytest.raises(RuntimeError, match="publish failed"):
        await process_record(_record(alert), dependencies)

    assert len(dependencies.idempotency_store.released) == 1
    assert dependencies.idempotency_store.completed == []
