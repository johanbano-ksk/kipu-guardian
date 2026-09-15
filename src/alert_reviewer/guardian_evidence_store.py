"""Local, bounded evidence revisions; HEAD is the atomic commit pointer.

Extraction creates a new revision before analysis. Only the current revision's
derived analysis may then change. Earlier revisions and their evidence remain
untouched. A crash between the revision write and HEAD commit fails closed on the
next read: an orphan must be inspected, never silently replaced by a new query.
This is an integrity boundary for the application, not a signed audit ledger.
"""

from __future__ import annotations

import json
import os
import re
import stat
import tempfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from alert_reviewer.datalake_history import HistoryError

MAX_BYTES = 2_000_000
MAX_CHAIN_BYTES = 32_000_000
MAX_REVISIONS = 128
MAX_LIST_REVISIONS = 20
STORE_VERSION = "1.0"
IMMUTABLE_FIELDS = (
    "history",
    "evidence_digest",
    "original_alert",
    "policy",
    "metric_profile",
    "comparison_context",
    "protocol",
)
IDENTITY_FIELDS = (
    "case_id",
    "revision_id",
    "previous_revision_id",
    "revision_created_at",
)
_CASE_ID = re.compile(r"[0-9a-f]{64}\Z")
_REVISION_ID = re.compile(r"[0-9a-f]{32}\Z")


def _invalid(message: str = "La evidencia guardada requiere revisión.") -> HistoryError:
    return HistoryError("EVIDENCE_INVALID", message)


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _constant(value: str) -> None:
    raise ValueError("Non-finite JSON value")


def _decode(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs, parse_constant=_constant)
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        # Also rejects overflowing JSON exponents such as 1e999, parsed as infinity.
        json.dumps(value, allow_nan=False)
        return value
    except (ValueError, TypeError, RecursionError) as error:
        raise _invalid() from error


def _encode(value: dict[str, Any]) -> bytes:
    try:
        if not isinstance(value, dict):
            raise ValueError("Expected a JSON object")
        raw = json.dumps(
            value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        if len(raw) > MAX_BYTES:
            raise ValueError("Oversized evidence")
        _decode(raw)
        return raw
    except (ValueError, TypeError, UnicodeError, RecursionError) as error:
        raise _invalid() from error


def _safe_path(path: Path, *, directory: bool) -> None:
    """Reject symlinks/junctions in every existing component, without resolving them."""
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise _invalid("No se pudo verificar la ruta de evidencia.") from error
        if stat.S_ISLNK(info.st_mode) or part.is_junction():
            raise _invalid("Ruta de evidencia inválida.")
        if part != path or directory:
            if not stat.S_ISDIR(info.st_mode):
                raise _invalid("Ruta de evidencia inválida.")
        elif not stat.S_ISREG(info.st_mode):
            raise _invalid("Ruta de evidencia inválida.")


def _read(path: Path, *, maximum: int = MAX_BYTES) -> bytes:
    _safe_path(path, directory=False)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(path, flags), "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise _invalid()
            raw = stream.read(maximum + 1)
        if len(raw) > maximum:
            raise _invalid("La evidencia supera el límite de lectura local.")
        return raw
    except OSError as error:
        raise _invalid() from error


def _atomic_write(path: Path, raw: bytes, *, exclusive: bool = False) -> None:
    _safe_path(path, directory=False)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=path.parent, prefix=".revision-write-", suffix=".tmp", delete=False
        ) as stream:
            temporary = Path(stream.name)
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path, directory=False)
        if exclusive:
            # A hard link publishes complete bytes and cannot replace an existing revision.
            os.link(temporary, path)
        else:
            os.replace(temporary, path)
    except OSError as error:
        raise _invalid("No se pudo conservar la revisión de evidencia.") from error
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class GuardianEvidenceStore:
    """One exclusive local filesystem lock per immutable alert case."""

    def __init__(self, reports: Path) -> None:
        self.reports = Path(os.path.abspath(reports))
        _safe_path(self.reports, directory=True)
        self.directory = self.reports / "guardian-evidence"

    @contextmanager
    def locked(
        self,
        case_id: str,
        *,
        validator: Callable[[dict[str, Any]], Any] | None = None,
    ) -> Iterator[GuardianEvidenceCase]:
        """Apply an optional raising semantic validator to every persisted revision.

        The callback receives an isolated copy, and its return value is ignored.
        Validation is repeated before all reads and writes; a corrupt older
        revision cannot be bypassed by refreshing or reading only HEAD metadata.
        """
        if not isinstance(case_id, str) or not _CASE_ID.fullmatch(case_id):
            raise _invalid("Identificador de caso inválido.")
        if validator is not None and not callable(validator):
            raise _invalid("Validador de evidencia inválido.")
        directory = self.directory / case_id
        _safe_path(directory, directory=True)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise _invalid("No se pudo abrir el almacén de evidencia.") from error
        _safe_path(directory, directory=True)
        lock = directory / ".lock"
        _safe_path(lock, directory=False)
        try:
            handle = lock.open("xb")
        except FileExistsError:
            raise HistoryError("AGENT_BUSY", "Esta alerta ya está siendo analizada.") from None
        except OSError as error:
            raise _invalid("No se pudo bloquear el caso de evidencia.") from error
        lock_info = os.fstat(handle.fileno())
        case = GuardianEvidenceCase(directory, case_id, lock_info, validator=validator)
        try:
            yield case
        finally:
            case._active = False
            handle.close()
            # Do not unlink a replaced lock belonging to another process or a symlink.
            _safe_path(lock, directory=False)
            try:
                current = lock.stat()
                if (current.st_dev, current.st_ino) != (lock_info.st_dev, lock_info.st_ino):
                    raise _invalid("Cambió el bloqueo del caso; requiere revisión.")
                lock.unlink()
            except OSError as error:
                raise _invalid("No se pudo liberar el bloqueo del caso.") from error


class GuardianEvidenceCase:
    """Use only inside ``GuardianEvidenceStore.locked(case_id)``."""

    def __init__(
        self,
        directory: Path,
        case_id: str,
        lock_info: os.stat_result,
        *,
        validator: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        self.directory = directory
        self.case_id = case_id
        self._validator = validator
        self._lock_identity = (lock_info.st_dev, lock_info.st_ino)
        self._active = True

    def _check_active(self) -> None:
        if not self._active:
            raise _invalid("El caso de evidencia no está bloqueado.")
        _safe_path(self.directory, directory=True)
        lock = self.directory / ".lock"
        _safe_path(lock, directory=False)
        try:
            current = lock.stat()
        except OSError as error:
            raise _invalid("El bloqueo del caso requiere revisión.") from error
        if (current.st_dev, current.st_ino) != self._lock_identity:
            raise _invalid("Cambió el bloqueo del caso; requiere revisión.")

    def _validate_revision(self, report: dict[str, Any], revision_id: str) -> None:
        try:
            if report["case_id"] != self.case_id or report["revision_id"] != revision_id:
                raise ValueError("Revision identity mismatch")
            previous = report["previous_revision_id"]
            if previous is not None and (
                not isinstance(previous, str) or not _REVISION_ID.fullmatch(previous)
            ):
                raise ValueError("Invalid previous revision")
            stamp = report["revision_created_at"]
            if not isinstance(stamp, str) or "T" not in stamp:
                raise ValueError("Invalid revision timestamp")
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
            if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(None):
                raise ValueError("Revision timestamp must be UTC")
            if any(field not in report for field in IMMUTABLE_FIELDS):
                raise ValueError("Missing immutable evidence")
        except (ValueError, TypeError, KeyError) as error:
            raise _invalid() from error

    def _validate_semantics(self, report: dict[str, Any]) -> None:
        if self._validator is None:
            return
        try:
            self._validator(deepcopy(report))
        except Exception as error:
            # This extension point is intentionally fail-closed for any ordinary
            # validation failure, while process termination still propagates.
            raise _invalid(
                "Una revisión de evidencia no supera la validación semántica."
            ) from error

    def _chain(self) -> tuple[list[dict[str, Any]], int]:
        self._check_active()
        revisions: set[str] = set()
        head_exists = False
        try:
            for path in self.directory.iterdir():
                _safe_path(path, directory=False)
                if path.name == ".lock":
                    continue
                if path.name == "HEAD.json":
                    head_exists = True
                elif path.suffix == ".json" and _REVISION_ID.fullmatch(path.stem):
                    revisions.add(path.stem)
                else:
                    raise _invalid(
                        "El almacén contiene archivos no reconocidos; requiere revisión."
                    )
                if len(revisions) > MAX_REVISIONS:
                    raise _invalid("Se alcanzó el límite local de revisiones por caso.")
        except OSError as error:
            raise _invalid() from error
        if not head_exists:
            if revisions:
                raise _invalid("Hay evidencia sin un índice confirmado; requiere revisión.")
            return [], 0
        head = _decode(_read(self.directory / "HEAD.json", maximum=4096))
        if (
            set(head) != {"store_version", "case_id", "revision_id", "revision_count"}
            or head["store_version"] != STORE_VERSION
            or head["case_id"] != self.case_id
            or type(head["revision_count"]) is not int
            or head["revision_count"] != len(revisions)
            or not revisions
            or not isinstance(head["revision_id"], str)
            or not _REVISION_ID.fullmatch(head["revision_id"])
        ):
            raise _invalid("El índice de revisiones requiere revisión.")
        chain: list[dict[str, Any]] = []
        visited: set[str] = set()
        revision_id = head["revision_id"]
        size = 0
        while revision_id is not None:
            if revision_id not in revisions or revision_id in visited:
                raise _invalid("La cadena de evidencia está incompleta o es circular.")
            visited.add(revision_id)
            raw = _read(self.directory / f"{revision_id}.json")
            size += len(raw)
            if size > MAX_CHAIN_BYTES:
                raise _invalid("La cadena de evidencia supera el límite de lectura local.")
            report = _decode(raw)
            self._validate_revision(report, revision_id)
            chain.append(report)
            revision_id = report["previous_revision_id"]
        if visited != revisions:
            raise _invalid("Hay revisiones fuera de la cadena confirmada; requiere revisión.")
        for report in chain:
            self._validate_semantics(report)
        return chain, size

    def load_latest(self) -> dict[str, Any] | None:
        """Return a fresh report object, or None only for a truly empty case."""
        chain, _ = self._chain()
        return chain[0] if chain else None

    def create(self, report: dict[str, Any]) -> dict[str, Any]:
        """Commit new frozen evidence, linking the previous complete revision."""
        chain, size = self._chain()
        saved = _decode(_encode(report))
        if any(field in saved for field in IDENTITY_FIELDS):
            raise _invalid("Una revisión nueva no puede reutilizar identidad de otra revisión.")
        if len(chain) >= MAX_REVISIONS:
            raise _invalid("Se alcanzó el límite local de revisiones por caso.")
        revision_id = uuid4().hex
        saved.update(
            case_id=self.case_id,
            revision_id=revision_id,
            previous_revision_id=chain[0]["revision_id"] if chain else None,
            revision_created_at=datetime.now(UTC).isoformat(),
        )
        self._validate_revision(saved, revision_id)
        self._validate_semantics(saved)
        raw = _encode(saved)
        if size + len(raw) > MAX_CHAIN_BYTES:
            raise _invalid("La cadena de evidencia supera el límite de almacenamiento local.")
        _atomic_write(self.directory / f"{revision_id}.json", raw, exclusive=True)
        _atomic_write(
            self.directory / "HEAD.json",
            _encode(
                {
                    "store_version": STORE_VERSION,
                    "case_id": self.case_id,
                    "revision_id": revision_id,
                    "revision_count": len(chain) + 1,
                }
            ),
        )
        return saved

    def update(self, report: dict[str, Any]) -> dict[str, Any]:
        """Update derived analysis only on HEAD; never rewrite previous revisions."""
        chain, size = self._chain()
        if not chain:
            raise _invalid("No existe una revisión activa para actualizar.")
        saved = _decode(_encode(report))
        latest = chain[0]
        self._validate_revision(saved, latest["revision_id"])
        frozen = (*IMMUTABLE_FIELDS, *IDENTITY_FIELDS, "report_id")
        before = {key: latest[key] for key in frozen if key in latest}
        after = {key: saved[key] for key in frozen if key in saved}
        if _encode(before) != _encode(after):
            raise _invalid("La evidencia y la identidad de una revisión son inmutables.")
        self._validate_semantics(saved)
        raw = _encode(saved)
        if size - len(_encode(latest)) + len(raw) > MAX_CHAIN_BYTES:
            raise _invalid("La cadena de evidencia supera el límite de almacenamiento local.")
        _atomic_write(self.directory / f"{latest['revision_id']}.json", raw)
        return saved

    def list_revisions(self, limit: int = MAX_LIST_REVISIONS) -> list[dict[str, Any]]:
        """Latest-first bounded metadata, without merchant IDs or original payloads."""
        if type(limit) is not int or not 1 <= limit <= MAX_LIST_REVISIONS:
            raise _invalid("Límite de listado de revisiones inválido.")
        chain, _ = self._chain()
        fields = (
            "revision_id",
            "previous_revision_id",
            "revision_created_at",
            "evidence_digest",
            "analysis_status",
        )
        return [{field: report.get(field) for field in fields} for report in chain[:limit]]
