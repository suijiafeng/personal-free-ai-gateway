"""Pure, bounded quota observations; no requests, routing, accounting or refills.

Only reviewed response-header mappings may produce measured balances. HTTP 429
and Retry-After are availability feedback, never evidence of an empty quota.
"""
from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, fields, replace
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .config import Pool

UTC = timezone.utc
DEFAULT_TTL = timedelta(minutes=5)
MAX_RESET_DELAY = timedelta(days=2)
MAX_COUNT = 2**63 - 1
GROQ_REFERENCE = "https://console.groq.com/docs/rate-limits"
GEMINI_REFERENCE = "https://ai.google.dev/gemini-api/docs/rate-limits"
QUOTA_HEADER_NAMES = frozenset(
    f"x-ratelimit-{field}-{dimension}"
    for field in ("limit", "remaining", "reset")
    for dimension in ("requests", "tokens")
)
Source = Literal["upstream_observed", "local_estimate", "unknown", "expired"]
ResetSource = Literal["upstream_header", "upstream_observed", "documented_rule", "unknown"]


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("quota timestamps require a timezone")
    return value.astimezone(UTC)


def _count(value: object) -> int | None:
    """No coercion of booleans, floats, negative, huge or ambiguous counters."""
    return value if type(value) is int and 0 <= value <= MAX_COUNT else None


def _reference(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise ValueError("quota evidence requires a bounded reference")
    return value


def _ttl(value: timedelta) -> timedelta:
    if not isinstance(value, timedelta) or not timedelta(0) < value <= timedelta(days=1):
        raise ValueError("quota observation TTL must be positive and at most one day")
    return value


def next_local_midnight(tz: str, now: datetime) -> datetime:
    """Next calendar midnight in tz, stored as UTC, not now + 24 hours.

    Refuse nonexistent or ambiguous midnight boundaries instead of guessing.
    America/Los_Angeles has unambiguous midnights on its 23/25-hour DST days.
    """
    now = _utc(now)
    try:
        zone = ZoneInfo(tz)
    except (ZoneInfoNotFoundError, TypeError, ValueError):
        raise ValueError("unknown quota reset timezone") from None
    date = now.astimezone(zone).date() + timedelta(days=1)
    wall = datetime.combine(date, time.min)
    early, late = (wall.replace(tzinfo=zone, fold=f) for f in (0, 1))
    if early.utcoffset() != late.utcoffset() or early.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != wall:
        raise ValueError("quota midnight is ambiguous or nonexistent")
    return early.astimezone(UTC)


@dataclass(frozen=True)
class HeaderMapping:
    """Reviewed semantics for the six supported quota header names only."""

    provider: str
    dimension: Literal["requests", "tokens"]
    window: Literal["minute", "day"]
    reference: str
    limit_header: str
    remaining_header: str
    reset_header: str
    reset_format: Literal["duration", "seconds", "unix_seconds", "rfc3339"] = "duration"

    def __post_init__(self):
        _reference(self.provider)
        _reference(self.reference)
        if self.dimension not in ("requests", "tokens") or self.window not in ("minute", "day"):
            raise ValueError("unsupported quota dimension or window")
        if self.reset_format not in ("duration", "seconds", "unix_seconds", "rfc3339"):
            raise ValueError("unsupported quota reset format")
        for field in ("limit", "remaining", "reset"):
            name = getattr(self, field + "_header").lower()
            if name != f"x-ratelimit-{field}-{self.dimension}":
                raise ValueError("unapproved quota header mapping")
            object.__setattr__(self, field + "_header", name)


def _mapping(provider: str, dimension: str, window: str, reference: str) -> HeaderMapping:
    return HeaderMapping(provider, dimension, window, reference,
        *(f"x-ratelimit-{field}-{dimension}" for field in ("limit", "remaining", "reset")))


GROQ_MAPPINGS = {
    ("requests", "day"): _mapping("groq", "requests", "day", GROQ_REFERENCE),
    ("tokens", "minute"): _mapping("groq", "tokens", "minute", GROQ_REFERENCE),
}
MOCK_REQUESTS_MINUTE_MAPPING = _mapping("mock", "requests", "minute", "synthetic-upstream-contract-v1")


@dataclass(frozen=True)
class QuotaObservation:
    pool_id: str
    provider: str
    account_scope: str
    model_scope: tuple[str, ...]
    dimension: Literal["requests", "tokens"]
    window: Literal["minute", "day", "unknown"]
    reset_timezone: str | None
    scope_reference: str
    source: Source
    reference: str | None
    limit: int | None
    remaining: int | None
    observed_at: datetime
    expires_at: datetime | None = None
    reset_at: datetime | None = None
    reset_source: ResetSource = "unknown"
    reset_reference: str | None = None

    def __post_init__(self):
        for name in ("pool_id", "provider", "account_scope", "scope_reference"):
            _reference(getattr(self, name))
        if not isinstance(self.model_scope, (list, tuple)) or not self.model_scope:
            raise ValueError("quota model scope is required")
        object.__setattr__(self, "model_scope", tuple(_reference(v) for v in self.model_scope))
        if self.dimension not in ("requests", "tokens") or self.window not in ("minute", "day", "unknown"):
            raise ValueError("unsupported quota dimension or window")
        if self.reset_timezone is not None:
            try:
                ZoneInfo(self.reset_timezone)
            except (ZoneInfoNotFoundError, TypeError, ValueError):
                raise ValueError("unknown quota reset timezone") from None
        if self.source not in ("upstream_observed", "local_estimate", "unknown", "expired"):
            raise ValueError("unsupported quota source")
        if self.reset_source not in ("upstream_header", "upstream_observed", "documented_rule", "unknown"):
            raise ValueError("unsupported quota reset source")
        for name in ("reference", "reset_reference"):
            if getattr(self, name) is not None:
                _reference(getattr(self, name))
        for name in ("observed_at", "expires_at", "reset_at"):
            value = getattr(self, name)
            if value is not None:
                object.__setattr__(self, name, _utc(value))
        if self.observed_at is None:
            raise ValueError("quota observation time is required")
        if any(v is not None and _count(v) is None for v in (self.limit, self.remaining)):
            raise ValueError("quota counts must be nonnegative bounded integers")
        if self.limit is not None and self.remaining is not None and self.remaining > self.limit:
            raise ValueError("quota remaining exceeds the observed limit")
        if self.source in ("unknown", "expired") and (self.limit is not None or self.remaining is not None):
            raise ValueError("unknown or expired balances must be null")
        if self.source in ("upstream_observed", "local_estimate"):
            if self.reference is None or self.expires_at is None or self.expires_at <= self.observed_at:
                raise ValueError("quota evidence requires a reference and future expiry")
            if self.expires_at > self.observed_at + MAX_RESET_DELAY:
                raise ValueError("quota observation expiry exceeds supported horizon")
        if self.reset_at is not None and (self.reset_source == "unknown" or self.reset_reference is None):
            raise ValueError("quota reset requires provenance")
        if self.reset_at is not None and not self.observed_at < self.reset_at <= self.observed_at + MAX_RESET_DELAY:
            raise ValueError("quota reset exceeds supported observation horizon")
        if self.reset_at is None and (self.reset_source != "unknown" or self.reset_reference is not None):
            raise ValueError("quota reset provenance requires a reset time")

    @property
    def known_exhausted(self) -> bool:
        # Knowing the balance is zero does not imply knowing its recovery time.
        # The state layer can wait for the bounded evidence expiry before one
        # controlled request; that local probe policy is not a provider reset.
        return self.source == "upstream_observed" and self.remaining == 0

    @property
    def next_probe_at(self) -> datetime | None:
        return self.reset_at if self.known_exhausted else None

    def to_dict(self) -> dict:
        result = asdict(self)
        result.update(known_exhausted=self.known_exhausted, next_probe_at=self.next_probe_at)
        result["model_scope"] = list(self.model_scope)
        return {k: v.isoformat() if isinstance(v, datetime) else v for k, v in result.items()}

    @classmethod
    def from_dict(cls, value: Mapping) -> QuotaObservation:
        names = {field.name for field in fields(cls)}
        if not isinstance(value, Mapping) or set(value) - names - {"known_exhausted", "next_probe_at"}:
            raise ValueError("invalid quota observation fields")
        data = {key: value[key] for key in names if key in value}
        for name in ("observed_at", "expires_at", "reset_at"):
            if isinstance(data.get(name), str):
                try:
                    data[name] = datetime.fromisoformat(data[name])
                except ValueError:
                    raise ValueError("invalid quota observation timestamp") from None
        # Derived flags are never trusted on restore.
        try:
            return cls(**data)
        except TypeError:
            raise ValueError("invalid quota observation fields") from None


def _identity(pool: Pool) -> dict:
    return dict(pool_id=pool.id, provider=pool.provider, account_scope=pool.account_scope,
        model_scope=tuple(pool.model_scope), dimension=pool.dimension, window=pool.window,
        reset_timezone=pool.reset_timezone, scope_reference=pool.shared_scope_evidence)


def unknown_observation(pool: Pool, now: datetime) -> QuotaObservation:
    return QuotaObservation(**_identity(pool), source="unknown", reference=None,
        limit=None, remaining=None, observed_at=_utc(now))


def _at(observation: QuotaObservation, now: datetime) -> QuotaObservation:
    now = _utc(now)
    if observation.observed_at > now:
        return replace(observation, source="unknown", limit=None, remaining=None,
            reference=None, observed_at=now, expires_at=None, reset_at=None,
            reset_source="unknown", reset_reference=None)
    if observation.source not in ("unknown", "expired") and (
        observation.expires_at is None or now >= observation.expires_at
        or observation.reset_at is not None and now >= observation.reset_at
    ):
        return replace(observation, source="expired", limit=None, remaining=None)
    return observation


def present_observation(observation: QuotaObservation | Mapping, now: datetime) -> dict:
    """A current view; stored objects are immutable and never refilled on expiry."""
    if not isinstance(observation, QuotaObservation):
        observation = QuotaObservation.from_dict(observation)
    return _at(observation, now).to_dict()


def snapshot(pool: Pool, observation: QuotaObservation | Mapping | None, now: datetime) -> QuotaObservation:
    """Bind restored observations to the current pool; corrupt/missing is unknown."""
    now = _utc(now)
    try:
        if not isinstance(observation, QuotaObservation):
            observation = QuotaObservation.from_dict(observation)
        if any(getattr(observation, name) != value for name, value in _identity(pool).items()):
            raise ValueError("quota scope changed")
        return _at(observation, now)
    except (ValueError, TypeError, OverflowError):
        return unknown_observation(pool, now)


def _decimal(value: object) -> Decimal | None:
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]{1,6})?", value, flags=re.ASCII):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:
        return None


def parse_duration(value: object) -> timedelta | None:
    """Parse bounded provider durations such as 2m59.56s; no units guessed."""
    if not isinstance(value, str) or not value or len(value) > 128:
        return None
    value = value.strip()
    matches = list(re.finditer(r"([0-9]+(?:\.[0-9]{1,6})?)(ms|d|h|m|s)", value, flags=re.ASCII))
    if not matches or "".join(match.group(0) for match in matches) != value:
        return None
    units = {"d": (4, Decimal(86400)), "h": (3, Decimal(3600)),
        "m": (2, Decimal(60)), "s": (1, Decimal(1)), "ms": (0, Decimal("0.001"))}
    ranks = [units[match.group(2)][0] for match in matches]
    if ranks != sorted(set(ranks), reverse=True):
        return None
    seconds = sum((_decimal(match.group(1)) * units[match.group(2)][1] for match in matches), Decimal(0))
    if not 0 < seconds <= Decimal(str(MAX_RESET_DELAY.total_seconds())):
        return None
    duration = timedelta(microseconds=int(seconds * 1000000))
    return duration if duration > timedelta(0) else None


def _headers(headers: Mapping | None) -> dict[str, str]:
    """Do not retain or stringify values from unapproved header names."""
    result, seen, duplicates = {}, set(), set()
    if not isinstance(headers, Mapping):
        return result
    for key, value in headers.items():
        if not isinstance(key, str) or key.lower() not in QUOTA_HEADER_NAMES:
            continue
        key = key.lower()
        if key in seen:
            duplicates.add(key)
        seen.add(key)
        if isinstance(value, str) and len(value) <= 128:
            result[key] = value.strip()
    return {key: value for key, value in result.items() if key not in duplicates}


def _header_count(value: str | None) -> int | None:
    if value is None or not re.fullmatch(r"[0-9]{1,19}", value, flags=re.ASCII):
        return None
    return _count(int(value))


def _reset(value: str | None, format: str, now: datetime) -> datetime | None:
    try:
        if format == "duration":
            duration = parse_duration(value)
            result = now + duration if duration is not None else None
        elif format in ("seconds", "unix_seconds"):
            number = _decimal(value)
            if number is None:
                return None
            if format == "seconds":
                if not 0 < number <= MAX_RESET_DELAY.total_seconds():
                    return None
                result = now + timedelta(microseconds=int(number * 1000000))
            else:
                result = datetime.fromtimestamp(float(number), UTC)
        else:
            if not value or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?(?:Z|[+-][0-9]{2}:[0-9]{2})", value, flags=re.ASCII):
                return None
            result = _utc(datetime.fromisoformat(value))
        return result if result is not None and now < result <= now + MAX_RESET_DELAY else None
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def observe_values(pool: Pool, now: datetime, *, limit: int | None = None,
    remaining: int | None = None, reference: str, source: Source = "upstream_observed",
    reset_at: datetime | None = None, reset_source: ResetSource = "upstream_observed",
    reset_reference: str | None = None, ttl: timedelta = DEFAULT_TTL) -> QuotaObservation:
    """For a verified quota API or explicit estimate; never infer from usage/429.

    Callers must establish provenance. A configured Gemini RPD midnight is used
    only with its explicit Pacific zone; this does not provide a balance.
    """
    now, ttl = _utc(now), _ttl(ttl)
    if source not in ("upstream_observed", "local_estimate"):
        raise ValueError("new quota values require explicit provenance")
    _reference(reference)
    if reset_at is not None:
        reset_at = _utc(reset_at)
        if not now < reset_at <= now + MAX_RESET_DELAY:
            raise ValueError("quota reset must be a bounded future instant")
        _reference(reset_reference)
    elif pool.provider == "gemini" and pool.dimension == "requests" and pool.window == "day" and pool.reset_timezone == "America/Los_Angeles":
        reset_at = next_local_midnight(pool.reset_timezone, now)
        reset_source, reset_reference = "documented_rule", GEMINI_REFERENCE
    else:
        reset_source, reset_reference = "unknown", None
    # Empty observed quota remains evidence until its known reset. Estimates
    # never block, and positive/partial observations have a short freshness TTL.
    expires_at = (reset_at if source == "upstream_observed" and remaining == 0 and reset_at is not None
        else min(now + ttl, reset_at) if reset_at is not None else now + ttl)
    return QuotaObservation(**_identity(pool), source=source, reference=reference,
        limit=limit, remaining=remaining, observed_at=now, expires_at=expires_at,
        reset_at=reset_at, reset_source=reset_source, reset_reference=reset_reference)


def observe_local_estimate(pool: Pool, now: datetime, *, limit: int | None = None,
    remaining: int | None = None, reference: str, ttl: timedelta = DEFAULT_TTL) -> QuotaObservation:
    return observe_values(pool, now, limit=limit, remaining=remaining,
        reference=reference, source="local_estimate", ttl=ttl)


def observe_headers(pool: Pool, headers: Mapping | None, now: datetime, provider: str | None = None,
    *, mapping: HeaderMapping | None = None, ttl: timedelta = DEFAULT_TTL) -> QuotaObservation | None:
    """None means no new reliable quota observation; preserve existing evidence.

    Provider/window/dimension must all match. Explicit mappings support synthetic
    upstream tests, but header names stay allowlisted and raw headers stay out.
    """
    now, ttl = _utc(now), _ttl(ttl)
    provider = provider or pool.provider
    if provider != pool.provider:
        return None
    if mapping is None and provider == "groq":
        mapping = GROQ_MAPPINGS.get((pool.dimension, pool.window))
    if mapping is None or (mapping.provider, mapping.dimension, mapping.window) != (provider, pool.dimension, pool.window):
        return None
    values = _headers(headers)
    limit, remaining = (_header_count(values.get(name)) for name in (mapping.limit_header, mapping.remaining_header))
    if limit is not None and remaining is not None and remaining > limit:
        return None
    reset_at = _reset(values.get(mapping.reset_header), mapping.reset_format, now)
    if limit is None and remaining is None and reset_at is None:
        return None
    return observe_values(pool, now, limit=limit, remaining=remaining, reference=mapping.reference,
        reset_at=reset_at, reset_source="upstream_header",
        reset_reference=mapping.reset_header if reset_at is not None else None, ttl=ttl)
