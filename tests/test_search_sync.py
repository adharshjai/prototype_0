"""search_sync: sending the queue to (a fake) Globus Search, and the `pcldb search` command.

Everything here runs on both engines against tests/fake_globus.py. Nothing signs in, nothing
uses the network, nothing waits: `push` gets a clock and a sleep that only count.
"""

from __future__ import annotations

import contextlib
import io
import json
import types
import unittest
import unittest.mock

from pcldb import config, search_sync
from pcldb.errors import PclError, Unavailable
from pcldb.search_globus import GlobusSearch
from pcldb.store import open_store

from tests import search_cases, support
from tests.fake_globus import FAKE_IDENTITY, FakeGlobus
from tests.search_cases import SUBJECTS

IDENTITY = "urn:globus:auth:identity:" + FAKE_IDENTITY
GROUP = "urn:globus:groups:id:11111111-2222-4333-8444-555555555555"
SECRET = "FAKE-SECRET-ACCESS-TOKEN"
FIVE = ("pending", "sent", "synced", "failed", "delete_pending")


class Clock:
    """Time that only moves when somebody sleeps."""

    def __init__(self):
        self.now = 1000.0
        self.sleeps = []

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def args(action, **more):
    values = {"action": action, "no_browser": False, "name": None, "description": None,
              "request": None, "wait_seconds": None}
    values.update(more)
    return types.SimpleNamespace(**values)


class SyncBase:
    """A store with the twenty entries queued, a fake Globus account with one index."""

    def setUp(self):
        super().setUp()
        self.fake = FakeGlobus()
        self.index_id = self.fake.add_index()
        self.settings = self.settings.changed({"search.index_id": self.index_id})
        self.sign_in()
        self.backend = GlobusSearch(self.settings, sdk=self.fake.sdk)
        self.addCleanup(self.backend.close)
        self.clock = Clock()
        with self.store.app() as db:
            search_cases.insert_entries(db)

    def sign_in(self):
        self.fake.signed_in = True
        self.settings.globus_tokens_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings.globus_tokens_path.write_text('{"fake": true}\n', encoding="utf-8")

    def sign_out(self):
        self.fake.signed_in = False
        self.settings.globus_tokens_path.unlink()

    def push(self, **more):
        more.setdefault("sleep", self.clock.sleep)
        more.setdefault("clock", self.clock)
        with self.store.app() as db:
            return search_sync.push(db, self.backend, **more)

    def push_store(self, **more):
        more.setdefault("sleep", self.clock.sleep)
        more.setdefault("clock", self.clock)
        return search_sync.push_store(self.store, self.backend, **more)

    def rows(self):
        with self.store.app() as db:
            found = db.query("SELECT * FROM {app}.search_entries")
        return {row["subject"]: row for row in found}

    def states(self):
        with self.store.app() as db:
            return search_sync.status(db)

    def in_index(self):
        return self.fake.indexes[self.index_id]["entries"]

    def set_rows(self, subjects, **columns):
        names = sorted(columns)
        with self.store.app() as db:
            for subject in subjects:
                db.execute("UPDATE {app}.search_entries SET %s WHERE subject = ?"
                           % ", ".join("%s = ?" % name for name in names),
                           [db.param("strlist", columns[name]) if name == "visible_to"
                            else columns[name] for name in names] + [subject])


class PushCases(SyncBase):

    def test_status_counts_every_state(self):
        self.assertEqual(self.states(), {"pending": 20, "sent": 0, "synced": 0, "failed": 0,
                                         "delete_pending": 0})
        self.assertEqual(tuple(self.states()), FIVE)
        with self.store.app() as db:
            db.execute("DELETE FROM {app}.search_entries")
            self.assertEqual(search_sync.status(db), dict.fromkeys(FIVE, 0))

    def test_push_sends_everything_and_waits_for_success(self):
        counts = self.push()
        self.assertEqual(counts, {"sent": 20, "synced": 20, "failed": 0, "removed": 0,
                                  "requeued": 0, "waiting": 0, "batches": 1, "problems": []})
        self.assertEqual(self.states()["synced"], 20)
        self.assertEqual(self.fake.count("ingest"), 1)
        self.assertEqual(self.fake.count("get_task"), 4)       # CREATING PENDING PROGRESS SUCCESS
        self.assertEqual(self.clock.sleeps, [0.5, 1.0, 2.0, 4.0])
        for subject, row in self.rows().items():
            self.assertEqual(row["state"], "synced")
            self.assertTrue(row["task_id"])
            self.assertIsNone(row["last_error"])
            self.assertRegex(row["synced_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(sorted(self.in_index()), sorted(SUBJECTS.values()))

    def test_what_is_sent_is_exactly_subject_visible_to_content(self):
        self.push()
        entries = search_cases.make_entries()
        document = [call for call in self.fake.calls if call[0] == "ingest"][0][2]
        self.assertEqual(document["ingest_type"], "GMetaList")
        sent = {one["subject"]: one for one in document["ingest_data"]["gmeta"]}
        self.assertEqual(len(sent), 20)
        for key, entry in entries.items():
            one = sent[entry["subject"]]
            self.assertEqual(set(one), {"subject", "visible_to", "content"})
            self.assertEqual(one["content"], entry["content"])

    def test_an_empty_visible_to_becomes_the_signed_in_identity_only(self):
        self.set_rows([SUBJECTS["e02"]], visible_to=[GROUP])
        self.set_rows([SUBJECTS["e03"]], visible_to=[GROUP, IDENTITY])
        self.push()
        index = self.in_index()
        self.assertEqual(index[SUBJECTS["e01"]]["visible_to"], [IDENTITY])
        self.assertEqual(index[SUBJECTS["e02"]]["visible_to"], [GROUP])
        self.assertEqual(index[SUBJECTS["e03"]]["visible_to"], [GROUP, IDENTITY])
        everything = json.dumps([call for call in self.fake.calls if call[0] == "ingest"])
        self.assertNotIn("public", everything)
        self.assertNotIn("all_authenticated_users", everything)
        self.assertEqual(self.rows()[SUBJECTS["e01"]]["visible_to"], [],
                         "the row keeps what the settings said")

    def test_public_is_never_sent(self):
        self.set_rows([SUBJECTS["e02"]], visible_to=["public"])
        self.set_rows([SUBJECTS["e03"]], visible_to=[GROUP, "All_Authenticated_Users"])
        counts = self.push()
        self.assertEqual((counts["sent"], counts["synced"], counts["failed"]), (18, 18, 2))
        rows = self.rows()
        for key in ("e02", "e03"):
            self.assertEqual(rows[SUBJECTS[key]]["state"], "failed")
            self.assertIn("never public", rows[SUBJECTS[key]]["last_error"])
            self.assertNotIn(SUBJECTS[key], self.in_index())

    def test_batches(self):
        counts = self.push(batch=7)
        self.assertEqual((counts["sent"], counts["synced"], counts["batches"]), (20, 20, 3))
        sizes = [len(call[2]["ingest_data"]["gmeta"]) for call in self.fake.calls
                 if call[0] == "ingest"]
        self.assertEqual(sizes, [7, 7, 6])
        subjects = [one["subject"] for call in self.fake.calls if call[0] == "ingest"
                    for one in call[2]["ingest_data"]["gmeta"]]
        self.assertEqual(subjects, sorted(SUBJECTS.values()), "in the order of the subjects")
        self.assertEqual(len({row["task_id"] for row in self.rows().values()}), 3)

    def test_a_batch_is_also_cut_by_its_size(self):
        with unittest.mock.patch.object(search_sync, "BATCH_BYTES", 5000):
            counts = self.push(batch=100)
        self.assertEqual(counts["sent"], 20)
        self.assertGreater(counts["batches"], 3)

    def test_a_failed_task_keeps_its_message(self):
        self.fake.next_tasks(["CREATING", "PROGRESS", "FAILED"],
                             message="mapper_parsing_exception: failed to parse field [items]")
        counts = self.push(batch=10)
        self.assertEqual((counts["sent"], counts["synced"], counts["failed"]), (20, 10, 10))
        rows = self.rows()
        first = sorted(SUBJECTS.values())[:10]
        for subject, row in rows.items():
            if subject in first:
                self.assertEqual(row["state"], "failed")
                self.assertIn("FAILED", row["last_error"])
                self.assertIn("failed to parse field [items]", row["last_error"])
                self.assertIsNone(row["synced_utc"])
            else:
                self.assertEqual(row["state"], "synced")
        self.assertEqual(sorted(self.in_index()), sorted(set(SUBJECTS.values()) - set(first)))
        with self.store.app() as db:
            listed = search_sync.failures(db, limit=3)
        self.assertEqual(len(listed), 3)
        self.assertEqual(set(listed[0]), {"subject", "session_path", "last_error"})

    def test_a_cancelled_task_also_ends(self):
        self.fake.next_tasks(["PENDING", "CANCELLED"])
        counts = self.push()
        self.assertEqual((counts["failed"], counts["waiting"]), (20, 0))
        row = self.rows()[SUBJECTS["e01"]]
        self.assertEqual(row["state"], "failed")
        self.assertIn("CANCELLED", row["last_error"])
        self.assertIn("gave no reason", row["last_error"])

    def test_a_failed_entry_is_not_sent_again_until_reindex(self):
        self.fake.next_tasks(["FAILED"], message="no")
        self.push()
        self.assertEqual(self.states()["failed"], 20)
        counts = self.push()
        self.assertEqual((counts["sent"], self.fake.count("ingest")), (0, 1))
        with self.store.app() as db:
            self.assertEqual(search_sync.reindex(db), 20)
        counts = self.push()
        self.assertEqual((counts["sent"], counts["synced"]), (20, 20))
        self.assertIsNone(self.rows()[SUBJECTS["e01"]]["last_error"])

    def test_the_fake_index_refuses_a_changed_field_type_like_the_service(self):
        self.push()
        entry = search_cases.make_entries()["e01"]
        entry["subject"] = "urn:pcl:dataset:changed-type"
        entry["session_path"] = "raw/raman-portable/changed-type"
        entry["content"] = dict(entry["content"], items="eight")
        with self.store.app() as db:
            db.insert("app.search_entries", search_cases.entry_row(entry))
        counts = self.push()
        self.assertEqual((counts["sent"], counts["failed"]), (1, 1))
        self.assertIn("content.items", self.rows()[entry["subject"]]["last_error"])

    def test_timeout_leaves_sent_and_the_next_push_asks_first(self):
        self.fake.stall()
        counts = self.push(wait_seconds=10)
        self.assertEqual(counts, {"sent": 20, "synced": 0, "failed": 0, "removed": 0,
                                  "requeued": 0, "waiting": 20, "batches": 1, "problems": []})
        self.assertEqual(self.clock.sleeps, [0.5, 1.0, 2.0, 4.0, 2.5], "never past the deadline")
        rows = self.rows()
        task_ids = {row["task_id"] for row in rows.values()}
        self.assertEqual(len(task_ids), 1)
        self.assertTrue(all(row["state"] == "sent" for row in rows.values()))
        self.assertEqual(self.in_index(), {})

        # The second push asks about the old task before anything else and sends nothing again.
        before = len(self.fake.calls)
        self.fake.finish(task_ids.pop())
        counts = self.push(wait_seconds=10)
        later = [call[0] for call in self.fake.calls[before:]]
        self.assertEqual(later, ["login_required", "get_task"])
        self.assertEqual((counts["sent"], counts["synced"], counts["waiting"]), (0, 20, 0))
        self.assertEqual(self.states()["synced"], 20)
        self.assertEqual(len(self.in_index()), 20)

    def test_wait_zero_sends_and_does_not_wait(self):
        counts = self.push(wait_seconds=0)
        self.assertEqual((counts["sent"], counts["waiting"], counts["synced"]), (20, 20, 0))
        self.assertEqual(self.clock.sleeps, [])
        self.assertEqual(self.fake.count("get_task"), 0)
        counts = self.push(wait_seconds=60)
        self.assertEqual((counts["sent"], counts["synced"]), (0, 20))

    def test_a_state_this_program_does_not_know_keeps_waiting(self):
        self.fake.next_tasks(["SOMETHING_NEW"])
        counts = self.push(wait_seconds=5)
        self.assertEqual((counts["waiting"], counts["failed"], counts["synced"]), (20, 0, 0))
        self.assertLessEqual(sum(self.clock.sleeps), 5)

    def test_entries_waiting_to_be_removed(self):
        with self.store.app() as db:
            leaving = search_cases.insert_leaving_entry(db)
        self.assertEqual(self.states()["delete_pending"], 1)
        counts = self.push()
        self.assertEqual((counts["removed"], counts["sent"]), (1, 20))
        self.assertIn(("delete_subject", self.index_id, leaving), self.fake.calls)
        self.assertNotIn(leaving, self.rows())
        self.assertEqual(self.states()["delete_pending"], 0)
        names = self.fake.names()
        self.assertLess(names.index("delete_subject"), names.index("ingest"))

    def test_a_removal_that_fails_keeps_the_row(self):
        with self.store.app() as db:
            leaving = search_cases.insert_leaving_entry(db)
        self.fake.fail("delete_subject", self.fake.network_error())
        counts = self.push()
        self.assertEqual((counts["removed"], counts["sent"]), (0, 0))
        self.assertEqual(len(counts["problems"]), 1)
        self.assertIn("could not be removed", counts["problems"][0])
        self.assertEqual(self.rows()[leaving]["state"], "delete_pending")
        self.assertEqual(self.states()["pending"], 20)

    def test_refused_when_not_signed_in(self):
        self.sign_out()
        before = self.rows()
        with self.assertRaises(Unavailable) as caught:
            self.push()
        self.assertIn("pcldb search login", caught.exception.message)
        self.assertEqual(self.rows(), before)
        self.assertNotIn("ingest", self.fake.names())
        self.assertEqual(self.fake.logins, [], "push never starts a sign-in")

    def test_refused_when_no_index_is_set(self):
        settings = self.settings.changed({"search.index_id": None})
        backend = GlobusSearch(settings, sdk=self.fake.sdk)
        before = self.rows()
        with self.store.app() as db:
            with self.assertRaises(PclError) as caught:
                search_sync.push(db, backend, sleep=self.clock.sleep, clock=self.clock)
        self.assertIn("pcldb search create-index", caught.exception.message)
        self.assertEqual(self.rows(), before)
        self.assertEqual(self.fake.calls, [])

    def test_bad_arguments(self):
        for more in ({"batch": 0}, {"batch": 1001}, {"batch": True}, {"batch": 2.5},
                     {"wait_seconds": -1}, {"wait_seconds": "60"}, {"wait_seconds": 4000}):
            with self.subTest(more):
                with self.assertRaises(PclError):
                    self.push(**more)
        self.assertEqual(self.fake.calls, [])

    def test_the_network_fails_while_sending(self):
        self.fake.fail("ingest", self.fake.network_error())
        counts = self.push(batch=10)
        self.assertEqual((counts["sent"], counts["batches"]), (0, 0))
        self.assertEqual(len(counts["problems"]), 1)
        self.assertIn("could not be reached", counts["problems"][0])
        self.assertNotIn(SECRET, json.dumps(counts))
        self.assertEqual(self.states()["pending"], 20)
        self.assertEqual(self.fake.count("ingest"), 1, "it stops at the first failure")
        counts = self.push(batch=10)
        self.assertEqual((counts["sent"], counts["synced"], counts["problems"]), (20, 20, []))

    def test_the_network_fails_while_waiting(self):
        self.fake.fail("get_task", self.fake.network_error())
        counts = self.push()
        self.assertEqual((counts["sent"], counts["synced"], counts["waiting"]), (20, 0, 20))
        self.assertIn("could not be asked", counts["problems"][0])
        self.assertEqual(self.states()["sent"], 20)
        counts = self.push()
        self.assertEqual((counts["sent"], counts["synced"]), (0, 20))

    def test_a_batch_the_service_refuses_is_marked_and_the_rest_goes_on(self):
        self.fake.fail("ingest", self.fake.api_error(
            400, "BadRequest.ValidationError", "Request failed input validation."))
        counts = self.push(batch=10)
        self.assertEqual((counts["sent"], counts["synced"], counts["failed"]), (10, 10, 10))
        self.assertEqual(counts["problems"], [])
        first = sorted(SUBJECTS.values())[0]
        row = self.rows()[first]
        self.assertEqual(row["state"], "failed")
        self.assertIn("BadRequest.ValidationError", row["last_error"])
        self.assertNotIn(SECRET, row["last_error"])

    def test_no_permission_stops_and_keeps_the_queue(self):
        self.fake.fail("ingest", self.fake.api_error(403, "Forbidden.Generic", "no role"))
        counts = self.push()
        self.assertEqual(counts["sent"], 0)
        self.assertIn("writer role", counts["problems"][0])
        self.assertEqual(self.states()["pending"], 20)

    def test_a_task_globus_no_longer_knows_is_queued_again(self):
        self.fake.stall()
        self.push(wait_seconds=1)
        self.fake.tasks.clear()                     # as after some weeks
        self.fake.all_tasks(["SUCCESS"])
        counts = self.push()
        self.assertEqual(counts["requeued"], 20)
        self.assertEqual((counts["sent"], counts["synced"]), (20, 20))
        self.assertEqual(self.states()["synced"], 20)

    def test_a_sent_row_without_a_task_is_queued_again(self):
        self.set_rows([SUBJECTS["e01"]], state="sent")
        counts = self.push()
        self.assertEqual((counts["requeued"], counts["sent"], counts["synced"]), (1, 20, 20))

    def test_an_entry_that_changed_while_its_task_ran_is_sent_again(self):
        self.fake.stall()
        self.push(wait_seconds=1)
        old_task = self.rows()[SUBJECTS["e05"]]["task_id"]
        # What search_entries.refresh does when the session changed: new content, pending.
        entry = search_cases.make_entries()["e05"]
        entry["content"] = dict(entry["content"], completion="partial")
        changed = search_cases.entry_row(entry)
        with self.store.app() as db:
            db.upsert("app.search_entries", changed, key=("subject",))
        self.fake.all_tasks(["SUCCESS"])
        self.fake.finish(old_task)
        counts = self.push()
        self.assertEqual((counts["synced"], counts["sent"]), (20, 1))
        row = self.rows()[SUBJECTS["e05"]]
        self.assertEqual(row["state"], "synced")
        self.assertNotEqual(row["task_id"], old_task)
        self.assertEqual(self.in_index()[SUBJECTS["e05"]]["content"]["completion"], "partial")

    def test_nothing_to_do(self):
        self.push()
        before = len(self.fake.calls)
        counts = self.push()
        self.assertEqual(counts, {"sent": 0, "synced": 0, "failed": 0, "removed": 0,
                                  "requeued": 0, "waiting": 0, "batches": 0, "problems": []})
        self.assertEqual([call[0] for call in self.fake.calls[before:]], ["login_required"])
        self.assertEqual(self.clock.sleeps[4:], [])

    def test_reindex_queues_everything_but_the_leaving_entries(self):
        self.fake.next_tasks(["FAILED"], message="no")
        self.push(batch=10)
        self.assertEqual(self.states(), {"pending": 0, "sent": 0, "synced": 10, "failed": 10,
                                         "delete_pending": 0})
        with self.store.app() as db:
            leaving = search_cases.insert_leaving_entry(db)
            self.assertEqual(search_sync.reindex(db), 20)
        self.assertEqual(self.states(), {"pending": 20, "sent": 0, "synced": 0, "failed": 0,
                                         "delete_pending": 1})
        rows = self.rows()
        self.assertEqual(rows[leaving]["state"], "delete_pending")
        for subject in SUBJECTS.values():
            self.assertIsNone(rows[subject]["task_id"])
            self.assertIsNone(rows[subject]["last_error"])
            self.assertIsNone(rows[subject]["synced_utc"])

    def test_push_store_keeps_what_was_done_when_a_later_step_fails(self):
        self.fake.fail("ingest", self.fake.network_error(), after=1)   # the first batch passes
        counts = self.push_store(batch=7)
        self.assertEqual((counts["sent"], counts["batches"], counts["waiting"]), (7, 1, 7))
        self.assertEqual(len(counts["problems"]), 1)
        committed = self.outside_rows(
            "SELECT COUNT(*) FROM {app}.search_entries WHERE state = ?", ["sent"])
        self.assertEqual(committed[0][0], 7)
        counts = self.push_store(batch=7)
        self.assertEqual((counts["sent"], counts["synced"]), (13, 20))

    def test_push_in_one_transaction_is_undone_when_the_caller_fails(self):
        with self.assertRaises(RuntimeError):
            with self.store.app() as db:
                search_sync.push(db, self.backend, sleep=self.clock.sleep, clock=self.clock)
                raise RuntimeError("the caller's own problem")
        self.assertEqual(self.states()["pending"], 20, "the caller's transaction decides")

    def test_push_store_holds_no_transaction_while_globus_is_asked(self):
        seen = []

        def on_call(name):
            if name in ("ingest", "get_task", "delete_subject"):
                # A second writer must get in at once: nothing may be holding the database.
                with self.store.app() as db:
                    seen.append((name, search_sync.status(db)["pending"]))

        with self.store.app() as db:
            search_cases.insert_leaving_entry(db)
        self.fake.on_call = on_call
        counts = self.push_store(batch=10)
        self.fake.on_call = None
        self.assertEqual((counts["sent"], counts["synced"], counts["removed"]), (20, 20, 1))
        self.assertEqual([name for name, _pending in seen][:3],
                         ["delete_subject", "ingest", "ingest"])
        self.assertEqual([pending for name, pending in seen if name == "ingest"], [20, 10])


class CommandCases(SyncBase):
    """`pcldb search ...` through cmd_search(args, settings)."""

    def run_cmd(self, action, settings=None, **more):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = search_sync.cmd_search(args(action, **more), settings or self.settings,
                                          backend=self.backend)
        self.assertTrue(out.getvalue().isascii() and err.getvalue().isascii())
        self.assertNotIn(SECRET, out.getvalue() + err.getvalue())
        return code, out.getvalue(), err.getvalue()

    def test_status(self):
        code, out, _err = self.run_cmd("status")
        self.assertEqual(code, 0)
        self.assertIn("Search backend: local", out)
        self.assertIn("Globus Search index: " + self.index_id, out)
        self.assertIn("Index at Globus Search: \"test\", a trial index, 0 entries, 0 of 1 MB used.",
                      out)
        self.assertIn("Globus sign-in: signed in", out)
        self.assertIn("20 waiting to be sent, 0 sent and not confirmed yet, 0 in the index, "
                      "0 failed, 0 waiting to be removed", out)

    def test_status_without_index_and_sign_in(self):
        self.sign_out()
        settings = self.settings.changed({"search.index_id": None})
        self.backend = GlobusSearch(settings, sdk=self.fake.sdk)
        code, out, _err = self.run_cmd("status", settings)
        self.assertEqual(code, 0)
        self.assertIn("Globus Search index: none yet (pcldb search create-index)", out)
        self.assertIn("Globus sign-in: not signed in (pcldb search login)", out)
        self.assertNotIn("Index at Globus Search", out)
        self.assertEqual(self.fake.calls, [], "nothing to ask Globus about")

    def test_status_says_plainly_when_globus_does_not_answer(self):
        self.fake.fail("get_index", self.fake.network_error())
        code, out, _err = self.run_cmd("status")
        self.assertEqual(code, 0)
        self.assertIn("The index could not be asked right now: Globus could not be reached", out)
        self.assertIn("20 waiting to be sent", out)

    def test_status_warns_when_the_index_is_nearly_full(self):
        with unittest.mock.patch.object(self.backend, "index_info", return_value={
                "display_name": "x", "is_trial": False, "status": "open", "num_entries": 900,
                "size_in_mb": 4100.5, "max_size_in_mb": 5000}):
            _code, out, _err = self.run_cmd("status")
        self.assertIn("\"x\", 900 entries, 4100.5 of 5000 MB used.", out)
        self.assertIn("Warning: the index is 82 % full", out)
        self.assertNotIn("trial", out)
        with unittest.mock.patch.object(self.backend, "index_info", return_value={
                "display_name": "x", "is_trial": True, "status": "delete-pending",
                "num_entries": 1, "size_in_mb": 0, "max_size_in_mb": 1}):
            _code, out, _err = self.run_cmd("status")
        self.assertNotIn("Warning", out)
        self.assertIn("The index is not open: Globus Search reports the status delete-pending.",
                      out)

    def test_status_lists_failures_as_plain_text(self):
        self.set_rows([SUBJECTS["e01"]], state="failed",
                      last_error="it broke\n\x1b[31mhere caf\u00e9")
        code, out, _err = self.run_cmd("status")
        self.assertEqual(code, 0)
        self.assertIn("1 failed", out)
        self.assertIn("failed: raw/raman-portable/1d_y_scan_20261001_090000_001: it broke", out)
        self.assertNotIn("\x1b", out)
        self.assertIn("pcldb search reindex", out)

    def test_push(self):
        with unittest.mock.patch("time.sleep", self.clock.sleep):
            code, out, err = self.run_cmd("push")
        self.assertEqual((code, err), (0, ""))
        self.assertIn("Sent 20 entries to Globus Search in 1 batch.", out)
        self.assertIn("Confirmed in the index: 20. Failed: 0. Removed from the index: 0.", out)
        self.assertEqual(self.states()["synced"], 20)

    def test_push_with_wait_zero_says_to_come_back(self):
        code, out, _err = self.run_cmd("push", wait_seconds=0)
        self.assertEqual(code, 0)
        self.assertIn("has not finished 20 entries yet", out)
        self.assertIn("pcldb search push again", out)

    def test_push_reports_failures_with_exit_code_1(self):
        self.fake.next_tasks(["FAILED"], message="no")
        with unittest.mock.patch("time.sleep", self.clock.sleep):
            code, out, err = self.run_cmd("push")
        self.assertEqual(code, 1)
        self.assertIn("Failed: 20.", out)
        self.assertIn("pcldb search status", err)

    def test_push_reports_a_problem_with_exit_code_1(self):
        self.fake.fail("ingest", self.fake.network_error())
        code, _out, err = self.run_cmd("push")
        self.assertEqual(code, 1)
        self.assertIn("Problem: The entries could not be sent: Globus could not be reached", err)

    def test_push_without_sign_in_is_a_pcl_error(self):
        self.sign_out()
        with self.assertRaises(Unavailable):
            self.run_cmd("push")

    def test_status_says_who_really_answers(self):
        # NEW-13: "globus" chosen, but no index or no sign-in: this computer's copy answers.
        # (The fake stands in for globus-sdk, which need not be installed for the tests.)
        import importlib.util
        real_find_spec = importlib.util.find_spec
        sdk_there = unittest.mock.patch(
            "importlib.util.find_spec",
            lambda name, *rest: object() if name == "globus_sdk" else real_find_spec(name, *rest))
        sdk_there.start()
        self.addCleanup(sdk_there.stop)
        chosen = self.settings.changed({"search.backend": "globus"})
        code, out, _err = self.run_cmd("status", chosen)
        self.assertEqual(code, 0)
        self.assertIn("Search backend: globus (questions are answered by Globus Search)", out)
        self.sign_out()
        code, out, _err = self.run_cmd("status", chosen)
        self.assertIn("Search backend: globus is chosen, but until you sign in to Globus (pcldb "
                      "search login) questions are answered from this computer's copy", out)
        no_index = chosen.changed({"search.index_id": None})
        self.backend = GlobusSearch(no_index, sdk=self.fake.sdk)
        self.addCleanup(self.backend.close)
        code, out, _err = self.run_cmd("status", no_index)
        self.assertIn("until an index is set (pcldb search create-index)", out)
        self.assertNotIn("questions are answered by Globus Search", out)

    def test_reindex(self):
        # K-02: reindex builds every entry again with the current settings, then queues all.
        from pcldb import search_entries
        paths = ["raw/raman-portable/1d_y_scan_2026111%d_101500_000" % number
                 for number in range(3)]
        with self.store.app() as db:
            db.delete_where("app.search_entries", "1 = 1")
            for path in paths:
                db.insert("pcl.sessions", support.session_row(path))
            search_entries.refresh(db, None, self.settings)
            db.execute("UPDATE {app}.search_entries SET state = ?", ["synced"])
        before = self.rows()
        self.assertTrue(all(row["visible_to"] == [] for row in before.values()))
        self.assertTrue(all("operator" not in row["content_json"] for row in before.values()))
        changed = self.settings.changed({"search.visible_to": [GROUP],
                                         "search.include_operator": True})
        code, out, _err = self.run_cmd("reindex", changed)
        self.assertEqual(code, 0)
        self.assertIn("Queued 3 entries again", out)
        after = self.rows()
        self.assertEqual(sorted(row["session_path"] for row in after.values()), paths)
        for row in after.values():
            self.assertEqual(row["state"], "pending")
            self.assertEqual(row["visible_to"], [GROUP])
            self.assertEqual(row["content_json"]["operator"], "demo-operator-a")

    def test_login_and_logout(self):
        self.sign_out()
        code, out, _err = self.run_cmd("login")
        self.assertEqual(code, 0)
        self.assertIn("Signed in to Globus", out)
        self.assertIn("globus_tokens.json", out)
        self.assertIn("Next: pcldb search push", out)
        self.assertNotIn(FAKE_IDENTITY, out)
        self.assertEqual(self.fake.logins, ["local-server"])
        code, out, _err = self.run_cmd("logout")
        self.assertEqual(code, 0)
        self.assertIn("Signed out of Globus", out)
        self.assertFalse(self.settings.globus_tokens_path.exists())
        code, out, _err = self.run_cmd("logout")
        self.assertEqual(code, 0)
        self.assertIn("Nobody was signed in", out)

    def test_login_without_a_browser(self):
        self.sign_out()
        self.run_cmd("login", no_browser=True)
        self.assertEqual(self.fake.logins, ["command-line"])

    def test_logout_without_the_network(self):
        self.fake.fail("logout", self.fake.network_error())
        code, out, _err = self.run_cmd("logout")
        self.assertEqual(code, 0)
        self.assertIn("removed from this computer", out)
        self.assertFalse(self.settings.globus_tokens_path.exists())

    def test_create_index_writes_the_id_and_nothing_else(self):
        written = {"_about": "notes stay", "server": {"port": 9911, "_about": {"port": "kept"}},
                   "search": {"visible_to": [GROUP], "_about": "kept too"},
                   "sql": {"max_rows": 77}}
        self.settings.config_path.write_text(
            "\ufeff" + json.dumps(written, indent=2), encoding="utf-8")     # as Notepad saves
        settings = self.settings.changed({"search.index_id": None})
        self.backend = GlobusSearch(settings, sdk=self.fake.sdk)
        self.set_rows(SUBJECTS.values(), state="synced")

        code, out, err = self.run_cmd("create-index", settings)
        self.assertEqual((code, err), (0, ""))
        made = [index_id for index_id in self.fake.indexes if index_id != self.index_id]
        self.assertEqual(len(made), 1)
        self.assertIn("Made a trial Globus Search index: " + made[0], out)
        self.assertEqual(self.fake.calls[-1][:1], ("create_index",))
        self.assertEqual(self.fake.calls[-1][1:], (search_sync.DEFAULT_INDEX_NAME,
                                                   search_sync.DEFAULT_INDEX_DESCRIPTION))

        raw = self.settings.config_path.read_bytes()
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"), "never write a byte order mark")
        saved = json.loads(raw.decode("utf-8"))
        wanted = json.loads(json.dumps(written))
        wanted["search"]["index_id"] = made[0]
        self.assertEqual(saved, wanted)
        self.assertEqual(list(saved), list(written), "the order of the keys stays")
        self.assertNotIn("backend", saved["search"], "the backend is never switched by itself")

        loaded = config.load_settings(project_dir=self.settings.project_dir,
                                      user_dir=self.settings.user_dir, env={})
        self.assertEqual(loaded.search.index_id, made[0])
        self.assertEqual(loaded.search.backend, "local")
        self.assertEqual(loaded.search.visible_to, (GROUP,))
        self.assertEqual(loaded.sql.max_rows, 77)

        # the one command that switches the backend, and the next step
        self.assertIn(search_sync.switch_command("globus"), out)
        self.assertIn("use-globus", out)
        self.assertIn("Next: pcldb search push", out)
        self.assertNotIn("1 MB", out)
        # a new index is empty: everything is queued for it
        self.assertEqual(self.states()["pending"], 20)
        self.assertIn("20 entries are queued", out)

    def test_create_index_without_a_settings_file_makes_one(self):
        settings = self.settings.changed({"search.index_id": None})
        self.backend = GlobusSearch(settings, sdk=self.fake.sdk)
        self.assertFalse(settings.config_path.exists())
        code, _out, _err = self.run_cmd("create-index", settings, name="my name",
                                        description="my description")
        self.assertEqual(code, 0)
        self.assertEqual(self.fake.calls[-1][1:], ("my name", "my description"))
        saved = json.loads(settings.config_path.read_text(encoding="utf-8"))
        self.assertEqual(list(saved), ["search"])
        self.assertEqual(list(saved["search"]), ["index_id"])

    def test_create_index_refuses_when_one_is_set(self):
        with self.assertRaises(PclError) as caught:
            self.run_cmd("create-index")
        self.assertIn("already set", caught.exception.message)
        self.assertNotIn("create_index", self.fake.names())

    def test_create_index_needs_a_sign_in(self):
        self.sign_out()
        settings = self.settings.changed({"search.index_id": None})
        self.backend = GlobusSearch(settings, sdk=self.fake.sdk)
        with self.assertRaises(Unavailable) as caught:
            self.run_cmd("create-index", settings)
        self.assertIn("pcldb search login", caught.exception.message)
        self.assertFalse(settings.config_path.exists())

    def test_create_index_with_a_broken_settings_file_does_not_lose_the_id(self):
        settings = self.settings.changed({"search.index_id": None})
        self.backend = GlobusSearch(settings, sdk=self.fake.sdk)
        settings.config_path.write_text("{ this is not JSON", encoding="utf-8")
        code, out, err = self.run_cmd("create-index", settings)
        self.assertEqual(code, 1)
        made = [index_id for index_id in self.fake.indexes if index_id != self.index_id][0]
        self.assertIn(made, out)
        self.assertIn("\"index_id\": \"%s\"" % made, err)
        self.assertEqual(settings.config_path.read_text(encoding="utf-8"), "{ this is not JSON")

    def test_use_globus_and_use_local_switch_the_backend(self):
        self.settings.config_path.write_text(json.dumps({"sql": {"max_rows": 5}}),
                                             encoding="utf-8")
        code, out, _err = self.run_cmd("use-globus")
        self.assertEqual(code, 0)
        self.assertIn("answered by Globus Search", out)
        saved = json.loads(self.settings.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved, {"sql": {"max_rows": 5}, "search": {"backend": "globus"}})
        code, out, _err = self.run_cmd("use-local")
        self.assertEqual(code, 0)
        saved = json.loads(self.settings.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["search"]["backend"], "local")

    def test_the_switch_command_runs_as_printed(self):
        # Whatever switch_command() prints must exist: here the module's own entry point.
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            with unittest.mock.patch.object(config, "load_settings",
                                            return_value=self.settings) as loader:
                code = search_sync.main(["use-globus", "--dir", str(self.settings.project_dir)])
        self.assertEqual(code, 0, err.getvalue())
        loader.assert_called_once_with(project_dir=str(self.settings.project_dir))
        saved = json.loads(self.settings.config_path.read_text(encoding="utf-8"))
        self.assertEqual(saved["search"]["backend"], "globus")
        command = search_sync.switch_command("globus")
        self.assertTrue(command == "pcldb search use-globus"
                        or command.endswith("-m pcldb.search_sync use-globus"), command)

    def test_query_with_json_text(self):
        request = {"q": "calibration", "sort": [{"field_name": "started", "order": "asc"}]}
        code, out, err = self.run_cmd("query", request=json.dumps(request))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertGreater(out.count("\n"), 20, "printed over several lines, for people")
        self.assertEqual(result["@datatype"], "GSearchResult")
        self.assertEqual([search_cases.KEY_OF[item["subject"]] for item in result["gmeta"]][:2],
                         ["e01", "e16"])
        self.assertEqual(result["total"], 4)
        self.assertIn("this computer's copy", err)

    def test_query_with_a_file(self):
        path = self.project.path / "my request.json"
        path.write_text("\ufeff" + json.dumps({"q": "*", "limit": 0,
                                               "facets": [search_cases.terms("instrument")]}),
                        encoding="utf-8")
        code, out, _err = self.run_cmd("query", request="@" + str(path))
        self.assertEqual(code, 0)
        result = json.loads(out)
        self.assertEqual(result["total"], 20)
        self.assertEqual(result["facet_results"][0]["buckets"][0]["count"], 13)

    def test_query_through_globus(self):
        self.push()
        settings = self.settings.changed({"search.backend": "globus"})
        code, out, err = self.run_cmd("query", settings, request='{"q": "wafer"}')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["total"], 3)
        self.assertIn("Answered by: Globus Search", err)
        self.assertEqual(self.fake.calls[-1][0], "post_search")

    def test_query_falls_back_with_a_note(self):
        settings = self.settings.changed({"search.backend": "globus"})
        self.fake.fail("post_search", self.fake.network_error())
        code, out, err = self.run_cmd("query", settings, request='{"q": "wafer"}')
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(out)["total"], 3)
        self.assertIn("Note: Globus Search could not be reached; these results come from this "
                      "computer's copy of the index.", err)

    def test_query_refuses_bad_input(self):
        for request, piece in ((None, "Give the request"), ("", "Give the request"),
                               ("{not json", "not valid JSON"), ("[1, 2]", "JSON object"),
                               ('{"q": "*", "limit": NaN}', "not valid JSON"),
                               ('{"q": "*", "boosts": []}', "boosts"),
                               ("@" + str(self.project.path / "missing.json"), "does not exist")):
            with self.subTest(request):
                with self.assertRaises(PclError) as caught:
                    self.run_cmd("query", request=request)
                self.assertIn(piece, caught.exception.message)

    def test_an_unknown_action(self):
        with self.assertRaises(PclError) as caught:
            self.run_cmd("explode")
        self.assertIn("status, push, reindex, login, logout, create-index, query",
                      caught.exception.message)

    def test_commands_that_need_the_database_say_so_when_it_is_not_set_up(self):
        self.store.destroy()
        for action in ("status", "push", "reindex", "query"):
            with self.subTest(action):
                with self.assertRaises(PclError) as caught:
                    self.run_cmd(action, request='{"q": "*"}')
                self.assertIn("pcldb setup", caught.exception.message)

    def test_cmd_search_builds_its_own_backend_from_the_settings(self):
        # Without a sign-in and without an index the real GlobusSearch is never asked anything.
        self.sign_out()
        settings = self.settings.changed({"search.index_id": None})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = search_sync.cmd_search(args("status"), settings)
        self.assertEqual(code, 0)
        self.assertIn("not signed in", out.getvalue())
        store = open_store(settings)
        self.assertTrue(store.is_initialized())


class TestPushSqlite(PushCases, support.SqliteCase, unittest.TestCase):
    pass


class TestPushPostgres(PushCases, support.PostgresCase, unittest.TestCase):
    pass


class TestCommandSqlite(CommandCases, support.SqliteCase, unittest.TestCase):
    pass


class TestCommandPostgres(CommandCases, support.PostgresCase, unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
