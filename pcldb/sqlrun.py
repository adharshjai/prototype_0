"""Expert mode: run one SQL statement that a person typed, read-only (specification section 9).

    result = sqlrun.run_sql(store, "SELECT folder FROM sessions", settings=settings,
                            user_name="demo-operator-a")
    # {"columns": [{"name", "type"}], "rows": [[...]], "row_count", "truncated", "elapsed_ms"}

What keeps the statement from changing or seeing anything it should not is the DATABASE, never
this module:

- PostgreSQL: a login role that can only read the schema `pcl`, a read-only transaction, the
  extended protocol (one statement), a time limit (pcldb/store/reader_postgres.py and
  pg_hardening.py).
- SQLite: a connection that has only the catalogue file, opened read-only, with an authorizer
  that allows reading and nothing else (pcldb/store/reader_sqlite.py).

What this module adds is courtesy and bookkeeping:

- A few checks that give a clear sentence before the database is asked (empty text, too long,
  not a query). They are NOT the security boundary; a statement that slips past them still
  meets the database's own refusal.
- Every value is turned into something JSON can carry (`json_value`), and long cells are cut.
- One row in the audit table `app.query_log` per call, written through the app connection
  (never through the reader), for successes and for failures. A result is only handed back
  after its audit row was written.

Errors a person can cause are PclError with one of these codes: `sql_refused` (expert mode said
no), `sql_error` (the database's own message, with the position when the database gave one),
`sql_timeout` (stopped at the time limit).
"""

from __future__ import annotations

import csv
import datetime
import decimal
import io
import json
import math
import sys
import threading
import time
import uuid

from . import util
from .errors import PclError, Unavailable
from .store import schema_def

# What a statement may start with (after spaces and comments). A courtesy check: see above.
FIRST_WORDS = {
    "sqlite": ("SELECT", "WITH", "VALUES", "EXPLAIN"),
    "postgresql": ("SELECT", "WITH", "VALUES", "TABLE", "EXPLAIN"),
}

# A reader stops fetching when the rows it holds reach this size, and reports "truncated".
# It keeps one hostile statement from filling the backend's memory with a few huge rows.
MAX_RESULT_BYTES = 32 * 1024 * 1024

# How many expert queries one backend process runs at the same time, and how long another one
# waits for a place. One hostile statement can keep a processor busy, and the database's
# temporary space and memory in use, until the time limit; this bounds how many do so at once.
# (The PostgreSQL role's connection limit, pg_hardening.CONNECTION_LIMIT, is a little higher.)
MAX_CONCURRENT = 4
SLOT_WAIT_SECONDS = 3.0
_slots = threading.BoundedSemaphore(MAX_CONCURRENT)

# Whole numbers beyond this lose digits in a browser (JavaScript numbers), so they go as text.
_EXACT_INT = 2 ** 53

_CUT_MARK = "... [cut: %d characters in all]"
_CUT_MARK_BYTES = "... [cut: %d bytes in all]"
_CUT_MARK_LONGER = "... [cut: the value is longer]"


class CutText(str):
    """A text value that the database itself already cut short (the PostgreSQL reader asks the
    server for at most a cell's worth of every value, so a huge value never travels). The full
    length is not known."""

    __slots__ = ()

_LOG_ERROR_CHARS = 2000


# ---------------------------------------------------------------------- the three errors


# The same sentence on both engines for a statement that names the private part.
PRIVATE_PART = ("Expert mode only reads the catalogue. The private part of the database (app: "
                "users, tokens and logs) cannot be read here.")

# The same sentence on both engines for SQL with a placeholder ($1, ?, :name).
PARAMETER_MARK = ("This SQL has a parameter mark (?, :name, @name or $name). Write the value "
                  "itself instead.")


def refused(message, details=None):
    """Expert mode said no (the statement is not allowed, or it is larger than the limits)."""
    return PclError(message, code="sql_refused", status=400, details=details)


def failed(message, details=None):
    """The database's own complaint about the statement (a typing mistake, an unknown name)."""
    return PclError(message, code="sql_error", status=400, details=details)


def timed_out(seconds):
    """The statement ran into the time limit and was stopped."""
    return PclError(
        "The query was stopped after %s seconds, the time limit for expert mode. Make it "
        "smaller (add a WHERE or a LIMIT) and try again." % _plain_number(seconds),
        code="sql_timeout", status=400, details={"timeout_seconds": seconds})


def _plain_number(value):
    number = float(value)
    return str(int(number)) if number == int(number) else ("%.3f" % number).rstrip("0")


# ---------------------------------------------------------------------- reading the first word

_SPACE = {"sqlite": " \t\n\r\f\v", "postgresql": " \t\n\r\f\v"}


def leading_word(sql, engine):
    """The first word of a statement in capitals, after spaces and comments; "" when the text
    starts with anything else.

    The comment rules are each engine's own, so that this function and the database always
    agree on where the statement starts: `--` to the end of the line on both; `/* */` that may
    hold further `/* */` inside on PostgreSQL, and that ends at the first `*/` on SQLite.
    """
    space = _SPACE[engine]
    nested = engine == "postgresql"
    line_ends = "\n\r" if nested else "\n"
    position, length = 0, len(sql)
    while position < length:
        char = sql[position]
        if char in space:
            position += 1
        elif sql.startswith("--", position):
            position += 2
            while position < length and sql[position] not in line_ends:
                position += 1
        elif sql.startswith("/*", position):
            depth = 1
            position += 2
            while position < length and depth:
                if sql.startswith("*/", position):
                    depth -= 1
                    position += 2
                elif nested and sql.startswith("/*", position):
                    depth += 1
                    position += 2
                else:
                    position += 1
            if depth:
                return ""          # a comment that never ends: there is no statement
        else:
            break
    end = position
    while end < length and (sql[end].isascii() and (sql[end].isalnum() or sql[end] == "_")):
        end += 1
    word = sql[position:end]
    if not word or word[0].isdigit():
        return ""
    return word.upper()


def check_sql_text(sql, settings, engine):
    """The courtesy checks. Raises PclError(code "sql_refused") with a sentence a person can
    act on. NOT the security boundary: the database refuses on its own whatever gets past."""
    if not isinstance(sql, str):
        raise refused("The SQL must be text.")
    limit = settings.sql.max_sql_chars
    if len(sql) > limit:
        raise refused("This SQL is %d characters long; expert mode takes at most %d."
                      % (len(sql), limit), {"max_sql_chars": limit})
    if "\x00" in sql:
        raise refused("This SQL holds a NUL character, which a database cannot read. "
                      "Type the statement again.")
    try:
        sql.encode("utf-8")
    except UnicodeEncodeError:
        raise refused("This SQL holds a broken character (half of a surrogate pair). "
                      "Type the statement again.") from None
    if not sql.strip():
        raise refused("There is no SQL to run. Type one statement that starts with SELECT.")
    word = leading_word(sql, engine)
    allowed = FIRST_WORDS[engine]
    if word not in allowed:
        raise refused(
            "Expert mode only reads the catalogue. A statement must start with %s%s."
            % (_word_list(allowed),
               " (this one starts with %s)" % word if word else ""),
            {"first_word": word, "allowed": list(allowed)})


def _word_list(words):
    return ", ".join(words[:-1]) + " or " + words[-1]


# ---------------------------------------------------------------------- values to JSON


def _sound(text):
    """Text without half surrogate pairs (they cannot be sent as UTF-8)."""
    if text.isascii():
        return text
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return text.encode("utf-8", "replace").decode("utf-8")
    return text


def _cut(text, limit):
    if isinstance(text, CutText):
        return _sound(str(text[:limit])) + _CUT_MARK_LONGER
    if len(text) <= limit:
        return _sound(text)
    return _sound(text[:limit]) + _CUT_MARK % len(text)


def _nested(value):
    """A value inside a list or a JSON document, made JSON-safe (no cutting at this level)."""
    if value is None or value is True or value is False:
        return value
    kind = type(value)
    if kind is str:
        return _sound(value)
    if kind is int:
        return value if -_EXACT_INT <= value <= _EXACT_INT else str(value)
    if kind is float:
        return value if math.isfinite(value) else _float_word(value)
    if isinstance(value, dict):
        return {_sound(key if isinstance(key, str) else str(key)): _nested(item)
                for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_nested(item) for item in value]
    return _scalar_text(value)


def _float_word(value):
    if value != value:
        return "NaN"
    return "Infinity" if value > 0 else "-Infinity"


def _scalar_text(value):
    """The text for a value that JSON has no kind for."""
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "\\x" + bytes(value).hex()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, bool):
        return value
    if isinstance(value, int):          # a subclass of int
        return _nested(int(value))
    if isinstance(value, float):
        return _nested(float(value))
    try:
        return _sound(str(value))
    except Exception:                   # a value whose own text form fails: say what it was
        return "<%s>" % type(value).__name__


def json_value(value, max_cell_chars):
    """One cell of a result as a value JSON can carry (the conversion table of section 9).

    None, true/false, text and ordinary numbers stay as they are. A whole number beyond 2^53
    becomes text (a browser would lose digits). NaN and the infinities become the texts "NaN",
    "Infinity", "-Infinity". Decimal numbers become text (no digit is lost). Dates and times
    become ISO text. Bytes become `\\x` and hex digits. Lists stay lists, JSON documents stay
    as they are (with the same rules for what they hold). Anything else becomes its text.
    A text cell longer than `max_cell_chars` is cut and marked; a list or document whose JSON
    text would be longer than that becomes that cut text. Half surrogate pairs (they cannot be
    sent as UTF-8) become question marks.
    """
    if value is None or value is True or value is False:
        return value
    kind = type(value)
    if kind is str or kind is CutText:
        return _cut(value, max_cell_chars)
    if kind is int:
        return value if -_EXACT_INT <= value <= _EXACT_INT else str(value)
    if kind is float:
        return value if math.isfinite(value) else _float_word(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        data = bytes(value)
        keep = max(0, (max_cell_chars - 2) // 2)
        if len(data) <= keep:
            return "\\x" + data.hex()
        return "\\x" + data[:keep].hex() + _CUT_MARK_BYTES % len(data)
    if isinstance(value, (dict, list, tuple)):
        try:
            clean = _nested(value)
            text = json.dumps(clean, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        except (RecursionError, ValueError, TypeError):
            return _cut(str(value), max_cell_chars)
        if len(text) > max_cell_chars:
            return _cut(text, max_cell_chars)
        return clean
    converted = _scalar_text(value)
    return _cut(converted, max_cell_chars) if isinstance(converted, str) else converted


# ---------------------------------------------------------------------- running


def _clamp_rows(max_rows, settings):
    limit = settings.sql.max_rows
    if max_rows is None:
        return limit
    if isinstance(max_rows, bool) or not isinstance(max_rows, int) or max_rows < 1:
        raise PclError("max_rows must be a whole number of at least 1.", code="bad_request")
    return min(max_rows, limit)


def _storable(text, limit):
    """Text as the audit table can hold it: no NUL, no broken character, not endless."""
    text = text.replace("\x00", "\\x00")
    text = text.encode("utf-8", "replace").decode("utf-8")
    if len(text) > limit:
        text = text[:limit] + _CUT_MARK % len(text)
    return text


def _write_log(store, settings, *, user_name, sql, max_rows, ok, error_text, n_rows,
               elapsed_ms):
    row = {
        "log_id": util.new_id(),
        "at_utc": util.now_utc_text(),
        "user_name": _storable(user_name, 200) if isinstance(user_name, str) else None,
        "mode": "sql",
        "query_text": _storable(sql if isinstance(sql, str) else repr(sql),
                                settings.sql.max_sql_chars),
        "request_json": {"max_rows": max_rows},
        "ok": bool(ok),
        "error_text": None if error_text is None else _storable(error_text, _LOG_ERROR_CHARS),
        "n_rows": n_rows,
        "elapsed_ms": max(0, min(int(elapsed_ms), 2 ** 31 - 1)),
        "backend": store.engine,
    }
    with store.app_only() as db:
        db.insert("app.query_log", row)


def run_sql(store, sql, *, settings, max_rows=None, user_name=None):
    """Run one statement a person typed, read-only, and return the result ready for JSON.

    `max_rows`: at most this many rows come back (never more than the setting sql.max_rows);
    `truncated` says whether the statement had more. `user_name`: who asked, for the audit row
    (None for the local command line).

    Returns {"columns": [{"name", "type"}], "rows": [[...]], "row_count", "truncated",
    "elapsed_ms"}. Raises PclError: code `sql_refused`, `sql_error` or `sql_timeout` for what
    the person typed; `sql_busy` (503) when too many expert queries run at this moment;
    `db_busy` / `db_down` kinds (503) when the database does not answer.
    """
    started = time.monotonic()
    limit = None
    try:
        limit = _clamp_rows(max_rows, settings)
        check_sql_text(sql, settings, store.engine)
        if not _slots.acquire(timeout=SLOT_WAIT_SECONDS):
            raise Unavailable("Too many expert queries are running at this moment. Try again "
                              "in a few seconds.", code="sql_busy")
        try:
            with store.reader() as reader:
                columns, rows, truncated = reader.run(
                    sql, max_rows=limit, timeout_seconds=settings.sql.timeout_seconds)
        finally:
            _slots.release()
        cell = settings.sql.max_cell_chars
        result = {
            "columns": [{"name": name, "type": kind or ""} for name, kind in columns],
            "rows": [[json_value(value, cell) for value in row] for row in rows],
            "row_count": len(rows),
            "truncated": bool(truncated),
        }
    except PclError as error:
        elapsed_ms = int((time.monotonic() - started) * 1000)
        try:
            _write_log(store, settings, user_name=user_name, sql=sql, max_rows=limit, ok=False,
                       error_text="%s: %s" % (error.code, error.message), n_rows=None,
                       elapsed_ms=elapsed_ms)
        except Exception:
            pass        # the person gets the first error; a second one would only hide it
        raise
    result["elapsed_ms"] = int((time.monotonic() - started) * 1000)
    # The audit row comes first: no result leaves without one.
    _write_log(store, settings, user_name=user_name, sql=sql, max_rows=limit, ok=True,
               error_text=None, n_rows=result["row_count"], elapsed_ms=result["elapsed_ms"])
    return result


def history(db, user_name, limit=50):
    """A person's newest SQL queries, newest first, as GET /api/v1/sql/history sends them:
    [{"at_utc", "query_text", "ok", "n_rows", "elapsed_ms", "error_text"}]. `db` is an app
    connection (`with store.app() as db`). `user_name` None lists the local command line's."""
    limit = max(1, min(int(limit), 500))
    columns = "at_utc, query_text, ok, n_rows, elapsed_ms, error_text"
    if user_name is None:
        return db.query("SELECT " + columns + " FROM {app}.query_log WHERE mode = 'sql' "
                        "AND user_name IS NULL ORDER BY at_utc DESC, log_id DESC LIMIT ?",
                        [limit])
    return db.query("SELECT " + columns + " FROM {app}.query_log WHERE mode = 'sql' "
                    "AND user_name = ? ORDER BY at_utc DESC, log_id DESC LIMIT ?",
                    [user_name, limit])


# ---------------------------------------------------------------------- a self-test

# (statement, True when it must run / False when it must be stopped). The refusals here do not
# depend on anything a database administrator has to grant or revoke.
_SELF_TEST = {
    "sqlite": (
        ("SELECT COUNT(*) FROM sessions", True),
        ("INSERT INTO sessions (path) VALUES ('pcldb-self-test')", False),
        ("SELECT 1; SELECT 2", False),
        ("ATTACH DATABASE 'pcldb_self_test.sqlite' AS other", False),
        ("PRAGMA database_list", False),
        ("SELECT name FROM users", False),
    ),
    "postgresql": (
        ("SELECT COUNT(*) FROM sessions", True),
        ("INSERT INTO pcl.meta (key, value) VALUES ('pcldb-self-test', 'x')", False),
        ("SELECT 1; SELECT 2", False),
        ("COMMIT", False),
        ("CREATE TEMP TABLE pcldb_self_test (x int)", False),
        ("SELECT name FROM app.users", False),
    ),
}


def sandbox_self_test(store, timeout_seconds=5):
    """Run one harmless query and five statements that must be stopped, straight through the
    reader (no audit rows). For `pcldb doctor`.

    Returns [{"sql", "must_run", "ok", "outcome"}]: `ok` is True when the statement did what
    it must (ran, or was stopped); `outcome` is "ran" or the code and message of the refusal.
    Raises PclError only when the database cannot be reached at all."""
    report = []
    for sql, must_run in _SELF_TEST[store.engine]:
        try:
            with store.reader() as reader:
                reader.run(sql, max_rows=1, timeout_seconds=timeout_seconds)
        except PclError as error:
            if error.code not in ("sql_refused", "sql_error", "sql_timeout"):
                raise
            report.append({"sql": sql, "must_run": must_run, "ok": not must_run,
                           "outcome": "%s: %s" % (error.code, error.message)})
        else:
            report.append({"sql": sql, "must_run": must_run, "ok": must_run, "outcome": "ran"})
    return report


# ---------------------------------------------------------------------- the schema browser


def describe(store):
    """What expert mode can read, for the schema browser (GET /api/v1/sql/schema):
    {"engine", "schema": "pcl", "objects": [{"name", "kind": "table"|"view", "description",
    "columns": [{"name", "type", "nullable", "description"}]}]}. Types are the engine's own."""
    engine = store.engine
    objects = []
    for item in schema_def.relations("pcl"):
        objects.append({
            "name": item.name,
            "kind": "view" if item.is_view else "table",
            "description": item.description,
            "columns": [{"name": column.name,
                         "type": schema_def.type_name(engine, column.kind),
                         "nullable": bool(column.nullable),
                         "description": column.description}
                        for column in item.columns],
        })
    return {"engine": engine, "schema": "pcl", "objects": objects}


# ---------------------------------------------------------------------- the command line

_TABLE_CELL_CHARS = 60


def _console_text(value):
    """One cell for the aligned table: one line, ASCII only, not endless."""
    if value is None:
        return "NULL"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=True, separators=(",", ":"))
    else:
        text = str(value)
    text = text.encode("unicode_escape").decode("ascii") if not text.isascii() or \
        any(ord(char) < 32 for char in text) else text
    if len(text) > _TABLE_CELL_CHARS:
        text = text[:_TABLE_CELL_CHARS - 3] + "..."
    return text


def format_table(result):
    """The result as an aligned text table (ASCII only), with a closing line that counts."""
    names = [_console_text(column["name"]) for column in result["columns"]]
    rows = [[_console_text(value) for value in row] for row in result["rows"]]
    numeric = [bool(result["rows"]) and all(
        isinstance(row[index], (int, float)) and not isinstance(row[index], bool)
        or row[index] is None for row in result["rows"]) for index in range(len(names))]
    widths = [len(name) for name in names]
    for row in rows:
        for index, text in enumerate(row):
            widths[index] = max(widths[index], len(text))
    lines = []
    if names:
        lines.append("  ".join(name.ljust(width) for name, width in zip(names, widths)).rstrip())
        lines.append("  ".join("-" * width for width in widths))
        for row in rows:
            lines.append("  ".join(
                text.rjust(width) if right else text.ljust(width)
                for text, width, right in zip(row, widths, numeric)).rstrip())
    count = result["row_count"]
    if result["truncated"]:
        lines.append("(the first %d row%s; the query has more)"
                     % (count, "" if count == 1 else "s"))
    else:
        lines.append("(%d row%s)" % (count, "" if count == 1 else "s"))
    return "\n".join(lines) + "\n"


def format_csv(result):
    """The result as CSV text (a header line, then the rows; lists and documents as JSON)."""
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow([column["name"] for column in result["columns"]])
    for row in result["rows"]:
        writer.writerow(["" if value is None
                         else ("true" if value is True else "false") if isinstance(value, bool)
                         else json.dumps(value, ensure_ascii=False, separators=(",", ":"))
                         if isinstance(value, (dict, list)) else value
                         for value in row])
    return buffer.getvalue()


def _write(stream, text):
    try:
        stream.write(text)
    except UnicodeEncodeError:
        # A console that cannot show a character (an old Windows code page): escape it.
        encoding = getattr(stream, "encoding", None) or "ascii"
        stream.write(text.encode(encoding, "backslashreplace").decode(encoding))


def _write_data(stream, text):
    """Data meant for a file or another program (CSV): UTF-8 when the output is not a console,
    so `pcldb sql --csv > out.csv` writes the same bytes on Windows (whose redirected output
    otherwise uses the ANSI code page and loses every other character) as on a Mac."""
    buffer = getattr(stream, "buffer", None)
    try:
        console = stream.isatty()
    except (AttributeError, ValueError, OSError):
        console = True
    if buffer is None or console:
        _write(stream, text)
        return
    stream.flush()
    buffer.write(text.encode("utf-8"))
    buffer.flush()


def cmd_sql(args, settings):
    """`pcldb sql "select ..." [--csv|--json] [--max-rows N]`: expert mode in the terminal,
    through the same sandbox as the website. Prints an aligned table, or CSV, or JSON."""
    from .store import open_store

    if getattr(args, "as_csv", False) and getattr(args, "as_json", False):
        raise PclError("Choose one of --csv and --json, not both.", code="bad_request")
    store = open_store(settings)
    result = run_sql(store, args.sql, settings=settings,
                     max_rows=getattr(args, "max_rows", None), user_name=None)
    if getattr(args, "as_json", False):
        _write(sys.stdout, json.dumps(result, ensure_ascii=True, indent=2) + "\n")
    elif getattr(args, "as_csv", False):
        _write_data(sys.stdout, format_csv(result))
        if result["truncated"]:
            _write(sys.stderr, "Only the first %d rows were written; the query has more.\n"
                   % result["row_count"])
    else:
        _write(sys.stdout, format_table(result))
    return 0
