"""The schema, defined once, with the CREATE statements for both engines.

Two schemas:

- `pcl`, the catalogue: everything expert mode may read. Table, column and view names follow the
  `pcl_index` proof of concept, so SQL written against it carries over.
- `app`, the private part: users, token hashes, audit rows, the Search queue. Expert mode can
  never read it.

Every column has a KIND (section 4.2 of the specification). The kind decides the column's type
on each engine and how values are adapted going in and coming out:

    kind     Python            PostgreSQL         SQLite
    text     str               TEXT COLLATE "C"   TEXT
    int      int               INTEGER            INTEGER
    bigint   int               BIGINT             INTEGER
    real     float             DOUBLE PRECISION   REAL
    bool     bool              BOOLEAN            INTEGER 0/1
    utc      str (ms, Z)       TIMESTAMPTZ        TEXT (the same fixed-width text)
    json     dict/list/scalar  JSONB              TEXT (canonical JSON)
    strlist  list[str]         TEXT[] COLLATE "C" TEXT (JSON array)

Text columns are declared `COLLATE "C"` on PostgreSQL so that comparisons and ORDER BY use byte
order there too, as SQLite always does, whatever the database's own collation is.

`COLUMN_KINDS` maps every column NAME to its kind. One name never has two kinds (checked when
this module is imported and by a test), which is what lets query results be normalised by name.

The descriptions are what people read in the website's schema browser. On PostgreSQL they are
also applied as COMMENT ON.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import NamedTuple

SCHEMA_VERSION = 1

ENGINES = ("sqlite", "postgresql")
KINDS = ("text", "int", "bigint", "real", "bool", "utc", "json", "strlist")

# Kind -> the column type in each engine's CREATE TABLE.
SQL_TYPES = {
    "sqlite": {"text": "TEXT", "int": "INTEGER", "bigint": "INTEGER", "real": "REAL",
               "bool": "INTEGER", "utc": "TEXT", "json": "TEXT", "strlist": "TEXT"},
    "postgresql": {"text": 'TEXT COLLATE "C"', "int": "INTEGER", "bigint": "BIGINT",
                   "real": "DOUBLE PRECISION", "bool": "BOOLEAN", "utc": "TIMESTAMPTZ",
                   "json": "JSONB", "strlist": 'TEXT[] COLLATE "C"'},
}

# Kind -> the type name a person sees (the schema browser, sqlrun.describe).
TYPE_NAMES = {
    "sqlite": {"text": "TEXT", "int": "INTEGER", "bigint": "INTEGER", "real": "REAL",
               "bool": "INTEGER (0 or 1)", "utc": "TEXT (UTC time)", "json": "TEXT (JSON)",
               "strlist": "TEXT (JSON list)"},
    "postgresql": {"text": "text", "int": "integer", "bigint": "bigint",
                   "real": "double precision", "bool": "boolean",
                   "utc": "timestamp with time zone", "json": "jsonb", "strlist": "text[]"},
}

# What the two schemas are called inside each engine's SQL: {pcl} and {app} in module SQL.
SCHEMA_NAMES = {
    "sqlite": {"pcl": "pcl", "app": "main"},
    "postgresql": {"pcl": "pcl", "app": "app"},
}

SCHEMA_DESCRIPTIONS = {
    "pcl": "The lab catalogue: sessions, files, runs and what was found in them.",
    "app": "The private part of pcldb: users, token hashes, audit rows and the Search queue.",
}


class Column(NamedTuple):
    """One column: (name, kind, nullable, description)."""

    name: str
    kind: str
    nullable: bool
    description: str


class Index(NamedTuple):
    name: str
    columns: tuple


@dataclass(frozen=True, eq=False)
class Table:
    schema: str            # "pcl" or "app"
    name: str
    description: str
    columns: tuple         # of Column
    primary_key: tuple     # column names
    unique: tuple = ()     # of tuples of column names
    indexes: tuple = ()    # of Index

    @property
    def qualified(self):
        """"pcl.sessions": the name Db.insert and friends take."""
        return "%s.%s" % (self.schema, self.name)

    @property
    def column_names(self):
        return tuple(column.name for column in self.columns)

    @property
    def kinds(self):
        """{column name: kind}"""
        return {column.name: column.kind for column in self.columns}

    is_view = False


@dataclass(frozen=True, eq=False)
class View:
    schema: str
    name: str
    description: str
    columns: tuple         # of Column (nullable is always True: a view promises nothing)
    select: dict           # {engine: the view's SELECT}

    @property
    def qualified(self):
        return "%s.%s" % (self.schema, self.name)

    @property
    def column_names(self):
        return tuple(column.name for column in self.columns)

    @property
    def kinds(self):
        return {column.name: column.kind for column in self.columns}

    is_view = True


def _col(name, kind, description):
    return Column(name, kind, False, description)


def _opt(name, kind, description):
    return Column(name, kind, True, description)


# ---------------------------------------------------------------------- the catalogue: pcl

_META_COLUMNS = (
    _col("key", "text", "The name of the fact."),
    _opt("value", "text", "The fact's value, as text."),
)

_SESSIONS = Table(
    "pcl", "sessions",
    "One row per session folder on ARC: what was measured, when, with which instrument, and "
    "where the files are.",
    (
        _col("path", "text", "The session folder's path below the lab's project folder, for "
                             "example raw/raman-portable/simulated/<folder>; it identifies the "
                             "session."),
        _col("instrument", "text", "The instrument's short name, for example raman-portable."),
        _col("area", "text", "Where the folder sits under its instrument: real or simulated."),
        _col("folder", "text", "The session folder's own name (the last part of the path)."),
        _opt("session_name", "text", "The session's name as its tool made it, without any "
                                     "_copyN ending."),
        _opt("session_id", "text", "The tool's own unique id of the session; copies of one "
                                   "session share it."),
        _col("is_copy", "bool", "True when the folder is a second or later copy of a session "
                                "(its name ends in _copyN)."),
        _opt("started_utc", "utc", "When acquisition started (UTC)."),
        _opt("ended_utc", "utc", "When the last item was taken (UTC)."),
        _col("simulated", "bool", "True when any part of the session was simulated, or when "
                                  "nobody can tell."),
        _col("sample_ids", "strlist", "The sample identifiers, as typed at the instrument."),
        _opt("operator", "text", "Who ran the session, as typed (personal information)."),
        _opt("location", "text", "The room or setup, as typed."),
        _opt("method", "text", "The instrument's mode, for example the Raman scan mode 1d-y."),
        _opt("items", "int", "How many measurements the session holds."),
        _opt("items_planned", "int", "How many measurements were planned."),
        _opt("completion", "text", "How the session ended: complete, partial, failed or "
                                   "unknown."),
        _opt("notes", "text", "Free text typed at the instrument (may be personal)."),
        _col("tags", "strlist", "The lab's tags for the session."),
        _opt("format", "text", "What the files are, for example raman-session/1 folder."),
        _opt("uploaded_utc", "utc", "When the copy that went to ARC was made (UTC)."),
        _opt("upload_id", "text", "The uploader's id of the upload (u and 12 hex digits)."),
        _col("has_card", "bool", "True when the session has a lab card (pcl_session.json)."),
        _opt("card_json", "json", "The lab card as it was registered."),
        _col("corrections_json", "json", "The corrections applied to the session, oldest "
                                         "first: what was set, why, by whom, and the original "
                                         "value."),
        _col("corrected", "bool", "True when a correction changed one of the session's fields."),
        _col("withdrawn", "bool", "True when the lab took the session back; the files stay, "
                                  "searches leave it out."),
        _col("bytes", "bigint", "The total size of the session's files, in bytes."),
        _col("n_files", "int", "How many files the session has."),
        _opt("sums_sha256", "text", "The SHA-256 of the session's SHA256SUMS file; it "
                                    "identifies the session's exact contents."),
        _opt("verified_utc", "utc", "When the files were last checked against their checksums "
                                    "(UTC)."),
        _opt("verify_ok", "bool", "Whether that last check passed."),
        _opt("upload_json", "json", "How the session got to ARC: the transport, the upload id "
                                    "and how the copy was checked."),
        _col("collection_id", "text", "The Globus collection that holds the files."),
        _col("data_path", "text", "The session folder's path inside that Globus collection."),
        _col("storage_path", "text", "The session folder's path on ARC's file system."),
        _col("globus_url", "text", "The folder as a globus:// address."),
        _col("web_url", "text", "The folder in the Globus web app's file manager."),
        _col("subject", "text", "The session's id in Globus Search."),
        _col("registered_utc", "utc", "When the session was first registered here (UTC)."),
        _opt("registered_by", "text", "Who registered it."),
        _col("updated_utc", "utc", "When this row last changed (UTC)."),
        _col("source", "text", "How the row got here: upload, tree, index or demo."),
        _opt("doc_sha256", "text", "The SHA-256 of the registration document; it tells an "
                                   "unchanged registration from a changed one."),
    ),
    primary_key=("path",),
    unique=(("subject",),),
    indexes=(Index("sessions_started", ("started_utc",)),
             Index("sessions_instrument", ("instrument",))),
)

_FILES = Table(
    "pcl", "files",
    "One row per file of a session.",
    (
        _col("session_path", "text", "The session the row belongs to (sessions.path)."),
        _col("rel_path", "text", "The file's path inside the session folder."),
        _col("bytes", "bigint", "The file's size in bytes."),
        _opt("sha256", "text", "The file's SHA-256 from the session's SHA256SUMS; empty when "
                               "the file is not listed there."),
        _col("listed", "bool", "True when SHA256SUMS lists the file."),
    ),
    primary_key=("session_path", "rel_path"),
    indexes=(Index("files_sha256", ("sha256",)),),
)

_RUNS = Table(
    "pcl", "runs",
    "One row per run folder: a classification, a review or a correction made for a session.",
    (
        _col("path", "text", "The run folder's path below the project folder "
                             "(results/<instrument>/...); it identifies the run."),
        _col("instrument", "text", "The instrument's short name."),
        _col("area", "text", "Where the folder sits under its instrument: real or simulated."),
        _col("folder", "text", "The run folder's own name."),
        _opt("kind", "text", "What the run did: classify, review, correction or another kind."),
        _opt("made_utc", "utc", "When the run was made (UTC)."),
        _opt("session_path", "text", "The session the run belongs to, matched by contents and "
                                     "else by path."),
        _opt("stated_session_path", "text", "The session path the run's own run.json names."),
        _opt("session_id", "text", "The session id the run names."),
        _opt("sums_sha256", "text", "The SHA-256 of the SHA256SUMS of the session the run was "
                                    "made from."),
        _col("derived_from", "json", "The runs this run builds on."),
        _opt("tool_name", "text", "The program that made the run."),
        _opt("tool_version", "text", "That program's version."),
        _opt("model_name", "text", "The model a classify run used."),
        _opt("model_sha256", "text", "The SHA-256 that identifies that model's files."),
        _opt("runner", "text", "Where the run was made, for example mac, slurm or hand."),
        _opt("slurm_job_id", "text", "The ARC Slurm job number, when the run was a job."),
        _opt("run_by", "text", "Who made the run."),
        _opt("set_json", "json", "For a correction: the fields it sets and their new values."),
        _opt("why", "text", "For a correction: the reason given."),
        _opt("run_json", "json", "The run's run.json as it is."),
        _col("valid", "bool", "True when the run folder passed every check."),
    ),
    primary_key=("path",),
    indexes=(Index("runs_session", ("session_path", "kind", "model_name")),),
)

_POINTS = Table(
    "pcl", "points",
    "One row per measurement point of a classify run, with what the model said about it.",
    (
        _col("run_path", "text", "The run the row belongs to (runs.path)."),
        _col("point_file", "text", "The point's file name in the session."),
        _col("point_id", "int", "The point's number in the session."),
        _opt("capture_id", "text", "The capture's unique id."),
        _opt("label", "text", "The label the model gave (UNKNOWN when it was not sure)."),
        _opt("top_class", "text", "The class with the highest score."),
        _opt("confidence", "real", "The score of that class, from 0 to 1."),
        _opt("outcome", "text", "How the classification ended: confident, background, "
                                "low_confidence or rejected."),
        _col("needs_review", "bool", "True when a person should look at the point."),
        _opt("threshold", "real", "The confidence a label needed in this run."),
        _opt("reason", "text", "Why the point got this outcome."),
        _opt("raw_sha256", "text", "The SHA-256 of the raw spectrum file."),
        _opt("scores_json", "json", "The score of every class."),
    ),
    primary_key=("run_path", "point_file"),
    indexes=(Index("points_run", ("run_path", "point_id")),
             Index("points_label", ("label",))),
)

_REVIEWS = Table(
    "pcl", "reviews",
    "One row per decision a reviewer made about a point.",
    (
        _col("run_path", "text", "The run the row belongs to (runs.path)."),
        _col("row_no", "int", "The decision's row number inside its review run."),
        _opt("reviewed_run", "text", "The classify run the decision is about."),
        _col("point_id", "int", "The point's number in the session."),
        _opt("capture_id", "text", "The capture's unique id."),
        _col("status", "text", "The decision: confirmed, relabelled or rejected."),
        _opt("label", "text", "The label the reviewer set, when the point was relabelled."),
        _opt("reviewer", "text", "Who reviewed the point."),
        _col("time_utc", "utc", "When the decision was made (UTC)."),
        _opt("comment", "text", "The reviewer's note."),
    ),
    primary_key=("run_path", "row_no"),
    indexes=(Index("reviews_run", ("reviewed_run", "point_id")),),
)

_MODELS = Table(
    "pcl", "models",
    "One row per classification model the lab keeps.",
    (
        _col("name", "text", "The model's name."),
        _col("path", "text", "The model's folder below the project folder."),
        _opt("sha256", "text", "The SHA-256 that identifies the model's files."),
        _opt("classes_json", "json", "The classes the model can tell apart."),
        _opt("threshold", "real", "The confidence a label needs with this model."),
        _opt("files_json", "json", "The model's files with sizes and checksums."),
        _col("is_current", "bool", "True for the model the lab uses now."),
        _opt("model_json", "json", "The model's model.json as it is."),
    ),
    primary_key=("name",),
)

_PROBLEMS = Table(
    "pcl", "problems",
    "One row per problem the last check of the folders found.",
    (
        _col("problem_no", "int", "The problem's number in the last check."),
        _col("path", "text", "The folder or file the problem is about."),
        _col("code", "text", "The problem's short code."),
        _opt("detail", "text", "What exactly is wrong."),
        _opt("fix", "text", "What to do about it."),
        _opt("session_path", "text", "The session the problem belongs to, when there is one."),
    ),
    primary_key=("problem_no",),
)

_SESSION_ROLLUP = Table(
    "pcl", "session_rollup",
    "One row per session with what its runs add up to: labels, models, problems and the "
    "review state.",
    (
        _col("session_path", "text", "The session the row belongs to (sessions.path)."),
        _col("labels", "strlist", "The different labels of the session's current classify "
                                  "run, after reviews."),
        _col("label_counts", "json", "How many points have each label: a list of "
                                     "{label, count}."),
        _col("models", "strlist", "The models that have a classify run of the session."),
        _col("problems", "strlist", "The codes of the problems found for the session."),
        _col("review_state", "text", "Whether points still wait for a reviewer: none, pending "
                                     "or done."),
        _col("n_needs_review", "int", "How many points still wait for a reviewer."),
        _opt("last_run_utc", "utc", "When the session's newest run was made (UTC)."),
    ),
    primary_key=("session_path",),
)

# ---------------------------------------------------------------------- the private part: app

_USERS = Table(
    "app", "users",
    "The people who may sign in.",
    (
        _col("user_id", "text", "The user's id."),
        _col("name", "text", "The sign-in name."),
        _opt("display_name", "text", "The name shown in the website."),
        _col("role", "text", "What the user may do: viewer, analyst, uploader or admin."),
        _col("disabled", "bool", "True when the user may not sign in."),
        _col("created_utc", "utc", "When the user was added (UTC)."),
    ),
    primary_key=("user_id",),
    unique=(("name",),),
)

_TOKENS = Table(
    "app", "tokens",
    "The sign-in tokens that were issued; only their SHA-256 is kept, never the token.",
    (
        _col("token_id", "text", "The token's id."),
        _col("user_id", "text", "The user the token belongs to (users.user_id)."),
        _col("token_sha256", "text", "The SHA-256 of the token's text."),
        _opt("label", "text", "A note that says what the token is for."),
        _col("created_utc", "utc", "When the token was issued (UTC)."),
        _opt("expires_utc", "utc", "When the token stops working (UTC); empty means never."),
        _opt("revoked_utc", "utc", "When the token was taken back (UTC)."),
        _opt("last_used_utc", "utc", "When the token was last used (UTC), to the minute."),
    ),
    primary_key=("token_id",),
    unique=(("token_sha256",),),
    indexes=(Index("tokens_user", ("user_id",)),),
)

_REGISTRATIONS = Table(
    "app", "registrations",
    "The audit trail of registrations: one row each time a session was offered.",
    (
        _col("reg_id", "text", "The row's id."),
        _col("at_utc", "utc", "When it happened (UTC)."),
        _opt("user_name", "text", "The sign-in name of the person who did it."),
        _col("session_path", "text", "The session the row belongs to (sessions.path)."),
        _col("action", "text", "What happened: created, updated, unchanged or rejected."),
        _col("source", "text", "Where the registration came from: upload, tree, index or "
                               "demo."),
        _opt("doc_sha256", "text", "The SHA-256 of the registration document that was "
                                   "offered."),
        _opt("detail_json", "json", "More about what happened, for example why it was "
                                    "rejected."),
    ),
    primary_key=("reg_id",),
    indexes=(Index("registrations_at", ("at_utc",)),
             Index("registrations_session", ("session_path",))),
)

_QUERY_LOG = Table(
    "app", "query_log",
    "The audit trail of questions and SQL queries.",
    (
        _col("log_id", "text", "The row's id."),
        _col("at_utc", "utc", "When it happened (UTC)."),
        _opt("user_name", "text", "The sign-in name of the person who did it."),
        _col("mode", "text", "How it was asked: sql, ask or search."),
        _col("query_text", "text", "The question or the SQL, as typed."),
        _opt("request_json", "json", "The search request it became."),
        _col("ok", "bool", "True when it ran without an error."),
        _opt("error_text", "text", "The error message, when it failed."),
        _opt("n_rows", "int", "How many rows or results came back."),
        _opt("elapsed_ms", "int", "How long it took, in milliseconds."),
        _opt("backend", "text", "What answered: the engine, or the search backend."),
    ),
    primary_key=("log_id",),
    indexes=(Index("query_log_at", ("at_utc",)),
             Index("query_log_user", ("user_name", "at_utc"))),
)

_SEARCH_ENTRIES = Table(
    "app", "search_entries",
    "What is, or will be, in Globus Search: one entry per session.",
    (
        _col("subject", "text", "The entry's id in Globus Search (sessions.subject)."),
        _col("session_path", "text", "The session the row belongs to (sessions.path)."),
        _col("content_json", "json", "The entry's content (schema pcl-search/1)."),
        _col("visible_to", "strlist", "Who may see the entry in Globus Search."),
        _col("content_sha256", "text", "The SHA-256 of the content; a change makes the entry "
                                       "pending again."),
        _col("state", "text", "Where the entry stands: pending, sent, synced, failed or "
                              "delete_pending."),
        _opt("task_id", "text", "The Globus Search task that carries the entry."),
        _opt("last_error", "text", "Why the last push failed."),
        _col("updated_utc", "utc", "When the entry last changed (UTC)."),
        _opt("synced_utc", "utc", "When Globus Search confirmed the entry (UTC)."),
    ),
    primary_key=("subject",),
    unique=(("session_path",),),
    indexes=(Index("search_entries_state", ("state",)),),
)

TABLES = (
    Table("pcl", "meta", "Small facts about the catalogue, one per key: schema_version, "
                         "current_model, index_built_utc, index_source, demo.",
          _META_COLUMNS, primary_key=("key",)),
    _SESSIONS, _FILES, _RUNS, _POINTS, _REVIEWS, _MODELS, _PROBLEMS, _SESSION_ROLLUP,
    Table("app", "meta", "Small facts about the private part, one per key: schema_version.",
          _META_COLUMNS, primary_key=("key",)),
    _USERS, _TOKENS, _REGISTRATIONS, _QUERY_LOG, _SEARCH_ENTRIES,
)

# ---------------------------------------------------------------------- views (all in pcl)
#
# In the templates {p} stands for the schema prefix (empty on SQLite, where a view may only name
# objects of its own file; "pcl." on PostgreSQL) and {c} for ' COLLATE "C"' on PostgreSQL (empty
# on SQLite, which always compares text by bytes). Booleans are written so that they are native
# on both engines: `r.valid`, never `r.valid = 1`.

_CURRENT_RUNS = """\
SELECT r.path, r.instrument, r.area, r.session_path, r.made_utc, r.model_name, r.model_sha256
FROM {p}runs r
WHERE r.kind = 'classify' AND r.valid AND r.session_path IS NOT NULL
  AND r.model_name = (SELECT m.value FROM {p}meta m WHERE m.key = 'current_model')
  AND r.model_sha256 = (SELECT d.sha256 FROM {p}models d WHERE d.name = r.model_name)
  AND NOT EXISTS (
    SELECT 1 FROM {p}runs n
    WHERE n.kind = 'classify' AND n.valid AND n.session_path = r.session_path
      AND n.model_name = r.model_name AND n.model_sha256 = r.model_sha256
      AND (n.made_utc > r.made_utc
           OR (n.made_utc = r.made_utc AND n.path{c} > r.path{c})))"""

_LATEST_REVIEWS = """\
SELECT v.run_path, v.row_no, v.reviewed_run, v.point_id, v.capture_id, v.status, v.label,
       v.reviewer, v.time_utc, v.comment
FROM {p}reviews v
WHERE NOT EXISTS (
  SELECT 1 FROM {p}reviews w
  WHERE w.reviewed_run = v.reviewed_run AND w.point_id = v.point_id
    AND (w.time_utc > v.time_utc
         OR (w.time_utc = v.time_utc AND w.run_path{c} > v.run_path{c})
         OR (w.time_utc = v.time_utc AND w.run_path = v.run_path AND w.row_no > v.row_no)))"""

_CURRENT_POINTS = """\
SELECT s.path AS session_path, s.instrument, s.simulated, s.withdrawn, s.is_copy,
       c.path AS run_path, c.made_utc AS run_made_utc, c.model_name,
       p.point_file, p.point_id, p.capture_id, p.label AS model_label, p.top_class, p.confidence,
       p.outcome, p.needs_review, p.threshold, p.reason, p.raw_sha256, p.scores_json,
       v.status AS review_status, v.label AS review_label, v.reviewer,
       v.time_utc AS review_time_utc, v.run_path AS review_run,
       CASE WHEN v.status = 'rejected' THEN NULL
            WHEN v.status = 'relabelled' THEN v.label
            ELSE p.label END AS effective_label,
       (v.status IS NULL AND p.needs_review) AS awaiting_review
FROM {p}current_runs c
JOIN {p}sessions s ON s.path = c.session_path
JOIN {p}points p ON p.run_path = c.path
LEFT JOIN {p}latest_reviews v ON v.reviewed_run = c.path AND v.point_id = p.point_id"""

_SESSION_SUMMARY = """\
SELECT s.instrument, s.folder, s.session_id, s.path, s.method, s.completion, s.format, s.location,
       s.operator, COALESCE(r.review_state, 'none') AS review_state, s.notes,
       s.sample_ids, s.tags, r.labels, r.label_counts, r.models, r.problems,
       s.simulated, s.is_copy, s.withdrawn, s.corrected,
       s.items, s.items_planned, s.n_files AS files, s.bytes,
       COALESCE(r.n_needs_review, 0) AS n_needs_review,
       s.started_utc AS started, s.uploaded_utc AS uploaded,
       CASE WHEN s.verify_ok THEN s.verified_utc ELSE NULL END AS verified,
       r.last_run_utc AS last_run
FROM {p}sessions s
LEFT JOIN {p}session_rollup r ON r.session_path = s.path"""

def _both(template):
    """{engine: SELECT} for a template both engines share."""
    return {
        "sqlite": template.replace("{p}", "").replace("{c}", ""),
        "postgresql": template.replace("{p}", "pcl.").replace("{c}", ' COLLATE "C"'),
    }


# The three list views need each engine's own way to turn a list into rows.
_ENGINE_SELECTS = {
    "session_samples": {
        "sqlite": """\
SELECT s.path AS session_path, j.value AS sample_id
FROM sessions s, json_each(s.sample_ids) j""",
        "postgresql": """\
SELECT s.path AS session_path, u.item COLLATE "C" AS sample_id
FROM pcl.sessions s CROSS JOIN LATERAL unnest(s.sample_ids) AS u(item)""",
    },
    "session_tags": {
        "sqlite": """\
SELECT s.path AS session_path, j.value AS tag
FROM sessions s, json_each(s.tags) j""",
        "postgresql": """\
SELECT s.path AS session_path, u.item COLLATE "C" AS tag
FROM pcl.sessions s CROSS JOIN LATERAL unnest(s.tags) AS u(item)""",
    },
    "session_labels": {
        "sqlite": """\
SELECT r.session_path,
       CASE WHEN json_type(j.value, '$.label') = 'text'
            THEN json_extract(j.value, '$.label') END AS label,
       CASE WHEN json_type(j.value, '$.count') = 'integer'
            THEN json_extract(j.value, '$.count') END AS n
FROM session_rollup r, json_each(r.label_counts) j
WHERE json_type(r.label_counts) = 'array' AND j.type = 'object'""",
        "postgresql": """\
SELECT r.session_path,
       CASE WHEN jsonb_typeof(e.item -> 'label') = 'string'
            THEN (e.item ->> 'label') COLLATE "C" END AS label,
       CASE WHEN jsonb_typeof(e.item -> 'count') = 'number'
                 AND (e.item ->> 'count') ~ '^-?[0-9]{1,9}$'
            THEN CAST(e.item ->> 'count' AS INTEGER) END AS n
FROM pcl.session_rollup r
CROSS JOIN LATERAL jsonb_array_elements(
    CASE WHEN jsonb_typeof(r.label_counts) = 'array' THEN r.label_counts
         ELSE CAST('[]' AS JSONB) END) AS e(item)
WHERE jsonb_typeof(e.item) = 'object'""",
    },
}


def _from(table, names, renamed=None):
    """View columns that are copies of table columns (same kind and description)."""
    by_name = {column.name: column for column in table.columns}
    renamed = renamed or {}
    return tuple(Column(renamed.get(name, name), by_name[name].kind, True,
                        by_name[name].description) for name in names)


_SESSION_KEY = Column("session_path", "text", True,
                      "The session the row belongs to (sessions.path).")

VIEWS = (
    View("pcl", "session_samples",
         "One row per sample of each session; use it to find sessions by sample.",
         (_SESSION_KEY, Column("sample_id", "text", True, "One of the session's sample identifiers.")),
         _ENGINE_SELECTS["session_samples"]),
    View("pcl", "session_tags",
         "One row per tag of each session.",
         (_SESSION_KEY, Column("tag", "text", True, "One of the session's tags.")),
         _ENGINE_SELECTS["session_tags"]),
    View("pcl", "session_labels",
         "One row per label of each session's current classify run, with how many points "
         "have it.",
         (_SESSION_KEY,
          Column("label", "text", True, "One label that counts for the session (after "
                                        "reviews)."),
          Column("n", "int", True, "How many points have the label.")),
         _ENGINE_SELECTS["session_labels"]),
    View("pcl", "current_runs",
         "The newest valid classify run of each session that was made with the lab's current "
         "model.",
         _from(_RUNS, ("path", "instrument", "area", "session_path", "made_utc", "model_name",
                       "model_sha256")),
         _both(_CURRENT_RUNS)),
    View("pcl", "latest_reviews",
         "The newest decision for each reviewed point.",
         _from(_REVIEWS, ("run_path", "row_no", "reviewed_run", "point_id", "capture_id",
                          "status", "label", "reviewer", "time_utc", "comment")),
         _both(_LATEST_REVIEWS)),
    View("pcl", "current_points",
         "Every point of each session's current run: the model's label, the newest review, and "
         "the label that counts.",
         (_SESSION_KEY,)
         + _from(_SESSIONS, ("instrument", "simulated", "withdrawn", "is_copy"))
         + (Column("run_path", "text", True, "The run the row belongs to (runs.path)."),
            Column("run_made_utc", "utc", True, "When that run was made (UTC)."))
         + _from(_RUNS, ("model_name",))
         + _from(_POINTS, ("point_file", "point_id", "capture_id"))
         + (Column("model_label", "text", True, "The label the model gave."),)
         + _from(_POINTS, ("top_class", "confidence", "outcome", "needs_review", "threshold",
                           "reason", "raw_sha256", "scores_json"))
         + (Column("review_status", "text", True, "The newest review's decision: confirmed, "
                                                  "relabelled or rejected; empty when nobody "
                                                  "reviewed the point."),
            Column("review_label", "text", True, "The label the reviewer set."),
            Column("reviewer", "text", True, "Who reviewed the point."),
            Column("review_time_utc", "utc", True, "When the newest review was made (UTC)."),
            Column("review_run", "text", True, "The review run that holds the newest review."),
            Column("effective_label", "text", True, "The label that counts: the reviewer's "
                                                    "when relabelled, none when rejected, "
                                                    "else the model's."),
            Column("awaiting_review", "bool", True, "True when the point needs a review and "
                                                    "has none yet.")),
         _both(_CURRENT_POINTS)),
    View("pcl", "session_summary",
         "One row per session with the fields people search by; the search index carries the "
         "same fields.",
         _from(_SESSIONS, ("instrument", "folder", "session_id", "path", "method", "completion",
                           "format", "location", "operator"))
         + _from(_SESSION_ROLLUP, ("review_state",))
         + _from(_SESSIONS, ("notes", "sample_ids", "tags"))
         + _from(_SESSION_ROLLUP, ("labels", "label_counts", "models", "problems"))
         + _from(_SESSIONS, ("simulated", "is_copy", "withdrawn", "corrected", "items",
                             "items_planned"))
         + (Column("files", "int", True, "How many files the session has."),)
         + _from(_SESSIONS, ("bytes",))
         + _from(_SESSION_ROLLUP, ("n_needs_review",))
         + (Column("started", "utc", True, "When acquisition started (UTC)."),
            Column("uploaded", "utc", True, "When the copy that went to ARC was made (UTC)."),
            Column("verified", "utc", True, "When the newest checksum check passed (UTC); "
                                            "empty when it failed or there was none."),
            Column("last_run", "utc", True, "When the session's newest run was made (UTC).")),
         _both(_SESSION_SUMMARY)),
)

# The 30 columns of session_summary, in order (the proof of concept's SUMMARY_COLUMNS).
SUMMARY_COLUMNS = tuple(column.name for column in VIEWS[-1].columns)


# ---------------------------------------------------------------------- lookups

TABLE_BY_NAME = {table.qualified: table for table in TABLES}
VIEW_BY_NAME = {view.qualified: view for view in VIEWS}


def _column_kinds():
    kinds, seen_in = {}, {}
    for relation in TABLES + VIEWS:
        for column in relation.columns:
            if column.kind not in KINDS:
                raise RuntimeError("%s.%s has the unknown kind %r"
                                   % (relation.qualified, column.name, column.kind))
            if kinds.setdefault(column.name, column.kind) != column.kind:
                raise RuntimeError(
                    "the column name %r has two kinds: %s in %s and %s in %s"
                    % (column.name, kinds[column.name], seen_in[column.name], column.kind,
                       relation.qualified))
            seen_in.setdefault(column.name, relation.qualified)
    return kinds


# Column name -> kind, over every table and view of both schemas.
COLUMN_KINDS = _column_kinds()


def _check_engine(engine):
    if engine not in ENGINES:
        raise ValueError("unknown engine %r (known: %s)" % (engine, ", ".join(ENGINES)))


def split_name(name):
    """("pcl", "sessions") for "pcl.sessions" or "{pcl}.sessions"."""
    schema, dot, plain = str(name).partition(".")
    schema = schema.strip("{}")
    if not dot or schema not in SCHEMA_DESCRIPTIONS or not plain:
        raise ValueError("%r is not a table name. Write it with its schema: pcl.sessions, "
                         "app.users." % (name,))
    return schema, plain


def table(name):
    """The Table for "pcl.sessions" (also "{pcl}.sessions"). ValueError when there is none."""
    schema, plain = split_name(name)
    try:
        return TABLE_BY_NAME["%s.%s" % (schema, plain)]
    except KeyError:
        kind = "a view, not a table" if "%s.%s" % (schema, plain) in VIEW_BY_NAME else "unknown"
        raise ValueError("%s.%s is %s" % (schema, plain, kind)) from None


def view(name):
    """The View for "pcl.current_runs"."""
    schema, plain = split_name(name)
    try:
        return VIEW_BY_NAME["%s.%s" % (schema, plain)]
    except KeyError:
        raise ValueError("%s.%s is not a view" % (schema, plain)) from None


def relations(schema=None):
    """Tables, then views, of one schema ("pcl" or "app") or of both."""
    return tuple(item for item in TABLES + VIEWS if schema is None or item.schema == schema)


def type_name(engine, kind):
    """The type a person sees for a kind on one engine, for example "jsonb" or "TEXT (JSON)"."""
    _check_engine(engine)
    return TYPE_NAMES[engine][kind]


# ---------------------------------------------------------------------- CREATE statements


def _literal(text):
    return "'" + text.replace("'", "''") + "'"


def _table_ddl(engine, item, name):
    types = SQL_TYPES[engine]
    lines = []
    for column in item.columns:
        lines.append("    %s %s%s" % (column.name, types[column.kind],
                                      "" if column.nullable else " NOT NULL"))
    lines.append("    PRIMARY KEY (%s)" % ", ".join(item.primary_key))
    for columns in item.unique:
        lines.append("    UNIQUE (%s)" % ", ".join(columns))
    return "CREATE TABLE %s (\n%s\n)" % (name, ",\n".join(lines))


def ddl(engine, schema=None, qualified=True):
    """The CREATE statements, in order, that build the schema on an empty database.

    `schema`: "pcl" or "app" for one of the two, None for both (pcl first).

    PostgreSQL: CREATE SCHEMA, tables, indexes, views, then COMMENT ON for everything. Names are
    always written with their schema.

    SQLite: tables, indexes, views. With `qualified=True` (the default) the statements are for a
    connection that has the app file as `main` and the catalogue file attached as `pcl`
    (`CREATE TABLE pcl.sessions`, `CREATE TABLE main.users`). With `qualified=False` they carry
    no schema name: use `ddl("sqlite", "pcl", qualified=False)` to build the catalogue alone in
    a file of its own. Either way the file ends up holding the same definitions, because SQLite
    stores them without the schema name.
    """
    _check_engine(engine)
    if schema is not None and schema not in SCHEMA_DESCRIPTIONS:
        raise ValueError("unknown schema %r (known: pcl, app)" % (schema,))
    postgres = engine == "postgresql"
    statements = []
    for wanted in ("pcl", "app") if schema is None else (schema,):
        prefix = SCHEMA_NAMES[engine][wanted] + "." if (postgres or qualified) else ""
        if postgres:
            statements.append("CREATE SCHEMA %s" % wanted)
        tables = [item for item in TABLES if item.schema == wanted]
        views = [item for item in VIEWS if item.schema == wanted]
        for item in tables:
            statements.append(_table_ddl(engine, item, prefix + item.name))
        for item in tables:
            for index in item.indexes:
                if postgres:
                    statements.append("CREATE INDEX %s ON %s%s (%s)" % (
                        index.name, prefix, item.name, ", ".join(index.columns)))
                else:
                    statements.append("CREATE INDEX %s%s ON %s (%s)" % (
                        prefix, index.name, item.name, ", ".join(index.columns)))
        for item in views:
            statements.append("CREATE VIEW %s%s AS\n%s" % (prefix, item.name,
                                                           item.select[engine]))
        if postgres:
            statements.append("COMMENT ON SCHEMA %s IS %s"
                              % (wanted, _literal(SCHEMA_DESCRIPTIONS[wanted])))
            for item in tables + views:
                statements.append("COMMENT ON %s %s IS %s" % (
                    "VIEW" if item.is_view else "TABLE", item.qualified,
                    _literal(item.description)))
                for column in item.columns:
                    statements.append("COMMENT ON COLUMN %s.%s IS %s" % (
                        item.qualified, column.name, _literal(column.description)))
    return statements
