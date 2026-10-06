"""Official extension hooks enforcing eligibility and diagnostics, not a router or SSE parser."""
from __future__ import annotations
import asyncio
import anyio
import hashlib
import os
import time
from datetime import datetime, timezone, timedelta
from email.utils import parsedate_to_datetime

from fastapi import HTTPException
from litellm.integrations.custom_logger import CustomLogger

from .context import current_request
from .contract import check_capability
from .errors import GatewayError

STREAM_AUDIT_CLEANUP_TIMEOUT_SECONDS = 5.0
STREAM_CLOSE_TIMEOUT_SECONDS = 0.25


def safe_http(exc):
    if isinstance(exc, GatewayError):
        return HTTPException(status_code=exc.status, detail=exc.code)
    if isinstance(exc, HTTPException) and exc.status_code in (400,401,403,404,429,503,504):
        return HTTPException(status_code=exc.status_code, detail="request_failed")
    try:
        status = int(getattr(exc,"status_code",None) or getattr(exc,"code",0))
    except (ValueError,TypeError):
        status = 0
    if status in (400,401,403,404,429,503,504):
        return HTTPException(status_code=status,detail="request_failed")
    return HTTPException(status_code=502, detail="upstream_error")


class GatewayGuard(CustomLogger):
    def __init__(self):
        super().__init__()
        self.policy = None
        self.state = None

    def configure(self, policy, state):
        self.policy, self.state = policy, state
        self.deployments = {d.id: d for d in policy.deployments}
        self.state.pool_configs = {p.id:p for p in policy.pools}
        self.state.observation_max_age_seconds = policy.limits.observation_max_age_seconds
        return self

    def context(self):
        ctx = current_request.get()
        if self.policy is None or self.state is None or ctx is None:
            raise GatewayError(503, "policy_unavailable", "Required gateway policy context is unavailable.")
        return ctx

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        try:
            ctx = self.context()
            if "admin" in str(getattr(user_api_key_dict, "user_role", "")).lower():
                raise GatewayError(403, "access_denied", "Administrator credentials cannot be used for inference.")
            metadata = getattr(user_api_key_dict, "metadata", {}) or {}
            grant = metadata.get("gateway", {})
            ctx.allowed_deployments = frozenset(grant.get("deployment_ids", []))
            ctx.privacy_scope = grant.get("privacy_scope", "")
            # Ingress creates a one-way fingerprint before native authentication.
            # Do not substitute key_alias: distinct rotated keys can share that alias.
            if ctx.privacy_scope != self.policy.privacy_scope or not ctx.allowed_deployments:
                raise GatewayError(403, "access_denied", "This restricted key lacks an approved gateway grant.")
            if self.policy.alias not in (getattr(user_api_key_dict, "models", []) or []):
                raise GatewayError(403, "access_denied", "This restricted key cannot use this logical alias.")
            await self.state.ready()
            candidates = [d for d in self.deployments.values() if self._eligible(d, ctx)]
            for d in sorted(self.deployments.values(), key=lambda item: item.order):
                await self._candidate_decision(d, ctx, "request_validation")
            if not candidates:
                raise GatewayError(503, "no_eligible_free_model", "No approved free candidate is available.")
            if not any([await self.state.check_pools(d.quota_pools) for d in candidates]):
                raise GatewayError(503,"no_eligible_free_model","All approved candidates are cooling down or unavailable.")
            # Contract is the intersection, checked before the primary and again per attempt.
            for d in candidates:
                try:
                    check_capability(ctx.body, d)
                except GatewayError:
                    await self._candidate_decision(d, ctx, "capability_validation", "unsupported_capability")
                    raise
            data["num_retries"] = 0
            data["max_retries"] = 0
            data["drop_params"] = False
            return data
        except GatewayError as exc:
            raise safe_http(exc) from None

    def _eligibility_reason(self, d, ctx):
        if d is None:
            return "unregistered_target"
        if d.id not in ctx.allowed_deployments:
            return "deployment_not_authorized"
        if ctx.privacy_scope != self.policy.privacy_scope:
            return "privacy_scope_mismatch"
        return self.policy.exclusion(d)

    def _eligible(self, d, ctx):
        return self._eligibility_reason(d, ctx) is None

    async def _candidate_decision(self, d, ctx, phase, reason=None):
        # A historical policy decision, not a promise that native routing selects it.
        reason = reason or self._eligibility_reason(d, ctx)
        if reason is None and not await self.state.check_pools(d.quota_pools):
            reason = "quota_or_cooldown_unavailable"
        snapshot = (phase, ctx.attempts, d.id, reason)
        if snapshot not in ctx.decision_snapshots:
            await self._event("candidate_decision", ctx, deployment_id=d.id,
                pool_ids=d.quota_pools, candidate_order=d.order, phase=phase,
                decision="excluded" if reason else "eligible", exclusion_reason=reason,
                actual_model=d.model, provider=d.provider)
            ctx.decision_snapshots.add(snapshot)
        return reason

    async def async_filter_listed_models(self, user_api_key_dict, model_names):
        grant = (getattr(user_api_key_dict, "metadata", {}) or {}).get("gateway", {})
        if "admin" in str(getattr(user_api_key_dict, "user_role", "")).lower():
            return []
        if grant.get("privacy_scope") != self.policy.privacy_scope:
            return []
        return [m for m in model_names if m == self.policy.alias]

    async def async_filter_deployments(self, model, healthy_deployments, messages, request_kwargs=None, parent_otel_span=None):
        ctx = self.context()
        await self.state.ready()
        result = []
        native_ids = {str(dep.get("model_info", {}).get("id")) for dep in healthy_deployments}
        for d in sorted(self.deployments.values(), key=lambda item: item.order):
            reason = self._eligibility_reason(d, ctx)
            if reason is None and not await self.state.check_pools(d.quota_pools):
                reason = "quota_or_cooldown_unavailable"
            if reason is None and d.id not in native_ids:
                # The official hook does not expose why native routing omitted it.
                reason = "native_candidate_unavailable"
            await self._candidate_decision(d, ctx, "native_filter", reason)
        for dep in healthy_deployments:
            d = self.deployments.get(str(dep.get("model_info", {}).get("id")))
            if self._eligible(d, ctx) and await self.state.check_pools(d.quota_pools):
                try:
                    check_capability(ctx.body, d)
                except GatewayError:
                    await self._candidate_decision(d, ctx, "capability_validation", "unsupported_capability")
                    raise
                result.append(dep)
        return result

    def _deployment(self, kwargs):
        # Native router adds model_info to actual SDK kwargs for each selected deployment.
        model_info = kwargs.get("model_info", {}) or kwargs.get("litellm_params", {}).get("model_info", {})
        return self.deployments.get(str(model_info.get("id")))

    async def async_pre_call_deployment_hook(self, kwargs, call_type):
        ctx = self.context()
        if time.monotonic()-ctx.started >= self.policy.limits.total_timeout:
            ctx.stop_attempts = True
            ctx.error_code = "deadline_exceeded"
            raise GatewayError(504, "deadline_exceeded", "The total request deadline was reached.")
        if ctx.delivered or ctx.stop_attempts:
            ctx.stop_attempts = True
            raise GatewayError(502, "attempt_blocked", "Further upstream attempts are forbidden.")
        d = self._deployment(kwargs)
        if not self._eligible(d, ctx):
            raise GatewayError(503, "no_eligible_free_model", "Selected target is not approved.")
        actual_model = kwargs.get("model")
        from .config import deployment_native_model
        expected_model = deployment_native_model(d)
        if actual_model not in {expected_model, expected_model.split("/",1)[1]} or str(kwargs.get("api_base","")).rstrip("/") != d.api_base.rstrip("/"):
            raise GatewayError(403, "target_override_blocked", "Actual upstream target differs from approved deployment.")
        if self.policy.profile == "production" and kwargs.get("api_key") != os.environ.get(d.credential_env):
            raise GatewayError(403, "credential_override_blocked", "Actual credential differs from approved reference.")
        check_capability(ctx.body, d)
        if ctx.attempts >= self.policy.limits.max_attempts or d.id in ctx.attempted_ids:
            ctx.stop_attempts = True
            raise GatewayError(502, "attempt_budget_exhausted", "Upstream attempt budget exhausted.")
        await self.state.ready()
        if not await self.state.acquire(d.quota_pools):
            raise GatewayError(503, "no_eligible_free_model", "Selected quota scope cannot be probed yet.")
        if ctx.current_pools:
            await self._release(ctx)
            await self._event("attempt_finished",ctx,status="failed",error_code="upstream_stream_failed_before_first_event",upstream_execution_unknown=True)
            ctx.current_pools = ()
        ctx.attempts += 1
        ctx.attempted_ids.add(d.id)
        ctx.current_deployment = d.id
        ctx.current_pools = tuple(d.quota_pools)
        ctx.attempt_started = time.monotonic()
        ctx.attempt_first_event_ms = None
        ctx.retry_after_until = None
        ctx.genuine_terminal = False
        ctx.attempt_finalized = False
        ctx.usage_source = "unknown"
        ctx.observed_usage = None
        ctx.observed_headers = {}
        ctx.persisted_observation_headers = {}
        try:
            await self._event("attempt_started", ctx, status="running", retry_reason=ctx.retry_reason)
        except BaseException:
            await self._release(ctx)
            raise
        kwargs["max_retries"] = 0
        kwargs["num_retries"] = 0
        kwargs["drop_params"] = False
        return kwargs

    def log_post_api_call(self, kwargs, response_obj, start_time, end_time):
        # Official native hook sees the upstream JSON before LiteLLM normalization.
        # Extract only numeric usage/header fields; never retain or log the raw body.
        ctx=current_request.get()
        if ctx is None or ctx.current_deployment is None:
            return
        deployment=self._deployment(kwargs)
        if deployment is None or deployment.id != ctx.current_deployment:
            return
        if deployment.adapter == "openai_sdk":
            # SDK bridge captures typed upstream metadata before normalization.
            return
        from .metadata import extract_response_metadata
        metadata=extract_response_metadata(kwargs.get("original_response"),kwargs.get("response_headers"),streaming=ctx.streaming)
        ctx.usage_source=metadata.usage_source
        ctx.observed_usage=metadata.usage
        ctx.observed_headers.update(metadata.headers)

    async def _observe_quota(self,ctx,headers):
        if not headers or headers == ctx.persisted_observation_headers:
            return
        from .quota import observe_headers, MOCK_REQUESTS_MINUTE_MAPPING
        observations=[]
        for pool_id in ctx.current_pools:
            pool=self.state.pool_configs[pool_id]
            mapping=MOCK_REQUESTS_MINUTE_MAPPING if self.policy.profile=="mock" and pool.provider=="mock" and pool.dimension=="requests" and pool.window=="minute" else None
            deployment=self.deployments[ctx.current_deployment]
            provider=pool.provider if self.policy.profile=="mock" else deployment.provider
            observation=observe_headers(pool,headers,datetime.now(timezone.utc),provider=provider,mapping=mapping,ttl=timedelta(seconds=self.policy.limits.observation_max_age_seconds))
            if observation is not None:
                observations.append(observation.to_dict())
        if observations:
            try:
                await self.state.save_observations(observations)
            except BaseException:
                ctx.audit_failed=ctx.stop_attempts=True
                self.state.healthy=False
                raise
        ctx.persisted_observation_headers = dict(headers)

    async def _release(self, ctx, **kwargs):
        try:
            await self.state.release(ctx.current_pools, **kwargs)
        except asyncio.CancelledError:
            ctx.stop_attempts = True
            raise
        except BaseException:
            ctx.audit_failed = ctx.stop_attempts = True
            ctx.terminal = "failed"
            self.state.healthy = False
            raise

    async def _event(self, event, ctx, **fields):
        try:
            timing = {"elapsed_ms": round((time.monotonic() - ctx.started) * 1000)}
            if event == "attempt_finished":
                timing.update(duration_ms=round((time.monotonic() - ctx.attempt_started) * 1000) if ctx.attempt_started is not None else None,
                    first_event_ms=ctx.first_event_ms, attempt_first_event_ms=ctx.attempt_first_event_ms)
            deployment = self.deployments.get(ctx.current_deployment)
            await self.state.record({"event": event, "request_id": ctx.request_id,
            "config_revision": ctx.revision, "key_id": ctx.key_id, "alias": ctx.alias,
            "attempt": ctx.attempts, "deployment_id": ctx.current_deployment,
            "pool_ids": list(ctx.current_pools),
            "actual_model": deployment.model if deployment else None,
            "provider": deployment.provider if deployment else None,
            "usage_source": ctx.usage_source, **(ctx.observed_usage or {}), **timing, **fields})
        except asyncio.CancelledError:
            ctx.stop_attempts = True
            raise
        except BaseException:
            ctx.audit_failed = ctx.stop_attempts = True
            ctx.terminal = "failed"
            self.state.healthy = False
            raise
        if event == "attempt_finished":
            ctx.attempt_finalized = True

    async def async_post_call_failure_deployment_hook(self, request_data, exception, call_type, fallback_depth=None):
        ctx = self.context()
        status = getattr(exception, "status_code", None)
        cooldown = self.policy.limits.cooldown_seconds if status == 429 else 0
        from .metadata import filter_response_headers
        headers = {**ctx.observed_headers,
            **filter_response_headers(getattr(getattr(exception, "response", None), "headers", {}) or {})}
        raw = headers.get("retry-after")
        cooldown_source = "local_cooldown_policy"
        if status == 429 and raw:
            try:
                seconds = int(raw)
            except (ValueError, TypeError):
                try:
                    seconds = int((parsedate_to_datetime(raw)-datetime.now(timezone.utc)).total_seconds())
                except Exception:
                    seconds = None
            if seconds is not None:
                cooldown = max(1, min(86400, seconds))
                # Only forward a bounded, future, directly validated wait. The
                # local capped policy is not a precise provider reset promise.
                if 0 <= seconds <= 86400:
                    ctx.retry_after_until = time.monotonic() + cooldown
                    cooldown_source = "upstream_retry_after"
        await self._observe_quota(ctx,headers)
        if status in (401,403):
            ctx.upstream_auth_failed = True
        if status in (401,403) or status == 400:
            ctx.stop_attempts = True
        await self._release(ctx, status="disabled" if status in (401,403) else "cooldown" if status == 429 else "unknown", cooldown=cooldown)
        ctx.retry_reason = "upstream_auth_error" if status in (401,403) else "rate_limited" if status == 429 else "upstream_error"
        await self._event("attempt_finished", ctx, status="failed", error_code=ctx.retry_reason,
            upstream_status=status if type(status) is int and 100 <= status <= 599 else None,
            cooldown_seconds=cooldown, cooldown_source=cooldown_source if cooldown else None, upstream_execution_unknown=True)
        ctx.current_pools = ()

    async def async_post_call_success_deployment_hook(self, request_data, response, call_type):
        ctx = self.context()
        reason = response.choices[0].finish_reason if getattr(response,"choices",None) else None
        message = response.choices[0].message if getattr(response,"choices",None) else None
        refusal = getattr(message,"refusal",None) or (getattr(message,"provider_specific_fields",{}) or {}).get("refusal")
        if refusal:
            message.refusal = refusal
        ctx.terminal = "refused" if refusal else "complete" if reason == "stop" else "truncated" if reason == "length" else "refused" if reason == "content_filter" else "failed"
        from .metadata import extract_response_headers
        from litellm.types.utils import Usage
        if hasattr(response,"usage"):
            response.usage=Usage(**ctx.observed_usage) if ctx.usage_source=="upstream_observed" and ctx.observed_usage is not None else None
        ctx.observed_headers.update(extract_response_headers(response))
        await self._observe_quota(ctx,ctx.observed_headers)
        await self._release(ctx, status="available")
        await self._event("attempt_finished", ctx, status=ctx.terminal)
        ctx.current_pools = ()
        return response

    async def async_post_call_streaming_deployment_hook(self, request_data, response_chunk, call_type):
        ctx = current_request.get()
        if ctx:
            deployment=self._deployment(request_data)
            if deployment is None or deployment.id != ctx.current_deployment:
                return response_chunk
            from .metadata import extract_response_metadata,extract_response_headers
            metadata=extract_response_metadata(None,request_data.get("response_headers"),streaming=True)
            metadata.headers.update(extract_response_headers(response_chunk))
            if metadata.headers and metadata.headers != ctx.observed_headers:
                ctx.observed_headers.update(metadata.headers)
                await self._observe_quota(ctx,ctx.observed_headers)
            if deployment.adapter == "openai_sdk" and ctx.observed_headers:
                # Captured from the official SDK response before normalized
                # chunks; persist a changed header set at most once per attempt.
                await self._observe_quota(ctx,ctx.observed_headers)
            for choice in getattr(response_chunk, "choices", []) or []:
                if choice.finish_reason is not None:
                    ctx.genuine_terminal = True
        return response_chunk

    async def async_post_call_streaming_iterator_hook(self, user_api_key_dict, response, request_data):
        ctx = self.context()
        terminal = False
        try:
            iterator = response.__aiter__()
            while True:
                try:
                    item = await asyncio.wait_for(iterator.__anext__(), timeout=self.policy.limits.stream_idle_timeout if ctx.delivered else self.policy.limits.first_event_timeout)
                except StopAsyncIteration:
                    break
                if ctx.audit_failed:
                    raise GatewayError(503,"audit_unavailable","Required state recording failed.")
                for choice in getattr(item, "choices", []) or []:
                    if getattr(choice.delta,"content",None):
                        ctx.saw_text = True
                    refusal = getattr(choice.delta,"refusal",None) or (getattr(choice.delta,"provider_specific_fields",{}) or {}).get("refusal")
                    if refusal:
                        choice.delta.refusal = refusal
                        ctx.terminal = "refused"
                    reason = choice.finish_reason
                    if reason is not None:
                        if not ctx.genuine_terminal:
                            raise GatewayError(502, "stream_interrupted", "Upstream ended without a verified terminal event.")
                        if reason not in {"stop", "length", "content_filter"}:
                            raise GatewayError(502, "unsupported_stream_terminal", "Upstream returned an unsupported terminal reason.")
                        deployment = self.deployments.get(ctx.current_deployment)
                        if reason == "stop" and not ctx.saw_text and ctx.terminal != "refused" and (deployment is None or deployment.adapter != "openai_sdk"):
                            raise GatewayError(502,"unsupported_empty_stream","Empty or unsupported refusal streams cannot be certified as complete.")
                        terminal = True
                        ctx.terminal = "refused" if ctx.terminal == "refused" else "complete" if reason == "stop" else "truncated" if reason == "length" else "refused" if reason == "content_filter" else "failed"
                if hasattr(item, "usage"):
                    item.usage = None
                # Any typed chunk, including role/empty, locks further upstream attempts.
                ctx.delivered = True
                yield item
            if not terminal:
                raise GatewayError(502, "stream_interrupted", "Upstream ended without a verified terminal event.")
            await self._release(ctx, status="available")
            await self._event("attempt_finished", ctx, status=ctx.terminal)
            ctx.current_pools = ()
        except asyncio.CancelledError:
            ctx.stop_attempts = True
            if not ctx.response_finished:
                ctx.terminal = "cancelled"
            raise
        except Exception:
            ctx.stop_attempts = True
            ctx.terminal = "stream_interrupted"
            raise HTTPException(status_code=502, detail="stream_interrupted") from None
        finally:
            # Starlette cancels streams via an AnyIO cancel scope. Shield bounded
            # metadata cleanup so cancelling a client does not poison gateway health.
            try:
                with anyio.fail_after(STREAM_AUDIT_CLEANUP_TIMEOUT_SECONDS, shield=True):
                    if ctx.current_pools:
                        await self._release(ctx)
                        await self._event("attempt_finished", ctx, status=ctx.terminal, upstream_execution_unknown=not terminal)
                        ctx.current_pools = ()
            except Exception:
                ctx.audit_failed = ctx.stop_attempts = True
                self.state.healthy = False
                import logging
                logging.getLogger("gateway.audit").error("stream_audit_finalize_failed")
                raise
            finally:
                # Closing a provider iterator is best effort too. A broken
                # close cannot retain the local request slot after cancellation.
                closer = getattr(response, "aclose", None)
                if closer:
                    try:
                        with anyio.fail_after(STREAM_CLOSE_TIMEOUT_SECONDS, shield=True):
                            await closer()
                    except Exception:
                        import logging
                        logging.getLogger("gateway.audit").warning("upstream_close_unconfirmed")

    async def async_post_call_failure_hook(self, request_data, original_exception, user_api_key_dict, traceback_str=None):
        return safe_http(original_exception)


guard = GatewayGuard()
