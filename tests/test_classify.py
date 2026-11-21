"""Classifier tests.

Phase 0 recorded no 429 (docs/probe-results.md). The 503 body below is copied
from the Phase 0 recordings; the 429 bodies are synthetic, shaped like Google's
public RPC error format (QuotaFailure + RetryInfo).
"""

import json

import pytest

from quota_router.classify import classify_429, parse_error_body

# Recorded in Phase 0 (every 503 had this array shape).
RECORDED_503 = ('[{"error": {"code": 503, "message": "This model is currently experiencing high '
                'demand. Spikes in demand are usually temporary. Please try again later.", '
                '"status": "UNAVAILABLE"}}]')


def google_429(quota_id: str, metric: str, retry: str | None = "41s", message: str = "") -> dict:
    details = [{"@type": "type.googleapis.com/google.rpc.QuotaFailure",
                "violations": [{"quotaMetric": metric, "quotaId": quota_id,
                                "quotaDimensions": {"location": "global", "model": "gemini-x"},
                                "quotaValue": "15"}]},
               {"@type": "type.googleapis.com/google.rpc.Help", "links": []}]
    if retry:
        details.append({"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": retry})
    return {"error": {"code": 429, "message": message or "You exceeded your current quota.",
                      "status": "RESOURCE_EXHAUSTED", "details": details}}


RPD = google_429("GenerateRequestsPerDayPerProjectPerModel-FreeTier",
                 "generativelanguage.googleapis.com/generate_content_free_tier_requests", "12s")
RPM = google_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier",
                 "generativelanguage.googleapis.com/generate_content_free_tier_requests", "41.5s")
TPM = google_429("GenerateContentInputTokensPerModelPerMinute-FreeTier",
                 "generativelanguage.googleapis.com/generate_content_free_tier_input_token_count", "7s")


def test_parse_recorded_array_body():
    err = parse_error_body(RECORDED_503)
    assert err["code"] == 503 and err["status"] == "UNAVAILABLE"
    assert parse_error_body(RECORDED_503.encode()) == err
    assert classify_429(RECORDED_503).kind == "unknown"


@pytest.mark.parametrize("body, kind, delay", [(RPD, "rpd", 12.0), (RPM, "rpm", 41.5), (TPM, "tpm", 7.0)])
@pytest.mark.parametrize("shape", ["object", "array", "text"])
def test_known_429s_in_every_shape(body, kind, delay, shape):
    b = {"object": body, "array": [body], "text": json.dumps([body])}[shape]
    c = classify_429(b)
    assert (c.kind, c.retry_after_s) == (kind, delay)


def test_per_day_wins_over_per_minute():
    body = google_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "m")
    body["error"]["details"][0]["violations"].append(
        {"quotaId": "GenerateRequestsPerDayPerProjectPerModel-FreeTier", "quotaMetric": "m"})
    assert classify_429(body).kind == "rpd"


def test_message_fallback_and_retry_from_message():
    body = {"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
                      "message": "Quota exceeded: requests per day. Please retry in 33.2s."}}
    c = classify_429(body)
    assert (c.kind, c.retry_after_s) == ("rpd", 33.2)


def test_unknown_bodies():
    for body in ({"error": {"code": 429, "message": "Resource has been exhausted."}},
                 "<html>Too Many Requests</html>", b"", None, [], {"weird": True}, [1, "x"]):
        c = classify_429(body)
        assert c.kind == "unknown" and c.retry_after_s is None


def test_retry_after_header_used_when_body_has_no_delay():
    body = google_429("GenerateRequestsPerMinutePerProjectPerModel-FreeTier", "m", retry=None)
    assert classify_429(body, {"Retry-After": "9"}).retry_after_s == 9.0
    assert classify_429(body, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}).retry_after_s is None


def violations_body(violations) -> dict:
    return {"error": {"code": 429, "details": [
        {"@type": "type.googleapis.com/google.rpc.QuotaFailure", "violations": violations}]}}


@pytest.mark.parametrize("body, headers", [
    (violations_body(1), None),                       # truthy, not a list
    (violations_body({"quotaId": "PerDay"}), None),   # a dict, not a list
    ({"error": {"code": 429, "details": "x"}}, {"Retry-After": "²"}),  # isdigit() but not float()
    ("[" * 100_000 + "]" * 100_000, None),            # nesting deep enough to hit the recursion limit
    ({"error": {"code": 429, "details": [{"@type": "google.rpc.RetryInfo", "retryDelay": "9" * 400 + "s"}]}}, None),
    ({"error": {"code": 429, "message": "Please retry in " + "9" * 400 + "s"}}, None),
    ({"error": {"code": 429, "message": "Please retry in 999999s"}}, {"Retry-After": "99999999"}),
    ({"error": {"code": 429}}, {1: 2}),
])
def test_malformed_bodies_never_raise_and_give_no_usable_delay(body, headers):
    c = classify_429(body, headers)
    assert c.kind == "unknown" and c.retry_after_s is None
