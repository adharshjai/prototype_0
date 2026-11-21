"""Dump, restore and the one-file export (pcldb/transfer.py): on each engine and across them."""

from __future__ import annotations

import io
import json
import os
import sqlite3
import stat
import types
import unittest

from pcldb import reconcile, transfer, util
from pcldb.errors import Conflict, PclError
from pcldb.store import open_store, schema_def
from tests import support
from tests import test_reconcile as index_cases

TOKEN_TEXT = "pcl_" + "t" * 43
ODD_TEXT = "SELECT 'café ☃ line break \U0001f52c' -- 100%"
PRIVATE_TABLES = {"users", "tokens", "registrations", "query_log", "search_entries"}


def new_store(test, engine):
    """(store, settings, project): a fresh, initialised, empty store of one engine."""
    if engine == "sqlite":
        project = support.TempProject("sqlite")
        test.addCleanup(project.cleanup)
        store = open_store(project.settings)
        store.initialize()
    else:
        cluster = support.shared_cluster()
        database = cluster.new_database()
        test.addCleanup(cluster.drop_database, database)
        project = support.TempProject("postgresql", cluster.external_changes(database))
        test.addCleanup(project.cleanup)
        store = open_store(project.settings)
        cluster.make_empty(store)
    return store, project.settings, project


def fill(store, settings, folder):
    """Rows in every table: a small index, a user with a token, and two log rows."""
    path = index_cases.write_small_index(folder / "fill_index.sqlite")
    with store.app() as db:
        reconcile.load_index(db, path, actor="demo-operator-a", settings=settings)
        user_id = util.new_id()
        db.insert("app.users", {"user_id": user_id, "name": "demo-operator-a",
                                "display_name": "Demo operator A", "role": "admin",
                                "disabled": False, "created_utc": "2026-11-19T02:30:00.000Z"})
        db.insert("app.tokens", {
            "token_id": util.new_id(), "user_id": user_id,
            "token_sha256": util.sha256_hex(TOKEN_TEXT.encode("utf-8")), "label": "tests",
            "created_utc": "2026-11-19T02:30:00.000Z", "expires_utc": None,
            "revoked_utc": None, "last_used_utc": "2026-11-19T02:31:00.000Z"})
        db.insert("app.query_log", {
            "log_id": util.new_id(), "at_utc": "2026-11-19T03:00:00.123Z",
            "user_name": "demo-operator-a", "mode": "sql", "query_text": ODD_TEXT,
            "request_json": None, "ok": True, "error_text": None, "n_rows": 3, "elapsed_ms": 12,
            "backend": store.engine})
        db.insert("app.query_log", {
            "log_id": util.new_id(), "at_utc": "2026-11-19T03:01:00.000Z", "user_name": None,
            "mode": "ask", "query_text": "adenine last week",
            # Numbers that the two engines hand back in different Python types.
            "request_json": {"q": "*", "limit": 10, "filters": [], "score": 0.25,
                             "numbers": [1e16, -2.5e20, 1e15, 3.0, 1e-07, 10 ** 30, 0.1 + 0.2]},
            "ok": False, "error_text": "nothing answered", "n_rows": None, "elapsed_ms": None,
            "backend": "local"})
        db.upsert("pcl.meta", {"key": "demo", "value": "0"}, key=("key",))
    return path


def snapshot(store):
    with store.app() as db:
        return transfer.read_tables(db)


def row_lines(path):
    """Every line of a dump after the header."""
    with open(path, encoding="utf-8", newline="\n") as handle:
        return handle.read().split("\n")[1:]


def rewrite(source, target, edit):
    """Copy a dump, letting `edit(lines)` change its list of lines (the header is lines[0])."""
    with open(source, encoding="utf-8", newline="\n") as handle:
        lines = handle.read().split("\n")
    lines = edit(lines) or lines
    with open(target, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines))
    return target


def export_rows(path):
    """{table: raw rows} and the definitions of an exported file, read with plain sqlite3."""
    connection = sqlite3.connect(str(path))
    try:
        master = connection.execute(
            "SELECT type, name, sql FROM sqlite_master WHERE sql IS NOT NULL").fetchall()
        tables = {}
        for kind, name, _sql in master:
            if kind == "table":
                definition = schema_def.table("pcl." + name)
                tables[name] = connection.execute('SELECT * FROM "%s" ORDER BY %s' % (
                    name, ", ".join(definition.primary_key))).fetchall()
        return tables, master
    finally:
        connection.close()


class TransferCases:
    """The same tests on both engines."""

    def setUp(self):
        super().setUp()
        self.work = self.project.path / "work"
        self.work.mkdir()
        fill(self.store, self.settings, self.work)
        self.dump_path = self.work / "dump.jsonl"

    # -- dump ------------------------------------------------------------------------------------

    def test_dump_is_a_header_and_one_line_per_row(self):
        result = transfer.dump(self.store, self.dump_path)
        data = self.dump_path.read_bytes()
        self.assertTrue(data.isascii())
        self.assertTrue(data.endswith(b"\n"))
        self.assertNotIn(b"\r", data)
        lines = data.decode("ascii").split("\n")[:-1]
        header = json.loads(lines[0])
        self.assertEqual(header["format"], "pcldb-dump/1")
        self.assertEqual(header["schema_version"], schema_def.SCHEMA_VERSION)
        self.assertEqual(header["engine"], self.engine)
        self.assertEqual(header["tables"], result["tables"])
        self.assertEqual(list(header["tables"]), sorted(header["tables"]))
        self.assertEqual(set(header["tables"]), {table.qualified for table in schema_def.TABLES})
        self.assertEqual(len(lines) - 1, result["rows"])
        self.assertEqual(result["rows"], sum(header["tables"].values()))
        self.assertEqual((result["tables"]["pcl.sessions"], result["tables"]["pcl.points"],
                          result["tables"]["app.users"], result["tables"]["app.tokens"],
                          result["tables"]["app.query_log"], result["tables"]["app.search_entries"]),
                         (2, 14, 1, 1, 2, 2))
        order = []
        for text in lines[1:]:
            item = json.loads(text)
            self.assertEqual(set(item), {"table", "row"})
            if not order or order[-1] != item["table"]:
                order.append(item["table"])
        self.assertEqual(order, [table.qualified for table in schema_def.TABLES
                                 if header["tables"][table.qualified]])
        # Only the SHA-256 of a token is in the store, so only that can be in a dump.
        self.assertNotIn(TOKEN_TEXT.encode("ascii"), data)
        self.assertIn(util.sha256_hex(TOKEN_TEXT.encode("utf-8")).encode("ascii"), data)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.dump_path.stat().st_mode), 0o600)
        # A whole number that Python writes as 1e+16 is written the way both engines agree on.
        self.assertIn(b'"numbers":[10000000000000000,-250000000000000000000,'
                      b'1000000000000000.0,3.0,1e-07,1000000000000000000000000000000,'
                      b'0.30000000000000004]', data)
        # The normalised values, not an engine's own form.
        header, tables = transfer.read_dump(self.dump_path)
        session = {row["path"]: row for row in tables["pcl.sessions"]}[index_cases.A]
        self.assertIs(session["simulated"], True)
        self.assertEqual(session["sample_ids"], ["S-130"])
        self.assertEqual(session["started_utc"], "2026-11-16T22:50:15.281Z")
        self.assertIsInstance(session["card_json"], dict)
        self.assertIn(ODD_TEXT, [row["query_text"] for row in tables["app.query_log"]])

    def test_dump_can_leave_the_users_out(self):
        result = transfer.dump(self.store, self.dump_path, include_users=False)
        self.assertEqual((result["tables"]["app.users"], result["tables"]["app.tokens"]), (0, 0))
        header, tables = transfer.read_dump(self.dump_path)
        self.assertIs(header["includes_users"], False)
        self.assertEqual(tables["app.users"], [])
        self.assertNotIn(util.sha256_hex(TOKEN_TEXT.encode("utf-8")),
                         self.dump_path.read_text(encoding="utf-8"))

    # -- restore ---------------------------------------------------------------------------------

    def test_round_trip_on_this_engine(self):
        transfer.dump(self.store, self.dump_path)
        other, _settings, _project = new_store(self, self.engine)
        result = transfer.restore(other, self.dump_path, include_users=True)
        self.assertEqual(result["skipped"], {})
        self.assertEqual(result["dump_engine"], self.engine)
        self.assertEqual(snapshot(other), snapshot(self.store))
        second = self.work / "second.jsonl"
        transfer.dump(other, second)
        self.assertEqual(row_lines(second), row_lines(self.dump_path))
        # The views answer the same on the copy.
        for sql in ("SELECT session_path, label, n FROM {pcl}.session_labels",
                    "SELECT session_path, point_id, effective_label, awaiting_review "
                    "FROM {pcl}.current_points"):
            answers = []
            for store in (self.store, other):
                with store.app() as db:
                    answers.append(sorted(json.dumps(row, sort_keys=True)
                                          for row in db.query(sql)))
            self.assertEqual(answers[0], answers[1])
            self.assertTrue(answers[0])

    def test_users_and_tokens_are_skipped_unless_asked(self):
        transfer.dump(self.store, self.dump_path)
        other, _settings, _project = new_store(self, self.engine)
        with other.app() as db:
            db.insert("app.users", {"user_id": util.new_id(), "name": "demo-operator-b",
                                    "display_name": None, "role": "admin", "disabled": False,
                                    "created_utc": "2026-11-20T00:00:00.000Z"})
            # A log that is already there does not stand in the way either.
            db.insert("app.query_log", {
                "log_id": util.new_id(), "at_utc": "2026-11-20T00:00:01.000Z",
                "user_name": "demo-operator-b", "mode": "sql", "query_text": "SELECT 1",
                "ok": True})
        result = transfer.restore(other, self.dump_path)
        self.assertEqual(result["skipped"], {"app.users": 1, "app.tokens": 1})
        self.assertEqual((result["tables"]["app.users"], result["tables"]["app.tokens"]), (0, 0))
        after = snapshot(other)
        self.assertEqual([row["name"] for row in after["app.users"]], ["demo-operator-b"])
        self.assertEqual(after["app.tokens"], [])
        self.assertEqual(len(after["app.query_log"]), 3)
        source = snapshot(self.store)
        for name in ("pcl.sessions", "pcl.files", "pcl.runs", "pcl.points", "pcl.reviews",
                     "pcl.models", "pcl.problems", "pcl.session_rollup", "pcl.meta",
                     "app.registrations", "app.search_entries"):
            self.assertEqual(after[name], source[name], name)

    def assert_refused(self, store, path, code, mentions=(), error=PclError, **arguments):
        before = snapshot(store)
        with self.assertRaises(error) as caught:
            transfer.restore(store, path, **arguments)
        self.assertEqual(caught.exception.code, code, caught.exception.message)
        for text in mentions:
            self.assertIn(text, caught.exception.message)
        self.assertEqual(snapshot(store), before, "a refused restore changed the database")
        return caught.exception

    def test_restore_refuses_a_store_that_is_not_empty(self):
        transfer.dump(self.store, self.dump_path)
        error = self.assert_refused(self.store, self.dump_path, "not_empty",
                                    ["pcl.sessions", "pcldb db reset --yes"], error=Conflict)
        self.assertEqual(error.status, 409)
        other, _settings, _project = new_store(self, self.engine)
        with other.app() as db:
            db.insert("app.users", {"user_id": util.new_id(), "name": "demo-operator-b",
                                    "role": "admin", "disabled": False,
                                    "created_utc": "2026-11-20T00:00:00.000Z"})
        self.assert_refused(other, self.dump_path, "not_empty", ["app.users", "--include-users"],
                            error=Conflict, include_users=True)
        with other.app() as db:
            db.delete_where("app.users", "1 = 1")
            db.insert("pcl.problems", {"problem_no": 1, "path": "raw/x", "code": "stray"})
        self.assert_refused(other, self.dump_path, "not_empty", ["pcl.problems"], error=Conflict)

    def test_include_users_replaces_the_admin_that_setup_made(self):
        # NEW-01: setup and db reset always make the user admin; --include-users must still
        # work on such a database, and the refusal must name a sequence that works.
        from pcldb import auth, cli
        transfer.dump(self.store, self.dump_path)
        other, other_settings, _project = new_store(self, self.engine)
        self.assertTrue(cli.ensure_admin(other, other_settings))
        admin_file = cli.admin_token_path(other_settings)
        with other.app() as db:     # a second admin token, saved under var/tokens
            info = auth.issue_token_info(db, "admin", label="second")
        second_file = auth.save_token_file(auth.token_file_path(other_settings, info),
                                           info["token"])
        out = io.StringIO()
        args = types.SimpleNamespace(file=str(self.dump_path), include_users=True)
        self.assertEqual(transfer.cmd_restore(args, other_settings, out=out), 0)
        text = out.getvalue()
        self.assertIn('The user "admin" that pcldb setup made was replaced', text)
        self.assertFalse(admin_file.exists())
        self.assertFalse(second_file.exists())
        tables = snapshot(other)
        self.assertEqual(tables["app.users"], snapshot(self.store)["app.users"])
        self.assertEqual(tables["app.tokens"], snapshot(self.store)["app.tokens"])
        with other.app() as db:
            self.assertEqual(auth.authenticate(db, TOKEN_TEXT).name, "demo-operator-a")

        # Without --include-users the setup admin stays.
        third, third_settings, _project = new_store(self, self.engine)
        cli.ensure_admin(third, third_settings)
        result = transfer.restore(third, self.dump_path)
        self.assertFalse(result["replaced_setup_admin"])
        self.assertEqual([row["name"] for row in snapshot(third)["app.users"]], ["admin"])
        self.assertTrue(cli.admin_token_path(third_settings).exists())

        # A second user next to the setup admin is refused, and nothing changes.
        fourth, fourth_settings, _project = new_store(self, self.engine)
        cli.ensure_admin(fourth, fourth_settings)
        with fourth.app() as db:
            auth.create_user(db, "someone", "viewer")
        error = self.assert_refused(fourth, self.dump_path, "not_empty",
                                    ["app.users", "pcldb db reset --yes"], error=Conflict,
                                    include_users=True)
        self.assertIn("other than the admin that pcldb setup made", error.message)

    def test_restore_refuses_another_schema_version_and_what_is_no_dump(self):
        transfer.dump(self.store, self.dump_path)
        other, _settings, _project = new_store(self, self.engine)

        def version(number):
            def edit(lines):
                header = json.loads(lines[0])
                header["schema_version"] = number
                lines[0] = json.dumps(header)
            return edit

        for number in (2, 0, "1", True, None):
            with self.subTest(schema_version=number):
                path = rewrite(self.dump_path, self.work / "other_version.jsonl", version(number))
                error = self.assert_refused(other, path, "dump_version",
                                            ["schema version", json.dumps(number)])
                self.assertIn("Nothing was restored", error.message)
        cases = {
            "empty.jsonl": "",
            "words.jsonl": "these are just words\n",
            "other_json.jsonl": '{"format": "something-else/1"}\n',
            "no_counts.jsonl": '{"format": "pcldb-dump/1", "schema_version": 1}\n',
            "list.jsonl": "[1, 2, 3]\n",
        }
        for name, text in cases.items():
            with self.subTest(file=name):
                path = self.work / name
                path.write_text(text, encoding="utf-8")
                self.assert_refused(other, path, "bad_dump", [name])
        binary = self.work / "binary.jsonl"
        binary.write_bytes(b"\xff\xfe\x00\x01" * 20)
        self.assert_refused(other, binary, "bad_dump")
        self.assert_refused(other, self.work / "not_there.jsonl", "file_missing")

    def test_restore_refuses_a_damaged_dump_and_writes_nothing(self):
        transfer.dump(self.store, self.dump_path)
        other, _settings, _project = new_store(self, self.engine)

        def first_line_of(table, lines):
            return next(number for number, text in enumerate(lines)
                        if number and json.loads(text)["table"] == table)

        def cut_short(lines):
            return lines[:-40]

        def unknown_column(lines):
            number = first_line_of("pcl.files", lines)
            item = json.loads(lines[number])
            item["row"]["owner"] = "somebody"
            lines[number] = json.dumps(item)

        def too_big(lines):
            number = first_line_of("pcl.sessions", lines)
            item = json.loads(lines[number])
            item["row"]["items"] = 3000000000
            lines[number] = json.dumps(item)

        def wrong_type(lines):
            number = first_line_of("pcl.points", lines)
            item = json.loads(lines[number])
            item["row"]["needs_review"] = 1
            lines[number] = json.dumps(item)

        def empty_value(lines):
            number = first_line_of("pcl.runs", lines)
            item = json.loads(lines[number])
            item["row"]["valid"] = None
            lines[number] = json.dumps(item)

        def twice(lines):
            number = first_line_of("pcl.models", lines)
            header = json.loads(lines[0])
            header["tables"]["pcl.models"] += 1
            lines[0] = json.dumps(header)
            lines.insert(number, lines[number])

        def unknown_table(lines):
            lines.insert(3, json.dumps({"table": "pcl.secrets", "row": {"a": 1}}))

        def not_a_row(lines):
            lines.insert(3, json.dumps({"table": "pcl.files"}))

        def nan(lines):
            number = first_line_of("pcl.points", lines)
            lines[number] = lines[number].replace('"confidence":', '"confidence":NaN,"x":', 1)

        def broken_json(lines):
            lines[5] = lines[5][:-7]

        def extra_rows(lines):
            header = json.loads(lines[0])
            del header["tables"]["pcl.problems"]
            lines[0] = json.dumps(header)

        cases = [
            (cut_short, ["not complete", "cut short"]),
            (unknown_column, ["owner", "pcl.files"]),
            (too_big, ["pcl.sessions.items"]),
            (wrong_type, ["pcl.points.needs_review"]),
            (empty_value, ["pcl.runs.valid", "is empty"]),
            (twice, ["pcl.models", "twice"]),
            (unknown_table, ["pcl.secrets"]),
            (not_a_row, ["Line 4"]),
            (nan, ["NaN"]),
            (broken_json, ["Line 6", "not valid JSON"]),
            (extra_rows, ["pcl.problems", "does not list"]),
        ]
        for edit, mentions in cases:
            with self.subTest(case=edit.__name__):
                path = rewrite(self.dump_path, self.work / (edit.__name__ + ".jsonl"), edit)
                error = self.assert_refused(other, path, "bad_dump", mentions,
                                            include_users=True)
                self.assertIn("Nothing was restored", error.message)
        # The undamaged file still goes in afterwards: nothing was left behind.
        transfer.restore(other, self.dump_path, include_users=True)
        self.assertEqual(snapshot(other), snapshot(self.store))

    def test_a_dump_edited_on_windows_still_restores(self):
        transfer.dump(self.store, self.dump_path)
        text = self.dump_path.read_text(encoding="utf-8")
        edited = self.work / "windows.jsonl"
        edited.write_bytes(b"\xef\xbb\xbf" + text.replace("\n", "\r\n").encode("utf-8") + b"\r\n")
        other, _settings, _project = new_store(self, self.engine)
        transfer.restore(other, edited, include_users=True)
        self.assertEqual(snapshot(other), snapshot(self.store))

    # -- the one-file export ---------------------------------------------------------------------

    def test_export_is_one_plain_sqlite_file_of_the_catalogue(self):
        path = self.work / "export" / "catalogue.sqlite"
        result = transfer.export_sqlite(self.store, path)
        self.assertEqual(os.listdir(path.parent), ["catalogue.sqlite"])
        self.assertEqual(result["bytes"], path.stat().st_size)
        tables, master = export_rows(path)
        names = {(kind, name) for kind, name, _sql in master}
        wanted = {("table", table.name) for table in schema_def.TABLES if table.schema == "pcl"}
        wanted |= {("view", view.name) for view in schema_def.VIEWS}
        wanted |= {("index", index.name) for table in schema_def.TABLES
                   if table.schema == "pcl" for index in table.indexes}
        self.assertEqual(names, wanted)
        self.assertFalse({name for _kind, name in names} & PRIVATE_TABLES)
        self.assertEqual({sql for _kind, _name, sql in master},
                         set(schema_def.ddl("sqlite", "pcl", qualified=False)))
        self.assertEqual(result["tables"], {name: len(rows) for name, rows in tables.items()})
        self.assertEqual((result["tables"]["sessions"], result["tables"]["files"],
                          result["tables"]["points"]), (2, 9, 14))
        self.assertIn(("schema_version", str(schema_def.SCHEMA_VERSION)), tables["meta"])
        data = path.read_bytes()
        self.assertNotIn(util.sha256_hex(TOKEN_TEXT.encode("utf-8")).encode("ascii"), data)
        self.assertNotIn(b"adenine last week", data)

        # The same example queries, asked of the source and of the file.
        connection = sqlite3.connect(str(path))
        self.addCleanup(connection.close)
        with self.store.app() as db:
            labels = db.query("SELECT session_path, label, n FROM {pcl}.session_labels")
            points = db.query("SELECT session_path, point_id, effective_label, awaiting_review, "
                              "confidence FROM {pcl}.current_points")
            summary = db.query("SELECT path, review_state, n_needs_review, files, labels, "
                               "simulated, started FROM {pcl}.session_summary")
            per_instrument = db.query(
                "SELECT instrument, area, COUNT(*) AS sessions, MAX(started_utc) AS newest "
                "FROM {pcl}.sessions WHERE NOT withdrawn GROUP BY instrument, area")
        self.assertEqual(
            sorted(connection.execute("SELECT session_path, label, n FROM session_labels")),
            sorted((row["session_path"], row["label"], row["n"]) for row in labels))
        self.assertEqual(len(labels), 4)
        self.assertEqual(
            sorted(connection.execute("SELECT session_path, point_id, effective_label, "
                                      "awaiting_review, confidence FROM current_points"),
                   key=repr),
            sorted(((row["session_path"], row["point_id"], row["effective_label"],
                     int(row["awaiting_review"]), row["confidence"]) for row in points),
                   key=repr))
        self.assertEqual(
            sorted(connection.execute("SELECT path, review_state, n_needs_review, files, labels, "
                                      "simulated, started FROM session_summary")),
            sorted((row["path"], row["review_state"], row["n_needs_review"], row["files"],
                    json.dumps(row["labels"], separators=(",", ":")), int(row["simulated"]),
                    row["started"]) for row in summary))
        self.assertEqual(
            sorted(connection.execute(
                "SELECT instrument, area, COUNT(*), MAX(started_utc) FROM sessions "
                "WHERE NOT withdrawn GROUP BY instrument, area")),
            sorted((row["instrument"], row["area"], row["sessions"], row["newest"])
                   for row in per_instrument))

    def test_export_replaces_a_file_and_refuses_a_folder(self):
        path = self.work / "catalogue.sqlite"
        path.write_text("an older export", encoding="utf-8")
        transfer.export_sqlite(self.store, path)
        tables, _master = export_rows(path)
        self.assertEqual(len(tables["sessions"]), 2)
        transfer.export_sqlite(self.store, path)          # and again over a real export
        self.assertEqual(sorted(os.listdir(self.work)),
                         sorted(["catalogue.sqlite", "fill_index.sqlite"]))
        with self.assertRaises(PclError) as caught:
            transfer.export_sqlite(self.store, self.work)
        self.assertEqual(caught.exception.code, "bad_file")
        with self.assertRaises(PclError) as caught:
            transfer.dump(self.store, self.work)
        self.assertEqual(caught.exception.code, "bad_file")

    # -- the commands ----------------------------------------------------------------------------

    def test_commands_print_short_plain_summaries(self):
        out = io.StringIO()
        args = types.SimpleNamespace(file=str(self.dump_path), include_users=False)
        self.assertEqual(transfer.cmd_dump(args, self.settings, out=out), 0)
        text = out.getvalue()
        self.assertIn("56 rows from 15 tables", text)
        self.assertTrue(text.isascii())
        self.assertIn("engine %s, schema version 1" % self.engine, text)
        self.assertIn("pcldb restore", text)

        other, other_settings, _project = new_store(self, self.engine)
        out = io.StringIO()
        self.assertEqual(transfer.cmd_restore(args, other_settings, out=out), 0)
        text = out.getvalue()
        self.assertIn("into the %s database" % self.engine, text)
        self.assertIn("Sessions: 2. Files: 9. Runs: 8. Points: 14. Search entries: 2.", text)
        self.assertIn("Users and tokens were left out (2 rows)", text)
        self.assertEqual(len(snapshot(other)["pcl.sessions"]), 2)

        out = io.StringIO()
        export = self.work / "catalogue export.sqlite"
        self.assertEqual(transfer.cmd_export_sqlite(types.SimpleNamespace(file=str(export)),
                                                    self.settings, out=out), 0)
        text = out.getvalue()
        self.assertIn("Sessions: 2. Files: 9. Runs: 8. Points: 14.", text)
        self.assertIn('sqlite3 "%s"' % export, text)
        # NEW-09: it does hold operator names and notes, and is written for this account only
        self.assertIn("it does hold operator names and notes", text)
        self.assertNotIn("Nothing private", text)
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(export.stat().st_mode), 0o600)
        self.assertTrue(export.is_file())
        self.assertTrue(text.isascii())
        for command in (transfer.cmd_dump, transfer.cmd_restore, transfer.cmd_export_sqlite):
            with self.assertRaises(PclError) as caught:
                command(types.SimpleNamespace(file=None, include_users=False), self.settings,
                        out=out)
            self.assertEqual(caught.exception.code, "usage")


class TestTransferSqlite(TransferCases, support.SqliteCase, unittest.TestCase):

    def test_export_holds_exactly_what_the_engines_own_catalogue_file_holds(self):
        path = self.work / "catalogue.sqlite"
        transfer.export_sqlite(self.store, path)
        exported, exported_master = export_rows(path)
        own, own_master = export_rows(self.store.catalog_path)
        self.assertEqual(exported, own)
        self.assertEqual(sorted(exported_master), sorted(own_master))

    def test_export_refuses_the_stores_own_files(self):
        before = self.store.catalog_path.read_bytes()
        private = self.store.app_path.read_bytes()
        for own in (self.store.catalog_path, self.store.app_path):
            for write in (transfer.export_sqlite, transfer.dump):
                with self.assertRaises(PclError) as caught:
                    write(self.store, own)
                self.assertEqual(caught.exception.code, "bad_file")
        self.assertEqual(self.store.catalog_path.read_bytes(), before)
        self.assertEqual(self.store.app_path.read_bytes(), private)


class TestTransferPostgres(TransferCases, support.PostgresCase, unittest.TestCase):
    pass


class TestTransferAcrossEngines(unittest.TestCase):
    """SQLite -> dump -> PostgreSQL -> dump gives the same rows, and the other way round."""

    def setUp(self):
        support.require_postgres()

    def round_trip(self, first, second):
        source, settings, project = new_store(self, first)
        work = project.path / "work"
        work.mkdir()
        fill(source, settings, work)
        first_dump = work / "first.jsonl"
        transfer.dump(source, first_dump)

        middle, _settings, _project = new_store(self, second)
        result = transfer.restore(middle, first_dump, include_users=True)
        self.assertEqual(result["dump_engine"], first)
        second_dump = work / "second.jsonl"
        transfer.dump(middle, second_dump)
        self.assertEqual(row_lines(second_dump), row_lines(first_dump))
        self.assertEqual(json.loads(second_dump.read_text(encoding="utf-8").split("\n")[0])
                         ["engine"], second)
        self.assertEqual(snapshot(middle), snapshot(source))

        back, _settings, _project = new_store(self, first)
        transfer.restore(back, second_dump, include_users=True)
        third_dump = work / "third.jsonl"
        transfer.dump(back, third_dump)
        self.assertEqual(row_lines(third_dump), row_lines(first_dump))
        self.assertEqual(snapshot(back), snapshot(source))

        # The one-file export is the same file contents whichever engine it comes from.
        exports = []
        for number, store in enumerate((source, middle, back)):
            path = work / ("export_%d.sqlite" % number)
            transfer.export_sqlite(store, path)
            exports.append(export_rows(path))
        self.assertEqual(exports[0][0], exports[1][0])
        self.assertEqual(exports[0][0], exports[2][0])
        self.assertEqual(sorted(exports[0][1]), sorted(exports[1][1]))
        self.assertEqual(len(exports[0][0]["points"]), 14)

    def test_sqlite_to_postgresql_and_back(self):
        self.round_trip("sqlite", "postgresql")

    def test_postgresql_to_sqlite_and_back(self):
        self.round_trip("postgresql", "sqlite")


if __name__ == "__main__":
    unittest.main()
