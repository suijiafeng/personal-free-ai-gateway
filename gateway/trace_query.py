"""Strict private metadata queries. No SQL identifiers or raw values come from users."""
from __future__ import annotations
from datetime import datetime, timezone
import re
from urllib.parse import parse_qs
from uuid import UUID

from .errors import invalid

STATUSES = frozenset({"complete", "truncated", "refused", "failed", "stream_interrupted", "cancelled"})


def parse_trace_query(raw: bytes) -> dict:
    try:
        if len(raw) > 2048:
            raise ValueError
        query = parse_qs(raw.decode("ascii"), strict_parsing=True, keep_blank_values=True)
        allowed = {"request_id", "limit", "before_id", "since", "until", "alias", "key_id", "error_code", "final_status", "fallback"}
        if set(query) - allowed or any(len(values) != 1 for values in query.values()):
            raise ValueError
        result = {"limit": 100}
        for name in ("limit", "before_id"):
            if name in query:
                value = query[name][0]
                if not re.fullmatch(r"[1-9][0-9]{0,18}", value):
                    raise ValueError
                result[name] = int(value)
        if result["limit"] > 100 or result.get("before_id", 1) > 2**63 - 1:
            raise ValueError
        if "request_id" in query:
            result["request_id"] = str(UUID(query["request_id"][0]))
        for name in ("since", "until"):
            if name in query:
                value = datetime.fromisoformat(query[name][0].replace("Z", "+00:00"))
                if value.tzinfo is None:
                    raise ValueError
                result[name] = value.astimezone(timezone.utc).isoformat()
        if "since" in result and "until" in result and result["since"] >= result["until"]:
            raise ValueError
        if "alias" in query:
            if query["alias"][0] != "general-free":
                raise ValueError
            result["alias"] = "general-free"
        if "key_id" in query:
            if not re.fullmatch(r"[0-9a-f]{16}", query["key_id"][0]):
                raise ValueError
            result["key_id"] = query["key_id"][0]
        if "error_code" in query:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", query["error_code"][0]):
                raise ValueError
            result["error_code"] = query["error_code"][0]
        if "final_status" in query:
            if query["final_status"][0] not in STATUSES:
                raise ValueError
            result["final_status"] = query["final_status"][0]
        if "fallback" in query:
            if query["fallback"][0] not in ("true", "false"):
                raise ValueError
            result["fallback"] = query["fallback"][0] == "true"
        return result
    except (ValueError, UnicodeError, OverflowError):
        raise invalid("Invalid diagnostic filter, timezone, UUID, or pagination limit.") from None


def page_result(rows, limit, filters):
    has_more = len(rows) > limit
    data = rows[:limit]
    return {"data": data, "next_cursor": data[-1]["event_id"] if has_more else None,
        "order": "event_id_desc", "filters": {k: v for k, v in filters.items() if v is not None},
        "filter_scope": "request_id/key_id/alias and time match events; error/final_status/fallback select complete request timelines",
        "contains_content": False, "schema_version": 1}


def match_event(event, filters):
    for field in ("request_id", "alias", "key_id"):
        if filters.get(field) is not None and event.get(field) != filters[field]:
            return False
    if filters.get("before_id") is not None and event["event_id"] >= filters["before_id"]:
        return False
    timestamp = datetime.fromisoformat(event["timestamp"])
    if filters.get("since") and timestamp < datetime.fromisoformat(filters["since"]):
        return False
    if filters.get("until") and timestamp >= datetime.fromisoformat(filters["until"]):
        return False
    return True
