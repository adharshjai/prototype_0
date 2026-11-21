"""Sources: make registration documents from session folders on this computer.

A session folder here is a copy of what is (or will be) on ARC: the session's files, the lab
card `pcl_session.json` and the checksum list `SHA256SUMS`. `document_from_folder` reads the
card, walks the folder for names and sizes, and takes each file's SHA-256 from `SHA256SUMS`.
No file is hashed here (only `SHA256SUMS` itself, whose own SHA-256 identifies the contents).
`documents_from_tree` does that for every session folder of a tree laid out like ARC:
`raw/<instrument>/<folder>` and `raw/<instrument>/simulated/<folder>`.

`SHA256SUMS` has one line per file, `<64 hex digits><two spaces><relative path>`, with `/`
between folders, and lists every file except itself. The reader here accepts what the proof of
concept's reader accepts: CRLF line ends, blank lines, upper-case hex, ` *` in place of the two
spaces, a leading `./`. A line it cannot read (one that starts with a byte order mark, for
example) is skipped, and the file it named shows as not listed.

Paths inside documents are POSIX text on every system: they are joined with `/` from the
names the folder walk gives, never taken from `str(path)`.
"""

from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path, PurePath

from . import util
from .errors import PclError

CARD_NAME = "pcl_session.json"
SUMS_NAME = "SHA256SUMS"
INCOMING = "_incoming"
SIMULATED = "simulated"
DOCUMENT_SCHEMA = "pcl-registration/1"

MAX_FILES = 100000
MAX_CARD_BYTES = 4 * 1024 * 1024
MAX_SUMS_BYTES = 64 * 1024 * 1024

# Files the operating system drops into a folder that someone looked at. They are not part of
# the session (the placement leaves them out), so a local copy may hold them without harm.
_OS_FILES = frozenset(["Thumbs.db", "desktop.ini", "Icon\r"])

_SUMS_LINE = re.compile(r"([0-9a-fA-F]{64}) [ *](.+)")


def posix_text(path):
    """A path argument as POSIX text. A path object (also a Windows one) gives its parts joined
    with `/`; text is returned exactly as it is, never normalised."""
    if isinstance(path, PurePath):
        return path.as_posix()
    return path


def _show(value, limit=120):
    try:
        text = json.dumps(str(value), ensure_ascii=True)
    except (TypeError, ValueError):
        text = ascii(value)
    except RecursionError:
        text = "(a deeply nested value)"
    return text if len(text) <= limit else text[:limit - 3] + "..."


def is_machinery(name):
    """Names that start with _ or . are machinery or hidden, never data."""
    return name.startswith(("_", "."))


def _plain_part(name):
    """A file or folder name a checksum list can hold: valid UTF-8, no control character, no
    backslash."""
    try:
        name.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return bool(name) and name not in (".", "..") and not any(
        ord(character) < 32 or ord(character) == 127 or character == "\\" for character in name)


def _is_real_folder(path):
    """True for a folder that is not a link (and not a Windows junction)."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISDIR(info.st_mode) or util.is_junction(info):
        return False
    is_junction = getattr(os.path, "isjunction", None)
    return not (is_junction and is_junction(path))


def _read_small(path, limit, what):
    """The bytes of a plain file (never a link) of at most `limit` bytes."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        raise PclError("%s does not exist." % what, code="file_missing") from None
    except OSError as error:
        raise PclError("%s could not be read (%s)." % (what, error.strerror or error),
                       code="bad_file") from None
    if not stat.S_ISREG(info.st_mode):
        raise PclError("%s is a link or not a plain file. Only plain files are read."
                       % what, code="bad_file")
    if info.st_size > limit:
        raise PclError("%s is larger than %d bytes, which is not plausible for this file."
                       % (what, limit), code="bad_file")
    try:
        with open(path, "rb") as handle:
            return handle.read(limit + 1)
    except OSError as error:
        raise PclError("%s could not be read (%s)." % (what, error.strerror or error),
                       code="bad_file") from None


def _refuse_constant(name):
    raise ValueError("it holds %s, which is not valid JSON" % name)


def parse_json_bytes(data, what):
    """A JSON value from UTF-8 bytes (a byte order mark is accepted). NaN and Infinity, bytes
    that are not UTF-8 and text that is not JSON are a PclError that names the file."""
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise PclError("%s is not UTF-8 text." % what, code="bad_file") from None
    try:
        return json.loads(text, parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as error:
        raise PclError("%s is not valid JSON (%s)." % (what, error), code="bad_json") from None


def read_card(path, what=None):
    """The lab card in the file `path`: a JSON object."""
    what = what or "The card %s" % _show(Path(path).name)
    card = parse_json_bytes(_read_small(path, MAX_CARD_BYTES, what), what)
    if not isinstance(card, dict):
        raise PclError("%s must hold one JSON object." % what, code="invalid_card")
    return card


def parse_sums(data):
    """({relative path: sha256}, [numbers of the lines that could not be read]) for the bytes
    of a SHA256SUMS file. The same path twice: the later line wins."""
    entries, malformed = {}, []
    text = data.decode("utf-8", errors="replace")
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        match = _SUMS_LINE.fullmatch(line)
        relative = match.group(2) if match else None
        while relative is not None and relative.startswith("./"):
            relative = relative[2:].lstrip("/")
        if not match or not relative or relative.startswith("/") or ".." in relative.split("/"):
            malformed.append(number)
            continue
        entries[relative] = match.group(1).lower()
    return entries, malformed


def list_files(folder):
    """[(relative POSIX path, size in bytes)] of the plain files below `folder`, each folder's
    entries in name order. Links are never followed: a link or a special file is a PclError,
    and so is a name that a checksum list cannot hold. Hidden names (they start with a dot) and
    the files an operating system leaves behind are not part of a session and are left out."""
    found = []

    def walk(directory, parts):
        try:
            entries = sorted(os.scandir(directory), key=lambda entry: entry.name)
        except OSError as error:
            raise PclError("The folder %s could not be listed (%s)."
                           % (_show("/".join(parts) or Path(folder).name),
                              error.strerror or error), code="bad_file") from None
        for entry in entries:
            if entry.name.startswith(".") or entry.name in _OS_FILES:
                continue
            relative = parts + [entry.name]
            shown = _show("/".join(relative))
            if not _plain_part(entry.name):
                raise PclError("The name %s holds a character that a checksum list cannot hold "
                               "(a backslash, a control character or a broken character). "
                               "Rename it before registering." % shown, code="bad_file")
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise PclError("%s could not be looked at (%s)."
                               % (shown, error.strerror or error), code="bad_file") from None
            is_junction = getattr(entry, "is_junction", None)
            if stat.S_ISDIR(info.st_mode) and not util.is_junction(info) \
                    and not (is_junction and is_junction()):
                walk(entry.path, relative)
            elif stat.S_ISREG(info.st_mode):
                found.append(("/".join(relative), info.st_size))
                if len(found) > MAX_FILES:
                    raise PclError("The folder holds more than %d files; that many cannot be "
                                   "registered for one session. Pack small files into an "
                                   "archive first." % MAX_FILES, code="too_many_files")
            else:
                raise PclError("%s is a link or a special file. A session folder holds plain "
                               "files only; nothing was read from it." % shown, code="bad_file")

    walk(str(folder), [])
    return found


def document_from_folder(folder, rel_path, card_path=None):
    """The registration document of one session folder on this computer.

    `folder` is the local copy; `rel_path` is where the session is below the project folder on
    ARC (POSIX text such as `raw/raman-portable/simulated/<folder>`). The card is read from
    `card_path` when given, else from `pcl_session.json` in the folder. Sizes come from the file
    system and SHA-256 values from the folder's `SHA256SUMS`: a file that is not listed there
    has `sha256` null, and a listed file that is missing is an error. Without a `SHA256SUMS`
    every `sha256` and `sums_sha256` are null.
    """
    folder = Path(folder)
    name = _show(folder.name)
    if not _is_real_folder(folder):
        raise PclError("%s is not a folder on this computer (or it is a link). Give the "
                       "session folder itself." % _show(folder), code="file_missing")
    if card_path is not None:
        card = read_card(card_path, "The card file %s" % _show(Path(card_path).name))
    elif os.path.lexists(folder / CARD_NAME):
        card = read_card(folder / CARD_NAME, "The card %s of %s" % (CARD_NAME, name))
    else:
        raise PclError("%s has no %s (the lab card). A session is registered with its card: "
                       "put the card into the folder, or give one with --card."
                       % (name, CARD_NAME), code="card_missing")
    files = list_files(folder)
    sums, sums_sha256 = {}, None
    if os.path.lexists(folder / SUMS_NAME):
        data = _read_small(folder / SUMS_NAME, MAX_SUMS_BYTES, "%s of %s" % (SUMS_NAME, name))
        sums, _malformed = parse_sums(data)
        sums_sha256 = util.sha256_hex(data)
    present = {relative for relative, _size in files}
    missing = sorted(relative for relative in sums if relative not in present)
    if missing:
        raise PclError("%s of %s lists %s, but the folder does not hold %s. The copy is not "
                       "complete, so nothing was registered. Copy the session again."
                       % (SUMS_NAME, name, ", ".join(_show(item) for item in missing[:5])
                          + (" and %d more" % (len(missing) - 5) if len(missing) > 5 else ""),
                          "it" if len(missing) == 1 else "them"), code="file_missing",
                       details={"missing": missing[:100]})
    listed = []
    for relative, size in sorted(files):
        digest = sums_sha256 if relative == SUMS_NAME else sums.get(relative)
        listed.append({"rel_path": relative, "bytes": size, "sha256": digest})
    return {"schema": DOCUMENT_SCHEMA,
            "location": {"path": posix_text(rel_path), "collection_id": None,
                         "project_path": None},
            "card": card, "files": listed, "sums_sha256": sums_sha256}


def _folders(directory):
    """The names of the real folders in `directory` that are data: sorted, no links, nothing
    that starts with _ or . (so never _incoming)."""
    try:
        names = sorted(entry.name for entry in os.scandir(directory))
    except OSError:
        return []
    return [name for name in names
            if not is_machinery(name) and name != INCOMING
            and _is_real_folder(os.path.join(directory, name))]


def documents_from_tree(root):
    """Every session folder of a tree laid out like ARC, as (rel_path, document | PclError).

    Walks `root/raw/<instrument>/<folder>` and `root/raw/<instrument>/simulated/<folder>` in
    name order. `_incoming` and every name that starts with `_` or `.` are skipped. A folder
    that cannot be read as a session (no card, for example) yields its PclError and the walk
    goes on.
    """
    root = Path(root)
    raw = root / "raw"
    if not _is_real_folder(raw):
        raise PclError("%s has no folder named raw. A tree is laid out like ARC: "
                       "raw/<instrument>/<session folder> and "
                       "raw/<instrument>/simulated/<session folder>." % _show(root),
                       code="file_missing")
    for instrument in _folders(raw):
        base = raw / instrument
        places = []
        for name in _folders(base):
            if name == SIMULATED:
                places.extend((["raw", instrument, SIMULATED, inner], base / SIMULATED / inner)
                              for inner in _folders(base / SIMULATED))
            else:
                places.append((["raw", instrument, name], base / name))
        for parts, folder in sorted(places):
            rel_path = "/".join(parts)
            try:
                yield rel_path, document_from_folder(folder, rel_path)
            except PclError as error:
                yield rel_path, error
