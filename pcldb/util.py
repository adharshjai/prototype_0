"""Helpers every module shares: the one time format, canonical JSON, hashes, atomic writes, ids.

Times. Inside pcldb a time is always the text `YYYY-MM-DDTHH:MM:SS.mmmZ`: UTC, milliseconds,
24 characters, so texts sort like times. `utc_text` makes that form from an aware datetime or
from any form a lab card may hold (0 to 9 fraction digits, capital T and Z); it cuts, it never
rounds. Search entries carry the second form (`second_text`).

Files. Everything is written as UTF-8 bytes with `\\n` line ends, to a temporary file beside the
target, then moved over it with `os.replace` (atomic on macOS, Linux and Windows).
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import random
import re
import secrets
import socket
import time
import uuid
from pathlib import Path

from .errors import PclError

UTC = datetime.timezone.utc

# The card's time form (schemas/pcl_session.schema.json): always used with fullmatch().
_UTC_TEXT = re.compile(
    r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.([0-9]{1,9}))?Z")

_EXAMPLE_TIME = "2026-11-17T03:50:15.281Z"


def _show(value, limit=60):
    """A short, safe picture of a value for an error message."""
    text = repr(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


# ---------------------------------------------------------------------- time


def parse_utc(value):
    """An aware UTC datetime for a UTC text (0 to 9 fraction digits, Z) or an aware datetime.

    Digits after the sixth are dropped. Anything else is a PclError: another text form, a date
    that does not exist, a datetime without a time zone (it would silently be read as local
    time).
    """
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise PclError("The time %s has no time zone. Times must be UTC, like %s."
                           % (value.isoformat(), _EXAMPLE_TIME), code="bad_time")
        try:
            return value.astimezone(UTC)
        except (OverflowError, ValueError):
            raise PclError("The time %s is outside the years 0001 to 9999." % _show(value),
                           code="bad_time") from None
    if not isinstance(value, str):
        raise PclError("%s is not a time. Write a time like %s." % (_show(value), _EXAMPLE_TIME),
                       code="bad_time")
    match = _UTC_TEXT.fullmatch(value)
    if match is None:
        raise PclError("%s is not a UTC time. Write it like %s (ISO 8601 with T and Z)."
                       % (_show(value), _EXAMPLE_TIME), code="bad_time")
    year, month, day, hour, minute, second = (int(part) for part in match.groups()[:6])
    micro = int(((match.group(7) or "") + "000000")[:6])
    try:
        return datetime.datetime(year, month, day, hour, minute, second, micro, tzinfo=UTC)
    except ValueError:
        raise PclError("%s is not a real date and time." % _show(value), code="bad_time") from None


def utc_text(value):
    """`YYYY-MM-DDTHH:MM:SS.mmmZ` for a UTC text or an aware datetime (see parse_utc)."""
    moment = parse_utc(value)
    return "%04d-%02d-%02dT%02d:%02d:%02d.%03dZ" % (
        moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second,
        moment.microsecond // 1000)


def second_text(value):
    """`YYYY-MM-DDTHH:MM:SSZ` (cut to the second): the form Search entries carry."""
    moment = parse_utc(value)
    return "%04d-%02d-%02dT%02d:%02d:%02dZ" % (
        moment.year, moment.month, moment.day, moment.hour, moment.minute, moment.second)


def now_utc_text():
    """The time now, in the one text form."""
    return utc_text(datetime.datetime.now(UTC))


# ---------------------------------------------------------------------- JSON, hashes, ids


def canonical_json(obj):
    """One fixed JSON text for a value: sorted keys, no spaces, non-ASCII letters kept as they are.

    The same value always gives the same text, so the text can be hashed and compared. NaN and
    Infinity are refused (they are not JSON), and so is text that cannot be written as UTF-8.
    """
    try:
        text = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                          allow_nan=False)
    except ValueError as error:
        raise PclError("This value cannot be written as JSON: it holds NaN or Infinity, or it "
                       "contains itself (%s)." % error, code="bad_json") from None
    except TypeError as error:
        raise PclError("This value cannot be written as JSON (%s)." % error,
                       code="bad_json") from None
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        raise PclError("This value cannot be written as JSON: it holds a broken character "
                       "(half of a surrogate pair).", code="bad_json") from None
    return text


def sha256_hex(data):
    """The SHA-256 of bytes as 64 lower-case hex digits. Text must be encoded first."""
    return hashlib.sha256(data).hexdigest()


def new_id():
    """A new random id: 32 hex digits (a version 4 UUID without dashes)."""
    return uuid.uuid4().hex


def like_escape(text):
    """`text` with the LIKE wildcards made literal. Use it with: ... LIKE ? ESCAPE '\\'

    Both engines then match the same rows: SQLite has no escape character unless the statement
    names one.
    """
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


# ---------------------------------------------------------------------- files


def _refuse_constant(name):
    raise ValueError("it holds %s, which is not valid JSON" % name)


def read_json(path):
    """The content of a JSON file that a person may have edited.

    Reads UTF-8 and accepts a byte order mark (Windows Notepad writes one). A missing or
    unreadable file, text that is not JSON, and the constants NaN and Infinity are a PclError
    that names the file.
    """
    path = Path(path)
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            text = handle.read()
    except FileNotFoundError:
        raise PclError("%s does not exist." % path, code="file_missing") from None
    except UnicodeDecodeError:
        raise PclError("%s is not UTF-8 text. Save it as UTF-8 and try again." % path,
                       code="bad_file") from None
    except OSError as error:
        raise PclError("%s could not be read (%s)." % (path, error.strerror or error),
                       code="bad_file") from None
    try:
        return json.loads(text, parse_constant=_refuse_constant)
    except (ValueError, RecursionError) as error:
        raise PclError("%s is not valid JSON (%s)." % (path, error), code="bad_json") from None


_REPLACE_TRIES = 10
_REPLACE_WAIT_SECONDS = 0.1


def _replace(temporary, path):
    """os.replace with a short wait for Windows, where a virus scanner, the search indexer or an
    editor can hold the target open for a moment."""
    for attempt in range(_REPLACE_TRIES + 1):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == _REPLACE_TRIES:
                raise PclError(
                    "%s could not be written: another program has it open, or this folder cannot "
                    "be written to. Close the program that uses the file and try again." % path,
                    code="file_in_use", status=503) from None
            time.sleep(_REPLACE_WAIT_SECONDS)


def write_bytes_atomic(path, data, mode=None):
    """Write bytes to `path` so that a reader sees either the old file or the whole new one.

    `mode` (for example 0o600) is applied where the system has file modes; the file never
    exists with wider rights, not even for a moment. Missing parent folders are made.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(".%s.%s.tmp" % (path.name, secrets.token_hex(4)))
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(temporary, flags, 0o666 if mode is None else mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None and os.name != "nt":
            os.chmod(temporary, mode)
        _replace(temporary, path)
    finally:
        try:
            os.unlink(temporary)
        except OSError:
            pass  # moved into place, or Windows still holds it; nothing more to do here


IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003   # stat.IO_REPARSE_TAG_MOUNT_POINT (Windows only)


def is_junction(info):
    """True for a Windows directory junction, from what os.lstat or
    DirEntry.stat(follow_symlinks=False) returned. Python before 3.12 has no is_junction and
    reports a junction as a plain folder; its reparse tag still says what it is."""
    return getattr(info, "st_reparse_tag", 0) == IO_REPARSE_TAG_MOUNT_POINT


# How to run install.py here: a stock Mac has python3 and no python; Windows has the py launcher.
INSTALL_PY = "py -3 install.py" if os.name == "nt" else "python3 install.py"
# How to run pcldb from the project folder: the launcher of this system.
LAUNCHER = ".\\pcldb.cmd" if os.name == "nt" else "./pcldb.sh"


def write_text_atomic(path, text, mode=None):
    """Write text as UTF-8, exactly as given (`\\n` stays `\\n` on every system), atomically."""
    write_bytes_atomic(path, text.encode("utf-8"), mode)


def write_json_atomic(path, obj, mode=None):
    """Write a value as JSON a person can read (indent 2, UTF-8, a final newline), atomically."""
    try:
        text = json.dumps(obj, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    except (TypeError, ValueError) as error:
        raise PclError("%s was not written: the value cannot be written as JSON (%s)."
                       % (Path(path).name, error), code="bad_json") from None
    write_text_atomic(path, text, mode)


# ---------------------------------------------------------------------- ports

# Ports pcldb uses by default; tests must never touch them.
RESERVED_PORTS = frozenset({8787, 15439, 54329})
_handed_out = set()


def port_answers(port, host="127.0.0.1", timeout=0.5):
    """True when something accepts TCP connections on host:port."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _can_bind(port, host):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        if os.name != "nt":
            # As PostgreSQL and Python's servers do: a port that was closed a moment ago counts
            # as free. (On Windows this option would let the probe bind a port that is taken.)
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, port))
        except OSError:
            return False
    return True


def port_in_use(port, host="127.0.0.1"):
    """True when something listens on host:port, or the port cannot be bound right now."""
    return port_answers(port, host) or not _can_bind(port, host)


def free_port(host="127.0.0.1"):
    """A TCP port on this computer that is free now and that this process has not handed out.

    Picks below 49152 where it can (Windows hands out client ports above that), and never one
    of pcldb's default ports. Only binds to look (a connection attempt to a closed port takes
    half a second on Windows, and tests ask for many ports).
    """
    chooser = random.SystemRandom()
    for _ in range(200):
        port = chooser.randrange(20000, 49000)
        if port in RESERVED_PORTS or port in _handed_out or not _can_bind(port, host):
            continue
        _handed_out.add(port)
        return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        port = probe.getsockname()[1]
    _handed_out.add(port)
    return port
