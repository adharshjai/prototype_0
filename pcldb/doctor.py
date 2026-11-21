"""`pcldb doctor`: one line per check, OK or PROBLEM, and for a problem what to do about it
(specification section 13).

Checks, in order: Python; the packages; the settings file; the engine (the local PostgreSQL
database is started for the checks when it is stopped, and stopped again afterwards); the
store is set up for this version; the expert-mode sandbox (one harmless query and five hostile
statements, straight through the reader; on PostgreSQL also the role check); what is in the
database; the search backend and its queue; the ARC assistant (key present, service
reachable); the website's port.

The exit code is 0 when the essentials are fine (Python, the engine, the store, the sandbox):
a missing ARC key or an unsent search queue is a PROBLEM line, but pcldb still works without
them. Nothing secret is printed: never a key, a token or a password.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.request

from . import __version__, util
from .errors import PclError

MIN_PYTHON = (3, 11)

KEY_SENTENCE = (
    "The ARC LLM key is the key of the person running pcldb on this computer (ARC logs every "
    "request under the key owner's name and a key must not be shared), and the ARC LLM service "
    "only answers on the VT campus network or VPN; a shared server needs ARC's answer about "
    "keys first.")


class Report:
    """The lines of one doctor run."""

    def __init__(self, out=None):
        self.out = out
        self.lines = []
        self.essential_problems = 0
        self.problems = 0

    def _write(self, text):
        text = str(text).encode("ascii", "backslashreplace").decode("ascii")
        text = "".join(char if " " <= char <= "~" else " " for char in text)
        self.lines.append(text)
        if self.out is not None:
            self.out.write(text + "\n")
            self.out.flush()

    def ok(self, what, detail):
        self._write("OK       %s: %s" % (what, detail))

    def problem(self, what, detail, fix=None, essential=False):
        self.problems += 1
        if essential:
            self.essential_problems += 1
        text = "PROBLEM  %s: %s" % (what, detail.rstrip())
        if fix:
            text += " Fix: " + fix
        self._write(text)

    def note(self, text):
        self._write("         " + text)


# ---------------------------------------------------------------------- the checks


def _check_python(report):
    found = ".".join(str(part) for part in sys.version_info[:3])
    if sys.version_info[:2] >= MIN_PYTHON:
        report.ok("Python", "%s (%s)" % (found, sys.executable))
    else:
        report.problem("Python", "this is Python %s; pcldb needs 3.11 or newer." % found,
                       "run install.py with a newer Python.", essential=True)


def _check_packages(report, settings):
    engine = settings.database.engine
    has_psycopg = importlib.util.find_spec("psycopg") is not None
    if has_psycopg:
        import psycopg
        report.ok("psycopg", "installed (%s)" % getattr(psycopg, "__version__", "version unknown"))
    elif engine == "postgresql":
        report.problem("psycopg", "the PostgreSQL engine is chosen, but the Python package "
                       "psycopg is not installed.", "%s (or: pip install -r "
                       "requirements.txt)" % util.INSTALL_PY, essential=True)
    else:
        report.ok("psycopg", "not installed (only needed for the PostgreSQL engine)")
    has_sdk = importlib.util.find_spec("globus_sdk") is not None
    if has_sdk:
        report.ok("globus-sdk", "installed (needed only for the live Globus Search)")
    elif settings.search.backend == "globus":
        report.problem("globus-sdk", "search.backend is \"globus\", but the package globus-sdk "
                       "is not installed, so searches use this computer's copy of the index.",
                       "%s --globus" % util.INSTALL_PY)
    else:
        report.ok("globus-sdk", "not installed (only needed for the live Globus Search)")


def _check_settings(report, settings):
    if settings.config_path.exists():
        report.ok("settings", "%s, engine %s" % (settings.config_path,
                                                 settings.database.engine))
        return True
    report.problem("settings", "this project folder (%s) has no settings file yet."
                   % settings.project_dir, "run: pcldb setup", essential=True)
    return False


def _check_engine(report, settings):
    """Returns (usable, the cluster this check started or None)."""
    engine = settings.database.engine
    if engine == "sqlite":
        import sqlite3
        from .store import sqlite_engine
        try:
            sqlite_engine.require_sqlite_version()
        except PclError as error:
            report.problem("engine", error.message, essential=True)
            return False, None
        report.ok("engine", "SQLite %s, files in %s" % (sqlite3.sqlite_version,
                                                         settings.sqlite_dir))
        return True, None
    if importlib.util.find_spec("psycopg") is None:
        report.problem("engine", "PostgreSQL cannot be used without psycopg.",
                       util.INSTALL_PY, essential=True)
        return False, None
    if settings.database.postgresql.mode != "local":
        from .store import open_store
        described = open_store(settings).describe()
        if described["version"] == "unknown":
            report.problem("engine", "the PostgreSQL server at %s does not answer."
                           % described["where"], "start that server or correct the "
                           "connection settings (PCLDB_PG_*_DSN).", essential=True)
            return False, None
        report.ok("engine", "PostgreSQL %s at %s (a server pcldb does not start or stop)"
                  % (described["version"], described["where"]))
        return True, None
    from .store import pgcluster
    try:
        found = pgcluster.choose_pg_bin(settings)
    except PclError as error:
        report.problem("engine", error.message, essential=True)
        return False, None
    cluster = pgcluster.LocalCluster(settings)
    if not cluster.is_initialized():
        report.problem("engine", "PostgreSQL %s was found, but the local database has not been "
                       "made yet." % found.version, "run: pcldb setup", essential=True)
        return False, None
    try:
        started = cluster.ensure_running()
    except PclError as error:
        report.problem("engine", "the local PostgreSQL database could not be started: %s"
                       % error.message, essential=True)
        return False, None
    if started:
        report.ok("engine", "PostgreSQL %s in %s, port %d (it was stopped; doctor started it "
                  "for these checks and stops it again at the end)"
                  % (found.version, cluster.data_dir, cluster.port))
        return True, cluster
    report.ok("engine", "PostgreSQL %s in %s, running on 127.0.0.1 port %d"
              % (found.version, cluster.data_dir, cluster.port))
    return True, None


def _check_store(report, store):
    try:
        ready = store.is_initialized()
    except PclError as error:
        report.problem("database", error.message, essential=True)
        return False
    if not ready:
        try:
            with store.app():
                pass
        except PclError as error:
            report.problem("database", error.message, essential=True)
            return False
        report.problem("database", "it is not set up.", "run: pcldb setup", essential=True)
        return False
    report.ok("database", "set up for this version of pcldb (%s)"
              % store.describe().get("where"))
    return True


def _check_sandbox(report, store):
    from . import sqlrun
    try:
        results = sqlrun.sandbox_self_test(store)
    except PclError as error:
        report.problem("expert-mode sandbox", "the test queries could not run: %s"
                       % error.message, essential=True)
        return
    harmless = [item for item in results if item["must_run"]]
    hostile = [item for item in results if not item["must_run"]]
    wrong = [item for item in results if not item["ok"]]
    if wrong:
        report.problem("expert-mode sandbox", "%d of %d test statements did not do what they "
                       "must: %s" % (len(wrong), len(results), "; ".join(
                           "%s -> %s" % (item["sql"], item["outcome"]) for item in wrong)),
                       "do not offer expert SQL until this is fixed; run pcldb db reset --yes "
                       "on a test copy or report it.", essential=True)
    else:
        report.ok("expert-mode sandbox", "%d harmless query ran and all %d hostile statements "
                  "were stopped (%s)" % (len(harmless), len(hostile), "; ".join(
                      item["sql"] for item in hostile)))
    if store.engine != "postgresql":
        return
    from .store import pg_hardening
    try:
        state = pg_hardening.check(store)
    except PclError as error:
        report.problem("expert-mode role", "the role check could not run: %s" % error.message,
                       essential=True)
        return
    if state["problems"]:
        report.problem("expert-mode role", "the PostgreSQL role for expert SQL could read "
                       "private data or change something: %s." % "; ".join(state["problems"]),
                       "run pcldb setup again as a superuser owner (it applies the limits), "
                       "and do not offer expert SQL until this line says OK.", essential=True)
        return
    report.ok("expert-mode role", "pcl_reader reads only the catalogue")
    if state["notes"]:
        from .server import EXPERT_NOTE_WEAKER
        report.note(EXPERT_NOTE_WEAKER)
        for text in state["notes"]:
            report.note("- " + text)


def _check_contents(report, store):
    from . import auth, demo
    with store.app() as db:
        counts = {name: int(db.scalar("SELECT COUNT(*) FROM {pcl}.%s" % name) or 0)
                  for name in ("sessions", "files", "runs", "points")}
        is_demo = demo.is_loaded(db)
        users = auth.list_users(db)
    admins = [user for user in users if user.role == "admin" and not user.disabled]
    report.ok("records", "%d sessions, %d files, %d runs, %d points%s"
              % (counts["sessions"], counts["files"], counts["runs"], counts["points"],
                 " (demo data: remove it with pcldb demo clear)" if is_demo else ""))
    if not users:
        report.problem("users", "there is no user, so nobody can sign in.",
                       "run: pcldb setup (it makes the admin)")
    elif not admins:
        report.problem("users", "%d user(s), but no admin that is switched on." % len(users),
                       "pcldb user enable admin")
    else:
        report.ok("users", "%d user(s), %d admin(s)" % (len(users), len(admins)))


def _check_search(report, settings, store):
    from . import search_sync
    with store.app() as db:
        counts = search_sync.status(db)
    queue = ", ".join("%d %s" % (counts[state], state.replace("_", " "))
                      for state in search_sync.STATES)
    backend = settings.search.backend
    if backend == "local":
        report.ok("search", "answered by this computer's copy of the index (entries: %s)"
                  % queue)
        return
    from . import search_globus
    problems = []
    if not settings.search.index_id:
        problems.append(("no Globus Search index is set.", "pcldb search create-index"))
    if not search_globus.shared(settings).signed_in():
        problems.append(("nobody is signed in to Globus on this computer.",
                         "pcldb search login"))
    if counts["failed"]:
        problems.append(("%d entries could not be sent." % counts["failed"],
                         "see pcldb search status, then pcldb search push"))
    if problems:
        for detail, fix in problems:
            report.problem("search", "backend globus: " + detail, fix)
        return
    report.ok("search", "Globus Search index %s (entries: %s)" % (settings.search.index_id,
                                                                    queue))


def _check_assistant(report, settings):
    from . import llm
    status = llm.assistant_status(settings)
    state = status["state"]
    where = "model %s at %s" % (status["model"], settings.llm.base_url)
    if state == "ready":
        report.ok("ARC assistant", "%s: %s" % (where, status["note"]))
    elif state == "disabled":
        report.ok("ARC assistant", "switched off in the settings (llm.enabled); questions are "
                  "answered by the keyword rules")
    elif state == "no_key":
        path = settings.llm_api_key_path
        with_txt = path.with_name(path.name + ".txt")
        if with_txt.is_file():      # Notepad adds .txt, and Explorer hides it
            report.problem("ARC assistant", "no ARC LLM key is saved under the name pcldb reads, "
                           "but %s is there (Notepad adds .txt to the name)." % with_txt.name,
                           "rename it to %s (without .txt)" % path)
        else:
            report.problem("ARC assistant", "no ARC LLM key is saved, so questions are answered "
                           "by the keyword rules.", "make a key at llm.arc.vt.edu and save it as "
                           "the one line of %s" % path)
    else:
        report.problem("ARC assistant", "%s: %s" % (where, status["note"]),
                       "until it answers, questions are answered by the keyword rules.")
    report.note(KEY_SENTENCE)


def _pcldb_answers(port):
    request = urllib.request.Request("http://127.0.0.1:%d/api/v1/health" % port)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=2) as reply:
            return json.loads(reply.read().decode("utf-8")).get("ok") is True
    except Exception:
        return False


def _check_port(report, settings):
    port = settings.server.port
    host = settings.server.host
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0", "::", "::1", "localhost") else host
    if not util.port_in_use(port, probe_host):
        report.ok("website port", "%d is free for pcldb serve" % port)
    elif _pcldb_answers(port):
        report.ok("website port", "pcldb serve is running on port %d already" % port)
    else:
        report.problem("website port", "port %d is in use by another program." % port,
                       "stop it, or start the website on another port: pcldb serve --port %d "
                       "(or change server.port in pcldb_config.json)" % (port + 1))


# ---------------------------------------------------------------------- running them


def run_checks(settings, out=None):
    """Run every check; returns the Report (its lines, and how many problems were found)."""
    from .store import open_store
    report = Report(out)
    report._write("pcldb %s doctor, project folder %s" % (__version__, settings.project_dir))
    _check_python(report)
    _check_packages(report, settings)
    has_settings = _check_settings(report, settings)
    started = None
    try:
        usable, started = _check_engine(report, settings)
        store = open_store(settings) if usable and has_settings else None
        if store is not None and _check_store(report, store):
            for check in (lambda: _check_sandbox(report, store),
                          lambda: _check_contents(report, store),
                          lambda: _check_search(report, settings, store)):
                try:
                    check()
                except PclError as error:
                    report.problem("database", error.message, essential=True)
        elif usable and not has_settings:
            report._write("         (the database checks were left out: there is no settings "
                          "file)")
        _check_assistant(report, settings)
        _check_port(report, settings)
    finally:
        if started is not None:
            try:
                started.stop()
                report._write("         Stopped the local PostgreSQL database again.")
            except PclError as error:
                report.problem("engine", "the local PostgreSQL database could not be stopped "
                               "again: %s" % error.message, "pcldb db stop")
    if report.essential_problems:
        report._write("Result: %d problem(s); %d of them stop pcldb from working."
                      % (report.problems, report.essential_problems))
    elif report.problems:
        report._write("Result: the essentials are fine; %d problem(s) above limit what pcldb "
                      "can do." % report.problems)
    else:
        report._write("Result: everything is fine.")
    return report


def cmd_doctor(args, settings):
    """`pcldb doctor`: exit 0 when the essentials are fine, else 1."""
    report = run_checks(settings, out=sys.stdout)
    return 1 if report.essential_problems else 0
