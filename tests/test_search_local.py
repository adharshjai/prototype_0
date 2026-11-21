"""search_local: the stand-in for Globus Search, on both engines, plus the opt-in live run.

The live run asks a REAL Globus Search index the same cases the stand-in must pass. It is off
unless all of these are set:

    PCLDB_LIVE_GLOBUS=1
    PCLDB_LIVE_GLOBUS_INDEX=<index id>      an index the signed-in person may write to
    PCLDB_USER_DIR=<folder>                 the folder that holds globus_tokens.json
                                            (after `pcldb search login`: ~/.pcldb)

It adds twenty made-up entries (visible to the signed-in person only, marked by their `format`
value), runs the cases with one more filter that selects only those entries, and removes them
again. Use a trial index made for this, not the lab's working index: an index fixes its field
types forever.
"""

from __future__ import annotations

import copy
import dataclasses
import os
import time
import unittest
import uuid
from pathlib import Path

from pcldb import search_local
from pcldb.errors import PclError

from tests import search_cases, support
from tests.search_cases import CASES, SUBJECTS, any_of, between, check_result, terms

# The example of a search result in the research report (section E7), as the service sends it.
SERVICE_EXAMPLE = {
    "@datatype": "GSearchResult",
    "@version": "2017-09-01",
    "total": 11,
    "count": 2,
    "offset": 0,
    "has_next_page": True,
    "gmeta": [
        {
            "@datatype": "GMetaResult",
            "@version": "2017-09-01",
            "subject": "<subject string>",
            "entries": [
                {"entry_id": None, "matched_principal_sets": [], "content": {"...": "..."}}
            ],
        }
    ],
    "facet_results": [
        {"@datatype": "GFacetResult", "@version": "2017-09-01", "name": "by_sample",
         "buckets": [{"@datatype": "GBucket", "@version": "2017-09-01", "value": "polystyrene",
                      "count": 4}]}
    ],
}


def skeleton(value, key=None):
    """The structure of a JSON value: keys and kinds, a list by its first item. The inside of
    `content` is the entry's own business and is not compared."""
    if key == "content":
        return "object"
    if isinstance(value, dict):
        return {name: skeleton(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [skeleton(value[0])] if value else []
    return type(value).__name__


class TestWords(unittest.TestCase):
    """How the stand-in cuts text into words (difference 3 in the module text)."""

    def test_words(self):
        table = [
            ("Lab 214 bench A", ["lab", "214", "bench", "a"]),
            ("raman-portable", ["raman", "portable"]),
            ("methylene_blue", ["methylene_blue"]),
            ("1d_y_scan_20261116_225015_281", ["1d_y_scan_20261116_225015_281"]),
            ("raw/raman-portable/simulated/x", ["raw", "raman", "portable", "simulated", "x"]),
            ("session.json", ["session.json"]),
            ("v1.2 and 1,000.5", ["v1.2", "and", "1,000.5"]),
            ("a.1", ["a", "1"]),
            ("end. Next", ["end", "next"]),
            ("tags:wafer", ["tags:wafer"]),
            ("it's", ["it's"]),
            ("2026-11-17T03:50:15Z", ["2026", "11", "17t03", "50", "15z"]),
            ("UNKNOWN", ["unknown"]),
            ("\u00c9chantillon N\u00b05", ["\u00e9chantillon", "n", "5"]),
            ("_ __ a_", ["a_"]),
            ("", []),
            ("  ...  ", []),
        ]
        for text, wanted in table:
            with self.subTest(text):
                self.assertEqual(search_local.words(text), wanted)

    def test_parse_q(self):
        table = [
            ("*", [("all",)]),
            ("adenine", [("word", "adenine")]),
            ("Adenine ADENINE", [("word", "adenine")]),
            ("wafer mapping", [("word", "wafer"), ("word", "mapping")]),
            ("calib*", [("prefix", "calib")]),
            ("raman-port*", [("word", "raman"), ("prefix", "port")]),
            ("\"bench a\"", [("phrase", ("bench", "a"))]),
            ("\"Lab\"", [("word", "lab")]),
            ("x \"lab 214\" y*", [("word", "x"), ("phrase", ("lab", "214")), ("prefix", "y")]),
            ("\"calibration", [("word", "calibration")]),
            ("\"\"", []),
            ("- + |", []),
            ("**", [("all",)]),
            ("tags:wafer", [("word", "tags:wafer")]),
            ("a*b", [("word", "a"), ("word", "b")]),
        ]
        for q, wanted in table:
            with self.subTest(q):
                self.assertEqual(search_local.parse_q(q), wanted)


class TestPure(unittest.TestCase):
    """The evaluation without a database."""

    def setUp(self):
        self.entries = list(search_cases.make_entries().values())

    def test_every_case(self):
        self.assertGreaterEqual(len(CASES), 80)
        for one in CASES:
            with self.subTest(one["name"]):
                check_result(self, one, search_local.search_entries(self.entries, one["request"]))

    def test_the_entries_are_not_changed_and_the_result_is_a_copy(self):
        before = copy.deepcopy(self.entries)
        result = search_local.search_entries(self.entries, {"q": "*", "limit": 100})
        result["gmeta"][0]["entries"][0]["content"]["title"] = "changed"
        self.assertEqual(self.entries, before)

    def test_field_values(self):
        content = search_cases.make_entries()["e03"]["content"]
        self.assertEqual(search_local.field_values(content, "instrument"), ["raman-portable"])
        self.assertEqual(search_local.field_values(content, "tags"), ["mapping", "Priority"])
        self.assertEqual(search_local.field_values(content, "label_counts.count"), [4, 2])
        self.assertEqual(search_local.field_values(content, "location"), [])
        self.assertEqual(search_local.field_values(content, "instrument.name"), [])
        self.assertEqual(search_local.field_values({"a": None, "b": [None, "x"]}, "a"), [])
        self.assertEqual(search_local.field_values({"a": None, "b": [None, "x"]}, "b"), ["x"])

    def test_true_is_not_one_and_text_is_not_a_number(self):
        entries = [{"subject": "s1", "content": {"items": 1, "simulated": True, "tags": ["1"]}}]
        found = search_local.search_entries(entries, {"filters": [any_of("simulated", True)]})
        self.assertEqual(found["total"], 1)
        found = search_local.search_entries(entries, {"filters": [between("items", 1, 1)]})
        self.assertEqual(found["total"], 1)
        # a value of the wrong kind in an entry never matches a range
        odd = [{"subject": "s1", "content": {"items": True, "started": 20261117}}]
        self.assertEqual(search_local.search_entries(
            odd, {"filters": [between("items", "*", "*")]})["total"], 1)
        self.assertEqual(search_local.search_entries(
            odd, {"filters": [between("items", 0, 5)]})["total"], 0)
        self.assertEqual(search_local.search_entries(
            odd, {"filters": [between("started", "2026-01-01T00:00:00Z", "*")]})["total"], 0)

    def test_numbers_and_true_false_are_not_words(self):
        entries = [{"subject": "s1", "content": {"items": 42, "simulated": True, "title": "x"}}]
        for q in ("42", "true", "items", "simulated"):
            with self.subTest(q):
                self.assertEqual(search_local.search_entries(entries, {"q": q})["total"], 0)

    def test_an_invalid_request_is_refused(self):
        with self.assertRaises(PclError):
            search_local.search_entries(self.entries, {"q": "*", "limit": 1000})
        with self.assertRaises(PclError):
            search_local.search_entries(self.entries, {"filters": [any_of("nope", "x")]})


class LocalCases:
    """The stand-in over app.search_entries. Runs on both engines."""

    def setUp(self):
        super().setUp()
        with self.store.app() as db:
            search_cases.insert_entries(db)
            self.leaving = search_cases.insert_leaving_entry(db)

    def test_every_case(self):
        with self.store.app() as db:
            for one in CASES:
                with self.subTest(one["name"]):
                    check_result(self, one, search_local.search(db, one["request"]))

    def test_the_result_has_the_structure_of_the_service(self):
        request = {"q": "*", "limit": 2, "sort": [{"field_name": "started", "order": "asc"}],
                   "facets": [terms("instrument", name="by_sample")]}
        with self.store.app() as db:
            result = search_local.search(db, request)
        self.assertEqual(skeleton(result), skeleton(SERVICE_EXAMPLE))
        self.assertEqual(result["@datatype"], "GSearchResult")
        self.assertEqual(result["@version"], "2017-09-01")
        self.assertEqual(result["gmeta"][0]["@datatype"], "GMetaResult")
        entry = result["gmeta"][0]["entries"]
        self.assertEqual(len(entry), 1)
        self.assertIsNone(entry[0]["entry_id"])
        self.assertEqual(entry[0]["matched_principal_sets"], [])
        self.assertEqual(result["facet_results"][0]["@datatype"], "GFacetResult")
        self.assertEqual(result["facet_results"][0]["buckets"][0]["@datatype"], "GBucket")
        self.assertEqual((result["total"], result["count"], result["offset"],
                          result["has_next_page"]), (20, 2, 0, True))

    def test_facet_results_only_when_asked_for(self):
        with self.store.app() as db:
            without = search_local.search(db, {"q": "*"})
            with_empty = search_local.search(db, {"q": "*", "facets": []})
        self.assertEqual(set(without), {"@datatype", "@version", "total", "count", "offset",
                                        "has_next_page", "gmeta"})
        self.assertNotIn("facet_results", with_empty)

    def test_the_content_comes_back_exactly_as_stored(self):
        entries = search_cases.make_entries()
        with self.store.app() as db:
            result = search_local.search(db, {"q": "*", "limit": 100})
        found = {item["subject"]: item["entries"][0]["content"] for item in result["gmeta"]}
        self.assertEqual(len(found), 20)
        for key, entry in entries.items():
            with self.subTest(key):
                self.assertEqual(found[entry["subject"]], entry["content"])
                self.assertIs(type(found[entry["subject"]]["simulated"]), bool)
                self.assertIs(type(found[entry["subject"]]["bytes"]), int)

    def test_an_entry_that_waits_to_be_removed_is_never_found(self):
        with self.store.app() as db:
            for request in ({"q": "*", "limit": 100}, {"q": "calibration"},
                            {"filters": [any_of("instrument", "raman-portable")], "limit": 100}):
                result = search_local.search(db, request)
                self.assertNotIn(self.leaving, [item["subject"] for item in result["gmeta"]])
            self.assertEqual(search_local.search(db, {"q": "*", "limit": 0})["total"], 20)

    def test_every_other_state_is_searched(self):
        states = {"e01": "pending", "e07": "sent", "e08": "synced", "e16": "failed"}
        with self.store.app() as db:
            for key, state in states.items():
                db.execute("UPDATE {app}.search_entries SET state = ? WHERE subject = ?",
                           [state, SUBJECTS[key]])
            result = search_local.search(db, {"q": "calibration"})
        self.assertEqual(sorted(item["subject"] for item in result["gmeta"]),
                         sorted(SUBJECTS[key] for key in states))

    def test_no_entries_at_all(self):
        with self.store.app() as db:
            db.execute("DELETE FROM {app}.search_entries")
            result = search_local.search(db, {"q": "*", "facets": [terms("instrument")]})
        self.assertEqual((result["total"], result["count"], result["has_next_page"]),
                         (0, 0, False))
        self.assertEqual(result["gmeta"], [])
        self.assertEqual(result["facet_results"][0]["buckets"], [])

    def test_text_from_a_record_is_only_ever_data(self):
        hostile = search_cases.make_entries()["e01"]
        hostile["subject"] = "urn:pcl:dataset:hostile"
        hostile["session_path"] = "raw/raman-portable/hostile"
        hostile["content"] = dict(hostile["content"], title="'; DROP TABLE search_entries; --",
                                  location="<script>alert(1)</script> \" OR 1=1 *",
                                  tags=["%", "_", "\\", "caf\u00e9 \u6e2c\u8a66"])
        with self.store.app() as db:
            db.insert("app.search_entries", search_cases.entry_row(hostile))
            self.assertEqual(search_local.search(db, {"q": "drop"})["total"], 1)
            self.assertEqual(search_local.search(db, {"q": "\"or 1\""})["total"], 1)
            self.assertEqual(search_local.search(db, {"q": "\u6e2c\u8a66"})["total"], 1)
            self.assertEqual(search_local.search(
                db, {"filters": [any_of("tags", "%")]})["total"], 1)
            self.assertEqual(search_local.search(
                db, {"filters": [any_of("tags", "caf\u00e9 \u6e2c\u8a66")]})["total"], 1)
            self.assertEqual(search_local.search(db, {"q": "*", "limit": 0})["total"], 21)

    def test_an_invalid_request_is_refused_before_the_database_is_read(self):
        with self.store.app() as db:
            with self.assertRaises(PclError) as caught:
                search_local.search(db, {"q": "*", "advanced": True})
        self.assertEqual(caught.exception.code, "bad_search_request")

    def test_paging_walks_through_everything_once(self):
        seen = []
        with self.store.app() as db:
            offset = 0
            while True:
                page = search_local.search(db, {"q": "*", "limit": 7, "offset": offset})
                seen.extend(item["subject"] for item in page["gmeta"])
                self.assertEqual(page["offset"], offset)
                if not page["has_next_page"]:
                    break
                offset += 7
        self.assertEqual(len(seen), 20)
        self.assertEqual(len(set(seen)), 20)
        self.assertEqual(offset, 14)


class TestLocalSqlite(LocalCases, support.SqliteCase, unittest.TestCase):
    pass


class TestLocalPostgres(LocalCases, support.PostgresCase, unittest.TestCase):
    pass


# ---------------------------------------------------------------------- the opt-in live run

LIVE_INDEX = os.environ.get("PCLDB_LIVE_GLOBUS_INDEX", "").strip()
LIVE_ON = os.environ.get("PCLDB_LIVE_GLOBUS") == "1" and bool(LIVE_INDEX)
LIVE_WAIT_SECONDS = 180


@unittest.skipUnless(LIVE_ON, "the live Globus Search run is off: set PCLDB_LIVE_GLOBUS=1, "
                              "PCLDB_LIVE_GLOBUS_INDEX=<index id> and PCLDB_USER_DIR=<the folder "
                              "with globus_tokens.json> to run the shared cases against a real "
                              "index")
class TestLiveGlobusSearch(unittest.TestCase):
    """The shared cases against a real index. Uses the network and a real sign-in."""

    @classmethod
    def setUpClass(cls):
        from pcldb.search_globus import GlobusSearch
        user_dir = os.environ.get("PCLDB_USER_DIR", "").strip()
        if not user_dir:
            raise unittest.SkipTest("the live Globus Search run needs PCLDB_USER_DIR: the folder "
                                    "that holds globus_tokens.json (tests never look into "
                                    "~/.pcldb by themselves)")
        cls.project = support.TempProject("sqlite", {"search.index_id": LIVE_INDEX,
                                                     "search.backend": "globus"})
        cls.addClassCleanup(cls.project.cleanup)
        settings = dataclasses.replace(cls.project.settings,
                                       user_dir=Path(user_dir).expanduser())
        cls.backend = GlobusSearch(settings)
        cls.addClassCleanup(cls.backend.close)
        if not cls.backend.signed_in():
            raise unittest.SkipTest("the live Globus Search run needs a sign-in: run "
                                    "pcldb search login first")
        identity = cls.backend.identity_urn()
        run = uuid.uuid4().hex[:12]
        cls.marker = "pcldb-contract-test-%s folder" % run
        cls.subjects = {}
        entries = []
        for key, entry in search_cases.make_entries().items():
            subject = "urn:pcl:contract-test:%s:%s" % (run, key)
            cls.subjects[key] = subject
            entries.append({"subject": subject, "visible_to": [identity],
                            "content": dict(entry["content"], format=cls.marker)})
        cls.addClassCleanup(cls.remove_entries)
        task_id = cls.backend.ingest(entries)
        deadline = time.monotonic() + LIVE_WAIT_SECONDS
        while True:
            answer = cls.backend.task(task_id)
            if answer["state"] == "SUCCESS":
                break
            if answer["state"] in ("FAILED", "CANCELLED"):
                raise AssertionError("the index did not take the test entries: %s"
                                     % answer["message"])
            if time.monotonic() > deadline:
                raise AssertionError("the index had not taken the test entries after %d s"
                                     % LIVE_WAIT_SECONDS)
            time.sleep(2)
        time.sleep(2)           # a moment for the index to show what the task wrote

    @classmethod
    def remove_entries(cls):
        for subject in cls.subjects.values():
            try:
                cls.backend.delete_subject(subject)
            except PclError:
                pass
            time.sleep(0.15)

    def test_the_shared_cases(self):
        differences = {"observed": [], "assumed": []}
        only_ours = any_of("format", self.marker)
        for one in CASES:
            request = copy.deepcopy(one["request"])
            request["filters"] = list(request.get("filters", [])) + [only_ours]
            time.sleep(0.15)                       # the service allows ten requests a second
            try:
                result = self.backend.search(request)
                check_result(self, one, result, self.subjects)
            except (AssertionError, PclError) as problem:
                differences[one["live"]].append("%s\n    %s" % (
                    search_cases.describe(one), str(problem).replace("\n", "\n    ")))
        if differences["observed"] or differences["assumed"]:
            self.fail(
                "The real service and the stand-in disagree.\n"
                "Cases whose behaviour had been OBSERVED on the service (a bug or a change of "
                "the service):\n  %s\n"
                "Cases the stand-in only ASSUMED (write each into the list of known differences "
                "in pcldb/search_local.py, or change the stand-in):\n  %s"
                % ("\n  ".join(differences["observed"]) or "none",
                   "\n  ".join(differences["assumed"]) or "none"))

    def test_the_result_has_the_structure_the_stand_in_copies(self):
        result = self.backend.search({
            "q": "*", "limit": 2, "filters": [any_of("format", self.marker)],
            "facets": [terms("instrument", name="by_sample")]})
        self.assertEqual(skeleton(result), skeleton(SERVICE_EXAMPLE))


if __name__ == "__main__":
    unittest.main()
