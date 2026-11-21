"""Tests of pcldb/server.py (specification section 11), through real HTTP on a free port.

- Every endpoint on BOTH engines, its answer compared key by key with the specification's
  "Exact result shapes" (the expected key sets are the constants below).
- Every permission of the role table: viewer, analyst, uploader, admin, a disabled user, a
  revoked and an expired token, and no token.
- Every hardening rule: Host allow-list (421), Content-Type on POST (415), body limits (413,
  also for 20 MB sent over a raw socket, answered without reading it all), no CORS, the
  security headers and the content security policy, the fixed table of static files (path
  traversal attempts), tokens only in the header, socket timeouts (a client that sends half a
  request while others are served).
- The one-time sign-in code: works once, expires after five minutes, a wrong code is refused.
- 20 threads asking and querying at once on each engine: no errors and, on PostgreSQL, the
  number of server sessions is the same before and after.
- A registration through the API, then the session in /ask (rules), /sql and /session.
- from_lead 20a: on SQLite a registration waits for a slow expert query instead of failing.

No test uses port 8787 or 15439, ~/.pcldb or the real var/ folder.
"""

from __future__ import annotations

import io
import json
import os
import re
import socket
import threading
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from unittest import mock

from pcldb import __version__, auth, server, util
from pcldb.store import schema_def, sqlite_engine
from tests import registry_support as rs
from tests import support

# ---------------------------------------------------------------------- the exact shapes (11)

HEALTH_KEYS = {"ok", "version"}
SIGNIN_KEYS = {"token", "expires_utc", "user"}
USER_KEYS = {"name", "display_name", "role"}
ME_KEYS = {"user", "capabilities", "engine", "defaults", "storage", "version"}
ME_ENGINE_KEYS = {"name", "version"}
ME_DEFAULTS_KEYS = {"include_simulated", "sql_max_rows"}
ME_STORAGE_KEYS = {"ssh_host", "project_path", "collection_id"}
STATUS_KEYS = {"database", "search", "assistant", "registrations"}
STATUS_DATABASE_KEYS = {"engine", "version", "where", "sessions", "files", "runs", "points",
                        "demo", "instruments", "newest_started_utc"}
STATUS_INSTRUMENT_KEYS = {"instrument", "sessions"}
STATUS_SEARCH_KEYS = {"backend", "index_set", "signed_in", "entries"}
STATUS_ENTRIES_KEYS = {"pending", "sent", "synced", "failed", "delete_pending"}
STATUS_ASSISTANT_KEYS = {"enabled", "key_present", "model", "state", "note"}
ASSISTANT_STATES = {"ready", "off_vpn", "bad_key", "no_key", "disabled", "error", "unknown"}
REGISTRATION_ROW_KEYS = {"at_utc", "user_name", "session_path", "action", "source"}
ASK_KEYS = {"question", "answer", "answer_by", "planned_by", "interpretation", "request",
            "backend", "notices", "total", "count", "results", "facets", "timing_ms"}
ASK_FACET_KEYS = {"name", "field_name", "buckets"}
ASK_BUCKET_KEYS = {"value", "count"}
ASK_TIMING_KEYS = {"plan", "search", "answer"}
SEARCH_KEYS = {"result", "backend", "notices"}
SEARCH_RESULT_KEYS = {"@datatype", "@version", "total", "count", "offset", "has_next_page",
                      "gmeta"}
SESSION_KEYS = {"session", "files", "runs", "labels", "review_state", "n_needs_review"}
SESSION_FILES_KEYS = {"total", "shown", "items"}
SESSION_FILE_KEYS = {"rel_path", "bytes", "sha256", "listed"}
SESSION_RUN_KEYS = {"path", "folder", "kind", "made_utc", "model_name", "run_by", "valid"}
SESSION_LABEL_KEYS = {"label", "count"}
SQL_KEYS = {"columns", "rows", "row_count", "truncated", "elapsed_ms"}
SQL_COLUMN_KEYS = {"name", "type"}
SCHEMA_KEYS = {"engine", "schema", "objects"}
SCHEMA_OBJECT_KEYS = {"name", "kind", "description", "columns"}
SCHEMA_COLUMN_KEYS = {"name", "type", "nullable", "description"}
EXAMPLES_KEYS = {"engine", "examples"}
EXAMPLE_KEYS = {"title", "sql"}
HISTORY_KEYS = {"items"}
HISTORY_ITEM_KEYS = {"at_utc", "query_text", "ok", "n_rows", "elapsed_ms", "error_text"}
REGISTER_KEYS = {"action", "path", "subject", "web_url", "n_files", "bytes"}
REGISTRATIONS_KEYS = {"items"}
ERROR_KEYS = {"error"}
ERROR_INNER_KEYS = {"code", "message", "details"}

SESSION_COLUMNS = set(schema_def.table("pcl.sessions").column_names)
_FIELDS = json.loads((Path(server.__file__).parent / "schemas" / "search_fields.json")
                     .read_text(encoding="utf-8"))["fields"]
ENTRY_KEYS = {"subject"} | set(_FIELDS)        # what an /ask result may hold
ENTRY_ALWAYS = {"subject", "path", "folder", "instrument", "simulated", "is_copy", "withdrawn",
                "corrected"}
UTC_TEXT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z")
SECOND_TEXT = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ")

EXPECTED_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                "connect-src 'self'; base-uri 'none'; form-action 'none'; "
                "frame-ancestors 'none'")

ROLE_USERS = {"viewer": "demo-viewer", "analyst": "demo-analyst", "uploader": "demo-uploader",
              "admin": "admin"}


# ---------------------------------------------------------------------- HTTP helpers


class Reply:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = {name.lower(): value for name, value in headers.items()}
        self.body = body

    @property
    def json(self):
        return json.loads(self.body.decode("utf-8"))

    def __repr__(self):
        return "Reply(%d, %r)" % (self.status, self.body[:300])


class _NoRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirects())


def http(port, method, path, body=None, *, token=None, headers=None, raw=None,
         content_type="application/json", timeout=60):
    """One request through urllib. `body` is sent as JSON, `raw` as given."""
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path), method=method)
    data = raw
    if body is not None:
        data = json.dumps(body).encode("utf-8")
    if data is not None and content_type:
        request.add_header("Content-Type", content_type)
    if token:
        request.add_header("Authorization", "Bearer " + token)
    for name, value in (headers or {}).items():
        request.add_header(name, value)
    try:
        with _OPENER.open(request, data=data, timeout=timeout) as answer:
            return Reply(answer.status, dict(answer.headers), answer.read())
    except urllib.error.HTTPError as error:
        return Reply(error.code, dict(error.headers), error.read())


def raw_exchange(port, data, *, timeout=10, read=True):
    """Send bytes over a plain socket and read the whole answer (until the server closes)."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as connection:
        connection.sendall(data)
        if not read:
            return b""
        return _read_all(connection)


def _read_all(connection):
    chunks = []
    while True:
        try:
            chunk = connection.recv(65536)
        except (ConnectionResetError, socket.timeout):
            break
        if not chunk:
            break
        chunks.append(chunk)
    return b"".join(chunks)


def raw_status(answer):
    first = answer.split(b"\r\n", 1)[0].decode("latin-1")
    parts = first.split()
    return int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None


def raw_json(answer):
    return json.loads(answer.split(b"\r\n\r\n", 1)[1].decode("utf-8"))


# ---------------------------------------------------------------------- the running server


class RunningServer:
    """A pcldb server on a free port in a thread of this process."""

    def __init__(self, settings, store, **options):
        self.server = server.make_server(settings, host="127.0.0.1", port=0, store=store,
                                         **options)
        self.app = self.server.app
        self.port = self.server.port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(10)


def make_users(db):
    """One user per role (plus the admin `admin`), a disabled user, and tokens: returns
    {role or case: token}."""
    tokens = {}
    for role, name in ROLE_USERS.items():
        if auth.find_user(db, name) is None:        # `admin` may come from cli.ensure_admin
            auth.create_user(db, name, role,
                             display_name=None if role != "admin" else "Local admin")
        tokens[role] = auth.issue_token(db, name, label="tests")
    auth.create_user(db, "demo-disabled", "analyst")
    tokens["disabled"] = auth.issue_token(db, "demo-disabled")
    auth.set_disabled(db, "demo-disabled", True)
    auth.create_user(db, "demo-reviewer", "analyst")
    info = auth.issue_token_info(db, "demo-reviewer")
    auth.revoke_token(db, info["token_id"])
    tokens["revoked"] = info["token"]
    tokens["expired"] = auth.issue_token(db, "demo-reviewer", days=1,
                                         now="2026-01-01T00:00:00.000Z")
    return tokens


def a_document(number, simulated=True):
    """A valid registration document of its own (a different folder for every number)."""
    folder = "1d_y_scan_20261116_2250%02d_%03d" % (number % 60, number)
    path = "raw/raman-portable/%s%s" % ("simulated/" if simulated else "", folder)
    # One upload places one folder: every document has an upload id of its own.
    return rs.make_document(path=path, card=rs.make_card(session=folder, simulated=simulated),
                            upload=rs.make_upload(upload_id="u%012d" % number))


class ServerCase:
    """A seeded store, users of every role, and a server in a thread."""

    settings_changes = {"ask.per_user_per_minute": 1000}
    server_options = {}

    def setUp(self):
        super().setUp()
        from pcldb import ask, search_entries
        ask.clear_caches()
        with self.store.app() as db:
            self.keys = support.seed_minimal(db)
            self.tokens = make_users(db)
            search_entries.refresh(db, None, self.settings)
        self.running = RunningServer(self.settings, self.store, **self.server_options)
        self.addCleanup(self.running.close)
        self.port = self.running.port

    def call(self, method, path, body=None, *, as_role="admin", **options):
        token = self.tokens.get(as_role) if as_role else None
        return http(self.port, method, path, body, token=token, **options)

    def ok(self, method, path, body=None, **options):
        reply = self.call(method, path, body, **options)
        self.assertEqual(reply.status, 200, reply)
        return reply.json

    def assertKeys(self, value, keys, what):
        self.assertIsInstance(value, dict, what)
        self.assertEqual(set(value), keys, what)

    def assertError(self, reply, status, code=None):
        self.assertEqual(reply.status, status, reply)
        self.assertTrue(reply.headers["content-type"].startswith("application/json"))
        payload = reply.json
        self.assertKeys(payload, ERROR_KEYS, "error")
        self.assertKeys(payload["error"], ERROR_INNER_KEYS, "error inner")
        self.assertIsInstance(payload["error"]["message"], str)
        self.assertTrue(payload["error"]["message"])
        if code is not None:
            self.assertEqual(payload["error"]["code"], code, payload)
        return payload["error"]


# ---------------------------------------------------------------------- every endpoint, both


class EndpointCases(ServerCase):

    def test_health(self):
        payload = self.ok("GET", "/api/v1/health", as_role=None)
        self.assertKeys(payload, HEALTH_KEYS, "health")
        self.assertEqual(payload, {"ok": True, "version": __version__})

    def test_signin_shape_and_the_token_works(self):
        code = self.running.app.signin.new()
        self.assertRegex(code, r"^[A-Za-z0-9_-]{32}$")
        payload = self.ok("POST", "/api/v1/signin", {"code": code}, as_role=None)
        self.assertKeys(payload, SIGNIN_KEYS, "signin")
        self.assertKeys(payload["user"], USER_KEYS, "signin user")
        self.assertEqual(payload["user"], {"name": "admin", "display_name": "Local admin",
                                           "role": "admin"})
        self.assertRegex(payload["token"], r"^pcl_[A-Za-z0-9_-]{43}$")
        self.assertRegex(payload["expires_utc"], UTC_TEXT)
        hours = (util.parse_utc(payload["expires_utc"])
                 - util.parse_utc(util.now_utc_text())).total_seconds() / 3600
        self.assertTrue(11.9 < hours <= 12.0, hours)
        me = http(self.port, "GET", "/api/v1/me", token=payload["token"])
        self.assertEqual(me.status, 200)
        self.assertEqual(me.json["user"]["name"], "admin")

    def test_me(self):
        payload = self.ok("GET", "/api/v1/me")
        self.assertKeys(payload, ME_KEYS, "me")
        self.assertKeys(payload["user"], USER_KEYS, "me.user")
        self.assertKeys(payload["engine"], ME_ENGINE_KEYS, "me.engine")
        self.assertKeys(payload["defaults"], ME_DEFAULTS_KEYS, "me.defaults")
        self.assertKeys(payload["storage"], ME_STORAGE_KEYS, "me.storage")
        self.assertEqual(payload["capabilities"], ["ask", "sql", "register", "manage"])
        self.assertEqual(payload["engine"]["name"], self.engine)
        self.assertNotEqual(payload["engine"]["version"], "unknown")
        self.assertEqual(payload["defaults"], {"include_simulated": True, "sql_max_rows": 1000})
        self.assertEqual(payload["storage"], {
            "ssh_host": "datatransfer.arc.vt.edu", "project_path": "/projects/pcl_photonics",
            "collection_id": "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec"})
        self.assertEqual(payload["version"], __version__)
        viewer = self.ok("GET", "/api/v1/me", as_role="viewer")
        self.assertEqual(viewer["capabilities"], ["ask"])
        self.assertEqual(viewer["user"], {"name": "demo-viewer", "display_name": None,
                                          "role": "viewer"})

    def test_status(self):
        with self.store.app() as db:
            from pcldb import registration
            registration.register_document(db, a_document(1), actor="demo-uploader",
                                           source="upload", settings=self.settings)
        payload = self.ok("GET", "/api/v1/status", as_role="viewer")
        self.assertKeys(payload, STATUS_KEYS, "status")
        database = payload["database"]
        self.assertKeys(database, STATUS_DATABASE_KEYS, "status.database")
        self.assertEqual(database["engine"], self.engine)
        self.assertEqual((database["sessions"], database["files"], database["runs"],
                          database["points"]), (3, 13, 2, 3))
        self.assertIs(database["demo"], False)
        for item in database["instruments"]:
            self.assertKeys(item, STATUS_INSTRUMENT_KEYS, "status.database.instruments[]")
        self.assertEqual(database["instruments"], [{"instrument": "raman-portable",
                                                    "sessions": 3}])
        self.assertEqual(database["newest_started_utc"], "2026-11-17T03:50:15.281Z")
        self.assertIsInstance(database["where"], str)
        search = payload["search"]
        self.assertKeys(search, STATUS_SEARCH_KEYS, "status.search")
        self.assertEqual((search["backend"], search["index_set"], search["signed_in"]),
                         ("local", False, False))
        self.assertKeys(search["entries"], STATUS_ENTRIES_KEYS, "status.search.entries")
        self.assertEqual(search["entries"]["pending"], 3)
        assistant = payload["assistant"]
        self.assertKeys(assistant, STATUS_ASSISTANT_KEYS, "status.assistant")
        self.assertIn(assistant["state"], ASSISTANT_STATES)
        self.assertEqual((assistant["state"], assistant["key_present"]), ("no_key", False))
        self.assertEqual(len(payload["registrations"]), 1)
        for row in payload["registrations"]:
            self.assertKeys(row, REGISTRATION_ROW_KEYS, "status.registrations[]")
        self.assertEqual(payload["registrations"][0]["user_name"], "demo-uploader")

    def test_ask(self):
        payload = self.ok("POST", "/api/v1/ask",
                          {"question": "how many sessions per instrument",
                           "include_simulated": True}, as_role="viewer")
        self.assertKeys(payload, ASK_KEYS, "ask")
        self.assertEqual((payload["planned_by"], payload["answer_by"], payload["backend"]),
                         ("rules", "none", "local"))
        self.assertIsNone(payload["answer"])
        self.assertEqual((payload["total"], payload["count"]), (2, 2))
        self.assertKeys(payload["timing_ms"], ASK_TIMING_KEYS, "ask.timing_ms")
        self.assertIsInstance(payload["interpretation"], str)
        self.assertEqual(payload["request"]["@version"], "query#1.0.0")
        self.assertEqual(payload["notices"], [])
        for result in payload["results"]:
            self.assertLessEqual(set(result), ENTRY_KEYS, "ask.results[]")
            self.assertLessEqual(ENTRY_ALWAYS, set(result), "ask.results[]")
            for name in ("started", "uploaded", "verified", "last_run"):
                if name in result:
                    self.assertRegex(result[name], SECOND_TEXT)
            for name, value in result.items():
                self.assertNotIn(value, (None, "", []), name)
        self.assertEqual({result["path"] for result in payload["results"]},
                         set(self.keys["session_paths"]))
        self.assertEqual(len(payload["facets"]), 1)
        for facet in payload["facets"]:
            self.assertKeys(facet, ASK_FACET_KEYS, "ask.facets[]")
            for bucket in facet["buckets"]:
                self.assertKeys(bucket, ASK_BUCKET_KEYS, "ask.facets[].buckets[]")
        self.assertEqual(payload["facets"][0]["buckets"],
                         [{"value": "raman-portable", "count": 2}])
        without = self.ok("POST", "/api/v1/ask", {"question": "newest sessions",
                                                  "include_simulated": False})
        self.assertEqual([result["path"] for result in without["results"]],
                         [self.keys["real_path"]])

    def test_ask_refuses_what_is_not_a_question(self):
        self.assertError(self.call("POST", "/api/v1/ask", {"question": ""}), 400, "bad_question")
        self.assertError(self.call("POST", "/api/v1/ask", {"question": "x" * 501}), 400,
                         "bad_question")
        self.assertError(self.call("POST", "/api/v1/ask", {"question": "a",
                                                           "include_simulated": "yes"}),
                         400, "bad_request")
        self.assertError(self.call("POST", "/api/v1/ask", {"question": "a", "extra": 1}),
                         400, "bad_request")

    def test_search(self):
        payload = self.ok("POST", "/api/v1/search",
                          {"request": {"q": "*", "limit": 1,
                                       "facets": [{"type": "terms", "field_name": "instrument"}]}},
                          as_role="uploader")
        self.assertKeys(payload, SEARCH_KEYS, "search")
        self.assertEqual((payload["backend"], payload["notices"]), ("local", []))
        result = payload["result"]
        self.assertKeys(result, SEARCH_RESULT_KEYS | {"facet_results"}, "search.result")
        self.assertEqual((result["total"], result["count"], result["has_next_page"]),
                         (2, 1, True))
        entry = result["gmeta"][0]["entries"][0]
        self.assertEqual(set(entry), {"entry_id", "matched_principal_sets", "content"})
        plain = self.ok("POST", "/api/v1/search", {"request": {"q": "*"}})
        self.assertKeys(plain["result"], SEARCH_RESULT_KEYS, "search.result without facets")
        refused = self.assertError(self.call("POST", "/api/v1/search",
                                             {"request": {"q": "*", "boosts": []}}),
                                   400, "bad_search_request")
        self.assertIn("boosts", refused["message"])
        with self.store.app() as db:
            rows = db.query("SELECT ok, mode, user_name FROM {app}.query_log WHERE mode = ?",
                            ["search"])
        self.assertEqual(sorted((row["ok"], row["user_name"]) for row in rows),
                         [(False, "admin"), (True, "admin"), (True, "demo-uploader")])

    def test_session(self):
        path = self.keys["real_path"]
        payload = self.ok("GET", "/api/v1/session?path=" + urllib.parse.quote(path, safe=""),
                          as_role="viewer")
        self.assertKeys(payload, SESSION_KEYS, "session")
        self.assertKeys(payload["session"], SESSION_COLUMNS, "session.session")
        self.assertEqual(payload["session"]["path"], path)
        self.assertIs(payload["session"]["simulated"], False)
        self.assertEqual(payload["session"]["sample_ids"], ["S-100"])
        self.assertRegex(payload["session"]["started_utc"], UTC_TEXT)
        self.assertKeys(payload["files"], SESSION_FILES_KEYS, "session.files")
        self.assertEqual((payload["files"]["total"], payload["files"]["shown"]), (5, 5))
        for item in payload["files"]["items"]:
            self.assertKeys(item, SESSION_FILE_KEYS, "session.files.items[]")
        self.assertEqual([item["rel_path"] for item in payload["files"]["items"]],
                         ["SHA256SUMS", "point_0001.json", "point_0002.json", "point_0003.json",
                          "session.json"])          # byte order on both engines
        self.assertEqual(len(payload["runs"]), 2)
        for run in payload["runs"]:
            self.assertKeys(run, SESSION_RUN_KEYS, "session.runs[]")
        self.assertEqual(payload["runs"][0]["path"], support.REVIEW_RUN_PATH)   # newest first
        for label in payload["labels"]:
            self.assertKeys(label, SESSION_LABEL_KEYS, "session.labels[]")
        self.assertEqual(payload["labels"], [{"label": "UNKNOWN", "count": 1},
                                             {"label": "adenine", "count": 1},
                                             {"label": "r6g", "count": 1}])
        self.assertEqual((payload["review_state"], payload["n_needs_review"]), ("pending", 1))
        # A session without runs or a rollup row.
        other = self.ok("GET", "/api/v1/session?path=" + urllib.parse.quote(
            self.keys["simulated_path"], safe=""))
        self.assertEqual((other["runs"], other["labels"], other["review_state"]),
                         ([], [], "none"))
        self.assertError(self.call("GET", "/api/v1/session?path=raw%2Fno%2Fsuch"), 404,
                         "no_such_session")
        self.assertError(self.call("GET", "/api/v1/session"), 400, "bad_request")
        self.assertError(self.call("GET", "/api/v1/session?path=a&path=b"), 400, "bad_request")
        self.assertError(self.call("GET", "/api/v1/session?path=raw%0A"), 400, "bad_request")

    def test_session_shows_at_most_500_files(self):
        path = self.keys["simulated_path"]
        with self.store.app() as db:
            db.insert_many("pcl.files", [
                {"session_path": path, "rel_path": "extra/f%04d.txt" % number, "bytes": number,
                 "sha256": None, "listed": False} for number in range(600)])
        payload = self.ok("GET", "/api/v1/session?path=" + urllib.parse.quote(path, safe=""))
        self.assertEqual((payload["files"]["total"], payload["files"]["shown"],
                          len(payload["files"]["items"])), (603, 500, 500))

    def test_sql(self):
        payload = self.ok("POST", "/api/v1/sql",
                          {"sql": "SELECT path, simulated FROM sessions ORDER BY path",
                           "max_rows": 1}, as_role="analyst")
        self.assertKeys(payload, SQL_KEYS, "sql")
        for column in payload["columns"]:
            self.assertKeys(column, SQL_COLUMN_KEYS, "sql.columns[]")
        self.assertEqual([column["name"] for column in payload["columns"]], ["path", "simulated"])
        self.assertEqual((payload["row_count"], payload["truncated"]), (1, True))
        self.assertEqual(payload["rows"][0][0], self.keys["real_path"])
        refused = self.call("POST", "/api/v1/sql", {"sql": "DELETE FROM sessions"},
                            as_role="analyst")
        self.assertEqual(refused.status, 400, refused)
        self.assertIn(refused.json["error"]["code"], ("sql_refused", "sql_error"))
        private = self.call("POST", "/api/v1/sql", {"sql": "SELECT * FROM app.users"},
                            as_role="analyst")
        self.assertEqual(private.status, 400, private)
        self.assertNotIn(self.tokens["analyst"], private.body.decode("utf-8"))
        self.assertError(self.call("POST", "/api/v1/sql", {"sql": 7}), 400, "bad_request")
        self.assertError(self.call("POST", "/api/v1/sql", {"sql": "SELECT 1", "max_rows": "9"}),
                         400, "bad_request")

    def test_sql_schema(self):
        payload = self.ok("GET", "/api/v1/sql/schema", as_role="analyst")
        self.assertKeys(payload, SCHEMA_KEYS, "sql/schema")
        self.assertEqual((payload["engine"], payload["schema"]), (self.engine, "pcl"))
        names = set()
        for item in payload["objects"]:
            self.assertKeys(item, SCHEMA_OBJECT_KEYS, "sql/schema.objects[]")
            self.assertIn(item["kind"], ("table", "view"))
            names.add(item["name"])
            for column in item["columns"]:
                self.assertKeys(column, SCHEMA_COLUMN_KEYS, "sql/schema.objects[].columns[]")
        self.assertIn("sessions", names)
        self.assertFalse(names & {"users", "tokens", "query_log", "registrations"})

    def test_sql_examples(self):
        payload = self.ok("GET", "/api/v1/sql/examples", as_role="analyst")
        self.assertKeys(payload, EXAMPLES_KEYS, "sql/examples")
        self.assertEqual(payload["engine"], self.engine)
        self.assertGreaterEqual(len(payload["examples"]), 10)
        for example in payload["examples"]:
            self.assertKeys(example, EXAMPLE_KEYS, "sql/examples.examples[]")
        first = self.ok("POST", "/api/v1/sql", {"sql": payload["examples"][0]["sql"]},
                        as_role="analyst")
        self.assertKeys(first, SQL_KEYS, "an example run")

    def test_sql_history_is_the_callers_own(self):
        self.ok("POST", "/api/v1/sql", {"sql": "SELECT 1 AS one"}, as_role="analyst")
        self.call("POST", "/api/v1/sql", {"sql": "SELECT nothing FROM nowhere"},
                  as_role="analyst")
        self.ok("POST", "/api/v1/sql", {"sql": "SELECT 2 AS two"}, as_role="admin")
        payload = self.ok("GET", "/api/v1/sql/history", as_role="analyst")
        self.assertKeys(payload, HISTORY_KEYS, "sql/history")
        for item in payload["items"]:
            self.assertKeys(item, HISTORY_ITEM_KEYS, "sql/history.items[]")
        self.assertEqual([(item["query_text"], item["ok"]) for item in payload["items"]],
                         [("SELECT nothing FROM nowhere", False), ("SELECT 1 AS one", True)])
        self.assertIsNotNone(payload["items"][0]["error_text"])
        admin = self.ok("GET", "/api/v1/sql/history")
        self.assertEqual([item["query_text"] for item in admin["items"]], ["SELECT 2 AS two"])

    def test_registration_then_ask_sql_and_session(self):
        document = a_document(7, simulated=False)
        payload = self.ok("POST", "/api/v1/registrations", {"document": document},
                          as_role="uploader")
        self.assertKeys(payload, REGISTER_KEYS, "registrations POST")
        path = document["location"]["path"]
        self.assertEqual((payload["action"], payload["path"]), ("created", path))
        self.assertTrue(payload["subject"].startswith("urn:pcl:dataset:"))
        again = self.ok("POST", "/api/v1/registrations", {"document": document},
                        as_role="uploader")
        self.assertEqual(again["action"], "unchanged")
        # The new session is in normal mode (rules) ...
        asked = self.ok("POST", "/api/v1/ask", {"question": "newest sessions",
                                                "include_simulated": True}, as_role="viewer")
        self.assertIn(path, [result["path"] for result in asked["results"]])
        # ... in expert mode ...
        rows = self.ok("POST", "/api/v1/sql",
                       {"sql": "SELECT path, registered_by, source FROM sessions "
                               "WHERE path = '%s'" % path}, as_role="analyst")["rows"]
        self.assertEqual(rows, [[path, "demo-uploader", "upload"]])
        # ... and in the detail panel, with how it got to ARC.
        detail = self.ok("GET", "/api/v1/session?path=" + urllib.parse.quote(path, safe=""))
        self.assertEqual(detail["session"]["upload_json"],
                         rs.make_upload(upload_id=detail["session"]["upload_json"]["upload_id"]))
        self.assertRegex(detail["session"]["upload_json"]["upload_id"], r"^u\d{12}$")
        listed = self.ok("GET", "/api/v1/registrations", as_role="viewer")
        self.assertKeys(listed, REGISTRATIONS_KEYS, "registrations GET")
        for row in listed["items"]:
            self.assertKeys(row, REGISTRATION_ROW_KEYS, "registrations.items[]")
        self.assertEqual([(row["action"], row["user_name"], row["source"])
                          for row in listed["items"]],
                         [("unchanged", "demo-uploader", "upload"),
                          ("created", "demo-uploader", "upload")])

    def test_registration_refusals(self):
        document = a_document(8)
        self.ok("POST", "/api/v1/registrations", {"document": document}, as_role="uploader")
        changed = json.loads(json.dumps(document))
        changed["card"]["notes"] = "other notes"
        self.assertError(self.call("POST", "/api/v1/registrations", {"document": changed},
                                   as_role="uploader"), 409, "already_registered")
        # replace needs admin
        self.assertError(self.call("POST", "/api/v1/registrations",
                                   {"document": changed, "replace": True}, as_role="uploader"),
                         403, "forbidden")
        replaced = self.ok("POST", "/api/v1/registrations",
                           {"document": changed, "replace": True})
        self.assertEqual(replaced["action"], "updated")
        bad = json.loads(json.dumps(document))
        bad["location"]["path"] = "../outside"
        self.assertError(self.call("POST", "/api/v1/registrations", {"document": bad}), 400)
        self.assertError(self.call("POST", "/api/v1/registrations", {"document": []}), 400,
                         "invalid_document")
        self.assertError(self.call("POST", "/api/v1/registrations",
                                   {"document": document, "replace": "no"}), 400, "bad_request")
        listed = self.ok("GET", "/api/v1/registrations")["items"]
        self.assertIn("rejected", [row["action"] for row in listed])


class TestEndpointsSqlite(EndpointCases, support.SqliteCase, unittest.TestCase):
    pass


class TestEndpointsPostgres(EndpointCases, support.PostgresCase, unittest.TestCase):
    pass


# ---------------------------------------------------------------------- permissions, both


GET_ENDPOINTS = {
    "/api/v1/me": "any", "/api/v1/status": "any",
    "/api/v1/session?path=" + urllib.parse.quote(support.REAL_PATH, safe=""): "ask",
    "/api/v1/registrations": "ask", "/api/v1/sql/schema": "sql",
    "/api/v1/sql/examples": "sql", "/api/v1/sql/history": "sql",
}
POST_ENDPOINTS = {
    "/api/v1/ask": ({"question": "sessions"}, "ask"),
    "/api/v1/search": ({"request": {"q": "*"}}, "ask"),
    "/api/v1/sql": ({"sql": "SELECT 1 AS one"}, "sql"),
}
ALLOWED = {"any": {"viewer", "analyst", "uploader", "admin"},
           "ask": {"viewer", "analyst", "uploader", "admin"},
           "sql": {"analyst", "admin"}, "register": {"uploader", "admin"}}


class PermissionCases(ServerCase):

    def test_the_role_table(self):
        number = 20
        for role in ROLE_USERS:
            for path, needs in GET_ENDPOINTS.items():
                reply = self.call("GET", path, as_role=role)
                expected = 200 if role in ALLOWED[needs] else 403
                self.assertEqual(reply.status, expected, (role, path, reply))
                if expected == 403:
                    self.assertError(reply, 403, "forbidden")
            for path, (body, needs) in POST_ENDPOINTS.items():
                reply = self.call("POST", path, body, as_role=role)
                expected = 200 if role in ALLOWED[needs] else 403
                self.assertEqual(reply.status, expected, (role, path, reply))
            number += 1
            reply = self.call("POST", "/api/v1/registrations", {"document": a_document(number)},
                              as_role=role)
            expected = 200 if role in ALLOWED["register"] else 403
            self.assertEqual(reply.status, expected, (role, "registrations", reply))
        # A viewer is told what it would need, in words.
        refused = self.assertError(self.call("GET", "/api/v1/sql/schema", as_role="viewer"), 403)
        self.assertIn("analyst or admin", refused["message"])

    def test_no_token_disabled_revoked_expired_get_401(self):
        calls = [("GET", path, None) for path in GET_ENDPOINTS]
        calls += [("POST", path, body) for path, (body, _needs) in POST_ENDPOINTS.items()]
        calls.append(("POST", "/api/v1/registrations", {"document": a_document(40)}))
        for case in (None, "disabled", "revoked", "expired", "garbage", "not-bearer"):
            for method, path, body in calls:
                headers = {}
                token = self.tokens.get(case) if case else None
                if case == "garbage":
                    token = "pcl_" + "x" * 43
                if case == "not-bearer":
                    token, headers = None, {"Authorization": "Basic " + self.tokens["admin"]}
                reply = http(self.port, method, path, body, token=token, headers=headers)
                error = self.assertError(reply, 401, "unauthorized")
                self.assertEqual(reply.headers.get("www-authenticate"), 'Bearer realm="pcldb"')
                self.assertNotIn("Traceback", error["message"])
        # health and signin need no token
        self.assertEqual(http(self.port, "GET", "/api/v1/health").status, 200)
        self.assertEqual(http(self.port, "POST", "/api/v1/signin", {"code": "x" * 32}).status,
                         401)

    def test_a_token_is_never_taken_from_the_address(self):
        token = self.tokens["admin"]
        for name in ("token", "access_token", "Authorization", "TOKEN"):
            reply = http(self.port, "GET", "/api/v1/me?%s=%s" % (name, token), token=token)
            self.assertError(reply, 400, "token_in_address")
        reply = http(self.port, "GET", "/api/v1/me?token=" + token)
        self.assertEqual(reply.status, 400)


class TestPermissionsSqlite(PermissionCases, support.SqliteCase, unittest.TestCase):
    pass


class TestPermissionsPostgres(PermissionCases, support.PostgresCase, unittest.TestCase):
    pass


# ---------------------------------------------------------------------- 20 threads, both


class ConcurrencyCases(ServerCase):

    def _pg_sessions(self):
        import psycopg
        connection = psycopg.connect(self._cluster.conninfo("owner", self._database),
                                     autocommit=True)
        try:
            return connection.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE datname = %s "
                "AND application_name LIKE 'pcldb-%%'", [self._database]).fetchone()[0]
        finally:
            connection.close()

    def test_twenty_threads_ask_and_query_at_once(self):
        before = self._pg_sessions() if self.engine == "postgresql" else None
        barrier = threading.Barrier(20)
        statuses, problems = [], []
        lock = threading.Lock()
        questions = ["adenine sessions", "how many sessions per instrument", "newest sessions",
                     "simulated sessions from this month"]

        def worker(number):
            try:
                barrier.wait(30)
                role = ("admin", "analyst")[number % 2]
                answers = [
                    self.call("POST", "/api/v1/ask",
                              {"question": questions[number % 4], "include_simulated": True},
                              as_role=role),
                    self.call("POST", "/api/v1/sql",
                              {"sql": "SELECT instrument, COUNT(*) AS n FROM sessions "
                                      "GROUP BY instrument"}, as_role=role),
                    self.call("GET", "/api/v1/me", as_role=role),
                ]
                with lock:
                    statuses.extend(answer.status for answer in answers)
                    problems.extend(answer for answer in answers if answer.status != 200)
            except Exception as error:      # noqa: BLE001 - every failure is reported below
                with lock:
                    problems.append(error)

        threads = [threading.Thread(target=worker, args=(number,)) for number in range(20)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(problems, [])
        self.assertEqual(statuses, [200] * 60)
        with self.store.app() as db:
            logged = db.scalar("SELECT COUNT(*) FROM {app}.query_log")
        self.assertEqual(logged, 40)                # 20 questions and 20 queries, all audited
        if self.engine == "postgresql":
            deadline = time.monotonic() + 10
            after = self._pg_sessions()
            while after != before and time.monotonic() < deadline:
                time.sleep(0.1)
                after = self._pg_sessions()
            self.assertEqual(after, before, "PostgreSQL sessions before and after")
        else:
            # Nothing kept a transaction open: a writer gets the lock at once.
            with mock.patch.object(sqlite_engine, "busy_milliseconds", lambda _settings: 200):
                with self.store.app() as db:
                    db.insert("pcl.meta", {"key": "after-the-storm", "value": "1"})


class TestConcurrencySqlite(ConcurrencyCases, support.SqliteCase, unittest.TestCase):
    pass


class TestConcurrencyPostgres(ConcurrencyCases, support.PostgresCase, unittest.TestCase):
    pass


# ---------------------------------------------------------------------- hardening (SQLite)


class TestHardening(ServerCase, support.SqliteCase, unittest.TestCase):
    """The rules that do not depend on the engine, on SQLite."""

    settings_changes = {"ask.per_user_per_minute": 1000,
                        "server.allowed_hosts": ["lab-pc.example.invalid"]}

    def raw(self, request_text, body=b"", **options):
        return raw_exchange(self.port, request_text.encode("latin-1") + body, **options)

    def test_the_host_must_name_this_server(self):
        port = self.port
        for host, expected in (("127.0.0.1:%d" % port, 200), ("localhost:%d" % port, 200),
                               ("LOCALHOST:%d" % port, 200), ("[::1]:%d" % port, 200),
                               ("lab-pc.example.invalid:%d" % port, 200),
                               ("lab-pc.example.invalid", 200),
                               ("evil.example:%d" % port, 421), ("127.0.0.1:1", 421),
                               ("127.0.0.1", 421), ("localhost.evil.example:%d" % port, 421),
                               ("", 421)):
            answer = self.raw("GET /api/v1/health HTTP/1.1\r\nHost: %s\r\n\r\n" % host)
            self.assertEqual(raw_status(answer), expected, host)
            if expected == 421:
                self.assertEqual(raw_json(answer)["error"]["code"], "wrong_host")
        no_host = self.raw("GET /api/v1/health HTTP/1.0\r\n\r\n")
        self.assertEqual(raw_status(no_host), 421)
        page = self.raw("GET / HTTP/1.1\r\nHost: rebinding.example:%d\r\n\r\n" % port)
        self.assertEqual(raw_status(page), 421)         # the page too, not only the API

    def test_post_needs_json_content_type(self):
        for content_type in (None, "text/plain", "application/x-www-form-urlencoded",
                             "multipart/form-data; boundary=x", "application/json; charset=latin-1"):
            reply = http(self.port, "POST", "/api/v1/ask", raw=b'{"question": "x"}',
                         token=self.tokens["admin"], content_type=content_type)
            self.assertError(reply, 415, "wrong_content_type")
        reply = http(self.port, "POST", "/api/v1/ask", raw=b'{"question": "sessions"}',
                     token=self.tokens["admin"], content_type="application/json; charset=utf-8")
        self.assertEqual(reply.status, 200)

    def test_body_limits(self):
        big = json.dumps({"sql": "SELECT 1 -- " + "x" * (64 * 1024)}).encode()
        self.assertError(http(self.port, "POST", "/api/v1/sql", raw=big,
                              token=self.tokens["admin"]), 413, "too_large")
        self.assertError(http(self.port, "POST", "/api/v1/signin", raw=big), 413, "too_large")
        # Registrations may be larger: 100 KB is read and judged, not refused for its size.
        files = [{"rel_path": "f/%05d.txt" % number, "bytes": 1, "sha256": None}
                 for number in range(1500)]
        document = a_document(30)
        document["files"] = files
        document["sums_sha256"] = None
        body = json.dumps({"document": document}).encode()
        self.assertGreater(len(body), 64 * 1024)
        reply = http(self.port, "POST", "/api/v1/registrations", raw=body,
                     token=self.tokens["admin"])
        self.assertEqual(reply.status, 200, reply)
        chunked = self.raw("POST /api/v1/ask HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: "
                           "application/json\r\nTransfer-Encoding: chunked\r\n\r\n" % self.port,
                           b"5\r\n{\"a\":\r\n0\r\n\r\n")
        self.assertEqual(raw_status(chunked), 411)

    def test_twenty_megabytes_are_refused_without_reading_them(self):
        size = 20 * 1024 * 1024
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as connection:
            connection.sendall((
                "POST /api/v1/registrations HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                "Content-Type: application/json\r\nContent-Length: %d\r\n"
                "Authorization: Bearer %s\r\n\r\n" % (self.port, size, self.tokens["admin"])
            ).encode("ascii") + b'{"document": ' + b" " * 65536)
            answer = _read_all(connection)          # only 64 KB of the 20 MB were ever sent
        self.assertEqual(raw_status(answer), 413)
        self.assertEqual(raw_json(answer)["error"]["code"], "too_large")
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(http(self.port, "GET", "/api/v1/health").status, 200)

    def test_a_body_without_a_valid_token_is_never_read(self):
        # SEC-03: 401 and 403 come before the body is read (it used to read up to 16 MB first).
        size = 16 * 1024 * 1024
        for headers, status in (("", 401), ("Authorization: Bearer pcl_%s\r\n" % ("A" * 43), 401),
                                ("Authorization: Bearer %s\r\n" % self.tokens["viewer"], 403)):
            started = time.monotonic()
            with socket.create_connection(("127.0.0.1", self.port), timeout=10) as connection:
                connection.sendall((
                    "POST /api/v1/registrations HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n"
                    "Content-Type: application/json\r\nContent-Length: %d\r\n%s\r\n"
                    % (self.port, size, headers)).encode("ascii") + b'{"document": ')
                answer = _read_all(connection)          # only a few bytes were ever sent
            self.assertEqual(raw_status(answer), status, answer[:200])
            self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(http(self.port, "GET", "/api/v1/health").status, 200)

    def test_a_refusal_before_the_body_reaches_a_client_that_sends_it_all_first(self):
        # SEC2-01: pcldb.client (urllib) sends the whole body before it reads the answer. A
        # 401 or 403 sent before the body is read must still reach it, for a body of several
        # MB, as the 401 or 403 (it used to be "could not be reached": a broken pipe).
        from pcldb.client import Client
        from pcldb.errors import PclError
        document = {"pad": "x" * (8 * 1024 * 1024)}
        address = "http://127.0.0.1:%d" % self.port
        for token, status, code in (("pcl_" + "A" * 43, 401, "unauthorized"),
                                    (self.tokens["viewer"], 403, "forbidden")):
            with self.assertRaises(PclError) as caught:
                Client(address, token, timeout=60).register(document)
            self.assertEqual((caught.exception.status, caught.exception.code), (status, code),
                             caught.exception.message)
        self.assertEqual(http(self.port, "GET", "/api/v1/health").status, 200)

    def test_no_cors(self):
        preflight = http(self.port, "OPTIONS", "/api/v1/ask",
                         headers={"Origin": "https://evil.example",
                                  "Access-Control-Request-Method": "POST"})
        self.assertEqual(preflight.status, 405)
        answers = [preflight,
                   http(self.port, "GET", "/api/v1/me", token=self.tokens["admin"],
                        headers={"Origin": "https://evil.example"}),
                   http(self.port, "GET", "/", headers={"Origin": "https://evil.example"}),
                   http(self.port, "GET", "/api/v1/nothing")]
        for answer in answers:
            self.assertFalse([name for name in answer.headers
                              if name.startswith("access-control-")], answer.headers)

    def test_security_headers_on_every_kind_of_answer(self):
        answers = {
            "api": http(self.port, "GET", "/api/v1/me", token=self.tokens["admin"]),
            "error": http(self.port, "GET", "/api/v1/me"),
            "not found": http(self.port, "GET", "/nothing-here"),
            "page": http(self.port, "GET", "/"),
            "script": http(self.port, "GET", "/app.js"),
            "bad request line": Reply(400, {}, b""),
        }
        line = self.raw("BROKEN\r\n\r\n")
        self.assertEqual(raw_status(line), 400)
        head = line.split(b"\r\n\r\n", 1)[0].decode("latin-1").lower()
        self.assertIn("x-content-type-options: nosniff", head)
        del answers["bad request line"]
        for what, answer in answers.items():
            self.assertEqual(answer.headers.get("x-content-type-options"), "nosniff", what)
            self.assertEqual(answer.headers.get("referrer-policy"), "no-referrer", what)
            self.assertEqual(answer.headers.get("cache-control"), "no-store", what)
            self.assertEqual(answer.headers.get("content-security-policy"), EXPECTED_CSP, what)
            self.assertNotIn("set-cookie", answer.headers, what)

    def test_the_static_files_come_from_a_fixed_table(self):
        web = Path(server.__file__).parent / "web"
        for path, (name, content_type) in (("/", ("index.html", "text/html; charset=utf-8")),
                                           ("/index.html", ("index.html",
                                                            "text/html; charset=utf-8")),
                                           ("/app.js", ("app.js",
                                                        "text/javascript; charset=utf-8")),
                                           ("/app.css", ("app.css", "text/css; charset=utf-8")),
                                           ("/favicon.svg", ("favicon.svg", "image/svg+xml"))):
            reply = http(self.port, "GET", path)
            self.assertEqual(reply.status, 200, path)
            self.assertEqual(reply.headers["content-type"], content_type, path)
            self.assertEqual(reply.body, (web / name).read_bytes(), path)
        for attempt in ("/../server.py", "/%2e%2e/server.py", "/..%2fserver.py",
                        "/web/app.js", "/pcldb/web/app.js", "//etc/passwd", "/app.js/../cli.py",
                        "/app.js%00", "/APP.JS", "/app.js.", "/%61pp.js", "/index.html/",
                        "/./app.js", "/.%2e/.%2e/pcldb/config.py", "/schemas/search_fields.json",
                        "/../../../../etc/passwd", "/\\..\\server.py", "/favicon.ico"):
            answer = self.raw("GET %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\n\r\n"
                              % (attempt, self.port))
            self.assertEqual(raw_status(answer), 404, attempt)
            self.assertNotIn(b"import ", answer)
        self.assertEqual(http(self.port, "POST", "/app.js", {"x": 1}).status, 405)

    def test_errors_are_json_with_a_sentence(self):
        cases = (("GET", "/api/v1/nothing", None, 404, "not_found"),
                 ("GET", "/api/v1/ask", None, 405, "method_not_allowed"),
                 ("DELETE", "/api/v1/registrations", None, 405, "method_not_allowed"))
        for method, path, body, status, code in cases:
            self.assertError(http(self.port, method, path, body, token=self.tokens["admin"]),
                             status, code)
        allowed = http(self.port, "GET", "/api/v1/ask", token=self.tokens["admin"])
        self.assertEqual(allowed.headers.get("allow"), "POST")
        many = "&".join("p%d=1" % number for number in range(60))
        self.assertError(http(self.port, "GET", "/api/v1/me?" + many, token=self.tokens["admin"]),
                         400, "bad_request")
        long_address = http(self.port, "GET", "/api/v1/me?x=" + "a" * 9000,
                            token=self.tokens["admin"])
        self.assertError(long_address, 414, "too_long")
        for raw in (b"{not json", b"[1, 2]", b'{"question": NaN}', b'{"question": Infinity}',
                    b'{"question": "a", "question": "b"}', b"\xff\xfe", b"", b'"text"',
                    b"[" * 30000):
            reply = http(self.port, "POST", "/api/v1/ask", raw=raw, token=self.tokens["admin"])
            self.assertError(reply, 400, "bad_json")

    def test_an_unexpected_failure_is_a_plain_500_and_the_trace_goes_to_the_log(self):
        log_path = self.project.path / "var" / "log" / "server.log"
        log, handler = server.file_log(log_path)
        self.addCleanup(handler.close)
        self.running.app.log = log

        def broken(_request):
            raise RuntimeError("an inner detail zq-41")

        with mock.patch.dict(server.ROUTES, {("GET", "/api/v1/me"): (broken, server.ANY, 0)}):
            reply = http(self.port, "GET", "/api/v1/me", token=self.tokens["admin"])
        error = self.assertError(reply, 500, "internal_error")
        self.assertNotIn("zq-41", reply.body.decode())
        self.assertIn("server.log", error["message"])
        handler.flush()
        text = log_path.read_text(encoding="utf-8")
        self.assertIn("RuntimeError: an inner detail zq-41", text)

    def test_the_log_holds_no_token_question_or_sql(self):
        log_path = self.project.path / "var" / "log" / "server.log"
        log, handler = server.file_log(log_path)
        self.addCleanup(handler.close)
        self.running.app.log = log
        token = self.tokens["admin"]
        http(self.port, "POST", "/api/v1/ask", {"question": "secret-question-q81"}, token=token)
        http(self.port, "POST", "/api/v1/sql", {"sql": "SELECT 'secret-sql-s93' AS x"},
             token=token)
        http(self.port, "GET", "/api/v1/me?token=" + token, token=token)
        http(self.port, "GET", "/api/v1/me", token=token + "x")
        code = self.running.app.signin.new()
        http(self.port, "POST", "/api/v1/signin", {"code": code})
        handler.flush()
        text = log_path.read_text(encoding="utf-8")
        self.assertIn("/api/v1/ask 200 admin", text)
        self.assertIn("/api/v1/me 400", text)
        for secret in (token, code, "secret-question-q81", "secret-sql-s93", "Bearer"):
            self.assertNotIn(secret, text)

    def test_rate_limit_answers_429(self):
        self.running.app.settings = self.settings.changed({"ask.per_user_per_minute": 2})
        statuses = [self.call("POST", "/api/v1/ask", {"question": "sessions"},
                              as_role="viewer").status for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])


class TestSlowClients(ServerCase, support.SqliteCase, unittest.TestCase):
    """A client that sends half a request holds one thread, for one second here, and the
    server keeps answering everybody else."""

    server_options = {"socket_timeout": 1.0}

    def test_half_a_request_while_others_are_served(self):
        stalled_body = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        stalled_head = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(stalled_body.close)
        self.addCleanup(stalled_head.close)
        stalled_body.sendall((
            "POST /api/v1/ask HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json"
            "\r\nContent-Length: 100\r\nAuthorization: Bearer %s\r\n\r\n{\"quest"
            % (self.port, self.tokens["admin"])).encode("ascii"))
        stalled_head.sendall(b"GET /api/v1/health HTTP/1.1\r\nHost: 127.0.")
        started = time.monotonic()
        for _ in range(5):
            self.assertEqual(http(self.port, "GET", "/api/v1/health", timeout=5).status, 200)
            self.assertEqual(self.call("GET", "/api/v1/me").status, 200)
        self.assertLess(time.monotonic() - started, 5)
        # The server gives up on the stalled clients after its socket timeout.
        for connection in (stalled_body, stalled_head):
            connection.settimeout(5)
            waited = time.monotonic()
            answer = _read_all(connection)
            self.assertLess(time.monotonic() - waited, 4.5)
            self.assertIn(raw_status(answer) if answer else None, (None, 408))


    def test_a_slow_drip_does_not_hold_a_thread_past_the_deadline(self):
        # SEC-06: one byte every 0.3 s keeps every single wait short; the whole request must
        # still be in within the socket timeout (1 s here), else the server lets go.
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(connection.close)
        connection.sendall(("GET /api/v1/health HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nX-Pad: "
                            % self.port).encode("ascii"))
        started = time.monotonic()
        closed = False
        while time.monotonic() - started < 6:
            try:
                connection.sendall(b"a")
            except OSError:
                closed = True
                break
            connection.settimeout(0.3)
            try:
                if connection.recv(4096) == b"":
                    closed = True
                    break
            except socket.timeout:
                pass
            except OSError:
                closed = True
                break
        self.assertTrue(closed, "the server still waited after 6 s")
        self.assertLess(time.monotonic() - started, 4)
        self.assertEqual(http(self.port, "GET", "/api/v1/health", timeout=5).status, 200)

    def post_head(self, length, path="/api/v1/registrations"):
        return ("POST %s HTTP/1.1\r\nHost: 127.0.0.1:%d\r\nContent-Type: application/json\r\n"
                "Content-Length: %d\r\nAuthorization: Bearer %s\r\n\r\n"
                % (path, self.port, length, self.tokens["admin"])).encode("ascii")

    def test_a_slow_upload_that_keeps_coming_is_read_and_answered(self):
        # SEC2-02: a body that arrives slowly but steadily (here 16 KB every 0.2 s, about
        # 80 KB/s) is read in full, even when that takes longer than the old allowance of the
        # socket timeout plus one second per 256 KB (1 s + 1.2 s here).
        document = a_document(40)
        document["files"] = [{"rel_path": "f/%05d.txt" % number, "bytes": 1, "sha256": None}
                             for number in range(6000)]
        document["sums_sha256"] = None
        body = json.dumps({"document": document}).encode("utf-8")
        self.assertGreater(len(body), 300 * 1024)
        piece = 16 * 1024
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", self.port), timeout=30) as connection:
            connection.sendall(self.post_head(len(body)))
            for offset in range(0, len(body), piece):
                connection.sendall(body[offset:offset + piece])
                time.sleep(0.2)
            answer = _read_all(connection)
        self.assertGreater(time.monotonic() - started, 3)
        self.assertEqual(raw_status(answer), 200, answer[:300])
        self.assertEqual(raw_json(answer)["n_files"], 6000)

    def test_a_body_that_comes_too_slowly_gets_408_and_the_log_says_timeout(self):
        # SEC2-02: when the deadline cuts a body off, the client gets 408 "timeout" (only the
        # reading side is shut), and the log line says 408 timeout, not 400. Here 12 KB come
        # in the first 0.4 s and then nothing: less than 16 KB in the first second (the socket
        # timeout here), so the deadline cuts the body off before any single wait runs out.
        log_path = self.project.path / "var" / "log" / "server.log"
        log, handler = server.file_log(log_path)
        self.addCleanup(handler.close)
        self.running.app.log = log
        started = time.monotonic()
        with socket.create_connection(("127.0.0.1", self.port), timeout=10) as connection:
            connection.sendall(self.post_head(100000) + b'{"document": ')
            for _ in range(3):
                connection.sendall(b" " * 4096)
                time.sleep(0.2)
            answer = _read_all(connection)
        self.assertLess(time.monotonic() - started, 4.5)
        self.assertEqual(raw_status(answer), 408, answer[:300])
        self.assertEqual(raw_json(answer)["error"]["code"], "timeout")
        handler.flush()
        text = log_path.read_text(encoding="utf-8")
        self.assertRegex(text, r"POST /api/v1/registrations 408 admin \d+ms timeout")
        self.assertNotIn("/api/v1/registrations 400", text)
        self.assertEqual(http(self.port, "GET", "/api/v1/health", timeout=5).status, 200)

    def test_a_slow_drip_of_the_body_does_not_hold_a_thread(self):
        # SEC-06 for the body: one byte every 0.3 s keeps every single wait short, but far
        # less than 16 KB comes in each second (the socket timeout here), so the server lets go.
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        self.addCleanup(connection.close)
        connection.sendall(self.post_head(16 * 1024 * 1024) + b'{"document": ')
        started = time.monotonic()
        answer, closed = b"", False
        while time.monotonic() - started < 6:
            try:
                connection.sendall(b" ")
            except OSError:
                closed = True
                break
            connection.settimeout(0.3)
            try:
                chunk = connection.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                closed = True
                break
            if not chunk:
                closed = True
                break
            answer += chunk
        self.assertTrue(closed or answer, "the server still waited after 6 s")
        self.assertLess(time.monotonic() - started, 4)
        if answer:
            self.assertEqual(raw_status(answer), 408, answer[:300])
        self.assertEqual(http(self.port, "GET", "/api/v1/health", timeout=5).status, 200)


class TestSigninCode(ServerCase, support.SqliteCase, unittest.TestCase):

    def test_works_once(self):
        code = self.running.app.signin.new()
        first = http(self.port, "POST", "/api/v1/signin", {"code": code})
        self.assertEqual(first.status, 200)
        second = http(self.port, "POST", "/api/v1/signin", {"code": code})
        self.assertError(second, 401, "signin_refused")

    def test_expires_after_five_minutes(self):
        now = [1000.0]
        self.running.app.signin = server.SigninCode(clock=lambda: now[0])
        code = self.running.app.signin.new()
        now[0] += 299                           # still inside the five minutes
        self.assertEqual(http(self.port, "POST", "/api/v1/signin", {"code": code}).status, 200)
        late = self.running.app.signin.new()
        now[0] += 300                           # five minutes later: gone
        self.assertError(http(self.port, "POST", "/api/v1/signin", {"code": late}), 401,
                         "signin_refused")

    def test_a_wrong_or_missing_code_is_refused(self):
        self.assertError(http(self.port, "POST", "/api/v1/signin", {"code": "x" * 32}), 401,
                         "signin_refused")       # no code was made at all
        code = self.running.app.signin.new()
        for wrong in (code[:-1] + ("A" if code[-1] != "A" else "B"), code + "x", code.upper()
                      if code.upper() != code else code.lower(), "", " " + code):
            reply = http(self.port, "POST", "/api/v1/signin", {"code": wrong})
            self.assertIn(reply.status, (400, 401), wrong)
        self.assertError(http(self.port, "POST", "/api/v1/signin", {"code": 5}), 400)
        self.assertError(http(self.port, "POST", "/api/v1/signin", {}), 400)
        # the right code still works after the wrong ones
        self.assertEqual(http(self.port, "POST", "/api/v1/signin", {"code": code}).status, 200)

    def test_a_new_code_replaces_the_old_one(self):
        old = self.running.app.signin.new()
        new = self.running.app.signin.new()
        self.assertNotEqual(old, new)
        self.assertError(http(self.port, "POST", "/api/v1/signin", {"code": old}), 401)
        self.assertEqual(http(self.port, "POST", "/api/v1/signin", {"code": new}).status, 200)

    def test_a_switched_off_admin_gets_no_token_and_the_code_stays(self):
        with self.store.app() as db:
            auth.create_user(db, "second-admin", "admin")
            auth.set_disabled(db, "admin", True)
        code = self.running.app.signin.new()
        self.assertError(http(self.port, "POST", "/api/v1/signin", {"code": code}), 409)
        with self.store.app() as db:
            auth.set_disabled(db, "admin", False)
        self.assertEqual(http(self.port, "POST", "/api/v1/signin", {"code": code}).status, 200)


# ---------------------------------------------------------------------- from_lead 20a (SQLite)


class TestSqliteWriterOutlastsExpertQuery(ServerCase, support.SqliteCase, unittest.TestCase):
    """from_lead 20a: an expert query keeps a read lock on the catalogue until it ends; a
    registration's commit waits for it (sql.timeout_seconds + 5 s) instead of failing after
    5 s with db_busy, which the sandbox owner measured (5.2 s)."""

    settings_changes = {"ask.per_user_per_minute": 1000, "sql.timeout_seconds": 7}
    SLOW_SQL = ("SELECT COUNT(*) FROM sessions AS s, (WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL "
                "SELECT x + 1 FROM c) SELECT x FROM c) AS forever")

    def _register_during_slow_query(self, number):
        outcome = {}

        def slow():
            outcome["sql"] = self.call("POST", "/api/v1/sql", {"sql": self.SLOW_SQL},
                                       as_role="analyst", timeout=60)

        thread = threading.Thread(target=slow)
        thread.start()
        time.sleep(0.5)                     # the query runs and holds its read lock
        started = time.monotonic()
        reply = self.call("POST", "/api/v1/registrations", {"document": a_document(number)},
                          as_role="uploader", timeout=60)
        waited = time.monotonic() - started
        thread.join(60)
        self.assertEqual(outcome["sql"].json["error"]["code"], "sql_timeout")
        return reply, waited

    def test_the_wait_is_longer_than_the_longest_expert_query(self):
        self.assertEqual(sqlite_engine.busy_milliseconds(self.settings), 12000)
        self.assertEqual(sqlite_engine.busy_milliseconds(
            self.settings.changed({"sql.timeout_seconds": 0.5})), 5500)
        self.assertEqual(sqlite_engine.busy_milliseconds(
            self.settings.changed({"sql.timeout_seconds": 15})), 20000)

    def test_with_the_old_five_seconds_the_registration_failed(self):
        with mock.patch.object(sqlite_engine, "busy_milliseconds", lambda _settings: 5000):
            reply, waited = self._register_during_slow_query(50)
        self.assertError(reply, 503, "db_busy")
        self.assertGreater(waited, 4.5)

    def test_now_the_registration_waits_and_succeeds(self):
        reply, waited = self._register_during_slow_query(51)
        self.assertEqual(reply.status, 200, reply)
        self.assertEqual(reply.json["action"], "created")
        self.assertGreater(waited, 5.2)             # it outlasted the old limit
        self.assertLess(waited, 12)


# ---------------------------------------------------------------------- serve() itself


class FakeCluster:
    """Stands in for pgcluster.LocalCluster in the tests of serve()."""

    made = []
    running_before = False

    def __init__(self, settings):
        self.port = settings.database.postgresql.port
        self.data_dir = Path("pgdata")
        self.calls = []
        FakeCluster.made.append(self)

    def is_initialized(self):
        return True

    def ensure_running(self):
        self.calls.append("ensure_running")
        return not FakeCluster.running_before

    def stop(self):
        self.calls.append("stop")
        return True


class TestServe(support.SqliteCase, unittest.TestCase):
    """serve(): the printed sign-in link, and the local PostgreSQL cluster is stopped at the
    end only when serve started it. (The real cluster and Ctrl+C: tests/test_end_to_end.py.)"""

    def run_serve(self, settings, store):
        captured = {}
        real_make_server = server.make_server

        def make_server(*args, **kwargs):
            kwargs["port"] = 0
            made = captured["server"] = real_make_server(*args, **kwargs)
            return made

        out = io.StringIO()
        result = {}

        def run():
            try:
                result["code"] = server.serve(settings, out=out)
            except BaseException as error:      # noqa: BLE001
                result["error"] = error

        with mock.patch.object(server, "make_server", make_server), \
                mock.patch("pcldb.store.open_store", lambda _settings: store):
            thread = threading.Thread(target=run, daemon=True)
            thread.start()
            deadline = time.monotonic() + 20
            while "Open http" not in out.getvalue() and time.monotonic() < deadline:
                if "error" in result:
                    raise result["error"]
                time.sleep(0.05)
            port = captured["server"].port
            line = [text for text in out.getvalue().splitlines() if text.startswith("Open ")][0]
            code = re.fullmatch(r"Open http://127\.0\.0\.1:%d/#signin=([A-Za-z0-9_-]{32})"
                                % port, line).group(1)
            token = http(port, "POST", "/api/v1/signin", {"code": code}).json["token"]
            self.assertEqual(http(port, "GET", "/api/v1/me", token=token).status, 200)
            captured["server"].shutdown()
            thread.join(20)
        self.assertEqual(result.get("code"), 0, result)
        return out.getvalue()

    def setUp(self):
        super().setUp()
        from pcldb import cli
        cli.ensure_admin(self.store, self.settings)
        FakeCluster.made = []

    def test_sqlite_prints_the_link_and_stops_cleanly(self):
        text = self.run_serve(self.settings, self.store)
        self.assertIn("Stopping the website.", text)
        self.assertNotIn("PostgreSQL", text)
        log = (self.project.path / "var" / "log" / "server.log").read_text(encoding="utf-8")
        self.assertIn("/api/v1/me 200 admin", log)
        self.assertNotIn("signin=", log)

    def _postgres_settings(self):
        return self.settings.changed({"database.engine": "postgresql"})

    def test_the_cluster_it_started_is_stopped(self):
        FakeCluster.running_before = False
        with mock.patch("pcldb.store.pgcluster.LocalCluster", FakeCluster), \
                mock.patch("pcldb.store.postgres_engine.load_psycopg", lambda settings=None: None):
            text = self.run_serve(self._postgres_settings(), self.store)
        self.assertEqual(FakeCluster.made[0].calls, ["ensure_running", "stop"])
        self.assertIn("Stopped the local PostgreSQL database", text)

    def test_a_cluster_that_was_running_keeps_running(self):
        FakeCluster.running_before = True
        with mock.patch("pcldb.store.pgcluster.LocalCluster", FakeCluster), \
                mock.patch("pcldb.store.postgres_engine.load_psycopg", lambda settings=None: None):
            text = self.run_serve(self._postgres_settings(), self.store)
        self.assertEqual(FakeCluster.made[0].calls, ["ensure_running"])
        self.assertNotIn("Stopped the local PostgreSQL database", text)

    def test_a_second_serve_on_the_same_project_is_refused(self):
        # NEW-02: stopping the first would stop the database under the second.
        lock = server.ServeLock(self.settings)
        lock.acquire(18555)
        try:
            with self.assertRaises(server.PclError) as caught:
                server.serve(self.settings, out=io.StringIO())
            self.assertEqual(caught.exception.code, "already_serving")
            self.assertIn("running already for this project folder (process %d, port 18555)"
                          % os.getpid(), caught.exception.message)
            # the refusal changed nothing: the first server's lock and note are still there
            self.assertTrue((self.settings.var_dir / server.SERVE_INFO_FILE).is_file())
        finally:
            lock.release()
        self.assertFalse((self.settings.var_dir / server.SERVE_INFO_FILE).exists())
        text = self.run_serve(self.settings, self.store)     # free again once it stopped
        self.assertIn("Stopping the website.", text)
        again = server.ServeLock(self.settings)
        again.acquire(1)           # and serve let go of it at the end
        again.release()

    def test_a_folder_that_cannot_be_locked_is_not_called_already_serving(self):
        # SEC2-06: only "held by another process" means another serve. A folder whose file
        # system has no locks (ENOTSUP, for example a network folder) gets a note that names
        # the real error, and serve runs without the check.
        import errno

        def unsupported(_self, _handle):
            raise OSError(errno.ENOTSUP, "Operation not supported")

        with mock.patch.object(server.ServeLock, "_lock", unsupported):
            lock = server.ServeLock(self.settings)
            lock.acquire(18556)
            try:
                self.assertFalse(lock.locked)
                self.assertIn("cannot be locked (Operation not supported)", lock.note)
                self.assertIn(str(self.settings.var_dir / server.SERVE_LOCK_FILE), lock.note)
                self.assertNotIn("running already", lock.note)
            finally:
                lock.release()
            self.assertFalse((self.settings.var_dir / server.SERVE_INFO_FILE).exists())
            text = self.run_serve(self.settings, self.store)
        self.assertIn("is not available here", text)
        self.assertIn("Stopping the website.", text)
        # A lock that another process holds is still "running already", whatever the system
        # says it with.
        held = [errno.EWOULDBLOCK, errno.EAGAIN, errno.EACCES]
        if os.name == "nt":
            held.append(getattr(errno, "EDEADLOCK", 36))
        for number in held:
            def busy(_self, _handle, number=number):
                raise OSError(number, os.strerror(number))

            with mock.patch.object(server.ServeLock, "_lock", busy):
                with self.assertRaises(server.PclError) as caught:
                    server.ServeLock(self.settings).acquire(18557)
            self.assertEqual(caught.exception.code, "already_serving", number)

    def test_a_port_in_use_is_a_plain_sentence(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            port = taken.getsockname()[1]
            with self.assertRaises(server.PclError) as caught:
                server.make_server(self.settings, port=port, store=self.store)
        self.assertEqual(caught.exception.code, "port_in_use")
        self.assertIn("pcldb serve --port", caught.exception.message)

    def test_an_empty_store_is_refused_before_anything_listens(self):
        empty = support.TempProject("sqlite")
        self.addCleanup(empty.cleanup)
        from pcldb.store import open_store
        store = open_store(empty.settings)
        with self.assertRaises(server.PclError) as caught:
            with mock.patch("pcldb.store.open_store", lambda _settings: store):
                server.serve(empty.settings, out=io.StringIO())
        self.assertIn("pcldb setup", caught.exception.message)


class TestHelpers(unittest.TestCase):

    def test_address_for(self):
        self.assertEqual(server.address_for("127.0.0.1", 9), "http://127.0.0.1:9/")
        self.assertEqual(server.address_for("0.0.0.0", 9), "http://127.0.0.1:9/")
        self.assertEqual(server.address_for("::1", 9), "http://[::1]:9/")

    def test_parse_json_body(self):
        self.assertEqual(server.parse_json_body(b'\xef\xbb\xbf{"a": 1}'), {"a": 1})
        for raw in (b"", b" ", b"[]", b"1", b'{"a": NaN}', b'{"a":1,"a":2}', b"\x80"):
            with self.assertRaises(server.PclError):
                server.parse_json_body(raw)

    def test_the_route_table_matches_the_specification(self):
        self.assertEqual({(method, path[len("/api/v1"):]): needs
                          for (method, path), (_function, needs, _limit) in server.ROUTES.items()},
                         {("GET", "/health"): None, ("POST", "/signin"): None,
                          ("GET", "/me"): "any", ("GET", "/status"): "any",
                          ("POST", "/ask"): "ask", ("POST", "/search"): "ask",
                          ("GET", "/session"): "ask", ("POST", "/sql"): "sql",
                          ("GET", "/sql/schema"): "sql", ("GET", "/sql/examples"): "sql",
                          ("GET", "/sql/history"): "sql", ("POST", "/registrations"): "register",
                          ("GET", "/registrations"): "ask"})
        limits = {path: limit for (method, path), (_f, _n, limit) in server.ROUTES.items()
                  if method == "POST"}
        self.assertEqual(limits.pop("/api/v1/registrations"), 16 * 1024 * 1024)
        self.assertEqual(set(limits.values()), {64 * 1024})


if __name__ == "__main__":
    unittest.main()
