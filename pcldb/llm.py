"""The client for the Virginia Tech ARC LLM service (an OpenAI-compatible chat API).

Standard library only (`urllib`). What this module promises:

- The key never leaves this module except in the `Authorization` header of a request to the
  configured address. It is not in any error message, `repr()`, log line or returned value, and
  it is never sent along a redirect (redirects are refused).
- A request holds only `model`, `messages`, `max_tokens`, `temperature`, `reasoning_effort`
  (when set) and `response_format` (only when the setting is on). No tools, no files.
- Every failure is an `Unavailable` error with a sentence a person understands. The service's
  own error body is read as: the status first, then JSON, then plain text (off the VT network
  the service answers HTTP 403 with a plain-text body).
- Retries are bounded and decided by status (see `ChatClient.chat`).
- At most `max_concurrent` chat requests are in flight in this process.

Nobody has yet seen a successful reply from the real service, so reading a reply is tolerant:
only `choices[0].message.content` is used; `reasoning` fields are ignored; `<think>` blocks are
removed; a JSON object is found wherever it sits in the text (`extract_json_object`).
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import logging
import re
import socket
import ssl
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field

from . import __version__
from .errors import Unavailable

_log = logging.getLogger("pcldb.llm")

DEFAULT_MODEL = "gpt-oss-120b"
MAX_TOKENS_CEILING = 8000        # the service's cap for a request that is not streamed
CONNECT_TIMEOUT_SECONDS = 10.0
PROBE_CACHE_SECONDS = 60.0
_PROBE_TIMEOUT = 8.0             # a status page must not hang on a silent service
_LIST_TIMEOUT = 20.0
_MAX_REPLY_BYTES = 4 * 1024 * 1024
_MAX_RETRY_WAIT = 20.0
_DEFAULT_429_WAIT = 5.0
_TRANSIENT_WAIT = 1.0
_TRANSIENT_STATUSES = (502, 503, 504)

# Sentences people read. Kept here so tests and the other modules can compare against them.
MESSAGE_NO_KEY = ("No ARC LLM key is saved on this computer. Make one at llm.arc.vt.edu "
                  "(Settings > Account > API keys) and save it, or set PCLDB_LLM_API_KEY.")
MESSAGE_BAD_KEY_FORM = ("The saved ARC LLM key does not look like a key (it holds spaces, line "
                        "breaks or unusual characters). Save it again as one line.")
MESSAGE_KEY_REFUSED = ("The ARC LLM key was refused. Make a new one at llm.arc.vt.edu and save "
                       "it again.")
MESSAGE_OFF_VPN = ("The ARC LLM service only answers on the VT campus network or VPN. Connect "
                   "to the VT VPN and ask again.")
MESSAGE_DISABLED = "The assistant is switched off in the settings (llm.enabled)."
MESSAGE_BUSY = ("The ARC LLM service is busy (too many requests at once). Wait a minute and "
                "ask again.")
MESSAGE_TOO_LARGE = ("The request was too large for the ARC LLM service (HTTP 413). Ask a "
                     "shorter question.")
MESSAGE_NO_CONNECTION = ("The ARC LLM service could not be reached. Check the network "
                         "connection and the VT VPN.")
MESSAGE_EMPTY = "The ARC LLM service gave no usable answer (it was empty or cut off)."
MESSAGE_ODD_REPLY = "The ARC LLM service answered in a form pcldb does not understand."


# ---------------------------------------------------------------------- reading a reply


_THINK_TAG = re.compile(r"<\s*(/?)\s*(?:think|thinking)\s*>", re.IGNORECASE)
_FINAL_CHANNEL = re.compile(r"<\|channel\|>\s*final\s*<\|message\|>")
_CHANNEL_END = re.compile(r"<\|(?:return|end|start)\|>")


def strip_reasoning(text):
    """`text` without the model's thinking.

    Removes every `<think>...</think>` block (also `<thinking>`, any letter case, nested). A
    block that is never closed runs to the end of the text. When a closing tag comes without an
    opening one (some servers drop the opening tag), everything before it is thinking. A reply
    in the raw channel form of gpt-oss keeps only its final channel. One pass over the text.
    """
    if not isinstance(text, str) or not text:
        return ""
    final = None
    for final in _FINAL_CHANNEL.finditer(text):
        pass
    if final is not None:
        text = text[final.end():]
        end = _CHANNEL_END.search(text)
        if end is not None:
            text = text[:end.start()]
    if "<" not in text:
        return text
    kept = []
    position = 0
    depth = 0
    for tag in _THINK_TAG.finditer(text):
        closing = bool(tag.group(1))
        if not closing:
            if depth == 0:
                kept.append(text[position:tag.start()])
            depth += 1
        elif depth > 0:
            depth -= 1
            if depth == 0:
                position = tag.end()
        else:
            kept = []
            position = tag.end()
    if depth == 0:
        kept.append(text[position:])
    return "".join(kept)


_MAX_SCAN_CHARS = 400_000
_MAX_CANDIDATES = 200


def _refuse_constant(name):
    raise ValueError("%s is not JSON" % name)


_DECODER = json.JSONDecoder(parse_constant=_refuse_constant)


def extract_json_object(text):
    """The first complete JSON object in `text`, as a dict.

    The object may be surrounded by anything: thinking tags (removed first), a code fence, a
    sentence before or after, an array before it. Braces and quotes inside strings are read by
    the real JSON reader, not counted by hand. NaN and Infinity are not JSON here. Raises
    `ValueError` with a plain sentence when there is no object or the object is cut off.
    """
    text = strip_reasoning(text)
    if len(text) > _MAX_SCAN_CHARS:
        text = text[:_MAX_SCAN_CHARS]
    start = text.find("{")
    if start < 0:
        raise ValueError("The reply holds no JSON object.")
    end_of_text = len(text.rstrip())
    tried = 0
    while start >= 0 and tried < _MAX_CANDIDATES:
        tried += 1
        try:
            value, _ = _DECODER.raw_decode(text, start)
        except json.JSONDecodeError as error:
            if error.pos >= end_of_text or error.msg.startswith("Unterminated string"):
                # The text ends inside this object: nothing after it can be a whole plan.
                raise ValueError("The JSON object in the reply is cut off.") from None
            start = text.find("{", max(start + 1, error.pos))
            continue
        except (ValueError, RecursionError):
            start = text.find("{", start + 1)
            continue
        if isinstance(value, dict):
            return value
        start = text.find("{", start + 1)
    raise ValueError("The reply holds no complete JSON object.")


def _plain(text, limit=200):
    """A server's text made safe to show: one line, no control characters, cut to `limit`."""
    if not isinstance(text, str):
        return ""
    cleaned = "".join(" " if unicodedata.category(ch)[0] in ("C", "Z") else ch for ch in text)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > limit:
        cleaned = cleaned[:limit - 3].rstrip() + "..."
    return cleaned


# ---------------------------------------------------------------------- process-wide state


_state_lock = threading.Lock()
_semaphores = {}            # max_concurrent -> BoundedSemaphore
_format_refused = set()     # base addresses that answered 400/422 to response_format
_probe_cache = {}           # (address, model, enabled, key fingerprint) -> (time, result)


def _semaphore_for(count):
    with _state_lock:
        semaphore = _semaphores.get(count)
        if semaphore is None:
            semaphore = _semaphores[count] = threading.BoundedSemaphore(count)
        return semaphore


def reset_process_state():
    """Forget what this process learned (probe results, a refused response_format). For tests."""
    with _state_lock:
        _format_refused.clear()
        _probe_cache.clear()
        _semaphores.clear()
        _openers.clear()


# ---------------------------------------------------------------------- HTTP plumbing


def _is_loopback(host):
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _connection_class(base, connect_timeout):
    """`base` with its own, shorter time limit for opening the connection."""

    class Connection(base):
        def connect(self):
            whole = self.timeout
            if whole is None or whole > connect_timeout:
                self.timeout = connect_timeout
            try:
                super().connect()
            finally:
                self.timeout = whole
            if self.sock is not None:
                self.sock.settimeout(whole)

    return Connection


class _HttpHandler(urllib.request.HTTPHandler):
    def __init__(self, connect_timeout):
        super().__init__()
        self._connection = _connection_class(http.client.HTTPConnection, connect_timeout)

    def http_open(self, request):
        return self.do_open(self._connection, request)


class _HttpsHandler(urllib.request.HTTPSHandler):
    def __init__(self, connect_timeout):
        self._pcl_context = ssl.create_default_context()
        super().__init__(context=self._pcl_context)
        self._connection = _connection_class(http.client.HTTPSConnection, connect_timeout)

    def https_open(self, request):
        return self.do_open(self._connection, request, context=self._pcl_context)


_openers = {}


def _opener_for(host, connect_timeout):
    """One opener per host and connect time limit for the whole process (making one reads the
    system's certificates, which is slow on some systems)."""
    key = (host, connect_timeout)
    with _state_lock:
        opener = _openers.get(key)
    if opener is None:
        opener = _build_opener(host, connect_timeout)
        with _state_lock:
            opener = _openers.setdefault(key, opener)
    return opener


def _build_opener(host, connect_timeout):
    """An opener that knows http and https only and never follows a redirect (a redirect could
    carry the key to another host). Loopback addresses never go through a proxy."""
    opener = urllib.request.OpenerDirector()
    if not _is_loopback(host):
        opener.add_handler(urllib.request.ProxyHandler())
    opener.add_handler(_HttpHandler(connect_timeout))
    opener.add_handler(_HttpsHandler(connect_timeout))
    opener.add_handler(urllib.request.HTTPDefaultErrorHandler())
    opener.add_handler(urllib.request.HTTPErrorProcessor())
    return opener


@dataclass(frozen=True)
class _Reply:
    kind: str                 # "http" | "timeout" | "connection"
    status: int = 0
    text: str = ""
    retry_after: str | None = None
    content_type: str = ""


def _read_limited(stream, deadline):
    chunks = []
    total = 0
    while True:
        if time.monotonic() > deadline:
            raise TimeoutError("the reply took too long")
        chunk = stream.read(65536)
        if not chunk:
            break
        total += len(chunk)
        if total > _MAX_REPLY_BYTES:
            break
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _json_or_none(text):
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        return None


def _error_parts(text):
    """(message, code, retry_after_s) from an error body that may be JSON or plain text."""
    payload = _json_or_none(text)
    message, code, wait = "", "", None
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail") or ""
            code = error.get("code") or error.get("reason") or error.get("type") or ""
            wait = error.get("retry_after_s")
        elif isinstance(error, str):
            message = error
        if not message:
            for key in ("detail", "message"):
                if isinstance(payload.get(key), str):
                    message = payload[key]
                    break
        if wait is None:
            wait = payload.get("retry_after_s")
    elif payload is None:
        stripped = text.strip()
        if stripped and not stripped.startswith("<"):     # not an HTML error page
            message = stripped
    if isinstance(wait, bool) or not isinstance(wait, (int, float)):
        wait = None
    return (message if isinstance(message, str) else "", code if isinstance(code, str) else "",
            wait)


def _wait_seconds(reply, default):
    """How long to wait before a retry: Retry-After, else error.retry_after_s, else `default`;
    never more than 20 seconds."""
    wait = None
    if reply.retry_after:
        try:
            wait = float(reply.retry_after.strip())
        except ValueError:
            wait = None
    if wait is None:
        wait = _error_parts(reply.text)[2]
    if wait is None or wait != wait or wait < 0:
        wait = default
    return min(float(wait), _MAX_RETRY_WAIT)


# ---------------------------------------------------------------------- the client


@dataclass(frozen=True)
class ChatResult:
    """One answer: the text (thinking removed), the token counts the service reported, and the
    time the whole call took (retries and waits included)."""

    text: str
    usage: dict = field(default_factory=dict)
    elapsed_ms: int = 0


class ChatClient:
    """Talks to one OpenAI-compatible chat address with one key and one model."""

    def __init__(self, base_url, api_key, model, timeout=90, *, reasoning_effort="low",
                 response_format=False, max_concurrent=2, enabled=True,
                 connect_timeout=CONNECT_TIMEOUT_SECONDS, sleep=time.sleep):
        self.base_url = str(base_url or "").strip().rstrip("/")
        self.model = str(model or "").strip() or DEFAULT_MODEL
        self.timeout = float(timeout)
        self.reasoning_effort = str(reasoning_effort or "").strip()
        self.response_format = bool(response_format)
        self.max_concurrent = max(1, int(max_concurrent))
        self.enabled = bool(enabled)
        self.connect_timeout = float(connect_timeout)
        self._sleep = sleep
        self._api_key = api_key.strip() if isinstance(api_key, str) else ""
        self._scrub = self._api_key if len(self._api_key) >= 4 else ""
        parts = urllib.parse.urlsplit(self.base_url)
        self._address_ok = parts.scheme in ("http", "https") and bool(parts.hostname)
        self._opener = _opener_for(parts.hostname or "", self.connect_timeout)

    @classmethod
    def from_settings(cls, settings, *, sleep=time.sleep):
        """The client the settings describe (the key from `settings.llm_api_key()`)."""
        llm = settings.llm
        return cls(llm.base_url, settings.llm_api_key(), llm.model, llm.timeout_seconds,
                   reasoning_effort=llm.reasoning_effort, response_format=llm.response_format,
                   max_concurrent=llm.max_concurrent, enabled=llm.enabled, sleep=sleep)

    def __repr__(self):
        return "ChatClient(base_url=%r, model=%r, key=%s)" % (
            self.base_url, self.model, "set" if self._api_key else "not set")

    @property
    def has_key(self):
        return bool(self._api_key)

    # -------------------------------------------------------------- errors

    def _safe(self, text, limit=200):
        text = _plain(text, limit)
        if self._scrub and self._scrub in text:
            text = text.replace(self._scrub, "[key]")
        return text

    def _unavailable(self, message, reason, status=None):
        details = {"reason": reason}
        if status:
            details["http_status"] = status
        return Unavailable(self._safe(message, 400), code="llm_" + reason, details=details)

    def _check_ready(self):
        if not self.enabled:
            raise self._unavailable(MESSAGE_DISABLED, "disabled")
        if not self._api_key:
            raise self._unavailable(MESSAGE_NO_KEY, "no_key")
        if any(ord(ch) <= 32 or ord(ch) >= 127 for ch in self._api_key):
            raise self._unavailable(MESSAGE_BAD_KEY_FORM, "bad_key_form")
        if not self._address_ok:
            raise self._unavailable(
                "The setting llm.base_url is not a web address (it must start with https:// "
                "or http://).", "bad_address")

    def _error_for(self, reply):
        """The Unavailable error for an HTTP status that is not a success."""
        status = reply.status
        message, code, _ = _error_parts(reply.text)
        said = self._safe(message)
        if status == 401:
            return self._unavailable(MESSAGE_KEY_REFUSED, "bad_key", status)
        if status == 403:
            if "vpn" in reply.text.lower():
                return self._unavailable(MESSAGE_OFF_VPN, "off_vpn", status)
            text = "The ARC LLM service refused the request (HTTP 403)"
            return self._unavailable(text + (": " + said if said else "."), "refused", status)
        if status == 413:
            return self._unavailable(MESSAGE_TOO_LARGE, "too_large", status)
        if status == 429:
            return self._unavailable(MESSAGE_BUSY, "busy", status)
        if status == 404 or (status in (400, 422) and "model" in (message + code).lower()):
            text = ("The ARC LLM service does not know this address or the model '%s' (HTTP %d). "
                    "Check llm.base_url and llm.model in the settings; the models change every "
                    "few months." % (self.model, status))
            return self._unavailable(text, "not_found", status)
        if status >= 500:
            text = ("The ARC LLM service has a problem right now (HTTP %d). Try again later."
                    % status)
            return self._unavailable(text, "server_error", status)
        text = "The ARC LLM service did not accept the request (HTTP %d)" % status
        return self._unavailable(text + (": " + said if said else "."), "http_error", status)

    # -------------------------------------------------------------- one request

    def _request(self, method, path, body=None, timeout=None, limited=True):
        """One HTTP exchange. Never raises for a network problem: the reply says what happened.
        `limited`: count it against `max_concurrent` (every chat request is; the small request
        for the model list is not, so a status check never waits behind two long answers)."""
        timeout = self.timeout if timeout is None else timeout
        request = urllib.request.Request(self.base_url + path, method=method)
        if body is not None:
            request.data = json.dumps(body, ensure_ascii=True, allow_nan=False).encode("ascii")
            request.add_header("Content-Type", "application/json")
        request.add_header("Accept", "application/json")
        request.add_header("User-Agent", "pcldb/" + __version__)
        request.add_unredirected_header("Authorization", "Bearer " + self._api_key)
        semaphore = _semaphore_for(self.max_concurrent) if limited else None
        if semaphore is not None and not semaphore.acquire(timeout=max(timeout, 1.0)):
            return _Reply("timeout")
        started = time.monotonic()
        deadline = started + timeout
        try:
            try:
                with self._opener.open(request, timeout=timeout) as response:
                    reply = _Reply("http", response.status, _read_limited(response, deadline),
                                   response.headers.get("Retry-After"),
                                   response.headers.get("Content-Type") or "")
            except urllib.error.HTTPError as error:
                try:
                    text = _read_limited(error, deadline)
                except (OSError, http.client.HTTPException, AttributeError, ValueError):
                    text = ""           # the status is known; its text could not be read
                finally:
                    error.close()
                reply = _Reply("http", error.code, text, error.headers.get("Retry-After"),
                               error.headers.get("Content-Type") or "")
        except urllib.error.URLError as error:
            timed_out = isinstance(error.reason, (socket.timeout, TimeoutError))
            reply = _Reply("timeout" if timed_out else "connection")
        except (socket.timeout, TimeoutError):
            reply = _Reply("timeout")
        except (OSError, http.client.HTTPException, ValueError):
            reply = _Reply("connection")
        finally:
            if semaphore is not None:
                semaphore.release()
        _log.debug("%s %s -> %s %s in %d ms", method, path, reply.kind, reply.status or "",
                   int((time.monotonic() - started) * 1000))
        return reply

    # -------------------------------------------------------------- chat

    def chat(self, messages, *, json_object=False, max_tokens=800, temperature=0):
        """Send the messages; return `ChatResult(text, usage, elapsed_ms)`.

        Retries, all bounded: HTTP 429 waits (`Retry-After`, else `error.retry_after_s`, else
        5 s; at most 20 s) and tries again at most twice; 502, 503, 504, a timeout or a broken
        connection is tried once more; an empty answer or one cut off by the token limit is
        tried once more with double `max_tokens`; a 400 or 422 while `response_format` was sent
        switches that field off for this process and repeats the call once. 401, 403 and 413
        are never retried. Anything that still fails is an `Unavailable` error.
        """
        started = time.monotonic()
        self._check_ready()
        clean = []
        for message in messages:
            role, content = message.get("role"), message.get("content")
            if role not in ("system", "user", "assistant") or not isinstance(content, str):
                raise ValueError("a chat message needs a role and a text content")
            clean.append({"role": role, "content": content})
        if not clean:
            raise ValueError("chat() needs at least one message")
        budget = max(1, min(int(max_tokens), MAX_TOKENS_CEILING))
        body = {"model": self.model, "messages": clean, "max_tokens": budget,
                "temperature": temperature}
        if self.reasoning_effort:
            body["reasoning_effort"] = self.reasoning_effort
        if json_object and self.response_format and self.base_url not in _format_refused:
            body["response_format"] = {"type": "json_object"}

        busy_retries = 0
        retried_transient = False
        retried_empty = False
        while True:
            reply = self._request("POST", "/chat/completions", body)
            if reply.kind != "http" or reply.status in _TRANSIENT_STATUSES:
                if not retried_transient:
                    retried_transient = True
                    self._sleep(_wait_seconds(reply, _TRANSIENT_WAIT) if reply.kind == "http"
                                else _TRANSIENT_WAIT)
                    continue
                if reply.kind == "timeout":
                    raise self._unavailable(
                        "The ARC LLM service did not answer within %g seconds. Try again later."
                        % self.timeout, "timeout")
                if reply.kind == "connection":
                    raise self._unavailable(MESSAGE_NO_CONNECTION, "no_connection")
                raise self._error_for(reply)
            if reply.status == 429 and busy_retries < 2:
                busy_retries += 1
                self._sleep(_wait_seconds(reply, _DEFAULT_429_WAIT))
                continue
            if reply.status in (400, 422) and "response_format" in body:
                with _state_lock:
                    _format_refused.add(self.base_url)
                del body["response_format"]
                continue
            if reply.status != 200:
                raise self._error_for(reply)
            text, finish, usage = self._read_chat(reply)
            if not text or finish == "length":
                if not retried_empty:
                    retried_empty = True
                    body["max_tokens"] = min(body["max_tokens"] * 2, MAX_TOKENS_CEILING)
                    continue
                raise self._unavailable(MESSAGE_EMPTY, "empty")
            return ChatResult(text=text, usage=usage,
                              elapsed_ms=int((time.monotonic() - started) * 1000))

    def _read_chat(self, reply):
        """(text, finish_reason, usage) from a 200 reply. Only `message.content` is the answer;
        `reasoning` and `reasoning_content` are never read."""
        payload = _json_or_none(reply.text)
        try:
            choice = payload["choices"][0]
            content = choice["message"]["content"]
        except (TypeError, KeyError, IndexError):
            raise self._unavailable(MESSAGE_ODD_REPLY, "odd_reply") from None
        if isinstance(content, list):       # the "content parts" form
            content = "".join(part.get("text", "") for part in content
                              if isinstance(part, dict) and isinstance(part.get("text"), str))
        if not isinstance(content, str):
            content = ""
        finish = choice.get("finish_reason") if isinstance(choice, dict) else None
        usage = {}
        if isinstance(payload.get("usage"), dict):
            for name in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = payload["usage"].get(name)
                if isinstance(value, int) and not isinstance(value, bool):
                    usage[name] = value
        return strip_reasoning(content).strip(), finish, usage

    # -------------------------------------------------------------- models and probe

    def models(self):
        """The model ids the service lists, or [] when it offers no list (the list address is
        not documented by ARC). Other failures are `Unavailable`, as in `chat`. One attempt."""
        self._check_ready()
        reply = self._request("GET", "/models", timeout=min(self.timeout, _LIST_TIMEOUT),
                              limited=False)
        return self._models_from(reply)

    def _models_from(self, reply):
        if reply.kind == "timeout":
            raise self._unavailable("The ARC LLM service did not answer in time.", "timeout")
        if reply.kind == "connection":
            raise self._unavailable(MESSAGE_NO_CONNECTION, "no_connection")
        if reply.status in (404, 405, 501):
            return []
        if reply.status != 200:
            raise self._error_for(reply)
        payload = _json_or_none(reply.text)
        if isinstance(payload, dict):
            items = payload.get("data")
            if not isinstance(items, list):
                items = payload.get("models")
        else:
            items = payload
        names = []
        for item in items if isinstance(items, list) else []:
            name = item.get("id") or item.get("name") if isinstance(item, dict) else item
            if isinstance(name, str) and name.strip():
                names.append(self._safe(name, 100))
        return names

    def probe(self, max_age=PROBE_CACHE_SECONDS):
        """Can the assistant be used right now? `{"state", "note"}`; never raises.

        States: `disabled`, `no_key`, `ready` (the service answered 200, or 404 because it
        offers no model list), `off_vpn` (HTTP 403 that mentions the VPN: connect to the VT
        VPN; the key is fine), `bad_key` (401), `error` (anything else; the note has the
        status). One cheap request (the model list); the result is kept for 60 seconds.
        """
        fingerprint = hash(self._api_key)
        cache_key = (self.base_url, self.model, self.enabled, fingerprint)
        now = time.monotonic()
        with _state_lock:
            cached = _probe_cache.get(cache_key)
        if cached is not None and max_age > 0 and now - cached[0] < max_age:
            return dict(cached[1])
        try:
            result = self._probe_now()
        except Exception:                      # never raises, whatever the service does
            result = {"state": "error", "note": "The check of the ARC LLM service failed."}
        with _state_lock:
            _probe_cache[cache_key] = (now, result)
        return dict(result)

    def _probe_now(self):
        try:
            self._check_ready()
        except Unavailable as error:
            reason = (error.details or {}).get("reason")
            state = reason if reason in ("disabled", "no_key") else "error"
            return {"state": state, "note": error.message}
        reply = self._request("GET", "/models", timeout=min(self.timeout, _PROBE_TIMEOUT),
                              limited=False)
        if reply.kind == "timeout":
            return {"state": "error", "note": "The ARC LLM service did not answer in time."}
        if reply.kind == "connection":
            return {"state": "error", "note": MESSAGE_NO_CONNECTION}
        if reply.status == 200:
            return {"state": "ready", "note": "The ARC LLM service answers and accepts the key."}
        if reply.status == 404:
            return {"state": "ready",
                    "note": "The ARC LLM service answers. It offers no model list, so the key "
                            "and the model are checked by the first question."}
        error = self._error_for(reply)
        reason = error.details["reason"]
        if reason == "off_vpn":
            return {"state": "off_vpn",
                    "note": "This computer is not on the VT campus network or VPN, and the ARC "
                            "LLM service only answers there. Connect to the VT VPN to use the "
                            "assistant; until then questions are answered by the keyword rules."}
        if reason == "bad_key":
            return {"state": "bad_key", "note": error.message}
        note = error.message
        if "HTTP %d" % reply.status not in note:
            note = "%s (HTTP %d)" % (note.rstrip("."), reply.status)
        return {"state": "error", "note": note}


def assistant_status(settings):
    """What the Status page and `doctor` show about the assistant:
    `{"enabled", "key_present", "model", "state", "note"}` (see `ChatClient.probe`; the probe
    is kept for 60 seconds). Never raises and never holds the key."""
    client = ChatClient.from_settings(settings)
    probe = client.probe()
    return {"enabled": client.enabled, "key_present": client.has_key, "model": client.model,
            "state": probe["state"], "note": probe["note"]}
