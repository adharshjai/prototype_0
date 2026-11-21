"""The stand-in for Globus Search: the same requests, answered on this computer.

`search(db, request)` evaluates a checked request (`search.validate_request`) over the entries
queued in `app.search_entries` (every state but `delete_pending`) and returns a result in the
service's own shape:

    {"@datatype": "GSearchResult", "@version": "2017-09-01",
     "total": 11, "count": 2, "offset": 0, "has_next_page": true,
     "gmeta": [{"@datatype": "GMetaResult", "@version": "2017-09-01", "subject": "...",
                "entries": [{"entry_id": null, "matched_principal_sets": [], "content": {...}}]}],
     "facet_results": [{"@datatype": "GFacetResult", "@version": "2017-09-01", "name": "...",
                        "buckets": [{"@datatype": "GBucket", "@version": "2017-09-01",
                                     "value": "...", "count": 4}]}]}

`facet_results` is there only when the request asked for facets.

What is copied from the service as it behaved on 2026-11-20 (the things that surprise):

- `field_name` is relative to `content`; a dot goes into a nested object or list of objects.
- The filters of the top-level list are AND-ed. `match_any` and `match_all` compare whole
  values, exactly and case-sensitively; on a list field any element may match. `range` is
  inclusive with `from`/`to`, `gte`, `lte` and exclusive with `gt`, `lt`; `"*"` is an open end;
  several range objects in one filter are OR-ed. `exists` means: there with a value.
- Simple `q` is case-insensitive and matches whole words anywhere in the content's text (nested
  too). Several words are OR-ed unless `q_settings.default_operator` is `and`. A trailing `*`
  matches every word that starts so; a double-quoted phrase matches the words in that order;
  `*` alone matches everything; `field:value` is NOT understood (it is read as one odd word).
- `limit` 0 gives counts and facets only. `has_next_page` is true while `offset + count` is
  below `total` (so also with `limit` 0).
- `sort`: entries without the field come last in both directions; a list field sorts by its
  smallest value ascending and its largest descending.
- `terms` facets count each entry once per value, most frequent first (equal counts: smaller
  value first), cut at `size`, and they count the entries the filters and `q` left. A bucket
  value is a JSON number on a whole-number field and the text `"true"` / `"false"` on a
  true/false field.
- Lists of objects (`label_counts.label`, `label_counts.count`) are flattened: a condition
  matches when any element fits, and two conditions need not fit the same element.
- `match_any` on a date field with a full date text matches that moment.

KNOWN DIFFERENCES from the real service (keep this list true; `tests/search_cases.py` marks the
cases that depend on an unobserved point):

 1. Who may see an entry. The service returns only entries whose `visible_to` fits the signed-in
    Globus identity. The stand-in returns every entry to everyone who may search in pcldb.
 2. When an entry can be found. Here: as soon as it is queued (also `pending` and `failed`
    entries). On the service: after its ingest task succeeded.
 3. How text is cut into words is NOT documented by Globus. The stand-in assumes the usual
    Elasticsearch standard analysis: lower-case; letters, digits and the underscore make a word;
    a dot, apostrophe or colon between two letters and a dot or comma between two digits stay
    inside the word; everything else separates. So `1d_y_scan_20261116` is ONE word (find it
    with `1d_y*`), `methylene_blue` is one word, `raman-portable` is two. Only the colon and the
    case rule were observed on the service.
 4. `q` with `default_operator` `and`: here every word must be somewhere in the entry (any
    field). The service may want them all in one field; that was not determined.
 5. `q` looks at every JSON text value of the content, dates and addresses included, and never
    at numbers or true/false. On the service date fields are stored as dates, so a word such as
    `2026` may match differently there, and a bare number as `q` did match records of one
    public index (through which fields is not known).
 6. Other marks of the service's query syntax (`+`, `-`, `|`, a `*` that is not at the end of a
    word, `?`, `~`) are plain separators here. A quote without its partner is ignored.
 7. Order without `sort`: the service orders by its own relevance score. Here: more matched
    words first, then `started` newest first (entries without it last), then the subject. With
    `sort`, entries that are equal in every sort field keep that same order.
 8. `sort` on a field that no entry of the index has ever had: the service answers 400; the
    stand-in treats every entry as "without the field".
 9. The request checker is stricter than the service, for both backends: an unknown field
    (service: zero results, no error), a wrongly typed value (service: zero results on text
    fields, HTTP 400 on number, true/false and date fields), a number or a true/false written
    as text (`"8"`, `"true"`: the service takes them), a date not written
    `YYYY-MM-DDTHH:MM:SSZ` (the service takes several forms), `range` on a text field (the
    service compares by letters), `limit` above 100.
10. Not accepted at all: `like` (on the service: the whole value, case-sensitive, `*` and `?`),
    the geo filters, `advanced`, `boosts`, `fields`, `post_facet_filters`,
    `filter_principal_sets`, `bypass_visible_to`, and every facet type but `terms`.
11. A text value longer than 10,000 characters is left out of facets and sorts last on the
    service; the stand-in does not copy that.
12. No rate limit, no 30-second limit, no 10 MB limit. Problems are `PclError`s, not the
    service's `GError` documents.
13. The `@version` texts inside the result are the ones the live service sent (`2017-09-01`);
    its documentation shows another one for `GMetaResult`. Do not branch on them.
"""

from __future__ import annotations

import copy
import functools

from . import search as _search

RESULT_VERSION = "2017-09-01"

_MID_LETTER = ".':\u2019\u00b7"      # stay inside a word between two letters
_MID_NUMBER = ".,';\u2019"           # stay inside a word between two digits


# ---------------------------------------------------------------------- words


def words(text):
    """The lower-case words of a text, cut the way the stand-in assumes the service cuts them."""
    return list(_words(text))


@functools.lru_cache(maxsize=50000)
def _words(text):
    # Kept between searches: the same texts (instrument names, paths, tags) come back with
    # every entry and every question.
    text = text.lower()
    found = []
    current = []
    size = len(text)
    for position, char in enumerate(text):
        if char.isalnum() or char == "_":
            current.append(char)
            continue
        if current and position + 1 < size:
            before, after = current[-1], text[position + 1]
            if ((char in _MID_LETTER and before.isalpha() and after.isalpha())
                    or (char in _MID_NUMBER and before.isdigit() and after.isdigit())):
                current.append(char)
                continue
        if current:
            found.append("".join(current))
            current = []
    if current:
        found.append("".join(current))
    return tuple(word for word in found if word.strip("_"))


def parse_q(q):
    """The terms of a simple query: ("all",), ("word", w), ("prefix", start), ("phrase", (w, ..)).

    Never fails: what it cannot read is treated as plain text, as the service's simple syntax
    does. The same term twice counts once.
    """
    pieces = []           # (text, quoted)
    current = []
    position = 0
    while position < len(q):
        char = q[position]
        if char == '"':
            end = q.find('"', position + 1)
            if end != -1:
                if current:
                    pieces.append(("".join(current), False))
                    current = []
                pieces.append((q[position + 1:end], True))
                position = end + 1
                continue
            char = " "          # a quote without its partner
        if char.isspace():
            if current:
                pieces.append(("".join(current), False))
                current = []
        else:
            current.append(char)
        position += 1
    if current:
        pieces.append(("".join(current), False))

    terms = []

    def add(term):
        if term not in terms:
            terms.append(term)

    for text, quoted in pieces:
        if quoted:
            parts = words(text)
            if len(parts) == 1:
                add(("word", parts[0]))
            elif parts:
                add(("phrase", tuple(parts)))
        elif text.strip("*") == "":
            add(("all",))
        elif text.endswith("*"):
            parts = words(text.rstrip("*"))
            for part in parts[:-1]:
                add(("word", part))
            if parts:
                add(("prefix", parts[-1]))
        else:
            for part in words(text):
                add(("word", part))
    return terms


def _texts(value, into):
    """Every JSON text value inside `value` (keys are not looked at)."""
    if isinstance(value, str):
        into.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _texts(item, into)
    elif isinstance(value, list):
        for item in value:
            _texts(item, into)
    return into


def _term_matches(term, word_lists, word_set):
    kind = term[0]
    if kind == "all":
        return True
    if kind == "word":
        return term[1] in word_set
    if kind == "prefix":
        return any(word.startswith(term[1]) for word in word_set)
    phrase = term[1]
    width = len(phrase)
    for word_list in word_lists:
        for start in range(len(word_list) - width + 1):
            if tuple(word_list[start:start + width]) == phrase:
                return True
    return False


def _q_matches(terms, operator, content):
    """(matches, number of matched terms) of one entry for the parsed query."""
    if not terms:
        return False, 0
    if all(term[0] == "all" for term in terms):
        return True, 0
    word_lists = [_words(text) for text in _texts(content, [])]
    word_set = set()
    for word_list in word_lists:
        word_set.update(word_list)
    hits = [_term_matches(term, word_lists, word_set) for term in terms]
    counted = sum(1 for term, hit in zip(terms, hits) if hit and term[0] != "all")
    return (all(hits) if operator == "and" else any(hits)), counted


# ---------------------------------------------------------------------- filters


def field_values(content, field_name):
    """The values an entry has for a field name: a dot goes into objects and lists of objects,
    a list gives each of its elements, null counts as not there."""
    found = [content]
    for part in field_name.split("."):
        following = []
        for item in found:
            if isinstance(item, dict) and part in item:
                value = item[part]
                if isinstance(value, list):
                    following.extend(value)
                else:
                    following.append(value)
        found = following
    return [value for value in found if value is not None]


def _same(left, right):
    return type(left) is type(right) and left == right


def _in_range(value, bounds):
    """True when `value` lies inside one range object."""
    for key, bound in bounds.items():
        if isinstance(bound, str) and bound == "*":
            continue
        if isinstance(bound, bool) or isinstance(value, bool):
            return False
        if isinstance(bound, int) != isinstance(value, int) or isinstance(bound, str) != isinstance(
                value, str):
            return False
        if key in ("from", "gte") and not value >= bound:
            return False
        if key == "gt" and not value > bound:
            return False
        if key in ("to", "lte") and not value <= bound:
            return False
        if key == "lt" and not value < bound:
            return False
    return True


def _filter_matches(one, content):
    kind = one["type"]
    if kind == "and":
        return all(_filter_matches(inner, content) for inner in one["filters"])
    if kind == "or":
        return any(_filter_matches(inner, content) for inner in one["filters"])
    if kind == "not":
        return not _filter_matches(one["filter"], content)
    values = field_values(content, one["field_name"])
    if kind == "exists":
        return bool(values)
    if kind == "match_any":
        return any(_same(value, wanted) for value in values for wanted in one["values"])
    if kind == "match_all":
        return all(any(_same(value, wanted) for value in values) for wanted in one["values"])
    if kind == "range":
        return any(_in_range(value, bounds) for value in values for bounds in one["values"]
                   if isinstance(value, (int, str)))
    raise ValueError("unknown filter type %r" % (kind,))     # the checker lets none through


# ---------------------------------------------------------------------- sort and facets


def _rank(value):
    """A key that orders values of one field, and never compares text with a number."""
    if isinstance(value, bool):
        return (0, int(value))
    if isinstance(value, (int, float)):
        return (1, value)
    return (2, str(value))


def _scalars(content, field_name):
    return [value for value in field_values(content, field_name)
            if isinstance(value, (str, int, float, bool))]


def _sort_key(content, field_name, descending):
    values = _scalars(content, field_name)
    if not values:
        return (0,) if descending else (1,)       # without the field: last in both directions
    ranks = [_rank(value) for value in values]
    return (1, max(ranks)) if descending else (0, min(ranks))


def _facet_result(facet, contents):
    counts = {}
    shown = {}
    for content in contents:
        seen = set()
        for value in _scalars(content, facet["field_name"]):
            rank = _rank(value)
            if rank not in seen:
                seen.add(rank)
                counts[rank] = counts.get(rank, 0) + 1
                # the service writes the buckets of a true/false field as "true" and "false"
                shown[rank] = ("true" if value else "false") if isinstance(value, bool) else value
    ordered = sorted(counts, key=lambda rank: (-counts[rank], rank))[:facet["size"]]
    return {"@datatype": "GFacetResult", "@version": RESULT_VERSION, "name": facet["name"],
            "buckets": [{"@datatype": "GBucket", "@version": RESULT_VERSION,
                         "value": shown[rank], "count": counts[rank]} for rank in ordered]}


# ---------------------------------------------------------------------- the search


def search_entries(entries, request):
    """Evaluate a request over `entries`, a list of {"subject": ..., "content": {...}}.

    This is the whole stand-in without the database: `search` feeds it the queued entries, and
    the tests feed it lists. The request is checked here again (checking twice changes nothing).
    """
    request = _search.validate_request(request)
    terms = parse_q(request["q"]) if "q" in request else None
    operator = request.get("q_settings", {}).get("default_operator", "or")
    filters = request.get("filters", [])

    matched = []                         # (matched words, subject, content)
    for entry in entries:
        content = entry["content"]
        if not all(_filter_matches(one, content) for one in filters):
            continue
        hits = 0
        if terms is not None:
            fits, hits = _q_matches(terms, operator, content)
            if not fits:
                continue
        matched.append((hits, entry["subject"], content))

    matched.sort(key=lambda item: item[1])
    started = {item[1]: _sort_key(item[2], "started", True) for item in matched}
    matched.sort(key=lambda item: started[item[1]], reverse=True)
    matched.sort(key=lambda item: item[0], reverse=True)
    for spec in reversed(request.get("sort", [])):
        descending = spec["order"] == "desc"
        matched.sort(key=lambda item: _sort_key(item[2], spec["field_name"], descending),
                     reverse=descending)

    offset, limit = request["offset"], request["limit"]
    page = matched[offset:offset + limit]
    result = {
        "@datatype": "GSearchResult",
        "@version": RESULT_VERSION,
        "total": len(matched),
        "count": len(page),
        "offset": offset,
        "has_next_page": offset + len(page) < len(matched),
        "gmeta": [{"@datatype": "GMetaResult", "@version": RESULT_VERSION, "subject": subject,
                   "entries": [{"entry_id": None, "matched_principal_sets": [],
                                "content": copy.deepcopy(content)}]}
                  for _hits, subject, content in page],
    }
    if "facets" in request:
        contents = [item[2] for item in matched]
        result["facet_results"] = [_facet_result(facet, contents) for facet in request["facets"]]
    return result


def load_entries(db):
    """The entries the stand-in searches: everything queued that is not waiting to be removed."""
    rows = db.query("SELECT subject, content_json FROM {app}.search_entries WHERE state <> ?",
                    ["delete_pending"])
    return [{"subject": row["subject"], "content": row["content_json"]} for row in rows
            if isinstance(row["content_json"], dict)]


def search(db, request):
    """Answer a search request from this computer's copy of the index (`app.search_entries`)."""
    return search_entries(load_entries(db), request)
