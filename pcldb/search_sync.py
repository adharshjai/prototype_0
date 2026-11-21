"""Sending the queued Search entries to Globus Search, and the `pcldb search` command.

Registration only QUEUES an entry (`search_entries.refresh` writes a row of
`app.search_entries` in the state `pending`). Nothing reaches Globus Search until somebody runs
`pcldb search push`. The life of a row:

    pending --ingest accepted--> sent --task SUCCESS--> synced
                                   |-----task FAILED or CANCELLED--> failed (last_error says why)
                                   |-----any other state--> still sent; asked again
                                   |-----not finished in time--> stays sent; the next push asks
                                                                 about the task again first
    delete_pending --delete accepted--> the row is removed

Globus Search works through a queue of its own: `ingest` only hands a batch over and returns a
task id; the entries can be found once that task reached SUCCESS. So `push` sends, then asks
about the tasks with a short growing pause (0.5 s, 1 s, 2 s, 4 s, then every 5 s) for at most
`wait_seconds`.

Who may see an entry: the row's `visible_to` (from the setting `search.visible_to`). When that
is empty the entry is sent visible to the signed-in person alone
(`urn:globus:auth:identity:<id>`). `public` and `all_authenticated_users` are never sent.

`failed` rows stay failed until their content changes or `pcldb search reindex` queues
everything again (also the way to fill a new index: a trial index can vanish).

`push(db, ...)` works inside the caller's transaction, as the specification says. The command
line uses `push_store(store, ...)` instead: the same steps, but every database step is its own
short transaction and no transaction is open while Globus is being talked to, so a slow network
never blocks a registration.
"""

from __future__ import annotations

import contextlib
import json
import sys
import time
from pathlib import Path

from . import util
from .errors import NotFound, PclError, Unavailable

STATES = ("pending", "sent", "synced", "failed", "delete_pending")
NEVER_VISIBLE_TO = ("public", "all_authenticated_users")
ENDED_BADLY = ("FAILED", "CANCELLED")    # with SUCCESS the only states that end a task

DEFAULT_BATCH = 100
MAX_BATCH = 1000
DEFAULT_WAIT_SECONDS = 60
BATCH_BYTES = 4_000_000            # one ingest request stays far below the service's 10 MB
FIRST_PAUSE_SECONDS = 0.5
LONGEST_PAUSE_SECONDS = 5.0
MAX_ERROR_CHARS = 2000

# Anyone who knows an index id can read its name and description: keep both bland.
DEFAULT_INDEX_NAME = "pcldb prototype"
DEFAULT_INDEX_DESCRIPTION = "Prototype index of session entries."

TABLE = "app.search_entries"


# ---------------------------------------------------------------------- counts


def status(db):
    """How many entries are in each state: always all five keys."""
    counts = {state: 0 for state in STATES}
    for row in db.query("SELECT state, COUNT(*) AS n_rows FROM {app}.search_entries "
                        "GROUP BY state"):
        if row["state"] in counts:
            counts[row["state"]] = int(row["n_rows"])
    return counts


def failures(db, limit=10):
    """The entries that could not be sent: [{"subject", "session_path", "last_error"}], sorted
    by session path, at most `limit`."""
    rows = db.query("SELECT subject, session_path, last_error FROM {app}.search_entries "
                    "WHERE state = ?", ["failed"])
    rows.sort(key=lambda row: (row["session_path"] or "", row["subject"]))
    return rows[:limit]


def reindex(db, settings=None):
    """Queue every entry again (state `pending`). Returns how many. Entries waiting to be
    removed keep waiting. Use it after making a new index: it starts empty.

    With `settings`, every entry is first built again from the database with the current
    settings (`search.visible_to`, `search.include_operator`, `search.include_notes`), so a
    changed setting reaches every entry."""
    if settings is not None:
        from . import search_entries
        search_entries.refresh(db, None, settings)
    now = db.param("utc", util.now_utc_text())
    return db.execute(
        "UPDATE {app}.search_entries SET state = ?, task_id = NULL, last_error = NULL, "
        "synced_utc = NULL, updated_utc = ? WHERE state <> ?", ["pending", now, "delete_pending"])


# ---------------------------------------------------------------------- push


def push(db, backend, *, batch=DEFAULT_BATCH, wait_seconds=DEFAULT_WAIT_SECONDS,
         sleep=None, clock=None):
    """Send what is queued to Globus Search, inside the caller's transaction.

    `backend` is a `search_globus.GlobusSearch`. Returns counts:
    `{"sent", "synced", "failed", "removed", "requeued", "waiting", "batches", "problems"}`
    (`sent`: entries handed over now; `waiting`: entries whose task had not finished when the
    time was up; `problems`: sentences about what stopped the push early, empty when all went
    well). Raises before anything is changed when nobody is signed in to Globus or no index is
    set. `sleep` and `clock` (default: `time.sleep`, `time.monotonic`) are there so that tests
    need not wait.
    """
    @contextlib.contextmanager
    def same():
        yield db
    return _push(same, backend, batch, wait_seconds, sleep, clock)


def push_store(store, backend, *, batch=DEFAULT_BATCH, wait_seconds=DEFAULT_WAIT_SECONDS,
               sleep=None, clock=None):
    """As `push`, with a short transaction of its own for every database step (see the module
    text). What was done stays done when a later step fails."""
    return _push(store.app, backend, batch, wait_seconds, sleep, clock)


def _cut(text):
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_ERROR_CHARS else text[:MAX_ERROR_CHARS - 3] + "..."


def _push(scope, backend, batch, wait_seconds, sleep, clock):
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    if isinstance(batch, bool) or not isinstance(batch, int) or not 1 <= batch <= MAX_BATCH:
        raise PclError("The batch size must be a whole number from 1 to %d." % MAX_BATCH,
                       code="bad_argument")
    if (isinstance(wait_seconds, bool) or not isinstance(wait_seconds, (int, float))
            or not 0 <= wait_seconds <= 3600):
        raise PclError("The waiting time must be a number of seconds from 0 to 3600.",
                       code="bad_argument")
    if not backend.index_id:
        raise PclError("No Globus Search index is set yet, so there is nowhere to send the "
                       "entries. Run: pcldb search create-index", code="no_search_index",
                       status=409)
    if not backend.signed_in():
        raise Unavailable("You are not signed in to Globus, so nothing was sent. Run: pcldb "
                          "search login", code="globus_sign_in")
    identity = backend.identity_urn()

    counts = {"sent": 0, "synced": 0, "failed": 0, "removed": 0, "requeued": 0, "waiting": 0,
              "batches": 0, "problems": []}
    work = _Work(scope, backend, counts)

    # 1. Tasks of an earlier push that had not finished: ask about them first.
    with scope() as db:
        earlier = db.query("SELECT subject, task_id FROM {app}.search_entries WHERE state = ?",
                           ["sent"])
    open_tasks = []
    lost = sorted(row["subject"] for row in earlier if not row["task_id"])
    for task_id in sorted({row["task_id"] for row in earlier if row["task_id"]}):
        open_tasks.append(task_id)
    if lost:
        with scope() as db:
            for subject in lost:
                counts["requeued"] += _requeue(db, "subject = ?", subject,
                                               "The task of an earlier push is not known; "
                                               "queued again.")
    open_tasks = work.ask(open_tasks)

    # 2. Entries whose session is gone: remove them from the index.
    if not work.stopped:
        with scope() as db:
            leaving = sorted(row["subject"] for row in db.query(
                "SELECT subject FROM {app}.search_entries WHERE state = ?", ["delete_pending"]))
        for subject in leaving:
            try:
                backend.delete_subject(subject)
            except PclError as error:
                work.stop("An entry could not be removed from the index: " + error.message)
                break
            with scope() as db:
                counts["removed"] += db.delete_where(TABLE, "subject = ? AND state = ?",
                                                     [subject, "delete_pending"])

    # 3. Queued entries: send them in batches.
    if not work.stopped:
        with scope() as db:
            queued = db.query("SELECT subject, content_json, visible_to, content_sha256 "
                              "FROM {app}.search_entries WHERE state = ?", ["pending"])
        queued.sort(key=lambda row: row["subject"])
        for rows, entries in _batches(queued, identity, batch, work):
            try:
                task_id = backend.ingest(entries)
            except PclError as error:
                if error.status in (400, 413):
                    # The service (or the check before it) refused this batch for what is in
                    # it. Sending it again cannot work: mark it, go on with the next batch.
                    work.fail_rows(rows, "Globus Search did not take the entry: " + error.message)
                    continue
                work.stop("The entries could not be sent: " + error.message)
                break
            counts["batches"] += 1
            now = util.now_utc_text()
            with scope() as db:
                for row in rows:
                    counts["sent"] += db.execute(
                        "UPDATE {app}.search_entries SET state = ?, task_id = ?, "
                        "last_error = NULL, updated_utc = ? "
                        "WHERE subject = ? AND state = ? AND content_sha256 = ?",
                        ["sent", task_id, db.param("utc", now), row["subject"], "pending",
                         row["content_sha256"]])
            open_tasks.append(task_id)

    # 4. Wait for the tasks, with a short growing pause.
    deadline = clock() + wait_seconds
    pause = FIRST_PAUSE_SECONDS
    while open_tasks and not work.stopped:
        left = deadline - clock()
        if left <= 0:
            break
        sleep(min(pause, left))
        pause = min(pause * 2, LONGEST_PAUSE_SECONDS)
        open_tasks = work.ask(open_tasks)

    with scope() as db:
        counts["waiting"] = status(db)["sent"]
    return counts


class _Work:
    """The shared pieces of one push: asking about tasks, marking rows, stopping early."""

    def __init__(self, scope, backend, counts):
        self.scope = scope
        self.backend = backend
        self.counts = counts
        self.stopped = False

    def stop(self, sentence):
        self.stopped = True
        self.counts["problems"].append(_cut(sentence))

    def fail_rows(self, rows, sentence):
        now = util.now_utc_text()
        with self.scope() as db:
            for row in rows:
                self.counts["failed"] += db.execute(
                    "UPDATE {app}.search_entries SET state = ?, last_error = ?, updated_utc = ? "
                    "WHERE subject = ? AND state = ? AND content_sha256 = ?",
                    ["failed", _cut(sentence), db.param("utc", now), row["subject"], "pending",
                     row["content_sha256"]])

    def ask(self, task_ids):
        """Ask the service about each task once. Returns the tasks that have not finished."""
        still_open = []
        for position, task_id in enumerate(task_ids):
            if self.stopped:
                still_open.extend(task_ids[position:])
                break
            try:
                answer = self.backend.task(task_id)
            except NotFound:
                # The service forgets a task after some weeks. What it did is not known.
                with self.scope() as db:
                    self.counts["requeued"] += _requeue(
                        db, "task_id = ?", task_id,
                        "Globus Search no longer knows the task of an earlier push; queued "
                        "again.")
                continue
            except PclError as error:
                self.stop("Globus Search could not be asked how far it is: " + error.message)
                still_open.extend(task_ids[position:])
                break
            state = answer.get("state")
            now = util.now_utc_text()
            if state == "SUCCESS":
                with self.scope() as db:
                    stamp = db.param("utc", now)
                    self.counts["synced"] += db.execute(
                        "UPDATE {app}.search_entries SET state = ?, synced_utc = ?, "
                        "updated_utc = ?, last_error = NULL WHERE state = ? AND task_id = ?",
                        ["synced", stamp, stamp, "sent", task_id])
            elif state not in ENDED_BADLY:
                # CREATING, PENDING, PROGRESS, or a state this program does not know: keep
                # asking until the time is up.
                still_open.append(task_id)
            else:
                reason = answer.get("message") or "Globus Search gave no reason."
                sentence = "The task ended as %s: %s" % (state, reason)
                with self.scope() as db:
                    self.counts["failed"] += db.execute(
                        "UPDATE {app}.search_entries SET state = ?, last_error = ?, "
                        "updated_utc = ? WHERE state = ? AND task_id = ?",
                        ["failed", _cut(sentence), db.param("utc", now), "sent", task_id])
        return still_open


def _requeue(db, where, value, sentence):
    return db.execute(
        "UPDATE {app}.search_entries SET state = ?, task_id = NULL, last_error = ?, "
        "updated_utc = ? WHERE state = ? AND " + where,
        ["pending", sentence, db.param("utc", util.now_utc_text()), "sent", value])


def _batches(queued, identity, batch, work):
    """Yield `(rows, entries)` batches of at most `batch` entries and about BATCH_BYTES bytes.
    Rows that must never be sent are marked failed here and left out."""
    rows, entries, size = [], [], 0
    for row in queued:
        visible_to = [value for value in (row["visible_to"] or [])] or [identity]
        content = row["content_json"]
        if any(not isinstance(value, str) or value.strip().lower() in NEVER_VISIBLE_TO
               for value in visible_to):
            work.fail_rows([row], "Entries are never public: visible_to must name the lab's "
                                  "Globus group or people, not everybody. Correct the setting "
                                  "search.visible_to, then run: pcldb search reindex")
            continue
        if not isinstance(content, dict) or not content:
            work.fail_rows([row], "The entry has no content to send.")
            continue
        entry = {"subject": row["subject"], "visible_to": visible_to, "content": content}
        entry_size = len(json.dumps(entry, ensure_ascii=False).encode("utf-8", "replace"))
        if rows and (len(rows) >= batch or size + entry_size > BATCH_BYTES):
            yield rows, entries
            rows, entries, size = [], [], 0
        rows.append(row)
        entries.append(entry)
        size += entry_size
    if rows:
        yield rows, entries


# ---------------------------------------------------------------------- the settings file


def save_setting(settings, dotted, value):
    """Write one key (for example `search.index_id`) into the settings file and leave every
    other key, and every note in it, as it is. Returns the file's path."""
    settings.changed({dotted: value})            # refuses a value the settings would not take
    path = settings.config_path
    data = util.read_json(path) if path.exists() else {}
    if not isinstance(data, dict):
        raise PclError("%s does not hold a group of settings written in { }, so %s was not "
                       "saved. Correct the file, or delete it and run: pcldb setup"
                       % (path, dotted), code="bad_setting")
    parts = dotted.split(".")
    node = data
    for part in parts[:-1]:
        inner = node.get(part)
        if inner is None:
            inner = node[part] = {}
        elif not isinstance(inner, dict):
            raise PclError("In %s the key %s is not a group written in { }, so %s was not "
                           "saved." % (path, part, dotted), code="bad_setting")
        node = inner
    node[parts[-1]] = value
    util.write_json_atomic(path, data)
    return path


def _cli_knows(action):
    """True when the command line (pcldb/cli.py) declares this action of `pcldb search`."""
    try:
        text = (Path(__file__).resolve().parent / "cli.py").read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False
    return ('"%s"' % action) in text or ("'%s'" % action) in text


def switch_command(backend="globus"):
    """The one command a person types to switch the search backend: `pcldb search use-globus`
    when the command line knows that action, else this module run directly (it always works,
    from the project folder)."""
    action = "use-" + backend
    if _cli_knows(action):
        return "pcldb search " + action
    program = sys.executable or "python"
    if " " in program:
        program = '"%s"' % program
    return "%s -m pcldb.search_sync %s" % (program, action)


FULL_WARNING_SHARE = 0.8


def index_lines(backend):
    """What Globus Search itself says about the index, as sentences for a person. Globus tells
    these facts to anybody who knows the index id, so no sign-in is needed. Never raises: when
    the network does not answer, the one line says so."""
    if not backend.index_id:
        return []
    try:
        info = backend.index_info()
    except PclError as error:
        return ["The index could not be asked right now: " + error.message]

    def number(key):
        value = info.get(key)
        return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None

    name = _plain(info.get("display_name") or "no name")
    line = "Index at Globus Search: \"%s\"%s" % (
        name, ", a trial index" if info.get("is_trial") is True else "")
    entries, size, cap = number("num_entries"), number("size_in_mb"), number("max_size_in_mb")
    if entries is not None:
        line += ", %d entries" % entries
    if size is not None and cap:
        line += ", %s of %s MB used" % (_megabytes(size), _megabytes(cap))
    lines = [line + "."]
    status = info.get("status")
    if isinstance(status, str) and status != "open":
        lines.append("The index is not open: Globus Search reports the status %s."
                     % _plain(status))
    if size is not None and cap and size > FULL_WARNING_SHARE * cap:
        lines.append("Warning: the index is %d %% full. Before sending more, ask Globus support "
                     "(through ARC, which holds Virginia Tech's subscription) for a larger index."
                     % round(100 * size / cap))
    if info.get("is_trial") is True:
        lines.append("Globus may delete a trial index from 30 days after it was made. The "
                     "database stays the record; pcldb search reindex fills a new index.")
    return lines


def _megabytes(value):
    return ("%.2f" % value).rstrip("0").rstrip(".") if isinstance(value, float) else str(value)


# ---------------------------------------------------------------------- the command line


def _plain(text):
    """Text for the console: ASCII only (a Windows console may know nothing else), and no
    control characters (text from a record must not be able to move the cursor)."""
    text = "".join(char if " " <= char != "\x7f" else " " for char in str(text))
    return text.encode("ascii", "backslashreplace").decode("ascii")


def _say(text="", stream=None):
    print(_plain(text), file=stream or sys.stdout)


def _open_store(settings):
    from .store import open_store
    store = open_store(settings)
    if not store.is_initialized():
        raise PclError("The database is not set up yet. Run: pcldb setup",
                       code="not_initialized", status=503)
    return store


def _backend(settings, backend):
    if backend is not None:
        return backend
    from . import search_globus
    return search_globus.GlobusSearch(settings)


def _read_request(text):
    if text is None or not str(text).strip():
        raise PclError("Give the request as JSON text, or as @ and a file name. Example: "
                       "pcldb search query --request '{\"q\": \"*\", \"limit\": 5}'",
                       code="bad_argument")
    text = str(text).strip()
    if text.startswith("@"):
        return util.read_json(Path(text[1:]).expanduser())

    def refuse(name):
        raise ValueError("it holds %s, which is not JSON" % name)
    try:
        return json.loads(text, parse_constant=refuse)
    except (ValueError, RecursionError) as error:
        raise PclError("The request is not valid JSON (%s). To read it from a file, write @ "
                       "and the file name." % error, code="bad_json") from None


def _plural(number, one, many=None):
    return "%d %s" % (number, one if number == 1 else (many or one + "s"))


def _cmd_status(settings, backend):
    store = _open_store(settings)
    with store.app() as db:
        counts = status(db)
        failed = failures(db)
    search = settings.search
    backend = _backend(settings, backend)
    signed_in = backend.signed_in()
    if search.backend == "globus":
        # Say who really answers now: "globus" is only the choice until it can be used.
        import importlib.util
        missing = None
        if not search.index_id:
            missing = "an index is set (pcldb search create-index)"
        elif importlib.util.find_spec("globus_sdk") is None:
            missing = "the package globus-sdk is installed"
        elif not signed_in:
            missing = "you sign in to Globus (pcldb search login)"
        if missing is None:
            _say("Search backend: globus (questions are answered by Globus Search)")
        elif search.fallback_to_local:
            _say("Search backend: globus is chosen, but until %s questions are answered from "
                 "this computer's copy of the index" % missing)
        else:
            _say("Search backend: globus is chosen, but until %s questions cannot be answered"
                 % missing)
    else:
        _say("Search backend: local (questions are answered from this computer's copy of the "
             "index)")
    _say("Globus Search index: %s" % (search.index_id or "none yet (pcldb search create-index)"))
    for line in index_lines(backend):
        _say(line)
    _say("Globus sign-in: %s" % ("signed in" if signed_in
                                 else "not signed in (pcldb search login)"))
    _say("Entries: %d waiting to be sent, %d sent and not confirmed yet, %d in the index, "
         "%d failed, %d waiting to be removed"
         % (counts["pending"], counts["sent"], counts["synced"], counts["failed"],
            counts["delete_pending"]))
    for row in failed:
        _say("  failed: %s: %s" % (_plain(row["session_path"]),
                                   _plain(row["last_error"] or "no reason was kept")))
    if counts["failed"] > len(failed):
        _say("  ... and %d more" % (counts["failed"] - len(failed)))
    if counts["failed"]:
        _say("To try the failed entries again: pcldb search reindex, then pcldb search push")
    return 0


def _cmd_push(args, settings, backend):
    store = _open_store(settings)
    wait_seconds = getattr(args, "wait_seconds", None)
    if wait_seconds is None:
        wait_seconds = DEFAULT_WAIT_SECONDS
    counts = push_store(store, _backend(settings, backend), wait_seconds=wait_seconds)
    _say("Sent %s to Globus Search in %s." % (_plural(counts["sent"], "entry", "entries"),
                                             _plural(counts["batches"], "batch", "batches")))
    _say("Confirmed in the index: %d. Failed: %d. Removed from the index: %d."
         % (counts["synced"], counts["failed"], counts["removed"]))
    if counts["requeued"]:
        _say("Queued again because their earlier task is not known any more: %d. Run: pcldb "
             "search push" % counts["requeued"])
    if counts["waiting"]:
        _say("Globus Search has not finished %s yet. Run: pcldb search push again in a minute."
             % _plural(counts["waiting"], "entry", "entries"))
    for problem in counts["problems"]:
        _say("Problem: " + _plain(problem), sys.stderr)
    if counts["failed"]:
        _say("See why with: pcldb search status", sys.stderr)
    return 1 if counts["problems"] or counts["failed"] else 0


def _cmd_reindex(settings):
    store = _open_store(settings)
    with store.app() as db:
        number = reindex(db, settings)
    _say("Queued %s again. Send them with: pcldb search push"
         % _plural(number, "entry", "entries"))
    return 0


def _cmd_login(args, settings, backend):
    backend = _backend(settings, backend)
    backend.login(no_browser=bool(getattr(args, "no_browser", False)))
    _say("Signed in to Globus. The sign-in is saved in %s (keep that file to yourself)."
         % backend.token_path)
    if settings.search.index_id:
        _say("Next: pcldb search push")
    else:
        _say("Next: pcldb search create-index")
    return 0


def _cmd_logout(settings, backend):
    backend = _backend(settings, backend)
    if not backend.token_path.is_file():
        _say("Nobody was signed in to Globus on this computer.")
        return 0
    if backend.logout():
        _say("Signed out of Globus: the sign-in was cancelled and removed from this computer.")
    else:
        _say("The sign-in was removed from this computer. Globus could not be reached to "
             "cancel it there; it runs out by itself, or cancel it at "
             "https://app.globus.org/settings/consents")
    return 0


def _cmd_create_index(args, settings, backend):
    backend = _backend(settings, backend)
    if settings.search.index_id:
        raise PclError(
            "An index is already set (search.index_id is %s). A person may own only 3 trial "
            "indexes, so no second one was made. To replace it, take search.index_id out of %s "
            "and run this again." % (settings.search.index_id, settings.config_path),
            code="index_exists", status=409)
    if not backend.signed_in():
        raise Unavailable("You are not signed in to Globus. Run: pcldb search login",
                          code="globus_sign_in")
    name = getattr(args, "name", None) or DEFAULT_INDEX_NAME
    description = getattr(args, "description", None) or DEFAULT_INDEX_DESCRIPTION
    index_id = backend.create_index(name, description)
    _say("Made a trial Globus Search index: %s" % index_id)
    try:
        path = save_setting(settings, "search.index_id", index_id)
    except PclError as error:
        _say("The index exists, but its id could not be saved: %s" % error.message, sys.stderr)
        _say("Put it into the settings file by hand, in the group \"search\": "
             "\"index_id\": \"%s\"" % index_id, sys.stderr)
        return 1
    _say("Saved as search.index_id in %s" % path)
    _say("A trial index is small (pcldb search status shows its size limit), a person may own "
         "3, and Globus may delete it from 30 days after today. Anyone who knows its id can "
         "read its name and counts, never the entries.")
    try:
        from .store import open_store
        store = open_store(settings)
        if store.is_initialized():
            with store.app() as db:
                number = reindex(db)
            _say("The new index is empty: %s queued for it."
                 % _plural(number, "entry is", "entries are"))
    except PclError as error:
        _say("The entries could not be queued (%s). Run: pcldb search reindex" % error.message,
             sys.stderr)
    _say("Next: pcldb search push")
    _say("Questions are still answered from this computer's copy. To let Globus Search answer "
         "them, run:")
    _say("    " + switch_command("globus"))
    return 0


def _cmd_use(settings, backend_name):
    path = save_setting(settings, "search.backend", backend_name)
    if backend_name == "globus":
        _say("Saved in %s: questions are now answered by Globus Search." % path)
        if not settings.search.index_id:
            _say("No index is set yet. Run: pcldb search create-index")
        if settings.search.fallback_to_local:
            _say("When Globus Search cannot be reached, this computer's copy answers and the "
                 "result says so.")
        _say("To go back: " + switch_command("local"))
    else:
        _say("Saved in %s: questions are now answered from this computer's copy of the index."
             % path)
    _say("A running server reads the settings when it starts: stop it and start it again.")
    return 0


def _cmd_query(args, settings, backend):
    from . import search
    request = search.validate_request(_read_request(getattr(args, "request", None)))
    store = _open_store(settings)
    result, used, notices = search.run_store(store, request, settings, globus=backend)
    # JSON written with ensure_ascii is ASCII already and holds no raw control character.
    print(json.dumps(result, indent=2, ensure_ascii=True))
    _say("Answered by: %s" % ("Globus Search" if used == "globus"
                              else "this computer's copy of the index"), sys.stderr)
    for notice in notices:
        _say("Note: " + _plain(notice), sys.stderr)
    return 0


def cmd_search(args, settings, *, backend=None):
    """`pcldb search status|push|reindex|login|logout|create-index|query` (and `use-globus`,
    `use-local`). Returns the exit code. `backend` is for tests: the GlobusSearch to use."""
    action = getattr(args, "action", None)
    if action == "status":
        return _cmd_status(settings, backend)
    if action == "push":
        return _cmd_push(args, settings, backend)
    if action == "reindex":
        return _cmd_reindex(settings)
    if action == "login":
        return _cmd_login(args, settings, backend)
    if action == "logout":
        return _cmd_logout(settings, backend)
    if action == "create-index":
        return _cmd_create_index(args, settings, backend)
    if action == "query":
        return _cmd_query(args, settings, backend)
    if action in ("use-globus", "use-local"):
        return _cmd_use(settings, action[4:])
    raise PclError("pcldb search does not know the action %r. The actions are: status, push, "
                   "reindex, login, logout, create-index, query." % (action,),
                   code="bad_argument")


def main(argv=None):
    """`python -m pcldb.search_sync use-globus|use-local [--dir FOLDER]`: switch the search
    backend in the settings file. (The same as `pcldb search use-globus` / `use-local`.)"""
    import argparse
    from . import config
    parser = argparse.ArgumentParser(
        prog="python -m pcldb.search_sync",
        description="Switch who answers searches: Globus Search, or this computer's copy.")
    parser.add_argument("action", choices=["use-globus", "use-local"])
    parser.add_argument("--dir", dest="dir", default=None,
                        help="the project folder (default: PCLDB_DIR, else where pcldb is)")
    args = parser.parse_args(argv)
    try:
        settings = config.load_settings(project_dir=args.dir)
        return cmd_search(args, settings)
    except PclError as error:
        print(_plain(error.message), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
