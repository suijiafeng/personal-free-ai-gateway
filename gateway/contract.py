"""An explicit input capability contract; no silent parameter dropping."""
from .config import Policy
from .errors import GatewayError, invalid

ALLOWED = frozenset({"model", "messages", "stream", "max_completion_tokens", "n"})


def validate_request(data: object, policy: Policy) -> dict:
    if not isinstance(data, dict) or set(data) - ALLOWED:
        raise invalid("Only the documented text fields are supported; route overrides are forbidden.")
    if data.get("model") != policy.alias:
        raise GatewayError(404, "model_not_found", "Unknown logical model alias.")
    messages = data.get("messages")
    if not isinstance(messages, list) or not messages:
        raise invalid("messages must be a nonempty ordered array.")
    for m in messages:
        if not isinstance(m, dict) or set(m) != {"role", "content"}:
            raise invalid("Each message requires only role and text content.")
        if m["role"] not in ("system", "user", "assistant") or not isinstance(m["content"], str):
            raise invalid("Only system, user and assistant text messages are supported.")
    if type(data.get("stream", False)) is not bool:
        raise invalid("stream must be a boolean.")
    if type(data.get("n", 1)) is not int or data.get("n", 1) != 1:
        raise invalid("Only n=1 is supported.")
    tokens = data.get("max_completion_tokens", policy.limits.default_output_tokens)
    if type(tokens) is not int or not 1 <= tokens <= 4096:
        raise invalid("max_completion_tokens must be an integer from 1 to 4096.")
    result = dict(data)
    result["max_completion_tokens"] = tokens
    return result


def check_capability(body, deployment):
    cap = deployment.capability
    if body.get("stream", False) and not cap.streaming:
        raise GatewayError(400, "unsupported_capability", "Streaming is not approved for a candidate.")
    if body["max_completion_tokens"] > cap.max_output_tokens:
        raise GatewayError(400, "unsupported_capability", "Output limit exceeds approved capabilities.")
    if any(m["role"] not in cap.roles for m in body["messages"]):
        raise GatewayError(400, "unsupported_capability", "Message role is not approved for a candidate.")
    if sum(len(m["content"]) for m in body["messages"]) > cap.max_input_chars:
        raise GatewayError(400, "unsupported_capability", "Input exceeds the documented conservative character limit.")
    if "max_completion_tokens" not in cap.parameters:
        raise GatewayError(400, "unsupported_capability", "Output limit parameter is not approved for a candidate.")
