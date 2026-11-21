"""The Raman controller's uploads: read its upload records and make registration documents.

The Raman controller edition (macOS) leaves one folder per upload in
`<controller home>/data_catalog/arc_uploads/<upload id>/` with three plain files that matter
here: `record.json` (schema `raman-arc-upload/1`), `raman_upload_manifest.json`
(`raman-arc-upload-manifest/1`) and `SHA256SUMS`. This adapter only READS those files, with the
standard library, on any system. It never imports or runs the controller and never writes into
its folders.

`list_receipts` turns each record that proves a checked copy on ARC into a neutral receipt. A
receipt is made only when the record is of the known schema, is not a demo (a demo upload is on
this computer, not on ARC), has reached the state `verified`, and its manifest and `SHA256SUMS`
agree with each other and with the record. Everything else is a refusal that says which rule
failed. `document_from_receipt` turns a receipt into a registration document.

Privacy: a record also holds the uploader's Globus identity, ARC user name, this computer's
collection id and local paths. A receipt is built by picking the needed values one by one, so
none of those can get into it, into a document or into a message.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path

from . import __version__, util
from .errors import PclError
from .sources import parse_json_bytes, parse_sums

RECORD_SCHEMA = "raman-arc-upload/1"
MANIFEST_SCHEMA = "raman-arc-upload-manifest/1"
RECEIPT_SCHEMA = "pcl-upload-receipt/1"
DOCUMENT_SCHEMA = "pcl-registration/1"

RECORD_NAME = "record.json"
MANIFEST_NAME = "raman_upload_manifest.json"
SUMS_NAME = "SHA256SUMS"
UPLOADS_FOLDER = ("data_catalog", "arc_uploads")
SESSION_FILE = "session.json"
CARD_FORMAT = "raman-session/1 folder"

TRANSPORTS = ("globus", "ssh")
CHECK_METHODS = ("sha256-on-arc", "globus-checksum", "globus-verify")
COMPLETION = {"finished": "complete", "stopped": "partial", "failed": "failed",
              "ended": "unknown"}

MAX_RECORD_BYTES = 16 * 1024 * 1024
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MAX_SESSION_BYTES = 4 * 1024 * 1024

_UPLOAD_ID = re.compile(r"u[0-9a-f]{12}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class _Refused(Exception):
    """One rule failed. The text is the sentence; it never holds a value from the record that
    could be personal."""


def _no(sentence):
    return _Refused(sentence)


def _read_plain(path, limit, what):
    """The bytes of a plain file (never a link)."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise _no("it has no %s" % what) from None
    except OSError as error:
        raise _no("%s could not be read (%s)" % (what, error.strerror or "the system refused")) from None
    if not stat.S_ISREG(info.st_mode):
        raise _no("%s is a link or not a plain file; only plain files are read" % what)
    if info.st_size > limit:
        raise _no("%s is larger than %d bytes" % (what, limit))
    try:
        with open(path, "rb") as handle:
            return handle.read(limit + 1)
    except OSError as error:
        raise _no("%s could not be read (%s)" % (what, error.strerror or "the system refused")) from None


def _json_object(data, what):
    try:
        value = parse_json_bytes(data, what)
    except PclError as error:
        raise _no(error.message.rstrip(".")) from None
    if not isinstance(value, dict):
        raise _no("%s does not hold a JSON object" % what)
    return value


def _text(value):
    return value if isinstance(value, str) and value else None


def _whole(value):
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _utc(value):
    """The millisecond UTC text of a time in the record, or None when it is not one."""
    if not isinstance(value, str):
        return None
    try:
        return util.utc_text(value)
    except PclError:
        return None


def _safe_rel_path(path):
    """True for a relative POSIX path a file list can hold."""
    if not isinstance(path, str) or not path or len(path) > 1024 or "\\" in path:
        return False
    try:
        path.encode("utf-8")
    except UnicodeEncodeError:
        return False
    if any(ord(character) < 32 or ord(character) == 127 for character in path):
        return False
    return all(part not in ("", ".", "..") and len(part) <= 255 for part in path.split("/"))


def _is_real_folder(path):
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or util.is_junction(info):
        return False
    is_junction = getattr(os.path, "isjunction", None)
    return not (is_junction and is_junction(path))


def last_check_failed(checks):
    """True when the newest check that could look at the copy on ARC did not find it right.

    `checks` is the record's list, newest last. An entry with `"looked": false` could not look
    and says nothing about the copy.
    """
    if not isinstance(checks, list):
        return False
    for check in reversed(checks):
        if isinstance(check, dict) and check.get("looked") is not False:
            return check.get("ok") is False
    return False


def _receipt(folder, upload_id):
    """The receipt of one record folder, or _Refused with the rule that failed."""
    record = _json_object(_read_plain(folder / RECORD_NAME, MAX_RECORD_BYTES, RECORD_NAME),
                          RECORD_NAME)
    if record.get("schema") != RECORD_SCHEMA:
        raise _no("its record is not of the schema %s (made by another version of the "
                  "controller)" % RECORD_SCHEMA)
    if record.get("upload_id") != upload_id:
        raise _no("its record names another upload id than the folder")
    if record.get("demo") is not False:
        raise _no("it is a demo upload: its files are on the controller's computer, not on ARC"
                  if record.get("demo") is True else
                  "its record does not say that it is not a demo upload")
    if record.get("state") != "verified":
        raise _no("the upload has not reached the state \"verified\" (on ARC and checked)")
    transport = record.get("transport")
    if transport not in TRANSPORTS:
        raise _no("its record names a transport other than globus or ssh")
    destination = record.get("destination")
    if not isinstance(destination, dict):
        raise _no("its record has no destination")
    placed_as, final_path = _text(destination.get("placed_as")), _text(destination.get("final_path"))
    if placed_as is None or final_path is None:
        raise _no("its record does not say where the folder was placed "
                  "(destination.placed_as and destination.final_path)")
    if not (final_path.startswith("/") and _safe_rel_path(final_path[1:])):
        raise _no("the placed folder's path in its record is not a plain absolute path")
    if final_path.rsplit("/", 1)[1] != placed_as:
        raise _no("the placed folder's path in its record does not end with the name the "
                  "folder was placed as")
    area = destination.get("area")
    if area not in ("real", "simulated"):
        raise _no("its record does not say whether the session went to the real or the "
                  "simulated area")
    verification = record.get("verification")
    if not isinstance(verification, dict):
        raise _no("its record has no verification (how the copy on ARC was checked)")
    method = verification.get("method")
    if method not in CHECK_METHODS:
        raise _no("its record names a way of checking that this version does not know")
    session = record.get("session")
    if not isinstance(session, dict):
        raise _no("its record has no session")
    session_folder = _text(session.get("folder"))
    if session_folder is None or not _SESSION_NAME.fullmatch(session_folder):
        raise _no("the session folder's name in its record is not a plain session name")
    payload = record.get("payload")
    manifest_sha256 = payload.get("manifest_sha256") if isinstance(payload, dict) else None
    if not (isinstance(manifest_sha256, str) and _SHA256.fullmatch(manifest_sha256)):
        raise _no("its record has no SHA-256 of the manifest (payload.manifest_sha256)")

    manifest_bytes = _read_plain(folder / MANIFEST_NAME, MAX_MANIFEST_BYTES, MANIFEST_NAME)
    if util.sha256_hex(manifest_bytes) != manifest_sha256:
        raise _no("the kept %s is not the one the record names (its SHA-256 differs from "
                  "payload.manifest_sha256)" % MANIFEST_NAME)
    manifest = _json_object(manifest_bytes, MANIFEST_NAME)
    if manifest.get("schema") != MANIFEST_SCHEMA:
        raise _no("its manifest is not of the schema %s" % MANIFEST_SCHEMA)
    if manifest.get("upload_id") != upload_id:
        raise _no("its manifest names another upload id than the record")
    listed = manifest.get("files")
    if not isinstance(listed, list):
        raise _no("its manifest has no list of files")
    files, digests = [], {}
    for entry in listed:
        if not isinstance(entry, dict):
            raise _no("its manifest's list of files holds something that is not a file entry")
        path, size, digest = entry.get("path"), _whole(entry.get("bytes")), entry.get("sha256")
        if not _safe_rel_path(path) or path in (MANIFEST_NAME, SUMS_NAME) or path in digests:
            raise _no("its manifest lists a file name that cannot be used (not a plain "
                      "relative path, listed twice, or the name of the manifest itself)")
        if size is None or not (isinstance(digest, str) and _SHA256.fullmatch(digest)):
            raise _no("its manifest lists a file without a size or without a SHA-256")
        digests[path] = digest
        files.append({"rel_path": path, "bytes": size, "sha256": digest})

    sums_bytes = _read_plain(folder / SUMS_NAME, MAX_MANIFEST_BYTES, SUMS_NAME)
    sums, malformed = parse_sums(sums_bytes)
    expected = dict(digests)
    expected[MANIFEST_NAME] = manifest_sha256
    if malformed or sums != expected:
        raise _no("the kept %s does not list exactly the manifest's files with the same "
                  "SHA-256 values, plus the manifest itself" % SUMS_NAME)
    sums_sha256 = util.sha256_hex(sums_bytes)
    files.append({"rel_path": MANIFEST_NAME, "bytes": len(manifest_bytes),
                  "sha256": manifest_sha256})
    files.append({"rel_path": SUMS_NAME, "bytes": len(sums_bytes), "sha256": sums_sha256})
    files.sort(key=lambda entry: entry["rel_path"])

    darks = manifest.get("darks")
    darks_sent = sum(1 for dark in darks if isinstance(dark, dict) and dark.get("sent") is True) \
        if isinstance(darks, list) else 0
    sample_id = session.get("sample_id")
    if isinstance(sample_id, bool) or not isinstance(sample_id, (str, int)):
        sample_id = None
    # Every value is picked by name. Nothing else of the record is carried along: not its
    # globus or ssh blocks, not settings_used, not the local paths of a demo.
    return {
        "schema": RECEIPT_SCHEMA,
        "upload_id": upload_id,
        "transport": transport,
        "updated_utc": _utc(record.get("updated_utc")),
        "session": {
            "folder": session_folder,
            "session_id": _text(session.get("session_id")),
            "scan_mode": _text(session.get("scan_mode")),
            "started_utc": _utc(session.get("started_utc")),
            "sample_id": sample_id,
            "points": _whole(session.get("points")),
            "points_planned": _whole(session.get("points_planned")),
            "end": _text(session.get("end")),
            "kind": _text(session.get("kind")),
            "kind_declared_by_uploader": session.get("kind_declared_by_uploader") is True,
        },
        "destination": {"final_path": final_path, "placed_as": placed_as, "area": area},
        "verification": {"method": method, "algorithm": _text(verification.get("algorithm")),
                         "utc": _utc(verification.get("utc"))},
        "last_check_failed": last_check_failed(record.get("checks")),
        "made_utc": _utc(manifest.get("made_utc")),
        "darks_sent": darks_sent,
        "manifest_sha256": manifest_sha256,
        "sums_sha256": sums_sha256,
        "files": files,
    }


def list_receipts(controller_home):
    """Every upload record of a controller home, as (upload_id, receipt | PclError).

    Looks at the real folders (not links) named `u` + 12 hex digits in
    `<controller home>/data_catalog/arc_uploads/`; names that start with a dot are folders
    still being made and are skipped. A record that does not prove a checked copy on ARC yields
    a PclError that says which rule failed, and the walk goes on.
    """
    uploads = Path(controller_home).joinpath(*UPLOADS_FOLDER)
    if not _is_real_folder(uploads):
        raise PclError("No upload records were found: the controller home has no folder "
                       "data_catalog/arc_uploads. Give the Raman controller's home folder (the "
                       "one that holds data_catalog and scan_sessions).", code="file_missing")
    try:
        names = sorted(entry.name for entry in os.scandir(uploads))
    except OSError as error:
        raise PclError("The upload records could not be listed (%s). They belong to the "
                       "account that runs the controller."
                       % (error.strerror or "the system refused"), code="bad_file") from None
    for name in names:
        if name.startswith(".") or not _UPLOAD_ID.fullmatch(name):
            continue
        folder = uploads / name
        try:
            if not _is_real_folder(folder):
                raise _no("it is a link or not a folder; only real folders are read")
            yield name, _receipt(folder, name)
        except _Refused as refusal:
            yield name, PclError("The upload %s was not registered: %s." % (name, refusal),
                                 code="upload_not_usable", details={"upload_id": name})


def _relative_to_project(final_path, project_path, upload_id):
    project = project_path.rstrip("/")
    if project and not final_path.startswith(project + "/"):
        raise PclError("The upload %s was placed in %s, which is not below the project folder "
                       "%s. Check storage.project_path in the settings; nothing was registered."
                       % (upload_id, json.dumps(final_path), json.dumps(project_path)),
                       code="bad_path", details={"upload_id": upload_id})
    return final_path[len(project) + 1:]


def _enrichment(scan_sessions_dir, folder, sent_sha256):
    """The personal fields of the session's own session.json, but only when the local file is
    byte for byte the one that was sent (its SHA-256 equals the manifest's entry)."""
    if scan_sessions_dir is None or sent_sha256 is None:
        return {}
    path = Path(scan_sessions_dir) / folder / SESSION_FILE
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SESSION_BYTES:
            return {}
        with open(path, "rb") as handle:
            data = handle.read(MAX_SESSION_BYTES + 1)
    except OSError:
        return {}
    if util.sha256_hex(data) != sent_sha256:
        return {}
    try:
        session = parse_json_bytes(data, SESSION_FILE)
    except PclError:
        return {}
    if not isinstance(session, dict):
        return {}
    found = {}
    for name, limit in (("operator", 256), ("location", 256), ("notes", 20000)):
        value = session.get(name)
        if isinstance(value, str) and value and len(value) <= limit and _storable(value):
            found[name] = value
    if isinstance(session.get("instrument"), dict) and session["instrument"]:
        found["instrument_info"] = session["instrument"]
    return found


def _storable(text):
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return not any(ord(character) < 32 and character not in "\t\n\r" for character in text)


def document_from_receipt(receipt, settings, *, instrument="raman-portable",
                          scan_sessions_dir=None):
    """The registration document of one upload receipt.

    The session's place is the record's placed folder, made relative to
    `storage.project_path`. The card is built from what the record and the manifest froze when
    the copy was made. With `scan_sessions_dir`, the operator, location, notes and instrument
    details are also taken from `<scan_sessions_dir>/<folder>/session.json`, but only when
    that file is exactly the one that was sent.
    """
    upload_id = receipt["upload_id"]
    session, destination = receipt["session"], receipt["destination"]
    path = _relative_to_project(destination["final_path"], settings.storage.project_path,
                                upload_id)
    if not session.get("session_id"):
        raise PclError("The upload %s was not registered: its record has no session id."
                       % upload_id, code="upload_not_usable", details={"upload_id": upload_id})
    if not session.get("started_utc"):
        raise PclError("The upload %s was not registered: its record has no start time for the "
                       "session (the session's own session.json had none), and a session is "
                       "found by its start time. Register it by hand with a card that gives "
                       "started_utc." % upload_id, code="upload_not_usable",
                       details={"upload_id": upload_id})
    if not receipt.get("made_utc"):
        raise PclError("The upload %s was not registered: its manifest does not say when the "
                       "copy was made (made_utc)." % upload_id, code="upload_not_usable",
                       details={"upload_id": upload_id})
    card = {
        "schema": "pcl-session/1",
        "instrument": instrument,
        "session": session["folder"],
        "session_id": session["session_id"],
        "started_utc": session["started_utc"],
        "simulated": destination["area"] == "simulated",
        "sample_ids": [],
    }
    sample_id = session.get("sample_id")
    if sample_id is not None and str(sample_id).strip():
        card["sample_ids"] = [str(sample_id).strip()]
    digests = {entry["rel_path"]: entry["sha256"] for entry in receipt["files"]}
    personal = _enrichment(scan_sessions_dir, session["folder"], digests.get(SESSION_FILE))
    for name in ("operator", "location", "notes"):
        if name in personal:
            card[name] = personal[name]
    if session.get("scan_mode"):
        card["method"] = session["scan_mode"]
    if session.get("points") is not None:
        card["items"] = session["points"]
    if session.get("points_planned") is not None:
        card["items_planned"] = session["points_planned"]
    card["completion"] = COMPLETION.get(session.get("end"), "unknown")
    card["format"] = CARD_FORMAT
    if "instrument_info" in personal:
        card["instrument_info"] = personal["instrument_info"]
    card["uploaded_utc"] = receipt["made_utc"]
    card["upload_id"] = upload_id
    card["written_by"] = {"name": "pcldb", "version": __version__}
    card["extra"] = {"kind": session.get("kind"), "end": session.get("end"),
                     "kind_declared_by_uploader": bool(session.get("kind_declared_by_uploader")),
                     "darks_sent": receipt.get("darks_sent", 0)}
    verification = receipt["verification"]
    return {
        "schema": DOCUMENT_SCHEMA,
        "location": {"path": path, "collection_id": None, "project_path": None},
        "card": card,
        "files": [dict(entry) for entry in receipt["files"]],
        "sums_sha256": receipt["sums_sha256"],
        "upload": {"transport": receipt["transport"], "upload_id": upload_id,
                   "checked": not receipt.get("last_check_failed", False),
                   "check_method": verification["method"],
                   "checked_utc": verification.get("utc")},
    }
