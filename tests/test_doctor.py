"""Tests of pcldb/doctor.py (specification section 13): one line per check, OK or PROBLEM with
the fix; exit 0 when the essentials are fine; the five hostile statements through the sandbox;
the sentence about the ARC LLM key; nothing secret in the output. Also from_lead 20b: when
the PostgreSQL role check has notes, doctor and /status say in one sentence what a reader can
still do there.
"""

from __future__ import annotations

import argparse
import dataclasses
import io
import json
import socket
import unittest
from pathlib import Path
from unittest import mock

from pcldb import cli, doctor, server, sqlrun
from pcldb.store import open_store
from tests import support
from tests.fake_llm import FAKE_KEY, FakeLlmServer
from tests.test_server import RunningServer, http, make_users

LINE = r"^(OK      |PROBLEM |        ) "


def write_settings_file(settings):
    """A settings file like the one setup writes (without it doctor says: run pcldb setup)."""
    settings.config_path.write_text(json.dumps({"database": {"engine": settings.database.engine},
                                                "server": {"port": settings.server.port}}),
                                    encoding="utf-8")


class DoctorCases:

    def setUp(self):
        super().setUp()
        write_settings_file(self.settings)
        cli.ensure_admin(self.store, self.settings)

    def run_doctor(self, settings=None):
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = doctor.cmd_doctor(argparse.Namespace(command="doctor", dir=None),
                                     settings or self.settings)
        return code, out.getvalue().splitlines()

    def line(self, lines, what):
        found = [text for text in lines if text[9:].startswith(what + ":")]
        self.assertEqual(len(found), 1, (what, lines))
        return found[0]

    def test_a_healthy_project(self):
        code, lines = self.run_doctor()
        self.assertEqual(code, 0, "\n".join(lines))
        for text in lines[1:-1]:
            self.assertRegex(text, LINE)
            text.encode("ascii")
        for what in ("Python", "psycopg", "settings", "engine", "database",
                     "expert-mode sandbox", "records", "users", "search", "website port"):
            self.assertTrue(self.line(lines, what).startswith("OK "), what)
        sandbox = self.line(lines, "expert-mode sandbox")
        self.assertIn("1 harmless query ran and all 5 hostile statements were stopped", sandbox)
        # No ARC key in a test project: a PROBLEM, but not an essential one.
        assistant = self.line(lines, "ARC assistant")
        self.assertTrue(assistant.startswith("PROBLEM "))
        self.assertIn("Fix:", assistant)
        self.assertIn(doctor.KEY_SENTENCE, " ".join(text.strip() for text in lines))
        self.assertIn("only answers on the VT campus network or VPN", doctor.KEY_SENTENCE)
        self.assertIn("the key of the person running pcldb on this computer",
                      doctor.KEY_SENTENCE)
        self.assertTrue(lines[-1].startswith("Result: the essentials are fine"), lines[-1])
        if self.engine == "postgresql":
            self.assertTrue(self.line(lines, "expert-mode role").startswith("OK "))

    def test_the_sandbox_really_runs_five_hostile_statements(self):
        seen = []
        real = sqlrun.sandbox_self_test

        def recording(store, *args, **kwargs):
            report = real(store, *args, **kwargs)
            seen.extend(report)
            return report

        with mock.patch.object(sqlrun, "sandbox_self_test", recording):
            code, _lines = self.run_doctor()
        self.assertEqual(code, 0)
        self.assertEqual(sum(1 for item in seen if not item["must_run"]), 5)
        self.assertTrue(all(item["ok"] for item in seen))

    def test_a_sandbox_that_lets_something_through_is_an_essential_problem(self):
        report = [{"sql": "SELECT 1", "must_run": True, "ok": True, "outcome": "ran"},
                  {"sql": "INSERT INTO sessions VALUES (1)", "must_run": False, "ok": False,
                   "outcome": "ran"}]
        with mock.patch.object(sqlrun, "sandbox_self_test", lambda store: report):
            code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        sandbox = self.line(lines, "expert-mode sandbox")
        self.assertTrue(sandbox.startswith("PROBLEM "))
        self.assertIn("INSERT INTO sessions VALUES (1) -> ran", sandbox)
        self.assertIn("stop pcldb from working", lines[-1])

    def test_a_key_saved_with_txt_is_pointed_out(self):
        # WIN-06: Notepad saves "arc_llm_api_key" as arc_llm_api_key.txt.
        path = self.settings.llm_api_key_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with_txt = path.with_name(path.name + ".txt")
        with_txt.write_text("not-a-real-key\n", encoding="utf-8")
        self.addCleanup(with_txt.unlink)
        _code, lines = self.run_doctor()
        assistant = self.line(lines, "ARC assistant")
        self.assertTrue(assistant.startswith("PROBLEM "))
        self.assertIn("arc_llm_api_key.txt is there", " ".join(text.strip() for text in lines))
        self.assertNotIn("not-a-real-key", "\n".join(lines))

    def test_with_the_assistant_reachable(self):
        with FakeLlmServer() as fake:
            settings = dataclasses.replace(
                self.settings.changed({"llm.base_url": fake.base_url}),
                env_llm_api_key=FAKE_KEY)
            from pcldb import llm
            llm.reset_process_state()
            code, lines = self.run_doctor(settings)
        self.assertEqual(code, 0)
        self.assertTrue(self.line(lines, "ARC assistant").startswith("OK "), lines)
        self.assertNotIn(FAKE_KEY, "\n".join(lines))

    def test_a_port_taken_by_another_program(self):
        with socket.socket() as taken:
            taken.bind(("127.0.0.1", 0))
            taken.listen(1)
            settings = self.settings.changed({"server.port": taken.getsockname()[1]})
            code, lines = self.run_doctor(settings)
        self.assertEqual(code, 0)
        port_line = self.line(lines, "website port")
        self.assertTrue(port_line.startswith("PROBLEM "))
        self.assertIn("pcldb serve --port", port_line)

    def test_a_running_pcldb_server_on_the_port_is_fine(self):
        running = RunningServer(self.settings, self.store)
        self.addCleanup(running.close)
        code, lines = self.run_doctor(self.settings.changed({"server.port": running.port}))
        self.assertEqual(code, 0)
        self.assertIn("pcldb serve is running on port", self.line(lines, "website port"))


class TestDoctorSqlite(DoctorCases, support.SqliteCase, unittest.TestCase):

    def test_a_folder_that_was_never_set_up(self):
        empty = support.TempProject("sqlite")
        self.addCleanup(empty.cleanup)
        out = io.StringIO()
        with mock.patch("sys.stdout", out):
            code = doctor.cmd_doctor(argparse.Namespace(command="doctor", dir=None),
                                     empty.settings)
        lines = out.getvalue().splitlines()
        self.assertEqual(code, 1)
        settings_line = [text for text in lines if text[9:].startswith("settings:")][0]
        self.assertTrue(settings_line.startswith("PROBLEM "))
        self.assertIn("pcldb setup", settings_line)

    def test_a_store_of_another_version(self):
        with self.store.app() as db:
            db.execute("UPDATE {app}.meta SET value = ? WHERE key = ?", ["99", "schema_version"])
        code, lines = self.run_doctor()
        self.assertEqual(code, 1)
        database = [text for text in lines if text[9:].startswith("database:")][0]
        self.assertIn("pcldb db reset --yes", database)


class TestDoctorPostgres(DoctorCases, support.PostgresCase, unittest.TestCase):

    def test_role_notes_are_said_in_one_sentence_by_doctor_and_status(self):
        """from_lead 20b: an external PostgreSQL whose owner is not a superuser."""
        notes = {"problems": [], "notes": ["The reader role may use the schema public."]}
        with mock.patch("pcldb.store.pg_hardening.check", lambda store: notes):
            code, lines = self.run_doctor()
            self.assertEqual(code, 0)
            self.assertIn(server.EXPERT_NOTE_WEAKER, [text.strip() for text in lines])
            with self.store.app() as db:
                tokens = make_users(db)
            running = RunningServer(self.settings, self.store)
            self.addCleanup(running.close)
            status = http(running.port, "GET", "/api/v1/status", token=tokens["viewer"]).json
            self.assertEqual(status["database"]["expert_note"], server.EXPERT_NOTE_WEAKER)
            sql = http(running.port, "POST", "/api/v1/sql", {"sql": "SELECT 1 AS one"},
                       token=tokens["analyst"])
            self.assertEqual(sql.status, 200)       # expert mode stays available

    def test_role_problems_stop_expert_mode(self):
        state = {"problems": ["it may use the private schema app"], "notes": []}
        with mock.patch("pcldb.store.pg_hardening.check", lambda store: state):
            code, lines = self.run_doctor()
            self.assertEqual(code, 1)
            role = [text for text in lines if text[9:].startswith("expert-mode role:")][0]
            self.assertTrue(role.startswith("PROBLEM "))
            with self.store.app() as db:
                tokens = make_users(db)
            running = RunningServer(self.settings, self.store)
            self.addCleanup(running.close)
            sql = http(running.port, "POST", "/api/v1/sql", {"sql": "SELECT 1 AS one"},
                       token=tokens["analyst"])
            self.assertEqual((sql.status, sql.json["error"]["code"]), (503, "sandbox_unsafe"))
            status = http(running.port, "GET", "/api/v1/status", token=tokens["viewer"]).json
            self.assertIn("switched off", status["database"]["expert_note"])

    def test_without_role_notes_status_has_exactly_the_specified_keys(self):
        with self.store.app() as db:
            tokens = make_users(db)
        running = RunningServer(self.settings, self.store)
        self.addCleanup(running.close)
        status = http(running.port, "GET", "/api/v1/status", token=tokens["viewer"]).json
        self.assertNotIn("expert_note", status["database"])


class FakeCluster:
    made = []

    def __init__(self, settings):
        self.port = settings.database.postgresql.port
        self.data_dir = Path("pgdata")
        self.calls = []
        FakeCluster.made.append(self)

    def is_initialized(self):
        return True

    def ensure_running(self):
        self.calls.append("ensure_running")
        return True

    def stop(self):
        self.calls.append("stop")
        return True


class TestDoctorLocalCluster(support.SqliteCase, unittest.TestCase):
    """A stopped local PostgreSQL database is started for the checks and stopped again."""

    def test_started_for_the_checks_and_stopped_again(self):
        import importlib.util
        if importlib.util.find_spec("psycopg") is None:
            # doctor says "PostgreSQL cannot be used without psycopg" before any cluster.
            self.skipTest("psycopg is not installed (the bare-Python run of the CI)")
        settings = self.settings.changed({"database.engine": "postgresql"})
        write_settings_file(settings)
        FakeCluster.made = []
        found = mock.Mock(version="18.7")
        out = io.StringIO()
        with mock.patch("pcldb.store.pgcluster.LocalCluster", FakeCluster), \
                mock.patch("pcldb.store.pgcluster.choose_pg_bin", lambda _settings: found), \
                mock.patch("pcldb.store.open_store", lambda _settings: self.store), \
                mock.patch("sys.stdout", out):
            code = doctor.cmd_doctor(argparse.Namespace(command="doctor", dir=None), settings)
        text = out.getvalue()
        self.assertEqual(FakeCluster.made[0].calls, ["ensure_running", "stop"])
        self.assertIn("doctor started it for these checks", text)
        self.assertIn("Stopped the local PostgreSQL database again.", text)
        self.assertEqual(code, 0, text)


if __name__ == "__main__":
    unittest.main()
