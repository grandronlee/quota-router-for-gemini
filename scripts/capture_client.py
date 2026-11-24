#!/usr/bin/env python3
"""Capture OpenAI-compatible client requests on loopback, without upstream calls.

    python -I scripts/capture_client.py
    python -I scripts/capture_client.py --port 4010 --models gemini-auto
    python -I scripts/capture_client.py --summary

Point the client at http://127.0.0.1:4010/v1. Captures go to data/capture/
under the repo root by default. Use --out to choose another directory.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import shlex
import socket
import stat
import sys
import threading
import uuid
from collections.abc import Iterable, Iterator, Sequence
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from socketserver import TCPServer
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data" / "capture"
LOOPBACK_HOSTS = {"127.0.0.1": "127.0.0.1", "localhost": "127.0.0.1", "::1": "::1"}
MODELS_PATHS = {"/v1/models", "/models"}
COMPLETIONS_PATHS = {"/v1/chat/completions", "/chat/completions"}
REPLY = "capture ok"
DEFAULT_MODEL = "gemini-auto"
USAGE = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
REQUEST_FIELDS = (
    "model", "stream", "reasoning_effort", "max_tokens", "max_completion_tokens",
    "temperature", "tool_choice",
)
IMAGE_TYPES = ("image_url", "input_image", "image")
MODE_LINE = re.compile(r"\bmode\b|<slug>.*?</slug>", re.IGNORECASE)
MAX_MODE_LINES = 20
MAX_BODY_BYTES = 64 * 1024 * 1024
KEY_PATTERN = r"AIza[0-9A-Za-z_\-]{20,}"
# Exactly the names write_capture makes, so nothing else in --out is read or deleted.
CAPTURE_NAME = re.compile(r"([0-9]{4,})-[0-9]{8}T[0-9]{12}Z\.json")
CAPTURE_LOCK = threading.Lock()
# Every capture carries this, so --summary and --clean act only on files this script wrote.
WRITTEN_BY = "scripts/capture_client.py"


def as_object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def text_parts(content: Any) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        part["text"] for part in map(as_object, as_list(content))
        if part.get("type") == "text" and isinstance(part.get("text"), str)
    )


def tool_functions(request: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for tool in map(as_object, as_list(request.get("tools"))):
        function = as_object(tool.get("function"))
        if tool.get("type") == "function" and isinstance(function.get("name"), str):
            yield function


def describe_request(body: Any) -> tuple[dict[str, Any], str, str]:
    """Return shape, system text and last user text for a JSON request."""
    request = as_object(body)
    roles = []
    chars_by_role: dict[str, int] = {}
    system_messages = []
    last_user_text = ""
    has_image = False
    for message in map(as_object, as_list(request.get("messages"))):
        content = message.get("content")
        has_image |= any(
            as_object(part).get("type") in IMAGE_TYPES for part in as_list(content)
        )
        role = message.get("role")
        if not isinstance(role, str):
            continue
        text = text_parts(content)
        roles.append(role)
        chars_by_role[role] = chars_by_role.get(role, 0) + len(text)
        if role in ("system", "developer"):
            system_messages.append(text)
        elif role == "user":
            last_user_text = text

    system_text = "\n".join(system_messages)
    shape = {field: request.get(field) for field in REQUEST_FIELDS}
    shape.update(
        keys=sorted(request),
        n_tools=len(as_list(request.get("tools"))),
        tool_names=[function["name"] for function in tool_functions(request)],
        roles=roles,
        chars_by_role=chars_by_role,
        system_chars=len(system_text),
        last_user_chars=len(last_user_text),
        has_image=has_image,
        has_tool_result="tool" in chars_by_role,
    )
    return shape, system_text, last_user_text


def mode_lines(text: str, pattern: re.Pattern[str]) -> list[str]:
    """Lines naming a mode (the word `mode` or a `<slug>` tag), at most 20, each cut to 200."""
    found = [line for line in text.splitlines() if MODE_LINE.search(line)]
    return [scrub(line, pattern)[:200] for line in found[:MAX_MODE_LINES]]


def markers(system_text: str, last_user_text: str, pattern: re.Pattern[str]) -> dict[str, list[str]]:
    """Capped per source, so a long system prompt can't crowd out the user message's lines."""
    role_lines = [line.lstrip() for line in system_text.splitlines() if line.lstrip().startswith("You are")]
    return {
        "system_you_are": [scrub(line, pattern) for line in role_lines[:3]],
        "system_mode_lines": mode_lines(system_text, pattern),
        "user_mode_lines": mode_lines(last_user_text, pattern),
    }


def scrub_pattern(secrets: Iterable[str] = ()) -> re.Pattern[str]:
    literals = sorted({secret for secret in secrets if secret}, key=len, reverse=True)
    return re.compile("|".join([*(re.escape(secret) for secret in literals), KEY_PATTERN]))


def scrub(value: Any, pattern: re.Pattern[str]) -> Any:
    if isinstance(value, str):
        return pattern.sub("<redacted>", value)
    if isinstance(value, list):
        return [scrub(item, pattern) for item in value]
    if isinstance(value, dict):
        return {scrub(key, pattern): scrub(item, pattern) for key, item in value.items()}
    return value


def capture_record(
    body: Any, user_agent: str | None, received_at: dt.datetime, pattern: re.Pattern[str],
) -> dict[str, Any]:
    shape, system_text, last_user_text = describe_request(body)
    return {
        "written_by": WRITTEN_BY,
        "received_at": received_at.isoformat(),
        "user_agent": scrub(user_agent, pattern),
        "shape": scrub(shape, pattern),
        "markers": markers(system_text, last_user_text, pattern),
        "body": scrub(body, pattern),
    }


def capture_paths(out_dir: Path) -> Iterator[tuple[int, Path]]:
    """Regular files (never symlinks) whose name is in the form write_capture makes."""
    if out_dir.is_dir() and not out_dir.is_symlink():
        for path in out_dir.iterdir():
            match = CAPTURE_NAME.fullmatch(path.name)
            if match and stat.S_ISREG(path.lstat().st_mode):
                yield int(match[1]), path


def read_own(path: Path) -> dict[str, Any] | None:
    """The capture at `path` if this script wrote it (its `written_by` marker), else None.
    Opened without following a symlink swapped in after listing."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, encoding="utf-8") as source:
            record = json.load(source)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) and record.get("written_by") == WRITTEN_BY else None


def own_captures(out_dir: Path) -> list[tuple[Path, dict[str, Any]]]:
    found = []
    for _, path in sorted(capture_paths(out_dir)):
        record = read_own(path)
        if record is not None:
            found.append((path, record))
    return found


def write_capture(out_dir: Path, capture: dict[str, Any], received_at: dt.datetime) -> Path:
    stamp = received_at.strftime("%Y%m%dT%H%M%S%fZ")
    encoded = json.dumps(capture, indent=2) + "\n"
    with CAPTURE_LOCK:
        if out_dir.is_symlink():
            raise OSError(f"refusing a symlinked capture directory: {out_dir}")
        if not out_dir.exists():
            out_dir.mkdir(mode=0o700, parents=True)
            out_dir.chmod(0o700)  # mkdir's mode is masked by the umask
        elif stat.S_IMODE(out_dir.stat().st_mode) & 0o077:
            raise OSError(f"capture directory must be private (0700): {out_dir}")
        seq = max((seq for seq, _ in capture_paths(out_dir)), default=0) + 1
        while True:
            path = out_dir / f"{seq:04d}-{stamp}.json"
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                break
            except FileExistsError:
                seq += 1
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            os.fchmod(output.fileno(), 0o600)
            output.write(encoded)
    return path


def is_string_type(value: Any) -> bool:
    """`"string"`, or a strict schema's union such as `["string", "null"]`."""
    return value == "string" or (isinstance(value, list) and "string" in value)


def build_completion(body: Any, completion_id: str, created: int) -> dict[str, Any]:
    request = as_object(body)
    model = request.get("model")
    message: dict[str, Any] = {"role": "assistant", "content": REPLY}
    finish_reason = "stop"
    for function in tool_functions(request):
        if function["name"] == "attempt_completion":
            parameters = as_object(function.get("parameters"))
            properties = as_object(parameters.get("properties"))
            arguments = {
                name: REPLY for name in as_list(parameters.get("required"))
                if isinstance(name, str) and is_string_type(as_object(properties.get(name)).get("type"))
            }
            message.update(content=None, tool_calls=[{
                "id": f"call_{completion_id.removeprefix('chatcmpl-')}",
                "type": "function",
                "function": {"name": function["name"], "arguments": json.dumps(arguments)},
            }])
            finish_reason = "tool_calls"
            break
    return {
        "id": completion_id,
        "object": "chat.completion",
        "created": created,
        "model": model if isinstance(model, str) else DEFAULT_MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": USAGE.copy(),
    }


def completion_chunks(completion: dict[str, Any]) -> Iterator[dict[str, Any]]:
    base = {key: completion[key] for key in ("id", "created", "model")}
    base["object"] = "chat.completion.chunk"
    choice = completion["choices"][0]
    message = choice["message"]
    yield {**base, "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}]}
    if "tool_calls" in message:
        delta = {"tool_calls": [{"index": 0, **message["tool_calls"][0]}]}
    else:
        delta = {"content": message["content"]}
    yield {**base, "choices": [{"index": 0, "delta": delta, "finish_reason": None}]}
    yield {
        **base,
        "choices": [{"index": 0, "delta": {}, "finish_reason": choice["finish_reason"]}],
        "usage": completion["usage"],
    }


class CaptureHandler(BaseHTTPRequestHandler):
    server: CaptureServer
    timeout = 30  # a body shorter than its Content-Length can't hold a thread forever

    def log_message(self, format: str, *args: Any) -> None:
        # The capture is the only request log.
        return

    def send_json(self, status: int, value: Any) -> None:
        data = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def send_error(self, code: int, message: str | None = None, explain: str | None = None) -> None:
        self.send_json(code, {"error": {"message": message or self.responses[code][0]}})

    def request_path(self) -> str:
        return self.path.partition("?")[0]

    def do_GET(self) -> None:
        if self.request_path() not in MODELS_PATHS:
            self.send_error(404)
            return
        self.send_json(200, {"object": "list", "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "capture"}
            for model in self.server.models
        ]})

    def do_POST(self) -> None:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = -1
        if length > MAX_BODY_BYTES:
            self.send_error(413, "Request body too large")
            return
        try:
            raw = self.rfile.read(length) if length > 0 else b""  # read first, so no reply resets
        except TimeoutError:
            return
        if self.request_path() not in COMPLETIONS_PATHS:
            self.send_error(404)
            return
        received_at = dt.datetime.now(dt.timezone.utc)
        try:
            if not raw:
                raise ValueError
            body = json.loads(raw, parse_constant=reject_constant)
        except (ValueError, OverflowError, RecursionError):
            self.send_error(400, "Invalid JSON request body")
            return

        try:
            record = capture_record(body, self.headers.get("User-Agent"), received_at, self.server.pattern)
            write_capture(self.server.out_dir, record, received_at)
        except RecursionError:
            self.send_error(400, "JSON request body is too deeply nested")
            return
        except OSError as error:
            print(f"capture not written: {error}", file=sys.stderr, flush=True)
            self.send_error(500, "Could not write capture")
            return

        completion = build_completion(body, f"chatcmpl-{uuid.uuid4().hex}", int(received_at.timestamp()))
        if as_object(body).get("stream") is not True:
            self.send_json(200, completion)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        for chunk in completion_chunks(completion):
            self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode("utf-8"))
        self.wfile.write(b"data: [DONE]\n\n")


def reject_constant(value: str) -> None:
    raise ValueError(f"Invalid JSON constant {value}")


class CaptureServer(ThreadingHTTPServer):
    def __init__(self, host: str, port: int, out_dir: Path, models: Sequence[str]):
        address = LOOPBACK_HOSTS.get(host)
        if address is None:
            raise ValueError("--host must be 127.0.0.1, ::1 or localhost")
        self.address_family = socket.AF_INET6 if address == "::1" else socket.AF_INET
        self.out_dir = Path(out_dir)
        self.models = tuple(models)
        self.pattern = scrub_pattern(os.environ.get(name, "") for name in ("GEMINI_API_KEY", "PROXY_TOKEN"))
        super().__init__((address, port), CaptureHandler)

    def server_bind(self) -> None:
        # TCPServer binds the numeric address. HTTPServer's bind also performs a hostname lookup.
        TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]


def make_server(host: str, port: int, out_dir: Path, models: Sequence[str]) -> CaptureServer:
    return CaptureServer(host, port, out_dir, models)


def print_summary(out_dir: Path) -> None:
    writer = csv.writer(sys.stdout, delimiter="\t", lineterminator="\n")
    writer.writerow(("file", "user_agent", "model", "stream", "n_tools", "roles", "system_chars",
                     "last_user_chars", "reasoning_effort", "has_image", "has_tool_result",
                     "system_you_are", "system_mode_lines", "user_mode_lines"))
    for path, capture in own_captures(out_dir):
        shape = capture["shape"]
        found = capture["markers"]
        writer.writerow((path.name, capture["user_agent"], shape["model"], shape["stream"],
                         shape["n_tools"], len(shape["roles"]), shape["system_chars"],
                         shape["last_user_chars"], shape["reasoning_effort"], shape["has_image"],
                         shape["has_tool_result"], next(iter(found["system_you_are"]), ""),
                         " | ".join(found["system_mode_lines"]), " | ".join(found["user_mode_lines"])))


def clean(out_dir: Path) -> int:
    """Delete the captures this script wrote in `out_dir` and nothing else. Only the default
    folder, which the script owns, is then removed, and only if empty. Returns the count.
    Not defended: a folder swapped or re-linked by another process during the call."""
    if out_dir.is_symlink():
        raise OSError(f"refusing a symlinked capture directory: {out_dir}")
    paths = [path for path, _ in own_captures(out_dir)]
    for path in paths:
        path.unlink()
    if Path(os.path.abspath(out_dir)) == DATA_DIR:
        try:
            out_dir.rmdir()  # fails, and is left alone, if anything else is in it
        except OSError:
            pass
    return len(paths)


def reminder(out_dir: Path) -> str:
    """The exact command to run, with an absolute, shell-quoted --out, so it can be pasted
    from any directory."""
    out_dir = Path(os.path.abspath(out_dir))
    out = "" if out_dir == DATA_DIR else f" --out {shlex.quote(str(out_dir))}"
    return ("After recording results in docs/probe-results.md, delete the captures with: "
            f"python3 -I scripts/capture_client.py --clean{out}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--host", choices=tuple(LOOPBACK_HOSTS), default="127.0.0.1")
    parser.add_argument("--port", type=int, default=4010)
    parser.add_argument("--models", default=DEFAULT_MODEL, help="comma-separated model IDs")
    parser.add_argument("--out", type=Path, default=DATA_DIR, help="capture directory")
    parser.add_argument("--summary", action="store_true", help="print a TSV summary and exit")
    parser.add_argument("--clean", action="store_true",
                        help="delete the capture files (and the folder if then empty) and exit")
    args = parser.parse_args(argv)
    if args.clean:
        try:
            print(f"deleted {clean(args.out)} capture file(s) from {args.out}")
        except OSError as error:
            parser.exit(1, f"Could not clean: {error}\n")
        return 0
    note = reminder(args.out)
    if args.summary:
        try:
            print_summary(args.out)
        finally:
            print(note, file=sys.stderr)
        return 0

    models = [model.strip() for model in args.models.split(",") if model.strip()]
    try:
        with make_server(args.host, args.port, args.out, models) as server:
            host, port = server.server_address[:2]
            display_host = f"[{host}]" if ":" in host else host
            print(f"Listening on http://{display_host}:{port}/v1", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        print(note, file=sys.stderr)
    except (OSError, OverflowError) as error:
        parser.exit(1, f"Could not start capture server: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
