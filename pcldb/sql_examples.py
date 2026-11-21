"""Ready-made queries for expert mode, one set per engine (specification section 9).

    sql_examples.examples("postgresql")   ->   [{"title": ..., "sql": ...}, ...]

The website shows them next to the SQL editor (GET /api/v1/sql/examples); a click puts one into
the editor. Every example is a single read-only statement over the catalogue (`pcl`), starts
with a comment that says what it shows and what to change, and passes the expert-mode sandbox:
a test runs each one on its engine.

Most examples are the same text on both engines. Where the engines differ (date arithmetic,
reading a value out of JSON) each engine gets its own text.
"""

from __future__ import annotations

ENGINES = ("sqlite", "postgresql")

# (title, the SQL for both engines)  or  (title, {"sqlite": ..., "postgresql": ...})
_EXAMPLES = (
    ("Newest sessions", """\
-- The 20 newest sessions. Withdrawn sessions are left out.
SELECT folder, instrument, started_utc, method, completion, simulated
FROM sessions
WHERE NOT withdrawn
ORDER BY started_utc DESC NULLS LAST
LIMIT 20"""),

    ("Sessions per instrument", """\
-- How many sessions each instrument has, real and simulated, and when the first and the
-- newest one started.
SELECT instrument, area, COUNT(*) AS sessions,
       MIN(started_utc) AS first_started, MAX(started_utc) AS newest_started
FROM sessions
WHERE NOT withdrawn
GROUP BY instrument, area
ORDER BY instrument, area"""),

    ("Sessions that measured a sample", """\
-- Every session that measured one sample. The view session_samples has one row per sample
-- of a session. Put your sample id between the quotes.
SELECT s.folder, s.instrument, s.started_utc, s.method, s.completion
FROM session_samples x
JOIN sessions s ON s.path = x.session_path
WHERE x.sample_id = 'DEMO-S-101'
ORDER BY s.started_utc DESC"""),

    ("Points with a label above a confidence", """\
-- Points of each session's current classify run that count as one label with at least a
-- given confidence. effective_label is the reviewer's label when a point was relabelled.
-- Change the label and the number.
SELECT session_path, point_id, effective_label, confidence, review_status
FROM current_points
WHERE effective_label = 'adenine' AND confidence >= 0.9
ORDER BY confidence DESC
LIMIT 200"""),

    ("Label counts per session", """\
-- How many points of each session have each label (after reviews), through the view
-- session_labels.
SELECT s.folder, l.label, l.n
FROM session_labels l
JOIN sessions s ON s.path = l.session_path
ORDER BY s.folder, l.n DESC, l.label"""),

    ("A session's files, largest first", """\
-- The files of the newest session, largest first. To see another session, replace the part
-- in brackets by its path in quotes: WHERE session_path = 'raw/...'
SELECT rel_path, bytes, sha256
FROM files
WHERE session_path = (SELECT path FROM sessions
                      ORDER BY started_utc DESC NULLS LAST LIMIT 1)
ORDER BY bytes DESC
LIMIT 100"""),

    ("Sessions awaiting review", """\
-- Sessions whose current classify run has points that still need a person's review, the
-- ones with most open points first.
SELECT s.folder, s.instrument, r.n_needs_review, r.last_run_utc
FROM session_rollup r
JOIN sessions s ON s.path = r.session_path
WHERE r.review_state = 'pending' AND NOT s.withdrawn
ORDER BY r.n_needs_review DESC, s.folder"""),

    ("Sessions of the last 7 days", {
        "sqlite": """\
-- Sessions that started in the last 7 days. Times are stored as UTC text like
-- 2026-11-17T03:50:15.281Z, so the start of the period is built in the same form.
SELECT folder, instrument, started_utc, completion
FROM sessions
WHERE started_utc >= strftime('%Y-%m-%dT%H:%M:%fZ', 'now', '-7 days')
ORDER BY started_utc DESC""",
        "postgresql": """\
-- Sessions that started in the last 7 days. Change the number of days in the interval.
SELECT folder, instrument, started_utc, completion
FROM sessions
WHERE started_utc >= now() - interval '7 days'
ORDER BY started_utc DESC""",
    }),

    ("Storage used per instrument", """\
-- How much each instrument's sessions take on ARC: sessions, files, bytes and megabytes.
SELECT instrument, COUNT(*) AS sessions, SUM(n_files) AS files,
       CAST(SUM(bytes) AS BIGINT) AS total_bytes,
       CAST(ROUND(SUM(bytes) / 1048576.0, 1) AS DOUBLE PRECISION) AS megabytes
FROM sessions
GROUP BY instrument
ORDER BY total_bytes DESC"""),

    ("Where a session's data is", """\
-- Where the files of the newest sessions are: the folder on ARC (for scp, sftp or rsync)
-- and the address that opens the folder in the Globus web app.
SELECT folder, storage_path, web_url
FROM sessions
WHERE NOT withdrawn
ORDER BY started_utc DESC NULLS LAST
LIMIT 20"""),

    ("Simulated versus real", """\
-- How many sessions are simulated and how many are real measurements, with their size.
SELECT CASE WHEN simulated THEN 'simulated' ELSE 'real' END AS kind,
       COUNT(*) AS sessions, CAST(SUM(bytes) AS BIGINT) AS total_bytes
FROM sessions
WHERE NOT withdrawn
GROUP BY 1
ORDER BY 1"""),

    ("Sessions per day", {
        "sqlite": """\
-- How many sessions started on each day (UTC), newest day first. The first ten characters
-- of the time text are the date.
SELECT substr(started_utc, 1, 10) AS day, COUNT(*) AS sessions
FROM sessions
WHERE started_utc IS NOT NULL
GROUP BY 1
ORDER BY 1 DESC
LIMIT 60""",
        "postgresql": """\
-- How many sessions started on each day (UTC), newest day first.
SELECT to_char(started_utc AT TIME ZONE 'UTC', 'YYYY-MM-DD') AS day, COUNT(*) AS sessions
FROM sessions
WHERE started_utc IS NOT NULL
GROUP BY 1
ORDER BY 1 DESC
LIMIT 60""",
    }),

    ("Sessions per tag", """\
-- How many sessions carry each tag, through the view session_tags (one row per tag of a
-- session).
SELECT t.tag, COUNT(*) AS sessions
FROM session_tags t
GROUP BY t.tag
ORDER BY sessions DESC, t.tag"""),

    ("Sessions that did not finish", """\
-- Sessions that ended early or failed, with how many measurements were taken and planned.
SELECT folder, instrument, started_utc, completion, items, items_planned
FROM sessions
WHERE completion IN ('partial', 'failed')
ORDER BY started_utc DESC NULLS LAST"""),

    ("A value from the lab card", {
        "sqlite": """\
-- Read a value out of the lab card, which is kept as JSON text. '$.schema' is the card's
-- format name. Write another path, such as '$.written_by.name', to read something else.
SELECT folder, json_extract(card_json, '$.schema') AS card_format
FROM sessions
WHERE has_card
ORDER BY started_utc DESC NULLS LAST
LIMIT 20""",
        "postgresql": """\
-- Read a value out of the lab card, which is kept as JSON. ->> 'schema' is the card's format
-- name. Chain -> for nested values, for example card_json -> 'written_by' ->> 'name'.
SELECT folder, card_json ->> 'schema' AS card_format
FROM sessions
WHERE has_card
ORDER BY started_utc DESC NULLS LAST
LIMIT 20""",
    }),

    ("Problems the nightly index found", """\
-- What the nightly index reported, by kind of problem, with one example path each.
SELECT code, COUNT(*) AS n, MIN(path) AS example_path
FROM problems
GROUP BY code
ORDER BY n DESC, code"""),
)


def examples(engine):
    """The example queries for one engine ("sqlite" or "postgresql"), in the order the
    website shows them: [{"title": ..., "sql": ...}]."""
    if engine not in ENGINES:
        raise ValueError("unknown engine %r (known: %s)" % (engine, ", ".join(ENGINES)))
    result = []
    for title, text in _EXAMPLES:
        result.append({"title": title, "sql": text[engine] if isinstance(text, dict) else text})
    return result
