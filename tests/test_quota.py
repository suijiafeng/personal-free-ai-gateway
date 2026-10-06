"""AT-09/13/14: pure observations, not provider/account acceptance tests."""
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
import json
from zoneinfo import ZoneInfo

import pytest

from gateway.config import Pool
from gateway.quota import (
    DEFAULT_TTL, GEMINI_REFERENCE, GROQ_REFERENCE, HeaderMapping,
    MOCK_REQUESTS_MINUTE_MAPPING, QuotaObservation, next_local_midnight,
    observe_headers, observe_local_estimate, observe_values, parse_duration,
    present_observation, snapshot, unknown_observation,
)

UTC = timezone.utc
NOW = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)


def pool(**changes):
    return Pool(**(dict(id="org-model-rpd", provider="groq", account_scope="organization-a",
        model_scope=["reviewed-model"], dimension="requests", window="day", reset_timezone=None,
        shared_scope_evidence="synthetic reviewed scope") | changes))


def headers(dimension="requests", limit="100", remaining="12", reset="2m59.56s"):
    return {f"x-ratelimit-limit-{dimension}": limit,
        f"x-ratelimit-remaining-{dimension}": remaining,
        f"x-ratelimit-reset-{dimension}": reset}


def observed(**changes):
    return observe_headers(pool(), headers(**changes), NOW)


def test_groq_request_headers_are_daily_and_keep_scope():
    result = observed()
    assert result.source == "upstream_observed"
    assert (result.pool_id, result.dimension, result.window) == ("org-model-rpd", "requests", "day")
    assert result.limit == 100 and result.remaining == 12
    assert result.account_scope == "organization-a" and result.model_scope == ("reviewed-model",)
    assert result.scope_reference == "synthetic reviewed scope"
    assert result.reference == GROQ_REFERENCE
    assert result.reset_source == "upstream_header"
    assert result.reset_reference == "x-ratelimit-reset-requests"
    assert result.reset_at == NOW + timedelta(seconds=179.56)
    assert result.expires_at == result.reset_at
    assert result.next_probe_at is None and not result.known_exhausted


def test_groq_tokens_are_separate_minute_observations_not_request_balance():
    combined = headers() | headers("tokens", "5000", "4900", "7.66s")
    requests = observe_headers(pool(), combined, NOW)
    tokens = observe_headers(pool(id="org-model-tpm", dimension="tokens", window="minute"), combined, NOW)
    assert (requests.remaining, requests.window) == (12, "day")
    assert (tokens.remaining, tokens.window) == (4900, "minute")
    assert tokens.reset_at == NOW + timedelta(seconds=7.66)
    assert tokens.pool_id != requests.pool_id


@pytest.mark.parametrize("dimension,window", [("requests", "minute"), ("tokens", "day"),
    ("requests", "unknown"), ("tokens", "unknown")])
def test_groq_unsupported_windows_do_not_relabel_headers(dimension, window):
    assert observe_headers(pool(dimension=dimension, window=window), headers() | headers("tokens"), NOW) is None


@pytest.mark.parametrize("provider", ["gemini", "openai", "mock", "unknown"])
def test_header_names_alone_do_not_certify_other_provider_semantics(provider):
    assert observe_headers(pool(provider=provider), headers(), NOW) is None


def test_mock_mapping_requires_matching_explicit_configuration():
    mock_pool = pool(provider="mock", dimension="requests", window="minute")
    assert observe_headers(mock_pool, headers(), NOW) is None
    result = observe_headers(mock_pool, headers(remaining="0"), NOW, mapping=MOCK_REQUESTS_MINUTE_MAPPING)
    assert result.source == "upstream_observed" and result.known_exhausted
    assert result.reference == "synthetic-upstream-contract-v1"
    assert observe_headers(pool(), headers(), NOW, mapping=MOCK_REQUESTS_MINUTE_MAPPING) is None
    assert observe_headers(mock_pool, headers(), NOW, provider="openai", mapping=MOCK_REQUESTS_MINUTE_MAPPING) is None


@pytest.mark.parametrize("values", [{}, {"retry-after": "90"}, {"status": "429"},
    {"x-ratelimit-remaining-requests": "NaN"}, {"authorization": "secret"}, None])
def test_no_valid_quota_evidence_returns_none_not_zero(values):
    assert observe_headers(pool(), values, NOW) is None


def test_unknown_observation_contains_nulls_not_zero_or_full():
    result = unknown_observation(pool(), NOW)
    assert result.source == "unknown"
    assert result.limit is None and result.remaining is None
    assert result.observed_at == NOW and result.expires_at is None
    assert result.reference is None and result.reset_source == "unknown"
    assert result.next_probe_at is None and not result.known_exhausted


def test_missing_remaining_with_limit_does_not_assume_full():
    result = observe_headers(pool(), {"x-ratelimit-limit-requests": "100"}, NOW)
    assert result.limit == 100 and result.remaining is None
    assert result.source == "upstream_observed" and not result.known_exhausted


def test_missing_limit_does_not_discard_explicit_remaining():
    result = observe_headers(pool(), {"x-ratelimit-remaining-requests": "12"}, NOW)
    assert result.remaining == 12 and result.limit is None


def test_reset_only_does_not_create_balance_or_exhaustion():
    result = observe_headers(pool(), {"x-ratelimit-reset-requests": "1m"}, NOW)
    assert result.remaining is None and result.limit is None
    assert result.reset_at == NOW + timedelta(minutes=1)
    assert not result.known_exhausted and result.next_probe_at is None


@pytest.mark.parametrize("invalid", ["", "-1", "1.0", "1e2", "+1", "1,000", "1, 2", "NaN", "Infinity",
    "١", "０", "0\x00", "100\r\nAuthorization: secret", "9223372036854775808", "1" * 129,
    0, 10, True, False, None, [], {}, b"0"])
def test_invalid_counters_stay_unknown(invalid):
    result = observe_headers(pool(), headers(remaining=invalid), NOW)
    assert result.remaining is None and not result.known_exhausted


@pytest.mark.parametrize("valid,expected", [("0", 0), ("00012", 12), (" 12 ", 12), ("9223372036854775807", 2**63-1)])
def test_valid_count_headers(valid, expected):
    result = observe_headers(pool(), {"x-ratelimit-remaining-requests": valid}, NOW)
    assert result.remaining == expected


def test_inconsistent_limit_remaining_rejects_whole_observation():
    assert observed(limit="3", remaining="4") is None


def test_case_insensitive_headers_and_no_raw_header_retention():
    class Poison:
        def __str__(self):
            raise AssertionError("unapproved header value must not be converted")
    result = observe_headers(pool(), {**{k.upper(): v for k, v in headers().items()},
        "Authorization": Poison(), "Set-Cookie": "secret-marker", "raw_error": "secret-marker"}, NOW)
    assert result.remaining == 12
    assert "secret-marker" not in json.dumps(result.to_dict())
    assert "authorization" not in result.to_dict()


@pytest.mark.parametrize("first", ["2", None, True, b"0"])
def test_case_variant_duplicate_remaining_is_not_trusted(first):
    result = observe_headers(pool(), {**headers(), "X-Ratelimit-Remaining-Requests": first}, NOW)
    assert result.remaining is None and not result.known_exhausted


@pytest.mark.parametrize("raw,seconds", [("2m59.56s", 179.56), ("7.66s", 7.66), ("1h2m3s", 3723),
    ("1d", 86400), ("2d", 172800), ("100ms", .1), ("0.000001s", .000001), (" 2m ", 120)])
def test_valid_durations(raw, seconds):
    assert parse_duration(raw) == timedelta(seconds=seconds)


@pytest.mark.parametrize("raw", [None, 10, True, "", "0", "0s", "-1s", "+1s", "1e2s", "nan", "inf",
    "tomorrow", "1 minute", "1m 2s", "3d", "1s1s", "1s1m", "1m2h", "1.0000001s", "1e99h", "١s",
    "1mextra", "1" * 129 + "s", "0.000001ms", "2026-10-07T00:00:00Z"])
def test_invalid_or_ambiguous_durations_are_unknown(raw):
    assert parse_duration(raw) is None


def test_known_exhausted_with_trusted_reset_is_retained_to_reset():
    result = observed(remaining="0", reset="12h")
    assert result.known_exhausted
    assert result.next_probe_at == NOW + timedelta(hours=12)
    assert result.expires_at == result.reset_at
    # Do not age out known exhaustion after the normal positive-balance TTL.
    assert snapshot(pool(), result, NOW + DEFAULT_TTL).known_exhausted
    assert not observed(remaining="1", reset="12h").known_exhausted


@pytest.mark.parametrize("reset", [None, "", "0s", "-1s", "bad", "3d", "2026-10-07T00:00:00Z"])
def test_explicit_zero_without_valid_reset_is_exhausted_with_unknown_recovery(reset):
    result = observed(remaining="0", reset=reset)
    assert result.remaining == 0 and result.known_exhausted
    assert result.next_probe_at is None and result.reset_at is None
    assert result.expires_at == NOW + DEFAULT_TTL
    expired = snapshot(pool(), result, NOW + DEFAULT_TTL)
    assert expired.source == "expired" and expired.remaining is None
    assert not expired.known_exhausted and expired.next_probe_at is None


def test_retry_after_is_not_a_quota_reset_even_with_remaining_zero():
    result = observe_headers(pool(), {"x-ratelimit-remaining-requests": "0", "retry-after": "86400"}, NOW)
    assert result.reset_at is None and result.known_exhausted
    assert result.next_probe_at is None and result.expires_at == NOW + DEFAULT_TTL


def test_positive_balance_short_ttl_not_day_reset_promise():
    result = observed(reset="12h")
    assert result.expires_at == NOW + DEFAULT_TTL
    assert result.reset_at == NOW + timedelta(hours=12)


def test_exact_expiry_clears_balances_and_never_refills():
    original = observed(remaining="0", reset="1h")
    expired = snapshot(pool(), original, original.reset_at)
    assert expired.source == "expired" and expired.remaining is None and expired.limit is None
    assert expired.next_probe_at is None and not expired.known_exhausted
    assert expired.observed_at == NOW and expired.reset_at == original.reset_at
    assert expired.reset_reference == original.reset_reference
    assert original.remaining == 0 and original.source == "upstream_observed"


def test_expired_presentation_is_json_safe_nonmutating_and_keeps_provenance():
    original = observed(reset="1h")
    stored = original.to_dict()
    shown = present_observation(stored, NOW + DEFAULT_TTL)
    assert shown["source"] == "expired" and shown["remaining"] is None
    assert shown["reference"] == GROQ_REFERENCE
    assert shown["observed_at"].endswith("+00:00")
    assert stored["remaining"] == 12
    json.dumps(shown)


def test_observations_are_immutable():
    with pytest.raises(FrozenInstanceError):
        observed().remaining = 99


def test_restored_observation_survives_before_reset():
    original = observed(remaining="0", reset="2h")
    restored = snapshot(pool(), json.loads(json.dumps(original.to_dict())), NOW + timedelta(hours=1))
    assert restored.known_exhausted and restored.remaining == 0
    assert restored.observed_at == original.observed_at


@pytest.mark.parametrize("value", [None, {}, [], {"source": "upstream_feedback"}, {"remaining": 100, "status": "available"}])
def test_missing_or_legacy_restored_data_is_unknown(value):
    result = snapshot(pool(), value, NOW)
    assert result.source == "unknown" and result.remaining is None and result.limit is None


@pytest.mark.parametrize("field,value", [("pool_id", "another-pool"), ("provider", "gemini"),
    ("account_scope", "another-org"), ("model_scope", ["another-model"]), ("dimension", "tokens"),
    ("window", "minute"), ("reset_timezone", "UTC"), ("scope_reference", "changed-scope-review")])
def test_changed_scope_or_dimension_does_not_reuse_old_balance(field, value):
    stored = observed().to_dict()
    stored[field] = value
    assert snapshot(pool(), stored, NOW).source == "unknown"


@pytest.mark.parametrize("field,value", [("remaining", -1), ("limit", True), ("source", "full"),
    ("observed_at", "invalid"), ("observed_at", "2026-10-06T08:00:00"), ("expires_at", None),
    ("expires_at", "2099-01-01T00:00:00Z"), ("reset_at", "2099-01-01T00:00:00Z"),
    ("reset_at", "2026-10-06T08:00:00Z"),
    ("reset_source", "inferred-from-429"), ("reset_reference", None), ("raw_error", "secret-marker")])
def test_invalid_restored_values_do_not_revive_balance(field, value):
    stored = observed().to_dict()
    stored[field] = value
    result = snapshot(pool(), stored, NOW)
    assert result.source == "unknown" and result.remaining is None
    assert "secret-marker" not in json.dumps(result.to_dict())


def test_restored_derived_exhaustion_flags_are_recomputed():
    stored = observed().to_dict() | {"known_exhausted": True, "next_probe_at": "2099-01-01T00:00:00Z"}
    restored = snapshot(pool(), stored, NOW)
    assert restored.remaining == 12 and not restored.known_exhausted and restored.next_probe_at is None


def test_future_observation_is_unknown_not_a_clock_skew_balance_claim():
    result = snapshot(pool(), observed(), NOW - timedelta(seconds=1))
    assert result.source == "unknown" and result.remaining is None and result.reset_at is None


def test_independent_pools_and_shared_pool_reference_do_not_sum():
    a = pool(id="a", account_scope="organization-a")
    b = pool(id="b", account_scope="organization-b")
    first = observe_headers(a, headers(remaining="0"), NOW)
    second = observe_headers(b, headers(remaining="80"), NOW)
    assert snapshot(a, first, NOW).known_exhausted
    assert not snapshot(b, second, NOW).known_exhausted
    assert snapshot(b, first, NOW).source == "unknown"
    assert snapshot(a, first, NOW) == snapshot(a, first.to_dict(), NOW)


def test_local_estimates_are_labelled_and_never_known_exhausted():
    gemini = pool(provider="gemini", reset_timezone="America/Los_Angeles")
    result = observe_local_estimate(gemini, NOW, limit=100, remaining=0, reference="gateway-local-counter-v1")
    assert result.source == "local_estimate" and result.remaining == 0
    assert not result.known_exhausted and result.next_probe_at is None
    assert result.reset_source == "documented_rule"
    assert snapshot(gemini, result, NOW + DEFAULT_TTL).source == "expired"


def test_gemini_daily_reset_is_known_rule_not_balance_source():
    gemini = pool(provider="gemini", account_scope="project-a", reset_timezone="America/Los_Angeles")
    assert observe_headers(gemini, headers(remaining="0"), NOW) is None
    assert unknown_observation(gemini, NOW).remaining is None
    result = observe_values(gemini, NOW, remaining=0, reference="synthetic-explicit-quota-API-response")
    assert result.known_exhausted and result.reset_source == "documented_rule"
    assert result.reset_reference == GEMINI_REFERENCE
    assert result.next_probe_at == datetime(2026, 10, 7, 7, tzinfo=UTC)


@pytest.mark.parametrize("changes", [{"reset_timezone": None}, {"reset_timezone": "UTC"},
    {"window": "minute"}, {"dimension": "tokens"}, {"provider": "groq"}])
def test_daily_reset_rule_is_not_generalized_without_evidence(changes):
    config = dict(provider="gemini", reset_timezone="America/Los_Angeles") | changes
    result = observe_values(pool(**config), NOW, remaining=0, reference="synthetic-explicit-observation")
    assert result.reset_at is None and result.known_exhausted
    assert result.next_probe_at is None


@pytest.mark.parametrize("now,expected,hours", [
    (datetime(2026, 3, 8, 8, tzinfo=UTC), datetime(2026, 3, 9, 7, tzinfo=UTC), 23),
    (datetime(2026, 11, 1, 7, tzinfo=UTC), datetime(2026, 11, 2, 8, tzinfo=UTC), 25),
    (datetime(2026, 1, 1, 8, tzinfo=UTC), datetime(2026, 1, 2, 8, tzinfo=UTC), 24),
    (datetime(2026, 7, 1, 7, tzinfo=UTC), datetime(2026, 7, 2, 7, tzinfo=UTC), 24),
])
def test_pacific_midnight_follows_dst_calendar_days(now, expected, hours):
    result = next_local_midnight("America/Los_Angeles", now)
    assert result == expected and result - now == timedelta(hours=hours)
    assert result.astimezone(ZoneInfo("America/Los_Angeles")).hour == 0


def test_midnight_uses_provider_day_not_utc_calendar_day():
    before_pacific_midnight = datetime(2026, 10, 6, 6, 59, 59, tzinfo=UTC)
    assert next_local_midnight("America/Los_Angeles", before_pacific_midnight) == datetime(2026, 10, 6, 7, tzinfo=UTC)
    assert next_local_midnight("America/Los_Angeles", before_pacific_midnight + timedelta(seconds=1)) == datetime(2026, 10, 7, 7, tzinfo=UTC)


def test_aware_non_utc_observation_is_normalized_to_utc():
    now = NOW.astimezone(ZoneInfo("Asia/Tokyo"))
    result = observe_headers(pool(), headers(), now)
    assert result.observed_at == NOW and result.observed_at.tzinfo == UTC
    assert result.to_dict()["observed_at"].endswith("+00:00")


@pytest.mark.parametrize("tz", ["Mars/Sea", "", None, "../UTC"])
def test_invalid_reset_timezone_fails_without_echoing_input(tz):
    with pytest.raises(ValueError, match="unknown quota reset timezone"):
        next_local_midnight(tz, NOW)


def test_nonexistent_midnight_is_not_guessed():
    # Samoa skipped December 30, 2011 entirely.
    with pytest.raises(ValueError, match="ambiguous or nonexistent"):
        next_local_midnight("Pacific/Apia", datetime(2011, 12, 29, 22, tzinfo=UTC))


@pytest.mark.parametrize("call", [lambda: next_local_midnight("UTC", datetime(2026, 1, 1)),
    lambda: observe_headers(pool(), headers(), datetime(2026, 1, 1)),
    lambda: present_observation(observed(), datetime(2026, 1, 1))])
def test_naive_times_are_never_interpreted_using_server_timezone(call):
    with pytest.raises(ValueError, match="timezone"):
        call()


@pytest.mark.parametrize("format,raw", [("seconds", "30.5"), ("unix_seconds", str(NOW.timestamp() + 30.5)),
    ("rfc3339", "2026-10-06T08:00:30.500000Z")])
def test_explicit_reviewed_reset_formats(format, raw):
    mapping = replace(MOCK_REQUESTS_MINUTE_MAPPING, reset_format=format)
    result = observe_headers(pool(provider="mock", window="minute"), headers(remaining="0", reset=raw), NOW, mapping=mapping)
    assert result.known_exhausted and result.reset_at == NOW + timedelta(seconds=30.5)


@pytest.mark.parametrize("format,raw", [("seconds", "-1"), ("seconds", "NaN"), ("seconds", "999999999999"),
    ("unix_seconds", str(NOW.timestamp())), ("unix_seconds", "1e9999"), ("unix_seconds", "9"*127),
    ("rfc3339", "2026-10-06T08:00:30"), ("rfc3339", "2026-10-06 08:00:30Z"),
    ("rfc3339", "2026-10-05T00:00:00Z"), ("rfc3339", "2099-01-01T00:00:00Z")])
def test_invalid_explicit_reset_formats_do_not_invent_recovery_for_observed_zero(format, raw):
    mapping = replace(MOCK_REQUESTS_MINUTE_MAPPING, reset_format=format)
    result = observe_headers(pool(provider="mock", window="minute"), headers(remaining="0", reset=raw), NOW, mapping=mapping)
    assert result.remaining == 0 and result.reset_at is None and result.known_exhausted
    assert result.next_probe_at is None and result.expires_at == NOW + DEFAULT_TTL


@pytest.mark.parametrize("changes", [{"limit_header": "authorization"}, {"remaining_header": "cookie"},
    {"reset_header": "retry-after"}, {"dimension": "tokens"}, {"window": "unknown"},
    {"reset_format": "guess"}, {"reference": ""}])
def test_mapping_cannot_capture_arbitrary_headers_or_guess_semantics(changes):
    with pytest.raises(ValueError):
        replace(MOCK_REQUESTS_MINUTE_MAPPING, **changes)


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1), timedelta(days=2), 60, None])
def test_invalid_ttl_cannot_make_indefinite_observation(ttl):
    with pytest.raises(ValueError, match="TTL"):
        observe_headers(pool(), headers(), NOW, ttl=ttl)


@pytest.mark.parametrize("values", [{"remaining": True}, {"remaining": -1}, {"remaining": 1.5},
    {"remaining": 11, "limit": 10}, {"remaining": "0"}, {"limit": 2**63}, {"reference": ""},
    {"reset_at": NOW}, {"reset_at": NOW + timedelta(days=3), "reset_reference": "synthetic"}])
def test_explicit_observation_rejects_invalid_values(values):
    with pytest.raises(ValueError):
        observe_values(pool(), NOW, **(dict(reference="synthetic-quota-api") | values))


def test_corrupt_payload_errors_never_include_raw_data():
    with pytest.raises(ValueError) as error:
        QuotaObservation.from_dict(observed().to_dict() | {"observed_at": "secret-marker"})
    assert "secret-marker" not in str(error.value)
