"""People and permissions (pcldb/auth.py, specification section 10), on both engines."""

from __future__ import annotations

import contextlib
import datetime
import os
import re
import unittest

from pcldb import auth, util
from pcldb.errors import Conflict, Forbidden, NotFound, PclError, Unauthorized
from tests import support

T0 = "2026-11-20T10:00:00.000Z"


def later(seconds=0, days=0):
    moment = util.parse_utc(T0) + datetime.timedelta(seconds=seconds, days=days)
    return util.utc_text(moment)


class RoleTable(unittest.TestCase):
    """The table of specification section 10, without a database."""

    def test_the_four_roles_and_what_they_may_do(self):
        self.assertEqual(auth.ROLES, ("viewer", "analyst", "uploader", "admin"))
        self.assertEqual(auth.CAPABILITIES, ("ask", "sql", "register", "manage"))
        table = {
            "viewer": {"ask": True, "sql": False, "register": False, "manage": False},
            "analyst": {"ask": True, "sql": True, "register": False, "manage": False},
            "uploader": {"ask": True, "sql": False, "register": True, "manage": False},
            "admin": {"ask": True, "sql": True, "register": True, "manage": True},
        }
        for role, row in table.items():
            user = auth.User("0" * 32, "demo-reviewer", None, role, False, T0)
            for capability, allowed in row.items():
                with self.subTest(role=role, capability=capability):
                    self.assertEqual(user.can(capability), allowed)
                    if allowed:
                        self.assertIs(auth.require(user, capability), user)
                    else:
                        with self.assertRaises(Forbidden) as caught:
                            auth.require(user, capability)
                        self.assertEqual(caught.exception.status, 403)
                        self.assertIn(role, caught.exception.message)

    def test_capabilities_keep_the_order_the_api_shows(self):
        self.assertEqual(auth.capabilities("admin"), ("ask", "sql", "register", "manage"))
        self.assertEqual(auth.capabilities("uploader"), ("ask", "register"))
        self.assertEqual(auth.capabilities("no-such-role"), ())

    def test_a_disabled_user_may_do_nothing(self):
        user = auth.User("0" * 32, "demo-reviewer", None, "admin", True, T0)
        self.assertEqual(user.capabilities, ())
        with self.assertRaises(Forbidden) as caught:
            auth.require(user, "ask")
        self.assertIn("pcldb user enable demo-reviewer", caught.exception.message)

    def test_nobody_signed_in_is_unauthorized(self):
        with self.assertRaises(Unauthorized) as caught:
            auth.require(None, "ask")
        self.assertEqual(caught.exception.status, 401)

    def test_an_unknown_capability_is_a_programming_error(self):
        user = auth.User("0" * 32, "demo-reviewer", None, "admin", False, T0)
        with self.assertRaises(ValueError):
            auth.require(user, "everything")

    def test_public_shows_three_fields_only(self):
        user = auth.User("0" * 32, "demo-reviewer", "Demo reviewer", "viewer", False, T0)
        self.assertEqual(user.public(), {"name": "demo-reviewer",
                                         "display_name": "Demo reviewer", "role": "viewer"})

    def test_user_names(self):
        for good in ("a", "0", "demo-operator-a", "a.b_c-d", "a" * 64, "9lives"):
            with self.subTest(good=good):
                self.assertEqual(auth.check_name(good), good)
        for bad in ("", "A", "Demo", "-a", ".a", "_a", "a b", "a" * 65, "a/b", "a\n", "caf\u00e9",
                    None, 7, "a\x00"):
            with self.subTest(bad=bad):
                with self.assertRaises(PclError) as caught:
                    auth.check_name(bad)
                self.assertEqual(caught.exception.code, "bad_user_name")

    def test_token_text_shape(self):
        seen = set()
        for _ in range(50):
            token = auth.new_token_text()
            self.assertRegex(token, r"\Apcl_[A-Za-z0-9_-]{43}\Z")
            self.assertEqual(len(token), auth.TOKEN_LENGTH)
            seen.add(token)
        self.assertEqual(len(seen), 50)

    def test_token_hash_is_sha256_of_the_utf8_text(self):
        self.assertEqual(auth.token_sha256("pcl_x"), util.sha256_hex(b"pcl_x"))


class AuthCases:
    """The shared test methods; they run on SQLite and on PostgreSQL."""

    # -- users -----------------------------------------------------------------------------------

    def test_create_and_list_users(self):
        with self.store.app() as db:
            made = auth.create_user(db, "demo-operator-b", "uploader", "Demo operator B", now=T0)
            auth.create_user(db, "demo-operator-a", "viewer")
            users = auth.list_users(db)
            count = auth.count_users(db)
        self.assertEqual((made.name, made.role, made.display_name, made.disabled, made.created_utc),
                         ("demo-operator-b", "uploader", "Demo operator B", False, T0))
        self.assertRegex(made.user_id, r"\A[0-9a-f]{32}\Z")
        self.assertEqual([user.name for user in users], ["demo-operator-a", "demo-operator-b"])
        self.assertIsNone(users[0].display_name)
        self.assertIsInstance(users[0].disabled, bool)
        self.assertEqual(count, 2)
        self.assertEqual(self.outside_rows("SELECT COUNT(*) FROM {app}.users"), [(2,)])

    def test_create_user_refuses_bad_input_and_writes_nothing(self):
        cases = [
            (("Demo", "viewer"), {}, "bad_user_name"),
            (("demo operator", "viewer"), {}, "bad_user_name"),
            (("demo-reviewer", "owner"), {}, "bad_role"),
            (("demo-reviewer", None), {}, "bad_role"),
            (("demo-reviewer", "viewer", "two\nlines"), {}, "bad_display_name"),
            (("demo-reviewer", "viewer", "x" * 201), {}, "bad_display_name"),
            (("demo-reviewer", "viewer", 5), {}, "bad_display_name"),
        ]
        with self.store.app() as db:
            for args, kwargs, code in cases:
                with self.subTest(args=args):
                    with self.assertRaises(PclError) as caught:
                        auth.create_user(db, *args, **kwargs)
                    self.assertEqual(caught.exception.code, code)
            self.assertEqual(auth.count_users(db), 0)

    def test_an_empty_display_name_is_stored_as_none(self):
        with self.store.app() as db:
            user = auth.create_user(db, "demo-reviewer", "viewer", "   ")
        self.assertIsNone(user.display_name)

    def test_a_name_can_be_taken_only_once(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-reviewer", "viewer")
            with self.assertRaises(Conflict) as caught:
                auth.create_user(db, "demo-reviewer", "admin")
            self.assertEqual(caught.exception.code, "user_exists")
            self.assertEqual(auth.get_user(db, "demo-reviewer").role, "viewer")

    def test_get_and_find_user(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-reviewer", "viewer")
            self.assertEqual(auth.find_user(db, "demo-reviewer").name, "demo-reviewer")
            self.assertIsNone(auth.find_user(db, "nobody"))
            self.assertIsNone(auth.find_user(db, "Not A Name"))
            with self.assertRaises(NotFound) as caught:
                auth.get_user(db, "nobody")
            self.assertEqual(caught.exception.status, 404)

    def test_set_role(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-reviewer", "viewer")
            self.assertEqual(auth.set_role(db, "demo-reviewer", "analyst").role, "analyst")
            self.assertEqual(auth.set_role(db, "demo-reviewer", "analyst").role, "analyst")
            with self.assertRaises(PclError) as caught:
                auth.set_role(db, "demo-reviewer", "boss")
            self.assertEqual(caught.exception.code, "bad_role")
            with self.assertRaises(NotFound):
                auth.set_role(db, "nobody", "viewer")
        self.assertEqual(self.outside_rows("SELECT role FROM {app}.users"), [("analyst",)])

    def test_set_disabled_and_enabled(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-reviewer", "viewer")
            self.assertTrue(auth.set_disabled(db, "demo-reviewer").disabled)
            self.assertTrue(auth.set_disabled(db, "demo-reviewer", True).disabled)
            self.assertFalse(auth.set_disabled(db, "demo-reviewer", False).disabled)
            with self.assertRaises(NotFound):
                auth.set_disabled(db, "nobody")
            with self.assertRaises(TypeError):
                auth.set_disabled(db, "demo-reviewer", 1)

    def test_the_last_working_admin_stays(self):
        with self.store.app() as db:
            auth.create_user(db, "admin", "admin")
            auth.create_user(db, "demo-reviewer", "viewer")
            for change in (lambda: auth.set_disabled(db, "admin"),
                           lambda: auth.set_role(db, "admin", "viewer")):
                with self.assertRaises(Conflict) as caught:
                    change()
                self.assertEqual(caught.exception.code, "last_admin")
            # With a second admin the first one may go; a disabled admin does not count.
            auth.create_user(db, "demo-operator-a", "admin")
            auth.set_disabled(db, "demo-operator-a")
            with self.assertRaises(Conflict):
                auth.set_role(db, "admin", "viewer")
            auth.set_disabled(db, "demo-operator-a", False)
            self.assertEqual(auth.set_role(db, "admin", "viewer").role, "viewer")
            with self.assertRaises(Conflict):
                auth.set_disabled(db, "demo-operator-a")

    # -- tokens ----------------------------------------------------------------------------------

    def test_issue_token_and_authenticate(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "uploader", "Demo operator A")
            token = auth.issue_token(db, "demo-operator-a", label="laptop")
            user = auth.authenticate(db, token)
        self.assertRegex(token, r"\Apcl_[A-Za-z0-9_-]{43}\Z")
        self.assertIsInstance(user, auth.User)
        self.assertEqual((user.name, user.role, user.display_name, user.disabled),
                         ("demo-operator-a", "uploader", "Demo operator A", False))
        self.assertEqual(user.capabilities, ("ask", "register"))

    def test_only_the_sha256_of_a_token_is_stored(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer")
            token = auth.issue_token(db, "demo-operator-a", label="laptop", days=30)
        rows = self.outside_rows("SELECT * FROM {app}.tokens")
        self.assertEqual(len(rows), 1)
        cells = [str(cell) for cell in rows[0]]
        self.assertIn(util.sha256_hex(token.encode("utf-8")), cells)
        for cell in cells:
            self.assertNotIn(token, cell)
            self.assertNotIn(token[len(auth.TOKEN_PREFIX):], cell)
        for row in self.outside_rows("SELECT * FROM {app}.users"):
            self.assertNotIn(token, " ".join(str(cell) for cell in row))

    def test_the_token_of_seed_minimal_is_accepted(self):
        with self.store.app() as db:
            keys = support.seed_minimal(db)
            user = auth.authenticate(db, keys["token"])
        self.assertEqual((user.name, user.role, user.user_id),
                         (keys["user_name"], "admin", keys["user_id"]))

    def test_tokens_that_are_not_accepted_give_none(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer")
            token = auth.issue_token(db, "demo-operator-a")
            wrong = [None, "", 5, b"pcl_x", "pcl_", token[:-1], token + "A", token.upper(),
                     "pcl_" + "A" * 43, "xxx_" + token[4:], " " + token, token + "\n",
                     "' OR 1=1 --", "pcl_" + "\u00e9" * 43]
            for item in wrong:
                with self.subTest(item=item):
                    self.assertIsNone(auth.authenticate(db, item))
            self.assertIsNotNone(auth.authenticate(db, token))

    def test_a_revoked_token_gives_none(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer")
            info = auth.issue_token_info(db, "demo-operator-a")
            other = auth.issue_token(db, "demo-operator-a")
            self.assertTrue(auth.revoke_token(db, info["token_id"], now=T0))
            self.assertFalse(auth.revoke_token(db, info["token_id"], now=later(60)))
            self.assertIsNone(auth.authenticate(db, info["token"]))
            self.assertIsNotNone(auth.authenticate(db, other))
            row = [item for item in auth.list_tokens(db) if item["token_id"] == info["token_id"]]
        self.assertEqual(row[0]["revoked_utc"], T0)     # the first time stays
        self.assertEqual(row[0]["state"], "revoked")

    def test_revoke_needs_a_real_token_id(self):
        with self.store.app() as db:
            for bad in ("", "abc", None, "g" * 32, "0" * 32, "%", "0" * 31 + "%"):
                with self.subTest(bad=bad):
                    with self.assertRaises(NotFound) as caught:
                        auth.revoke_token(db, bad)
                    self.assertEqual(caught.exception.code, "no_such_token")

    def test_an_expired_token_gives_none(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer")
            info = auth.issue_token_info(db, "demo-operator-a", days=1, now=T0)
            self.assertEqual(info["expires_utc"], later(days=1))
            self.assertIsNotNone(
                auth.authenticate(db, info["token"], now=later(days=1, seconds=-1)))
            self.assertIsNone(auth.authenticate(db, info["token"], now=later(days=1)))
            self.assertIsNone(auth.authenticate(db, info["token"], now=later(days=400)))
            half = auth.issue_token_info(db, "demo-operator-a", days=0.5, now=T0)
            self.assertEqual(half["expires_utc"], later(seconds=12 * 3600))
            states = {item["token_id"]: item["state"]
                      for item in auth.list_tokens(db, now=later(days=2))}
        self.assertEqual(states, {info["token_id"]: "expired", half["token_id"]: "expired"})

    def test_days_must_be_a_sensible_number(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer")
            for bad in (0, -1, 3651, True, "7", float("nan"), float("inf")):
                with self.subTest(bad=bad):
                    with self.assertRaises(PclError) as caught:
                        auth.issue_token(db, "demo-operator-a", days=bad)
                    self.assertEqual(caught.exception.code, "bad_days")
            self.assertEqual(auth.list_tokens(db), [])

    def test_a_disabled_user_cannot_sign_in_and_gets_no_new_token(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer")
            token = auth.issue_token(db, "demo-operator-a")
            auth.set_disabled(db, "demo-operator-a")
            self.assertIsNone(auth.authenticate(db, token))
            with self.assertRaises(Conflict) as caught:
                auth.issue_token(db, "demo-operator-a")
            self.assertEqual(caught.exception.code, "user_disabled")
            auth.set_disabled(db, "demo-operator-a", False)
            self.assertEqual(auth.authenticate(db, token).name, "demo-operator-a")

    def test_a_token_for_nobody_is_refused(self):
        with self.store.app() as db:
            with self.assertRaises(NotFound):
                auth.issue_token(db, "nobody")
            auth.create_user(db, "demo-operator-a", "viewer")
            for bad in ("two\nlines", "x" * 101, 5):
                with self.assertRaises(PclError) as caught:
                    auth.issue_token(db, "demo-operator-a", label=bad)
                self.assertEqual(caught.exception.code, "bad_label")

    def test_list_tokens_never_holds_the_text_or_the_hash(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer")
            auth.create_user(db, "demo-operator-b", "viewer")
            first = auth.issue_token_info(db, "demo-operator-a", label="one", now=T0)
            second = auth.issue_token_info(db, "demo-operator-b", days=2, now=later(1))
            everything = auth.list_tokens(db, now=later(2))
            only_b = auth.list_tokens(db, "demo-operator-b", now=later(2))
            with self.assertRaises(NotFound):
                auth.list_tokens(db, "nobody")
        self.assertEqual([row["token_id"] for row in everything],
                         [first["token_id"], second["token_id"]])
        self.assertEqual(everything[0], {
            "token_id": first["token_id"], "user_name": "demo-operator-a", "label": "one",
            "created_utc": T0, "expires_utc": None, "revoked_utc": None, "last_used_utc": None,
            "state": "active"})
        self.assertEqual([row["user_name"] for row in only_b], ["demo-operator-b"])
        shown = repr(everything)
        for info in (first, second):
            self.assertNotIn(info["token"], shown)
            self.assertNotIn(util.sha256_hex(info["token"].encode("utf-8")), shown)

    def test_issue_token_info_keys(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer")
            info = auth.issue_token_info(db, "demo-operator-a", label=" note ", now=T0)
        self.assertEqual(sorted(info), ["created_utc", "expires_utc", "label", "token",
                                        "token_id", "user_name"])
        self.assertEqual((info["label"], info["created_utc"], info["expires_utc"]),
                         ("note", T0, None))

    # -- last used -------------------------------------------------------------------------------

    def _last_used(self, db, token_id):
        return db.scalar("SELECT last_used_utc FROM {app}.tokens WHERE token_id = ?", [token_id])

    def test_last_used_is_written_at_most_once_a_minute(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer")
            info = auth.issue_token_info(db, "demo-operator-a", now=T0)
            token, token_id = info["token"], info["token_id"]
            self.assertIsNone(self._last_used(db, token_id))
            auth.authenticate(db, token, now=later(5))
            self.assertEqual(self._last_used(db, token_id), later(5))
            auth.authenticate(db, token, now=later(30))
            auth.authenticate(db, token, now=later(64.9))
            self.assertEqual(self._last_used(db, token_id), later(5))
            auth.authenticate(db, token, now=later(65))
            self.assertEqual(self._last_used(db, token_id), later(65))
            # A clock that went back never moves the note back.
            self.assertIsNotNone(auth.authenticate(db, token, now=later(1)))
            self.assertEqual(self._last_used(db, token_id), later(65))
            # A token that is not accepted leaves no note.
            auth.revoke_token(db, token_id)
            auth.authenticate(db, token, now=later(600))
            self.assertEqual(self._last_used(db, token_id), later(65))

    def test_a_failed_last_used_note_is_ignored(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer")
            token = auth.issue_token(db, "demo-operator-a")

        class NoWrites:
            """A db whose reads work and whose writes fail."""

            def __init__(self, real):
                self.real = real

            def one(self, sql, params=()):
                return self.real.one(sql, params)

            def param(self, kind, value):
                return self.real.param(kind, value)

            def execute(self, sql, params=()):
                raise RuntimeError("the disk is full")

        with self.store.app() as db:
            user = auth.authenticate(NoWrites(db), token)
        self.assertEqual(user.name, "demo-operator-a")

    def test_authenticate_store_uses_its_own_short_transactions(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "analyst")
            info = auth.issue_token_info(db, "demo-operator-a", now=T0)
        user = auth.authenticate_store(self.store, info["token"], now=later(10))
        self.assertEqual((user.name, user.role), ("demo-operator-a", "analyst"))
        self.assertIsNone(auth.authenticate_store(self.store, "pcl_" + "A" * 43))
        self.assertIsNone(auth.authenticate_store(self.store, "nonsense"))
        with self.store.app() as db:
            self.assertEqual(self._last_used(db, info["token_id"]), later(10))

    def test_authenticate_store_ignores_a_failed_note(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "analyst")
            token = auth.issue_token(db, "demo-operator-a")
        real_app = self.store.app
        calls = []

        @contextlib.contextmanager
        def second_block_fails():
            calls.append(len(calls))
            if len(calls) > 1:
                raise RuntimeError("the database went away")
            with real_app() as db:
                yield db

        self.store.app = self.store.app_only = second_block_fails
        try:
            user = auth.authenticate_store(self.store, token)
        finally:
            del self.store.app, self.store.app_only
        self.assertEqual(user.name, "demo-operator-a")
        self.assertEqual(len(calls), 2)

    def test_hostile_text_is_only_ever_a_value(self):
        hostile = "x'); DROP TABLE users; --"
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer", hostile)
            info = auth.issue_token_info(db, "demo-operator-a", label=hostile)
            self.assertEqual(auth.get_user(db, "demo-operator-a").display_name, hostile)
            self.assertEqual(auth.list_tokens(db)[0]["label"], hostile)
            self.assertIsNone(auth.find_user(db, hostile))
            self.assertIsNotNone(auth.authenticate(db, info["token"]))
        self.assertEqual(self.outside_rows("SELECT COUNT(*) FROM {app}.users"), [(1,)])

    def test_token_file_path_and_private_file(self):
        with self.store.app() as db:
            auth.create_user(db, "demo-operator-a", "viewer")
            info = auth.issue_token_info(db, "demo-operator-a")
        path = auth.token_file_path(self.settings, info)
        self.assertEqual(path.parent, self.settings.var_dir / "tokens")
        self.assertTrue(re.fullmatch(r"demo-operator-a_[0-9a-f]{8}\.txt", path.name))
        auth.save_token_file(path, info["token"])
        self.assertEqual(path.read_bytes(), (info["token"] + "\n").encode("ascii"))
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)


class TestAuthSqlite(AuthCases, support.SqliteCase, unittest.TestCase):
    pass


class TestAuthPostgres(AuthCases, support.PostgresCase, unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
