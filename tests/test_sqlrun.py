"""Expert mode from the caller's side (pcldb/sqlrun.py, specification section 9): the courtesy
checks, the conversion of every value to JSON, the result shape, the audit rows, the schema
browser's data and the command line. The engine-neutral tests run on both engines."""

from __future__ import annotations

import argparse
import contextlib
import datetime
import decimal
import io
import json
import unittest
import uuid

from pcldb import sqlrun
from pcldb.errors import PclError
from pcldb.store import open_store, schema_def
from tests import support

MARKER = "PRIVATE-MARKER-5e1f9c"


class TestLeadingWord(unittest.TestCase):
    def test_both_engines(self):
        for sql, word in (
                ("select 1", "SELECT"),
                ("  \t\r\n\f\vSeLeCt 1", "SELECT"),
                ("-- a comment\nWITH x AS (SELECT 1) SELECT * FROM x", "WITH"),
                ("/* one */ /* two */ values (1)", "VALUES"),
                ("/* a */ -- b\n /* c */ EXPLAIN select 1", "EXPLAIN"),
                ("explain(analyze) select 1", "EXPLAIN"),
                ("EXPLAINX select 1", "EXPLAINX"),
                ("select_1", "SELECT_1"),
                ("", ""),
                ("   ", ""),
                ("-- only a comment", ""),
                ("/* never closed select 1", ""),
                ("(select 1)", ""),
                (";select 1", ""),
                ("1select", ""),
                ("\"select\" 1", ""),
                ("sélect 1", "S"),
                ("﻿select 1", "")):
            for engine in ("sqlite", "postgresql"):
                self.assertEqual(sqlrun.leading_word(sql, engine), word, (sql, engine))

    def test_where_the_engines_differ(self):
        # PostgreSQL's block comments nest; SQLite's end at the first */.
        sql = "/* outer /* inner */ EXPLAIN */ DELETE FROM x"
        self.assertEqual(sqlrun.leading_word(sql, "postgresql"), "DELETE")
        self.assertEqual(sqlrun.leading_word(sql, "sqlite"), "EXPLAIN")
        # PostgreSQL ends a -- comment at a carriage return too.
        sql = "-- note\rSELECT 1"
        self.assertEqual(sqlrun.leading_word(sql, "postgresql"), "SELECT")
        self.assertEqual(sqlrun.leading_word(sql, "sqlite"), "")


class TestCourtesyChecks(unittest.TestCase):
    def setUp(self):
        self.project = support.TempProject("sqlite", {"sql.max_sql_chars": 200})
        self.addCleanup(self.project.cleanup)
        self.settings = self.project.settings

    def refusal(self, sql, engine="sqlite"):
        with self.assertRaises(PclError) as caught:
            sqlrun.check_sql_text(sql, self.settings, engine)
        self.assertEqual(caught.exception.code, "sql_refused")
        self.assertEqual(caught.exception.status, 400)
        return caught.exception.message

    def test_what_passes(self):
        for sql in ("SELECT 1", " select 1;", "-- why\nWITH x AS (SELECT 1) SELECT * FROM x",
                    "VALUES (1)", "EXPLAIN SELECT 1", "explain query plan select 1"):
            sqlrun.check_sql_text(sql, self.settings, "sqlite")
            sqlrun.check_sql_text(sql, self.settings, "postgresql")
        sqlrun.check_sql_text("TABLE sessions", self.settings, "postgresql")

    def test_what_is_refused_and_how_it_is_said(self):
        self.assertIn("no SQL", self.refusal(""))
        self.assertIn("no SQL", self.refusal("  \n\t "))
        self.assertIn("must be text", self.refusal(None))
        self.assertIn("must be text", self.refusal(b"SELECT 1"))
        self.assertIn("201 characters long; expert mode takes at most 200",
                      self.refusal("SELECT 1" + " " * 193))
        self.assertIn("NUL", self.refusal("SELECT 1\x00"))
        self.assertIn("broken character", self.refusal("SELECT '\ud800'"))
        message = self.refusal("DELETE FROM sessions")
        self.assertIn("SELECT, WITH, VALUES or EXPLAIN", message)
        self.assertIn("starts with DELETE", message)
        self.assertIn("SELECT, WITH, VALUES, TABLE or EXPLAIN",
                      self.refusal("drop table x", "postgresql"))
        self.assertIn("must start with", self.refusal("TABLE sessions"))     # SQLite has none
        self.assertIn("must start with", self.refusal("-- only a comment"))
        self.assertIn("must start with", self.refusal("(SELECT 1)"))
        self.assertIn("must start with", self.refusal("PRAGMA table_info(sessions)"))


class TestJsonValue(unittest.TestCase):
    """The conversion table of specification section 9, value by value."""

    def convert(self, value, limit=20):
        converted = sqlrun.json_value(value, limit)
        json.dumps(converted, allow_nan=False).encode("utf-8")      # always sendable
        return converted

    def test_values_json_knows(self):
        for value in (None, True, False, 0, -7, 2 ** 53, -(2 ** 53), 1.5, -0.25, "", "text",
                      "smørrebrød ☃"):
            converted = self.convert(value)
            self.assertEqual(converted, value)
            self.assertIs(type(converted), type(value))

    def test_numbers_json_cannot_carry(self):
        self.assertEqual(self.convert(2 ** 53 + 1), "9007199254740993")
        self.assertEqual(self.convert(-(2 ** 53) - 1), "-9007199254740993")
        self.assertEqual(self.convert(2 ** 63 - 1), "9223372036854775807")
        self.assertEqual(self.convert(10 ** 40), "1" + "0" * 40)
        self.assertEqual(self.convert(float("nan")), "NaN")
        self.assertEqual(self.convert(float("inf")), "Infinity")
        self.assertEqual(self.convert(float("-inf")), "-Infinity")
        self.assertEqual(self.convert(decimal.Decimal("123.4500")), "123.4500")
        self.assertEqual(self.convert(decimal.Decimal("NaN")), "NaN")
        self.assertEqual(self.convert(decimal.Decimal("-Infinity")), "-Infinity")
        self.assertEqual(self.convert(decimal.Decimal("12345678901234567890.123456789"), 99),
                         "12345678901234567890.123456789")

    def test_dates_times_ids_and_bytes(self):
        utc = datetime.timezone.utc
        self.assertEqual(self.convert(datetime.date(2026, 11, 17)), "2026-11-17")
        self.assertEqual(self.convert(datetime.time(3, 50, 15, 281000)), "03:50:15.281000")
        self.assertEqual(self.convert(datetime.datetime(2026, 11, 17, 3, 50, 15), 99),
                         "2026-11-17T03:50:15")
        self.assertEqual(
            self.convert(datetime.datetime(2026, 11, 17, 3, 50, 15, 281000, tzinfo=utc), 99),
            "2026-11-17T03:50:15.281000+00:00")
        self.assertEqual(self.convert(datetime.timedelta(days=3, seconds=4)), "3 days, 0:00:04")
        self.assertEqual(self.convert(uuid.UUID("8a6e0804-2bd0-4672-b79d-d97027f9071a"), 99),
                         "8a6e0804-2bd0-4672-b79d-d97027f9071a")
        self.assertEqual(self.convert(b"\xde\xad\xbe\xef"), "\\xdeadbeef")
        self.assertEqual(self.convert(bytearray(b"\x00\x01")), "\\x0001")
        self.assertEqual(self.convert(memoryview(b"\xff")), "\\xff")
        self.assertEqual(self.convert(b""), "\\x")

    def test_lists_and_documents(self):
        self.assertEqual(self.convert([1, "a", None, [2.5, True]], 99),
                         [1, "a", None, [2.5, True]])
        self.assertEqual(self.convert((1, 2)), [1, 2])
        self.assertEqual(self.convert({"k": [1, {"n": None}]}, 99), {"k": [1, {"n": None}]})
        self.assertEqual(
            self.convert({"big": 2 ** 60, "nan": float("nan"), "inf": float("inf"),
                          "d": decimal.Decimal("1.50"), "b": b"\x01",
                          "t": datetime.date(2026, 1, 2), 5: "five"}, 999),
            {"big": str(2 ** 60), "nan": "NaN", "inf": "Infinity", "d": "1.50", "b": "\\x01",
             "t": "2026-01-02", "5": "five"})

    def test_long_cells_are_cut_and_marked(self):
        self.assertEqual(self.convert("x" * 20), "x" * 20)
        self.assertEqual(self.convert("x" * 21), "x" * 20 + "... [cut: 21 characters in all]")
        self.assertEqual(self.convert(b"\xab" * 9), "\\x" + "ab" * 9)
        self.assertEqual(self.convert(b"\xab" * 10),
                         "\\x" + "ab" * 9 + "... [cut: 10 bytes in all]")
        # A list or document that would be longer than a cell becomes its cut JSON text.
        self.assertEqual(self.convert(list(range(30))),
                         "[0,1,2,3,4,5,6,7,8,9... [cut: 81 characters in all]")
        self.assertEqual(self.convert({"k": "v" * 30}),
                         '{"k":"vvvvvvvvvvvvvv... [cut: 38 characters in all]')
        self.assertEqual(self.convert(sqlrun.CutText("y" * 25)),
                         "y" * 20 + "... [cut: the value is longer]")
        self.assertEqual(self.convert(sqlrun.CutText("short")),
                         "short... [cut: the value is longer]")

    def test_text_that_cannot_be_sent(self):
        self.assertEqual(self.convert("a\ud800b"), "a?b")
        self.assertEqual(self.convert(["\udfff"]), ["?"])
        self.assertEqual(self.convert({"\ud800": 1}), {"?": 1})
        self.assertEqual(self.convert("nul\x00inside"), "nul\x00inside")

    def test_anything_else_becomes_its_text(self):
        class Odd:
            def __str__(self):
                return "odd thing"

        class Broken:
            def __str__(self):
                raise RuntimeError("no text")

        self.assertEqual(self.convert(Odd()), "odd thing")
        self.assertEqual(self.convert(Broken()), "<Broken>")
        self.assertEqual(self.convert(1 + 2j), "(1+2j)")
        deep = []
        for _level in range(5000):
            deep = [deep]
        self.assertIsInstance(self.convert(deep), str)      # no crash on absurd nesting

    def test_deep_nesting_gives_the_same_answer_on_every_python(self):
        def nest(levels, inner, kind="list"):
            value = inner
            for _level in range(levels):
                value = [value] if kind == "list" else {"k": value}
            return value

        def depth_and_inside(value):
            levels = 0
            while isinstance(value, (list, dict)):
                value = value[0] if isinstance(value, list) else value["k"]
                levels += 1
            return levels, value

        big = 1_000_000
        # 150 levels (and 200, the limit) come back whole, on 3.11 too.
        for levels in (150, 200):
            for kind in ("list", "dict"):
                converted = self.convert(nest(levels, 7, kind), big)
                self.assertIsInstance(converted, list if kind == "list" else dict)
                self.assertEqual(depth_and_inside(converted), (levels, 7), (levels, kind))
        # Deeper: the first 200 levels, then a short note instead of the rest (not a crash, not
        # a text whose form depends on the Python version).
        for levels in (201, 5000):
            for kind in ("list", "dict"):
                converted = self.convert(nest(levels, 7, kind), big)
                self.assertEqual(depth_and_inside(converted),
                                 (200, "[nested too deeply to show]"), (levels, kind))
        loop = []
        loop.append(loop)                                   # a list that holds itself
        self.assertEqual(depth_and_inside(self.convert(loop, big)),
                         (200, "[nested too deeply to show]"))

    def test_a_key_whose_text_fails_is_named_by_its_kind(self):
        class Endless:
            def __str__(self):
                raise RecursionError("maximum recursion depth exceeded")
            __repr__ = __str__

            def __hash__(self):
                return 1

        self.assertEqual(self.convert({Endless(): 1}), {"<Endless>": 1})


class TestFormats(unittest.TestCase):
    RESULT = {"columns": [{"name": "folder", "type": "text"}, {"name": "n", "type": "integer"},
                          {"name": "doc", "type": "jsonb"}, {"name": "ok", "type": "boolean"}],
              "rows": [["s1", 12, {"a": [1, 2]}, True], ["line\nbreak, \"q\" é", None, None,
                                                         False]],
              "row_count": 2, "truncated": False, "elapsed_ms": 3}

    def test_the_table_is_aligned_and_ascii(self):
        text = sqlrun.format_table(self.RESULT)
        self.assertTrue(text.isascii())
        lines = text.splitlines()
        self.assertEqual(lines[0].split(), ["folder", "n", "doc", "ok"])
        self.assertEqual(set(lines[1].replace(" ", "")), {"-"})
        self.assertIn('{"a":[1,2]}', lines[2])
        self.assertIn("true", lines[2])
        self.assertIn("line\\nbreak", lines[3])
        self.assertIn("\\xe9", lines[3])
        self.assertIn("NULL", lines[3])
        self.assertEqual(lines[2].index("12") + 2, lines[3].index("NULL") + 4)   # numbers right
        self.assertEqual(lines[-1], "(2 rows)")
        cut = dict(self.RESULT, truncated=True, row_count=1, rows=self.RESULT["rows"][:1])
        self.assertEqual(sqlrun.format_table(cut).splitlines()[-1],
                         "(the first 1 row; the query has more)")
        empty = {"columns": [], "rows": [], "row_count": 0, "truncated": False}
        self.assertEqual(sqlrun.format_table(empty), "(0 rows)\n")

    def test_csv(self):
        text = sqlrun.format_csv(self.RESULT)
        self.assertEqual(text.splitlines()[0], "folder,n,doc,ok")
        self.assertEqual(text.splitlines()[1], 's1,12,"{""a"":[1,2]}",true')
        self.assertIn('"line\nbreak, ""q"" é",,,false', text)


class SqlRunCases:
    """run_sql, describe, history and the command line; on both engines."""

    def setUp(self):
        super().setUp()
        with self.store.app() as db:
            self.keys = support.seed_minimal(db)
            db.insert("app.query_log", {"log_id": "l-marker", "at_utc": "2026-11-19T02:00:00.000Z",
                                        "user_name": "someone-else", "mode": "sql",
                                        "query_text": "select '%s'" % MARKER, "ok": True})

    def run_sql(self, sql, **arguments):
        return sqlrun.run_sql(self.store, sql, settings=self.settings, **arguments)

    def log_rows(self, user_name=None):
        with self.store.app() as db:
            if user_name is None:
                return db.query("SELECT * FROM {app}.query_log WHERE user_name IS NULL "
                                "ORDER BY at_utc, log_id")
            return db.query("SELECT * FROM {app}.query_log WHERE user_name = ? "
                            "ORDER BY at_utc, log_id", [user_name])

    def test_the_result_shape(self):
        result = self.run_sql("SELECT folder, bytes FROM sessions ORDER BY folder",
                              user_name="demo-operator-a")
        self.assertEqual(sorted(result), ["columns", "elapsed_ms", "row_count", "rows",
                                          "truncated"])
        self.assertEqual([column["name"] for column in result["columns"]], ["folder", "bytes"])
        self.assertTrue(all(set(column) == {"name", "type"} for column in result["columns"]))
        self.assertEqual(result["rows"], [["1d_y_scan_20261115_110940_537", 121843],
                                          ["1d_y_scan_20261116_225015_281", 40841]])
        self.assertEqual((result["row_count"], result["truncated"]), (2, False))
        self.assertIsInstance(result["elapsed_ms"], int)
        json.dumps(result, allow_nan=False)

    def test_max_rows_and_truncated(self):
        result = self.run_sql("SELECT rel_path FROM files ORDER BY 1", max_rows=3)
        self.assertEqual((result["row_count"], result["truncated"]), (3, True))
        result = self.run_sql("SELECT rel_path FROM files ORDER BY 1", max_rows=8)
        self.assertEqual((result["row_count"], result["truncated"]), (8, False))
        small = self.settings.changed({"sql.max_rows": 2})
        result = sqlrun.run_sql(self.store, "SELECT rel_path FROM files", settings=small)
        self.assertEqual((result["row_count"], result["truncated"]), (2, True))
        # A caller cannot ask for more than the setting allows.
        result = sqlrun.run_sql(self.store, "SELECT rel_path FROM files", settings=small,
                                max_rows=500)
        self.assertEqual(result["row_count"], 2)
        for bad in (0, -1, True, "5", 2.5):
            with self.assertRaises(PclError) as caught:
                self.run_sql("SELECT 1", max_rows=bad)
            self.assertEqual(caught.exception.code, "bad_request")

    def test_one_audit_row_for_a_success(self):
        self.run_sql("SELECT count(*) FROM files", user_name="demo-operator-a", max_rows=10)
        rows = self.log_rows("demo-operator-a")
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual((row["mode"], row["query_text"], row["ok"], row["error_text"],
                          row["n_rows"], row["backend"], row["request_json"]),
                         ("sql", "SELECT count(*) FROM files", True, None, 1, self.engine,
                          {"max_rows": 10}))
        self.assertEqual(len(row["log_id"]), 32)
        self.assertEqual(len(row["at_utc"]), 24)
        self.assertGreaterEqual(row["elapsed_ms"], 0)

    def test_the_private_part_is_the_same_refusal_on_both_engines(self):
        # K-04: sql_error "no such table" on SQLite, sql_refused on PostgreSQL before.
        for sql in ("select * from app.users", "SELECT name FROM app.tokens",
                    "SELECT s.path FROM sessions s JOIN app.query_log q ON 1 = 1"):
            with self.subTest(sql=sql):
                with self.assertRaises(PclError) as caught:
                    self.run_sql(sql)
                self.assertEqual(caught.exception.code, "sql_refused")
                self.assertEqual(caught.exception.message, sqlrun.PRIVATE_PART)

    def test_a_parameter_mark_is_the_same_refusal_on_both_engines(self):
        # SEC-04: on PostgreSQL "$1" used to say that the database connection was lost.
        for sql in ("SELECT path FROM sessions WHERE instrument = $1", "SELECT $1",
                    "EXPLAIN SELECT $1"):
            with self.subTest(sql=sql):
                with self.assertRaises(PclError) as caught:
                    self.run_sql(sql)
                self.assertEqual(caught.exception.code, "sql_refused")
                self.assertEqual(caught.exception.status, 400)
                self.assertIn(sqlrun.PARAMETER_MARK, caught.exception.message)
        self.assertEqual(self.run_sql("SELECT 1")["rows"], [[1]])

    def test_one_audit_row_for_every_kind_of_failure(self):
        cases = (
            ("DELETE FROM sessions", "sql_refused"),                 # the courtesy check
            ("SELECT 1; SELECT 2", "sql_refused"),                   # one statement only
            ("SELECT no_such_column FROM sessions", "sql_error"),    # the database's own message
            ("WITH x AS (SELECT 1) INSERT INTO meta SELECT 'a', 'b' FROM x"
             if self.engine == "sqlite" else
             "WITH x AS (INSERT INTO pcl.meta VALUES ('a', 'b') RETURNING 1) SELECT * FROM x",
             "sql_refused"),                                         # past the courtesy check
            ("", "sql_refused"),
        )
        for sql, code in cases:
            with self.assertRaises(PclError) as caught:
                self.run_sql(sql, user_name="demo-reviewer")
            self.assertEqual(caught.exception.code, code, sql)
        rows = self.log_rows("demo-reviewer")
        self.assertEqual([row["query_text"] for row in rows], [sql for sql, _code in cases])
        for row, (_sql, code) in zip(rows, cases):
            self.assertIs(row["ok"], False)
            self.assertIsNone(row["n_rows"])
            self.assertTrue(row["error_text"].startswith(code + ": "), row["error_text"])
            self.assertEqual(row["backend"], self.engine)
        # Nothing was written by any of them.
        self.assertEqual(self.outside_rows("SELECT COUNT(*) FROM {pcl}.sessions"), [(2,)])
        self.assertEqual(self.outside_rows(
            "SELECT COUNT(*) FROM {pcl}.meta WHERE key = 'a'"), [(0,)])

    def test_a_timeout_is_logged_too(self):
        quick = self.settings.changed({"sql.timeout_seconds": 0.5})
        if self.engine == "sqlite":
            sql = ("WITH RECURSIVE r(n) AS (SELECT 1 UNION ALL SELECT n + 1 FROM r) "
                   "SELECT count(*) FROM r")
        else:
            sql = "SELECT count(*) FROM pg_class a, pg_class b, pg_class c, pg_class d"
        with self.assertRaises(PclError) as caught:
            sqlrun.run_sql(self.store, sql, settings=quick, user_name="demo-operator-b")
        self.assertEqual(caught.exception.code, "sql_timeout")
        self.assertIn("0.5 seconds", caught.exception.message)
        rows = self.log_rows("demo-operator-b")
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["error_text"].startswith("sql_timeout: "))
        self.assertGreaterEqual(rows[0]["elapsed_ms"], 450)

    def test_text_the_audit_table_cannot_hold_is_made_storable(self):
        long_sql = "SELECT 1 -- " + "x" * 30000
        cases = (("SELECT 1\x00; DROP TABLE x", "SELECT 1\\x00; DROP TABLE x"),
                 ("SELECT '\ud800'", "SELECT '?'"),
                 (long_sql, long_sql[:20000] + "... [cut: 30012 characters in all]"),
                 (None, "None"), (b"SELECT 1", "b'SELECT 1'"))
        for sql, _stored in cases:
            with self.assertRaises(PclError) as caught:
                self.run_sql(sql)
            self.assertEqual(caught.exception.code, "sql_refused")
        # (sorted: rows written within the same millisecond have no order of their own)
        self.assertEqual(sorted(row["query_text"] for row in self.log_rows()),
                         sorted(stored for _sql, stored in cases))

    def test_the_audit_rows_go_through_the_app_connection_not_the_reader(self):
        self.run_sql("SELECT 1", user_name="demo-operator-a")
        with self.assertRaises(PclError):
            self.run_sql("SELECT * FROM %s" % ("query_log" if self.engine == "sqlite"
                                               else "app.query_log"),
                         user_name="demo-operator-a")
        with self.assertRaises(PclError) as caught:
            self.run_sql("SELECT query_text FROM %s" % (
                "main.query_log" if self.engine == "sqlite" else "app.query_log"))
        self.assertNotIn(MARKER, caught.exception.message)
        self.assertEqual(len(self.log_rows("demo-operator-a")), 2)

    def test_no_result_without_its_audit_row(self):
        store = open_store(self.settings)
        real_app = store.app

        @contextlib.contextmanager
        def failing_app():
            raise PclError("the audit table cannot be written", code="db_busy", status=503)
            yield       # pragma: no cover

        store.app = store.app_only = failing_app
        with self.assertRaises(PclError) as caught:
            sqlrun.run_sql(store, "SELECT folder FROM sessions", settings=self.settings)
        self.assertEqual(caught.exception.code, "db_busy")
        # A failure of the audit row does not hide the first error of a refused statement.
        with self.assertRaises(PclError) as caught:
            sqlrun.run_sql(store, "DELETE FROM sessions", settings=self.settings)
        self.assertEqual(caught.exception.code, "sql_refused")
        store.app = real_app
        del store.app_only
        self.assertEqual(self.log_rows(), [])

    def test_only_a_few_expert_queries_run_at_once(self):
        import threading
        originals = (sqlrun._slots, sqlrun.SLOT_WAIT_SECONDS)
        self.addCleanup(lambda: (setattr(sqlrun, "_slots", originals[0]),
                                 setattr(sqlrun, "SLOT_WAIT_SECONDS", originals[1])))
        sqlrun._slots = threading.BoundedSemaphore(1)
        sqlrun.SLOT_WAIT_SECONDS = 0.2
        self.assertEqual(self.run_sql("SELECT 1")["rows"], [[1]])
        sqlrun._slots.acquire()                 # as if another person's query were running
        with self.assertRaises(PclError) as caught:
            self.run_sql("SELECT 1", user_name="demo-operator-b")
        self.assertEqual((caught.exception.code, caught.exception.status), ("sql_busy", 503))
        sqlrun._slots.release()
        self.assertEqual(self.run_sql("SELECT 2")["rows"], [[2]])       # the place came back
        with self.assertRaises(PclError):
            self.run_sql("SELECT nope")
        self.assertEqual(self.run_sql("SELECT 3")["rows"], [[3]])       # also after a failure
        self.assertTrue(self.log_rows("demo-operator-b")[0]["error_text"].startswith("sql_busy"))

    def test_history_is_per_person_and_newest_first(self):
        for number in range(3):
            self.run_sql("SELECT %d" % number, user_name="demo-operator-a")
        with self.assertRaises(PclError):
            self.run_sql("SELECT nope", user_name="demo-operator-a")
        self.run_sql("SELECT 'other'", user_name="demo-operator-b")
        self.run_sql("SELECT 'local'")
        with self.store.app() as db:
            mine = sqlrun.history(db, "demo-operator-a")
            two = sqlrun.history(db, "demo-operator-a", limit=2)
            local = sqlrun.history(db, None)
        self.assertEqual([item["query_text"] for item in mine],
                         ["SELECT nope", "SELECT 2", "SELECT 1", "SELECT 0"])
        self.assertEqual(sorted(mine[0]), ["at_utc", "elapsed_ms", "error_text", "n_rows", "ok",
                                           "query_text"])
        self.assertEqual([item["ok"] for item in mine], [False, True, True, True])
        self.assertEqual(len(two), 2)
        self.assertEqual([item["query_text"] for item in local], ["SELECT 'local'"])

    def test_describe_lists_the_catalogue_with_the_engines_types(self):
        described = sqlrun.describe(self.store)
        self.assertEqual((described["engine"], described["schema"]), (self.engine, "pcl"))
        names = [item["name"] for item in described["objects"]]
        self.assertEqual(names, [item.name for item in schema_def.relations("pcl")])
        for private in ("users", "tokens", "query_log", "registrations", "search_entries"):
            self.assertNotIn(private, names)
        kinds = {item["name"]: item["kind"] for item in described["objects"]}
        self.assertEqual((kinds["sessions"], kinds["session_summary"]), ("table", "view"))
        sessions = next(item for item in described["objects"] if item["name"] == "sessions")
        columns = {column["name"]: column for column in sessions["columns"]}
        self.assertEqual(sorted(columns["path"]), ["description", "name", "nullable", "type"])
        self.assertTrue(sessions["description"] and columns["path"]["description"])
        self.assertIs(columns["path"]["nullable"], False)
        self.assertIs(columns["notes"]["nullable"], True)
        expected = {"sqlite": ("TEXT", "INTEGER (0 or 1)", "TEXT (JSON list)", "TEXT (UTC time)"),
                    "postgresql": ("text", "boolean", "text[]", "timestamp with time zone")}
        self.assertEqual(tuple(columns[name]["type"] for name in
                               ("path", "simulated", "sample_ids", "started_utc")),
                         expected[self.engine])
        json.dumps(described)
        # Every object it names can really be read through the sandbox.
        for name in names:
            self.assertEqual(self.run_sql("SELECT count(*) FROM %s" % name)["row_count"], 1)

    def test_the_self_test_passes(self):
        report = sqlrun.sandbox_self_test(self.store)
        self.assertEqual(len(report), 6)
        self.assertEqual([item["must_run"] for item in report], [True] + [False] * 5)
        self.assertTrue(all(item["ok"] for item in report), report)
        self.assertEqual(report[0]["outcome"], "ran")
        self.assertEqual(self.log_rows(), [])       # it leaves no audit rows

    def command(self, sql, **flags):
        arguments = argparse.Namespace(sql=sql, as_csv=False, as_json=False, max_rows=None)
        for name, value in flags.items():
            setattr(arguments, name, value)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sqlrun.cmd_sql(arguments, self.settings)
        return code, out.getvalue(), err.getvalue()

    def test_the_command_prints_a_table_csv_or_json(self):
        sql = "SELECT folder, n_files FROM sessions ORDER BY folder"
        code, out, err = self.command(sql)
        self.assertEqual((code, err), (0, ""))
        lines = out.splitlines()
        self.assertEqual(lines[0].split(), ["folder", "n_files"])
        self.assertEqual(lines[2].split(), ["1d_y_scan_20261115_110940_537", "5"])
        self.assertEqual(lines[-1], "(2 rows)")
        self.assertTrue(out.isascii())
        code, out, err = self.command(sql, as_csv=True)
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(out, "folder,n_files\n1d_y_scan_20261115_110940_537,5\n"
                              "1d_y_scan_20261116_225015_281,3\n")
        code, out, err = self.command(sql, as_json=True, max_rows=1)
        self.assertEqual(code, 0)
        loaded = json.loads(out)
        self.assertEqual((loaded["rows"], loaded["truncated"]),
                         ([["1d_y_scan_20261115_110940_537", 5]], True))
        code, out, err = self.command(sql, as_csv=True, max_rows=1)
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("Only the first 1 rows", err)
        with self.assertRaises(PclError):
            self.command(sql, as_csv=True, as_json=True)
        with self.assertRaises(PclError) as caught:
            self.command("DROP TABLE sessions")
        self.assertEqual(caught.exception.code, "sql_refused")
        # The command line's queries are audited as nobody's (the local terminal).
        self.assertEqual(len(self.log_rows()), 5)


class TestCsvToAFile(unittest.TestCase):
    """WIN-03: on Windows a redirected stdout uses the ANSI code page (cp1252); --csv must still
    write UTF-8, the same bytes as on a Mac."""

    def test_csv_into_a_file_is_utf8_whatever_the_code_page(self):
        text = "notes\n\u03bb=785 nm, \u03b1-\u03b2 \u2192 \u4e2d\u6587 \u22655 \u00b5m\n"
        raw = io.BytesIO()
        stream = io.TextIOWrapper(raw, encoding="cp1252", newline="\r\n")
        sqlrun._write_data(stream, text)
        stream.flush()
        self.assertEqual(raw.getvalue(), text.encode("utf-8"))

    def test_a_console_still_gets_escapes_it_can_show(self):
        stream = io.StringIO()
        sqlrun._write_data(stream, "\u03bb\n")           # no .buffer: written as text
        self.assertEqual(stream.getvalue(), "\u03bb\n")


class TestSqlRunSqlite(SqlRunCases, support.SqliteCase, unittest.TestCase):
    def test_every_kind_of_value_becomes_json(self):
        result = self.run_sql(
            "SELECT 1 AS i, 9007199254740993 AS big, -9223372036854775807 - 1 AS small, "
            "1.5 AS r, 9e999 AS inf, -9e999 AS ninf, 'text' AS t, x'deadbeef' AS raw, "
            "NULL AS empty, printf('%.*c', 30000, 'x') AS long_text, "
            "zeroblob(20000) AS long_blob, sample_ids, card_json, simulated, started_utc, "
            "json_extract(card_json, '$.simulated') AS flag "
            "FROM sessions ORDER BY path LIMIT 1")
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["rows"], [[
            1, "9007199254740993", "-9223372036854775808", 1.5, "Infinity", "-Infinity", "text",
            "\\xdeadbeef", None, "x" * 10000 + "... [cut: 30000 characters in all]",
            "\\x" + "00" * 4999 + "... [cut: 20000 bytes in all]", '["S-100"]',
            '{"schema":"pcl-session/1","session":"1d_y_scan_20261115_110940_537",'
            '"simulated":false}', 0, "2026-11-15T16:09:40.537Z", 0]])
        self.assertEqual([column["type"] for column in result["columns"]],
                         ["integer", "integer", "integer", "real", "real", "real", "text",
                          "blob", "", "text", "blob", "text", "text", "integer", "text",
                          "integer"])


class TestSqlRunPostgres(SqlRunCases, support.PostgresCase, unittest.TestCase):
    QUERY = """
        SELECT ARRAY[1, 2, NULL]::int[] AS ints, ARRAY['a', 'b'] AS texts,
               '{"k": [1, 2.5, null], "big": 12345678901234567890}'::jsonb AS doc,
               '{"n": 1e400}'::json AS huge,
               123.4500::numeric AS num, 'NaN'::numeric AS num_nan,
               'NaN'::float8 AS f_nan, 'Infinity'::float8 AS f_inf, '-Infinity'::float4 AS f_ninf,
               DATE '2026-11-17' AS d, TIME '03:50:15' AS t,
               TIMESTAMP '2026-11-17 03:50:15.281' AS ts,
               TIMESTAMPTZ '2026-11-17 03:50:15.281+00' AS tstz,
               'infinity'::timestamptz AS ts_inf, DATE '4714-11-24 BC' AS d_bc,
               INTERVAL '1 year 2 mons 3 days 04:05:06' AS iv,
               '\\xdeadbeef'::bytea AS raw, '8a6e0804-2bd0-4672-b79d-d97027f9071a'::uuid AS id,
               9007199254740993::bigint AS big, 9007199254740992::bigint AS exact,
               (-9223372036854775807 - 1)::bigint AS small, true AS yes, NULL::text AS nothing,
               1.5::float8 AS f, repeat('x', 30000) AS long_text, int4range(1, 5) AS rng,
               '{1.5,2.25}'::numeric[] AS nums, ARRAY[1.5, 'NaN']::float8[] AS floats,
               repeat('9', 30000)::numeric AS long_number, ROW(1, 'a') AS pair,
               'a "quoted" \\ line
break'::text AS odd, 7::smallint AS tiny, 'x'::"char" AS one, 'abc'::char(5) AS padded
    """

    def test_every_kind_of_value_becomes_json(self):
        result = self.run_sql(self.QUERY)
        json.dumps(result, allow_nan=False)
        longer = "... [cut: the value is longer]"
        self.assertEqual(dict(zip([column["name"] for column in result["columns"]],
                                  result["rows"][0])), {
            "ints": [1, 2, None], "texts": ["a", "b"],
            "doc": {"k": [1, 2.5, None], "big": "12345678901234567890"},
            "huge": {"n": "Infinity"},
            "num": "123.4500", "num_nan": "NaN",
            "f_nan": "NaN", "f_inf": "Infinity", "f_ninf": "-Infinity",
            "d": "2026-11-17", "t": "03:50:15", "ts": "2026-11-17T03:50:15.281",
            "tstz": "2026-11-17T03:50:15.281+00:00", "ts_inf": "infinity",
            "d_bc": "4714-11-24 BC", "iv": "1 year 2 mons 3 days 04:05:06",
            "raw": "\\xdeadbeef", "id": "8a6e0804-2bd0-4672-b79d-d97027f9071a",
            "big": "9007199254740993", "exact": 9007199254740992,
            "small": "-9223372036854775808", "yes": True, "nothing": None, "f": 1.5,
            "long_text": "x" * 10000 + longer, "rng": "[1,5)",
            "nums": ["1.5", "2.25"], "floats": [1.5, "NaN"],
            "long_number": "9" * 10000 + longer, "pair": {"f1": 1, "f2": "a"},
            "odd": 'a "quoted" \\ line\nbreak', "tiny": 7, "one": "x", "padded": "abc  ",
        })
        self.assertEqual([column["type"] for column in result["columns"]], [
            "integer[]", "text[]", "jsonb", "json", "numeric", "numeric", "double precision",
            "double precision", "real", "date", "time without time zone",
            "timestamp without time zone", "timestamp with time zone",
            "timestamp with time zone", "date", "interval", "bytea", "uuid", "bigint", "bigint",
            "bigint", "boolean", "text", "double precision", "text", "int4range", "numeric[]",
            "double precision[]", "numeric", "record", "text", "smallint", '"char"',
            "character"])

    def test_long_cells_of_every_kind_are_cut_by_the_server(self):
        store = open_store(self.settings.changed({"sql.max_cell_chars": 40}))
        settings = store.settings
        result = sqlrun.run_sql(store, """
            SELECT repeat('x', 100) AS t, repeat('é"\\', 100) AS escaped,
                   repeat('ab', 100)::bytea AS raw, array_fill(7, ARRAY[1000]) AS list,
                   jsonb_build_object('k', repeat('v', 1000)) AS doc,
                   array_fill(7, ARRAY[100]) AS small_list,
                   jsonb_build_object('k', repeat('v', 100)) AS small_doc,
                   repeat('1', 100)::numeric AS num, 'short' AS s, repeat('y', 40) AS fits
        """, settings=settings)
        longer = "... [cut: the value is longer]"
        row = dict(zip([column["name"] for column in result["columns"]], result["rows"][0]))
        self.assertEqual(row["t"], "x" * 40 + longer)
        self.assertEqual(row["escaped"], ('é"\\' * 14)[:40] + longer)
        self.assertEqual(row["raw"], ("\\x" + "6162" * 100)[:40] + longer)
        # Cut by the server (its JSON text is longer than what one cell may bring) ...
        self.assertEqual(row["list"], ("[" + "7," * 100)[:40] + longer)
        self.assertEqual(row["doc"], ('{"k": "' + "v" * 100)[:40] + longer)
        # ... or here, where the whole value arrived and its length is known.
        self.assertEqual(row["small_list"],
                         ("[" + "7," * 100)[:40] + "... [cut: 201 characters in all]")
        self.assertEqual(row["small_doc"],
                         ('{"k":"' + "v" * 100)[:40] + "... [cut: 108 characters in all]")
        self.assertEqual(row["num"], "1" * 40 + longer)
        self.assertEqual((row["s"], row["fits"]), ("short", "y" * 40))


if __name__ == "__main__":
    unittest.main()
