"""FastAPI application exposing the offline JSON merge service."""
from __future__ import annotations

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from .engine import LogValidationError, merge_logs
from .models import (
    ApplyRequest,
    BatchRequest,
    BatchResponse,
    MergeRequest,
    MergeResult,
    OperationId,
    QueryRequest,
    QueryResponse,
    ReplaceRequest,
    ReplaceResponse,
    ResolveRequest,
    ResolveResult,
    RevisionChangesRequest,
    RevisionChangesResponse,
    RouteChangesRequest,
    RouteChangesResult,
    SessionCreateRequest,
    SessionCreateResponse,
    SessionStateResponse,
)
from .query import QueryValidationError, run_queries
from .resolve import ResolveValidationError, resolve_documents
from .routing import RoutingValidationError, route_changes
from .sessions import (
    CursorOutOfRangeError,
    ReplaceValidationError,
    RevisionConflictError,
    SessionNotFoundError,
    SessionStore,
)

app = FastAPI(
    title="Offline JSON Merge API",
    version="1.7.0",
    description=(
        "Concurrent, offline-first merging of JSON document operation logs "
        "using vector clocks, deterministic (actor, seq) arbitration, "
        "tombstones and stable array inserts, with optional pre-merge "
        "operation exclusion and replay-log export; snapshot diffing with "
        "wildcard subscriber routing (POST /route-changes); restricted "
        "JSONPath querying (POST /query); cross-document reference "
        "resolution with overlays (POST /resolve); and durable "
        "batched-submission sessions (POST /sessions, "
        "POST /sessions/{session_id}/batches, GET /sessions/{session_id}, "
        "POST /sessions/{session_id}/replace, "
        "POST /sessions/{session_id}/changes) that accumulate operation "
        "batches into one cumulative log re-merged from the initial "
        "document on every commit, and page the per-revision routed "
        "change events after a caller-supplied revision cursor."
    ),
)

app.state.session_store = SessionStore.from_env()


def _session_store(request: Request) -> SessionStore:
    return request.app.state.session_store


@app.get("/healthz")
def healthz() -> dict[str, str]:
    return {"status": "ok"}


def _excluded_ids(exclude: list[OperationId] | None) -> list[tuple[str, int]] | None:
    if not exclude:
        return None
    return [(item.actor, item.seq) for item in exclude]


def _merge_response(result: dict) -> JSONResponse:
    """Serialize a merge result, omitting pruning fields absent by request."""
    payload = MergeResult.model_validate(result).model_dump(mode="json")
    if result.get("replay_log") is None:
        payload.pop("replay_log", None)
        payload.pop("excluded", None)
    return JSONResponse(payload)


@app.post("/merge", response_model=MergeResult)
def merge(req: MergeRequest) -> JSONResponse:
    """Merge the operation logs of two replicas of the same initial document."""
    try:
        result = merge_logs(
            req.document,
            req.version,
            [req.replica_a, req.replica_b],
            exclude=_excluded_ids(req.exclude),
        )
    except LogValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _merge_response(result)


@app.post("/apply", response_model=MergeResult)
def apply(req: ApplyRequest) -> JSONResponse:
    """Replay a single replica's operation log onto a document."""
    try:
        result = merge_logs(
            req.document,
            req.version,
            [req.operations],
            exclude=_excluded_ids(req.exclude),
        )
    except LogValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _merge_response(result)


@app.post(
    "/route-changes",
    response_model=RouteChangesResult,
)
def route_changes_endpoint(req: RouteChangesRequest) -> dict:
    """Diff two JSON snapshots and route change events to subscribers."""
    try:
        return route_changes(req.before, req.after, req.subscriptions)
    except RoutingValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/query", response_model=QueryResponse)
def query_endpoint(req: QueryRequest) -> dict:
    """Evaluate restricted JSONPath queries against a document (read-only)."""
    try:
        return run_queries(req.document, req.queries)
    except QueryValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@app.post("/resolve", response_model=ResolveResult)
def resolve_endpoint(req: ResolveRequest) -> dict:
    """Materialize cross-document JSON references into a single document."""
    try:
        return resolve_documents(req.documents, req.root)
    except ResolveValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# batched-submission sessions
# ---------------------------------------------------------------------------


@app.post("/sessions", response_model=SessionCreateResponse)
def create_session(req: SessionCreateRequest, request: Request) -> dict:
    """Create a session pinned to an initial document and version vector."""
    store = _session_store(request)
    try:
        rec = store.create(req.document, req.version)
    except LogValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {"session_id": rec.session_id, "revision": rec.revision}


@app.get("/sessions/{session_id}", response_model=SessionStateResponse)
def get_session(session_id: str, request: Request) -> dict:
    """Read the latest revision and the recomputed merge state of a session."""
    try:
        return _session_store(request).get_state(session_id)
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"session {session_id!r} not found"
        ) from exc


@app.post("/sessions/{session_id}/batches", response_model=BatchResponse)
def submit_batch(session_id: str, req: BatchRequest, request: Request) -> dict:
    """Atomically validate, recompute and commit one operation batch."""
    try:
        outcome = _session_store(request).submit_batch(
            session_id, req.expected_revision, req.operations
        )
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"session {session_id!r} not found"
        ) from exc
    except RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LogValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "session_id": outcome.session_id,
        "revision": outcome.revision,
        "accepted": outcome.accepted,
        "duplicates": outcome.duplicates,
    }


@app.post(
    "/sessions/{session_id}/replace",
    response_model=ReplaceResponse,
)
def replace_by_path(
    session_id: str, req: ReplaceRequest, request: Request
) -> dict:
    """Atomically set one value at every node a JSONPath matches.

    Selection on the current merged document, generated-operation
    validation, the expected-revision check and persistence are one
    cross-process commit: a stale revision yields 409 and a malformed,
    over-budget or nested-path query yields 422, neither writing anything.
    """
    try:
        outcome = _session_store(request).replace_paths(
            session_id,
            req.expected_revision,
            req.actor,
            req.path,
            req.value,
        )
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"session {session_id!r} not found"
        ) from exc
    except RevisionConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (ReplaceValidationError, LogValidationError) as exc:
        # ReplaceValidationError: malformed/over-budget/nested query.
        # LogValidationError: the generated set ops failed the ordinary
        # merge rules; nothing is persisted in either case.
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "session_id": outcome.session_id,
        "revision": outcome.revision,
        "matched_paths": outcome.matched_paths,
        "operations": [
            {"actor": actor, "seq": seq, "path": path}
            for actor, seq, path in outcome.operations
        ],
    }


@app.post(
    "/sessions/{session_id}/changes",
    response_model=RevisionChangesResponse,
)
def read_revision_changes(
    session_id: str, req: RevisionChangesRequest, request: Request
) -> dict:
    """Page committed revisions' routed change events after a revision cursor."""
    try:
        return _session_store(request).read_changes(
            session_id, req.after_revision, req.subscriptions
        )
    except SessionNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"session {session_id!r} not found"
        ) from exc
    except CursorOutOfRangeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except RoutingValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
