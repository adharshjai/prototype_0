"""The backend: the HTTP API under /api/v1 and the website's files (specification section 11).

    pcldb serve [--open] [--host H] [--port N]

A `ThreadingHTTPServer` (standard library). Every request runs in its own thread and opens its
own short-lived database connections through the store (`with store.app()`, `store.reader()`);
nothing is pooled and nothing is shared between requests. The assistant's LLM client, the
Globus Search object and the per-person question limit are made once per process.

Endpoints (JSON only; `Authorization: Bearer <token>` on everything except health, signin and
the website's files):

    GET  /api/v1/health          none      {"ok": true, "version"}
    POST /api/v1/signin          none      {"code"} -> {"token", "expires_utc", "user"}
    GET  /api/v1/me              any       user, capabilities, engine, defaults, storage
    GET  /api/v1/status          any       database, search, assistant, newest registrations
    POST /api/v1/ask             ask       {"question", "include_simulated"?}
    POST /api/v1/search          ask       {"request"} -> {"result", "backend", "notices"}
    GET  /api/v1/session?path=   ask       the session row, files, runs, labels
    POST /api/v1/sql             sql       {"sql", "max_rows"?}
    GET  /api/v1/sql/schema      sql
    GET  /api/v1/sql/examples    sql
    GET  /api/v1/sql/history     sql       the caller's last 50 SQL queries
    POST /api/v1/registrations   register  {"document", "replace"?} (replace needs admin)
    GET  /api/v1/registrations   ask       the newest 50 audit rows

Hardening (each one is tested): the Host header must name this server (else 421, against DNS
rebinding); POST needs `Content-Type: application/json` (else 415) and a Content-Length within
64 KB (16 MB for registrations; else 413, answered before the body is read); no CORS headers
at all; every response carries `X-Content-Type-Options: nosniff`, `Referrer-Policy:
no-referrer`, `Cache-Control: no-store` and the content security policy; the website's files
come from a fixed table of names, never from a path taken from the address; a token is only
accepted in the Authorization header (an address that carries one is refused); every socket
has a timeout, so a client that stops halfway cannot hold a thread for long: the request line
and headers must be in within that timeout, and a body must keep coming (at least 16 KB in
each such time, and all of it at 16 KB/s on average), else the answer is 408 `timeout`. A
refusal sent before the body is read (401, 403) is followed by reading the rest of the body
64 KB at a time and throwing it away, so that a client that sends the whole body before it
reads (urllib) still gets the refusal.

The log file `var/log/server.log` gets one line per request (method, path WITHOUT the query
text, status, user name, time) and the stack trace of unexpected failures. It never holds a
token, a header, a question, SQL text or a key.
"""

from __future__ import annotations

import contextlib
import errno
import json
import logging
import logging.handlers
import os
import re
import secrets
import signal
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs

from . import __version__, util
from .errors import Forbidden, NotFound, PclError, TooLarge, Unauthorized, Unavailable

API = "/api/v1"
WEB_DIR = Path(__file__).resolve().parent / "web"

SMALL_BODY = 64 * 1024                  # every POST but registrations
LARGE_BODY = 16 * 1024 * 1024           # POST /registrations
SOCKET_TIMEOUT_SECONDS = 30             # a stalled client frees its thread after this
# A request body must keep coming: at least BODY_BYTES_PER_WINDOW in every socket-timeout
# window (16 KB in 30 s), and all of it within the socket timeout plus one second for each
# BODY_BYTES_PER_SECOND (16 MB: about 17 minutes, a link of 128 kbit/s).
BODY_BYTES_PER_WINDOW = 16 * 1024
BODY_BYTES_PER_SECOND = 16 * 1024
BODY_CHUNK = 64 * 1024                  # a body is read (or thrown away) this much at a time
MAX_TARGET_CHARS = 8192                 # the request target (path and query)
SIGNIN_SECONDS = 300                    # a sign-in code works once, for five minutes
SIGNIN_TOKEN_DAYS = 0.5                 # the website's token: twelve hours
NEWEST_REGISTRATIONS = 10               # in /status
LIST_LIMIT = 50                         # /registrations, /sql/history
FILES_SHOWN = 500                       # /session
DRAIN_BYTES = 1024 * 1024               # after a body that is too large: read at most this ...
DRAIN_SECONDS = 1.0                     # ... for at most this long, then close
LOG_BYTES = 5 * 1024 * 1024
LOG_BACKUPS = 3

CONTENT_SECURITY_POLICY = (
    "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")

SECURITY_HEADERS = (
    ("X-Content-Type-Options", "nosniff"),
    ("Referrer-Policy", "no-referrer"),
    ("Cache-Control", "no-store"),
    ("Content-Security-Policy", CONTENT_SECURITY_POLICY),
)

# The website: a fixed table of names. Nothing else is ever read from the web folder.
STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

# A query parameter with one of these names would put a secret into an address (and so into
# browser history and logs): refused, whatever the endpoint.
_SECRET_PARAMETERS = {"token", "access_token", "auth", "authorization", "api_key", "apikey",
                      "key", "password", "bearer"}

ANY = "any"     # needs a valid token, no particular capability

_UNSAFE_LOG_TEXT = re.compile(r"[^\x20-\x7e]")
_HAS_PORT = re.compile(r"(\[[^\]]+\]|[^:\[\]]+):[0-9]+")   # host:port or [v6]:port

SIGNIN_REFUSED = ("That sign-in code is not accepted: it was already used or is more than five "
                  "minutes old. Start pcldb serve again for a new sign-in link, or sign in with "
                  "a token.")


# ---------------------------------------------------------------------- small helpers


def _json_bytes(value):
    """JSON as the API sends it: ASCII only (every other character escaped), no NaN."""
    return json.dumps(value, ensure_ascii=True, allow_nan=False, default=str,
                      separators=(",", ":")).encode("ascii")


def _reject_constant(name):
    raise ValueError("%s is not a number JSON allows" % name)


def _unique_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("the key %r appears twice" % key)
        result[key] = value
    return result


def parse_json_body(raw):
    """The body of a POST as a dict, or a PclError (400) that says what is wrong."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise PclError("The request body is not UTF-8 text.", code="bad_json") from None
    if text.startswith("\ufeff"):
        text = text[1:]
    if not text.strip():
        raise PclError("The request has no body. Send a JSON object.", code="bad_json")
    try:
        value = json.loads(text, parse_constant=_reject_constant,
                           object_pairs_hook=_unique_keys)
    except RecursionError:
        raise PclError("The request body is nested too deeply.", code="bad_json") from None
    except ValueError as error:
        raise PclError("The request body is not valid JSON: %s" % _short(error, 200),
                       code="bad_json") from None
    if not isinstance(value, dict):
        raise PclError("The request body must be a JSON object ({...}).", code="bad_json")
    return value


def _short(value, limit=120):
    text = " ".join(str(value).split())
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _check_keys(body, allowed, required=()):
    for key in body:
        if key not in allowed:
            raise PclError("The request has the key %s, which this endpoint does not know. Known "
                           "keys: %s." % (json.dumps(_short(key, 60)), ", ".join(allowed)),
                           code="bad_request", details={"key": _short(key, 60)})
    for key in required:
        if key not in body:
            raise PclError("The request needs the key \"%s\"." % key, code="bad_request",
                           details={"key": key})


def _optional_bool(body, key):
    value = body.get(key)
    if value is not None and not isinstance(value, bool):
        raise PclError("\"%s\" must be true or false." % key, code="bad_request",
                       details={"key": key})
    return value


def _log_text(text, limit=300):
    """Text that may go into the log: printable ASCII, one line, no query strings."""
    text = re.sub(r"\?\S*", "?...", str(text))
    text = _UNSAFE_LOG_TEXT.sub("?", text)
    return text[:limit]


# ---------------------------------------------------------------------- the sign-in code


class SigninCode:
    """The one-time sign-in code of `pcldb serve`: 32 url-safe characters, works once, for
    five minutes, kept only in memory."""

    def __init__(self, clock=time.monotonic, seconds=SIGNIN_SECONDS):
        self._clock = clock
        self._seconds = seconds
        self._lock = threading.Lock()
        self._code = None
        self._until = 0.0

    def new(self):
        """Make a new code (the old one stops working) and return it."""
        code = secrets.token_urlsafe(24)            # 24 random bytes = 32 characters
        with self._lock:
            self._code = code
            self._until = self._clock() + self._seconds
        return code

    @contextlib.contextmanager
    def spend(self, code):
        """`with signin.spend(code):` runs the block when the code is good, and the code is
        used up when the block ends without an error. `Unauthorized` when it is not good."""
        with self._lock:
            good = (isinstance(code, str) and self._code is not None
                    and self._clock() < self._until
                    and secrets.compare_digest(code.encode("utf-8", "replace"),
                                               self._code.encode("ascii")))
            if not good:
                raise Unauthorized(SIGNIN_REFUSED, code="signin_refused")
            yield
            self._code = None


# ---------------------------------------------------------------------- the application


class App:
    """What every request needs: settings, the store, the assistant, Globus Search, the
    sign-in code, the log. One per server process."""

    def __init__(self, settings, *, store=None, llm=None, use_settings_llm=True, globus=None,
                 log=None, socket_timeout=SOCKET_TIMEOUT_SECONDS, clock=time.monotonic):
        from . import ask
        from .store import open_store
        self.settings = settings
        self.store = store if store is not None else open_store(settings)
        # The assistant: one client per process (it carries the limit of requests in flight).
        self.llm = ask.client_for(settings) if (llm is None and use_settings_llm) else llm
        self._globus = globus
        self._globus_lock = threading.Lock()
        self.limiter = ask.RateLimiter()
        self.signin = SigninCode(clock=clock)
        self.socket_timeout = socket_timeout
        self.log = log or _null_log()
        self._engine_version = None
        self._sandbox = None
        self._sandbox_lock = threading.Lock()

    # -- things made on first use ---------------------------------------------------------

    @property
    def globus(self):
        """The one GlobusSearch of this process (made on first use; it imports nothing and
        opens nothing until it is asked something)."""
        with self._globus_lock:
            if self._globus is None:
                from . import search_globus
                self._globus = search_globus.shared(self.settings)
            return self._globus

    def engine_version(self):
        if self._engine_version is None:
            version = self.store.describe().get("version") or "unknown"
            if version == "unknown":
                return version
            self._engine_version = version
        return self._engine_version

    def sandbox_state(self):
        """What the PostgreSQL hardening check says about the expert-mode reader, asked once:
        {"problems": [...], "notes": [...]}. Empty lists on SQLite."""
        with self._sandbox_lock:
            if self._sandbox is None:
                state = {"problems": [], "notes": []}
                if self.store.engine == "postgresql":
                    from .store import pg_hardening
                    try:
                        state = pg_hardening.check(self.store)
                    except PclError:
                        return {"problems": [], "notes": []}     # asked again next time
                self._sandbox = state
            return self._sandbox

    def expert_note(self):
        """One sentence for /status and doctor when the reader's limits are weaker than they
        should be (from_lead 20b), else None."""
        state = self.sandbox_state()
        if state["problems"]:
            return ("Expert mode is switched off: the PostgreSQL role for expert SQL could read "
                    "private data or change something (%s). Run pcldb doctor."
                    % "; ".join(state["problems"]))
        if state["notes"]:
            return EXPERT_NOTE_WEAKER
        return None


EXPERT_NOTE_WEAKER = (
    "On this PostgreSQL server pcldb could not apply every limit to the role for expert SQL "
    "(its owner is not a superuser): expert SQL still cannot read private data or change "
    "anything, but a query can call pg_sleep and set_config, take advisory locks, end other "
    "expert queries and fill temporary files until its time limit.")


def _null_log():
    log = logging.Logger("pcldb.server.quiet")
    log.addHandler(logging.NullHandler())
    log.propagate = False
    return log


def file_log(path):
    """A logger that writes to `path` (var/log/server.log), rotated at 5 MB."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=LOG_BYTES,
                                                   backupCount=LOG_BACKUPS, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log = logging.Logger("pcldb.server")
    log.addHandler(handler)
    log.propagate = False
    return log, handler


# ---------------------------------------------------------------------- the endpoints


class Request:
    """What an endpoint function gets."""

    def __init__(self, app, user, body, query):
        self.app = app
        self.user = user
        self.body = body
        self.query = query


def _health(request):
    return {"ok": True, "version": __version__}


def _signin(request):
    from . import auth, cli
    body = request.body
    _check_keys(body, ("code",), required=("code",))
    code = body["code"]
    if not isinstance(code, str) or not code or len(code) > 200:
        raise PclError("\"code\" must be the sign-in code from the address pcldb serve printed.",
                       code="bad_request")
    app = request.app
    with app.signin.spend(code):
        with app.store.app() as db:
            info = auth.issue_token_info(db, cli.ADMIN_NAME, label="website sign-in",
                                         days=SIGNIN_TOKEN_DAYS)
            user = auth.get_user(db, cli.ADMIN_NAME)
    return {"token": info["token"], "expires_utc": info["expires_utc"], "user": user.public()}


def _me(request):
    app, user, settings = request.app, request.user, request.app.settings
    storage = settings.storage
    return {
        "user": user.public(),
        "capabilities": list(user.capabilities),
        "engine": {"name": app.store.engine, "version": app.engine_version()},
        "defaults": {"include_simulated": bool(settings.ask.include_simulated_default),
                     "sql_max_rows": settings.sql.max_rows},
        "storage": {"ssh_host": storage.ssh_host, "project_path": storage.project_path,
                    "collection_id": storage.collection_id},
        "version": __version__,
    }


def _newest_registrations(db, limit):
    return db.query("SELECT at_utc, user_name, session_path, action, source "
                    "FROM {app}.registrations ORDER BY at_utc DESC, reg_id DESC LIMIT ?",
                    [limit])


def _status(request):
    from . import demo, llm, search_sync
    app, settings = request.app, request.app.settings
    described = app.store.describe()
    with app.store.app() as db:
        counts = {name: int(db.scalar("SELECT COUNT(*) FROM {pcl}.%s" % name) or 0)
                  for name in ("sessions", "files", "runs", "points")}
        instruments = db.query("SELECT instrument, COUNT(*) AS n_sessions FROM {pcl}.sessions "
                               "GROUP BY instrument")
        newest = db.scalar("SELECT MAX(started_utc) AS started_utc FROM {pcl}.sessions")
        is_demo = bool(demo.is_loaded(db))
        entries = search_sync.status(db)
        registrations = _newest_registrations(db, NEWEST_REGISTRATIONS)
    instruments = sorted(({"instrument": row["instrument"], "sessions": int(row["n_sessions"])}
                          for row in instruments),
                         key=lambda item: (-item["sessions"], item["instrument"]))
    database = {
        "engine": app.store.engine, "version": described.get("version") or "unknown",
        "where": described.get("where") or "unknown",
        "sessions": counts["sessions"], "files": counts["files"], "runs": counts["runs"],
        "points": counts["points"], "demo": is_demo, "instruments": instruments,
        "newest_started_utc": newest,
    }
    note = app.expert_note()
    if note:
        database["expert_note"] = note
    return {
        "database": database,
        "search": {"backend": settings.search.backend,
                   "index_set": bool(settings.search.index_id),
                   "signed_in": bool(app.globus.signed_in()),
                   "entries": entries},
        "assistant": llm.assistant_status(settings),
        "registrations": registrations,
    }


def _ask(request):
    from . import ask
    body = request.body
    _check_keys(body, ("question", "include_simulated"), required=("question",))
    include_simulated = _optional_bool(body, "include_simulated")
    app = request.app
    return ask.answer(body["question"], store=app.store, settings=app.settings, llm=app.llm,
                      user=request.user, include_simulated=include_simulated,
                      limiter=app.limiter)


def _search(request):
    from . import search
    body = request.body
    _check_keys(body, ("request",), required=("request",))
    if not isinstance(body["request"], dict):
        raise PclError("\"request\" must be a search request: a JSON object such as "
                       "{\"q\": \"*\", \"limit\": 10}.", code="bad_search_request")
    app = request.app
    started = time.monotonic()
    checked = None
    try:
        checked = search.validate_request(body["request"])
        globus = app.globus if app.settings.search.backend == "globus" else None
        result, backend, notices = search.run_store(app.store, checked, app.settings,
                                                    globus=globus)
    except PclError as error:
        _search_log(app, request.user, checked, ok=False, error=error, started=started)
        raise
    count = len(result.get("gmeta") or [])
    _search_log(app, request.user, checked, ok=True, n_rows=count, backend=backend,
                started=started)
    return {"result": result, "backend": backend, "notices": list(notices or [])}


def _search_log(app, user, checked, *, ok, started, n_rows=None, backend=None, error=None):
    """One row of app.query_log (mode search). A failed write never hides a refusal."""
    row = {
        "log_id": util.new_id(), "at_utc": util.now_utc_text(),
        "user_name": getattr(user, "name", None), "mode": "search",
        "query_text": util.canonical_json(checked)[:2000] if checked is not None
        else "(a request that was refused before it ran)",
        "request_json": checked, "ok": bool(ok),
        "error_text": None if error is None else ("%s: %s" % (error.code, error.message))[:2000],
        "n_rows": n_rows, "elapsed_ms": int((time.monotonic() - started) * 1000),
        "backend": backend,
    }
    try:
        with app.store.app() as db:
            db.insert("app.query_log", row)
    except Exception:
        if ok:
            raise
        app.log.warning("the audit row of a refused search could not be written")


_SESSION_PATH_MAX = 1024


def _session(request):
    values = request.query.get("path") or []
    if len(values) != 1 or not values[0]:
        raise PclError("Say which session: /api/v1/session?path=raw/<instrument>/<folder>",
                       code="bad_request", details={"key": "path"})
    path = values[0]
    if len(path) > _SESSION_PATH_MAX or any(char < " " or char == "\x7f" for char in path):
        raise PclError("That is not a session path (at most 1024 characters, no control "
                       "characters).", code="bad_request", details={"key": "path"})
    with request.app.store.app() as db:
        session = db.one("SELECT * FROM {pcl}.sessions WHERE path = ?", [path])
        if session is None:
            raise NotFound("No session is registered at %s." % json.dumps(_short(path, 200)),
                           code="no_such_session")
        total = int(db.scalar("SELECT COUNT(*) FROM {pcl}.files WHERE session_path = ?",
                              [path]) or 0)
        items = db.query("SELECT rel_path, bytes, sha256, listed FROM {pcl}.files "
                         "WHERE session_path = ? ORDER BY rel_path LIMIT ?", [path, FILES_SHOWN])
        runs = db.query("SELECT path, folder, kind, made_utc, model_name, run_by, valid "
                        "FROM {pcl}.runs WHERE session_path = ?", [path])
        rollup = db.one("SELECT label_counts, review_state, n_needs_review "
                        "FROM {pcl}.session_rollup WHERE session_path = ?", [path])
    runs.sort(key=lambda run: (run["made_utc"] or "", run["path"]), reverse=True)
    labels = []
    for item in (rollup or {}).get("label_counts") or []:
        if isinstance(item, dict):
            labels.append({"label": item.get("label"), "count": item.get("count")})
    return {
        "session": session,
        "files": {"total": total, "shown": len(items), "items": items},
        "runs": runs,
        "labels": labels,
        "review_state": (rollup or {}).get("review_state") or "none",
        "n_needs_review": int((rollup or {}).get("n_needs_review") or 0),
    }


def _sql(request):
    from . import sqlrun
    body = request.body
    _check_keys(body, ("sql", "max_rows"), required=("sql",))
    sql = body["sql"]
    if not isinstance(sql, str):
        raise PclError("\"sql\" must be text: one SELECT statement.", code="bad_request",
                       details={"key": "sql"})
    max_rows = body.get("max_rows")
    if max_rows is not None and (isinstance(max_rows, bool) or not isinstance(max_rows, int)):
        raise PclError("\"max_rows\" must be a whole number of at least 1.", code="bad_request",
                       details={"key": "max_rows"})
    app = request.app
    if app.sandbox_state()["problems"]:
        raise Unavailable(app.expert_note(), code="sandbox_unsafe")
    return sqlrun.run_sql(app.store, sql, settings=app.settings, max_rows=max_rows,
                          user_name=request.user.name)


def _sql_schema(request):
    from . import sqlrun
    return sqlrun.describe(request.app.store)


def _sql_examples(request):
    from . import sql_examples
    engine = request.app.store.engine
    return {"engine": engine, "examples": sql_examples.examples(engine)}


def _sql_history(request):
    from . import sqlrun
    with request.app.store.app() as db:
        items = sqlrun.history(db, request.user.name, LIST_LIMIT)
    return {"items": items}


def _register(request):
    from . import auth, registration
    body = request.body
    _check_keys(body, ("document", "replace"), required=("document",))
    replace = bool(_optional_bool(body, "replace"))
    if replace:
        auth.require(request.user, "manage")
    document = body["document"]
    if not isinstance(document, dict):
        raise PclError("\"document\" must be a registration document (a JSON object with "
                       "\"schema\": \"pcl-registration/1\").", code="invalid_document")
    app = request.app
    return registration.register(app.store, document, actor=request.user, source="upload",
                                 settings=app.settings, replace=replace)


def _registrations(request):
    with request.app.store.app() as db:
        return {"items": _newest_registrations(db, LIST_LIMIT)}


# (method, path) -> (function, what the caller needs: None / ANY / a capability, body limit)
ROUTES = {
    ("GET", API + "/health"): (_health, None, 0),
    ("POST", API + "/signin"): (_signin, None, SMALL_BODY),
    ("GET", API + "/me"): (_me, ANY, 0),
    ("GET", API + "/status"): (_status, ANY, 0),
    ("POST", API + "/ask"): (_ask, "ask", SMALL_BODY),
    ("POST", API + "/search"): (_search, "ask", SMALL_BODY),
    ("GET", API + "/session"): (_session, "ask", 0),
    ("POST", API + "/sql"): (_sql, "sql", SMALL_BODY),
    ("GET", API + "/sql/schema"): (_sql_schema, "sql", 0),
    ("GET", API + "/sql/examples"): (_sql_examples, "sql", 0),
    ("GET", API + "/sql/history"): (_sql_history, "sql", 0),
    ("POST", API + "/registrations"): (_register, "register", LARGE_BODY),
    ("GET", API + "/registrations"): (_registrations, "ask", 0),
}
_PATHS = {path for _method, path in ROUTES}


# ---------------------------------------------------------------------- HTTP


class _Refused(Exception):
    """A response that ends the request early: (status, error dict, extra headers, close)."""

    def __init__(self, status, code, message, headers=(), drain=False):
        super().__init__(message)
        self.status, self.code, self.message = status, code, message
        self.headers, self.drain = tuple(headers), drain


class Handler(BaseHTTPRequestHandler):
    server_version = "pcldb"
    sys_version = ""

    def version_string(self):
        return "pcldb"
    protocol_version = "HTTP/1.0"       # one request per connection: nothing idles in a thread

    # -- plumbing ---------------------------------------------------------------------------

    def setup(self):
        self.timeout = self.server.app.socket_timeout
        super().setup()
        # The socket timeout bounds each single wait for data; this bounds the request line and
        # headers. A client that sends one byte every few seconds is cut off once they are not
        # in after the same number of seconds. A body is timed by _receive, by its progress.
        self._reading = None
        self._timed_out = False
        self._status = None
        self._read_within(self.timeout)

    def _read_within(self, seconds):
        self._request_is_in()
        timer = threading.Timer(seconds, self._stop_reading)
        timer.daemon = True
        self._reading = timer
        timer.start()

    def _stop_reading(self):
        """The time is up: shut only the reading side, so the 408 can still be written."""
        self._timed_out = True
        try:
            self.connection.shutdown(socket.SHUT_RD)
        except OSError:
            pass

    def _request_is_in(self):
        timer = getattr(self, "_reading", None)
        if timer is not None:
            timer.cancel()
            self._reading = None

    def finish(self):
        self._request_is_in()
        super().finish()

    def log_request(self, code="-", size="-"):
        pass                            # one line per request is written by _finish_log

    def log_message(self, format, *args):
        try:
            text = format % args
        except Exception:
            text = format
        self.server.app.log.info("%s %s", self.client_address[0], _log_text(text))

    def send_error(self, code, message=None, explain=None):
        """Errors found by the standard library while it reads the request line and headers
        (bad request line, too long, wrong version): answered as JSON like every other error."""
        self.close_connection = True
        if self._timed_out:     # the request line was cut off by the deadline, not malformed
            self._send_timeout()
            self._finish_log(408, None)
            return
        words = {400: "The request could not be read.", 414: "The address is too long.",
                 431: "The request's headers are too large.",
                 505: "Only HTTP/1.0 and HTTP/1.1 are spoken here."}
        try:
            self._send_json(int(code), {"error": {"code": "bad_request",
                                                  "message": words.get(int(code),
                                                                       "The request was "
                                                                       "refused."),
                                                  "details": None}})
        except OSError:
            pass
        self._finish_log(int(code), None)

    def _send(self, status, body, content_type, extra=()):
        if self.request_version == "HTTP/0.9":
            self.request_version = "HTTP/1.0"   # always a status line and headers
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for name, value in SECURITY_HEADERS:
            self.send_header(name, value)
        for name, value in extra:
            self.send_header(name, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)
        self._status = status

    def _send_json(self, status, value, extra=()):
        self._send(status, _json_bytes(value), "application/json; charset=utf-8", extra)

    def _error(self, status, code, message, details=None, extra=()):
        self._send_json(status, {"error": {"code": code, "message": message,
                                           "details": details}}, extra)

    def _finish_log(self, status, user):
        started = getattr(self, "_started", None)
        elapsed = int((time.monotonic() - started) * 1000) if started else 0
        path = getattr(self, "_path", None) or "-"
        self.server.app.log.info("%s %s %s %s %s %dms%s", self.client_address[0],
                                 _log_text(getattr(self, "command", None) or "-", 10),
                                 _log_text(path, 200), status,
                                 _log_text(getattr(user, "name", None) or "-", 64), elapsed,
                                 " timeout" if status == 408 else "")

    def _send_timeout(self):
        self._safe_error(408, "timeout", "The request was not sent in time.")

    def _end_answer(self):
        """The answer is complete: tell the client so (it may still be sending)."""
        try:
            self.wfile.flush()
            self.connection.shutdown(socket.SHUT_WR)
        except OSError:
            return False
        return True

    def _drain(self, limit):
        """After refusing a body that is too large: stop sending, read a little of what the
        client still sends (so the refusal reaches it instead of a reset connection), then give
        up. Such a body is never read in full."""
        if not self._end_answer():
            return
        deadline = time.monotonic() + DRAIN_SECONDS
        left = min(limit, DRAIN_BYTES)
        try:
            while left > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self.connection.settimeout(remaining)
                chunk = self.connection.recv(min(BODY_CHUNK, left))
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass

    def _discard_body(self, length):
        """After a refusal sent before the body was read (401, 403): the body is within the
        route's limit, so read the rest of it and throw it away, 64 KB at a time, under the
        same deadline as a body that is kept. A client that sends the whole body before it
        reads the answer (urllib, so pcldb.client) then gets the refusal, not a reset."""
        if not self._end_answer():
            return
        try:
            self._receive(length, keep=False)
        except (_Refused, OSError):     # a timeout is an OSError too: nothing more to say
            pass

    def _receive(self, length, keep=True):
        """Read a body of `length` bytes, 64 KB at a time; the bytes, or b"" when not kept.

        The body must keep coming: at least BODY_BYTES_PER_WINDOW (or the rest) in every
        window of the socket timeout, and all of it within the socket timeout plus one second
        for each BODY_BYTES_PER_SECOND. Else the reading side is shut and TimeoutError is
        raised (answered with 408). A body that ends early is 400."""
        kept = []
        left = length
        started = time.monotonic()
        last_moment = started + self.timeout + length / BODY_BYTES_PER_SECOND
        window_end = started + self.timeout
        needed, got = min(BODY_BYTES_PER_WINDOW, left), 0
        try:
            while left > 0:
                now = time.monotonic()
                if now >= window_end:
                    if got < needed:
                        raise TimeoutError("too little of the body came in time")
                    window_end, needed, got = now + self.timeout, min(BODY_BYTES_PER_WINDOW,
                                                                      left), 0
                if now >= last_moment:
                    raise TimeoutError("the body did not come in time")
                wait = min(self.timeout, last_moment - now)
                if got < needed:
                    wait = min(wait, window_end - now)
                self.connection.settimeout(max(wait, 0.001))
                chunk = self.rfile.read1(min(BODY_CHUNK, left))
                if not chunk:
                    if self._timed_out:
                        raise TimeoutError("the reading side was shut")
                    raise _Refused(400, "bad_request", "The request body ended before its "
                                                       "stated length.")
                left -= len(chunk)
                got += len(chunk)
                if keep:
                    kept.append(chunk)
        except TimeoutError:
            self._stop_reading()
            raise
        finally:
            try:
                self.connection.settimeout(self.timeout)    # for the answer
            except OSError:
                pass
        return b"".join(kept)

    # -- the methods ------------------------------------------------------------------------

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_HEAD(self):
        self._handle()

    def do_PUT(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_OPTIONS(self):
        self._handle()

    # -- one request --------------------------------------------------------------------------

    def _handle(self):
        if self.command != "POST":
            self._request_is_in()       # the request line and headers are all there is
        self._started = time.monotonic()
        self._status = None
        self._user = None
        self._body_pending = 0
        self.close_connection = True
        if self._timed_out:             # the headers were cut off by the deadline
            self._path = self.path.partition("?")[0][:200] if self.path.startswith("/") else "-"
            self._send_timeout()
            self._finish_log(408, None)
            return
        try:
            self._route()
        except _Refused as refused:
            self._safe_error(refused.status, refused.code, refused.message, None,
                             refused.headers)
            if refused.drain:
                self._drain(int(self.headers.get("Content-Length") or 0))
        except PclError as error:
            extra = ()
            if error.status == 401:
                extra = (("WWW-Authenticate", 'Bearer realm="pcldb"'),)
            details = error.details if isinstance(error.details, (dict, list)) else None
            self._safe_error(error.status, error.code, error.message, details, extra)
            if self._body_pending:      # refused before the body was read (401, 403)
                self._discard_body(self._body_pending)
        except (BrokenPipeError, ConnectionResetError):
            self._status = 499          # the client went away
        except TimeoutError:
            self._send_timeout()
        except Exception:
            self.server.app.log.error("unexpected failure in %s %s\n%s",
                                      _log_text(self.command, 10),
                                      _log_text(getattr(self, "_path", "-"), 200),
                                      traceback.format_exc())
            self._safe_error(500, "internal_error",
                             "Something went wrong inside pcldb. The details are in the "
                             "server's log (var/log/server.log).")
        self._finish_log(self._status or "-", self._user)

    def _safe_error(self, status, code, message, details=None, extra=()):
        if self._status is not None:
            return                      # a response is on its way already
        try:
            self._error(status, code, message, details, extra)
        except OSError:
            self._status = status

    def _route(self):
        app = self.server.app
        self._check_host()
        target = self.path
        if len(target) > MAX_TARGET_CHARS:
            raise _Refused(414, "too_long", "The address is too long.")
        if not target.startswith("/"):
            raise _Refused(400, "bad_request", "The request names no path on this server.")
        # Origin form only: the path is everything before ? (never decoded, never resolved).
        path, _, query_text = target.split("#", 1)[0].partition("?")
        self._path = path
        try:
            query = parse_qs(query_text, keep_blank_values=True, max_num_fields=50) \
                if query_text else {}
        except ValueError:
            raise _Refused(400, "bad_request", "The address has too many parameters.") from None
        if any(name.strip().lower() in _SECRET_PARAMETERS for name in query):
            raise _Refused(400, "token_in_address",
                           "A token or key is never accepted in the address. Send the token in "
                           "the header: Authorization: Bearer <token>")

        if path in STATIC_FILES:
            if self.command not in ("GET", "HEAD"):
                raise _Refused(405, "method_not_allowed", "Only GET is answered here.",
                               (("Allow", "GET"),))
            self._static(path)
            return None
        route = ROUTES.get((self.command, path))
        if route is None:
            if path in _PATHS:
                allowed = sorted(method for method, known in ROUTES if known == path)
                raise _Refused(405, "method_not_allowed",
                               "%s is not answered for %s. Use: %s."
                               % (_log_text(self.command, 10), path, ", ".join(allowed)),
                               (("Allow", ", ".join(allowed)),))
            raise _Refused(404, "not_found", "There is nothing at this address.")
        function, needs, limit = route

        body = {}
        length = 0
        if self.command == "POST":
            length = self._body_length(limit)   # 415 / 411 / 413 come before any work
            self._body_pending = length
            self._request_is_in()       # the headers are in; _receive times the body
        user = None
        if needs is not None:
            # The token and the role are checked BEFORE the body is read: a request without a
            # valid token never makes the server hold up to 16 MB of it in memory.
            user = self._user = self._authenticate()
            if needs != ANY:
                from . import auth
                auth.require(user, needs)
        if self.command == "POST":
            self._body_pending = 0      # from here a failure ends the reading itself
            raw = self._receive(length)
            body = parse_json_body(raw)
        result = function(Request(app, user, body, query))
        self._send_json(200, result)

    def _check_host(self):
        host = (self.headers.get("Host") or "").strip().lower()
        port = self.server.server_address[1]
        allowed = {"127.0.0.1:%d" % port, "localhost:%d" % port, "[::1]:%d" % port}
        for name in self.server.app.settings.server.allowed_hosts:
            name = str(name).strip().lower()
            if name:
                allowed.add(name)
                if not _HAS_PORT.fullmatch(name):
                    allowed.add("%s:%d" % (name, port))
        if host not in allowed:
            self._path = self.path.partition("?")[0][:200] if self.path.startswith("/") else "-"
            raise _Refused(421, "wrong_host",
                           "This pcldb server only answers requests addressed to "
                           "127.0.0.1:%d or localhost:%d. To reach it under another name, add "
                           "that name to server.allowed_hosts in pcldb_config.json."
                           % (port, port))

    def _body_length(self, limit):
        """The checked Content-Length of a POST body (nothing of the body is read yet)."""
        content_type = self.headers.get_content_type()
        charset = (self.headers.get_content_charset() or "utf-8").lower()
        if content_type != "application/json" or charset not in ("utf-8", "utf8"):
            raise _Refused(415, "wrong_content_type",
                           "Send the body as JSON with the header Content-Type: "
                           "application/json.")
        if self.headers.get("Transfer-Encoding"):
            raise _Refused(411, "length_required",
                           "Send the body with a Content-Length header; bodies in chunks are "
                           "not accepted.", drain=False)
        length_text = (self.headers.get("Content-Length") or "").strip()
        if not length_text.isdigit() or len(length_text) > 12:
            raise _Refused(411, "length_required",
                           "Send the body with a Content-Length header.")
        length = int(length_text)
        if length > limit:
            raise _Refused(413, TooLarge.code,
                           "The request body is %d bytes; at most %d bytes are accepted here."
                           % (length, limit), drain=True)
        return length

    def _authenticate(self):
        from . import auth
        header = self.headers.get("Authorization") or ""
        scheme, _, token = header.strip().partition(" ")
        if not header:
            raise Unauthorized("Sign in first: send your token in the header Authorization: "
                               "Bearer <token>.", code="unauthorized")
        if scheme.lower() != "bearer" or not token.strip():
            raise Unauthorized("The Authorization header must be: Bearer <token>.",
                               code="unauthorized")
        user = auth.authenticate_store(self.server.app.store, token.strip())
        if user is None:
            raise Unauthorized("This token is not accepted: it is unknown, revoked or expired, "
                               "or its user is switched off. Sign in again.",
                               code="unauthorized")
        return user

    def _static(self, path):
        name, content_type = STATIC_FILES[path]
        try:
            data = (WEB_DIR / name).read_bytes()
        except OSError:
            raise _Refused(404, "not_found", "The website's file %s is missing from this copy "
                                             "of pcldb." % name) from None
        self._send(200, data, content_type)


class Server(ThreadingHTTPServer):
    """The HTTP server; `server.app` holds what the requests share."""

    daemon_threads = True
    allow_reuse_address = os.name != "nt"   # on Windows that flag would let others share the port
    request_queue_size = 64

    def __init__(self, address, app):
        self.app = app
        if ":" in address[0]:
            self.address_family = socket.AF_INET6
        super().__init__(address, Handler)

    def handle_error(self, request, client_address):
        # Errors outside a request's own handling (for example a reset during setup): logged,
        # never printed with the request's text.
        self.app.log.warning("connection problem with %s: %s", client_address[0],
                             _log_text(sys.exc_info()[1], 200))

    @property
    def port(self):
        return self.server_address[1]


def make_server(settings, *, host=None, port=None, **app_options):
    """Bind the server (it does not serve yet). `port=0` picks a free port (tests)."""
    host = settings.server.host if host is None else host
    port = settings.server.port if port is None else port
    app = App(settings, **app_options)
    try:
        return Server((host, port), app)
    except OSError as error:
        if port and util.port_in_use(port, host if ":" not in host else "127.0.0.1"):
            raise PclError("Port %d on this computer is already in use (perhaps pcldb serve is "
                           "running already). Stop the other program, or choose another port: "
                           "pcldb serve --port %d" % (port, port + 1), code="port_in_use",
                           status=503) from None
        raise PclError("The website could not listen on %s port %s: %s"
                       % (host, port, error.strerror or error), code="cannot_listen",
                       status=503) from None


def address_for(host, port):
    """The address to print and open: a wildcard host is reached through 127.0.0.1."""
    if host in ("", "0.0.0.0", "::"):
        host = "127.0.0.1"
    if ":" in host:
        host = "[%s]" % host
    return "http://%s:%d/" % (host, port)


# ---------------------------------------------------------------------- serve


def _say(text="", stream=None):
    stream = stream or sys.stdout
    stream.write(str(text).encode("ascii", "backslashreplace").decode("ascii") + "\n")
    stream.flush()


@contextlib.contextmanager
def _stop_signals_as_ctrl_c():
    """SIGTERM, and CTRL_BREAK (SIGBREAK) on Windows, end `serve` the way Ctrl+C does, so
    the database it started is stopped too. Only possible in the main thread."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return

    def interrupt(_number, _frame):
        raise KeyboardInterrupt

    kept = []
    for name in ("SIGTERM", "SIGBREAK"):
        number = getattr(signal, name, None)
        if number is None:
            continue
        try:
            kept.append((number, signal.signal(number, interrupt)))
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for number, handler in kept:
            try:
                signal.signal(number, handler)
            except (ValueError, OSError):
                pass


def _admin_problem(store):
    """A sentence when the sign-in link cannot work (no admin, or the admin is switched off)."""
    from . import auth, cli
    with store.app() as db:
        admin = auth.find_user(db, cli.ADMIN_NAME)
    if admin is None:
        return ("Note: there is no user \"%s\", so the sign-in link will not work. Sign in with "
                "a token instead (pcldb token new --user <name>)." % cli.ADMIN_NAME)
    if admin.disabled:
        return ("Note: the user \"%s\" is switched off, so the sign-in link will not work. "
                "Switch it on (pcldb user enable %s) or sign in with another user's token."
                % (cli.ADMIN_NAME, cli.ADMIN_NAME))
    return None


SERVE_LOCK_FILE = "serve.lock"
SERVE_INFO_FILE = "serve.json"


class ServeLock:
    """One `pcldb serve` per project folder: an operating-system lock on var/serve.lock, held
    while the server runs (the system lets go of it when the process ends, also after a crash).
    var/serve.json says which process holds it and on which port, for the refusal."""

    def __init__(self, settings):
        self.var_dir = settings.var_dir
        self.path = self.var_dir / SERVE_LOCK_FILE
        self.info = self.var_dir / SERVE_INFO_FILE
        self.handle = None
        self.locked = False
        self.note = None        # a sentence for serve's window when the lock is not available

    @staticmethod
    def _held_elsewhere(error):
        """True when a locking error means that another process holds the lock. Any other
        error (ENOTSUP, ENOLCK, ... on a network folder) means locks do not work here."""
        held = {errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES}
        if os.name == "nt":
            held.add(getattr(errno, "EDEADLOCK", 36))      # msvcrt.locking: EACCES or EDEADLOCK
        return error.errno in held

    def _lock(self, handle):
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self, handle):
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def acquire(self, port):
        self.var_dir.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            self._lock(handle)
        except OSError as error:
            if not self._held_elsewhere(error):
                # Locks do not work in this folder. Refusing would stop every serve here for
                # good, and the lock only guards against a mistake, so serve runs, with a note.
                self.handle, self.locked = handle, False
                self.note = (
                    "Note: the check that only one pcldb serve runs for this project folder is "
                    "not available here: %s cannot be locked (%s). The website runs anyway; "
                    "make sure no other pcldb serve uses this folder."
                    % (self.path, error.strerror or error))
                self.write_port(port)
                return
            handle.close()
            held = ""
            try:
                found = json.loads(self.info.read_text(encoding="utf-8"))
                held = " (process %s, port %s)" % (int(found["pid"]), int(found["port"]))
            except (OSError, ValueError, TypeError, KeyError):
                pass
            raise PclError(
                "pcldb serve is running already for this project folder%s. Two servers on one "
                "project would share its database, and stopping one would take the database "
                "away from the other. Use the running one, or stop it first (Ctrl+C in its "
                "window)." % held, code="already_serving", status=409) from None
        self.handle, self.locked = handle, True
        self.write_port(port)

    def write_port(self, port):
        try:
            util.write_text_atomic(self.info, json.dumps({"pid": os.getpid(), "port": port})
                                   + "\n")
        except OSError:
            pass

    def release(self):
        if self.handle is None:
            return
        try:
            self.info.unlink()
        except OSError:
            pass
        if self.locked:
            try:
                self._unlock(self.handle)
            except OSError:
                pass
        self.handle.close()
        self.handle, self.locked = None, False


def serve(settings, *, open_browser=False, host=None, port=None, out=None):
    """Run the website and the API until Ctrl+C (specification section 11).

    Starts the local PostgreSQL cluster when that engine is configured and it is stopped, and
    stops it again at the end only when this call started it. Prints the address with a
    one-time sign-in code (it works once, for five minutes). Returns 0. A second serve on the
    same project folder is refused (`ServeLock`).
    """
    out = out or sys.stdout
    if host is not None or port is not None:
        changes = {}
        if host is not None:
            changes["server.host"] = host
        if port is not None:
            changes["server.port"] = port
        settings = settings.changed(changes)
    lock = ServeLock(settings)
    lock.acquire(settings.server.port)
    if lock.note:
        _say(lock.note, out)
    try:
        return _serve_locked(settings, lock, open_browser=open_browser, out=out)
    finally:
        lock.release()


def _serve_locked(settings, lock, *, open_browser, out):
    from .store import open_store
    cluster = started_cluster = None
    if (settings.database.engine == "postgresql"
            and settings.database.postgresql.mode == "local"):
        from .store import pgcluster, postgres_engine
        postgres_engine.load_psycopg(settings)
        cluster = pgcluster.LocalCluster(settings)
        if not cluster.is_initialized():
            raise PclError("The local PostgreSQL database has not been made yet. Run: pcldb "
                           "setup", code="not_initialized", status=503)
        started_cluster = cluster.ensure_running()
        if started_cluster:
            _say("Started the local PostgreSQL database (127.0.0.1, port %d). It is stopped "
                 "again when the website stops." % cluster.port, out)
    log = handler = server = None
    try:
        store = open_store(settings)
        if not store.is_initialized():
            with store.app():           # raises the store's own sentence
                pass
        log, handler = file_log(settings.log_dir / "server.log")
        pcldb_log = logging.getLogger("pcldb")
        pcldb_log.addHandler(handler)   # module loggers (ask, search) write to the same file
        if pcldb_log.level == logging.NOTSET:
            pcldb_log.setLevel(logging.INFO)
        server = make_server(settings, store=store, log=log)
        lock.write_port(server.port)
        note = _admin_problem(store)
        code = server.app.signin.new()
        address = address_for(settings.server.host, server.port)
        log.info("pcldb %s serving on %s (engine %s)", __version__,
                 address_for(settings.server.host, server.port), store.engine)
        _say("pcldb is running: the website and the API on %s" % address, out)
        _say("Engine: %s (%s)." % (store.engine, store.describe().get("where")), out)
        _say("Open %s#signin=%s" % (address.rstrip("/") + "/", code), out)
        _say("That link signs the browser in as the local admin. It works once, within five "
             "minutes. Stop the website with Ctrl+C.", out)
        if note:
            _say(note, out)
        if open_browser:
            try:
                import webbrowser
                webbrowser.open(address + "#signin=" + code)
            except Exception:
                _say("The browser could not be opened; open the address above yourself.", out)
        with _stop_signals_as_ctrl_c():
            try:
                server.serve_forever(poll_interval=0.25)
            except KeyboardInterrupt:
                pass
        _say("Stopping the website.", out)
    finally:
        if server is not None:
            try:
                server.server_close()
            except OSError:
                pass
        if log is not None:
            log.info("pcldb stopped")
        if handler is not None:
            logging.getLogger("pcldb").removeHandler(handler)
            handler.close()
        if started_cluster:
            try:
                cluster.stop()
                _say("Stopped the local PostgreSQL database (pcldb serve had started it).", out)
            except PclError as error:
                _say("The local PostgreSQL database could not be stopped: %s Stop it with: "
                     "pcldb db stop" % error.message, out)
    return 0


def cmd_serve(args, settings):
    """`pcldb serve [--open] [--host H] [--port N]`."""
    try:
        return serve(settings, open_browser=bool(getattr(args, "open_browser", False)),
                     host=getattr(args, "host", None), port=getattr(args, "port", None))
    except KeyboardInterrupt:
        return 0
