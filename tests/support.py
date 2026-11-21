"""Shared helpers for the tests (specification section 14).

One set of test methods runs on both engines:

    class RegistrationCases:                      # no TestCase here: the methods only
        def test_something(self):
            with self.store.app() as db:
                keys = support.seed_minimal(db)
            ...

    class TestRegistrationSqlite(RegistrationCases, support.SqliteCase, unittest.TestCase):
        pass

    class TestRegistrationPostgres(RegistrationCases, support.PostgresCase, unittest.TestCase):
        pass

Every test METHOD gets, on either engine: `self.project` (a TempProject: a fresh temporary
project folder with a space in its path), `self.settings`, and `self.store`, initialised and
EMPTY. Settings a test class needs are given as `settings_changes = {"sql.max_rows": 5}`.

PostgreSQL: one temporary cluster per test process (made on first use in a temporary folder on
a free port; stopped and deleted when the process ends, also after failures), one fresh database
per test class, emptied before every test method. The classes are skipped with a reason when
PostgreSQL's programs or psycopg are missing; with PCLDB_REQUIRE_POSTGRES=1 that is a failure.
Set PCLDB_PG_BIN to choose the PostgreSQL version the tests run against.

Nothing here touches ~/.pcldb, the project's real var/ folder, or the ports 8787 and 15439.
"""

from __future__ import annotations

import atexit
import os
import secrets
import shutil
import signal
import stat
import sys
import tempfile
import threading
import unittest
import uuid
from pathlib import Path
from urllib.parse import urlencode

from pcldb import util
from pcldb.config import ARC_PROJECTS_COLLECTION, Settings
from pcldb.store import open_store, schema_def
from pcldb.store.base import prepare_sql

free_port = util.free_port

REQUIRE_POSTGRES = os.environ.get("PCLDB_REQUIRE_POSTGRES") == "1"


# ---------------------------------------------------------------------- temporary folders


def remove_tree(path):
    """Delete a folder with everything in it, also read-only files (Windows keeps those)."""
    def retry_writable(function, target, _error):
        try:
            os.chmod(target, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
            function(target)
        except OSError:
            pass
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry_writable)
    else:
        shutil.rmtree(path, onerror=retry_writable)


def make_temp_folder(prefix, windows=None):
    """A new, empty temporary folder whose name starts with `prefix`; its path as text.

    On Windows the folder is made without a mode (os.mkdir(path)), so it inherits the rights of
    the temporary folder. tempfile.mkdtemp gives it mode 0o700, which Python 3.13 and newer turn
    into rights for SYSTEM, Administrators and the owner only; PostgreSQL's programs run with
    reduced rights on Windows and then cannot open anything in it (initdb: Permission denied).
    Use this for every folder that may hold a PostgreSQL cluster. `windows` is for the tests.
    """
    if windows is None:
        windows = os.name == "nt"
    if not windows:
        return tempfile.mkdtemp(prefix=prefix)
    base = os.path.abspath(tempfile.gettempdir())
    for _attempt in range(100):
        path = os.path.join(base, prefix + secrets.token_hex(4))
        try:
            os.mkdir(path)
        except FileExistsError:
            continue
        return path
    raise FileExistsError("no unused temporary folder name starting with %r in %s"
                          % (prefix, base))


_open_projects = set()


def _remove_forgotten_projects():
    """At exit: delete the folders of TempProjects that nobody cleaned up."""
    for path in list(_open_projects):
        try:
            remove_tree(path)
        except OSError:
            pass
        _open_projects.discard(path)


atexit.register(_remove_forgotten_projects)


class TempProject:
    """A temporary project folder and its Settings. The folder name holds a space on purpose.

        with TempProject("sqlite", {"sql.max_rows": 5}) as project:
            project.path, project.settings

    Call cleanup() (or use `with`, or self.addCleanup(project.cleanup)). A folder that is
    forgotten is still deleted when the test process ends.
    """

    def __init__(self, engine="sqlite", changes=None, prefix="pcldb test "):
        self.path = Path(make_temp_folder(prefix))
        _open_projects.add(self.path)
        try:
            self.settings = Settings.for_test(self.path, engine=engine, changes=changes)
        except BaseException:
            self.cleanup()
            raise

    def cleanup(self):
        if self.path.exists():
            remove_tree(self.path)
        _open_projects.discard(self.path)

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self.cleanup()


# ---------------------------------------------------------------------- the shared PostgreSQL

_problem_checked = False
_problem = None
_shared = None
_shared_lock = threading.Lock()


def postgres_problem():
    """None when the PostgreSQL tests can run here, else the reason why not (one sentence)."""
    global _problem_checked, _problem
    if not _problem_checked:
        try:
            import psycopg  # noqa: F401
        except ImportError:
            _problem = "the Python package psycopg is not installed"
        else:
            from pcldb.store import pgcluster
            wanted = (os.environ.get("PCLDB_PG_BIN") or "").strip()
            found = pgcluster.find_pg_bins()
            if wanted and not (found and pgcluster._same_folder(found[0].bin_dir, wanted)):
                # Never test another version than the one that was asked for.
                _problem = ("PCLDB_PG_BIN names %s, but PostgreSQL's programs (initdb, pg_ctl, "
                            "postgres; version 16 or newer) are not there" % wanted)
            elif not found:
                _problem = ("PostgreSQL's programs (initdb, pg_ctl, postgres; version 16 or "
                            "newer) were not found; set PCLDB_PG_BIN to their folder")
        _problem_checked = True
    return _problem


def require_postgres():
    """Skip the calling test (class) when PostgreSQL cannot be used here; with
    PCLDB_REQUIRE_POSTGRES=1 fail instead."""
    problem = postgres_problem()
    if problem is None:
        return
    if REQUIRE_POSTGRES:
        raise AssertionError("PCLDB_REQUIRE_POSTGRES=1, but " + problem)
    raise unittest.SkipTest("PostgreSQL tests skipped: " + problem)


class _SharedCluster:
    """The one temporary PostgreSQL cluster of this test process."""

    def __init__(self):
        from pcldb.store import pgcluster
        from pcldb.store.postgres_engine import PostgresStore
        self.root = Path(make_temp_folder("pcldb pg "))
        self.closed = False
        try:
            self.settings = Settings.for_test(self.root, engine="postgresql")
            self.cluster = pgcluster.LocalCluster(self.settings, test_mode=True)
            self.cluster.init()
            self.cluster.start()
            self.port = self.settings.database.postgresql.port
            self.passwords = pgcluster.read_local_secrets(self.settings)
            # The roles pcl_app and pcl_reader, made once, exactly as local mode makes them.
            PostgresStore(self.settings)._ensure_local_roles_and_database()
        except BaseException:
            self.close()
            raise

    def conninfo(self, role, database):
        from psycopg.conninfo import make_conninfo
        from pcldb.store.postgres_engine import ROLES
        return make_conninfo("", host="127.0.0.1", port=self.port, dbname=database,
                             user=ROLES[role], password=self.passwords[role])

    def external_changes(self, database):
        """The settings changes that point a project at one database of this cluster."""
        return {
            "database.postgresql.mode": "external",
            "database.postgresql.external.owner": self.conninfo("owner", database),
            "database.postgresql.external.app": self.conninfo("app", database),
            "database.postgresql.external.reader": self.conninfo("reader", database),
        }

    def _owner(self, database="postgres"):
        import psycopg
        return psycopg.connect(self.conninfo("owner", database), autocommit=True)

    def new_database(self):
        from psycopg import sql
        name = "t_" + secrets.token_hex(6)
        connection = self._owner()
        try:
            connection.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'")
                               .format(sql.Identifier(name)))
        finally:
            connection.close()
        return name

    def drop_database(self, name):
        from psycopg import sql
        if self.closed:
            return
        connection = self._owner()
        try:
            connection.execute(sql.SQL("DROP DATABASE IF EXISTS {} WITH (FORCE)")
                               .format(sql.Identifier(name)))
        finally:
            connection.close()

    def make_empty(self, store):
        """Leave `store` initialised and without rows (fast when it is initialised already)."""
        import psycopg
        wanted = str(schema_def.SCHEMA_VERSION)
        connection = psycopg.connect(store.conninfo("owner"))
        try:
            versions = store._versions(connection)
            if all(found == wanted for found in versions.values()):
                connection.execute("TRUNCATE " + ", ".join(
                    table.qualified for table in schema_def.TABLES))
                for schema in ("pcl", "app"):
                    connection.execute(
                        "INSERT INTO " + schema + ".meta (key, value) VALUES "
                        "('schema_version', %s)", [wanted])
                connection.commit()
                return
        finally:
            connection.close()
        store.reset()

    def close(self):
        if self.closed:
            return
        self.closed = True
        try:
            cluster = getattr(self, "cluster", None)
            if cluster is not None:
                cluster.stop()
        except Exception as error:      # still delete the folder; say what happened
            sys.stderr.write("tests/support.py: the test cluster did not stop cleanly: %s\n"
                             % error)
        finally:
            shutil.rmtree(self.root, ignore_errors=True)


def _on_terminate(number, _frame):
    """A run that is told to end (SIGTERM, for example by a time limit) still stops its cluster
    and deletes its folders, then ends the way it was told to. (Raising an exception here would
    not do: unittest would count it as one failed test and go on.)"""
    if _shared is not None:
        _shared.close()
    _remove_forgotten_projects()
    signal.signal(number, signal.SIG_DFL)
    os.kill(os.getpid(), number)


def shared_cluster():
    """The process-wide temporary cluster, started on first use. Skips or fails the calling
    test when PostgreSQL cannot be used (see require_postgres)."""
    global _shared
    require_postgres()
    with _shared_lock:
        if _shared is None:
            _shared = _SharedCluster()
            atexit.register(_shared.close)
            try:
                if (threading.current_thread() is threading.main_thread()
                        and signal.getsignal(signal.SIGTERM) is signal.SIG_DFL):
                    signal.signal(signal.SIGTERM, _on_terminate)
            except (ValueError, OSError):
                pass
        return _shared


# ---------------------------------------------------------------------- the two mixins


class StoreCase:
    """What SqliteCase and PostgresCase share. Put it BEFORE unittest.TestCase in the bases."""

    engine = None
    settings_changes = {}       # dotted settings keys for this test class

    def outside_rows(self, sql, params=()):
        """Rows as a second, read-only connection sees them now: committed data only.

        Write the SQL as for Db ({pcl}, {app}, ?). The rows are tuples of the driver's RAW
        values (0/1 or True/False, texts or datetimes): compare counts and plain text.
        """
        text, _ = prepare_sql(sql, self.engine)
        if self.engine == "sqlite":
            connection = self.store._open("ro")
            try:
                return connection.execute(text, list(params)).fetchall()
            finally:
                connection.close()
        import psycopg
        connection = psycopg.connect(self.store.conninfo("owner"), autocommit=True)
        try:
            return connection.execute(text, list(params)).fetchall()
        finally:
            connection.close()


class SqliteCase(StoreCase):
    engine = "sqlite"

    def setUp(self):
        super().setUp()
        self.project = TempProject("sqlite", dict(self.settings_changes))
        self.addCleanup(self.project.cleanup)
        self.settings = self.project.settings
        self.store = open_store(self.settings)
        self.store.initialize()


class PostgresCase(StoreCase):
    engine = "postgresql"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._cluster = shared_cluster()
        cls._database = cls._cluster.new_database()
        cls.addClassCleanup(cls._cluster.drop_database, cls._database)

    def setUp(self):
        super().setUp()
        changes = self._cluster.external_changes(self._database)
        changes.update(self.settings_changes)
        self.project = TempProject("postgresql", changes)
        self.addCleanup(self.project.cleanup)
        self.settings = self.project.settings
        self.store = open_store(self.settings)
        self._cluster.make_empty(self.store)


# ---------------------------------------------------------------------- rows for tests

REAL_PATH = "raw/raman-portable/1d_y_scan_20261115_110940_537"
SIMULATED_PATH = "raw/raman-portable/simulated/1d_y_scan_20261116_225015_281"
RUN_PATH = "results/raman-portable/1d_y_scan_20261115_110940_537__20261119T023150Z_classify"
REVIEW_RUN_PATH = "results/raman-portable/1d_y_scan_20261115_110940_537__20261119T023151Z_review"
MODEL_NAME = "demo_model_20260922"
MODEL_SHA256 = "ab" * 32


def session_row(path, simulated=False, **changes):
    """A complete, valid row for pcl.sessions (every column that may not be empty is filled).
    The addresses follow the rules of specification section 6.1 for the default storage
    settings. Override any column with keyword arguments."""
    folder = path.rsplit("/", 1)[-1]
    parts = path.split("/")
    data_path = "/pcl_photonics/" + path
    row = {
        "path": path,
        "instrument": parts[1] if len(parts) > 2 and parts[0] == "raw" else "raman-portable",
        "area": "simulated" if simulated else "real",
        "folder": folder,
        "session_name": folder,
        "session_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "pcldb-test-session:" + path)),
        "is_copy": False,
        "started_utc": "2026-11-15T16:09:40.537Z",
        "ended_utc": "2026-11-15T16:10:05.120Z",
        "simulated": simulated,
        "sample_ids": ["S-100"],
        "operator": "demo-operator-a",
        "location": "demo bench",
        "method": "1d-y",
        "items": 3,
        "items_planned": 3,
        "completion": "complete",
        "notes": "test notes",
        "tags": [],
        "format": "raman-session/1 folder",
        "uploaded_utc": "2026-11-19T02:31:49.530Z",
        "upload_id": "u" + util.sha256_hex(path.encode("utf-8"))[:12],
        "has_card": True,
        "card_json": {"schema": "pcl-session/1", "session": folder, "simulated": simulated},
        "corrections_json": [],
        "corrected": False,
        "withdrawn": False,
        "bytes": 0,
        "n_files": 0,
        "sums_sha256": None,
        "verified_utc": None,
        "verify_ok": None,
        "upload_json": None,
        "collection_id": ARC_PROJECTS_COLLECTION,
        "data_path": data_path,
        "storage_path": "/projects/pcl_photonics/" + path,
        "globus_url": "globus://%s%s/" % (ARC_PROJECTS_COLLECTION, data_path),
        "web_url": "https://app.globus.org/file-manager?" + urlencode(
            {"origin_id": ARC_PROJECTS_COLLECTION, "origin_path": data_path + "/"}),
        "subject": "urn:pcl:dataset:" + str(uuid.uuid5(
            uuid.NAMESPACE_URL, ARC_PROJECTS_COLLECTION + ":" + data_path)),
        "registered_utc": "2026-11-19T02:35:00.000Z",
        "registered_by": "demo-operator-a",
        "updated_utc": "2026-11-19T02:35:00.000Z",
        "source": "upload",
        "doc_sha256": None,
    }
    unknown = set(changes) - set(row)
    if unknown:
        raise ValueError("pcl.sessions has no column %s" % ", ".join(sorted(unknown)))
    row.update(changes)
    return row


def seed_minimal(db):
    """Insert a small, complete set of rows and return their keys.

    Two sessions (one real, one simulated) with files; a current model; one classify run of the
    real session with three points; one review (point 2 relabelled); the rollup of both
    sessions; one user (an admin) and one token. Returned:

        real_path, simulated_path, session_paths, run_path, review_run_path, model_name,
        user_id, user_name, token_id, token   (the token's TEXT; only its SHA-256 is stored)
    """
    files = {
        REAL_PATH: [("session.json", 812, "11" * 32), ("point_0001.json", 40210, "22" * 32),
                    ("point_0002.json", 40188, "33" * 32), ("point_0003.json", 40302, "44" * 32),
                    ("SHA256SUMS", 331, "55" * 32)],
        SIMULATED_PATH: [("session.json", 799, "66" * 32), ("point_0001.json", 39876, "77" * 32),
                         ("SHA256SUMS", 166, "88" * 32)],
    }
    for path, simulated in ((REAL_PATH, False), (SIMULATED_PATH, True)):
        listed = files[path]
        db.insert("pcl.sessions", session_row(
            path, simulated,
            bytes=sum(size for _name, size, _digest in listed), n_files=len(listed),
            sums_sha256=listed[-1][2],
            started_utc="2026-11-15T16:09:40.537Z" if not simulated else "2026-11-17T03:50:15.281Z",
            ended_utc="2026-11-15T16:10:05.120Z" if not simulated else "2026-11-17T03:50:40.043Z",
            sample_ids=["S-100"] if not simulated else ["S-200", "S-201"],
            operator="demo-operator-a" if not simulated else "demo-operator-b",
            tags=[] if not simulated else ["demo"],
            items=3 if not simulated else 1, items_planned=3 if not simulated else 1,
            verified_utc="2026-11-19T02:31:51.105Z" if not simulated else None,
            verify_ok=True if not simulated else None))
        db.insert_many("pcl.files", [
            {"session_path": path, "rel_path": name, "bytes": size, "sha256": digest,
             "listed": True} for name, size, digest in listed])

    db.upsert("pcl.meta", {"key": "current_model", "value": MODEL_NAME}, key=("key",))
    db.insert("pcl.models", {
        "name": MODEL_NAME, "path": "models/" + MODEL_NAME, "sha256": MODEL_SHA256,
        "classes_json": ["r6g", "methylene_blue", "adenine"], "threshold": 0.8,
        "files_json": [{"name": "model.json", "bytes": 512, "sha256": "99" * 32}],
        "is_current": True, "model_json": {"schema": "pcl-model/1", "name": MODEL_NAME}})
    db.insert("pcl.runs", {
        "path": RUN_PATH, "instrument": "raman-portable", "area": "real",
        "folder": RUN_PATH.rsplit("/", 1)[-1], "kind": "classify",
        "made_utc": "2026-11-19T02:31:50.210Z", "session_path": REAL_PATH,
        "stated_session_path": REAL_PATH, "session_id": None, "sums_sha256": "55" * 32,
        "derived_from": [], "tool_name": "pcldb-tests", "tool_version": "0", "model_name": MODEL_NAME,
        "model_sha256": MODEL_SHA256, "runner": "hand", "run_by": "demo-operator-a",
        "run_json": {"schema": "pcl-run/1", "kind": "classify"}, "valid": True})
    db.insert("pcl.runs", {
        "path": REVIEW_RUN_PATH, "instrument": "raman-portable", "area": "real",
        "folder": REVIEW_RUN_PATH.rsplit("/", 1)[-1], "kind": "review",
        "made_utc": "2026-11-19T02:31:51.020Z", "session_path": REAL_PATH,
        "stated_session_path": REAL_PATH, "derived_from": [RUN_PATH], "runner": "hand",
        "run_by": "demo-reviewer", "run_json": {"schema": "pcl-run/1", "kind": "review"},
        "valid": True})
    db.insert_many("pcl.points", [
        {"run_path": RUN_PATH, "point_file": "point_0001.json", "point_id": 1,
         "capture_id": "c-1", "label": "adenine", "top_class": "adenine", "confidence": 0.97,
         "outcome": "confident", "needs_review": False, "threshold": 0.8, "reason": None,
         "raw_sha256": "22" * 32, "scores_json": {"adenine": 0.97, "r6g": 0.02}},
        {"run_path": RUN_PATH, "point_file": "point_0002.json", "point_id": 2,
         "capture_id": "c-2", "label": "UNKNOWN", "top_class": "r6g", "confidence": 0.55,
         "outcome": "low_confidence", "needs_review": True, "threshold": 0.8,
         "reason": "Low confidence", "raw_sha256": "33" * 32,
         "scores_json": {"adenine": 0.4, "r6g": 0.55}},
        {"run_path": RUN_PATH, "point_file": "point_0003.json", "point_id": 3,
         "capture_id": "c-3", "label": "UNKNOWN", "top_class": "methylene_blue",
         "confidence": 0.61, "outcome": "low_confidence", "needs_review": True, "threshold": 0.8,
         "reason": "Low confidence", "raw_sha256": "44" * 32,
         "scores_json": {"methylene_blue": 0.61, "r6g": 0.3}},
    ])
    db.insert("pcl.reviews", {
        "run_path": REVIEW_RUN_PATH, "row_no": 1, "reviewed_run": RUN_PATH, "point_id": 2,
        "capture_id": "c-2", "status": "relabelled", "label": "r6g", "reviewer": "demo-reviewer",
        "time_utc": "2026-11-19T02:31:51.020Z", "comment": "test review"})
    db.insert("pcl.session_rollup", {
        "session_path": REAL_PATH, "labels": ["UNKNOWN", "adenine", "r6g"],
        "label_counts": [{"label": "UNKNOWN", "count": 1}, {"label": "adenine", "count": 1},
                         {"label": "r6g", "count": 1}],
        "models": [MODEL_NAME], "problems": [], "review_state": "pending", "n_needs_review": 1,
        "last_run_utc": "2026-11-19T02:31:51.020Z"})
    db.insert("pcl.session_rollup", {
        "session_path": SIMULATED_PATH, "labels": [], "label_counts": [], "models": [],
        "problems": [], "review_state": "none", "n_needs_review": 0, "last_run_utc": None})

    user_id, token_id = util.new_id(), util.new_id()
    token = "pcl_" + secrets.token_urlsafe(32)
    db.insert("app.users", {
        "user_id": user_id, "name": "demo-operator-a", "display_name": "Demo operator A",
        "role": "admin", "disabled": False, "created_utc": "2026-11-19T02:30:00.000Z"})
    db.insert("app.tokens", {
        "token_id": token_id, "user_id": user_id,
        "token_sha256": util.sha256_hex(token.encode("utf-8")), "label": "tests",
        "created_utc": "2026-11-19T02:30:00.000Z", "expires_utc": None, "revoked_utc": None,
        "last_used_utc": None})
    return {
        "real_path": REAL_PATH,
        "simulated_path": SIMULATED_PATH,
        "session_paths": [REAL_PATH, SIMULATED_PATH],
        "run_path": RUN_PATH,
        "review_run_path": REVIEW_RUN_PATH,
        "model_name": MODEL_NAME,
        "user_id": user_id,
        "user_name": "demo-operator-a",
        "token_id": token_id,
        "token": token,
    }
