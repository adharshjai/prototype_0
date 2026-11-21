"""Moving data between the two engines, and the catalogue as one file.

- `dump(store, path)` writes every table as JSON Lines: one header line, then one line per row
  (`{"table": "pcl.sessions", "row": {...}}`) in the store's normalised values (true/false,
  the UTC time text, JSON values, lists). Tables come in the schema's order and rows in the
  order of their key, so two stores with the same rows give the same lines whatever their
  engine is. The file is plain ASCII.
- `restore(store, path)` loads a dump into an empty, initialised store of either engine, in
  one transaction. Users and tokens are skipped unless `include_users=True`. A dump of another
  schema version, a store that already holds data, a row that does not fit and a file that was
  cut short are all refused, and then nothing is written.
- `export_sqlite(store, path)` writes just the catalogue (`pcl`: everything expert mode may
  read; no users, tokens or logs, but operator names and notes) as one SQLite file with the same tables and views the SQLite engine
  uses. The file opens in any SQLite tool, on ARC too: `sqlite3 pcl_catalog.sqlite`.

A dump never holds a token: the store only ever keeps the SHA-256 of each token. It does hold
user names and whatever people typed into notes, so it is written for this account only.
"""

from __future__ import annotations

import json
import os
import secrets
import sqlite3
import sys
import time
from pathlib import Path

from . import __version__, util
from .errors import Conflict, PclError
from .store import IntegrityError, schema_def
from .store import base as store_base

DUMP_FORMAT = "pcldb-dump/1"

# Skipped by restore unless include_users is set.
USER_TABLES = ("app.users", "app.tokens")

# Rows that a restore merges into what is there instead of asking for an empty table: the
# small facts (the schema version itself is never overwritten) and the log of questions.
_MERGED_TABLES = ("pcl.meta", "app.meta", "app.query_log")

_BATCH = 500
_REPLACE_TRIES = 10
_REPLACE_WAIT_SECONDS = 0.1


def _show(value, limit=120):
    try:
        text = json.dumps(value, ensure_ascii=True)
    except (TypeError, ValueError):
        text = ascii(value)
    except RecursionError:
        text = "(a deeply nested value)"
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _line(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                      allow_nan=False)


def _portable(value):
    """A JSON value as BOTH engines hand it back. PostgreSQL keeps numbers exactly, so a whole
    number that Python writes with an exponent (1e+16 and larger) comes back from it as an
    integer; SQLite keeps the text and gives the float. The dump writes the integer for both,
    so the same rows give the same lines."""
    if isinstance(value, float):
        return int(value) if value.is_integer() and abs(value) >= 1e16 else value
    if isinstance(value, dict):
        return {key: _portable(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_portable(item) for item in value]
    return value


def _key_order(table):
    def order(row):
        return tuple((row[name] is not None, row[name]) for name in table.primary_key)
    return order


def _refuse_own_file(store, path):
    """Never write over the database itself (the SQLite engine's two files)."""
    for own in (getattr(store, "catalog_path", None), getattr(store, "app_path", None)):
        if own is not None and _same_file(own, path):
            raise PclError("%s is one of the database's own files. Give another file name."
                           % _show(str(path)), code="bad_file")


def _same_file(first, second):
    try:
        return os.path.samefile(first, second)
    except OSError:
        return (os.path.normcase(os.path.abspath(first))
                == os.path.normcase(os.path.abspath(second)))


def read_tables(db, schema=None):
    """{"pcl.sessions": [row, ...], ...}: every row of every table (of one schema, or of both),
    in normalised values, each table sorted by its key."""
    found = {}
    for table in schema_def.TABLES:
        if schema is not None and table.schema != schema:
            continue
        rows = db.query("SELECT %s FROM {%s}.%s" % (", ".join(table.column_names), table.schema,
                                                    table.name))
        rows.sort(key=_key_order(table))
        found[table.qualified] = rows
    return found


# ---------------------------------------------------------------------- dump


def dump(store, path, *, include_users=True):
    """Write every table of the store as JSON Lines to `path` (replaced when it exists).

    Returns {"file", "engine", "schema_version", "tables": {name: rows}, "rows": total}. With
    `include_users=False` the users and token hashes are left out of the file.
    """
    path = Path(path)
    if path.is_dir():
        raise PclError("%s is a folder. Give the name of the file to write, for example "
                       "pcl_dump.jsonl." % _show(str(path)), code="bad_file")
    _refuse_own_file(store, path)
    with store.app() as db:
        tables = read_tables(db)
    if not include_users:
        for name in USER_TABLES:
            tables[name] = []
    counts = {name: len(rows) for name, rows in tables.items()}
    header = {"format": DUMP_FORMAT, "schema_version": schema_def.SCHEMA_VERSION,
              "engine": store.engine, "pcldb_version": __version__,
              "made_utc": util.now_utc_text(), "includes_users": bool(include_users),
              "tables": counts}
    lines = [_line(header)]
    for name, rows in tables.items():
        json_columns = [column.name for column in schema_def.table(name).columns
                        if column.kind == "json"]
        for row in rows:
            for column in json_columns:
                row[column] = _portable(row[column])
            try:
                lines.append(_line({"table": name, "row": row}))
            except (TypeError, ValueError) as error:
                raise PclError("A row of %s cannot be written as JSON (%s). The dump was not "
                               "written." % (name, error), code="bad_value") from None
    util.write_text_atomic(path, "\n".join(lines) + "\n", mode=0o600)
    return {"file": str(path), "engine": store.engine,
            "schema_version": schema_def.SCHEMA_VERSION, "tables": counts,
            "rows": sum(counts.values())}


# ---------------------------------------------------------------------- restore


def _refuse_constant(name):
    raise ValueError("it holds %s, which is not valid JSON" % name)


def _parse(text, number, shown):
    try:
        return json.loads(text, parse_constant=_refuse_constant)
    except RecursionError:
        raise PclError("Line %d of %s is nested too deeply to be read. Nothing was restored."
                       % (number, shown), code="bad_dump") from None
    except ValueError as error:
        raise PclError("Line %d of %s is not valid JSON (%s). The file is not a pcldb dump, or "
                       "it was damaged. Nothing was restored." % (number, shown, error),
                       code="bad_dump") from None


def _check_header(header, shown):
    if not isinstance(header, dict) or header.get("format") != DUMP_FORMAT:
        raise PclError("%s is not a pcldb dump: its first line does not say %s. Give a file "
                       "that pcldb dump wrote." % (shown, DUMP_FORMAT), code="bad_dump")
    version = header.get("schema_version")
    if isinstance(version, bool) or version != schema_def.SCHEMA_VERSION:
        raise PclError("%s was made with schema version %s; this version of pcldb works with "
                       "schema version %d. Restore it with the pcldb that made it. Nothing was "
                       "restored." % (shown, _show(version), schema_def.SCHEMA_VERSION),
                       code="dump_version", details={"found": version if isinstance(
                           version, (int, str)) and not isinstance(version, bool) else None})
    counts = header.get("tables")
    if not isinstance(counts, dict) or not all(
            isinstance(count, int) and not isinstance(count, bool) and count >= 0
            for count in counts.values()):
        raise PclError("The first line of %s does not say how many rows each table has, so the "
                       "file cannot be checked. Nothing was restored." % shown, code="bad_dump")
    unknown = sorted(name for name in counts if name not in schema_def.TABLE_BY_NAME)
    if unknown:
        raise PclError("%s names the table %s, which this version of pcldb does not have. "
                       "Nothing was restored." % (shown, _show(unknown[0])), code="bad_dump")
    return counts


def read_dump(path):
    """(header, {table: [row, ...]}) of a dump file, as written (nothing is checked against
    the schema beyond the header). For tools and tests; `restore` reads line by line."""
    path = Path(path)
    shown = _show(path.name)
    tables = {table.qualified: [] for table in schema_def.TABLES}
    header = None
    for number, text, item in _lines(path, shown):
        if header is None:
            header = item
            _check_header(header, shown)
            continue
        _check_row_line(item, number, shown)
        tables[item["table"]].append(item["row"])
    if header is None:
        raise PclError("%s is empty; it is not a pcldb dump." % shown, code="bad_dump")
    return header, tables


def _lines(path, shown):
    """(line number, text, parsed value) for every line of the file that is not blank."""
    try:
        handle = open(path, encoding="utf-8-sig", newline="\n")
    except FileNotFoundError:
        raise PclError("The dump file %s was not found." % _show(str(path)),
                       code="file_missing") from None
    except OSError as error:
        raise PclError("The dump file %s could not be opened (%s)."
                       % (shown, error.strerror or error), code="bad_file") from None
    with handle:
        try:
            for number, text in enumerate(handle, 1):
                if text.strip():
                    yield number, text, _parse(text, number, shown)
        except UnicodeDecodeError:
            raise PclError("%s is not UTF-8 text, so it is not a pcldb dump. Nothing was "
                           "restored." % shown, code="bad_dump") from None


def _check_row_line(item, number, shown):
    if (not isinstance(item, dict) or set(item) != {"table", "row"}
            or not isinstance(item["table"], str) or not isinstance(item["row"], dict)):
        raise PclError('Line %d of %s is not a row of a pcldb dump (it must be {"table": ..., '
                       '"row": {...}}). Nothing was restored.' % (number, shown), code="bad_dump")
    if item["table"] not in schema_def.TABLE_BY_NAME:
        raise PclError("Line %d of %s names the table %s, which this version of pcldb does not "
                       "have. Nothing was restored." % (number, shown, _show(item["table"])),
                       code="bad_dump")


def _check_row(table, row, number, shown):
    """Refuse a row that the table cannot hold, naming the line and the column."""
    kinds = table.kinds
    unknown = sorted(name for name in row if name not in kinds)
    if unknown:
        raise PclError("Line %d of %s has the column %s, which the table %s does not have. "
                       "Nothing was restored." % (number, shown, _show(unknown[0]),
                                                  table.qualified), code="bad_dump")
    for column in table.columns:
        where = "%s.%s" % (table.qualified, column.name)
        try:
            clean = store_base.clean_value(column.kind, row.get(column.name), where)
        except PclError as error:
            raise PclError("Line %d of %s does not fit: %s Nothing was restored."
                           % (number, shown, error.message), code="bad_dump",
                           details={"line": number, "column": where}) from None
        if clean is None and not column.nullable:
            raise PclError("Line %d of %s does not fit: %s is empty, but every row needs a "
                           "value there. Nothing was restored." % (number, shown, where),
                           code="bad_dump", details={"line": number, "column": where})


SETUP_ADMIN_NAME = "admin"     # the user that pcldb setup and pcldb db reset make


def _setup_admin_only(db):
    """The user row when the only user is the admin that pcldb setup made (named admin, role
    admin) and every token belongs to it; else None. Such a database counts as having no users
    of its own, so `restore --include-users` may replace that admin with the dump's users."""
    users = db.query("SELECT user_id, name, role FROM {app}.users")
    if len(users) != 1 or users[0]["name"] != SETUP_ADMIN_NAME or users[0]["role"] != "admin":
        return None
    others = db.scalar("SELECT COUNT(*) FROM {app}.tokens WHERE user_id <> ?",
                       [users[0]["user_id"]])
    return None if others else users[0]


def _require_empty(db, include_users):
    """Refuse a store that holds data. With include_users, a store whose only user is the admin
    that setup made is accepted: returns that user's row (restore replaces it), else None."""
    setup_admin = None
    if include_users:
        setup_admin = _setup_admin_only(db)
    must_be_empty = [table.qualified for table in schema_def.TABLES
                     if table.qualified not in _MERGED_TABLES
                     and (include_users or table.qualified not in USER_TABLES)
                     and not (setup_admin is not None and table.qualified in USER_TABLES)]
    for name in must_be_empty:
        table = schema_def.table(name)
        count = db.scalar("SELECT COUNT(*) FROM {%s}.%s" % (table.schema, table.name))
        if count:
            if name in USER_TABLES:
                raise Conflict(
                    "This database already has %d row(s) in %s (users other than the admin "
                    "that pcldb setup made), so the users of the dump cannot be restored into "
                    "it. Restore without --include-users to keep the users that are here, or "
                    "start from an empty database (everything in it is removed): pcldb db reset "
                    "--yes, then the same restore again" % (count, name), code="not_empty",
                    details={"table": name})
            raise Conflict(
                "This database is not empty: %s has %d row(s). A dump is restored into an "
                "empty database only, so nothing was restored. To empty this one (everything "
                "in it is removed) run: pcldb db reset --yes" % (name, count),
                code="not_empty", details={"table": name})
    return setup_admin


def _drop_setup_admin(db, user):
    """Remove the setup-made admin and its tokens. Returns the SHA-256 of each token."""
    hashes = [row["token_sha256"] for row in db.query(
        "SELECT token_sha256 FROM {app}.tokens WHERE user_id = ?", [user["user_id"]])]
    db.delete_where("app.tokens", "user_id = ?", [user["user_id"]])
    db.delete_where("app.users", "user_id = ?", [user["user_id"]])
    return hashes


def restore(store, path, *, include_users=False):
    """Load a dump into an empty, initialised store, in one transaction.

    `app.users` and `app.tokens` are skipped unless `include_users=True`. With it, a store
    whose only user is the admin that pcldb setup made counts as empty: that admin and its
    tokens are removed in the same transaction and the dump's users take their place. Returns
    {"file", "dump_engine", "made_utc", "tables": {name: rows written}, "rows": total,
    "skipped": {name: rows left out}, "replaced_setup_admin": bool, "replaced_token_sha256":
    [the removed tokens' SHA-256]}. Any refusal is a PclError and leaves the store as it was.
    """
    path = Path(path)
    shown = _show(path.name)
    written = {table.qualified: 0 for table in schema_def.TABLES}
    seen = dict(written)
    skipped = {}
    header = None
    replaced_tokens = None
    with store.app() as db:
        expected = None
        pending_table, pending = None, []

        def flush():
            nonlocal pending_table, pending
            if not pending:
                return
            table = schema_def.table(pending_table)
            try:
                if pending_table in ("pcl.meta", "app.meta"):
                    for row in pending:
                        db.upsert(pending_table, row, key=table.primary_key)
                elif pending_table == "app.query_log":
                    for row in pending:
                        db.upsert(pending_table, row, key=table.primary_key, update=[])
                else:
                    db.insert_many(pending_table, pending)
            except IntegrityError as error:
                raise PclError(
                    "The dump %s cannot be restored: the table %s refused a row (%s). Most "
                    "often the file holds the same row twice. Nothing was restored."
                    % (shown, pending_table, error), code="bad_dump",
                    details={"table": pending_table}) from None
            written[pending_table] += len(pending)
            pending_table, pending = None, []

        for number, _text, item in _lines(path, shown):
            if header is None:
                header = item
                expected = _check_header(header, shown)
                # A dump without users replaces nobody: the users that are here stay.
                wants_users = include_users and expected.get("app.users", 0) > 0
                setup_admin = _require_empty(db, wants_users)
                if setup_admin is not None:
                    replaced_tokens = _drop_setup_admin(db, setup_admin)
                continue
            _check_row_line(item, number, shown)
            name, row = item["table"], item["row"]
            seen[name] += 1
            if name in USER_TABLES and not include_users:
                skipped[name] = skipped.get(name, 0) + 1
                continue
            if name in ("pcl.meta", "app.meta") and row.get("key") == "schema_version":
                continue
            _check_row(schema_def.table(name), row, number, shown)
            if name != pending_table or len(pending) >= _BATCH:
                flush()
                pending_table = name
            pending.append(row)
        if header is None:
            raise PclError("%s is empty; it is not a pcldb dump." % shown, code="bad_dump")
        flush()
        for name, count in sorted(expected.items()):
            if seen[name] != count:
                raise PclError(
                    "The dump %s is not complete: its first line promises %d row(s) of %s, but "
                    "the file holds %d. It was cut short or changed. Nothing was restored."
                    % (shown, count, name, seen[name]), code="bad_dump",
                    details={"table": name, "expected": count, "found": seen[name]})
        extra = sorted(name for name, count in seen.items() if count and name not in expected)
        if extra:
            raise PclError("The dump %s holds rows of %s that its first line does not list. "
                           "It was changed after it was written. Nothing was restored."
                           % (shown, extra[0]), code="bad_dump", details={"table": extra[0]})
    return {"file": str(path), "dump_engine": header.get("engine"),
            "made_utc": header.get("made_utc"), "tables": written,
            "rows": sum(written.values()), "skipped": skipped,
            "replaced_setup_admin": replaced_tokens is not None,
            "replaced_token_sha256": replaced_tokens or []}


# ---------------------------------------------------------------------- the catalogue as one file


def _sqlite_value(kind, value, where):
    """A normalised value as the SQLite engine stores it."""
    clean = store_base.clean_value(kind, value, where)
    if clean is None:
        return None
    if kind == "bool":
        return int(clean)
    if kind == "strlist":
        return store_base.strlist_text(clean)
    return clean


def _replace(temporary, path):
    """os.replace that waits a moment when Windows says the target is in use."""
    for attempt in range(_REPLACE_TRIES):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES - 1:
                raise PclError("%s could not be written: another program has the file open. "
                               "Close it and try again." % _show(str(path)), code="file_in_use",
                               status=503) from None
            time.sleep(_REPLACE_WAIT_SECONDS)


def export_sqlite(store, path):
    """Write the catalogue (schema `pcl`: tables and views, nothing private) as one SQLite
    file at `path`, from either engine. An existing file is replaced.

    Returns {"file", "tables": {name: rows}, "rows": total, "bytes": size of the file}.
    """
    path = Path(path)
    if path.is_dir():
        raise PclError("%s is a folder. Give the name of the file to write, for example "
                       "pcl_catalog_export.sqlite." % _show(str(path)), code="bad_file")
    _refuse_own_file(store, path)
    with store.app() as db:
        tables = read_tables(db, "pcl")
    meta = tables["pcl.meta"]
    if not any(row["key"] == "schema_version" for row in meta):
        meta.append({"key": "schema_version", "value": str(schema_def.SCHEMA_VERSION)})
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        raise PclError("The folder for %s could not be made (%s)."
                       % (_show(str(path)), error.strerror or error), code="bad_file") from None
    temporary = path.with_name(".%s.%s.tmp" % (path.name, secrets.token_hex(4)))
    counts = {}
    try:
        try:
            connection = sqlite3.connect(os.fspath(temporary), isolation_level=None)
        except sqlite3.Error as error:
            raise PclError("%s could not be written (%s)." % (_show(str(path)), error),
                           code="bad_file") from None
        if os.name != "nt":
            os.chmod(temporary, 0o600)     # operator names and notes: for this account only
        try:
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("BEGIN")
            for statement in schema_def.ddl("sqlite", "pcl", qualified=False):
                connection.execute(statement)
            for name, rows in tables.items():
                table = schema_def.table(name)
                columns = table.column_names
                kinds = table.kinds
                connection.executemany(
                    "INSERT INTO %s (%s) VALUES (%s)" % (table.name, ", ".join(columns),
                                                         ", ".join("?" for _ in columns)),
                    [[_sqlite_value(kinds[column], row.get(column), "%s.%s" % (name, column))
                      for column in columns] for row in rows])
                counts[table.name] = len(rows)
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            raise PclError("%s could not be written (%s)." % (_show(str(path)), error),
                           code="bad_file") from None
        finally:
            connection.close()
        _replace(temporary, path)
    finally:
        for leftover in (temporary, temporary.with_name(temporary.name + "-journal")):
            try:
                leftover.unlink()
            except OSError:
                pass
    return {"file": str(path), "tables": counts, "rows": sum(counts.values()),
            "bytes": path.stat().st_size}


# ---------------------------------------------------------------------- the command line


def _say(text="", stream=None):
    """One ASCII line (Windows consoles): anything else is written as an escape."""
    stream = stream or sys.stdout
    stream.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")


def _typed(path):
    """A path as a person types it in a command: in quotes when it holds a space."""
    return '"%s"' % path if any(character.isspace() for character in path) else path


def _file_argument(args, example):
    path = getattr(args, "file", None)
    if not path:
        raise PclError("Give the file name, for example: %s" % example, code="usage")
    return os.fspath(path)


def cmd_dump(args, settings, *, out=None):
    """`pcldb dump FILE`: every table as JSON Lines. Returns 0."""
    from .store import open_store
    path = _file_argument(args, "pcldb dump pcl_dump.jsonl")
    result = dump(open_store(settings), path)
    filled = sum(1 for count in result["tables"].values() if count)
    _say("Wrote %s: %d rows from %d tables (engine %s, schema version %d)."
         % (path, result["rows"], filled, result["engine"], result["schema_version"]), out)
    _say("The file holds no token (only the SHA-256 of each), but it does hold user names and "
         "whatever was typed into notes. Keep it private.", out)
    _say("Load it into an empty database of either engine with: pcldb restore %s"
         % _typed(path), out)
    return 0


def cmd_restore(args, settings, *, out=None):
    """`pcldb restore FILE [--include-users]`: load a dump into an empty database. Returns 0."""
    from .store import open_store
    path = _file_argument(args, "pcldb restore pcl_dump.jsonl")
    include_users = bool(getattr(args, "include_users", False))
    store = open_store(settings)
    result = restore(store, path, include_users=include_users)
    tables = result["tables"]
    _say("Restored %s (made %s on the %s engine) into the %s database: %d rows."
         % (path, result["made_utc"] or "at an unknown time", result["dump_engine"] or "unknown",
            store.engine, result["rows"]), out)
    _say("Sessions: %d. Files: %d. Runs: %d. Points: %d. Search entries: %d."
         % (tables["pcl.sessions"], tables["pcl.files"], tables["pcl.runs"],
            tables["pcl.points"], tables["app.search_entries"]), out)
    if result["replaced_setup_admin"]:
        removed = _remove_token_files(settings, result["replaced_token_sha256"])
        _say("The user \"%s\" that pcldb setup made was replaced by the users of the dump "
             "(%d user(s), %d token(s))." % (SETUP_ADMIN_NAME, tables["app.users"],
                                             tables["app.tokens"]), out)
        if removed:
            _say("Its token no longer works, so these files were removed: %s"
                 % ", ".join(removed), out)
        _say("The dump's tokens work again where they were saved. To make a new token: pcldb "
             "token new --user <name>   (the names: pcldb user list)", out)
    left_out = sum(result["skipped"].values())
    if left_out:
        _say("Users and tokens were left out (%d rows). The users of this database stay as "
             "they are. To bring the dump's users along, add --include-users (it replaces the admin "
             "that pcldb setup made when that is the only user)." % left_out, out)
    return 0


def _remove_token_files(settings, hashes):
    """Delete var/admin_token.txt and var/tokens/*.txt whose token is one of `hashes` (tokens
    that no longer exist). Returns the names of the files removed."""
    if not hashes:
        return []
    from . import auth
    wanted = set(hashes)
    var_dir = settings.var_dir
    paths = [var_dir / "admin_token.txt"]
    try:
        paths += sorted(path for path in (var_dir / auth.TOKEN_FOLDER).iterdir()
                        if path.suffix == ".txt" and path.is_file())
    except OSError:
        pass
    removed = []
    for path in paths:
        try:
            token = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if token and auth.token_sha256(token) in wanted:
            try:
                path.unlink()
                removed.append(str(path))
            except OSError:
                pass
    return removed


def cmd_export_sqlite(args, settings, *, out=None):
    """`pcldb export-sqlite FILE`: the catalogue as one SQLite file. Returns 0."""
    from .store import open_store
    path = _file_argument(args, "pcldb export-sqlite pcl_catalog_export.sqlite")
    result = export_sqlite(open_store(settings), path)
    tables = result["tables"]
    _say("Wrote %s (%d bytes): the catalogue as one SQLite file." % (path, result["bytes"]), out)
    _say("Sessions: %d. Files: %d. Runs: %d. Points: %d. It holds no users, no tokens and no "
         "logs, but it does hold operator names and notes: keep it inside the lab's project "
         "folder." % (tables["sessions"], tables["files"], tables["runs"],
                                tables["points"]), out)
    _say('Open it with any SQLite tool, for example: sqlite3 %s "SELECT COUNT(*) FROM sessions"'
         % _typed(path), out)
    return 0
