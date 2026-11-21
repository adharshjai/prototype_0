"""Search: the one request checker, and the one door to whichever backend answers.

The request language is a strict subset of Globus Search's `GSearchRequest`, version
`query#1.0.0`. `validate_request` is used before EITHER backend (the stand-in on this computer,
`search_local`, or the real service, `search_globus`), so a request means the same thing on both
and nothing reaches the service that pcldb did not check. What it returns is a normalised copy:
defaults filled in, every sort order written out, `@version` set. That copy is exactly what may
be sent to the service.

pcldb is stricter than the service on purpose:

- The service answers a filter on a field it has never seen with zero results and no error
  (and a wrongly typed value with zero results on a text field, with HTTP 400 on a number,
  true/false or date field). pcldb refuses a `field_name` that is not a field of
  `schemas/search_fields.json`, and a value whose type does not fit the field's frozen type;
  the message names the field.
- Keys that would widen what a person may see or that pcldb does not copy in the stand-in are
  refused by name: `advanced`, `boosts`, `fields`, `post_facet_filters`,
  `filter_principal_sets`, `bypass_visible_to`; the filters `like`, `geo_bounding_box` and
  `geo_shape`; every facet type but `terms`.
- `limit` stops at 100 (the service allows 10000).
- `range` is for whole-number and date fields only, and a date is always written
  `YYYY-MM-DDTHH:MM:SSZ`. Whole-number fields take `range`, never `match_any`. Numbers are
  JSON numbers and true/false is JSON true/false, never text (the service also takes `"8"`
  and `"true"`; pcldb sends one form only).
- An empty `filters`, `facets` or `sort` list is dropped (the service refuses an empty
  `filters` list when there is no `q`).
- Safety limits of pcldb's own: at most 100 values in one filter, filters nested at most 8
  deep, at most 96 filters counting the nested ones.

`run(db, request, settings)` checks the request, asks the backend the settings name, and falls
back to the stand-in when the service does not answer (when the settings allow it).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from . import util
from .errors import PclError

VERSION = "query#1.0.0"

ACCEPTED_KEYS = ("@version", "q", "q_settings", "limit", "offset", "filters", "facets", "sort")

MAX_Q_CHARS = 256
DEFAULT_LIMIT = 10
MAX_LIMIT = 100
MAX_OFFSET = 9999
MAX_WINDOW = 10000          # offset + limit may not pass this (the service's result window)
MAX_FILTERS = 32
MAX_FACETS = 32
MAX_SORT = 8
DEFAULT_FACET_SIZE = 10
MAX_FACET_SIZE = 100
MAX_VALUES = 100            # pcldb's own limit for one filter
MAX_FILTER_DEPTH = 8        # pcldb's own limit
MAX_FILTERS_TOTAL = 96      # the service's limit over all filter lists; here it counts nested ones
MAX_VALUE_CHARS = 2000      # pcldb's own limit for one text value
MAX_NAME_CHARS = 64

LOCAL_COPY_NOTE = "these results come from this computer's copy of the index."
FALLBACK_NOTICE = "Globus Search could not be reached; " + LOCAL_COPY_NOTE

_REFUSED_KEYS = {
    "advanced": "the advanced query syntax is not accepted; use filters for exact conditions",
    "boosts": "boosts are not accepted",
    "fields": "pcldb always returns the content of an entry and nothing else",
    "post_facet_filters": "use filters",
    "filter_principal_sets": "pcldb does not use principal sets",
    "bypass_visible_to": "pcldb never asks Globus Search to ignore who may see an entry",
    "result_format_version": "the service no longer knows it",
}
_REFUSED_FILTERS = ("like", "geo_bounding_box", "geo_shape")
_FILTER_TYPES = ("match_any", "match_all", "range", "exists", "not", "and", "or")
_RANGE_LOW = ("gte", "gt")
_RANGE_HIGH = ("lte", "lt")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z")
_DATE_EXAMPLE = "2026-11-17T03:50:15Z"
_INT64 = 2 ** 63

_FIELDS_FILE = Path(__file__).resolve().parent / "schemas" / "search_fields.json"
_cache = {}


# ---------------------------------------------------------------------- the frozen fields


def field_kinds():
    """Every name a request may use as `field_name`, with its frozen kind.

    The kinds are those of `schemas/search_fields.json`: `string`, `text`, `string_list`,
    `object_list`, `boolean`, `integer`, `date`. The members of a list of objects are named
    with a dot: `label_counts.label` (string), `label_counts.count` (integer).
    """
    if "kinds" not in _cache:
        with open(_FIELDS_FILE, encoding="utf-8") as handle:
            fields = json.load(handle)["fields"]
        kinds = {}
        for name, spec in fields.items():
            kind = spec["type"] if isinstance(spec, dict) else spec
            kinds[name] = kind
            if kind == "object_list":
                for member, member_kind in spec.get("fields", {}).items():
                    kinds["%s.%s" % (name, member)] = member_kind
        _cache["kinds"] = kinds
    return dict(_cache["kinds"])


# ---------------------------------------------------------------------- small helpers


def _refuse(message, where=None):
    return PclError(message, code="bad_search_request", status=400,
                    details={"where": where} if where else None)


def _show(value, limit=60):
    """A short, safe (ASCII) picture of a value for a message."""
    try:
        text = json.dumps(value, ensure_ascii=True)
    except (TypeError, ValueError):
        text = ascii(value)
    return text if len(text) <= limit else text[:limit - 3] + "..."


def _is_whole(value):
    return isinstance(value, int) and not isinstance(value, bool)


def _usable_text(value):
    """True for text that can be stored and sent: no NUL, no half of a surrogate pair."""
    if "\x00" in value:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _object(value, where):
    if not isinstance(value, dict):
        raise _refuse("%s must be a JSON object written in { }, not %s." % (where, _show(value)),
                      where)
    for key in value:
        if not isinstance(key, str):
            raise _refuse("%s has a key that is not text." % where, where)
    return value


def _only_keys(value, where, required, optional=()):
    for key in value:
        if key not in required and key not in optional:
            raise _refuse("%s has the key %s, which is not accepted there. Accepted: %s."
                          % (where, _show(key), ", ".join(tuple(required) + tuple(optional))),
                          "%s.%s" % (where, key))
    for key in required:
        if key not in value:
            raise _refuse("%s needs the key %s." % (where, key), "%s.%s" % (where, key))


def _list(value, where, most, what):
    if not isinstance(value, list):
        raise _refuse("%s must be a list written in [ ], not %s." % (where, _show(value)), where)
    if len(value) > most:
        raise _refuse("%s has %d %s; at most %d are accepted." % (where, len(value), what, most),
                      where)
    return value


def _field(value, where, kinds):
    if not isinstance(value, str):
        raise _refuse("%s must be the name of a field, not %s." % (where, _show(value)), where)
    if value not in kinds:
        hint = ""
        if value.startswith("content."):
            hint = " Field names are written without the \"content.\" prefix."
        raise _refuse(
            "%s names the field %s, which is not a field of the session entries.%s The fields "
            "are: %s." % (where, _show(value), hint, ", ".join(sorted(kinds))), where)
    return value


def _is_date(value):
    if not isinstance(value, str) or not _DATE.fullmatch(value):
        return False
    try:
        util.parse_utc(value)
    except PclError:
        return False
    return True


# ---------------------------------------------------------------------- filters


def _match_values(values, kind, field, where):
    out = []
    for position, value in enumerate(values):
        here = "%s.values[%d]" % (where, position)
        if value is None:
            raise _refuse("%s is null. A value is text or true/false, never null (use an "
                          "\"exists\" filter to ask whether a field is there)." % here, here)
        if kind == "boolean":
            if not isinstance(value, bool):
                raise _refuse("%s is %s, but %s is a true/false field: its values are true or "
                              "false, without quotes." % (here, _show(value), field), here)
        elif kind == "date":
            if not _is_date(value):
                raise _refuse("%s is %s, but %s is a date field: write a date like %s."
                              % (here, _show(value), field, _DATE_EXAMPLE), here)
        else:
            if not isinstance(value, str):
                raise _refuse("%s is %s, but %s is a text field: its values are text in quotes."
                              % (here, _show(value), field), here)
            if len(value) > MAX_VALUE_CHARS:
                raise _refuse("%s is longer than %d characters." % (here, MAX_VALUE_CHARS), here)
            if not _usable_text(value):
                raise _refuse("%s holds a character that cannot be sent (NUL or half of a "
                              "surrogate pair)." % here, here)
        out.append(value)
    return out


def _range_end(value, kind, field, here):
    if isinstance(value, str) and value == "*":
        return value
    if kind == "integer":
        if not _is_whole(value) or not -_INT64 <= value < _INT64:
            raise _refuse("%s is %s, but %s is a whole-number field: write a whole number, or "
                          "\"*\" for an open end." % (here, _show(value), field), here)
        return value
    if not _is_date(value):
        raise _refuse("%s is %s, but %s is a date field: write a date like %s, or \"*\" for an "
                      "open end." % (here, _show(value), field, _DATE_EXAMPLE), here)
    return value


def _range_object(value, kind, field, where):
    value = _object(value, where)
    if "from" in value or "to" in value:
        for key in value:
            if key not in ("from", "to"):
                raise _refuse("%s mixes \"from\"/\"to\" with %s. Write either {\"from\": A, "
                              "\"to\": B} or {\"gte\" or \"gt\": A, \"lte\" or \"lt\": B}."
                              % (where, _show(key)), "%s.%s" % (where, key))
        for key in ("from", "to"):
            if key not in value:
                raise _refuse("%s needs both \"from\" and \"to\"; use \"*\" for an open end."
                              % where, "%s.%s" % (where, key))
        return {"from": _range_end(value["from"], kind, field, where + ".from"),
                "to": _range_end(value["to"], kind, field, where + ".to")}
    for key in value:
        if key not in _RANGE_LOW and key not in _RANGE_HIGH:
            raise _refuse("%s has the key %s, which is not accepted there. Write either "
                          "{\"from\": A, \"to\": B} or {\"gte\" or \"gt\": A, \"lte\" or \"lt\": "
                          "B}." % (where, _show(key)), "%s.%s" % (where, key))
    low = [key for key in _RANGE_LOW if key in value]
    high = [key for key in _RANGE_HIGH if key in value]
    if len(low) != 1 or len(high) != 1:
        raise _refuse("%s needs one lower end (\"gte\" or \"gt\") and one upper end (\"lte\" or "
                      "\"lt\"); use \"*\" for an open end." % where, where)
    return {low[0]: _range_end(value[low[0]], kind, field, "%s.%s" % (where, low[0])),
            high[0]: _range_end(value[high[0]], kind, field, "%s.%s" % (where, high[0]))}


def _filter(value, where, kinds, state, depth):
    value = _object(value, where)
    state["count"] += 1
    if state["count"] > MAX_FILTERS_TOTAL:
        raise _refuse("The request has more than %d filters, counting the ones inside "
                      "and/or/not." % MAX_FILTERS_TOTAL, where)
    if depth > MAX_FILTER_DEPTH:
        raise _refuse("%s is nested more than %d filters deep." % (where, MAX_FILTER_DEPTH), where)
    kind_of_filter = value.get("type")
    if kind_of_filter in _REFUSED_FILTERS:
        raise _refuse("%s has the type %s, which pcldb does not accept. Accepted types: %s."
                      % (where, _show(kind_of_filter), ", ".join(_FILTER_TYPES)), where + ".type")
    if not isinstance(kind_of_filter, str) or kind_of_filter not in _FILTER_TYPES:
        raise _refuse("%s needs a \"type\" that is one of: %s (found %s)."
                      % (where, ", ".join(_FILTER_TYPES), _show(kind_of_filter)), where + ".type")

    if kind_of_filter in ("match_any", "match_all"):
        _only_keys(value, where, ("type", "field_name", "values"))
        field = _field(value["field_name"], where + ".field_name", kinds)
        kind = kinds[field]
        if kind == "integer":
            raise _refuse("%s uses %s on %s, which is a whole-number field. Use a \"range\" "
                          "filter there: {\"from\": 5, \"to\": 5} means exactly 5."
                          % (where, kind_of_filter, field), where + ".type")
        if kind == "object_list":
            raise _refuse("%s uses %s on %s, which is a list of objects. Name one of its "
                          "members, for example %s.label." % (where, kind_of_filter, field, field),
                          where + ".field_name")
        values = _list(value["values"], where + ".values", MAX_VALUES, "values")
        if not values:
            raise _refuse("%s.values is empty; give at least one value." % where,
                          where + ".values")
        return {"type": kind_of_filter, "field_name": field,
                "values": _match_values(values, kind, field, where)}

    if kind_of_filter == "range":
        _only_keys(value, where, ("type", "field_name", "values"))
        field = _field(value["field_name"], where + ".field_name", kinds)
        kind = kinds[field]
        if kind not in ("integer", "date"):
            raise _refuse("%s is a range on %s, which is neither a whole-number nor a date "
                          "field. Use match_any there." % (where, field), where + ".field_name")
        values = _list(value["values"], where + ".values", MAX_VALUES, "ranges")
        if not values:
            raise _refuse("%s.values is empty; give at least one range such as {\"from\": A, "
                          "\"to\": B}." % where, where + ".values")
        return {"type": "range", "field_name": field,
                "values": [_range_object(item, kind, field, "%s.values[%d]" % (where, position))
                           for position, item in enumerate(values)]}

    if kind_of_filter == "exists":
        _only_keys(value, where, ("type", "field_name"))
        return {"type": "exists",
                "field_name": _field(value["field_name"], where + ".field_name", kinds)}

    if kind_of_filter == "not":
        _only_keys(value, where, ("type", "filter"))
        return {"type": "not",
                "filter": _filter(value["filter"], where + ".filter", kinds, state, depth + 1)}

    _only_keys(value, where, ("type", "filters"))
    inner = _list(value["filters"], where + ".filters", MAX_FILTERS, "filters")
    if not inner:
        raise _refuse("%s.filters is empty; an \"%s\" filter needs at least one filter inside."
                      % (where, kind_of_filter), where + ".filters")
    return {"type": kind_of_filter,
            "filters": [_filter(item, "%s.filters[%d]" % (where, position), kinds, state,
                                depth + 1)
                        for position, item in enumerate(inner)]}


# ---------------------------------------------------------------------- facets and sort


def _facet(value, where, kinds, names):
    value = _object(value, where)
    _only_keys(value, where, ("type", "field_name"), ("name", "size"))
    if value["type"] != "terms":
        raise _refuse("%s has the type %s. pcldb accepts only \"terms\" facets (counts per "
                      "value)." % (where, _show(value["type"])), where + ".type")
    field = _field(value["field_name"], where + ".field_name", kinds)
    if kinds[field] in ("object_list", "date"):
        other = ("name one of its members, for example %s.label" % field
                 if kinds[field] == "object_list" else "dates cannot be counted per value")
        raise _refuse("%s counts values of %s, which is not possible: %s."
                      % (where, field, other), where + ".field_name")
    name = value.get("name", field)
    if (not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_CHARS
            or not _usable_text(name) or any(mark in name for mark in "[]>")):
        raise _refuse("%s.name must be text of 1 to %d characters without [ ] or > (found %s)."
                      % (where, MAX_NAME_CHARS, _show(name)), where + ".name")
    if name in names:
        raise _refuse("%s has the name %s, which another facet of the request already has. "
                      "Give each facet its own \"name\"." % (where, _show(name)), where + ".name")
    names.add(name)
    size = value.get("size", DEFAULT_FACET_SIZE)
    if not _is_whole(size) or not 1 <= size <= MAX_FACET_SIZE:
        raise _refuse("%s.size must be a whole number from 1 to %d (found %s)."
                      % (where, MAX_FACET_SIZE, _show(size)), where + ".size")
    return {"name": name, "type": "terms", "field_name": field, "size": size}


def _sort(value, where, kinds):
    value = _object(value, where)
    _only_keys(value, where, ("field_name",), ("order",))
    field = _field(value["field_name"], where + ".field_name", kinds)
    if kinds[field] == "object_list":
        raise _refuse("%s sorts by %s, which is a list of objects. Name one of its members, "
                      "for example %s.count." % (where, field, field), where + ".field_name")
    order = value.get("order", "desc")     # the service's own default when order is left out
    if not isinstance(order, str) or order not in ("asc", "desc"):
        raise _refuse("%s.order must be \"asc\" or \"desc\" (found %s)." % (where, _show(order)),
                      where + ".order")
    return {"field_name": field, "order": order}


# ---------------------------------------------------------------------- the request


def validate_request(request):
    """Check a search request and return its normalised copy; a PclError names what is wrong.

    The copy has `@version`, `limit` and `offset` always; `q`, `q_settings`, `filters`,
    `facets` and `sort` when the request has them (an empty list counts as not there); every
    facet with `name` and `size`; every sort with `order`. Checking a normalised copy again
    returns an equal copy.
    """
    if not isinstance(request, dict):
        raise _refuse("A search request must be a JSON object written in { }, not %s."
                      % _show(request))
    for key in request:
        if not isinstance(key, str):
            raise _refuse("The search request has a key that is not text.")
        if key in _REFUSED_KEYS:
            raise _refuse("The search request has the key %s, which pcldb does not accept: %s. "
                          "Accepted keys: %s." % (_show(key), _REFUSED_KEYS[key],
                                                  ", ".join(ACCEPTED_KEYS)), key)
        if key not in ACCEPTED_KEYS:
            raise _refuse("The search request has the key %s, which is not known. Accepted "
                          "keys: %s." % (_show(key), ", ".join(ACCEPTED_KEYS)), key)
    kinds = field_kinds()
    out = {}

    version = request.get("@version", VERSION)
    if not isinstance(version, str) or version != VERSION:
        raise _refuse("@version must be \"%s\" (found %s)." % (VERSION, _show(version)),
                      "@version")
    out["@version"] = VERSION

    if "q" in request:
        q = request["q"]
        if not isinstance(q, str):
            raise _refuse("q must be text, not %s." % _show(q), "q")
        if len(q) > MAX_Q_CHARS:
            raise _refuse("q has %d characters; at most %d are accepted." % (len(q), MAX_Q_CHARS),
                          "q")
        if not q.strip():
            raise _refuse("q is empty. Write the words to look for, or \"*\" for everything.",
                          "q")
        if not _usable_text(q):
            raise _refuse("q holds a character that cannot be sent (NUL or half of a surrogate "
                          "pair).", "q")
        out["q"] = q

    if "q_settings" in request:
        settings = _object(request["q_settings"], "q_settings")
        _only_keys(settings, "q_settings", ("mode", "default_operator"))
        if settings["mode"] != "query_string":
            raise _refuse("q_settings.mode must be \"query_string\" (found %s)."
                          % _show(settings["mode"]), "q_settings.mode")
        operator = settings["default_operator"]
        if not isinstance(operator, str) or operator not in ("and", "or"):
            raise _refuse("q_settings.default_operator must be \"and\" or \"or\" (found %s)."
                          % _show(operator), "q_settings.default_operator")
        if "q" not in out:
            raise _refuse("q_settings says how to read q, but the request has no q.",
                          "q_settings")
        out["q_settings"] = {"mode": "query_string", "default_operator": operator}

    limit = request.get("limit", DEFAULT_LIMIT)
    if not _is_whole(limit) or not 0 <= limit <= MAX_LIMIT:
        raise _refuse("limit must be a whole number from 0 to %d (found %s). 0 gives counts "
                      "only." % (MAX_LIMIT, _show(limit)), "limit")
    offset = request.get("offset", 0)
    if not _is_whole(offset) or not 0 <= offset <= MAX_OFFSET:
        raise _refuse("offset must be a whole number from 0 to %d (found %s)."
                      % (MAX_OFFSET, _show(offset)), "offset")
    if offset + limit > MAX_WINDOW:
        raise _refuse("offset plus limit is %d; a search can reach the first %d results only."
                      % (offset + limit, MAX_WINDOW), "offset")
    out["limit"] = limit
    out["offset"] = offset

    if "filters" in request:
        filters = _list(request["filters"], "filters", MAX_FILTERS, "filters")
        state = {"count": 0}
        checked = [_filter(item, "filters[%d]" % position, kinds, state, 1)
                   for position, item in enumerate(filters)]
        if checked:
            out["filters"] = checked

    if "q" not in out and "filters" not in out:
        raise _refuse("A search request needs q (the words to look for; \"*\" means "
                      "everything) or at least one filter.", "q")

    if "facets" in request:
        facets = _list(request["facets"], "facets", MAX_FACETS, "facets")
        names = set()
        checked = [_facet(item, "facets[%d]" % position, kinds, names)
                   for position, item in enumerate(facets)]
        if checked:
            out["facets"] = checked

    if "sort" in request:
        sort = _list(request["sort"], "sort", MAX_SORT, "sort fields")
        checked = [_sort(item, "sort[%d]" % position, kinds)
                   for position, item in enumerate(sort)]
        if checked:
            out["sort"] = checked

    return out


# ---------------------------------------------------------------------- the facade


def run(db, request, settings, *, globus=None):
    """Answer one search request. Returns `(result, backend_used, notices)`.

    `result` has the shape of a Globus Search result (see `search_local`). `backend_used` is
    `"local"` or `"globus"`. With `search.backend` = `"globus"` the service is asked; when that
    fails and `search.fallback_to_local` is true, the stand-in answers instead and `notices`
    says so in one sentence; when it is false the failure is raised (`Unavailable` when the
    service could not be reached). `globus` is the `GlobusSearch` to use (the server keeps
    one); without it the shared one for these settings is used.

    `db` is only used when the stand-in answers. A caller that would otherwise hold a
    transaction open while Globus is asked can use `run_store`.
    """
    from . import search_local
    return _run(lambda checked: search_local.search(db, checked), request, settings, globus)


def run_store(store, request, settings, *, globus=None):
    """As `run`, for a caller that has the store and no open transaction: a transaction is
    opened only for the moment the stand-in reads the entries, never while Globus is asked."""
    from . import search_local

    def local(checked):
        with store.app() as db:
            return search_local.search(db, checked)
    return _run(local, request, settings, globus)


def _run(local, request, settings, globus):
    request = validate_request(request)
    backend = settings.search.backend
    if backend == "local":
        return local(request), "local", []
    if backend != "globus":
        raise PclError("The setting search.backend is %s; it must be \"local\" or \"globus\"."
                       % _show(backend), code="bad_setting")
    try:
        if globus is None:
            from . import search_globus
            globus = search_globus.shared(settings)
        return globus.search(request), "globus", []
    except PclError as error:
        if not settings.search.fallback_to_local:
            raise
        return local(request), "local", [_fallback_notice(error)]


def _fallback_notice(error):
    """One sentence for the person: why this computer's copy answered instead of the service."""
    if error.code == "globus_sign_in":
        return ("Globus Search was not asked because the sign-in to Globus is missing or has "
                "run out (run: pcldb search login); " + LOCAL_COPY_NOTE)
    if error.code == "no_search_index":
        return ("Globus Search was not asked because no index is set yet (run: pcldb search "
                "create-index); " + LOCAL_COPY_NOTE)
    if error.status == 503:
        return FALLBACK_NOTICE
    return ("Globus Search did not answer this request: %s These results come from this "
            "computer's copy of the index." % _one_line(error.message))


def _one_line(text, limit=300):
    text = " ".join(str(text).split())
    if len(text) > limit:
        text = text[:limit - 3] + "..."
    return text if text.endswith((".", "!", "?")) else text + "."
