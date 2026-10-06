"""Durable per-session storage for batched operation submission.

A session pins the shared initial document and initial version vector and
accumulates every accepted batch into one cumulative operation log.  Both
reads and batch commits re-run the ordinary merge rules *from the initial
document* over the whole log -- the previous result is never used as a new
baseline -- so late-arriving concurrent writes still take part in the
original arbitration and conflict accounting, and a batch may causally
reference operations stored by earlier batches.

Every commit that contributes new operations also appends an immutable
**history boundary**: the merged document at the new revision.  Boundary
``h`` is the document of revision ``h``, so the events of revision ``r``
are the snapshot diff from boundary ``r-1`` to boundary ``r`` -- exactly
what ``POST /route-changes`` would compute between the two documents.
Boundaries are persisted in the same atomic write as the revision (inside
the same cross-process locked section), so an incremental reader can page
through committed revisions after a restart.  Retransmissions, stale
``expected_revision`` submissions and invalid batches add no history.

Late-arriving concurrent edits change the *current* merge result but can
never rewrite a previously committed boundary: a boundary stores the
document snapshot that was current at commit time, not a pointer into a
later recomputation.

Sessions written by older versions of the service have no history
boundaries.  On the first access (any read, or the first successful
commit) of such a legacy record the boundary history is seeded with one
entry: the currently visible merged document at the session's current
revision.  That revision becomes the session's history origin: cursors
before it, or past the current revision, are rejected (422 at the HTTP
layer).

Batch commits are atomic: validation, recomputation and the revision bump
happen inside a critical section guarded by an in-process lock *and* a
per-session ``flock`` file (``.{session_id}.lock`` next to the session
file), and the record is re-read from disk inside that section.  The
guarantee therefore holds not only between threads of one process but
also between several service processes sharing ``SESSION_STORE_DIR``:
two concurrent submissions carrying the same expected revision cannot
both succeed -- the loser observes the bumped revision and gets a
conflict.  The updated session is written to disk (temp file +
``os.replace``) before the lock is released, and reads always come
straight from disk (never from a per-process cache), so every process
immediately sees the latest committed revision.

JSONPath replacement commits (``replace_paths``) reuse the same critical
section: the restricted-JSONPath selection is evaluated there against the
locked revision's recomputed document, so selected paths can never belong
to a stale revision; the generated per-actor contiguous ``set`` run and
the revision check, recomputation, boundary append and persist then act
as one commit.  Over-limit (>20 matches) or ancestor/descendant match
sets, malformed expressions and query-budget overruns reject before
anything is written; a zero-hit query is a success that stores no
operation, bumps no revision and appends no boundary.

A session may additionally pin up to 20 distinct **protected JSON
Pointers** at creation time.  The rules are immutable for the session's
lifetime and live in its durable file, so they keep working after a
restart and between processes sharing the store directory; records
written before this feature existed simply carry an empty rule set.
Inside each commit's locked section -- both ``submit_batch`` and
``replace_paths`` -- the freshly recomputed pre/post documents are
compared at every protected pointer, before any log, revision or
boundary is written.  A mismatch rejects the whole commit with
``ProtectedPathError`` (HTTP 422).  Comparison is value based and JSON
aware: a missing node differs from an explicit ``null``, a boolean
differs from a number, and replacing an ancestor subtree or
inserting/deleting in an array so that an index points at another value
is therefore caught as well.  Commits that generate operations but leave
every protected value (and the visible document) untouched still commit
in the ordinary way; retransmissions and zero-hit replacements reach no
comparison at all, and a stale ``expected_revision`` keeps its 409.

Sessions are persisted as one JSON file per session inside a directory
(``SESSION_STORE_DIR`` env var, default ``.sessions``), so they remain
readable after the service (or ``docker compose``) restarts.  The lock
files are never deleted -- they are tiny, their number is bounded by the
number of sessions, and deleting them would reintroduce an
unlink/reopen race.
"""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import threading
import uuid
from collections.abc import Iterator
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .engine import merge_logs, parse_pointer, validate_and_dedupe
from .models import Operation, Subscription
from .query import QueryValidationError, execute, parse_path
from .routing import (
    _json_equal,
    compile_subscriptions,
    diff_events,
    route_events,
)

_SESSION_ID_RE = re.compile(r"[0-9a-f]{32}\Z")

#: Maximum number of committed revisions returned by one incremental read.
CHANGES_PAGE_SIZE = 20

#: Maximum number of nodes one POST .../replace query may match.
REPLACE_MATCH_LIMIT = 20


class SessionNotFoundError(KeyError):
    """Raised when a session id does not exist."""


class RevisionConflictError(RuntimeError):
    """Raised when the expected revision does not match the current one."""


class CursorOutOfRangeError(ValueError):
    """Raised when an incremental-read cursor is outside the known history."""


class ReplaceValidationError(ValueError):
    """Raised when a JSONPath replace batch is malformed or unsafe."""


class ProtectedPathError(ValueError):
    """Raised when a commit would change a session-protected JSON Pointer."""


#: Marker for a JSON Pointer that does not resolve to any node.  It is
#: deliberately distinct from ``None``: an explicit JSON ``null`` must be
#: reported as a real value, not as "missing".
_MISSING_VALUE = object()


def _pointer_value(document: Any, tokens: list[str]) -> Any:
    """Resolve parsed JSON Pointer ``tokens`` in ``document``.

    Returns ``_MISSING_VALUE`` when navigation fails (a non-object/array
    ancestor, an absent member or an out-of-range array index), mirroring
    the merge engine's own pointer semantics.
    """
    node: Any = document
    for token in tokens:
        if isinstance(node, dict):
            if token not in node:
                return _MISSING_VALUE
            node = node[token]
        elif isinstance(node, list):
            # Mirror the merge engine's own array navigation exactly:
            # digits-only tokens are indices, every other token (including
            # RFC 6901's append token "-") is a missing node.
            index = int(token) if token.isdigit() else None
            if index is None or not (0 <= index < len(node)):
                return _MISSING_VALUE
            node = node[index]
        else:
            return _MISSING_VALUE
    return node


def _values_equal(left: Any, right: Any) -> bool:
    """JSON-aware equality keeping missing/null/booleans/numbers distinct."""
    if left is _MISSING_VALUE or right is _MISSING_VALUE:
        return left is right
    return _json_equal(left, right)


def _check_protected_paths(
    protected_paths: list[str], before: Any, after: Any
) -> None:
    """Reject a commit changing the value at any protected pointer.

    The comparison runs per pointer over the whole pre/post documents, so
    it catches not only direct writes to a protected location but also
    ancestor replacement and array insert/delete shifts that move what an
    index points at.  A missing location differs from JSON ``null`` and a
    boolean differs from a number.
    """
    for pointer in protected_paths:
        tokens = parse_pointer(pointer)
        if not _values_equal(_pointer_value(before, tokens), _pointer_value(after, tokens)):
            raise ProtectedPathError(
                f"commit rejected: protected JSON Pointer {pointer!r} changed"
            )


def _reject_nested_pointers(session_id: str, pointers: list[str]) -> None:
    """Reject a match set containing a parent/child pair (whole request 422).

    Replacing an ancestor and its descendant in one batch is ambiguous --
    the ancestor's subtree overwrite would discard the descendant's value
    regardless of operation order -- so the whole request is rejected.
    """
    tokens: list[list[str]] = []
    for pointer in pointers:
        try:
            tokens.append(parse_pointer(pointer))
        except ValueError as exc:  # pragma: no cover - matches emit valid pointers
            raise ReplaceValidationError(
                f"session {session_id!r}: query returned an invalid JSON "
                f"Pointer {pointer!r}"
            ) from exc
    for i in range(len(tokens)):
        for j in range(i + 1, len(tokens)):
            a, b = tokens[i], tokens[j]
            shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
            if longer[: len(shorter)] == shorter:
                raise ReplaceValidationError(
                    f"matched paths {pointers[i]!r} and {pointers[j]!r} are "
                    "in a parent/child relationship; refine the query so no "
                    "match contains another"
                )


@dataclass
class _Boundary:
    """One immutable history point: the merged document at ``revision``."""

    revision: int
    document: Any

    def to_json(self) -> dict[str, Any]:
        return {"revision": self.revision, "document": self.document}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "_Boundary":
        return cls(revision=int(data["revision"]), document=data["document"])


@dataclass
class SessionRecord:
    """The durable state of one session.

    ``document``/``version`` are the pinned initial state; ``log`` is the
    cumulative, de-duplicated operation log accepted so far; ``revision``
    counts the batches that contributed at least one new operation.
    ``history`` is the revision-ordered list of document snapshots
    (boundaries), starting at the history origin -- revision 0 for new
    sessions, the then-current revision for records created before this
    feature existed.  ``history is None`` marks a not-yet-migrated legacy
    record.
    """

    session_id: str
    revision: int
    document: Any
    version: dict[str, int]
    log: list[Operation] = field(default_factory=list)
    history: list[_Boundary] | None = None
    protected_paths: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "revision": self.revision,
            "document": self.document,
            "version": self.version,
            "log": [op.model_dump(mode="json") for op in self.log],
            # Legacy records (history is None) keep omitting the field;
            # the first successful access seeds and persists it.
            **(
                {"history": [b.to_json() for b in self.history]}
                if self.history is not None
                else {}
            ),
            # Always written by new code; records from before this feature
            # omit it and load with the empty rule set.
            "protected_paths": list(self.protected_paths),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> "SessionRecord":
        raw_history = data.get("history", None)
        history = (
            [_Boundary.from_json(item) for item in raw_history]
            if raw_history is not None
            else None
        )
        return cls(
            session_id=data["session_id"],
            revision=int(data["revision"]),
            document=data["document"],
            version={k: int(v) for k, v in data["version"].items()},
            log=[Operation.model_validate(o) for o in data["log"]],
            history=history,
            protected_paths=list(data.get("protected_paths", [])),
        )


@dataclass
class BatchOutcome:
    """Result of an accepted batch submission."""

    session_id: str
    revision: int
    accepted: int
    duplicates: int


@dataclass
class ReplaceOutcome:
    """Result of an accepted JSONPath replacement submission."""

    session_id: str
    revision: int
    matched_paths: list[str]
    operations: list[tuple[str, int, str]]


class SessionStore:
    """Directory-backed session store with atomic batch commits.

    Commits are serialized both between the threads of this process (an
    in-process lock) and between processes sharing the same directory (a
    per-session ``flock`` file); the record is always re-read from disk
    inside that locked section, so the revision check, recomputation and
    persist step act as one cross-process critical section.
    """

    def __init__(self, directory: str | os.PathLike[str]):
        self._dir = Path(directory)
        self._lock = threading.RLock()

    @classmethod
    def from_env(cls) -> "SessionStore":
        return cls(os.environ.get("SESSION_STORE_DIR", ".sessions"))

    # -- persistence ---------------------------------------------------

    def _path(self, session_id: str) -> Path:
        return self._dir / f"{session_id}.json"

    def _lock_path(self, session_id: str) -> Path:
        return self._dir / f".{session_id}.lock"

    @contextlib.contextmanager
    def _locked(self, session_id: str) -> Iterator[None]:
        """Serialize one session's commit section across threads and processes.

        The in-process lock orders the threads of this process; the
        ``flock`` on the per-session lock file orders processes that share
        the store directory.  The lock file is created on first use and
        never deleted, so there is no unlink/reopen race.
        """
        with self._lock:
            self._dir.mkdir(parents=True, exist_ok=True)
            with self._lock_path(session_id).open("a+b") as fh:
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def _persist(self, rec: SessionRecord) -> None:
        """Write ``rec`` atomically (temp file + rename within the directory)."""
        self._dir.mkdir(parents=True, exist_ok=True)
        tmp = self._dir / f".{rec.session_id}.{uuid.uuid4().hex}.tmp"
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(rec.to_json(), fh)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, self._path(rec.session_id))

    def _load(self, session_id: str) -> SessionRecord | None:
        """Read the record straight from disk; nothing is cached per process.

        Every call observes the latest committed revision, including
        commits written by other processes sharing the directory.  The
        atomic temp-file-plus-rename persist means a reader never sees a
        partially written record, so reads need no file lock.
        """
        if not _SESSION_ID_RE.fullmatch(session_id):
            return None
        try:
            raw = self._path(session_id).read_text(encoding="utf-8")
        except OSError:
            return None
        try:
            return SessionRecord.from_json(json.loads(raw))
        except (KeyError, TypeError, ValueError):
            return None

    # -- session lifecycle ----------------------------------------------

    @staticmethod
    def _merged_document(rec: SessionRecord) -> Any:
        """The current merged document, recomputed from the initial document."""
        return merge_logs(rec.document, rec.version, [rec.log])["document"]

    def _seed_history(self, rec: SessionRecord) -> bool:
        """Seed boundary history for a legacy record; return whether seeded.

        The origin is the merged document visible at the record's current
        revision.  The caller must hold the session's cross-process lock
        and persist after this returns ``True``.
        """
        if rec.history is not None:
            return False
        if rec.revision == 0:
            origin = deepcopy(rec.document)
        else:
            origin = self._merged_document(rec)
        rec.history = [_Boundary(rec.revision, origin)]
        return True

    def create(
        self,
        document: Any,
        version: dict[str, int],
        protected_paths: list[str] | None = None,
    ) -> SessionRecord:
        """Create a session pinned to ``document``/``version`` at revision 0.

        ``protected_paths`` are the immutable protection rules for the
        session's lifetime; the caller is responsible for having validated
        their syntax (the HTTP model does).
        """
        initial_version = dict(version or {})
        validate_and_dedupe([], initial_version)  # rejects negative entries
        rec = SessionRecord(
            session_id=uuid.uuid4().hex,
            revision=0,
            document=document,
            version=initial_version,
            # Revision 0 is the history origin: its boundary is the pinned
            # initial document and no event can ever point before it.
            history=[_Boundary(0, deepcopy(document))],
            protected_paths=list(protected_paths or []),
        )
        with self._lock:
            self._persist(rec)
        return rec

    def get(self, session_id: str) -> SessionRecord:
        with self._lock:
            rec = self._load(session_id)
        if rec is None:
            raise SessionNotFoundError(session_id)
        return rec

    def get_state(self, session_id: str) -> dict[str, Any]:
        """Recompute the merged state from the initial document and full log."""
        with self._lock:
            rec = self._load(session_id)
        if rec is None:
            raise SessionNotFoundError(session_id)
        # Reads stay lock-free (the atomic temp-file-plus-rename persist
        # guarantees a coherent file); only a legacy record's first touch
        # enters the cross-process section to seed and persist its origin.
        if rec.history is None:
            rec = self._migrate_legacy(session_id)
        result = merge_logs(rec.document, rec.version, [rec.log])
        return {
            "session_id": rec.session_id,
            "revision": rec.revision,
            "document": result["document"],
            "version": result["version"],
            "conflicts": result["conflicts"],
            "tombstones": result["tombstones"],
            "protected_paths": list(rec.protected_paths),
        }

    def _migrate_legacy(self, session_id: str) -> SessionRecord:
        """Re-load and seed a pre-upgrade record's history origin under lock.

        The record is re-read from disk inside the cross-process section so
        concurrent first touches converge on one persisted origin.
        """
        with self._locked(session_id):
            rec = self._load(session_id)
            if rec is None:  # pragma: no cover - existence checked by caller
                raise SessionNotFoundError(session_id)
            self._seed_and_persist(rec)
            return rec

    def _seed_and_persist(self, rec: SessionRecord) -> None:
        if self._seed_history(rec):
            self._persist(rec)

    def submit_batch(
        self, session_id: str, expected_revision: int, operations: list[Operation]
    ) -> BatchOutcome:
        """Validate, recompute and commit one batch atomically.

        The batch is validated together with the already-stored log under
        the ordinary rules: retransmitted operations (same id, same
        content) are not stored twice, the same id with different content
        is rejected, and causal references may point at stored operations.
        A batch that contributes no new operation leaves the revision --
        and the history -- untouched; any validation or execution failure
        leaves the session untouched as well (no partial commits, no new
        boundary).

        The record is re-read from disk and the revision check,
        recomputation, history append and persist all run inside the
        session's cross-process lock, so two processes sharing the store
        directory cannot both commit on top of the same revision, and the
        new revision's boundary is durable together with the revision.
        """
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise SessionNotFoundError(session_id)
        with self._locked(session_id):
            rec = self._load(session_id)
            if rec is None:
                raise SessionNotFoundError(session_id)
            if expected_revision != rec.revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision} but session "
                    f"{session_id!r} is at revision {rec.revision}"
                )
            combined = list(rec.log) + list(operations)
            # Full validation of the cumulative log: identical
            # retransmissions are de-duplicated, conflicting ids rejected.
            unique_ops, duplicates, _ = validate_and_dedupe(combined, rec.version)
            stored = {(op.actor, op.seq) for op in rec.log}
            new_ops = [op for op in unique_ops if (op.actor, op.seq) not in stored]
            if not new_ops:
                # Exact retransmission at the current revision: a success
                # is still an access, so a legacy record gets its origin
                # seeded here as well -- without adding any history entry.
                self._seed_and_persist(rec)
                return BatchOutcome(session_id, rec.revision, 0, duplicates)
            # Legacy records carry no boundary for the pre-batch state;
            # seed the origin (the document visible at the current
            # revision) before diffing against the new document.
            self._seed_history(rec)
            # Recompute from the initial document over the cumulative log;
            # a validation or execution failure raises before anything is
            # written, so the session keeps its previous revision and log
            # and the legacy origin is also not persisted.
            new_document = merge_logs(rec.document, rec.version, [unique_ops])[
                "document"
            ]
            # Protected-path enforcement happens on the recomputed
            # pre/post documents while still holding the session lock and
            # before any mutation or persist: a changed protected pointer
            # rejects the whole commit, so no log, revision or history
            # boundary is written.
            _check_protected_paths(
                rec.protected_paths, rec.history[-1].document, new_document
            )
            rec.log = unique_ops
            rec.revision += 1
            # Immutable boundary: later batches never rewrite this entry,
            # even when late concurrent edits change the current merge.
            rec.history.append(_Boundary(rec.revision, new_document))
            self._persist(rec)
            return BatchOutcome(session_id, rec.revision, len(new_ops), duplicates)

    # -- JSONPath multi-path replacement --------------------------------

    def replace_paths(
        self,
        session_id: str,
        expected_revision: int,
        actor: str,
        path_expression: str,
        value: Any,
    ) -> ReplaceOutcome:
        """Select existing nodes with one JSONPath and commit one set per hit.

        The query is evaluated against the current merged document *inside*
        the session's cross-process critical section, so the selected paths
        always belong to the revision the caller named.  At most
        ``REPLACE_MATCH_LIMIT`` matches are taken (more rejects the whole
        request) and no two matches may be in a parent/child relationship;
        a zero-hit query is a success that adds no operation, revision or
        history boundary.

        Otherwise the matches, in document pre-order, become one
        contiguous run of ``set`` operations for ``actor``.  Their version
        vectors witness the merged version of the whole prior log and each
        preceding operation of the run, and the commit goes through exactly
        the same validation, recomputation, boundary append and atomic
        persist steps as :meth:`submit_batch` -- another process committing
        the same revision first turns this request into a ``409`` and paths
        selected from a stale document can never be written to a newer one.
        """
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise SessionNotFoundError(session_id)
        with self._locked(session_id):
            rec = self._load(session_id)
            if rec is None:
                raise SessionNotFoundError(session_id)
            if expected_revision != rec.revision:
                raise RevisionConflictError(
                    f"expected revision {expected_revision} but session "
                    f"{session_id!r} is at revision {rec.revision}"
                )
            # Selection happens on the locked revision's recomputed document.
            merged = merge_logs(rec.document, rec.version, [rec.log])
            current_document = merged["document"]
            base_version = merged["version"]
            try:
                segments = parse_path(path_expression)
                matches = execute(current_document, segments)
            except QueryValidationError as exc:
                raise ReplaceValidationError(str(exc)) from exc
            if len(matches) > REPLACE_MATCH_LIMIT:
                raise ReplaceValidationError(
                    f"JSONPath matched {len(matches)} nodes; at most "
                    f"{REPLACE_MATCH_LIMIT} may be replaced in one request"
                )
            pointers = [row["path"] for row in matches]
            _reject_nested_pointers(session_id, pointers)
            if not pointers:
                # Nothing selected: no operation, no revision, no history.
                # As for an exact batch retransmission, a legacy record's
                # origin is still seeded -- without appending a boundary.
                self._seed_and_persist(rec)
                return ReplaceOutcome(session_id, rec.revision, [], [])
            # A failed commit must not seed a legacy origin either.
            self._seed_history(rec)
            base_seq = max(
                rec.version.get(actor, 0),
                base_version.get(actor, 0),
            )

            def op_version(offset: int) -> dict[str, int]:
                # Witness the whole prior log (every actor's merged
                # position) and the earlier operations of this run.
                version = dict(base_version)
                version[actor] = base_seq + offset
                return version

            operations = [
                Operation(
                    actor=actor,
                    seq=base_seq + offset,
                    version=op_version(offset),
                    path=pointer,
                    action="set",
                    value=deepcopy(value),
                )
                for offset, pointer in enumerate(pointers, start=1)
            ]
            # Validate the cumulative log and re-merge from the initial
            # document under the ordinary rules; a failure raises before
            # anything is persisted, so no operation or history survives it.
            combined = list(rec.log) + operations
            unique_ops, _, _ = validate_and_dedupe(combined, rec.version)
            new_document = merge_logs(rec.document, rec.version, [unique_ops])[
                "document"
            ]
            # Same protection gate as /batches: the generated set ops are
            # rejected wholesale when any protected pointer's value would
            # change -- including shifts caused by the matched writes
            # themselves -- before a single operation or boundary is
            # stored.
            _check_protected_paths(
                rec.protected_paths, current_document, new_document
            )
            rec.log = unique_ops
            rec.revision += 1
            rec.history.append(_Boundary(rec.revision, new_document))
            self._persist(rec)
            return ReplaceOutcome(
                session_id,
                rec.revision,
                pointers,
                [
                    (actor, base_seq + offset, pointer)
                    for offset, pointer in enumerate(pointers, start=1)
                ],
            )

    # -- incremental revision reads -------------------------------------

    def read_changes(
        self,
        session_id: str,
        after_revision: int,
        subscriptions: list[Subscription],
    ) -> dict[str, Any]:
        """Return at most ``CHANGES_PAGE_SIZE`` routed revisions after a cursor.

        Each returned revision carries the ``/route-changes`` events of its
        boundary document relative to the previous revision's boundary;
        revisions whose document is unchanged still appear with empty
        routes/unrouted.  Boundaries are read from durable history, so a
        revision's events can never change after the commit -- including
        when later concurrent edits change the current merged document.

        Cursors before the session's history origin or past the current
        revision raise ``CursorOutOfRangeError`` (HTTP 422).  With no new
        revisions the cursor is echoed unchanged.
        """
        if not _SESSION_ID_RE.fullmatch(session_id):
            raise SessionNotFoundError(session_id)
        with self._lock:
            rec = self._load(session_id)
        if rec is None:
            raise SessionNotFoundError(session_id)
        if rec.history is None:
            rec = self._migrate_legacy(session_id)
        # Validate patterns after the session is known to exist (so a
        # missing session stays a 404) and after any legacy migration; an
        # invalid request therefore never seeds or persists anything.
        compiled = compile_subscriptions(subscriptions)
        history = rec.history
        assert history is not None  # just seeded above
        origin = history[0].revision
        if after_revision < origin or after_revision > rec.revision:
            raise CursorOutOfRangeError(
                f"after_revision {after_revision} is outside the known "
                f"history of session {session_id!r}: revisions "
                f"{origin}..{rec.revision}"
            )
        return self._build_changes(rec, history, after_revision, compiled)

    def _build_changes(
        self,
        rec: SessionRecord,
        history: list[_Boundary],
        after_revision: int,
        compiled: list[tuple[str, tuple, tuple]],
    ) -> dict[str, Any]:
        # Boundaries are revision-contiguous from the origin, so the
        # previous document of the first selected revision is at index-1.
        start = after_revision - history[0].revision
        selected = history[start + 1 : start + 1 + CHANGES_PAGE_SIZE]
        changes: list[dict[str, Any]] = []
        prev_doc = history[start].document
        for boundary in selected:
            events = diff_events(prev_doc, boundary.document)
            routed = route_events(events, compiled)
            changes.append(
                {
                    "revision": boundary.revision,
                    "routes": routed["routes"],
                    "unrouted": routed["unrouted"],
                }
            )
            prev_doc = boundary.document
        if selected:
            next_cursor = selected[-1].revision
        else:
            next_cursor = after_revision
        return {
            "session_id": rec.session_id,
            "current_revision": rec.revision,
            "after_revision": after_revision,
            "next_cursor": next_cursor,
            "has_more": next_cursor < rec.revision,
            "changes": changes,
        }
