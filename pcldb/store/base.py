"""What the two engines share: the Store and Db shapes, the SQL text rules, the value rules.

SQL for `Db.execute/query/one/scalar` is written ONCE for both engines:

- `?` marks a parameter. A `?` inside a quoted piece of SQL is refused (SqlTextError): it would
  be a parameter on one engine and plain text on the other.
- `{pcl}` and `{app}` stand for the two schemas.
- `%` is plain text (`LIKE 'raw/%'` works on both engines).
- One statement per call. A second statement after a `;` is refused.

Values. `insert`, `insert_many` and `upsert` check and adapt every value by the kind of its
column (schema_def). What SQLite would accept and PostgreSQL would refuse is refused HERE, with
a PclError that names the column, before either database sees it: a whole number outside the
column's range, True or False where a number belongs, NaN or infinity, text with a NUL
character or half a surrogate pair, a list that holds something other than text. So the two
engines never differ.

Failures. A statement the database refuses raises DatabaseError (IntegrityError when a
constraint said no). After that the transaction is dead on BOTH engines: every further call
raises DatabaseError, and leaving the `with store.app()` block rolls back.
"""

from __future__ import annotations

import contextlib
import datetime
import functools
import importlib
import json
import math
import re
import threading

from .. import util
from ..errors import PclError, Unavailable
from . import schema_def


class DatabaseError(Exception):
    """The database refused a statement. A bug in the calling code, or the database's state."""


class IntegrityError(DatabaseError):
    """A constraint refused the change. `kind`: unique, not_null, foreign_key, check or other."""

    def __init__(self, message, kind="other"):
        super().__init__(message)
        self.kind = kind


class SqlTextError(ValueError):
    """SQL text or parameters handed to Db that cannot be run the same way on both engines."""


# ---------------------------------------------------------------------- SQL text

_PLAIN_RUN = re.compile(r"""[^'"\-/?%{;]+""")


def _quoted_end(sql, start):
    """The index just after the quoted piece that starts at `start` ('...' or "...")."""
    quote = sql[start]
    position = start + 1
    while True:
        position = sql.find(quote, position)
        if position < 0:
            raise SqlTextError("This SQL has a quote that is never closed: %s"
                               % util._show(sql[start:start + 40]))
        if sql.startswith(quote, position + 1):
            position += 2          # a doubled quote is a quote character inside the text
            continue
        return position + 1


@functools.lru_cache(maxsize=2048)
def prepare_sql(sql, engine):
    """(the SQL as the engine's driver wants it, how many parameters it takes).

    SQLite keeps `?`; PostgreSQL (psycopg) gets `%s` and every literal `%` doubled.
    """
    names = schema_def.SCHEMA_NAMES[engine]
    postgres = engine == "postgresql"
    out, count, position, length = [], 0, 0, len(sql)

    def keep(piece):
        out.append(piece.replace("%", "%%") if postgres else piece)

    while position < length:
        match = _PLAIN_RUN.match(sql, position)
        if match:
            out.append(match.group())
            position = match.end()
            continue
        char = sql[position]
        if char in "'\"":
            end = _quoted_end(sql, position)
            piece = sql[position:end]
            if "?" in piece:
                raise SqlTextError(
                    "This SQL has a ? inside quotes (%s). It would be a parameter on one engine "
                    "and plain text on the other. Write ? outside the quotes and pass the text "
                    "as a parameter." % util._show(piece))
            keep(piece)
            position = end
        elif sql.startswith("--", position):
            end = sql.find("\n", position)
            end = length if end < 0 else end
            keep(sql[position:end])
            position = end
        elif sql.startswith("/*", position):
            end = sql.find("*/", position + 2)
            if end < 0:
                raise SqlTextError("This SQL has a /* comment that is never closed.")
            keep(sql[position:end + 2])
            position = end + 2
        elif char == "?":
            if position + 1 < length and sql[position + 1].isdigit():
                raise SqlTextError("Numbered parameters (?1) are not supported; write plain ?.")
            out.append("%s" if postgres else "?")
            count += 1
            position += 1
        elif char == "%":
            out.append("%%" if postgres else "%")
            position += 1
        elif char == ";":
            if sql[position + 1:].strip():
                raise SqlTextError("One statement per call: this SQL goes on after a ';'.")
            position = length      # a closing ';' is dropped
        elif sql.startswith("{pcl}", position):
            out.append(names["pcl"])
            position += 5
        elif sql.startswith("{app}", position):
            out.append(names["app"])
            position += 5
        else:
            out.append(char)       # a lone - / or {
            position += 1
    return "".join(out), count


# ---------------------------------------------------------------------- values

_INT_RANGE = {"int": (-2 ** 31, 2 ** 31 - 1), "bigint": (-2 ** 63, 2 ** 63 - 1)}


def _refuse(where, problem, value=None):
    shown = "" if value is None else " (%s)" % util._show(value, 40)
    return PclError("%s %s%s." % (where, problem, shown), code="bad_value",
                    details={"column": where})


def _clean_text(value, where):
    if not isinstance(value, str):
        raise _refuse(where, "must be text, not %s" % _what(value), value)
    if "\x00" in value:
        raise _refuse(where, "holds a NUL character, which a database cannot store")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise _refuse(where, "holds a broken character (half of a surrogate pair)") from None
    return value


def _what(value):
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "text"
    if isinstance(value, (list, tuple)):
        return "a list"
    if isinstance(value, dict):
        return "a group of values"
    return type(value).__name__


def _has_nul(value):
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, dict):
        return any(_has_nul(key) or _has_nul(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_has_nul(item) for item in value)
    return False


def clean_value(kind, value, where):
    """`value` checked for a column of this kind, in the one form both engines start from.

    None stays None (SQL NULL). text -> str; int/bigint -> int; real -> float; bool -> bool;
    utc -> the fixed-width text; json -> the canonical JSON TEXT; strlist -> list of str.
    `where` names the column in the PclError, for example "pcl.sessions.items".
    """
    if value is None:
        return None
    if kind == "text":
        return _clean_text(value, where)
    if kind in _INT_RANGE:
        if isinstance(value, bool) or not isinstance(value, int):
            raise _refuse(where, "must be a whole number, not %s" % _what(value), value)
        low, high = _INT_RANGE[kind]
        if not low <= value <= high:
            raise _refuse(where, "must be a whole number from %d to %d" % (low, high), value)
        return value
    if kind == "real":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise _refuse(where, "must be a number, not %s" % _what(value), value)
        try:
            number = float(value)
        except OverflowError:
            number = math.inf
        if not math.isfinite(number):
            raise _refuse(where, "must be a finite number, not NaN or infinity", value)
        return number
    if kind == "bool":
        if not isinstance(value, bool):
            raise _refuse(where, "must be true or false, not %s" % _what(value), value)
        return value
    if kind == "utc":
        try:
            return util.utc_text(value)
        except PclError as error:
            raise PclError("%s: %s" % (where, error.message), code="bad_value",
                           details={"column": where}) from None
    if kind == "json":
        try:
            text = util.canonical_json(value)
        except PclError as error:
            raise PclError("%s: %s" % (where, error.message), code="bad_value",
                           details={"column": where}) from None
        if "\\u0000" in text and _has_nul(value):
            raise _refuse(where, "holds a NUL character, which a database cannot store")
        return text
    if kind == "strlist":
        if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
            raise _refuse(where, "must be a list of texts, not %s" % _what(value), value)
        return [_clean_text(item, where + " (item %d)" % (number + 1))
                for number, item in enumerate(value)]
    raise ValueError("unknown kind %r" % (kind,))


def clean_param(value, number):
    """A plain parameter of hand-written SQL, checked the same way for both engines."""
    where = "Parameter %d of this SQL" % number
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return clean_value("bigint", value, where)
    if isinstance(value, float):
        return clean_value("real", value, where)
    if isinstance(value, str):
        return _clean_text(value, where)
    raise TypeError(
        "%s is %s. Pass text, a whole number, a number, True/False or None; for a time use "
        "db.param('utc', value), for JSON db.param('json', value), for a list column "
        "db.param('strlist', value), and for IN (...) write one ? per value."
        % (where, _what(value)))


class Ready:
    """A parameter that db.param() has already made ready for this engine."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def decimal_number(value):
    """A PostgreSQL NUMERIC as SQLite would give it: an int when it has no fraction digits
    (SUM of whole numbers), else a float (AVG)."""
    if value.is_nan() or value.is_infinite():
        return float(value)
    return int(value) if value.as_tuple().exponent >= 0 else float(value)


# ---------------------------------------------------------------------- Db


class Db:
    """One read-write connection inside one transaction. Same behaviour on both engines."""

    engine = None

    def __init__(self):
        self._failed = False
        self._closed = False

    # -- what an engine provides ------------------------------------------------------------

    def _run(self, text, values):
        """Run one statement; return the driver's cursor."""
        raise NotImplementedError

    def _run_many(self, text, rows):
        """Run one statement once per row of values; return how many rows changed."""
        raise NotImplementedError

    def _rows(self, cursor):
        """Every row of a cursor as a dict of normalised values."""
        raise NotImplementedError

    def _encode(self, kind, clean):
        """A clean value (see clean_value) as this engine's driver wants it."""
        raise NotImplementedError

    def _translate(self, error):
        """The engine-neutral exception for a driver error."""
        raise NotImplementedError

    # -- parameters ----------------------------------------------------------------------------

    def param(self, kind, value):
        """A value made ready as a parameter of hand-written SQL: db.param("utc", when),
        db.param("json", obj), db.param("strlist", names). Any kind of schema_def works."""
        if kind not in schema_def.KINDS:
            raise ValueError("unknown kind %r (known: %s)" % (kind, ", ".join(schema_def.KINDS)))
        clean = clean_value(kind, value, "The %s parameter" % kind)
        return Ready(None if clean is None else self._encode(kind, clean))

    def _bind(self, params, expected):
        if isinstance(params, (str, bytes, dict)) or not isinstance(params, (list, tuple)):
            raise SqlTextError("The parameters must be a list or a tuple, one item per ?.")
        if len(params) != expected:
            raise SqlTextError("This SQL has %d ? but %d parameter(s) were given."
                               % (expected, len(params)))
        values = []
        for number, value in enumerate(params, 1):
            if isinstance(value, Ready):
                values.append(value.value)
            elif isinstance(value, datetime.datetime):
                clean = clean_value("utc", value, "Parameter %d of this SQL" % number)
                values.append(self._encode("utc", clean))
            else:
                values.append(clean_param(value, number))
        return values

    # -- running ---------------------------------------------------------------------------------

    def _check_open(self):
        if self._closed:
            raise DatabaseError("This database connection is closed: it was used after its "
                                "'with store.app()' block ended.")
        if self._failed:
            raise DatabaseError("An earlier statement in this transaction failed, so the "
                                "transaction can only be rolled back.")

    def _call(self, function, *args):
        self._check_open()
        try:
            return function(*args)
        except Exception as error:
            translated = self._translate(error)
            if translated is None:
                raise
            self._failed = True
            raise translated from error

    def _statement(self, sql, params):
        if not isinstance(sql, str):
            raise SqlTextError("The SQL must be text.")
        text, expected = prepare_sql(sql, self.engine)
        return self._call(self._run, text, self._bind(params, expected))

    def execute(self, sql, params=()):
        """Run one statement; the number of rows it changed."""
        return self._statement(sql, params).rowcount

    def query(self, sql, params=()):
        """Every row as a dict of normalised values."""
        cursor = self._statement(sql, params)
        return self._call(self._rows, cursor)

    def one(self, sql, params=()):
        """The first row as a dict, or None."""
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def scalar(self, sql, params=()):
        """The first column of the first row, or None."""
        row = self.one(sql, params)
        if not row:
            return None
        return next(iter(row.values()))

    # -- rows of known tables ----------------------------------------------------------------

    def _columns_of(self, table, names):
        unknown = [name for name in names if name not in table.kinds]
        if unknown:
            raise ValueError("%s has no column %s" % (table.qualified, ", ".join(sorted(unknown))))
        return [name for name in table.column_names if name in names]

    def _values_of(self, table, columns, row):
        kinds = table.kinds
        values = []
        for name in columns:
            clean = clean_value(kinds[name], row.get(name), "%s.%s" % (table.qualified, name))
            values.append(None if clean is None else self._encode(kinds[name], clean))
        return values

    @staticmethod
    def _insert_sql(table, columns):
        return "INSERT INTO {%s}.%s (%s) VALUES (%s)" % (
            table.schema, table.name, ", ".join(columns), ", ".join("?" for _ in columns))

    def insert(self, table_name, row):
        """Insert one row (a dict of normalised values). Columns left out become NULL."""
        table = schema_def.table(table_name)
        columns = self._columns_of(table, set(row))
        if not columns:
            raise ValueError("insert into %s needs at least one column" % table.qualified)
        text, _ = prepare_sql(self._insert_sql(table, columns), self.engine)
        return self._call(self._run, text, self._values_of(table, columns, row)).rowcount

    def insert_many(self, table_name, rows):
        """Insert many rows; how many were inserted. A row may leave columns out (NULL)."""
        table = schema_def.table(table_name)
        rows = list(rows)
        if not rows:
            return 0
        names = set()
        for row in rows:
            names.update(row)
        columns = self._columns_of(table, names)
        text, _ = prepare_sql(self._insert_sql(table, columns), self.engine)
        total = 0
        for start in range(0, len(rows), 500):
            batch = [self._values_of(table, columns, row) for row in rows[start:start + 500]]
            total += self._call(self._run_many, text, batch)
        return total

    def upsert(self, table_name, row, key, update=None):
        """Insert a row, or change the row that has the same key.

        `key`: the primary key or one of the table's unique column sets. `update`: the columns
        to overwrite when the row exists; None means every column given that is not in the key;
        an empty list leaves an existing row as it is. Returns the number of rows written.
        """
        table = schema_def.table(table_name)
        key = tuple(key)
        if key != table.primary_key and key not in table.unique:
            raise ValueError("%s has no primary key or unique set %s" % (table.qualified, key))
        columns = self._columns_of(table, set(row))
        missing = [name for name in key if name not in row]
        if missing:
            raise ValueError("upsert into %s needs the key column %s"
                             % (table.qualified, ", ".join(missing)))
        if update is None:
            update = [name for name in columns if name not in key]
        else:
            update = self._columns_of(table, set(update))
            absent = [name for name in update if name not in row]
            if absent:
                raise ValueError("upsert into %s: the row has no value for %s"
                                 % (table.qualified, ", ".join(absent)))
        action = ("DO UPDATE SET " + ", ".join("%s = excluded.%s" % (name, name)
                                                for name in update)) if update else "DO NOTHING"
        sql = "%s ON CONFLICT (%s) %s" % (self._insert_sql(table, columns), ", ".join(key), action)
        text, _ = prepare_sql(sql, self.engine)
        return self._call(self._run, text, self._values_of(table, columns, row)).rowcount

    def delete_where(self, table_name, where, params=()):
        """DELETE FROM the table WHERE ...; the number of rows removed."""
        table = schema_def.table(table_name)
        if not isinstance(where, str) or not where.strip():
            raise ValueError("delete_where needs a condition; to empty a table write '1 = 1'")
        return self.execute("DELETE FROM {%s}.%s WHERE %s" % (table.schema, table.name, where),
                            params)


# ---------------------------------------------------------------------- Store

_blocks = threading.local()


@contextlib.contextmanager
def one_block_at_a_time(database):
    """Refuse a `with store.app()` block opened inside another one for the same database, in the
    same thread. On SQLite the inner block would wait for the outer one's write lock until it
    gives up; so it is refused at once, and on both engines alike."""
    held = getattr(_blocks, "held", None)
    if held is None:
        held = _blocks.held = set()
    if database in held:
        raise DatabaseError(
            "store.app() was opened inside another 'with store.app()' block for the same "
            "database. The inner block would wait for the outer one and never get its turn. "
            "Pass the db you already have to the code that needs it, or close the first block "
            "before opening the next.")
    held.add(database)
    try:
        yield
    finally:
        held.discard(database)


class Store:
    """One database, on one of the two engines. Made by pcldb.store.open_store(settings)."""

    engine = None

    def __init__(self, settings):
        self.settings = settings

    def describe(self):
        """{"engine", "version", "where"}: for people; never a password."""
        raise NotImplementedError

    def is_initialized(self):
        """True when both schemas are there with this program's schema_version."""
        raise NotImplementedError

    def initialize(self):
        """Create everything that is missing. Safe to call again."""
        raise NotImplementedError

    def app(self):
        """`with store.app() as db:` one read-write transaction: committed when the block ends
        well, rolled back when it raises, always closed."""
        raise NotImplementedError

    def app_only(self):
        """`app()` for a short block that writes only the private part (app.*), such as a
        token's "last used" or a row of the query log. The SQLite engine then never waits for
        readers of the catalogue; elsewhere it is `app()`."""
        return self.app()

    def destroy(self):
        """Remove every table and view (the SQLite files; the two PostgreSQL schemas)."""
        raise NotImplementedError

    def reset(self):
        """Remove everything and build it again, empty."""
        self.destroy()
        self.initialize()

    def reader(self):
        """The sandboxed read-only connection for expert SQL (specification section 9)."""
        name = {"sqlite": "reader_sqlite", "postgresql": "reader_postgres"}[self.engine]
        module = importlib.import_module("%s.%s" % (__package__, name))
        return module.open_reader(self)


def not_initialized_error(found=None):
    """The error (Unavailable, code "not_initialized") for a store that is empty or was made by
    another version of the schema."""
    if found is None:
        message = "The database is not set up yet. Run: pcldb setup"
    else:
        message = ("This database is not set up for this version of pcldb (its schema version "
                   "is %s, this program needs %d). To start over with an empty database run: "
                   "pcldb db reset --yes" % (found, schema_def.SCHEMA_VERSION))
    return Unavailable(message, code="not_initialized")


def strlist_text(items):
    """A list of texts as SQLite stores it: a compact JSON array."""
    return json.dumps(list(items), ensure_ascii=False, separators=(",", ":"))
