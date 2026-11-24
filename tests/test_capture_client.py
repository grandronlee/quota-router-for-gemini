"""Offline capture tests. HTTP requests stay on 127.0.0.1.

Run: python3 -I -m unittest discover -s tests -p test_capture_client.py -v
"""

import csv
import datetime as dt
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.client import HTTPResponse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import ProxyHandler, Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import capture_client as capture  # noqa: E402

TOOL = {
    "type": "function",
    "function": {
        "name": "attempt_completion",
        "parameters": {
            "properties": {
                "result": {"type": "string"},
                "details": {"type": "string"},
                "count": {"type": "integer"},
                "optional": {"type": "string"},
            },
            "required": ["result", "details", "count", "missing"],
        },
    },
}
ZERO_USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}


class MemoryConnection:
    """The stream interface used by the handler when TCP is sandboxed."""

    def __init__(self, incoming):
        self.incoming = incoming
        self.outgoing = bytearray()

    def makefile(self, mode, *args):
        return io.BytesIO(self.incoming)

    def sendall(self, data):
        self.outgoing.extend(data)

    def settimeout(self, timeout):
        """StreamRequestHandler.setup sets the handler's socket timeout."""


class CaptureHTTPTests(unittest.TestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.out_dir = Path(tmp) / "capture"
        secrets = {
            "PROXY_TOKEN": "offline-proxy-fixture",
            "GEMINI_API_KEY": "offline-gemini-fixture",
        }
        self.enterContext(patch.dict(os.environ, secrets))
        models = ["gemini-auto", "another-model"]
        try:
            server = capture.make_server("127.0.0.1", 0, self.out_dir, models)
        except PermissionError:
            # Exercise the same wire protocol if this environment denies loopback sockets.
            self.opener = None
            self.server = SimpleNamespace(
                out_dir=self.out_dir, models=models, pattern=capture.scrub_pattern(secrets.values()),
            )
            return
        self.server = self.enterContext(server)
        worker = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01})
        worker.start()
        self.addCleanup(worker.join)
        self.addCleanup(self.server.shutdown)
        self.opener = build_opener(ProxyHandler({}))
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def request(self, path, body=None, headers=None):
        data = body
        if body is not None and not isinstance(body, bytes):
            data = json.dumps(body).encode()
        if self.opener is None:
            return self.memory_request(path, data, headers or {})
        request = Request(self.url + path, data=data, headers=headers or {})
        try:
            response = self.opener.open(request, timeout=3)
        except HTTPError as error:
            response = error
        with response:
            return response.code, response.headers.get_content_type(), response.read()

    def memory_request(self, path, data, headers):
        method = "GET" if data is None else "POST"
        payload = data or b""
        head = [f"{method} {path} HTTP/1.1", f"Content-Length: {len(payload)}"]
        head.extend(f"{name}: {value}" for name, value in headers.items())
        connection = MemoryConnection("\r\n".join([*head, "", ""]).encode() + payload)
        capture.CaptureHandler(connection, ("127.0.0.1", 0), self.server)
        with HTTPResponse(MemoryConnection(connection.outgoing)) as response:
            response.begin()
            return response.status, response.headers.get_content_type(), response.read()

    def check_tool_call(self, tool_call):
        self.assertTrue(tool_call["id"].startswith("call_"))
        self.assertEqual(tool_call["type"], "function")
        self.assertEqual(tool_call["function"]["name"], "attempt_completion")
        self.assertEqual(json.loads(tool_call["function"]["arguments"]), {
            "result": "capture ok", "details": "capture ok",
        })

    def test_nonstream_completions(self):
        for tools, finish in (([], "stop"), ([TOOL], "tool_calls")):
            with self.subTest(finish=finish):
                status, content_type, data = self.request("/v1/chat/completions", {
                    "model": "another-model", "tools": tools, "stream": False,
                })
                self.assertEqual((status, content_type), (200, "application/json"))
                reply = json.loads(data)
                self.assertEqual(reply["object"], "chat.completion")
                self.assertEqual(reply["model"], "another-model")
                self.assertEqual(reply["usage"], ZERO_USAGE)
                choice, = reply["choices"]
                self.assertEqual(choice["finish_reason"], finish)
                message = choice["message"]
                self.assertEqual(message["role"], "assistant")
                if tools:
                    self.assertIsNone(message["content"])
                    tool_call, = message["tool_calls"]
                    self.check_tool_call(tool_call)
                else:
                    self.assertEqual(message["content"], "capture ok")
                    self.assertNotIn("tool_calls", message)

    def test_stream_completions(self):
        for tools, finish in (([], "stop"), ([TOOL], "tool_calls")):
            with self.subTest(finish=finish):
                status, content_type, data = self.request("/chat/completions", {
                    "model": "gemini-auto", "tools": tools, "stream": True,
                })
                self.assertEqual((status, content_type), (200, "text/event-stream"))
                *events, done, tail = data.decode().split("\n\n")
                self.assertEqual((done, tail), ("data: [DONE]", ""))
                self.assertTrue(all(event.startswith("data: ") for event in events))
                chunks = [json.loads(event.removeprefix("data: ")) for event in events]
                self.assertEqual(len({chunk["id"] for chunk in chunks}), 1)
                self.assertTrue(all(chunk["object"] == "chat.completion.chunk" for chunk in chunks))
                role, answer, final = [chunk["choices"][0] for chunk in chunks]
                self.assertEqual(role["delta"], {"role": "assistant"})
                self.assertIsNone(role["finish_reason"])
                self.assertIsNone(answer["finish_reason"])
                if tools:
                    tool_call, = answer["delta"]["tool_calls"]
                    self.assertEqual(tool_call["index"], 0)
                    self.check_tool_call(tool_call)
                else:
                    self.assertEqual(answer["delta"], {"content": "capture ok"})
                self.assertEqual(final["delta"], {})
                self.assertEqual(final["finish_reason"], finish)
                self.assertEqual(chunks[-1]["usage"], ZERO_USAGE)

    def test_models_and_unknown_paths(self):
        for path in ("/v1/models", "/models"):
            status, content_type, data = self.request(path)
            self.assertEqual((status, content_type), (200, "application/json"))
            reply = json.loads(data)
            self.assertEqual(reply["object"], "list")
            self.assertEqual([model["id"] for model in reply["data"]], ["gemini-auto", "another-model"])
        for body in (None, {}):
            status, content_type, data = self.request("/missing", body)
            self.assertEqual((status, content_type), (404, "application/json"))
            self.assertIn("error", json.loads(data))
        self.assertFalse(self.out_dir.exists())

    def test_bad_json_is_a_json_error(self):
        for body in (b'{"model":', b"\xff", b'{"temperature":NaN}'):
            with self.subTest(body=body):
                status, content_type, data = self.request("/v1/chat/completions", body)
                self.assertEqual((status, content_type), (400, "application/json"))
                self.assertIn("error", json.loads(data))
        self.assertFalse(self.out_dir.exists())

    def test_capture_permissions_shape_markers_and_scrub(self):
        key = "AIza" + "0123456789abcdefghij_"
        proxy_token = os.environ["PROXY_TOKEN"]
        env_key = os.environ["GEMINI_API_KEY"]
        system = f"  You are a capture client in {proxy_token} mode."
        developer = "You are using test settings.\n<slug>ask</slug>"
        user = f"MODE: debug\nKeep {key}, {proxy_token}, and {env_key} private."
        body = {
            "model": "gemini-auto", "stream": False, "reasoning_effort": "high",
            "max_tokens": 12, "max_completion_tokens": 24, "temperature": 0.25,
            "tool_choice": {"type": "function", "function": {"name": "attempt_completion"}},
            "tools": [TOOL],
            "messages": [
                {"role": "system", "content": system},
                {"role": "developer", "content": [{"type": "text", "text": developer}]},
                {"role": "user", "content": "Older mode line"},
                {"role": "tool", "content": None},
                {"role": "user", "content": [
                    {"type": "text", "text": user},
                    {"type": "image_url", "image_url": {"url": "inline image"}},
                    {"type": "unknown", "text": "ignored"},
                    {"type": "text", "text": 42},
                    {"type": "text", "text": " End."},
                ]},
            ],
        }
        authorization = "Bearer offline-header-only-fixture"
        previous_umask = os.umask(0)
        try:
            status, _, _ = self.request("/v1/chat/completions", body, {
                "Authorization": authorization, "User-Agent": "OfflineClient/1.0",
                "X-Extra": "extra-header-fixture",
            })
        finally:
            os.umask(previous_umask)
        self.assertEqual(status, 200)
        path, = self.out_dir.iterdir()
        self.assertEqual(stat.S_IMODE(self.out_dir.stat().st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        raw = path.read_text()
        for secret in (authorization, "Authorization", "extra-header-fixture", key, env_key, proxy_token):
            self.assertNotIn(secret, raw)
        record = json.loads(raw)
        self.assertEqual(set(record), {"written_by", "received_at", "user_agent", "shape", "markers", "body"})
        self.assertEqual(record["written_by"], "scripts/capture_client.py")
        self.assertEqual(dt.datetime.fromisoformat(record["received_at"]).utcoffset(), dt.timedelta(0))
        self.assertEqual(record["user_agent"], "OfflineClient/1.0")
        self.assertEqual(record["shape"], {
            "model": "gemini-auto", "stream": False, "reasoning_effort": "high",
            "max_tokens": 12, "max_completion_tokens": 24, "temperature": 0.25,
            "tool_choice": body["tool_choice"], "keys": sorted(body),
            "n_tools": 1, "tool_names": ["attempt_completion"],
            "roles": ["system", "developer", "user", "tool", "user"],
            "chars_by_role": {"system": len(system), "developer": len(developer),
                              "user": len("Older mode line" + user + " End."), "tool": 0},
            "system_chars": len(system + "\n" + developer), "last_user_chars": len(user + " End."),
            "has_image": True, "has_tool_result": True,
        })
        self.assertEqual(record["markers"], {
            "system_you_are": ["You are a capture client in <redacted> mode.",
                               "You are using test settings."],
            "system_mode_lines": ["  You are a capture client in <redacted> mode.", "<slug>ask</slug>"],
            "user_mode_lines": ["MODE: debug"],
        })
        body["messages"][0]["content"] = "  You are a capture client in <redacted> mode."
        body["messages"][-1]["content"][0]["text"] = "MODE: debug\nKeep <redacted>, <redacted>, and <redacted> private."
        self.assertEqual(record["body"], body)

    def raw_request(self, raw):
        """Send raw request bytes; returns (status, parsed JSON)."""
        if self.opener is None:
            connection = MemoryConnection(raw)
            capture.CaptureHandler(connection, ("127.0.0.1", 0), self.server)
            reply = bytes(connection.outgoing)
        else:
            import socket
            with socket.create_connection(("127.0.0.1", self.server.server_port), timeout=3) as sock:
                sock.sendall(raw)
                sock.shutdown(socket.SHUT_WR)
                reply = b"".join(iter(lambda: sock.recv(65536), b""))
        with HTTPResponse(MemoryConnection(reply)) as response:
            response.begin()
            return response.status, json.loads(response.read())

    def test_content_length_limits(self):
        cases = ((str(10**12), 413), ("-5", 400), ("abc", 400))
        for length, expected in cases:
            with self.subTest(length=length):
                raw = (f"POST /v1/chat/completions HTTP/1.1\r\nContent-Length: {length}\r\n\r\n").encode()
                status, reply = self.raw_request(raw)
                self.assertEqual(status, expected)
                self.assertIn("error", reply)
        self.assertFalse(self.out_dir.exists())

    def test_malformed_shapes_still_get_captured(self):
        for body in ([], {"messages": "wrong", "tools": {}}, {
            "messages": [None, {"role": []}, {"role": "user", "content": None},
                         {"role": "user", "content": [{"type": []}, None]}],
            "tools": [None, {"type": "function", "function": []}],
        }):
            with self.subTest(body=body):
                status, _, data = self.request("/v1/chat/completions", body)
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(data)["choices"][0]["message"]["content"], "capture ok")
        self.assertEqual(len(list(self.out_dir.iterdir())), 3)


class InMemoryCaptureHTTPTests(CaptureHTTPTests):
    """The same HTTP tests through the fallback used when loopback binding is denied, so that
    path can't break unseen (found in review, 2026-11-23: it lacked `settimeout`)."""

    def setUp(self):
        with patch.object(capture, "make_server", side_effect=PermissionError):
            super().setUp()
        self.assertIsNone(self.opener)


class CleanupInvariantTests(unittest.TestCase):
    """The script deletes, reads or points to only the files it wrote (found in review,
    2026-11-24: the reminder said "Delete <--out folder>", which could be a home folder)."""

    DECOYS = (
        "notes.txt", "0001-notes.json", "2024-plan.json", "12-report.json",
        "0001-20261124T034359607388Z.json.bak", "0001-20261124T034359607388.json",
        "0001-2026112T034359607388Z.json", "x0001-20261124T034359607388Z.json",
        "001-20261124T034359607388Z.json",
    )

    def make_folder(self, tmp):
        folder = Path(tmp) / "mine"
        folder.mkdir(mode=0o700)
        folder.chmod(0o700)
        for name in self.DECOYS:
            (folder / name).write_text("keep")
        (folder / "sub").mkdir()
        received_at = dt.datetime(2026, 11, 24, 3, 43, 59, 607388, tzinfo=dt.timezone.utc)
        record = capture.capture_record({"model": "gemini-auto"}, None, received_at,
                                        capture.scrub_pattern())
        written = [capture.write_capture(folder, record, received_at) for _ in range(3)]
        return folder, written

    def test_names_written_are_exactly_the_names_recognised(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, written = self.make_folder(tmp)
            found = sorted(path for _, path in capture.capture_paths(folder))
            self.assertEqual(found, sorted(written))

    def test_clean_deletes_only_captures_and_keeps_a_non_empty_folder(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, written = self.make_folder(tmp)
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(capture.main(["--out", str(folder), "--clean"]), 0)
            self.assertIn("deleted 3 capture file(s)", out.getvalue())
            self.assertEqual(sorted(p.name for p in folder.iterdir()), sorted([*self.DECOYS, "sub"]))
            self.assertTrue(all((folder / name).read_text() == "keep" for name in self.DECOYS))

    def own_record(self):
        return capture.capture_record({}, None, dt.datetime.now(dt.timezone.utc), capture.scrub_pattern())

    def test_clean_removes_the_default_folder_left_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "capture"
            capture.write_capture(folder, self.own_record(), dt.datetime.now(dt.timezone.utc))
            with patch.object(capture, "DATA_DIR", folder), redirect_stdout(io.StringIO()):
                capture.main(["--clean"])
            self.assertFalse(folder.exists())

    def test_clean_never_removes_a_custom_folder(self):
        # Found in Codex review, 2026-11-24: an empty folder another tool made was removed.
        with tempfile.TemporaryDirectory() as tmp:
            empty = Path(tmp) / "theirs"
            empty.mkdir(mode=0o700)
            used = Path(tmp) / "used"
            capture.write_capture(used, self.own_record(), dt.datetime.now(dt.timezone.utc))
            with redirect_stdout(io.StringIO()):
                capture.main(["--out", str(empty), "--clean"])
                capture.main(["--out", str(used), "--clean"])
            self.assertTrue(empty.is_dir())
            self.assertTrue(used.is_dir())
            self.assertEqual(list(used.iterdir()), [])

    def test_foreign_file_with_a_capture_name_is_neither_read_nor_deleted(self):
        # Found in Codex review, 2026-11-24: the name alone decided.
        with tempfile.TemporaryDirectory() as tmp:
            folder, written = self.make_folder(tmp)
            foreign = folder / "0099-20261124T034359607388Z.json"
            foreign.write_text('{"shape": "theirs"}')
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                capture.main(["--out", str(folder), "--summary"])
            self.assertNotIn(foreign.name, out.getvalue())
            with redirect_stdout(io.StringIO()):
                capture.main(["--out", str(folder), "--clean"])
            self.assertEqual(foreign.read_text(), '{"shape": "theirs"}')
            self.assertTrue(all(not path.exists() for path in written))

    def test_capture_named_symlink_is_ignored(self):
        # Found in Codex review, 2026-11-24: --summary followed it to its target.
        with tempfile.TemporaryDirectory() as tmp:
            folder, written = self.make_folder(tmp)
            target = Path(tmp) / "secret.json"
            target.write_text(written[0].read_text())  # even a real capture's content
            link = folder / "0098-20261124T034359607388Z.json"
            link.symlink_to(target)
            self.assertNotIn(link, [path for _, path in capture.capture_paths(folder)])
            with redirect_stdout(io.StringIO()):
                capture.main(["--out", str(folder), "--clean"])
            self.assertTrue(link.is_symlink())
            self.assertTrue(target.exists())

    def test_reminder_round_trips_through_the_shell(self):
        # Found in Codex review, 2026-11-24: spaces split the path; a relative path depended on
        # where the command was pasted.
        import shlex
        with tempfile.TemporaryDirectory() as tmp:
            spaced = Path(tmp) / "my captures"
            for given in (spaced, Path(os.path.relpath(spaced))):
                with self.subTest(given=given):
                    words = shlex.split(capture.reminder(given).split(": ", 1)[1])
                    self.assertEqual(words[-2:], ["--out", str(spaced)])
            self.assertNotIn("--out", capture.reminder(capture.DATA_DIR))

    def test_clean_refuses_a_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "target"
            capture.write_capture(target, {}, dt.datetime.now(dt.timezone.utc))
            link = Path(tmp) / "link"
            link.symlink_to(target)
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                capture.main(["--out", str(link), "--clean"])
            self.assertEqual(len(list(target.iterdir())), 1)

    def test_summary_ignores_files_it_did_not_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder, written = self.make_folder(tmp)
            (folder / "2024-plan.json").write_text("not json")
            out = io.StringIO()
            with redirect_stdout(out), redirect_stderr(io.StringIO()):
                capture.main(["--out", str(folder), "--summary"])
            self.assertEqual(len(out.getvalue().splitlines()), 1 + len(written))


class CaptureFileTests(unittest.TestCase):
    def test_non_loopback_is_refused(self):
        for host in ("0.0.0.0", "192.0.2.10", "example.invalid", "127.0.0.2"):
            with self.subTest(host=host):
                with self.assertRaises(ValueError):
                    capture.make_server(host, 0, Path("unused"), ["gemini-auto"])
        stderr = io.StringIO()
        with redirect_stderr(stderr), self.assertRaises(SystemExit) as result:
            capture.main(["--host", "0.0.0.0"])
        self.assertEqual(result.exception.code, 2)
        self.assertIn("--host", stderr.getvalue())

    def test_sequence_and_summary(self):
        received_at = dt.datetime(2026, 11, 23, 1, 2, 3, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "captures"
            pattern = capture.scrub_pattern()
            first = capture.capture_record({
                "model": "gemini-auto", "reasoning_effort": "high", "tools": [TOOL],
                "messages": [{"role": "system", "content": "You are in test mode."},
                             {"role": "user", "content": "Private prompt"}],
            }, "OfflineClient/1.0", received_at, pattern)
            second = capture.capture_record({"model": "another-model"}, None, received_at, pattern)
            first_path = capture.write_capture(out_dir, first, received_at)
            original = first_path.read_bytes()
            second_path = capture.write_capture(out_dir, second, received_at)
            self.assertTrue(first_path.name.startswith("0001-"))
            self.assertTrue(second_path.name.startswith("0002-"))
            self.assertEqual(first_path.read_bytes(), original)
            self.assertEqual(len(list(out_dir.iterdir())), 2)

            stdout, stderr = io.StringIO(), io.StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr), patch.object(capture, "make_server") as server:
                self.assertEqual(capture.main(["--out", str(out_dir), "--summary"]), 0)
                server.assert_not_called()
            rows = list(csv.reader(io.StringIO(stdout.getvalue()), delimiter="\t"))
            self.assertEqual(rows[1:], [
                [first_path.name, "OfflineClient/1.0", "gemini-auto", "", "1", "2", "21", "14", "high",
                 "False", "False", "You are in test mode.", "You are in test mode.", ""],
                [second_path.name, "", "another-model", "", "0", "0", "0", "0", "", "False", "False",
                 "", "", ""],
            ])
            self.assertNotIn("Private prompt", stdout.getvalue())
            self.assertIn(f"--clean --out {os.path.abspath(out_dir)}", stderr.getvalue())
            self.assertNotIn("Delete", stderr.getvalue())
            self.assertIn("docs/probe-results.md", stderr.getvalue())

    def test_strict_schema_string_union_is_filled(self):
        tool = {"type": "function", "function": {"name": "attempt_completion", "parameters": {
            "type": "object", "properties": {"result": {"type": ["string", "null"]}},
            "required": ["result"], "additionalProperties": False}}}
        reply = capture.build_completion({"tools": [tool]}, "chatcmpl-x", 0)
        call, = reply["choices"][0]["message"]["tool_calls"]
        self.assertEqual(json.loads(call["function"]["arguments"]), {"result": "capture ok"})

    def test_marker_limits(self):
        # A long system prompt can't crowd out the user message's mode line (review, 2026-11-23).
        system = "\n".join(f"You are in mode {index} " + "x" * 200 for index in range(21))
        found = capture.markers(system, "<slug>last</slug>", capture.scrub_pattern())
        self.assertEqual(found["system_you_are"], system.splitlines()[:3])
        self.assertEqual(found["system_mode_lines"], [line[:200] for line in system.splitlines()[:20]])
        self.assertEqual(found["user_mode_lines"], ["<slug>last</slug>"])

    def test_mode_is_a_word(self):
        found = capture.markers("Pick a model.\nModes are listed.\nModerate.\nCurrent Mode: ask",
                                "the model", capture.scrub_pattern())
        self.assertEqual(found["system_mode_lines"], ["Current Mode: ask"])
        self.assertEqual(found["user_mode_lines"], [])

    def test_existing_public_directory_is_refused_not_chmodded(self):
        received_at = dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            public = Path(tmp) / "public"
            public.mkdir()
            public.chmod(0o755)
            with self.assertRaises(OSError):
                capture.write_capture(public, {}, received_at)
            self.assertEqual(stat.S_IMODE(public.stat().st_mode), 0o755)
            self.assertEqual(list(public.iterdir()), [])
            private = Path(tmp) / "private"
            private.mkdir(mode=0o700)
            link = Path(tmp) / "link"
            link.symlink_to(private)
            with self.assertRaises(OSError):
                capture.write_capture(link, {}, received_at)
            self.assertEqual(list(private.iterdir()), [])

    def test_new_directory_is_private_under_permissive_umask(self):
        received_at = dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as tmp:
            out_dir = Path(tmp) / "a" / "capture"
            previous = os.umask(0)
            try:
                path = capture.write_capture(out_dir, {}, received_at)
            finally:
                os.umask(previous)
            self.assertEqual(stat.S_IMODE(out_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_user_agent_is_scrubbed(self):
        key = "AIza" + "0123456789abcdefghij_"
        record = capture.capture_record({}, f"Client {key}", dt.datetime.now(dt.timezone.utc),
                                        capture.scrub_pattern())
        self.assertEqual(record["user_agent"], "Client <redacted>")

    def test_dict_keys_are_scrubbed(self):
        # Found in review, 2026-11-23: keys were left as they came.
        key = "AIza" + "0123456789abcdefghij_"
        with patch.dict(os.environ, {"PROXY_TOKEN": "offline-key-fixture"}):
            pattern = capture.scrub_pattern([os.environ["PROXY_TOKEN"]])
        record = capture.capture_record(
            {"metadata": {key: 1, "offline-key-fixture": {"nested": "x"}}},
            None, dt.datetime.now(dt.timezone.utc), pattern)
        raw = json.dumps(record)
        self.assertNotIn(key, raw)
        self.assertNotIn("offline-key-fixture", raw)
        self.assertIn("<redacted>", record["body"]["metadata"])

    def test_server_interrupt_prints_reminder(self):
        stderr = io.StringIO()
        with patch.object(capture, "make_server") as make_server, redirect_stdout(io.StringIO()), redirect_stderr(stderr):
            server = make_server.return_value.__enter__.return_value
            server.server_address = ("127.0.0.1", 4010)
            server.serve_forever.side_effect = KeyboardInterrupt
            self.assertEqual(capture.main([]), 0)
        make_server.assert_called_once_with("127.0.0.1", 4010, capture.DATA_DIR, ["gemini-auto"])
        self.assertIn("capture_client.py --clean", stderr.getvalue())
        self.assertNotIn("--out", stderr.getvalue())
        self.assertNotIn("Delete", stderr.getvalue())


if __name__ == "__main__":
    unittest.main()
