"""Registration: after an upload succeeds, write the session into the database.

A registration document (`pcl-registration/1`) says where a session folder is on ARC, carries
its lab card (`pcl-session/1`) and lists its files with sizes and SHA-256 values:

    {"schema": "pcl-registration/1",
     "location": {"path": "raw/raman-portable/simulated/<folder>",
                  "collection_id": null, "project_path": null},
     "card": {...},
     "files": [{"rel_path": "session.json", "bytes": 1234, "sha256": "<64 hex or null>"}],
     "sums_sha256": "<64 hex or null>",
     "upload": {"transport": "ssh", "upload_id": "...", "checked": true,
                "check_method": "sha256-on-arc", "checked_utc": "..."}}

`register_document(db, document, ...)` checks every part, then writes the session, its files,
one audit row and the queued Search entry in the caller's ONE transaction. `register(store,
document, ...)` is what the command line and the server call: it opens that transaction and,
when a document is refused, writes the audit row "rejected" in a transaction of its own, so
the refusal is on record although everything else was rolled back.

Paths are case-sensitive POSIX text and are stored exactly as given. They are never passed
through the operating system: the Search subject is a hash of the path text.

The card checker (`validate_card`) is a small validator for exactly the JSON Schema features
that `schemas/pcl_session.schema.json` uses. It keeps three rules that are stricter than a
standard validator: a pattern written `^...$` must match the whole text, `"format": "utc"`
needs a real date and time, and every text and field name at any depth must be storable.
"""

from __future__ import annotations

import json
import math
import re
import sys
import unicodedata
from pathlib import Path, PurePath

from . import search_entries, util
from .errors import Conflict, PclError
from .store.base import IntegrityError

DOCUMENT_SCHEMA = "pcl-registration/1"
CARD_SCHEMA = "pcl-session/1"

SOURCES = ("upload", "tree", "index", "demo")
TRANSPORTS = ("globus", "ssh", "local")
CHECK_METHODS = ("sha256-on-arc", "globus-checksum", "globus-verify", "none")
UPLOAD_KEYS = ("transport", "upload_id", "checked", "check_method", "checked_utc")

MAX_FILES = 100000
MAX_PATH_CHARS = 1024
MAX_SEGMENT_CHARS = 255
MAX_INT = 2147483647
MAX_BIGINT = 9223372036854775807
MAX_DEPTH = 64

_SHA256 = re.compile(r"[0-9a-f]{64}")
_UTC_FORM = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,9})?Z")
_COPY_NUMBER = re.compile(r"[1-9][0-9]*")
_WINDOWS_DEVICES = frozenset(
    ["con", "prn", "aux", "nul"] + ["com%s" % n for n in "123456789\u00b9\u00b2\u00b3"]
    + ["lpt%s" % n for n in "123456789\u00b9\u00b2\u00b3"])

_SCHEMA_FILE = Path(__file__).resolve().parent / "schemas" / "pcl_session.schema.json"
_schema_cache = {}


# ---------------------------------------------------------------------- small helpers


def _show(value, limit=120):
    """A short, safe (ASCII) picture of a value for a message."""
    try:
        text = json.dumps(value, ensure_ascii=True)
    except (TypeError, ValueError):
        text = ascii(value)
    except RecursionError:
        text = "(a deeply nested value)"
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _refuse(message, code="invalid_document", details=None):
    return PclError(message, code=code, status=400, details=details)


def storable_text(text):
    """True when `text` can be stored anywhere: valid UTF-8 (no half of a surrogate pair) and
    no control character other than tab, newline and carriage return."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(ord(character) < 32 and character not in "\t\n\r" for character in text)


def _clean_text(text, limit=2000):
    """Any text as storable ASCII-safe text, for the audit row of a refusal."""
    text = str(text).encode("utf-8", "backslashreplace").decode("utf-8")
    text = "".join(character if ord(character) >= 32 or character in "\t\n" else
                   "\\x%02x" % ord(character) for character in text)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _real_utc(text):
    try:
        util.parse_utc(text)
    except PclError:
        return False
    return True


# ---------------------------------------------------------------------- the card checker

_TYPES = {
    "string": lambda value: isinstance(value, str),
    "integer": lambda value: isinstance(value, int) and not isinstance(value, bool),
    "number": lambda value: isinstance(value, (int, float)) and not isinstance(value, bool),
    "boolean": lambda value: isinstance(value, bool),
    "object": lambda value: isinstance(value, dict),
    "array": lambda value: isinstance(value, list),
    "null": lambda value: value is None,
}


def load_card_schema():
    """schemas/pcl_session.schema.json, read once."""
    if "card" not in _schema_cache:
        with open(_SCHEMA_FILE, encoding="utf-8") as handle:
            _schema_cache["card"] = json.load(handle)
    return _schema_cache["card"]


def _pattern_matches(pattern, text):
    # A pattern written ^...$ is matched whole: Python's $ would also match before a final
    # newline, and "2026-11-17T03:50:15Z\n" is not a time.
    if pattern.startswith("^") and pattern.endswith("$") and not pattern.endswith("\\$"):
        return re.fullmatch(pattern[1:-1], text) is not None
    return re.search(pattern, text) is not None


def schema_errors(instance, schema, where="card"):
    """The problems of `instance` against `schema`, as sentences (empty when it is valid).

    Supported keywords: type (also a list), const, enum, pattern, minLength, maxLength,
    minimum, minItems, required, properties, additionalProperties (false or a schema), items,
    not, allOf, if/then, and "format": "utc".
    """
    found = []
    try:
        _storable(instance, where, found, 0)
        _check(instance, schema, where, found)
    except RecursionError:
        return ["%s: is nested too deeply to be read" % where]
    return found


def _storable(value, where, found, depth):
    """Every text and field name, at any depth, must be storable text."""
    if depth > MAX_DEPTH:
        found.append("%s: is nested deeper than %d levels" % (where, MAX_DEPTH))
        return
    if isinstance(value, str):
        if not storable_text(value):
            found.append("%s: holds a character that cannot be stored (not UTF-8, or a control "
                         "character)" % where)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _storable(item, "%s[%d]" % (where, index), found, depth + 1)
    elif isinstance(value, dict):
        for name, item in value.items():
            if not storable_text(str(name)):
                found.append("%s: a field name holds a character that cannot be stored" % where)
                continue
            _storable(item, "%s.%s" % (where, name), found, depth + 1)


def _check(value, schema, where, found):
    if not isinstance(schema, dict):
        return
    if "type" in schema:
        allowed = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_TYPES[name](value) for name in allowed):
            found.append("%s: must be %s, not %s" % (where, " or ".join(allowed), _type_name(value)))
            return
    if "const" in schema and value != schema["const"]:
        found.append("%s: must be %s" % (where, json.dumps(schema["const"])))
    if "enum" in schema and value not in schema["enum"]:
        found.append("%s: must be one of %s"
                     % (where, ", ".join(json.dumps(item) for item in schema["enum"])))
    if isinstance(value, str):
        if schema.get("format") == "utc" and _UTC_FORM.fullmatch(value) and not _real_utc(value):
            found.append("%s: %s is not a real date and time" % (where, json.dumps(value)))
        if "minLength" in schema and len(value) < schema["minLength"]:
            found.append("%s: must not be empty" % where if schema["minLength"] == 1
                         else "%s: shorter than %d characters" % (where, schema["minLength"]))
        if "maxLength" in schema and len(value) > schema["maxLength"]:
            found.append("%s: longer than %d characters" % (where, schema["maxLength"]))
        if "pattern" in schema and not _pattern_matches(schema["pattern"], value):
            found.append("%s: %s does not have the required form %s"
                         % (where, _show(value, 200), schema["pattern"]))
    if isinstance(value, (int, float)) and not isinstance(value, bool) and "minimum" in schema:
        if value < schema["minimum"]:
            found.append("%s: must be at least %s" % (where, schema["minimum"]))
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            found.append("%s: needs at least %d item(s)" % (where, schema["minItems"]))
        if "items" in schema:
            for index, item in enumerate(value):
                _check(item, schema["items"], "%s[%d]" % (where, index), found)
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                found.append("%s: %s is missing" % (where, name))
        properties = schema.get("properties", {})
        for name, item in value.items():
            if name in properties:
                _check(item, properties[name], "%s.%s" % (where, name), found)
            elif schema.get("additionalProperties") is False:
                found.append("%s: unknown field %s" % (where, name))
            elif isinstance(schema.get("additionalProperties"), dict):
                _check(item, schema["additionalProperties"], "%s.%s" % (where, name), found)
    if "not" in schema:
        inner = []
        _check(value, schema["not"], where, inner)
        if not inner:
            found.append("%s: %s is not allowed" % (where, _show(value, 200)))
    for part in schema.get("allOf", []):
        _check(value, part, where, found)
    if "if" in schema:
        condition = []
        _check(value, schema["if"], where, condition)
        if not condition and "then" in schema:
            _check(value, schema["then"], where, found)


def _type_name(value):
    if isinstance(value, bool):
        return "boolean"
    if value is None:
        return "null"
    if isinstance(value, int):
        return "integer"
    if isinstance(value, float):
        return "number"
    if isinstance(value, str):
        return "string"
    if isinstance(value, list):
        return "array"
    return "object"


def validate_card(card):
    """The problems of a lab card against schemas/pcl_session.schema.json (empty when valid)."""
    if not isinstance(card, dict):
        return ["card: must be a JSON object"]
    return schema_errors(card, load_card_schema(), "card")


# ---------------------------------------------------------------------- checks of the document


def _not_finite(value, where, depth=0):
    """The place of the first NaN or Infinity in a JSON value, or None."""
    if depth > MAX_DEPTH + 4:
        raise _refuse("The document is nested deeper than %d levels. Nothing was registered."
                      % MAX_DEPTH)
    if isinstance(value, float):
        return None if math.isfinite(value) else where
    if isinstance(value, list):
        for index, item in enumerate(value):
            place = _not_finite(item, "%s[%d]" % (where, index), depth + 1)
            if place:
                return place
    elif isinstance(value, dict):
        for name, item in value.items():
            place = _not_finite(item, "%s.%s" % (where, _clean_text(name, 60)), depth + 1)
            if place:
                return place
    return None


def path_problem(path, what="location.path"):
    """Why `path` is not a relative POSIX path that can be stored, or None when it is fine.

    Rules: text of 1 to 1024 characters; segments separated by `/`, each 1 to 255 characters;
    no empty segment (so no leading or trailing `/`), no `.` or `..`, no backslash, no control
    character, valid UTF-8.
    """
    if not isinstance(path, str):
        return "%s must be text, not %s." % (what, _type_name(path))
    if not path:
        return "%s is empty." % what
    if len(path) > MAX_PATH_CHARS:
        return "%s is longer than %d characters." % (what, MAX_PATH_CHARS)
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return "%s holds a broken character (not UTF-8)." % what
    if "\\" in path:
        return ("%s %s holds a backslash. Write the path with / between the folders."
                % (what, _show(path)))
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        return "%s %s holds a control character." % (what, _show(path))
    if path.startswith("/"):
        return ("%s %s starts with /. Give the path below the project folder, for example "
                "raw/raman-portable/<session folder>." % (what, _show(path))
                if what == "location.path" else
                "%s %s starts with /. It must be a path inside the session folder."
                % (what, _show(path)))
    for segment in path.split("/"):
        if segment == "":
            return "%s %s has an empty part (a doubled or trailing /)." % (what, _show(path))
        if segment in (".", ".."):
            return "%s %s holds the part %s, which is not allowed." % (what, _show(path), _show(segment))
        if len(segment) > MAX_SEGMENT_CHARS:
            return "%s has a part longer than %d characters." % (what, MAX_SEGMENT_CHARS)
    return None


def windows_name_problem(segment):
    """Why a folder name could not exist on Windows, or None."""
    if segment.endswith(".") or segment.endswith(" "):
        return "ends with a dot or a space"
    if segment.split(".")[0].rstrip(" ").lower() in _WINDOWS_DEVICES:
        return "is a device name on Windows"
    return None


def _check_keys(value, allowed, what, required=()):
    if not isinstance(value, dict):
        raise _refuse("%s must be a JSON object, not %s." % (what, _type_name(value)))
    for name in value:
        if name not in allowed:
            raise _refuse("%s has the unknown field %s. Allowed: %s."
                          % (what, _show(name), ", ".join(allowed)))
    for name in required:
        if name not in value:
            raise _refuse("%s has no %s." % (what, name))


def _layout_parts(segments):
    """(instrument, area, folder) when the path has the layout shape, else None."""
    if len(segments) == 3 and segments[0] == "raw":
        return segments[1], "real", segments[2]
    if len(segments) == 4 and segments[0] == "raw" and segments[2] == "simulated":
        return segments[1], "simulated", segments[3]
    return None


def _check_location(document):
    location = document["location"]
    _check_keys(location, ("path", "collection_id", "project_path"), "location", ("path",))
    path = location["path"]
    problem = path_problem(path)
    if problem:
        raise _refuse(problem + " Nothing was registered.", code="bad_path")
    segments = path.split("/")
    # The session folder's own name, and in the lab layout the instrument's folder, must be
    # names a Windows computer can hold: people keep copies of sessions there.
    named = [segments[-1]] + ([segments[1]] if _layout_parts(segments) else [])
    for segment in segments:
        problem = windows_name_problem(segment) if segment in named else None
        if problem:
            raise _refuse("location.path %s: the folder name %s %s, so a copy of the session "
                          "could not be kept on a Windows computer. Rename the folder and "
                          "register it again." % (_show(path), _show(segment), problem),
                          code="bad_path")
        if segment == "_incoming":
            raise _refuse("location.path %s is inside _incoming: that copy is still being "
                          "made. Register the session once it has been placed."
                          % _show(path), code="bad_path")
    project_path = location.get("project_path")
    if project_path is not None:
        problem = None
        if not isinstance(project_path, str) or not project_path.startswith("/"):
            problem = "must be a path on ARC that starts with /, for example /projects/pcl_photonics"
        elif project_path != "/" and path_problem(project_path[1:], "location.project_path"):
            problem = "is not a plain path: " + path_problem(project_path[1:], "it")
        if problem:
            raise _refuse("location.project_path %s %s." % (_show(project_path), problem.rstrip(".")),
                          code="bad_path")
    return path, segments


def _check_card(card, path, segments):
    """Rules 1 to 3. Returns (area, folder, is_copy)."""
    problems = [_clean_text(problem, 500) for problem in validate_card(card)]
    if problems:
        shown = "; ".join(problems[:12]) + ("; and %d more" % (len(problems) - 12)
                                            if len(problems) > 12 else "")
        raise _refuse("The card of %s has %d problem(s): %s. Nothing was registered."
                      % (_show(path), len(problems), shown), code="invalid_card",
                      details={"problems": problems})
    for name in ("items", "items_planned"):
        if card.get(name) is not None and card[name] > MAX_INT:
            raise _refuse("The card of %s says %s = %d. That is more than %d, the largest "
                          "number the database keeps there. Correct the card."
                          % (_show(path), name, card[name], MAX_INT), code="invalid_card")
    folder = segments[-1]
    session = card["session"]
    is_copy = False
    if folder.startswith(session + "_copy"):
        number = folder[len(session) + 5:]
        is_copy = bool(_COPY_NUMBER.fullmatch(number)) and int(number) >= 2
    layout = _layout_parts(segments)
    if layout is None:
        return ("simulated" if card["simulated"] else "real"), folder, is_copy
    instrument, area, folder = layout
    if area == "real" and folder == "simulated":
        raise _refuse("%s is the folder that holds an instrument's simulated sessions, not a "
                      "session. Give the session folder inside it." % _show(path), code="bad_path")
    if instrument != card["instrument"]:
        raise _refuse("%s is in the folder of the instrument %s, but its card says the "
                      "instrument is %s. Correct the card or the path; nothing was registered."
                      % (_show(path), _show(instrument), _show(card["instrument"])),
                      code="instrument_mismatch")
    if area == "simulated" and card["simulated"] is not True:
        raise _refuse("%s is in a simulated/ folder, but its card says simulated is false. "
                      "Measured sessions go to raw/%s/<folder>; nothing was registered."
                      % (_show(path), instrument), code="simulated_mismatch")
    if area == "real" and card["simulated"] is not False:
        raise _refuse("%s is not in a simulated/ folder, but its card says simulated is true. "
                      "Simulated sessions go to raw/%s/simulated/<folder>; nothing was "
                      "registered." % (_show(path), instrument), code="simulated_mismatch")
    if folder != session and not is_copy:
        raise _refuse("The folder is named %s, but its card says the session is %s. The folder "
                      "must have the session's name (or that name with _copy2, _copy3, ...). "
                      "Nothing was registered." % (_show(folder), _show(session)),
                      code="name_mismatch")
    return area, folder, is_copy


def _check_files(files):
    """Rule 4. Returns (rows without session_path, total bytes)."""
    if not isinstance(files, list):
        raise _refuse("files must be a list, not %s." % _type_name(files))
    if len(files) > MAX_FILES:
        raise _refuse("The document lists %d files; at most %d can be registered for one "
                      "session. Pack small files into an archive first."
                      % (len(files), MAX_FILES), code="too_many_files")
    rows, seen, total = [], set(), 0
    for index, entry in enumerate(files):
        what = "files[%d]" % index
        _check_keys(entry, ("rel_path", "bytes", "sha256"), what, ("rel_path", "bytes", "sha256"))
        rel_path = entry["rel_path"]
        problem = path_problem(rel_path, what + ".rel_path")
        if problem:
            raise _refuse(problem + " Nothing was registered.", code="bad_path")
        if rel_path in seen:
            raise _refuse("The file %s is listed twice. Nothing was registered." % _show(rel_path))
        seen.add(rel_path)
        size = entry["bytes"]
        if not isinstance(size, int) or isinstance(size, bool) or size < 0 or size > MAX_BIGINT:
            raise _refuse("%s.bytes (file %s) must be the size in bytes, a whole number from 0 "
                          "up, not %s." % (what, _show(rel_path), _show(size)))
        digest = entry["sha256"]
        if digest is not None and not (isinstance(digest, str) and _SHA256.fullmatch(digest)):
            raise _refuse("%s.sha256 (file %s) must be 64 lower-case hex digits or null, not %s."
                          % (what, _show(rel_path), _show(digest)))
        total += size
        rows.append({"rel_path": rel_path, "bytes": size, "sha256": digest,
                     "listed": digest is not None})
    if total > MAX_BIGINT:
        raise _refuse("The sizes of the files add up to more than the database can keep.")
    return rows, total


def _check_upload(upload):
    """The optional upload block: only the five known keys, each of its own type."""
    if upload is None:
        return None
    _check_keys(upload, UPLOAD_KEYS, "upload")
    transport = upload.get("transport")
    if transport is not None and transport not in TRANSPORTS:
        raise _refuse("upload.transport must be one of %s, not %s."
                      % (", ".join(TRANSPORTS), _show(transport)))
    upload_id = upload.get("upload_id")
    if upload_id is not None and not (isinstance(upload_id, str) and 1 <= len(upload_id) <= 64
                                      and storable_text(upload_id)):
        raise _refuse("upload.upload_id must be text of 1 to 64 characters, not %s."
                      % _show(upload_id))
    checked = upload.get("checked")
    if checked is not None and not isinstance(checked, bool):
        raise _refuse("upload.checked must be true or false, not %s." % _show(checked))
    method = upload.get("check_method")
    if method is not None and method not in CHECK_METHODS:
        raise _refuse("upload.check_method must be one of %s, not %s."
                      % (", ".join(CHECK_METHODS), _show(method)))
    checked_utc = upload.get("checked_utc")
    if checked_utc is not None and not (isinstance(checked_utc, str)
                                        and _UTC_FORM.fullmatch(checked_utc)
                                        and _real_utc(checked_utc)):
        raise _refuse("upload.checked_utc must be a UTC time like 2026-11-17T03:50:15.281Z or "
                      "null, not %s." % _show(checked_utc))
    return dict(upload)


def document_hash(path, card, files, sums_sha256):
    """`doc_sha256`: the SHA-256 of the canonical JSON of the path, the card, the files sorted
    by rel_path and sums_sha256. The same contents always give the same value."""
    listed = sorted(({"rel_path": entry["rel_path"], "bytes": entry["bytes"],
                      "sha256": entry["sha256"]} for entry in files),
                    key=lambda entry: entry["rel_path"])
    body = {"path": path, "card": card, "files": listed, "sums_sha256": sums_sha256}
    return util.sha256_hex(util.canonical_json(body).encode("utf-8"))


def check_document(document, settings):
    """Every check that needs no database. Returns the parts `register_document` writes.

    Raises a PclError that says what is wrong and that nothing was registered.
    """
    _check_keys(document, ("schema", "location", "card", "files", "sums_sha256", "upload"),
                "The registration document", ("schema", "location", "card", "files"))
    place = _not_finite(document, "document")
    if place:
        raise _refuse("%s holds NaN or Infinity, which is not valid JSON and cannot be stored. "
                      "Correct the value; nothing was registered." % place, code="bad_number")
    if document["schema"] != DOCUMENT_SCHEMA or not isinstance(document["schema"], str):
        raise _refuse("The document's schema is %s; this version registers %s only."
                      % (_show(document["schema"]), DOCUMENT_SCHEMA), code="unknown_schema")
    path, segments = _check_location(document)
    card = document["card"]
    area, folder, is_copy = _check_card(card, path, segments)
    files, total = _check_files(document["files"])
    sums = document.get("sums_sha256")
    if sums is not None and not (isinstance(sums, str) and _SHA256.fullmatch(sums)):
        raise _refuse("sums_sha256 must be 64 lower-case hex digits or null, not %s." % _show(sums))
    for entry in files:
        if (entry["rel_path"] == "SHA256SUMS" and entry["sha256"] is not None
                and sums is not None and entry["sha256"] != sums):
            raise _refuse("The document gives two different SHA-256 values for SHA256SUMS: one "
                          "in files and one in sums_sha256. Build the document again from the "
                          "folder; nothing was registered.")
    upload = _check_upload(document.get("upload"))
    location = document["location"]
    where = search_entries.location_values(path, settings,
                                           collection_id=location.get("collection_id"),
                                           project_path=location.get("project_path"))
    return {"path": path, "area": area, "folder": folder, "is_copy": is_copy, "card": card,
            "files": files, "bytes": total, "sums_sha256": sums, "upload": upload,
            "where": where, "doc_sha256": document_hash(path, card, files, sums)}


# ---------------------------------------------------------------------- writing


def _actor_name(actor):
    if actor is None or isinstance(actor, str):
        return actor
    return getattr(actor, "name", None) or str(actor)


def _audit(db, *, actor, path, action, source, doc_sha256, detail=None):
    db.insert("app.registrations", {
        "reg_id": util.new_id(), "at_utc": util.now_utc_text(), "user_name": _actor_name(actor),
        "session_path": path, "action": action, "source": source, "doc_sha256": doc_sha256,
        "detail_json": detail})


def folded_path(path):
    """`path` as Windows and macOS compare names: letter case ignored, and an accented letter
    the same whether it is one character or a letter plus an accent (NFC and NFD)."""
    return unicodedata.normalize("NFC", path).casefold()


def _other_spelling(db, path):
    """A registered path that equals `path` when letter case and the Unicode form are ignored
    but is other text."""
    # Every cased ASCII character becomes the one-character wildcard and every run of other
    # characters the many-character one, so both engines pick the same candidates whatever
    # their own rules for letter case are (an accented letter may be one or two characters).
    # Python then compares.
    pattern = []
    for character in path:
        if not character.isascii():
            if not pattern or pattern[-1] != "%":
                pattern.append("%")
        elif character.lower() != character.upper():
            pattern.append("_")
        else:
            pattern.append(util.like_escape(character))
    folded = folded_path(path)
    for row in db.query("SELECT path FROM {pcl}.sessions WHERE path LIKE ? ESCAPE '\\'",
                        ["".join(pattern)]):
        if row["path"] != path and folded_path(row["path"]) == folded:
            return row["path"]
    return None


def _session_row(parts, existing, *, actor, source, now):
    card = parts["card"]
    row = {
        "path": parts["path"], "instrument": card["instrument"], "area": parts["area"],
        "folder": parts["folder"], "session_name": card["session"],
        "session_id": card["session_id"], "is_copy": parts["is_copy"],
        "started_utc": util.utc_text(card["started_utc"]),
        "ended_utc": util.utc_text(card["ended_utc"]) if card.get("ended_utc") else None,
        "simulated": card["simulated"], "sample_ids": list(card["sample_ids"]),
        "operator": card.get("operator"), "location": card.get("location"),
        "method": card.get("method"), "items": card.get("items"),
        "items_planned": card.get("items_planned"), "completion": card["completion"],
        "notes": card.get("notes"), "tags": list(card.get("tags", [])),
        "format": card["format"], "uploaded_utc": util.utc_text(card["uploaded_utc"]),
        "upload_id": card.get("upload_id"), "has_card": True, "card_json": card,
        "corrections_json": [], "corrected": False,
        # Withdrawing is decided by a correction run, never by a card: a replaced session
        # stays withdrawn until the next index load says otherwise.
        "withdrawn": bool(existing["withdrawn"]) if existing else False,
        "bytes": parts["bytes"], "n_files": len(parts["files"]),
        "sums_sha256": parts["sums_sha256"], "verified_utc": None, "verify_ok": None,
        "upload_json": parts["upload"],
        "registered_utc": existing["registered_utc"] if existing else now,
        "registered_by": existing["registered_by"] if existing else _actor_name(actor),
        "updated_utc": now, "source": source, "doc_sha256": parts["doc_sha256"],
    }
    row.update(parts["where"])
    return row


def _other_place_or_upload(parts, existing):
    """True when a document with the same contents names another place on ARC, or says
    something else about how the copy was checked, than what is stored."""
    return (parts["where"]["subject"] != existing["subject"]
            or (parts["upload"] is not None and parts["upload"] != existing["upload_json"]))


def _differs_only_in_written_by(parts, existing):
    """True when the stored card and the new one differ only in `card.written_by` (the
    registrar that built the card, for example pcldb's own version after an upgrade): with the
    stored written_by put back, the new document hashes to the stored doc_sha256."""
    stored = existing.get("card_json")
    card = parts["card"]
    if not isinstance(stored, dict) or stored.get("written_by") == card.get("written_by"):
        return False
    other = dict(card)
    if "written_by" in stored:
        other["written_by"] = stored["written_by"]
    else:
        other.pop("written_by", None)
    return document_hash(parts["path"], other, parts["files"],
                         parts["sums_sha256"]) == existing["doc_sha256"]


def register_document(db, document, *, actor, source, settings, replace=False):
    """Register one session from its registration document, inside the caller's transaction.

    Returns {"action": "created" | "updated" | "unchanged", "path", "subject", "web_url",
    "n_files", "bytes"}. A document that breaks a rule is a PclError (`Conflict` when the
    session is already registered with other contents) and nothing is written.

    The same path with the same contents is `unchanged` and writes only the audit row. With
    `replace`, the same contents are written again (`updated`) when the document names another
    collection or project folder, or says something else about how the copy was checked.
    """
    if source not in SOURCES:
        raise _refuse("source must be one of %s, not %s." % (", ".join(SOURCES), _show(source)))
    parts = check_document(document, settings)
    path, where = parts["path"], parts["where"]
    if source != "demo" and db.scalar("SELECT value FROM {pcl}.meta WHERE key = ?",
                                      ["demo"]) == "1":
        raise Conflict("The demo data is loaded, and demo data and real data never mix, so %s "
                       "was not registered. Remove the demo data first: pcldb demo clear"
                       % _show(path), code="demo_loaded")
    if db.engine == "postgresql":
        # Registrations of one folder (in any spelling) take turns, as they do on SQLite: the
        # look-up below then always sees what the other one wrote.
        db.scalar("SELECT pg_advisory_xact_lock(hashtext(?))", [folded_path(path)])
    existing = db.one("SELECT path, doc_sha256, withdrawn, registered_utc, registered_by, "
                      "subject, web_url, upload_json, card_json FROM {pcl}.sessions "
                      "WHERE path = ?", [path])
    if (existing is not None and existing["doc_sha256"] != parts["doc_sha256"]
            and _differs_only_in_written_by(parts, existing)):
        # Only the registrar's own name or version changed: the same contents.
        parts = dict(parts, doc_sha256=existing["doc_sha256"], card=existing["card_json"])
    result = {"action": None, "path": path, "subject": where["subject"],
              "web_url": where["web_url"], "n_files": len(parts["files"]), "bytes": parts["bytes"]}
    # The record of one upload (its "upload" block) names one place on ARC. The same upload at
    # another path is the same folder, moved or renamed by hand: never a second session.
    upload_id = (parts["upload"] or {}).get("upload_id")
    if existing is None and upload_id:
        placed = db.one("SELECT path FROM {pcl}.sessions WHERE upload_id = ? AND path <> ?",
                        [upload_id, path])
        if placed is not None:
            raise Conflict("The upload %s is registered already, as %s. Its record now names %s: "
                           "the folder was moved or renamed by hand after it was registered. "
                           "Nothing was registered, and the registered entry still names the old "
                           "place. pcldb has no command to move an entry yet: tell whoever looks "
                           "after pcldb." % (_show(upload_id), _show(placed["path"]), _show(path)),
                           code="upload_moved", details={"registered_path": placed["path"]})
    if existing is None:
        other = _other_spelling(db, path)
        if other is not None:
            raise Conflict("A session is already registered as %s. %s differs from it only in "
                           "letter case (or in how an accented letter is written), and Windows "
                           "and macOS could not keep both folders. Use the registered spelling; "
                           "nothing was registered."
                           % (_show(other), _show(path)), code="case_conflict",
                           details={"registered_path": other})
    elif existing["doc_sha256"] == parts["doc_sha256"] and not (
            replace and _other_place_or_upload(parts, existing)):
        result.update(action="unchanged", subject=existing["subject"],
                      web_url=existing["web_url"])
        _audit(db, actor=actor, path=path, action="unchanged", source=source,
               doc_sha256=parts["doc_sha256"])
        return result
    elif existing["doc_sha256"] != parts["doc_sha256"] and not replace:
        raise Conflict("%s is already registered with other contents; placed folders are never "
                       "changed. Nothing was registered. If the first registration was wrong, "
                       "an admin can register it again with --replace." % _show(path),
                       code="already_registered")
    same_folder = db.one("SELECT path FROM {pcl}.sessions WHERE subject = ? AND path <> ?",
                         [where["subject"], path])
    if same_folder is not None:
        raise Conflict("The folder %s on ARC is already registered as %s. One folder is "
                       "registered once; nothing was registered."
                       % (_show(where["storage_path"]), _show(same_folder["path"])),
                       code="already_registered")
    now = util.now_utc_text()
    row = _session_row(parts, existing, actor=actor, source=source, now=now)
    if existing is None:
        try:
            db.insert("pcl.sessions", row)
        except IntegrityError as error:
            if error.kind != "unique":
                raise
            # Someone registered the same folder between the look and the write.
            raise Conflict("%s was registered by someone else at this very moment. Nothing "
                           "was registered by this request; send it again to see whether the "
                           "two agree." % _show(path), code="already_registered") from None
    else:
        db.upsert("pcl.sessions", row, key=("path",))
        db.delete_where("pcl.files", "session_path = ?", [path])
    if parts["files"]:
        db.insert_many("pcl.files", [dict(entry, session_path=path) for entry in parts["files"]])
    action = "created" if existing is None else "updated"
    detail = {"n_files": len(parts["files"]), "bytes": parts["bytes"]}
    if existing is not None:
        detail["replaced_doc_sha256"] = existing["doc_sha256"]
    _audit(db, actor=actor, path=path, action=action, source=source,
           doc_sha256=parts["doc_sha256"], detail=detail)
    search_entries.refresh(db, [path], settings)
    result["action"] = action
    return result


def _usable_path(document):
    """The document's path when it can go into the audit table, else None."""
    try:
        path = document["location"]["path"]
    except (TypeError, KeyError, IndexError):
        return None
    if isinstance(path, str) and 1 <= len(path) <= MAX_PATH_CHARS and storable_text(path) \
            and "\t" not in path and "\n" not in path and "\r" not in path:
        return path
    return None


def record_rejection(store, document, error, *, actor, source):
    """Write the audit row of a refused document in a transaction of its own.

    Only a document with a usable `location.path` gets a row, and only a refusal of the
    document itself: a database that is busy or down (status 503) is not a rejection. Returns
    True when the row was written; a failure to write it is never raised (the refusal itself
    is what the person must see).
    """
    path = _usable_path(document)
    if path is None or getattr(error, "status", 400) >= 500:
        return False
    doc_sha256 = None
    try:
        doc_sha256 = document_hash(path, document.get("card"), [
            {"rel_path": entry["rel_path"], "bytes": entry["bytes"], "sha256": entry["sha256"]}
            for entry in document.get("files")], document.get("sums_sha256"))
    except Exception:  # a document too broken to hash still gets its audit row
        doc_sha256 = None
    detail = {"reason": _clean_text(getattr(error, "message", error)),
              "code": _clean_text(getattr(error, "code", "error"), 64)}
    try:
        with store.app() as db:
            _audit(db, actor=actor, path=path, action="rejected",
                   source=source if source in SOURCES else "upload", doc_sha256=doc_sha256,
                   detail=detail)
    except Exception:
        return False
    return True


def register(store, document, *, actor, source, settings, replace=False):
    """Register one document in a transaction of its own: what the command line and the
    server call. A refusal is written to the audit table (action `rejected`, the reason in
    `detail_json`) and then raised again."""
    try:
        with store.app() as db:
            return register_document(db, document, actor=actor, source=source,
                                     settings=settings, replace=replace)
    except PclError as error:
        record_rejection(store, document, error, actor=actor, source=source)
        raise


# ---------------------------------------------------------------------- the command line

LOCAL_ACTOR = "local-cli"


def _say(text, stream=None):
    """One ASCII line (Windows consoles): anything else is written as an escape."""
    stream = stream or sys.stdout
    stream.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")


def _read_token(args, settings):
    """The token for the server: PCLDB_TOKEN, else --token-file, else var/admin_token.txt.
    It is never a command-line argument and never printed."""
    if settings.token:
        return settings.token
    given = getattr(args, "token_file", None)
    path = Path(given) if given else settings.var_dir / "admin_token.txt"
    try:
        with open(path, encoding="utf-8-sig") as handle:
            token = handle.readline().strip()
    except OSError:
        token = ""
    if not token:
        raise PclError("No token was found for the server. Put one into the variable "
                       "PCLDB_TOKEN, or give a file that holds it with --token-file (looked "
                       "in %s). An admin makes one with: pcldb token new --user <name>" % path,
                       code="no_token", status=401)
    return token


def _chosen_source(args):
    """("folder" | "tree" | "document" | "raman", value) for the one source the arguments name."""
    given = [(kind, getattr(args, name, None)) for kind, name in (
        ("folder", "folder"), ("tree", "tree"), ("document", "document"),
        ("raman", "raman_uploads"))]
    given = [(kind, value) for kind, value in given if value]
    if len(given) != 1:
        raise PclError("Give exactly one thing to register: a session FOLDER with --path, or "
                       "--tree ROOT, or --document FILE, or --raman-uploads CONTROLLER_HOME "
                       "(%s)." % ("none was given" if not given else "%d were given" % len(given)),
                       code="usage")
    kind, value = given[0]
    if kind == "folder" and not getattr(args, "path", None):
        raise PclError("Say where the folder is below the project folder on ARC: add "
                       "--path raw/<instrument>/<session folder> (simulated sessions: "
                       "raw/<instrument>/simulated/<session folder>).", code="usage")
    return kind, value


def _documents(kind, value, args, settings):
    """(label, document or PclError, source) for every session of the chosen source."""
    from . import sources
    if kind == "folder":
        label = sources.posix_text(args.path)
        try:
            yield label, sources.document_from_folder(value, args.path,
                                                      getattr(args, "card", None)), "upload"
        except PclError as error:
            yield label, error, "upload"
    elif kind == "tree":
        for rel_path, item in sources.documents_from_tree(value):
            yield rel_path, item, "tree"
    elif kind == "document":
        loaded = util.read_json(value)
        yield _usable_path(loaded) or PurePath(str(value)).name, loaded, "upload"
    else:
        from . import adapters_raman
        instrument = getattr(args, "instrument", None) or "raman-portable"
        for upload_id, receipt in adapters_raman.list_receipts(value):
            if isinstance(receipt, PclError):
                yield "upload " + upload_id, receipt, "upload"
                continue
            try:
                made = adapters_raman.document_from_receipt(
                    receipt, settings, instrument=instrument,
                    scan_sessions_dir=getattr(args, "scan_sessions", None))
            except PclError as error:
                yield "upload " + upload_id, error, "upload"
                continue
            yield made["location"]["path"], made, "upload"


def cmd_register(args, settings, *, out=None):
    """`pcldb register ...`: one line per session and a total. Returns 0 when nothing was
    refused, 1 otherwise."""
    replace = bool(getattr(args, "replace", False))
    kind, value = _chosen_source(args)
    server_url = getattr(args, "server", None) or settings.server_url
    if server_url:
        from .client import Client
        client = Client(server_url, _read_token(args, settings))
        store = None
    else:
        from .store import open_store
        store = open_store(settings)
        if not store.is_initialized():
            with store.app():     # raises the store's own sentence: not set up, or another version
                pass
            raise PclError("The database is not set up yet. Run: pcldb setup",
                           code="not_initialized", status=503)
    counts = {"created": 0, "updated": 0, "unchanged": 0, "refused": 0}
    for label, item, source in _documents(kind, value, args, settings):
        try:
            if isinstance(item, PclError):
                raise item
            if store is None:
                result = client.register(item, replace=replace)
            else:
                result = register(store, item, actor=LOCAL_ACTOR, source=source,
                                  settings=settings, replace=replace)
        except PclError as error:
            counts["refused"] += 1
            _say("refused    %s: %s" % (label, error.message), out)
            continue
        action = result.get("action")
        counts[action if action in counts else "created"] += 1
        _say("%-10s %s  (%d file(s), %d bytes)" % (action, result.get("path", label),
                                                   result.get("n_files") or 0,
                                                   result.get("bytes") or 0), out)
    total = sum(counts.values())
    _say("Total: %d session(s): %d created, %d updated, %d unchanged, %d refused."
         % (total, counts["created"], counts["updated"], counts["unchanged"], counts["refused"]),
         out)
    if total == 0:
        _say("Nothing to register was found.", out)
    return 1 if counts["refused"] else 0
