"""Evidence refreshes are separate auditable revisions, never cache overwrites."""

import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest

from alert_reviewer import guardian_evidence_store as module
from alert_reviewer.datalake_history import HistoryError
from alert_reviewer.guardian_evidence_store import GuardianEvidenceStore

CASE = "a" * 64
OTHER_CASE = "b" * 64


def report(**changes):
    return {
        "report_id": CASE,
        "protocol": "synthetic-protocol-v3",
        "history": {"metrics": {"total_transactions": 100}},
        "evidence_digest": "c" * 64,
        "original_alert": {"merchant_code": "synthetic-mid"},
        "policy": {"accepted": True},
        "metric_profile": {"id": "sales", "version": "1"},
        "comparison_context": {"comparable": False},
        "analysis_status": "pending",
        "conclusions": None,
        **changes,
    }


def case_path(tmp_path):
    return tmp_path / "guardian-evidence" / CASE


def revision_path(tmp_path, revision):
    return case_path(tmp_path) / f"{revision['revision_id']}.json"


def dump(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


def assert_invalid(action):
    with pytest.raises(HistoryError) as caught:
        action()
    assert caught.value.code == "EVIDENCE_INVALID"


def make_symlink(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        pytest.skip(f"Symlinks unavailable on this filesystem: {error}")


def test_empty_case_does_not_create_evidence_or_head(tmp_path):
    store = GuardianEvidenceStore(tmp_path)
    with store.locked(CASE) as case:
        assert case.load_latest() is None
        assert case.list_revisions() == []
        assert not (case_path(tmp_path) / "HEAD.json").exists()
    assert list(case_path(tmp_path).iterdir()) == []


def test_new_revision_has_identity_and_does_not_mutate_input(tmp_path):
    original = report()
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(original)
        assert saved["case_id"] == CASE
        assert len(saved["revision_id"]) == 32
        assert saved["previous_revision_id"] is None
        assert datetime.fromisoformat(saved["revision_created_at"]).tzinfo == UTC
        assert case.load_latest() == saved
        assert "revision_id" not in original
        saved["history"]["metrics"]["total_transactions"] = 999
        assert case.load_latest()["history"]["metrics"]["total_transactions"] == 100


def test_refresh_creates_new_revision_and_preserves_previous_bytes(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        first = case.create(report())
        first_bytes = revision_path(tmp_path, first).read_bytes()
        second = case.create(report(history={"metrics": {"total_transactions": 120}}))
        assert second["revision_id"] != first["revision_id"]
        assert second["previous_revision_id"] == first["revision_id"]
        assert revision_path(tmp_path, first).read_bytes() == first_bytes
        assert case.load_latest() == second
        assert [r["revision_id"] for r in case.list_revisions()] == [
            second["revision_id"],
            first["revision_id"],
        ]


def test_analysis_unavailable_retry_preserves_evidence_and_head(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(report())
        head_bytes = (case_path(tmp_path) / "HEAD.json").read_bytes()
        pending_history = deepcopy(saved["history"])
        saved.update(analysis_status="unavailable", analysis_error={"code": "AI_UNAVAILABLE"})
        unavailable = case.update(saved)
        assert unavailable["history"] == pending_history
        unavailable.update(analysis_status="completed", conclusions={"summary": "Synthetic"})
        done = case.update(unavailable)
        assert done["history"] == pending_history
        assert done["revision_id"] == saved["revision_id"]
        assert case.load_latest() == done
        assert (case_path(tmp_path) / "HEAD.json").read_bytes() == head_bytes


@pytest.mark.parametrize("field", (*module.IMMUTABLE_FIELDS, *module.IDENTITY_FIELDS, "report_id"))
def test_immutable_field_mutation_is_rejected_without_writes(tmp_path, field):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(report())
        before = revision_path(tmp_path, saved).read_bytes()
        changed = deepcopy(saved)
        changed[field] = "changed"
        assert_invalid(lambda: case.update(changed))
        assert revision_path(tmp_path, saved).read_bytes() == before


@pytest.mark.parametrize("field", (*module.IMMUTABLE_FIELDS, *module.IDENTITY_FIELDS, "report_id"))
def test_immutable_field_removal_is_rejected(tmp_path, field):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(report())
        saved.pop(field)
        assert_invalid(lambda: case.update(saved))


def test_strict_canonical_identity_detects_integer_changed_to_bool(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(report(policy={"threshold": 1}))
        saved["policy"]["threshold"] = True
        assert_invalid(lambda: case.update(saved))


def test_immutable_key_order_is_not_a_change(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(report(policy={"a": 1, "b": 2}))
        saved["policy"] = {"b": 2, "a": 1}
        assert case.update(saved)["policy"] == {"a": 1, "b": 2}


def test_previous_revision_cannot_be_updated(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        old = case.create(report())
        old_bytes = revision_path(tmp_path, old).read_bytes()
        current = case.create(report())
        old["analysis_status"] = "unavailable"
        assert_invalid(lambda: case.update(old))
        assert revision_path(tmp_path, old).read_bytes() == old_bytes
        assert case.load_latest() == current


def test_update_without_active_revision_is_rejected(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        assert_invalid(lambda: case.update(report()))


@pytest.mark.parametrize("field", module.IDENTITY_FIELDS)
def test_create_cannot_reuse_revision_identity(tmp_path, field):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        assert_invalid(lambda: case.create(report(**{field: "supplied"})))
        assert case.load_latest() is None


@pytest.mark.parametrize("field", module.IMMUTABLE_FIELDS)
def test_create_requires_immutable_evidence_fields(tmp_path, field):
    missing = report()
    missing.pop(field)
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        assert_invalid(lambda: case.create(missing))
        assert case.load_latest() is None


@pytest.mark.parametrize("identifier", [None, "", "../outside", "A" * 64, "f" * 63, "f" * 65])
def test_case_identifiers_are_strict_lowercase_hex(tmp_path, identifier):
    store = GuardianEvidenceStore(tmp_path)
    assert_invalid(lambda: store.locked(identifier).__enter__())
    assert not (tmp_path / "guardian-evidence").exists()


def test_lock_rejects_parallel_work_and_is_released_after_error(tmp_path):
    store = GuardianEvidenceStore(tmp_path)
    with pytest.raises(RuntimeError, match="synthetic failure"), store.locked(CASE):
        with pytest.raises(HistoryError) as caught, store.locked(CASE):
            pass
        assert caught.value.code == "AGENT_BUSY"
        # Independent cases do not block each other.
        with store.locked(OTHER_CASE) as independent:
            assert independent.load_latest() is None
        raise RuntimeError("synthetic failure")
    with store.locked(CASE) as case:
        assert case.load_latest() is None
    assert not (case_path(tmp_path) / ".lock").exists()


def test_case_is_unusable_after_context_exit(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        pass
    assert_invalid(case.load_latest)
    assert_invalid(lambda: case.create(report()))


@pytest.mark.parametrize(
    "head",
    [
        b"{broken",
        b"[]",
        b'{"revision_id":"x","revision_id":"y"}',
        b'{"unexpected": true}',
        b'{"value": NaN}',
        b'{"value": 1e999}',
        b'{"value": "\xff"}',
    ],
)
def test_corrupt_head_never_falls_back_to_empty_case(tmp_path, head):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        case.create(report())
        (case_path(tmp_path) / "HEAD.json").write_bytes(head)
        assert_invalid(case.load_latest)
        assert_invalid(lambda: case.create(report()))


@pytest.mark.parametrize(
    "change",
    [
        {"revision_id": "../../outside"},
        {"case_id": OTHER_CASE},
        {"store_version": "future"},
        {"revision_count": True},
        {"revision_count": 2},
        {"extra": "unexpected"},
    ],
)
def test_invalid_head_fields_are_rejected(tmp_path, change):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        case.create(report())
        path = case_path(tmp_path) / "HEAD.json"
        head = json.loads(path.read_text())
        head.update(change)
        dump(path, head)
        assert_invalid(case.load_latest)


def test_missing_head_with_existing_revision_fails_closed(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(report())
        before = revision_path(tmp_path, saved).read_bytes()
        (case_path(tmp_path) / "HEAD.json").unlink()
        assert_invalid(case.load_latest)
        assert_invalid(lambda: case.create(report()))
        assert revision_path(tmp_path, saved).read_bytes() == before


def test_missing_revision_is_not_overwritten_by_a_new_query(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(report())
        revision_path(tmp_path, saved).unlink()
        head = (case_path(tmp_path) / "HEAD.json").read_bytes()
        assert_invalid(case.load_latest)
        assert_invalid(lambda: case.create(report()))
        assert (case_path(tmp_path) / "HEAD.json").read_bytes() == head


@pytest.mark.parametrize("corrupt_previous", ["../outside", "f" * 32, "self", None])
def test_bad_previous_pointer_and_disconnected_chain_are_rejected(tmp_path, corrupt_previous):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        case.create(report())
        saved = case.create(report())
        saved["previous_revision_id"] = (
            saved["revision_id"] if corrupt_previous == "self" else corrupt_previous
        )
        dump(revision_path(tmp_path, saved), saved)
        assert_invalid(case.load_latest)


def test_corruption_in_older_revision_is_not_ignored(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        old = case.create(report())
        case.create(report())
        revision_path(tmp_path, old).write_bytes(b'{"case_id": "broken"}')
        assert_invalid(case.load_latest)
        assert_invalid(case.list_revisions)


@pytest.mark.parametrize("stamp", [None, "2026-09-09", "2026-09-09T12:00:00", "bad", 10])
def test_invalid_revision_timestamp_is_rejected(tmp_path, stamp):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(report())
        saved["revision_created_at"] = stamp
        dump(revision_path(tmp_path, saved), saved)
        assert_invalid(case.load_latest)


@pytest.mark.parametrize("raw", [b"[]", b'{"a":1,"a":2}', b'{"a":NaN}', b'{"a":1e999}'])
def test_invalid_saved_json_is_rejected(tmp_path, raw):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(report())
        revision_path(tmp_path, saved).write_bytes(raw)
        assert_invalid(case.load_latest)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), object(), {1: 1, "1": 2}])
def test_invalid_input_cannot_be_persisted(tmp_path, value):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        assert_invalid(lambda: case.create(report(conclusions=value)))
        assert case.load_latest() is None


def test_oversized_input_and_saved_json_are_rejected(tmp_path, monkeypatch):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(report())
        before = revision_path(tmp_path, saved).read_bytes()
        monkeypatch.setattr(module, "MAX_BYTES", 100)
        # _read accepts an explicit bound: test actual saved-size guard at production bound too.
        assert_invalid(lambda: case.create(report(conclusions="x" * 200)))
        assert revision_path(tmp_path, saved).read_bytes() == before
        assert_invalid(lambda: module._read(revision_path(tmp_path, saved), maximum=100))


def test_chain_count_and_total_bytes_are_bounded(tmp_path, monkeypatch):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        first = case.create(report())
        size = len(revision_path(tmp_path, first).read_bytes())
        monkeypatch.setattr(module, "MAX_CHAIN_BYTES", size + 1)
        assert_invalid(lambda: case.create(report()))
        assert case.load_latest() == first
        monkeypatch.setattr(module, "MAX_CHAIN_BYTES", 32_000_000)
        monkeypatch.setattr(module, "MAX_REVISIONS", 1)
        assert_invalid(lambda: case.create(report()))
        assert case.load_latest() == first


def test_metadata_is_latest_first_bounded_and_excludes_customer_payload(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        for _ in range(3):
            case.create(report())
        summaries = case.list_revisions(2)
        assert len(summaries) == 2
        assert summaries[0]["revision_id"] == case.load_latest()["revision_id"]
        assert set(summaries[0]) == {
            "revision_id",
            "previous_revision_id",
            "revision_created_at",
            "evidence_digest",
            "analysis_status",
        }
        assert "synthetic-mid" not in json.dumps(summaries)


@pytest.mark.parametrize("limit", [0, 21, True, "2", None])
def test_invalid_listing_limit_is_rejected(tmp_path, limit):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        assert_invalid(lambda: case.list_revisions(limit))


def test_revision_id_collision_never_overwrites_existing_bytes(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        saved = case.create(report())
        before = revision_path(tmp_path, saved).read_bytes()
        with patch.object(module, "uuid4") as fake_uuid:
            fake_uuid.return_value.hex = saved["revision_id"]
            assert_invalid(lambda: case.create(report()))
        assert revision_path(tmp_path, saved).read_bytes() == before
        assert case.load_latest() == saved


def test_failed_head_commit_leaves_visible_orphan_and_blocks_silent_requery(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        first = case.create(report())
        before = revision_path(tmp_path, first).read_bytes()
        head = (case_path(tmp_path) / "HEAD.json").read_bytes()
        original = module.os.replace

        def fail_head(source, target):
            if Path(target).name == "HEAD.json":
                raise OSError("synthetic disk failure")
            return original(source, target)

        with patch.object(module.os, "replace", side_effect=fail_head):
            assert_invalid(lambda: case.create(report()))
        assert revision_path(tmp_path, first).read_bytes() == before
        assert (case_path(tmp_path) / "HEAD.json").read_bytes() == head
        assert len(list(case_path(tmp_path).glob("*.json"))) == 3
        assert not list(case_path(tmp_path).glob("*.tmp"))
        assert_invalid(case.load_latest)
        assert_invalid(lambda: case.create(report()))


def test_unrecognized_or_temporary_files_fail_closed(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE) as case:
        (case_path(tmp_path) / ".revision-write-interrupted.tmp").write_bytes(b"partial")
        assert_invalid(case.load_latest)


@pytest.mark.parametrize("component", ["reports", "store", "case", "lock", "head", "revision"])
def test_symlink_components_are_rejected_without_following_target(tmp_path, component):
    target = tmp_path / "outside"
    target.mkdir()
    marker = target / "marker.json"
    marker.write_text("outside unchanged", encoding="utf-8")
    reports = tmp_path / "reports"
    reports.mkdir()
    store = GuardianEvidenceStore(reports)
    case_directory = reports / "guardian-evidence" / CASE
    if component == "reports":
        reports.rmdir()
        make_symlink(reports, target, directory=True)
        assert_invalid(lambda: GuardianEvidenceStore(reports))
    elif component in {"store", "case", "lock"}:
        path = {
            "store": reports / "guardian-evidence",
            "case": case_directory,
            "lock": case_directory / ".lock",
        }[component]
        path.parent.mkdir(parents=True, exist_ok=True)
        make_symlink(path, marker if component == "lock" else target, directory=component != "lock")
        assert_invalid(lambda: store.locked(CASE).__enter__())
    else:
        with store.locked(CASE) as case:
            saved = case.create(report())
            path = (
                case_directory / "HEAD.json"
                if component == "head"
                else case_directory / f"{saved['revision_id']}.json"
            )
            path.unlink()
            make_symlink(path, marker)
            assert_invalid(case.load_latest)
            assert_invalid(lambda: case.create(report()))
    assert marker.read_text() == "outside unchanged"


@pytest.mark.parametrize("component", ["reports", "store", "case", "lock", "head", "revision"])
def test_junction_detection_without_windows_creation_privilege(tmp_path, monkeypatch, component):
    """Exercise the reparse-point guard even where the OS cannot create symlinks."""
    reports = tmp_path / "reports"
    store = GuardianEvidenceStore(reports)
    with store.locked(CASE) as case:
        saved = case.create(report())
    paths = {
        "reports": reports,
        "store": reports / "guardian-evidence",
        "case": reports / "guardian-evidence" / CASE,
        "lock": reports / "guardian-evidence" / CASE / ".lock",
        "head": reports / "guardian-evidence" / CASE / "HEAD.json",
        "revision": reports / "guardian-evidence" / CASE / f"{saved['revision_id']}.json",
    }
    target = paths[component]
    if component == "lock":
        target.write_bytes(b"")
    with monkeypatch.context() as guard:
        guard.setattr(Path, "is_junction", lambda path: path == target)
        if component in {"head", "revision"}:
            with store.locked(CASE) as case:
                assert_invalid(case.load_latest)
        else:
            assert_invalid(lambda: store.locked(CASE).__enter__())


def test_case_cannot_continue_when_lock_is_missing(tmp_path, monkeypatch):
    store = GuardianEvidenceStore(tmp_path)
    lock = case_path(tmp_path) / ".lock"
    original = Path.stat

    def missing_lock(path, **kwargs):
        if path == lock:
            raise FileNotFoundError("synthetic removed lock")
        return original(path, **kwargs)

    # Windows denies real deletion of an open lock; simulate the failed existence check.
    with store.locked(CASE) as case, monkeypatch.context() as guard:
        guard.setattr(Path, "stat", missing_lock)
        assert_invalid(case.load_latest)
        assert_invalid(lambda: case.create(report()))
    assert not (case_path(tmp_path) / "HEAD.json").exists()


def semantic_validator(saved):
    """Synthetic application contract independent of the generic storage layer."""
    if saved["history"]["metrics"]["total_transactions"] != 100:
        raise ValueError("Synthetic historical count mismatch")
    if saved["evidence_digest"] != "c" * 64:
        raise ValueError("Synthetic digest mismatch")
    if saved.get("analysis_model") not in {None, "synthetic-model"}:
        raise ValueError("Synthetic model mismatch")


@pytest.mark.parametrize("operation", ["load", "list", "create", "update"])
@pytest.mark.parametrize("field", ["history", "evidence_digest", "analysis_model"])
def test_semantic_corruption_in_older_revision_blocks_all_operations(tmp_path, operation, field):
    store = GuardianEvidenceStore(tmp_path)
    with store.locked(CASE, validator=semantic_validator) as case:
        first = case.create(report())
        latest = case.create(report())
        corrupted = deepcopy(first)
        corrupted[field] = {
            "history": {"metrics": {"total_transactions": 999}},
            "evidence_digest": "d" * 64,
            "analysis_model": "wrong-model",
        }[field]
        dump(revision_path(tmp_path, first), corrupted)
        head_bytes = (case_path(tmp_path) / "HEAD.json").read_bytes()
        first_bytes = revision_path(tmp_path, first).read_bytes()
        latest_bytes = revision_path(tmp_path, latest).read_bytes()
        actions = {
            "load": case.load_latest,
            "list": case.list_revisions,
            "create": lambda: case.create(report()),
            "update": lambda: case.update({**latest, "analysis_status": "unavailable"}),
        }
        assert_invalid(actions[operation])
        assert (case_path(tmp_path) / "HEAD.json").read_bytes() == head_bytes
        assert revision_path(tmp_path, first).read_bytes() == first_bytes
        assert revision_path(tmp_path, latest).read_bytes() == latest_bytes


def test_semantic_callback_runs_on_every_revision_for_metadata_reads(tmp_path):
    store = GuardianEvidenceStore(tmp_path)
    with store.locked(CASE) as case:
        first = case.create(report())
        second = case.create(report())
        third = case.create(report())
    seen = []
    with store.locked(CASE, validator=lambda value: seen.append(value["revision_id"])) as case:
        assert len(case.list_revisions(limit=1)) == 1
    assert seen == [third["revision_id"], second["revision_id"], first["revision_id"]]


def test_semantic_callback_cannot_mutate_loaded_or_new_evidence(tmp_path):
    def modifying_validator(saved):
        saved["history"]["metrics"]["total_transactions"] = -10
        saved["revision_id"] = "changed-by-callback"
        return {"replacement": True}

    with GuardianEvidenceStore(tmp_path).locked(CASE, validator=modifying_validator) as case:
        saved = case.create(report())
        assert saved["history"]["metrics"]["total_transactions"] == 100
        assert saved["revision_id"] != "changed-by-callback"
        updated = case.update({**saved, "analysis_status": "unavailable"})
        assert updated["history"]["metrics"]["total_transactions"] == 100
        assert case.load_latest() == updated


def test_bad_new_revision_fails_validation_before_any_write(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE, validator=semantic_validator) as case:
        assert_invalid(lambda: case.create(report(history={"metrics": {"total_transactions": -1}})))
        assert case.load_latest() is None
        assert not list(case_path(tmp_path).glob("*.json"))


def test_bad_derived_analysis_update_fails_validation_before_write(tmp_path):
    with GuardianEvidenceStore(tmp_path).locked(CASE, validator=semantic_validator) as case:
        saved = case.create(report())
        before = revision_path(tmp_path, saved).read_bytes()
        assert_invalid(lambda: case.update({**saved, "analysis_model": "wrong-model"}))
        assert revision_path(tmp_path, saved).read_bytes() == before


@pytest.mark.parametrize("error_type", [ValueError, KeyError, RuntimeError, OSError])
def test_callback_validation_exceptions_fail_closed_and_release_lock(tmp_path, error_type):
    def failing_validator(saved):
        raise error_type("synthetic failure")

    store = GuardianEvidenceStore(tmp_path)
    with (
        pytest.raises(HistoryError) as caught,
        store.locked(CASE, validator=failing_validator) as case,
    ):
        case.create(report())
    assert caught.value.code == "EVIDENCE_INVALID"
    with store.locked(CASE) as case:
        assert case.load_latest() is None


def test_noncallable_validator_is_rejected_before_lock_creation(tmp_path):
    store = GuardianEvidenceStore(tmp_path)
    assert_invalid(lambda: store.locked(CASE, validator="invalid").__enter__())
    assert not (tmp_path / "guardian-evidence").exists()
