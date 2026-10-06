"""ASGI boundary around the native proxy. Does not parse or generate provider SSE."""
from __future__ import annotations
import asyncio
import anyio
import json
import math
import hashlib
import os
import secrets
import time
from urllib.parse import parse_qs
from uuid import UUID
from pathlib import Path

from .context import RequestContext, current_request
from .contract import validate_request
from .errors import GatewayError, invalid

CONSUMER = {("POST", "/v1/chat/completions"), ("GET", "/v1/models")}
ADMIN = {("POST", "/key/generate"), ("POST", "/key/delete"), ("GET", "/key/info"), ("GET", "/key/list"), ("GET", "/gateway/status"), ("GET", "/gateway/traces"), ("GET", "/gateway/resources"), ("GET", "/gateway/config"), ("GET", "/gateway/summary"), ("POST", "/gateway/drain")}
HEALTH = {("GET", "/health/liveliness"), ("GET", "/health/readiness")}
# A peer that has stopped reading must not retain a generation slot while the
# error path tries to send another body. These are cleanup budgets, not another
# upstream-generation or retry deadline.
FINAL_SEND_TIMEOUT_SECONDS = 0.25
AUDIT_CLEANUP_TIMEOUT_SECONDS = 5.0
NATIVE_SLOT_CLEANUP_TIMEOUT_SECONDS = 1.0


def reject_duplicates(pairs):
    data = {}
    for k,v in pairs:
        if k in data:
            raise ValueError("duplicate field")
        data[k] = v
    return data


def status_error(status):
    code, message = {400:("invalid_request","Unsupported request or capability."),
        401:("authentication_error","A valid restricted API key is required."),
        403:("access_denied","This identity is not authorized for the request."),
        404:("model_not_found","Unknown model or endpoint."),
        429:("rate_limited","The request is temporarily rate limited."),
        503:("no_eligible_free_model","No approved free candidate or required state is available."),
        504:("deadline_exceeded","The total request deadline was reached.")}.get(status,("upstream_error","The upstream request could not be completed."))
    return GatewayError(status if status in (400,401,403,404,429,503,504) else 502,code,message)


class GatewayIngress:
    def __init__(self, app, policy, state, state_dir, master_key, *, native_request_cleanup=None):
        self.app, self.policy, self.state = app, policy, state
        self.state_dir = Path(state_dir)
        self.master_key = master_key
        self.native_request_cleanup = native_request_cleanup
        self.active_requests = 0
        self.cleanup_task = None

    @property
    def draining(self):
        return (self.state_dir / "drain.json").exists()

    async def status(self):
        ready = True
        try:
            await self.state.ready()
        except GatewayError:
            ready = False
        retention_ready = not (self.cleanup_task is not None and self.cleanup_task.done())
        return {"ready": ready and retention_ready and not self.draining, "database_ready":ready,
            "retention_ready":retention_ready,
            "error_code":None if retention_ready else "retention_cleanup_failed",
            "draining":self.draining,"active_requests":self.active_requests,
            "config_revision":self.policy.revision,"profile":self.policy.profile}

    async def _retention(self):
        while True:
            await self.state.cleanup(self.policy.limits.retention_days)
            await asyncio.sleep(3600)

    async def _json(self, send, status, data, request_id, extra_headers=None):
        body = json.dumps(data, ensure_ascii=False).encode()
        await send({"type":"http.response.start","status":status,"headers":[
            (b"content-type",b"application/json"),(b"x-request-id",request_id.encode()),
            (b"x-config-revision",self.policy.revision.encode()),(b"cache-control",b"no-store"),
            *((extra_headers or []))]})
        await send({"type":"http.response.body","body":body})

    async def _close_downstream(self, send):
        """Best-effort EOF only; never wait indefinitely behind backpressure."""
        try:
            with anyio.fail_after(FINAL_SEND_TIMEOUT_SECONDS, shield=True):
                await send({"type":"http.response.body","body":b"","more_body":False})
        except Exception:
            # Returning an incomplete ASGI response lets the server close the
            # connection. No fake SSE terminal is emitted, and this peer failure
            # does not poison otherwise healthy audit/database state.
            import logging
            logging.getLogger("gateway.audit").warning("downstream_close_unconfirmed")

    async def _error_response(self, send, error, request_id):
        """An error raised outside the request timeout still has a send budget."""
        try:
            with anyio.fail_after(FINAL_SEND_TIMEOUT_SECONDS, shield=True):
                await self._json(send,error.status,error.as_dict(),request_id)
        except Exception:
            import logging
            logging.getLogger("gateway.audit").warning("downstream_error_unconfirmed")

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            async def lifespan_send(msg):
                if msg["type"] == "lifespan.startup.complete":
                    if hasattr(self.state,"initialize"):
                        await self.state.initialize()
                    await self.state.cleanup(self.policy.limits.retention_days)
                    self.cleanup_task = asyncio.create_task(self._retention())
                if msg["type"] == "lifespan.shutdown.complete" and self.cleanup_task:
                    self.cleanup_task.cancel()
                await send(msg)
            return await self.app(scope, receive, lifespan_send)
        if scope["type"] != "http":
            return
        ctx = RequestContext(revision=self.policy.revision)
        token = current_request.set(ctx)
        acquired = False
        native_entered = False
        is_generation=(scope.get("method"),scope.get("path")) == ("POST","/v1/chat/completions")
        downstream_started = False
        downstream_finished = False
        error_sent = False
        status = 500
        try:
            route = (scope["method"],scope["path"])
            raw_headers=scope.get("headers",[])
            for singleton in (b"authorization",b"content-type",b"content-length"):
                if sum(k.lower()==singleton for k,_ in raw_headers)>1:
                    raise invalid("Duplicate security or framing headers are forbidden.")
            headers = {k.lower():v for k,v in raw_headers}
            supplied = headers.get(b"authorization",b"").decode("latin1")
            if supplied.startswith("Bearer "):
                ctx.key_id=hashlib.sha256(supplied[7:].encode()).hexdigest()[:16]
            is_admin = bool(self.master_key) and secrets.compare_digest(supplied,"Bearer "+self.master_key)
            if route not in CONSUMER | ADMIN | HEALTH:
                raise GatewayError(404,"unsupported_endpoint","This endpoint is not exposed.")
            if scope.get("query_string") and route in CONSUMER:
                raise invalid("Consumer query overrides are not supported.")
            if route in ADMIN and not is_admin:
                raise GatewayError(403,"access_denied","Administrator access is required.")
            if route in CONSUMER and is_admin:
                raise GatewayError(403,"access_denied","Use a separate restricted consumer key.")
            if route == ("GET","/health/liveliness"):
                return await self._json(send,200,{"alive":True},ctx.request_id)
            if route in {("GET","/health/readiness"),("GET","/gateway/status")}:
                result = await self.status()
                return await self._json(send,200 if route[1].endswith("status") or result["ready"] else 503,result,ctx.request_id)
            if route == ("POST","/gateway/drain"):
                self.state_dir.mkdir(parents=True,exist_ok=True)
                (self.state_dir/"drain.json").write_text('{"draining":true}')
                return await self._json(send,200,await self.status(),ctx.request_id)
            if route == ("GET","/gateway/resources"):
                from .diagnostics import resources_view
                return await self._json(send,200,await resources_view(self.policy,self.state),ctx.request_id)
            if route == ("GET","/gateway/config"):
                from .diagnostics import config_view
                return await self._json(send,200,config_view(self.policy),ctx.request_id)
            if route == ("GET","/gateway/summary"):
                try:
                    query = parse_qs(scope.get("query_string",b"").decode("ascii"),strict_parsing=True,keep_blank_values=True)
                    if set(query)-{"days"} or any(len(values)!=1 for values in query.values()):
                        raise ValueError
                    days = int(query.get("days",[str(self.policy.limits.retention_days)])[0])
                    if not 1 <= days <= self.policy.limits.retention_days:
                        raise ValueError
                except (ValueError,UnicodeError):
                    raise invalid("Summary days must be within the retained window.") from None
                return await self._json(send,200,await self.state.summary(days),ctx.request_id)
            if route == ("GET","/gateway/traces"):
                from .trace_query import parse_trace_query
                query = parse_trace_query(scope.get("query_string", b""))
                result = await self.state.trace_page(**query)
                result["retention_days"] = self.policy.limits.retention_days
                result["config_revision"] = self.policy.revision
                return await self._json(send,200,result,ctx.request_id)
            if route in CONSUMER:
                async with asyncio.timeout(max(0.001,self.policy.limits.total_timeout-(time.monotonic()-ctx.started))):
                    await self.state.ready()
                if self.cleanup_task and self.cleanup_task.done():
                    raise GatewayError(503,"retention_cleanup_failed","Metadata cleanup needs administrator attention.")
            if route == ("POST","/v1/chat/completions"):
                if self.draining:
                    raise GatewayError(503,"maintenance","The gateway is draining for maintenance.")
                if self.active_requests >= self.policy.limits.concurrency:
                    raise GatewayError(429,"rate_limited","Local request concurrency limit reached.")
                self.active_requests += 1
                acquired = True
            body = bytearray()
            if scope["method"] == "POST":
                if headers.get(b"content-type",b"").split(b";")[0].strip() != b"application/json":
                    raise invalid("Content-Type must be application/json.")
                while True:
                    async with asyncio.timeout(min(self.policy.limits.first_event_timeout,max(0.001,self.policy.limits.total_timeout-(time.monotonic()-ctx.started)))):
                        message = await receive()
                    if message["type"] == "http.disconnect":
                        ctx.terminal = "cancelled"
                        return
                    body.extend(message.get("body",b""))
                    if len(body) > self.policy.limits.max_body_bytes:
                        raise invalid("Request body exceeds 1 MiB limit.")
                    if not message.get("more_body",False):
                        break
                try:
                    data = json.loads(body, object_pairs_hook=reject_duplicates)
                except (ValueError,UnicodeError,RecursionError):
                    raise invalid("Body must be valid JSON without duplicate fields.") from None
                if route in CONSUMER:
                    ctx.body = validate_request(data,self.policy)
                    ctx.streaming = ctx.body.get("stream",False)
                    body = bytearray(json.dumps(ctx.body,ensure_ascii=False).encode())
                elif route == ("POST","/key/generate"):
                    self._check_key_request(data)
            sent_body = False
            async def native_receive():
                nonlocal sent_body
                if not sent_body:
                    sent_body = True
                    return {"type":"http.request","body":bytes(body),"more_body":False}
                event = await receive()
                if event["type"] == "http.disconnect":
                    ctx.stop_attempts = True
                    # ASGI may report disconnect after a fully sent response. It
                    # must not rewrite a verified normal terminal as cancelled.
                    if not ctx.response_finished:
                        ctx.terminal = "cancelled"
                return event

            async def native_send(message):
                nonlocal downstream_started,downstream_finished,error_sent,status
                if message["type"] == "http.response.start":
                    status = message["status"]
                    if is_generation and time.monotonic()-ctx.started >= self.policy.limits.total_timeout:
                        # A native synchronous section can delay asyncio's timer
                        # callback. Do not publish a stale 200/503 after expiry.
                        status = 504
                        ctx.stop_attempts = True
                        ctx.error_code = "deadline_exceeded"
                        ctx.terminal = "failed"
                    if route in CONSUMER and status != 504 and ctx.attempts and (ctx.audit_failed or (not ctx.streaming and not ctx.attempt_finalized)):
                        status = 503
                    if status >= 400:
                        error_sent = True
                        downstream_started = downstream_finished = True
                        err = GatewayError(502,"upstream_auth_error","An upstream credential requires administrator review.") if ctx.upstream_auth_failed and status != 504 else status_error(status)
                        ctx.error_code = err.code
                        wait = math.ceil(ctx.retry_after_until - time.monotonic()) if ctx.retry_after_until is not None else 0
                        extra_headers = [(b"retry-after", str(wait).encode())] if err.status == 429 and wait > 0 else None
                        return await self._json(send,err.status,err.as_dict(),ctx.request_id,extra_headers=extra_headers)
                    clean = [(k,v) for k,v in message.get("headers",[]) if k.lower() in (b"content-type",b"content-length",b"cache-control")]
                    clean.extend([(b"x-request-id",ctx.request_id.encode()),(b"x-config-revision",ctx.revision.encode()),(b"x-usage-source",ctx.usage_source.encode())])
                    if ctx.current_deployment and not ctx.streaming:
                        clean.append((b"x-actual-deployment",ctx.current_deployment.encode()))
                    message = {**message,"headers":clean}
                    downstream_started = ctx.response_started = True
                elif message["type"] == "http.response.body":
                    if error_sent:
                        return
                    if ctx.streaming and message.get("body"):
                        ctx.delivered = True
                await send(message)
                if message["type"] == "http.response.body":
                    if ctx.streaming and message.get("body") and ctx.first_event_ms is None:
                        # First event latency means delivery to ASGI send succeeded.
                        # It does not assert that the remote application consumed it.
                        ctx.first_event_ms = round((time.monotonic() - ctx.started) * 1000)
                        ctx.attempt_first_event_ms = round((time.monotonic() - ctx.attempt_started) * 1000) if ctx.attempt_started is not None else None
                    # Mark completion only after the final send actually succeeds.
                    downstream_finished = not message.get("more_body",False)
                    ctx.response_finished = downstream_finished

            # Native request parsing sees only these headers; provider override headers never reach it.
            safe_headers = [(k,v) for k,v in scope.get("headers",[]) if k.lower() in (b"authorization",b"content-type",b"accept",b"user-agent")]
            safe_scope = {**scope,"headers":safe_headers}
            async with asyncio.timeout(max(0.001,self.policy.limits.total_timeout-(time.monotonic()-ctx.started))):
                native_entered = True
                await self.app(safe_scope,native_receive,native_send)
        except asyncio.CancelledError:
            ctx.stop_attempts = True
            if not ctx.response_finished:
                ctx.terminal = "cancelled"
            raise
        except TimeoutError:
            ctx.error_code = "deadline_exceeded"
            ctx.stop_attempts = True
            ctx.terminal = "stream_interrupted" if ctx.delivered else "failed"
            if not downstream_started:
                await self._error_response(send,status_error(504),ctx.request_id)
            elif not downstream_finished:
                # No fake DONE or finish reason. Client sees an incomplete stream.
                await self._close_downstream(send)
        except Exception as exc:
            ctx.stop_attempts = True
            if ctx.delivered:
                ctx.terminal = "stream_interrupted"
            err = exc if isinstance(exc,GatewayError) else status_error(502)
            ctx.error_code = err.code
            if not downstream_started:
                await self._error_response(send,err,ctx.request_id)
            elif not downstream_finished:
                await self._close_downstream(send)
        finally:
            if is_generation:
                # Native success/failure callbacks do not cover every outer
                # deadline/cancellation. The pinned lifecycle callback removes
                # only this request's native slot id and is idempotent with
                # native completion/stream cleanup. Never clear a shared cache.
                if native_entered and self.native_request_cleanup is not None:
                    try:
                        with anyio.fail_after(NATIVE_SLOT_CLEANUP_TIMEOUT_SECONDS, shield=True):
                            await self.native_request_cleanup()
                    except BaseException:
                        import logging
                        ctx.audit_failed = ctx.stop_attempts = True
                        ctx.error_code = "native_slot_cleanup_failed"
                        self.state.healthy = False
                        logging.getLogger("gateway.audit").error("native_slot_cleanup_failed")
                try:
                    with anyio.fail_after(AUDIT_CLEANUP_TIMEOUT_SECONDS, shield=True):
                        if ctx.current_pools:
                            await self.state.release(ctx.current_pools)
                            await self.state.record({"event":"attempt_finished","request_id":ctx.request_id,"config_revision":ctx.revision,
                                "key_id":ctx.key_id,"alias":ctx.alias,"attempt":ctx.attempts,"deployment_id":ctx.current_deployment,
                                "pool_ids":list(ctx.current_pools),"status":ctx.terminal,"error_code":ctx.error_code,
                                "duration_ms":round((time.monotonic()-ctx.attempt_started)*1000) if ctx.attempt_started is not None else None,
                                "first_event_ms":ctx.first_event_ms,"attempt_first_event_ms":ctx.attempt_first_event_ms,
                                "usage_source":"unknown","upstream_execution_unknown":True})
                        await self.state.record({"event":"request_finished","request_id":ctx.request_id,"config_revision":ctx.revision,"key_id":ctx.key_id,"alias":ctx.alias,"attempt":ctx.attempts,"status":ctx.terminal,"error_code":ctx.error_code,"first_event_ms":ctx.first_event_ms,"duration_ms":round((time.monotonic()-ctx.started)*1000),"usage_source":ctx.usage_source,**(ctx.observed_usage or {}),"upstream_execution_unknown":ctx.terminal in ("failed","stream_interrupted","cancelled")})
                except Exception:
                    import logging
                    self.state.healthy = False
                    logging.getLogger("gateway.audit").error("request_audit_finalize_failed")
                finally:
                    if acquired:
                        self.active_requests -= 1
            current_request.reset(token)

    def _check_key_request(self,data):
        allowed = {"key_alias","models","duration","metadata","max_parallel_requests"}
        if not isinstance(data,dict) or set(data)-allowed:
            raise invalid("Only restricted key creation fields are allowed.")
        if data.get("models") != [self.policy.alias] or not data.get("duration"):
            raise invalid("Keys require the approved alias and explicit expiry.")
        metadata = data.get("metadata",{})
        if not isinstance(metadata,dict) or set(metadata) != {"gateway"}:
            raise invalid("Metadata must contain only the explicit gateway grant.")
        grant = metadata["gateway"]
        if not isinstance(grant,dict) or set(grant) != {"privacy_scope","deployment_ids"}:
            raise invalid("Gateway grant fields must be explicit and unambiguous.")
        if not isinstance(grant.get("deployment_ids"),list) or not all(isinstance(x,str) for x in grant["deployment_ids"]):
            raise invalid("Deployment grants must be an array of registered IDs.")
        if grant.get("privacy_scope") != self.policy.privacy_scope:
            raise invalid("A reviewed non_sensitive privacy grant is required.")
        ids = set(grant.get("deployment_ids",[]))
        eligible = {d.id for d in self.policy.deployments if self.policy.exclusion(d) is None}
        if not ids or not ids <= eligible:
            raise invalid("Key deployment grants must be currently approved.")
        if type(data.get("max_parallel_requests",2)) is not int or data.get("max_parallel_requests",2) not in (1,2):
            raise invalid("Consumer key concurrency must be 1 or 2.")
