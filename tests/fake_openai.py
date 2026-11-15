"""A local stand-in for the OpenAI Responses API, for tests that run the real program.

It runs in a thread of the test process and answers `POST /v1/responses` from what is in the
request, not from the order of the calls: a program that was killed and restarted asks the same
question again and must get a sensible answer. A test can hold requests back (`gate.clear()`), to
make the program wait for the model exactly when the test wants to stop it.

The replies have the shape of the real API's non-streaming ones (compare `aimock`'s responses
builder, a maintained mock of the same API): an envelope with `output` items and `usage`.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

USAGE = {
    "input_tokens": 20,
    "output_tokens": 5,
    "total_tokens": 25,
    "input_tokens_details": {"cached_tokens": 0},
    "output_tokens_details": {"reasoning_tokens": 0},
}


def say(text: str) -> list[dict[str, Any]]:
    return [
        {
            "type": "message",
            "id": "msg_fake",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        }
    ]


def call(name: str, arguments: dict[str, Any], call_id: str = "call_1") -> list[dict[str, Any]]:
    return [
        {
            "type": "function_call",
            "id": "fc_fake",
            "call_id": call_id,
            "name": name,
            "arguments": json.dumps(arguments),
            "status": "completed",
        }
    ]


def answer(request: str) -> list[dict[str, Any]]:
    """What the model says, decided by what it was shown (the JSON of the request's `input`)."""
    if '"function_call_output"' in request:
        return say("Done.")
    if "write a note" in request:
        return call("write_file", {"path": "note.txt", "content": "hello"})
    return say("Hello.")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        """Keep the test output clean."""

    def do_POST(self) -> None:
        fake: FakeOpenAI = self.server.fake  # type: ignore[attr-defined]
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        if not self.path.rstrip("/").endswith("/responses"):
            self._send(404, {"error": {"message": f"no route for {self.path}"}})
            return
        fake.record(body)
        fake.gate.wait(timeout=fake.hold_for)
        output = answer(json.dumps(body.get("input", "")))
        self._send(
            200,
            {
                "id": f"resp_{len(fake.requests)}",
                "object": "response",
                "created_at": int(time.time()),
                "model": body.get("model", "fake-model"),
                "status": "completed",
                "output": output,
                "usage": USAGE,
            },
        )

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        data = json.dumps(payload).encode()
        try:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the program that asked was killed while it waited: nobody to answer


class FakeOpenAI:
    def __init__(self, *, hold_for: float = 120.0) -> None:
        self.requests: list[dict[str, Any]] = []
        self.gate = threading.Event()
        """While it is clear, requests wait. A test that does not care leaves it set."""
        self.gate.set()
        self.hold_for = hold_for
        self._arrived = threading.Condition()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.fake = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> FakeOpenAI:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.gate.set()
        self._server.shutdown()
        self._server.server_close()

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{host!s}:{port}/v1"

    def record(self, body: dict[str, Any]) -> None:
        with self._arrived:
            self.requests.append(body)
            self._arrived.notify_all()

    def wait_for_requests(self, count: int, timeout: float = 30.0) -> None:
        """Block until the program has asked `count` times (it may still be waiting for the answer)."""
        with self._arrived:
            if not self._arrived.wait_for(lambda: len(self.requests) >= count, timeout):
                raise TimeoutError(f"only {len(self.requests)} of {count} requests arrived")
