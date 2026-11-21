"""A stand-in for the ARC LLM service: a small OpenAI-compatible HTTP server on 127.0.0.1.

It answers `POST .../chat/completions` with the replies a test scripted, in order, and records
every request it received (path, headers, parsed body), so a test can assert on exactly what was
sent. Nothing here touches the network beyond the loopback address.

    with FakeLlmServer() as server:
        server.script(content('{"keywords": ""}'), VPN_403)
        client = ChatClient(server.base_url, FAKE_KEY, "test-model", timeout=5)
        ...
        server.chat_requests[0]["body"]["messages"]

A scripted reply may also be a function `(request record) -> Reply`. When the script is empty
the server uses `server.default` (a Reply or such a function); the first default is an HTTP 500
that says nothing was scripted, so a forgotten script shows up at once.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# A made-up key in the shape of a real one. It is not a secret; tests look for it in errors,
# logs and results to prove that a real key could never show up there.
FAKE_KEY = "sk-fake0123456789abcdef0123456789ab"

VPN_BODY = "API access is restricted to the VT Campus VPN. Please connect to the VPN and retry."


@dataclass
class Reply:
    status: int = 200
    body: bytes = b""
    headers: dict = field(default_factory=dict)
    delay: float = 0.0          # seconds to wait before answering
    drop: bool = False          # close the connection without answering


def content(text, *, finish_reason="stop", reasoning=None, reasoning_content=None, usage=None,
            delay=0.0):
    """A normal chat completion whose answer is `text` (None = no content field value)."""
    message = {"role": "assistant", "content": text}
    if reasoning is not None:
        message["reasoning"] = reasoning
    if reasoning_content is not None:
        message["reasoning_content"] = reasoning_content
    payload = {
        "id": "chatcmpl-fake", "object": "chat.completion", "model": "test-model",
        "choices": [{"index": 0, "message": message, "finish_reason": finish_reason}],
        "usage": usage if usage is not None else
        {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }
    return json_reply(200, payload, delay=delay)


def json_reply(status, payload, headers=None, delay=0.0):
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    return Reply(status, json.dumps(payload).encode("utf-8"), merged, delay)


def text_reply(status, text="", headers=None, content_type="text/plain", delay=0.0):
    merged = {"Content-Type": content_type}
    merged.update(headers or {})
    return Reply(status, text.encode("utf-8"), merged, delay)


def error_reply(status, message="something went wrong", code=None, headers=None, **more):
    """An error in the JSON form: {"error": {"message", "code", ...}}."""
    error = {"message": message}
    if code is not None:
        error["code"] = code
    error.update(more)
    return json_reply(status, {"error": error}, headers)


def models_reply(*names):
    return json_reply(200, {"object": "list", "data": [{"id": name, "object": "model"}
                                                       for name in names]})


def slow(seconds, reply):
    return Reply(reply.status, reply.body, dict(reply.headers), seconds, reply.drop)


VPN_403 = text_reply(403, VPN_BODY)
DROP = Reply(drop=True)
NOTHING_SCRIPTED = text_reply(500, "the fake LLM server has no scripted reply for this request")


class _Handler(BaseHTTPRequestHandler):
    server_version = "FakeLlm/1"

    def log_message(self, format, *args):       # keep test output quiet
        pass

    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            body = None
        return {"method": self.command, "path": self.path,
                "headers": {name.lower(): value for name, value in self.headers.items()},
                "body": body, "raw": raw}

    def _answer(self, reply):
        fake = self.server.fake
        if reply.delay:
            fake.stopping.wait(reply.delay)
        if reply.drop:
            try:
                self.connection.close()
            except OSError:
                pass
            return
        try:
            self.send_response(reply.status)
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(reply.body)))
            self.end_headers()
            self.wfile.write(reply.body)
        except OSError:
            pass            # the client gave up (a timeout test); nothing to do

    def _handle(self):
        fake = self.server.fake
        record = self._record()
        with fake.lock:
            fake.requests.append(record)
            fake.in_flight += 1
            fake.max_in_flight = max(fake.max_in_flight, fake.in_flight)
        try:
            path = self.path.split("?", 1)[0].rstrip("/")
            if self.command == "POST" and path.endswith("/chat/completions"):
                reply = fake.next_reply(record)
            elif self.command == "GET" and path.endswith("/models"):
                reply = fake.models
            else:
                reply = text_reply(404, "no such path on the fake LLM server")
            if callable(reply):
                reply = reply(record)
            self._answer(reply)
        finally:
            with fake.lock:
                fake.in_flight -= 1

    do_GET = _handle
    do_POST = _handle


class FakeLlmServer:
    """Start with `with FakeLlmServer() as server:` or call start() and close()."""

    def __init__(self):
        self.lock = threading.Lock()
        self.stopping = threading.Event()
        self.requests = []
        self.queue = []
        self.default = NOTHING_SCRIPTED
        self.models = text_reply(404, "Not Found")     # the list is not offered, as ARC may
        self.in_flight = 0
        self.max_in_flight = 0
        self._server = None
        self._thread = None

    # ---- life cycle

    def start(self):
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._server.daemon_threads = True
        self._server.fake = self
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        kwargs={"poll_interval": 0.05}, daemon=True)
        self._thread.start()
        return self

    def close(self):
        self.stopping.set()
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._thread.join(timeout=5)
            self._server = None

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.close()
        return False

    # ---- what a test uses

    @property
    def port(self):
        return self._server.server_address[1]

    @property
    def base_url(self):
        return "http://127.0.0.1:%d/api/v1" % self.port

    def script(self, *replies):
        """Queue replies for the next chat requests, in order."""
        with self.lock:
            self.queue.extend(replies)
        return self

    def next_reply(self, record):
        with self.lock:
            if self.queue:
                return self.queue.pop(0)
        return self.default

    @property
    def chat_requests(self):
        with self.lock:
            return [record for record in self.requests
                    if record["method"] == "POST"
                    and record["path"].split("?", 1)[0].rstrip("/").endswith("/chat/completions")]

    def messages(self, index=-1):
        """The `messages` list of one received chat request."""
        return self.chat_requests[index]["body"]["messages"]

    def reset(self):
        with self.lock:
            self.requests.clear()
            self.queue.clear()
            self.in_flight = 0
            self.max_in_flight = 0
