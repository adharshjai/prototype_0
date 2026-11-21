"""The store (specification section 4): the same test methods run against both engines, then
the things that belong to one engine only."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import stat
import subprocess
import sys
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pcldb import util
from pcldb.errors import PclError, Unavailable
from pcldb.store import DatabaseError, IntegrityError, SqlTextError, open_store, schema_def
from pcldb.store import base, sqlite_engine
from tests import support

PROJECT = Path(__file__).resolve().parent.parent
A, B, C = "raw/raman-portable/a", "raw/raman-portable/b", "raw/raman-portable/c"


class TestSqlText(unittest.TestCase):
    """How one SQL text becomes each engine's text."""

    def test_placeholders_schemas_and_percent(self):
        sql = "SELECT path FROM {pcl}.sessions s JOIN {app}.users u ON 1 = 1 " \
              "WHERE folder LIKE 'scan%' AND instrument = ? AND items > ? -- 100% sure?\n"
        self.assertEqual(base.prepare_sql(sql, "sqlite"), (
            "SELECT path FROM pcl.sessions s JOIN main.users u ON 1 = 1 "
            "WHERE folder LIKE 'scan%' AND instrument = ? AND items > ? -- 100% sure?\n", 2))
        self.assertEqual(base.prepare_sql(sql, "postgresql"), (
            "SELECT path FROM pcl.sessions s JOIN app.users u ON 1 = 1 "
            "WHERE folder LIKE 'scan%%' AND instrument = %s AND items > %s -- 100%% sure?\n", 2))

    def test_what_is_inside_quotes_and_comments_is_left_alone(self):
        sql = "SELECT '{pcl} it''s 5 - 3 / 2; ok' AS a, \"odd;name\" /* {app} ? */ FROM {pcl}.meta"
        for engine, schema in (("sqlite", "pcl"), ("postgresql", "pcl")):
            text, count = base.prepare_sql(sql, engine)
            self.assertEqual(count, 0)
            self.assertIn("'{pcl} it''s 5 - 3 / 2; ok'", text)
            self.assertIn("/* {app} ? */", text)
            self.assertTrue(text.endswith("FROM %s.meta" % schema))

    def test_a_question_mark_inside_quotes_is_refused(self):
        for sql in ("SELECT * FROM {pcl}.meta WHERE value = 'why?'",
                    "SELECT * FROM {pcl}.meta WHERE value = 'a''b?' AND key = ?",
                    'SELECT "what?" FROM {pcl}.meta'):
            for engine in schema_def.ENGINES:
                with self.assertRaises(SqlTextError, msg=sql) as caught:
                    base.prepare_sql(sql, engine)
                self.assertIn("inside quotes", str(caught.exception))
                self.assertIn("parameter", str(caught.exception))

    def test_other_text_both_engines_would_read_differently_is_refused(self):
        for sql in ("SELECT 1; SELECT 2", "DELETE FROM {pcl}.meta; -- gone\nDROP TABLE x",
                    "SELECT ?1", "SELECT 'never closed", "SELECT 1 /* never closed"):
            for engine in schema_def.ENGINES:
                with self.assertRaises(SqlTextError, msg=sql):
                    base.prepare_sql(sql, engine)
        self.assertEqual(base.prepare_sql("SELECT 1 ;  \n", "sqlite"), ("SELECT 1 ", 0))
        self.assertTrue(issubclass(SqlTextError, ValueError))


class StoreCases:
    """Everything here must hold on both engines."""

    def insert_session(self, db, path=A, **changes):
        row = support.session_row(path, **changes)
        db.insert("pcl.sessions", row)
        return row

    def count(self, table="pcl.sessions"):
        schema, name = table.split(".")
        return self.outside_rows("SELECT COUNT(*) FROM {%s}.%s" % (schema, name))[0][0]

    # -- the store itself ----------------------------------------------------------------------

    def test_describe(self):
        described = self.store.describe()
        self.assertEqual(sorted(described), ["engine", "version", "where"])
        self.assertEqual(described["engine"], self.engine)
        self.assertEqual(self.store.engine, self.engine)
        self.assertRegex(described["version"], r"^\d+\.\d+")
        self.assertIsInstance(described["where"], str)
        # K-01: /status shows "where" to every signed-in person: no folder of this computer
        # (it names the account), no user name and no password.
        project = str(self.store.settings.project_dir)
        self.assertNotIn(project, described["where"])
        self.assertNotIn(os.path.expanduser("~"), described["where"])
        if self.engine == "sqlite":
            self.assertEqual(described["where"],
                             "this computer, SQLite files in the project folder (var/sqlite)")
        if self.engine == "postgresql":
            from psycopg.conninfo import conninfo_to_dict
            for role in ("owner", "app", "reader"):
                password = conninfo_to_dict(self.store.conninfo(role))["password"]
                self.assertNotIn(password, json.dumps(described))

    def test_initialize_is_idempotent_and_keeps_the_rows(self):
        self.assertTrue(self.store.is_initialized())
        with self.store.app() as db:
            self.insert_session(db)
        self.store.initialize()
        self.store.initialize()
        again = open_store(self.settings)
        again.initialize()
        self.assertTrue(again.is_initialized())
        with again.app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 1)
            self.assertEqual(
                db.query("SELECT key, value FROM {pcl}.meta"),
                [{"key": "schema_version", "value": str(schema_def.SCHEMA_VERSION)}])
            self.assertEqual(
                db.query("SELECT key, value FROM {app}.meta"),
                [{"key": "schema_version", "value": str(schema_def.SCHEMA_VERSION)}])

    def test_an_empty_store_is_not_initialized(self):
        self.store.destroy()
        fresh = open_store(self.settings)
        self.assertFalse(fresh.is_initialized())
        with self.assertRaises(Unavailable) as caught:
            with fresh.app():
                pass
        self.assertEqual(caught.exception.code, "not_initialized")
        self.assertIn("pcldb setup", str(caught.exception))
        self.assertFalse(fresh.is_initialized())       # asking did not create anything
        fresh.initialize()
        self.assertTrue(fresh.is_initialized())
        with fresh.app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 0)

    def test_a_store_of_another_version_is_not_initialized(self):
        for schema in ("pcl", "app"):
            with self.store.app() as db:
                db.execute("UPDATE {%s}.meta SET value = ? WHERE key = ?"
                           % schema, ["0", "schema_version"])
            other = open_store(self.settings)
            self.assertFalse(other.is_initialized(), schema)
            for action in (other.initialize, lambda: other.app().__enter__()):
                with self.assertRaises(PclError) as caught:
                    action()
                self.assertEqual(caught.exception.code, "not_initialized")
                self.assertIn("pcldb db reset", str(caught.exception))
                self.assertIn("0", str(caught.exception))
            other.reset()
            self.assertTrue(other.is_initialized())

    def test_reset_empties_the_store(self):
        with self.store.app() as db:
            support.seed_minimal(db)
        self.store.reset()
        self.assertTrue(self.store.is_initialized())
        for table in schema_def.TABLES:
            expected = 1 if table.name == "meta" else 0
            self.assertEqual(self.count(table.qualified), expected, table.qualified)

    # -- values in and out -----------------------------------------------------------------

    def test_every_kind_goes_in_and_comes_out_normalised(self):
        card = {"schema": "pcl-session/1", "nested": {"list": [1, 2.5, None, True, "x"]},
                "text": "caf\u00e9 \u6f22\u5b57 \U0001f52c", "empty": {}, "zero": 0}
        row = support.session_row(
            A, simulated=True, bytes=2 ** 40 + 7, n_files=2 ** 31 - 1, items=0,
            items_planned=None, sample_ids=["S-1", "caf\u00e9", "\u6f22 \U0001f52c", "a,b", '"q"', ""],
            tags=[], notes="line 1\nline 2\ttab 'quote' \"double\" \\ back % percent ? mark",
            operator="demo-operator-\u00e5\u00f1\u00e9", card_json=card, corrections_json=[],
            upload_json={"transport": "ssh", "checked": True},
            started_utc="2026-11-17T03:50:15.281999Z",
            ended_utc=datetime.datetime(2026, 11, 17, 9, 20, 40, 43000,
                                        tzinfo=datetime.timezone(datetime.timedelta(hours=5,
                                                                                    minutes=30))),
            uploaded_utc="2026-11-19T02:31:49Z", verified_utc=None, verify_ok=None,
            is_copy=False, has_card=True, corrected=True, withdrawn=False, location=None)
        with self.store.app() as db:
            self.assertEqual(db.insert("pcl.sessions", row), 1)
            db.insert("pcl.points", {
                "run_path": "results/r", "point_file": "p1", "point_id": -5, "needs_review": True,
                "confidence": 0.7195311299559257, "threshold": 1, "scores_json": 0.25,
                "label": ""})
        expected = dict(row, started_utc="2026-11-17T03:50:15.281Z",
                        ended_utc="2026-11-17T03:50:40.043Z",
                        uploaded_utc="2026-11-19T02:31:49.000Z")
        with self.store.app() as db:
            found = db.one("SELECT * FROM {pcl}.sessions WHERE path = ?", [A])
            point = db.one("SELECT * FROM {pcl}.points")
        self.assertEqual(found, expected)
        self.assertEqual(list(found), list(schema_def.table("pcl.sessions").column_names))
        kinds = schema_def.table("pcl.sessions").kinds
        for name, value in found.items():
            if value is None:
                continue
            wanted = {"text": str, "int": int, "bigint": int, "bool": bool, "utc": str,
                      "strlist": list, "json": (dict, list)}[kinds[name]]
            self.assertIsInstance(value, wanted, name)
            if kinds[name] in ("int", "bigint"):
                self.assertNotIsInstance(value, bool, name)
            if kinds[name] == "utc":
                self.assertRegex(value, r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$", name)
        self.assertEqual(found["bytes"], 1099511627783)
        self.assertEqual(found["card_json"]["text"], "caf\u00e9 \u6f22\u5b57 \U0001f52c")
        self.assertEqual(point["confidence"], 0.7195311299559257)
        self.assertIsInstance(point["threshold"], float)
        self.assertEqual(point["threshold"], 1.0)
        self.assertEqual(point["scores_json"], 0.25)
        self.assertIs(point["needs_review"], True)
        self.assertEqual(point["label"], "")            # empty text is not NULL
        self.assertIsNone(point["capture_id"])          # a column left out is NULL
        self.assertEqual(point["point_id"], -5)

    def test_json_values_of_every_shape(self):
        shapes = [{"a": 1}, [1, [2, [3]]], "plain text", 12, 2.5, True, False, [], {},
                  {"k\u00e9y": "v\u00e4l"}, {"deep": {"er": {"est": [None]}}}, "", 0]
        with self.store.app() as db:
            for number, shape in enumerate(shapes):
                db.insert("app.registrations", {
                    "reg_id": "r%02d" % number, "at_utc": util.now_utc_text(),
                    "session_path": A, "action": "created", "source": "upload",
                    "detail_json": shape})
            db.insert("app.registrations", {
                "reg_id": "r99", "at_utc": util.now_utc_text(), "session_path": A,
                "action": "created", "source": "upload", "detail_json": None})
            rows = db.query("SELECT reg_id, detail_json FROM {app}.registrations ORDER BY reg_id")
        self.assertEqual([row["detail_json"] for row in rows], shapes + [None])
        for row, shape in zip(rows, shapes):
            self.assertIs(type(row["detail_json"]), type(shape), row["reg_id"])
        self.assertEqual(self.outside_rows(
            "SELECT COUNT(*) FROM {app}.registrations WHERE detail_json IS NULL"), [(1,)])

    def test_values_are_stored_in_each_engines_own_types(self):
        with self.store.app() as db:
            self.insert_session(db, sample_ids=["x"], is_copy=True)
        if self.engine == "postgresql":
            self.assertEqual(self.outside_rows(
                "SELECT pg_typeof(started_utc)::text, pg_typeof(card_json)::text, "
                "pg_typeof(sample_ids)::text, pg_typeof(is_copy)::text, pg_typeof(bytes)::text "
                "FROM {pcl}.sessions"),
                [("timestamp with time zone", "jsonb", "text[]", "boolean", "bigint")])
        else:
            self.assertEqual(self.outside_rows(
                "SELECT started_utc, card_json, sample_ids, is_copy, typeof(bytes) "
                "FROM {pcl}.sessions"),
                [("2026-11-15T16:09:40.537Z",
                  '{"schema":"pcl-session/1","session":"a","simulated":false}', '["x"]', 1,
                  "integer")])

    def test_insert_many(self):
        rows = [{"session_path": A, "rel_path": "f%03d" % number, "bytes": number, "listed": True,
                 "sha256": "%064x" % number} for number in range(1200)]
        rows.append({"session_path": A, "rel_path": "SHA256SUMS", "bytes": 2 ** 33,
                     "listed": False})                       # no sha256 key: NULL
        with self.store.app() as db:
            self.assertEqual(db.insert_many("pcl.files", rows), 1201)
            self.assertEqual(db.insert_many("pcl.files", []), 0)
            self.assertEqual(db.insert_many("pcl.files", iter([
                {"session_path": B, "rel_path": "one", "bytes": 1, "listed": True}])), 1)
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.files"), 1202)
            self.assertEqual(db.scalar("SELECT SUM(bytes) FROM {pcl}.files WHERE listed"),
                             sum(range(1200)) + 1)
            last = db.one("SELECT * FROM {pcl}.files WHERE rel_path = ?", ["SHA256SUMS"])
        self.assertEqual(last, {"session_path": A, "rel_path": "SHA256SUMS", "bytes": 8589934592,
                                "sha256": None, "listed": False})

    def test_upsert(self):
        with self.store.app() as db:
            self.assertEqual(db.upsert("pcl.sessions", support.session_row(A, notes="first"),
                                       key=("path",)), 1)
            # update=None: every column given that is not in the key.
            changed = support.session_row(A, notes="second", items=9, tags=["new"])
            self.assertEqual(db.upsert("pcl.sessions", changed, key=("path",)), 1)
            row = db.one("SELECT notes, items, tags FROM {pcl}.sessions WHERE path = ?", [A])
            self.assertEqual(row, {"notes": "second", "items": 9, "tags": ["new"]})
            # update=[...]: only those columns; the others keep what is stored.
            later = support.session_row(A, notes="third", items=1, registered_by="someone-else",
                                        updated_utc="2026-12-01T00:00:00.000Z")
            db.upsert("pcl.sessions", later, key=("path",), update=["notes", "updated_utc"])
            row = db.one("SELECT notes, items, registered_by, updated_utc FROM {pcl}.sessions")
            self.assertEqual(row, {"notes": "third", "items": 9,
                                   "registered_by": "demo-operator-a",
                                   "updated_utc": "2026-12-01T00:00:00.000Z"})
            # update=[]: an existing row stays as it is; a new one is inserted.
            self.assertEqual(db.upsert("pcl.sessions", support.session_row(A, notes="fourth"),
                                       key=("path",), update=[]), 0)
            self.assertEqual(db.upsert("pcl.sessions", support.session_row(B), key=("path",),
                                       update=[]), 1)
            self.assertEqual(db.scalar("SELECT notes FROM {pcl}.sessions WHERE path = ?", [A]),
                             "third")
            # A unique set works as the key too.
            entry = {"subject": "urn:x:1", "session_path": A, "content_json": {"v": 1},
                     "visible_to": [], "content_sha256": "0" * 64, "state": "pending",
                     "updated_utc": util.now_utc_text()}
            db.upsert("app.search_entries", entry, key=("subject",))
            db.upsert("app.search_entries", dict(entry, subject="urn:x:2", state="sent"),
                      key=("session_path",))
            self.assertEqual(db.query("SELECT subject, state FROM {app}.search_entries"),
                             [{"subject": "urn:x:2", "state": "sent"}])
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 2)

    def test_upsert_and_insert_refuse_what_cannot_work(self):
        with self.store.app() as db:
            for call in (
                    lambda: db.upsert("pcl.sessions", support.session_row(A), key=("folder",)),
                    lambda: db.upsert("pcl.sessions", {"notes": "x"}, key=("path",)),
                    lambda: db.upsert("pcl.sessions", {"path": A}, key=("path",), update=["notes"]),
                    lambda: db.insert("pcl.sessions", dict(support.session_row(A), colour="red")),
                    lambda: db.insert("pcl.sessions", {}),
                    lambda: db.insert("pcl.nothing", {"a": 1}),
                    lambda: db.insert("sessions", support.session_row(A)),
                    lambda: db.insert("pcl.current_runs", {"path": "x"}),
                    lambda: db.insert_many("pcl.files", [{"session_path": A, "size": 1}]),
                    lambda: db.delete_where("pcl.files", ""),
                    lambda: db.delete_where("pcl.session_summary", "1 = 1")):
                with self.assertRaises(ValueError):
                    call()
            # None of that reached the database: the transaction still works.
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 0)

    def test_execute_delete_where_and_row_counts(self):
        with self.store.app() as db:
            db.insert_many("pcl.files", [
                {"session_path": path, "rel_path": "f%d" % number, "bytes": number, "listed": True}
                for path in (A, B) for number in range(5)])
            self.assertEqual(db.execute("UPDATE {pcl}.files SET listed = ? WHERE bytes >= ?",
                                        [False, 3]), 4)
            self.assertEqual(db.execute("UPDATE {pcl}.files SET bytes = 0 WHERE 1 = 0"), 0)
            self.assertEqual(db.delete_where("pcl.files", "session_path = ? AND NOT listed", [A]), 2)
            self.assertEqual(db.delete_where("{pcl}.files", "session_path = ?", ["nobody"]), 0)
            self.assertEqual(db.delete_where("pcl.files", "1 = 1"), 8)
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.files"), 0)

    def test_query_one_scalar(self):
        with self.store.app() as db:
            self.assertEqual(db.query("SELECT path FROM {pcl}.sessions"), [])
            self.assertIsNone(db.one("SELECT path FROM {pcl}.sessions"))
            self.assertIsNone(db.scalar("SELECT path FROM {pcl}.sessions"))
            self.insert_session(db, A, location=None)
            self.insert_session(db, B, started_utc="2026-12-01T00:00:00Z")
            rows = db.query("SELECT path, location FROM {pcl}.sessions ORDER BY path")
            self.assertEqual(rows, [{"path": A, "location": None},
                                    {"path": B, "location": "demo bench"}])
            self.assertIsInstance(rows[0], dict)
            self.assertEqual(db.one("SELECT path FROM {pcl}.sessions ORDER BY path DESC"),
                             {"path": B})
            self.assertEqual(db.scalar("SELECT path FROM {pcl}.sessions ORDER BY path"), A)
            self.assertIsNone(db.scalar("SELECT location FROM {pcl}.sessions WHERE path = ?", [A]))
            self.assertEqual(db.query("UPDATE {pcl}.sessions SET notes = ?", ["x"]), [])

    def test_aggregates_come_back_the_same_on_both_engines(self):
        with self.store.app() as db:
            self.assertEqual(db.one("SELECT SUM(bytes) AS total, MAX(started_utc) AS newest, "
                                    "COUNT(*) AS n FROM {pcl}.sessions"),
                             {"total": None, "newest": None, "n": 0})
            self.insert_session(db, A, bytes=2 ** 40, n_files=1, items=4)
            self.insert_session(db, B, bytes=2 ** 41, n_files=2, items=None,
                                started_utc="2026-12-01T00:00:00Z")
            row = db.one(
                "SELECT COUNT(*) AS n, SUM(bytes) AS total, SUM(n_files) AS small, "
                "AVG(n_files) AS mean, MIN(started_utc) AS oldest, MAX(started_utc) AS newest, "
                "MAX(bytes) AS bytes, COUNT(items) AS with_items, MIN(folder) AS first_folder, "
                "COALESCE(SUM(items), 0) AS items FROM {pcl}.sessions")
        self.assertEqual(row, {
            "n": 2, "total": 2 ** 40 + 2 ** 41, "small": 3, "mean": 1.5,
            "oldest": "2026-11-15T16:09:40.537Z", "newest": "2026-12-01T00:00:00.000Z",
            "bytes": 2 ** 41, "with_items": 1, "first_folder": "a", "items": 4})
        for name in ("n", "total", "small", "bytes", "with_items", "items"):
            self.assertIs(type(row[name]), int, name)
        self.assertIs(type(row["mean"]), float)

    # -- the SQL text ----------------------------------------------------------------------

    def test_parameters_and_schema_names(self):
        with self.store.app() as db:
            for path, instrument in ((A, "raman-portable"), (B, "jeol-jsm-it800-shl"),
                                     (C, "raman-portable")):
                self.insert_session(db, path, instrument=instrument)
            db.insert("app.users", {"user_id": "u1", "name": "demo-reviewer", "role": "viewer",
                                    "disabled": False, "created_utc": util.now_utc_text()})
            self.assertEqual(
                [row["path"] for row in db.query(
                    "SELECT path FROM {pcl}.sessions WHERE instrument = ? AND path IN (?, ?) "
                    "ORDER BY path LIMIT ?", ["raman-portable", A, C, 5])], [A, C])
            self.assertEqual(db.scalar(
                "SELECT COUNT(*) FROM {pcl}.sessions s JOIN {app}.users u ON u.name = ? "
                "WHERE s.withdrawn = ? AND s.items >= ? AND s.location IS NOT NULL",
                ("demo-reviewer", False, 3)), 3)
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions WHERE NOT simulated "
                                       "AND has_card"), 3)
            self.assertEqual(db.scalar(
                "SELECT CASE WHEN COUNT(*) > ? THEN 'many' ELSE 'few' END FROM {pcl}.sessions",
                [2]), "many")
            self.assertEqual(db.scalar(
                "SELECT COALESCE(session_id, ?) FROM {pcl}.sessions WHERE path = ?", [None, A]),
                support.session_row(A)["session_id"])

    def test_a_percent_sign_is_plain_text(self):
        with self.store.app() as db:
            for path, notes in ((A, "100% done"), (B, "50% done"), (C, "done"),
                                ("raw/x/odd_1", "under_score"), ("raw/x/oddX1", "underXscore")):
                self.insert_session(db, path, notes=notes)
            def paths(sql, params=()):
                return [row["path"] for row in db.query(sql + " ORDER BY path", params)]
            # A % written in the SQL text, alone and beside a parameter.
            self.assertEqual(paths("SELECT path FROM {pcl}.sessions WHERE notes LIKE '%\\% done' "
                                   "ESCAPE '\\'"), [A, B])
            self.assertEqual(paths("SELECT path FROM {pcl}.sessions WHERE notes LIKE '1%' "
                                   "AND path = ?", [A]), [A])
            self.assertEqual(paths("SELECT path FROM {pcl}.sessions WHERE path LIKE 'raw/raman%'"),
                             [A, B, C])
            # A % inside a parameter.
            self.assertEqual(paths("SELECT path FROM {pcl}.sessions WHERE notes LIKE ?",
                                   ["%done"]), [A, B, C])
            self.assertEqual(db.scalar("SELECT ? || '%' || notes FROM {pcl}.sessions "
                                       "WHERE path = ?", ["5", C]), "5%done")
            # Wildcards in a person's text are made literal the same way on both engines.
            pattern = "%" + util.like_escape("r_s") + "%"
            self.assertEqual(paths("SELECT path FROM {pcl}.sessions WHERE notes LIKE ? "
                                   "ESCAPE '\\'", [pattern]), ["raw/x/odd_1"])
            self.assertEqual(paths("SELECT path FROM {pcl}.sessions WHERE notes LIKE ? "
                                   "ESCAPE '\\'", ["%" + util.like_escape("0% d") + "%"]), [A, B])

    def test_like_is_case_sensitive_and_lower_changes_ascii_only(self):
        with self.store.app() as db:
            self.insert_session(db, A, notes="Adenine \u00c9COLE")
            def matches(sql, params):
                return db.scalar("SELECT COUNT(*) FROM {pcl}.sessions WHERE " + sql, params)
            self.assertEqual(matches("notes LIKE ?", ["Adenine%"]), 1)
            self.assertEqual(matches("notes LIKE ?", ["adenine%"]), 0)
            self.assertEqual(matches("notes LIKE 'ADENINE%'", []), 0)
            self.assertEqual(matches("LOWER(notes) LIKE ?", ["adenine%"]), 1)
            self.assertEqual(db.scalar("SELECT LOWER(notes) FROM {pcl}.sessions"),
                             "adenine \u00c9cole")
            self.assertEqual(db.scalar("SELECT UPPER(folder) FROM {pcl}.sessions"), "A")
            db.execute("UPDATE {pcl}.sessions SET notes = ?", ["stra\u00dfe \u00e9t\u00e9 abc"])
            self.assertEqual(db.scalar("SELECT UPPER(notes) FROM {pcl}.sessions"),
                             "STRA\u00dfE \u00e9T\u00e9 ABC")
            self.assertEqual(matches("LOWER(notes) = ?", ["stra\u00dfe \u00e9t\u00e9 abc"]), 1)
            self.assertIsNone(db.scalar("SELECT LOWER(location) FROM {pcl}.sessions "
                                        "WHERE location IS NULL"))

    def test_text_sorts_by_bytes(self):
        folders = ["b", "B", "a", "A", "_x", "\u00e9", "Z", "10", "9", "a b", "aB", "ab", "~"]
        with self.store.app() as db:
            for number, folder in enumerate(folders):
                self.insert_session(db, "raw/x/%02d" % number, folder=folder)
            ordered = [row["folder"] for row in db.query(
                "SELECT folder FROM {pcl}.sessions ORDER BY folder")]
            smallest = db.scalar("SELECT MIN(folder) FROM {pcl}.sessions")
            greater = db.scalar("SELECT COUNT(*) FROM {pcl}.sessions WHERE folder > ?", ["a"])
        self.assertEqual(ordered, sorted(folders, key=lambda text: text.encode("utf-8")))
        self.assertEqual(smallest, "10")
        self.assertEqual(greater, len([f for f in folders if f.encode() > b"a"]))

    def test_a_question_mark_inside_quotes_is_refused_and_nothing_is_bound_wrongly(self):
        with self.store.app() as db:
            self.insert_session(db, A, notes="why?")
            for call in (
                    lambda: db.query("SELECT path FROM {pcl}.sessions WHERE notes = 'why?'"),
                    lambda: db.query("SELECT path FROM {pcl}.sessions WHERE notes = 'why?' "
                                     "AND path = ?", [A]),
                    lambda: db.execute("UPDATE {pcl}.sessions SET notes = '?' WHERE path = ?", [A]),
                    lambda: db.scalar('SELECT COUNT(*) AS "n?" FROM {pcl}.sessions')):
                with self.assertRaises(SqlTextError):
                    call()
            # The way to do it: the text is a parameter. A ? in a comment is only a comment.
            self.assertEqual(db.scalar("SELECT path FROM {pcl}.sessions WHERE notes = ? "
                                       "-- is this the one?\n", ["why?"]), A)
            self.assertEqual(db.scalar("SELECT notes /* really? */ FROM {pcl}.sessions"), "why?")

    def test_the_number_of_parameters_must_fit(self):
        with self.store.app() as db:
            for sql, params in (("SELECT ? AS x", []), ("SELECT ? AS x", [1, 2]),
                                ("SELECT 1 AS x", [1]), ("SELECT ? AS x, ? AS y", [1]),
                                ("SELECT ? AS x", "a"), ("SELECT ? AS x", {"x": 1})):
                with self.assertRaises(SqlTextError, msg=sql):
                    db.query(sql, params)
            with self.assertRaises(SqlTextError):
                db.execute("DELETE FROM {pcl}.files; DELETE FROM {pcl}.sessions")
            with self.assertRaises(SqlTextError):
                db.execute(b"SELECT 1")
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions;"), 0)

    def test_param_for_times_json_and_lists(self):
        moment = datetime.datetime(2026, 11, 17, 3, 50, 15, 281000, tzinfo=datetime.timezone.utc)
        with self.store.app() as db:
            self.insert_session(db, A, started_utc="2026-11-17T03:50:15.281Z")
            self.insert_session(db, B, started_utc="2026-11-17T03:50:15.282Z")
            self.insert_session(db, C, started_utc=None)
            def paths(sql, params):
                return [row["path"] for row in db.query(
                    "SELECT path FROM {pcl}.sessions WHERE " + sql + " ORDER BY path", params)]
            for value in ("2026-11-17T03:50:15.281Z", "2026-11-17T03:50:15.281999Z", moment,
                          moment.astimezone(datetime.timezone(datetime.timedelta(hours=-5)))):
                self.assertEqual(paths("started_utc > ?", [db.param("utc", value)]), [B])
                self.assertEqual(paths("started_utc = ?", [db.param("utc", value)]), [A])
            # An aware datetime may be passed as it is; so may the fixed-width text.
            self.assertEqual(paths("started_utc >= ?", [moment]), [A, B])
            self.assertEqual(paths("started_utc <= ?", ["2026-11-17T03:50:15.281Z"]), [A])
            self.assertEqual(paths("started_utc IS NULL OR started_utc < ?",
                                   [db.param("utc", "2026-01-01T00:00:00Z")]), [C])

            self.assertEqual(db.execute(
                "UPDATE {pcl}.sessions SET card_json = ?, tags = ?, updated_utc = ?, "
                "upload_json = ? WHERE path = ?",
                [db.param("json", {"b": [1, "\u00e9"], "a": None}),
                 db.param("strlist", ["x", "y z"]), db.param("utc", "2027-01-02T03:04:05Z"),
                 db.param("json", None), A]), 1)
            self.assertEqual(
                db.one("SELECT card_json, tags, updated_utc, upload_json FROM {pcl}.sessions "
                       "WHERE path = ?", [A]),
                {"card_json": {"a": None, "b": [1, "\u00e9"]}, "tags": ["x", "y z"],
                 "updated_utc": "2027-01-02T03:04:05.000Z", "upload_json": None})
            self.assertEqual(paths("tags = ?", [db.param("strlist", ["x", "y z"])]), [A])
            self.assertEqual(paths("tags = ?", [db.param("strlist", [])]), [B, C])
            self.assertEqual(paths("simulated = ?", [db.param("bool", False)]), [A, B, C])
            with self.assertRaises(ValueError):
                db.param("timestamp", moment)
            with self.assertRaises(PclError):
                db.param("utc", "yesterday")
            with self.assertRaises(PclError):
                db.param("json", {"x": float("nan")})

    def test_parameters_that_are_not_plain_values_are_refused(self):
        with self.store.app() as db:
            self.insert_session(db)
            for value in ({"a": 1}, ["a"], ("a",), {"a"}, b"bytes", object(),
                          datetime.date(2026, 11, 17)):
                with self.assertRaises(TypeError, msg=repr(value)) as caught:
                    db.query("SELECT path FROM {pcl}.sessions WHERE path = ?", [value])
                self.assertIn("Parameter 1", str(caught.exception))
            for value in ("nul\x00inside", "\udc80", 2 ** 63, -2 ** 63 - 1, float("nan"),
                          float("inf"), datetime.datetime(2026, 1, 1)):
                with self.assertRaises(PclError, msg=repr(value)):
                    db.query("SELECT path FROM {pcl}.sessions WHERE folder = ? OR items = ?",
                             ["a", value])
            # Refused before the database saw anything: the transaction goes on.
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions WHERE bytes < ?",
                                       [2 ** 63 - 1]), 1)

    # -- the same refusals on both engines (from_lead.md, item 1) ---------------------------

    def test_values_one_engine_would_take_and_the_other_refuse_are_refused_by_the_store(self):
        base_row = support.session_row(A)
        point = {"run_path": "results/r", "point_file": "p", "point_id": 1, "needs_review": False}
        cases = [
            ("pcl.sessions", dict(base_row, items=2 ** 31), "pcl.sessions.items"),
            ("pcl.sessions", dict(base_row, items=-2 ** 31 - 1), "pcl.sessions.items"),
            ("pcl.sessions", dict(base_row, n_files=10 ** 30), "pcl.sessions.n_files"),
            ("pcl.sessions", dict(base_row, bytes=2 ** 63), "pcl.sessions.bytes"),
            ("pcl.sessions", dict(base_row, bytes=-2 ** 63 - 1), "pcl.sessions.bytes"),
            ("pcl.sessions", dict(base_row, items=True), "pcl.sessions.items"),
            ("pcl.sessions", dict(base_row, bytes=False), "pcl.sessions.bytes"),
            ("pcl.sessions", dict(base_row, items=3.0), "pcl.sessions.items"),
            ("pcl.sessions", dict(base_row, items="3"), "pcl.sessions.items"),
            ("pcl.sessions", dict(base_row, simulated=1), "pcl.sessions.simulated"),
            ("pcl.sessions", dict(base_row, withdrawn="no"), "pcl.sessions.withdrawn"),
            ("pcl.sessions", dict(base_row, notes=5), "pcl.sessions.notes"),
            ("pcl.sessions", dict(base_row, notes=b"bytes"), "pcl.sessions.notes"),
            ("pcl.sessions", dict(base_row, notes="nul\x00inside"), "pcl.sessions.notes"),
            ("pcl.sessions", dict(base_row, operator="half \ud83d"), "pcl.sessions.operator"),
            ("pcl.sessions", dict(base_row, sample_ids=["ok", 5]), "pcl.sessions.sample_ids"),
            ("pcl.sessions", dict(base_row, sample_ids=["ok", None]), "pcl.sessions.sample_ids"),
            ("pcl.sessions", dict(base_row, sample_ids="S-1"), "pcl.sessions.sample_ids"),
            ("pcl.sessions", dict(base_row, tags=["a\x00b"]), "pcl.sessions.tags"),
            ("pcl.sessions", dict(base_row, tags=["\udc80"]), "pcl.sessions.tags"),
            ("pcl.sessions", dict(base_row, card_json={"x": float("nan")}),
             "pcl.sessions.card_json"),
            ("pcl.sessions", dict(base_row, card_json={"a": [1, {"b": float("-inf")}]}),
             "pcl.sessions.card_json"),
            ("pcl.sessions", dict(base_row, card_json={"a": ["nul\x00"]}),
             "pcl.sessions.card_json"),
            ("pcl.sessions", dict(base_row, card_json={"k\x00": 1}), "pcl.sessions.card_json"),
            ("pcl.sessions", dict(base_row, card_json={"a": "\ud800"}), "pcl.sessions.card_json"),
            ("pcl.sessions", dict(base_row, card_json={"a": object()}), "pcl.sessions.card_json"),
            ("pcl.sessions", dict(base_row, started_utc="2026-11-17 03:50"),
             "pcl.sessions.started_utc"),
            ("pcl.sessions", dict(base_row, started_utc=datetime.datetime(2026, 1, 1)),
             "pcl.sessions.started_utc"),
            ("pcl.sessions", dict(base_row, started_utc=1794887440), "pcl.sessions.started_utc"),
            ("pcl.points", dict(point, confidence=float("nan")), "pcl.points.confidence"),
            ("pcl.points", dict(point, confidence=float("inf")), "pcl.points.confidence"),
            ("pcl.points", dict(point, threshold=-float("inf")), "pcl.points.threshold"),
            ("pcl.points", dict(point, confidence=True), "pcl.points.confidence"),
            ("pcl.points", dict(point, confidence="0.5"), "pcl.points.confidence"),
            ("pcl.points", dict(point, confidence=10 ** 400), "pcl.points.confidence"),
        ]
        with self.store.app() as db:
            for table, row, column in cases:
                for call in (lambda: db.insert(table, row),
                             lambda: db.insert_many(table, [row]),
                             lambda: db.upsert(table, row, key=schema_def.table(table).primary_key)):
                    with self.assertRaises(PclError, msg=column) as caught:
                        call()
                    self.assertIn(column, str(caught.exception))
                    self.assertEqual(caught.exception.code, "bad_value")
                    self.assertTrue(caught.exception.details["column"].startswith(column))
            # Text that only LOOKS like a NUL escape is fine, and so are the edges of the ranges.
            db.insert("pcl.sessions", dict(base_row, items=2 ** 31 - 1, n_files=-2 ** 31,
                                           bytes=2 ** 63 - 1,
                                           card_json={"a": "\\u0000 is six characters"}))
            db.insert("pcl.points", dict(point, confidence=1e308, threshold=-0.0))
            self.assertEqual(db.one("SELECT items, n_files, bytes, card_json FROM {pcl}.sessions"),
                             {"items": 2147483647, "n_files": -2147483648,
                              "bytes": 9223372036854775807,
                              "card_json": {"a": "\\u0000 is six characters"}})
        # Nothing of the refused rows was written, and the good rows were committed.
        self.assertEqual(self.count("pcl.sessions"), 1)
        self.assertEqual(self.count("pcl.points"), 1)

    # -- transactions --------------------------------------------------------------------------

    def test_commit_on_success(self):
        with self.store.app() as db:
            self.insert_session(db)
            self.assertEqual(self.count(), 0)      # not visible to others before the commit
        self.assertEqual(self.count(), 1)

    def test_rollback_on_exception(self):
        class Stop(Exception):
            pass
        with self.assertRaises(Stop):
            with self.store.app() as db:
                self.insert_session(db)
                db.insert("app.users", {"user_id": "u1", "name": "demo-reviewer",
                                        "role": "viewer", "disabled": False,
                                        "created_utc": util.now_utc_text()})
                self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 1)
                raise Stop()
        self.assertEqual(self.count("pcl.sessions"), 0)     # both schemas rolled back together
        self.assertEqual(self.count("app.users"), 0)
        with self.assertRaises(KeyboardInterrupt):
            with self.store.app() as db:
                self.insert_session(db)
                raise KeyboardInterrupt()
        self.assertEqual(self.count("pcl.sessions"), 0)

    def assert_closed(self, db):
        with self.assertRaises(DatabaseError) as caught:
            db.execute("SELECT 1")
        self.assertIn("closed", str(caught.exception))
        with self.assertRaises(DatabaseError):
            db.insert("pcl.meta", {"key": "late", "value": "x"})
        if self.engine == "sqlite":
            with self.assertRaises(sqlite3.ProgrammingError):
                db._connection.execute("SELECT 1")
        else:
            self.assertTrue(db._connection.closed)

    def test_the_connection_is_always_closed(self):
        with self.store.app() as db:
            self.insert_session(db)
        self.assert_closed(db)
        kept = []
        with self.assertRaises(ZeroDivisionError):
            with self.store.app() as db:
                kept.append(db)
                1 / 0
        self.assert_closed(kept[0])
        with self.assertRaises(DatabaseError):
            with self.store.app() as db:
                kept.append(db)
                db.execute("SELECT nothing FROM {pcl}.nowhere")
        self.assert_closed(kept[1])
        self.assertEqual(self.count("pcl.meta"), 1)

    def test_a_refused_statement_ends_the_transaction_on_both_engines(self):
        with self.store.app() as db:
            self.insert_session(db, A)
        # Swallowing the error does not help: nothing more runs, and nothing is committed.
        with self.assertRaises(DatabaseError) as outer:
            with self.store.app() as db:
                self.insert_session(db, B)
                with self.assertRaises(IntegrityError) as caught:
                    self.insert_session(db, A)                      # the key is taken
                self.assertEqual(caught.exception.kind, "unique")
                self.assertIsInstance(caught.exception, DatabaseError)
                with self.assertRaises(DatabaseError) as later:
                    db.scalar("SELECT COUNT(*) FROM {pcl}.sessions")
                self.assertIn("earlier statement", str(later.exception))
                with self.assertRaises(DatabaseError):
                    db.insert("pcl.meta", {"key": "k", "value": "v"})
        self.assertIn("rolled back", str(outer.exception))
        self.assertEqual(self.count("pcl.sessions"), 1)
        self.assertEqual(self.count("pcl.meta"), 1)

    def test_kinds_of_database_errors(self):
        def failing(action):
            with self.assertRaises(DatabaseError) as caught:
                with self.store.app() as db:
                    action(db)
            return caught.exception
        with self.store.app() as db:
            self.insert_session(db, A)
        error = failing(lambda db: db.insert("pcl.sessions", support.session_row(
            B, subject=support.session_row(A)["subject"])))             # a UNIQUE set
        self.assertIsInstance(error, IntegrityError)
        self.assertEqual(error.kind, "unique")
        error = failing(lambda db: db.insert("pcl.sessions", support.session_row(B, folder=None)))
        self.assertIsInstance(error, IntegrityError)
        self.assertEqual(error.kind, "not_null")
        error = failing(lambda db: db.insert("pcl.files", {"session_path": A, "bytes": 1,
                                                           "listed": True}))  # no rel_path
        self.assertEqual(error.kind, "not_null")
        for sql in ("SELECT * FROM {pcl}.no_such_table", "SELECT no_such_column FROM {pcl}.meta",
                    "SELEC 1", "INSERT INTO {pcl}.meta (key) VALUES (?, ?)"):
            error = failing(lambda db: db.execute(sql, [1, 2][:sql.count("?")]))
            self.assertNotIsInstance(error, IntegrityError, sql)
            self.assertTrue(str(error), sql)
        self.assertEqual(self.count("pcl.sessions"), 1)

    def test_a_second_connection_sees_only_committed_rows(self):
        with self.store.app() as db:
            self.insert_session(db, A)
        with self.store.app() as db:
            self.insert_session(db, B)
            db.execute("UPDATE {pcl}.sessions SET notes = ? WHERE path = ?", ["changed", A])
            self.assertEqual(self.outside_rows(
                "SELECT path, notes FROM {pcl}.sessions ORDER BY path"), [(A, "test notes")])
        self.assertEqual(self.outside_rows("SELECT path, notes FROM {pcl}.sessions ORDER BY path"),
                         [(A, "changed"), (B, "test notes")])

    def test_two_writers_both_succeed_and_the_second_waits_for_the_first(self):
        steps, errors = [], []
        entered, finished = threading.Event(), threading.Event()

        def second_writer():
            try:
                entered.set()
                with self.store.app() as db:
                    steps.append("second writes")
                    db.insert("pcl.meta", {"key": "second", "value": "2"})
                steps.append("second committed")
            except BaseException as error:      # reported by the main thread
                errors.append(error)
            finally:
                finished.set()

        thread = threading.Thread(target=second_writer, daemon=True)
        with self.store.app() as db:
            db.insert("pcl.meta", {"key": "first", "value": "1"})
            thread.start()
            self.assertTrue(entered.wait(10))
            done_early = finished.wait(0.7)
            if self.engine == "sqlite":
                # One writer at a time: the second is waiting for the lock, it has not failed.
                self.assertFalse(done_early)
                self.assertEqual(steps, [])
            steps.append("first leaves")
        thread.join(20)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertIn("second committed", steps)
        if self.engine == "sqlite":
            self.assertEqual(steps, ["first leaves", "second writes", "second committed"])
        self.assertEqual(self.outside_rows("SELECT key FROM {pcl}.meta ORDER BY key"),
                         [("first",), ("schema_version",), ("second",)])

    def test_a_block_inside_a_block_is_refused_at_once(self):
        started = time.monotonic()
        with self.store.app() as db:
            db.insert("pcl.meta", {"key": "outer", "value": "1"})
            for store in (self.store, open_store(self.settings)):
                with self.assertRaises(DatabaseError) as caught:
                    with store.app():
                        self.fail("a second block was opened inside the first")
                self.assertIn("inside another", str(caught.exception))
            db.insert("pcl.meta", {"key": "still", "value": "works"})  # the outer block is fine
        self.assertLess(time.monotonic() - started, 3)              # no waiting for a lock
        self.assertEqual(self.count("pcl.meta"), 3)
        with self.store.app():                                       # one after the other: fine
            pass
        # Another database in the same thread is another matter.
        with support.TempProject("sqlite") as other_project:
            other = open_store(other_project.settings)
            other.initialize()
            with self.store.app() as db, other.app() as other_db:
                db.insert("pcl.meta", {"key": "a", "value": "1"})
                other_db.insert("pcl.meta", {"key": "b", "value": "2"})
        # And a block that failed leaves nothing held.
        with self.assertRaises(ZeroDivisionError):
            with self.store.app():
                1 / 0
        with self.store.app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.meta"), 4)

    # -- the seed and the views ----------------------------------------------------------------

    def test_seed_minimal(self):
        with self.store.app() as db:
            keys = support.seed_minimal(db)
        self.assertEqual(sorted(keys), sorted([
            "real_path", "simulated_path", "session_paths", "run_path", "review_run_path",
            "model_name", "user_id", "user_name", "token_id", "token"]))
        with self.store.app() as db:
            sessions = {row["path"]: row for row in db.query("SELECT * FROM {pcl}.sessions")}
            self.assertEqual(sorted(sessions), sorted(keys["session_paths"]))
            self.assertIs(sessions[keys["real_path"]]["simulated"], False)
            self.assertIs(sessions[keys["simulated_path"]]["simulated"], True)
            self.assertEqual(sessions[keys["simulated_path"]]["area"], "simulated")
            for path, row in sessions.items():
                files = db.one("SELECT COUNT(*) AS n, SUM(bytes) AS total FROM {pcl}.files "
                               "WHERE session_path = ?", [path])
                self.assertEqual((row["n_files"], row["bytes"]), (files["n"], files["total"]))
                self.assertGreater(files["n"], 0)
            self.assertEqual(db.query("SELECT path, session_path FROM {pcl}.current_runs"),
                             [{"path": keys["run_path"], "session_path": keys["real_path"]}])
            points = db.query("SELECT point_id, effective_label, awaiting_review "
                              "FROM {pcl}.current_points ORDER BY point_id")
            self.assertEqual(points, [
                {"point_id": 1, "effective_label": "adenine", "awaiting_review": False},
                {"point_id": 2, "effective_label": "r6g", "awaiting_review": False},
                {"point_id": 3, "effective_label": "UNKNOWN", "awaiting_review": True}])
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.latest_reviews"), 1)
            summary = db.one("SELECT * FROM {pcl}.session_summary WHERE path = ?",
                             [keys["real_path"]])
            self.assertEqual((summary["review_state"], summary["n_needs_review"],
                              summary["labels"], summary["files"]),
                             ("pending", 1, ["UNKNOWN", "adenine", "r6g"], 5))
            self.assertEqual(db.query("SELECT label, n FROM {pcl}.session_labels ORDER BY label"),
                             [{"label": "UNKNOWN", "n": 1}, {"label": "adenine", "n": 1},
                              {"label": "r6g", "n": 1}])
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.session_samples"), 3)
            self.assertEqual(db.query("SELECT * FROM {pcl}.session_tags"),
                             [{"session_path": keys["simulated_path"], "tag": "demo"}])
            user = db.one("SELECT * FROM {app}.users")
            self.assertEqual((user["user_id"], user["name"], user["role"], user["disabled"]),
                             (keys["user_id"], keys["user_name"], "admin", False))
            token = db.one("SELECT * FROM {app}.tokens")
            self.assertEqual(token["token_id"], keys["token_id"])
            self.assertEqual(token["user_id"], keys["user_id"])
            self.assertEqual(token["token_sha256"],
                             util.sha256_hex(keys["token"].encode("utf-8")))
            self.assertRegex(keys["token"], r"^pcl_[A-Za-z0-9_-]{43}$")
            stored = json.dumps(db.query("SELECT * FROM {app}.tokens"))
            self.assertNotIn(keys["token"], stored)        # only the hash is in the database

    def test_every_test_method_starts_with_an_empty_store_1(self):
        self._starts_empty()

    def test_every_test_method_starts_with_an_empty_store_2(self):
        self._starts_empty()

    def _starts_empty(self):
        self.assertEqual(self.count("pcl.sessions"), 0)
        self.assertEqual(self.count("app.users"), 0)
        self.assertTrue(self.project.path.is_dir())
        self.assertIn(" ", str(self.project.path))
        self.assertEqual(self.settings.project_dir, self.project.path)
        with self.store.app() as db:
            support.seed_minimal(db)
        (self.project.path / "left behind.txt").write_text("x", encoding="utf-8")


class SettingsChangesCases:
    settings_changes = {"sql.max_rows": 5, "search.include_notes": True}

    def test_settings_changes_reach_the_test(self):
        self.assertEqual(self.settings.sql.max_rows, 5)
        self.assertTrue(self.settings.search.include_notes)
        self.assertEqual(self.settings.database.engine, self.engine)
        self.assertTrue(self.store.is_initialized())


class TestStoreSqlite(StoreCases, support.SqliteCase, unittest.TestCase):
    pass


class TestStorePostgres(StoreCases, support.PostgresCase, unittest.TestCase):
    pass


class TestSettingsChangesSqlite(SettingsChangesCases, support.SqliteCase, unittest.TestCase):
    pass


class TestSettingsChangesPostgres(SettingsChangesCases, support.PostgresCase, unittest.TestCase):
    pass


# ---------------------------------------------------------------------- SQLite only


class TestSqliteOnly(support.SqliteCase, unittest.TestCase):
    def test_two_files_in_the_configured_folder(self):
        folder = self.project.path / "var" / "sqlite"
        self.assertEqual(self.store.folder, folder)
        self.assertEqual(self.store.app_path, folder / "pcl_app.sqlite")
        self.assertEqual(self.store.catalog_path, folder / "pcl_catalog.sqlite")
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ["pcl_app.sqlite", "pcl_catalog.sqlite"])
        with self.store.app() as db:
            support.seed_minimal(db)
        self.assertEqual(sorted(path.name for path in folder.iterdir()),
                         ["pcl_app.sqlite", "pcl_catalog.sqlite"])      # no side files stay

    def test_a_network_share_is_refused_in_plain_words(self):
        # WIN-07: SQLite refuses file://server/share/... ("invalid uri authority").
        from unittest import mock
        from pcldb.store import sqlite_engine
        is_share = sqlite_engine.is_network_path
        for path, expected in ((r"\\labserver\share\pcldb\var\sqlite\pcl_app.sqlite", True),
                               (r"\\?\UNC\labserver\share\x.sqlite", True),
                               ("//labserver/share/x.sqlite", True),
                               (r"C:\Users\me\pcldb\var\sqlite\pcl_app.sqlite", False),
                               (r"Z:\pcldb\x.sqlite", False),
                               (r"\\?\C:\pcldb\x.sqlite", False)):
            with self.subTest(path=path):
                self.assertIs(is_share(path, windows=True), expected)
        self.assertFalse(is_share("//labserver/share/x.sqlite", windows=False))
        with mock.patch.object(sqlite_engine, "is_network_path", lambda path: True):
            with self.assertRaises(PclError) as caught:
                sqlite_engine.file_uri(self.store.app_path, "rw")
        self.assertEqual(caught.exception.code, "network_folder")
        self.assertIn("local disk", caught.exception.message)

    def test_reads_and_private_writes_do_not_wait_for_a_long_catalogue_reader(self):
        # SEC-01: an expert query reads the catalogue for up to sql.timeout_seconds. A token
        # check (a read), its "last used" note and the query log must not wait behind it.
        import sqlite3
        import time
        from pcldb import auth
        from pcldb.store import open_store
        store = open_store(self.settings.changed({"sql.timeout_seconds": 1}))
        with store.app() as db:
            support.seed_minimal(db)
            auth.create_user(db, "demo-viewer", "viewer")
            token = auth.issue_token(db, "demo-viewer")
        reader = sqlite3.connect(str(store.catalog_path), isolation_level=None)
        try:
            reader.execute("BEGIN")
            reader.execute("SELECT COUNT(*) FROM sessions").fetchone()     # holds the read lock
            started = time.monotonic()
            with store.app() as db:
                self.assertEqual(db.scalar("SELECT COUNT(*) FROM {app}.users"), 2)
            user = auth.authenticate_store(store, token)          # writes "last used" too
            with store.app_only() as db:
                db.insert("app.query_log", {
                    "log_id": "0" * 32, "at_utc": "2026-11-20T00:00:00.000Z",
                    "user_name": "demo-viewer", "mode": "sql", "query_text": "SELECT 1",
                    "request_json": None, "ok": True, "error_text": None, "n_rows": 1,
                    "elapsed_ms": 1, "backend": "sqlite"})
            self.assertLess(time.monotonic() - started, 2.0)
            self.assertEqual(user.name, "demo-viewer")
        finally:
            reader.execute("ROLLBACK")
            reader.close()
        with store.app() as db:
            self.assertIsNotNone(db.scalar("SELECT last_used_utc FROM {app}.tokens WHERE "
                                           "token_sha256 = ?", [auth.token_sha256(token)]))
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {app}.query_log WHERE log_id = ?",
                                       ["0" * 32]), 1)

    def test_attach_names_pragmas_and_begin_immediate(self):
        with self.store.app() as db:
            connection = db._connection
            names = {row[1]: row[2] for row in connection.execute("PRAGMA database_list")}
            self.assertEqual(sorted(names), ["main", "pcl"])
            self.assertTrue(os.path.samefile(names["main"], self.store.app_path))
            self.assertTrue(os.path.samefile(names["pcl"], self.store.catalog_path))
            self.assertEqual(connection.execute("PRAGMA foreign_keys").fetchone()[0], 1)
            # from_lead 20a: longer than the longest expert query (15 s + 5 s by default).
            self.assertEqual(connection.execute("PRAGMA busy_timeout").fetchone()[0], 20000)
            self.assertEqual(connection.execute("PRAGMA main.journal_mode").fetchone()[0], "delete")
            self.assertEqual(connection.execute("PRAGMA pcl.journal_mode").fetchone()[0], "delete")
            self.assertTrue(connection.in_transaction)
            # BEGIN IMMEDIATE: the write lock is held on BOTH files before anything is written.
            for path in (self.store.app_path, self.store.catalog_path):
                other = sqlite3.connect(path, isolation_level=None, timeout=0)
                try:
                    with self.assertRaises(sqlite3.OperationalError) as caught:
                        other.execute("BEGIN IMMEDIATE")
                    self.assertIn("locked", str(caught.exception))
                    other.execute("SELECT COUNT(*) FROM sqlite_master").fetchall()  # reading: fine
                finally:
                    other.close()
            # {app} is main, {pcl} is the attached catalogue.
            self.assertEqual(base.prepare_sql("{app}.users {pcl}.sessions", "sqlite")[0],
                             "main.users pcl.sessions")
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM main.users"), 0)

    def test_a_journal_mode_set_by_another_tool_is_put_back(self):
        other = sqlite3.connect(self.store.catalog_path)
        self.assertEqual(other.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        other.close()
        with self.store.app() as db:
            self.assertEqual(db._connection.execute("PRAGMA pcl.journal_mode").fetchone()[0],
                             "delete")

    def test_the_catalogue_file_stands_alone_and_holds_no_app_table(self):
        with self.store.app() as db:
            keys = support.seed_minimal(db)
        copy = self.project.path / "copied catalogue.sqlite"
        copy.write_bytes(self.store.catalog_path.read_bytes())
        connection = sqlite3.connect(copy)              # a plain connection, nothing attached
        try:
            names = {row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
            self.assertEqual(names, {item.name for item in schema_def.relations("pcl")})
            for private in ("users", "tokens", "registrations", "query_log", "search_entries"):
                self.assertNotIn(private, names)
            # Every view works with only this file.
            for view in schema_def.VIEWS:
                connection.execute("SELECT * FROM %s" % view.name).fetchall()
            self.assertEqual(connection.execute(
                "SELECT s.folder, c.model_name FROM current_runs c "
                "JOIN sessions s ON s.path = c.session_path").fetchall(),
                [("1d_y_scan_20261115_110940_537", keys["model_name"])])
            self.assertEqual(connection.execute(
                "SELECT sample_id FROM session_samples ORDER BY 1").fetchall(),
                [("S-100",), ("S-200",), ("S-201",)])
            content = copy.read_bytes()
            self.assertNotIn(keys["token"].encode(), content)
            self.assertNotIn(util.sha256_hex(keys["token"].encode()).encode(), content)
        finally:
            connection.close()
        private = sqlite3.connect(self.store.app_path)
        try:
            names = {row[0] for row in private.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')")}
            self.assertEqual(names, {item.name for item in schema_def.relations("app")})
        finally:
            private.close()

    @unittest.skipIf(os.name == "nt", "Windows has no file modes")
    def test_the_private_file_is_for_this_account_only(self):
        self.assertEqual(stat.S_IMODE(self.store.app_path.stat().st_mode), 0o600)

    def test_a_writer_that_waits_too_long_gets_a_plain_error(self):
        outcome = {}

        def second_writer():
            started = time.monotonic()
            try:
                with open_store(self.settings).app():
                    outcome["error"] = "two writers at once"
            except BaseException as error:
                outcome["error"] = error
            outcome["waited"] = time.monotonic() - started

        with mock.patch.object(sqlite_engine, "busy_milliseconds", lambda _settings: 150):
            with self.store.app() as db:
                db.insert("pcl.meta", {"key": "k", "value": "v"})
                thread = threading.Thread(target=second_writer, daemon=True)
                thread.start()
                thread.join(20)
                self.assertFalse(thread.is_alive())
        self.assertIsInstance(outcome["error"], Unavailable)
        self.assertEqual(outcome["error"].code, "db_busy")
        self.assertIn("busy", str(outcome["error"]))
        self.assertGreaterEqual(outcome["waited"], 0.1)
        self.assertLess(outcome["waited"], 3)

    def test_a_commit_that_cannot_get_the_file_rolls_back(self):
        reader = sqlite3.connect(sqlite_engine.file_uri(self.store.catalog_path, "ro"), uri=True)
        try:
            with self.store.app() as db:
                db.insert_many("pcl.meta", [{"key": "k%d" % n, "value": "v"} for n in range(50)])
            cursor = reader.execute("SELECT key FROM meta")
            cursor.fetchone()                       # a reader in the middle of its rows
            with mock.patch.object(sqlite_engine, "busy_milliseconds", lambda _settings: 150):
                with self.assertRaises(Unavailable) as caught:
                    with self.store.app() as db:
                        db.insert("pcl.meta", {"key": "blocked", "value": "v"})
            self.assertEqual(caught.exception.code, "db_busy")
            cursor.fetchall()
        finally:
            reader.close()
        self.assertEqual(self.outside_rows("SELECT COUNT(*) FROM {pcl}.meta WHERE key = ?",
                                           ["blocked"]), [(0,)])
        with self.store.app() as db:                # and the store works again
            db.insert("pcl.meta", {"key": "blocked", "value": "v"})

    def test_an_old_sqlite_is_named_in_the_error(self):
        with mock.patch.object(sqlite3, "sqlite_version_info", (3, 34, 1)), \
                mock.patch.object(sqlite3, "sqlite_version", "3.34.1"):
            for action in (self.store.initialize, lambda: self.store.app().__enter__(),
                           open_store(self.settings).is_initialized):
                with self.assertRaises(PclError) as caught:
                    action()
                self.assertIn("3.34.1", str(caught.exception))
                self.assertIn("3.35", str(caught.exception))

    def test_file_addresses_work_with_spaces_and_odd_characters(self):
        for name in ("with space", "hash#tag", "100%25", "caf\u00e9 \u6f22"):
            folder = self.project.path / name
            settings = self.settings.changed({"database.sqlite.folder": str(folder)})
            store = open_store(settings)
            store.initialize()
            with store.app() as db:
                db.insert("pcl.meta", {"key": "where", "value": name})
            self.assertEqual(sorted(path.name for path in folder.iterdir()),
                             ["pcl_app.sqlite", "pcl_catalog.sqlite"], name)
            connection = sqlite3.connect(store.catalog_uri("ro"), uri=True)
            try:
                self.assertEqual(connection.execute(
                    "SELECT value FROM meta WHERE key = 'where'").fetchall(), [(name,)])
                with self.assertRaises(sqlite3.OperationalError):
                    connection.execute("DELETE FROM meta")          # mode=ro
            finally:
                connection.close()
        self.assertTrue(sqlite_engine.file_uri(self.store.catalog_path, "ro").startswith("file:///"))
        self.assertTrue(self.store.catalog_uri().endswith("pcl_catalog.sqlite?mode=ro"))
        self.assertIn("%20", self.store.catalog_uri())              # the space of the test folder

    def test_half_a_store_is_completed_and_a_foreign_file_is_refused(self):
        with self.store.app() as db:
            keys = support.seed_minimal(db)
        self.store.catalog_path.unlink()                 # the catalogue can always be rebuilt
        again = open_store(self.settings)
        self.assertFalse(again.is_initialized())
        again.initialize()
        with again.app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 0)
            self.assertEqual(db.scalar("SELECT name FROM {app}.users"), keys["user_name"])
        again.catalog_path.unlink()
        again.catalog_path.write_bytes(b"this is not a database, it is a text file\n" * 30)
        other = open_store(self.settings)
        self.assertFalse(other.is_initialized())
        with self.assertRaises(PclError):
            other.initialize()
        self.assertEqual(other.catalog_path.read_bytes()[:11], b"this is not")   # left untouched

    def test_a_crash_in_the_middle_of_a_commit_is_repaired_when_the_store_is_opened(self):
        import shutil
        with self.store.app() as db:
            db.insert_many("pcl.files", [
                {"session_path": A, "rel_path": "f%05d" % number, "bytes": number, "listed": True}
                for number in range(2000)])
        # A writer that has already put changed pages into the file, its journal beside it.
        writer = sqlite3.connect(self.store.catalog_path, isolation_level=None)
        try:
            writer.execute("PRAGMA cache_size=5")
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE files SET bytes = bytes + 1")
            writer.execute("DELETE FROM meta")
            crashed = self.project.path / "after the crash"
            crashed.mkdir()
            for path in self.store.folder.iterdir():        # what a power cut would leave
                shutil.copy2(path, crashed / path.name)
        finally:
            writer.execute("ROLLBACK")
            writer.close()
        self.assertIn("pcl_catalog.sqlite-journal", [path.name for path in crashed.iterdir()])
        read_only = sqlite3.connect(sqlite_engine.file_uri(crashed / "pcl_catalog.sqlite", "ro"),
                                    uri=True)
        try:
            with self.assertRaises(sqlite3.OperationalError):    # cannot replay the journal
                read_only.execute("SELECT COUNT(*) FROM meta").fetchall()
        finally:
            read_only.close()
        store = open_store(self.settings.changed({"database.sqlite.folder": str(crashed)}))
        self.assertTrue(store.is_initialized())
        with store.app() as db:
            self.assertEqual(db.scalar("SELECT value FROM {pcl}.meta WHERE key = ?",
                                       ["schema_version"]), "1")
            self.assertEqual(db.scalar("SELECT SUM(bytes) FROM {pcl}.files"), sum(range(2000)))
        self.assertEqual(sorted(path.name for path in crashed.iterdir()),
                         ["pcl_app.sqlite", "pcl_catalog.sqlite"])

    def test_destroy_removes_the_files(self):
        self.store.destroy()
        self.assertEqual(list(self.store.folder.iterdir()), [])
        self.store.destroy()                             # nothing there: still fine
        self.assertFalse(self.store.is_initialized())
        self.assertEqual(list(self.store.folder.iterdir()), [])     # asking creates nothing

    def test_it_works_when_psycopg_cannot_be_imported(self):
        script = textwrap.dedent("""
            import shutil, sys, tempfile
            sys.modules["psycopg"] = None          # from now on "import psycopg" fails
            sys.modules["globus_sdk"] = None
            try:
                import psycopg
            except ImportError:
                print("psycopg cannot be imported")
            from pcldb.config import Settings
            from pcldb.errors import PclError
            from pcldb.store import open_store
            folder = tempfile.mkdtemp(prefix="pcldb bare ")
            try:
                settings = Settings.for_test(folder, engine="sqlite")
                store = open_store(settings)
                store.initialize()
                with store.app() as db:
                    db.insert("pcl.session_rollup", {
                        "session_path": "raw/x/y", "labels": ["a"], "label_counts": [],
                        "models": [], "problems": [], "review_state": "none",
                        "n_needs_review": 0, "last_run_utc": "2026-11-17T03:50:15Z"})
                with store.app() as db:
                    print(db.one("SELECT labels, last_run_utc FROM {pcl}.session_rollup"))
                print(store.describe()["engine"], store.is_initialized())
                try:
                    open_store(settings.changed({"database.engine": "postgresql"})).initialize()
                except PclError as error:
                    print("postgresql:", error.code, "|", error)
                loaded = sorted(name for name, module in sys.modules.items()
                                if module is not None and name.split(".")[0] in
                                ("psycopg", "psycopg_binary", "globus_sdk"))
                print("loaded:", loaded)
            finally:
                shutil.rmtree(folder, ignore_errors=True)
        """)
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=str(PROJECT),
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "psycopg cannot be imported")
        self.assertEqual(lines[1], "{'labels': ['a'], 'last_run_utc': '2026-11-17T03:50:15.000Z'}")
        self.assertEqual(lines[2], "sqlite True")
        self.assertTrue(lines[3].startswith("postgresql: psycopg_missing |"), lines[3])
        self.assertIn("pcldb setup --engine sqlite", lines[3])
        self.assertEqual(lines[4], "loaded: []")
        self.assertEqual(result.stderr, "")

    def test_importing_the_package_loads_nothing_heavy(self):
        script = ("import sys; import pcldb, pcldb.config, pcldb.util, pcldb.errors, pcldb.store, "
                  "pcldb.store.schema_def, pcldb.store.pgcluster, pcldb.store.postgres_engine, "
                  "pcldb.store.sqlite_engine, pcldb.store.pg_hardening; "
                  "print(pcldb.__version__, sorted(n for n in sys.modules if n.split('.')[0] in "
                  "('psycopg', 'psycopg_binary', 'globus_sdk', 'sqlite3_never')))")
        result = subprocess.run([sys.executable, "-B", "-c", script], cwd=str(PROJECT),
                                capture_output=True, text=True, timeout=120)
        self.assertEqual((result.returncode, result.stdout.strip(), result.stderr),
                         (0, "0.1.0 []", ""))


class TestOpenStore(unittest.TestCase):
    def test_the_engine_setting_chooses_the_class(self):
        with support.TempProject("sqlite") as project:
            store = open_store(project.settings)
            self.assertEqual(type(store).__name__, "SqliteStore")
            self.assertEqual(store.engine, "sqlite")
            other = open_store(project.settings.changed({"database.engine": "postgresql"}))
            self.assertEqual(type(other).__name__, "PostgresStore")
            self.assertEqual(other.engine, "postgresql")
            self.assertIsInstance(store, base.Store)
            self.assertIsInstance(other, base.Store)

    def test_the_reader_comes_from_the_sandbox_modules_through_a_lazy_import(self):
        calls = []
        fake = mock.Mock()
        fake.open_reader.side_effect = lambda store: calls.append(store) or "the reader"
        with support.TempProject("sqlite") as project:
            store = open_store(project.settings)
            other = open_store(project.settings.changed({"database.engine": "postgresql"}))
            with mock.patch.dict(sys.modules, {"pcldb.store.reader_sqlite": fake,
                                               "pcldb.store.reader_postgres": fake}):
                self.assertEqual(store.reader(), "the reader")
                self.assertEqual(other.reader(), "the reader")
            self.assertEqual(calls, [store, other])


# ---------------------------------------------------------------------- PostgreSQL only


class TestPostgresOnly(support.PostgresCase, unittest.TestCase):
    def connect(self, role, **more):
        import psycopg
        connection = psycopg.connect(self.store.conninfo(role), **more)
        self.addCleanup(connection.close)
        return connection

    def test_connection_strings_name_everything_and_the_app_runs_as_pcl_app(self):
        from psycopg.conninfo import conninfo_to_dict
        self.assertEqual(self.store.mode, "external")
        self.assertEqual(self.store.database_name, self._database)
        for role, user in (("owner", "pcl_owner"), ("app", "pcl_app"), ("reader", "pcl_reader")):
            parts = conninfo_to_dict(self.store.conninfo(role))
            self.assertEqual((parts["host"], parts["user"], parts["dbname"]),
                             ("127.0.0.1", user, self._database))
            self.assertEqual(str(parts["port"]), str(self._cluster.port))
            self.assertEqual(str(parts["connect_timeout"]), "5")
            self.assertEqual(parts["application_name"], "pcldb-" + role)
            self.assertTrue(parts["password"])
        with self.assertRaises(ValueError):
            self.store.conninfo("root")
        with self.store.app() as db:
            row = db.one("SELECT current_user AS who, session_user AS signed_in, "
                         "current_setting('application_name') AS name, "
                         "current_database() AS base")
        self.assertEqual(row, {"who": "pcl_app", "signed_in": "pcl_app", "name": "pcldb-app",
                               "base": self._database})

    def test_the_reader_role_reads_the_catalogue_and_nothing_else(self):
        import psycopg
        with self.store.app() as db:
            keys = support.seed_minimal(db)
        reader = self.connect("reader", autocommit=True)
        self.assertEqual(reader.execute("SELECT current_user").fetchone()[0], "pcl_reader")
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM pcl.sessions").fetchone()[0], 2)
        # The role's RIGHTS are the boundary. A read-only default (the hardening sets one) is a
        # second layer that a session can switch off, so it is switched off for this test.
        reader.execute("SET default_transaction_read_only = off")
        for relation in schema_def.relations("pcl"):
            reader.execute("SELECT * FROM %s" % relation.qualified).fetchall()
        refused = [
            # Nothing of the private schema, not even its existence check through a view.
            *["SELECT * FROM %s" % table.qualified for table in schema_def.relations("app")],
            "SELECT COUNT(*) FROM app.users",
            "INSERT INTO app.users (user_id, name, role, disabled, created_utc) "
            "VALUES ('x', 'x', 'admin', false, now())",
            "UPDATE app.users SET role = 'admin'",
            "DELETE FROM app.tokens",
            # No writing in the catalogue.
            "INSERT INTO pcl.meta (key, value) VALUES ('k', 'v')",
            "UPDATE pcl.sessions SET notes = 'changed'",
            "DELETE FROM pcl.files",
            "TRUNCATE pcl.files",
            "UPDATE pcl.current_runs SET model_name = 'x'",
            "SELECT * FROM pcl.sessions FOR UPDATE",
            # No new objects anywhere.
            "CREATE TABLE pcl.mine (a int)",
            "CREATE TABLE app.mine (a int)",
            "CREATE TABLE public.mine (a int)",
            "CREATE TABLE mine (a int)",
            "CREATE TEMP TABLE mine (a int)",
            "CREATE SCHEMA mine",
            "CREATE VIEW pcl.mine AS SELECT 1",
            "DROP TABLE pcl.files",
            "ALTER TABLE pcl.files ADD COLUMN mine int",
            "COMMENT ON TABLE pcl.files IS 'mine'",
            # No code, no other role.
            "DO $$ BEGIN PERFORM 1; END $$",
            "CREATE FUNCTION pcl.mine() RETURNS int LANGUAGE sql AS 'SELECT 1'",
            "SET ROLE pcl_app",
            "SET ROLE pcl_owner",
            "SET SESSION AUTHORIZATION pcl_owner",
        ]
        for statement in refused:
            with self.assertRaises(psycopg.errors.InsufficientPrivilege, msg=statement):
                reader.execute(statement)
        # Passing its own right on is not an error in PostgreSQL, only a warning: nothing is given.
        reader.execute("GRANT SELECT ON pcl.files TO PUBLIC")
        self.assertEqual(self.outside_rows(
            "SELECT has_table_privilege('public', 'pcl.files', 'SELECT')"), [(False,)])
        self.assertEqual(self.outside_rows("SELECT COUNT(*) FROM {pcl}.files"), [(8,)])
        self.assertEqual(self.outside_rows("SELECT role FROM {app}.users"), [("admin",)])
        self.assertEqual(self.outside_rows("SELECT COUNT(*) FROM {app}.tokens"), [(1,)])
        self.assertIsNotNone(keys)

    def test_the_app_role_changes_rows_but_not_the_structure(self):
        with self.store.app() as db:
            support.seed_minimal(db)
        for statement in ("CREATE TABLE {pcl}.mine (a int)", "CREATE TABLE {app}.mine (a int)",
                          "DROP TABLE {pcl}.files", "ALTER TABLE {app}.users ADD COLUMN x int",
                          "TRUNCATE {pcl}.files", "CREATE SCHEMA mine",
                          "DROP VIEW {pcl}.current_runs", "CREATE TEMP TABLE mine (a int)",
                          "DELETE FROM {pcl}.current_runs",
                          "CREATE INDEX mine ON {pcl}.files (bytes)"):
            with self.assertRaises(DatabaseError, msg=statement) as caught:
                with self.store.app() as db:
                    db.execute(statement)
            self.assertRegex(str(caught.exception), "denied|must be owner", statement)
        self.assertEqual(self.outside_rows("SELECT COUNT(*) FROM {pcl}.files"), [(8,)])
        with self.store.app() as db:
            for table in schema_def.TABLES:             # every table: read and write
                db.query("SELECT * FROM {%s}.%s" % (table.schema, table.name))
                db.execute("DELETE FROM {%s}.%s WHERE 1 = 0" % (table.schema, table.name))
            for view in schema_def.VIEWS:
                db.query("SELECT * FROM {pcl}.%s" % view.name)

    def test_initialize_applies_the_rights_again(self):
        import psycopg
        owner = self.connect("owner", autocommit=True)
        owner.execute("REVOKE ALL ON ALL TABLES IN SCHEMA pcl FROM pcl_reader, pcl_app")
        owner.execute("GRANT ALL ON SCHEMA app TO pcl_reader")
        owner.execute("GRANT SELECT ON app.users TO pcl_reader")
        reader = self.connect("reader", autocommit=True)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            reader.execute("SELECT COUNT(*) FROM pcl.sessions")
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM app.users").fetchone()[0], 0)
        self.store.initialize()
        self.assertEqual(reader.execute("SELECT COUNT(*) FROM pcl.sessions").fetchone()[0], 0)
        with self.assertRaises(psycopg.errors.InsufficientPrivilege):
            reader.execute("SELECT COUNT(*) FROM app.users")
        with self.store.app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 0)

    def test_public_gets_nothing_in_this_database(self):
        owner = self.connect("owner", autocommit=True)
        self.assertEqual(owner.execute(
            "SELECT has_database_privilege('public', current_database(), 'CONNECT'), "
            "has_database_privilege('public', current_database(), 'TEMP'), "
            "has_schema_privilege('public', 'pcl', 'USAGE'), "
            "has_schema_privilege('public', 'app', 'USAGE'), "
            "has_language_privilege('public', 'plpgsql', 'USAGE')").fetchone(),
            (False, False, False, False, False))
        self.assertEqual(owner.execute(
            "SELECT has_database_privilege('pcl_reader', current_database(), 'CONNECT'), "
            "has_database_privilege('pcl_reader', current_database(), 'TEMP'), "
            "has_schema_privilege('pcl_reader', 'pcl', 'USAGE'), "
            "has_schema_privilege('pcl_reader', 'pcl', 'CREATE'), "
            "has_schema_privilege('pcl_reader', 'app', 'USAGE'), "
            "has_database_privilege('pcl_app', current_database(), 'TEMP'), "
            "has_schema_privilege('pcl_app', 'pcl', 'CREATE')").fetchone(),
            (True, False, True, False, False, False, False))
        members = owner.execute(
            "SELECT COUNT(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid = m.member "
            "WHERE r.rolname IN ('pcl_reader', 'pcl_app')").fetchone()[0]
        self.assertEqual(members, 0)
        flags = owner.execute(
            "SELECT rolname, rolsuper, rolcreatedb, rolcreaterole, rolcanlogin FROM pg_roles "
            "WHERE rolname IN ('pcl_reader', 'pcl_app') ORDER BY rolname").fetchall()
        self.assertEqual(flags, [("pcl_app", False, False, False, True),
                                 ("pcl_reader", False, False, False, True)])

    def external(self, **changes):
        return open_store(self.settings.changed({
            "database.postgresql.external.%s" % role: value for role, value in changes.items()}))

    def test_errors_never_repeat_a_password_or_a_connection_string(self):
        from psycopg.conninfo import conninfo_to_dict, make_conninfo
        good = self.store.conninfo("app")
        password = conninfo_to_dict(good)["password"]
        cases = {
            "not a connection string": "postgres//pcl_app:hunter2@127.0.0.1/db",
            "wrong password": make_conninfo(good, password="hunter2-wrong"),
            "nothing listens": make_conninfo(good, port=support.free_port(), password="hunter2"),
            "no such database": make_conninfo(good, dbname="no_such_db_hunter2"),
            "no host": "dbname=x user=pcl_app password=hunter2",
        }
        codes = {}
        for name, text in cases.items():
            store = self.external(app=text)
            for action in (lambda: store.app().__enter__(), store.is_initialized,
                           lambda: store.conninfo("app")):
                try:
                    action()
                except PclError as error:
                    shown = "%s %r %r" % (error, error.details, error.to_dict())
                    self.assertNotIn("hunter2", shown.replace("no_such_db_hunter2", ""), name)
                    self.assertNotIn(password, shown, name)
                    self.assertNotIn(text, shown, name)
                    codes.setdefault(name, error.code)
            self.assertEqual(store.describe()["engine"], "postgresql")
            self.assertNotIn("hunter2", json.dumps(store.describe()).replace(
                "no_such_db_hunter2", ""))
        self.assertEqual(codes, {"not a connection string": "bad_setting",
                                 "wrong password": "db_signin_refused",
                                 "nothing listens": "db_down",
                                 "no such database": "db_missing", "no host": "bad_setting"})

    def test_a_server_that_does_not_answer_is_unavailable_not_uninitialized(self):
        from psycopg.conninfo import make_conninfo
        closed = support.free_port()
        store = self.external(**{role: make_conninfo(self.store.conninfo(role), port=closed)
                                 for role in ("owner", "app", "reader")})
        for action in (store.is_initialized, store.initialize, lambda: store.app().__enter__()):
            with self.assertRaises(Unavailable) as caught:
                action()
            self.assertEqual(caught.exception.code, "db_down")
            self.assertEqual(caught.exception.status, 503)
            self.assertIn(str(closed), str(caught.exception))
        self.assertEqual(store.describe(), {"engine": "postgresql", "version": "unknown",
                                            "where": "127.0.0.1, port %d, database %s"
                                                     % (closed, self._database)})

    def test_a_missing_database_is_made_by_initialize(self):
        from psycopg.conninfo import make_conninfo
        name = "t_made_" + util.new_id()[:8]
        self.addCleanup(self._cluster.drop_database, name)
        store = self.external(**{role: make_conninfo(self.store.conninfo(role), dbname=name)
                                 for role in ("owner", "app", "reader")})
        self.assertFalse(store.is_initialized())
        with self.assertRaises(Unavailable) as caught:
            store.app().__enter__()
        self.assertEqual(caught.exception.code, "db_missing")
        self.assertIn("pcldb setup", str(caught.exception))
        store.initialize()
        self.assertTrue(store.is_initialized())
        self.assertEqual(store.database_name, name)
        with store.app() as db:
            self.assertEqual(db.scalar("SELECT current_database()"), name)

    def test_external_mode_needs_the_three_strings_and_the_fixed_role_names(self):
        from psycopg.conninfo import make_conninfo
        missing = open_store(self.settings.changed({"database.postgresql.external.reader": None}))
        with self.assertRaises(PclError) as caught:
            missing.conninfo("reader")
        self.assertIn("database.postgresql.external.reader", str(caught.exception))
        self.assertIn("PCLDB_PG_READER_DSN", str(caught.exception))
        missing.conninfo("app")                                 # the others still work
        wrong_user = self.external(app=self.store.conninfo("owner"))
        with self.assertRaises(PclError) as caught:
            wrong_user.initialize()
        self.assertIn("pcl_app", str(caught.exception))
        self.assertEqual(caught.exception.code, "bad_setting")
        # A connection string given as an address works too, and gets the port by default.
        from psycopg.conninfo import conninfo_to_dict
        parts = conninfo_to_dict(self.store.conninfo("app"))
        address = "postgresql://%s:%s@%s:%s/%s" % (parts["user"], parts["password"],
                                                   parts["host"], parts["port"], parts["dbname"])
        with self.external(app=address).app() as db:
            self.assertEqual(db.scalar("SELECT current_user"), "pcl_app")
        self.assertEqual(conninfo_to_dict(open_store(self.settings.changed({
            "database.postgresql.external.app": "host=h dbname=d user=pcl_app"})).conninfo("app"))
            ["port"], "5432")
        self.assertIsNotNone(make_conninfo)

    def test_pg_variables_of_the_environment_do_not_change_where_it_connects(self):
        nonsense = {"PGHOST": "192.0.2.1", "PGPORT": "1", "PGUSER": "nobody",
                    "PGPASSWORD": "wrong", "PGDATABASE": "nothing", "PGCLIENTENCODING": "LATIN1",
                    "PGAPPNAME": "other", "PGCONNECT_TIMEOUT": "1"}
        with mock.patch.dict(os.environ, nonsense):
            store = open_store(self.settings)
            self.assertTrue(store.is_initialized())
            with store.app() as db:
                db.insert("pcl.meta", {"key": "k", "value": "caf\u00e9 \u6f22"})
                row = db.one("SELECT current_user AS who, current_database() AS base, "
                             "current_setting('client_encoding') AS encoding, "
                             "current_setting('application_name') AS name")
                self.assertEqual(db.scalar("SELECT value FROM {pcl}.meta WHERE key = ?", ["k"]),
                                 "caf\u00e9 \u6f22")
            store.initialize()
        self.assertEqual(row, {"who": "pcl_app", "base": self._database, "encoding": "UTF8",
                               "name": "pcldb-app"})

    def test_times_are_utc_whatever_the_session_time_zone_is(self):
        owner = self.connect("owner", autocommit=True)
        owner.execute("ALTER ROLE pcl_app SET timezone = 'Asia/Kolkata'")
        self.addCleanup(owner.execute, "ALTER ROLE pcl_app RESET timezone")
        with self.store.app() as db:
            self.assertEqual(db.scalar("SELECT current_setting('TimeZone')"), "Asia/Kolkata")
            db.insert("pcl.sessions", support.session_row(
                A, started_utc="2026-11-17T03:50:15.281Z"))
            self.assertEqual(db.scalar("SELECT started_utc FROM {pcl}.sessions"),
                             "2026-11-17T03:50:15.281Z")
            self.assertEqual(db.scalar(
                "SELECT COUNT(*) FROM {pcl}.sessions WHERE started_utc = ?",
                ["2026-11-17T03:50:15.281Z"]), 1)
            self.assertEqual(db.scalar("SELECT MAX(started_utc) FROM {pcl}.sessions"),
                             "2026-11-17T03:50:15.281Z")

    def test_psycopg_missing_is_said_in_plain_words(self):
        from pcldb.store import postgres_engine
        with mock.patch.dict(sys.modules, {"psycopg": None}):
            for action in (self.store.initialize, self.store.is_initialized,
                           lambda: self.store.app().__enter__(),
                           lambda: self.store.conninfo("app"), postgres_engine.load_psycopg):
                with self.assertRaises(PclError) as caught:
                    action()
                self.assertEqual(caught.exception.code, "psycopg_missing")
                self.assertIn("install.py", str(caught.exception))


class TestPostgresLocalMode(unittest.TestCase):
    """The whole local path: cluster, generated passwords, roles, database, start and stop."""

    @classmethod
    def setUpClass(cls):
        support.require_postgres()

    def test_setup_run_stop_start(self):
        from pcldb.store import pgcluster
        project = support.TempProject("postgresql")
        self.addCleanup(project.cleanup)
        settings = project.settings
        self.assertEqual(settings.database.postgresql.mode, "local")
        cluster = pgcluster.LocalCluster(settings, test_mode=True)
        self.addCleanup(cluster.destroy)
        store = open_store(settings)

        self.assertFalse(store.is_initialized())            # nothing there yet: no error
        self.assertFalse((settings.var_dir / "pgdata").exists())
        with self.assertRaises(PclError) as caught:
            store.conninfo("app")
        self.assertEqual(caught.exception.code, "db_secrets_missing")

        cluster.init()                                      # fast: the test flavour of initdb
        with self.assertRaises(Unavailable) as caught:      # a cluster, but stopped: nobody
            store.is_initialized()                          # can tell, and the error says so
        self.assertEqual(caught.exception.code, "db_down")
        store.initialize()                                  # starts it, makes roles and database
        self.assertTrue(store.is_initialized())
        self.assertTrue(cluster.status()["running"])

        passwords = pgcluster.read_local_secrets(settings)
        self.assertEqual(sorted(passwords), ["app", "owner", "reader"])
        self.assertEqual(len(set(passwords.values())), 3)
        for value in passwords.values():
            self.assertGreaterEqual(len(value), 24)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(settings.local_secrets_path.stat().st_mode), 0o600)

        described = store.describe()
        self.assertEqual(described["where"],
                         "this computer, port %d" % settings.database.postgresql.port)
        self.assertTrue(cluster.pg_bin.version.startswith(described["version"].split(".")[0]))
        self.assertEqual(store.database_name, "pcl")
        with store.app() as db:
            support.seed_minimal(db)
            self.assertEqual(db.scalar("SELECT current_user"), "pcl_app")
            self.assertEqual(db.scalar("SELECT current_database()"), "pcl")
        store.initialize()                                  # again: nothing breaks, rows stay

        import psycopg
        reader = psycopg.connect(store.conninfo("reader"), autocommit=True)
        try:
            self.assertEqual(reader.execute("SELECT COUNT(*) FROM pcl.sessions").fetchone()[0], 2)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                reader.execute("SELECT * FROM app.tokens")
        finally:
            reader.close()

        # No password was written anywhere but the passwords file.
        for path in settings.var_dir.rglob("*"):
            if path.is_file() and path != settings.local_secrets_path \
                    and path.stat().st_size < 5_000_000:
                content = path.read_bytes()
                for role, value in passwords.items():
                    self.assertNotIn(value.encode(), content, "%s password in %s" % (role, path))

        self.assertTrue(cluster.stop())
        for action in (store.is_initialized, lambda: store.app().__enter__()):
            with self.assertRaises(Unavailable) as caught:
                action()
            self.assertEqual(caught.exception.code, "db_down")
            self.assertIn("pcldb db start", str(caught.exception))
        self.assertEqual(store.describe()["version"], described["version"])  # remembered

        self.assertTrue(cluster.ensure_running())
        with store.app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 2)
        store.reset()
        with store.app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 0)
        store.destroy()
        self.assertFalse(open_store(settings).is_initialized())
        self.assertTrue(cluster.status()["running"])        # destroy drops tables, not the server


if __name__ == "__main__":
    unittest.main()
