"""Search entries: the small JSON entry of each session that is, or will be, in Globus Search.

Globus Search does not read files on ARC. It is a hosted index of entries that pcldb pushes.
This module makes the entry of one session (`content_for`), checks it against the frozen field
types of `schemas/search_fields.json` (`check_content`), and keeps the table
`app.search_entries` in step with the sessions (`refresh`). Nothing here talks to a network:
the pusher (`search_sync`) sends what this module queued.

The content is field for field what the proof of concept `pcl_index` exported (schema
`pcl-search/1`): empty values are left out, booleans are always there, times are cut to the
second, and `operator` and `notes` are only there when the settings switch them on.

Where a session's files are (`location_values`) is computed here too, because the Search
subject is a hash of exactly that text: `urn:pcl:dataset:` + uuid5(NAMESPACE_URL,
`<collection id>:<path inside the collection>`). A changed letter case or separator would make
another subject, so paths are never passed through the operating system.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path, PurePosixPath
from urllib.parse import urlencode

from . import util
from .errors import PclError

CONTENT_SCHEMA = "pcl-search/1"
SUBJECT_PREFIX = "urn:pcl:dataset:"
WEB_APP = "https://app.globus.org/file-manager"

# Entries are never public. These two Globus values mean "everybody" and are always refused.
NEVER_VISIBLE_TO = ("public", "all_authenticated_users")

ENTRY_STATES = ("pending", "sent", "synced", "failed", "delete_pending")

_FIELDS_FILE = Path(__file__).resolve().parent / "schemas" / "search_fields.json"
_fields_cache = {}


# ---------------------------------------------------------------------- where the files are


def collection_id_text(value, what="collection_id"):
    """The canonical lower-case text of a collection id, or a PclError when it is not a UUID."""
    try:
        return str(uuid.UUID(str(value).strip()))
    except (ValueError, AttributeError, TypeError):
        raise PclError("%s must be a Globus collection id (a UUID such as "
                       "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec), not %s."
                       % (what, _show(value)), code="bad_collection_id") from None


def data_path_for(storage_path, collection_root):
    """The path as the Globus collection shows it: the collection root is taken off the front.

    ARC's Projects collection shows `/projects` as `/`, so `/projects/pcl_photonics/raw/x` is
    `/pcl_photonics/raw/x` there. A path that is not inside the root is a PclError.
    """
    root = PurePosixPath(collection_root).as_posix()
    if root == "/":
        return storage_path
    if storage_path == root:
        return "/"
    if storage_path.startswith(root + "/"):
        return storage_path[len(root):]
    raise PclError("The path %s is not inside the collection root %s, so it has no address in "
                   "the Globus collection. Check storage.collection_root in the settings."
                   % (_show(storage_path), _show(root)), code="bad_path")


def subject_for(collection_id, data_path):
    """The Globus Search subject of one folder of a collection (the lab's shared scheme)."""
    collection_id = collection_id_text(collection_id)
    data_path = PurePosixPath(str(data_path).strip()).as_posix()
    return SUBJECT_PREFIX + str(uuid.uuid5(uuid.NAMESPACE_URL, collection_id + ":" + data_path))


def globus_url_for(collection_id, data_path):
    return "globus://%s%s/" % (collection_id, data_path)


def web_url_for(collection_id, data_path):
    """The folder in the Globus web app's file manager."""
    return WEB_APP + "?" + urlencode({"origin_id": collection_id,
                                      "origin_path": data_path.rstrip("/") + "/"})


def location_values(path, settings, *, collection_id=None, project_path=None):
    """Where the session folder `path` (relative POSIX text below the project folder) is.

    Returns `collection_id`, `storage_path` (on ARC's file system), `data_path` (inside the
    Globus collection), `globus_url`, `web_url` and `subject`. `collection_id` and
    `project_path` default to the settings. `path` is used exactly as given.
    """
    storage = settings.storage
    collection = collection_id_text(storage.collection_id if collection_id is None
                                    else collection_id)
    project = storage.project_path if project_path is None else project_path
    project = PurePosixPath(project).as_posix() if project_path is None else project
    storage_path = (project.rstrip("/") + "/" + path) if project != "/" else "/" + path
    data_path = data_path_for(storage_path, storage.collection_root)
    return {
        "collection_id": collection,
        "storage_path": storage_path,
        "data_path": data_path,
        "globus_url": globus_url_for(collection, data_path),
        "web_url": web_url_for(collection, data_path),
        "subject": subject_for(collection, data_path),
    }


# ---------------------------------------------------------------------- the content of an entry


def load_fields():
    """The frozen Search fields: {name: {"type": ..., ...}} from schemas/search_fields.json."""
    if "fields" not in _fields_cache:
        with open(_FIELDS_FILE, encoding="utf-8") as handle:
            _fields_cache["fields"] = json.load(handle)["fields"]
    return _fields_cache["fields"]


def _type_of(spec):
    return spec["type"] if isinstance(spec, dict) else spec


def _is_second_text(value):
    """True for a real UTC time written to the second: 2026-11-17T03:50:15Z."""
    if not isinstance(value, str) or len(value) != 20:
        return False
    try:
        util.parse_utc(value)
    except PclError:
        return False
    return True


def check_content(content, fields=None):
    """Problems of one entry's content against search_fields.json (an empty list when fine)."""
    fields = fields or load_fields()
    found = []
    if not isinstance(content, dict):
        return ["the content must be a JSON object"]
    for name, value in content.items():
        spec = fields.get(name) if isinstance(name, str) else None
        if spec is None:
            found.append("%s is not a frozen Search field" % name)
            continue
        kind = _type_of(spec)
        if value is None or value == "" or value == []:
            found.append("%s is empty (empty values are left out)" % name)
        elif kind in ("string", "text") and not isinstance(value, str):
            found.append("%s must be a string" % name)
        elif kind == "string_list" and not (isinstance(value, list) and all(
                isinstance(item, str) and item for item in value)):
            found.append("%s must be a list of strings" % name)
        elif kind == "boolean" and not isinstance(value, bool):
            found.append("%s must be true or false" % name)
        elif kind == "integer" and (not isinstance(value, int) or isinstance(value, bool)):
            found.append("%s must be a whole number" % name)
        elif kind == "date" and not _is_second_text(value):
            found.append("%s must be a date like 2026-11-17T03:50:15Z" % name)
        elif kind == "object_list":
            members = spec.get("fields", {})
            if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
                found.append("%s must be a list of objects" % name)
            else:
                for item in value:
                    if set(item) != set(members):
                        found.append("%s items must have exactly %s"
                                     % (name, ", ".join(sorted(members))))
                        break
                    for key, member_kind in members.items():
                        sub = check_content({key: item[key]}, {key: {"type": member_kind}})
                        if sub:
                            found.append("%s.%s" % (name, sub[0]))
    return found


def _second(value):
    return util.second_text(value) if value not in (None, "") else None


def _list(value):
    return list(value) if value else []


def content_for(session_row, rollup_row, settings):
    """The Search content of one session, cast to the frozen types.

    `session_row` is a row of `pcl.sessions` and `rollup_row` the session's row of
    `pcl.session_rollup` (or None when it has none yet), both with normalised values.
    """
    row = session_row
    rollup = rollup_row or {}
    where = {name: row.get(name) for name in ("collection_id", "data_path", "globus_url", "web_url")}
    if not all(where.values()):
        where = location_values(row["path"], settings, collection_id=row.get("collection_id"))
    content = {
        "schema": CONTENT_SCHEMA,
        "title": row["folder"],
        "instrument": row["instrument"],
        "folder": row["folder"],
        "session_id": row.get("session_id"),
        "path": row["path"],
        "collection_id": where["collection_id"],
        "data_path": where["data_path"],
        "globus_url": where["globus_url"],
        "web_url": where["web_url"],
        "method": row.get("method"),
        "completion": row.get("completion"),
        "format": row.get("format"),
        "location": row.get("location"),
        "review_state": rollup.get("review_state") or "none",
    }
    if settings.search.include_operator:
        content["operator"] = row.get("operator")
    if settings.search.include_notes:
        content["notes"] = row.get("notes")
    for name in ("sample_ids", "tags"):
        content[name] = _list(row.get(name))
    for name in ("labels", "models", "problems", "label_counts"):
        content[name] = _list(rollup.get(name))
    for name in ("simulated", "is_copy", "withdrawn", "corrected"):
        content[name] = bool(row.get(name))
    numbers = {"items": row.get("items"), "items_planned": row.get("items_planned"),
               "files": row.get("n_files"), "bytes": row.get("bytes"),
               "n_needs_review": rollup.get("n_needs_review") or 0}
    for name, value in numbers.items():
        content[name] = int(value) if value is not None else None
    content["started"] = _second(row.get("started_utc"))
    content["uploaded"] = _second(row.get("uploaded_utc"))
    # A check that failed, or no check at all, leaves "verified" out.
    content["verified"] = _second(row.get("verified_utc")) if row.get("verify_ok") is True else None
    content["last_run"] = _second(rollup.get("last_run_utc"))
    # Never null, never an empty string or list: leave the field out.
    return {name: value for name, value in content.items()
            if value is not None and value != "" and value != []}


# ---------------------------------------------------------------------- the queue


def visible_to_for(settings):
    """Who may see the entries, from the settings. May be empty: the pusher then uses the
    signed-in person's own identity. `public` and `all_authenticated_users` are refused."""
    values = []
    for value in settings.search.visible_to:
        if not isinstance(value, str) or not value.strip():
            raise PclError("search.visible_to must hold Globus identity or group addresses "
                           "(urn:globus:...), not %s." % _show(value), code="bad_visible_to")
        if value.strip().lower() in NEVER_VISIBLE_TO:
            raise PclError("Search entries are never public: search.visible_to must not hold "
                           "%s. Put the lab's Globus group there (urn:globus:groups:id:<id>) "
                           "or leave it empty." % _show(value.strip()), code="bad_visible_to")
        values.append(value.strip())
    return values


def _entry_for(session_row, rollup_row, settings):
    content = content_for(session_row, rollup_row, settings)
    problems = check_content(content)
    if problems:
        raise PclError("The Search entry of %s does not fit the frozen fields: %s."
                       % (_show(session_row["path"]), "; ".join(problems[:10])),
                       code="bad_search_entry", details={"problems": problems})
    subject = session_row.get("subject") or subject_for(content["collection_id"],
                                                        content["data_path"])
    return subject, content, util.sha256_hex(util.canonical_json(content).encode("utf-8"))


def _mark_for_delete(db, entry, now, session_path=None):
    changes = ["state = ?", "updated_utc = ?", "task_id = NULL", "last_error = NULL"]
    params = ["delete_pending", db.param("utc", now)]
    if session_path is not None:
        changes.append("session_path = ?")
        params.append(session_path)
    db.execute("UPDATE {app}.search_entries SET %s WHERE subject = ?" % ", ".join(changes),
               params + [entry["subject"]])


def _refresh_one(db, path, session_row, rollup_row, entry, visible_to, settings, now):
    """Bring one session's entry up to date. Returns "changed", "unchanged", "removed" or None."""
    if session_row is None:
        if entry is None or entry["state"] == "delete_pending":
            return None
        _mark_for_delete(db, entry, now)
        return "removed"
    subject, content, content_sha256 = _entry_for(session_row, rollup_row, settings)
    if entry is not None and entry["subject"] != subject:
        # The folder has another address now (another collection or project folder). The old
        # entry must leave the index; it keeps its subject and gives up the session's path.
        _mark_for_delete(db, entry, now, session_path="replaced:" + entry["subject"])
        entry = None
    taken = db.one("SELECT session_path, state FROM {app}.search_entries WHERE subject = ?",
                   [subject])
    if taken is not None and taken["session_path"] != path:
        still_there = db.one("SELECT path FROM {pcl}.sessions WHERE path = ?",
                             [taken["session_path"]])
        if taken["state"] != "delete_pending" and still_there is not None:
            raise PclError("The Search entry of %s would get the subject of %s: both are the "
                           "same folder of the same collection."
                           % (_show(path), _show(taken["session_path"])), code="conflict",
                           status=409)
        db.delete_where("app.search_entries", "subject = ?", [subject])
    if (entry is not None and entry["state"] != "delete_pending"
            and entry["content_sha256"] == content_sha256
            and list(entry["visible_to"] or []) == visible_to):
        return "unchanged"
    row = {"subject": subject, "session_path": path, "content_json": content,
           "visible_to": visible_to, "content_sha256": content_sha256, "state": "pending",
           "task_id": None, "last_error": None, "updated_utc": now,
           "synced_utc": entry["synced_utc"] if entry is not None else None}
    if entry is None:
        db.insert("app.search_entries", row)
    else:
        db.upsert("app.search_entries", row, key=("subject",))
    return "changed"


def refresh(db, paths, settings):
    """Queue the Search entries of the sessions `paths` (None = every session).

    An entry whose content or `visible_to` changed goes to the state `pending`; an entry whose
    session is gone goes to `delete_pending`; everything else is left alone. Runs inside the
    caller's transaction. Returns {"changed": n, "unchanged": n, "removed": n}.
    """
    visible_to = visible_to_for(settings)
    now = util.now_utc_text()
    counts = {"changed": 0, "unchanged": 0, "removed": 0}
    if isinstance(paths, str):
        paths = [paths]
    if paths is None:
        sessions = {row["path"]: row for row in db.query("SELECT * FROM {pcl}.sessions")}
        rollups = {row["session_path"]: row
                   for row in db.query("SELECT * FROM {pcl}.session_rollup")}
        entries = {row["session_path"]: row
                   for row in db.query("SELECT * FROM {app}.search_entries")}
        todo = sorted(set(sessions) | set(entries))
    else:
        todo = []
        for path in paths:
            if path not in todo:
                todo.append(path)
        sessions, rollups, entries = {}, {}, {}
        for path in todo:
            session = db.one("SELECT * FROM {pcl}.sessions WHERE path = ?", [path])
            if session is not None:
                sessions[path] = session
                rollup = db.one("SELECT * FROM {pcl}.session_rollup WHERE session_path = ?",
                                [path])
                if rollup is not None:
                    rollups[path] = rollup
            entry = db.one("SELECT * FROM {app}.search_entries WHERE session_path = ?", [path])
            if entry is not None:
                entries[path] = entry
    for path in todo:
        outcome = _refresh_one(db, path, sessions.get(path), rollups.get(path),
                               entries.get(path), visible_to, settings, now)
        if outcome is not None:
            counts[outcome] += 1
    return counts


def _show(value, limit=120):
    """A short, safe (ASCII) picture of a value for a message."""
    try:
        text = json.dumps(value, ensure_ascii=True)
    except (TypeError, ValueError):
        text = ascii(value)
    except RecursionError:
        text = "(a deeply nested value)"
    return text if len(text) <= limit else text[:limit - 3] + "..."
