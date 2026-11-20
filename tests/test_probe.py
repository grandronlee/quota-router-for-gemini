"""Offline tests for scripts/probe.py verdicts and parsing. No network.

Run: python3 -I -m unittest discover -s tests -v
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


if __name__ == "__main__":
    unittest.main()
