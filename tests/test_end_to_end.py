"""The story a new user lives through, per engine, with real processes (specification 11, 13):

    python -m pcldb setup --engine X --demo        in a temporary PCLDB_DIR (nothing installed)
    python -m pcldb serve --port <free port>       in the background
    the printed sign-in link -> a token (POST /signin), then /me, /status, /ask (keyword rules,
    because the stand-in ARC service says "VPN only"), /search, /session, /sql with an example,
    /sql/schema, /sql/examples, /sql/history, a registration of a synthetic Raman upload record
    through `pcldb register --server` (which uses pcldb.client), /registrations, and one /ask
    with the assistant on (the fake LLM server answers);
    then the server is stopped the way Ctrl+C stops it, and on PostgreSQL the test checks that
    the database cluster serve had started was stopped again.

How the server is stopped, on every system (`stop_like_ctrl_c`): on Windows a child process
cannot be sent SIGINT, so it is started in a new process group
(`subprocess.CREATE_NEW_PROCESS_GROUP`) and sent `CTRL_BREAK_EVENT`, which `pcldb serve`
handles like Ctrl+C (SIGBREAK); everywhere else it is sent SIGINT, exactly what Ctrl+C sends.

Never port 8787 or 15439 (free ports are given with --port), never ~/.pcldb (PCLDB_USER_DIR is
a temporary folder), never the project's own var/ (PCLDB_DIR is a temporary folder).
"""

from __future__ import annotations

import os
import queue
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pcldb import config
from pcldb.client import Client
from tests import registry_support as rs
from tests import support
from tests.fake_llm import FAKE_KEY, VPN_403, FakeLlmServer, content

CODE_ROOT = Path(__file__).resolve().parent.parent
OPEN_LINE = re.compile(r"^Open (http://127\.0\.0\.1:(\d+)/)#signin=([A-Za-z0-9_-]{32})$")

PLAN = ('{"keywords": "", "filters": [{"field": "instrument", "op": "any", "values": '
        '["raman-portable"]}, {"field": "labels", "op": "any", "values": ["adenine"]}], '
        '"sort": {"field": "started", "order": "desc"}, "limit": 5, "count_by": null, '
        '"explanation": "Raman sessions with adenine."}')
SUMMARY = "Several Raman demo sessions are labelled adenine; the newest is listed first."


def child_environment(project, user_dir, extra=None):
    """The environment of a pcldb child process: no PCLDB_* or PG* setting of this shell
    (except PCLDB_PG_BIN, which picks the PostgreSQL version the tests run against)."""
    env = {name: value for name, value in os.environ.items()
           if not (name.startswith("PCLDB_") and name != "PCLDB_PG_BIN")
           and not name.startswith("PG")}
    env.update({"PCLDB_DIR": str(project), "PCLDB_USER_DIR": str(user_dir),
                "PYTHONPATH": str(CODE_ROOT), "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1"})
    env.update(extra or {})
    return env


def pcldb(args, env, timeout=300):
    """Run `python -m pcldb ...` to the end; returns (exit code, stdout + stderr)."""
    done = subprocess.run([sys.executable, "-m", "pcldb"] + list(args), env=env,
                          cwd=str(Path(env["PCLDB_DIR"]).parent), stdin=subprocess.DEVNULL,
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout,
                          encoding="utf-8", errors="replace")
    return done.returncode, done.stdout


def start_in_background(args, env):
    """Start `python -m pcldb ...` so that it can later be stopped like Ctrl+C."""
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0
    process = subprocess.Popen([sys.executable, "-m", "pcldb"] + list(args), env=env,
                               cwd=str(Path(env["PCLDB_DIR"]).parent), stdin=subprocess.DEVNULL,
                               stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               encoding="utf-8", errors="replace", creationflags=flags)
    lines = queue.Queue()

    def read():
        try:
            for line in process.stdout:
                lines.put(line.rstrip("\r\n"))
        finally:
            process.stdout.close()
            lines.put(None)

    threading.Thread(target=read, daemon=True).start()
    return process, lines


def stop_like_ctrl_c(process):
    """What Ctrl+C does, on every system (see the module text)."""
    if os.name == "nt":
        process.send_signal(signal.CTRL_BREAK_EVENT)
    else:
        process.send_signal(signal.SIGINT)


def wait_for_line(lines, pattern, seen, timeout=120):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            line = lines.get(timeout=max(0.1, deadline - time.monotonic()))
        except queue.Empty:
            break
        if line is None:
            break
        seen.append(line)
        found = pattern.match(line)
        if found:
            return found
    raise AssertionError("pcldb serve did not print the sign-in link. Output:\n" + "\n".join(seen))


class StoryCases:
    engine = None

    def setUp(self):
        super().setUp()
        if self.engine == "postgresql":
            support.require_postgres()
        self.root = Path(tempfile.mkdtemp(prefix="pcldb e2e "))
        self.addCleanup(support.remove_tree, self.root)
        self.project = self.root / "project"
        self.user_dir = self.root / "user"
        self.env = child_environment(self.project, self.user_dir)

    def _stop_cluster_if_left(self):
        if self.engine != "postgresql":
            return
        try:
            from pcldb.store import pgcluster
            settings = config.load_settings(project_dir=self.project, user_dir=self.user_dir,
                                            env={})
            pgcluster.LocalCluster(settings).stop()
        except Exception:       # noqa: BLE001 - best effort after a failure
            pass

    def test_the_story_of_a_new_user(self):
        # 1. Setup, with the demo data.
        args = ["setup", "--engine", self.engine, "--demo"]
        if self.engine == "postgresql":
            args += ["--port", str(support.free_port())]
        self.addCleanup(self._stop_cluster_if_left)
        code, output = pcldb(args, self.env)
        self.assertEqual(code, 0, output)
        self.assertIn("Loaded the demo data: 16 made-up sessions.", output)
        self.assertIn("Setup is done.", output)
        self.assertTrue((self.project / "var" / "admin_token.txt").is_file())
        admin_token = (self.project / "var" / "admin_token.txt").read_text(encoding="utf-8")
        self.assertNotIn(admin_token.strip(), output)
        settings = config.load_settings(project_dir=self.project, user_dir=self.user_dir, env={})
        if self.engine == "postgresql":
            code, output = pcldb(["db", "stop"], self.env)      # serve must start it itself
            self.assertEqual(code, 0, output)

        with FakeLlmServer() as fake:
            fake.default = VPN_403          # the ARC service as seen from off the VPN
            env = child_environment(self.project, self.user_dir, {
                "PCLDB_LLM_BASE_URL": fake.base_url, "PCLDB_LLM_API_KEY": FAKE_KEY})
            port = support.free_port()
            process, lines = start_in_background(["serve", "--port", str(port)], env)

            def stop_server():
                if process.poll() is None:
                    process.kill()
                    process.wait(30)
            self.addCleanup(stop_server)
            seen = []
            found = wait_for_line(lines, OPEN_LINE, seen)
            base, printed_port, signin_code = found.group(1), int(found.group(2)), found.group(3)
            self.assertEqual(printed_port, port)
            if self.engine == "postgresql":
                self.assertTrue(any(line.startswith("Started the local PostgreSQL database")
                                    for line in seen), seen)

            # 2. The printed link's code becomes a token.
            client = Client(base, None)
            signed_in = client.signin(signin_code)
            token = signed_in["token"]
            self.assertEqual(signed_in["user"]["name"], "admin")

            # 3. Every endpoint the website uses, once.
            me = client.me()
            self.assertEqual(me["engine"]["name"], self.engine)
            self.assertEqual(me["capabilities"], ["ask", "sql", "register", "manage"])
            status = client.status()
            self.assertEqual((status["database"]["sessions"], status["database"]["demo"]),
                             (16, True))
            self.assertEqual(status["assistant"]["state"], "ready")
            asked = client.ask("adenine sessions", include_simulated=True)
            self.assertEqual(asked["planned_by"], "rules")
            self.assertTrue(any("VPN" in notice for notice in asked["notices"]), asked["notices"])
            self.assertGreater(asked["total"], 0)
            self.assertTrue(all("adenine" in result.get("labels", [])
                                for result in asked["results"]))
            searched = client.search({"q": "*", "limit": 3, "facets": [
                {"name": "instrument", "type": "terms", "field_name": "instrument"}]})
            self.assertEqual((searched["backend"], searched["result"]["total"]), ("local", 16))
            path = asked["results"][0]["path"]
            detail = client.session(path)
            self.assertEqual(detail["session"]["path"], path)
            self.assertGreater(detail["files"]["total"], 0)
            examples = client.sql_examples()
            self.assertEqual(examples["engine"], self.engine)
            first = examples["examples"][0]
            rows = client.sql(first["sql"])
            self.assertGreater(rows["row_count"], 0)
            schema = client.sql_schema()
            self.assertIn("sessions", [item["name"] for item in schema["objects"]])
            history = client.sql_history()
            self.assertEqual(history["items"][0]["query_text"], first["sql"])

            # 4. One question with the assistant on.
            fake.script(content(PLAN), content(SUMMARY))
            assisted = client.ask("which raman sessions found adenine?", include_simulated=True)
            self.assertEqual((assisted["planned_by"], assisted["answer_by"]), ("llm", "llm"))
            self.assertEqual(assisted["answer"], SUMMARY)
            self.assertGreater(assisted["total"], 0)
            self.assertEqual(len(fake.chat_requests), 1 + 2)     # 1 refused earlier, 2 now
            self.assertEqual(fake.chat_requests[-1]["headers"]["authorization"],
                             "Bearer " + FAKE_KEY)

            # 5. A Raman upload registered through the server (pcldb register --server): refused
            # while the demo is loaded (demo and real data never mix), done after demo clear.
            home = self.root / "controller home"
            made = rs.write_raman_record(home)
            register = ["register", "--raman-uploads", str(home), "--scan-sessions",
                        str(made["scan_sessions"]), "--server", base]
            with_token = child_environment(self.project, self.user_dir, {"PCLDB_TOKEN": token})
            code, output = pcldb(register, with_token)
            self.assertEqual(code, 1, output)
            self.assertIn("Remove the demo data first: pcldb demo clear", output)
            code, output = pcldb(["demo", "clear"], self.env)
            self.assertEqual(code, 0, output)
            code, output = pcldb(register, with_token)
            self.assertEqual(code, 0, output)
            registered = "raman_globus_upload/simulated/" + rs.DEMO_SESSION
            self.assertIn("created    " + registered, output)
            self.assertNotIn(token, output)
            for marker in rs.PERSONAL_MARKERS.values():
                self.assertNotIn(marker, output)
            newest = client.registrations()["items"][0]
            self.assertEqual((newest["session_path"], newest["user_name"], newest["action"],
                              newest["source"]), (registered, "admin", "created", "upload"))
            again = client.ask("newest sessions", include_simulated=True)
            self.assertIn(registered, [result["path"] for result in again["results"]])

            # 6. Stop it the way Ctrl+C does.
            stop_like_ctrl_c(process)
            self.assertEqual(process.wait(120), 0)
        rest = []
        while True:
            line = lines.get(timeout=30)
            if line is None:
                break
            rest.append(line)
        printed = "\n".join(seen + rest)
        self.assertIn("Stopping the website.", printed)
        self.assertNotIn("Traceback", printed)
        self.assertNotIn(token, printed)
        self.assertNotIn(FAKE_KEY, printed)
        log = (self.project / "var" / "log" / "server.log").read_text(encoding="utf-8")
        self.assertIn("/api/v1/ask 200 admin", log)
        for secret in (token, FAKE_KEY, admin_token.strip(), signin_code):
            self.assertNotIn(secret, log)
        if self.engine == "postgresql":
            self.assertIn("Stopped the local PostgreSQL database (pcldb serve had started it).",
                          printed)
            from pcldb.store import pgcluster
            self.assertFalse(pgcluster.LocalCluster(settings).status()["running"])


class TestStorySqlite(StoryCases, unittest.TestCase):
    engine = "sqlite"


class TestStoryPostgres(StoryCases, unittest.TestCase):
    engine = "postgresql"


if __name__ == "__main__":
    unittest.main()
