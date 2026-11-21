"""End-to-end smoke of pcldb, the way a person (or the CI) would try it on a new computer.

    python tools/smoke.py --engine sqlite|postgresql|both   [--keep] [--no-launcher]

For each engine, in a NEW temporary folder (with a space in its name) used as PCLDB_DIR, with a
temporary PCLDB_USER_DIR, through real processes (`python -m pcldb ...`):

    setup --engine X --demo        (PostgreSQL: on a free port, never 15439)
    the settings file gets a free website port, so nothing here uses port 8787
    doctor                         (exit 0)
    sql "<a query>" --json         (rows for both demo instruments)
    ask --no-llm --json "<a question>"
    db stop                        (PostgreSQL: so that serve has to start it, and stop it)
    serve --port P                 in the background; wait for /api/v1/health
    POST /signin with the printed one-time code, then /me, /status, /ask, /sql/examples, /sql
    stop serve the way Ctrl+C does (Windows: CTRL_BREAK_EVENT to its process group; elsewhere
    SIGINT); it must exit 0 and, on PostgreSQL, stop the database it started
    db stop, db status             (nothing may be left running: no answer on either port)
    SQLite only: the launcher (pcldb.cmd on Windows, pcldb.sh elsewhere) from a copy of the code
    in a folder with spaces, with its own empty .venv, with a JSON argument
    the temporary folder is removed (also after a failure, unless --keep)

One line per step: PASS or FAIL, and for a failure the last 40 lines of the step's output and of
the log files that belong to it. Exit code 0 only when every step passed.

Standard library only; runs on Windows, macOS and Linux. The PostgreSQL programs are found the
way pcldb finds them (PCLDB_PG_BIN, PATH, the usual places). Other PCLDB_* variables of the
calling shell are not passed on; PG* variables ARE passed on, on purpose: pcldb must ignore them.
Nothing in it needs the network: the ARC assistant's address points at a closed local port.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

CODE_ROOT = Path(__file__).resolve().parent.parent
RESERVED_PORTS = (8787, 15439)     # pcldb's defaults: a smoke never takes them
TAIL_LINES = 40
OPEN_LINE = re.compile(r"^Open (http://127\.0\.0\.1:(\d+)/)#signin=([A-Za-z0-9_-]{16,128})\s*$")
SQL = "SELECT instrument, COUNT(*) AS n FROM sessions GROUP BY instrument ORDER BY instrument"
QUESTION = "raman sessions with adenine"
LAUNCHER_REQUEST = '{"q": "*", "limit": 1}'
IS_WINDOWS = os.name == "nt"


# ---------------------------------------------------------------------- small helpers


def ascii_text(text):
    """What may be printed on any console: ASCII, control characters as spaces."""
    text = str(text).encode("ascii", "backslashreplace").decode("ascii")
    return "".join(char if " " <= char <= "~" else " " for char in text)


def say(text=""):
    sys.stdout.write(ascii_text(text) + "\n")
    sys.stdout.flush()


def tail(text, lines=TAIL_LINES):
    return text.splitlines()[-lines:]


def tail_file(path, lines=TAIL_LINES):
    """The last lines of a (log) file; only its end is read."""
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 200000))
            return tail(handle.read().decode("utf-8", "replace"), lines)
    except OSError:
        return []


def port_answers(port, host="127.0.0.1", timeout=0.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def can_bind(port, host="127.0.0.1"):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


_handed_out = set()


def free_port():
    """A free TCP port below 49152 (Windows hands out client ports above it), never one of
    pcldb's defaults and never one this process gave out before."""
    chooser = random.SystemRandom()
    for _ in range(500):
        port = chooser.randrange(20000, 49000)
        if port in RESERVED_PORTS or port in _handed_out or not can_bind(port):
            continue
        _handed_out.add(port)
        return port
    raise RuntimeError("no free TCP port was found between 20000 and 49000")


def child_environment(base, project, user_dir, code_root=CODE_ROOT):
    """The environment of every pcldb process of the smoke: no PCLDB_* setting of the calling
    shell (except PCLDB_PG_BIN, which picks the PostgreSQL programs), the temporary folders,
    and an ARC assistant address nobody answers on (no network, no key is used)."""
    env = {name: value for name, value in base.items()
           if not (name.upper().startswith("PCLDB_") and name.upper() != "PCLDB_PG_BIN")}
    env.update({
        "PCLDB_DIR": str(project),
        "PCLDB_USER_DIR": str(user_dir),
        "PCLDB_LLM_BASE_URL": "http://127.0.0.1:9/api/v1",
        "PYTHONPATH": str(code_root),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
    })
    return env


def remove_tree(path, attempts=20):
    """Delete a folder, also read-only files; on Windows a file can stay locked for a moment
    after the process that held it ended, so try again for a while. True when it is gone."""
    def make_writable(function, target, _error):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            function(target)
        except OSError:
            pass
    for attempt in range(attempts):
        if not Path(path).exists():
            return True
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=make_writable)
            else:
                shutil.rmtree(path, onerror=make_writable)
        except OSError:
            pass
        if not Path(path).exists():
            return True
        time.sleep(0.5)
    return not Path(path).exists()


def make_temp_folder(prefix, windows=None):
    """A new, empty temporary folder whose name starts with `prefix`; its path as text.

    On Windows it is made without a mode, so it inherits the temporary folder's rights:
    tempfile.mkdtemp's mode 0o700 becomes, on Python 3.13 and newer, rights for SYSTEM,
    Administrators and the owner only, and PostgreSQL's programs (which run with reduced rights
    on Windows) then cannot open anything in it. The same helper as in tests/support.py.
    """
    if windows is None:
        windows = IS_WINDOWS
    if not windows:
        return tempfile.mkdtemp(prefix=prefix)
    base = os.path.abspath(tempfile.gettempdir())
    for _attempt in range(100):
        path = os.path.join(base, prefix + os.urandom(4).hex())
        try:
            os.mkdir(path)
        except FileExistsError:
            continue
        return path
    raise FileExistsError("no unused temporary folder name starting with %r in %s"
                          % (prefix, base))


def find_json(text):
    """The first JSON object a command printed (text after it is left alone)."""
    start = text.find("{")
    if start < 0:
        raise ValueError("no JSON object in the output")
    return json.JSONDecoder().raw_decode(text, start)[0]


class StepFailed(Exception):
    def __init__(self, message, output="", logs=()):
        super().__init__(message)
        self.message = message
        self.output = output
        self.logs = list(logs)


# ---------------------------------------------------------------------- one engine


class EngineSmoke:
    """The smoke of one engine in its own temporary folder."""

    def __init__(self, engine, *, python=None, keep=False, launcher=True):
        self.engine = engine
        self.python = python or sys.executable
        self.keep = keep
        self.launcher = launcher and engine == "sqlite"
        self.root = Path(make_temp_folder("pcldb smoke %s " % engine))
        self.project = self.root / "project"
        self.user_dir = self.root / "user"
        self.env = child_environment(os.environ, self.project, self.user_dir)
        self.web_port = free_port()
        self.pg_port = free_port() if engine == "postgresql" else None
        self.serve = None
        self.serve_output = self.root / "serve output.txt"
        self.results = []           # (step, ok)
        self.token = None

    # -- reporting --------------------------------------------------------------------------

    def log_files(self, *names):
        var = self.project / "var"
        files = {
            "server": [var / "log" / "server.log"],
            "postgresql": [var / "log" / "postgresql.log", var / "log" / "pg_ctl.log",
                           var / "log" / "initdb.log"],
            "serve": [self.serve_output],
        }
        found = []
        for name in names:
            found += files.get(name, [])
        if "postgresql" in names:
            try:
                server_logs = sorted((var / "pgdata" / "log").iterdir(),
                                     key=lambda path: path.stat().st_mtime)
                found += server_logs[-1:]
            except OSError:
                pass
        return found

    def run_step(self, name, function):
        started = time.monotonic()
        try:
            detail = function()
        except StepFailed as failure:
            self.results.append((name, False))
            say("[%s] FAIL  %s: %s  (%.1f s)" % (self.engine, name, failure.message,
                                                 time.monotonic() - started))
            if failure.output:
                say("    --- the last %d lines of its output ---" % TAIL_LINES)
                for line in tail(failure.output):
                    say("    | " + line)
            for path in failure.logs:
                lines = tail_file(path)
                if lines:
                    say("    --- the last %d lines of %s ---" % (TAIL_LINES, path))
                    for line in lines:
                        say("    | " + line)
            return False
        except Exception as error:      # noqa: BLE001 - a smoke reports, it does not crash
            self.results.append((name, False))
            say("[%s] FAIL  %s: unexpected %s: %s" % (self.engine, name,
                                                      type(error).__name__, error))
            return False
        self.results.append((name, True))
        say("[%s] PASS  %s%s  (%.1f s)" % (self.engine, name, ": " + detail if detail else "",
                                           time.monotonic() - started))
        return True

    # -- running pcldb ----------------------------------------------------------------------

    def pcldb(self, args, *, timeout=600, logs=(), expect=0):
        """Run `python -m pcldb ...` to the end; its output. Raises StepFailed when the exit
        code is not `expect`."""
        try:
            done = subprocess.run([self.python, "-m", "pcldb"] + list(args), env=self.env,
                                  cwd=str(self.root), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=timeout)
        except subprocess.TimeoutExpired as error:
            output = (error.output or b"").decode("utf-8", "replace")
            raise StepFailed("did not finish within %d seconds" % timeout, output,
                             self.log_files(*logs)) from None
        output = done.stdout.decode("utf-8", "replace")
        if done.returncode != expect:
            raise StepFailed("exit code %s (wanted %s)" % (done.returncode, expect), output,
                             self.log_files(*logs))
        return output

    # -- the steps --------------------------------------------------------------------------

    def step_setup(self):
        args = ["setup", "--engine", self.engine, "--demo"]
        if self.pg_port is not None:
            args += ["--port", str(self.pg_port)]
        output = self.pcldb(args, logs=("postgresql",))
        for wanted in ("Setup is done.", "16 made-up sessions"):
            if wanted not in output:
                raise StepFailed("the output does not say %r" % wanted, output)
        if not (self.project / "var" / "admin_token.txt").is_file():
            raise StepFailed("var/admin_token.txt was not written", output)
        return "16 demo sessions"

    def step_settings(self):
        """Give the website a free port in the settings file (doctor checks that port)."""
        path = self.project / "pcldb_config.json"
        settings = json.loads(path.read_text(encoding="utf-8-sig"))
        settings.setdefault("server", {})["port"] = self.web_port
        path.write_text(json.dumps(settings, indent=2) + "\n", encoding="utf-8", newline="\n")
        return "website port %d%s" % (self.web_port, ", database port %d" % self.pg_port
                                      if self.pg_port else "")

    def step_doctor(self):
        output = self.pcldb(["doctor"], logs=("postgresql",))
        result = [line for line in output.splitlines() if line.startswith("Result:")]
        if not result:
            raise StepFailed("doctor printed no Result line", output)
        problems = [line.split(":", 1)[0][len("PROBLEM"):].strip()
                    for line in output.splitlines() if line.startswith("PROBLEM")]
        engine = [line.split(":", 1)[1].strip() for line in output.splitlines()
                  if line.startswith("OK") and line[2:].strip().startswith("engine:")]
        return "%s%s%s" % (engine[0][:90] + "; " if engine else "", result[-1],
                           " (PROBLEM lines: %s)" % ", ".join(problems) if problems else "")

    def step_sql(self):
        output = self.pcldb(["sql", SQL, "--json"])
        try:
            result = find_json(output)
            rows = {row[0]: row[1] for row in result["rows"]}
        except (ValueError, KeyError, TypeError, IndexError) as error:
            raise StepFailed("the output is not the expected JSON (%s)" % error, output)
        if not rows.get("raman-portable") or not rows.get("jeol-jsm-it800-shl"):
            raise StepFailed("the rows lack a demo instrument: %r" % rows, output)
        return "%d rows" % len(rows)

    def step_ask(self):
        output = self.pcldb(["ask", "--no-llm", "--json", QUESTION])
        try:
            result = find_json(output)
        except ValueError as error:
            raise StepFailed("the output is not JSON (%s)" % error, output)
        if result.get("planned_by") != "rules" or not result.get("total"):
            raise StepFailed("expected results planned by the keyword rules, got planned_by=%r "
                             "total=%r" % (result.get("planned_by"), result.get("total")),
                             output)
        return "%d sessions" % result["total"]

    def step_db_stop(self):
        output = self.pcldb(["db", "stop"], logs=("postgresql",))
        return output.strip().splitlines()[-1] if output.strip() else ""

    def step_serve(self):
        flags = 0
        if IS_WINDOWS:
            # Its own process group, so that CTRL_BREAK_EVENT reaches it and nothing else.
            flags = subprocess.CREATE_NEW_PROCESS_GROUP
        with open(self.serve_output, "wb") as output:
            self.serve = subprocess.Popen(
                [self.python, "-m", "pcldb", "serve", "--port", str(self.web_port)],
                env=self.env, cwd=str(self.root), stdin=subprocess.DEVNULL, stdout=output,
                stderr=subprocess.STDOUT, creationflags=flags)
        deadline = time.monotonic() + 180
        found = None
        while time.monotonic() < deadline and found is None:
            for line in self.serve_text().splitlines():
                match = OPEN_LINE.match(line)
                if match:
                    found = match
                    break
            if found is None:
                if self.serve.poll() is not None:
                    raise StepFailed("serve ended with exit code %s before it printed the "
                                     "sign-in link" % self.serve.returncode, "",
                                     self.log_files("serve", "server", "postgresql"))
                time.sleep(0.2)
        if found is None:
            raise StepFailed("serve printed no sign-in link within 180 seconds", "",
                             self.log_files("serve", "server", "postgresql"))
        if int(found.group(2)) != self.web_port:
            raise StepFailed("serve printed port %s, not %d" % (found.group(2), self.web_port),
                             self.serve_text())
        self.base = found.group(1).rstrip("/")
        self.signin_code = found.group(3)
        while time.monotonic() < deadline:
            try:
                status, body = self.http("GET", "/api/v1/health")
                if status == 200 and body.get("ok") is True:
                    return "%s, version %s" % (self.base, body.get("version"))
            except OSError:
                pass
            time.sleep(0.2)
        raise StepFailed("/api/v1/health did not answer", self.serve_text(),
                         self.log_files("server"))

    def serve_text(self):
        try:
            return self.serve_output.read_bytes().decode("utf-8", "replace")
        except OSError:
            return ""

    def http(self, method, path, body=None, token=None):
        """(status, JSON body). No proxy, whatever the environment says."""
        data = None
        request = urllib.request.Request(self.base + path, method=method)
        request.add_header("Accept", "application/json")
        if token:
            request.add_header("Authorization", "Bearer " + token)
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            request.add_header("Content-Type", "application/json")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            with opener.open(request, data=data, timeout=120) as reply:
                return reply.status, json.loads(reply.read().decode("utf-8") or "null")
        except urllib.error.HTTPError as error:
            text = error.read().decode("utf-8", "replace")
            try:
                return error.code, json.loads(text)
            except ValueError:
                return error.code, {"text": text}

    def api(self, method, path, body=None):
        status, answer = self.http(method, path, body, self.token)
        if status != 200:
            raise StepFailed("%s %s answered %d: %s" % (method, path, status,
                                                        json.dumps(answer)[:300]),
                             "", self.log_files("server"))
        return answer

    def step_api(self):
        status, signed_in = self.http("POST", "/api/v1/signin", {"code": self.signin_code})
        if status != 200 or not str(signed_in.get("token", "")).startswith("pcl_"):
            raise StepFailed("the sign-in code was not taken (%d)" % status, "",
                             self.log_files("server"))
        self.token = signed_in["token"]
        status, again = self.http("POST", "/api/v1/signin", {"code": self.signin_code})
        if status == 200:
            raise StepFailed("the sign-in code worked twice")
        me = self.api("GET", "/api/v1/me")
        if me.get("engine", {}).get("name") != self.engine or "sql" not in me.get(
                "capabilities", []):
            raise StepFailed("/me says %s" % json.dumps(me)[:300])
        status_answer = self.api("GET", "/api/v1/status")
        database = status_answer.get("database", {})
        if (database.get("sessions"), database.get("demo")) != (16, True):
            raise StepFailed("/status says sessions=%r demo=%r" % (database.get("sessions"),
                                                                    database.get("demo")))
        asked = self.api("POST", "/api/v1/ask", {"question": QUESTION, "include_simulated": True})
        if not asked.get("total"):
            raise StepFailed("/ask found nothing: %s" % json.dumps(asked)[:300])
        examples = self.api("GET", "/api/v1/sql/examples")
        if examples.get("engine") != self.engine or not examples.get("examples"):
            raise StepFailed("/sql/examples says %s" % json.dumps(examples)[:300])
        first = examples["examples"][0]
        rows = self.api("POST", "/api/v1/sql", {"sql": first["sql"], "max_rows": 50})
        if not rows.get("row_count"):
            raise StepFailed("the example %r gave no rows" % first.get("title"))
        return "signin, /me, /status, /ask (%d), /sql/examples, /sql (%r: %d rows)" % (
            asked["total"], first.get("title"), rows["row_count"])

    def stop_serve_like_ctrl_c(self):
        """What Ctrl+C does: SIGINT; on Windows CTRL_BREAK_EVENT to the process group (a
        child cannot be sent Ctrl+C there), which pcldb serve handles the same way."""
        if IS_WINDOWS:
            self.serve.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            self.serve.send_signal(signal.SIGINT)

    def step_stop_serve(self):
        try:
            self.stop_serve_like_ctrl_c()
        except OSError as error:
            raise StepFailed("the stop signal could not be sent (%s)" % error)
        try:
            code = self.serve.wait(150)
        except subprocess.TimeoutExpired:
            raise StepFailed("serve did not stop within 150 seconds of the signal",
                             self.serve_text(), self.log_files("server")) from None
        output = self.serve_text()
        if code != 0:
            raise StepFailed("serve ended with exit code %s" % code, output,
                             self.log_files("server", "postgresql"))
        wanted = ["Stopping the website."]
        if self.engine == "postgresql":
            wanted.append("Stopped the local PostgreSQL database (pcldb serve had started it).")
        for sentence in wanted:
            if sentence not in output:
                raise StepFailed("serve did not say %r" % sentence, output,
                                 self.log_files("server", "postgresql"))
        if "Traceback" in output:
            raise StepFailed("serve printed a Python traceback", output)
        if self.token and (self.token in output or self.token in "\n".join(
                tail_file(self.project / "var" / "log" / "server.log", 100000))):
            raise StepFailed("the sign-in token was printed or logged")
        return "exit code 0"

    def step_nothing_left(self):
        output = self.pcldb(["db", "status"], logs=("postgresql",))
        problems = []
        if self.serve is not None and self.serve.poll() is None:
            problems.append("pcldb serve still runs")
        if port_answers(self.web_port):
            problems.append("something still answers on the website port %d" % self.web_port)
        if self.engine == "postgresql":
            if "Running: no" not in output:
                problems.append("db status does not say 'Running: no'")
            if port_answers(self.pg_port):
                problems.append("something still answers on the database port %d" % self.pg_port)
            if (self.project / "var" / "pgdata" / "postmaster.pid").exists():
                problems.append("var/pgdata/postmaster.pid is still there")
        if problems:
            raise StepFailed("; ".join(problems), output, self.log_files("postgresql"))
        return "no process, no port"

    def step_launcher(self):
        """The launcher of this system, from a copy of the code in a folder with spaces, with
        its own empty .venv (no psycopg, no globus-sdk), with a JSON argument."""
        copy = self.root / "code copy (launcher)"
        shutil.copytree(CODE_ROOT / "pcldb", copy / "pcldb",
                        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        name = "pcldb.cmd" if IS_WINDOWS else "pcldb.sh"
        shutil.copy2(CODE_ROOT / name, copy / name)
        made = subprocess.run([self.python, "-m", "venv", "--without-pip", str(copy / ".venv")],
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=300)
        if made.returncode != 0:
            raise StepFailed("python -m venv failed", made.stdout.decode("utf-8", "replace"))
        env = dict(self.env)
        env.pop("PYTHONPATH", None)
        outputs = []
        for args in (["--version"],
                     ["search", "query", "--request", LAUNCHER_REQUEST]):
            done = subprocess.run([str(copy / name)] + args, env=env, cwd=str(self.root),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, timeout=300)
            text = done.stdout.decode("utf-8", "replace")
            outputs.append(text)
            if done.returncode != 0:
                raise StepFailed("%s %s: exit code %s" % (name, " ".join(args), done.returncode),
                                 text)
        try:
            result = find_json(outputs[1])
            total = result.get("total", result.get("result", {}).get("total"))
        except (ValueError, AttributeError) as error:
            raise StepFailed("search query did not print JSON (%s)" % error, outputs[1])
        if total != 16:
            raise StepFailed("search query says total=%r, not 16" % (total,), outputs[1])
        return "%s from %r: %s; search query total 16" % (name, copy.name,
                                                          outputs[0].strip()[:40])

    # -- the whole run ----------------------------------------------------------------------

    def cleanup(self):
        """Whatever happened: stop serve, stop the database, remove the folder."""
        if self.serve is not None and self.serve.poll() is None:
            try:
                self.stop_serve_like_ctrl_c()
                self.serve.wait(60)
            except (OSError, subprocess.TimeoutExpired):
                self.serve.kill()
                try:
                    self.serve.wait(30)
                except subprocess.TimeoutExpired:
                    pass
        if self.engine == "postgresql" and (self.project / "var" / "pgdata").exists():
            try:
                self.pcldb(["db", "stop"], timeout=300)
            except StepFailed as failure:
                say("[%s] note: db stop during the clean-up failed: %s"
                    % (self.engine, failure.message))

    def run(self):
        say("[%s] folder: %s" % (self.engine, self.root))
        steps = [
            ("setup --engine %s --demo" % self.engine, self.step_setup),
            ("settings", self.step_settings),
            ("doctor", self.step_doctor),
            ("sql", self.step_sql),
            ("ask --no-llm", self.step_ask),
        ]
        if self.engine == "postgresql":
            steps.append(("db stop (serve must start it)", self.step_db_stop))
        steps += [
            ("serve in the background", self.step_serve),
            ("the API: signin, me, status, ask, sql", self.step_api),
            ("stop serve like Ctrl+C", self.step_stop_serve),
            ("db stop", self.step_db_stop),
            ("nothing left running", self.step_nothing_left),
        ]
        if self.launcher:
            steps.append(("launcher", self.step_launcher))
        try:
            for name, function in steps:
                if not self.run_step(name, function):
                    break
        finally:
            self.cleanup()
            if self.keep:
                say("[%s] kept: %s" % (self.engine, self.root))
            else:
                gone = remove_tree(self.root)
                self.results.append(("remove the folder", gone))
                say("[%s] %s  remove the folder" % (self.engine, "PASS" if gone else "FAIL"))
        return all(ok for _, ok in self.results) and len(self.results) >= len(steps)


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python tools/smoke.py",
        description="End-to-end smoke of pcldb in a temporary folder (see the top of the file).")
    parser.add_argument("--engine", choices=("sqlite", "postgresql", "both"), default="both")
    parser.add_argument("--keep", action="store_true", help="keep the temporary folders")
    parser.add_argument("--no-launcher", dest="launcher", action="store_false",
                        help="leave out the launcher step")
    parser.add_argument("--python", default=None,
                        help="the Python that runs pcldb (default: this one)")
    args = parser.parse_args(argv)
    engines = ["sqlite", "postgresql"] if args.engine == "both" else [args.engine]
    say("pcldb smoke: %s, Python %s on %s, code in %s"
        % (" and ".join(engines), sys.version.split()[0], sys.platform, CODE_ROOT))
    summary = []
    for engine in engines:
        smoke = EngineSmoke(engine, python=args.python, keep=args.keep, launcher=args.launcher)
        ok = smoke.run()
        failed = [name for name, passed in smoke.results if not passed]
        summary.append("%s %s" % (engine, "PASS" if ok else "FAIL (%s)" % ", ".join(failed)))
    say("smoke result: " + "; ".join(summary))
    return 0 if all(part.split()[1] == "PASS" for part in summary) else 1


if __name__ == "__main__":
    sys.exit(main())
