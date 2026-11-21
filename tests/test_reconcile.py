"""Loading the nightly index (pcldb/reconcile.py), on both engines.

The index files are written by `small_index()` below: two sessions, classify runs of the
current and of an older model, a review, a correction. Nothing is copied from a real index;
the people are `demo-operator-a`, `demo-operator-b` and `demo-reviewer`.
"""

from __future__ import annotations

import io
import sqlite3
import types
import unittest

from pcldb import demo, reconcile, registration, search_entries, transfer
from pcldb.errors import Conflict, PclError
from pcldb.store import schema_def
from tests import registry_support, support

A = registry_support.SIMULATED_PATH       # raw/raman-portable/simulated/1d_y_scan_20261116_225015_281
B = "raw/raman-portable/1d_y_scan_20261115_110940_537"
A_FOLDER = A.rsplit("/", 1)[-1]
B_FOLDER = B.rsplit("/", 1)[-1]

MODEL_NEW, NEW_SHA = "demo_model_new", "aa" * 32
MODEL_OLD, OLD_SHA = "demo_model_old", "bb" * 32

RA_FIRST = "results/raman-portable/simulated/%s__20261117T090000Z_classify" % A_FOLDER
RA_OLD = "results/raman-portable/simulated/%s__20261117T100000Z_classify" % A_FOLDER
RA_NEW = "results/raman-portable/simulated/%s__20261117T110000Z_classify" % A_FOLDER
RA_REVIEW = "results/raman-portable/simulated/%s__20261117T120000Z_review" % A_FOLDER
RA_CORRECTION = "results/raman-portable/simulated/%s__20261117T130000Z_correction" % A_FOLDER
RB_NEW = "results/raman-portable/%s__20261116T120000Z_classify" % B_FOLDER
RB_INVALID = "results/raman-portable/%s__20261116T130000Z_classify" % B_FOLDER
RB_OTHER_HASH = "results/raman-portable/%s__20261116T140000Z_classify" % B_FOLDER

BUILT = "2026-11-19T02:31:51.157Z"


def _session(path, card, files, **changes):
    row = {
        "path": path, "instrument": card["instrument"],
        "area": "simulated" if card["simulated"] else "real", "folder": path.rsplit("/", 1)[-1],
        "session_name": card["session"], "session_id": card["session_id"], "is_copy": False,
        "started_utc": "2026-11-16T22:50:15.281Z", "ended_utc": "2026-11-16T22:51:40.072Z",
        "simulated": card["simulated"], "sample_ids": list(card["sample_ids"]),
        "operator": card.get("operator"), "location": card.get("location"),
        "method": card.get("method"), "items": card.get("items"),
        "items_planned": card.get("items_planned"), "completion": card["completion"],
        "notes": card.get("notes"), "tags": list(card.get("tags", [])), "format": card["format"],
        "uploaded_utc": "2026-11-17T03:50:15.281Z", "upload_id": card.get("upload_id"),
        "has_card": True, "card_json": card, "corrections_json": [], "corrected": False,
        "withdrawn": False, "bytes": sum(entry["bytes"] for entry in files),
        "n_files": len(files),
        "sums_sha256": next((entry["sha256"] for entry in files
                             if entry["rel_path"] == "SHA256SUMS"), None),
        "verified_utc": None, "verify_ok": None,
    }
    row.update(changes)
    return row


def _run(path, kind, made, session_path, **changes):
    row = {
        "path": path, "instrument": "raman-portable",
        "area": "simulated" if "/simulated/" in path else "real",
        "folder": path.rsplit("/", 1)[-1], "kind": kind, "made_utc": made,
        "session_path": session_path, "stated_session_path": session_path, "session_id": None,
        "sums_sha256": None, "derived_from": [], "tool_name": "pcldb-tests", "tool_version": "0",
        "model_name": None, "model_sha256": None, "runner": "hand", "slurm_job_id": None,
        "run_by": "demo-operator-a", "set_json": None, "why": None,
        "run_json": {"schema": "pcl-run/1", "kind": kind}, "valid": True,
    }
    row.update(changes)
    return row


def _point(run_path, point_id, label, confidence, needs_review=False, top_class=None):
    return {
        "run_path": run_path, "point_file": "point_%04d.json" % point_id, "point_id": point_id,
        "capture_id": "c-%d" % point_id, "label": label,
        "top_class": top_class or (label if label != "UNKNOWN" else "r6g"),
        "confidence": confidence,
        "outcome": "low_confidence" if needs_review else "confident",
        "needs_review": needs_review, "threshold": 0.8,
        "reason": "Low confidence" if needs_review else None,
        "raw_sha256": registry_support.fake_sha("%s %d" % (run_path, point_id)),
        "scores_json": {"adenine": 0.25, "r6g": confidence},
    }


def small_index():
    """(meta, tables) of a small, complete index in normalised values."""
    files_a, _ = registry_support.make_files()
    files_b, _ = registry_support.make_files(names=("session.json", "point_0001.json"))
    card_a = registry_support.make_card(sample_ids=["S-129"])
    card_b = registry_support.make_card(
        session=B_FOLDER, session_id="0d0d0d0d-demo-4000-8000-000000000002", simulated=False,
        sample_ids=["S-100"], operator="demo-operator-b", tags=[],
        extra={"kind": "real", "big": 10 ** 30})
    why = "test correction: the sample id was typed wrong"
    sessions = [
        _session(A, card_a, files_a, sample_ids=["S-130"], corrected=True, corrections_json=[{
            "run": RA_CORRECTION.rsplit("/", 1)[-1], "made_utc": "2026-11-17T13:00:00.000Z",
            "set": {"sample_ids": ["S-130"]}, "why": why, "by": "demo-reviewer",
            "original": {"sample_ids": ["S-129"]}}],
            verified_utc="2026-11-18T02:00:00.000Z", verify_ok=True),
        _session(B, card_b, files_b, started_utc="2026-11-15T16:09:40.537Z",
                 ended_utc="2026-11-15T16:10:05.120Z", verified_utc="2026-11-18T02:00:01.000Z",
                 verify_ok=False),
    ]
    files = [dict(entry, session_path=path, listed=entry["sha256"] is not None)
             for path, entries in ((A, files_a), (B, files_b)) for entry in entries]
    new = {"model_name": MODEL_NEW, "model_sha256": NEW_SHA}
    runs = [
        _run(RA_FIRST, "classify", "2026-11-17T09:00:00.000Z", A, **new),
        _run(RA_OLD, "classify", "2026-11-17T10:00:00.000Z", A, model_name=MODEL_OLD,
             model_sha256=OLD_SHA),
        _run(RA_NEW, "classify", "2026-11-17T11:00:00.000Z", A, **new),
        _run(RA_REVIEW, "review", "2026-11-17T12:00:00.000Z", A,
             derived_from=[RA_NEW.rsplit("/", 1)[-1]], run_by="demo-reviewer"),
        _run(RA_CORRECTION, "correction", "2026-11-17T13:00:00.000Z", A,
             set_json={"sample_ids": ["S-130"]}, why=why, run_by="demo-reviewer"),
        _run(RB_NEW, "classify", "2026-11-16T12:00:00.000Z", B, **new),
        _run(RB_INVALID, "classify", "2026-11-16T13:00:00.000Z", B, valid=False, **new),
        _run(RB_OTHER_HASH, "classify", "2026-11-16T14:00:00.000Z", B, model_name=MODEL_NEW,
             model_sha256="cc" * 32),
    ]
    points = (
        [_point(RA_FIRST, number, "r6g", 0.9) for number in (1, 2, 3, 4)]
        + [_point(RA_OLD, number, "r6g", 0.85) for number in (1, 2)]
        + [_point(RA_NEW, 1, "adenine", 0.95), _point(RA_NEW, 2, "UNKNOWN", 0.55, True),
           _point(RA_NEW, 3, "UNKNOWN", 0.61, True, "methylene_blue"),
           _point(RA_NEW, 4, "methylene_blue", 0.9),
           _point(RB_NEW, 1, "r6g", 0.93), _point(RB_NEW, 2, "UNKNOWN", 0.5, True),
           _point(RB_INVALID, 1, "adenine", 0.99), _point(RB_OTHER_HASH, 1, "adenine", 0.99)])

    def review(row_no, point_id, status, label, time_utc):
        return {"run_path": RA_REVIEW, "row_no": row_no, "reviewed_run": RA_NEW,
                "point_id": point_id, "capture_id": "c-%d" % point_id, "status": status,
                "label": label, "reviewer": "demo-reviewer", "time_utc": time_utc,
                "comment": "test review"}

    reviews = [review(1, 2, "confirmed", "UNKNOWN", "2026-11-17T11:50:00.000Z"),
               review(2, 2, "relabelled", "adenine", "2026-11-17T12:00:00.000Z"),
               review(3, 3, "rejected", None, "2026-11-17T12:00:00.000Z")]
    models = [
        {"name": MODEL_NEW, "path": "models/" + MODEL_NEW, "sha256": NEW_SHA,
         "classes_json": ["r6g", "methylene_blue", "adenine"], "threshold": 0.8,
         "files_json": [{"name": "model.bin", "bytes": 10, "sha256": "dd" * 32}],
         "is_current": True, "model_json": {"schema": "pcl-model/1", "name": MODEL_NEW}},
        {"name": MODEL_OLD, "path": "models/" + MODEL_OLD, "sha256": OLD_SHA,
         "classes_json": ["r6g"], "threshold": 0.7, "files_json": [], "is_current": False,
         "model_json": {"schema": "pcl-model/1", "name": MODEL_OLD}},
    ]
    problems = [
        {"problem_no": 1, "path": "raw/raman-portable/_incoming/left", "code": "incoming_leftover",
         "detail": "untouched for 9 days", "fix": "Ask who started it.", "session_path": None},
        {"problem_no": 2, "path": B, "code": "run_session_changed", "detail": None,
         "fix": "Run the classification again.", "session_path": B},
    ]
    tables = {"sessions": sessions, "files": files, "runs": runs, "points": points,
              "reviews": reviews, "models": models, "problems": problems,
              "session_rollup": demo.rollups(sessions, runs, points, reviews, models, problems,
                                             MODEL_NEW)}
    meta = {"schema": "pcl-index/1", "built_utc": BUILT, "tool": "pcl_index 0.1.0",
            "current_model": MODEL_NEW}
    return meta, tables


def write_small_index(path, statements=(), meta_changes=None, change=None):
    """Write the small index to `path`. `change(meta, tables)` may edit the rows first;
    `statements` are (sql, params) run on the finished file (for values no helper would write)."""
    meta, tables = small_index()
    for key, value in (meta_changes or {}).items():
        if value is None:
            meta.pop(key, None)
        else:
            meta[key] = value
    if change is not None:
        change(meta, tables)
    demo.write_index(path, meta, tables)
    if statements:
        connection = sqlite3.connect(str(path))
        try:
            for sql, params in statements:
                connection.execute(sql, params)
            connection.commit()
        finally:
            connection.close()
    return path


EXPECTED_ROLLUPS = {
    A: {"session_path": A, "labels": ["adenine", "methylene_blue"],
        "label_counts": [{"label": "adenine", "count": 2}, {"label": "methylene_blue", "count": 1}],
        "models": [MODEL_NEW, MODEL_OLD], "problems": [], "review_state": "done",
        "n_needs_review": 0, "last_run_utc": "2026-11-17T13:00:00.000Z"},
    B: {"session_path": B, "labels": ["UNKNOWN", "r6g"],
        "label_counts": [{"label": "UNKNOWN", "count": 1}, {"label": "r6g", "count": 1}],
        "models": [MODEL_NEW], "problems": ["run_session_changed"], "review_state": "pending",
        "n_needs_review": 1, "last_run_utc": "2026-11-16T14:00:00.000Z"},
}


class TestIndexShape(unittest.TestCase):
    """What needs no database."""

    def test_needed_columns_are_columns_of_the_index_and_of_the_store(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        for statement in demo.INDEX_DDL:
            connection.execute(statement)
        for table, columns in reconcile.INDEX_COLUMNS.items():
            in_file = [row[1] for row in connection.execute('PRAGMA table_info("%s")' % table)]
            self.assertEqual(list(columns), in_file, table)
            if table != "meta":
                self.assertLessEqual(set(columns), set(schema_def.table("pcl." + table).kinds))
        for table in reconcile.DERIVED_TABLES:
            self.assertEqual(reconcile.INDEX_COLUMNS[table],
                             schema_def.table("pcl." + table).column_names)

    def test_rollups_of_the_small_index_are_the_expected_ones(self):
        _meta, tables = small_index()
        self.assertEqual({row["session_path"]: row for row in tables["session_rollup"]},
                         EXPECTED_ROLLUPS)

    def test_read_index_gives_normalised_values_and_touches_no_database(self):
        with support.TempProject() as project:
            path = write_small_index(project.path / "sessions.sqlite")
            index = reconcile.read_index(path)
        self.assertEqual(index["meta"]["schema"], "pcl-index/1")
        self.assertEqual(index["not_finite"], 0)
        session = {row["path"]: row for row in index["tables"]["sessions"]}[A]
        self.assertIs(session["simulated"], True)
        self.assertIs(session["is_copy"], False)
        self.assertEqual(session["sample_ids"], ["S-130"])
        self.assertEqual(session["card_json"]["sample_ids"], ["S-129"])
        self.assertEqual(session["started_utc"], "2026-11-16T22:50:15.281Z")
        point = index["tables"]["points"][0]
        self.assertIsInstance(point["confidence"], float)
        self.assertIsInstance(point["scores_json"], dict)

    def test_a_missing_file_and_a_file_that_is_no_database(self):
        with support.TempProject() as project:
            with self.assertRaises(PclError) as caught:
                reconcile.read_index(project.path / "nothing.sqlite")
            self.assertEqual(caught.exception.code, "file_missing")
            text_file = project.path / "notes.sqlite"
            text_file.write_text("this is not a database\n" * 50, encoding="utf-8")
            with self.assertRaises(PclError) as caught:
                reconcile.read_index(text_file)
            self.assertEqual(caught.exception.code, "bad_index")
            self.assertIn("notes.sqlite", caught.exception.message)


class ReconcileCases:
    """The same tests on both engines."""

    def setUp(self):
        super().setUp()
        self.work = self.project.path / "work"
        self.work.mkdir()
        self.index = self.work / "sessions.sqlite"

    def load(self, path=None, **arguments):
        arguments.setdefault("actor", "demo-operator-b")
        arguments.setdefault("settings", self.settings)
        with self.store.app() as db:
            return reconcile.load_index(db, path or self.index, **arguments)

    def snapshot(self):
        with self.store.app() as db:
            return transfer.read_tables(db)

    def rows(self, sql, params=()):
        with self.store.app() as db:
            return db.query(sql, params)

    # -- the plain load -----------------------------------------------------------------------

    def test_load_counts_and_normalised_rows(self):
        write_small_index(self.index)
        result = self.load()
        self.assertEqual(
            {name: result[name] for name in (
                "file", "schema", "source", "built_utc", "tool", "current_model", "sessions",
                "sessions_created", "sessions_updated", "sessions_unchanged", "files", "runs",
                "points", "reviews", "models", "problems", "session_rollup", "not_finite",
                "not_in_index")},
            {"file": "sessions.sqlite", "schema": "pcl-index/1", "source": "index",
             "built_utc": BUILT, "tool": "pcl_index 0.1.0", "current_model": MODEL_NEW,
             "sessions": 2, "sessions_created": 2, "sessions_updated": 0, "sessions_unchanged": 0,
             "files": 9, "runs": 8, "points": 14, "reviews": 3, "models": 2, "problems": 2,
             "session_rollup": 2, "not_finite": 0, "not_in_index": []})
        self.assertEqual(result["search"], {"changed": 2, "unchanged": 0, "removed": 0})

        meta, tables = small_index()
        sessions = {row["path"]: row for row in self.rows("SELECT * FROM {pcl}.sessions")}
        self.assertEqual(set(sessions), {A, B})
        for wanted in tables["sessions"]:
            got = sessions[wanted["path"]]
            for name, value in wanted.items():
                self.assertEqual(got[name], value, name)
                self.assertIs(type(got[name]), type(value), name)
            # Where the files are: exactly what a registration computes.
            where = search_entries.location_values(wanted["path"], self.settings)
            self.assertEqual({name: got[name] for name in where}, where)
            self.assertEqual(got["source"], "index")
            self.assertEqual(got["registered_by"], "demo-operator-b")
            self.assertEqual(got["registered_utc"], got["updated_utc"])
            self.assertIsNone(got["upload_json"])
            files = [{"rel_path": row["rel_path"], "bytes": row["bytes"], "sha256": row["sha256"]}
                     for row in tables["files"] if row["session_path"] == wanted["path"]]
            self.assertEqual(got["doc_sha256"], registration.document_hash(
                wanted["path"], wanted["card_json"], files, wanted["sums_sha256"]))
        # A whole number too big for any integer column lives on inside the card.
        self.assertEqual(sessions[B]["card_json"]["extra"]["big"], 10 ** 30)

        for table in ("files", "runs", "points", "reviews", "models", "problems",
                      "session_rollup"):
            definition = schema_def.table("pcl." + table)
            got = self.rows("SELECT * FROM {pcl}.%s" % table)
            key = lambda row: tuple(row[name] for name in definition.primary_key)  # noqa: E731
            self.assertEqual(sorted(got, key=key), sorted(tables[table], key=key), table)
        run = self.rows("SELECT valid, derived_from, set_json FROM {pcl}.runs WHERE path = ?",
                        [RA_REVIEW])[0]
        self.assertIs(run["valid"], True)
        self.assertEqual(run["derived_from"], [RA_NEW.rsplit("/", 1)[-1]])

        facts = {row["key"]: row["value"] for row in self.rows("SELECT * FROM {pcl}.meta")}
        self.assertEqual(facts["current_model"], MODEL_NEW)
        self.assertEqual(facts["index_built_utc"], BUILT)
        self.assertEqual(facts["index_source"], "pcl_index 0.1.0")
        self.assertNotEqual(facts.get("demo"), "1")

        entries = self.rows("SELECT * FROM {app}.search_entries")
        self.assertEqual({row["session_path"] for row in entries}, {A, B})
        for entry in entries:
            self.assertEqual(entry["state"], "pending")
            self.assertEqual(entry["subject"], sessions[entry["session_path"]]["subject"])
        by_path = {row["session_path"]: row["content_json"] for row in entries}
        self.assertEqual(by_path[A]["labels"], ["adenine", "methylene_blue"])
        self.assertEqual(by_path[A]["sample_ids"], ["S-130"])
        self.assertEqual(by_path[B]["review_state"], "pending")
        self.assertNotIn("operator", by_path[A])

        audit = self.rows("SELECT session_path, action, source, user_name FROM "
                          "{app}.registrations")
        self.assertEqual(sorted((row["session_path"], row["action"], row["source"],
                                 row["user_name"]) for row in audit),
                         sorted([(A, "created", "index", "demo-operator-b"),
                                 (B, "created", "index", "demo-operator-b")]))

    def test_views_after_a_load(self):
        write_small_index(self.index)
        self.load()
        current = self.rows("SELECT path, session_path, model_name FROM {pcl}.current_runs")
        self.assertEqual(sorted((row["path"], row["session_path"]) for row in current),
                         [(RB_NEW, B), (RA_NEW, A)])
        latest = self.rows("SELECT reviewed_run, point_id, row_no, status, label "
                           "FROM {pcl}.latest_reviews")
        self.assertEqual(sorted((row["point_id"], row["row_no"], row["status"], row["label"])
                                for row in latest),
                         [(2, 2, "relabelled", "adenine"), (3, 3, "rejected", None)])

        points = self.rows("SELECT * FROM {pcl}.current_points")
        got = {(row["session_path"], row["point_id"]): row for row in points}
        self.assertEqual(set(got), {(A, 1), (A, 2), (A, 3), (A, 4), (B, 1), (B, 2)})
        expected = {
            (A, 1): ("adenine", None, None, "adenine", False),
            (A, 2): ("UNKNOWN", "relabelled", "adenine", "adenine", False),
            (A, 3): ("UNKNOWN", "rejected", None, None, False),
            (A, 4): ("methylene_blue", None, None, "methylene_blue", False),
            (B, 1): ("r6g", None, None, "r6g", False),
            (B, 2): ("UNKNOWN", None, None, "UNKNOWN", True),
        }
        for key, (model_label, status, review_label, effective, awaiting) in expected.items():
            row = got[key]
            self.assertEqual((row["model_label"], row["review_status"], row["review_label"],
                              row["effective_label"]),
                             (model_label, status, review_label, effective), key)
            self.assertIs(row["awaiting_review"], awaiting, key)
            self.assertEqual(row["run_path"], RA_NEW if key[0] == A else RB_NEW)
            self.assertEqual(row["model_name"], MODEL_NEW)
        self.assertIs(got[(A, 2)]["needs_review"], True)
        self.assertIs(got[(A, 1)]["simulated"], True)
        self.assertIs(got[(B, 1)]["simulated"], False)
        self.assertEqual(got[(A, 2)]["reviewer"], "demo-reviewer")
        self.assertEqual(got[(A, 2)]["review_run"], RA_REVIEW)
        self.assertEqual(got[(A, 2)]["review_time_utc"], "2026-11-17T12:00:00.000Z")
        self.assertEqual(got[(A, 1)]["run_made_utc"], "2026-11-17T11:00:00.000Z")
        self.assertEqual(got[(A, 1)]["confidence"], 0.95)

        labels = self.rows("SELECT session_path, label, n FROM {pcl}.session_labels")
        self.assertEqual(sorted((row["session_path"], row["label"], row["n"]) for row in labels),
                         sorted([(A, "adenine", 2), (A, "methylene_blue", 1),
                                 (B, "UNKNOWN", 1), (B, "r6g", 1)]))
        samples = self.rows("SELECT session_path, sample_id FROM {pcl}.session_samples")
        self.assertEqual(sorted((row["session_path"], row["sample_id"]) for row in samples),
                         sorted([(A, "S-130"), (B, "S-100")]))
        tags = self.rows("SELECT session_path, tag FROM {pcl}.session_tags")
        self.assertEqual([(row["session_path"], row["tag"]) for row in tags], [(A, "demo")])

        summary = {row["path"]: row for row in self.rows("SELECT * FROM {pcl}.session_summary")}
        self.assertEqual(set(summary), {A, B})
        self.assertEqual(list(summary[A]), list(schema_def.SUMMARY_COLUMNS))
        row = summary[A]
        self.assertEqual((row["review_state"], row["n_needs_review"], row["labels"],
                          row["label_counts"], row["models"], row["problems"]),
                         ("done", 0, ["adenine", "methylene_blue"],
                          EXPECTED_ROLLUPS[A]["label_counts"], [MODEL_NEW, MODEL_OLD], []))
        self.assertEqual((row["files"], row["sample_ids"], row["tags"], row["corrected"],
                          row["verified"], row["last_run"], row["started"]),
                         (5, ["S-130"], ["demo"], True, "2026-11-18T02:00:00.000Z",
                          "2026-11-17T13:00:00.000Z", "2026-11-16T22:50:15.281Z"))
        row = summary[B]
        self.assertEqual((row["review_state"], row["n_needs_review"], row["labels"],
                          row["problems"], row["verified"], row["simulated"]),
                         ("pending", 1, ["UNKNOWN", "r6g"], ["run_session_changed"], None, False))

    # -- sessions that are already there ---------------------------------------------------

    def test_a_registered_session_keeps_who_registered_it_and_how(self):
        document = registry_support.make_document(path=A, upload=registry_support.make_upload())
        with self.store.app() as db:
            registration.register_document(db, document, actor="demo-operator-a",
                                           source="upload", settings=self.settings)
            db.execute("UPDATE {pcl}.sessions SET registered_utc = ?, updated_utc = ? "
                       "WHERE path = ?", [db.param("utc", "2026-11-17T04:00:00.000Z"),
                                          db.param("utc", "2026-11-17T04:00:00.000Z"), A])
        before = self.rows("SELECT * FROM {pcl}.sessions WHERE path = ?", [A])[0]
        write_small_index(self.index)
        result = self.load()
        self.assertEqual((result["sessions_created"], result["sessions_updated"],
                          result["sessions_unchanged"]), (1, 1, 0))
        after = self.rows("SELECT * FROM {pcl}.sessions WHERE path = ?", [A])[0]
        for name in ("registered_utc", "registered_by", "source", "upload_json", "doc_sha256",
                     "collection_id", "data_path", "storage_path", "globus_url", "web_url",
                     "subject"):
            self.assertEqual(after[name], before[name], name)
        self.assertEqual(after["registered_by"], "demo-operator-a")
        self.assertEqual(after["source"], "upload")
        self.assertEqual(after["upload_json"]["transport"], "ssh")
        # What the index knows better is taken from the index.
        self.assertEqual(after["sample_ids"], ["S-130"])
        self.assertIs(after["corrected"], True)
        self.assertGreater(after["updated_utc"], before["updated_utc"])
        other = self.rows("SELECT source, registered_by FROM {pcl}.sessions WHERE path = ?", [B])
        self.assertEqual(other, [{"source": "index", "registered_by": "demo-operator-b"}])
        files = self.rows("SELECT rel_path FROM {pcl}.files WHERE session_path = ?", [A])
        self.assertEqual(len(files), 5)

        # The same index again changes nothing.
        snapshot = self.snapshot()
        again = self.load()
        self.assertEqual((again["sessions_created"], again["sessions_updated"],
                          again["sessions_unchanged"]), (0, 0, 2))
        self.assertEqual(again["search"], {"changed": 0, "unchanged": 2, "removed": 0})
        self.assertEqual(self.snapshot(), snapshot)

    def test_sessions_missing_from_the_index_are_left_alone_and_reported(self):
        write_small_index(self.index)
        self.load()

        def without_b(_meta, tables):
            tables["sessions"] = [row for row in tables["sessions"] if row["path"] == A]
            tables["files"] = [row for row in tables["files"] if row["session_path"] == A]
            tables["runs"] = [row for row in tables["runs"] if row["session_path"] == A]
            tables["points"] = [row for row in tables["points"]
                                if row["run_path"].startswith("results/raman-portable/simulated/")]
            tables["problems"] = []
            tables["session_rollup"] = [row for row in tables["session_rollup"]
                                        if row["session_path"] == A]
            tables["sessions"][0]["notes"] = "changed in the second index"

        second = write_small_index(self.work / "second.sqlite", change=without_b,
                                   meta_changes={"current_model": None, "tool": None})
        result = self.load(second)
        self.assertEqual(result["not_in_index"], [B])
        self.assertEqual((result["sessions"], result["sessions_updated"],
                          result["sessions_created"]), (1, 1, 0))
        self.assertIsNone(result["current_model"])
        # B is still there with its files; what is rebuilt every night holds the index only.
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM {pcl}.sessions")[0]["n"], 2)
        self.assertEqual(
            self.rows("SELECT COUNT(*) AS n FROM {pcl}.files WHERE session_path = ?", [B])[0]["n"],
            4)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM {pcl}.runs WHERE session_path = ?",
                                   [B])[0]["n"], 0)
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM {pcl}.problems")[0]["n"], 0)
        self.assertEqual([row["session_path"] for row in
                          self.rows("SELECT session_path FROM {pcl}.session_rollup")], [A])
        self.assertEqual(self.rows("SELECT key FROM {pcl}.meta WHERE key IN (?, ?)",
                                   ["current_model", "index_source"]), [])
        self.assertEqual(self.rows("SELECT notes FROM {pcl}.sessions WHERE path = ?",
                                   [A])[0]["notes"], "changed in the second index")
        # B's Search entry stays (the session is still registered) but loses its labels.
        entry = self.rows("SELECT state, content_json FROM {app}.search_entries "
                          "WHERE session_path = ?", [B])[0]
        self.assertEqual(entry["state"], "pending")
        self.assertNotIn("labels", entry["content_json"])
        audit = self.rows("SELECT action FROM {app}.registrations WHERE session_path = ?", [A])
        self.assertEqual(sorted(row["action"] for row in audit), ["created", "updated"])

    # -- refusals -----------------------------------------------------------------------------

    def assert_refused(self, path, code=None, mentions=(), error=PclError):
        before = self.snapshot()
        with self.assertRaises(error) as caught:
            self.load(path)
        if code is not None:
            self.assertEqual(caught.exception.code, code, caught.exception.message)
        for text in mentions:
            self.assertIn(text, caught.exception.message)
        self.assertEqual(self.snapshot(), before, "a refused load changed the database")
        return caught.exception

    def test_another_schema_is_refused_and_named(self):
        self.assert_refused(write_small_index(self.index, meta_changes={"schema": "pcl-index/2"}),
                            "unknown_schema", ["pcl-index/2", "pcl-index/1"])
        self.assert_refused(write_small_index(self.index, meta_changes={"schema": None}),
                            "unknown_schema", ["missing"])

    def test_a_missing_table_or_column_is_named(self):
        error = self.assert_refused(
            write_small_index(self.index, statements=[("DROP VIEW current_points", ()),
                                                      ("DROP TABLE points", ())]),
            "bad_index", ["no table named points"])
        self.assertEqual(error.details, {"missing_table": "points"})
        error = self.assert_refused(
            write_small_index(self.index, statements=[("ALTER TABLE runs DROP COLUMN why", ())]),
            "bad_index", ["table runs", "no column named why"])
        self.assertEqual(error.details, {"table": "runs", "missing_columns": ["why"]})
        # A view that only looks like the table does not count as the table.
        self.assert_refused(
            write_small_index(self.index, statements=[
                ("DROP TABLE problems", ()),
                ("CREATE VIEW problems AS SELECT 1 AS problem_no, 'x' AS path, 'y' AS code, "
                 "NULL AS detail, NULL AS fix, NULL AS session_path", ())]),
            "bad_index", ["no table named problems"])

    def test_scores_that_are_not_finite_become_empty_and_are_counted(self):
        write_small_index(self.index, statements=[
            ("UPDATE points SET confidence = ? WHERE run_path = ? AND point_id = 1",
             (float("inf"), RA_NEW)),
            ("UPDATE points SET threshold = 'nan' WHERE run_path = ? AND point_id = 2",
             (RA_NEW,)),
            ("UPDATE models SET threshold = ? WHERE name = ?", (float("-inf"), MODEL_OLD)),
        ])
        result = self.load()
        self.assertEqual(result["not_finite"], 3)
        rows = self.rows("SELECT point_id, confidence, threshold FROM {pcl}.points "
                         "WHERE run_path = ? ORDER BY point_id", [RA_NEW])
        self.assertEqual([(row["confidence"], row["threshold"]) for row in rows],
                         [(None, 0.8), (0.55, None), (0.61, 0.8), (0.9, 0.8)])
        self.assertEqual(self.rows("SELECT threshold FROM {pcl}.models WHERE name = ?",
                                   [MODEL_OLD]), [{"threshold": None}])
        # An empty score never passes a comparison, on either engine.
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM {pcl}.points WHERE run_path = ? "
                                   "AND confidence >= ?", [RA_NEW, 0.9])[0]["n"], 1)
        self.assertIn("3 confidence or threshold", "\n".join(reconcile.summary_lines(result)))

    def test_a_value_that_does_not_fit_names_its_row_and_changes_nothing(self):
        write_small_index(self.index)
        self.load()          # so that there is something a broken load could spoil
        point = "UPDATE points SET %s WHERE run_path = ? AND point_file = 'point_0002.json'"
        cases = [
            ("UPDATE sessions SET items = 3000000000 WHERE path = ?", (A,),
             ["session", A, "items"]),
            (point % "point_id = 1099511627776", (RA_NEW,),
             ["point", RA_NEW, "point_0002.json", "point_id"]),
            ("UPDATE runs SET run_json = ? WHERE path = ?", ('{"x": NaN}', RB_NEW),
             ["run", RB_NEW, "run_json", "NaN"]),
            ("UPDATE runs SET run_json = ? WHERE path = ?", ('{"x": 1e999}', RB_NEW),
             ["run", RB_NEW, "run_json"]),
            ("UPDATE sessions SET card_json = ? WHERE path = ?", ('{"note": "a\\u0000b"}', B),
             ["session", B, "card_json"]),
            ("UPDATE sessions SET card_json = ? WHERE path = ?", ('{"note": "\\ud800"}', B),
             ["session", B, "card_json"]),
            ("UPDATE sessions SET notes = ? WHERE path = ?", ("a\x00b", A),
             ["session", A, "notes"]),
            ("UPDATE sessions SET is_copy = 2 WHERE path = ?", (B,), ["session", B, "is_copy"]),
            ("UPDATE sessions SET started_utc = 'yesterday' WHERE path = ?", (A,),
             ["session", A, "started_utc"]),
            ("UPDATE reviews SET time_utc = '2026-02-30T00:00:00.000Z' WHERE row_no = 1", (),
             ["review", RA_REVIEW, "time_utc"]),
            ("UPDATE sessions SET sample_ids = '[1, 2]' WHERE path = ?", (A,),
             ["session", A, "sample_ids"]),
            ("UPDATE sessions SET tags = 'not json' WHERE path = ?", (A,),
             ["session", A, "tags"]),
            ("UPDATE sessions SET bytes = 'many' WHERE path = ?", (B,), ["session", B, "bytes"]),
            ("UPDATE files SET sha256 = x'00ff' WHERE session_path = ? AND rel_path = "
             "'session.json'", (A,), ["file", A, "session.json", "sha256"]),
            ("UPDATE session_rollup SET labels = '{}' WHERE session_path = ?", (B,),
             ["summary row of session", B, "labels"]),
            ("UPDATE models SET is_current = 'yes' WHERE name = ?", (MODEL_OLD,),
             ["model", MODEL_OLD, "is_current"]),
        ]
        for number, (sql, params, mentions) in enumerate(cases):
            with self.subTest(sql=sql):
                # Each broken file also carries a harmless change in an earlier row.
                path = write_small_index(self.work / ("broken_%d.sqlite" % number), statements=[
                    ("UPDATE sessions SET location = 'somewhere else'", ()), (sql, params)])
                error = self.assert_refused(path, "bad_index_value", mentions)
                self.assertIn("Nothing was loaded", error.message)
                self.assertNotIsInstance(error, Conflict)

    def test_a_failure_after_the_first_write_rolls_everything_back(self):
        write_small_index(self.index)
        self.load()
        # Valid JSON, so it is stored; but no Search entry can be made from it, which is only
        # found out after the sessions were written. The transaction takes it all back.
        path = write_small_index(self.work / "late.sqlite", statements=[
            ("UPDATE sessions SET notes = 'changed'", ()),
            ("UPDATE session_rollup SET label_counts = ? WHERE session_path = ?",
             ('[{"label": "adenine", "count": "two"}]', A))])
        self.assert_refused(path, "bad_search_entry", [A])
        self.assertEqual(self.rows("SELECT COUNT(*) AS n FROM {pcl}.sessions WHERE notes = ?",
                                   ["changed"])[0]["n"], 0)

    def test_rows_for_unknown_sessions_and_odd_paths_are_refused(self):
        orphan = ("INSERT INTO files (session_path, rel_path, bytes, sha256, listed) "
                  "VALUES ('raw/raman-portable/not_there', 'a.txt', 1, NULL, 0)")
        self.assert_refused(write_small_index(self.index, statements=[(orphan, ())]),
                            "bad_index_value", ["raw/raman-portable/not_there", "files"])
        for bad in ("raw/../outside", "/projects/pcl_photonics/raw/x", "raw\\x\\y", "raw//x"):
            with self.subTest(path=bad):
                statements = [("UPDATE session_rollup SET session_path = ? WHERE session_path = ?",
                               (bad, B)),
                              ("UPDATE files SET session_path = ? WHERE session_path = ?", (bad, B)),
                              ("UPDATE sessions SET path = ? WHERE path = ?", (bad, B))]
                self.assert_refused(write_small_index(self.index, statements=statements),
                                    "bad_index_value", ["session path"])

    def test_two_sessions_that_are_the_same_folder_are_refused(self):
        write_small_index(self.index)
        self.load()
        with self.store.app() as db:
            subject = db.scalar("SELECT subject FROM {pcl}.sessions WHERE path = ?", [B])
            db.delete_where("app.search_entries", "session_path = ?", [B])
            db.execute("UPDATE {pcl}.sessions SET subject = ? WHERE path = ?",
                       ["urn:pcl:dataset:moved", B])
            db.execute("UPDATE {pcl}.sessions SET path = ?, subject = ? WHERE path = ?",
                       ["raw/raman-portable/elsewhere", subject, B])
            db.delete_where("pcl.files", "session_path = ?", [B])
            db.delete_where("pcl.session_rollup", "session_path = ?", [B])
        error = self.assert_refused(self.index, "same_folder", [B, "raw/raman-portable/elsewhere"],
                                    error=Conflict)
        self.assertEqual(error.status, 409)

    def test_refused_while_the_demo_is_loaded(self):
        write_small_index(self.index)
        with self.store.app() as db:
            db.upsert("pcl.meta", {"key": "demo", "value": "1"}, key=("key",))
        error = self.assert_refused(self.index, "demo_loaded", ["pcldb demo clear"],
                                    error=Conflict)
        self.assertEqual(error.status, 409)
        with self.store.app() as db:
            db.upsert("pcl.meta", {"key": "demo", "value": "0"}, key=("key",))
            db.insert("pcl.sessions", support.session_row(
                "raw/raman-portable/simulated/demo_left_over", True, source="demo"))
        self.assert_refused(self.index, "demo_loaded", ["pcldb demo clear"], error=Conflict)

    def test_the_demo_source_is_refused_when_real_sessions_exist(self):
        write_small_index(self.index)
        self.load()
        with self.assertRaises(Conflict) as caught:
            self.load(source="demo")
        self.assertEqual(caught.exception.code, "real_data_present")
        with self.assertRaises(PclError) as caught:
            self.load(source="somewhere")
        self.assertEqual(caught.exception.code, "bad_source")

    # -- the command -----------------------------------------------------------------------------

    def test_command_prints_a_short_plain_summary(self):
        write_small_index(self.index)
        with self.store.app() as db:
            db.insert("pcl.sessions", support.session_row("raw/raman-portable/only_here"))
        out = io.StringIO()
        code = reconcile.cmd_load_index(types.SimpleNamespace(file=str(self.index)),
                                        self.settings, out=out)
        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertTrue(text.isascii())
        self.assertIn("Loaded sessions.sqlite (built %s by pcl_index 0.1.0)." % BUILT, text)
        self.assertIn("Sessions: 2 (2 new, 0 changed, 0 unchanged). Files: 9.", text)
        self.assertIn("Runs: 8. Points: 14. Reviews: 3. Models: 2 (current: %s). Problems: 2."
                      % MODEL_NEW, text)
        self.assertIn("1 session(s) are in the database but not in this index", text)
        self.assertIn("  raw/raman-portable/only_here", text)
        self.assertLessEqual(len(text.splitlines()), 8)
        self.assertEqual(self.rows("SELECT registered_by FROM {pcl}.sessions WHERE path = ?",
                                   [A]), [{"registered_by": "local-cli"}])
        with self.assertRaises(PclError) as caught:
            reconcile.cmd_load_index(types.SimpleNamespace(file=None), self.settings, out=out)
        self.assertEqual(caught.exception.code, "usage")


class TestReconcileSqlite(ReconcileCases, support.SqliteCase, unittest.TestCase):
    pass


class TestReconcilePostgres(ReconcileCases, support.PostgresCase, unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
