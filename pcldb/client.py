"""A small client for a running pcldb server (specification section 11).

    from pcldb.client import Client
    client = Client("http://127.0.0.1:8787", token)
    client.register(document)            # after an upload succeeded
    client.ask("adenine sessions from last week")
    client.sql("SELECT COUNT(*) FROM sessions")

This is what an uploader, the teammate's Bridge or `pcldb register --server` calls. Standard
library only (`urllib`). The token travels only in the `Authorization` header, never in an
address; redirects are not followed (they could carry the header to another place) and no
proxy is used. Every refusal of the server becomes a `PclError` with the server's code, message,
status and details; a server that cannot be reached is `Unavailable`.
"""

from __future__ import annotations

import json
import re
import socket
import urllib.error
import urllib.parse
import urllib.request

from .errors import PclError, Unavailable

API = "/api/v1"
DEFAULT_TIMEOUT_SECONDS = 180       # a question may wait for the ARC LLM (90 s, and a retry)
_TOKEN_TEXT = re.compile(r"[A-Za-z0-9_-]{1,512}")


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None                     # the 3xx answer is raised as an HTTPError instead


def _opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())


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
        self._open = _opener().open

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
        url = self.server_url + API + path
        data = None
        request = urllib.request.Request(url, method=method)
        request.add_header("Accept", "application/json")
        if body is not None:
            try:
                data = json.dumps(body, ensure_ascii=True, allow_nan=False).encode("ascii")
            except (TypeError, ValueError) as error:
                raise PclError("The request could not be written as JSON: %s" % error,
                               code="bad_request") from None
            request.add_header("Content-Type", "application/json")
        if auth:
            if not self._token:
                raise PclError("No token was given for the pcldb server. Put it into the "
                               "variable PCLDB_TOKEN or a file given with --token-file.",
                               code="no_token", status=401)
            request.add_header("Authorization", "Bearer " + self._token)
        try:
            with self._open(request, data=data, timeout=self.timeout) as reply:
                return self._read(reply.status, reply.read(), reply.headers)
        except urllib.error.HTTPError as error:
            try:
                raw = error.read()
            except OSError:
                raw = b""
            raise self._error(error.code, raw) from None
        except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError,
                OSError) as error:
            reason = getattr(error, "reason", error)
            raise Unavailable("The pcldb server at %s could not be reached (%s). Is pcldb serve "
                              "running there?" % (self.server_url, _plain(reason)),
                              code="server_unreachable") from None

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


def _plain(value, limit=200):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."
