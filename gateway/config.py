"""Compile an immutable, reviewed policy to native LiteLLM settings, never route requests."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo,ZoneInfoNotFoundError

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class Capability(Strict):
    roles: list[Literal["system", "user", "assistant"]] = ["system", "user", "assistant"]
    streaming: bool = True
    max_output_tokens: int = Field(default=4096, ge=1, le=4096)
    max_input_chars: int = Field(default=32000, ge=1, le=1000000)
    parameters: list[Literal["max_completion_tokens", "stream"]] = ["max_completion_tokens", "stream"]


class Eligibility(Strict):
    status: Literal["confirmed_free", "unknown", "trial", "paid"] = "unknown"
    evidence: str = ""
    checked_at: datetime | None = None
    review_by: datetime | None = None
    billing_hard_limit: bool = False
    privacy_scopes: list[Literal["non_sensitive"]] = []


class Pool(Strict):
    id: str
    provider: str
    account_scope: str
    model_scope: list[str]
    dimension: Literal["requests", "tokens"]
    window: Literal["minute", "day", "unknown"]
    reset_timezone: str | None = None
    shared_scope_evidence: str

    @model_validator(mode="after")
    def validate_scope(self):
        for value in (self.id,self.provider,self.account_scope,self.shared_scope_evidence,*self.model_scope):
            if not value.strip() or len(value)>2048 or any(ord(c)<32 for c in value):
                raise ValueError("quota scope fields require nonempty bounded text")
        if not self.model_scope or len(self.model_scope)!=len(set(self.model_scope)):
            raise ValueError("quota model scope requires unique model identities")
        if self.reset_timezone is not None:
            try:
                ZoneInfo(self.reset_timezone)
            except (ZoneInfoNotFoundError,ValueError):
                raise ValueError("quota reset timezone must be a valid IANA timezone") from None
        self.model_scope=sorted(self.model_scope)
        return self


class Deployment(Strict):
    id: str
    provider: Literal["openai", "groq", "gemini"]
    adapter: Literal["native", "openai_sdk"] = "native"
    model: str
    api_base: str
    credential_env: str
    enabled: bool = False
    order: int = Field(ge=1, le=2)
    eligibility: Eligibility = Eligibility()
    capability: Capability = Capability()
    quota_pools: list[str]


class Limits(Strict):
    concurrency: int = Field(default=2, ge=1, le=2)
    max_attempts: int = Field(default=2, ge=1, le=2)
    total_timeout: float = Field(default=90, gt=0, le=90)
    first_event_timeout: float = Field(default=15, gt=0, le=15)
    stream_idle_timeout: float = Field(default=20, gt=0, le=20)
    max_body_bytes: int = Field(default=1048576, ge=1, le=1048576)
    default_output_tokens: int = Field(default=1024, ge=1, le=4096)
    retention_days: int = Field(default=7, ge=1, le=7)
    cooldown_seconds: int = Field(default=60, ge=1, le=86400)
    observation_max_age_seconds: int = Field(default=300, ge=1, le=86400)


class Policy(Strict):
    schema_version: Literal[1] = 1
    profile: Literal["production", "mock"]
    alias: Literal["general-free"] = "general-free"
    privacy_scope: Literal["non_sensitive"] = "non_sensitive"
    allowed_hosts: list[str]
    limits: Limits = Limits()
    pools: list[Pool]
    deployments: list[Deployment]

    @model_validator(mode="after")
    def check_graph(self):
        ids = [d.id for d in self.deployments]
        pools = {p.id for p in self.pools}
        if len(ids) != len(set(ids)) or len(pools) != len(self.pools):
            raise ValueError("deployment/pool IDs must be unique")
        enabled = [d for d in self.deployments if d.enabled]
        if len(enabled) > 2 or len({d.order for d in enabled}) != len(enabled):
            raise ValueError("at most two distinct ordered candidates are allowed")
        for d in self.deployments:
            if not re.fullmatch(r"[A-Z][A-Z0-9_]{2,80}", d.credential_env):
                raise ValueError("credential must be an environment variable name")
            u = urlparse(d.api_base)
            if u.hostname not in self.allowed_hosts or u.username or u.password or u.query or u.fragment:
                raise ValueError("endpoint must be a reviewed host without credentials/query/fragment")
            if self.profile == "production" and (u.scheme != "https" or u.port not in (None, 443)):
                raise ValueError("production providers require HTTPS on port 443")
            if self.profile == "mock" and u.hostname not in ("127.0.0.1", "localhost", "mock-upstream"):
                raise ValueError("mock mode must use loopback or mock-upstream only")
            if self.profile == "production":
                approved = {"groq": "api.groq.com", "gemini": "generativelanguage.googleapis.com"}
                if approved.get(d.provider) != u.hostname:
                    raise ValueError("production supports only reviewed direct Groq/Gemini adapters")
            if self.profile == "production" and d.adapter == "native" and d.capability.streaming:
                raise ValueError("native production streaming is not certified; select the reviewed SDK bridge")
            if d.adapter == "openai_sdk" and self.profile == "production":
                expected = {"groq":"https://api.groq.com/openai/v1", "gemini":"https://generativelanguage.googleapis.com/v1beta/openai"}
                if d.api_base.rstrip("/") != expected.get(d.provider):
                    raise ValueError("SDK bridge requires the reviewed exact OpenAI-compatible endpoint")
            if not d.model.startswith(d.provider + "/"):
                raise ValueError("native provider prefix must match provider")
            if not d.quota_pools or not set(d.quota_pools) <= pools:
                raise ValueError("each deployment requires registered quota pools")
            for pool in (p for p in self.pools if p.id in d.quota_pools):
                if self.profile=="production" and pool.provider != d.provider:
                    raise ValueError("deployment and quota pool providers must match")
                if d.model not in pool.model_scope and d.model.split("/",1)[1] not in pool.model_scope:
                    raise ValueError("quota pool model scope must include its deployment")
            if d.enabled and self.exclusion(d) is not None:
                raise ValueError(f"enabled deployment {d.id}: {self.exclusion(d)}")
            if d.enabled and self.limits.default_output_tokens > d.capability.max_output_tokens:
                raise ValueError("default output exceeds candidate capability")
        return self

    def exclusion(self, d: Deployment, now: datetime | None = None) -> str | None:
        if not d.enabled:
            return "disabled"
        now = now or datetime.now(timezone.utc)
        e = d.eligibility
        if e.status != "confirmed_free" or not e.billing_hard_limit or not e.evidence.strip():
            return "free_eligibility_unconfirmed"
        if e.checked_at is None or e.review_by is None:
            return "eligibility_review_missing"
        if e.checked_at.tzinfo is None or e.review_by.tzinfo is None or not e.checked_at <= now < e.review_by:
            return "eligibility_review_expired_or_invalid"
        if self.privacy_scope not in e.privacy_scopes:
            return "privacy_scope_unapproved"
        return None

    @property
    def revision(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()

    def native_config(self):
        models = []
        for d in self.deployments:
            if self.exclusion(d) is not None:
                continue
            models.append({"model_name": self.alias, "litellm_params": {
                "model": deployment_native_model(d), "api_base": d.api_base,
                "api_key": "os.environ/" + d.credential_env,
                "order": d.order, "max_retries": 0,
                "timeout": self.limits.first_event_timeout,
                "stream_timeout": self.limits.stream_idle_timeout,
            }, "model_info": {"id": d.id, "allowed_fails": 0}})
        return {"model_list": models,
            "router_settings": {"routing_strategy": "simple-shuffle", "num_retries": 0,
                "max_fallbacks": 1, "timeout": self.limits.total_timeout,
                "allowed_fails": 0, "cooldown_time": self.limits.cooldown_seconds,
                "fallbacks": [], "context_window_fallbacks": [], "content_policy_fallbacks": [],
                "retry_policy": {"AuthenticationErrorRetries": 0, "BadRequestErrorRetries": 0,
                    "TimeoutErrorRetries": 0, "RateLimitErrorRetries": 0, "InternalServerErrorRetries": 0,
                    "ContentPolicyViolationErrorRetries": 0}},
            "litellm_settings": {"callbacks": ["gateway.hooks.guard"],
                **({"custom_provider_map":[{"provider":"gateway_openai_sdk","custom_handler":"gateway.stream_bridge.bridge"}]}
                   if any(d.adapter == "openai_sdk" for d in self.deployments) else {}),
                "drop_params": False,
                "set_verbose": False, "suppress_debug_info": True, "turn_off_message_logging": True,
                "telemetry": False, "cache": False, "store_audit_logs": False},
            "general_settings": {"master_key": "os.environ/LITELLM_MASTER_KEY",
                "database_url": "os.environ/DATABASE_URL", "store_model_in_db": False,
                "disable_spend_logs": True, "disable_spend_updates": True, "disable_error_logs": True,
                "allow_requests_on_db_unavailable": False, "enforce_fallback_model_access": True,
                "background_health_checks": False}}


def deployment_native_model(deployment: Deployment) -> str:
    """Map an explicit adapter choice, retaining provider identity in its suffix."""
    return "gateway_openai_sdk/" + deployment.model if deployment.adapter == "openai_sdk" else deployment.model


def load_policy(path: str | Path) -> Policy:
    return Policy.model_validate(yaml.safe_load(Path(path).read_text()))


def main():
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["validate", "compile"])
    p.add_argument("--config", required=True)
    p.add_argument("--output")
    args = p.parse_args()
    policy = load_policy(args.config)
    if args.action == "compile":
        if not args.output:
            p.error("compile requires --output")
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.safe_dump(policy.native_config(), sort_keys=False))
    print(json.dumps({"valid": True, "config_revision": policy.revision,
        "profile": policy.profile, "enabled_deployments": [d.id for d in policy.deployments if d.enabled]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
