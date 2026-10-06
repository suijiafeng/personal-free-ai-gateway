"""Launch the unchanged native LiteLLM proxy under a thin ASGI safety boundary."""
from __future__ import annotations
import argparse
import importlib.metadata
import logging
import os
import threading
from pathlib import Path
from urllib.parse import urlparse

from .config import load_policy

PIN = "1.104.0"
_APP_CREATED = False
_BUILD_LOCK = threading.Lock()


class NativeLogBlock(logging.Filter):
    """Native arbitrary exception/request text is not a safe diagnostic format."""
    def filter(self,record):
        return False


def build_app(policy_path, state_dir=None):
    """Construct one native proxy per process; restart using a fresh process.

    Native shutdown retains process-global callbacks and other state. A second
    construction must fail explicitly rather than register policy hooks twice.
    """
    global _APP_CREATED
    with _BUILD_LOCK:
        if _APP_CREATED:
            raise RuntimeError("Only one gateway app is supported per process; start a fresh process.")
        result = _build_app(policy_path, state_dir)
        _APP_CREATED = True
        return result


def _build_app(policy_path, state_dir=None):
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "True"
    os.environ["LITELLM_LOG"] = "CRITICAL"
    os.environ["LITELLM_TELEMETRY"] = "False"
    os.environ["STORE_MODEL_IN_DB"] = "False"
    os.environ["LITELLM_STORE_AUDIT_LOGS"] = "False"
    os.environ["DISABLE_SCHEMA_UPDATE"] = "true"
    os.environ["LITELLM_MODE"] = "PRODUCTION"
    if importlib.metadata.version("litellm") != PIN:
        raise RuntimeError("Pinned LiteLLM version mismatch; re-run acceptance before upgrading.")
    if os.environ.get("WEB_CONCURRENCY","1") != "1" or os.environ.get("UVICORN_WORKERS","1") != "1":
        raise RuntimeError("Only one worker is supported.")
    policy = load_policy(policy_path)
    if any(d.adapter == "openai_sdk" for d in policy.deployments):
        from .stream_bridge import SDK_PIN
        if importlib.metadata.version("openai") != SDK_PIN:
            raise RuntimeError("Pinned SDK version mismatch; re-run bridge acceptance before upgrading.")
    master = os.environ.get("LITELLM_MASTER_KEY","")
    dsn = os.environ.get("DATABASE_URL","")
    salt = os.environ.get("LITELLM_SALT_KEY","")
    if not master.startswith("sk-") or len(master)<24 or not salt or master==salt or not dsn.startswith("postgresql://"):
        raise RuntimeError("Provide separate administrator/salt secrets and a PostgreSQL URL through the environment.")
    if policy.profile == "production" and urlparse(dsn).username != "gateway_runtime":
        raise RuntimeError("Production requires the dedicated gateway_runtime database role.")
    if policy.profile == "production" and not any(d.enabled for d in policy.deployments):
        logging.getLogger("gateway.audit").warning("no_approved_provider_production_unavailable")
    for d in policy.deployments:
        if d.enabled and not os.environ.get(d.credential_env):
            raise RuntimeError("Approved provider credential reference is not configured.")
    import yaml
    runtime = Path(os.environ.get("GATEWAY_RUNTIME_DIR",".runtime"))
    runtime.mkdir(parents=True,exist_ok=True)
    native_path = runtime / "litellm.generated.yaml"
    native_path.write_text(yaml.safe_dump(policy.native_config(),sort_keys=False))
    os.environ["CONFIG_FILE_PATH"] = str(native_path.resolve())
    # Native exception text can contain provider bodies. Redact at record creation,
    # including handlers installed later during native startup.
    old_factory = logging.getLogRecordFactory()
    def safe_factory(*args, **kwargs):
        record = old_factory(*args, **kwargs)
        if not record.name.startswith("gateway"):
            record.msg, record.args, record.exc_info, record.exc_text, record.stack_info = "native_event_redacted", (), None, None, None
        return record
    logging.setLogRecordFactory(safe_factory)
    from .hooks import guard
    from .state import PostgresState
    from .ingress import GatewayIngress
    state = PostgresState(dsn, expected_runtime_role="gateway_runtime" if policy.profile == "production" else None)
    guard.configure(policy,state)
    from litellm.proxy import proxy_server
    app = proxy_server.app
    state.native_ready_check = lambda: proxy_server.prisma_client is not None and proxy_server.prisma_client.db.is_connected()
    # The ingress has its own safe metadata diagnostics. Never ship arbitrary upstream logs.
    for name in list(logging.root.manager.loggerDict):
        if not name.startswith("gateway"):
            logger = logging.getLogger(name)
            logger.addFilter(NativeLogBlock())
            for handler in logger.handlers:
                handler.addFilter(NativeLogBlock())
    async def release_native_request_slot():
        # Version-bound lifecycle integration, not a generic plugin contract.
        # The native method owns its ContextVar stash, slot id and release lock.
        # Empty auth contains no key; this exact implementation uses the stash
        # alone, and never reconstructs identity from a client-provided value.
        from litellm.proxy._types import UserAPIKeyAuth
        from litellm.proxy.hooks.parallel_request_limiter_v3 import _PROXY_MaxParallelRequestsHandler_v3
        limiter = proxy_server.proxy_logging_obj.get_proxy_hook("parallel_request_limiter")
        release = getattr(limiter, "async_release_max_parallel_requests_on_disconnect", None)
        if (importlib.metadata.version("litellm") != PIN
                or type(limiter) is not _PROXY_MaxParallelRequestsHandler_v3
                or not callable(release)):
            raise RuntimeError("Pinned native request-slot cleanup is unavailable.")
        await release(UserAPIKeyAuth())
    return GatewayIngress(app,policy,state,state_dir or os.environ.get("GATEWAY_STATE_DIR",".runtime/state"),master,
        native_request_cleanup=release_native_request_slot)


def main():
    p=argparse.ArgumentParser()
    p.add_argument("--config",default=os.environ.get("GATEWAY_POLICY_PATH","config/policy.yaml"))
    p.add_argument("--port",type=int,default=4000)
    p.add_argument("--host",default="0.0.0.0")
    p.add_argument("--profile",choices=["production","mock"])
    args=p.parse_args()
    if args.profile and load_policy(args.config).profile != args.profile:
        p.error("Profile and policy do not match.")
    import uvicorn
    uvicorn.run(build_app(args.config),host=args.host,port=args.port,workers=1,access_log=False,log_level="warning")


if __name__ == "__main__":
    main()
