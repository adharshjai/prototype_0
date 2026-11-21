"""The demo data (pcldb/demo.py): the index file it builds, and loading and clearing it on
both engines."""

from __future__ import annotations

import hashlib
import io
import json
import sqlite3
import types
import unittest
from pathlib import Path

from pcldb import demo, reconcile, registration, transfer, util
from pcldb.errors import Conflict, PclError
from pcldb.store import schema_def
from tests import support
from tests import test_reconcile as index_cases

NOW = "2026-11-20T18:00:00.000Z"
LATER = "2026-12-24T09:30:00.000Z"

# SHA-256 of what the proof of concept prints for its SQLite shape (`pcl_index ddl --dialect
# sqlite`), computed once on the lead's computer. The demo index must have exactly that shape.
POC_DDL_SHA256 = "b3a7a1d535422c28ced79b0c7c901c6412f843469ad4bbe45c3d4046652cd798"

PEOPLE = {"demo-operator-a", "demo-operator-b", "demo-reviewer", "demo-classifier"}
LABELS = {"adenine", "methylene_blue", "r6g", "UNKNOWN"}


def file_rows(path):
    """{table or view: rows} of an index file, read with a plain connection."""
    connection = sqlite3.connect(str(path))
    try:
        names = [row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') ORDER BY name")]
        return {name: connection.execute('SELECT * FROM "%s" ORDER BY 1, 2' % name).fetchall()
                for name in names}
    finally:
        connection.close()


class TestDemoIndex(unittest.TestCase):
    """The index file and its rows; no database."""

    @classmethod
    def setUpClass(cls):
        cls.meta, cls.tables = demo.demo_tables(NOW)

    def test_the_shape_is_the_proof_of_concepts_shape(self):
        text = ";\n\n".join(demo.INDEX_DDL) + ";\n"
        self.assertEqual(hashlib.sha256(text.encode("utf-8")).hexdigest(), POC_DDL_SHA256)
        with support.TempProject() as project:
            path = project.path / "demo.sqlite"
            demo.build_demo_index(path, NOW)
            connection = sqlite3.connect(str(path))
            try:
                made = {row[0] for row in connection.execute(
                    "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL")}
                kinds = dict(connection.execute(
                    "SELECT type, COUNT(*) FROM sqlite_master WHERE sql IS NOT NULL GROUP BY type"))
            finally:
                connection.close()
        self.assertEqual(made, set(demo.INDEX_DDL))
        self.assertEqual(kinds, {"table": 11, "index": 6, "view": 4})

    def test_the_same_now_gives_the_same_rows_and_another_now_other_rows(self):
        with support.TempProject() as project:
            first, second, third = (project.path / name for name in ("a.sqlite", "b.sqlite",
                                                                     "c.sqlite"))
            counts = demo.build_demo_index(first, NOW)
            demo.build_demo_index(second, util.parse_utc(NOW))       # a datetime works too
            demo.build_demo_index(third, LATER)
            demo.build_demo_index(third, LATER)                      # an existing file is replaced
            rows_first, rows_second, rows_third = (file_rows(path) for path in (first, second,
                                                                                third))
        self.assertEqual(rows_first, rows_second)
        self.assertEqual(counts["sessions"], 16)
        self.assertEqual(len(rows_first["sessions"]), 16)
        self.assertEqual(len(rows_third["sessions"]), 16)
        self.assertFalse({row[0] for row in rows_first["sessions"]}
                         & {row[0] for row in rows_third["sessions"]})
        self.assertEqual(dict(rows_first["meta"]), {
            "schema": "pcl-index/1", "built_utc": NOW, "current_model": demo.MODEL_CURRENT,
            "tool": demo.DEMO_TOOL})
        self.assertEqual(demo.demo_tables(NOW), demo.demo_tables(NOW))

    def test_every_session_is_clearly_fake(self):
        sessions = self.tables["sessions"]
        self.assertEqual(len(sessions), 16)
        self.assertEqual({row["instrument"] for row in sessions},
                         {"raman-portable", "jeol-jsm-it800-shl"})
        self.assertEqual(sum(row["instrument"] == "raman-portable" for row in sessions), 12)
        now = util.parse_utc(NOW)
        for row in sessions:
            self.assertIs(row["simulated"], True)
            self.assertEqual(row["area"], "simulated")
            self.assertIn("demo", row["tags"])
            self.assertEqual(row["path"], "raw/%s/simulated/%s" % (row["instrument"],
                                                                 row["folder"]))
            self.assertIn(row["operator"], demo.OPERATORS)
            self.assertIn("Demo data", row["notes"])
            self.assertTrue(row["session_id"].startswith("0d0d0d0d-demo-"))
            self.assertTrue(all(sample.startswith("DEMO-") for sample in row["sample_ids"]))
            started = util.parse_utc(row["started_utc"])
            self.assertLess((now - started).total_seconds(), 30 * 86400)
            self.assertLess(started, now)
            self.assertIsNone(registration.path_problem(row["path"]))
            # The card is a real lab card: a registration would accept it.
            self.assertEqual(registration.validate_card(row["card_json"]), [], row["path"])
            self.assertEqual(row["card_json"]["session"], row["folder"])
        self.assertEqual({row["operator"] for row in sessions}, set(demo.OPERATORS))
        self.assertEqual(len({row["path"] for row in sessions}), 16)
        self.assertEqual(len({row["session_id"] for row in sessions}), 16)

    def test_no_time_is_in_the_future(self):
        times = []
        for table, rows in self.tables.items():
            for row in rows:
                times.extend(value for name, value in row.items()
                             if schema_def.COLUMN_KINDS.get(name) == "utc" and value)
        self.assertGreater(len(times), 60)
        self.assertLessEqual(max(times), NOW)
        self.assertGreater(min(times), "2026-10-20")

    def test_only_demo_people_and_nothing_personal(self):
        self.assertEqual({row["run_by"] for row in self.tables["runs"]},
                         {"demo-classifier", "demo-reviewer"})
        self.assertEqual({row["reviewer"] for row in self.tables["reviews"]}, {"demo-reviewer"})
        people = set()
        for row in self.tables["sessions"]:
            people.add(row["operator"])
            people.add(row["card_json"]["operator"])
        self.assertLessEqual(people, PEOPLE)
        text = json.dumps(self.tables)
        for word in ("/Users/", "/home/", "@", "vt.edu"):
            self.assertNotIn(word, text)

    def test_what_the_specification_asks_the_demo_to_hold(self):
        sessions, runs = self.tables["sessions"], self.tables["runs"]
        self.assertEqual(sorted(row["completion"] for row in sessions).count("partial"), 1)
        self.assertEqual(sorted(row["completion"] for row in sessions).count("failed"), 1)
        partial = next(row for row in sessions if row["completion"] == "partial")
        self.assertLess(partial["items"], partial["items_planned"])
        kinds = [row["kind"] for row in runs]
        self.assertEqual(kinds.count("review"), 1)
        self.assertEqual(kinds.count("correction"), 1)
        self.assertGreaterEqual(kinds.count("classify"), 10)
        self.assertEqual({row["model_name"] for row in runs if row["kind"] == "classify"},
                         {demo.MODEL_CURRENT, demo.MODEL_OLDER})
        self.assertTrue(all(row["instrument"] == "raman-portable" for row in runs))
        self.assertEqual({row["label"] for row in self.tables["points"]}, LABELS)
        for point in self.tables["points"]:
            self.assertTrue(0.0 < point["confidence"] < 1.0)
            self.assertAlmostEqual(sum(point["scores_json"].values()), 1.0, places=5)
            self.assertEqual(point["scores_json"][point["top_class"]], point["confidence"])
            self.assertEqual(point["needs_review"], point["confidence"] < point["threshold"])
        self.assertEqual(sorted(row["status"] for row in self.tables["reviews"]),
                         ["confirmed", "rejected", "relabelled"])
        corrected = [row for row in sessions if row["corrected"]]
        self.assertEqual(len(corrected), 1)
        correction = corrected[0]["corrections_json"][0]
        self.assertEqual(correction["set"], {"sample_ids": corrected[0]["sample_ids"]})
        self.assertEqual(correction["original"],
                         {"sample_ids": corrected[0]["card_json"]["sample_ids"]})
        self.assertNotEqual(correction["set"], correction["original"])
        self.assertEqual({row["is_current"] for row in self.tables["models"]}, {True, False})
        self.assertEqual(len(self.tables["problems"]), 2)
        self.assertEqual([row["problem_no"] for row in self.tables["problems"]], [1, 2])
        states = [row["review_state"] for row in self.tables["session_rollup"]]
        self.assertEqual({state: states.count(state) for state in set(states)},
                         {"none": 11, "pending": 4, "done": 1})

    def test_files_add_up(self):
        by_session = {}
        for row in self.tables["files"]:
            by_session.setdefault(row["session_path"], []).append(row)
        point_hashes = {(row["session_path"], row["rel_path"]): row["sha256"]
                        for row in self.tables["files"]}
        for session in self.tables["sessions"]:
            files = by_session[session["path"]]
            self.assertEqual(session["n_files"], len(files))
            self.assertEqual(session["bytes"], sum(row["bytes"] for row in files))
            names = {row["rel_path"]: row for row in files}
            self.assertEqual(names["SHA256SUMS"]["sha256"], session["sums_sha256"])
            self.assertIn("pcl_session.json", names)
            if session["instrument"] == "raman-portable":
                self.assertEqual(sum(name.startswith("point_") for name in names),
                                 session["items"])
        self.assertEqual(sum(not row["listed"] for row in self.tables["files"]), 1)
        self.assertTrue(all((row["sha256"] is None) == (not row["listed"])
                            for row in self.tables["files"]))
        runs = {row["path"]: row for row in self.tables["runs"]}
        for point in self.tables["points"]:
            session_path = runs[point["run_path"]]["session_path"]
            self.assertEqual(point["raw_sha256"], point_hashes[(session_path, point["point_file"])])

    def test_the_rollups_agree_with_the_index_files_own_views(self):
        with support.TempProject() as project:
            path = project.path / "demo.sqlite"
            demo.build_demo_index(path, NOW)
            connection = sqlite3.connect(str(path))
            try:
                counted = connection.execute(
                    "SELECT session_path, effective_label, COUNT(*) FROM current_points "
                    "WHERE effective_label IS NOT NULL GROUP BY 1, 2").fetchall()
                awaiting = dict(connection.execute(
                    "SELECT session_path, SUM(awaiting_review) FROM current_points GROUP BY 1"))
                summary = connection.execute(
                    "SELECT path, review_state, n_needs_review, labels FROM session_summary"
                ).fetchall()
                newest = dict(connection.execute(
                    "SELECT session_path, MAX(made_utc) FROM runs GROUP BY 1"))
            finally:
                connection.close()
        from_views = {}
        for session_path, label, count in counted:
            from_views.setdefault(session_path, {})[label] = count
        rollups = {row["session_path"]: row for row in self.tables["session_rollup"]}
        self.assertEqual(len(rollups), 16)
        for path, row in rollups.items():
            self.assertEqual({item["label"]: item["count"] for item in row["label_counts"]},
                             from_views.get(path, {}), path)
            self.assertEqual(row["labels"], sorted(from_views.get(path, {})))
            self.assertEqual(row["n_needs_review"], awaiting.get(path) or 0)
            self.assertEqual(row["last_run_utc"], newest.get(path))
        self.assertEqual(sorted(summary), sorted(
            (path, row["review_state"], row["n_needs_review"], json.dumps(row["labels"]))
            for path, row in rollups.items()))
        # The session that was classified again counts its newest run only, and the session
        # that only the older model classified has no labels.
        only_old = [row for row in rollups.values() if row["models"] == [demo.MODEL_OLDER]]
        self.assertEqual(len(only_old), 1)
        self.assertEqual(only_old[0]["labels"], [])

    def test_write_index_refuses_an_unknown_table(self):
        with support.TempProject() as project:
            with self.assertRaises(ValueError):
                demo.write_index(project.path / "x.sqlite", {}, {"users": []})


class DemoCases:
    """Loading and clearing, the same on both engines."""

    def load(self, now=NOW):
        with self.store.app() as db:
            return demo.load_demo(db, self.settings, now)

    def clear(self):
        with self.store.app() as db:
            return demo.clear_demo(db)

    def rows(self, sql, params=()):
        with self.store.app() as db:
            return db.query(sql, params)

    def count(self, table):
        definition = schema_def.table(table)
        return self.rows("SELECT COUNT(*) AS n FROM {%s}.%s"
                         % (definition.schema, definition.name))[0]["n"]

    def snapshot(self):
        with self.store.app() as db:
            return transfer.read_tables(db)

    def test_load_fills_the_catalogue(self):
        result = self.load()
        self.assertEqual((result["source"], result["sessions"], result["sessions_created"]),
                         ("demo", 16, 16))
        self.assertEqual(result["search"], {"changed": 16, "unchanged": 0, "removed": 0})
        meta, tables = demo.demo_tables(NOW)
        for table, rows in tables.items():
            self.assertEqual(self.count("pcl." + table), len(rows), table)
        self.assertGreater(self.count("pcl.files"), 200)
        self.assertGreater(self.count("pcl.points"), 100)
        self.assertEqual(self.count("pcl.reviews"), 3)

        sessions = self.rows("SELECT * FROM {pcl}.sessions")
        self.assertEqual(len(sessions), 16)
        for row in sessions:
            self.assertEqual(row["source"], "demo")
            self.assertEqual(row["registered_by"], demo.DEMO_ACTOR)
            self.assertIs(row["simulated"], True)
            self.assertIn("demo", row["tags"])
            self.assertTrue(row["storage_path"].startswith("/projects/pcl_photonics/raw/"))
        facts = {row["key"]: row["value"] for row in self.rows("SELECT * FROM {pcl}.meta")}
        self.assertEqual(facts["demo"], "1")
        self.assertEqual(facts["current_model"], demo.MODEL_CURRENT)
        self.assertEqual(facts["index_built_utc"], NOW)
        self.assertEqual(facts["index_source"], demo.DEMO_TOOL)
        with self.store.app() as db:
            self.assertTrue(demo.is_loaded(db))

        # One Search entry per session, waiting to be sent, and marked as what it is.
        entries = self.rows("SELECT * FROM {app}.search_entries")
        self.assertEqual(sorted(row["session_path"] for row in entries),
                         sorted(row["path"] for row in sessions))
        for entry in entries:
            self.assertEqual(entry["state"], "pending")
            self.assertIs(entry["content_json"]["simulated"], True)
            self.assertIn("demo", entry["content_json"]["tags"])
            self.assertNotIn("operator", entry["content_json"])
            self.assertNotIn("notes", entry["content_json"])
        audit = self.rows("SELECT action, source, user_name FROM {app}.registrations")
        self.assertEqual(len(audit), 16)
        self.assertEqual({(row["action"], row["source"], row["user_name"]) for row in audit},
                         {("created", "demo", demo.DEMO_ACTOR)})

    def test_the_views_agree_with_the_rollups_on_this_engine(self):
        self.load()
        _meta, tables = demo.demo_tables(NOW)
        wanted = sorted((row["session_path"], item["label"], item["count"])
                        for row in tables["session_rollup"] for item in row["label_counts"])
        got = self.rows("SELECT session_path, label, n FROM {pcl}.session_labels")
        self.assertEqual(sorted((row["session_path"], row["label"], row["n"]) for row in got),
                         wanted)
        counted = self.rows(
            "SELECT session_path, effective_label, COUNT(*) AS n FROM {pcl}.current_points "
            "WHERE effective_label IS NOT NULL GROUP BY session_path, effective_label")
        self.assertEqual(sorted((row["session_path"], row["effective_label"], row["n"])
                                for row in counted), wanted)
        awaiting = self.rows("SELECT session_path, COUNT(*) AS n FROM {pcl}.current_points "
                             "WHERE awaiting_review GROUP BY session_path")
        self.assertEqual({row["session_path"]: row["n"] for row in awaiting},
                         {row["session_path"]: row["n_needs_review"]
                          for row in tables["session_rollup"] if row["n_needs_review"]})
        summary = self.rows("SELECT path, review_state, labels, files, simulated "
                            "FROM {pcl}.session_summary")
        self.assertEqual(len(summary), 16)
        states = [row["review_state"] for row in summary]
        self.assertEqual((states.count("none"), states.count("pending"), states.count("done")),
                         (11, 4, 1))
        tags = self.rows("SELECT tag, COUNT(*) AS n FROM {pcl}.session_tags GROUP BY tag")
        self.assertEqual({row["tag"]: row["n"] for row in tags}["demo"], 16)
        high = self.rows("SELECT COUNT(*) AS n FROM {pcl}.current_points "
                         "WHERE effective_label = ? AND confidence >= ?", ["adenine", 0.9])
        self.assertGreater(high[0]["n"], 0)

    def test_clear_leaves_no_demo_row_and_no_entry(self):
        self.load()
        counts = self.clear()
        self.assertEqual((counts["loaded"], counts["sessions"], counts["entries_removed"],
                          counts["entries_to_remove"]), (True, 16, 16, 0))
        self.assertGreater(counts["files"], 200)
        self.assertGreater(counts["points"], 100)
        for table in schema_def.TABLES:
            if table.name == "meta":
                continue
            self.assertEqual(self.count(table.qualified), 0, table.qualified)
        facts = {row["key"]: row["value"] for row in self.rows("SELECT * FROM {pcl}.meta")}
        self.assertEqual(facts, {"schema_version": str(schema_def.SCHEMA_VERSION), "demo": "0"})
        with self.store.app() as db:
            self.assertFalse(demo.is_loaded(db))
        # Clearing again, or clearing a database that never had the demo, changes nothing.
        before = self.snapshot()
        self.assertEqual(self.clear()["loaded"], False)
        self.assertEqual(self.snapshot(), before)
        # And now a real index may be loaded.
        path = index_cases.write_small_index(self.project.path / "sessions.sqlite")
        with self.store.app() as db:
            result = reconcile.load_index(db, path, actor="demo-operator-a",
                                          settings=self.settings)
        self.assertEqual(result["sessions_created"], 2)

    def test_clear_does_not_touch_a_database_without_the_demo(self):
        with self.store.app() as db:
            support.seed_minimal(db)
        before = self.snapshot()
        counts = self.clear()
        self.assertFalse(counts["loaded"])
        self.assertEqual(self.snapshot(), before)

    def test_an_entry_that_may_be_in_globus_search_is_marked_for_removal(self):
        self.load()
        paths = sorted(row["path"] for row in self.rows("SELECT path FROM {pcl}.sessions"))
        with self.store.app() as db:
            db.execute("UPDATE {app}.search_entries SET state = ?, synced_utc = ? "
                       "WHERE session_path = ?",
                       ["synced", db.param("utc", "2026-11-20T18:05:00.000Z"), paths[0]])
            db.execute("UPDATE {app}.search_entries SET state = ?, task_id = ? "
                       "WHERE session_path = ?", ["sent", "task-1", paths[1]])
        counts = self.clear()
        self.assertEqual((counts["entries_removed"], counts["entries_to_remove"]), (14, 2))
        left = self.rows("SELECT session_path, state, task_id FROM {app}.search_entries")
        self.assertEqual(sorted((row["session_path"], row["state"], row["task_id"])
                                for row in left),
                         [(paths[0], "delete_pending", None), (paths[1], "delete_pending", None)])
        self.assertEqual(self.count("pcl.sessions"), 0)
        # Loading the demo again takes the two entries back into use.
        self.load()
        states = self.rows("SELECT state, COUNT(*) AS n FROM {app}.search_entries GROUP BY state")
        self.assertEqual({row["state"]: row["n"] for row in states}, {"pending": 16})

    def test_load_is_refused_when_real_sessions_exist(self):
        with self.store.app() as db:
            support.seed_minimal(db)
        before = self.snapshot()
        with self.assertRaises(Conflict) as caught:
            self.load()
        self.assertEqual(caught.exception.code, "real_data_present")
        self.assertIn("2 session(s)", caught.exception.message)
        self.assertEqual(self.snapshot(), before)

    def test_a_real_index_is_refused_while_the_demo_is_loaded(self):
        self.load()
        before = self.snapshot()
        path = index_cases.write_small_index(self.project.path / "sessions.sqlite")
        with self.assertRaises(Conflict) as caught:
            with self.store.app() as db:
                reconcile.load_index(db, path, actor="demo-operator-a", settings=self.settings)
        self.assertEqual(caught.exception.code, "demo_loaded")
        self.assertIn("pcldb demo clear", caught.exception.message)
        self.assertEqual(self.snapshot(), before)

    def test_loading_again_replaces_the_demo(self):
        self.load(NOW)
        first = {row["path"] for row in self.rows("SELECT path FROM {pcl}.sessions")}
        self.load(LATER)
        second = {row["path"] for row in self.rows("SELECT path FROM {pcl}.sessions")}
        self.assertEqual(len(second), 16)
        self.assertFalse(first & second)
        self.assertEqual(self.count("app.search_entries"), 16)
        self.assertEqual(self.count("app.registrations"), 16)
        _meta, tables = demo.demo_tables(LATER)
        self.assertEqual(self.count("pcl.files"), len(tables["files"]))
        # The same moment twice: the same rows (only the bookkeeping times differ).
        self.load(LATER)
        self.assertEqual({row["path"] for row in self.rows("SELECT path FROM {pcl}.sessions")},
                         second)

    def test_a_session_registered_beside_the_demo_survives_the_clearing(self):
        self.load()
        real = "raw/raman-portable/1d_y_scan_20261115_110940_537"
        with self.store.app() as db:
            db.insert("pcl.sessions", support.session_row(real, n_files=1, bytes=10))
            db.insert("pcl.files", {"session_path": real, "rel_path": "session.json",
                                    "bytes": 10, "sha256": None, "listed": False})
        self.clear()
        self.assertEqual(self.rows("SELECT path, source FROM {pcl}.sessions"),
                         [{"path": real, "source": "upload"}])
        self.assertEqual(self.count("pcl.files"), 1)
        self.assertEqual(self.count("pcl.runs"), 0)

    def test_plain_questions_find_demo_sessions_with_the_keyword_rules(self):
        # K-03: questions a lab member types must find the sessions their filters describe.
        # The first five are the website's example buttons (pcldb/web/index.html).
        import datetime
        from pcldb import ask
        self.load()
        now = datetime.datetime(2026, 11, 20, 13, 0,
                                tzinfo=datetime.timezone(datetime.timedelta(hours=-5)))
        unlimited = types.SimpleNamespace(check=lambda *_: None)
        questions = [
            "Which sessions have adenine?", "Show Raman sessions from the last 7 days",
            "How many sessions per instrument?", "Which sessions still need review?",
            "Which sessions were partial or failed?",
            "Which sessions found adenine?", "Which sessions measured adenine?",
            "Sessions that contain r6g", "list the raman scans", "give me the failed sessions",
            "show me sessions with methylene blue",
            "can you find raman sessions labelled adenine", "I want all simulated sessions",
            "what scans were done this week", "which runs detected r6g",
            "get every jeol session", "please list sessions where r6g was identified",
            "show sessions that showed adenine", "Which sessions have methylene blue in them?",
            "find the latest raman scan", "Did any session see r6g?",
            "Are there sessions with adenine?", "Show me data where r6g appears",
            "Which raman sessions completed?", "which sessions have problems",
        ]
        self.assertGreaterEqual(len(questions), 20)
        index_html = (Path(__file__).resolve().parent.parent / "pcldb" / "web"
                      / "index.html").read_text(
            encoding="utf-8")
        for question in questions[:5]:
            self.assertIn(">%s</button>" % question, index_html)
        for question in questions:
            with self.subTest(question=question):
                result = ask.answer(question, store=self.store, settings=self.settings,
                                    llm=None, now=now, limiter=unlimited)
                self.assertGreater(result["total"], 0, result["interpretation"])
                self.assertEqual(result["request"]["q"], "*", result["interpretation"])
        # NEW-05: a known location is a filter, not three words ("1" is in every Raman entry).
        here = self.rows("SELECT COUNT(*) AS n FROM {pcl}.sessions WHERE location = ?",
                         ["demo-bench-1"])[0]["n"]
        result = ask.answer("sessions at demo-bench-1", store=self.store,
                            settings=self.settings, llm=None, now=now, limiter=unlimited)
        self.assertEqual((result["total"], result["request"]["q"]), (here, "*"))

    def test_every_sql_example_finds_rows_in_the_demo(self):
        # COR-11: a new user clicks the examples on the demo data first.
        from pcldb import sql_examples, sqlrun
        self.load(now=util.now_utc_text())     # "the last 7 days" follows the real clock
        for item in sql_examples.examples(self.engine):
            with self.subTest(title=item["title"]):
                result = sqlrun.run_sql(self.store, item["sql"], settings=self.settings)
                self.assertGreater(result["row_count"], 0)

    def test_commands(self):
        out = io.StringIO()
        self.assertEqual(demo.cmd_demo(types.SimpleNamespace(action="load"), self.settings,
                                       out=out, now=NOW), 0)
        text = out.getvalue()
        self.assertTrue(text.isascii())
        self.assertIn("Loaded the demo data: 16 sessions (all simulated, all tagged demo)", text)
        self.assertIn("pcldb demo clear", text)
        self.assertEqual(self.count("pcl.sessions"), 16)
        out = io.StringIO()
        self.assertEqual(demo.cmd_demo(types.SimpleNamespace(action="clear"), self.settings,
                                       out=out), 0)
        self.assertIn("Removed the demo data: 16 sessions", out.getvalue())
        self.assertEqual(self.count("pcl.sessions"), 0)
        out = io.StringIO()
        self.assertEqual(demo.cmd_demo(types.SimpleNamespace(action="clear"), self.settings,
                                       out=out), 0)
        self.assertIn("There is no demo data", out.getvalue())
        with self.assertRaises(PclError) as caught:
            demo.cmd_demo(types.SimpleNamespace(action="again"), self.settings, out=out)
        self.assertEqual(caught.exception.code, "usage")


class TestDemoSqlite(DemoCases, support.SqliteCase, unittest.TestCase):
    pass


class TestDemoPostgres(DemoCases, support.PostgresCase, unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
