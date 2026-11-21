"""The example queries of expert mode (pcldb/sql_examples.py): every one runs on its engine,
through the same sandbox and the same checks a person's SQL meets in the website."""

from __future__ import annotations

import json
import unittest

from pcldb import sql_examples, sqlrun, util
from pcldb.store import reader_postgres
from tests import support

# What the minimal seed must give back, by example title: {column: values}, in row order.
EXPECTED = {
    "Newest sessions": {"folder": ["recent_session", "1d_y_scan_20261116_225015_281",
                                   "1d_y_scan_20261115_110940_537"]},
    "Sessions per instrument": {"area": ["real", "simulated"], "sessions": [2, 1]},
    "Sessions that measured a sample": {"folder": ["1d_y_scan_20261115_110940_537"]},
    "Points with a label above a confidence": {"point_id": [1], "effective_label": ["adenine"]},
    "Label counts per session": {"label": ["UNKNOWN", "adenine", "r6g"], "n": [1, 1, 1]},
    "A session's files, largest first": {"rel_path": ["a.json", "b.json", "c.json"],
                                         "bytes": [30, 20, 10]},
    "Sessions awaiting review": {"folder": ["1d_y_scan_20261115_110940_537"],
                                 "n_needs_review": [1]},
    "Storage used per instrument": {"instrument": ["raman-portable"], "sessions": [3],
                                    "files": [11], "total_bytes": [162744],
                                    "megabytes": [0.2]},
    "Where a session's data is": {"storage_path": [
        "/projects/pcl_photonics/raw/raman-portable/recent_session",
        "/projects/pcl_photonics/" + support.SIMULATED_PATH,
        "/projects/pcl_photonics/" + support.REAL_PATH]},
    "Simulated versus real": {"kind": ["real", "simulated"], "sessions": [2, 1]},
    "Sessions per tag": {"tag": ["demo"], "sessions": [1]},
    "Sessions that did not finish": {"folder": ["recent_session"], "completion": ["partial"]},
    "A value from the lab card": {"card_format": ["pcl-session/1"] * 3},
    "Problems the nightly index found": {"code": ["no_card"], "n": [2]},
}


class TestTheLists(unittest.TestCase):
    def test_both_engines_have_the_same_titles(self):
        sqlite = sql_examples.examples("sqlite")
        postgres = sql_examples.examples("postgresql")
        self.assertGreaterEqual(len(sqlite), 10)
        self.assertEqual([item["title"] for item in sqlite],
                         [item["title"] for item in postgres])
        self.assertEqual(len({item["title"] for item in sqlite}), len(sqlite))
        different = [a["title"] for a, b in zip(sqlite, postgres) if a["sql"] != b["sql"]]
        self.assertEqual(different, ["Sessions of the last 7 days", "Sessions per day",
                                     "A value from the lab card"])
        with self.assertRaises(ValueError):
            sql_examples.examples("oracle")

    def test_every_example_is_one_commented_statement(self):
        for engine in sql_examples.ENGINES:
            for item in sql_examples.examples(engine):
                self.assertEqual(sorted(item), ["sql", "title"])
                sql = item["sql"]
                self.assertTrue(sql.startswith("-- "), item["title"])
                self.assertTrue(sql.isascii(), item["title"])
                self.assertEqual(sql, sql.strip())
                self.assertNotIn(";", sql)
                self.assertEqual(sqlrun.leading_word(sql, engine), "SELECT")
                self.assertEqual(reader_postgres.statement_end(sql), (len(sql), False))
                self.assertLess(max(len(line) for line in sql.splitlines()), 96)
        json.dumps(sql_examples.examples("sqlite"))

    def test_the_topics_the_specification_asks_for_are_there(self):
        text = " ".join(item["sql"] for item in sql_examples.examples("postgresql"))
        for needed in ("session_samples", "current_points", "session_labels", "session_rollup",
                       "session_tags", "web_url", "storage_path", "simulated",
                       "interval '7 days'", "files", "confidence >="):
            self.assertIn(needed, text)
        self.assertIn("'-7 days'", " ".join(item["sql"]
                                             for item in sql_examples.examples("sqlite")))


class ExampleCases:
    def setUp(self):
        super().setUp()
        with self.store.app() as db:
            keys = support.seed_minimal(db)
            # The example names a sample id of the demo data (DEMO-S-101).
            db.execute("UPDATE {pcl}.sessions SET sample_ids = ? WHERE folder = ?",
                       [db.param("strlist", ["DEMO-S-101"]), "1d_y_scan_20261115_110940_537"])
            del keys
            # One session of today, so that "the last 7 days" has something to find, and two
            # problems for the problems example.
            db.insert("pcl.sessions", support.session_row(
                "raw/raman-portable/recent_session", started_utc=util.now_utc_text(),
                completion="partial", items=2, items_planned=9, sample_ids=["S-900"],
                bytes=60, n_files=3))
            db.insert_many("pcl.files", [
                {"session_path": "raw/raman-portable/recent_session", "rel_path": name,
                 "bytes": size, "sha256": None, "listed": False}
                for name, size in (("b.json", 20), ("a.json", 30), ("c.json", 10))])
            db.insert_many("pcl.problems", [
                {"problem_no": 1, "path": "raw/raman-portable/a", "code": "no_card"},
                {"problem_no": 2, "path": "raw/raman-portable/b", "code": "no_card"}])

    def test_every_example_runs_through_the_sandbox(self):
        examples = sql_examples.examples(self.engine)
        self.assertGreaterEqual(len(examples), 10)
        for item in examples:
            sqlrun.check_sql_text(item["sql"], self.settings, self.engine)
            result = sqlrun.run_sql(self.store, item["sql"], settings=self.settings,
                                    user_name="demo-operator-a")
            self.assertFalse(result["truncated"], item["title"])
            json.dumps(result, allow_nan=False)
            expected = EXPECTED.get(item["title"])
            if expected is None:
                continue
            names = [column["name"] for column in result["columns"]]
            for column, values in expected.items():
                self.assertIn(column, names, item["title"])
                found = [row[names.index(column)] for row in result["rows"]]
                if self.engine == "sqlite" and column in ("simulated",):
                    found = [bool(value) for value in found]
                self.assertEqual(found, values, "%s, column %s" % (item["title"], column))
        self.assertEqual(sorted(list(EXPECTED) + ["Sessions per day",
                                                  "Sessions of the last 7 days"]),
                         sorted(item["title"] for item in examples))
        # Each run left its audit row.
        with self.store.app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {app}.query_log WHERE ok"),
                             len(examples))

    def example(self, title):
        item = next(entry for entry in sql_examples.examples(self.engine)
                    if entry["title"] == title)
        return sqlrun.run_sql(self.store, item["sql"], settings=self.settings)

    def test_sessions_per_day(self):
        result = self.example("Sessions per day")
        self.assertEqual([column["name"] for column in result["columns"]], ["day", "sessions"])
        counts = dict(map(tuple, result["rows"]))
        self.assertEqual(sum(counts.values()), 3)
        self.assertEqual(counts[util.now_utc_text()[:10]], 1 + sum(
            1 for day in ("2026-11-15", "2026-11-17") if day == util.now_utc_text()[:10]))
        self.assertEqual([row[0] for row in result["rows"]],
                         sorted(counts, reverse=True))

    def test_the_last_7_days_follow_the_clock(self):
        # Whatever today is: the session that started just now is in, and one that started
        # eight days ago is out. (The two seeded sessions are in or out by their own dates.)
        import datetime
        now = util.parse_utc(util.now_utc_text())
        with self.store.app() as db:
            db.insert("pcl.sessions", support.session_row(
                "raw/raman-portable/eight_days_ago",
                started_utc=util.utc_text(now - datetime.timedelta(days=8))))
            db.insert("pcl.sessions", support.session_row(
                "raw/raman-portable/six_days_ago",
                started_utc=util.utc_text(now - datetime.timedelta(days=6, hours=23))))
        result = self.example("Sessions of the last 7 days")
        folders = [row[0] for row in result["rows"]]
        self.assertEqual(folders[0], "recent_session")
        self.assertIn("six_days_ago", folders)
        self.assertNotIn("eight_days_ago", folders)
        week_ago = util.utc_text(now - datetime.timedelta(days=7))
        expected = sum(1 for started in ("2026-11-15T16:09:40.537Z", "2026-11-17T03:50:15.281Z")
                       if started >= week_ago) + 2
        self.assertEqual(len(folders), expected)


class TestExamplesSqlite(ExampleCases, support.SqliteCase, unittest.TestCase):
    pass


class TestExamplesPostgres(ExampleCases, support.PostgresCase, unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
