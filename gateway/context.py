from __future__ import annotations
from contextvars import ContextVar
from dataclasses import dataclass, field
import time
import uuid


@dataclass
class RequestContext:
    request_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    started: float = field(default_factory=time.monotonic)
    alias: str = "general-free"
    revision: str = ""
    allowed_deployments: frozenset[str] = frozenset()
    privacy_scope: str = ""
    key_id: str = ""
    body: dict = field(default_factory=dict, repr=False)
    attempts: int = 0
    current_deployment: str | None = None
    current_pools: tuple[str, ...] = ()
    terminal: str = "failed"
    genuine_terminal: bool = False
    saw_text: bool = False
    delivered: bool = False
    attempted_ids: set[str] = field(default_factory=set)
    stop_attempts: bool = False
    upstream_auth_failed: bool = False
    usage_source: str = "unknown"
    observed_usage: dict | None = None
    observed_headers: dict = field(default_factory=dict, repr=False)
    persisted_observation_headers: dict = field(default_factory=dict, repr=False)
    audit_failed: bool = False
    attempt_finalized: bool = False
    response_started: bool = False
    response_finished: bool = False
    streaming: bool = False
    attempt_started: float | None = None
    first_event_ms: int | None = None
    attempt_first_event_ms: int | None = None
    retry_after_until: float | None = None
    retry_reason: str | None = None
    error_code: str | None = None
    decision_snapshots: set[tuple] = field(default_factory=set, repr=False)


current_request: ContextVar[RequestContext | None] = ContextVar("gateway_request", default=None)
