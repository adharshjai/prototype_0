"""Tests of pcldb.search_entries: where a session is, the content of its Search entry, the
check against the frozen fields, and the queue in app.search_entries (on both engines).

The fixed vectors below were computed ONCE with the proof of concept's own functions
(pcl_index search.collection_path, subject_for, web_url, content_for, run read-only) and are
written here as plain values, so this test needs nothing outside this folder.
"""

from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from pcldb import registration, search_entries
from pcldb.config import Settings
from pcldb.errors import PclError
from tests import registry_support as rs
from tests.support import PostgresCase, SqliteCase

COLLECTION = "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec"
WEB = "https://app.globus.org/file-manager?origin_id=" + COLLECTION + "&origin_path="

# path -> what the proof of concept computes for it with the default settings.
LOCATION_VECTORS = {
    "raw/raman-portable/1d_y_scan_20261116_225015_281": {
        "storage_path": "/projects/pcl_photonics/raw/raman-portable/1d_y_scan_20261116_225015_281",
        "data_path": "/pcl_photonics/raw/raman-portable/1d_y_scan_20261116_225015_281",
        "globus_url": "globus://" + COLLECTION
                      + "/pcl_photonics/raw/raman-portable/1d_y_scan_20261116_225015_281/",
        "web_url": WEB + "%2Fpcl_photonics%2Fraw%2Framan-portable%2F1d_y_scan_20261116_225015_281%2F",
        "subject": "urn:pcl:dataset:3fed1377-32f5-5a68-8ce5-569e77f19b3a"},
    "raw/raman-portable/simulated/1d_y_scan_20261116_225015_281": {
        "storage_path": "/projects/pcl_photonics/raw/raman-portable/simulated/"
                        "1d_y_scan_20261116_225015_281",
        "data_path": "/pcl_photonics/raw/raman-portable/simulated/1d_y_scan_20261116_225015_281",
        "globus_url": "globus://" + COLLECTION
                      + "/pcl_photonics/raw/raman-portable/simulated/1d_y_scan_20261116_225015_281/",
        "web_url": WEB + "%2Fpcl_photonics%2Fraw%2Framan-portable%2Fsimulated%2F"
                         "1d_y_scan_20261116_225015_281%2F",
        "subject": "urn:pcl:dataset:e426e2ff-9a87-56e9-b40b-aeefb2eaea19"},
    "raman_globus_upload/simulated/single_20261115_101500_000": {
        "storage_path": "/projects/pcl_photonics/raman_globus_upload/simulated/"
                        "single_20261115_101500_000",
        "data_path": "/pcl_photonics/raman_globus_upload/simulated/single_20261115_101500_000",
        "globus_url": "globus://" + COLLECTION
                      + "/pcl_photonics/raman_globus_upload/simulated/single_20261115_101500_000/",
        "web_url": WEB + "%2Fpcl_photonics%2Framan_globus_upload%2Fsimulated%2F"
                         "single_20261115_101500_000%2F",
        "subject": "urn:pcl:dataset:3d79bdfe-36c1-57f4-93fe-58f0c53d3a45"},
    "raw/jeol-jsm-it800-shl/demo sem #1 (a+b)": {
        "storage_path": "/projects/pcl_photonics/raw/jeol-jsm-it800-shl/demo sem #1 (a+b)",
        "data_path": "/pcl_photonics/raw/jeol-jsm-it800-shl/demo sem #1 (a+b)",
        "globus_url": "globus://" + COLLECTION
                      + "/pcl_photonics/raw/jeol-jsm-it800-shl/demo sem #1 (a+b)/",
        "web_url": WEB + "%2Fpcl_photonics%2Fraw%2Fjeol-jsm-it800-shl%2Fdemo+sem+%231+%28a%2Bb%29%2F",
        "subject": "urn:pcl:dataset:acfe3c62-688f-5753-80c5-df5992fc92e6"},
}

RICH_PATH = "raw/raman-portable/simulated/1d_y_scan_20261116_225015_281"
SPARSE_PATH = "raw/raman-portable/single_20261115_101500_000"
COPY_PATH = "raman_globus_upload/simulated/single_20261115_101500_000_copy2"

# Session and rollup rows with normalised values, as Db.query returns them (the location
# columns are added by session_row()).
RICH_SESSION = {
    "path": RICH_PATH, "instrument": "raman-portable", "folder": "1d_y_scan_20261116_225015_281",
    "session_id": "0d0d0d0d-demo-4000-8000-000000000001", "method": "1d-y",
    "completion": "complete", "format": "raman-session/1 folder", "location": "demo-lab-1",
    "operator": "demo-operator-a", "notes": "demo run, nothing was measured",
    "sample_ids": ["demo-sample-129"], "tags": ["demo", "week-47"], "simulated": True,
    "is_copy": False, "withdrawn": False, "corrected": True, "items": 13, "items_planned": 13,
    "n_files": 23, "bytes": 584556, "started_utc": "2026-11-16T22:50:15.281Z",
    "uploaded_utc": "2026-11-17T03:50:15.999Z", "verified_utc": "2026-11-18T07:00:01.500Z",
    "verify_ok": True}
RICH_ROLLUP = {
    "session_path": RICH_PATH, "labels": ["adenine", "r6g"],
    "label_counts": [{"label": "adenine", "count": 9}, {"label": "r6g", "count": 4}],
    "models": ["cnn3-demo"], "problems": ["duplicate_session_id"], "review_state": "pending",
    "n_needs_review": 2, "last_run_utc": "2026-11-17T05:00:00.123Z"}
RICH_CONTENT = {
    "schema": "pcl-search/1", "title": "1d_y_scan_20261116_225015_281",
    "instrument": "raman-portable", "folder": "1d_y_scan_20261116_225015_281",
    "session_id": "0d0d0d0d-demo-4000-8000-000000000001", "path": RICH_PATH,
    "collection_id": COLLECTION,
    "data_path": LOCATION_VECTORS[RICH_PATH]["data_path"],
    "globus_url": LOCATION_VECTORS[RICH_PATH]["globus_url"],
    "web_url": LOCATION_VECTORS[RICH_PATH]["web_url"],
    "method": "1d-y", "completion": "complete", "format": "raman-session/1 folder",
    "location": "demo-lab-1", "review_state": "pending", "sample_ids": ["demo-sample-129"],
    "tags": ["demo", "week-47"], "labels": ["adenine", "r6g"], "models": ["cnn3-demo"],
    "problems": ["duplicate_session_id"],
    "label_counts": [{"label": "adenine", "count": 9}, {"label": "r6g", "count": 4}],
    "simulated": True, "is_copy": False, "withdrawn": False, "corrected": True, "items": 13,
    "items_planned": 13, "files": 23, "bytes": 584556, "n_needs_review": 2,
    "started": "2026-11-16T22:50:15Z", "uploaded": "2026-11-17T03:50:15Z",
    "verified": "2026-11-18T07:00:01Z", "last_run": "2026-11-17T05:00:00Z"}
RICH_CONTENT_WITH_PEOPLE = dict(RICH_CONTENT, operator="demo-operator-a",
                                notes="demo run, nothing was measured")

SPARSE_SESSION = {
    "path": SPARSE_PATH, "instrument": "raman-portable", "folder": "single_20261115_101500_000",
    "session_id": "0d0d0d0d-demo-4000-8000-000000000002", "method": None,
    "completion": "unknown", "format": "raman-session/1 folder", "location": None,
    "operator": None, "notes": "", "sample_ids": [], "tags": [], "simulated": False,
    "is_copy": False, "withdrawn": False, "corrected": False, "items": None,
    "items_planned": None, "n_files": 3, "bytes": 0, "started_utc": "2026-11-15T10:15:00.000Z",
    "uploaded_utc": "2026-11-15T10:20:00.000Z", "verified_utc": "2026-11-18T07:00:01.500Z",
    "verify_ok": False}
SPARSE_CONTENT = {
    "schema": "pcl-search/1", "title": "single_20261115_101500_000",
    "instrument": "raman-portable", "folder": "single_20261115_101500_000",
    "session_id": "0d0d0d0d-demo-4000-8000-000000000002", "path": SPARSE_PATH,
    "collection_id": COLLECTION,
    "data_path": "/pcl_photonics/raw/raman-portable/single_20261115_101500_000",
    "globus_url": "globus://" + COLLECTION
                  + "/pcl_photonics/raw/raman-portable/single_20261115_101500_000/",
    "web_url": WEB + "%2Fpcl_photonics%2Fraw%2Framan-portable%2Fsingle_20261115_101500_000%2F",
    "completion": "unknown", "format": "raman-session/1 folder", "review_state": "none",
    "simulated": False, "is_copy": False, "withdrawn": False, "corrected": False, "files": 3,
    "bytes": 0, "n_needs_review": 0, "started": "2026-11-15T10:15:00Z",
    "uploaded": "2026-11-15T10:20:00Z"}

COPY_SESSION = {
    "path": COPY_PATH, "instrument": "raman-portable",
    "folder": "single_20261115_101500_000_copy2",
    "session_id": "0d0d0d0d-demo-4000-8000-000000000002", "method": "single",
    "completion": "failed", "format": "raman-session/1 folder", "location": "demo-lab-2",
    "operator": "demo-operator-b", "notes": None, "sample_ids": ["129", "demo b"], "tags": [],
    "simulated": True, "is_copy": True, "withdrawn": True, "corrected": False, "items": 0,
    "items_planned": 1, "n_files": 9, "bytes": 12345678901,
    "started_utc": "2026-11-15T10:15:00.000Z", "uploaded_utc": "2026-11-15T10:20:00.000Z",
    "verified_utc": None, "verify_ok": None}
COPY_ROLLUP = {"session_path": COPY_PATH, "labels": [], "label_counts": [], "models": [],
               "problems": [], "review_state": "done", "n_needs_review": 0,
               "last_run_utc": None}
COPY_CONTENT = {
    "schema": "pcl-search/1", "title": "single_20261115_101500_000_copy2",
    "instrument": "raman-portable", "folder": "single_20261115_101500_000_copy2",
    "session_id": "0d0d0d0d-demo-4000-8000-000000000002", "path": COPY_PATH,
    "collection_id": COLLECTION,
    "data_path": "/pcl_photonics/raman_globus_upload/simulated/single_20261115_101500_000_copy2",
    "globus_url": "globus://" + COLLECTION
                  + "/pcl_photonics/raman_globus_upload/simulated/single_20261115_101500_000_copy2/",
    "web_url": WEB + "%2Fpcl_photonics%2Framan_globus_upload%2Fsimulated%2F"
                     "single_20261115_101500_000_copy2%2F",
    "method": "single", "completion": "failed", "format": "raman-session/1 folder",
    "location": "demo-lab-2", "review_state": "done", "sample_ids": ["129", "demo b"],
    "simulated": True, "is_copy": True, "withdrawn": True, "corrected": False, "items": 0,
    "items_planned": 1, "files": 9, "bytes": 12345678901, "n_needs_review": 0,
    "started": "2026-11-15T10:15:00Z", "uploaded": "2026-11-15T10:20:00Z"}


class PureCase(unittest.TestCase):

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.settings = Settings.for_test(Path(holder.name))

    def session_row(self, values):
        row = dict(values)
        row.update(search_entries.location_values(row["path"], self.settings))
        return row


class LocationTests(PureCase):

    def test_the_fixed_vectors_of_the_proof_of_concept(self):
        for path, expected in LOCATION_VECTORS.items():
            with self.subTest(path=path):
                found = search_entries.location_values(path, self.settings)
                self.assertEqual(found, dict(expected, collection_id=COLLECTION))

    def test_the_parts_agree_with_each_other(self):
        for path, expected in LOCATION_VECTORS.items():
            self.assertEqual(search_entries.subject_for(COLLECTION, expected["data_path"]),
                             expected["subject"])
            self.assertEqual(search_entries.web_url_for(COLLECTION, expected["data_path"]),
                             expected["web_url"])
            self.assertEqual(search_entries.globus_url_for(COLLECTION, expected["data_path"]),
                             expected["globus_url"])
            self.assertEqual(search_entries.data_path_for(expected["storage_path"], "/projects"),
                             expected["data_path"])

    def test_what_does_and_does_not_change_a_subject(self):
        data_path = "/pcl_photonics/raw/raman-portable/x"
        subject = search_entries.subject_for(COLLECTION, data_path)
        self.assertEqual(search_entries.subject_for(COLLECTION.upper(), data_path), subject)
        self.assertEqual(search_entries.subject_for(" %s " % COLLECTION, data_path + "/"), subject)
        self.assertNotEqual(search_entries.subject_for(COLLECTION, data_path.upper()), subject)
        self.assertNotEqual(search_entries.subject_for(COLLECTION, data_path[1:]), subject)
        self.assertNotEqual(search_entries.subject_for(COLLECTION, data_path.replace("/", "\\")),
                            subject)

    def test_a_collection_id_must_be_a_uuid(self):
        for value in ("not-a-uuid", "", 5, "8e804f3a-5bac-4b6d-9836", ["x"]):
            with self.subTest(value=value):
                with self.assertRaises(PclError) as caught:
                    search_entries.location_values("raw/x/y", self.settings, collection_id=value)
                self.assertIn("collection id", caught.exception.message)
        other = "0A0A0A0A-1111-4222-8333-444444444444"
        found = search_entries.location_values("raw/x/y", self.settings, collection_id=other)
        self.assertEqual(found["collection_id"], other.lower())
        self.assertIn(other.lower(), found["globus_url"])

    def test_another_project_folder(self):
        found = search_entries.location_values("raw/x/y", self.settings,
                                               project_path="/projects/other_lab/sub")
        self.assertEqual(found["storage_path"], "/projects/other_lab/sub/raw/x/y")
        self.assertEqual(found["data_path"], "/other_lab/sub/raw/x/y")
        settings = self.settings.changed({"storage.project_path": "/projects/other_lab/sub/"})
        self.assertEqual(search_entries.location_values("raw/x/y", settings), found)

    def test_a_folder_outside_the_collection_has_no_address(self):
        with self.assertRaises(PclError) as caught:
            search_entries.location_values("raw/x/y", self.settings, project_path="/home/demo")
        self.assertIn("collection root", caught.exception.message)
        with self.assertRaises(PclError):
            search_entries.location_values("raw/x/y", self.settings,
                                           project_path="/projects_other/lab")

    def test_collection_root_rules(self):
        self.assertEqual(search_entries.data_path_for("/a/b", "/"), "/a/b")
        self.assertEqual(search_entries.data_path_for("/projects", "/projects"), "/")
        self.assertEqual(search_entries.data_path_for("/projects/a", "/projects/"), "/a")
        settings = self.settings.changed({"storage.collection_root": "/",
                                          "storage.project_path": "/projects/pcl_photonics"})
        found = search_entries.location_values("raw/x/y", settings)
        self.assertEqual(found["data_path"], "/projects/pcl_photonics/raw/x/y")
        self.assertEqual(found["storage_path"], found["data_path"])

    def test_the_path_text_is_used_exactly_as_given(self):
        lower = search_entries.location_values("raw/x/abc", self.settings)
        upper = search_entries.location_values("raw/x/ABC", self.settings)
        self.assertNotEqual(lower["subject"], upper["subject"])
        self.assertTrue(upper["storage_path"].endswith("/raw/x/ABC"))


class ContentTests(PureCase):

    def content(self, session, rollup, **changes):
        settings = self.settings.changed(changes) if changes else self.settings
        return search_entries.content_for(self.session_row(session), rollup, settings)

    def test_the_fixed_vector_with_labels_a_correction_and_a_verified_time(self):
        self.assertEqual(self.content(RICH_SESSION, RICH_ROLLUP), RICH_CONTENT)

    def test_the_fixed_vector_of_a_sparse_session_without_a_rollup(self):
        self.assertEqual(self.content(SPARSE_SESSION, None), SPARSE_CONTENT)

    def test_the_fixed_vector_of_a_withdrawn_copy_outside_the_layout(self):
        self.assertEqual(self.content(COPY_SESSION, COPY_ROLLUP), COPY_CONTENT)

    def test_the_fixed_vector_with_operator_and_notes_switched_on(self):
        found = self.content(RICH_SESSION, RICH_ROLLUP, **{"search.include_operator": True,
                                                           "search.include_notes": True})
        self.assertEqual(found, RICH_CONTENT_WITH_PEOPLE)

    def test_every_fixed_vector_fits_the_frozen_fields(self):
        for content in (RICH_CONTENT, SPARSE_CONTENT, COPY_CONTENT, RICH_CONTENT_WITH_PEOPLE):
            self.assertEqual(search_entries.check_content(content), [])

    def test_operator_and_notes_are_switched_one_by_one(self):
        only_operator = self.content(RICH_SESSION, RICH_ROLLUP, **{"search.include_operator": True})
        self.assertEqual(only_operator["operator"], "demo-operator-a")
        self.assertNotIn("notes", only_operator)
        only_notes = self.content(RICH_SESSION, RICH_ROLLUP, **{"search.include_notes": True})
        self.assertEqual(only_notes["notes"], "demo run, nothing was measured")
        self.assertNotIn("operator", only_notes)
        default = self.content(RICH_SESSION, RICH_ROLLUP)
        self.assertNotIn("operator", default)
        self.assertNotIn("notes", default)
        self.assertNotIn("demo-operator-a", repr(default))

    def test_empty_values_are_left_out_and_booleans_are_always_there(self):
        found = self.content(SPARSE_SESSION, None, **{"search.include_operator": True,
                                                      "search.include_notes": True})
        for name in ("method", "location", "operator", "notes", "sample_ids", "tags", "labels",
                     "label_counts", "models", "problems", "items", "items_planned", "verified",
                     "last_run"):
            self.assertNotIn(name, found)
        for name in ("simulated", "is_copy", "withdrawn", "corrected"):
            self.assertIs(found[name], False)
        self.assertEqual(found["bytes"], 0)                   # zero is a value, not "empty"
        self.assertEqual(found["n_needs_review"], 0)
        self.assertFalse(any(value is None or value == "" or value == []
                             for value in found.values()))

    def test_verified_needs_a_check_that_passed(self):
        for verify_ok, expected in ((True, True), (False, False), (None, False)):
            session = dict(RICH_SESSION, verify_ok=verify_ok)
            self.assertEqual("verified" in self.content(session, RICH_ROLLUP), expected)
        self.assertNotIn("verified", self.content(dict(RICH_SESSION, verified_utc=None), None))

    def test_times_are_cut_to_the_second_never_rounded(self):
        found = self.content(dict(RICH_SESSION, started_utc="2026-11-16T22:50:15.999Z"), None)
        self.assertEqual(found["started"], "2026-11-16T22:50:15Z")

    def test_the_row_s_own_location_wins_over_the_settings(self):
        row = self.session_row(RICH_SESSION)
        other = self.settings.changed({"storage.project_path": "/projects/somewhere_else"})
        self.assertEqual(search_entries.content_for(row, RICH_ROLLUP, other), RICH_CONTENT)

    def test_a_row_without_location_columns_gets_them_from_the_settings(self):
        self.assertEqual(search_entries.content_for(dict(RICH_SESSION), RICH_ROLLUP,
                                                    self.settings), RICH_CONTENT)

    def test_the_rows_are_not_changed(self):
        session, rollup = self.session_row(RICH_SESSION), copy.deepcopy(RICH_ROLLUP)
        before = copy.deepcopy((session, rollup))
        found = search_entries.content_for(session, rollup, self.settings)
        found["labels"].append("changed")
        found["sample_ids"].append("changed")
        self.assertEqual((session, rollup), before)


class CheckContentTests(unittest.TestCase):

    def problems(self, **changes):
        content = copy.deepcopy(RICH_CONTENT)
        content.update(changes)
        return search_entries.check_content(content)

    def test_good_content(self):
        self.assertEqual(self.problems(), [])
        self.assertEqual(search_entries.check_content({}), [])

    def test_the_frozen_fields_are_the_file_s_fields(self):
        fields = search_entries.load_fields()
        self.assertEqual(set(RICH_CONTENT_WITH_PEOPLE), set(fields))
        self.assertEqual(fields["label_counts"]["fields"], {"label": "string", "count": "integer"})

    def test_an_unknown_field(self):
        self.assertEqual(self.problems(colour="blue"), ["colour is not a frozen Search field"])

    def test_empty_values(self):
        self.assertEqual(self.problems(method=None), ["method is empty (empty values are left out)"])
        self.assertEqual(self.problems(method=""), ["method is empty (empty values are left out)"])
        self.assertEqual(self.problems(tags=[]), ["tags is empty (empty values are left out)"])

    def test_wrong_types(self):
        cases = {
            "instrument": (5, "instrument must be a string"),
            "notes": (["a"], "notes must be a string"),
            "tags": ("demo", "tags must be a list of strings"),
            "labels": (["a", 5], "labels must be a list of strings"),
            "sample_ids": (["a", ""], "sample_ids must be a list of strings"),
            "simulated": (1, "simulated must be true or false"),
            "withdrawn": ("false", "withdrawn must be true or false"),
            "items": (True, "items must be a whole number"),
            "bytes": (1.5, "bytes must be a whole number"),
            "files": ("3", "files must be a whole number"),
            "started": ("2026-11-16T22:50:15.281Z", "started must be a date like 2026-11-17T03:50:15Z"),
            "uploaded": ("2026-02-30T00:00:00Z", "uploaded must be a date like 2026-11-17T03:50:15Z"),
            "verified": (20261117, "verified must be a date like 2026-11-17T03:50:15Z"),
            "last_run": ("2026-11-17 05:00:00Z", "last_run must be a date like 2026-11-17T03:50:15Z"),
            "label_counts": ({"adenine": 9}, "label_counts must be a list of objects"),
        }
        for name, (value, expected) in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self.problems(**{name: value}), [expected])

    def test_label_counts_items(self):
        self.assertEqual(self.problems(label_counts=[{"label": "adenine"}]),
                         ["label_counts items must have exactly count, label"])
        self.assertEqual(self.problems(label_counts=[{"label": "a", "count": 1, "n": 2}]),
                         ["label_counts items must have exactly count, label"])
        self.assertEqual(self.problems(label_counts=[{"label": "a", "count": "9"}]),
                         ["label_counts.count must be a whole number"])
        self.assertEqual(self.problems(label_counts=[{"label": 7, "count": 9}]),
                         ["label_counts.label must be a string"])
        self.assertEqual(self.problems(label_counts=[{"label": "", "count": 9}]),
                         ["label_counts.label is empty (empty values are left out)"])
        self.assertEqual(self.problems(label_counts=["adenine"]),
                         ["label_counts must be a list of objects"])

    def test_no_field_takes_a_fraction_nan_or_infinity(self):
        for value in (1.0, float("nan"), float("inf")):
            for name in ("items", "items_planned", "files", "bytes", "n_needs_review"):
                self.assertEqual(self.problems(**{name: value}),
                                 ["%s must be a whole number" % name])
            self.assertTrue(self.problems(label_counts=[{"label": "a", "count": value}]))
            self.assertTrue(self.problems(started=value))
            self.assertTrue(self.problems(method=value))

    def test_several_problems_are_all_listed(self):
        found = self.problems(colour="blue", items="13", tags=[])
        self.assertEqual(len(found), 3)

    def test_content_that_is_not_an_object(self):
        self.assertEqual(search_entries.check_content(["x"]), ["the content must be a JSON object"])


# ---------------------------------------------------------------------- the queue, both engines


class RefreshCases:
    """The state rules of app.search_entries. Runs on SQLite and on PostgreSQL."""

    PATH = rs.SIMULATED_PATH

    def register(self, document=None, **options):
        document = document or rs.make_document()
        with self.store.app() as db:
            return registration.register_document(db, document, actor="demo-operator-a",
                                                  source="upload", settings=self.settings,
                                                  **options)

    def entry(self, path=None):
        with self.store.app() as db:
            return db.one("SELECT * FROM {app}.search_entries WHERE session_path = ?",
                          [path or self.PATH])

    def entries(self):
        with self.store.app() as db:
            return {row["session_path"]: row
                    for row in db.query("SELECT * FROM {app}.search_entries")}

    def refresh(self, paths="one", settings=None):
        with self.store.app() as db:
            return search_entries.refresh(db, [self.PATH] if paths == "one" else paths,
                                          settings or self.settings)

    def mark_synced(self, path=None):
        """What the pusher does once Globus Search confirmed an entry."""
        with self.store.app() as db:
            db.execute("UPDATE {app}.search_entries SET state = ?, task_id = ?, synced_utc = ? "
                       "WHERE session_path = ?",
                       ["synced", "demo-task", db.param("utc", "2026-11-18T00:00:00.000Z"),
                        path or self.PATH])

    def remove_session(self, path=None):
        with self.store.app() as db:
            db.delete_where("pcl.files", "session_path = ?", [path or self.PATH])
            db.delete_where("pcl.sessions", "path = ?", [path or self.PATH])

    def test_a_new_session_gets_a_pending_entry(self):
        result = self.register()
        entry = self.entry()
        self.assertEqual(entry["state"], "pending")
        self.assertEqual(entry["subject"], result["subject"])
        self.assertEqual(entry["subject"], LOCATION_VECTORS[self.PATH]["subject"])
        self.assertEqual(entry["visible_to"], [])
        self.assertIsNone(entry["task_id"])
        self.assertIsNone(entry["last_error"])
        self.assertIsNone(entry["synced_utc"])
        with self.store.app() as db:
            session = db.one("SELECT * FROM {pcl}.sessions WHERE path = ?", [self.PATH])
        content = search_entries.content_for(session, None, self.settings)
        self.assertEqual(entry["content_json"], content)
        self.assertEqual(search_entries.check_content(entry["content_json"]), [])
        self.assertEqual(len(entry["content_sha256"]), 64)
        self.assertNotIn("operator", entry["content_json"])
        self.assertNotIn("notes", entry["content_json"])

    def test_nothing_changed_means_unchanged(self):
        self.register()
        before = self.entry()
        self.assertEqual(self.refresh(), {"changed": 0, "unchanged": 1, "removed": 0})
        self.assertEqual(self.entry(), before)

    def test_a_synced_entry_stays_synced_while_nothing_changes(self):
        self.register()
        self.mark_synced()
        self.assertEqual(self.refresh(), {"changed": 0, "unchanged": 1, "removed": 0})
        self.assertEqual(self.refresh(None), {"changed": 0, "unchanged": 1, "removed": 0})
        entry = self.entry()
        self.assertEqual(entry["state"], "synced")
        self.assertEqual(entry["task_id"], "demo-task")

    def test_changed_content_makes_the_entry_pending_again(self):
        self.register()
        self.mark_synced()
        with self.store.app() as db:
            db.execute("UPDATE {pcl}.sessions SET withdrawn = ? WHERE path = ?", [True, self.PATH])
        before = self.entry()
        self.assertEqual(self.refresh(), {"changed": 1, "unchanged": 0, "removed": 0})
        entry = self.entry()
        self.assertEqual(entry["state"], "pending")
        self.assertIs(entry["content_json"]["withdrawn"], True)
        self.assertNotEqual(entry["content_sha256"], before["content_sha256"])
        self.assertIsNone(entry["task_id"])
        self.assertEqual(entry["synced_utc"], "2026-11-18T00:00:00.000Z")   # when it last was
        self.assertEqual(entry["subject"], before["subject"])

    def test_a_rollup_changes_the_content(self):
        self.register()
        self.mark_synced()
        with self.store.app() as db:
            db.insert("pcl.session_rollup", dict(RICH_ROLLUP, session_path=self.PATH))
        self.assertEqual(self.refresh()["changed"], 1)
        content = self.entry()["content_json"]
        self.assertEqual(content["labels"], ["adenine", "r6g"])
        self.assertEqual(content["label_counts"], RICH_ROLLUP["label_counts"])
        self.assertEqual(content["review_state"], "pending")
        self.assertEqual(content["n_needs_review"], 2)
        self.assertEqual(content["last_run"], "2026-11-17T05:00:00Z")
        self.assertEqual(search_entries.check_content(content), [])

    def test_changed_visible_to_makes_the_entry_pending_again(self):
        self.register()
        self.mark_synced()
        group = "urn:globus:groups:id:0a0a0a0a-1111-4222-8333-444444444444"
        settings = self.settings.changed({"search.visible_to": [group]})
        before = self.entry()
        self.assertEqual(self.refresh(settings=settings),
                         {"changed": 1, "unchanged": 0, "removed": 0})
        entry = self.entry()
        self.assertEqual(entry["state"], "pending")
        self.assertEqual(entry["visible_to"], [group])
        self.assertEqual(entry["content_sha256"], before["content_sha256"])
        self.assertEqual(self.refresh(settings=settings)["unchanged"], 1)

    def test_switching_the_operator_on_changes_the_content(self):
        self.register()
        settings = self.settings.changed({"search.include_operator": True,
                                          "search.include_notes": True})
        self.assertEqual(self.refresh(settings=settings)["changed"], 1)
        content = self.entry()["content_json"]
        self.assertEqual(content["operator"], "demo-operator-a")
        self.assertEqual(content["notes"], "demo run, nothing was measured")

    def test_a_failed_entry_is_left_alone_while_nothing_changes(self):
        self.register()
        with self.store.app() as db:
            db.execute("UPDATE {app}.search_entries SET state = ?, last_error = ? "
                       "WHERE session_path = ?", ["failed", "demo failure", self.PATH])
        self.assertEqual(self.refresh()["unchanged"], 1)
        entry = self.entry()
        self.assertEqual((entry["state"], entry["last_error"]), ("failed", "demo failure"))

    def test_a_session_that_is_gone_gets_delete_pending(self):
        self.register()
        self.mark_synced()
        self.remove_session()
        self.assertEqual(self.refresh(), {"changed": 0, "unchanged": 0, "removed": 1})
        entry = self.entry()
        self.assertEqual(entry["state"], "delete_pending")
        self.assertEqual(entry["subject"], LOCATION_VECTORS[self.PATH]["subject"])
        self.assertIsNone(entry["task_id"])
        self.assertEqual(self.refresh(), {"changed": 0, "unchanged": 0, "removed": 0})
        self.assertEqual(self.refresh(None), {"changed": 0, "unchanged": 0, "removed": 0})
        self.assertEqual(self.entry()["state"], "delete_pending")

    def test_refresh_of_everything_finds_new_changed_and_gone_sessions(self):
        self.register()
        other = rs.make_document(path=rs.OTHER_PATH)
        third = rs.make_document(path="other/place/" + rs.DEMO_SESSION)
        self.register(other)
        self.register(third)
        self.mark_synced(rs.OTHER_PATH)
        with self.store.app() as db:
            db.delete_where("app.search_entries", "session_path = ?", [self.PATH])   # "new"
            db.execute("UPDATE {pcl}.sessions SET corrected = ? WHERE path = ?",
                       [True, rs.OTHER_PATH])                                         # changed
        self.remove_session("other/place/" + rs.DEMO_SESSION)                         # gone
        self.assertEqual(self.refresh(None), {"changed": 2, "unchanged": 0, "removed": 1})
        states = {path: row["state"] for path, row in self.entries().items()}
        self.assertEqual(states, {self.PATH: "pending", rs.OTHER_PATH: "pending",
                                  "other/place/" + rs.DEMO_SESSION: "delete_pending"})
        self.assertEqual(self.refresh(None), {"changed": 0, "unchanged": 2, "removed": 0})

    def test_a_session_that_comes_back_is_pending_again(self):
        document = rs.make_document()
        self.register(document)
        self.remove_session()
        self.refresh()
        self.assertEqual(self.entry()["state"], "delete_pending")
        self.assertEqual(self.register(document)["action"], "created")
        entry = self.entry()
        self.assertEqual(entry["state"], "pending")
        self.assertEqual(len(self.entries()), 1)

    def test_a_path_that_was_never_there_changes_nothing(self):
        self.assertEqual(self.refresh(["raw/raman-portable/never_registered"]),
                         {"changed": 0, "unchanged": 0, "removed": 0})
        self.assertEqual(self.refresh([]), {"changed": 0, "unchanged": 0, "removed": 0})
        self.assertEqual(self.refresh(None), {"changed": 0, "unchanged": 0, "removed": 0})
        self.assertEqual(self.entries(), {})

    def test_a_path_given_twice_counts_once(self):
        self.register()
        self.assertEqual(self.refresh([self.PATH, self.PATH]),
                         {"changed": 0, "unchanged": 1, "removed": 0})
        self.assertEqual(self.refresh(self.PATH), {"changed": 0, "unchanged": 1, "removed": 0})

    def test_the_entry_of_a_vanished_session_gives_way_to_the_same_folder(self):
        # The same folder on ARC, first registered below one project folder, then (after the
        # first row was removed) below another: one subject, two paths.
        first = self.register(rs.make_document(path="raw_copy/sub/" + rs.DEMO_SESSION))
        self.mark_synced("raw_copy/sub/" + rs.DEMO_SESSION)
        self.remove_session("raw_copy/sub/" + rs.DEMO_SESSION)       # no refresh in between
        second = self.register(rs.make_document(
            path="sub/" + rs.DEMO_SESSION, project_path="/projects/pcl_photonics/raw_copy"))
        self.assertEqual(second["subject"], first["subject"])
        entries = self.entries()
        self.assertEqual(set(entries), {"sub/" + rs.DEMO_SESSION})
        self.assertEqual(entries["sub/" + rs.DEMO_SESSION]["state"], "pending")

    def test_public_entries_are_refused(self):
        self.register()
        self.mark_synced()
        before = self.entries()
        for value in ("public", "all_authenticated_users", " Public ", "ALL_AUTHENTICATED_USERS"):
            with self.subTest(value=value):
                settings = self.settings.changed({"search.visible_to": [
                    "urn:globus:groups:id:0a0a0a0a-1111-4222-8333-444444444444", value]})
                with self.assertRaises(PclError) as caught:
                    self.refresh(settings=settings)
                self.assertIn("never public", caught.exception.message)
                with self.assertRaises(PclError):
                    self.refresh(None, settings=settings)
                self.assertEqual(self.entries(), before)

    def test_a_public_setting_also_stops_a_registration(self):
        settings = self.settings.changed({"search.visible_to": ["public"]})
        with self.assertRaises(PclError):
            with self.store.app() as db:
                registration.register_document(db, rs.make_document(), actor="demo-operator-a",
                                               source="upload", settings=settings)
        with self.store.app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 0)
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {app}.search_entries"), 0)

    def test_a_folder_that_moved_to_another_collection_gets_a_new_subject(self):
        document = rs.make_document()
        first = self.register(document)
        self.mark_synced()
        moved = rs.make_document(collection_id="0a0a0a0a-1111-4222-8333-444444444444",
                                 card=rs.make_card(notes="moved"))
        second = self.register(moved, replace=True)
        self.assertNotEqual(first["subject"], second["subject"])
        with self.store.app() as db:
            rows = {row["subject"]: row
                    for row in db.query("SELECT * FROM {app}.search_entries")}
        self.assertEqual(set(rows), {first["subject"], second["subject"]})
        self.assertEqual(rows[first["subject"]]["state"], "delete_pending")   # leaves the index
        self.assertEqual(rows[second["subject"]]["state"], "pending")
        self.assertEqual(rows[second["subject"]]["session_path"], self.PATH)
        self.assertEqual(self.refresh(None), {"changed": 0, "unchanged": 1, "removed": 0})


class TestRefreshSqlite(RefreshCases, SqliteCase, unittest.TestCase):
    pass


class TestRefreshPostgres(RefreshCases, PostgresCase, unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
