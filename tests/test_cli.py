"""The command line (pcldb/cli.py, pcldb/__main__.py), setup, db, the user and token commands,
and the two launchers (specification section 13).

Nothing here touches ~/.pcldb, the project's real var/ folder or the default ports: every
command runs against a temporary project folder (PCLDB_DIR), with PCLDB_USER_DIR inside it.
"""

from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from pcldb import auth, cli, config
from pcldb.errors import PclError
from pcldb.store import open_store
from tests import registry_support, support

PROJECT = Path(__file__).resolve().parent.parent
TOKEN_SHAPE = re.compile(r"pcl_[A-Za-z0-9_-]{43}")
POSIX_LAUNCHER = PROJECT / "pcldb.sh"
WINDOWS_LAUNCHER = PROJECT / "pcldb.cmd"

# Specification section 13: command -> (module, function).
EXPECTED_COMMANDS = {
    "setup": ("cli", "cmd_setup"),
    "db": ("cli", "cmd_db"),
    "serve": ("server", "cmd_serve"),
    "doctor": ("doctor", "cmd_doctor"),
    "user": ("auth", "cmd_user"),
    "token": ("auth", "cmd_token"),
    "register": ("registration", "cmd_register"),
    "load-index": ("reconcile", "cmd_load_index"),
    "demo": ("demo", "cmd_demo"),
    "dump": ("transfer", "cmd_dump"),
    "restore": ("transfer", "cmd_restore"),
    "export-sqlite": ("transfer", "cmd_export_sqlite"),
    "search": ("search_sync", "cmd_search"),
    "ask": ("ask", "cmd_ask"),
    "sql": ("sqlrun", "cmd_sql"),
}

# Specification section 13, the Arguments table: a command line that sets every argument, and
# the `dest` names and values the command function must then see.
ARGUMENT_CASES = [
    (["setup", "--engine", "sqlite", "--demo", "--port", "23456"],
     {"engine": "sqlite", "demo": True, "port": 23456}),
    (["setup"], {"engine": None, "demo": False, "port": None}),
    (["db", "reset", "--yes"], {"action": "reset", "yes": True}),
    (["db", "status"], {"action": "status", "yes": False}),
    (["serve", "--open", "--host", "127.0.0.1", "--port", "23457"],
     {"open_browser": True, "host": "127.0.0.1", "port": 23457}),
    (["serve"], {"open_browser": False, "host": None, "port": None}),
    (["doctor"], {}),
    (["user", "add", "demo-operator-a", "--role", "uploader", "--display-name", "Demo A"],
     {"action": "add", "name": "demo-operator-a", "role": "uploader", "display_name": "Demo A"}),
    (["user", "list"], {"action": "list", "name": None, "role": None, "display_name": None}),
    (["token", "new", "--user", "demo-operator-a", "--label", "laptop", "--days", "30"],
     {"action": "new", "user": "demo-operator-a", "label": "laptop", "days": 30,
      "token_id": None}),
    (["token", "revoke", "--id", "ab" * 16],
     {"action": "revoke", "user": None, "label": None, "days": None, "token_id": "ab" * 16}),
    (["register", "FOLDER", "--path", "raw/x/y", "--card", "card.json", "--replace", "--tree",
      "ROOT", "--document", "doc.json", "--raman-uploads", "HOME", "--instrument", "slug",
      "--scan-sessions", "SCANS", "--server", "http://127.0.0.1:23458", "--token-file", "t.txt"],
     {"folder": "FOLDER", "path": "raw/x/y", "card": "card.json", "replace": True,
      "tree": "ROOT", "document": "doc.json", "raman_uploads": "HOME", "instrument": "slug",
      "scan_sessions": "SCANS", "server": "http://127.0.0.1:23458", "token_file": "t.txt"}),
    (["register", "--tree", "ROOT"],
     {"folder": None, "path": None, "card": None, "replace": False, "tree": "ROOT",
      "document": None, "raman_uploads": None, "instrument": "raman-portable",
      "scan_sessions": None, "server": None, "token_file": None}),
    (["load-index", "sessions.sqlite"], {"file": "sessions.sqlite"}),
    (["demo", "load"], {"action": "load"}),
    (["dump", "out.jsonl"], {"file": "out.jsonl"}),
    (["export-sqlite", "copy.sqlite"], {"file": "copy.sqlite"}),
    (["restore", "in.jsonl", "--include-users"], {"file": "in.jsonl", "include_users": True}),
    (["restore", "in.jsonl"], {"file": "in.jsonl", "include_users": False}),
    (["search", "create-index", "--no-browser", "--name", "n", "--description", "d",
      "--request", "@r.json", "--wait", "5"],
     {"action": "create-index", "no_browser": True, "name": "n", "description": "d",
      "request": "@r.json", "wait_seconds": 5}),
    (["search", "status"],
     {"action": "status", "no_browser": False, "name": None, "description": None,
      "request": None, "wait_seconds": None}),
    (["ask", "which sessions?", "--no-llm", "--json", "--no-simulated"],
     {"question": "which sessions?", "no_llm": True, "as_json": True, "no_simulated": True}),
    (["ask", "q"], {"question": "q", "no_llm": False, "as_json": False, "no_simulated": False}),
    (["sql", "SELECT 1", "--csv", "--json", "--max-rows", "7"],
     {"sql": "SELECT 1", "as_csv": True, "as_json": True, "max_rows": 7}),
    (["sql", "SELECT 1"], {"sql": "SELECT 1", "as_csv": False, "as_json": False,
                           "max_rows": None}),
]

_KEPT_VARIABLES = ("PCLDB_PG_BIN", "PCLDB_REQUIRE_POSTGRES")


def clean_environment(project_dir=None, **extra):
    """A copy of the environment without pcldb's variables (so that nothing of the person who
    runs the tests leaks in), with the per-user folder inside the temporary project."""
    env = {name: value for name, value in os.environ.items()
           if not name.startswith("PCLDB_") or name in _KEPT_VARIABLES}
    env.pop("PYTHONPATH", None)
    if project_dir is not None:
        env["PCLDB_DIR"] = str(project_dir)
        env["PCLDB_USER_DIR"] = str(Path(project_dir) / "user")
    env.update(extra)
    return env


def run_main(argv, *, env=None, settings=None):
    """Run cli.main in this process. Returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        if env is not None:
            stack.enter_context(mock.patch.dict(os.environ, env, clear=True))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        code = cli.main(list(argv), settings=settings)
    return code, out.getvalue(), err.getvalue()


def switch_in_one_folder(case, new_database):
    """SEC2-03 / C2-06: the documented switch in one project folder, from the command line:
    setup, a user with a token, dump, change the database in pcldb_config.json
    (`new_database`: the new "database" group), setup, restore --include-users. Checks that
    var/admin_token.txt holds a working token of the dump's admin afterwards, and returns the
    restore's output."""
    from pcldb import util
    var = case.folder / "var"
    admin_file = var / "admin_token.txt"
    dump_file = case.folder / "switch dump.jsonl"
    code, out, err = case.run_cli("setup", "--engine", "sqlite")
    case.assertEqual(code, 0, err)
    for argv in (("user", "add", "ana", "--role", "analyst"), ("token", "new", "--user", "ana")):
        code, out, err = case.run_cli(*argv)
        case.assertEqual(code, 0, err)
    ana_file = [path for path in (var / "tokens").iterdir() if path.name.startswith("ana_")][0]
    first_admin_token = admin_file.read_text(encoding="ascii").strip()
    code, out, err = case.run_cli("dump", str(dump_file))
    case.assertEqual(code, 0, err)
    case.assertIn("%s restore" % util.LAUNCHER, out)

    config_path = case.folder / config.CONFIG_NAME
    written = json.loads(config_path.read_text(encoding="utf-8"))
    written["database"] = new_database
    config_path.write_text(json.dumps(written), encoding="utf-8")
    code, out, err = case.run_cli("setup")
    case.assertEqual(code, 0, err)
    setup_token = admin_file.read_text(encoding="ascii").strip()
    case.assertNotEqual(setup_token, first_admin_token)     # setup wrote over the file

    code, out, err = case.run_cli("restore", str(dump_file), "--include-users")
    case.assertEqual(code, 0, err)
    case.assert_clean(out, err)
    case.assertIn('The dump\'s user "admin" got a new token, saved in %s' % admin_file, out)
    case.assertNotIn("where they were saved", out)
    case.assertIn("%s token new --user <name>" % util.LAUNCHER, out)
    case.assertIsNone(TOKEN_SHAPE.search(out))
    case.assertTrue(admin_file.is_file())
    if os.name != "nt":
        case.assertEqual(admin_file.stat().st_mode & 0o777, 0o600)
    new_token = admin_file.read_text(encoding="ascii").strip()
    case.assertRegex(new_token, r"\A%s\Z" % TOKEN_SHAPE.pattern)
    case.assertNotIn(new_token, (setup_token, first_admin_token))
    with open_store(case.loaded_settings()).app() as db:
        case.assertEqual(auth.authenticate(db, new_token).name, "admin")
        case.assertIsNone(auth.authenticate(db, setup_token))
        case.assertEqual(auth.authenticate(db, first_admin_token).name, "admin")
        ana_token = ana_file.read_text(encoding="ascii").strip()
        case.assertEqual(auth.authenticate(db, ana_token).name, "ana")
    # The commands that read var/admin_token.txt by themselves have a token again.
    code, listed, err = case.run_cli("user", "list")
    case.assertEqual(code, 0, err)
    case.assertIn("ana", listed)
    return out


class TempFolderCase(unittest.TestCase):
    """A temporary project folder (with a space in its name) that holds nothing yet."""

    def setUp(self):
        super().setUp()
        self.project = support.TempProject("sqlite")
        self.addCleanup(self.project.cleanup)
        self.folder = self.project.path
        self.env = clean_environment(self.folder)

    def run_cli(self, *argv, **extra_env):
        env = dict(self.env)
        env.update(extra_env)
        return run_main(argv, env=env)

    def loaded_settings(self):
        return config.load_settings(project_dir=self.folder, user_dir=self.folder / "user",
                                    env=self.env)

    def assert_clean(self, *texts):
        """No stack trace, ASCII only."""
        for text in texts:
            self.assertNotIn("Traceback", text)
            self.assertTrue(text.isascii(), text)


# ---------------------------------------------------------------------- the parser


class TestParser(unittest.TestCase):

    def test_the_command_table_is_the_specifications(self):
        self.assertEqual(cli.COMMANDS, EXPECTED_COMMANDS)

    def test_every_command_is_declared(self):
        parser = cli.build_parser()
        actions = [action for action in parser._actions
                   if isinstance(action.choices, dict)]
        self.assertEqual(len(actions), 1)
        self.assertEqual(set(actions[0].choices), set(EXPECTED_COMMANDS))

    def test_help_of_the_top_level_and_of_every_command(self):
        for argv in [["--help"], ["-h"]] + [[name, "--help"] for name in EXPECTED_COMMANDS]:
            with self.subTest(argv=argv):
                code, out, err = run_main(argv)
                self.assertEqual(code, 0)
                self.assertEqual(err, "")
                self.assertIn("usage: pcldb", out)
                self.assertTrue(out.isascii())
                self.assertNotIn("Traceback", out)
                if argv[0] in EXPECTED_COMMANDS:
                    self.assertIn("usage: pcldb %s" % argv[0], out)

    def test_top_help_names_every_command(self):
        _code, out, _err = run_main(["--help"])
        for name in EXPECTED_COMMANDS:
            self.assertRegex(out, r"(?m)^\s+%s\s" % re.escape(name))

    def test_version(self):
        code, out, _err = run_main(["--version"])
        self.assertEqual(code, 0)
        self.assertRegex(out.strip(), r"\Apcldb \d+\.\d+\.\d+\Z")

    def test_argument_names_are_the_contract(self):
        parser = cli.build_parser()
        for argv, expected in ARGUMENT_CASES:
            with self.subTest(argv=argv):
                found = vars(parser.parse_args(argv))
                self.assertEqual(found.pop("command"), argv[0])
                self.assertIsNone(found.pop("dir"))
                self.assertEqual(found, expected)

    def test_dir_comes_before_the_command(self):
        args = cli.build_parser().parse_args(["--dir", "some folder", "doctor"])
        self.assertEqual((args.dir, args.command), ("some folder", "doctor"))

    def test_every_action_is_accepted(self):
        parser = cli.build_parser()
        actions = {"db": ["status", "start", "stop", "reset"],
                   "user": ["add", "list", "role", "disable", "enable"],
                   "token": ["new", "list", "revoke"],
                   "demo": ["load", "clear"],
                   "search": ["status", "push", "reindex", "login", "logout", "create-index",
                              "query", "use-globus", "use-local"]}
        for command, names in actions.items():
            for name in names:
                self.assertEqual(parser.parse_args([command, name]).action, name)

    def test_roles_and_engines_match_the_modules(self):
        self.assertEqual(cli.ROLE_CHOICES, auth.ROLES)
        self.assertEqual(cli.ENGINE_CHOICES, config.ENGINES)

    def test_wrong_usage_is_exit_code_2(self):
        cases = [[], ["nonsense"], ["--nonsense"], ["setup", "--nonsense"],
                 ["setup", "--engine", "oracle"], ["setup", "--port", "0"],
                 ["setup", "--port", "70000"], ["setup", "--port", "abc"], ["db"],
                 ["db", "explode"], ["user"], ["user", "add", "x", "--role", "boss"],
                 ["token", "new", "--days", "0"], ["token", "new", "--days", "many"],
                 ["token", "burn"], ["sql"], ["sql", "SELECT 1", "--max-rows", "0"], ["ask"],
                 ["search", "push", "--wait", "-1"], ["demo", "explode"], ["load-index"],
                 ["doctor", "extra"]]
        for argv in cases:
            with self.subTest(argv=argv):
                code, out, err = run_main(argv)
                self.assertEqual(code, 2)
                self.assertEqual(out, "")
                self.assertIn("usage: pcldb", err)
                self.assertNotIn("Traceback", err)
                self.assertTrue(err.isascii())


class TestDispatch(TempFolderCase):

    def test_a_part_that_is_not_built_yet_says_so_in_one_sentence(self):
        for target in (("no_such_part_of_pcldb", "cmd_doctor"), ("auth", "cmd_not_written")):
            with self.subTest(target=target):
                with mock.patch.dict(cli.COMMANDS, {"doctor": target}):
                    code, out, err = self.run_cli("doctor")
                self.assertEqual(code, 1)
                self.assertEqual(out, "")
                self.assertEqual(len(err.strip().splitlines()), 1)
                self.assertIn("not built yet", err)
                self.assertIn("pcldb doctor", err)
                self.assert_clean(err)

    def test_a_part_that_misses_something_else_is_not_called_unbuilt(self):
        def missing_package(name, package=None):
            raise ModuleNotFoundError("No module named 'some_package'", name="some_package")
        with mock.patch.object(cli.importlib, "import_module", missing_package):
            with self.assertRaises(ModuleNotFoundError):
                cli.command_function("doctor")

    def test_every_built_command_function_can_be_called(self):
        for name in EXPECTED_COMMANDS:
            function = cli.command_function(name)
            self.assertTrue(function is None or callable(function), name)
        self.assertIs(cli.command_function("setup"), cli.cmd_setup)
        self.assertIs(cli.command_function("db"), cli.cmd_db)
        self.assertIs(cli.command_function("user"), auth.cmd_user)
        self.assertIs(cli.command_function("token"), auth.cmd_token)

    def test_the_return_value_becomes_the_exit_code(self):
        seen = {}

        def fake(args, settings):
            seen["args"], seen["settings"] = args, settings
            return seen.get("answer")

        fake_module = mock.Mock(cmd_doctor=fake)
        for answer, expected in ((None, 0), (0, 0), (1, 1), (3, 3)):
            seen["answer"] = answer
            with mock.patch.object(cli.importlib, "import_module", return_value=fake_module):
                code, _out, _err = self.run_cli("doctor")
            self.assertEqual(code, expected)
        self.assertEqual(seen["args"].command, "doctor")
        self.assertEqual(seen["settings"].project_dir, Path(os.path.abspath(self.folder)))

    def test_a_pcl_error_is_one_message_and_exit_code_1(self):
        def refuses(args, settings):
            raise PclError("The thing was not done. Do the other thing: caf\u00e9")
        with mock.patch.object(cli.importlib, "import_module",
                               return_value=mock.Mock(cmd_doctor=refuses)):
            code, out, err = self.run_cli("doctor")
        self.assertEqual((code, out), (1, ""))
        self.assertEqual(err, "The thing was not done. Do the other thing: caf\\xe9\n")

    def test_ctrl_c_ends_quietly(self):
        def interrupted(args, settings):
            raise KeyboardInterrupt
        with mock.patch.object(cli.importlib, "import_module",
                               return_value=mock.Mock(cmd_doctor=interrupted)):
            code, _out, err = self.run_cli("doctor")
        self.assertEqual(code, 130)
        self.assert_clean(err)

    def test_a_bad_settings_file_is_a_message_not_a_stack_trace(self):
        (self.folder / config.CONFIG_NAME).write_text('{"databse": {}}', encoding="utf-8")
        code, _out, err = self.run_cli("db", "status")
        self.assertEqual(code, 1)
        self.assertIn("databse", err)
        self.assert_clean(err)

    def test_dir_option_chooses_the_project_folder(self):
        other = support.TempProject("sqlite")
        self.addCleanup(other.cleanup)
        code, out, err = self.run_cli("--dir", str(other.path), "setup", "--engine", "sqlite")
        self.assertEqual(code, 0, err)
        self.assertTrue((other.path / config.CONFIG_NAME).is_file())
        self.assertFalse((self.folder / config.CONFIG_NAME).exists())


# ---------------------------------------------------------------------- setup, SQLite


class TestSetupSqlite(TempFolderCase):

    def snapshot(self):
        """Everything setup makes, as bytes and rows."""
        settings = self.loaded_settings()
        store = open_store(settings)
        with store.app() as db:
            users = db.query("SELECT * FROM {app}.users")
            tokens = db.query("SELECT * FROM {app}.tokens")
        files = {}
        for path in sorted(self.folder.rglob("*")):
            if path.is_file():
                files[str(path.relative_to(self.folder))] = path.read_bytes()
        return {"users": users, "tokens": tokens, "files": files}

    def test_setup_makes_everything_and_a_second_run_changes_nothing(self):
        code, out, err = self.run_cli("setup", "--engine", "sqlite")
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assert_clean(out)
        var = self.folder / "var"
        config_path = self.folder / config.CONFIG_NAME
        token_path = var / "admin_token.txt"
        for path in (config_path, var / "sqlite" / "pcl_app.sqlite",
                     var / "sqlite" / "pcl_catalog.sqlite", token_path):
            self.assertTrue(path.is_file(), path)

        written = json.loads(config_path.read_text(encoding="utf-8"))
        self.assertEqual(written["database"], {"engine": "sqlite"})
        settings = self.loaded_settings()
        self.assertEqual(settings.database.engine, "sqlite")

        token_text = token_path.read_text(encoding="ascii")
        self.assertTrue(token_text.endswith("\n"))
        token = token_text.strip()
        self.assertRegex(token, r"\A%s\Z" % TOKEN_SHAPE.pattern)
        self.assertNotIn(token, out)
        self.assertIsNone(TOKEN_SHAPE.search(out))
        self.assertIn(str(token_path), out)
        if os.name != "nt":
            self.assertEqual(token_path.stat().st_mode & 0o777, 0o600)

        store = open_store(settings)
        self.assertTrue(store.is_initialized())
        with store.app() as db:
            users = auth.list_users(db)
            user = auth.authenticate(db, token)
        self.assertEqual([(item.name, item.role, item.disabled) for item in users],
                         [("admin", "admin", False)])
        self.assertEqual((user.name, user.role), ("admin", "admin"))

        before = self.snapshot()
        code, out, err = self.run_cli("setup", "--engine", "sqlite")
        self.assertEqual(code, 0, err)
        self.assertIn("exists already", out)
        self.assertIsNone(TOKEN_SHAPE.search(out))
        self.assertEqual(self.snapshot(), before)
        code, out, err = self.run_cli("setup")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.snapshot(), before)
        # NEW-11: the next step names the launcher that works as typed on this system.
        from pcldb import util
        self.assertIn("Next: %s serve --open" % util.LAUNCHER, out)
        # NEW-04: without --engine the kept engine is explained, with the way to switch.
        self.assertIn("Engine: SQLite, as that file says", out)
        self.assertIn("To switch to PostgreSQL: %s dump FILE, change \"database.engine\" in "
                      "that file to \"postgresql\", %s setup, then %s restore FILE "
                      "--include-users." % (util.LAUNCHER, util.LAUNCHER, util.LAUNCHER), out)
        self.assertNotIn(" pcldb dump", out)

    def test_the_switch_in_one_folder_keeps_a_working_admin_token_file(self):
        # SEC2-03 / C2-06 on SQLite alone: a new, empty SQLite database in the same folder
        # (another database.sqlite.folder) goes through the same steps as a switch of engine.
        switch_in_one_folder(self, {"engine": "sqlite",
                                    "sqlite": {"folder": "var/sqlite after the switch"}})
        self.assertTrue((self.folder / "var" / "sqlite after the switch" /
                         "pcl_app.sqlite").is_file())

    def test_the_users_own_folder_and_the_real_project_are_left_alone(self):
        real_var = PROJECT / "var"
        before = sorted(str(path) for path in real_var.rglob("*")) if real_var.exists() else None
        code, _out, err = self.run_cli("setup", "--engine", "sqlite")
        self.assertEqual(code, 0, err)
        after = sorted(str(path) for path in real_var.rglob("*")) if real_var.exists() else None
        self.assertEqual(after, before)
        self.assertFalse((self.folder / "user").exists())     # nothing personal was needed

    def test_a_persons_settings_file_is_never_overwritten(self):
        config_path = self.folder / config.CONFIG_NAME
        mine = ('{\n  "_note": "my own file",\n  "database": {"engine": "sqlite"},\n'
                '  "sql": {"max_rows": 77}\n}\n').encode("utf-8")
        config_path.write_bytes(mine)
        code, out, err = self.run_cli("setup")
        self.assertEqual(code, 0, err)
        self.assertEqual(config_path.read_bytes(), mine)
        self.assertIn("was not changed", out)
        self.assertTrue((self.folder / "var" / "admin_token.txt").is_file())

    def test_another_engine_than_the_settings_file_is_refused(self):
        self.assertEqual(self.run_cli("setup", "--engine", "sqlite")[0], 0)
        before = self.snapshot()
        code, _out, err = self.run_cli("setup", "--engine", "postgresql")
        self.assertEqual(code, 1)
        self.assertIn("never changes a settings file", err)
        self.assertIn("database.engine", err)
        from pcldb import util
        self.assertIn("%s dump FILE before, %s restore FILE --include-users after"
                      % (util.LAUNCHER, util.LAUNCHER), err)
        self.assert_clean(err)
        self.assertEqual(self.snapshot(), before)
        self.assertFalse((self.folder / "var" / "pgdata").exists())

    def test_another_port_than_the_settings_file_is_refused(self):
        port = support.free_port()
        self.assertEqual(self.run_cli("setup", "--engine", "sqlite", "--port", str(port))[0], 0)
        self.assertEqual(self.loaded_settings().database.postgresql.port, port)
        self.assertEqual(self.run_cli("setup", "--port", str(port))[0], 0)
        code, _out, err = self.run_cli("setup", "--port", str(support.free_port()))
        self.assertEqual(code, 1)
        self.assertIn("database.postgresql.port", err)

    def test_the_engine_variable_decides_when_nothing_else_does(self):
        code, out, err = self.run_cli("setup", PCLDB_ENGINE="sqlite")
        self.assertEqual(code, 0, err)
        self.assertIn("PCLDB_ENGINE", out)
        written = json.loads((self.folder / config.CONFIG_NAME).read_text(encoding="utf-8"))
        self.assertEqual(written["database"]["engine"], "sqlite")

    def test_engine_option_against_the_engine_variable_is_refused(self):
        code, _out, err = self.run_cli("setup", "--engine", "sqlite", PCLDB_ENGINE="postgresql")
        self.assertEqual(code, 1)
        self.assertIn("PCLDB_ENGINE", err)
        self.assertFalse((self.folder / "var").exists())
        self.assertFalse((self.folder / config.CONFIG_NAME).exists())

    def test_without_postgres_programs_sqlite_is_chosen_and_it_says_how_to_switch(self):
        from pcldb.store import pgcluster
        with mock.patch.object(pgcluster, "find_pg_bins", return_value=[]):
            code, out, err = self.run_cli("setup")
        self.assertEqual(code, 0, err)
        self.assertIn("Engine: SQLite", out)
        self.assertIn("were not found", out)
        self.assertIn('"postgresql"', out)
        self.assertEqual(self.loaded_settings().database.engine, "sqlite")

    def test_a_first_setup_that_cannot_work_writes_nothing(self):
        from pcldb.store import pgcluster, postgres_engine
        missing = PclError("PostgreSQL's programs were not found. Install PostgreSQL or run: "
                           "pcldb setup --engine sqlite", code="postgres_missing", status=503)
        # psycopg counts as installed (also on a bare Python), so that the missing programs are
        # what stops this setup on every machine.
        with mock.patch.object(pgcluster, "choose_pg_bin", side_effect=missing), \
                mock.patch.object(postgres_engine, "load_psycopg", return_value=None), \
                mock.patch.object(cli.importlib.util, "find_spec", return_value=object()):
            code, _out, err = self.run_cli("setup", "--engine", "postgresql")
        self.assertEqual(code, 1)
        self.assertIn("pcldb setup --engine sqlite", err)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), [])
        # So the command the message names works right away.
        code, _out, err = self.run_cli("setup", "--engine", "sqlite")
        self.assertEqual(code, 0, err)

    def test_a_missing_admin_token_file_is_explained_not_remade(self):
        self.assertEqual(self.run_cli("setup", "--engine", "sqlite")[0], 0)
        (self.folder / "var" / "admin_token.txt").unlink()
        code, out, err = self.run_cli("setup")
        self.assertEqual(code, 0, err)
        self.assertIn("pcldb token new --user admin", out)
        self.assertFalse((self.folder / "var" / "admin_token.txt").exists())
        with open_store(self.loaded_settings()).app() as db:
            self.assertEqual(auth.count_users(db), 1)

    def test_an_admin_is_only_made_when_there_is_no_user_at_all(self):
        settings = self.project.settings
        store = open_store(settings)
        store.initialize()
        with store.app() as db:
            auth.create_user(db, "demo-reviewer", "viewer")
        self.assertFalse(cli.ensure_admin(store, settings))
        with store.app() as db:
            self.assertEqual([user.name for user in auth.list_users(db)], ["demo-reviewer"])
        self.assertFalse(cli.admin_token_path(settings).exists())

    def test_nothing_is_saved_when_the_admin_could_not_be_made(self):
        settings = self.project.settings
        store = open_store(settings)
        store.initialize()
        with store.app() as db:
            auth.create_user(db, "admin", "viewer")
        # Pretend nobody exists: making "admin" then fails before a token is saved.
        with mock.patch.object(auth, "count_users", return_value=0):
            with self.assertRaises(PclError):
                cli.ensure_admin(store, settings)
        self.assertFalse(cli.admin_token_path(settings).exists())
        with store.app() as db:
            self.assertEqual(auth.list_tokens(db), [])

    def test_the_token_file_goes_when_the_admin_was_not_committed(self):
        settings = self.project.settings
        store = open_store(settings)
        store.initialize()
        real_app = store.app
        seen = {}

        @contextlib.contextmanager
        def commit_fails():
            with real_app() as db:
                yield db
                seen["file_was_there"] = cli.admin_token_path(settings).is_file()
                raise RuntimeError("the commit failed")

        store.app = commit_fails
        try:
            with self.assertRaises(RuntimeError):
                cli.ensure_admin(store, settings)
        finally:
            del store.app
        self.assertTrue(seen["file_was_there"])
        self.assertFalse(cli.admin_token_path(settings).exists())
        with store.app() as db:
            self.assertEqual(auth.count_users(db), 0)

    def test_setup_with_demo(self):
        code, out, err = self.run_cli("setup", "--engine", "sqlite", "--demo")
        self.assertEqual(code, 0, err)
        self.assert_clean(out)
        if importlib.util.find_spec("pcldb.demo") is None:
            self.assertIn("not built yet", out)
            return
        with open_store(self.loaded_settings()).app() as db:
            sessions = db.scalar("SELECT COUNT(*) FROM {pcl}.sessions")
        self.assertGreater(sessions, 0)
        code, out, err = self.run_cli("setup", "--demo")
        self.assertEqual(code, 0, err)
        self.assertIn("loaded already", out)
        with open_store(self.loaded_settings()).app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), sessions)

    def test_setup_with_demo_while_the_demo_part_is_missing(self):
        real_import = importlib.import_module

        def without_demo(name, package=None):
            if name == "pcldb.demo":
                raise ModuleNotFoundError("No module named 'pcldb.demo'", name="pcldb.demo")
            return real_import(name, package)

        with mock.patch.object(cli.importlib, "import_module", without_demo):
            code, out, err = self.run_cli("setup", "--engine", "sqlite", "--demo")
        self.assertEqual(code, 0, err)
        self.assertIn("not built yet", out)
        self.assertTrue((self.folder / "var" / "admin_token.txt").is_file())


class TestChooseEngine(unittest.TestCase):

    def setUp(self):
        self.project = support.TempProject("sqlite")
        self.addCleanup(self.project.cleanup)
        self.settings = self.project.settings

    def choose(self, found, driver, wanted=None, env=None):
        from pcldb.store import pgcluster
        bins = [pgcluster.PgBin(Path("/somewhere/postgresql@18/bin"), "18.7", 18, 7, "")] \
            if found else []
        spec = object() if driver else None
        with mock.patch.object(pgcluster, "find_pg_bins", return_value=bins), \
                mock.patch.object(cli.importlib.util, "find_spec", return_value=spec):
            return cli.choose_engine(self.settings, wanted, env=env or {})

    def test_postgresql_when_programs_and_driver_are_there(self):
        engine, sentences = self.choose(True, True)
        self.assertEqual(engine, "postgresql")
        text = " ".join(sentences)
        self.assertIn("PostgreSQL 18.7", text)
        self.assertIn('"sqlite"', text)

    def test_sqlite_when_the_programs_are_missing(self):
        engine, sentences = self.choose(False, True)
        self.assertEqual(engine, "sqlite")
        text = " ".join(sentences)
        self.assertIn("were not found", text)
        self.assertIn("brew install postgresql@18", text)
        self.assertIn("winget install PostgreSQL.PostgreSQL.18", text)

    def test_sqlite_when_the_driver_is_missing(self):
        engine, sentences = self.choose(True, False)
        self.assertEqual(engine, "sqlite")
        self.assertIn("psycopg", " ".join(sentences))

    def test_the_option_wins(self):
        self.assertEqual(self.choose(False, False, wanted="postgresql")[0], "postgresql")
        self.assertEqual(self.choose(True, True, wanted="sqlite")[0], "sqlite")

    def test_the_variable_comes_before_looking(self):
        engine, sentences = self.choose(True, True, env={"PCLDB_ENGINE": "sqlite"})
        self.assertEqual(engine, self.settings.database.engine)
        self.assertIn("PCLDB_ENGINE", sentences[0])

    def test_every_sentence_is_ascii(self):
        for found in (True, False):
            for driver in (True, False):
                for sentence in self.choose(found, driver)[1]:
                    self.assertTrue(sentence.isascii())


# ---------------------------------------------------------------------- db, SQLite


class TestDbSqlite(TempFolderCase):

    def setUp(self):
        super().setUp()
        code, _out, err = self.run_cli("setup", "--engine", "sqlite")
        self.assertEqual(code, 0, err)
        self.token_path = self.folder / "var" / "admin_token.txt"

    def test_status_prints_the_files_and_their_sizes(self):
        code, out, err = self.run_cli("db", "status")
        self.assertEqual((code, err), (0, ""))
        sqlite_dir = self.folder / "var" / "sqlite"
        self.assertIn(str(sqlite_dir), out)
        for name in ("pcl_app.sqlite", "pcl_catalog.sqlite"):
            self.assertIn("%s  (%d bytes)" % (name, (sqlite_dir / name).stat().st_size), out)
        self.assertIn("Set up: yes", out)
        self.assert_clean(out)

    def test_status_before_setup(self):
        other = support.TempProject("sqlite")
        self.addCleanup(other.cleanup)
        code, out, _err = run_main(["db", "status"],
                                   env=clean_environment(other.path, PCLDB_ENGINE="sqlite"))
        self.assertEqual(code, 0)
        self.assertIn("not there yet", out)
        self.assertIn("Set up: no", out)
        self.assertIn("pcldb setup", out)
        self.assertFalse((other.path / "var").exists())     # looking makes nothing

    def test_start_and_stop_say_there_is_nothing_to_do(self):
        for action in ("start", "stop"):
            code, out, err = self.run_cli("db", action)
            self.assertEqual((code, err), (0, ""))
            self.assertIn("nothing to %s" % action, out)

    def test_reset_needs_yes(self):
        before = self.token_path.read_bytes()
        code, out, err = self.run_cli("db", "reset")
        self.assertEqual((code, out), (1, ""))
        self.assertIn("pcldb db reset --yes", err)
        self.assertIn("Nothing was deleted", err)
        self.assert_clean(err)
        self.assertEqual(self.token_path.read_bytes(), before)
        with open_store(self.loaded_settings()).app() as db:
            self.assertEqual(auth.count_users(db), 1)

    def test_reset_with_yes_empties_the_database_and_makes_a_new_admin(self):
        old_token = self.token_path.read_text(encoding="ascii").strip()
        self.assertEqual(self.run_cli("user", "add", "demo-operator-a")[0], 0)
        self.assertEqual(self.run_cli("token", "new", "--user", "demo-operator-a")[0], 0)
        saved = list((self.folder / "var" / "tokens").glob("*.txt"))
        self.assertEqual(len(saved), 1)
        store = open_store(self.loaded_settings())
        with store.app() as db:
            db.insert("pcl.sessions", support.session_row(support.REAL_PATH))

        code, out, err = self.run_cli("db", "reset", "--yes")
        self.assertEqual(code, 0, err)
        self.assertIsNone(TOKEN_SHAPE.search(out))
        new_token = self.token_path.read_text(encoding="ascii").strip()
        self.assertNotEqual(new_token, old_token)
        self.assertEqual(list((self.folder / "var" / "tokens").glob("*.txt")), [])
        store = open_store(self.loaded_settings())
        with store.app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 0)
            self.assertEqual([user.name for user in auth.list_users(db)], ["admin"])
            self.assertIsNone(auth.authenticate(db, old_token))
            self.assertEqual(auth.authenticate(db, new_token).name, "admin")

    def test_reset_in_a_folder_that_was_never_set_up(self):
        other = support.TempProject("sqlite")
        self.addCleanup(other.cleanup)
        code, _out, err = run_main(["db", "reset", "--yes"],
                                   env=clean_environment(other.path, PCLDB_ENGINE="sqlite"))
        self.assertEqual(code, 1)
        self.assertIn("pcldb setup", err)
        self.assertFalse((other.path / "var").exists())


# ---------------------------------------------------------------------- setup and db, PostgreSQL


class TestSetupPostgres(TempFolderCase):
    """One real local cluster in a temporary folder on a free port: made by setup, stopped and
    deleted at the end, also when a step fails."""

    def setUp(self):
        support.require_postgres()
        super().setUp()
        self.port = support.free_port()
        self.addCleanup(self.stop_cluster)      # runs before the folder is removed

    def stop_cluster(self):
        from pcldb.store import pgcluster
        try:
            settings = self.loaded_settings()
        except PclError:
            return
        if settings.database.engine == "postgresql":
            pgcluster.LocalCluster(settings).stop()

    def is_running(self):
        from pcldb.store import pgcluster
        return pgcluster.LocalCluster(self.loaded_settings()).status()["running"]

    def state(self):
        files = {}
        for name in (config.CONFIG_NAME, "var/admin_token.txt", "var/local_secrets.json"):
            files[name] = (self.folder / name).read_bytes()
        with open_store(self.loaded_settings()).app() as db:
            users = db.query("SELECT * FROM {app}.users")
            tokens = db.query("SELECT * FROM {app}.tokens")
        return files, users, tokens

    def test_a_port_that_is_taken_is_refused_before_anything_is_written(self):
        import socket
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:
            taken.bind(("127.0.0.1", self.port))
            taken.listen(1)
            code, _out, err = self.run_cli("setup", "--engine", "postgresql", "--port",
                                           str(self.port))
        self.assertEqual(code, 1)
        self.assertIn("Port %d" % self.port, err)
        self.assertIn("--port", err)
        self.assert_clean(err)
        self.assertEqual(sorted(path.name for path in self.folder.iterdir()), [])

    def test_setup_status_stop_start_reset(self):
        port = str(self.port)
        code, out, err = self.run_cli("setup", "--engine", "postgresql", "--port", port)
        self.assertEqual(code, 0, err)
        self.assert_clean(out, err)
        var = self.folder / "var"
        token_path = var / "admin_token.txt"
        for path in (self.folder / config.CONFIG_NAME, var / "pgdata" / "PG_VERSION",
                     var / "local_secrets.json", token_path):
            self.assertTrue(path.is_file(), path)
        written = json.loads((self.folder / config.CONFIG_NAME).read_text(encoding="utf-8"))
        self.assertEqual(written["database"],
                         {"engine": "postgresql", "postgresql": {"port": self.port}})
        token = token_path.read_text(encoding="ascii").strip()
        self.assertRegex(token, r"\A%s\Z" % TOKEN_SHAPE.pattern)
        self.assertIsNone(TOKEN_SHAPE.search(out))
        secrets_text = (var / "local_secrets.json").read_text(encoding="utf-8")
        for password in (value for key, value in json.loads(secrets_text).items()
                         if not key.startswith("_")):
            self.assertNotIn(password, out)
        self.assertTrue(self.is_running())
        with open_store(self.loaded_settings()).app() as db:
            self.assertEqual([(user.name, user.role) for user in auth.list_users(db)],
                             [("admin", "admin")])
            self.assertEqual(auth.authenticate(db, token).name, "admin")

        # A second run changes nothing.
        before = self.state()
        code, out, err = self.run_cli("setup", "--engine", "postgresql", "--port", port)
        self.assertEqual(code, 0, err)
        self.assertIn("running already", out)
        self.assertEqual(self.state(), before)

        # db status, while it runs.
        code, out, err = self.run_cli("db", "status")
        self.assertEqual(code, 0, err)
        self.assertIn("Running: yes (127.0.0.1, port %s" % port, out)
        self.assertIn("Set up: yes", out)
        self.assertIn(str(var / "pgdata"), out)

        # db stop; stopping twice is fine; a command that needs the database says what to do.
        code, out, err = self.run_cli("db", "stop")
        self.assertEqual(code, 0, err)
        self.assertIn("Stopped", out)
        self.assertFalse(self.is_running())
        code, out, err = self.run_cli("db", "stop")
        self.assertEqual(code, 0, err)
        self.assertIn("not running", out)
        code, out, err = self.run_cli("db", "status")
        self.assertEqual(code, 0, err)
        self.assertIn("Running: no", out)
        self.assertIn("pcldb db start", out)
        code, out, err = self.run_cli("user", "list")
        self.assertEqual(code, 1)
        self.assertIn("pcldb db start", err)
        self.assert_clean(err)

        # db start; starting twice is fine; the records are still there.
        code, out, err = self.run_cli("db", "start")
        self.assertEqual(code, 0, err)
        self.assertIn("Started", out)
        self.assertTrue(self.is_running())
        code, out, err = self.run_cli("db", "start")
        self.assertEqual(code, 0, err)
        self.assertIn("running already", out)
        code, out, err = self.run_cli("user", "list")
        self.assertEqual(code, 0, err)
        self.assertIn("admin", out)

        # db reset: refused without --yes; with it, everything is new.
        code, out, err = self.run_cli("db", "reset")
        self.assertEqual(code, 1)
        self.assertEqual(self.state(), before)
        self.assertEqual(self.run_cli("user", "add", "demo-operator-a")[0], 0)
        code, out, err = self.run_cli("db", "reset", "--yes")
        self.assertEqual(code, 0, err)
        self.assert_clean(out, err)
        self.assertIsNone(TOKEN_SHAPE.search(out))
        self.assertTrue(self.is_running())
        new_token = token_path.read_text(encoding="ascii").strip()
        self.assertNotEqual(new_token, token)
        self.assertNotEqual((var / "local_secrets.json").read_bytes(),
                            before[0]["var/local_secrets.json"])
        with open_store(self.loaded_settings()).app() as db:
            self.assertEqual([user.name for user in auth.list_users(db)], ["admin"])
            self.assertIsNone(auth.authenticate(db, token))
            self.assertEqual(auth.authenticate(db, new_token).name, "admin")

        code, out, err = self.run_cli("db", "stop")
        self.assertEqual(code, 0, err)
        self.assertFalse(self.is_running())

    def test_the_switch_from_sqlite_to_postgresql_keeps_a_working_admin_token_file(self):
        # SEC2-03 / C2-06: the documented switch, SQLite to a local PostgreSQL cluster made by
        # the second setup, in one project folder.
        switch_in_one_folder(self, {"engine": "postgresql", "postgresql": {"port": self.port}})
        self.assertTrue(self.is_running())
        code, _out, err = self.run_cli("db", "stop")
        self.assertEqual(code, 0, err)


# ---------------------------------------------------------------------- user and token commands


class UserTokenCases:
    """`pcldb user ...` and `pcldb token ...`, through the real parser, on both engines."""

    def cli(self, *argv):
        return run_main(argv, settings=self.settings)

    def ok(self, *argv):
        code, out, err = self.cli(*argv)
        self.assertEqual(code, 0, err)
        self.assertEqual(err, "")
        self.assertTrue(out.isascii())
        self.assertIsNone(TOKEN_SHAPE.search(out), "a token was printed")
        return out

    def refused(self, *argv):
        code, out, err = self.cli(*argv)
        self.assertEqual(code, 1, out)
        self.assertNotIn("Traceback", err)
        self.assertTrue(err.strip())
        self.assertTrue(err.isascii())
        return err

    def users(self):
        with self.store.app() as db:
            return {user.name: user for user in auth.list_users(db)}

    def test_user_add_list_role_disable_enable(self):
        self.assertIn("no user yet", self.ok("user", "list"))
        out = self.ok("user", "add", "admin", "--role", "admin")
        self.assertIn("pcldb token new --user admin", out)
        self.ok("user", "add", "demo-operator-a", "--role", "uploader", "--display-name",
                "Demo operator A")
        self.ok("user", "add", "demo-reviewer")
        users = self.users()
        self.assertEqual({name: user.role for name, user in users.items()},
                         {"admin": "admin", "demo-operator-a": "uploader",
                          "demo-reviewer": "viewer"})
        self.assertEqual(users["demo-operator-a"].display_name, "Demo operator A")

        out = self.ok("user", "list")
        lines = out.splitlines()
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0].split()[:3], ["NAME", "ROLE", "STATE"])
        self.assertEqual([line.split()[0] for line in lines[1:]],
                         ["admin", "demo-operator-a", "demo-reviewer"])
        self.assertIn("Demo operator A", out)
        out = self.ok("user", "list", "demo-reviewer")
        self.assertEqual(len(out.splitlines()), 2)
        self.assertIn("no user named", self.refused("user", "list", "nobody"))

        self.assertIn("analyst", self.ok("user", "role", "demo-reviewer", "--role", "analyst"))
        self.assertEqual(self.users()["demo-reviewer"].role, "analyst")
        self.assertIn("already", self.ok("user", "role", "demo-reviewer", "--role", "analyst"))

        self.ok("user", "disable", "demo-reviewer")
        self.assertTrue(self.users()["demo-reviewer"].disabled)
        self.assertIn("off", self.ok("user", "list", "demo-reviewer").splitlines()[1])
        self.assertIn("already", self.ok("user", "disable", "demo-reviewer"))
        self.ok("user", "enable", "demo-reviewer")
        self.assertFalse(self.users()["demo-reviewer"].disabled)

    def test_user_commands_that_are_refused(self):
        self.ok("user", "add", "admin", "--role", "admin")
        self.assertIn("exists already", self.refused("user", "add", "admin"))
        self.assertIn("not a usable user name", self.refused("user", "add", "Demo Person"))
        self.assertIn("pcldb user add <name>", self.refused("user", "add"))
        self.assertIn("--role", self.refused("user", "role", "admin"))
        self.assertIn("no user named", self.refused("user", "role", "nobody", "--role", "viewer"))
        self.assertIn("pcldb user disable <name>", self.refused("user", "disable"))
        self.assertIn("only admin", self.refused("user", "disable", "admin"))
        self.assertIn("only admin", self.refused("user", "role", "admin", "--role", "viewer"))
        self.assertEqual(list(self.users()), ["admin"])
        self.assertFalse(self.users()["admin"].disabled)

    def test_token_new_list_revoke(self):
        self.ok("user", "add", "demo-operator-a", "--role", "uploader")
        self.assertIn("no token", self.ok("token", "list"))

        out = self.ok("token", "new", "--user", "demo-operator-a", "--label", "upload laptop")
        folder = self.settings.var_dir / "tokens"
        files = sorted(folder.glob("*.txt"))
        self.assertEqual(len(files), 1)
        self.assertIn(str(files[0]), out)
        self.assertRegex(files[0].name, r"\Ademo-operator-a_[0-9a-f]{8}\.txt\Z")
        if os.name != "nt":
            self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)
        token = files[0].read_text(encoding="ascii").strip()
        self.assertRegex(token, r"\A%s\Z" % TOKEN_SHAPE.pattern)
        self.assertIn("does not expire", out)
        with self.store.app() as db:
            self.assertEqual(auth.authenticate(db, token).name, "demo-operator-a")
            listed = auth.list_tokens(db)
        self.assertEqual(len(listed), 1)
        token_id = listed[0]["token_id"]
        self.assertIn(token_id, out)
        self.assertTrue(files[0].name.startswith("demo-operator-a_" + token_id[:8]))

        out = self.ok("token", "new", "--user", "demo-operator-a", "--days", "0.5")
        self.assertIn("stops working at", out)
        self.assertEqual(len(list(folder.glob("*.txt"))), 2)

        out = self.ok("token", "list")
        lines = out.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines[0].split()[:2], ["TOKEN", "ID"])
        self.assertIn(token_id, out)
        self.assertIn("upload laptop", out)
        self.assertIn("active", out)
        self.assertNotIn(token[4:], out)
        self.assertNotIn(auth.token_sha256(token), out)
        self.assertEqual(len(self.ok("token", "list", "--user", "demo-operator-a").splitlines()),
                         3)

        out = self.ok("token", "revoke", "--id", token_id)
        self.assertIn("revoked", out)
        with self.store.app() as db:
            self.assertIsNone(auth.authenticate(db, token))
        self.assertIn("before", self.ok("token", "revoke", "--id", token_id.upper()))
        self.assertIn("revoked", self.ok("token", "list"))

    def test_token_commands_that_are_refused(self):
        self.ok("user", "add", "demo-operator-a")
        self.assertIn("--user", self.refused("token", "new"))
        self.assertIn("no user named", self.refused("token", "new", "--user", "nobody"))
        self.assertIn("no user named", self.refused("token", "list", "--user", "nobody"))
        self.assertIn("--id", self.refused("token", "revoke"))
        self.assertIn("no token with the id", self.refused("token", "revoke", "--id", "0" * 32))
        self.assertIn("not a token id", self.refused("token", "revoke", "--id", "12"))
        self.ok("user", "disable", "demo-operator-a")
        self.assertIn("switched off", self.refused("token", "new", "--user", "demo-operator-a"))
        self.assertEqual(list((self.settings.var_dir / "tokens").glob("*")), [])
        with self.store.app() as db:
            self.assertEqual(auth.list_tokens(db), [])

    def test_a_token_whose_file_cannot_be_written_is_not_kept(self):
        self.ok("user", "add", "demo-operator-a")
        with mock.patch.object(auth, "save_token_file",
                               side_effect=PclError("The folder cannot be written to.")):
            self.assertIn("cannot be written", self.refused("token", "new", "--user",
                                                            "demo-operator-a"))
        with self.store.app() as db:
            self.assertEqual(auth.list_tokens(db), [])


class TestUserTokenSqlite(UserTokenCases, support.SqliteCase, unittest.TestCase):
    pass


class TestUserTokenPostgres(UserTokenCases, support.PostgresCase, unittest.TestCase):
    pass


class TestNotSetUp(TempFolderCase):

    def test_commands_before_setup_say_to_run_setup(self):
        for argv in (["user", "list"], ["token", "list"], ["user", "add", "demo-reviewer"]):
            with self.subTest(argv=argv):
                code, out, err = self.run_cli(*argv, PCLDB_ENGINE="sqlite")
                self.assertEqual((code, out), (1, ""))
                self.assertIn("pcldb setup", err)
                self.assert_clean(err)
        self.assertFalse((self.folder / "var" / "sqlite" / "pcl_app.sqlite").exists())


# ---------------------------------------------------------------------- real parts, real calls


def _built(module):
    return importlib.util.find_spec("pcldb." + module) is not None


class TestRealCommands(TempFolderCase):
    """One real call of each part that exists, through the parser and the lazy import."""

    def setUp(self):
        super().setUp()
        code, _out, err = self.run_cli("setup", "--engine", "sqlite")
        self.assertEqual(code, 0, err)

    def test_sql(self):
        code, out, err = self.run_cli("sql", "SELECT COUNT(*) AS n FROM sessions", "--json")
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["rows"], [[0]])
        code, out, err = self.run_cli("sql", "DELETE FROM sessions")
        self.assertEqual(code, 1)
        self.assert_clean(err)

    def test_ask_without_the_llm(self):
        code, out, err = self.run_cli("ask", "how many sessions are there", "--no-llm", "--json")
        self.assertEqual(code, 0, err)
        result = json.loads(out)
        self.assertEqual(result["planned_by"], "rules")
        self.assertEqual(result["total"], 0)

    def test_search_status_and_query(self):
        code, out, err = self.run_cli("search", "status")
        self.assertEqual(code, 0, err)
        code, out, err = self.run_cli("search", "query", "--request", '{"q": "*", "limit": 1}')
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["total"], 0)
        code, out, err = self.run_cli("search", "query", "--request", '{"nonsense": 1}')
        self.assertEqual(code, 1)
        self.assert_clean(err)

    def test_search_use_local_writes_the_setting(self):
        code, _out, err = self.run_cli("search", "use-local")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.loaded_settings().search.backend, "local")

    def test_register_a_document(self):
        document = self.folder / "document.json"
        document.write_text(json.dumps(registry_support.make_document()), encoding="utf-8")
        code, out, err = self.run_cli("register", "--document", str(document))
        self.assertEqual(code, 0, err)
        self.assertIn("created", out)
        code, out, err = self.run_cli("register", "--document", str(document))
        self.assertEqual(code, 0, err)
        self.assertIn("unchanged", out)
        code, out, err = self.run_cli("register")
        self.assertEqual(code, 1)
        self.assert_clean(err)
        with open_store(self.loaded_settings()).app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 1)

    @unittest.skipUnless(_built("demo"), "pcldb/demo.py is not there")
    def test_demo_load_and_clear(self):
        code, _out, err = self.run_cli("demo", "load")
        self.assertEqual(code, 0, err)
        with open_store(self.loaded_settings()).app() as db:
            self.assertGreater(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 0)
        code, _out, err = self.run_cli("demo", "clear")
        self.assertEqual(code, 0, err)
        with open_store(self.loaded_settings()).app() as db:
            self.assertEqual(db.scalar("SELECT COUNT(*) FROM {pcl}.sessions"), 0)

    @unittest.skipUnless(_built("reconcile"), "pcldb/reconcile.py is not there")
    def test_load_index_of_a_file_that_is_not_there(self):
        code, _out, err = self.run_cli("load-index", str(self.folder / "no such file.sqlite"))
        self.assertEqual(code, 1)
        self.assert_clean(err)

    @unittest.skipUnless(_built("transfer"), "pcldb/transfer.py is not there")
    def test_dump_and_export(self):
        dump = self.folder / "dump.jsonl"
        code, _out, err = self.run_cli("dump", str(dump))
        self.assertEqual(code, 0, err)
        self.assertTrue(dump.is_file())
        copy = self.folder / "copy.sqlite"
        code, _out, err = self.run_cli("export-sqlite", str(copy))
        self.assertEqual(code, 0, err)
        self.assertTrue(copy.is_file())


# ---------------------------------------------------------------------- python -m and launchers


class TestProcess(TempFolderCase):
    """`python -m pcldb` and the launchers as a person runs them: a new process."""

    def run_process(self, command, cwd=None):
        env = dict(self.env)
        return subprocess.run(command, cwd=str(cwd or self.folder), env=env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120)

    def module(self, *argv, cwd=None):
        env_path = str(PROJECT)
        self.env["PYTHONPATH"] = env_path
        return self.run_process([sys.executable, "-B", "-m", "pcldb"] + list(argv), cwd)

    def test_help_from_another_folder(self):
        done = self.module("--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(b"usage: pcldb", done.stdout)
        self.assertEqual(done.stderr, b"")

    def test_a_pcl_error_is_exit_code_1_without_a_stack_trace(self):
        self.env["PCLDB_ENGINE"] = "sqlite"
        done = self.module("user", "list")
        self.assertEqual(done.returncode, 1)
        self.assertEqual(done.stdout, b"")
        self.assertIn(b"pcldb setup", done.stderr)
        self.assertNotIn(b"Traceback", done.stderr)
        self.assertTrue(done.stderr.isascii())

    def test_a_wrong_option_is_exit_code_2(self):
        done = self.module("setup", "--no-such-option")
        self.assertEqual(done.returncode, 2)
        self.assertIn(b"usage: pcldb", done.stderr)
        self.assertNotIn(b"Traceback", done.stderr)
        done = self.module()
        self.assertEqual(done.returncode, 2)

    def test_setup_and_a_query_in_a_new_process(self):
        done = self.module("setup", "--engine", "sqlite")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue((self.folder / "var" / "admin_token.txt").is_file())
        token = (self.folder / "var" / "admin_token.txt").read_bytes().strip()
        self.assertNotIn(token, done.stdout + done.stderr)
        done = self.module("user", "list")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(b"admin", done.stdout)


class TestLaunchers(TempFolderCase):

    def test_line_endings(self):
        posix = POSIX_LAUNCHER.read_bytes()
        self.assertTrue(posix.startswith(b"#!/bin/sh\n"))
        self.assertNotIn(b"\r", posix)
        self.assertTrue(posix.endswith(b"\n"))
        windows = WINDOWS_LAUNCHER.read_bytes()
        self.assertTrue(windows.endswith(b"\r\n"))
        self.assertEqual(windows.count(b"\n"), windows.count(b"\r\n"))
        self.assertNotIn(b"\r\r", windows)
        self.assertGreater(windows.count(b"\r\n"), 10)
        self.assertTrue(posix.isascii() and windows.isascii())

    def test_what_the_windows_launcher_runs(self):
        text = WINDOWS_LAUNCHER.read_text(encoding="ascii")
        self.assertIn(r'"%PCLDB_HERE%\.venv\Scripts\python.exe" -m pcldb %*', text)
        self.assertIn("py -3 -m pcldb %*", text)
        self.assertIn('set "PCLDB_HERE=%~dp0"', text)
        self.assertIn('set "PYTHONPATH=%PCLDB_HERE%;%PYTHONPATH%"', text)
        self.assertIn('set "PYTHONPATH=%PCLDB_HERE%"', text)
        self.assertIn("exit /b %ERRORLEVEL%", text)
        # The venv is looked for before the Python launcher is used.
        self.assertLess(text.index(r".venv\Scripts\python.exe"), text.index("py -3 -m pcldb"))

    def test_what_the_posix_launcher_runs(self):
        text = POSIX_LAUNCHER.read_text(encoding="ascii")
        self.assertIn('exec "$python" -m pcldb "$@"', text)
        self.assertIn("$here/.venv/bin/python", text)
        self.assertIn("export PYTHONPATH", text)

    @unittest.skipIf(os.name == "nt", "the executable bit does not exist on Windows")
    def test_the_posix_launcher_is_executable(self):
        self.assertTrue(os.access(POSIX_LAUNCHER, os.X_OK))
        self.assertEqual(POSIX_LAUNCHER.stat().st_mode & 0o111, 0o111)

    @unittest.skipIf(os.name == "nt" or shutil.which("sh") is None, "needs a POSIX sh")
    def test_sh_accepts_the_posix_launcher(self):
        done = subprocess.run(["sh", "-n", str(POSIX_LAUNCHER)], stdin=subprocess.DEVNULL,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
        self.assertEqual((done.returncode, done.stderr), (0, b""))

    @unittest.skipIf(os.name == "nt" or shutil.which("sh") is None, "needs a POSIX sh")
    def test_the_posix_launcher_works_from_another_folder(self):
        for command in ([str(POSIX_LAUNCHER), "--help"], ["sh", str(POSIX_LAUNCHER), "--help"]):
            done = subprocess.run(command, cwd=str(self.folder), env=self.env,
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, timeout=120)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertIn(b"usage: pcldb", done.stdout)
        done = subprocess.run([str(POSIX_LAUNCHER), "db", "status"], cwd=str(self.folder),
                              env=dict(self.env, PCLDB_ENGINE="sqlite"),
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn(str(self.folder / "var" / "sqlite").encode("utf-8"), done.stdout)
        done = subprocess.run([str(POSIX_LAUNCHER), "--no-such-option"], cwd=str(self.folder),
                              env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(done.returncode, 2)

    @unittest.skipIf(os.name == "nt" or shutil.which("sh") is None, "needs a POSIX sh")
    def test_the_posix_launcher_works_through_a_link(self):
        link = self.folder / "link to pcldb"
        os.symlink(POSIX_LAUNCHER, link)
        done = subprocess.run([str(link), "--version"], cwd=str(self.folder), env=self.env,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, timeout=120)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith(b"pcldb "))


if __name__ == "__main__":
    unittest.main()
