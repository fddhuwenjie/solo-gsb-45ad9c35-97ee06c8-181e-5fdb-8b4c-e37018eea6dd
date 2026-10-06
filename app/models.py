"""Pydantic request/response models for the merge API."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, Field, field_validator, model_serializer


class Operation(BaseModel):
    """A single operation in a replica's operation log.

    - ``actor``:   unique id of the replica that produced the operation.
    - ``seq``:     per-actor sequence number, contiguous and increasing.
    - ``version``: causal version vector observed by the actor when the
      operation was issued (must contain ``version[actor] == seq``).
    - ``path``:    JSON Pointer (RFC 6901) to the target node; ``""`` is the
      document root.  For ``insert`` the path points at the target array.
    - ``action``:  ``set`` | ``delete`` | ``insert``.
    - ``value``:   payload for ``set``/``insert`` (ignored by ``delete``).
    - ``index``:   target position inside an array for ``insert``
      (defaults to appending at the end).
    """

    actor: str = Field(min_length=1)
    seq: int = Field(ge=1)
    version: dict[str, int] = Field(default_factory=dict)
    path: str = ""
    action: Literal["set", "delete", "insert"]
    value: Any = None
    index: int | None = Field(default=None, ge=0)

    @field_validator("path")
    @classmethod
    def _check_pointer(cls, v: str) -> str:
        if v != "" and not v.startswith("/"):
            raise ValueError(f"path must be a JSON Pointer (got {v!r})")
        return v


class OperationId(BaseModel):
    """Reference to a single operation by its ``(actor, seq)`` identity."""

    actor: str = Field(min_length=1)
    seq: int = Field(ge=1)


_EXCLUDE_ALIAS = AliasChoices("exclude", "exclude_operations")


class MergeRequest(BaseModel):
    """Body of POST /merge: the shared initial document plus both logs."""

    document: Any = None
    version: dict[str, int] = Field(
        default_factory=dict,
        description="version vector of the initial document",
    )
    replica_a: list[Operation] = Field(default_factory=list)
    replica_b: list[Operation] = Field(default_factory=list)
    exclude: list[OperationId] | None = Field(
        default=None,
        validation_alias=_EXCLUDE_ALIAS,
        description=(
            "optional operation ids to drop before merging; same-actor "
            "successors and cross-actor operations that depend on them are "
            "cascaded out (alias: exclude_operations)"
        ),
    )


class ApplyRequest(BaseModel):
    """Body of POST /apply: a document plus a single replica's log."""

    document: Any = None
    version: dict[str, int] = Field(default_factory=dict)
    operations: list[Operation] = Field(default_factory=list)
    exclude: list[OperationId] | None = Field(
        default=None,
        validation_alias=_EXCLUDE_ALIAS,
        description=(
            "optional operation ids to drop before replaying; same-actor "
            "successors and cross-actor operations that depend on them are "
            "cascaded out (alias: exclude_operations)"
        ),
    )


class OpSummary(BaseModel):
    actor: str
    seq: int
    action: str
    value: Any = None


class Conflict(BaseModel):
    path: str
    reason: str
    winner: OpSummary
    losers: list[OpSummary]


class ExcludedOperation(BaseModel):
    """One operation dropped by a pruning request.

    ``mode`` is ``"explicit"`` for ids listed in ``exclude`` and
    ``"cascade"`` for operations forced out by an explicit exclusion
    (a same-actor higher seq, or a cross-actor operation depending on a
    dropped operation).  ``reason`` names the triggering operation.
    """

    actor: str
    seq: int
    mode: Literal["explicit", "cascade"]
    reason: str | None = None


class MergeResult(BaseModel):
    document: Any = None
    version: dict[str, int]
    conflicts: list[Conflict]
    tombstones: list[str]
    applied: int
    duplicates: int
    # Present only when the request carries an ``exclude`` list:
    replay_log: list[Operation] | None = Field(
        default=None,
        description=(
            "retained operations in deterministic causal order; replayable "
            "via POST /apply from the same initial document (only present "
            "when the request carries an 'exclude' list)"
        ),
    )
    excluded: list[ExcludedOperation] | None = Field(
        default=None,
        description=(
            "dropped operations sorted by (actor, seq), distinguishing "
            "'explicit' exclusions from 'cascade' removals (only present "
            "when the request carries an 'exclude' list)"
        ),
    )


# ---------------------------------------------------------------------------
# POST /route-changes
# ---------------------------------------------------------------------------


class Subscription(BaseModel):
    """A subscriber's include/exclude patterns for change events.

    Both lists hold JSON Pointer patterns (RFC 6901 escaping) with two
    wildcard segments: ``*`` matches exactly one path segment and ``**``
    matches zero or more segments.  An event must hit at least one
    ``include`` and no ``exclude`` to be routed to this subscriber.
    """

    subscriber_id: str = Field(min_length=1)
    include: list[str] = Field(
        default_factory=list,
        description="JSON Pointer patterns; '*' one segment, '**' zero or more",
    )
    exclude: list[str] = Field(default_factory=list)


class RouteChangesRequest(BaseModel):
    """Body of POST /route-changes: two snapshots plus 1..100 subscriptions."""

    before: Any = None
    after: Any = None
    subscriptions: list[Subscription] = Field(min_length=1, max_length=100)


class ChangeEvent(BaseModel):
    """A single change at the smallest differing path.

    ``old`` is present on remove/replace events and ``new`` on add/replace
    events (an explicit JSON ``null`` value is always preserved).
    """

    type: Literal["add", "remove", "replace"]
    path: str = Field(description="JSON Pointer (RFC 6901) of the changed node")
    old: Any = Field(default=None, description="previous value (replace/remove)")
    new: Any = Field(default=None, description="current value (add/replace)")

    @model_serializer
    def _serialize(self) -> dict[str, Any]:
        data: dict[str, Any] = {"type": self.type, "path": self.path}
        if self.type in ("remove", "replace"):
            data["old"] = self.old
        if self.type in ("add", "replace"):
            data["new"] = self.new
        return data


class SubscriberChanges(BaseModel):
    subscriber_id: str
    events: list[ChangeEvent]


class RouteChangesResult(BaseModel):
    routes: list[SubscriberChanges]
    unrouted: list[ChangeEvent]


# ---------------------------------------------------------------------------
# POST /query
# ---------------------------------------------------------------------------


class QueryItem(BaseModel):
    """A single restricted-JSONPath query.

    - ``query_id``: unique identifier of the query within the request.
    - ``path``:     restricted JSONPath expression (root ``$``, ``.name``,
      bracket keys with escapes, array indices, ``[*]``, recursive descent
      ``..name``/``..*`` and array filters ``[?(@.field op literal)]``).
      The alias ``query`` is accepted for convenience.
    """

    query_id: str = Field(min_length=1)
    path: str = Field(
        min_length=1,
        validation_alias=AliasChoices("path", "query"),
        description="restricted JSONPath expression",
    )


class QueryRequest(BaseModel):
    """Body of POST /query: a document plus 1..20 queries."""

    document: Any = None
    queries: list[QueryItem] = Field(min_length=1, max_length=20)


class QueryMatch(BaseModel):
    """One matched node: JSON Pointer (RFC 6901) plus the node's value."""

    path: str = Field(description="JSON Pointer of the matched node ('' is root)")
    value: Any = None


class QueryResult(BaseModel):
    query_id: str
    matches: list[QueryMatch]


class QueryResponse(BaseModel):
    results: list[QueryResult]


# ---------------------------------------------------------------------------
# POST /resolve
# ---------------------------------------------------------------------------


class ResolveDocument(BaseModel):
    """One named input document for ``POST /resolve``."""

    document_id: str = Field(min_length=1)
    document: Any = None


class ResolveRequest(BaseModel):
    """Body of POST /resolve: 1..50 named documents plus the root reference.

    ``root`` has the form ``document_id`` (whole document) or
    ``document_id#/JSON/Pointer`` and must name one of the documents.
    """

    documents: list[ResolveDocument] = Field(min_length=1, max_length=50)
    root: str = Field(min_length=1)


class ResolveResult(BaseModel):
    resolved: Any = None
    dependencies: list[str]
    sources: dict[str, str]


# ---------------------------------------------------------------------------
# POST /sessions -- durable batched operation submission
# ---------------------------------------------------------------------------


class SessionCreateRequest(BaseModel):
    """Body of POST /sessions: the shared initial document and version.

    The session pins both for its whole lifetime; every accepted batch is
    re-merged from this initial state over the cumulative operation log.

    ``protected_paths`` optionally lists up to 20 distinct JSON Pointers
    (RFC 6901) whose values must remain unchanged by every later commit
    (``/batches`` and ``/replace``).  The rules are stored with the session
    and never change after creation; a malformed pointer or a duplicated
    entry rejects the whole request (422).  Sessions created before this
    feature existed behave as if the list were empty.
    """

    document: Any = None
    version: dict[str, int] = Field(
        default_factory=dict,
        description="version vector of the initial document",
    )
    protected_paths: list[str] = Field(
        default_factory=list,
        max_length=20,
        description=(
            "up to 20 distinct JSON Pointers (RFC 6901) protected against "
            "any value change for the session's lifetime; '' is the root"
        ),
    )

    @field_validator("protected_paths")
    @classmethod
    def _check_protected_paths(cls, paths: list[str]) -> list[str]:
        for pointer in paths:
            _validate_pointer_syntax(pointer)
        if len(set(paths)) != len(paths):
            raise ValueError("protected_paths must not contain duplicates")
        return paths


def _validate_pointer_syntax(pointer: str) -> None:
    """Validate the syntax of an RFC 6901 JSON Pointer.

    ``""`` denotes the document root; every other pointer must start with
    ``/`` and each ``~`` must be escaped as ``~0``/``~1``.  Array indices
    are syntactically ordinary segments here (an out-of-range or
    non-numeric segment simply resolves to a missing node at read time).
    """
    if pointer == "":
        return
    if not pointer.startswith("/"):
        raise ValueError(
            f"protected path {pointer!r} must be a JSON Pointer "
            "(empty string for the root or starting with '/')"
        )
    for segment in pointer.split("/")[1:]:
        i = 0
        while i < len(segment):
            if segment[i] == "~" and (
                i + 1 >= len(segment) or segment[i + 1] not in "01"
            ):
                raise ValueError(
                    f"protected path {pointer!r} is not a valid JSON Pointer: "
                    "unescaped '~' (use '~0' for '~' and '~1' for '/')"
                )
            i += 1


class SessionCreateResponse(BaseModel):
    session_id: str
    revision: int
    protected_paths: list[str] = Field(
        default_factory=list,
        description="protected JSON Pointers stored with this session",
    )


class BatchRequest(BaseModel):
    """Body of POST /sessions/{session_id}/batches: one non-empty batch.

    ``expected_revision`` must equal the session's current revision
    (optimistic concurrency); the batch is validated, re-merged and
    committed atomically -- a stale revision yields ``409`` and an invalid
    batch ``422``, neither of which writes anything.
    """

    expected_revision: int = Field(ge=0)
    operations: list[Operation] = Field(min_length=1)


class BatchResponse(BaseModel):
    session_id: str
    revision: int = Field(
        description=(
            "latest revision after the commit; unchanged when the batch "
            "contained no new operations"
        )
    )
    accepted: int = Field(description="operations newly stored from this batch")
    duplicates: int = Field(
        description="retransmitted operations (same id, same content) skipped"
    )


class SessionStateResponse(BaseModel):
    """Latest session state, recomputed from the initial document."""

    session_id: str
    revision: int
    document: Any = None
    version: dict[str, int]
    conflicts: list[Conflict]
    tombstones: list[str]
    protected_paths: list[str] = Field(
        default_factory=list,
        description="protected JSON Pointers pinned at session creation",
    )


# ---------------------------------------------------------------------------
# POST /sessions/{id}/replace -- one JSONPath, one value, one atomic commit
# ---------------------------------------------------------------------------


class ReplaceRequest(BaseModel):
    """Body of POST /sessions/{id}/replace: one JSONPath applied at once.

    ``path`` is a restricted JSONPath expression with the exact same syntax
    as the ``path``/``query`` field of ``POST /query``.  Every existing
    node it matches on the current merged document (at most 20) is written
    with ``value`` by one ``set`` operation attributed to ``actor``; the
    operations are numbered contiguously for that actor and carry causal
    version vectors covering the whole prior log.  ``expected_revision``
    is the optimistic-concurrency guard, exactly as for ``/batches``.
    """

    expected_revision: int = Field(ge=0)
    actor: str = Field(min_length=1)
    path: str = Field(
        min_length=1,
        validation_alias=AliasChoices("path", "query"),
        description="restricted JSONPath expression (same syntax as POST /query)",
    )
    value: Any = None


class ReplaceOperationId(BaseModel):
    """One generated set operation's identity, in document pre-order."""

    actor: str
    seq: int
    path: str = Field(description="JSON Pointer (RFC 6901) of the matched node")


class ReplaceResponse(BaseModel):
    session_id: str
    revision: int = Field(
        description=(
            "latest revision after the commit; unchanged when the query "
            "matched no node"
        )
    )
    matched_paths: list[str] = Field(
        description="JSON Pointers of the matched nodes, in document pre-order"
    )
    operations: list[ReplaceOperationId] = Field(
        description="the generated set operations, in the same order"
    )


# ---------------------------------------------------------------------------
# POST /sessions/{id}/changes -- incremental, subscription-routed revision reads
# ---------------------------------------------------------------------------


class RevisionChangesRequest(BaseModel):
    """Body of POST /sessions/{id}/changes: a cursor plus 1..100 subscriptions.

    ``after_revision`` is the last revision the caller has already read;
    revisions strictly after it (up to 20 per response) are returned in
    revision order, each carrying the route-changes events of its document
    relative to the previous revision, routed with exactly the same rules
    as ``POST /route-changes``.
    """

    after_revision: int = Field(
        ge=0,
        description="last revision already read; revision 0 is the initial document",
    )
    subscriptions: list[Subscription] = Field(min_length=1, max_length=100)


class RevisionChanges(BaseModel):
    """One committed revision and its routed changes versus revision - 1.

    A revision stays present even when its document is identical to the
    previous revision's; its routes/unrouted are simply empty then.
    """

    revision: int
    routes: list[SubscriberChanges]
    unrouted: list[ChangeEvent]


class RevisionChangesResponse(BaseModel):
    """Incremental revision window; ``next_cursor`` continues the paging."""

    session_id: str
    current_revision: int = Field(
        description="the session's latest committed revision at read time"
    )
    after_revision: int = Field(description="the cursor echoed from the request")
    next_cursor: int = Field(
        description=(
            "revision of the last entry returned (== current_revision when no "
            "more revisions remain); equals after_revision when there were no "
            "new revisions"
        )
    )
    has_more: bool = Field(
        description="true when more committed revisions remain past next_cursor"
    )
    changes: list[RevisionChanges] = Field(
        description="up to 20 revisions after the cursor, in revision order"
    )
