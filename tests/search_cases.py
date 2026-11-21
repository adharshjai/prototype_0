"""Twenty made-up session entries and the search requests they must answer.

One list, three users: `test_search_local` runs every case on the stand-in (both database
engines), `test_search_facade` uses the entries behind a fake Globus Search, and the opt-in live
test runs the same cases against a real index. The expected subjects were worked out by hand
from the table below, not by running the stand-in.

Every case says how sure the expectation is for the REAL service:

- "observed": the behaviour was seen on the live service on 2026-11-20 (research report E3-E7).
- "assumed":  the stand-in follows the documentation or a likely rule that nobody has seen on
              the service yet (how words are cut, order by relevance, facets on true/false ...).
              A live run that disagrees here has found a real difference: write it into the
              list in `pcldb/search_local.py`.

All names here are invented; nobody's real data is in this file.
"""

from __future__ import annotations

import copy
import json
import uuid
from urllib.parse import urlencode

from pcldb import util

COLLECTION = "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec"   # the public ARC Projects collection
FORMAT = "pcldb-contract-test folder"                  # the same in every entry: the live test
#                                                        filters on it to see only its own entries
RAMAN, JEOL, UVVIS = "raman-portable", "jeol-jsm-it800-shl", "uv-vis-lambda"


def _content(instrument, folder, started=None, *, simulated=False, session_id=None, **more):
    path = "raw/%s/%s%s" % (instrument, "simulated/" if simulated else "", folder)
    data_path = "/pcl_photonics/" + path
    content = {
        "schema": "pcl-search/1",
        "title": folder,
        "instrument": instrument,
        "folder": folder,
        "session_id": session_id or "sess-" + folder[-3:],
        "path": path,
        "collection_id": COLLECTION,
        "data_path": data_path,
        "globus_url": "globus://%s%s/" % (COLLECTION, data_path),
        "web_url": "https://app.globus.org/file-manager?" + urlencode(
            {"origin_id": COLLECTION, "origin_path": data_path + "/"}),
        "format": FORMAT,
        "review_state": "none",
        "simulated": simulated,
        "is_copy": False,
        "withdrawn": False,
        "corrected": False,
        "n_needs_review": 0,
    }
    if started:
        content["started"] = started
    labels = more.pop("label_counts", None)
    if labels:
        content["labels"] = [label for label, _count in labels]
        content["label_counts"] = [{"label": label, "count": count} for label, count in labels]
    content.update(more)
    return content


def _table():
    v3, v4 = "raman_cnn_v3", "raman_cnn_v4"
    scan, grid = "1d_y_scan", "2d_map"
    return {
        "e01": _content(RAMAN, "1d_y_scan_20261001_090000_001", "2026-10-01T09:00:00Z",
                        method=scan, completion="complete", sample_ids=["S-001"],
                        tags=["calibration"], label_counts=[("adenine", 5), ("r6g", 3)],
                        models=[v3], items=8, items_planned=8, review_state="done",
                        location="Lab 214 bench A", uploaded="2026-10-01T09:30:00Z",
                        verified="2026-10-02T02:00:00Z", last_run="2026-10-01T10:00:00Z",
                        files=12, bytes=120000),
        "e02": _content(RAMAN, "1d_y_scan_20261003_101500_002", "2026-10-03T10:15:00Z",
                        method=scan, completion="complete", sample_ids=["S-002"],
                        label_counts=[("adenine", 8)], models=[v3], items=8, items_planned=8,
                        review_state="pending", n_needs_review=2,
                        uploaded="2026-10-03T10:40:00Z", last_run="2026-10-03T11:00:00Z",
                        files=12, bytes=118000),
        "e03": _content(RAMAN, "2d_map_20261007_140000_003", "2026-10-07T14:00:00Z",
                        method=grid, completion="partial", sample_ids=["S-002", "S-003"],
                        tags=["mapping", "Priority"],
                        label_counts=[("methylene_blue", 4), ("UNKNOWN", 2)], models=[v3],
                        items=6, items_planned=16, review_state="pending", n_needs_review=2,
                        problems=["sums_missing"], uploaded="2026-10-07T15:00:00Z",
                        last_run="2026-10-07T15:30:00Z", files=9, bytes=90000),
        "e04": _content(RAMAN, "single_point_20261010_083000_004", "2026-10-10T08:30:00Z",
                        method="single_point", completion="complete", sample_ids=["S-004"],
                        items=1, items_planned=1, corrected=True,
                        uploaded="2026-10-10T08:45:00Z", verified="2026-10-11T02:00:00Z",
                        files=4, bytes=15000),
        "e05": _content(RAMAN, "1d_y_scan_20261015_120000_005", "2026-10-15T12:00:00Z",
                        simulated=True, method=scan, completion="complete",
                        sample_ids=["SIM-A"], tags=["demo"], label_counts=[("r6g", 10)],
                        models=[v3], items=10, items_planned=10,
                        last_run="2026-10-15T12:30:00Z", files=14, bytes=150000),
        "e06": _content(RAMAN, "2d_map_20261020_160000_006", "2026-10-20T16:00:00Z",
                        simulated=True, method=grid, completion="failed", sample_ids=["SIM-A"],
                        tags=["demo", "mapping"], items=0, items_planned=25,
                        problems=["card_missing_field"], files=3, bytes=2000),
        "e07": _content(RAMAN, "1d_y_scan_20261101_091500_007", "2026-11-01T09:15:00Z",
                        method=scan, completion="complete", sample_ids=["S-005"],
                        tags=["calibration"],
                        label_counts=[("adenine", 2), ("methylene_blue", 6)], models=[v3],
                        items=8, items_planned=8, review_state="done",
                        uploaded="2026-11-01T09:40:00Z", verified="2026-11-02T02:00:00Z",
                        last_run="2026-11-01T10:00:00Z", files=12, bytes=121000),
        "e08": _content(RAMAN, "1d_y_scan_20261101_091500_007_copy2", "2026-11-01T09:15:00Z",
                        session_id="sess-007", method=scan, completion="complete",
                        sample_ids=["S-005"], tags=["calibration"],
                        label_counts=[("adenine", 2), ("methylene_blue", 6)], models=[v3],
                        items=8, items_planned=8, review_state="done", is_copy=True,
                        uploaded="2026-11-01T09:40:00Z", verified="2026-11-02T02:00:00Z",
                        last_run="2026-11-01T10:00:00Z", files=12, bytes=121000),
        "e09": _content(RAMAN, "1d_y_scan_20261105_110000_009", "2026-11-05T11:00:00Z",
                        method=scan, completion="complete", sample_ids=["S-006"],
                        label_counts=[("adenine", 4)], models=[v3], items=4, items_planned=4,
                        withdrawn=True, last_run="2026-11-05T11:30:00Z", files=8, bytes=60000),
        "e10": _content(RAMAN, "2d_map_20261110_133000_010", "2026-11-10T13:30:00Z",
                        method=grid, completion="complete", sample_ids=["S-006", "S-007"],
                        tags=["mapping"], label_counts=[("r6g", 12), ("UNKNOWN", 4)],
                        models=[v3], items=16, items_planned=16, review_state="pending",
                        n_needs_review=4, uploaded="2026-11-10T14:00:00Z",
                        last_run="2026-11-10T14:30:00Z", files=20, bytes=300000),
        "e11": _content(JEOL, "sem_20261002_100000_011", "2026-10-02T10:00:00Z",
                        method="sem_image", completion="complete", sample_ids=["W-10"],
                        tags=["wafer"], items=12, items_planned=12, location="Cleanroom SEM",
                        uploaded="2026-10-02T11:00:00Z", verified="2026-10-03T02:00:00Z",
                        files=13, bytes=52000000),
        "e12": _content(JEOL, "sem_20261012_150000_012", "2026-10-12T15:00:00Z",
                        method="sem_image", completion="complete", sample_ids=["W-10", "W-11"],
                        tags=["wafer", "cross-section"], items=30, items_planned=30,
                        uploaded="2026-10-12T16:00:00Z", files=31, bytes=130000000),
        "e13": _content(JEOL, "sem_20261025_093000_013", "2026-10-25T09:30:00Z",
                        simulated=True, method="sem_image", completion="complete",
                        sample_ids=["SIM-B"], tags=["demo"], items=5, items_planned=5,
                        files=6, bytes=800000),
        "e14": _content(JEOL, "eds_20261108_141500_014", "2026-11-08T14:15:00Z",
                        method="eds_map", completion="partial", sample_ids=["W-12"],
                        tags=["wafer"], items=3, items_planned=9,
                        uploaded="2026-11-08T15:00:00Z", files=5, bytes=9000000),
        "e15": _content(JEOL, "sem_20261115_100000_015", "2026-11-15T10:00:00Z",
                        method="sem_image", completion="complete", sample_ids=["W-12"],
                        items=20, items_planned=20, uploaded="2026-11-15T11:00:00Z",
                        verified="2026-11-16T02:00:00Z", files=21, bytes=88000000),
        "e16": _content(UVVIS, "absorbance_20261018_110000_016", "2026-10-18T11:00:00Z",
                        method="absorbance", completion="complete", sample_ids=["S-001"],
                        tags=["calibration"], items=2, items_planned=2,
                        uploaded="2026-10-18T11:20:00Z", files=3, bytes=40000),
        "e17": _content(UVVIS, "absorbance_20261112_120000_017", "2026-11-12T12:00:00Z",
                        method="absorbance", completion="complete", sample_ids=["S-007"],
                        items=2, items_planned=2, files=3, bytes=41000),
        "e18": _content(RAMAN, "1d_y_scan_20261118_170000_018", "2026-11-18T17:00:00Z",
                        method=scan, completion="complete", sample_ids=["S-008"],
                        tags=["Priority"],
                        label_counts=[("adenine", 3), ("r6g", 3), ("methylene_blue", 2)],
                        models=[v3, v4], items=8, items_planned=8, review_state="pending",
                        n_needs_review=1, uploaded="2026-11-18T17:30:00Z",
                        last_run="2026-11-18T18:00:00Z", files=12, bytes=119000),
        # No date, no method, no completion, no samples, no tags, no items: the optional
        # fields are simply absent.
        "e19": _content(RAMAN, "undated_session_019", files=2, bytes=500),
        "e20": _content(RAMAN, "1d_y_scan_20261120_080000_020", "2026-11-20T08:00:00Z",
                        simulated=True, method=scan, completion="complete",
                        sample_ids=["SIM-A"], tags=["demo"], label_counts=[("adenine", 6)],
                        models=[v4], items=6, items_planned=6, review_state="done",
                        last_run="2026-11-20T08:30:00Z", files=10, bytes=95000),
    }


def subject_of(content):
    return "urn:pcl:dataset:" + str(uuid.uuid5(
        uuid.NAMESPACE_URL, content["collection_id"] + ":" + content["data_path"]))


def make_entries():
    """{key: {"subject", "session_path", "content", "visible_to"}} for e01 .. e20 (fresh copies)."""
    return {key: {"subject": subject_of(content), "session_path": content["path"],
                  "content": content, "visible_to": []}
            for key, content in copy.deepcopy(_table()).items()}


ALL = ["e%02d" % number for number in range(1, 21)]
SUBJECTS = {key: entry["subject"] for key, entry in make_entries().items()}
KEY_OF = {subject: key for key, subject in SUBJECTS.items()}


def entry_row(entry, state="pending", **more):
    """One row of app.search_entries for an entry of make_entries()."""
    content = entry["content"]
    row = {
        "subject": entry["subject"],
        "session_path": entry["session_path"],
        "content_json": content,
        "visible_to": list(entry.get("visible_to") or []),
        "content_sha256": util.sha256_hex(util.canonical_json(content).encode("utf-8")),
        "state": state,
        "task_id": None,
        "last_error": None,
        "updated_utc": "2026-11-20T12:00:00.000Z",
        "synced_utc": None,
    }
    row.update(more)
    return row


def insert_entries(db, entries=None, state="pending"):
    """Put entries (default: all twenty) into app.search_entries. Returns {key: subject}."""
    entries = make_entries() if entries is None else entries
    for key in sorted(entries):
        db.insert("app.search_entries", entry_row(entries[key], state))
    return {key: entry["subject"] for key, entry in entries.items()}


def insert_leaving_entry(db):
    """One more entry in the state delete_pending: no search may ever return it."""
    content = _content(RAMAN, "1d_y_scan_20260901_090000_099", "2026-09-01T09:00:00Z",
                       method="1d_y_scan", completion="complete", tags=["calibration"],
                       items=8, files=1, bytes=1)
    entry = {"subject": subject_of(content), "session_path": content["path"],
             "content": content, "visible_to": []}
    db.insert("app.search_entries", entry_row(entry, "delete_pending"))
    return entry["subject"]


# ---------------------------------------------------------------------- small request builders


def any_of(field, *values):
    return {"type": "match_any", "field_name": field, "values": list(values)}


def all_of(field, *values):
    return {"type": "match_all", "field_name": field, "values": list(values)}


def between(field, low, high):
    return {"type": "range", "field_name": field, "values": [{"from": low, "to": high}]}


def exists(field):
    return {"type": "exists", "field_name": field}


def no(one):
    return {"type": "not", "filter": one}


def terms(field, **more):
    facet = {"type": "terms", "field_name": field}
    facet.update(more)
    return facet


def by(field, order=None):
    return {"field_name": field, "order": order} if order else {"field_name": field}


def _keys(text):
    return ["e" + part for part in text.split()]


def case(name, request, subjects, *, ordered=False, live="observed", total=None,
         has_next_page=None, facets=None):
    """One case. `subjects` are entry keys ("01 07"); a tuple inside an ordered list means
    "these, in the order of their subjects" (entries that are equal in every sort field)."""
    if isinstance(subjects, str):
        subjects = _keys(subjects)
    request = dict(request)
    request.setdefault("limit", 100)
    return {"name": name, "request": request, "subjects": subjects, "ordered": ordered,
            "live": live, "total": total, "has_next_page": has_next_page, "facets": facets}


def expected_subjects(one, subjects=None):
    """The subjects a case expects, in order (tie groups resolved by subject)."""
    subjects = subjects or SUBJECTS
    out = []
    for item in one["subjects"]:
        if isinstance(item, tuple):
            out.extend(sorted(subjects["e" + part] for part in item))
        else:
            out.append(subjects[item])
    return out


EVERYTHING = {"q": "*"}
RAMAN_KEYS = "01 02 03 04 05 06 07 08 09 10 18 19 20"
NOT_SIMULATED = "01 02 03 04 07 08 09 10 11 12 14 15 16 17 18 19"
CALIBRATION = "01 07 08 16"
ADENINE = "01 02 07 08 09 18 20"

# started, oldest first; e07 and e08 started in the same second; e19 has no date (always last)
OLDEST_FIRST = ["e01", "e11", "e02", "e03", "e04", "e12", "e05", "e16", "e06", "e13",
                ("07", "08"), "e09", "e14", "e10", "e17", "e15", "e18", "e20", "e19"]
NEWEST_FIRST = ["e20", "e18", "e15", "e17", "e10", "e14", "e09", ("07", "08"), "e13", "e06",
                "e16", "e05", "e12", "e04", "e03", "e02", "e11", "e01", "e19"]

CASES = [
    # ---- everything, and exact filters
    case("everything", EVERYTHING, ALL),
    case("filters without q", {"filters": [any_of("withdrawn", True)]}, "09"),
    case("match_any one value", {"filters": [any_of("instrument", RAMAN)]}, RAMAN_KEYS),
    case("match_any two values", {"filters": [any_of("instrument", JEOL, UVVIS)]},
         "11 12 13 14 15 16 17"),
    case("match_any is case-sensitive", {"filters": [any_of("tags", "priority")]}, ""),
    case("match_any with the stored case", {"filters": [any_of("tags", "Priority")]}, "03 18"),
    case("match_any compares the whole value", {"filters": [any_of("instrument", "raman")]}, ""),
    case("match_any: one word of a longer value", {"filters": [any_of("location", "Lab")]}, ""),
    case("match_any: the complete value", {"filters": [any_of("location", "Lab 214 bench A")]},
         "01"),
    case("match_any on a list field: any element",
         {"filters": [any_of("labels", "adenine", "UNKNOWN")]}, "01 02 03 07 08 09 10 18 20"),
    case("match_any on sample ids", {"filters": [any_of("sample_ids", "S-002", "W-12")]},
         "02 03 14 15"),
    case("match_all needs every value", {"filters": [all_of("labels", "adenine", "r6g")]},
         "01 18"),
    case("match_all with a value nobody has",
         {"filters": [all_of("labels", "adenine", "no_such_label")]}, ""),
    case("two top-level filters are AND-ed",
         {"filters": [any_of("instrument", RAMAN), any_of("instrument", JEOL)]}, ""),
    case("an or filter gives the union",
         {"filters": [{"type": "or", "filters": [any_of("instrument", RAMAN),
                                                 any_of("instrument", JEOL)]}]},
         RAMAN_KEYS + " 11 12 13 14 15"),
    case("true/false field: true", {"filters": [any_of("simulated", True)]}, "05 06 13 20"),
    case("true/false field: false", {"filters": [any_of("simulated", False)]}, NOT_SIMULATED),
    case("true/false field: both values", {"filters": [any_of("simulated", True, False)]}, ALL),
    case("not", {"filters": [no(any_of("simulated", True))]}, NOT_SIMULATED),
    case("and with not inside",
         {"filters": [{"type": "and", "filters": [
             any_of("instrument", RAMAN), no(any_of("withdrawn", True)),
             no(any_of("is_copy", True)), any_of("simulated", False)]}]},
         "01 02 03 04 07 10 18 19"),
    case("the three filters every question gets",
         {"q": "*", "filters": [any_of("withdrawn", False), any_of("is_copy", False),
                                any_of("simulated", False)]},
         "01 02 03 04 07 10 11 12 14 15 16 17 18 19"),
    case("exists", {"filters": [exists("started")]},
         "01 02 03 04 05 06 07 08 09 10 11 12 13 14 15 16 17 18 20"),
    case("not exists", {"filters": [no(exists("started"))]}, "19"),
    case("exists on a list field", {"filters": [exists("tags")]},
         "01 03 05 06 07 08 10 11 12 13 14 16 18 20"),
    case("exists on the list of objects", {"filters": [exists("label_counts")]},
         "01 02 03 05 07 08 09 10 18 20", live="assumed"),
    case("exists and not exists together",
         {"filters": [exists("uploaded"), no(exists("verified"))]}, "02 03 10 12 14 16 18"),
    case("problems exist", {"filters": [exists("problems")]}, "03 06"),

    # ---- ranges
    case("range: from and to are both inclusive", {"filters": [between("items", 8, 8)]},
         "01 02 07 08 18"),
    case("range with an open upper end", {"filters": [between("items", 10, "*")]},
         "05 10 11 12 15"),
    case("range with an open lower end", {"filters": [between("items", "*", 2)]},
         "04 06 16 17"),
    case("range gt/lt are exclusive",
         {"filters": [{"type": "range", "field_name": "items",
                       "values": [{"gt": 6, "lt": 10}]}]}, "01 02 07 08 18"),
    case("range gte/lte are inclusive",
         {"filters": [{"type": "range", "field_name": "items",
                       "values": [{"gte": 6, "lte": 10}]}]},
         "01 02 03 05 07 08 18 20"),
    case("several range objects are OR-ed",
         {"filters": [{"type": "range", "field_name": "items",
                       "values": [{"from": 0, "to": 1}, {"from": 20, "to": 30}]}]},
         "04 06 12 15"),
    case("range on a date: November",
         {"filters": [between("started", "2026-11-01T00:00:00Z", "2026-11-30T23:59:59Z")]},
         "07 08 09 10 14 15 17 18 20"),
    case("range on a date: both ends count",
         {"filters": [between("started", "2026-10-01T09:00:00Z", "2026-10-03T10:15:00Z")]},
         "01 02 11"),
    case("range on a date: gt/lt leave the ends out",
         {"filters": [{"type": "range", "field_name": "started",
                       "values": [{"gt": "2026-10-01T09:00:00Z",
                                   "lt": "2026-10-03T10:15:00Z"}]}]}, "11"),
    case("range and exact filter together",
         {"filters": [any_of("review_state", "pending"), between("n_needs_review", 2, "*")]},
         "02 03 10"),
    case("exact match on a date", {"filters": [any_of("started", "2026-10-10T08:30:00Z")]},
         "04"),

    # ---- the list of objects
    case("a member of the list of objects", {"filters": [any_of("label_counts.label", "UNKNOWN")]},
         "03 10"),
    case("range on a member of the list of objects",
         {"filters": [between("label_counts.count", 10, "*")]}, "05 10"),
    case("two conditions need not hit the same object",
         {"filters": [any_of("label_counts.label", "adenine"),
                      between("label_counts.count", 6, "*")]}, "02 07 08 20"),

    # ---- words
    case("one word", {"q": "calibration"}, CALIBRATION),
    case("words ignore case", {"q": "CALIBRATION"}, CALIBRATION),
    case("a stored capital is found in lower case", {"q": "priority"}, "03 18"),
    case("two words are OR-ed", {"q": "wafer mapping"}, "03 06 10 11 12 14"),
    case("two words with operator or",
         {"q": "wafer mapping",
          "q_settings": {"mode": "query_string", "default_operator": "or"}},
         "03 06 10 11 12 14"),
    case("two words with operator and: nobody has both",
         {"q": "wafer mapping",
          "q_settings": {"mode": "query_string", "default_operator": "and"}}, ""),
    case("operator and: both words in one field",
         {"q": "lab bench", "q_settings": {"mode": "query_string", "default_operator": "and"}},
         "01"),
    case("operator and: the words in two fields",
         {"q": "adenine calibration",
          "q_settings": {"mode": "query_string", "default_operator": "and"}},
         "01 07 08", live="assumed"),
    case("a trailing star is a prefix", {"q": "calib*"}, CALIBRATION),
    case("a prefix finds the start of long words", {"q": "sem*"}, "11 12 13 15", live="assumed"),
    case("without the star only the whole word counts", {"q": "sem"}, "11", live="assumed"),
    case("a quoted phrase", {"q": "\"bench a\""}, "01"),
    case("a quoted phrase keeps the order", {"q": "\"a bench\""}, ""),
    case("a quoted phrase with a number", {"q": "\"lab 214\""}, "01"),
    case("field:value is not understood", {"q": "tags:wafer"}, ""),
    case("words inside nested values are found", {"q": "unknown"}, "03 10"),
    case("words of the path are found", {"q": "simulated"}, "05 06 13 20"),
    case("a hyphen separates words", {"q": "cross"}, "12", live="assumed"),
    case("a hyphenated word finds its parts", {"q": "raman"}, RAMAN_KEYS, live="assumed"),
    case("an underscore does not separate words", {"q": "methylene_blue"}, "03 07 08 18",
         live="assumed"),
    case("half of an underscore word is not found", {"q": "methylene"}, "", live="assumed"),
    case("the word scan is glued into the folder names", {"q": "scan"}, "", live="assumed"),
    case("prefix over an underscore word", {"q": "1d_y*"}, "01 02 05 07 08 09 18 20",
         live="assumed"),
    case("a quote without its partner is ignored", {"q": "\"calibration"}, CALIBRATION,
         live="assumed"),
    case("a word nobody has", {"q": "zzzznotthere"}, "", total=0, has_next_page=False),
    case("words and a filter", {"q": "calibration", "filters": [any_of("instrument", RAMAN)]},
         "01 07 08"),
    case("star with a filter", {"q": "*", "filters": [any_of("instrument", UVVIS)]}, "16 17"),
    case("more matched words come first", {"q": "wafer cross"}, ["e12", "e14", "e11"],
         ordered=True, live="assumed"),

    # ---- limit, offset, sort
    case("limit 0 gives counts only", {"q": "*", "limit": 0}, "", total=20, has_next_page=True),
    case("the first page", {"q": "*", "sort": [by("started", "asc")], "limit": 5},
         ["e01", "e11", "e02", "e03", "e04"], ordered=True, total=20, has_next_page=True),
    case("the last page", {"q": "*", "sort": [by("started", "asc")], "limit": 5, "offset": 15},
         ["e17", "e15", "e18", "e20", "e19"], ordered=True, total=20, has_next_page=False),
    case("a short last page", {"q": "*", "sort": [by("started", "asc")], "limit": 5,
                               "offset": 18},
         ["e20", "e19"], ordered=True, total=20, has_next_page=False),
    case("an offset past the end", {"q": "*", "limit": 5, "offset": 30}, "", total=20,
         has_next_page=False),
    case("sort ascending, entries without the field last",
         {"q": "*", "sort": [by("started", "asc")]}, OLDEST_FIRST, ordered=True,
         live="assumed"),
    case("sort descending, entries without the field still last",
         {"q": "*", "sort": [by("started", "desc")]}, NEWEST_FIRST, ordered=True,
         live="assumed"),
    case("sort without order is descending", {"q": "*", "sort": [by("items")], "limit": 3},
         ["e12", "e15", "e10"], ordered=True, total=20, has_next_page=True, live="assumed"),
    case("sort by two fields",
         {"q": "*", "sort": [by("items", "asc"), by("started", "asc")], "limit": 4},
         ["e06", "e04", "e16", "e17"], ordered=True, total=20, has_next_page=True,
         live="assumed"),
    case("sort by text, then by date",
         {"q": "*", "sort": [by("instrument", "asc"), by("started", "desc")], "limit": 6},
         ["e15", "e14", "e13", "e12", "e11", "e20"], ordered=True, total=20,
         has_next_page=True, live="assumed"),
    case("sort by a list field uses its smallest value",
         {"filters": [exists("tags")], "sort": [by("tags", "asc"), by("started", "asc")]},
         ["e03", "e18", "e01", "e16", ("07", "08"), "e12", "e05", "e06", "e13", "e20", "e10",
          "e11", "e14"], ordered=True, live="assumed"),
    case("sort descending by a list field uses its largest value",
         {"filters": [exists("tags")], "sort": [by("tags", "desc"), by("started", "asc")],
          "limit": 5},
         ["e11", "e12", "e14", "e03", "e06"], ordered=True, total=14, has_next_page=True,
         live="assumed"),
    case("sort by true/false",
         {"q": "*", "sort": [by("simulated", "desc"), by("started", "asc")], "limit": 5},
         ["e05", "e06", "e13", "e20", "e01"], ordered=True, total=20, has_next_page=True,
         live="assumed"),
    case("sort by a number, then by date",
         {"q": "*", "sort": [by("n_needs_review", "desc"), by("started", "asc")], "limit": 4},
         ["e10", "e02", "e03", "e18"], ordered=True, total=20, has_next_page=True),

    # ---- facets
    case("terms facet, most frequent first",
         {"q": "*", "limit": 0, "facets": [terms("instrument")]}, "", total=20, has_next_page=True,
         facets={"instrument": [(RAMAN, 13), (JEOL, 5), (UVVIS, 2)]}),
    case("facets count what the filters left",
         {"q": "*", "limit": 0, "filters": [any_of("simulated", False)],
          "facets": [terms("instrument")]}, "", total=16, has_next_page=True,
         facets={"instrument": [(RAMAN, 10), (JEOL, 4), (UVVIS, 2)]}),
    case("facet on a list field; equal counts by value",
         {"q": "*", "limit": 0, "facets": [terms("labels")]}, "", total=20, has_next_page=True,
         facets={"labels": [("adenine", 7), ("methylene_blue", 4), ("r6g", 4), ("UNKNOWN", 2)]}),
    case("facet size cuts the list",
         {"q": "*", "limit": 0, "facets": [terms("labels", size=2)]}, "", total=20,
         has_next_page=True, facets={"labels": [("adenine", 7), ("methylene_blue", 4)]}),
    case("two facets, one with its own name",
         {"q": "*", "limit": 0,
          "facets": [terms("method", name="by_method"), terms("completion")]},
         "", total=20, has_next_page=True,
         facets={"by_method": [("1d_y_scan", 8), ("sem_image", 4), ("2d_map", 3),
                               ("absorbance", 2), ("eds_map", 1), ("single_point", 1)],
                 "completion": [("complete", 16), ("partial", 2), ("failed", 1)]}),
    case("facets count what q left", {"q": "wafer", "facets": [terms("tags")]}, "11 12 14",
         facets={"tags": [("wafer", 3), ("cross-section", 1)]}),
    case("a facet with nothing to count",
         {"filters": [any_of("instrument", UVVIS)], "facets": [terms("labels")]}, "16 17",
         facets={"labels": []}),
    case("facet with equal counts everywhere", {"q": "*", "limit": 0, "facets": [terms("tags")]},
         "", total=20, has_next_page=True,
         facets={"tags": [("calibration", 4), ("demo", 4), ("mapping", 3), ("wafer", 3),
                          ("Priority", 2), ("cross-section", 1)]}),
    case("facet on a true/false field",
         {"q": "*", "limit": 0, "facets": [terms("simulated")]}, "", total=20,
         has_next_page=True, facets={"simulated": [("false", 16), ("true", 4)]}),
    case("facet on a member of the list of objects",
         {"q": "*", "limit": 0, "facets": [terms("label_counts.label", name="labels")]}, "",
         total=20, has_next_page=True,
         facets={"labels": [("adenine", 7), ("methylene_blue", 4), ("r6g", 4), ("UNKNOWN", 2)]}),
]


def check_result(test, one, result, subjects=None):
    """Assert that `result` (a Globus-shaped search result) is what the case `one` expects."""
    name = one["name"]
    wanted = expected_subjects(one, subjects)
    got = [item["subject"] for item in result["gmeta"]]
    if one["ordered"]:
        test.assertEqual(got, wanted, name)
    else:
        test.assertEqual(sorted(got), sorted(wanted), name)
    test.assertEqual(result["count"], len(got), name)
    total = one["total"] if one["total"] is not None else len(wanted)
    test.assertEqual(result["total"], total, name)
    test.assertEqual(result["offset"], one["request"].get("offset", 0), name)
    if one["has_next_page"] is not None:
        test.assertEqual(result["has_next_page"], one["has_next_page"], name)
    if one["facets"] is None:
        test.assertNotIn("facet_results", result, name)
    else:
        found = {facet["name"]: [(bucket["value"], bucket["count"]) for bucket in facet["buckets"]]
                 for facet in result["facet_results"]}
        test.assertEqual(found, one["facets"], name)


def describe(one):
    """One line for a failure message: the case and its request."""
    return "%s: %s" % (one["name"], json.dumps(one["request"], sort_keys=True))
