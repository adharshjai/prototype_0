"""The SQLite engine: two files and nothing to install (specification section 4.4).

- `pcl_app.sqlite` is private: users, token hashes, audit rows, the Search queue.
- `pcl_catalog.sqlite` is the catalogue. It holds nothing secret, so it can be copied and opened
  in any SQLite tool; its views name only objects of that file.

An `app()` connection opens the app file as `main` and attaches the catalogue as `pcl`. Journal
mode is DELETE (not WAL), so a commit that touches both files is atomic and a read-only reader
needs no side files. `BEGIN IMMEDIATE` takes the write lock on both files at once: a second
writer waits (up to `sql.timeout_seconds` + 5 seconds, at least 5; see busy_milliseconds)
instead of failing in the middle of its work.

Values are stored as section 4.2 says (booleans 0/1, times as the fixed-width UTC text, JSON as
canonical text, lists as JSON arrays) and results are normalised by column NAME with
schema_def.COLUMN_KINDS.

Two things are pinned so that every SQLite build answers like PostgreSQL does for our columns
(which are COLLATE "C" there): LIKE is case-sensitive, and LOWER() / UPPER() change the letters
A to Z only. (SQLite builds that include ICU, as conda's do, would otherwise change every letter.)
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import sqlite3
from pathlib import Path

from .. import util
from ..errors import PclError, Unavailable
from . import schema_def
from .base import (DatabaseError, Db, IntegrityError, Store, not_initialized_error,
                   one_block_at_a_time, strlist_text)

APP_FILE = "pcl_app.sqlite"
CATALOG_FILE = "pcl_catalog.sqlite"
MIN_SQLITE = (3, 35, 0)
BUSY_MILLISECONDS = 5000        # the least time a writer waits for the write lock

_BUSY_MESSAGE = ("The database is busy: another pcldb task is writing to it. "
                 "Wait a moment and try again.")


def require_sqlite_version():
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise PclError(
            "This Python comes with SQLite %s; pcldb needs SQLite 3.35 or newer. Use a newer "
            "Python (3.11 or later from python.org), or the PostgreSQL engine."
            % sqlite3.sqlite_version, code="sqlite_too_old", status=503)


def busy_milliseconds(settings):
    """How long an app() connection waits for the write lock (from_lead 20a): longer than the
    longest expert query, `sql.timeout_seconds` + 5 seconds, and at least BUSY_MILLISECONDS.
    An expert query holds a read lock on the catalogue until its statement ends, and a commit
    has to wait for that; with this wait the commit outlasts the query instead of failing."""
    try:
        seconds = float(settings.sql.timeout_seconds)
    except (AttributeError, TypeError, ValueError):
        seconds = 0.0
    return max(BUSY_MILLISECONDS, int(round((seconds + 5) * 1000)))


def file_uri(path, mode):
    """The `file:` address of a database file for sqlite3.connect(..., uri=True).

    `mode`: "ro" (read only), "rw" (must exist), "rwc" (create when missing). Built with
    pathlib so that drive letters, spaces and odd characters work on every system.
    """
    absolute = os.path.abspath(path)
    if is_network_path(absolute):
        raise PclError(
            "The SQLite files would be on a network share (%s). SQLite cannot open its files "
            "there, and a database on a share is easily damaged. Put the project folder (or "
            "PCLDB_DIR) on a local disk, for example in your user profile."
            % os.path.dirname(absolute), code="network_folder", status=503)
    return Path(absolute).as_uri() + "?mode=" + mode


def is_network_path(path, windows=None):
    """True for a Windows network path (\\\\server\\share\\..., also \\\\?\\UNC\\...); never
    elsewhere. A mapped drive letter is not one."""
    windows = os.name == "nt" if windows is None else windows
    if not windows:
        return False
    import ntpath
    text = str(path).replace("/", "\\")
    if text.upper().startswith("\\\\?\\UNC\\"):
        return True
    if text.startswith("\\\\?\\") or text.startswith("\\\\.\\"):
        return False
    return ntpath.splitdrive(text)[0].startswith("\\\\")


_TO_LOWER = {code: code + 32 for code in range(ord("A"), ord("Z") + 1)}
_TO_UPPER = {code: code - 32 for code in range(ord("a"), ord("z") + 1)}


def _ascii_lower(value):
    if isinstance(value, str):
        return value.translate(_TO_LOWER)
    return value if value is None else str(value).translate(_TO_LOWER)


def _ascii_upper(value):
    if isinstance(value, str):
        return value.translate(_TO_UPPER)
    return value if value is None else str(value).translate(_TO_UPPER)


def pin_text_functions(connection):
    """Make LIKE case-sensitive and LOWER/UPPER ASCII-only on this connection, whatever the
    SQLite build would do by itself."""
    connection.execute("PRAGMA case_sensitive_like=ON")
    connection.create_function("lower", 1, _ascii_lower, deterministic=True)
    connection.create_function("upper", 1, _ascii_upper, deterministic=True)


def _is_busy(error):
    code = getattr(error, "sqlite_errorcode", None)
    if code is not None:
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return "locked" in str(error) or "busy" in str(error)


# ---------------------------------------------------------------------- values coming out


def _out_bool(value):
    return bool(value) if isinstance(value, int) else value


def _out_json(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return value
    return value


def _out_real(value):
    return float(value) if isinstance(value, int) and not isinstance(value, bool) else value


def _out_utc(value):
    if isinstance(value, str) and len(value) != 24:
        try:
            return util.utc_text(value)
        except PclError:
            return value
    return value


_OUT = {"bool": _out_bool, "json": _out_json, "strlist": _out_json, "real": _out_real,
        "utc": _out_utc}

_CONSTRAINT_KINDS = {
    "SQLITE_CONSTRAINT_UNIQUE": "unique", "SQLITE_CONSTRAINT_PRIMARYKEY": "unique",
    "SQLITE_CONSTRAINT_NOTNULL": "not_null", "SQLITE_CONSTRAINT_FOREIGNKEY": "foreign_key",
    "SQLITE_CONSTRAINT_CHECK": "check",
}


_READ_ONLY_START = re.compile(r"\s*SELECT\b", re.IGNORECASE)


class SqliteDb(Db):
    engine = "sqlite"

    def __init__(self, connection):
        super().__init__()
        self._connection = connection

    _wrote = False

    def _run(self, text, values):
        if not _READ_ONLY_START.match(text):
            self._wrote = True      # anything but a plain SELECT is kept (DDL, pragmas, ...)
        return self._connection.execute(text, values)

    def _run_many(self, text, rows):
        self._wrote = True
        return self._connection.executemany(text, rows).rowcount

    def _rows(self, cursor):
        if cursor.description is None:
            return []
        names = [item[0] for item in cursor.description]
        converters = [_OUT.get(schema_def.COLUMN_KINDS.get(name)) for name in names]
        rows = []
        for raw in cursor.fetchall():
            row = {}
            for name, convert, value in zip(names, converters, raw):
                row[name] = value if convert is None or value is None else convert(value)
            rows.append(row)
        return rows

    def _encode(self, kind, clean):
        if kind == "bool":
            return int(clean)
        if kind == "strlist":
            return strlist_text(clean)
        return clean    # text, numbers, the UTC text and the canonical JSON text go in as they are

    def _translate(self, error):
        return translate_error(error)


def translate_error(error):
    """The engine-neutral exception for a sqlite3 error; None for anything else."""
    if isinstance(error, sqlite3.IntegrityError):
        kind = _CONSTRAINT_KINDS.get(getattr(error, "sqlite_errorname", ""), "other")
        return IntegrityError(str(error), kind)
    if isinstance(error, sqlite3.OperationalError) and _is_busy(error):
        return Unavailable(_BUSY_MESSAGE, code="db_busy")
    if isinstance(error, sqlite3.Error):
        return DatabaseError(str(error))
    return None


# ---------------------------------------------------------------------- the store


class SqliteStore(Store):
    engine = "sqlite"

    def __init__(self, settings):
        super().__init__(settings)
        self.folder = settings.sqlite_dir
        self.app_path = self.folder / APP_FILE
        self.catalog_path = self.folder / CATALOG_FILE
        self._ready = False

    def describe(self):
        # No absolute path: /status shows this to every signed-in person, and a path names the
        # account. `pcldb db status` prints the full folder for the person at this computer.
        try:
            inside = self.folder.resolve().relative_to(
                self.settings.project_dir.resolve()).as_posix()
        except (OSError, ValueError):
            inside = None
        if inside is not None:
            where = "this computer, SQLite files in the project folder (%s)" % inside
        else:
            where = "this computer, SQLite files in the folder set in pcldb_config.json"
        return {"engine": "sqlite", "version": sqlite3.sqlite_version, "where": where}

    def catalog_uri(self, mode="ro"):
        """The catalogue file's `file:` address (for the read-only reader)."""
        return file_uri(self.catalog_path, mode)

    # -- connections -------------------------------------------------------------------------

    def _open(self, mode):
        """A connection with the app file as main and the catalogue attached as pcl."""
        require_sqlite_version()
        busy = busy_milliseconds(self.settings)
        connection = None
        try:
            connection = sqlite3.connect(file_uri(self.app_path, mode), uri=True,
                                         isolation_level=None, timeout=busy / 1000.0)
            connection.execute("PRAGMA busy_timeout=%d" % busy)
            connection.execute("ATTACH DATABASE ? AS pcl", (file_uri(self.catalog_path, mode),))
            connection.execute("PRAGMA foreign_keys=ON")
            pin_text_functions(connection)
            if mode != "ro":
                connection.execute("PRAGMA main.journal_mode=DELETE")
                connection.execute("PRAGMA pcl.journal_mode=DELETE")
        except sqlite3.Error as error:
            if connection is not None:
                connection.close()
            if _is_busy(error):
                raise Unavailable(_BUSY_MESSAGE, code="db_busy") from None
            if mode == "rwc":
                raise PclError("The SQLite files in %s could not be opened or made (%s)."
                               % (self.folder, error), code="db_unusable", status=503) from None
            raise not_initialized_error() from None
        return connection

    @staticmethod
    def _rollback(connection):
        try:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        except sqlite3.Error:
            pass    # closing the connection drops the transaction anyway

    def app(self):
        return self._block("BEGIN IMMEDIATE")

    def app_only(self):
        """A transaction for a block that writes only the private part (app.*: "last used",
        the query log). It starts deferred, so it never takes the catalogue's write lock, and
        its commit never waits for a long expert query that is reading the catalogue."""
        return self._block("BEGIN")

    @contextlib.contextmanager
    def _block(self, begin):
        # BEGIN IMMEDIATE takes the write lock of both files at once (no deadlock between two
        # writers). A block that wrote nothing ends with ROLLBACK, not COMMIT: SQLite's commit
        # of a transaction that holds the catalogue's write lock waits until every reader of
        # the catalogue is done, and an expert query may read it for many seconds. Without
        # this, a token check (a read) would wait behind one analyst's slow query.
        with one_block_at_a_time(os.path.normcase(os.path.abspath(self.app_path))):
            if not self._ready:
                self._require_initialized()
            connection = self._open("rw")
            db = None
            try:
                try:
                    connection.execute(begin)
                except sqlite3.Error as error:
                    raise (translate_error(error) or error) from None
                db = SqliteDb(connection)
                changes = connection.total_changes
                try:
                    yield db
                except BaseException:
                    self._rollback(connection)
                    raise
                if db._failed:
                    self._rollback(connection)
                    raise DatabaseError("The transaction was rolled back because one of its "
                                        "statements failed.")
                if not db._wrote and connection.total_changes == changes:
                    self._rollback(connection)      # nothing to keep: see above
                    return
                try:
                    connection.execute("COMMIT")
                except sqlite3.Error as error:
                    self._rollback(connection)
                    raise (translate_error(error) or error) from None
            finally:
                if db is not None:
                    db._closed = True
                connection.close()

    # -- setting up ---------------------------------------------------------------------------

    def _file_version(self, path):
        """None for a missing or empty file, else its schema_version text ("unknown" when the
        file holds something that is not this program's schema)."""
        if not path.is_file() or path.stat().st_size == 0:
            return None
        require_sqlite_version()
        # Read-only first. Only when that fails, once more with write access: after a crash in
        # the middle of a commit SQLite must replay its journal before the file can be read,
        # and a read-only connection is not allowed to. ("rw" never creates a file.)
        for mode in ("ro", "rw"):
            try:
                connection = sqlite3.connect(file_uri(path, mode), uri=True, isolation_level=None,
                                             timeout=BUSY_MILLISECONDS / 1000.0)
            except sqlite3.Error:
                continue
            try:
                if connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()[0] == 0:
                    return None
                row = connection.execute(
                    "SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
                return str(row[0]) if row and row[0] is not None else "unknown"
            except sqlite3.Error as error:
                if _is_busy(error):
                    raise Unavailable(_BUSY_MESSAGE, code="db_busy") from None
            finally:
                connection.close()
        return "unknown"

    def _versions(self):
        return {"app": self._file_version(self.app_path),
                "pcl": self._file_version(self.catalog_path)}

    def is_initialized(self):
        wanted = str(schema_def.SCHEMA_VERSION)
        return all(found == wanted for found in self._versions().values())

    def _require_initialized(self):
        wanted = str(schema_def.SCHEMA_VERSION)
        for found in self._versions().values():
            if found != wanted:
                raise not_initialized_error(found)
        self._ready = True

    def initialize(self):
        require_sqlite_version()
        wanted = str(schema_def.SCHEMA_VERSION)
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise PclError("The folder %s could not be made (%s)."
                           % (self.folder, error.strerror or error), code="db_unusable",
                           status=503) from None
        connection = self._open("rwc")
        try:
            connection.execute("BEGIN IMMEDIATE")
            # Looked at inside the write lock, so two programs setting up at once cannot collide.
            for schema in ("pcl", "app"):
                name = schema_def.SCHEMA_NAMES["sqlite"][schema]
                tables = connection.execute(
                    "SELECT COUNT(*) FROM %s.sqlite_master" % name).fetchone()[0]
                if tables:
                    try:
                        row = connection.execute(
                            "SELECT value FROM %s.meta WHERE key = 'schema_version'"
                            % name).fetchone()
                    except sqlite3.Error:
                        row = None
                    found = str(row[0]) if row and row[0] is not None else "unknown"
                    if found != wanted:
                        raise not_initialized_error(found)
                    continue
                for statement in schema_def.ddl("sqlite", schema):
                    connection.execute(statement)
                connection.execute(
                    "INSERT INTO %s.meta (key, value) VALUES ('schema_version', ?)" % name,
                    (wanted,))
            connection.execute("COMMIT")
        except BaseException as error:
            self._rollback(connection)
            if isinstance(error, sqlite3.Error):
                raise (translate_error(error) or error) from None
            raise
        finally:
            connection.close()
        if os.name != "nt":
            try:
                os.chmod(self.app_path, 0o600)     # the private file: this account only
            except OSError:
                pass
        self._ready = True

    def destroy(self):
        """Delete both files (and SQLite's side files). The folder stays."""
        self._ready = False
        for path in (self.app_path, self.catalog_path):
            for suffix in ("", "-journal", "-wal", "-shm"):
                target = path.with_name(path.name + suffix)
                try:
                    target.unlink()
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise PclError(
                        "%s could not be deleted (%s). Stop the pcldb server and close any "
                        "program that has the file open, then try again."
                        % (target, error.strerror or error), code="file_in_use",
                        status=503) from None
