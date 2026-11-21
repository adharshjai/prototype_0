"""A small client for a running pcldb server (specification section 11).

    from pcldb.client import Client
    client = Client("http://127.0.0.1:8787", token)
    client.register(document)            # after an upload succeeded
    client.ask("adenine sessions from last week")
    client.sql("SELECT COUNT(*) FROM sessions")

This is what an uploader, the teammate's Bridge or `pcldb register --server` calls. Standard
library only (`http.client`). The token travels only in the `Authorization` header, never in an
address; redirects are not followed (they could carry the header to another place) and no
proxy is used. Every refusal of the server becomes a `PclError` with the server's code, message,
status and details; a server that cannot be reached is `Unavailable`.

A body is sent in pieces, and before each piece the client looks whether the server has
answered already. So a refusal that comes while the body is still on its way (408 when it comes
too slowly, 401, 413) reaches the caller as that refusal, not as a broken connection.
"""

from __future__ import annotations

import http.client
import json
import re
import selectors
import socket
import urllib.parse

from .errors import PclError, Unavailable

API = "/api/v1"
DEFAULT_TIMEOUT_SECONDS = 180       # a question may wait for the ARC LLM (90 s, and a retry)
_TOKEN_TEXT = re.compile(r"[A-Za-z0-9_-]{1,512}")
SEND_PIECE_BYTES = 64 * 1024        # the body goes out in pieces of at most this size


def check_server_url(server_url):
    """The server's address without a trailing slash, or a PclError that says what is wrong."""
    if not isinstance(server_url, str) or not server_url.strip():
        raise PclError("Give the pcldb server's address, for example http://127.0.0.1:8787",
                       code="bad_server_url")
    text = server_url.strip()
    parts = urllib.parse.urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise PclError("%r is not the address of a pcldb server. It looks like "
                       "http://127.0.0.1:8787" % text[:200], code="bad_server_url")
    if parts.username or parts.password or "@" in parts.netloc:
        raise PclError("The server's address must not hold a user name or password. The token "
                       "is sent in a header, never in the address.", code="bad_server_url")
    if parts.query or parts.fragment:
        raise PclError("The server's address must not hold ? or #: give only "
                       "http://host:port", code="bad_server_url")
    path = parts.path.rstrip("/")
    if path.endswith(API):
        path = path[:-len(API)]
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, "", ""))


class Client:
    """One pcldb server and one token. Methods return the server's JSON answer as Python data."""

    def __init__(self, server_url, token, *, timeout=DEFAULT_TIMEOUT_SECONDS):
        self.server_url = check_server_url(server_url)
        if token is not None and (not isinstance(token, str)
                                  or not _TOKEN_TEXT.fullmatch(token.strip())):
            raise PclError("The token is not usable: a pcldb token is one line of letters, "
                           "digits, _ and - (it starts with pcl_).", code="bad_token")
        self._token = token.strip() if token else None
        self.timeout = timeout

    def __repr__(self):
        return "Client(%r, token=%s)" % (self.server_url, "<hidden>" if self._token else None)

    # -- the calls -------------------------------------------------------------------------

    def health(self):
        return self._call("GET", "/health", auth=False)

    def me(self):
        return self._call("GET", "/me")

    def status(self):
        return self._call("GET", "/status")

    def register(self, document, replace=False):
        """Register one session from its registration document (section 6.1). Returns
        {"action", "path", "subject", "web_url", "n_files", "bytes"}."""
        return self._call("POST", "/registrations",
                          {"document": document, "replace": bool(replace)})

    def registrations(self):
        return self._call("GET", "/registrations")

    def ask(self, question, include_simulated=None):
        body = {"question": question}
        if include_simulated is not None:
            body["include_simulated"] = bool(include_simulated)
        return self._call("POST", "/ask", body)

    def search(self, request):
        return self._call("POST", "/search", {"request": request})

    def session(self, path):
        return self._call("GET", "/session?path=" + urllib.parse.quote(str(path), safe=""))

    def sql(self, text, max_rows=None):
        body = {"sql": text}
        if max_rows is not None:
            body["max_rows"] = max_rows
        return self._call("POST", "/sql", body)

    def sql_schema(self):
        return self._call("GET", "/sql/schema")

    def sql_examples(self):
        return self._call("GET", "/sql/examples")

    def sql_history(self):
        return self._call("GET", "/sql/history")

    def signin(self, code):
        """Swap a one-time sign-in code (from `pcldb serve`) for a token; the client uses that
        token from now on. Returns the server's answer ({"token", "expires_utc", "user"})."""
        answer = self._call("POST", "/signin", {"code": code}, auth=False)
        self._token = answer.get("token")
        return answer

    # -- HTTP ----------------------------------------------------------------------------

    def _call(self, method, path, body=None, *, auth=True):
        data = None
        headers = [("Accept", "application/json"), ("Connection", "close")]
        if body is not None:
            try:
                data = json.dumps(body, ensure_ascii=True, allow_nan=False).encode("ascii")
            except (TypeError, ValueError) as error:
                raise PclError("The request could not be written as JSON: %s" % error,
                               code="bad_request") from None
            headers += [("Content-Type", "application/json"),
                        ("Content-Length", str(len(data)))]
        if auth:
            if not self._token:
                raise PclError("No token was given for the pcldb server. Put it into the "
                               "variable PCLDB_TOKEN or a file given with --token-file.",
                               code="no_token", status=401)
            headers.append(("Authorization", "Bearer " + self._token))
        try:
            status, raw = self._exchange(method, API + path, headers, data)
        except _ClosedWhileSending as error:
            raise Unavailable("The pcldb server at %s closed the connection while the request "
                              "was being sent (%s). The request came too slowly or was "
                              "refused; the server's log (var/log/server.log) says which."
                              % (self.server_url, _plain(error.cause)),
                              code="server_closed") from None
        except (OSError, http.client.HTTPException) as error:
            raise Unavailable("The pcldb server at %s could not be reached (%s). Is pcldb serve "
                              "running there?" % (self.server_url, _plain(error)),
                              code="server_unreachable") from None
        if 200 <= status < 300:
            return self._read(status, raw, None)
        raise self._error(status, raw)

    def _exchange(self, method, path, headers, data):
        """Send one request, read the answer: (status, body bytes). No proxy, no redirect."""
        parts = urllib.parse.urlsplit(self.server_url)
        kind = (http.client.HTTPSConnection if parts.scheme == "https"
                else http.client.HTTPConnection)
        connection = kind(parts.netloc, timeout=self.timeout)
        try:
            connection.putrequest(method, parts.path + path, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            connection.endheaders()
            whole = data is None or _send_body(connection.sock, data, self.timeout)
            try:
                reply = connection.getresponse()
                return reply.status, reply.read()
            except (OSError, http.client.HTTPException) as error:
                if whole:
                    raise
                raise _ClosedWhileSending(error) from None
        finally:
            connection.close()

    @staticmethod
    def _read(status, raw, headers):
        try:
            return json.loads(raw.decode("utf-8")) if raw else None
        except (UnicodeDecodeError, ValueError):
            raise PclError("The pcldb server answered HTTP %d with something that is not JSON."
                           % status, code="bad_answer", status=502) from None

    @staticmethod
    def _error(status, raw):
        try:
            payload = json.loads(raw.decode("utf-8"))
            error = payload["error"]
            message = str(error["message"])
            code = str(error.get("code") or "error")
            details = error.get("details")
        except (UnicodeDecodeError, ValueError, KeyError, TypeError):
            if 300 <= status < 400:
                message = ("The pcldb server answered with a redirect (HTTP %d), which is not "
                           "followed. Check the server's address." % status)
            else:
                message = "The pcldb server answered HTTP %d." % status
            return PclError(message, code="http_%d" % status, status=status)
        return PclError(message, code=code, status=status, details=details)


class _ClosedWhileSending(Exception):
    def __init__(self, cause):
        super().__init__(str(cause))
        self.cause = cause


def _send_body(sock, data, timeout):
    """Send `data` in pieces. Before each piece, look whether the server has answered (or
    closed); then stop, so that its answer can be read. True when all of it was sent; False
    when it stopped early, also when the server closed the connection during a piece (its
    answer may still be readable)."""
    view = memoryview(data)
    sent = 0
    with selectors.DefaultSelector() as watch:
        watch.register(sock, selectors.EVENT_READ | selectors.EVENT_WRITE)
        while sent < len(data):
            ready = watch.select(timeout)
            if not ready:
                raise socket.timeout("timed out")
            if any(events & selectors.EVENT_READ for _key, events in ready):
                return False
            try:
                sent += sock.send(view[sent:sent + SEND_PIECE_BYTES])
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                return False
    return True


def _plain(value, limit=200):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."
