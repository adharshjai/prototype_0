"""Loading the nightly index: one `sessions.sqlite` made by `pcl_index build`, into the store.

The lab's nightly job walks the folders on ARC and writes everything it found into one SQLite
file (schema name `pcl-index/1`): the sessions, their files, the runs made for them
(classifications, reviews, corrections), the models, the problems, and one roll-up row per
session. `load_index` brings that file into the database of either engine:

1. The file is opened read-only and checked: its `meta.schema`, every table and column that is
   needed (`INDEX_COLUMNS`). A missing one is named in the refusal.
2. Every value is converted to the kinds the store works with: 0/1 becomes true/false, JSON text
   becomes values, the times stay the millisecond text form. All rows are checked HERE, before
   anything is written, so a row that cannot be stored is refused with its session or run named
   and the database stays as it was. A confidence or threshold that is not a finite number
   (NaN, infinity) is stored as empty and counted.
3. In the caller's transaction: every session of the index is written (a session that is
   already registered keeps who registered it, when, how, and where its files are), its files
   are replaced, and the tables that are always a full rebuild (`runs`, `points`, `reviews`,
   `models`, `problems`, `session_rollup`) are replaced as a whole. The current model's name is
   copied, and every Search entry is brought up to date.

Sessions that are in the database but not in the index are left alone and reported.

Demo data and real data never mix: while the demo is loaded, an index is refused.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import sys
from pathlib import Path

from . import registration, search_entries, util
from .errors import Conflict, PclError
from .store import base as store_base
from .store import schema_def
from .store.sqlite_engine import file_uri

INDEX_SCHEMA = "pcl-index/1"

# What an index file must hold: table -> the columns that are read. Other tables and columns
# of the file are ignored (its own `session_samples` and `session_tags` tables, for example:
# here they are views over the lists of each session).
INDEX_COLUMNS = {
    "meta": ("key", "value"),
    "sessions": (
        "path", "instrument", "area", "folder", "session_name", "session_id", "is_copy",
        "started_utc", "ended_utc", "simulated", "sample_ids", "operator", "location", "method",
        "items", "items_planned", "completion", "notes", "tags", "format", "uploaded_utc",
        "upload_id", "has_card", "card_json", "corrections_json", "corrected", "withdrawn",
        "bytes", "n_files", "sums_sha256", "verified_utc", "verify_ok"),
    "files": ("session_path", "rel_path", "bytes", "sha256", "listed"),
    "runs": (
        "path", "instrument", "area", "folder", "kind", "made_utc", "session_path",
        "stated_session_path", "session_id", "sums_sha256", "derived_from", "tool_name",
        "tool_version", "model_name", "model_sha256", "runner", "slurm_job_id", "run_by",
        "set_json", "why", "run_json", "valid"),
    "points": (
        "run_path", "point_file", "point_id", "capture_id", "label", "top_class", "confidence",
        "outcome", "needs_review", "threshold", "reason", "raw_sha256", "scores_json"),
    "reviews": (
        "run_path", "row_no", "reviewed_run", "point_id", "capture_id", "status", "label",
        "reviewer", "time_utc", "comment"),
    "models": (
        "name", "path", "sha256", "classes_json", "threshold", "files_json", "is_current",
        "model_json"),
    "problems": ("problem_no", "path", "code", "detail", "fix", "session_path"),
    "session_rollup": (
        "session_path", "labels", "label_counts", "models", "problems", "review_state",
        "n_needs_review", "last_run_utc"),
}

# The tables that are a full rebuild every night: replaced as a whole, in this order.
DERIVED_TABLES = ("runs", "points", "reviews", "models", "problems", "session_rollup")

# What names a row in a message: table -> (the word, the columns).
_ROW_NAMES = {
    "sessions": ("session", ("path",)),
    "files": ("file", ("session_path", "rel_path")),
    "runs": ("run", ("path",)),
    "points": ("point", ("run_path", "point_file")),
    "reviews": ("review", ("run_path", "row_no")),
    "models": ("model", ("name",)),
    "problems": ("problem", ("problem_no",)),
    "session_rollup": ("summary row of session", ("session_path",)),
}

# The session columns that the index does not know: they are kept when the session is already
# registered and filled in here when it is new.
_KEPT_COLUMNS = ("upload_json", "collection_id", "data_path", "storage_path", "globus_url",
                 "web_url", "subject", "registered_utc", "registered_by", "source")

MAX_META_CHARS = 200
LOCAL_ACTOR = "local-cli"


class _Problem(Exception):
    """One value that cannot be stored; the row loop adds which row it was."""

    def __init__(self, column, text):
        super().__init__(text)
        self.column = column
        self.text = text


def _show(value, limit=120):
    """A short, safe (ASCII) picture of a value for a message."""
    try:
        text = json.dumps(value, ensure_ascii=True)
    except (TypeError, ValueError):
        text = ascii(value)
    except RecursionError:
        text = "(a deeply nested value)"
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _what(value):
    if isinstance(value, bytes):
        return "raw bytes"
    if isinstance(value, bool):
        return "true or false"
    if isinstance(value, (int, float)):
        return "a number"
    if isinstance(value, str):
        return "text"
    return type(value).__name__


def _actor_name(actor):
    if actor is None or isinstance(actor, str):
        return actor
    return getattr(actor, "name", None) or str(actor)


# ---------------------------------------------------------------------- reading the file


def _open_index(sqlite_path):
    """A read-only connection to the index file, or a PclError that says what is wrong."""
    path = Path(sqlite_path)
    shown = _show(path.name)
    if not path.is_file():
        raise PclError("The index file %s was not found. Give the sessions.sqlite that "
                       "pcl_index build wrote (on ARC: index/sessions.sqlite)." % _show(str(path)),
                       code="file_missing")
    try:
        connection = sqlite3.connect(file_uri(path, "ro"), uri=True, isolation_level=None)
    except sqlite3.Error as error:
        raise PclError("The index file %s could not be opened (%s)." % (shown, error),
                       code="bad_index") from None
    try:
        connection.execute("PRAGMA query_only=ON")
        # The file comes from outside: nothing in its own definitions (views, triggers) gets
        # to run functions with side effects.
        connection.execute("PRAGMA trusted_schema=OFF")
        connection.execute("PRAGMA cell_size_check=ON")
        connection.execute("SELECT COUNT(*) FROM sqlite_master").fetchone()
    except sqlite3.Error as error:
        connection.close()
        raise PclError("%s is not a SQLite index file (%s). Give the sessions.sqlite that "
                       "pcl_index build wrote." % (shown, error), code="bad_index") from None
    return connection


def _check_shape(connection, shown):
    """Refuse a file that lacks a needed table or column, naming it. Returns the meta values."""
    tables = {row[0] for row in connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'")}
    if "meta" not in tables:
        raise PclError("%s is not a nightly index: it has no table named meta. Give the "
                       "sessions.sqlite that pcl_index build wrote." % shown, code="bad_index",
                       details={"missing_table": "meta"})
    for table, wanted in INDEX_COLUMNS.items():
        if table not in tables:
            raise PclError("The index file %s has no table named %s, which is needed. Build "
                           "the index again with pcl_index build; nothing was loaded."
                           % (shown, table), code="bad_index", details={"missing_table": table})
        present = {row[1] for row in connection.execute('PRAGMA table_info("%s")' % table)}
        missing = [name for name in wanted if name not in present]
        if missing:
            raise PclError("The table %s of the index file %s has no column named %s, which "
                           "is needed. Build the index again with pcl_index build; nothing was "
                           "loaded." % (table, shown, ", ".join(missing)), code="bad_index",
                           details={"table": table, "missing_columns": missing})
    meta = {}
    for key, value in connection.execute("SELECT key, value FROM meta"):
        if isinstance(key, str):
            meta[key] = value
    found = meta.get("schema")
    if found != INDEX_SCHEMA:
        raise PclError("The index file %s says its schema is %s; this version of pcldb loads "
                       "%s only. Nothing was loaded."
                       % (shown, "missing" if found is None else _show(found), INDEX_SCHEMA),
                       code="unknown_schema",
                       details={"found": found if isinstance(found, str) else None})
    return meta


def _refuse_constant(name):
    raise ValueError("it holds %s, which is not valid JSON" % name)


def _json_value(column, value):
    if not isinstance(value, str):
        raise _Problem(column, "must be JSON text, but the file holds %s" % _what(value))
    try:
        return json.loads(value, parse_constant=_refuse_constant)
    except RecursionError:
        raise _Problem(column, "is nested too deeply to be read") from None
    except ValueError as error:
        raise _Problem(column, "is not valid JSON (%s)" % error) from None


def _cleaned(kind, column, value):
    """The store's own check of a value, with its refusal turned into a _Problem."""
    try:
        store_base.clean_value(kind, value, column)
    except PclError as error:
        text = error.message
        if text.startswith(column + " "):
            text = text[len(column) + 1:]
        elif text.startswith(column + ": "):
            text = text[len(column) + 2:]
        raise _Problem(column, text.rstrip(".")) from None
    return value


def _convert(kind, column, value, counts):
    """One value of the index file as the store's normalised kind. None stays None."""
    if value is None:
        return None
    if isinstance(value, bytes):
        raise _Problem(column, "holds raw bytes, which cannot be loaded")
    if kind == "text":
        if not isinstance(value, str):
            raise _Problem(column, "must be text, but the file holds %s (%s)"
                           % (_what(value), _show(value, 40)))
        return _cleaned(kind, column, value)
    if kind in ("int", "bigint"):
        if isinstance(value, bool) or not isinstance(value, int):
            raise _Problem(column, "must be a whole number, but the file holds %s (%s)"
                           % (_what(value), _show(value, 40)))
        return _cleaned(kind, column, value)
    if kind == "real":
        # Every real column of the index is a confidence or a threshold. NaN and infinity
        # would compare differently on the two engines, so they are stored as empty.
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            try:
                number = float(value)
            except OverflowError:
                number = math.inf
            if math.isfinite(number):
                return number
        counts["not_finite"] += 1
        return None
    if kind == "bool":
        if isinstance(value, int) and value in (0, 1):
            return bool(value)
        raise _Problem(column, "must be 0 or 1, but the file holds %s" % _show(value, 40))
    if kind == "utc":
        if not isinstance(value, str):
            raise _Problem(column, "must be a UTC time text, but the file holds %s (%s)"
                           % (_what(value), _show(value, 40)))
        try:
            return util.utc_text(value)
        except PclError as error:
            raise _Problem(column, "is not a time that can be stored: %s"
                           % error.message.rstrip(".")) from None
    if kind == "json":
        return _cleaned(kind, column, _json_value(column, value))
    if kind == "strlist":
        items = _json_value(column, value)
        if not isinstance(items, list) or not all(isinstance(item, str) for item in items):
            raise _Problem(column, "must be a JSON list of texts, not %s" % _show(items, 60))
        return _cleaned(kind, column, items)
    raise ValueError("unknown kind %r" % (kind,))


def _row_name(table, values):
    word, columns = _ROW_NAMES[table]
    shown = []
    for name in columns:
        value = values.get(name)
        shown.append(_show(value) if not isinstance(value, bytes) else "(raw bytes)")
    return "%s %s" % (word, ", ".join(shown))


def _read_table(connection, table, counts, shown):
    """Every row of one index table as a dict of normalised values."""
    columns = INDEX_COLUMNS[table]
    definition = schema_def.table("pcl." + table)
    kinds = definition.kinds
    nullable = {column.name: column.nullable for column in definition.columns}
    try:
        raw_rows = connection.execute(
            'SELECT %s FROM "%s"' % (", ".join('"%s"' % name for name in columns), table)).fetchall()
    except sqlite3.Error as error:
        raise PclError("The table %s of the index file %s could not be read (%s). Nothing was "
                       "loaded." % (table, shown, error), code="bad_index") from None
    rows, seen = [], set()
    key_columns = definition.primary_key
    for raw in raw_rows:
        values = dict(zip(columns, raw))
        row = {}
        try:
            for name in columns:
                converted = _convert(kinds[name], name, values[name], counts)
                if converted is None and not nullable[name]:
                    raise _Problem(name, "is empty, but every row needs a value there")
                row[name] = converted
        except _Problem as problem:
            raise PclError(
                "The index file %s cannot be loaded: in its table %s, the %s has a value that "
                "does not fit: %s %s. Nothing was loaded; the database is as it was."
                % (shown, table, _row_name(table, values), problem.column, problem.text),
                code="bad_index_value",
                details={"table": table, "column": problem.column,
                         "row": {name: values.get(name) for name in _ROW_NAMES[table][1]
                                 if isinstance(values.get(name), (str, int))}}) from None
        key = tuple(row[name] for name in key_columns)
        if key in seen:
            raise PclError("The index file %s cannot be loaded: its table %s holds the %s "
                           "twice. Build the index again; nothing was loaded."
                           % (shown, table, _row_name(table, values)), code="bad_index_value",
                           details={"table": table})
        seen.add(key)
        rows.append(row)
    return rows


def read_index(sqlite_path):
    """Read and check a whole index file without touching any database.

    Returns {"meta": {key: value}, "tables": {table: [row, ...]}, "not_finite": n} with every
    row in normalised values. Raises a PclError that names what is wrong.
    """
    connection = _open_index(sqlite_path)
    shown = _show(Path(sqlite_path).name)
    counts = {"not_finite": 0}
    try:
        try:
            meta = _check_shape(connection, shown)
            tables = {table: _read_table(connection, table, counts, shown)
                      for table in INDEX_COLUMNS if table != "meta"}
        except sqlite3.Error as error:
            raise PclError("The index file %s could not be read (%s). Nothing was loaded."
                           % (shown, error), code="bad_index") from None
    finally:
        connection.close()
    return {"meta": meta, "tables": tables, "not_finite": counts["not_finite"]}


# ---------------------------------------------------------------------- checks across rows


def _meta_text(value):
    """A meta value of the file as a short, storable text, or None."""
    if not isinstance(value, str) or not value.strip():
        return None
    text = "".join(character for character in value.strip()
                   if ord(character) >= 32 and ord(character) != 127)
    if not registration.storable_text(text):
        return None
    return text[:MAX_META_CHARS] or None


def _meta_time(value):
    try:
        return util.utc_text(value)
    except PclError:
        return None


def _check_references(tables, shown):
    paths = set()
    for row in tables["sessions"]:
        problem = registration.path_problem(row["path"], "The session path")
        if problem:
            raise PclError("The index file %s cannot be loaded: %s Nothing was loaded."
                           % (shown, problem), code="bad_index_value",
                           details={"table": "sessions", "column": "path"})
        paths.add(row["path"])
    for table, word in (("files", "file"), ("session_rollup", "summary row")):
        for row in tables[table]:
            if row["session_path"] not in paths:
                raise PclError(
                    "The index file %s cannot be loaded: its table %s has a %s for the session "
                    "%s, which is not in its table sessions. Build the index again; nothing "
                    "was loaded." % (shown, table, word, _show(row["session_path"])),
                    code="bad_index_value", details={"table": table})
    return paths


def _doc_hash(row, files):
    """What a registration document of the same folder would hash to, or None without a card."""
    if not row["has_card"] or not isinstance(row["card_json"], dict):
        return None
    try:
        return registration.document_hash(row["path"], row["card_json"], files,
                                          row["sums_sha256"])
    except PclError:
        return None


def _refuse_mixing(db, source):
    demo_flag = db.scalar("SELECT value FROM {pcl}.meta WHERE key = ?", ["demo"]) == "1"
    if source == "demo":
        real = db.scalar("SELECT COUNT(*) FROM {pcl}.sessions WHERE source <> ?", ["demo"])
        if real:
            raise Conflict("The database holds %d session(s) that are not demo data. Demo data "
                           "and real data never mix, so the demo was not loaded. Use an empty "
                           "database for the demo (pcldb db reset --yes removes everything)."
                           % real, code="real_data_present")
        return
    demo_rows = db.scalar("SELECT COUNT(*) FROM {pcl}.sessions WHERE source = ?", ["demo"])
    if demo_flag or demo_rows:
        raise Conflict("The demo data is loaded, and demo data and real data never mix. "
                       "Remove the demo data first: pcldb demo clear", code="demo_loaded")


# ---------------------------------------------------------------------- the load


def load_index(db, sqlite_path, *, actor, settings, source="index"):
    """Load a nightly index file into the database, inside the caller's transaction.

    Returns the counts: {"file", "schema", "source", "built_utc", "tool", "current_model",
    "sessions", "sessions_created", "sessions_updated", "sessions_unchanged", "files", "runs",
    "points", "reviews", "models", "problems", "session_rollup", "not_finite" (confidences and
    thresholds stored as empty), "not_in_index" (sorted paths of sessions the database has and
    the index does not; they are left alone), "search": {"changed", "unchanged", "removed"}}.

    Everything is read and checked before the first write. Any refusal is a PclError; let it
    leave the `with store.app()` block and the database is exactly as it was.
    """
    if source not in registration.SOURCES:
        raise PclError("source must be one of %s, not %s."
                       % (", ".join(registration.SOURCES), _show(source)), code="bad_source")
    _refuse_mixing(db, source)
    shown = _show(Path(sqlite_path).name)
    index = read_index(sqlite_path)
    tables, meta = index["tables"], index["meta"]
    index_paths = _check_references(tables, shown)

    files_of = {}
    for row in tables["files"]:
        files_of.setdefault(row["session_path"], []).append(row)

    existing = {row["path"]: row for row in db.query("SELECT * FROM {pcl}.sessions")}
    now = util.now_utc_text()
    actor_name = _actor_name(actor)
    index_columns = INDEX_COLUMNS["sessions"]
    subjects = {row["subject"]: path for path, row in existing.items()}
    to_write, audit = [], []
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    for row in tables["sessions"]:
        path = row["path"]
        old = existing.get(path)
        full = dict(row)
        doc_sha256 = _doc_hash(row, files_of.get(path, []))
        if old is None:
            try:
                where = search_entries.location_values(path, settings)
            except PclError as error:
                raise PclError("The index file %s cannot be loaded: the session %s has no "
                               "address on ARC with these settings (%s) Nothing was loaded."
                               % (shown, _show(path), error.message), code="bad_index_value",
                               details={"table": "sessions", "column": "path"}) from None
            other = subjects.get(where["subject"])
            if other is not None and other != path:
                raise Conflict("The index file %s cannot be loaded: its session %s is the same "
                               "folder on ARC as the session %s. One folder is one session; "
                               "nothing was loaded." % (shown, _show(path), _show(other)),
                               code="same_folder")
            subjects[where["subject"]] = path
            full.update(where)
            full.update(upload_json=None, registered_utc=now, registered_by=actor_name,
                        source=source, updated_utc=now, doc_sha256=doc_sha256)
            counts["created"] += 1
            audit.append((path, "created", doc_sha256))
        else:
            for name in _KEPT_COLUMNS:
                full[name] = old[name]
            full["doc_sha256"] = old["doc_sha256"] if old["doc_sha256"] else doc_sha256
            if (all(row[name] == old[name] for name in index_columns)
                    and full["doc_sha256"] == old["doc_sha256"]):
                counts["unchanged"] += 1
                continue
            full["updated_utc"] = now
            counts["updated"] += 1
            audit.append((path, "updated", full["doc_sha256"]))
        to_write.append(full)

    # Everything is checked. From here on the database changes.
    for full in to_write:
        try:
            db.upsert("pcl.sessions", full, key=("path",))
        except PclError as error:
            raise PclError("The index file %s cannot be loaded: the session %s has a value "
                           "that does not fit (%s) Nothing was loaded."
                           % (shown, _show(full["path"]), error.message),
                           code="bad_index_value", details=error.details) from None
    if index_paths:
        if set(existing) <= index_paths:
            db.delete_where("pcl.files", "1 = 1")
        else:
            for path in sorted(index_paths & set(existing)):
                db.delete_where("pcl.files", "session_path = ?", [path])
    db.insert_many("pcl.files", tables["files"])
    for table in DERIVED_TABLES:
        db.delete_where("pcl." + table, "1 = 1")
        db.insert_many("pcl." + table, tables[table])

    current_model = _meta_text(meta.get("current_model"))
    built_utc = _meta_time(meta.get("built_utc"))
    tool = _meta_text(meta.get("tool"))
    for key, value in (("current_model", current_model), ("index_built_utc", built_utc),
                       ("index_source", tool)):
        if value is None:
            db.delete_where("pcl.meta", "key = ?", [key])
        else:
            db.upsert("pcl.meta", {"key": key, "value": value}, key=("key",))
    for path, action, doc_sha256 in audit:
        db.insert("app.registrations", {
            "reg_id": util.new_id(), "at_utc": now, "user_name": actor_name,
            "session_path": path, "action": action, "source": source,
            "doc_sha256": doc_sha256, "detail_json": {"from": "index", "index": INDEX_SCHEMA}})
    search = search_entries.refresh(db, None, settings)

    return {
        "file": Path(sqlite_path).name, "schema": INDEX_SCHEMA, "source": source,
        "built_utc": built_utc, "tool": tool, "current_model": current_model,
        "sessions": len(tables["sessions"]), "sessions_created": counts["created"],
        "sessions_updated": counts["updated"], "sessions_unchanged": counts["unchanged"],
        "files": len(tables["files"]), "runs": len(tables["runs"]),
        "points": len(tables["points"]), "reviews": len(tables["reviews"]),
        "models": len(tables["models"]), "problems": len(tables["problems"]),
        "session_rollup": len(tables["session_rollup"]), "not_finite": index["not_finite"],
        "not_in_index": sorted(set(existing) - index_paths), "search": search,
    }


def load_index_store(store, sqlite_path, *, actor, settings, source="index"):
    """`load_index` in a transaction of its own: what the command line calls."""
    with store.app() as db:
        return load_index(db, sqlite_path, actor=actor, settings=settings, source=source)


# ---------------------------------------------------------------------- the command line


def _say(text="", stream=None):
    """One ASCII line (Windows consoles): anything else is written as an escape."""
    stream = stream or sys.stdout
    stream.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")


def summary_lines(result, limit=20):
    """The plain sentences that say what a load did (also used by `pcldb demo load`)."""
    lines = []
    made = ""
    if result["built_utc"]:
        by = " by " + result["tool"] if result["tool"] else ""
        made = " (built %s%s)" % (result["built_utc"], by)
    lines.append("Loaded %s%s." % (result["file"], made))
    lines.append("Sessions: %d (%d new, %d changed, %d unchanged). Files: %d."
                 % (result["sessions"], result["sessions_created"], result["sessions_updated"],
                    result["sessions_unchanged"], result["files"]))
    lines.append("Runs: %d. Points: %d. Reviews: %d. Models: %d (current: %s). Problems: %d."
                 % (result["runs"], result["points"], result["reviews"], result["models"],
                    result["current_model"] or "none", result["problems"]))
    search = result["search"]
    lines.append("Search entries: %d to send, %d unchanged, %d to remove."
                 % (search["changed"], search["unchanged"], search["removed"]))
    if result["not_finite"]:
        lines.append("%d confidence or threshold value(s) were not finite numbers; they were "
                     "stored as empty." % result["not_finite"])
    missing = result["not_in_index"]
    if missing:
        lines.append("%d session(s) are in the database but not in this index. They were left "
                     "as they are:" % len(missing))
        lines.extend("  " + path for path in missing[:limit])
        if len(missing) > limit:
            lines.append("  ... and %d more" % (len(missing) - limit))
    return lines


def cmd_load_index(args, settings, *, out=None):
    """`pcldb load-index FILE`: load a nightly index and say what changed. Returns 0."""
    from .store import open_store
    path = getattr(args, "file", None)
    if not path:
        raise PclError("Give the index file to load: pcldb load-index FILE (the sessions.sqlite "
                       "that pcl_index build wrote).", code="usage")
    store = open_store(settings)
    result = load_index_store(store, os.fspath(path), actor=LOCAL_ACTOR, settings=settings)
    for line in summary_lines(result):
        _say(line, out)
    return 0
