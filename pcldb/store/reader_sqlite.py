"""The sandboxed read-only SQLite connection for expert SQL (specification section 9).

    with store.reader() as reader:                  # one new connection, used once
        columns, rows, truncated = reader.run(sql, max_rows=1000, timeout_seconds=15)

A person types the SQL, so it is hostile until proven otherwise. What it meets, layer by layer:

1. The connection never has the private file. Its main database is an empty in-memory one; the
   catalogue file is attached READ-ONLY as `pcl` (so `sessions` and `pcl.sessions` both work).
   `pcl_app.sqlite` (users, token hashes, audit rows) is never opened here, and no further file
   can be attached (limit of attached databases = 1, and ATTACH is refused anyway).
2. A default-deny authorizer. SQLite asks it about every action while it compiles a statement.
   Allowed: SELECT, a recursive query, reading a column of a table or view of the catalogue
   (plus `sqlite_master` / `sqlite_schema`, `json_each`, `json_tree`), and calling a function
   of the allow-list below. Everything else is refused: PRAGMA (statements and `pragma_*`
   tables), ATTACH, DETACH, VACUUM, every write, CREATE, DROP, ALTER, transactions, savepoints,
   temporary tables, other built-in tables (`dbstat`, the full-text helpers), `load_extension`.
   One case needs a rule of its own: for `SELECT count(*) FROM x`, where no column is read,
   SQLite asks about the name `x` exactly as it was typed, and `x` may be a WITH name or a
   sub-query. Such a column-less read is allowed unless the name is one of SQLite's built-in
   table modules, a `pragma_*` table or a `sqlite_*` table.
3. Limits on the connection: values of at most 10 MB, a short LIKE pattern, shallow expressions,
   and a cap on the memory SQLite itself may take (see MEMORY_LIMIT_BYTES).
4. The driver takes one statement per call ("You can only execute one statement at a time").
5. A time limit: a progress handler stops the statement at the deadline, and a watchdog thread
   calls `interrupt()` shortly after as a second line.
6. Rows are fetched one at a time and fetching stops at `max_rows + 1` rows or when the rows in
   hand reach sqlrun.MAX_RESULT_BYTES, so a huge result is never held in memory.

Measured on SQLite 3.45 (the notes are in the tests): a single call of `instr`, `replace`,
`trim(text, characters)`, `unhex(text, characters)` or `json_patch` on megabyte-sized texts
runs for minutes inside ONE step of SQLite, where neither the progress handler nor
`interrupt()` can reach it. So `json_patch` is not on the allow-list, the four others are
replaced by guarded versions that refuse oversized work and otherwise hand the call to
SQLite's own function (same results), and LIKE / GLOB patterns are limited to 200 characters.

7. Temporary disk space. A query that sorts or groups millions of made-up rows makes SQLite
   write temporary files (measured: about 400 MB per second; they are removed when the query
   ends). SQLite has no limit for them, so the progress handler also watches the free space of
   the temporary folder and stops a statement during which it fell by more than
   TEMP_DISK_BUDGET_BYTES.

Known limit of this engine (the documents say so): a query that reads the catalogue holds a
read lock on that file until it ends, so a writer (a registration) waits for it and gives up
after its own 5 seconds.
"""

from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
import threading
import time

from .. import sqlrun
from ..errors import PclError, Unavailable
from .base import not_initialized_error
from .sqlite_engine import require_sqlite_version

# ---------------------------------------------------------------------- limits

VALUE_LIMIT_BYTES = 10 * 1000 * 1000    # SQLITE_LIMIT_LENGTH: one text, blob or row
LIKE_PATTERN_CHARS = 200                # one LIKE/GLOB call is not interruptible: keep it short
EXPRESSION_DEPTH = 500                  # deep expressions use the thread's stack
# Brackets nested deeper than this are refused before SQLite parses the text. SQLite up to 3.45
# stops at "parser stack overflow" (measured on 3.45: 94 plain brackets, 16 nested sub-queries);
# from 3.46 its parser grows on the heap, and a few thousand nested sub-queries then use up a
# small thread stack inside SQLite (the process dies). With this check deep nesting is refused
# the same way on every SQLite version, and a request thread stays safe.
BRACKET_DEPTH = 100
COMPOUND_SELECT_TERMS = 100
FUNCTION_ARGUMENTS = 100
PROGRAM_STEPS = 2000000                 # size of one compiled statement

# Process-wide cap on SQLite's own memory (PRAGMA hard_heap_limit; it can only be lowered once
# set). Measured: 20 common table expressions that each read the one before twice make SQLite
# build a tree of a million parts while it compiles (1.7 GB, 7 s; 24 of them would exhaust the
# machine). With the cap the same statement ends in 0.3 s with "out of memory". pcldb's own
# connections need a few megabytes.
MEMORY_LIMIT_BYTES = 256 * 1024 * 1024

# How far the free space of SQLite's temporary folder may fall while one statement runs.
TEMP_DISK_BUDGET_BYTES = 1024 * 1024 * 1024
DISK_CHECK_SECONDS = 0.2

WATCHDOG_GRACE_SECONDS = 0.5
BUSY_SECONDS = 2.0
PROGRESS_STEPS = 2000

# Work limits of the guarded functions: (size of first argument) x (size of second argument).
# Measured cost: instr/replace about 1e11 units per second, trim/unhex about 5e8.
_GUARDS = (("instr", 2, 2 * 10 ** 9), ("replace", 3, 2 * 10 ** 9),
           ("trim", 2, 5 * 10 ** 7), ("ltrim", 2, 5 * 10 ** 7), ("rtrim", 2, 5 * 10 ** 7),
           ("unhex", 2, 5 * 10 ** 7))

# ---------------------------------------------------------------------- the allow-lists

ALLOWED_FUNCTIONS = frozenset("""
    abs char coalesce concat concat_ws format glob hex if ifnull iif instr length like
    likelihood likely lower ltrim max min nullif octet_length printf quote random randomblob
    replace round rtrim sign soundex sqlite_version substr substring trim typeof unhex unicode
    unistr unistr_quote unlikely upper zeroblob
    date time datetime julianday unixepoch strftime timediff
    current_date current_time current_timestamp
    acos acosh asin asinh atan atan2 atanh ceil ceiling cos cosh degrees exp floor ln log log10
    log2 mod pi pow power radians sin sinh sqrt tan tanh trunc
    json json_array json_array_length json_error_position json_extract json_insert json_object
    json_pretty json_quote json_remove json_replace json_set json_type json_valid
    jsonb jsonb_array jsonb_extract jsonb_insert jsonb_object jsonb_remove jsonb_replace
    jsonb_set -> ->>
    json_group_array json_group_object jsonb_group_array jsonb_group_object
    avg count group_concat string_agg sum total median percentile percentile_cont
    percentile_disc
    row_number rank dense_rank percent_rank cume_dist ntile lag lead first_value last_value
    nth_value
    """.split())

# Readable besides the catalogue's own tables and views.
_ALWAYS_READABLE = frozenset(("sqlite_master", "sqlite_schema", "json_each", "json_tree"))

# Table modules SQLite builds may carry (PRAGMA module_list adds what this build really has).
_KNOWN_MODULES = frozenset((
    "bytecode", "carray", "completion", "csv", "dbstat", "fsdir", "fts3", "fts3tokenize",
    "fts4", "fts4aux", "fts5", "fts5vocab", "generate_series", "geopoly", "rtree", "rtree_i32",
    "sqlite_dbdata", "sqlite_dbpage", "sqlite_dbptr", "sqlite_stmt", "stmt", "swarmvtab",
    "tables_used", "unionvtab", "vtablog", "zipfile"))

_WRITES = frozenset(getattr(sqlite3, name) for name in (
    "SQLITE_INSERT", "SQLITE_UPDATE", "SQLITE_DELETE", "SQLITE_CREATE_INDEX",
    "SQLITE_CREATE_TABLE", "SQLITE_CREATE_TEMP_INDEX", "SQLITE_CREATE_TEMP_TABLE",
    "SQLITE_CREATE_TEMP_TRIGGER", "SQLITE_CREATE_TEMP_VIEW", "SQLITE_CREATE_TRIGGER",
    "SQLITE_CREATE_VIEW", "SQLITE_CREATE_VTABLE", "SQLITE_DROP_INDEX", "SQLITE_DROP_TABLE",
    "SQLITE_DROP_TEMP_INDEX", "SQLITE_DROP_TEMP_TABLE", "SQLITE_DROP_TEMP_TRIGGER",
    "SQLITE_DROP_TEMP_VIEW", "SQLITE_DROP_TRIGGER", "SQLITE_DROP_VIEW", "SQLITE_DROP_VTABLE",
    "SQLITE_ALTER_TABLE", "SQLITE_REINDEX", "SQLITE_ANALYZE") if hasattr(sqlite3, name))

_ONLY_READS = "Expert mode only reads the catalogue. "
_PRIVATE_NAME = re.compile(r"^no such (?:table|view): app\.", re.IGNORECASE)


def bracket_depth(sql):
    """How deeply round brackets nest in `sql`, not counting brackets inside '...', "...",
    `...`, [...] or comments."""
    depth = deepest = 0
    index, size = 0, len(sql)
    while index < size:
        char = sql[index]
        if char in "'\"`[":
            end = sql.find("]" if char == "[" else char, index + 1)
            if end < 0:
                break
            index = end + 1
            continue
        if char == "-" and sql.startswith("--", index):
            end = sql.find("\n", index)
            if end < 0:
                break
            index = end + 1
            continue
        if char == "/" and sql.startswith("/*", index):
            end = sql.find("*/", index + 2)
            if end < 0:
                break
            index = end + 2
            continue
        if char == "(":
            depth += 1
            deepest = max(deepest, depth)
        elif char == ")" and depth:
            depth -= 1
        index += 1
    return deepest

_memory_lock = threading.Lock()
_memory_capped = False


def _cap_memory(connection):
    """Set the process-wide cap on SQLite's memory once (see MEMORY_LIMIT_BYTES)."""
    global _memory_capped
    if _memory_capped:
        return
    with _memory_lock:
        if _memory_capped:
            return
        try:
            connection.execute("PRAGMA hard_heap_limit=%d" % MEMORY_LIMIT_BYTES)
        except sqlite3.Error:
            pass            # an SQLite older than 3.31 has no such cap; the other layers stay
        _memory_capped = True


def _size(value):
    return len(value) if isinstance(value, (str, bytes)) else 24


def temp_folder():
    """The folder SQLite writes its temporary files into (its own search order)."""
    if os.name == "nt":
        return tempfile.gettempdir()
    candidates = [os.environ.get("SQLITE_TMPDIR"), os.environ.get("TMPDIR"), "/var/tmp",
                  "/usr/tmp", "/tmp", "."]
    for folder in candidates:
        if folder and os.path.isdir(folder) and os.access(folder, os.W_OK | os.X_OK):
            return folder
    return tempfile.gettempdir()


def _free_bytes(folder):
    try:
        return shutil.disk_usage(folder).free
    except OSError:
        return None


class _TooMuchWork(Exception):
    pass


def open_reader(store):
    """A new sandboxed connection for ONE statement. Called by `store.reader()`."""
    return SqliteReader(store)


class SqliteReader:
    """One read-only connection for one statement. Use it in a `with` block and call `run`
    once; the connection is closed when `run` returns."""

    engine = "sqlite"

    def __init__(self, store):
        require_sqlite_version()
        self._connection = None
        self._helper = None
        self._used = False
        self._denied = None         # what the authorizer refused first: (action, a, b)
        self._guard = None          # the guarded function that refused its arguments
        self._guard_error = None    # what SQLite's own function said inside a guarded call
        self._deadline = None
        self._deadline_hit = False
        self._late = False          # the authorizer was asked after the deadline had passed
        self._temp_folder = None
        self._free_at_start = None
        self._next_disk_check = 0.0
        self._disk_hit = False      # the statement used up its temporary disk space
        self._lock = threading.Lock()
        max_sql_chars = store.settings.sql.max_sql_chars
        catalog = store.catalog_path
        if not catalog.is_file():
            raise not_initialized_error()
        connection = None
        try:
            connection = sqlite3.connect(":memory:", uri=True, isolation_level=None,
                                         cached_statements=0, check_same_thread=False,
                                         timeout=BUSY_SECONDS)
            self._connection = connection
            _cap_memory(connection)
            connection.execute("ATTACH DATABASE ? AS pcl", (store.catalog_uri("ro"),))
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA case_sensitive_like=ON")   # as the app connection has it
            if hasattr(connection, "enable_load_extension"):
                connection.enable_load_extension(False)
            self._readable = self._catalog_objects(connection) | _ALWAYS_READABLE
            self._modules = self._table_modules(connection) - self._readable
            connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 1)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, VALUE_LIMIT_BYTES)
            connection.setlimit(sqlite3.SQLITE_LIMIT_SQL_LENGTH, 4 * max_sql_chars + 64)
            connection.setlimit(sqlite3.SQLITE_LIMIT_LIKE_PATTERN_LENGTH, LIKE_PATTERN_CHARS)
            connection.setlimit(sqlite3.SQLITE_LIMIT_EXPR_DEPTH, EXPRESSION_DEPTH)
            connection.setlimit(sqlite3.SQLITE_LIMIT_COMPOUND_SELECT, COMPOUND_SELECT_TERMS)
            connection.setlimit(sqlite3.SQLITE_LIMIT_FUNCTION_ARG, FUNCTION_ARGUMENTS)
            connection.setlimit(sqlite3.SQLITE_LIMIT_VDBE_OP, PROGRAM_STEPS)
            connection.setlimit(sqlite3.SQLITE_LIMIT_TRIGGER_DEPTH, 1)
            connection.setlimit(sqlite3.SQLITE_LIMIT_WORKER_THREADS, 0)
            self._install_guards(connection)
            connection.set_authorizer(self._authorize)     # last: from here on, reads only
        except sqlite3.Error as error:
            self.close()
            if _is_busy(error):
                raise Unavailable("The database is busy: another pcldb task is writing to it. "
                                  "Wait a moment and try again.", code="db_busy") from None
            raise not_initialized_error() from None
        except BaseException:
            self.close()
            raise

    # -- set-up ---------------------------------------------------------------------------------

    @staticmethod
    def _catalog_objects(connection):
        """The lower-case names of the catalogue's ordinary tables and views."""
        names = set()
        for name, kind, text in connection.execute(
                "SELECT name, type, sql FROM pcl.sqlite_master WHERE type IN ('table', 'view')"):
            if not isinstance(name, str) or name.lower().startswith("sqlite_"):
                continue
            if kind == "table" and isinstance(text, str) \
                    and text.lstrip().upper().startswith("CREATE VIRTUAL"):
                continue
            names.add(name.lower())
        return frozenset(names)

    @staticmethod
    def _table_modules(connection):
        """The lower-case names of the table modules this SQLite has (dbstat, fts5 ...)."""
        names = set(_KNOWN_MODULES)
        try:
            for row in connection.execute("PRAGMA module_list"):
                if isinstance(row[0], str):
                    names.add(row[0].lower())
        except sqlite3.Error:
            pass            # an SQLite without that PRAGMA: the fixed list above stands
        return frozenset(names)

    def _install_guards(self, connection):
        """Replace the built-in functions whose single call can run for minutes by versions
        that refuse oversized work and otherwise ask SQLite's own function (same result)."""
        helper = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
        self._helper = helper
        helper.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, VALUE_LIMIT_BYTES)
        for name, count, budget in _GUARDS:
            statement = "SELECT %s(%s)" % (name, ", ".join("?" * count))
            try:
                helper.execute(statement, (None,) * count).fetchone()
            except sqlite3.Error:
                continue            # this SQLite has no such function (unhex before 3.41)
            connection.create_function(name, count, self._guarded(name, statement, budget),
                                       deterministic=True)

    def _guarded(self, name, statement, budget):
        helper = self._helper

        def call(*arguments):
            if _size(arguments[0]) * _size(arguments[1]) > budget:
                self._guard = name
                raise _TooMuchWork(name)
            try:
                return helper.execute(statement, arguments).fetchone()[0]
            except Exception as error:      # for example "string or blob too big"
                self._guard_error = error
                raise

        return call

    # -- the authorizer ---------------------------------------------------------------------------

    def _authorize(self, action, first, second, database, source):
        try:
            if self._deadline is not None and time.monotonic() >= self._deadline:
                self._late = True       # compiling took too long: stop here
                return sqlite3.SQLITE_DENY
            if action == sqlite3.SQLITE_SELECT or action == sqlite3.SQLITE_RECURSIVE:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ:
                name = first.lower() if isinstance(first, str) else ""
                if name in self._readable:
                    return sqlite3.SQLITE_OK
                # No column is read and the name is as typed: a WITH name or a sub-query.
                if second == "" and database is None and name \
                        and name not in self._modules \
                        and not name.startswith(("pragma_", "sqlite_")):
                    return sqlite3.SQLITE_OK
            elif action == sqlite3.SQLITE_FUNCTION:
                if isinstance(second, str) and second.lower() in ALLOWED_FUNCTIONS:
                    return sqlite3.SQLITE_OK
            if self._denied is None:
                self._denied = (action, first, second)
        except Exception:
            pass
        return sqlite3.SQLITE_DENY

    def _denied_message(self):
        action, first, second = self._denied
        if action == sqlite3.SQLITE_READ:
            return _ONLY_READS + "The table %s is not part of it." % _shown(first)
        if action == sqlite3.SQLITE_FUNCTION:
            return ("The function %s is not available in expert mode." % _shown(second))
        if action == sqlite3.SQLITE_PRAGMA:
            return (_ONLY_READS + "PRAGMA is not available; the schema browser lists every "
                    "table and column.")
        if action in (sqlite3.SQLITE_ATTACH, sqlite3.SQLITE_DETACH):
            return _ONLY_READS + "Opening or closing other database files is not allowed."
        if action in (sqlite3.SQLITE_TRANSACTION, sqlite3.SQLITE_SAVEPOINT):
            return (_ONLY_READS + "BEGIN, COMMIT, ROLLBACK and SAVEPOINT are not needed: every "
                    "query runs on its own and nothing can be changed.")
        if action in _WRITES:
            return _ONLY_READS + "This statement would change something, so it was not run."
        return _ONLY_READS + "This statement was not run."

    # -- running ---------------------------------------------------------------------------------

    def _progress(self):
        now = time.monotonic()
        if now >= self._deadline:
            self._deadline_hit = True
            return 1
        if now >= self._next_disk_check and self._free_at_start is not None:
            self._next_disk_check = now + DISK_CHECK_SECONDS
            free = _free_bytes(self._temp_folder)
            if free is not None and self._free_at_start - free > TEMP_DISK_BUDGET_BYTES:
                self._disk_hit = True
                return 1
        return 0

    def _watchdog(self):
        with self._lock:
            connection = self._connection
            if connection is None:
                return
            self._deadline_hit = True
            try:
                connection.interrupt()
            except sqlite3.Error:
                pass

    def run(self, sql, max_rows=1000, timeout_seconds=15):
        """Run one statement. Returns (columns, rows, truncated): columns is a list of
        (name, storage class of the first value: "integer", "real", "text", "blob" or ""),
        rows a list of tuples of raw values, truncated True when the statement had more rows
        than `max_rows` (or more data than sqlrun.MAX_RESULT_BYTES).

        Raises PclError with code "sql_refused", "sql_error" or "sql_timeout".
        """
        if self._used or self._connection is None:
            raise RuntimeError("a reader runs one statement; open a new one with store.reader()")
        self._used = True
        if not isinstance(sql, str):
            raise sqlrun.refused("The SQL must be text.")
        depth = bracket_depth(sql)
        if depth > BRACKET_DEPTH:
            raise sqlrun.refused("This SQL nests brackets %d deep; expert mode allows at most %d. "
                                 "Write it with fewer levels (a WITH part can help)."
                                 % (depth, BRACKET_DEPTH))
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise ValueError("max_rows must be a whole number of at least 1")
        timeout_seconds = float(timeout_seconds)
        if not timeout_seconds > 0:
            raise ValueError("timeout_seconds must be more than 0")
        connection = self._connection
        self._temp_folder = temp_folder()
        self._free_at_start = _free_bytes(self._temp_folder)
        self._next_disk_check = time.monotonic() + DISK_CHECK_SECONDS
        self._deadline = time.monotonic() + timeout_seconds
        connection.set_progress_handler(self._progress, PROGRESS_STEPS)
        watchdog = threading.Timer(timeout_seconds + WATCHDOG_GRACE_SECONDS, self._watchdog)
        watchdog.daemon = True
        watchdog.start()
        cursor = None
        try:
            cursor = connection.execute(sql)
            names = [item[0] for item in cursor.description] if cursor.description else []
            rows, size, truncated = [], 0, False
            while True:
                row = cursor.fetchone()
                if row is None:
                    break
                if len(rows) >= max_rows:
                    truncated = True
                    break
                rows.append(row)
                for value in row:
                    size += len(value) if isinstance(value, (str, bytes)) else 8
                if size >= sqlrun.MAX_RESULT_BYTES:
                    truncated = cursor.fetchone() is not None
                    break
            return [(name, _storage_class(rows, index)) for index, name in enumerate(names)], \
                rows, truncated
        except PclError:
            raise
        except Exception as error:
            raise self._translate(error, timeout_seconds) from None
        finally:
            watchdog.cancel()
            if cursor is not None:
                try:
                    cursor.close()
                except sqlite3.Error:
                    pass
            self.close()

    def _translate(self, error, timeout_seconds):
        """The PclError for whatever stopped the statement. Never a stack trace, never a path."""
        if self._late:
            return sqlrun.timed_out(timeout_seconds)
        if self._disk_hit:
            return sqlrun.refused(
                "This query needs more temporary disk space than expert mode allows (about "
                "%d MB). Make it smaller: sort or group fewer rows."
                % (TEMP_DISK_BUDGET_BYTES // (1024 * 1024)),
                {"limit_bytes": TEMP_DISK_BUDGET_BYTES})
        if self._guard_error is not None:
            error, self._guard_error = self._guard_error, None
        if self._guard is not None:
            return sqlrun.refused(
                "This call of %s() works on texts that are too large for expert mode. "
                "Use shorter texts." % self._guard, {"function": self._guard})
        if self._denied is not None:
            return sqlrun.refused(self._denied_message())
        if isinstance(error, MemoryError):
            return sqlrun.refused(
                "This statement needs more memory than expert mode allows. Make it simpler "
                "(fewer nested WITH parts, smaller values).")
        code = getattr(error, "sqlite_errorcode", None)
        code = code & 0xFF if isinstance(code, int) else None
        text = str(error)
        if code == sqlite3.SQLITE_INTERRUPT or (code is None and text == "interrupted"):
            if self._deadline_hit or time.monotonic() >= self._deadline:
                return sqlrun.timed_out(timeout_seconds)
            return sqlrun.failed("The query was interrupted.")
        if code == sqlite3.SQLITE_NOMEM:
            return sqlrun.refused(
                "This statement needs more memory than expert mode allows. Make it simpler "
                "(fewer nested WITH parts, smaller values).")
        if code == sqlite3.SQLITE_TOOBIG or text == "string or blob too big":
            return sqlrun.refused(
                "A value in this query is larger than expert mode allows (%d MB)."
                % (VALUE_LIMIT_BYTES // 1000000), {"limit_bytes": VALUE_LIMIT_BYTES})
        if _is_busy(error):
            return Unavailable("The database is busy: another pcldb task is writing to it. "
                               "Wait a moment and try again.", code="db_busy")
        if "null character" in text and isinstance(error, (ValueError, sqlite3.Error)):
            return sqlrun.refused("This SQL holds a NUL character, which a database cannot "
                                  "read. Type the statement again.")
        if isinstance(error, (sqlite3.ProgrammingError, sqlite3.Warning)):
            if "one statement at a time" in text:
                return sqlrun.refused("Expert mode runs one statement at a time. Remove "
                                      "everything after the first ';'.")
            if "bindings" in text or "binding" in text:
                return sqlrun.refused(sqlrun.PARAMETER_MARK)
            return sqlrun.failed("SQLite could not run this: %s" % text)
        if isinstance(error, UnicodeError):
            return sqlrun.refused("This SQL or its result holds text that is not valid UTF-8.")
        if isinstance(error, sqlite3.Error):
            if code == sqlite3.SQLITE_AUTH:
                return sqlrun.refused(_ONLY_READS + "This statement was not run.")
            if _PRIVATE_NAME.search(text):
                # The private file is never attached here; say so as PostgreSQL does.
                return sqlrun.refused(sqlrun.PRIVATE_PART)
            return sqlrun.failed("SQLite said: %s" % text)
        if isinstance(error, (OverflowError, RecursionError)):
            return sqlrun.failed("The result could not be read (%s)." % type(error).__name__)
        raise error

    # -- closing ----------------------------------------------------------------------------------

    def close(self):
        with self._lock:
            connection, self._connection = self._connection, None
            helper, self._helper = self._helper, None
        for item in (connection, helper):
            if item is not None:
                try:
                    item.close()
                except sqlite3.Error:
                    pass

    def __enter__(self):
        return self

    def __exit__(self, kind, value, trace):
        self.close()
        return False


def _is_busy(error):
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED)
    return "database is locked" in str(error) or "database table is locked" in str(error)


def _shown(name):
    text = str(name)
    return text if len(text) <= 80 else text[:77] + "..."


_CLASSES = ((bool, "integer"), (int, "integer"), (float, "real"), (str, "text"),
            (bytes, "blob"))


def _storage_class(rows, index):
    """SQLite gives a result column no type of its own; report the kind of its first value."""
    for row in rows:
        value = row[index]
        if value is None:
            continue
        for kind, name in _CLASSES:
            if isinstance(value, kind):
                return name
        return ""
    return ""
