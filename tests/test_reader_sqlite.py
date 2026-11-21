"""The SQLite expert-mode sandbox (pcldb/store/reader_sqlite.py, specification section 9).

HOSTILE is the list of statements a person might type to read the private file, to write, to
escape the sandbox or to hang the backend, each with the outcome it must have and the layer
that stops it. After the whole list ran, both database files are byte for byte what they were,
no file appeared, and the marker text that the fixture puts into the private tables never came
back in a result or in an error message.

Measured on this build (macOS, Python 3.13, SQLite 3.45.3), kept here because they explain
the limits in reader_sqlite.py:

- instr(haystack of 800 000 'a', needle of 400 000 'a' + 'b'): 2.75 s inside ONE step, where
  neither the progress handler nor interrupt() reaches; the time grows with the square of the
  size (about 7 minutes at the 10 MB value limit). replace() behaves the same.
- trim(5 MB of 'a', a set of 20 000 different characters): 183 s, not interruptible.
- json_patch of an object with 80 000 keys onto itself: 10 s, not interruptible.
- LIKE '%' + 1000 'a' + 'b' against 9 MB of 'a': 7.6 s; with the pattern limit of 200: 1.3 s.
- 20 WITH parts that each read the one before twice: 1.7 GB of memory and 7 s while SQLite
  compiles the statement; with the 256 MB cap on SQLite's memory: "out of memory" after 0.3 s.
- A sort of made-up rows writes SQLite's temporary files at about 115 MB per second until the
  deadline (697 MB in 6 s); they are removed when the statement ends.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import threading
import time
import unittest

from pcldb import sqlrun, util
from pcldb.errors import PclError
from pcldb.store import open_store, reader_sqlite
from tests import support

MARKER = "PRIVATE-MARKER-5e1f9c"
TIMEOUT = 1.5       # seconds, for the statements that must run into the deadline

OK, REFUSED, ERROR, TIMED_OUT = "ok", "sql_refused", "sql_error", "sql_timeout"


def _recursive(rows):
    return ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r WHERE n < %d) "
            % rows)


def _fan_out(depth):
    parts = ["c0(x) AS (SELECT 1)"]
    for level in range(1, depth + 1):
        parts.append("c%d(x) AS (SELECT a.x FROM c%d a, c%d b)" % (level, level - 1, level - 1))
    return "WITH " + ", ".join(parts) + " SELECT count(*) FROM c%d" % depth


def _chain(parts, before=""):
    """WITH parts, each reading the one before (through a sub-query behind the operators
    `before` when given): sub-queries nested `parts` deep without a single bracket level more."""
    if before:
        body = ["a%d AS (SELECT %s(SELECT x FROM a%d) x)" % (i, before, i - 1)
                for i in range(1, parts)]
    else:
        body = ["a%d AS (SELECT * FROM a%d)" % (i, i - 1) for i in range(1, parts)]
    return ("WITH a0 AS (SELECT 1 x), " + ", ".join(body)
            + " SELECT * FROM a%d" % (parts - 1))


def _flat_chain(parts, before):
    """WITH parts that each put the operators `before` on the column of the one before:
    SQLite makes ONE expression of them, as deep as all the operators together."""
    body = ["a%d AS (SELECT %sx x FROM a%d)" % (i, before, i - 1) for i in range(1, parts)]
    return ("WITH a0 AS (SELECT 1 x), " + ", ".join(body)
            + " SELECT * FROM a%d" % (parts - 1))


def _has_dbstat():
    """Whether this SQLite has the dbstat table (Python's Windows build has none)."""
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("SELECT count(*) FROM dbstat").fetchone()
        return True
    except sqlite3.Error:
        return False
    finally:
        connection.close()


# Where this SQLite has dbstat, the authorizer refuses it; where it has none, SQLite itself
# stops at "no such table: dbstat" (an error, so still nothing is read).
_DBSTAT = REFUSED if _has_dbstat() else ERROR


# (statement, outcome, the layer that stops it). {app} = the private file, {dir} = its folder.
HOSTILE = [
    # ---- every write and every change of structure: the authorizer
    ("INSERT INTO sessions (path) VALUES ('x')", REFUSED, "authorizer"),
    ("INSERT INTO pcl.sessions (path) VALUES ('x')", REFUSED, "authorizer"),
    ("INSERT OR REPLACE INTO meta VALUES ('schema_version', '9')", REFUSED, "authorizer"),
    ("REPLACE INTO pcl.meta VALUES ('schema_version', '9')", REFUSED, "authorizer"),
    ("UPDATE sessions SET notes = 'x'", REFUSED, "authorizer"),
    ("UPDATE pcl.sessions SET withdrawn = 1 WHERE 1", REFUSED, "authorizer"),
    ("DELETE FROM sessions", REFUSED, "authorizer"),
    ("DELETE FROM pcl.files WHERE bytes > 0", REFUSED, "authorizer"),
    ("WITH x AS (SELECT 1) INSERT INTO meta SELECT 'a', 'b' FROM x", REFUSED, "authorizer"),
    ("WITH x AS (SELECT 1) DELETE FROM sessions", REFUSED, "authorizer"),
    ("CREATE TABLE t (x)", REFUSED, "authorizer"),
    ("CREATE TABLE pcl.t (x)", REFUSED, "authorizer"),
    ("CREATE TABLE main.t AS SELECT * FROM sessions", REFUSED, "authorizer"),
    ("CREATE TEMP TABLE t (x)", REFUSED, "authorizer"),
    ("CREATE TEMPORARY TABLE t AS SELECT * FROM sessions", REFUSED, "authorizer"),
    ("CREATE VIEW v AS SELECT 1", REFUSED, "authorizer"),
    ("CREATE TEMP VIEW v AS SELECT * FROM sessions", REFUSED, "authorizer"),
    ("CREATE INDEX pcl.i ON sessions (folder)", REFUSED, "authorizer"),
    ("CREATE TRIGGER pcl.g AFTER INSERT ON sessions BEGIN SELECT 1; END", REFUSED,
     "authorizer"),
    ("CREATE TEMP TRIGGER g AFTER INSERT ON pcl.sessions BEGIN SELECT 1; END", REFUSED,
     "authorizer"),
    ("CREATE VIRTUAL TABLE temp.f USING fts5(x)", REFUSED, "authorizer"),
    ("DROP TABLE sessions", REFUSED, "authorizer"),
    ("DROP VIEW session_summary", REFUSED, "authorizer"),
    ("DROP INDEX sessions_started", REFUSED, "authorizer"),
    ("ALTER TABLE sessions ADD COLUMN x", REFUSED, "authorizer"),
    ("ALTER TABLE sessions RENAME TO s2", REFUSED, "authorizer"),
    ("REINDEX", REFUSED, "authorizer"),
    ("ANALYZE", REFUSED, "authorizer"),
    ("EXPLAIN INSERT INTO meta VALUES ('a', 'b')", REFUSED, "authorizer"),
    ("EXPLAIN QUERY PLAN DELETE FROM sessions", REFUSED, "authorizer"),
    # ---- several statements in one text: the driver
    ("SELECT 1; SELECT 2", REFUSED, "driver"),
    ("SELECT 1; DELETE FROM sessions", REFUSED, "driver"),
    ("SELECT 1 /* x */; INSERT INTO meta VALUES ('a', 'b') --", REFUSED, "driver"),
    ("select 1;\n\n  ;drop table sessions", REFUSED, "driver"),
    ("SELECT 1;;", REFUSED, "driver"),
    # ---- comments and odd spacing do not change what a statement is
    ("/* hi */ -- x\n  DELETE FROM sessions", REFUSED, "authorizer"),
    ("\t\n INSERT/**/INTO/**/meta VALUES ('a', 'b')", REFUSED, "authorizer"),
    ("-- SELECT\nUPDATE sessions SET notes = 'x' -- SELECT", REFUSED, "authorizer"),
    ("/* /* */ DROP TABLE sessions", REFUSED, "authorizer"),
    # ---- the private tables: they are not on this connection at all
    ("SELECT * FROM users", ERROR, "the private file is not attached"),
    ("SELECT display_name FROM main.users", ERROR, "the private file is not attached"),
    ("SELECT * FROM app.users", REFUSED, "the private file is not attached (K-04: the same "
     "answer as PostgreSQL)"),
    ("SELECT * FROM pcl.users", ERROR, "the private file is not attached"),
    ("SELECT token_sha256 FROM tokens", ERROR, "the private file is not attached"),
    ("SELECT * FROM \"tokens\"", ERROR, "the private file is not attached"),
    ("SELECT query_text FROM [query_log]", ERROR, "the private file is not attached"),
    ("SELECT/**/name/**/FROM/**/users", ERROR, "the private file is not attached"),
    ("SELECT count(*) FROM search_entries", ERROR, "the private file is not attached"),
    ("SELECT sql FROM app.sqlite_master", REFUSED, "the private file is not attached"),
    # ---- opening the private file, or any other file
    ("ATTACH DATABASE '{app}' AS app", REFUSED, "authorizer"),
    ("ATTACH DATABASE 'file:{app}?mode=ro' AS a2", REFUSED, "authorizer"),
    ("ATTACH '{app}' AS pcl2", REFUSED, "authorizer"),
    ("ATTACH ':memory:' AS m", REFUSED, "authorizer"),
    ("ATTACH '' AS t", REFUSED, "authorizer"),
    ("ATTACH DATABASE '{dir}/made_by_attach.sqlite' AS n", REFUSED, "authorizer"),
    ("DETACH DATABASE pcl", REFUSED, "authorizer"),
    ("VACUUM", REFUSED, "authorizer"),
    ("VACUUM INTO '{dir}/stolen.sqlite'", REFUSED, "authorizer"),
    ("VACUUM pcl INTO '{dir}/stolen2.sqlite'", REFUSED, "authorizer"),
    # ---- PRAGMA, as a statement and as a table
    ("PRAGMA database_list", REFUSED, "authorizer"),
    ("PRAGMA table_info(sessions)", REFUSED, "authorizer"),
    ("PRAGMA query_only = 0", REFUSED, "authorizer"),
    ("PRAGMA writable_schema = 1", REFUSED, "authorizer"),
    ("PRAGMA journal_mode = WAL", REFUSED, "authorizer"),
    ("PRAGMA pcl.journal_mode = wal", REFUSED, "authorizer"),
    ("PRAGMA trusted_schema = 1", REFUSED, "authorizer"),
    ("PRAGMA hard_heap_limit = 0", REFUSED, "authorizer"),
    ("PRAGMA temp_store_directory = '{dir}'", REFUSED, "authorizer"),
    ("PRAGMA module_list", REFUSED, "authorizer"),
    ("SELECT * FROM pragma_database_list", REFUSED, "authorizer"),
    ("SELECT file FROM pragma_database_list()", REFUSED, "authorizer"),
    ("SELECT name FROM pragma_table_info('sessions')", REFUSED, "authorizer"),
    ("SELECT count(*) FROM pragma_function_list", REFUSED, "authorizer"),
    # ---- other schemas' lists of tables, and SQLite's built-in tables
    ("SELECT * FROM temp.sqlite_master", REFUSED, "authorizer"),
    ("SELECT * FROM sqlite_temp_master", REFUSED, "authorizer"),
    ("SELECT * FROM dbstat", _DBSTAT, "authorizer (or no such table in this SQLite)"),
    ("SELECT count(*) FROM pcl.dbstat", _DBSTAT, "authorizer (or no such table)"),
    ("SELECT * FROM sessions NATURAL JOIN dbstat", _DBSTAT, "authorizer (or no such table)"),
    ("SELECT * FROM fts3tokenize('simple', 'a b')", REFUSED, "authorizer"),
    # ---- functions that reach outside or tell about the build
    ("SELECT load_extension('{dir}/x')", REFUSED, "authorizer"),
    ("SELECT load_extension('x', 'entry')", REFUSED, "authorizer"),
    ("SELECT fts3_tokenizer('simple')", REFUSED, "authorizer"),
    ("SELECT sqlite_compileoption_get(0)", REFUSED, "authorizer"),
    ("SELECT sqlite_source_id()", REFUSED, "authorizer"),
    ("SELECT sqlite_log(1, 'x')", REFUSED, "authorizer"),
    ("SELECT json_patch('{}', '{}')", REFUSED, "authorizer"),
    ("SELECT readfile('/etc/hosts')", ERROR, "no such function in this SQLite"),
    ("SELECT writefile('{dir}/w.txt', 'x')", ERROR, "no such function in this SQLite"),
    # ---- transactions typed by the person
    ("BEGIN", REFUSED, "authorizer"),
    ("BEGIN IMMEDIATE", REFUSED, "authorizer"),
    ("BEGIN EXCLUSIVE", REFUSED, "authorizer"),
    ("COMMIT", REFUSED, "authorizer"),
    ("END", REFUSED, "authorizer"),
    ("ROLLBACK", REFUSED, "authorizer"),
    ("SAVEPOINT a", REFUSED, "authorizer"),
    ("RELEASE a", REFUSED, "authorizer"),
    ("ROLLBACK TO a", REFUSED, "authorizer"),
    # ---- parameters and broken text
    ("SELECT * FROM sessions WHERE path = ?1", REFUSED, "driver"),
    ("SELECT :name, @other, $third", REFUSED, "driver"),
    ("SELECT 1\x00; DROP TABLE sessions", REFUSED, "driver"),
    # ---- time: must end at the deadline
    (_recursive(10 ** 12) + "SELECT count(*) FROM r", TIMED_OUT, "timeout"),
    (_recursive(100000) + "SELECT count(*) FROM r a, r b", TIMED_OUT, "timeout"),
    # ---- size: huge values and huge work
    ("SELECT zeroblob(2000000000)", REFUSED, "limit"),
    ("SELECT hex(zeroblob(9000000))", REFUSED, "limit"),
    ("SELECT length(randomblob(2000000000))", REFUSED, "limit"),
    ("SELECT length(replace(printf('%.*c', 9000000, 'a'), 'a', 'bbbb'))", REFUSED, "limit"),
    (_recursive(100) + "SELECT length(group_concat(printf('%.*c', 1000000, 'x'))) FROM r",
     REFUSED, "limit"),
    ("SELECT instr(printf('%.*c', 800000, 'a'), printf('%.*c', 400000, 'a') || 'b')",
     REFUSED, "limit (guarded function)"),
    ("SELECT length(replace(printf('%.*c', 800000, 'a'), printf('%.*c', 400000, 'a') || 'b', "
     "'c'))", REFUSED, "limit (guarded function)"),
    ("SELECT 'aaa' LIKE '%' || printf('%.*c', 300, 'a')", ERROR, "limit"),
    (_fan_out(24), REFUSED, "limit (memory)"),
    ("SELECT " + "(" * 5000 + "1" + ")" * 5000, REFUSED, "limit: brackets"),
    ("SELECT * FROM " + "(SELECT * FROM " * 1000 + "(SELECT 1)" + ")" * 1000, REFUSED,
     "limit: brackets"),
    ("SELECT " + " OR ".join(["1"] * 4000), ERROR, "limit"),
    (" UNION ALL ".join(["SELECT 1"] * 150), ERROR, "limit: terms of a compound SELECT"),
    (" UNION ALL ".join(["SELECT 1"] * 600), REFUSED, "limit: SELECT parts"),
    (_chain(700), REFUSED, "limit: SELECT parts"),
    # ---- allowed, and harmless: what a reader may see
    ("SELECT printf('%.*c', 2000000000, 'x')", OK, "allowed: SQLite gives NULL"),
    (_recursive(100000000) + "SELECT n FROM r", OK, "allowed: cut at the row limit"),
    (_recursive(100) + "SELECT zeroblob(9000000) FROM r", OK, "allowed: cut at the size limit"),
    ("SELECT name FROM sqlite_master", OK, "allowed: the empty in-memory database"),
    ("SELECT name, sql FROM pcl.sqlite_master", OK, "allowed: the catalogue's own definitions"),
    ("SELECT sqlite_version()", OK, "allowed: the engine's version"),
    ("SELECT * FROM pcl.sessions AS users", OK, "allowed: only a name"),
]


def _file_state(folder):
    """{file name: SHA-256 of its bytes} for a folder."""
    state = {}
    for name in sorted(os.listdir(folder)):
        with open(os.path.join(folder, name), "rb") as handle:
            state[name] = hashlib.sha256(handle.read()).hexdigest()
    return state


class ReaderBase(support.SqliteCase):
    def setUp(self):
        super().setUp()
        with self.store.app() as db:
            self.keys = support.seed_minimal(db)
            now = util.now_utc_text()
            db.insert("app.users", {"user_id": "u-" + MARKER, "name": "marker-user",
                                    "display_name": MARKER, "role": "viewer", "disabled": False,
                                    "created_utc": now})
            db.insert("app.tokens", {"token_id": "t-marker", "user_id": "u-" + MARKER,
                                     "token_sha256": util.sha256_hex(MARKER.encode("utf-8")),
                                     "label": MARKER, "created_utc": now})
            db.insert("app.query_log", {"log_id": "l-marker", "at_utc": now, "user_name": MARKER,
                                        "mode": "sql", "query_text": "select '%s'" % MARKER,
                                        "ok": True})

    def attempt(self, sql, max_rows=50, timeout=TIMEOUT):
        """(outcome, text): "ok" and the result as text, or the error's code and message."""
        try:
            with self.store.reader() as reader:
                columns, rows, truncated = reader.run(sql, max_rows=max_rows,
                                                      timeout_seconds=timeout)
        except PclError as error:
            return error.code, error.message + repr(error.details)
        return OK, repr((columns, rows, truncated))

    def read(self, sql, **arguments):
        with self.store.reader() as reader:
            return reader.run(sql, **arguments)

    def dump(self):
        """Every row of every table of both files."""
        rows = {}
        for table in ("sessions", "files", "runs", "points", "reviews", "models", "problems",
                      "session_rollup", "meta"):
            rows["pcl." + table] = self.outside_rows(
                "SELECT * FROM {pcl}.%s ORDER BY 1, 2" % table)
        for table in ("users", "tokens", "registrations", "query_log", "search_entries", "meta"):
            rows["app." + table] = self.outside_rows(
                "SELECT * FROM {app}.%s ORDER BY 1, 2" % table)
        rows["objects"] = self.outside_rows(
            "SELECT type, name, sql FROM {pcl}.sqlite_master ORDER BY 1, 2") + self.outside_rows(
            "SELECT type, name, sql FROM {app}.sqlite_master ORDER BY 1, 2")
        return rows


class TestHostileStatements(ReaderBase, unittest.TestCase):
    def test_the_list_is_long_enough(self):
        self.assertGreaterEqual(len(HOSTILE), 40)
        self.assertEqual(len({sql for sql, _outcome, _layer in HOSTILE}), len(HOSTILE))

    def test_every_statement_has_its_outcome_and_nothing_changes(self):
        folder = self.store.folder
        files_before = _file_state(folder)
        rows_before = self.dump()
        project_before = sorted(os.listdir(self.project.path))
        wrong = []
        started = time.monotonic()
        for sql, expected, layer in HOSTILE:
            sql = sql.replace("{app}", str(self.store.app_path)).replace("{dir}", str(folder))
            outcome, text = self.attempt(sql)
            if outcome != expected:
                wrong.append("%s\n    expected %s (%s), got %s: %s"
                             % (sql[:120], expected, layer, outcome, text[:200]))
            self.assertNotIn(MARKER, text, "a private value came back for: " + sql[:120])
            self.assertNotIn(str(self.store.app_path), text.replace(sql, ""),
                             "the private file's path came back for: " + sql[:120])
        self.assertEqual(wrong, [], "\n".join(wrong))
        # Two statements run into the deadline; nothing else may take long.
        self.assertLess(time.monotonic() - started, 2 * TIMEOUT + 20)
        self.assertEqual(_file_state(folder), files_before)     # byte for byte, no new file
        self.assertEqual(self.dump(), rows_before)
        self.assertEqual(sorted(os.listdir(self.project.path)), project_before)

    def test_the_private_tables_really_hold_the_marker(self):
        # Otherwise "the marker never came back" would prove nothing.
        found = self.outside_rows("SELECT display_name FROM {app}.users WHERE display_name = ?",
                                  [MARKER])
        self.assertEqual(found, [(MARKER,)])
        with open(self.store.app_path, "rb") as handle:
            self.assertIn(MARKER.encode("utf-8"), handle.read())
        with open(self.store.catalog_path, "rb") as handle:
            self.assertNotIn(MARKER.encode("utf-8"), handle.read())


class TestWhatWorks(ReaderBase, unittest.TestCase):
    def test_plain_and_qualified_names_both_resolve(self):
        for sql in ("SELECT count(*) FROM sessions", "SELECT count(*) FROM pcl.sessions",
                    "SELECT count(*) FROM session_summary",
                    "SELECT count(*) FROM pcl.session_summary"):
            columns, rows, truncated = self.read(sql)
            self.assertEqual(rows, [(2,)], sql)
            self.assertFalse(truncated)
            self.assertEqual(columns, [("count(*)", "integer")])

    def test_views_lists_and_json(self):
        _columns, rows, _ = self.read(
            "SELECT sample_id FROM session_samples ORDER BY sample_id")
        self.assertEqual(rows, [("S-100",), ("S-200",), ("S-201",)])
        _columns, rows, _ = self.read(
            "SELECT s.folder, j.value FROM sessions s, json_each(s.sample_ids) j "
            "WHERE j.value = 'S-200'")
        self.assertEqual(rows, [(support.SIMULATED_PATH.rsplit("/", 1)[-1], "S-200")])
        _columns, rows, _ = self.read("SELECT label, n FROM session_labels ORDER BY label")
        self.assertEqual(rows, [("UNKNOWN", 1), ("adenine", 1), ("r6g", 1)])
        _columns, rows, _ = self.read(
            "SELECT point_id, effective_label FROM current_points ORDER BY point_id")
        self.assertEqual(rows, [(1, "adenine"), (2, "r6g"), (3, "UNKNOWN")])
        _columns, rows, _ = self.read(
            "SELECT json_extract(card_json, '$.schema'), card_json ->> 'schema' "
            "FROM sessions LIMIT 1")
        self.assertEqual(rows, [("pcl-session/1", "pcl-session/1")])

    def test_query_shapes(self):
        for sql, expected in (
                ("WITH x AS (SELECT path FROM sessions) SELECT count(*) FROM x", [(2,)]),
                ("SELECT count(*) FROM (SELECT 1 UNION ALL SELECT 2) q", [(2,)]),
                ("VALUES (1, 'a'), (2, 'b')", [(1, "a"), (2, "b")]),
                ("SELECT 1;", [(1,)]),
                ("SELECT 1; -- done", [(1,)]),
                ("/* first */ -- second\n SELECT 2", [(2,)]),
                (_recursive(5) + "SELECT sum(n) FROM r", [(15,)]),
                ("SELECT path LIKE 'raw/%', path LIKE 'RAW/%' FROM sessions LIMIT 1", [(1, 0)]),
                ("SELECT count(*) OVER (), row_number() OVER (ORDER BY path) FROM sessions "
                 "LIMIT 1", [(2, 1)]),
                ("SELECT instr('abc', 'c'), replace('abc', 'b', 'x'), trim('xxaxx', 'x'), "
                 "ltrim('xa', 'x'), rtrim('ax', 'x')", [(3, "axc", "a", "a", "a")])):
            self.assertEqual(self.read(sql)[1], expected, sql)
        columns, rows, _ = self.read("EXPLAIN QUERY PLAN SELECT * FROM sessions")
        self.assertEqual([name for name, _kind in columns], ["id", "parent", "notused", "detail"])
        self.assertTrue(rows)

    def test_column_kinds_come_from_the_values(self):
        columns, rows, _ = self.read(
            "SELECT 1 AS i, 1.5 AS r, 'x' AS t, x'00ff' AS b, NULL AS n, 9e999 AS inf")
        self.assertEqual(columns, [("i", "integer"), ("r", "real"), ("t", "text"),
                                   ("b", "blob"), ("n", ""), ("inf", "real")])
        self.assertEqual(rows[0][:5], (1, 1.5, "x", b"\x00\xff", None))
        self.assertEqual(rows[0][5], float("inf"))
        columns, rows, _ = self.read("SELECT path FROM sessions WHERE 0")
        self.assertEqual((columns, rows), ([("path", "")], []))

    def test_rows_are_cut_at_max_rows(self):
        columns, rows, truncated = self.read("SELECT rel_path FROM files ORDER BY 1", max_rows=3)
        self.assertEqual(len(rows), 3)
        self.assertTrue(truncated)
        _columns, rows, truncated = self.read("SELECT rel_path FROM files", max_rows=8)
        self.assertEqual(len(rows), 8)
        self.assertFalse(truncated)

    def test_rows_are_cut_at_the_size_limit(self):
        original = sqlrun.MAX_RESULT_BYTES
        sqlrun.MAX_RESULT_BYTES = 3000000
        self.addCleanup(setattr, sqlrun, "MAX_RESULT_BYTES", original)
        _columns, rows, truncated = self.read(
            _recursive(1000) + "SELECT zeroblob(1000000) FROM r", max_rows=1000)
        self.assertEqual(len(rows), 3)
        self.assertTrue(truncated)

    def test_a_statement_that_eats_temporary_disk_space_is_stopped(self):
        original = reader_sqlite.TEMP_DISK_BUDGET_BYTES
        reader_sqlite.TEMP_DISK_BUDGET_BYTES = 100 * 1024 * 1024
        self.addCleanup(setattr, reader_sqlite, "TEMP_DISK_BUDGET_BYTES", original)
        started = time.monotonic()
        with self.assertRaises(PclError) as caught:
            # Sorting random megabytes: SQLite writes its temporary files at full speed.
            self.read(_recursive(100000000) + "SELECT length(b) FROM "
                      "(SELECT randomblob(100000) b FROM r) ORDER BY b", timeout_seconds=20)
        self.assertEqual(caught.exception.code, "sql_refused")
        self.assertIn("temporary disk space", caught.exception.message)
        self.assertLess(time.monotonic() - started, 10)
        # An ordinary sort is not touched by the watch.
        self.assertEqual(len(self.read("SELECT rel_path FROM files ORDER BY bytes")[1]), 8)

    def test_a_hundred_million_rows_come_back_cut_and_fast(self):
        started = time.monotonic()
        _columns, rows, truncated = self.read(
            _recursive(100000000) + "SELECT n, printf('%.*c', 100, 'x') FROM r", max_rows=1000)
        self.assertEqual(len(rows), 1000)
        self.assertTrue(truncated)
        self.assertLess(time.monotonic() - started, 5.0)

    def test_a_reader_runs_one_statement_only(self):
        with self.store.reader() as reader:
            reader.run("SELECT 1")
            with self.assertRaises(RuntimeError):
                reader.run("SELECT 1")

    def test_bad_arguments(self):
        for arguments in ({"max_rows": 0}, {"max_rows": True}, {"timeout_seconds": 0}):
            with self.store.reader() as reader:
                with self.assertRaises(ValueError):
                    reader.run("SELECT 1", **arguments)
        with self.store.reader() as reader:
            with self.assertRaises(PclError) as caught:
                reader.run(b"SELECT 1")
        self.assertEqual(caught.exception.code, "sql_refused")


class TestTheConnection(ReaderBase, unittest.TestCase):
    def test_only_the_catalogue_is_attached_read_only(self):
        reader = self.store.reader()
        self.addCleanup(reader.close)
        connection = reader._connection
        self.assertEqual(connection.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED), 1)
        self.assertEqual(connection.getlimit(sqlite3.SQLITE_LIMIT_LENGTH),
                         reader_sqlite.VALUE_LIMIT_BYTES)
        self.assertEqual(connection.getlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH),
                         4 * self.settings.sql.max_sql_chars + 64)
        connection.set_authorizer(None)         # the test may look; a person never can
        databases = {row[1]: row[2] for row in connection.execute("PRAGMA database_list")}
        self.assertEqual(sorted(databases), ["main", "pcl"])
        self.assertEqual(databases["main"], "")
        self.assertEqual(os.path.realpath(databases["pcl"]),
                         os.path.realpath(self.store.catalog_path))
        self.assertEqual(connection.execute("PRAGMA query_only").fetchone()[0], 1)
        self.assertEqual(connection.execute("PRAGMA trusted_schema").fetchone()[0], 0)
        # Even without the authorizer the file itself is opened read-only.
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("INSERT INTO pcl.meta VALUES ('a', 'b')")
        connection.execute("PRAGMA query_only=OFF")
        with self.assertRaises(sqlite3.OperationalError):
            connection.execute("INSERT INTO pcl.meta VALUES ('a', 'b')")

    def test_set_up_statements_cannot_be_replayed(self):
        # The reader itself ran these before the authorizer was set. Typed by a person, the
        # same texts must be judged anew (no cached statement slips through).
        for sql in ("PRAGMA module_list", "ATTACH DATABASE ? AS pcl", "PRAGMA query_only=ON",
                    "PRAGMA trusted_schema=OFF", "PRAGMA case_sensitive_like=ON",
                    "SELECT name, type, sql FROM pcl.sqlite_master WHERE type IN "
                    "('table', 'view')"):
            outcome, _text = self.attempt(sql)
            self.assertEqual(outcome, OK if sql.startswith("SELECT") else REFUSED, sql)

    def test_the_memory_cap_is_set_for_the_process(self):
        self.store.reader().close()
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        limit = connection.execute("PRAGMA hard_heap_limit").fetchone()[0]
        self.assertEqual(limit, reader_sqlite.MEMORY_LIMIT_BYTES)

    def test_a_missing_catalogue_says_set_up_first(self):
        self.store.catalog_path.unlink()
        with self.assertRaises(PclError) as caught:
            self.store.reader()
        self.assertEqual(caught.exception.code, "not_initialized")

    def test_a_locked_catalogue_is_busy_not_stuck(self):
        writer = sqlite3.connect(self.store.catalog_path, isolation_level=None)
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with self.assertRaises(PclError) as caught:
            self.read("SELECT count(*) FROM sessions", timeout_seconds=10)
        self.assertEqual(caught.exception.code, "db_busy")
        self.assertLess(time.monotonic() - started, reader_sqlite.BUSY_SECONDS + 2)
        writer.execute("ROLLBACK")
        self.assertEqual(self.read("SELECT count(*) FROM sessions")[1], [(2,)])


class TestDeadline(ReaderBase, unittest.TestCase):
    RUNAWAY = _recursive(10 ** 12) + "SELECT count(*) FROM r"

    def test_the_progress_handler_stops_a_runaway_query(self):
        reader = self.store.reader()
        reader._watchdog = lambda: None         # the progress handler alone
        started = time.monotonic()
        with self.assertRaises(PclError) as caught:
            reader.run(self.RUNAWAY, timeout_seconds=1.0)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "sql_timeout")
        self.assertGreaterEqual(elapsed, 0.9)
        self.assertLess(elapsed, 1.0 + reader_sqlite.WATCHDOG_GRACE_SECONDS + 2.0)
        self.assertIn("1 seconds", caught.exception.message)

    def test_the_watchdog_stops_it_when_the_progress_handler_does_not(self):
        reader = self.store.reader()
        reader._progress = lambda: 0            # as if the first line had failed
        started = time.monotonic()
        with self.assertRaises(PclError) as caught:
            reader.run(self.RUNAWAY, timeout_seconds=1.0)
        elapsed = time.monotonic() - started
        self.assertEqual(caught.exception.code, "sql_timeout")
        self.assertGreaterEqual(elapsed, 1.0 + reader_sqlite.WATCHDOG_GRACE_SECONDS - 0.1)
        self.assertLess(elapsed, 1.0 + reader_sqlite.WATCHDOG_GRACE_SECONDS + 3.0)

    def test_the_connection_is_closed_after_a_timeout(self):
        reader = self.store.reader()
        with self.assertRaises(PclError):
            reader.run(self.RUNAWAY, timeout_seconds=0.3)
        self.assertIsNone(reader._connection)
        self.assertIsNone(reader._helper)
        # A writer gets the catalogue at once: no lock is left behind.
        writer = sqlite3.connect(self.store.catalog_path, isolation_level=None, timeout=0.2)
        self.addCleanup(writer.close)
        writer.execute("BEGIN EXCLUSIVE")
        writer.execute("ROLLBACK")

    def test_one_slow_query_does_not_slow_the_next_reader(self):
        slow = threading.Thread(target=self.attempt, args=(self.RUNAWAY,),
                                kwargs={"timeout": 1.5})
        slow.start()
        try:
            time.sleep(0.2)
            started = time.monotonic()
            self.assertEqual(self.read("SELECT count(*) FROM sessions")[1], [(2,)])
            self.assertLess(time.monotonic() - started, 1.0)
        finally:
            slow.join()

    def test_deep_nesting_in_a_thread_with_a_small_stack(self):
        # The server runs every request in a thread. A statement that nests thousands deep
        # must end in an error message, not in a crash of the process, on every SQLite
        # version (from 3.46 SQLite's parser no longer stops deep nesting itself: WIN-01).
        # Sub-queries nest without brackets too: a chain of WITH parts, each reading the one
        # before (SEC2-05; 700 parts ended the process in this thread), and such a chain whose
        # parts read the one before behind long chains of operators, which SQLite walks before
        # it checks their depth (on SQLite 3.50, 20 parts behind 450 NOTs ended the process
        # here). SEC2-05, second round: parts that put the operators on the column of the one
        # before become one expression as deep as all of them (on 3.50, 150 parts of 495 '~'
        # ended the process even with the 32 MB worker), so the text is at most
        # READER_SQL_CHARS long, whatever max_sql_chars says. Every text below but the last
        # stays under that cap, so SQLite itself meets them.
        results = []
        limit = 4 * self.settings.sql.max_sql_chars
        within, beyond = _flat_chain(57, "~" * 495), _flat_chain(150, "~" * 495)
        self.assertLess(len(within), reader_sqlite.READER_SQL_CHARS)
        self.assertGreater(len(beyond), reader_sqlite.READER_SQL_CHARS)

        def work():
            for sql in ("SELECT " + "(" * 14000 + "1" + ")" * 14000,
                        "SELECT " + "(SELECT " * 3000 + "1" + ")" * 3000,
                        "SELECT * FROM " + "(SELECT * FROM " * 1500 + "(SELECT 1)" + ")" * 1500,
                        "SELECT " + "NOT " * 7000 + "1",
                        "SELECT " + " + ".join(["1"] * 7000),
                        "SELECT json_valid('" + "[" * 29000 + "')",
                        _chain(700),
                        _chain(15, "NOT " * 450),
                        _chain(55, "~" * 495),
                        _chain(reader_sqlite.SELECT_PARTS - 1),
                        within, beyond):
                assert len(sql) < limit     # shorter than the SQL length limit: SQLite parses it
                results.append(self.attempt(sql)[0])

        previous = threading.stack_size(256 * 1024)
        try:
            thread = threading.Thread(target=work)
            thread.start()
            thread.join()
        finally:
            threading.stack_size(previous)
        # 57 parts (29,000 characters) run on 3.50 (slowly: the time limit may end them);
        # SQLite 3.45 stops them in its parser. What matters is that the process lives.
        self.assertIn(results[10], (OK, ERROR, TIMED_OUT))
        self.assertEqual(results[:10] + results[11:], [REFUSED, REFUSED, REFUSED, ERROR, ERROR,
                                                       OK, REFUSED, ERROR, ERROR, OK, REFUSED])

    def test_the_text_is_short_whatever_max_sql_chars_says(self):
        # SEC2-05, second round: a project may raise sql.max_sql_chars to 10,000,000; the
        # depth SQLite walks unchecked grows with the text, so the reader keeps its own cap.
        with support.TempProject("sqlite", {"sql.max_sql_chars": 1000000}) as project:
            store = open_store(project.settings)
            store.initialize()
            cap = reader_sqlite.READER_SQL_CHARS
            for sql in ("SELECT '" + "x" * cap + "'", _flat_chain(150, "~" * 495)):
                with self.assertRaises(PclError) as caught:
                    sqlrun.run_sql(store, sql, settings=project.settings)
                self.assertEqual(caught.exception.code, "sql_refused")
                self.assertIn("expert mode on SQLite takes at most %d" % cap,
                              caught.exception.message)
            short = "SELECT '" + "x" * (cap - 10) + "'"
            self.assertEqual(sqlrun.run_sql(store, short, settings=project.settings)["row_count"],
                             1)

    def test_brackets_up_to_the_limit_are_left_to_sqlite(self):
        depth = reader_sqlite.BRACKET_DEPTH
        at_limit = "SELECT " + "(" * depth + "1" + ")" * depth
        self.assertEqual(reader_sqlite.bracket_depth(at_limit), depth)
        # SQLite 3.45 itself stops at 94 plain brackets ("parser stack overflow"), 3.46 runs it
        self.assertIn(self.attempt(at_limit)[0], (OK, ERROR))
        self.assertEqual(self.attempt("SELECT (" + at_limit[7:] + ")")[0], REFUSED)
        nested = "SELECT * FROM " + "(SELECT * FROM " * 9 + "(SELECT 1)" + ")" * 9
        self.assertEqual(self.attempt(nested)[0], OK)
        self.assertEqual(self.attempt("SELECT '" + "(" * 5000 + "' AS text")[0], OK)
        self.assertEqual(reader_sqlite.bracket_depth(
            "SELECT '((', \"((\", `((`, [((], (1) -- ((\n/* (( */"), 1)

    def test_select_parts_up_to_the_limit_are_left_to_sqlite(self):
        at_limit = _chain(reader_sqlite.SELECT_PARTS - 1)
        self.assertEqual(reader_sqlite.select_parts(at_limit), reader_sqlite.SELECT_PARTS)
        self.assertEqual(self.attempt(at_limit)[0], OK)
        outcome, text = self.attempt(_chain(reader_sqlite.SELECT_PARTS))
        self.assertEqual(outcome, REFUSED)
        self.assertIn("SELECT parts", text)
        self.assertNotIn("WITH part can help", text)
        self.assertEqual(reader_sqlite.select_parts(
            "SELECT 'select', \"select\", `select`, [select], selected, select_x, x_select, "
            "(select 1) -- select\n/* SELECT */ SeLeCt"), 3)


class TestATamperedCatalogue(ReaderBase, unittest.TestCase):
    """The catalogue file is written by pcldb itself. Should someone else change it, what it
    holds is still judged by the authorizer when it is read."""

    def _owner(self):
        connection = sqlite3.connect(self.store.catalog_path, isolation_level=None)
        self.addCleanup(connection.close)
        return connection

    def test_a_view_cannot_smuggle_a_function_or_a_table(self):
        owner = self._owner()
        owner.execute("CREATE VIEW bad_function AS SELECT sqlite_source_id() AS x")
        owner.execute("CREATE VIEW bad_table AS SELECT * FROM pragma_database_list")
        owner.close()
        self.assertEqual(self.attempt("SELECT * FROM bad_function")[0], REFUSED)
        # Stopped twice over: the authorizer refuses PRAGMA, and with trusted_schema off
        # SQLite itself refuses such a table inside a view ("unsafe use of virtual table").
        outcome, text = self.attempt("SELECT * FROM bad_table")
        self.assertIn(outcome, (REFUSED, ERROR))
        self.assertNotIn("pcl_catalog", text)
        self.assertEqual(self.read("SELECT count(*) FROM sessions")[1], [(2,)])

    def test_statistics_and_virtual_tables_stay_closed(self):
        owner = self._owner()
        owner.execute("ANALYZE")                                    # makes sqlite_stat1
        owner.execute("CREATE VIRTUAL TABLE notes_index USING fts5(body)")
        owner.execute("INSERT INTO notes_index VALUES ('hello')")
        owner.close()
        self.assertEqual(self.attempt("SELECT * FROM sqlite_stat1")[0], REFUSED)
        self.assertEqual(self.attempt("SELECT * FROM notes_index")[0], REFUSED)
        self.assertEqual(self.attempt("SELECT count(*) FROM notes_index_content")[0], OK)


if __name__ == "__main__":
    unittest.main()
