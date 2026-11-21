"""search_globus.GlobusSearch on top of the fake SDK, and once on the real SDK without a sign-in.

No test here signs in or reaches the network. The tests that use the installed SDK block name
lookups and connections first, so a mistake fails at once instead of going out.
"""

from __future__ import annotations

import inspect
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from pcldb import search_globus
from pcldb.config import Settings
from pcldb.errors import Forbidden, NotFound, PclError, TooLarge, Unavailable
from pcldb.search_globus import GlobusSearch

from tests import fake_globus
from tests.fake_globus import FAKE_IDENTITY, FakeGlobus
from tests.search_cases import any_of, make_entries

SECRET = "FAKE-SECRET-ACCESS-TOKEN"
PROJECT = Path(__file__).resolve().parent.parent
IDENTITY = "urn:globus:auth:identity:" + FAKE_IDENTITY
GROUP = "urn:globus:groups:id:11111111-2222-4333-8444-555555555555"

try:
    import globus_sdk
except ImportError:                      # the bare-Python test runs still pass
    globus_sdk = None


def entry(key="e01", visible_to=None):
    one = make_entries()[key]
    return {"subject": one["subject"], "visible_to": visible_to or [IDENTITY],
            "content": one["content"]}


class GlobusCase(unittest.TestCase):
    """A temporary project, a fake Globus account with one index, a GlobusSearch on top."""

    signed_in = True

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pcldb-globus-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.fake = FakeGlobus(signed_in=self.signed_in)
        self.index_id = self.fake.add_index()
        self.settings = Settings.for_test(self.tmp, changes={"search.index_id": self.index_id})
        if self.signed_in:
            self.write_token_file(self.settings)
        self.backend = GlobusSearch(self.settings, sdk=self.fake.sdk)
        self.addCleanup(self.backend.close)

    @staticmethod
    def write_token_file(settings):
        settings.globus_tokens_path.parent.mkdir(parents=True, exist_ok=True)
        settings.globus_tokens_path.write_text('{"fake": true}\n', encoding="utf-8")


class TestBuilding(GlobusCase):

    def test_nothing_is_built_until_it_is_needed(self):
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.backend.index_id, self.index_id)
        self.assertEqual(self.backend.token_path, self.settings.user_dir / "globus_tokens.json")
        self.assertEqual(self.fake.calls, [])

    def test_the_app_and_client_are_built_as_the_specification_says(self):
        self.assertTrue(self.backend.signed_in())
        app, client = self.backend._app, self.backend._client
        self.assertEqual(app.app_name, "pcldb")
        self.assertEqual(app.client_id, self.settings.search.client_id)
        self.assertEqual(app.client_id, search_globus.DEFAULT_CLIENT_ID)
        given = app.config.given
        self.assertEqual(set(given), {"request_refresh_tokens", "login_flow_manager",
                                      "token_storage", "token_validation_error_handler"})
        self.assertIs(given["request_refresh_tokens"], True)
        self.assertEqual(given["login_flow_manager"], "local-server")
        self.assertIsNone(given["token_validation_error_handler"])
        self.assertEqual(given["token_storage"].filepath,
                         self.settings.user_dir / "globus_tokens.json")
        self.assertIs(client.app, app)
        self.assertEqual(client.app_scopes, [self.fake.sdk.search_scope])

    def test_built_once_and_reused(self):
        self.backend.signed_in()
        self.backend.index_info()
        self.backend.index_info()
        self.assertEqual(self.fake.count("UserApp"), 1)
        self.assertEqual(self.fake.count("SearchClient"), 1)

    def test_another_client_id_from_the_settings(self):
        other = "0b1c2d3e-0000-4000-8000-123456789abc"
        settings = self.settings.changed({"search.client_id": other})
        backend = GlobusSearch(settings, sdk=self.fake.sdk)
        backend.signed_in()
        self.assertEqual(backend._app.client_id, other)

    def test_close_gives_everything_back(self):
        self.backend.signed_in()
        app, client, storage = self.backend._app, self.backend._client, self.backend._storage
        self.backend.close()
        self.assertTrue(app.closed and client.closed and storage.closed)
        self.assertTrue(self.backend.signed_in(), "it builds again when asked")

    def test_shared_gives_one_object_per_settings(self):
        first = search_globus.shared(self.settings)
        self.assertIs(search_globus.shared(self.settings), first)
        other = search_globus.shared(self.settings.changed(
            {"search.index_id": "99999999-0000-4000-8000-000000000000"}))
        self.assertIsNot(other, first)


class TestSignIn(GlobusCase):

    signed_in = False

    def test_not_signed_in_without_a_token_file_and_without_touching_the_sdk(self):
        self.assertFalse(self.backend.signed_in())
        self.assertEqual(self.fake.calls, [])
        self.assertFalse(self.settings.user_dir.exists(), "asking must not make the folder")

    def test_signed_in_never_starts_a_sign_in(self):
        self.write_token_file(self.settings)          # a file, but the account says no
        self.assertFalse(self.backend.signed_in())
        self.assertEqual(self.fake.logins, [])
        self.assertNotIn("login", self.fake.names())

    def test_signed_in_never_raises(self):
        self.write_token_file(self.settings)
        self.fake.sdk.UserApp = mock.Mock(side_effect=RuntimeError("broken"))
        self.assertFalse(self.backend.signed_in())

    def test_login_with_the_browser(self):
        urn = self.backend.login()
        self.assertEqual(urn, IDENTITY)
        self.assertEqual(self.fake.logins, ["local-server"])
        self.assertTrue(self.settings.globus_tokens_path.is_file())
        self.assertTrue(self.backend.signed_in())
        self.assertEqual(self.backend.identity_urn(), IDENTITY)
        if os.name != "nt":
            self.assertEqual(self.settings.globus_tokens_path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.settings.user_dir.stat().st_mode & 0o777, 0o700)

    def test_login_without_a_browser(self):
        self.backend.login(no_browser=True)
        self.assertEqual(self.fake.logins, ["command-line"])
        self.assertEqual(self.backend._app.config.given["login_flow_manager"], "command-line")

    def test_login_twice_signs_in_once(self):
        self.backend.login()
        self.backend.login()
        self.assertEqual(self.fake.logins, ["local-server"])

    def test_login_that_cannot_run(self):
        self.fake.fail("login", fake_globus.FakeLoginError("no browser in a remote session"))
        with self.assertRaises(PclError) as caught:
            self.backend.login()
        self.assertIn("pcldb search login --no-browser", caught.exception.message)

    def test_identity_needs_a_sign_in(self):
        with self.assertRaises(Unavailable) as caught:
            self.backend.identity_urn()
        self.assertIn("pcldb search login", caught.exception.message)

    def test_logout_removes_the_tokens(self):
        self.backend.login()
        self.assertIs(self.backend.logout(), True)
        self.assertFalse(self.settings.globus_tokens_path.exists())
        self.assertFalse(self.backend.signed_in())
        self.assertIn("logout", self.fake.names())

    def test_logout_without_the_network_still_removes_the_tokens(self):
        self.backend.login()
        self.fake.fail("logout", self.fake.network_error())
        self.assertIs(self.backend.logout(), False)
        self.assertFalse(self.settings.globus_tokens_path.exists())

    def test_logout_when_nobody_is_signed_in(self):
        self.assertIs(self.backend.logout(), False)
        self.assertEqual(self.fake.calls, [])

    def test_calls_without_a_sign_in_say_what_to_run(self):
        for name, call in (("search", lambda: self.backend.search({"q": "*"})),
                           ("ingest", lambda: self.backend.ingest([entry()])),
                           ("delete", lambda: self.backend.delete_subject("urn:x")),
                           ("create", lambda: self.backend.create_index("a", "b"))):
            with self.subTest(name):
                with self.assertRaises(Unavailable) as caught:
                    call()
                self.assertIn("Run: pcldb search login", caught.exception.message)
                self.assertEqual(caught.exception.status, 503)


class TestIndexAndEntries(GlobusCase):

    def test_create_index(self):
        index_id = self.backend.create_index("pcldb prototype", "Prototype index.")
        self.assertIn(index_id, self.fake.indexes)
        self.assertEqual(self.fake.calls[-1], ("create_index", "pcldb prototype",
                                               "Prototype index."))

    def test_create_index_checks_the_texts(self):
        for name, description in (("", "d"), ("n" * 129, "d"), ("n", ""), ("n", "d" * 1001),
                                  (None, "d"), ("n", 5)):
            with self.subTest(name=name, description=description):
                with self.assertRaises(PclError):
                    self.backend.create_index(name, description)
        self.assertNotIn("create_index", self.fake.names())

    def test_the_fourth_trial_index_is_refused_in_plain_words(self):
        self.backend.create_index("a", "b")
        self.backend.create_index("a", "b")
        with self.assertRaises(PclError) as caught:
            self.backend.create_index("a", "b")
        self.assertIn("at most 3 trial indices", caught.exception.message)
        self.assertIn("Conflict.TrialIndexLimit", caught.exception.message)

    def test_index_info(self):
        info = self.backend.index_info()
        self.assertEqual(info["id"], self.index_id)
        self.assertIs(info["is_trial"], True)
        self.assertEqual(info["num_entries"], 0)
        self.assertIsNotNone(self.backend._app)

    def test_index_info_needs_no_sign_in(self):
        self.fake.signed_in = False
        self.settings.globus_tokens_path.unlink()
        backend = GlobusSearch(self.settings, sdk=self.fake.sdk)
        info = backend.index_info()
        self.assertEqual(info["id"], self.index_id)
        self.assertEqual(self.fake.count("UserApp"), 0, "no sign-in machinery for public facts")
        self.assertEqual(self.fake.calls[0], ("SearchClient", ()))
        backend.close()

    def test_no_index_id_names_the_command(self):
        backend = GlobusSearch(Settings.for_test(self.tmp), sdk=self.fake.sdk)
        for call in (backend.index_info, lambda: backend.ingest([entry()]),
                     lambda: backend.delete_subject("urn:x"), lambda: backend.search({"q": "*"})):
            with self.assertRaises(PclError) as caught:
                call()
            self.assertIn("pcldb search create-index", caught.exception.message)
        self.assertNotIn("ingest", self.fake.names())

    def test_ingest_sends_one_gmeta_list_of_exact_entries(self):
        entries = [entry("e01"), entry("e02", [GROUP])]
        task_id = self.backend.ingest(entries)
        self.assertIn(task_id, self.fake.tasks)
        name, index_id, document = self.fake.calls[-1]
        self.assertEqual((name, index_id), ("ingest", self.index_id))
        self.assertEqual(set(document), {"ingest_type", "ingest_data"})
        self.assertEqual(document["ingest_type"], "GMetaList")
        self.assertEqual(set(document["ingest_data"]), {"gmeta"})
        self.assertEqual(document["ingest_data"]["gmeta"], entries)
        for one in document["ingest_data"]["gmeta"]:
            self.assertEqual(set(one), {"subject", "visible_to", "content"})

    def test_ingest_refuses_what_must_never_be_sent(self):
        good = entry()
        bad = [
            ("public", dict(good, visible_to=["public"]), "never public"),
            ("public in capitals", dict(good, visible_to=[" Public "]), "never public"),
            ("all users", dict(good, visible_to=[IDENTITY, "all_authenticated_users"]),
             "never public"),
            ("empty visible_to", dict(good, visible_to=[]), "visible_to must name"),
            ("not a principal", dict(good, visible_to=["demo-operator-a"]), "neither a Globus"),
            ("extra key", dict(good, id="x"), "exactly the keys"),
            ("missing content", {"subject": "s", "visible_to": [IDENTITY]}, "exactly the keys"),
            ("empty subject", dict(good, subject=""), "subject"),
            ("long subject", dict(good, subject="s" * 501), "subject"),
            ("content not an object", dict(good, content=["x"]), "content"),
        ]
        for name, one, piece in bad:
            with self.subTest(name):
                with self.assertRaises(PclError) as caught:
                    self.backend.ingest([entry("e02"), one])
                self.assertIn(piece, caught.exception.message)
                self.assertIn("entry 2", caught.exception.message)
        with self.assertRaises(PclError):
            self.backend.ingest([])
        self.assertNotIn("ingest", self.fake.names())

    def test_ingest_refuses_a_batch_that_is_too_big(self):
        big = entry()
        big["content"] = dict(big["content"], notes="x" * (search_globus.MAX_INGEST_BYTES + 1))
        with self.assertRaises(TooLarge):
            self.backend.ingest([big])
        self.assertNotIn("ingest", self.fake.names())

    def test_task_states_play_until_success(self):
        task_id = self.backend.ingest([entry()])
        states = [self.backend.task(task_id)["state"] for _ in range(5)]
        self.assertEqual(states, ["CREATING", "PENDING", "PROGRESS", "SUCCESS", "SUCCESS"])
        self.assertEqual(set(self.backend.task(task_id)), {"state", "message"})
        self.assertIn(entry()["subject"], self.fake.indexes[self.index_id]["entries"])

    def test_task_failure_keeps_the_message(self):
        self.fake.next_tasks(["PROGRESS", "FAILED"], message="mapper_parsing_exception: items")
        task_id = self.backend.ingest([entry()])
        self.assertEqual(self.backend.task(task_id), {"state": "PROGRESS",
                                                      "message": "mapper_parsing_exception: items"})
        answer = self.backend.task(task_id)
        self.assertEqual(answer["state"], "FAILED")
        self.assertIn("mapper_parsing_exception", answer["message"])
        self.assertEqual(self.fake.indexes[self.index_id]["entries"], {})

    def test_the_fake_fixes_field_types_like_the_service(self):
        first = self.backend.ingest([entry("e01")])
        while self.backend.task(first)["state"] != "SUCCESS":
            pass
        changed = entry("e02")
        changed["content"] = dict(changed["content"], items="eight")
        second = self.backend.ingest([changed])
        answer = {"state": None}
        while answer["state"] not in ("SUCCESS", "FAILED"):
            answer = self.backend.task(second)
        self.assertEqual(answer["state"], "FAILED")
        self.assertIn("content.items", answer["message"])

    def test_delete_subject(self):
        subject = entry()["subject"]
        first = self.backend.ingest([entry()])
        while self.backend.task(first)["state"] != "SUCCESS":
            pass
        task_id = self.backend.delete_subject(subject)
        self.assertEqual(self.fake.calls[-1], ("delete_subject", self.index_id, subject))
        while self.backend.task(task_id)["state"] != "SUCCESS":
            pass
        self.assertEqual(self.fake.indexes[self.index_id]["entries"], {})

    def test_search_checks_the_request_before_anything_is_sent(self):
        with self.assertRaises(PclError) as caught:
            self.backend.search({"q": "*", "bypass_visible_to": True})
        self.assertEqual(caught.exception.code, "bad_search_request")
        self.assertNotIn("post_search", self.fake.names())

    def test_search_sends_the_normalised_request_and_returns_a_plain_dict(self):
        first = self.backend.ingest([entry("e01"), entry("e11")])
        while self.backend.task(first)["state"] != "SUCCESS":
            pass
        result = self.backend.search({"filters": [any_of("instrument", "raman-portable")],
                                      "sort": [{"field_name": "started"}]})
        self.assertIs(type(result), dict)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["gmeta"][0]["subject"], entry("e01")["subject"])
        name, index_id, sent = self.fake.calls[-1]
        self.assertEqual((name, index_id), ("post_search", self.index_id))
        self.assertEqual(sent, {"@version": "query#1.0.0", "limit": 10, "offset": 0,
                                "filters": [any_of("instrument", "raman-portable")],
                                "sort": [{"field_name": "started", "order": "desc"}]})

    def test_an_answer_that_is_no_result_is_unavailable(self):
        self.fake.search_result = {"unexpected": True}
        with self.assertRaises(Unavailable):
            self.backend.search({"q": "*"})


class TestErrors(GlobusCase):

    def check_no_secret(self, error):
        text = json.dumps(error.to_dict()) + str(error) + repr(error)
        self.assertNotIn(SECRET, text)
        self.assertNotIn("Bearer", text)
        self.assertTrue(error.message.isascii())

    def test_a_400_keeps_the_code_and_message_of_the_service(self):
        data = {"messages": {"json": {"sort": ["Unknown field."]}}}
        self.fake.fail("post_search", self.fake.api_error(
            400, "BadRequest.QueryString", "Failed to parse query string", data))
        with self.assertRaises(PclError) as caught:
            self.backend.search({"q": "*"})
        error = caught.exception
        self.assertEqual(error.status, 400)
        self.assertEqual(error.code, "BadRequest.QueryString")
        self.assertIn("Failed to parse query string", error.message)
        self.assertEqual(error.details, {"error_data": data})
        self.assertNotIsInstance(error, Unavailable)
        self.check_no_secret(error)

    def test_other_http_answers(self):
        table = [
            (401, "Unauthorized.Generic", Unavailable, 503,
             "Your Globus sign-in has expired. Run: pcldb search login"),
            (403, "Forbidden.Generic", Forbidden, 403, "writer role"),
            (404, "NotFound.NoSuchIndex", NotFound, 404, "pcldb search create-index"),
            (429, "TooManyRequests", Unavailable, 503, "Try again"),
            (500, "InternalError", Unavailable, 503, "Try again"),
            (503, "ServiceUnavailable", Unavailable, 503, "Try again"),
            (409, "Conflict.Generic", PclError, 502, "Conflict.Generic"),
        ]
        for http_status, code, kind, status, piece in table:
            with self.subTest(http_status):
                self.fake.fail("get_index", self.fake.api_error(http_status, code, "said no"))
                with self.assertRaises(kind) as caught:
                    self.backend.index_info()
                self.assertEqual(caught.exception.status, status)
                self.assertIn(piece, caught.exception.message)
                self.check_no_secret(caught.exception)

    def test_network_failure(self):
        self.fake.fail("ingest", self.fake.network_error())
        with self.assertRaises(Unavailable) as caught:
            self.backend.ingest([entry()])
        self.assertIn("could not be reached", caught.exception.message)
        self.check_no_secret(caught.exception)

    def test_token_problems_name_the_login_command(self):
        for name, error in (("token", self.fake.token_error()), ("auth", self.fake.auth_error())):
            with self.subTest(name):
                self.fake.fail("get_task", error)
                with self.assertRaises(Unavailable) as caught:
                    self.backend.task("t")
                self.assertIn("Run: pcldb search login", caught.exception.message)
                self.check_no_secret(caught.exception)

    def test_any_other_sdk_error_is_a_pcl_error_without_details(self):
        self.fake.fail("get_index", fake_globus.FakeGlobusError("internal " + SECRET))
        with self.assertRaises(PclError) as caught:
            self.backend.index_info()
        self.assertEqual(caught.exception.status, 502)
        self.check_no_secret(caught.exception)

    def test_a_damaged_token_file_names_the_way_out(self):
        self.fake.sdk.JSONTokenStorage = mock.Mock(side_effect=ValueError("not JSON " + SECRET))
        with self.assertRaises(PclError) as caught:
            self.backend.task("t")
        self.assertIn("pcldb search logout", caught.exception.message)
        self.check_no_secret(caught.exception)

    def test_an_answer_without_a_task_id(self):
        with mock.patch.object(fake_globus.FakeSearchClient, "ingest",
                               return_value=fake_globus.FakeResponse({"acknowledged": True})):
            with self.assertRaises(PclError) as caught:
                self.backend.ingest([entry()])
        self.assertIn("no task", caught.exception.message)

    def test_one_lock_around_every_call(self):
        active = []
        most = []
        guard = threading.Lock()

        def on_call(name):
            with guard:
                active.append(name)
                most.append(len(active))
            time.sleep(0.01)
            with guard:
                active.remove(name)

        self.fake.on_call = on_call
        errors = []

        def work(number):
            try:
                if number % 3 == 0:
                    self.backend.search({"q": "*"})
                elif number % 3 == 1:
                    self.backend.index_info()
                else:
                    self.backend.ingest([entry()])
            except Exception as error:          # reported below
                errors.append(error)

        threads = [threading.Thread(target=work, args=(number,)) for number in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(errors, [])
        self.assertEqual(max(most), 1, "two SDK calls ran at the same time")
        self.assertEqual(self.fake.count("UserApp"), 1)


class TestWithoutTheSdk(unittest.TestCase):

    def test_a_missing_sdk_says_how_to_install_it(self):
        with mock.patch.dict(sys.modules, {"globus_sdk": None}):
            with self.assertRaises(Unavailable) as caught:
                search_globus.load_sdk()
        self.assertIn("requirements-globus.txt", caught.exception.message)
        self.assertIn("install.py --globus", caught.exception.message)

    def test_signed_in_is_false_without_the_sdk(self):
        with tempfile.TemporaryDirectory(prefix="pcldb-globus-") as tmp:
            settings = Settings.for_test(Path(tmp))
            GlobusCase.write_token_file(settings)
            with mock.patch.dict(sys.modules, {"globus_sdk": None}):
                self.assertFalse(GlobusSearch(settings).signed_in())

    def test_the_search_modules_import_neither_the_sdk_nor_the_postgresql_driver(self):
        code = ("import sys\n"
                "import pcldb.search, pcldb.search_local, pcldb.search_globus, pcldb.search_sync\n"
                "found = [name for name in ('globus_sdk', 'psycopg', 'requests') "
                "if name in sys.modules]\n"
                "print(found)\n")
        done = subprocess.run([sys.executable, "-B", "-c", code], cwd=str(PROJECT),
                              capture_output=True, text=True, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertEqual(done.stdout.strip(), "[]")


@unittest.skipIf(globus_sdk is None, "the Globus SDK (globus-sdk) is not installed")
class TestAgainstTheInstalledSdk(unittest.TestCase):
    """The real classes, built up to (not including) a sign-in. Name lookups and connections
    are blocked for the whole class."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pcldb-globus-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        # The settings first: picking free ports for them looks at this computer's own ports.
        self.settings = Settings.for_test(
            self.tmp, changes={"search.index_id": "12345678-0000-4000-8000-000000000001"})
        self.attempts = []

        def blocked(*args, **kwargs):
            self.attempts.append(args[:2])
            raise socket.gaierror("the network is blocked in tests")

        for target in ("socket.getaddrinfo", "socket.create_connection"):
            patcher = mock.patch(target, side_effect=blocked)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = mock.patch.object(socket.socket, "connect", side_effect=blocked)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.backend = GlobusSearch(self.settings)
        self.addCleanup(self.backend.close)

    def write_real_token_file(self):
        """A token file in the SDK's own format, with made-up tokens."""
        from globus_sdk.token_storage import JSONTokenStorage, TokenStorageData
        self.settings.user_dir.mkdir(parents=True, exist_ok=True)
        storage = JSONTokenStorage(self.settings.globus_tokens_path)
        later = int(time.time()) + 3600
        storage.store_token_data_by_resource_server({
            "auth.globus.org": TokenStorageData(
                "auth.globus.org", FAKE_IDENTITY, "openid", "made-up-access-1",
                "made-up-refresh-1", later, "Bearer"),
            "search.api.globus.org": TokenStorageData(
                "search.api.globus.org", FAKE_IDENTITY,
                "urn:globus:auth:scope:search.api.globus.org:all", "made-up-access-2",
                "made-up-refresh-2", later, "Bearer"),
        })
        storage.close()

    def test_load_sdk_has_the_names_the_fake_has(self):
        real = search_globus.load_sdk()
        fake = FakeGlobus().sdk
        self.assertEqual(sorted(vars(real)), sorted(vars(fake)))
        self.assertEqual(str(real.search_scope), fake.search_scope)
        self.assertTrue(issubclass(globus_sdk.SearchAPIError, real.ApiError))
        self.assertTrue(issubclass(real.AuthApiError, real.ApiError))
        self.assertTrue(issubclass(globus_sdk.GlobusConnectionError, real.NetworkError))
        self.assertTrue(issubclass(globus_sdk.GlobusTimeoutError, real.NetworkError))
        self.assertTrue(issubclass(real.NetworkError, real.SdkError))
        self.assertTrue(issubclass(real.ApiError, real.SdkError))

    def test_the_fake_client_takes_the_arguments_the_real_client_takes(self):
        for name in ("create_index", "get_index", "ingest", "get_task", "delete_subject",
                     "post_search"):
            with self.subTest(name):
                real = inspect.signature(getattr(globus_sdk.SearchClient, name)).parameters
                fake = inspect.signature(getattr(fake_globus.FakeSearchClient, name)).parameters
                self.assertEqual(list(real)[0], "self")
                self.assertLessEqual(set(fake), set(real), "the fake has an argument the SDK lacks")
                needed = [key for key, value in real.items()
                          if value.default is inspect.Parameter.empty and key != "self"]
                self.assertEqual([key for key in fake if key in needed], needed)
        for name in ("login", "logout", "login_required", "close"):
            self.assertTrue(callable(getattr(globus_sdk.UserApp, name)), name)
        self.assertIn("force", inspect.signature(globus_sdk.UserApp.login).parameters)
        config = inspect.signature(globus_sdk.GlobusAppConfig).parameters
        self.assertLessEqual(set(fake_globus.FakeAppConfig._KNOWN), set(config))

    def test_the_real_objects_are_built_without_a_sign_in_or_the_network(self):
        self.assertFalse(self.backend.signed_in())            # no token file: nothing is built
        self.assertIsNone(self.backend._app)
        with self.backend._lock:
            self.backend._open()
        app, client = self.backend._app, self.backend._client
        self.assertIsInstance(app, globus_sdk.UserApp)
        self.assertIsInstance(client, globus_sdk.SearchClient)
        self.assertEqual(app.app_name, "pcldb")
        self.assertEqual(str(app.client_id), search_globus.DEFAULT_CLIENT_ID)
        self.assertIs(app.config.request_refresh_tokens, True)
        self.assertEqual(app.config.login_flow_manager, "local-server")
        self.assertIsNone(app.config.token_validation_error_handler)
        self.assertEqual(Path(app.config.token_storage.filepath),
                         self.settings.globus_tokens_path)
        wanted = app.scope_requirements["search.api.globus.org"]
        self.assertEqual([str(scope) for scope in wanted],
                         ["urn:globus:auth:scope:search.api.globus.org:all"])
        self.assertEqual(client.retry_config.max_retries, search_globus.HTTP_RETRIES)
        self.assertEqual(client.transport.http_timeout, search_globus.HTTP_TIMEOUT_SECONDS)
        self.assertTrue(app.login_required())
        self.assertFalse(self.settings.globus_tokens_path.exists())
        self.assertEqual(self.attempts, [], "building the objects must not use the network")

    def test_the_command_line_sign_in_way_also_builds(self):
        with self.backend._lock:
            self.backend._open("command-line")
        self.assertEqual(self.backend._app.config.login_flow_manager, "command-line")
        self.assertEqual(self.attempts, [])

    def test_without_tokens_the_real_sdk_error_names_the_login_command(self):
        self.settings.user_dir.mkdir(parents=True, exist_ok=True)
        for name, call in (("search", lambda: self.backend.search({"q": "*"})),
                           ("task", lambda: self.backend.task("t")),
                           ("delete", lambda: self.backend.delete_subject("urn:x")),
                           ("ingest", lambda: self.backend.ingest([entry()]))):
            with self.subTest(name):
                with self.assertRaises(Unavailable) as caught:
                    call()
                self.assertIn("Run: pcldb search login", caught.exception.message)
        with self.assertRaises(Unavailable):
            self.backend.identity_urn()
        self.assertEqual(self.attempts, [], "without a sign-in nothing may be sent")

    def test_index_facts_are_asked_for_without_a_sign_in(self):
        # Globus tells the facts of an index to anybody, so no token is needed (and none is
        # there to send). Here the network is blocked: the call must get that far and no further.
        with mock.patch.object(search_globus, "HTTP_RETRIES", 0):
            with self.assertRaises(Unavailable) as caught:
                self.backend.index_info()
        self.assertIn("could not be reached", caught.exception.message)
        self.assertTrue(self.attempts)
        client = self.backend._plain_client
        self.assertIsInstance(client, globus_sdk.SearchClient)
        self.assertIsNone(client.authorizer)
        self.assertIsNone(self.backend._app, "no sign-in machinery is built for this")

    def test_a_saved_sign_in_is_seen_without_the_network(self):
        self.write_real_token_file()
        self.assertTrue(self.backend.signed_in())
        self.assertEqual(self.backend.identity_urn(), IDENTITY)
        self.assertEqual(self.attempts, [])

    def test_a_damaged_token_file_means_not_signed_in(self):
        GlobusCase.write_token_file(self.settings)
        self.settings.globus_tokens_path.write_text("{ not json", encoding="utf-8")
        self.assertFalse(self.backend.signed_in())
        with self.assertRaises(PclError) as caught:
            self.backend.task("t")
        self.assertIn("pcldb search logout", caught.exception.message)
        self.assertEqual(self.attempts, [])

    def test_a_real_network_failure_becomes_unavailable(self):
        self.write_real_token_file()
        with self.backend._lock:
            self.backend._open()
        self.backend._client.retry_config.max_retries = 0
        with self.assertRaises(Unavailable) as caught:
            self.backend.search({"q": "*"})
        self.assertIn("could not be reached", caught.exception.message)
        self.assertNotIn("made-up-access", json.dumps(caught.exception.to_dict()))
        self.assertTrue(self.attempts, "the call should have tried (and been blocked)")


if __name__ == "__main__":
    unittest.main()
