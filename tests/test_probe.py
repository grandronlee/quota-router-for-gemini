"""Offline tests for scripts/probe.py verdicts and parsing. No network.

Run: python3 -I -m unittest discover -s tests -p test_probe.py -v
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import probe as p  # noqa: E402

OK_STREAM = dict(ok=True, status=200, saw_done=True, finish_reason="stop",
                 content_chars=40, has_marker=True, tool_calls=[])


def tool_call(arguments: str, sig: int = 0) -> dict:
    return {"has_id": True, "name": "get_weather", "arguments": arguments,
            "thought_signature_chars": sig}


class FakeResponse(list):
    status = 200


class FakeBrokenResponse:
    """Sends headers and one chunk, then the read times out."""
    status = 200

    def __iter__(self):
        yield b'data: {"choices":[{"index":0,"delta":{"content":"Par"}}]}\n'
        raise TimeoutError("The read operation timed out")


class VerdictTests(unittest.TestCase):
    def v(self, kind, **over):
        return p.verdict(kind, {**OK_STREAM, **over})[0]

    # review round 1
    def test_truncated_stream_fails(self):
        self.assertEqual(self.v("stream", saw_done=False, finish_reason=None), "FAIL")

    def test_empty_reply_fails(self):
        self.assertEqual(self.v("nonstream", content_chars=0), "FAIL")

    def test_tool_call_with_missing_or_broken_arguments_fails(self):
        for args in ("", "{}", '{"ci', "[]"):
            with self.subTest(args=args):
                self.assertEqual(self.v("tools", tool_calls=[tool_call(args)]), "FAIL")

    # review round 2
    def test_city_null_or_wrong_fails(self):
        for args in ('{"city": null}', '{"city": "London"}', '{"city": 5}', '{"city": " "}'):
            with self.subTest(args=args):
                self.assertEqual(self.v("tools", tool_calls=[tool_call(args)]), "FAIL")

    def test_city_matches_case_insensitively(self):
        self.assertEqual(self.v("tools", tool_calls=[tool_call('{"city":"paris"}')]), "PASS")

    def test_roundtrip_that_ignores_the_tool_result_fails(self):
        self.assertEqual(self.v("roundtrip", has_marker=False), "FAIL")
        # older raw runs have no has_marker field: not proven, so not a pass
        r = dict(OK_STREAM)
        del r["has_marker"]
        self.assertEqual(p.verdict("roundtrip", r)[0], "FAIL")

    def test_roundtrip_quoting_the_marker_passes(self):
        self.assertEqual(self.v("roundtrip"), "PASS")

    def test_finish_reason_length_fails_even_with_done(self):
        self.assertEqual(self.v("stream", finish_reason="length"), "FAIL")
        self.assertEqual(self.v("nonstream", finish_reason="content_filter"), "FAIL")

    def test_read_timeout_after_200_is_unavailable(self):
        r = p.Client("k" * 40, p.Scrubber("k" * 40))._read_stream(FakeBrokenResponse(), 0.0)
        self.assertEqual(p.verdict("stream", r)[0], "UNAVAILABLE")

    def test_in_stream_errors(self):
        self.assertEqual(p.verdict("stream", {"ok": False, "status": 200,
                                              "error_event": {"code": 503}})[0], "UNAVAILABLE")
        self.assertEqual(p.verdict("stream", {"ok": False, "status": 200,
                                              "error_event": {"code": 400}})[0], "FAIL")

    def test_http_statuses(self):
        self.assertEqual(p.verdict("tools", {"ok": False, "status": 503})[0], "UNAVAILABLE")
        self.assertEqual(p.verdict("tools", {"ok": False, "status": 429})[0], "UNAVAILABLE")
        self.assertEqual(p.verdict("tools", {"ok": False, "status": 400})[0], "FAIL")
        self.assertEqual(p.verdict("tools", {"ok": False, "status": None,
                                             "error": "TimeoutError: x"})[0], "UNAVAILABLE")


class ExperimentVerdictTests(unittest.TestCase):
    def exp(self, signature, source_sig, **result):
        return p.experiment_verdict({"signature": signature,
                                     "source_signature_chars": source_sig,
                                     "result": {**OK_STREAM, **result}})[0]

    def test_drop_without_a_source_signature_is_inconclusive(self):
        self.assertEqual(self.exp("drop", 0), "INCONCLUSIVE")
        self.assertEqual(self.exp("keep", 0), "INCONCLUSIVE")

    def test_with_a_source_signature_uses_the_reply_verdict(self):
        self.assertEqual(self.exp("drop", 300), "PASS")
        self.assertEqual(self.exp("keep", 300, has_marker=False), "FAIL")
        self.assertEqual(self.exp("dummy", 0), "PASS")

    def test_rejection_shows_googles_message(self):
        e = {"signature": "drop", "source_signature_chars": 196,
             "result": {"ok": False, "status": 400, "body": [
                 {"error": {"code": 400, "message": "missing a thought_signature"}}]}}
        self.assertEqual(p.experiment_verdict(e),
                         ("FAIL", "HTTP 400: missing a thought_signature"))

    def test_skipped(self):
        self.assertEqual(p.experiment_verdict({"signature": "keep", "skipped": "x"})[0],
                         "NOT RUN")


class StreamAndPrivacyTests(unittest.TestCase):
    LINES = [
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"id":"c1",'
        b'"function":{"name":"get_weather","arguments":"{\\"city\\":"},'
        b'"extra_content":{"google":{"thought_signature":"SIGSIG"}}}]}}]}\n',
        b'data: {"choices":[{"index":0,"delta":{"tool_calls":[{"index":0,"function":'
        b'{"arguments":"\\"Paris\\"}"}}]},"finish_reason":"stop"}],'
        b'"usage":{"prompt_tokens":5,"completion_tokens":3,"total_tokens":20}}\n',
        b"data: [DONE]\n",
    ]

    def setUp(self):
        c = p.Client("k" * 40, p.Scrubber("k" * 40))
        self.res = c._read_stream(FakeResponse(self.LINES), 0.0)

    def test_streamed_tool_call_is_assembled(self):
        self.assertEqual(p.verdict("tools", self.res)[0], "PASS")
        self.assertEqual(self.res["tool_calls"][0]["thought_signature_chars"], 6)

    def test_replay_carries_signature_only_when_asked(self):
        def extra(sig):
            return p.roundtrip_body("m", self.res, sig)["messages"][1]["tool_calls"][0] \
                .get("extra_content")
        self.assertEqual(extra("keep"), {"google": {"thought_signature": "SIGSIG"}})
        self.assertIsNone(extra("drop"))
        self.assertEqual(extra("dummy")["google"]["thought_signature"], p.DUMMY_SIGNATURE)

    def test_tool_result_marker_is_not_in_the_prompt(self):
        self.assertNotIn(p.TOOL_MARKER, p.TOOL_PROMPT)
        self.assertIn(p.TOOL_MARKER, json.dumps(p.TOOL_RESULT))

    def test_signature_is_never_saved(self):
        saved = json.dumps(p.strip_private({"probes": {"m": {"tools": self.res}}}))
        self.assertNotIn("SIGSIG", saved)

    def test_scrubber_removes_key_and_project_numbers(self):
        key = "AIza" + "x" * 35
        s = p.Scrubber(key).text(f'{key} projects/12345 "consumer": "projects/9"')
        self.assertNotIn(key, s)
        self.assertNotIn("12345", s)
        self.assertNotIn("projects/9", s)


class ReportTests(unittest.TestCase):
    RUN = {"started_pt": "2026-11-20T05:00:00-08:00", "requests_sent": 1,
           "mapping": [{"display": "M", "use": "route", "id": "m", "candidates": ["m"]}],
           "openai_models": {"summary": "1 models"}, "probes": {}, "errors_429": []}

    def test_manual_section_survives_regeneration(self):
        import tempfile
        path = Path(tempfile.mkdtemp()) / "r.md"
        p.write_report(path, [self.RUN])
        path.write_text(path.read_text() + "\n" + p.MANUAL_HEADING + "\n\nZoo Code: PASS\n")
        p.write_report(path, [self.RUN])
        p.write_report(path, [self.RUN])
        text = path.read_text()
        self.assertEqual(text.count(p.MANUAL_HEADING), 1)
        self.assertTrue(text.rstrip().endswith("Zoo Code: PASS"))
        self.assertEqual(text.count("# Phase 0 probe results"), 1)


def stream_call(name: str, args: dict, sig: str = "SIG-REAL", text: str = "") -> dict:
    """A passing streamed tool-call result, with the in-memory parts a real read keeps."""
    raw = {"id": f"call_{name}", "name": name, "arguments": json.dumps(args),
           "_extra": {"google": {"thought_signature": sig}}}
    return {**OK_STREAM, "content_chars": len(text), "has_marker": False,
            "tool_calls": [p.summarize_tool_call(raw)], "_text": text, "_calls": [raw]}


def answer(text: str) -> dict:
    return {**OK_STREAM, "content_chars": len(text), "tool_calls": [], "_text": text, "_calls": []}


SOURCE = stream_call("get_weather", {"city": "Paris"}, sig="SIG-SOURCE")
STEP2 = stream_call("get_forecast", {"report_id": p.TOOL_MARKER}, sig="SIG-TARGET")


class HandoffVerdictTests(unittest.TestCase):
    def v(self, **over):
        e = {"target": "t", "signature": "dummy", "source_model": "s", "step2": STEP2,
             "step3": answer(f"It will be sunny ({p.FORECAST_MARKER})."), "step3_has_marker": True}
        return p.handoff_verdict({**e, **over})

    def test_pass(self):
        self.assertEqual(self.v(), ("PASS", ""))

    def test_400_at_step_3_is_a_signature_failure(self):
        r = {"ok": False, "status": 400, "body": [{"error": {"message": "thought_signature bad"}}]}
        self.assertEqual(self.v(step3=r)[0], "FAIL")
        self.assertIn("step 3: HTTP 400: thought_signature", self.v(step3=r)[1])

    def test_503_or_timeout_is_unavailable_not_fail(self):
        for r in ({"ok": False, "status": 503}, {"ok": False, "status": None,
                                                  "error": "TimeoutError: timed out"}):
            with self.subTest(r=r):
                self.assertEqual(self.v(step2=r, step3=None)[0], "UNAVAILABLE")

    def test_step_2_must_be_its_own_get_forecast_call(self):
        for bad in (answer("no call"), stream_call("get_weather", {"city": "Paris"}),
                    stream_call("get_forecast", {"report_id": "other"})):
            with self.subTest(bad=bad["tool_calls"]):
                self.assertEqual(self.v(step2=bad, step3=None)[0], "FAIL")

    def test_step_3_must_quote_the_second_result_without_calling_again(self):
        self.assertEqual(self.v(step3_has_marker=False)[0], "FAIL")
        self.assertEqual(self.v(step3=STEP2)[0], "FAIL")

    def test_skipped(self):
        self.assertEqual(self.v(skipped="no confirmed model ID")[0], "NOT RUN")


class HandoffMessageTests(unittest.TestCase):
    def sigs(self, msgs):
        return [tc.get("extra_content", {}).get("google", {}).get("thought_signature")
                for m in msgs for tc in m.get("tool_calls") or []]

    def test_placeholder_replaces_only_the_source_call(self):
        msgs = p.handoff_messages(SOURCE, "dummy", own=STEP2)
        self.assertEqual(self.sigs(msgs), [p.DUMMY_SIGNATURE, "SIG-TARGET"])
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "tool", "assistant", "tool"])
        self.assertIn(p.TOOL_MARKER, msgs[2]["content"])
        self.assertIn(p.FORECAST_MARKER, msgs[4]["content"])
        self.assertEqual(msgs[4]["tool_call_id"], "call_get_forecast")

    def test_keep_sends_the_source_signature(self):
        self.assertEqual(self.sigs(p.handoff_messages(SOURCE, "keep")), ["SIG-SOURCE"])


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def chat(self, label, body):
        self.sent.append((label, body))
        return self.replies.pop(0)


MAPPING = [{"display": d, "id": d.lower().replace(" ", "-")} for d in
           (p.HANDOFF_SOURCE, "Gemini 4 Hydrogen", "Gemini 3 Flash")]


class RunHandoffTests(unittest.TestCase):
    def run_it(self, replies, wanted=frozenset(), sigs=("dummy",)):
        client = FakeClient(replies)
        run = {"handoffs": []}
        p.run_handoffs(client, MAPPING, set(wanted), sigs, run, lambda: None)
        return client, run

    def test_one_source_call_then_two_per_target(self):
        good = [STEP2, answer(f"ok {p.FORECAST_MARKER}")]
        client, run = self.run_it([SOURCE, *good, *good],
                                  wanted={p.key_of("Gemini 4 Hydrogen"), p.key_of("Gemini 3 Flash")})
        self.assertEqual(len(client.sent), 5)
        self.assertEqual([p.handoff_verdict(e)[0] for e in run["handoffs"]], ["PASS", "PASS"])
        self.assertEqual(client.sent[1][1]["model"], "gemini-4-hydrogen")

    def test_failed_step_2_skips_step_3(self):
        client, run = self.run_it([SOURCE, {"ok": False, "status": 503}],
                                  wanted={p.key_of("Gemini 4 Hydrogen")})
        self.assertEqual(len(client.sent), 2)
        self.assertEqual(p.handoff_verdict(run["handoffs"][0])[0], "UNAVAILABLE")

    def test_failed_source_sends_nothing_else(self):
        client, run = self.run_it([{"ok": False, "status": 503}])
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(run["handoffs"], [])

    def test_unconfirmed_target_is_skipped_without_a_request(self):
        client, run = self.run_it([SOURCE], wanted={p.key_of("Gemini 2.5 Flash")})
        self.assertEqual(len(client.sent), 1)
        self.assertEqual(p.handoff_verdict(run["handoffs"][0])[0], "NOT RUN")

    def test_saved_run_has_no_signatures_and_renders(self):
        import tempfile
        _, run = self.run_it([SOURCE, STEP2, answer(f"ok {p.FORECAST_MARKER}")],
                             wanted={p.key_of("Gemini 4 Hydrogen")})
        saved = json.dumps(p.strip_private(run))
        self.assertNotIn("SIG-", saved)
        full = {**ReportTests.RUN, **json.loads(saved)}
        path = Path(tempfile.mkdtemp()) / "r.md"
        p.write_report(path, [full])
        self.assertIn("| `gemini-4-hydrogen` | placeholder | PASS |", path.read_text())


if __name__ == "__main__":
    unittest.main()
