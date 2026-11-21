"""search.validate_request: what is accepted, how it comes back, and every refusal by name."""

from __future__ import annotations

import copy
import json
import unittest

from pcldb import search
from pcldb.errors import PclError

from tests.search_cases import CASES, all_of, any_of, between, by, exists, no, terms

V = "query#1.0.0"
AND = {"mode": "query_string", "default_operator": "and"}
DATE = "2026-11-17T03:50:15Z"


def full(**keys):
    """What a normalised request looks like: version, limit and offset are always there."""
    out = {"@version": V}
    if "q" in keys:
        out["q"] = keys.pop("q")
    if "q_settings" in keys:
        out["q_settings"] = keys.pop("q_settings")
    out["limit"] = keys.pop("limit", 10)
    out["offset"] = keys.pop("offset", 0)
    out.update(keys)
    return out


# (name, request, the normalised copy that must come back)
VALID = [
    ("only q", {"q": "adenine"}, full(q="adenine")),
    ("star", {"q": "*"}, full(q="*")),
    ("version given", {"@version": V, "q": "*"}, full(q="*")),
    ("q of 256 characters", {"q": "a" * 256}, full(q="a" * 256)),
    ("q with quotes and a star", {"q": "\"lab 214\" calib*"}, full(q="\"lab 214\" calib*")),
    ("q in another script", {"q": "\u6e2c\u8a66 \u00e9chantillon"},
     full(q="\u6e2c\u8a66 \u00e9chantillon")),
    ("q_settings and", {"q": "a b", "q_settings": AND}, full(q="a b", q_settings=AND)),
    ("q_settings or", {"q": "a b", "q_settings": {"mode": "query_string",
                                                  "default_operator": "or"}},
     full(q="a b", q_settings={"mode": "query_string", "default_operator": "or"})),
    ("limit 0", {"q": "*", "limit": 0}, full(q="*", limit=0)),
    ("limit 100", {"q": "*", "limit": 100}, full(q="*", limit=100)),
    ("offset 9999 with limit 1", {"q": "*", "limit": 1, "offset": 9999},
     full(q="*", limit=1, offset=9999)),
    ("offset plus limit exactly 10000", {"q": "*", "limit": 100, "offset": 9900},
     full(q="*", limit=100, offset=9900)),
    ("filters without q", {"filters": [any_of("instrument", "raman-portable")]},
     full(filters=[any_of("instrument", "raman-portable")])),
    ("match_any with several values", {"q": "*", "filters": [any_of("tags", "a", "b", "c")]},
     full(q="*", filters=[any_of("tags", "a", "b", "c")])),
    ("match_all on a list field", {"filters": [all_of("labels", "adenine", "r6g")]},
     full(filters=[all_of("labels", "adenine", "r6g")])),
    ("true/false field takes true and false", {"filters": [any_of("simulated", True, False)]},
     full(filters=[any_of("simulated", True, False)])),
    ("text field (notes) takes text", {"filters": [any_of("notes", "as written")]},
     full(filters=[any_of("notes", "as written")])),
    ("date field takes an exact date", {"filters": [any_of("started", DATE)]},
     full(filters=[any_of("started", DATE)])),
    ("member of the list of objects", {"filters": [any_of("label_counts.label", "adenine")]},
     full(filters=[any_of("label_counts.label", "adenine")])),
    ("range from/to on a number", {"filters": [between("items", 1, 10)]},
     full(filters=[between("items", 1, 10)])),
    ("range with both ends open", {"filters": [between("items", "*", "*")]},
     full(filters=[between("items", "*", "*")])),
    ("range on a date", {"filters": [between("started", "2026-11-01T00:00:00Z", "*")]},
     full(filters=[between("started", "2026-11-01T00:00:00Z", "*")])),
    ("range gte/lt", {"filters": [{"type": "range", "field_name": "bytes",
                                   "values": [{"gte": 0, "lt": 1000}]}]},
     full(filters=[{"type": "range", "field_name": "bytes", "values": [{"gte": 0, "lt": 1000}]}])),
    ("range gt/lte on a date",
     {"filters": [{"type": "range", "field_name": "uploaded",
                   "values": [{"gt": DATE, "lte": "*"}]}]},
     full(filters=[{"type": "range", "field_name": "uploaded",
                    "values": [{"gt": DATE, "lte": "*"}]}])),
    ("several range objects", {"filters": [{"type": "range", "field_name": "items", "values": [
        {"from": 0, "to": 1}, {"gte": 20, "lte": 30}]}]},
     full(filters=[{"type": "range", "field_name": "items", "values": [
         {"from": 0, "to": 1}, {"gte": 20, "lte": 30}]}])),
    ("range on the count of a label", {"filters": [between("label_counts.count", 5, "*")]},
     full(filters=[between("label_counts.count", 5, "*")])),
    ("negative and large numbers", {"filters": [between("bytes", -1, 2 ** 62)]},
     full(filters=[between("bytes", -1, 2 ** 62)])),
    ("exists", {"filters": [exists("verified")]}, full(filters=[exists("verified")])),
    ("exists on the list of objects", {"filters": [exists("label_counts")]},
     full(filters=[exists("label_counts")])),
    ("not", {"filters": [no(any_of("withdrawn", True))]},
     full(filters=[no(any_of("withdrawn", True))])),
    ("and / or nested",
     {"filters": [{"type": "or", "filters": [
         any_of("instrument", "a"),
         {"type": "and", "filters": [exists("started"), no(exists("verified"))]}]}]},
     full(filters=[{"type": "or", "filters": [
         any_of("instrument", "a"),
         {"type": "and", "filters": [exists("started"), no(exists("verified"))]}]}])),
    ("32 filters", {"filters": [exists("started")] * 32}, full(filters=[exists("started")] * 32)),
    ("facet defaults are filled in", {"q": "*", "facets": [terms("instrument")]},
     full(q="*", facets=[{"name": "instrument", "type": "terms", "field_name": "instrument",
                          "size": 10}])),
    ("facet with name and size", {"q": "*", "facets": [terms("labels", name="by label", size=100)]},
     full(q="*", facets=[{"name": "by label", "type": "terms", "field_name": "labels",
                          "size": 100}])),
    ("two facets on one field with two names",
     {"q": "*", "facets": [terms("tags"), terms("tags", name="tags again", size=1)]},
     full(q="*", facets=[{"name": "tags", "type": "terms", "field_name": "tags", "size": 10},
                         {"name": "tags again", "type": "terms", "field_name": "tags",
                          "size": 1}])),
    ("facet on true/false, a number and a member",
     {"q": "*", "facets": [terms("simulated"), terms("items"), terms("label_counts.label")]},
     full(q="*", facets=[
         {"name": "simulated", "type": "terms", "field_name": "simulated", "size": 10},
         {"name": "items", "type": "terms", "field_name": "items", "size": 10},
         {"name": "label_counts.label", "type": "terms", "field_name": "label_counts.label",
          "size": 10}])),
    ("sort with order", {"q": "*", "sort": [by("started", "asc")]},
     full(q="*", sort=[{"field_name": "started", "order": "asc"}])),
    ("sort without order becomes desc, as the service does", {"q": "*", "sort": [by("started")]},
     full(q="*", sort=[{"field_name": "started", "order": "desc"}])),
    ("eight sort fields",
     {"q": "*", "sort": [by(name) for name in ("started", "uploaded", "verified", "last_run",
                                                "items", "bytes", "files", "folder")]},
     full(q="*", sort=[{"field_name": name, "order": "desc"} for name in (
         "started", "uploaded", "verified", "last_run", "items", "bytes", "files", "folder")])),
    ("sort by a list field and a member",
     {"q": "*", "sort": [by("tags", "asc"), by("label_counts.count", "desc")]},
     full(q="*", sort=[{"field_name": "tags", "order": "asc"},
                       {"field_name": "label_counts.count", "order": "desc"}])),
    ("empty lists are dropped", {"q": "*", "filters": [], "facets": [], "sort": []},
     full(q="*")),
    ("everything at once",
     {"@version": V, "q": "adenine", "q_settings": AND, "limit": 25, "offset": 50,
      "filters": [any_of("withdrawn", False), between("started", DATE, "*")],
      "facets": [terms("instrument", size=5)], "sort": [by("started", "desc")]},
     full(q="adenine", q_settings=AND, limit=25, offset=50,
          filters=[any_of("withdrawn", False), between("started", DATE, "*")],
          facets=[{"name": "instrument", "type": "terms", "field_name": "instrument", "size": 5}],
          sort=[{"field_name": "started", "order": "desc"}])),
]

# (name, request, a piece of text the message must hold, details["where"])
INVALID = [
    ("not an object", ["q"], "JSON object", None),
    ("a string", "adenine", "JSON object", None),
    ("null", None, "JSON object", None),
    ("neither q nor filters", {}, "needs q", "q"),
    ("only limit", {"limit": 5}, "needs q", "q"),
    ("empty filters and no q", {"filters": []}, "needs q", "q"),
    ("unknown key", {"q": "*", "colour": "red"}, "\"colour\"", "colour"),
    ("advanced", {"q": "a:b", "advanced": True}, "\"advanced\"", "advanced"),
    ("boosts", {"q": "*", "boosts": []}, "\"boosts\"", "boosts"),
    ("fields", {"q": "*", "fields": ["content", "visible_to"]}, "\"fields\"", "fields"),
    ("post_facet_filters", {"q": "*", "post_facet_filters": []}, "\"post_facet_filters\"",
     "post_facet_filters"),
    ("filter_principal_sets", {"q": "*", "filter_principal_sets": ["a"]},
     "\"filter_principal_sets\"", "filter_principal_sets"),
    ("bypass_visible_to", {"q": "*", "bypass_visible_to": True}, "\"bypass_visible_to\"",
     "bypass_visible_to"),
    ("result_format_version", {"q": "*", "result_format_version": "2019-08-27"},
     "\"result_format_version\"", "result_format_version"),
    ("old version", {"@version": "2017-09-01", "q": "*"}, "@version", "@version"),
    ("version not text", {"@version": 1, "q": "*"}, "@version", "@version"),
    ("q a number", {"q": 5}, "q must be text", "q"),
    ("q null", {"q": None}, "q must be text", "q"),
    ("q empty", {"q": ""}, "q is empty", "q"),
    ("q only spaces", {"q": "   "}, "q is empty", "q"),
    ("q of 257 characters", {"q": "a" * 257}, "257 characters", "q"),
    ("q with NUL", {"q": "a\x00b"}, "cannot be sent", "q"),
    ("q with half a surrogate pair", {"q": "a\ud800"}, "cannot be sent", "q"),
    ("q_settings without q", {"q_settings": AND, "filters": [exists("started")]},
     "has no q", "q_settings"),
    ("q_settings not an object", {"q": "a", "q_settings": "and"}, "q_settings must be",
     "q_settings"),
    ("q_settings other mode", {"q": "a", "q_settings": {"mode": "advanced_query_string",
                                                         "default_operator": "and"}},
     "q_settings.mode", "q_settings.mode"),
    ("q_settings text_match", {"q": "a", "q_settings": {"mode": "text_match"}},
     "default_operator", "q_settings.default_operator"),
    ("q_settings without mode", {"q": "a", "q_settings": {"default_operator": "and"}},
     "needs the key mode", "q_settings.mode"),
    ("q_settings operator in capitals", {"q": "a", "q_settings": {"mode": "query_string",
                                                                   "default_operator": "AND"}},
     "default_operator", "q_settings.default_operator"),
    ("q_settings extra key", {"q": "a", "q_settings": dict(AND, fuzziness=1)}, "\"fuzziness\"",
     "q_settings.fuzziness"),
    ("limit 101", {"q": "*", "limit": 101}, "limit must be", "limit"),
    ("limit negative", {"q": "*", "limit": -1}, "limit must be", "limit"),
    ("limit as text", {"q": "*", "limit": "10"}, "limit must be", "limit"),
    ("limit true", {"q": "*", "limit": True}, "limit must be", "limit"),
    ("limit with a fraction", {"q": "*", "limit": 10.0}, "limit must be", "limit"),
    ("offset 10000", {"q": "*", "offset": 10000}, "offset must be", "offset"),
    ("offset negative", {"q": "*", "offset": -5}, "offset must be", "offset"),
    ("offset plus limit over 10000", {"q": "*", "offset": 9995, "limit": 100},
     "offset plus limit", "offset"),
    ("filters not a list", {"filters": {"type": "exists", "field_name": "started"}},
     "filters must be a list", "filters"),
    ("33 filters", {"filters": [exists("started")] * 33}, "at most 32", "filters"),
    ("a filter that is not an object", {"filters": ["started"]}, "filters[0] must be",
     "filters[0]"),
    ("filter without type", {"filters": [{"field_name": "started"}]}, "filters[0] needs a",
     "filters[0].type"),
    ("unknown filter type", {"filters": [{"type": "fuzzy", "field_name": "tags"}]},
     "filters[0] needs a", "filters[0].type"),
    ("like filter", {"filters": [{"type": "like", "field_name": "folder", "value": "*scan*"}]},
     "\"like\"", "filters[0].type"),
    ("geo filter", {"filters": [{"type": "geo_bounding_box", "field_name": "location"}]},
     "\"geo_bounding_box\"", "filters[0].type"),
    ("unknown field", {"filters": [any_of("sample", "S-001")]}, "\"sample\"",
     "filters[0].field_name"),
    ("content. prefix", {"filters": [any_of("content.instrument", "x")]},
     "without the \"content.\" prefix", "filters[0].field_name"),
    ("field name not text", {"filters": [any_of(7, "x")]}, "name of a field",
     "filters[0].field_name"),
    ("match_any without values", {"filters": [{"type": "match_any", "field_name": "tags"}]},
     "needs the key values", "filters[0].values"),
    ("match_any with an empty list", {"filters": [any_of("tags")]}, "values is empty",
     "filters[0].values"),
    ("match_any values not a list", {"filters": [{"type": "match_any", "field_name": "tags",
                                                  "values": "demo"}]},
     "must be a list", "filters[0].values"),
    ("match_any with null", {"filters": [any_of("tags", "demo", None)]}, "is null",
     "filters[0].values[1]"),
    ("a number for a text field", {"filters": [any_of("instrument", 5)]}, "text field",
     "filters[0].values[0]"),
    ("true for a text field", {"filters": [any_of("tags", True)]}, "text field",
     "filters[0].values[0]"),
    ("text for a true/false field", {"filters": [any_of("simulated", "true")]},
     "true/false field", "filters[0].values[0]"),
    ("mixed values for a true/false field", {"filters": [any_of("withdrawn", False, "no")]},
     "true/false field", "filters[0].values[1]"),
    ("match_any on a whole-number field", {"filters": [any_of("items", "8")]},
     "Use a \"range\" filter", "filters[0].type"),
    ("match_all on a whole-number field", {"filters": [all_of("bytes", 8)]},
     "Use a \"range\" filter", "filters[0].type"),
    ("match_any on the list of objects itself", {"filters": [any_of("label_counts", "adenine")]},
     "label_counts.label", "filters[0].field_name"),
    ("a day without a time for a date field", {"filters": [any_of("started", "2026-11-17")]},
     "date field", "filters[0].values[0]"),
    ("match_any extra key", {"filters": [dict(any_of("tags", "a"), post_filter=True)]},
     "\"post_filter\"", "filters[0].post_filter"),
    ("a value that is too long", {"filters": [any_of("tags", "x" * 2001)]}, "longer than",
     "filters[0].values[0]"),
    ("101 values", {"filters": [any_of("tags", *["t%d" % n for n in range(101)])]},
     "at most 100", "filters[0].values"),
    ("range on a text field", {"filters": [between("instrument", "a", "z")]},
     "neither a whole-number nor a date", "filters[0].field_name"),
    ("range on a true/false field", {"filters": [between("simulated", False, True)]},
     "neither a whole-number nor a date", "filters[0].field_name"),
    ("range without to", {"filters": [{"type": "range", "field_name": "items",
                                       "values": [{"from": 1}]}]},
     "both \"from\" and \"to\"", "filters[0].values[0].to"),
    ("range with gte only", {"filters": [{"type": "range", "field_name": "items",
                                          "values": [{"gte": 1}]}]},
     "one lower end", "filters[0].values[0]"),
    ("range with gt and gte", {"filters": [{"type": "range", "field_name": "items",
                                            "values": [{"gt": 1, "gte": 1, "lt": 5}]}]},
     "one lower end", "filters[0].values[0]"),
    ("range mixing from and lte", {"filters": [{"type": "range", "field_name": "items",
                                                "values": [{"from": 1, "lte": 5}]}]},
     "mixes", "filters[0].values[0].lte"),
    ("range with an unknown key", {"filters": [{"type": "range", "field_name": "items",
                                                "values": [{"min": 1, "max": 5}]}]},
     "\"min\"", "filters[0].values[0].min"),
    ("range values empty", {"filters": [{"type": "range", "field_name": "items", "values": []}]},
     "values is empty", "filters[0].values"),
    ("range value not an object", {"filters": [{"type": "range", "field_name": "items",
                                                "values": [[1, 5]]}]},
     "must be a JSON object", "filters[0].values[0]"),
    ("text end for a whole-number range", {"filters": [between("items", "1", 5)]},
     "whole-number field", "filters[0].values[0].from"),
    ("fraction end for a whole-number range", {"filters": [between("items", 1, 2.5)]},
     "whole-number field", "filters[0].values[0].to"),
    ("true as the end of a range", {"filters": [between("items", True, 5)]},
     "whole-number field", "filters[0].values[0].from"),
    ("a day without a time in a date range", {"filters": [between("started", "2026-11-01", "*")]},
     "date field", "filters[0].values[0].from"),
    ("a date that does not exist", {"filters": [between("started", "2026-02-30T00:00:00Z", "*")]},
     "date field", "filters[0].values[0].from"),
    ("a date with an offset", {"filters": [between("started", "*",
                                                   "2026-11-17T03:50:15+00:00")]},
     "date field", "filters[0].values[0].to"),
    ("a number for a date range", {"filters": [between("started", 20261117, "*")]},
     "date field", "filters[0].values[0].from"),
    ("exists with values", {"filters": [dict(exists("tags"), values=["a"])]}, "\"values\"",
     "filters[0].values"),
    ("exists without a field", {"filters": [{"type": "exists"}]}, "needs the key field_name",
     "filters[0].field_name"),
    ("not without filter", {"filters": [{"type": "not"}]}, "needs the key filter",
     "filters[0].filter"),
    ("not with a list", {"filters": [{"type": "not", "filter": [exists("tags")]}]},
     "must be a JSON object", "filters[0].filter"),
    ("problem inside not", {"filters": [no(any_of("nope", "x"))]}, "\"nope\"",
     "filters[0].filter.field_name"),
    ("and with nothing inside", {"filters": [{"type": "and", "filters": []}]},
     "at least one filter inside", "filters[0].filters"),
    ("or without filters", {"filters": [{"type": "or"}]}, "needs the key filters",
     "filters[0].filters"),
    ("problem deep inside",
     {"filters": [{"type": "or", "filters": [exists("tags"),
                                             {"type": "and", "filters": [any_of("tags", 1)]}]}]},
     "text field", "filters[0].filters[1].filters[0].values[0]"),
    ("facets not a list", {"q": "*", "facets": terms("tags")}, "facets must be a list", "facets"),
    ("33 facets", {"q": "*", "facets": [terms("tags", name="n%d" % n) for n in range(33)]},
     "at most 32", "facets"),
    ("facet of another type",
     {"q": "*", "facets": [{"type": "date_histogram", "field_name": "started"}]},
     "only \"terms\"", "facets[0].type"),
    ("facet without type", {"q": "*", "facets": [{"field_name": "tags"}]}, "needs the key type",
     "facets[0].type"),
    ("facet on an unknown field", {"q": "*", "facets": [terms("colour")]}, "\"colour\"",
     "facets[0].field_name"),
    ("facet on a date", {"q": "*", "facets": [terms("started")]}, "dates cannot be counted",
     "facets[0].field_name"),
    ("facet on the list of objects itself", {"q": "*", "facets": [terms("label_counts")]},
     "label_counts.label", "facets[0].field_name"),
    ("facet size 0", {"q": "*", "facets": [terms("tags", size=0)]}, "size must be",
     "facets[0].size"),
    ("facet size 101", {"q": "*", "facets": [terms("tags", size=101)]}, "size must be",
     "facets[0].size"),
    ("facet size as text", {"q": "*", "facets": [terms("tags", size="5")]}, "size must be",
     "facets[0].size"),
    ("two facets with one name", {"q": "*", "facets": [terms("tags"), terms("tags")]},
     "already has", "facets[1].name"),
    ("facet name with a bracket", {"q": "*", "facets": [terms("tags", name="a[0]")]},
     "name must be", "facets[0].name"),
    ("facet name empty", {"q": "*", "facets": [terms("tags", name="")]}, "name must be",
     "facets[0].name"),
    ("facet extra key", {"q": "*", "facets": [terms("tags", missing="none")]}, "\"missing\"",
     "facets[0].missing"),
    ("sort not a list", {"q": "*", "sort": by("started")}, "sort must be a list", "sort"),
    ("nine sort fields", {"q": "*", "sort": [by("started")] * 9}, "at most 8", "sort"),
    ("sort on an unknown field", {"q": "*", "sort": [by("date")]}, "\"date\"",
     "sort[0].field_name"),
    ("sort on the list of objects itself", {"q": "*", "sort": [by("label_counts")]},
     "label_counts.count", "sort[0].field_name"),
    ("sort order in capitals", {"q": "*", "sort": [by("started", "DESC")]}, "order must be",
     "sort[0].order"),
    ("sort order ascending spelled out", {"q": "*", "sort": [by("started", "ascending")]},
     "order must be", "sort[0].order"),
    ("sort without a field", {"q": "*", "sort": [{"order": "asc"}]}, "needs the key field_name",
     "sort[0].field_name"),
    ("sort extra key", {"q": "*", "sort": [dict(by("started", "asc"), missing="_last")]},
     "\"missing\"", "sort[0].missing"),
    ("sort given as text", {"q": "*", "sort": ["started"]}, "sort[0] must be", "sort[0]"),
]


class TestValidateRequest(unittest.TestCase):

    def test_the_table_is_big_enough(self):
        self.assertGreaterEqual(len(VALID), 30)
        self.assertGreaterEqual(len(INVALID), 60)
        names = [row[0] for row in VALID + INVALID]
        self.assertEqual(len(names), len(set(names)), "every row needs its own name")

    def test_valid_requests_come_back_normalised(self):
        for name, request, wanted in VALID:
            with self.subTest(name):
                before = copy.deepcopy(request)
                got = search.validate_request(request)
                self.assertEqual(got, wanted)
                self.assertEqual(request, before, "the given request must not be changed")
                self.assertEqual(search.validate_request(got), got,
                                 "checking twice changes nothing")
                json.dumps(got, allow_nan=False)

    def test_normalised_requests_only_hold_what_the_service_accepts(self):
        accepted = set(search.ACCEPTED_KEYS)
        for name, request, _wanted in VALID:
            with self.subTest(name):
                got = search.validate_request(request)
                self.assertLessEqual(set(got), accepted)
                self.assertEqual(got["@version"], "query#1.0.0")
                self.assertIn("limit", got)
                self.assertIn("offset", got)
                self.assertTrue("q" in got or "filters" in got)
                for one in got.get("sort", []):
                    self.assertEqual(set(one), {"field_name", "order"})
                for one in got.get("facets", []):
                    self.assertEqual(set(one), {"name", "type", "field_name", "size"})

    def test_the_copy_is_independent_of_the_request(self):
        request = {"q": "*", "filters": [any_of("tags", "demo")], "sort": [by("started")]}
        got = search.validate_request(request)
        got["filters"][0]["values"].append("changed")
        got["sort"][0]["order"] = "asc"
        self.assertEqual(request["filters"][0]["values"], ["demo"])
        self.assertNotIn("order", request["sort"][0])

    def test_invalid_requests_are_refused_by_name(self):
        for name, request, piece, where in INVALID:
            with self.subTest(name):
                with self.assertRaises(PclError) as caught:
                    search.validate_request(request)
                error = caught.exception
                self.assertIn(piece, error.message)
                self.assertEqual(error.status, 400)
                self.assertEqual(error.code, "bad_search_request")
                self.assertEqual((error.details or {}).get("where"), where)
                self.assertTrue(error.message.isascii(), error.message)
                self.assertTrue(error.message.rstrip().endswith("."), error.message)

    def test_every_accepted_key_alone_is_known(self):
        self.assertEqual(search.ACCEPTED_KEYS, ("@version", "q", "q_settings", "limit", "offset",
                                                "filters", "facets", "sort"))

    def test_field_kinds_come_from_the_frozen_field_list(self):
        kinds = search.field_kinds()
        self.assertEqual(kinds["instrument"], "string")
        self.assertEqual(kinds["notes"], "text")
        self.assertEqual(kinds["tags"], "string_list")
        self.assertEqual(kinds["simulated"], "boolean")
        self.assertEqual(kinds["items"], "integer")
        self.assertEqual(kinds["started"], "date")
        self.assertEqual(kinds["label_counts"], "object_list")
        self.assertEqual(kinds["label_counts.label"], "string")
        self.assertEqual(kinds["label_counts.count"], "integer")
        kinds["instrument"] = "changed"
        self.assertEqual(search.field_kinds()["instrument"], "string", "callers get a copy")

    def test_every_field_can_be_asked_for_in_the_way_its_kind_allows(self):
        for name, kind in search.field_kinds().items():
            with self.subTest(name):
                search.validate_request({"filters": [exists(name)]})
                if kind in ("string", "text", "string_list"):
                    search.validate_request({"filters": [any_of(name, "x")],
                                             "facets": [terms(name)], "sort": [by(name)]})
                elif kind == "boolean":
                    search.validate_request({"filters": [any_of(name, True)],
                                             "facets": [terms(name)], "sort": [by(name)]})
                elif kind == "integer":
                    search.validate_request({"filters": [between(name, 0, "*")],
                                             "facets": [terms(name)], "sort": [by(name)]})
                elif kind == "date":
                    search.validate_request({"filters": [between(name, DATE, "*")],
                                             "sort": [by(name)]})

    def test_nesting_and_total_limits(self):
        deep = exists("started")
        for _ in range(search.MAX_FILTER_DEPTH - 1):
            deep = no(deep)
        search.validate_request({"filters": [deep]})
        with self.assertRaises(PclError) as caught:
            search.validate_request({"filters": [no(deep)]})
        self.assertIn("nested more than", caught.exception.message)

        group = {"type": "or", "filters": [exists("started")] * 31}        # 32 filters each
        search.validate_request({"filters": [group, group, group]})         # 96 in all
        with self.assertRaises(PclError) as caught:
            search.validate_request({"filters": [group, group, group, exists("tags")]})
        self.assertIn("more than 96 filters", caught.exception.message)

    def test_messages_never_echo_a_long_value(self):
        with self.assertRaises(PclError) as caught:
            search.validate_request({"q": "*", "x" * 5000: 1})
        self.assertLess(len(caught.exception.message), 400)

    def test_every_shared_case_is_a_valid_request(self):
        for one in CASES:
            with self.subTest(one["name"]):
                search.validate_request(one["request"])


if __name__ == "__main__":
    unittest.main()
