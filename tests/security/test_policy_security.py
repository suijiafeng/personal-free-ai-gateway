"""Deterministic boundary tests. Stub downstream never makes a provider call."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
from fastapi import HTTPException
from gateway.config import Policy, load_policy
from gateway.context import RequestContext, current_request
from gateway.contract import check_capability, validate_request
from gateway.errors import GatewayError
from gateway.hooks import GatewayGuard
from gateway.ingress import GatewayIngress
from gateway.state import MemoryState

ROOT = Path(__file__).resolve().parents[2]

@pytest.fixture
def policy():
    return load_policy(ROOT / "config/policy.mock.yaml")

@pytest.fixture
def body():
    return {"model": "general-free", "messages": [{"role": "user", "content": "Synthetic harmless input"}]}

class Downstream:
    def __init__(self):
        self.calls = []

    async def __call__(self, scope, receive, send):
        self.calls.append({"scope": scope, "body": (await receive()).get("body", b"")})
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"application/json"), (b"x-secret-upstream", b"must-not-leak")]})
        await send({"type": "http.response.body", "body": b'{"ok":true}'})

async def invoke(policy, tmp_path, *, path="/v1/chat/completions", method="POST", payload=None,
                 raw=None, token="synthetic-consumer", extra_headers=None, query=b"", state=None):
    downstream = Downstream()
    app = GatewayIngress(downstream, policy, state or MemoryState(), tmp_path, "synthetic-admin")
    messages = []
    incoming = [{"type": "http.request", "body": raw if raw is not None else json.dumps(payload).encode(), "more_body": False}]
    async def receive():
        return incoming.pop(0) if incoming else {"type": "http.disconnect"}
    async def send(message):
        messages.append(message)
    scope = {"type": "http", "method": method, "path": path, "query_string": query,
             "headers": [(b"content-type", b"application/json"), (b"authorization", f"Bearer {token}".encode()), *(extra_headers or [])]}
    await app(scope, receive, send)
    status = next(m["status"] for m in messages if m["type"] == "http.response.start")
    return status, messages, downstream, app

@pytest.mark.parametrize("field,value", [
    ("api_base", "https://attacker.invalid"), ("api_key", "injected"),
    ("fallbacks", ["paid"]), ("model_list", []), ("user_config", {}),
    ("num_retries", 99), ("max_retries", 99), ("drop_params", True),
    ("tools", []), ("response_format", {"type": "json_object"}),
    ("metadata", {"gateway": {"deployment_ids": ["paid"]}}),
    ("vertex_ai_credentials", "/etc/passwd"), ("stream_options", {"include_usage": True}),
])
def test_top_level_override_is_rejected(policy, body, field, value):
    with pytest.raises(GatewayError) as exc:
        validate_request({**body, field: value}, policy)
    assert exc.value.status == 400

@pytest.mark.parametrize("message", [
    {"role": "user", "content": "x", "api_base": "https://attacker.invalid"},
    {"role": "user", "content": "x", "name": "extra"},
    {"role": "tool", "content": "x"}, {"role": "developer", "content": "x"},
    {"role": "user", "content": [{"type": "text", "text": "x"}]},
    {"role": "user", "content": {"api_key": "injected"}},
])
def test_nested_message_schema_is_closed(policy, body, message):
    with pytest.raises(GatewayError) as exc:
        validate_request({**body, "messages": [message]}, policy)
    assert exc.value.status == 400

@pytest.mark.parametrize("field,value", [("n", True), ("n", 2), ("n", 0), ("stream", 1),
    ("max_completion_tokens", True), ("max_completion_tokens", 0), ("max_completion_tokens", 4097), ("max_completion_tokens", 1.5)])
def test_types_and_numeric_bounds_are_strict(policy, body, field, value):
    with pytest.raises(GatewayError):
        validate_request({**body, field: value}, policy)

async def test_duplicate_json_top_level_and_nested_are_rejected(policy, tmp_path):
    for raw in [b'{"model":"general-free","model":"paid","messages":[{"role":"user","content":"x"}]}',
                b'{"model":"general-free","messages":[{"role":"user","content":"x","content":"y"}]}']:
        status, _, native, app = await invoke(policy, tmp_path, raw=raw)
        assert status == 400
        assert not native.calls
        assert app.active_requests == 0

@pytest.mark.parametrize("path,method", [("/v1/responses", "POST"), ("/chat/completions", "POST"),
    ("/model/new", "POST"), ("/config/update", "POST"), ("/docs", "GET"), ("/openapi.json", "GET"),
    ("/utils/transform_request", "POST"), ("/mcp", "POST"), ("/v1/chat/completions", "GET")])
async def test_unapproved_endpoints_never_reach_native(policy, body, tmp_path, path, method):
    status, _, native, _ = await invoke(policy, tmp_path, path=path, method=method, payload=body)
    assert status == 404
    assert not native.calls

@pytest.mark.parametrize("path,method", [("/v1/chat/completions", "POST"), ("/v1/models", "GET")])
async def test_administrator_master_key_cannot_consume(policy, body, tmp_path, path, method):
    status, _, native, _ = await invoke(policy, tmp_path, path=path, method=method, payload=body, token="synthetic-admin")
    assert status == 403
    assert not native.calls

async def test_consumer_cannot_generate_keys(policy, tmp_path):
    status, _, native, _ = await invoke(policy, tmp_path, path="/key/generate", payload={})
    assert status == 403
    assert not native.calls

async def test_query_override_and_provider_headers_are_not_forwarded(policy, body, tmp_path):
    status, _, native, _ = await invoke(policy, tmp_path, payload=body, query=b"api_base=https://attacker.invalid")
    assert status == 400 and not native.calls
    status, messages, native, _ = await invoke(policy, tmp_path, payload=body,
        extra_headers=[(b"x-litellm-api-base", b"https://attacker.invalid"), (b"x-provider-api-key", b"injected")])
    assert status == 200
    assert {k for k, _ in native.calls[0]["scope"]["headers"]} <= {b"authorization", b"content-type", b"accept", b"user-agent"}
    output_headers = next(m["headers"] for m in messages if m["type"] == "http.response.start")
    assert b"x-secret-upstream" not in dict(output_headers)

async def test_storage_failure_blocks_consumer_before_native(policy, body, tmp_path):
    state = MemoryState(); state.healthy = False
    status, _, native, _ = await invoke(policy, tmp_path, payload=body, state=state)
    assert status == 503 and not native.calls

@pytest.mark.parametrize("status", ["unknown", "trial", "paid"])
def test_unconfirmed_free_deployment_cannot_be_enabled(policy, status):
    data = policy.model_dump(mode="json")
    data["deployments"][0]["eligibility"]["status"] = status
    with pytest.raises(ValidationError):
        Policy.model_validate(data)

@pytest.mark.parametrize("change", ["expired", "future_checked", "missing", "no_hard_limit", "privacy"])
def test_eligibility_review_is_rechecked_at_call_time(policy, change):
    d = policy.deployments[0]
    if change == "expired": d.eligibility.review_by = datetime.now(timezone.utc) - timedelta(seconds=1)
    elif change == "future_checked": d.eligibility.checked_at = datetime.now(timezone.utc) + timedelta(days=1)
    elif change == "missing": d.eligibility.evidence = " "
    elif change == "no_hard_limit": d.eligibility.billing_hard_limit = False
    else: d.eligibility.privacy_scopes = []
    assert policy.exclusion(d) is not None
    assert d.id not in [x["model_info"]["id"] for x in policy.native_config()["model_list"]]

@pytest.mark.parametrize("change", ["stream", "roles", "parameter", "context", "output"])
def test_unapproved_candidate_capability_is_not_silently_dropped(policy, body, change):
    request = validate_request({**body, "stream": True}, policy)
    d = policy.deployments[1]
    if change == "stream": d.capability.streaming = False
    elif change == "roles": d.capability.roles = ["assistant"]
    elif change == "parameter": d.capability.parameters = []
    elif change == "context": d.capability.max_input_chars = 1
    else: d.capability.max_output_tokens = 1
    with pytest.raises(GatewayError) as exc:
        check_capability(request, d)
    assert exc.value.status == 400

def test_generated_native_config_has_no_retry_or_paid_default(policy):
    config = policy.native_config()
    router = config["router_settings"]
    assert router["num_retries"] == 0 and router["max_fallbacks"] == 1
    assert router["fallbacks"] == router["context_window_fallbacks"] == router["content_policy_fallbacks"] == []
    assert all(v == 0 for v in router["retry_policy"].values())
    assert config["litellm_settings"]["drop_params"] is False
    assert config["litellm_settings"]["cache"] is False
    assert config["general_settings"]["store_model_in_db"] is False
    assert config["general_settings"]["allow_requests_on_db_unavailable"] is False
    assert [d["litellm_params"]["order"] for d in config["model_list"]] == [1, 2]
    assert all(d["litellm_params"]["max_retries"] == 0 for d in config["model_list"])
    assert all(d["litellm_params"]["api_key"].startswith("os.environ/") for d in config["model_list"])

@pytest.mark.parametrize("grant", [
    {}, {"deployment_ids": ["mock-primary"], "privacy_scope": "unreviewed"},
    {"deployment_ids": ["paid-external"], "privacy_scope": "non_sensitive"},
])
async def test_unapproved_native_key_grants_fail_before_selection(policy, body, grant):
    guard = GatewayGuard().configure(policy, MemoryState())
    ctx = RequestContext(body=validate_request(body, policy))
    token = current_request.set(ctx)
    try:
        key = SimpleNamespace(user_role="internal_user", metadata={"gateway": grant}, models=[policy.alias], key_alias="synthetic")
        with pytest.raises(HTTPException):
            await guard.async_pre_call_hook(key, None, dict(body), "completion")
        assert ctx.attempts == 0
    finally:
        current_request.reset(token)

async def test_candidate_capability_intersection_checked_before_primary(policy, body):
    guard = GatewayGuard().configure(policy, MemoryState())
    policy.deployments[1].capability.parameters = []
    ctx = RequestContext(body=validate_request(body, policy))
    key = SimpleNamespace(user_role="internal_user", models=[policy.alias], key_alias="synthetic",
        metadata={"gateway": {"deployment_ids": [d.id for d in policy.deployments], "privacy_scope": policy.privacy_scope}})
    token = current_request.set(ctx)
    try:
        with pytest.raises(HTTPException) as exc:
            await guard.async_pre_call_hook(key, None, dict(body), "completion")
        assert exc.value.status_code == 400 and ctx.attempts == 0
    finally: current_request.reset(token)

@pytest.mark.parametrize("mutation", ["unapproved_alias", "unknown_deployment", "missing_expiry", "privacy", "extra_nested", "bool_concurrency"])
async def test_admin_cannot_create_unbounded_or_unreviewed_key(policy, tmp_path, mutation):
    data = {"models": [policy.alias], "duration": "1h", "max_parallel_requests": 1,
            "metadata": {"gateway": {"deployment_ids": ["mock-primary"], "privacy_scope": "non_sensitive"}}}
    if mutation == "unapproved_alias": data["models"] = ["*"]
    elif mutation == "unknown_deployment": data["metadata"]["gateway"]["deployment_ids"] = ["paid-external"]
    elif mutation == "missing_expiry": data.pop("duration")
    elif mutation == "privacy": data["metadata"]["gateway"]["privacy_scope"] = "sensitive"
    elif mutation == "extra_nested": data["metadata"]["gateway"]["api_base"] = "https://attacker.invalid"
    else: data["max_parallel_requests"] = True
    status, _, native, _ = await invoke(policy, tmp_path, path="/key/generate", token="synthetic-admin", payload=data)
    assert status == 400
    assert not native.calls

async def test_request_hook_forces_retry_zero_and_preserves_input(policy, body):
    state = MemoryState()
    guard = GatewayGuard().configure(policy, state)
    ctx = RequestContext(body=validate_request(body, policy))
    key = SimpleNamespace(user_role="internal_user", models=[policy.alias], key_alias="synthetic",
        metadata={"gateway": {"deployment_ids": ["mock-primary"], "privacy_scope": policy.privacy_scope}})
    token = current_request.set(ctx)
    try:
        data = {**body, "num_retries": 90, "max_retries": 90, "drop_params": True}
        result = await guard.async_pre_call_hook(key, None, data, "completion")
        assert result["num_retries"] == result["max_retries"] == 0
        assert result["drop_params"] is False
        assert result["messages"] == body["messages"]
        assert ctx.attempts == 0
    finally: current_request.reset(token)

@pytest.mark.parametrize("reason", ["delivered", "cancelled", "budget", "repeated", "target_override", "unapproved"])
async def test_per_attempt_guard_blocks_before_pool_or_provider_call(policy, body, reason):
    state = MemoryState()
    guard = GatewayGuard().configure(policy, state)
    d = policy.deployments[0]
    ctx = RequestContext(body=validate_request(body, policy), allowed_deployments=frozenset([d.id]), privacy_scope=policy.privacy_scope)
    kwargs = {"model_info": {"id": d.id}, "model": d.model, "api_base": d.api_base}
    if reason == "delivered": ctx.delivered = True
    elif reason == "cancelled": ctx.stop_attempts = True
    elif reason == "budget": ctx.attempts = policy.limits.max_attempts
    elif reason == "repeated": ctx.attempted_ids.add(d.id)
    elif reason == "target_override": kwargs["api_base"] = "https://attacker.invalid/v1"
    else: ctx.allowed_deployments = frozenset()
    previous_attempts = ctx.attempts
    token = current_request.set(ctx)
    try:
        with pytest.raises(GatewayError):
            await guard.async_pre_call_deployment_hook(kwargs, "acompletion")
        assert ctx.attempts == previous_attempts
        assert not state.inflight
        assert not state.events
    finally: current_request.reset(token)

async def test_unknown_alias_has_no_default_route(policy, body):
    with pytest.raises(GatewayError) as exc:
        validate_request({**body, "model": "unregistered-free-name"}, policy)
    assert exc.value.status == 404

@pytest.mark.parametrize('field,value',[
    ('id',''),('provider',''),('account_scope',''),('model_scope',[]),
    ('shared_scope_evidence',''),('reset_timezone','Not/A_Timezone'),
])
def test_quota_scope_config_rejects_unbounded_or_missing_identity(field,value):
    from gateway.config import Pool
    data={'id':'pool','provider':'mock','account_scope':'synthetic','model_scope':['mock-primary'],
          'dimension':'requests','window':'minute','reset_timezone':'UTC','shared_scope_evidence':'synthetic evidence'}
    data[field]=value
    with pytest.raises(ValueError):Pool.model_validate(data)


def test_production_pool_provider_must_match_actual_deployment():
    from gateway.config import load_policy,Policy
    from pathlib import Path
    policy=load_policy(Path(__file__).parents[2]/'config/policy.yaml').model_dump(mode='json')
    policy['pools'][0]['provider']='gemini'
    with pytest.raises(ValueError):Policy.model_validate(policy)


def test_pool_model_scope_must_include_target_model():
    from gateway.config import load_policy,Policy
    from pathlib import Path
    policy=load_policy(Path(__file__).parents[2]/'config/policy.mock.yaml').model_dump(mode='json')
    policy['pools'][0]['model_scope']=['different-model']
    with pytest.raises(ValueError):Policy.model_validate(policy)
