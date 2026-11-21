"""Tests of the advice in messages (C2-07): a refusal names commands with the launcher of this
system (./pcldb.sh on a Mac or Linux, .\\pcldb.cmd on Windows), and it never advises a command
that the project would refuse."""

from __future__ import annotations

import os
import sys
import types
import unittest
from unittest import mock

from pcldb import demo, registration, util
from pcldb.errors import Conflict, PclError
from pcldb.store import open_store, postgres_engine
from tests import support


class PsycopgMissing(unittest.TestCase):
    """Without psycopg, a project that was set up with PostgreSQL keeps it: "setup --engine
    sqlite" is refused there, so only installing psycopg is advised (as pgcluster does)."""

    def setUp(self):
        self.project = support.TempProject(engine="postgresql")
        self.addCleanup(self.project.cleanup)
        self.settings = self.project.settings
        hide = mock.patch.dict(sys.modules, {"psycopg": None})
        hide.start()
        self.addCleanup(hide.stop)

    def refusal(self, action):
        with self.assertRaises(PclError) as caught:
            action()
        self.assertEqual(caught.exception.code, "psycopg_missing")
        return str(caught.exception)

    def test_a_new_project_may_still_choose_sqlite(self):
        self.assertFalse(self.settings.config_path.exists())
        store = open_store(self.settings)
        for action in (lambda: postgres_engine.load_psycopg(self.settings),
                       lambda: store.conninfo("app")):
            message = self.refusal(action)
            self.assertIn("Run: %s" % util.INSTALL_PY, message)
            self.assertIn("%s setup --engine sqlite" % util.LAUNCHER, message)

    def test_a_project_that_was_set_up_is_not_told_to_switch(self):
        self.settings.config_path.write_text('{"database": {"engine": "postgresql"}}\n',
                                             encoding="utf-8")
        store = open_store(self.settings)
        for action in (lambda: postgres_engine.load_psycopg(self.settings),
                       lambda: store.conninfo("app"), lambda: store.app().__enter__()):
            message = self.refusal(action)
            self.assertIn("Run: %s" % util.INSTALL_PY, message)
            self.assertIn("set up with PostgreSQL", message)
            self.assertNotIn("--engine sqlite", message)

    def test_without_settings_the_project_folder_decides(self):
        with mock.patch.dict(os.environ, {"PCLDB_DIR": str(self.project.path)}):
            self.assertIn("--engine sqlite", self.refusal(postgres_engine.load_psycopg))
            self.settings.config_path.write_text("{}\n", encoding="utf-8")
            self.assertNotIn("--engine sqlite", self.refusal(postgres_engine.load_psycopg))


class PsycopgMissingWithTheDirOption(unittest.TestCase):
    """C2-07, second round: with --dir, the folder named there decides, not PCLDB_DIR (here an
    empty folder that was never set up) or the package folder."""

    def setUp(self):
        self.project = support.TempProject(engine="postgresql")
        self.addCleanup(self.project.cleanup)
        self.project.settings.config_path.write_text(
            '{"database": {"engine": "postgresql", "postgresql": {"port": 15571}}}\n',
            encoding="utf-8")
        self.elsewhere = support.TempProject(prefix="pcldb elsewhere ")
        self.addCleanup(self.elsewhere.cleanup)
        for patch in (mock.patch.dict(sys.modules, {"psycopg": None}),
                      mock.patch.dict(os.environ, {"PCLDB_DIR": str(self.elsewhere.path)})):
            patch.start()
            self.addCleanup(patch.stop)

    def cli(self, *argv):
        import contextlib
        import io
        from pcldb import cli
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["--dir", str(self.project.path)] + list(argv))
        return code, out.getvalue() + err.getvalue()

    def test_the_folder_of_the_dir_option_decides(self):
        for argv in (["sql", "select 1"], ["setup"], ["db", "reset", "--yes"]):
            with self.subTest(argv=argv):
                code, text = self.cli(*argv)
                self.assertEqual(code, 1, text)
                self.assertIn("Run: %s" % util.INSTALL_PY, text)
                self.assertIn("set up with PostgreSQL", text)
                self.assertNotIn("--engine sqlite", text)

    def test_serve_asks_the_project_it_serves(self):
        from pcldb import server
        settings = self.project.settings
        self.assertTrue(settings.config_path.exists())
        with self.assertRaises(PclError) as caught:
            server._serve_locked(settings, None, open_browser=False, out=sys.stdout)
        self.assertEqual(caught.exception.code, "psycopg_missing")
        self.assertNotIn("--engine sqlite", str(caught.exception))


class LauncherInRefusals(support.SqliteCase, unittest.TestCase):

    def test_registration_names_the_launcher(self):
        from tests import registry_support as rs
        with self.store.app() as db:
            db.upsert("pcl.meta", {"key": "demo", "value": "1"}, key=("key",))
        with self.assertRaises(Conflict) as caught:
            registration.register(self.store, rs.make_document(), actor="t", source="upload",
                                  settings=self.settings)
        self.assertEqual(caught.exception.code, "demo_loaded")
        self.assertIn("first: %s demo clear" % util.LAUNCHER, caught.exception.message)
        self.assertNotIn(" pcldb demo", caught.exception.message)
        no_token = types.SimpleNamespace(token_file=str(self.project.path / "no such file"))
        with self.assertRaises(PclError) as caught:
            registration._read_token(no_token, self.settings)
        self.assertIn("%s token new --user <name>" % util.LAUNCHER, caught.exception.message)

    def test_demo_names_the_launcher(self):
        with self.assertRaises(PclError) as caught:
            demo.cmd_demo(types.SimpleNamespace(action=None), self.settings)
        self.assertIn("%s demo load, or %s demo clear" % (util.LAUNCHER, util.LAUNCHER),
                      caught.exception.message)
        with self.store.app() as db:
            support.seed_minimal(db)
        with self.assertRaises(Conflict) as caught:
            with self.store.app() as db:
                demo.load_demo(db, self.settings)
        self.assertEqual(caught.exception.code, "real_data_present")
        self.assertIn("%s db reset --yes" % util.LAUNCHER, caught.exception.message)

    def test_the_launcher_is_the_one_of_this_system(self):
        self.assertEqual(util.LAUNCHER, ".\\pcldb.cmd" if os.name == "nt" else "./pcldb.sh")


if __name__ == "__main__":
    unittest.main()
