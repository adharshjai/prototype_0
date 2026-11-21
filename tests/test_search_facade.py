"""search.run: one door to the stand-in or to Globus Search, with the fallback. Both engines."""

from __future__ import annotations

import unittest
import unittest.mock

from pcldb import search, search_globus, search_local
from pcldb.errors import PclError, Unavailable
from pcldb.search_globus import GlobusSearch

from tests import search_cases, support
from tests.fake_globus import FAKE_IDENTITY, FakeGlobus
from tests.search_cases import CASES, SUBJECTS, any_of, check_result

IDENTITY = "urn:globus:auth:identity:" + FAKE_IDENTITY
SECRET = "FAKE-SECRET-ACCESS-TOKEN"
SPEC_NOTICE = ("Globus Search could not be reached; these results come from this computer's "
               "copy of the index.")
WAFER = {"q": "wafer", "sort": [{"field_name": "started", "order": "asc"}]}
WAFER_KEYS = ["e11", "e12", "e14"]


def keys(result):
    return [search_cases.KEY_OF[item["subject"]] for item in result["gmeta"]]


class FacadeCases:

    def setUp(self):
        super().setUp()
        self.fake = FakeGlobus()
        self.index_id = self.fake.add_index()
        self.local = self.settings.changed({"search.index_id": self.index_id})
        self.globus = self.local.changed({"search.backend": "globus"})
        self.strict = self.globus.changed({"search.fallback_to_local": False})
        self.settings.globus_tokens_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.globus_tokens_path.write_text('{"fake": true}\n', encoding="utf-8")
        self.backend = GlobusSearch(self.globus, sdk=self.fake.sdk)
        self.addCleanup(self.backend.close)
        entries = search_cases.make_entries()
        with self.store.app() as db:
            search_cases.insert_entries(db, entries)
        # What Globus Search holds: everything but e14, so the two backends can be told apart.
        self.fake.indexes[self.index_id]["entries"] = {
            entry["subject"]: {"subject": entry["subject"], "visible_to": [IDENTITY],
                               "content": entry["content"]}
            for key, entry in entries.items() if key != "e14"}

    def run_search(self, request, settings, **more):
        with self.store.app() as db:
            return search.run(db, request, settings, **more)

    # ------------------------------------------------------------------ local

    def test_local_backend(self):
        result, used, notices = self.run_search(WAFER, self.local, globus=self.backend)
        self.assertEqual((keys(result), used, notices), (WAFER_KEYS, "local", []))
        self.assertEqual(self.fake.calls, [], "the local backend never touches Globus")

    def test_local_backend_needs_no_globus_object_and_no_sdk(self):
        with unittest.mock.patch.object(search_globus, "load_sdk",
                                        side_effect=AssertionError("not needed")):
            result, used, notices = self.run_search(WAFER, self.settings)
        self.assertEqual((keys(result), used, notices), (WAFER_KEYS, "local", []))

    def test_every_shared_case_through_the_facade(self):
        with self.store.app() as db:
            for one in CASES:
                with self.subTest(one["name"]):
                    result, used, notices = search.run(db, one["request"], self.local)
                    check_result(self, one, result)
                    self.assertEqual((used, notices), ("local", []))

    def test_the_result_is_the_stand_ins_result(self):
        with self.store.app() as db:
            direct = search_local.search(db, WAFER)
            through, _used, _notices = search.run(db, WAFER, self.local)
        self.assertEqual(through, direct)

    # ------------------------------------------------------------------ globus

    def test_globus_backend(self):
        result, used, notices = self.run_search(WAFER, self.globus, globus=self.backend)
        self.assertEqual((keys(result), used, notices), (["e11", "e12"], "globus", []))
        name, index_id, sent = self.fake.calls[-1]
        self.assertEqual((name, index_id), ("post_search", self.index_id))
        self.assertEqual(sent, search.validate_request(WAFER), "the normalised request is sent")

    def test_globus_failing_with_fallback_gives_the_local_result_and_a_notice(self):
        self.fake.fail("post_search", self.fake.network_error())
        result, used, notices = self.run_search(WAFER, self.globus, globus=self.backend)
        self.assertEqual(keys(result), WAFER_KEYS)
        self.assertEqual(used, "local")
        self.assertEqual(notices, [SPEC_NOTICE])
        self.assertEqual(search.FALLBACK_NOTICE, SPEC_NOTICE)

    def test_globus_failing_without_fallback_is_unavailable(self):
        self.fake.fail("post_search", self.fake.network_error())
        with self.assertRaises(Unavailable) as caught:
            self.run_search(WAFER, self.strict, globus=self.backend)
        self.assertEqual(caught.exception.status, 503)
        self.assertNotIn(SECRET, caught.exception.message)

    def test_a_busy_service_falls_back_too(self):
        self.fake.fail("post_search", self.fake.api_error(503, "ServiceUnavailable", "busy"))
        _result, used, notices = self.run_search(WAFER, self.globus, globus=self.backend)
        self.assertEqual((used, notices), ("local", [SPEC_NOTICE]))

    def test_not_signed_in(self):
        self.fake.signed_in = False
        result, used, notices = self.run_search(WAFER, self.globus, globus=self.backend)
        self.assertEqual((keys(result), used), (WAFER_KEYS, "local"))
        self.assertEqual(len(notices), 1)
        self.assertIn("pcldb search login", notices[0])
        self.assertIn("this computer's copy of the index", notices[0])
        with self.assertRaises(Unavailable) as caught:
            self.run_search(WAFER, self.strict, globus=self.backend)
        self.assertIn("pcldb search login", caught.exception.message)
        self.assertEqual(self.fake.logins, [], "a search never starts a sign-in")

    def test_no_index_set(self):
        settings = self.globus.changed({"search.index_id": None})
        backend = GlobusSearch(settings, sdk=self.fake.sdk)
        result, used, notices = self.run_search(WAFER, settings, globus=backend)
        self.assertEqual((keys(result), used), (WAFER_KEYS, "local"))
        self.assertIn("pcldb search create-index", notices[0])
        with self.assertRaises(PclError) as caught:
            self.run_search(WAFER, settings.changed({"search.fallback_to_local": False}),
                            globus=backend)
        self.assertIn("pcldb search create-index", caught.exception.message)

    def test_a_request_the_service_refuses(self):
        # The service answers a sort on a field it has never seen with 400; the stand-in can
        # still answer.
        self.fake.fail("post_search", self.fake.api_error(
            400, "BadRequest.QueryString", "No mapping found for [last_run] in order to sort on"),
            times=2)
        result, used, notices = self.run_search(WAFER, self.globus, globus=self.backend)
        self.assertEqual((keys(result), used), (WAFER_KEYS, "local"))
        self.assertIn("No mapping found", notices[0])
        self.assertIn("BadRequest.QueryString", notices[0])
        self.assertTrue(notices[0].endswith("These results come from this computer's copy of "
                                            "the index."))
        with self.assertRaises(PclError) as caught:
            self.run_search(WAFER, self.strict, globus=self.backend)
        self.assertEqual(caught.exception.status, 400)
        self.assertEqual(caught.exception.code, "BadRequest.QueryString")

    def test_an_invalid_request_is_refused_before_any_backend(self):
        bad = {"q": "*", "filters": [any_of("no_such_field", "x")]}
        for settings in (self.local, self.globus, self.strict):
            with self.assertRaises(PclError) as caught:
                self.run_search(bad, settings, globus=self.backend)
            self.assertEqual(caught.exception.code, "bad_search_request")
        self.assertEqual(self.fake.calls, [], "nothing reaches the service unchecked")

    def test_without_a_globus_argument_the_shared_one_is_used(self):
        with unittest.mock.patch.object(search_globus, "shared",
                                        return_value=self.backend) as shared:
            result, used, _notices = self.run_search(WAFER, self.globus)
        shared.assert_called_once_with(self.globus)
        self.assertEqual((keys(result), used), (["e11", "e12"], "globus"))

    def test_without_the_sdk_the_fallback_still_answers(self):
        with unittest.mock.patch.dict("sys.modules", {"globus_sdk": None}):
            backend = GlobusSearch(self.globus)
            result, used, notices = self.run_search(WAFER, self.globus, globus=backend)
            self.assertEqual((keys(result), used), (WAFER_KEYS, "local"))
            self.assertEqual(len(notices), 1)
            with self.assertRaises(Unavailable) as caught:
                self.run_search(WAFER, self.strict, globus=backend)
            self.assertIn("requirements-globus.txt", caught.exception.message)

    # ------------------------------------------------------------------ run_store

    def test_run_store_gives_the_same_answers(self):
        for settings, wanted in ((self.local, (WAFER_KEYS, "local", [])),
                                 (self.globus, (["e11", "e12"], "globus", []))):
            result, used, notices = search.run_store(self.store, WAFER, settings,
                                                     globus=self.backend)
            self.assertEqual((keys(result), used, notices), wanted)
        self.fake.fail("post_search", self.fake.network_error())
        result, used, notices = search.run_store(self.store, WAFER, self.globus,
                                                 globus=self.backend)
        self.assertEqual((keys(result), used, notices), (WAFER_KEYS, "local", [SPEC_NOTICE]))

    def test_run_store_holds_no_transaction_while_globus_is_asked(self):
        seen = []

        def on_call(name):
            if name == "post_search":
                with self.store.app() as db:       # must get in at once
                    seen.append(db.scalar("SELECT COUNT(*) FROM {app}.search_entries"))

        self.fake.on_call = on_call
        search.run_store(self.store, WAFER, self.globus, globus=self.backend)
        self.fake.on_call = None
        self.assertEqual(seen, [20])

    def test_after_a_push_both_backends_agree(self):
        from pcldb import search_sync
        self.fake.indexes[self.index_id]["entries"].clear()
        self.fake.all_tasks(["SUCCESS"])
        counts = search_sync.push_store(self.store, self.backend, sleep=lambda seconds: None)
        self.assertEqual(counts["synced"], 20)
        for one in CASES[:25]:
            with self.subTest(one["name"]):
                here, _used, _n = search.run_store(self.store, one["request"], self.local)
                there, used, _n = search.run_store(self.store, one["request"], self.globus,
                                                   globus=self.backend)
                self.assertEqual(used, "globus")
                self.assertEqual(sorted(item["subject"] for item in here["gmeta"]),
                                 sorted(item["subject"] for item in there["gmeta"]))
        self.assertEqual(len(SUBJECTS), 20)


class TestFacadeSqlite(FacadeCases, support.SqliteCase, unittest.TestCase):
    pass


class TestFacadePostgres(FacadeCases, support.PostgresCase, unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
