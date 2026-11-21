"""The sandboxed read-only PostgreSQL connection for expert SQL (specification section 9).

    with store.reader() as reader:                  # one new connection, used once
        columns, rows, truncated = reader.run(sql, max_rows=1000, timeout_seconds=15)

A person types the SQL, so it is hostile until proven otherwise. What it meets, layer by layer:

1. The role. Every statement runs on a NEW connection that signed in as `pcl_reader`: a role
   with no memberships that may connect, see the schema `pcl` and read its tables and views.
   Nothing else (pg_hardening.py). The connection is closed afterwards and never reused, so
   nothing a statement does to its session can reach the next person's query.
2. A read-only transaction: `BEGIN READ ONLY`, then this module's own `SELECT 1` (after the
   first query a transaction can no longer be switched to read-write).
3. One statement. The person's text always travels in the extended query protocol (psycopg
   `execute(..., binary=True)`; binary results exist only in that protocol), where the SERVER
   refuses a second statement ("cannot insert multiple commands into a prepared statement").
   It is never passed to a plain `execute()` without parameters: that would use the simple
   protocol, which runs `COMMIT; anything` happily.
4. Queries only. The text is sent as `DECLARE pcl_check NO SCROLL CURSOR FOR <text>`. The
   server's own grammar then refuses everything that is not a SELECT, VALUES or TABLE: SET,
   SHOW, COPY, DO, LISTEN, COMMIT, PREPARE, INSERT ... RETURNING, SELECT INTO, a WITH that
   writes. Only a text whose first word is EXPLAIN takes a second path (EXPLAIN cannot be a
   cursor): it is sent as it is, still as one statement, read-only, as `pcl_reader`.
5. Small cells. Once the first DECLARE has proved that the text is ONE complete query, the same
   text is declared again inside `SELECT <cut of each column> FROM (<text>) AS pcl_q(c1, ...)`.
   So the server hands over at most a cell's worth of every value (a value of hundreds of
   megabytes never reaches this process), in forms that need no guessing: numbers as numbers,
   everything else as JSON made by the server (lists stay lists, times are ISO 8601).
   Because the text is a complete query on its own, it cannot close the bracket that this
   module opens.
6. Time. The connection's options carry `statement_timeout` (a running statement cannot lift
   it), `transaction_timeout` is set on PostgreSQL 17 and newer, and a watchdog thread asks the
   server to cancel at the deadline (`cancel_safe`) and, if that does not help, closes the
   connection.
7. Size. Rows arrive one at a time (libpq's single-row mode); fetching stops at `max_rows + 1`
   rows or when the rows in hand reach sqlrun.MAX_RESULT_BYTES.

What a reader can still learn (PostgreSQL cannot hide it): the NAMES, column names, sizes and
row estimates of the private tables (never their rows), the text of view definitions, role
names, the server's version, address and port.
"""

from __future__ import annotations

import decimal
import json
import re
import threading
import time

from .. import sqlrun
from ..errors import PclError, Unavailable
from .postgres_engine import load_psycopg

CHECK_CURSOR = "pcl_check"
ROWS_CURSOR = "pcl_rows"
_CHECK_PREFIX = "DECLARE %s NO SCROLL CURSOR FOR " % CHECK_CURSOR

LOCK_TIMEOUT_MS = 3000
WORK_MEM = "16MB"
CANCEL_WAIT_SECONDS = 3.0
HARD_STOP_SECONDS = 5.0         # after the deadline: close the connection whatever it is doing
TRANSACTION_SLACK_MS = 5000

# Result column types (pg_type.oid) by how the wrapped query hands them over.
_PLAIN = frozenset((16, 20, 21, 23, 26, 700, 701))      # bool, int8, int2, int4, oid, floats
_TEXT = frozenset((25, 1043, 19))                       # text, varchar, name
_NUMERIC, _NUMERIC_ARRAY = 1700, 1231

_ONLY_READS = "Expert mode only reads the catalogue. "


def open_reader(store):
    """A new sandboxed reader for ONE statement. Called by `store.reader()`. The connection is
    made when `run` is called, so that it carries that call's time limit from the start."""
    return PostgresReader(store)


def session_options(timeout_ms):
    """The `options` of a reader connection: the limits and the output formats. Set at sign-in,
    they are what RESET ALL returns to, and they win over the role's stored defaults."""
    settings = (
        ("default_transaction_read_only", "on"),
        ("statement_timeout", str(int(timeout_ms))),
        ("lock_timeout", str(LOCK_TIMEOUT_MS)),
        ("idle_in_transaction_session_timeout", str(int(timeout_ms) + TRANSACTION_SLACK_MS)),
        ("work_mem", WORK_MEM),
        ("max_parallel_workers_per_gather", "0"),
        ("jit", "off"),
        ("search_path", "pcl"),
        ("DateStyle", "ISO,YMD"),
        ("IntervalStyle", "postgres"),
        ("TimeZone", "UTC"),
        ("bytea_output", "hex"),
        ("standard_conforming_strings", "on"),
        ("client_min_messages", "warning"),
    )
    return " ".join("-c %s=%s" % item for item in settings)


# ---------------------------------------------------------------------- reading the text

_SPACE = " \t\n\r\f\v"
_DOLLAR = re.compile(r"\$(?:[A-Za-z_\x80-\U0010ffff][A-Za-z_0-9\x80-\U0010ffff]*)?\$")


def _word_char(char):
    return char.isalnum() or char in "_$" or ord(char) >= 0x80


def statement_end(sql):
    """Where the first statement of `sql` ends: the index of the first `;` that is not inside
    a string, a quoted name, a dollar-quoted text or a comment (len(sql) when there is none),
    and whether anything but spaces, comments and further `;` follows it."""
    position, length = 0, len(sql)
    end = None
    more = False
    while position < length:
        char = sql[position]
        if char == "'":
            escapes = (position > 0 and sql[position - 1] in "eE"
                       and (position < 2 or not _word_char(sql[position - 2])))
            position += 1
            while position < length:
                if escapes and sql[position] == "\\":
                    position += 2
                elif sql[position] == "'":
                    if sql.startswith("''", position):
                        position += 2
                    else:
                        break
                else:
                    position += 1
            position += 1
        elif char == '"':
            position += 1
            while position < length:
                if sql[position] == '"':
                    if sql.startswith('""', position):
                        position += 2
                    else:
                        break
                else:
                    position += 1
            position += 1
        elif sql.startswith("--", position):
            while position < length and sql[position] not in "\n\r":
                position += 1
            continue
        elif sql.startswith("/*", position):
            depth = 1
            position += 2
            while position < length and depth:
                if sql.startswith("*/", position):
                    depth -= 1
                    position += 2
                elif sql.startswith("/*", position):
                    depth += 1
                    position += 2
                else:
                    position += 1
            continue
        elif char == "$" and not (position > 0 and _word_char(sql[position - 1])):
            match = _DOLLAR.match(sql, position)
            if match is None:
                position += 1
            else:
                close = sql.find(match.group(), match.end())
                position = length if close < 0 else close + len(match.group())
        elif char == ";":
            if end is None:
                end = position
            position += 1
            continue
        elif char in _SPACE:
            position += 1
            continue
        else:
            position += 1
        if end is not None:
            more = True         # something that is not blank came after the first ';'
    return (length if end is None else end), more


def _code_tokens(sql):
    """The parts of `sql` that are code, in order: ("word", text) for a name or key word as
    typed, ("name", text) for a quoted name, ("param", "$1") for a parameter mark, ("mark", c)
    for any other character. Strings, dollar-quoted texts, comments and spaces are left out.
    The rules are statement_end's."""
    position, length = 0, len(sql)
    while position < length:
        char = sql[position]
        if char == "'":
            escapes = (position > 0 and sql[position - 1] in "eE"
                       and (position < 2 or not _word_char(sql[position - 2])))
            position += 1
            while position < length:
                if escapes and sql[position] == "\\":
                    position += 2
                elif sql[position] == "'":
                    if sql.startswith("''", position):
                        position += 2
                    else:
                        break
                else:
                    position += 1
            position += 1
        elif char == '"':
            close = position + 1
            while True:
                close = sql.find('"', close)
                if close < 0 or not sql.startswith('""', close):
                    break
                close += 2
            end = length if close < 0 else close
            yield "name", sql[position + 1:end].replace('""', '"')
            position = end + 1
        elif sql.startswith("--", position):
            while position < length and sql[position] not in "\n\r":
                position += 1
        elif sql.startswith("/*", position):
            depth = 1
            position += 2
            while position < length and depth:
                if sql.startswith("*/", position):
                    depth -= 1
                    position += 2
                elif sql.startswith("/*", position):
                    depth += 1
                    position += 2
                else:
                    position += 1
        elif char == "$":
            match = _DOLLAR.match(sql, position)
            if match is not None:
                close = sql.find(match.group(), match.end())
                position = length if close < 0 else close + len(match.group())
                continue
            end = position + 1
            while end < length and sql[end].isdigit():
                end += 1
            yield ("param", sql[position:end]) if end > position + 1 else ("mark", char)
            position = end
        elif char.isdigit():
            while position < length and (sql[position].isalnum() or sql[position] in "._"):
                position += 1
        elif _word_char(char):
            end = position
            while end < length and _word_char(sql[end]):
                end += 1
            yield "word", sql[position:end]
            position = end
        elif char in _SPACE:
            position += 1
        else:
            yield "mark", char
            position += 1


def has_parameter_mark(sql):
    """Whether `sql` holds a parameter mark such as $1 outside strings and comments."""
    return any(kind == "param" for kind, _text in _code_tokens(sql))


def has_key_word(sql, *words):
    """Whether one of `words` (lower case) stands in `sql` as a key word: unquoted, outside
    strings and comments."""
    wanted = frozenset(words)
    return any(kind == "word" and text.lower() in wanted for kind, text in _code_tokens(sql))


# The schema's name stands in the server's message as it is, in every language.
_APP_IN_MESSAGE = re.compile(r"(?<![A-Za-z0-9_$])app(?![A-Za-z0-9_$])")


def is_parameter_mark_error(code, sql, session_alive):
    """Whether an error is the server's answer to a parameter mark ($1) that got no value.
    The server calls that a protocol violation (08P01), like a broken connection, and its
    message is in the server's language (lc_messages, which a reader cannot change). So the
    decision rests on what does not depend on the language: after a parameter mark the
    session is still alive, and the text holds the mark."""
    return code == "08P01" and (session_alive or has_parameter_mark(sql))


def is_private_schema_error(code, schema_name, primary):
    """Whether a missing right (42501) is about the private schema app: the server's
    diagnostic says so, or the message names the schema app (the name stands as it is in every
    language). The SQL text is not asked: in "SELECT app.x, pg_read_file(...) FROM t app" the
    `app` is a table's other name, and the missing right is the function's."""
    return code == "42501" and (schema_name == "app"
                                or _APP_IN_MESSAGE.search(primary or "") is not None)


def _line_and_column(text, position):
    """(line, column), both from 1, of the character at `position` (from 1)."""
    before = text[:max(0, position - 1)]
    line = before.count("\n") + 1
    column = len(before) - (before.rfind("\n") + 1) + 1
    return line, column


# ---------------------------------------------------------------------- the reader


class PostgresReader:
    """One read-only connection for one statement. Use it in a `with` block and call `run`
    once; the connection is closed when `run` returns."""

    engine = "postgresql"

    def __init__(self, store):
        self._store = store
        self._psycopg = load_psycopg(getattr(store, "settings", None))
        self._connection = None
        self._used = False
        self._expired = False
        self._lock = threading.Lock()

    # -- connecting -------------------------------------------------------------------------

    def _connect(self, timeout_ms):
        psycopg = self._psycopg
        from psycopg.conninfo import make_conninfo
        info = make_conninfo(self._store.conninfo("reader"),
                             options=session_options(timeout_ms),
                             application_name="pcldb-reader", client_encoding="UTF8")
        try:
            connection = psycopg.connect(info, autocommit=False, prepare_threshold=None)
        except psycopg.Error as error:
            text = str(error)
            if "too many connections" in text or getattr(error, "sqlstate", None) == "53300":
                raise Unavailable(
                    "Too many expert queries are running at this moment. Try again in a few "
                    "seconds.", code="sql_busy") from None
            raise self._store._connect_error("reader", error) from None
        return connection

    # -- the watchdog -----------------------------------------------------------------------

    def _on_deadline(self):
        self._expired = True
        with self._lock:
            connection = self._connection
        if connection is None:
            return
        try:
            connection.cancel_safe(timeout=CANCEL_WAIT_SECONDS)
        except Exception:
            pass

    def _on_hard_stop(self):
        self._expired = True
        with self._lock:
            connection = self._connection
        if connection is None:
            return
        try:
            connection.close()
        except Exception:
            pass

    # -- running ----------------------------------------------------------------------------

    def run(self, sql, max_rows=1000, timeout_seconds=15):
        """Run one statement. Returns (columns, rows, truncated): columns is a list of
        (name, PostgreSQL type name), rows a list of tuples, truncated True when the statement
        had more rows than `max_rows` (or more data than sqlrun.MAX_RESULT_BYTES).

        Values: None, bool, int, float (NaN and the infinities included), decimal.Decimal for
        NUMERIC, text, lists for arrays, parsed JSON for json/jsonb; every other type as the
        text PostgreSQL's to_json gives (times as ISO 8601, bytea as \\x hex). A value longer
        than the cell limit comes as sqlrun.CutText.

        Raises PclError with code "sql_refused", "sql_error" or "sql_timeout".
        """
        if self._used:
            raise RuntimeError("a reader runs one statement; open a new one with store.reader()")
        self._used = True
        if not isinstance(sql, str):
            raise sqlrun.refused("The SQL must be text.")
        if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
            raise ValueError("max_rows must be a whole number of at least 1")
        timeout_seconds = float(timeout_seconds)
        if not timeout_seconds > 0:
            raise ValueError("timeout_seconds must be more than 0")
        if "\x00" in sql:
            raise sqlrun.refused("This SQL holds a NUL character, which a database cannot "
                                 "read. Type the statement again.")
        try:
            sql.encode("utf-8")
        except UnicodeEncodeError:
            raise sqlrun.refused("This SQL holds a broken character (half of a surrogate "
                                 "pair). Type the statement again.") from None

        psycopg = self._psycopg
        timeout_ms = max(1, int(timeout_seconds * 1000))
        deadline = threading.Timer(timeout_seconds, self._on_deadline)
        hard_stop = threading.Timer(timeout_seconds + HARD_STOP_SECONDS, self._on_hard_stop)
        deadline.daemon = hard_stop.daemon = True
        state = {"prefix": 0, "started": time.monotonic()}
        try:
            connection = self._connect(timeout_ms)
            with self._lock:
                self._connection = connection
            deadline.start()
            hard_stop.start()
            connection.read_only = True             # psycopg begins with BEGIN READ ONLY
            cursor = connection.cursor()
            cursor.execute("SELECT 1")              # from here the transaction stays read-only
            if connection.info.server_version >= 170000:
                cursor.execute("SET LOCAL transaction_timeout = %d"
                               % (timeout_ms + TRANSACTION_SLACK_MS))
            if sqlrun.leading_word(sql, "postgresql") == "EXPLAIN":
                return self._run_explain(cursor, sql, max_rows)
            return self._run_query(connection, cursor, sql, max_rows, state)
        except PclError:
            raise
        except psycopg.Error as error:
            raise self._translate(error, sql, timeout_seconds, state) from None
        except (RecursionError, OverflowError, UnicodeError, ValueError) as error:
            if self._expired:
                raise sqlrun.timed_out(timeout_seconds) from None
            raise sqlrun.failed("The result could not be read (%s)."
                                % type(error).__name__) from None
        finally:
            deadline.cancel()
            self.close()            # the hard stop still stands by while this rolls back
            hard_stop.cancel()

    def _run_explain(self, cursor, sql, max_rows):
        cursor.execute(sql.encode("utf-8"), binary=True)       # extended protocol: one statement
        if cursor.description is None:
            return [], [], False
        columns = [(column.name, column.type_display or "") for column in cursor.description]
        fetched = cursor.fetchmany(max_rows + 1)
        rows = []
        for row in fetched[:max_rows]:
            rows.append(tuple(
                value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray))
                else bytes(value).decode("utf-8", "replace") if isinstance(value, memoryview)
                else value for value in row))
        return columns, rows, len(fetched) > max_rows

    def _run_query(self, connection, cursor, sql, max_rows, state):
        cell = self._store.settings.sql.max_cell_chars
        # 1. Prove that the text is one complete query (and learn its columns).
        state["prefix"] = len(_CHECK_PREFIX)
        cursor.execute((_CHECK_PREFIX + sql).encode("utf-8"), binary=True)
        cursor.execute("FETCH 0 FROM " + CHECK_CURSOR)
        description = list(cursor.description or ())
        cursor.execute("CLOSE " + CHECK_CURSOR)
        state["prefix"] = None      # positions of later errors are not positions in the text
        names = [column.name for column in description]
        oids = [int(column.type_code) for column in description]
        types = self._type_names(cursor, oids)

        # 2. The same query again, with every column cut to a cell's worth by the server.
        end, more = statement_end(sql)
        body = sql if more else sql[:end]
        text_limit = cell + 1
        json_limit = 6 * cell + 16      # JSON writes some characters as six
        kinds, parts = [], []
        for number, oid in enumerate(oids, 1):
            name = "c%d" % number
            if oid in _PLAIN:
                kinds.append("plain")
                parts.append(name)
            elif oid in _TEXT:
                kinds.append("text")
                parts.append("pg_catalog.left(%s::text, %d)" % (name, text_limit))
            elif oid == _NUMERIC:
                kinds.append("numeric")
                parts.append("pg_catalog.left(%s::text, %d)" % (name, text_limit))
            else:
                kinds.append("exact" if oid == _NUMERIC_ARRAY else "json")
                parts.append("pg_catalog.left(pg_catalog.to_json(%s)::text, %d)"
                             % (name, json_limit))
        aliases = "(%s)" % ", ".join("c%d" % number for number in range(1, len(oids) + 1)) \
            if oids else ""
        wrapped = "DECLARE %s NO SCROLL CURSOR FOR SELECT %s FROM (\n%s\n) AS pcl_q%s" % (
            ROWS_CURSOR, ", ".join(parts), body, aliases)
        cursor.execute(wrapped.encode("utf-8"), binary=True)

        # 3. Fetch row by row, up to the row limit and the size limit.
        rows, size, truncated = [], 0, False
        stream = connection.cursor().stream(
            "FETCH FORWARD %d FROM %s" % (max_rows + 1, ROWS_CURSOR))
        try:
            for raw in stream:
                if len(rows) >= max_rows:
                    truncated = True
                    break
                row = []
                for kind, value in zip(kinds, raw):
                    if value is not None and kind != "plain":
                        size += len(value)
                        value = _load(kind, value, cell, text_limit, json_limit)
                    else:
                        size += 8
                    row.append(value)
                rows.append(tuple(row))
                if size >= sqlrun.MAX_RESULT_BYTES:
                    truncated = next(stream, None) is not None
                    break
        finally:
            stream.close()
        return list(zip(names, types)), rows, truncated

    @staticmethod
    def _type_names(cursor, oids):
        """PostgreSQL's own names for result column types ("text", "timestamp with time zone",
        "text[]" ...), in order."""
        if not oids:
            return []
        cursor.execute(
            "SELECT pg_catalog.format_type(t.o, NULL) FROM pg_catalog.unnest('{%s}'::oid[]) "
            "WITH ORDINALITY AS t(o, n) ORDER BY t.n" % ",".join(str(oid) for oid in oids))
        return [row[0] or "" for row in cursor.fetchall()]

    # -- errors -----------------------------------------------------------------------------

    def _translate(self, error, sql, timeout_seconds, state):
        """The PclError for a psycopg error: the server's own first line, the position inside
        the person's text when the server gave one, never a stack trace or a connection
        string."""
        psycopg = self._psycopg
        code = getattr(error, "sqlstate", None) or ""
        diag = getattr(error, "diag", None)
        primary = (getattr(diag, "message_primary", None) or "").strip()
        if code == "57014":             # the server cancelled the statement
            # Its statement_timeout, or this reader's deadline, or someone else's cancel. The
            # time that has passed tells them apart in every language.
            started = state.get("started")
            ran = None if started is None else time.monotonic() - started
            if self._expired or (ran is not None and ran >= timeout_seconds - 0.05):
                return sqlrun.timed_out(timeout_seconds)
            return sqlrun.failed("The query was cancelled.")
        if code in ("25P04", "25P03") or (self._expired and not code):
            return sqlrun.timed_out(timeout_seconds)
        if not code and isinstance(error, psycopg.OperationalError):
            return Unavailable("The database connection was lost while the query ran. Check "
                               "that the database is running (pcldb db status) and try again.",
                               code="db_down")
        if not primary:
            primary = str(error).strip().splitlines()[0] if str(error).strip() \
                else type(error).__name__
        said = "PostgreSQL said: %s" % primary
        details = {"sqlstate": code or None}
        position = getattr(diag, "statement_position", None)
        prefix = state.get("prefix")
        if position and prefix is not None:
            try:
                position = int(position) - prefix
            except (TypeError, ValueError):
                position = 0
            if 1 <= position <= len(sql) + 1:
                line, column = _line_and_column(sql, position)
                details.update(position=position, line=line, column=column)
                said += " (line %d, column %d)" % (line, column)
        hint = (getattr(diag, "message_hint", None) or "").strip()
        if hint:
            said += " Hint: %s" % hint.splitlines()[0]
        if code == "42601" and statement_end(sql)[1]:     # more after the first ';'
            return sqlrun.refused("Expert mode runs one statement at a time. Remove "
                                  "everything after the first ';'.", details)
        if is_private_schema_error(code, getattr(diag, "schema_name", None), primary):
            return sqlrun.refused(sqlrun.PRIVATE_PART, details)
        if code in ("42501", "25006"):  # no right to do that; the transaction is read-only
            return sqlrun.refused(_ONLY_READS + said, details)
        if (code == "0A000" and sqlrun.leading_word(sql, "postgresql") == "WITH"
                and has_key_word(sql, "insert", "update", "delete", "merge")):
            return sqlrun.refused(_ONLY_READS + "A WITH part that changes data cannot be run.",
                                  details)
        if code == "42601":
            word = sqlrun.leading_word(sql, "postgresql")
            if word not in sqlrun.FIRST_WORDS["postgresql"] or has_key_word(sql, "into"):
                return sqlrun.refused(
                    _ONLY_READS + "A statement must be a query (SELECT, WITH, VALUES, TABLE "
                    "or EXPLAIN). " + said, details)
            return sqlrun.failed(said, details)
        if code == "53400":
            return sqlrun.refused("This query needs more temporary disk space than expert mode "
                                  "allows. Make it smaller. " + said, details)
        if code.startswith(("53", "54")):
            return sqlrun.refused("This query is larger than the database allows. " + said,
                                  details)
        if is_parameter_mark_error(code, sql, self._session_alive(diag)):
            # A placeholder such as $1 with no value: the server calls that a protocol
            # violation, but the connection is fine and the SQL is what must change.
            return sqlrun.refused(sqlrun.PARAMETER_MARK, details)
        if code.startswith("08") or code.startswith("57P"):
            return Unavailable("The database connection was lost while the query ran. Check "
                               "that the database is running (pcldb db status) and try again.",
                               code="db_down")
        return sqlrun.failed(said, details)

    def _session_alive(self, diag):
        """Whether the server session survived the error: an ERROR (not FATAL or PANIC) on a
        connection that psycopg still holds open. Neither depends on the server's language."""
        severity = getattr(diag, "severity_nonlocalized", None)
        if severity is not None and severity != "ERROR":
            return False
        with self._lock:
            connection = self._connection
        if connection is None:
            return False
        try:
            return not connection.closed and not connection.broken
        except Exception:
            return False

    # -- closing ----------------------------------------------------------------------------

    def close(self):
        with self._lock:
            connection = self._connection
        if connection is None:
            return
        try:
            if not connection.closed and not connection.broken:
                connection.rollback()
        except Exception:
            pass
        with self._lock:
            self._connection = None
        try:
            connection.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, kind, value, trace):
        self.close()
        return False


# ---------------------------------------------------------------------- values


def _load(kind, value, cell, text_limit, json_limit):
    """One cell of the wrapped query as a Python value (see PostgresReader.run)."""
    if kind == "text":
        return sqlrun.CutText(value[:cell]) if len(value) >= text_limit else value
    if kind == "numeric":
        if len(value) >= text_limit:
            return sqlrun.CutText(value[:cell])
        try:
            return decimal.Decimal(value)
        except decimal.InvalidOperation:
            return value
    cut = len(value) >= json_limit
    try:
        if kind == "exact":         # an array of NUMERIC: keep every digit
            return _whole(json.loads(value, parse_float=decimal.Decimal,
                                     parse_int=decimal.Decimal), cut, value, cell)
        return _whole(json.loads(value), cut, value, cell)
    except RecursionError:          # nested deeper than Python's JSON reader goes
        return value if not cut else sqlrun.CutText(value[:cell])
    except ValueError:              # cut by the server, or a number Python will not read
        if not cut:
            return value
    # Not complete JSON: the server cut it. Give back readable text.
    if value.startswith('"'):
        for drop in range(0, 7):        # the cut may have gone through a \uXXXX
            try:
                text = json.loads(value[:len(value) - drop] + '"')
            except ValueError:
                continue
            return sqlrun.CutText(text[:cell])
    return sqlrun.CutText(value[:cell])


def _whole(loaded, cut, value, cell):
    """A parsed value; a NUMBER that fills the whole cell may have lost digits, so it stays
    text and is marked as cut."""
    if cut and isinstance(loaded, (int, float, decimal.Decimal)) \
            and not isinstance(loaded, bool):
        return sqlrun.CutText(value[:cell])
    return loaded
