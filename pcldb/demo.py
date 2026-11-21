"""Demo data: a small, clearly fake catalogue to try pcldb with before any real session exists.

`build_demo_index(path, now)` writes a `sessions.sqlite` in exactly the shape the lab's nightly
index has (`INDEX_DDL`), `load_demo(db, settings, now)` loads it through the same code that
loads a real index (`reconcile.load_index`, with the source `demo`) and marks the catalogue as
holding demo data, and `clear_demo(db)` removes it again.

Everything in it is made up and says so: every session is `simulated`, tagged `demo`, lies
under `raw/<instrument>/simulated/`, and the people are `demo-operator-a`, `demo-operator-b`
and `demo-reviewer`. The folders do not exist on ARC. For one `now` the result is always the
same: nothing is random (the numbers come from SHA-256 of fixed texts).

What it holds: 12 sessions of `raman-portable` and 4 of `jeol-jsm-it800-shl` over the last 30
days; classify runs of two models with labels `adenine`, `methylene_blue`, `r6g` and `UNKNOWN`;
a run that was made again; one review (a point relabelled, one rejected, one confirmed); one
correction of a sample id; a partial and a failed session; a session that is not classified
yet; two problems of the kind the nightly check reports.

Demo data and real data never mix: the demo is refused when real sessions are registered, and
a real index or a registration is refused while the demo is loaded.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

from . import __version__, reconcile, util
from .errors import Conflict, PclError
from .store import schema_def

# The nightly index as `pcl_index build` writes it (its SQLite dialect), statement by
# statement: tables, indexes, views. An index file built here has exactly this shape.
INDEX_DDL = (
    """CREATE TABLE meta (
    key TEXT NOT NULL,
    value TEXT,
    PRIMARY KEY (key)
)""",
    """CREATE TABLE sessions (
    path TEXT NOT NULL,
    instrument TEXT NOT NULL,
    area TEXT NOT NULL,
    folder TEXT NOT NULL,
    session_name TEXT,
    session_id TEXT,
    is_copy INTEGER NOT NULL,
    started_utc TEXT,
    ended_utc TEXT,
    simulated INTEGER NOT NULL,
    sample_ids TEXT NOT NULL,
    operator TEXT,
    location TEXT,
    method TEXT,
    items INTEGER,
    items_planned INTEGER,
    completion TEXT,
    notes TEXT,
    tags TEXT NOT NULL,
    format TEXT,
    uploaded_utc TEXT,
    upload_id TEXT,
    has_card INTEGER NOT NULL,
    card_json TEXT,
    corrections_json TEXT NOT NULL,
    corrected INTEGER NOT NULL,
    withdrawn INTEGER NOT NULL,
    bytes INTEGER NOT NULL,
    n_files INTEGER NOT NULL,
    sums_sha256 TEXT,
    verified_utc TEXT,
    verify_ok INTEGER,
    PRIMARY KEY (path)
)""",
    """CREATE TABLE session_samples (
    session_path TEXT NOT NULL,
    sample_id TEXT NOT NULL,
    PRIMARY KEY (session_path, sample_id)
)""",
    """CREATE TABLE session_tags (
    session_path TEXT NOT NULL,
    tag TEXT NOT NULL,
    PRIMARY KEY (session_path, tag)
)""",
    """CREATE TABLE files (
    session_path TEXT NOT NULL,
    rel_path TEXT NOT NULL,
    bytes INTEGER NOT NULL,
    sha256 TEXT,
    listed INTEGER NOT NULL,
    PRIMARY KEY (session_path, rel_path)
)""",
    """CREATE TABLE runs (
    path TEXT NOT NULL,
    instrument TEXT NOT NULL,
    area TEXT NOT NULL,
    folder TEXT NOT NULL,
    kind TEXT,
    made_utc TEXT,
    session_path TEXT,
    stated_session_path TEXT,
    session_id TEXT,
    sums_sha256 TEXT,
    derived_from TEXT NOT NULL,
    tool_name TEXT,
    tool_version TEXT,
    model_name TEXT,
    model_sha256 TEXT,
    runner TEXT,
    slurm_job_id TEXT,
    run_by TEXT,
    set_json TEXT,
    why TEXT,
    run_json TEXT,
    valid INTEGER NOT NULL,
    PRIMARY KEY (path)
)""",
    """CREATE TABLE points (
    run_path TEXT NOT NULL,
    point_file TEXT NOT NULL,
    point_id INTEGER NOT NULL,
    capture_id TEXT,
    label TEXT,
    top_class TEXT,
    confidence REAL,
    outcome TEXT,
    needs_review INTEGER NOT NULL,
    threshold REAL,
    reason TEXT,
    raw_sha256 TEXT,
    scores_json TEXT,
    PRIMARY KEY (run_path, point_file)
)""",
    """CREATE TABLE reviews (
    run_path TEXT NOT NULL,
    row_no INTEGER NOT NULL,
    reviewed_run TEXT,
    point_id INTEGER NOT NULL,
    capture_id TEXT,
    status TEXT NOT NULL,
    label TEXT,
    reviewer TEXT,
    time_utc TEXT NOT NULL,
    comment TEXT,
    PRIMARY KEY (run_path, row_no)
)""",
    """CREATE TABLE models (
    name TEXT NOT NULL,
    path TEXT NOT NULL,
    sha256 TEXT,
    classes_json TEXT,
    threshold REAL,
    files_json TEXT,
    is_current INTEGER NOT NULL,
    model_json TEXT,
    PRIMARY KEY (name)
)""",
    """CREATE TABLE problems (
    problem_no INTEGER NOT NULL,
    path TEXT NOT NULL,
    code TEXT NOT NULL,
    detail TEXT,
    fix TEXT,
    session_path TEXT,
    PRIMARY KEY (problem_no)
)""",
    """CREATE TABLE session_rollup (
    session_path TEXT NOT NULL,
    labels TEXT NOT NULL,
    label_counts TEXT NOT NULL,
    models TEXT NOT NULL,
    problems TEXT NOT NULL,
    review_state TEXT NOT NULL,
    n_needs_review INTEGER NOT NULL,
    last_run_utc TEXT,
    PRIMARY KEY (session_path)
)""",
    "CREATE INDEX runs_session ON runs (session_path, kind, model_name)",
    "CREATE INDEX points_run ON points (run_path, point_id)",
    "CREATE INDEX reviews_run ON reviews (reviewed_run, point_id)",
    "CREATE INDEX files_sha256 ON files (sha256)",
    "CREATE INDEX samples_id ON session_samples (sample_id)",
    "CREATE INDEX sessions_started ON sessions (started_utc)",
    """CREATE VIEW current_runs AS
SELECT r.path, r.instrument, r.area, r.session_path, r.made_utc, r.model_name, r.model_sha256
FROM runs r
WHERE r.kind = 'classify' AND r.valid = 1 AND r.session_path IS NOT NULL
  AND r.model_name = (SELECT m.value FROM meta m WHERE m.key = 'current_model')
  AND r.model_sha256 = (SELECT d.sha256 FROM models d WHERE d.name = r.model_name)
  AND NOT EXISTS (
    SELECT 1 FROM runs n
    WHERE n.kind = 'classify' AND n.valid = 1 AND n.session_path = r.session_path
      AND n.model_name = r.model_name AND n.model_sha256 = r.model_sha256
      AND (n.made_utc > r.made_utc OR (n.made_utc = r.made_utc AND n.path > r.path)))""",
    """CREATE VIEW latest_reviews AS
SELECT v.run_path, v.row_no, v.reviewed_run, v.point_id, v.capture_id, v.status, v.label, v.reviewer,
       v.time_utc, v.comment
FROM reviews v
WHERE NOT EXISTS (
  SELECT 1 FROM reviews w
  WHERE w.reviewed_run = v.reviewed_run AND w.point_id = v.point_id
    AND (w.time_utc > v.time_utc
         OR (w.time_utc = v.time_utc AND w.run_path > v.run_path)
         OR (w.time_utc = v.time_utc AND w.run_path = v.run_path AND w.row_no > v.row_no)))""",
    """CREATE VIEW current_points AS
SELECT s.path AS session_path, s.instrument, s.simulated, s.withdrawn, s.is_copy,
       c.path AS run_path, c.made_utc AS run_made_utc, c.model_name,
       p.point_file, p.point_id, p.capture_id, p.label AS model_label, p.top_class, p.confidence,
       p.outcome, p.needs_review, p.threshold, p.reason, p.raw_sha256, p.scores_json,
       v.status AS review_status, v.label AS review_label, v.reviewer, v.time_utc AS review_time_utc,
       v.run_path AS review_run,
       CASE WHEN v.status = 'rejected' THEN NULL
            WHEN v.status = 'relabelled' THEN v.label
            ELSE p.label END AS effective_label,
       CASE WHEN v.status IS NULL AND p.needs_review = 1 THEN 1 ELSE 0 END AS awaiting_review
FROM current_runs c
JOIN sessions s ON s.path = c.session_path
JOIN points p ON p.run_path = c.path
LEFT JOIN latest_reviews v ON v.reviewed_run = c.path AND v.point_id = p.point_id""",
    """CREATE VIEW session_summary AS
SELECT s.instrument, s.folder, s.session_id, s.path, s.method, s.completion, s.format, s.location,
       s.operator, COALESCE(r.review_state, 'none') AS review_state, s.notes,
       s.sample_ids, s.tags, r.labels, r.label_counts, r.models, r.problems,
       s.simulated, s.is_copy, s.withdrawn, s.corrected,
       s.items, s.items_planned, s.n_files AS files, s.bytes, COALESCE(r.n_needs_review, 0) AS n_needs_review,
       s.started_utc AS started, s.uploaded_utc AS uploaded,
       CASE WHEN s.verify_ok = 1 THEN s.verified_utc ELSE NULL END AS verified,
       r.last_run_utc AS last_run
FROM sessions s
LEFT JOIN session_rollup r ON r.session_path = s.path""",
)

# The tables of an index file, in the order they are filled, with their columns.
INDEX_TABLES = dict(reconcile.INDEX_COLUMNS,
                    session_samples=("session_path", "sample_id"),
                    session_tags=("session_path", "tag"))

DEMO_ACTOR = "pcldb-demo"
DEMO_TAG = "demo"
DEMO_TOOL = "pcldb demo " + __version__
OPERATORS = ("demo-operator-a", "demo-operator-b")
REVIEWER = "demo-reviewer"
RAMAN = "raman-portable"
SEM = "jeol-jsm-it800-shl"
MODEL_CURRENT = "demo_pca_svm_20260922"
MODEL_OLDER = "demo_pca_svm_20260601"
CLASSES = ("r6g", "methylene_blue", "adenine", "di_water")
THRESHOLD = 0.8
NOTES = "Demo data made by pcldb. Nothing was measured and the folder does not exist on ARC."

_LETTERS = {"A": "adenine", "M": "methylene_blue", "R": "r6g"}
_ANALYTES = ("adenine", "methylene_blue", "r6g")

# The Raman sessions, newest first. `runs` are classify runs: (model, hours after the start,
# one letter per point: B background, A adenine, M methylene_blue, R r6g, L low confidence).
_RAMAN_SESSIONS = (
    {"days": 0.6, "method": "1d-y", "planned": 13, "taken": 13, "completion": "complete",
     "samples": ["DEMO-S-101"], "tags": [], "runs": []},
    {"days": 1.4, "method": "1d-y", "planned": 13, "taken": 13, "completion": "complete",
     "samples": ["DEMO-S-102"], "tags": [],
     "runs": [(MODEL_CURRENT, 2, "BAAAAAAAAAALB")]},
    {"days": 2.3, "method": "2d-map", "planned": 25, "taken": 25, "completion": "complete",
     "samples": ["DEMO-S-103", "DEMO-S-113"], "tags": ["mapping"],
     "runs": [(MODEL_CURRENT, 3, "B" + "R" * 10 + "M" * 10 + "LLBB")]},
    {"days": 3.2, "method": "1d-x", "planned": 9, "taken": 9, "completion": "complete",
     "samples": ["DEMO-S-104"], "tags": [],
     "runs": [(MODEL_CURRENT, 2, "BAAAAMMLL")], "review": True},
    {"days": 5.1, "method": "1d-y", "planned": 13, "taken": 13, "completion": "complete",
     "samples": ["DEMO-S-150"], "tags": [],
     "runs": [(MODEL_CURRENT, 2, "BRRRRRRRRRRRB")], "correct_samples": ["DEMO-S-105"]},
    {"days": 6.4, "method": "1d-y", "planned": 13, "taken": 5, "completion": "partial",
     "samples": ["DEMO-S-106"], "tags": [],
     "runs": [(MODEL_CURRENT, 2, "BMMML")]},
    {"days": 8.2, "method": "single", "planned": 1, "taken": 0, "completion": "failed",
     "samples": ["DEMO-S-107"], "tags": [], "runs": []},
    {"days": 10.5, "method": "2d-map", "planned": 25, "taken": 25, "completion": "complete",
     "samples": ["DEMO-S-108"], "tags": ["mapping"],
     "runs": [(MODEL_CURRENT, 4, "BB" + "A" * 8 + "R" * 8 + "M" * 7)]},
    {"days": 13.3, "method": "1d-x", "planned": 9, "taken": 9, "completion": "complete",
     "samples": ["DEMO-S-109"], "tags": [],
     "runs": [(MODEL_CURRENT, 2, "BMMMLMMMM"), (MODEL_CURRENT, 30, "BMMMMMMMM")]},
    {"days": 17.6, "method": "1d-y", "planned": 13, "taken": 13, "completion": "complete",
     "samples": ["DEMO-S-110"], "tags": ["calibration"],
     "runs": [(MODEL_OLDER, 2, "BAAALRRRLMMLB"), (MODEL_CURRENT, 72, "BAAAARRRRMMLB")]},
    {"days": 22.4, "method": "1d-y", "planned": 13, "taken": 13, "completion": "complete",
     "samples": ["DEMO-S-111"], "tags": [],
     "runs": [(MODEL_OLDER, 2, "BBRRRRRRRRRRR"), (MODEL_CURRENT, 96, "BBRRRRRRRRRRR")],
     "unlisted_file": "notes_demo.txt"},
    {"days": 28.1, "method": "1d-x", "planned": 9, "taken": 9, "completion": "complete",
     "samples": ["DEMO-S-112"], "tags": ["calibration"],
     "runs": [(MODEL_OLDER, 2, "BAAAAAAAL")]},
)

# The electron microscope sessions: (days ago, detector mode, images, sample).
_SEM_SESSIONS = (
    (1.9, "sed", 6, "DEMO-S-201"),
    (7.3, "bed", 4, "DEMO-S-202"),
    (15.2, "sed", 8, "DEMO-S-203"),
    (25.6, "sed", 5, "DEMO-S-204"),
)


# ---------------------------------------------------------------------- fixed "random" numbers


def _digest(*parts):
    text = "pcldb-demo|" + "|".join(str(part) for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _unit(*parts):
    """A number from 0 (included) to 1 (not included) that depends only on `parts`."""
    return int(_digest(*parts)[:13], 16) / float(16 ** 13)


def _moment(now):
    """`now` as an aware UTC datetime cut to the millisecond (None = the time now)."""
    moment = util.parse_utc(util.now_utc_text() if now is None else now)
    return moment.replace(microsecond=moment.microsecond // 1000 * 1000)


def _text(moment):
    return util.utc_text(moment)


def _stamp(moment):
    """The compact stamp of run folder names: 20261119T023150Z."""
    return "%04d%02d%02dT%02d%02d%02dZ" % (moment.year, moment.month, moment.day, moment.hour,
                                          moment.minute, moment.second)


def _folder_stamp(moment):
    return "%04d%02d%02d_%02d%02d%02d" % (moment.year, moment.month, moment.day, moment.hour,
                                         moment.minute, moment.second)


# ---------------------------------------------------------------------- the rows


def _card(row, extra):
    """The lab card (pcl-session/1) of a demo session, as a registration would carry it."""
    card = {
        "schema": "pcl-session/1", "instrument": row["instrument"], "session": row["folder"],
        "session_id": row["session_id"], "started_utc": row["started_utc"],
        "ended_utc": row["ended_utc"], "simulated": True, "sample_ids": list(row["sample_ids"]),
        "operator": row["operator"], "location": row["location"], "notes": row["notes"],
        "method": row["method"], "items": row["items"], "items_planned": row["items_planned"],
        "completion": row["completion"], "format": row["format"],
        "instrument_info": {"model": "demo instrument", "serial": "demo-0000"},
        "uploaded_utc": row["uploaded_utc"], "upload_id": row["upload_id"],
        "written_by": {"name": "pcldb-demo", "version": __version__},
        "tags": list(row["tags"]), "extra": dict(extra, demo=True),
    }
    return card


def _session(number, instrument, folder, started, ended, now, *, samples, method, items, planned,
             completion, tags, fmt, location, extra):
    """One row of the index's sessions table (files and sizes are filled in afterwards)."""
    uploaded = min(started + datetime.timedelta(minutes=20 + int(_unit(number, "up") * 40)), now)
    checked = now - datetime.timedelta(hours=2)
    verified = checked if checked > uploaded else None
    row = {
        "path": "raw/%s/simulated/%s" % (instrument, folder), "instrument": instrument,
        "area": "simulated", "folder": folder, "session_name": folder,
        "session_id": "0d0d0d0d-demo-4000-8000-%012d" % number, "is_copy": False,
        "started_utc": _text(started), "ended_utc": _text(ended) if ended else None,
        "simulated": True, "sample_ids": list(samples),
        "operator": OPERATORS[number % len(OPERATORS)], "location": location, "method": method,
        "items": items, "items_planned": planned, "completion": completion, "notes": NOTES,
        "tags": [DEMO_TAG] + list(tags), "format": fmt, "uploaded_utc": _text(uploaded),
        "upload_id": "u" + _digest(number, "upload")[:12], "has_card": True, "card_json": None,
        "corrections_json": [], "corrected": False, "withdrawn": False, "bytes": 0,
        "n_files": 0, "sums_sha256": None,
        "verified_utc": _text(verified) if verified else None,
        "verify_ok": True if verified else None,
    }
    row["card_json"] = _card(row, extra)
    return row


def _files(row, names):
    """The files rows of one session from [(rel_path, bytes, listed)]; fills in the session's
    size, file count and the SHA-256 of its checksum list."""
    card_bytes = len(util.canonical_json(row["card_json"]).encode("utf-8")) + 1
    listed = [("pcl_session.json", card_bytes, True)] + list(names)
    files, lines = [], []
    for rel_path, size, is_listed in sorted(listed):
        digest = _digest(row["session_id"], "file", rel_path) if is_listed else None
        files.append({"session_path": row["path"], "rel_path": rel_path, "bytes": size,
                      "sha256": digest, "listed": is_listed})
        if is_listed:
            lines.append("%s  %s\n" % (digest, rel_path))
    sums_text = "".join(lines)
    sums_sha256 = util.sha256_hex(sums_text.encode("utf-8"))
    files.append({"session_path": row["path"], "rel_path": "SHA256SUMS",
                  "bytes": len(sums_text.encode("utf-8")), "sha256": sums_sha256, "listed": True})
    row["sums_sha256"] = sums_sha256
    row["n_files"] = len(files)
    row["bytes"] = sum(entry["bytes"] for entry in files)
    return files


def _scores(seed, top, confidence):
    """A score for every class: `confidence` for the top one, the rest shared out."""
    others = [name for name in CLASSES if name != top]
    weights = [0.2 + _unit(seed, "share", name) for name in others]
    total = sum(weights)
    scores = {top: confidence}
    rest = round(1.0 - confidence, 6)
    for name, weight in zip(others[:-1], weights[:-1]):
        scores[name] = round(rest * weight / total, 6)
    scores[others[-1]] = round(max(0.0, rest - sum(scores[name] for name in others[:-1])), 6)
    return scores


def _points(run_path, session, model, pattern):
    """The points rows of one classify run from its pattern of letters."""
    rows = []
    for number, letter in enumerate(pattern, 1):
        seed = "%s|%s|%d" % (session["session_id"], model, number)
        point_file = "point_%04d.json" % number
        if letter == "B":
            top, outcome, label, review = "di_water", "background", "UNKNOWN", False
            confidence = round(0.90 + 0.09 * _unit(seed, "c"), 6)
            reason = "Background: the solvent only"
        elif letter == "L":
            top = _ANALYTES[number % len(_ANALYTES)]
            outcome, label, review = "low_confidence", "UNKNOWN", True
            confidence = round(0.45 + 0.33 * _unit(seed, "c"), 6)
            reason = "Low pure-class confidence"
        else:
            top = _LETTERS[letter]
            outcome, label, review = "confident", top, False
            confidence = round(0.86 + 0.13 * _unit(seed, "c"), 6)
            reason = None
        rows.append({
            "run_path": run_path, "point_file": point_file, "point_id": number,
            "capture_id": "%s_%d" % (session["session_id"], number), "label": label,
            "top_class": top, "confidence": confidence, "outcome": outcome,
            "needs_review": review, "threshold": THRESHOLD, "reason": reason,
            "raw_sha256": _digest(session["session_id"], "file", point_file),
            "scores_json": _scores(seed, top, confidence)})
    return rows


def _run(session, made, kind, by, *, model=None, model_sha256=None, derived_from=(), set_values=None,
         why=None):
    """One row of the index's runs table, with the run.json it stands for."""
    folder = "%s__%s_%s" % (session["folder"], _stamp(made), kind)
    run_json = {
        "schema": "pcl-run/1", "kind": kind, "made_utc": _text(made),
        "session": {"path": session["path"], "session_id": session["session_id"],
                    "sums_sha256": session["sums_sha256"]},
        "derived_from": list(derived_from), "tool": {"name": "pcldb-demo", "version": __version__},
        "by": by, "job": {"runner": "demo", "slurm_job_id": None},
    }
    if model:
        run_json["model"] = {"name": model, "sha256": model_sha256}
    if set_values is not None:
        run_json["set"] = set_values
        run_json["why"] = why
    return {
        "path": "results/%s/simulated/%s" % (session["instrument"], folder),
        "instrument": session["instrument"], "area": "simulated", "folder": folder, "kind": kind,
        "made_utc": _text(made), "session_path": session["path"],
        "stated_session_path": session["path"], "session_id": session["session_id"],
        "sums_sha256": session["sums_sha256"], "derived_from": list(derived_from),
        "tool_name": "pcldb-demo", "tool_version": __version__, "model_name": model,
        "model_sha256": model_sha256, "runner": "demo", "slurm_job_id": None, "run_by": by,
        "set_json": set_values, "why": why, "run_json": run_json, "valid": True,
    }


def _model(name, is_current, added):
    files = [{"name": file_name, "bytes": 900 + int(_unit(name, file_name) * 90000),
              "sha256": _digest(name, "model-file", file_name)}
             for file_name in ("classifier.bin", "meta.json", "scaler.bin")]
    sha256 = _digest(name, "model")
    model_json = {"schema": "pcl-model/1", "name": name, "sha256": sha256, "hash_rule": "demo",
                  "classes": list(CLASSES), "threshold": THRESHOLD, "files": files,
                  "added_utc": _text(added),
                  "added_with": {"name": "pcldb-demo", "version": __version__}}
    return {"name": name, "path": "models/" + name, "sha256": sha256,
            "classes_json": list(CLASSES), "threshold": THRESHOLD, "files_json": files,
            "is_current": is_current, "model_json": model_json}


def rollups(sessions, runs, points, reviews, models, problems, current_model):
    """The session_rollup rows, by the rules of the nightly index: the labels of each session's
    newest valid classify run of the current model (name and hash both matching) after the
    newest review of each point; every model that classified it; its problem codes; whether
    points still wait for a reviewer; the time of its newest run of any kind."""
    model_sha = {model["name"]: model["sha256"] for model in models}
    joined, points_of, latest = {}, {}, {}
    for run in runs:
        if run["valid"] and run["session_path"]:
            joined.setdefault(run["session_path"], []).append(run)
    for point in points:
        points_of.setdefault(point["run_path"], []).append(point)
    for review in reviews:
        key = (review["reviewed_run"], review["point_id"])
        order = (review["time_utc"], review["run_path"], review["row_no"])
        if key not in latest or order > latest[key][0]:
            latest[key] = (order, review)
    codes = {}
    for problem in problems:
        if problem["session_path"]:
            codes.setdefault(problem["session_path"], set()).add(problem["code"])
    rows = []
    for session in sessions:
        own = joined.get(session["path"], [])
        classify = [run for run in own if run["kind"] == "classify"]
        current = [run for run in classify
                   if run["model_name"] == current_model and run["model_name"] in model_sha
                   and run["model_sha256"] == model_sha[run["model_name"]]]
        counts, awaiting, touched = {}, 0, False
        if current:
            newest = max(current, key=lambda run: (run["made_utc"], run["path"]))
            for point in points_of.get(newest["path"], []):
                found = latest.get((newest["path"], point["point_id"]))
                status = found[1]["status"] if found else None
                if status == "rejected":
                    label = None
                elif status == "relabelled":
                    label = found[1]["label"]
                else:
                    label = point["label"]
                if label is not None:
                    counts[label] = counts.get(label, 0) + 1
                if status is None and point["needs_review"]:
                    awaiting += 1
                if status is not None or point["needs_review"]:
                    touched = True
        made = [run["made_utc"] for run in own if run["made_utc"]]
        rows.append({
            "session_path": session["path"], "labels": sorted(counts),
            "label_counts": [{"label": label, "count": counts[label]} for label in sorted(counts)],
            "models": sorted({run["model_name"] for run in classify if run["model_name"]}),
            "problems": sorted(codes.get(session["path"], ())),
            "review_state": "pending" if awaiting else "done" if touched else "none",
            "n_needs_review": awaiting, "last_run_utc": max(made) if made else None})
    return rows


def demo_tables(now=None):
    """(meta, tables): everything the demo index holds, as normalised values.

    `tables` maps each table of the index to its rows (dicts). The same `now` always gives the
    same result.
    """
    now = _moment(now)
    models = [_model(MODEL_CURRENT, True, now - datetime.timedelta(days=59)),
              _model(MODEL_OLDER, False, now - datetime.timedelta(days=172))]
    model_sha = {model["name"]: model["sha256"] for model in models}
    sessions, files, runs, points, reviews, problems = [], [], [], [], [], []

    for index, spec in enumerate(_RAMAN_SESSIONS):
        number = index + 1
        started = now - datetime.timedelta(days=spec["days"],
                                           seconds=int(_unit(number, "s") * 3000),
                                           milliseconds=int(_unit(number, "ms") * 900))
        taken = spec["taken"]
        ended = started + datetime.timedelta(seconds=2 * taken + 1) if taken else None
        tag = spec["method"].replace("-", "_")
        folder = "%s_scan_%s_%03d" % (tag, _folder_stamp(started), started.microsecond // 1000)
        session = _session(
            number, RAMAN, folder, started, ended, now, samples=spec["samples"],
            method=spec["method"], items=taken, planned=spec["planned"],
            completion=spec["completion"], tags=spec["tags"], fmt="raman-session/1 folder",
            location="demo-bench-%d" % (1 + number % 2),
            extra={"kind": "simulated", "end": {"complete": "finished", "partial": "stopped",
                                                "failed": "failed"}[spec["completion"]]})
        names = [("session.json", 780 + int(_unit(number, "session") * 60), True)]
        names += [("point_%04d.json" % point, 39000 + int(_unit(number, "point", point) * 2000),
                   True) for point in range(1, taken + 1)]
        if taken:
            names.append(("dark/dark_%s.txt" % _folder_stamp(started),
                          47000 + int(_unit(number, "dark") * 1500), True))
        if spec.get("unlisted_file"):
            names.append((spec["unlisted_file"], 120 + int(_unit(number, "note") * 80), False))
            problems.append({"path": session["path"], "code": "sums_unlisted",
                             "detail": "1 file is not in SHA256SUMS: " + spec["unlisted_file"],
                             "fix": "Find out where the file came from. A placed folder is "
                                    "never changed: when the file belongs to the session, "
                                    "place the session again as a copy.",
                             "session_path": session["path"]})
        files.extend(_files(session, names))
        sessions.append(session)

        classify_runs = []
        for model, hours, pattern in spec["runs"]:
            made = started + datetime.timedelta(hours=hours, seconds=int(_unit(number, model, hours) * 50))
            run = _run(session, made, "classify", "demo-classifier", model=model,
                       model_sha256=model_sha[model])
            runs.append(run)
            classify_runs.append(run)
            points.extend(_points(run["path"], session, model, pattern))
        if spec.get("review"):
            target = classify_runs[-1]
            made = util.parse_utc(target["made_utc"]) + datetime.timedelta(hours=2)
            review_run = _run(session, made, "review", REVIEWER, derived_from=[target["folder"]])
            runs.append(review_run)
            decisions = ((2, "confirmed", "adenine"), (8, "relabelled", "adenine"),
                         (9, "rejected", None))
            for row_no, (point_id, status, label) in enumerate(decisions, 1):
                reviews.append({
                    "run_path": review_run["path"], "row_no": row_no,
                    "reviewed_run": target["path"], "point_id": point_id,
                    "capture_id": "%s_%d" % (session["session_id"], point_id), "status": status,
                    "label": label, "reviewer": REVIEWER,
                    "time_utc": _text(made - datetime.timedelta(seconds=30 * (4 - row_no))),
                    "comment": "demo review"})
        if spec.get("correct_samples"):
            made = started + datetime.timedelta(hours=26)
            original = list(session["sample_ids"])
            why = "Demo correction: the sample id was typed wrong at the instrument."
            correction = _run(session, made, "correction", REVIEWER,
                              set_values={"sample_ids": list(spec["correct_samples"])}, why=why)
            runs.append(correction)
            session["sample_ids"] = list(spec["correct_samples"])
            session["corrected"] = True
            session["corrections_json"] = [{
                "run": correction["folder"], "made_utc": correction["made_utc"],
                "set": {"sample_ids": list(spec["correct_samples"])}, "why": why, "by": REVIEWER,
                "original": {"sample_ids": original}}]

    for index, (days, mode, images, sample) in enumerate(_SEM_SESSIONS):
        number = len(_RAMAN_SESSIONS) + index + 1
        started = now - datetime.timedelta(days=days, seconds=int(_unit(number, "s") * 3000),
                                           milliseconds=int(_unit(number, "ms") * 900))
        ended = started + datetime.timedelta(minutes=3 * images)
        folder = "sem_demo_%s" % _folder_stamp(started)
        session = _session(
            number, SEM, folder, started, ended, now, samples=[sample], method=mode,
            items=images, planned=images, completion="complete", tags=[],
            fmt="sem-session/1 folder", location="demo-sem-room", extra={"kind": "simulated"})
        names = []
        for image in range(1, images + 1):
            names.append(("image_%04d.tif" % image,
                          4800000 + int(_unit(number, "tif", image) * 400000), True))
            names.append(("image_%04d.txt" % image,
                          2100 + int(_unit(number, "txt", image) * 300), True))
        files.extend(_files(session, names))
        sessions.append(session)

    problems.append({"path": "raw/%s/_incoming/demo_leftover" % RAMAN, "code": "incoming_leftover",
                     "detail": "untouched for more than 7 days (demo)",
                     "fix": "Ask who started the upload. Finish it or delete the leftover.",
                     "session_path": None})
    problems.sort(key=lambda row: (row["path"], row["code"], row["detail"] or ""))
    for number, problem in enumerate(problems, 1):
        problem["problem_no"] = number

    sessions.sort(key=lambda row: row["path"])
    files.sort(key=lambda row: (row["session_path"], row["rel_path"]))
    runs.sort(key=lambda row: row["path"])
    tables = {
        "sessions": sessions, "files": files, "runs": runs, "points": points,
        "reviews": reviews, "models": models, "problems": problems,
        "session_rollup": rollups(sessions, runs, points, reviews, models, problems,
                                  MODEL_CURRENT),
    }
    meta = {"schema": reconcile.INDEX_SCHEMA, "built_utc": _text(now), "tool": DEMO_TOOL,
            "current_model": MODEL_CURRENT}
    return meta, tables


# ---------------------------------------------------------------------- the index file


def _file_value(name, value):
    """A normalised value as the index file stores it: 0/1, JSON text, the time text."""
    if value is None:
        return None
    kind = schema_def.COLUMN_KINDS.get(name, "text")
    if kind == "bool" or isinstance(value, bool):
        return int(value)
    if kind in ("json", "strlist") or isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True, ensure_ascii=False)
    return value


def write_index(path, meta, tables):
    """Write an index file of the nightly shape from normalised rows.

    `meta`: {key: text}. `tables`: {table: [row dict, ...]}; a table that is left out stays
    empty; a column that a row leaves out is NULL. The file's own `session_samples` and
    `session_tags` tables are filled from the sessions unless they are given. An existing file
    is replaced. Returns {table: number of rows}.
    """
    path = Path(path)
    unknown = sorted(set(tables) - set(INDEX_TABLES))
    if unknown:
        raise ValueError("an index file has no table named %s" % ", ".join(unknown))
    tables = dict(tables)
    sessions = tables.get("sessions", [])
    tables.setdefault("session_samples", [
        {"session_path": row["path"], "sample_id": sample}
        for row in sessions for sample in sorted(set(row.get("sample_ids") or []))])
    tables.setdefault("session_tags", [
        {"session_path": row["path"], "tag": tag}
        for row in sessions for tag in sorted(set(row.get("tags") or []))])
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        path.unlink()
    connection = sqlite3.connect(os.fspath(path), isolation_level=None)
    counts = {}
    try:
        connection.execute("BEGIN")
        for statement in INDEX_DDL:
            connection.execute(statement)
        connection.executemany("INSERT INTO meta (key, value) VALUES (?, ?)",
                               sorted(meta.items()))
        for table, columns in INDEX_TABLES.items():
            if table == "meta":
                continue
            rows = tables.get(table, [])
            connection.executemany(
                "INSERT INTO %s (%s) VALUES (%s)" % (table, ", ".join(columns),
                                                     ", ".join("?" for _ in columns)),
                [[_file_value(name, row.get(name)) for name in columns] for row in rows])
            counts[table] = len(rows)
        connection.execute("COMMIT")
    finally:
        connection.close()
    return counts


def build_demo_index(path, now=None):
    """Write the demo catalogue as an index file at `path` (the nightly index's shape).

    `now` is an aware datetime or a UTC text; the sessions lie in the 30 days before it. The
    same `now` always gives the same rows. Returns {table: number of rows}.
    """
    meta, tables = demo_tables(now)
    return write_index(path, meta, tables)


# ---------------------------------------------------------------------- loading and clearing


def is_loaded(db):
    """True while the catalogue holds the demo data."""
    return db.scalar("SELECT value FROM {pcl}.meta WHERE key = ?", ["demo"]) == "1"


def load_demo(db, settings, now=None):
    """Load the demo data, inside the caller's transaction. Returns what `load_index` returns.

    Refused (Conflict) when sessions that are not demo data are registered. Demo data that is
    already there is replaced.
    """
    real = db.scalar("SELECT COUNT(*) FROM {pcl}.sessions WHERE source <> ?", ["demo"])
    if real:
        raise Conflict("The database holds %d session(s) that are not demo data. Demo data and "
                       "real data never mix, so the demo was not loaded. Use an empty database "
                       "for the demo (%s db reset --yes removes everything)."
                       % (real, util.LAUNCHER),
                       code="real_data_present")
    clear_demo(db)
    with tempfile.TemporaryDirectory(prefix="pcldb-demo-", ignore_cleanup_errors=True) as folder:
        path = Path(folder) / "demo_sessions.sqlite"
        build_demo_index(path, now)
        result = reconcile.load_index(db, path, actor=DEMO_ACTOR, settings=settings, source="demo")
    db.upsert("pcl.meta", {"key": "demo", "value": "1"}, key=("key",))
    return result


def clear_demo(db):
    """Remove every demo row, inside the caller's transaction.

    Gone afterwards: the demo sessions and their files, every run, point, review, model,
    problem and roll-up row (while the demo is loaded they can only be demo rows), the demo's
    audit rows, and its Search entries. An entry that was never sent anywhere is deleted; one
    that may be in Globus Search is marked for removal there, and the next push removes it.
    Returns the counts; {"loaded": False, ...} when there was no demo data.
    """
    paths = [row["path"] for row in
             db.query("SELECT path FROM {pcl}.sessions WHERE source = ?", ["demo"])]
    counts = {"loaded": bool(paths) or is_loaded(db), "sessions": 0, "files": 0, "runs": 0,
              "points": 0, "entries_removed": 0, "entries_to_remove": 0}
    if not counts["loaded"]:
        return counts
    now = util.now_utc_text()
    for path in sorted(paths):
        counts["files"] += db.delete_where("pcl.files", "session_path = ?", [path])
        entry = db.one("SELECT subject, state, task_id, synced_utc FROM {app}.search_entries "
                       "WHERE session_path = ?", [path])
        if entry is None:
            continue
        if entry["state"] == "pending" and not entry["task_id"] and not entry["synced_utc"]:
            db.delete_where("app.search_entries", "subject = ?", [entry["subject"]])
            counts["entries_removed"] += 1
        elif entry["state"] != "delete_pending":
            db.execute("UPDATE {app}.search_entries SET state = ?, updated_utc = ?, "
                       "task_id = NULL, last_error = NULL WHERE subject = ?",
                       ["delete_pending", db.param("utc", now), entry["subject"]])
            counts["entries_to_remove"] += 1
    counts["sessions"] = db.delete_where("pcl.sessions", "source = ?", ["demo"])
    for table in reconcile.DERIVED_TABLES:
        removed = db.delete_where("pcl." + table, "1 = 1")
        if table in counts:
            counts[table] = removed
    db.delete_where("app.registrations", "source = ?", ["demo"])
    db.delete_where("pcl.meta", "key IN (?, ?, ?)",
                    ["current_model", "index_built_utc", "index_source"])
    db.upsert("pcl.meta", {"key": "demo", "value": "0"}, key=("key",))
    return counts


# ---------------------------------------------------------------------- the command line


def _say(text="", stream=None):
    """One ASCII line (Windows consoles): anything else is written as an escape."""
    stream = stream or sys.stdout
    stream.write(text.encode("ascii", "backslashreplace").decode("ascii") + "\n")


def cmd_demo(args, settings, *, out=None, now=None):
    """`pcldb demo load` and `pcldb demo clear`. Returns 0."""
    from .store import open_store
    action = getattr(args, "action", None)
    if action not in ("load", "clear"):
        raise PclError("Say what to do with the demo data: %s demo load, or %s demo clear."
                       % (util.LAUNCHER, util.LAUNCHER), code="usage")
    store = open_store(settings)
    if action == "load":
        with store.app() as db:
            result = load_demo(db, settings, now)
        _say("Loaded the demo data: %d sessions (all simulated, all tagged demo), %d files, "
             "%d runs, %d points, %d reviews."
             % (result["sessions"], result["files"], result["runs"], result["points"],
                result["reviews"]), out)
        _say("Search entries waiting to be sent: %d." % result["search"]["changed"], out)
        _say("None of it is real: the folders do not exist on ARC. Remove it with: "
             "%s demo clear" % util.LAUNCHER, out)
        return 0
    with store.app() as db:
        counts = clear_demo(db)
    if not counts["loaded"]:
        _say("There is no demo data in the database. Nothing was changed.", out)
        return 0
    _say("Removed the demo data: %d sessions, %d files, %d runs, %d points."
         % (counts["sessions"], counts["files"], counts["runs"], counts["points"]), out)
    if counts["entries_to_remove"]:
        _say("%d Search entries may be in Globus Search. They are marked for removal; send "
             "that with: %s search push" % (counts["entries_to_remove"], util.LAUNCHER), out)
    return 0
