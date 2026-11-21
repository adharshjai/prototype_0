"""Tests of pcldb.registration: the card checker, the checks of a registration document, and
registering into the store (every store test runs on SQLite and on PostgreSQL)."""

from __future__ import annotations

import argparse
import copy
import dataclasses
import io
import json
import sys
import tempfile
import types
import unicodedata
import unittest
from pathlib import Path
from unittest import mock

from pcldb import adapters_raman, registration, search_entries, sources, util
from pcldb.config import Settings
from pcldb.errors import Conflict, PclError, Unavailable
from tests import registry_support as rs
from tests.support import PostgresCase, SqliteCase
from tests.test_search_entries import LOCATION_VECTORS

DEFAULT = object()
NAN = float("nan")
INFINITY = float("inf")


# ---------------------------------------------------------------------- the card checker


class CardCheckerTests(unittest.TestCase):

    def test_the_table_of_good_and_bad_cards(self):
        cases = rs.card_cases()
        self.assertGreaterEqual(len(cases), 40)
        self.assertGreaterEqual(sum(1 for _label, _card, valid in cases if valid), 15)
        self.assertGreaterEqual(sum(1 for _label, _card, valid in cases if not valid), 25)
        for label, card, valid in cases:
            with self.subTest(label):
                problems = registration.validate_card(card)
                self.assertIsInstance(problems, list)
                if valid:
                    self.assertEqual(problems, [])
                else:
                    self.assertTrue(problems, "no problem was found")
                    self.assertTrue(all(isinstance(problem, str) and problem.startswith("card")
                                        for problem in problems))

    def test_messages_that_were_compared_with_the_proof_of_concept(self):
        """Each of these was produced by pcl_index's validate.card_errors for the same card
        (run once, read-only) and is pinned here word for word."""
        form = "does not have the required form"
        time_form = "^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(\\.[0-9]{1,9})?Z$"
        pinned = {
            "a list instead of an object": ["card: must be a JSON object"],
            "no session id": ["card: session_id is missing"],
            "another schema version": ['card.schema: must be "pcl-session/1"'],
            "an unknown field": ["card: unknown field colour"],
            "an instrument with capital letters":
                ['card.instrument: "Raman-Portable" %s ^[a-z0-9][a-z0-9-]{0,39}$' % form],
            "a session named SiMuLaTeD": ['card.session: "SiMuLaTeD" is not allowed'],
            "an empty session id": ["card.session_id: must not be empty"],
            "a time followed by a newline":
                ['card.started_utc: "2026-11-17T03:50:15Z\\n" %s %s' % (form, time_form)],
            "the 30th of February":
                ['card.started_utc: "2026-02-30T00:00:00Z" is not a real date and time'],
            "ended_utc as a number": ["card.ended_utc: must be string or null, not integer"],
            "simulated as text": ["card.simulated: must be boolean, not string"],
            "a sample id that is a number": ["card.sample_ids[0]: must be string, not integer"],
            "notes of 20001 characters": ["card.notes: longer than 20000 characters"],
            "items as 13.0": ["card.items: must be integer, not number"],
            "items as true": ["card.items: must be integer, not boolean"],
            "items below zero": ["card.items: must be at least 0"],
            "a completion outside the list":
                ['card.completion: must be one of "complete", "partial", "failed", "unknown"'],
            "written_by without a version": ["card.written_by: version is missing"],
            "written_by with an extra field": ["card.written_by: unknown field host"],
            "a tag with a capital letter":
                ['card.tags[0]: "Demo" %s ^[a-z0-9][a-z0-9-]{0,63}$' % form],
            "a NUL character in the notes":
                ["card.notes: holds a character that cannot be stored (not UTF-8, or a control "
                 "character)"],
            "half of a surrogate pair in extra":
                ["card.extra.deep[0].x: holds a character that cannot be stored (not UTF-8, or "
                 "a control character)"],
            "a control character in a field name":
                ["card.extra: a field name holds a character that cannot be stored"],
        }
        cards = {label: card for label, card, _valid in rs.card_cases()}
        for label, expected in pinned.items():
            with self.subTest(label):
                self.assertEqual(registration.validate_card(cards[label]), expected)

    def test_stricter_rule_one_a_whole_match(self):
        self.assertEqual(registration.validate_card(rs.make_card(instrument="raman-portable")), [])
        self.assertTrue(registration.validate_card(rs.make_card(instrument="raman-portable\n")))
        self.assertTrue(registration.validate_card(rs.make_card(tags=["demo\n"])))
        self.assertTrue(registration.validate_card(rs.make_card(uploaded_utc="2026-11-17T03:50:15Z\n")))

    def test_stricter_rule_two_a_real_date_and_time(self):
        for text in ("2026-13-45T99:99:99Z", "2026-02-30T00:00:00Z", "2026-11-17T24:00:00Z",
                     "2026-11-17T03:50:60Z", "0000-01-01T00:00:00Z"):
            with self.subTest(text=text):
                for field in ("started_utc", "ended_utc", "uploaded_utc"):
                    problems = registration.validate_card(rs.make_card(**{field: text}))
                    self.assertEqual(problems, ['card.%s: "%s" is not a real date and time'
                                                % (field, text)])
        self.assertEqual(registration.validate_card(rs.make_card(started_utc="2028-02-29T23:59:59.9Z")), [])

    def test_stricter_rule_three_storable_text_at_any_depth(self):
        deep = {"a": [{"b": {"c": ["fine", "bad\x00"]}}]}
        self.assertEqual(registration.validate_card(rs.make_card(extra=deep)),
                         ["card.extra.a[0].b.c[1]: holds a character that cannot be stored (not "
                          "UTF-8, or a control character)"])
        self.assertTrue(registration.validate_card(rs.make_card(instrument_info={"\ud83d": 1})))
        self.assertEqual(registration.validate_card(rs.make_card(extra={"t": "tab\tnl\ncr\r"})), [])

    def test_integer_means_a_whole_number_that_is_not_true_or_false(self):
        self.assertEqual(registration.validate_card(rs.make_card(items=13)), [])
        for value in (13.0, True, False, "13", None, [13]):
            with self.subTest(value=value):
                self.assertTrue(registration.validate_card(rs.make_card(items=value)))

    def test_several_problems_are_all_listed(self):
        card = rs.make_card(instrument="X", simulated="no", colour=1, session_id=rs.REMOVE)
        problems = registration.validate_card(card)
        self.assertEqual(len(problems), 4)

    def test_a_card_nested_very_deeply_is_a_problem_not_a_crash(self):
        deep = current = {}
        for _ in range(5000):
            current["x"] = {}
            current = current["x"]
        problems = registration.validate_card(rs.make_card(extra=deep))
        self.assertTrue(problems)
        self.assertIn("nested", problems[0])

    def test_the_schema_file_is_the_packaged_one(self):
        schema = registration.load_card_schema()
        self.assertEqual(schema["$id"], "pcl-session/1")
        self.assertIs(schema["additionalProperties"], False)

    def test_the_small_validator_keywords(self):
        check = registration.schema_errors
        self.assertEqual(check(5, {"type": ["string", "null"]}, "v"),
                         ["v: must be string or null, not integer"])
        self.assertEqual(check("ab", {"minLength": 3}, "v"), ["v: shorter than 3 characters"])
        self.assertEqual(check([1], {"minItems": 2}, "v"), ["v: needs at least 2 item(s)"])
        self.assertEqual(check({"a": 1}, {"additionalProperties": {"type": "string"}}, "v"),
                         ["v.a: must be string, not integer"])
        self.assertEqual(check("x", {"allOf": [{"minLength": 2}, {"pattern": "^y"}]}, "v"),
                         ["v: shorter than 2 characters",
                          'v: "x" does not have the required form ^y'])
        conditional = {"if": {"properties": {"kind": {"const": "a"}}},
                       "then": {"required": ["extra"]}}
        self.assertEqual(check({"kind": "a"}, conditional, "v"), ["v: extra is missing"])
        self.assertEqual(check({"kind": "b"}, conditional, "v"), [])
        self.assertEqual(check("abc", {"pattern": "b"}, "v"), [])        # a search, not a whole match
        self.assertEqual(check(1.5, {"type": "number", "minimum": 2}, "v"), ["v: must be at least 2"])


# ---------------------------------------------------------------------- checks without a database


class DocumentCheckTests(unittest.TestCase):

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.settings = Settings.for_test(Path(holder.name))

    def check(self, document):
        return registration.check_document(document, self.settings)

    def refused(self, document, *expected, code=None):
        with self.assertRaises(PclError) as caught:
            self.check(document)
        error = caught.exception
        self.assertNotIsInstance(error, Conflict)
        self.assertEqual(error.status, 400)
        for text in expected:
            self.assertIn(text, error.message)
        if code:
            self.assertEqual(error.code, code)
        self.assertTrue(error.message.isascii(), "a message must be ASCII: %r" % error.message)
        return error

    def with_path(self, path, **card):
        return rs.make_document(path=path, card=rs.make_card(**card))

    # -- the document itself

    def test_a_good_document(self):
        parts = self.check(rs.make_document(upload=rs.make_upload()))
        self.assertEqual(parts["path"], rs.SIMULATED_PATH)
        self.assertEqual((parts["area"], parts["folder"], parts["is_copy"]),
                         ("simulated", rs.DEMO_SESSION, False))
        self.assertEqual(len(parts["files"]), 5)
        self.assertEqual(parts["bytes"], 100 + 101 + 102 + 700 + 300)
        self.assertEqual(len(parts["doc_sha256"]), 64)
        self.assertEqual(parts["upload"], rs.make_upload())

    def test_the_document_must_be_an_object_of_the_known_shape(self):
        for value in (None, [], "document", 5):
            with self.subTest(value=value):
                self.refused(value, "must be a JSON object")
        for name in ("schema", "location", "card", "files"):
            with self.subTest(missing=name):
                document = rs.make_document()
                del document[name]
                self.refused(document, "has no " + name)
        document = rs.make_document()
        document["owner"] = "demo-operator-a"
        self.refused(document, 'unknown field "owner"')
        document = rs.make_document()
        document["schema"] = "pcl-registration/2"
        self.refused(document, "pcl-registration/2", "pcl-registration/1", code="unknown_schema")

    def test_a_document_nested_very_deeply_is_refused_not_a_crash(self):
        deep = current = []
        for _ in range(200):
            current.append([])
            current = current[0]
        for document in (dict(rs.make_document(), schema=deep),
                         rs.make_document(card=rs.make_card(extra={"deep": deep})),
                         rs.make_document(path=deep), rs.make_document(upload={"transport": deep})):
            self.refused(document, "nested deeper than 64 levels")

    def test_sums_sha256_may_be_left_out(self):
        document = rs.make_document(files=[])
        del document["sums_sha256"]
        self.assertIsNone(self.check(document)["sums_sha256"])

    # -- rule 2: the path

    def test_paths_that_are_refused(self):
        cases = {
            "": "is empty",
            "/raw/raman-portable/x": "starts with /",
            "raw/raman-portable/x/": "empty part",
            "raw//x": "empty part",
            "raw/./x": 'holds the part "."',
            "raw/../x": 'holds the part ".."',
            "..": 'holds the part ".."',
            "raw\\raman-portable\\x": "backslash",
            "raw/raman-portable/a\tb": "control character",
            "raw/raman-portable/a\x7fb": "control character",
            "raw/raman-portable/a\nb": "control character",
            "raw/" + "s" * 256: "longer than 255",
            "/".join(["abcdefghi"] * 103): "longer than 1024",
            "raw/x/\ud800": "broken character",
        }
        for path, expected in cases.items():
            with self.subTest(path=path[:40]):
                self.refused(rs.make_document(path=path), expected, "Nothing was registered",
                             code="bad_path")
        for value in (None, 5, ["raw", "x"], {"path": "x"}):
            with self.subTest(value=value):
                self.refused(rs.make_document(path=value), "location.path must be text")

    def test_the_longest_path_and_segment_are_accepted(self):
        segment = "s" * 255
        path = "/".join([segment] * 3 + ["t" * 200, "u" * 55])
        self.assertEqual(len(path), 1024)
        parts = self.check(rs.make_document(path=path))
        self.assertEqual(parts["path"], path)

    def test_the_location_block(self):
        document = rs.make_document()
        document["location"] = "raw/x"
        self.refused(document, "location must be a JSON object")
        document = rs.make_document()
        document["location"]["host"] = "demo"
        self.refused(document, 'unknown field "host"')
        document = rs.make_document()
        del document["location"]["path"]
        self.refused(document, "location has no path")
        document = rs.make_document()
        document["location"] = {"path": rs.SIMULATED_PATH}          # the two others left out
        self.check(document)

    def test_names_windows_cannot_hold_are_refused(self):
        bad = ["CON", "con", "Prn", "AUX", "nul", "NUL.tar.gz", "com1", "COM9.old", "LPT1",
               "lpt9", "aux.txt", "ends_with_dot.", "ends with space ", "con .txt"]
        for name in bad:
            with self.subTest(name=name):
                self.refused(self.with_path("data/uploads/" + name), "Windows",
                             "Rename the folder", code="bad_path")
                self.refused(self.with_path(name), "Windows", code="bad_path")
                # A folder higher up is ARC's own business: only the session folder's name
                # (and, in the lab layout, the instrument's folder) must fit Windows.
                self.check(self.with_path("data/%s/session" % name))
        self.refused(self.with_path("raw/raman-portable/simulated/" + rs.DEMO_SESSION + "."),
                     "ends with a dot or a space")
        for slug in ("con", "aux", "nul", "com1", "lpt9", "prn"):
            with self.subTest(instrument=slug):
                self.assertEqual(registration.validate_card(rs.make_card(instrument=slug)), [])
                self.refused(self.with_path("raw/%s/simulated/%s" % (slug, rs.DEMO_SESSION),
                                            instrument=slug), "device name on Windows")
                self.refused(self.with_path("raw/%s/%s" % (slug, rs.DEMO_SESSION),
                                            instrument=slug, simulated=False),
                             "device name on Windows")
        good = ["console", "com10", "com0", "lpt", "nul_session", "aconb", "auxiliary",
                "my.con", "COM1x", "a.b", "a b"]
        for name in good:
            with self.subTest(name=name):
                self.check(self.with_path("data/uploads/" + name))

    def test_a_folder_inside_incoming_is_refused(self):
        self.refused(self.with_path("raman_globus_upload/_incoming/%s.u0123456789ab"
                                    % rs.DEMO_SESSION), "_incoming", "still being", code="bad_path")
        self.refused(self.with_path("raw/raman-portable/_incoming/x"), "_incoming")

    # -- rule 1 and from_lead 8: the card

    def test_an_invalid_card_lists_its_problems(self):
        card = rs.make_card(instrument="Raman", simulated="yes")
        error = self.refused(rs.make_document(card=card), "2 problem(s)", "card.instrument",
                             "card.simulated", "Nothing was registered", code="invalid_card")
        self.assertEqual(error.details["problems"], registration.validate_card(card))
        self.refused(rs.make_document(card=[]), "card: must be a JSON object", code="invalid_card")

    def test_every_bad_card_of_the_table_is_refused(self):
        for label, card, valid in rs.card_cases():
            if not valid:
                with self.subTest(label):
                    document = rs.make_document()
                    document["card"] = card
                    self.refused(document, code="invalid_card")

    def test_many_problems_are_cut_short_in_the_message_but_not_in_the_details(self):
        card = rs.make_card(tags=["BAD %d" % number for number in range(30)])
        error = self.refused(rs.make_document(card=card), "30 problem(s)", "and 18 more")
        self.assertEqual(len(error.details["problems"]), 30)

    def test_item_counts_above_the_database_s_range_are_refused(self):
        self.check(rs.make_document(card=rs.make_card(items=2147483647, items_planned=2147483647)))
        for name in ("items", "items_planned"):
            for value in (2147483648, 10 ** 30):
                with self.subTest(name=name, value=value):
                    self.refused(rs.make_document(card=rs.make_card(**{name: value})),
                                 name, "2147483647", "Correct the card", code="invalid_card")

    def test_nan_and_infinity_are_refused_anywhere(self):
        cases = {
            "card.extra": rs.make_document(card=rs.make_card(extra={"x": NAN})),
            "card deep": rs.make_document(card=rs.make_card(
                instrument_info={"a": [1, {"b": -INFINITY}]})),
            "files bytes": rs.make_document(files=[{"rel_path": "a", "bytes": INFINITY,
                                                    "sha256": None}]),
            "upload": rs.make_document(upload=rs.make_upload(checked=NAN)),
        }
        for label, document in cases.items():
            with self.subTest(label):
                self.refused(document, "NaN or Infinity", "nothing was registered",
                             code="bad_number")
        self.refused(cases["card deep"], "document.card.instrument_info.a[1].b")
        # What json.loads makes of the bare tokens is caught the same way.
        loaded = json.loads('{"x": NaN, "y": [Infinity]}')
        self.refused(rs.make_document(card=rs.make_card(extra=loaded)), "NaN or Infinity")

    def test_large_whole_numbers_in_free_objects_are_fine(self):
        self.check(rs.make_document(card=rs.make_card(extra={"count": 10 ** 30, "ratio": 1e300})))

    # -- rules 2 and 3: the layout

    def test_the_layout_shapes(self):
        real = self.check(self.with_path(rs.REAL_PATH, simulated=False))
        self.assertEqual((real["area"], real["folder"], real["is_copy"]),
                         ("real", rs.DEMO_SESSION, False))
        simulated = self.check(self.with_path(rs.SIMULATED_PATH, simulated=True))
        self.assertEqual((simulated["area"], simulated["folder"]), ("simulated", rs.DEMO_SESSION))

    def test_the_instrument_folder_must_be_the_card_s_instrument(self):
        self.refused(self.with_path("raw/jeol-jsm-it800-shl/" + rs.DEMO_SESSION, simulated=False),
                     "jeol-jsm-it800-shl", "raman-portable", "nothing was registered",
                     code="instrument_mismatch")
        self.refused(self.with_path("raw/Raman-Portable/simulated/" + rs.DEMO_SESSION),
                     code="instrument_mismatch")                       # case matters

    def test_a_simulated_folder_needs_a_simulated_card(self):
        self.refused(self.with_path(rs.SIMULATED_PATH, simulated=False),
                     "simulated/ folder", "simulated is false", code="simulated_mismatch")
        self.refused(self.with_path(rs.REAL_PATH, simulated=True),
                     "not in a simulated/ folder", "simulated is true", code="simulated_mismatch")

    def test_the_folder_must_have_the_session_s_name(self):
        self.refused(self.with_path("raw/raman-portable/simulated/another_name"),
                     "another_name", rs.DEMO_SESSION, "_copy2", code="name_mismatch")
        self.refused(self.with_path("raw/raman-portable/simulated/" + rs.DEMO_SESSION.upper()),
                     code="name_mismatch")

    def test_the_simulated_container_is_not_a_session(self):
        self.refused(self.with_path("raw/raman-portable/simulated", simulated=False),
                     "holds an instrument's simulated sessions", code="bad_path")
        self.refused(self.with_path("raw/raman-portable/simulated/simulated"), code="name_mismatch")

    def test_copies(self):
        for number in (2, 3, 10, 999, 12345):
            with self.subTest(number=number):
                parts = self.check(self.with_path("%s_copy%d" % (rs.SIMULATED_PATH, number)))
                self.assertTrue(parts["is_copy"])
                self.assertEqual(parts["folder"], "%s_copy%d" % (rs.DEMO_SESSION, number))
        for ending in ("_copy", "_copy0", "_copy1", "_copy02", "_copy2x", "_copy-2", "_COPY2",
                       "_copy2_copy3x"):
            with self.subTest(ending=ending):
                self.refused(self.with_path(rs.SIMULATED_PATH + ending), code="name_mismatch")

    def test_a_session_whose_own_name_ends_in_copy(self):
        card = {"session": "scan_copy2"}
        parts = self.check(self.with_path("raw/raman-portable/simulated/scan_copy2", **card))
        self.assertFalse(parts["is_copy"])                             # its own name
        parts = self.check(self.with_path("raw/raman-portable/simulated/scan_copy2_copy2", **card))
        self.assertTrue(parts["is_copy"])

    def test_any_other_path_under_the_project_is_accepted(self):
        for path in (rs.OTHER_PATH, "raman_globus_upload/" + rs.DEMO_SESSION, "x",
                     "users/demo-operator-a/tests/another_name", "raw/raman-portable/a/b/c",
                     "raw/" + rs.DEMO_SESSION, "results/raman-portable/x"):
            for simulated in (True, False):
                with self.subTest(path=path, simulated=simulated):
                    parts = self.check(self.with_path(path, simulated=simulated))
                    self.assertEqual(parts["area"], "simulated" if simulated else "real")
                    self.assertEqual(parts["folder"], path.split("/")[-1])
                    self.assertFalse(parts["is_copy"])
        parts = self.check(self.with_path(rs.OTHER_PATH + "_copy2"))
        self.assertTrue(parts["is_copy"])

    # -- rule 4: the files

    def test_file_lists_that_are_refused(self):
        good = {"rel_path": "a.txt", "bytes": 1, "sha256": "a" * 64}
        cases = {
            "not a list": ({"a.txt": 1}, "files must be a list"),
            "an entry that is no object": (["a.txt"], "files[0] must be a JSON object"),
            "an unknown key": ([dict(good, owner="x")], 'files[0] has the unknown field "owner"'),
            "listed key": ([dict(good, listed=True)], 'unknown field "listed"'),
            "no bytes": ([{"rel_path": "a", "sha256": None}], "files[0] has no bytes"),
            "no sha256": ([{"rel_path": "a", "bytes": 1}], "files[0] has no sha256"),
            "no rel_path": ([{"bytes": 1, "sha256": None}], "files[0] has no rel_path"),
            "absolute": ([dict(good, rel_path="/etc/passwd")], "starts with /"),
            "climbing": ([dict(good, rel_path="../a")], 'holds the part ".."'),
            "backslash": ([dict(good, rel_path="dark\\a.txt")], "backslash"),
            "empty": ([dict(good, rel_path="")], "is empty"),
            "not text": ([dict(good, rel_path=5)], "must be text"),
            "twice": ([good, dict(good)], '"a.txt" is listed twice'),
            "negative": ([dict(good, bytes=-1)], "whole number from 0 up"),
            "true": ([dict(good, bytes=True)], "whole number from 0 up"),
            "a fraction": ([dict(good, bytes=1.0)], "whole number from 0 up"),
            "text size": ([dict(good, bytes="1")], "whole number from 0 up"),
            "too large": ([dict(good, bytes=2 ** 63)], "whole number from 0 up"),
            "upper-case hash": ([dict(good, sha256="A" * 64)], "64 lower-case hex digits or null"),
            "short hash": ([dict(good, sha256="a" * 63)], "64 lower-case hex digits or null"),
            "hash with newline": ([dict(good, sha256="a" * 64 + "\n")], "64 lower-case hex"),
            "number as hash": ([dict(good, sha256=5)], "64 lower-case hex digits or null"),
            "sum too large": ([dict(good, bytes=2 ** 62), dict(good, rel_path="b", bytes=2 ** 62)],
                              "add up to more"),
        }
        for label, (files, expected) in cases.items():
            with self.subTest(label):
                self.refused(rs.make_document(files=files), expected)

    def test_at_most_one_hundred_thousand_files(self):
        entry = {"bytes": 1, "sha256": None}
        files = [dict(entry, rel_path="f%06d" % number) for number in range(100000)]
        parts = self.check(rs.make_document(files=files))
        self.assertEqual((len(parts["files"]), parts["bytes"]), (100000, 100000))
        files.append(dict(entry, rel_path="one_more"))
        self.refused(rs.make_document(files=files), "100001 files", "at most 100000",
                     code="too_many_files")

    def test_listed_means_the_file_has_a_hash(self):
        files = [{"rel_path": "a", "bytes": 5, "sha256": "a" * 64},
                 {"rel_path": "b", "bytes": 0, "sha256": None}]
        parts = self.check(rs.make_document(files=files))
        self.assertEqual([entry["listed"] for entry in parts["files"]], [True, False])
        self.assertEqual(parts["bytes"], 5)
        empty = self.check(rs.make_document(files=[]))
        self.assertEqual((empty["files"], empty["bytes"]), ([], 0))

    def test_sums_sha256(self):
        for value in ("A" * 64, "a" * 63, 5, "", ["a" * 64]):
            with self.subTest(value=value):
                self.refused(rs.make_document(sums_sha256=value), "sums_sha256 must be")
        self.assertIsNone(self.check(rs.make_document(sums_sha256=None))["sums_sha256"])

    def test_the_entry_of_sha256sums_must_agree_with_sums_sha256(self):
        document = rs.make_document()
        document["sums_sha256"] = "f" * 64
        self.refused(document, "two different SHA-256 values for SHA256SUMS")

    # -- the upload block

    def test_only_the_five_upload_keys_are_accepted(self):
        for name in ("username", "identity", "host", "collection", "ssh_username"):
            with self.subTest(name=name):
                self.refused(rs.make_document(upload=rs.make_upload(**{name: "demo-operator-a"})),
                             "upload has the unknown field", name)
        self.refused(rs.make_document(upload="ssh"), "upload must be a JSON object")

    def test_upload_values(self):
        bad = {
            "transport": ("ftp", 5, ""), "upload_id": ("", "u" * 65, 5, "a\x00b"),
            "checked": ("yes", 1, 0), "check_method": ("looked", 5, ""),
            "checked_utc": ("yesterday", "2026-02-30T00:00:00Z", 5, "2026-11-17T03:51:47Z\n"),
        }
        for name, values in bad.items():
            for value in values:
                with self.subTest(name=name, value=value):
                    self.refused(rs.make_document(upload=rs.make_upload(**{name: value})),
                                 "upload." + name)
        for transport in ("globus", "ssh", "local"):
            for method in ("sha256-on-arc", "globus-checksum", "globus-verify", "none"):
                upload = rs.make_upload(transport=transport, check_method=method, checked=False,
                                        checked_utc=None)
                self.assertEqual(self.check(rs.make_document(upload=upload))["upload"], upload)
        self.assertEqual(self.check(rs.make_document(upload={}))["upload"], {})
        self.assertIsNone(self.check(rs.make_document())["upload"])

    # -- rule 5: where the files are

    def test_the_fixed_location_vectors_of_the_proof_of_concept(self):
        cards = {
            rs.REAL_PATH: rs.make_card(simulated=False),
            rs.SIMULATED_PATH: rs.make_card(),
            "raman_globus_upload/simulated/single_20261115_101500_000":
                rs.make_card(session="single_20261115_101500_000"),
        }
        for path, card in cards.items():
            with self.subTest(path=path):
                where = self.check(rs.make_document(path=path, card=card))["where"]
                self.assertEqual(where, dict(LOCATION_VECTORS[path],
                                             collection_id="8e804f3a-5bac-4b6d-9836-bcd5b9c133ec"))

    def test_collection_and_project_folder_of_the_document(self):
        other = "0A0A0A0A-1111-4222-8333-444444444444"
        where = self.check(rs.make_document(collection_id=other,
                                            project_path="/projects/other_lab"))["where"]
        self.assertEqual(where["collection_id"], other.lower())
        self.assertEqual(where["storage_path"], "/projects/other_lab/" + rs.SIMULATED_PATH)
        self.assertEqual(where["data_path"], "/other_lab/" + rs.SIMULATED_PATH)
        self.assertNotEqual(where["subject"], LOCATION_VECTORS[rs.SIMULATED_PATH]["subject"])
        self.refused(rs.make_document(collection_id="not-a-uuid"), "collection id")
        for value in ("projects/x", "", 5, "/projects/x/", "/projects//x", "/projects/../x",
                      "/projects/x\\y"):
            with self.subTest(value=value):
                self.refused(rs.make_document(project_path=value), "location.project_path")
        self.refused(rs.make_document(project_path="/home/demo"), "collection root")

    # -- rule 6: the hash

    def test_the_document_hash(self):
        document = rs.make_document(upload=rs.make_upload())
        first = self.check(document)["doc_sha256"]
        shuffled = copy.deepcopy(document)
        shuffled["files"].reverse()
        shuffled["upload"] = rs.make_upload(checked=False)
        shuffled["location"]["collection_id"] = "0a0a0a0a-1111-4222-8333-444444444444"
        self.assertEqual(self.check(shuffled)["doc_sha256"], first)    # the same contents
        changed = [
            rs.make_document(card=rs.make_card(notes="another note")),
            rs.make_document(path=rs.SIMULATED_PATH + "_copy2"),
            rs.make_document(sums_sha256=None),
        ]
        more_files = rs.make_document()
        more_files["files"][0]["bytes"] += 1
        changed.append(more_files)
        hashes = {self.check(item)["doc_sha256"] for item in changed}
        self.assertEqual(len(hashes), 4)
        self.assertNotIn(first, hashes)

    def test_the_document_hash_is_a_fixed_function(self):
        files = [{"rel_path": "b", "bytes": 2, "sha256": None},
                 {"rel_path": "a", "bytes": 1, "sha256": "a" * 64}]
        expected = rs.sha256_hex((
            '{"card":{"x":"\u00b5"},"files":[{"bytes":1,"rel_path":"a","sha256":"%s"},'
            '{"bytes":2,"rel_path":"b","sha256":null}],"path":"p/q","sums_sha256":null}'
            % ("a" * 64)).encode("utf-8"))
        self.assertEqual(registration.document_hash("p/q", {"x": "\u00b5"}, files, None), expected)

    def test_the_checks_do_not_change_the_document(self):
        document = rs.make_document(upload=rs.make_upload())
        before = copy.deepcopy(document)
        self.check(document)
        self.assertEqual(document, before)


# ---------------------------------------------------------------------- registering, both engines


class StoreHelpers:

    ACTOR = "demo-operator-a"

    def register(self, document=DEFAULT, **options):
        """register_document in one transaction of the test's own."""
        document = rs.make_document() if document is DEFAULT else document
        options.setdefault("actor", self.ACTOR)
        options.setdefault("source", "upload")
        with self.store.app() as db:
            return registration.register_document(db, document, settings=self.settings, **options)

    def register_audited(self, document=DEFAULT, **options):
        document = rs.make_document() if document is DEFAULT else document
        options.setdefault("actor", self.ACTOR)
        options.setdefault("source", "upload")
        return registration.register(self.store, document, settings=self.settings, **options)

    def query(self, sql, params=()):
        with self.store.app() as db:
            return db.query(sql, params)

    def session(self, path=rs.SIMULATED_PATH):
        rows = self.query("SELECT * FROM {pcl}.sessions WHERE path = ?", [path])
        return rows[0] if rows else None

    def files(self, path=rs.SIMULATED_PATH):
        return {row["rel_path"]: row for row in self.query(
            "SELECT * FROM {pcl}.files WHERE session_path = ?", [path])}

    def audit(self):
        rows = self.query("SELECT * FROM {app}.registrations")
        return sorted(rows, key=lambda row: (row["at_utc"], row["action"]))

    def counts(self):
        with self.store.app() as db:
            return {name: db.scalar("SELECT COUNT(*) FROM " + name) for name in (
                "{pcl}.sessions", "{pcl}.files", "{app}.registrations", "{app}.search_entries")}


class RegistrationCases(StoreHelpers):

    def test_created(self):
        document = rs.make_document(upload=rs.make_upload())
        result = self.register(document)
        self.assertEqual(result, {
            "action": "created", "path": rs.SIMULATED_PATH,
            "subject": LOCATION_VECTORS[rs.SIMULATED_PATH]["subject"],
            "web_url": LOCATION_VECTORS[rs.SIMULATED_PATH]["web_url"],
            "n_files": 5, "bytes": 1303})
        self.assertEqual(self.counts(), {"{pcl}.sessions": 1, "{pcl}.files": 5,
                                         "{app}.registrations": 1, "{app}.search_entries": 1})

    def test_the_session_row(self):
        card = rs.make_card()
        document = rs.make_document(card=card, upload=rs.make_upload())
        self.register(document)
        row = self.session()
        times = {name: row.pop(name) for name in ("registered_utc", "updated_utc")}
        self.assertEqual(times["registered_utc"], times["updated_utc"])
        self.assertRegex(times["registered_utc"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        expected = {
            "path": rs.SIMULATED_PATH, "instrument": "raman-portable", "area": "simulated",
            "folder": rs.DEMO_SESSION, "session_name": rs.DEMO_SESSION,
            "session_id": rs.DEMO_SESSION_ID, "is_copy": False,
            "started_utc": "2026-11-16T22:50:15.281Z", "ended_utc": "2026-11-16T22:51:40.072Z",
            "simulated": True, "sample_ids": ["demo-sample-129"], "operator": "demo-operator-a",
            "location": "demo-lab-1", "method": "1d-y", "items": 13, "items_planned": 13,
            "completion": "complete", "notes": "demo run, nothing was measured", "tags": ["demo"],
            "format": "raman-session/1 folder", "uploaded_utc": "2026-11-17T03:50:15.281Z",
            "upload_id": rs.DEMO_UPLOAD_ID, "has_card": True, "card_json": card,
            "corrections_json": [], "corrected": False, "withdrawn": False, "bytes": 1303,
            "n_files": 5, "sums_sha256": document["sums_sha256"], "verified_utc": None,
            "verify_ok": None, "upload_json": rs.make_upload(),
            "collection_id": "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec",
            "registered_by": "demo-operator-a", "source": "upload",
            "doc_sha256": registration.check_document(document, self.settings)["doc_sha256"],
        }
        expected.update(LOCATION_VECTORS[rs.SIMULATED_PATH])
        self.assertEqual(row, expected)

    def test_card_fields_that_are_left_out_are_empty(self):
        card = rs.minimal_card(started_utc="2026-11-16T22:50:15Z",
                               uploaded_utc="2026-11-17T03:50:15.123456789Z")
        self.register(rs.make_document(card=card))
        row = self.session()
        for name in ("ended_utc", "operator", "location", "method", "items", "items_planned",
                     "notes", "upload_id", "upload_json", "verified_utc", "verify_ok"):
            self.assertIsNone(row[name], name)
        self.assertEqual((row["sample_ids"], row["tags"]), (["demo-sample-129"], []))
        self.assertEqual(row["started_utc"], "2026-11-16T22:50:15.000Z")     # one text form
        self.assertEqual(row["uploaded_utc"], "2026-11-17T03:50:15.123Z")    # cut, not rounded
        self.assertEqual(row["card_json"], card)                             # exactly as given

    def test_the_card_is_kept_exactly_as_given(self):
        card = rs.make_card(ended_utc=None, notes="5 \u00b5m\n\ttab", sample_ids=["129", "demo b"],
                            extra={"big": 10 ** 30, "nested": {"list": [1, 2.5, None, True]},
                                   "started_utc": "2026-11-16T22:50:15Z"})
        self.register(rs.make_document(card=card))
        row = self.session()
        self.assertEqual(row["card_json"], card)
        self.assertEqual(row["notes"], "5 \u00b5m\n\ttab")
        self.assertEqual(row["sample_ids"], ["129", "demo b"])
        self.assertIsNone(row["ended_utc"])

    def test_the_files_rows(self):
        document = rs.make_document()
        document["files"].append({"rel_path": "added_later.txt", "bytes": 0, "sha256": None})
        self.register(document)
        files = self.files()
        self.assertEqual(set(files), {"session.json", "point_0001.json", "dark/dark_demo.txt",
                                      "pcl_session.json", "SHA256SUMS", "added_later.txt"})
        self.assertEqual(files["session.json"], {
            "session_path": rs.SIMULATED_PATH, "rel_path": "session.json", "bytes": 100,
            "sha256": rs.fake_sha("session.json"), "listed": True})
        self.assertEqual(files["added_later.txt"]["sha256"], None)
        self.assertIs(files["added_later.txt"]["listed"], False)
        # SHA256SUMS carries its own hash, counts as listed, and is counted like every file.
        self.assertEqual(files["SHA256SUMS"]["sha256"], document["sums_sha256"])
        self.assertIs(files["SHA256SUMS"]["listed"], True)
        row = self.session()
        self.assertEqual((row["n_files"], row["bytes"]), (6, 1303))
        self.assertEqual(row["sums_sha256"], document["sums_sha256"])

    def test_the_audit_row_of_a_new_session(self):
        document = rs.make_document()
        self.register(document)
        (row,) = self.audit()
        self.assertEqual(len(row.pop("reg_id")), 32)
        self.assertRegex(row.pop("at_utc"), r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
        self.assertEqual(row, {
            "user_name": "demo-operator-a", "session_path": rs.SIMULATED_PATH,
            "action": "created", "source": "upload",
            "doc_sha256": self.session()["doc_sha256"],
            "detail_json": {"n_files": 5, "bytes": 1303}})

    def test_the_search_entry_is_queued_in_the_same_transaction(self):
        result = self.register()
        (entry,) = self.query("SELECT * FROM {app}.search_entries")
        self.assertEqual(entry["state"], "pending")
        self.assertEqual(entry["subject"], result["subject"])
        self.assertEqual(entry["session_path"], rs.SIMULATED_PATH)
        self.assertEqual(entry["content_json"],
                         search_entries.content_for(self.session(), None, self.settings))
        self.assertEqual(entry["content_json"]["web_url"], result["web_url"])

    def _race_document(self, folder):
        card = rs.make_card()
        card["session"] = folder if folder.isascii() else "cafe_1"
        return rs.make_document(path="race/%s" % folder, card=card)

    def _race(self, documents):
        import threading
        barrier = threading.Barrier(len(documents))
        outcomes = [None] * len(documents)

        def run(number, document):
            barrier.wait()
            try:
                outcomes[number] = self.register_audited(document)["action"]
            except PclError as error:
                outcomes[number] = error.code
        threads = [threading.Thread(target=run, args=(number, document))
                   for number, document in enumerate(documents)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        return sorted(outcomes)

    def test_registrations_at_the_same_moment_give_the_same_answers_on_both_engines(self):
        # COR-01: two spellings of one folder that differ only in letter case, registered at
        # the same moment, used to both be created on PostgreSQL.
        self.assertEqual(self._race([self._race_document(name)
                                     for name in ("Abc_2", "abc_2", "ABC_2", "aBc_2")]),
                         ["case_conflict"] * 3 + ["created"])
        # COR-06: the same new document sent several times at once is created once and is
        # "unchanged" for the others, never "rejected".
        self.assertEqual(self._race([self._race_document("Same_1") for _ in range(6)]),
                         ["created"] + ["unchanged"] * 5)
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM {app}.registrations WHERE "
                                    "action = 'rejected' AND session_path = ?",
                                    ["race/Same_1"])[0]["n"], 0)
        self.assertEqual(self.query("SELECT COUNT(*) AS n FROM {pcl}.sessions WHERE path "
                                    "LIKE 'race/%'")[0]["n"], 2)

    def test_two_unicode_forms_of_one_name_are_one_folder(self):
        # COR-09: macOS keeps cafe-with-accent as one name whether the accent is part of the
        # letter (NFC) or a character of its own (NFD).
        import unicodedata
        first = unicodedata.normalize("NFC", "caf\u00e9_1")
        second = unicodedata.normalize("NFD", first)
        self.assertNotEqual(first, second)
        self.assertEqual(self.register_audited(self._race_document(first))["action"], "created")
        with self.assertRaises(Conflict) as caught:
            self.register_audited(self._race_document(second))
        self.assertEqual(caught.exception.code, "case_conflict")
        self.assertIsNone(registration.windows_name_problem("caf\u00e9"))
        for name in ("COM\u00b9", "lpt\u00b2.txt", "com3"):
            self.assertEqual(registration.windows_name_problem(name),
                             "is a device name on Windows", name)

    def _upload_documents(self, folder, upload_id):
        """One upload as its record registers it (raman_globus_upload/..., with the upload
        block) and as a tree registers its raw/ copy (the card's upload id, no block)."""
        card = rs.make_card(session=folder, upload_id=upload_id)
        placed = rs.make_document(path="raman_globus_upload/simulated/" + folder, card=card,
                                  upload=rs.make_upload(upload_id=upload_id))
        raw_copy = rs.make_document(path="raw/raman-portable/simulated/" + folder, card=card)
        return placed, raw_copy

    def test_a_raw_copy_and_the_upload_itself_stand_side_by_side_in_either_order(self):
        # C2-01: the raw/ copy of an upload, registered first, made the upload's own record
        # "moved by hand"; in the other order both were registered.
        first_placed, first_copy = self._upload_documents("1d_y_scan_20261116_225015_001",
                                                          "u00000000001a")
        second_placed, second_copy = self._upload_documents("1d_y_scan_20261116_225015_002",
                                                            "u00000000002a")
        answers = [self.register_audited(document)["action"]
                   for document in (first_copy, first_placed, second_placed, second_copy)]
        self.assertEqual(answers, ["created"] * 4)
        for upload_id in ("u00000000001a", "u00000000002a"):
            self.assertEqual(len(self.query("SELECT path FROM {pcl}.sessions WHERE upload_id = ?",
                                            [upload_id])), 2)
        # The same record at a third place is still a folder moved by hand, in either order.
        for folder, upload_id in (("1d_y_scan_20261116_225015_001", "u00000000001a"),
                                  ("1d_y_scan_20261116_225015_002", "u00000000002a")):
            moved = rs.make_document(path="test_folder_moved/simulated/" + folder,
                                     card=rs.make_card(session=folder, upload_id=upload_id),
                                     upload=rs.make_upload(upload_id=upload_id))
            with self.assertRaises(Conflict) as caught:
                self.register_audited(moved)
            self.assertEqual(caught.exception.code, "upload_moved")
            self.assertEqual(caught.exception.details,
                             {"registered_path": "raman_globus_upload/simulated/" + folder})
        self.assertEqual(self.counts()["{pcl}.sessions"], 4)

    def test_one_upload_at_two_places_at_the_same_moment(self):
        # C2-04: on PostgreSQL both registrations were created; SQLite refused the second.
        for number in range(6):
            upload_id = "u%012x" % (0xabc000 + number)
            folder = "1d_y_scan_20261116_2250%02d_%03d" % (number, number)
            card = rs.make_card(session=folder, upload_id=upload_id)
            documents = [rs.make_document(path=path, card=card,
                                          upload=rs.make_upload(upload_id=upload_id))
                         for path in ("raman_globus_upload/simulated/" + folder,
                                      "raw/raman-portable/simulated/" + folder)]
            with self.subTest(upload_id=upload_id):
                self.assertEqual(self._race(documents), ["created", "upload_moved"])

    def test_letter_case_folds_one_character_at_a_time(self):
        # C2-05: "Straße" and "STRASSE" are two names on Windows and macOS. They were one
        # folder when "STRASSE" came first and two in the other order.
        for first, second in (("lab/STRASSE_1", "lab/Straße_1"),
                              ("lab/Straße_2", "lab/STRASSE_2")):
            with self.subTest(first=first):
                self.assertEqual(self.register_audited(self._race_document_at(first))["action"],
                                 "created")
                self.assertEqual(self.register_audited(self._race_document_at(second))["action"],
                                 "created")
        # Names that differ only in letter case stay one folder in both orders, also outside
        # ASCII: "STRAßE" and "straße", and the Greek sigma in its three forms.
        for first, second in (("lab/STRAßE_3", "lab/straße_3"),
                              ("lab/straße_4", "lab/STRAßE_4"),
                              ("lab/ΟΔΟΣ_5", "lab/οδος_5"),
                              ("lab/οδος_6", "lab/ΟΔΟΣ_6"),
                              ("lab/οδοσ_7", "lab/οδος_7")):
            with self.subTest(first=first):
                self.assertEqual(self.register_audited(self._race_document_at(first))["action"],
                                 "created")
                with self.assertRaises(Conflict) as caught:
                    self.register_audited(self._race_document_at(second))
                self.assertEqual(caught.exception.code, "case_conflict")
                self.assertEqual(caught.exception.details, {"registered_path": first})
        self.assertEqual(registration.folded_path("Straße"), "straße")
        self.assertEqual(registration.folded_path("STRASSE"), "strasse")
        for text in ("Straße", "İstanbul", "café", "ＡK"):
            self.assertEqual(len(registration.folded_path(text)),
                             len(unicodedata.normalize("NFC", text)), text)

    def _race_document_at(self, path):
        card = rs.make_card()
        card["session"] = "x_1"
        return rs.make_document(path=path, card=card)

    def test_a_registration_is_refused_while_the_demo_is_loaded(self):
        # NEW-03 / COR-10: demo data and real data never mix (as load-index already says).
        with self.store.app() as db:
            db.upsert("pcl.meta", {"key": "demo", "value": "1"}, key=("key",))
        with self.assertRaises(Conflict) as caught:
            self.register_audited()
        self.assertEqual(caught.exception.code, "demo_loaded")
        self.assertIn(util.LAUNCHER + " demo clear", caught.exception.message)
        self.assertIsNone(self.session())
        with self.store.app() as db:
            db.upsert("pcl.meta", {"key": "demo", "value": "0"}, key=("key",))
        self.assertEqual(self.register_audited()["action"], "created")

    def test_the_same_document_again_is_unchanged(self):
        document = rs.make_document()
        first = self.register(document)
        before = (self.session(), self.files(),
                  self.query("SELECT * FROM {app}.search_entries"))
        second = self.register(copy.deepcopy(document), actor="demo-operator-b", source="tree")
        self.assertEqual(second, dict(first, action="unchanged"))
        self.assertEqual((self.session(), self.files(),
                          self.query("SELECT * FROM {app}.search_entries")), before)
        rows = self.audit()
        self.assertEqual([row["action"] for row in rows], ["created", "unchanged"])
        self.assertEqual(rows[1]["user_name"], "demo-operator-b")
        self.assertEqual(rows[1]["source"], "tree")
        self.assertEqual(rows[1]["doc_sha256"], rows[0]["doc_sha256"])
        self.assertEqual(self.counts()["{app}.registrations"], 2)

    def test_file_order_and_upload_block_do_not_make_a_document_another_one(self):
        document = rs.make_document(upload=rs.make_upload())
        self.register(document)
        again = copy.deepcopy(document)
        again["files"].reverse()
        again["upload"] = rs.make_upload(checked=False, check_method="none")
        self.assertEqual(self.register(again)["action"], "unchanged")
        self.assertEqual(self.session()["upload_json"], rs.make_upload())   # nothing was written

    def test_other_contents_for_a_registered_path_are_a_conflict(self):
        self.register()
        before = (self.session(), self.files(), self.counts())
        changes = [
            rs.make_document(card=rs.make_card(notes="changed")),
            rs.make_document(sums_sha256=None),
            rs.make_document(files=[]),
        ]
        for document in changes:
            with self.assertRaises(Conflict) as caught:
                self.register(document)
            error = caught.exception
            self.assertEqual(error.status, 409)
            self.assertIn("already registered with other contents", error.message)
            self.assertIn("placed folders are never changed", error.message)
            self.assertIn("--replace", error.message)
            self.assertEqual((self.session(), self.files(), self.counts()), before)

    def test_replace_updates_the_session(self):
        first_document = rs.make_document()
        self.register(first_document)
        first = self.session()
        changed = rs.make_document(
            card=rs.make_card(notes="corrected notes", items=12, completion="partial"),
            files=[{"rel_path": "only.txt", "bytes": 7, "sha256": None}],
            upload=rs.make_upload())
        result = self.register(changed, replace=True, actor="demo-reviewer")
        self.assertEqual(result["action"], "updated")
        self.assertEqual((result["n_files"], result["bytes"]), (1, 7))
        row = self.session()
        self.assertEqual((row["notes"], row["items"], row["completion"]),
                         ("corrected notes", 12, "partial"))
        self.assertEqual((row["n_files"], row["bytes"], row["sums_sha256"]), (1, 7, None))
        self.assertEqual(row["upload_json"], rs.make_upload())
        self.assertEqual(row["registered_utc"], first["registered_utc"])    # first registration
        self.assertEqual(row["registered_by"], "demo-operator-a")
        self.assertGreaterEqual(row["updated_utc"], first["updated_utc"])
        self.assertNotEqual(row["doc_sha256"], first["doc_sha256"])
        self.assertEqual(set(self.files()), {"only.txt"})                   # replaced, not added
        rows = self.audit()
        self.assertEqual([row["action"] for row in rows], ["created", "updated"])
        self.assertEqual(rows[1]["user_name"], "demo-reviewer")
        self.assertEqual(rows[1]["detail_json"], {"n_files": 1, "bytes": 7,
                                                  "replaced_doc_sha256": first["doc_sha256"]})
        (entry,) = self.query("SELECT * FROM {app}.search_entries")
        self.assertEqual(entry["content_json"]["items"], 12)
        self.assertEqual(entry["state"], "pending")
        self.assertEqual(self.counts()["{pcl}.sessions"], 1)

    def test_replace_of_a_new_path_creates_and_of_the_same_document_is_unchanged(self):
        document = rs.make_document()
        self.assertEqual(self.register(document, replace=True)["action"], "created")
        self.assertEqual(self.register(document, replace=True)["action"], "unchanged")

    def test_the_same_contents_with_another_upload_block(self):
        document = rs.make_document(upload=rs.make_upload())
        self.register(document)
        first = self.session()
        failed_check = copy.deepcopy(document)
        failed_check["upload"] = rs.make_upload(checked=False)
        self.assertEqual(self.register(failed_check)["action"], "unchanged")
        self.assertEqual(self.session(), first)                             # nothing written
        self.assertEqual(self.register(failed_check, replace=True)["action"], "updated")
        row = self.session()
        self.assertIs(row["upload_json"]["checked"], False)
        self.assertEqual(row["doc_sha256"], first["doc_sha256"])
        self.assertEqual(row["registered_utc"], first["registered_utc"])
        self.assertEqual(self.register(failed_check, replace=True)["action"], "unchanged")
        without = copy.deepcopy(document)
        del without["upload"]
        self.assertEqual(self.register(without, replace=True)["action"], "unchanged")
        self.assertIs(self.session()["upload_json"]["checked"], False)      # kept
        self.assertEqual([row["action"] for row in self.audit()].count("updated"), 1)

    def test_the_same_contents_in_another_collection(self):
        document = rs.make_document()
        first = self.register(document)
        moved = rs.make_document(collection_id="0a0a0a0a-1111-4222-8333-444444444444")
        self.assertEqual(self.register(moved), dict(first, action="unchanged"))
        second = self.register(moved, replace=True)
        self.assertEqual(second["action"], "updated")
        self.assertNotEqual(second["subject"], first["subject"])
        self.assertEqual(self.session()["collection_id"], "0a0a0a0a-1111-4222-8333-444444444444")
        self.assertEqual(self.session()["subject"], second["subject"])

    def test_two_requests_for_the_same_new_folder_at_once(self):
        document = rs.make_document()
        self.register(document)

        class Blind:
            """A connection that does not see the session yet: what the second of two
            requests sees when both register the same folder at the same moment."""

            def __init__(self, db):
                self.db, self.blind = db, True

            def one(self, sql, params=()):
                if self.blind and "{pcl}.sessions WHERE path = ?" in sql:
                    self.blind = False
                    return None
                return self.db.one(sql, params)

            def __getattr__(self, name):
                return getattr(self.db, name)

        before = self.counts()
        with self.assertRaises(Conflict) as caught:
            with self.store.app() as db:
                registration.register_document(Blind(db), document, actor=self.ACTOR,
                                               source="upload", settings=self.settings)
        self.assertEqual(caught.exception.status, 409)
        self.assertIn("at this very moment", caught.exception.message)
        self.assertEqual(self.counts(), before)

    def test_replace_keeps_a_withdrawal(self):
        self.register()
        with self.store.app() as db:
            db.execute("UPDATE {pcl}.sessions SET withdrawn = ?, corrected = ? WHERE path = ?",
                       [True, True, rs.SIMULATED_PATH])
        self.register(rs.make_document(card=rs.make_card(notes="again")), replace=True)
        row = self.session()
        self.assertIs(row["withdrawn"], True)       # only a correction run takes it back
        self.assertIs(row["corrected"], False)      # the values are the new card's again
        (entry,) = self.query("SELECT * FROM {app}.search_entries")
        self.assertIs(entry["content_json"]["withdrawn"], True)

    def test_the_three_path_shapes(self):
        real = rs.make_document(path=rs.REAL_PATH, card=rs.make_card(simulated=False))
        simulated = rs.make_document(path=rs.SIMULATED_PATH)
        other = rs.make_document(path=rs.OTHER_PATH)
        other_real = rs.make_document(path="raman_globus_upload/" + rs.DEMO_SESSION,
                                      card=rs.make_card(simulated=False))
        for document in (real, simulated, other, other_real):
            self.assertEqual(self.register(document)["action"], "created")
        rows = {row["path"]: row for row in self.query("SELECT * FROM {pcl}.sessions")}
        self.assertEqual({path: (row["area"], row["simulated"], row["folder"], row["instrument"])
                          for path, row in rows.items()}, {
            rs.REAL_PATH: ("real", False, rs.DEMO_SESSION, "raman-portable"),
            rs.SIMULATED_PATH: ("simulated", True, rs.DEMO_SESSION, "raman-portable"),
            rs.OTHER_PATH: ("simulated", True, rs.DEMO_SESSION, "raman-portable"),
            "raman_globus_upload/" + rs.DEMO_SESSION: ("real", False, rs.DEMO_SESSION,
                                                       "raman-portable")})
        for path in (rs.REAL_PATH, rs.SIMULATED_PATH):
            for name, value in LOCATION_VECTORS[path].items():
                self.assertEqual(rows[path][name], value, name)
        self.assertEqual(len({row["subject"] for row in rows.values()}), 4)
        self.assertEqual(self.counts()["{app}.search_entries"], 4)

    def test_a_copy_is_registered_beside_the_original(self):
        self.register()
        result = self.register(rs.make_document(path=rs.SIMULATED_PATH + "_copy2"))
        self.assertEqual(result["action"], "created")
        row = self.session(rs.SIMULATED_PATH + "_copy2")
        self.assertIs(row["is_copy"], True)
        self.assertEqual(row["folder"], rs.DEMO_SESSION + "_copy2")
        self.assertEqual(row["session_name"], rs.DEMO_SESSION)
        self.assertEqual(row["session_id"], self.session()["session_id"])   # copies share it
        self.assertIs(self.session()["is_copy"], False)
        (entry,) = self.query("SELECT * FROM {app}.search_entries WHERE session_path = ?",
                              [rs.SIMULATED_PATH + "_copy2"])
        self.assertIs(entry["content_json"]["is_copy"], True)

    def test_a_path_that_differs_only_in_letter_case_is_a_conflict(self):
        self.register(rs.make_document(path=rs.OTHER_PATH))
        before = self.counts()
        for path in (rs.OTHER_PATH.upper(), rs.OTHER_PATH.replace("raman_", "Raman_"),
                     rs.OTHER_PATH.replace("1d_y_scan", "1D_y_scan")):
            with self.subTest(path=path):
                with self.assertRaises(Conflict) as caught:
                    self.register(rs.make_document(path=path))
                error = caught.exception
                self.assertEqual((error.status, error.code), (409, "case_conflict"))
                self.assertIn("only in letter case", error.message)
                self.assertIn(rs.OTHER_PATH, error.message)
                self.assertEqual(error.details, {"registered_path": rs.OTHER_PATH})
        self.assertEqual(self.counts(), before)

    def test_paths_that_only_look_alike_are_not_a_case_conflict(self):
        self.register(rs.make_document(path="data/a_b%c/x1"))
        for path in ("data/aXb%c/x1", "data/a_bZc/x1", "data/a_b%c/x2", "data/a_b%c/x",
                     "data/a_b%c/x1/y", "data/a-b%c/x1"):
            with self.subTest(path=path):
                self.assertEqual(self.register(rs.make_document(path=path))["action"], "created")
        with self.assertRaises(Conflict):
            self.register(rs.make_document(path="DATA/A_B%C/X1"))
        self.register(rs.make_document(path="data/\u00e4b/x"))        # letters outside ASCII
        with self.assertRaises(Conflict):
            self.register(rs.make_document(path="data/\u00c4B/x"))

    def test_one_folder_on_arc_is_registered_once(self):
        self.register(rs.make_document(path="raw_copy/sub/" + rs.DEMO_SESSION))
        same_folder = rs.make_document(path="sub/" + rs.DEMO_SESSION,
                                       project_path="/projects/pcl_photonics/raw_copy")
        with self.assertRaises(Conflict) as caught:
            self.register(same_folder)
        self.assertIn("already registered as", caught.exception.message)
        self.assertIn("raw_copy/sub/" + rs.DEMO_SESSION, caught.exception.message)
        self.assertEqual(self.counts()["{pcl}.sessions"], 1)

    def test_a_failure_in_the_middle_leaves_nothing_behind(self):
        with mock.patch.object(registration.search_entries, "refresh",
                               side_effect=RuntimeError("the Search refresh broke")):
            with self.assertRaises(RuntimeError):
                self.register()
        self.assertEqual(self.counts(), {"{pcl}.sessions": 0, "{pcl}.files": 0,
                                         "{app}.registrations": 0, "{app}.search_entries": 0})
        self.assertEqual(self.register()["action"], "created")              # and it works after

    def test_a_failure_in_the_middle_of_a_replace_keeps_the_old_session(self):
        self.register()
        before = (self.session(), self.files(), self.audit(),
                  self.query("SELECT * FROM {app}.search_entries"))
        changed = rs.make_document(card=rs.make_card(notes="changed"), files=[])
        with mock.patch.object(registration.search_entries, "refresh",
                               side_effect=RuntimeError("the Search refresh broke")):
            with self.assertRaises(RuntimeError):
                self.register(changed, replace=True)
        self.assertEqual((self.session(), self.files(), self.audit(),
                          self.query("SELECT * FROM {app}.search_entries")), before)

    def test_a_refused_document_writes_nothing_through_register_document(self):
        for document in (rs.make_document(card=rs.make_card(simulated="yes")),
                         rs.make_document(path="raw/../x"),
                         rs.make_document(card=rs.make_card(items=2 ** 31))):
            with self.assertRaises(PclError):
                self.register(document)
        self.assertEqual(sum(self.counts().values()), 0)

    def test_the_source_must_be_a_known_one(self):
        for source in ("index", "demo", "tree", "upload"):
            path = "data/%s/%s" % (source, rs.DEMO_SESSION)
            self.register(rs.make_document(path=path), source=source)
            self.assertEqual(self.session(path)["source"], source)
        for source in ("web", "", None):
            with self.assertRaises(PclError) as caught:
                self.register(rs.make_document(path="data/other/x"), source=source)
            self.assertIn("source must be one of", caught.exception.message)

    def test_who_registered(self):
        self.register(rs.make_document(path="data/a/x"), actor=None)
        self.assertIsNone(self.session("data/a/x")["registered_by"])
        user = types.SimpleNamespace(name="demo-reviewer", role="admin")
        self.register(rs.make_document(path="data/b/x"), actor=user)
        self.assertEqual(self.session("data/b/x")["registered_by"], "demo-reviewer")
        self.assertEqual({row["user_name"] for row in self.audit()}, {None, "demo-reviewer"})

    def test_record_content_is_only_ever_data(self):
        hostile = "'); DROP TABLE sessions; --"
        card = rs.make_card(notes=hostile, sample_ids=[hostile, "100%_x"], operator=hostile,
                            extra={hostile: hostile})
        path = "data/it's 100%_here/" + rs.DEMO_SESSION
        self.assertEqual(self.register(rs.make_document(path=path, card=card))["action"], "created")
        row = self.session(path)
        self.assertEqual((row["notes"], row["operator"]), (hostile, hostile))
        self.assertEqual(row["sample_ids"], [hostile, "100%_x"])
        self.assertEqual(row["card_json"], card)
        self.assertEqual(self.register(rs.make_document(path=path, card=card))["action"],
                         "unchanged")

    # -- register(): the transaction of its own and the audit of refusals

    def test_register_commits_and_returns_the_result(self):
        result = self.register_audited()
        self.assertEqual(result["action"], "created")
        self.assertEqual(self.register_audited()["action"], "unchanged")
        self.assertEqual([row["action"] for row in self.audit()], ["created", "unchanged"])

    def test_a_refusal_is_on_record_although_everything_else_was_rolled_back(self):
        document = rs.make_document(card=rs.make_card(simulated="yes"), upload=rs.make_upload())
        with self.assertRaises(PclError) as caught:
            self.register_audited(document, actor="demo-operator-b", source="tree")
        self.assertEqual(caught.exception.code, "invalid_card")
        self.assertEqual(self.counts(), {"{pcl}.sessions": 0, "{pcl}.files": 0,
                                         "{app}.registrations": 1, "{app}.search_entries": 0})
        (row,) = self.audit()
        self.assertEqual((row["action"], row["user_name"], row["source"], row["session_path"]),
                         ("rejected", "demo-operator-b", "tree", rs.SIMULATED_PATH))
        self.assertEqual(row["detail_json"], {"reason": caught.exception.message,
                                              "code": "invalid_card"})
        self.assertIn("card.simulated", row["detail_json"]["reason"])
        self.assertEqual(len(row["doc_sha256"]), 64)

    def test_every_kind_of_refusal_of_a_well_formed_document_is_on_record(self):
        self.register_audited(rs.make_document(path=rs.OTHER_PATH))
        refusals = {
            "conflict": (rs.make_document(path=rs.OTHER_PATH, card=rs.make_card(notes="x")),
                         "already_registered"),
            "letter case": (rs.make_document(path=rs.OTHER_PATH.upper()), "case_conflict"),
            "layout": (rs.make_document(path=rs.SIMULATED_PATH,
                                        card=rs.make_card(simulated=False)), "simulated_mismatch"),
            "instrument": (rs.make_document(path="raw/other-instrument/simulated/"
                                            + rs.DEMO_SESSION), "instrument_mismatch"),
            "name": (rs.make_document(path="raw/raman-portable/simulated/other_name"),
                     "name_mismatch"),
            "files": (rs.make_document(path="data/x", files=[{"rel_path": "a", "bytes": -1,
                                                              "sha256": None}]),
                      "invalid_document"),
            "number": (rs.make_document(path="data/y", card=rs.make_card(extra={"x": NAN})),
                       "bad_number"),
            "windows": (rs.make_document(path="data/z/CON"), "bad_path"),
            "upload": (rs.make_document(path="data/w", upload=rs.make_upload(username="x")),
                       "invalid_document"),
        }
        for label, (document, code) in refusals.items():
            with self.subTest(label):
                before = self.counts()
                with self.assertRaises(PclError) as caught:
                    self.register_audited(document)
                self.assertEqual(caught.exception.code, code)
                after = self.counts()
                self.assertEqual(after["{app}.registrations"], before["{app}.registrations"] + 1)
                self.assertEqual(after["{pcl}.sessions"], before["{pcl}.sessions"])
                row = [row for row in self.audit()
                       if row["session_path"] == document["location"]["path"]
                       and row["action"] == "rejected"][-1]
                self.assertEqual(row["detail_json"]["code"], code)
                self.assertEqual(row["detail_json"]["reason"], caught.exception.message)
        self.assertEqual(self.counts()["{pcl}.sessions"], 1)

    def test_a_document_without_a_usable_path_is_refused_without_an_audit_row(self):
        broken = [None, [], "text", {}, {"location": "x"}, rs.make_document(path=5),
                  rs.make_document(path=""), rs.make_document(path="a\x00b"),
                  rs.make_document(path="a\nb"), rs.make_document(path="p" * 2000),
                  rs.make_document(path="raw/x/\ud800")]
        for document in broken:
            with self.subTest(document=repr(document)[:50]):
                with self.assertRaises(PclError):
                    self.register_audited(document)
        self.assertEqual(sum(self.counts().values()), 0)

    def test_the_reason_of_a_refusal_is_stored_as_safe_text(self):
        document = rs.make_document(path="data/x")
        document["bad\x00key\ud800"] = 1
        with self.assertRaises(PclError):
            self.register_audited(document)
        (row,) = self.audit()
        self.assertEqual(row["action"], "rejected")
        self.assertIn("unknown field", row["detail_json"]["reason"])
        self.assertNotIn("\x00", row["detail_json"]["reason"])

    def test_a_bug_is_not_recorded_as_a_refusal(self):
        with mock.patch.object(registration.search_entries, "refresh",
                               side_effect=RuntimeError("a bug")):
            with self.assertRaises(RuntimeError):
                self.register_audited()
        self.assertEqual(sum(self.counts().values()), 0)

    def test_a_database_that_is_busy_is_not_recorded_as_a_refusal(self):
        busy = Unavailable("The database is busy. Try again in a moment.", code="db_busy")
        with mock.patch.object(registration, "register_document", side_effect=busy):
            with self.assertRaises(Unavailable):
                self.register_audited()
        self.assertEqual(sum(self.counts().values()), 0)

    def test_a_failing_audit_never_hides_the_refusal(self):
        document = rs.make_document(card=rs.make_card(simulated="yes"))
        with mock.patch.object(registration, "_audit", side_effect=RuntimeError("audit broke")):
            with self.assertRaises(PclError) as caught:
                self.register_audited(document)
        self.assertEqual(caught.exception.code, "invalid_card")

    # -- the sources, end to end

    def test_a_placed_folder_is_registered(self):
        made = rs.write_placed_folder(Path(self.settings.project_dir) / "copies" / rs.DEMO_SESSION)
        document = sources.document_from_folder(made["folder"], rs.SIMULATED_PATH)
        result = self.register_audited(document)
        self.assertEqual(result["action"], "created")
        self.assertEqual(result["n_files"], len(made["sizes"]))
        self.assertEqual(result["bytes"], sum(made["sizes"].values()))
        files = self.files()
        self.assertEqual({name: row["bytes"] for name, row in files.items()}, made["sizes"])
        for name, digest in made["digests"].items():
            self.assertEqual(files[name]["sha256"], digest)
            self.assertIs(files[name]["listed"], True)
        self.assertEqual(files["SHA256SUMS"]["sha256"], made["sums_sha256"])
        self.assertEqual(self.session()["sums_sha256"], made["sums_sha256"])
        again = sources.document_from_folder(made["folder"], rs.SIMULATED_PATH)
        self.assertEqual(self.register_audited(again)["action"], "unchanged")

    def test_a_tree_is_registered(self):
        root = Path(self.settings.project_dir) / "tree"
        made = rs.write_tree(root, [("raman-portable", False, "single_20261115_101500_000"),
                                    ("raman-portable", True, rs.DEMO_SESSION),
                                    ("raman-portable", True, rs.DEMO_SESSION + "_copy2")])
        actions = {}
        for path, document in sources.documents_from_tree(root):
            actions[path] = self.register_audited(document, source="tree")["action"]
        self.assertEqual(actions, {path: "created" for path in made})
        rows = {row["path"]: row for row in self.query("SELECT * FROM {pcl}.sessions")}
        self.assertEqual({path: (row["area"], row["is_copy"], row["source"])
                          for path, row in rows.items()}, {
            "raw/raman-portable/single_20261115_101500_000": ("real", False, "tree"),
            rs.SIMULATED_PATH: ("simulated", False, "tree"),
            rs.SIMULATED_PATH + "_copy2": ("simulated", True, "tree")})

    def test_raman_uploads_are_registered(self):
        home = Path(self.settings.project_dir) / "controller"
        rs.write_raman_record(home, "u00000000000a", transport="ssh")
        rs.write_raman_record(home, "u00000000000b", transport="globus",
                              placed_as=rs.DEMO_SESSION + "_copy2",
                              checks=[{"utc": "2026-11-19T00:00:00.000Z", "ok": False,
                                       "looked": True}])
        results = {}
        for upload_id, receipt in adapters_raman.list_receipts(home):
            document = adapters_raman.document_from_receipt(
                receipt, self.settings, scan_sessions_dir=home / "scan_sessions")
            results[upload_id] = self.register_audited(document)
        first = self.session(results["u00000000000a"]["path"])
        second = self.session(results["u00000000000b"]["path"])
        self.assertEqual(first["path"], "raman_globus_upload/simulated/" + rs.DEMO_SESSION)
        self.assertEqual((first["area"], first["is_copy"], first["upload_id"]),
                         ("simulated", False, "u00000000000a"))
        self.assertEqual(first["upload_json"], {
            "transport": "ssh", "upload_id": "u00000000000a", "checked": True,
            "check_method": "sha256-on-arc", "checked_utc": "2026-11-17T03:51:47.035Z"})
        self.assertEqual(first["operator"], "demo-operator-a")             # from session.json
        self.assertEqual(first["storage_path"],
                         "/projects/pcl_photonics/raman_globus_upload/simulated/" + rs.DEMO_SESSION)
        self.assertEqual((second["is_copy"], second["folder"], second["session_name"]),
                         (True, rs.DEMO_SESSION + "_copy2", rs.DEMO_SESSION))
        self.assertIs(second["upload_json"]["checked"], False)
        self.assertEqual(second["upload_json"]["check_method"], "globus-checksum")
        self.assertEqual(first["session_id"], second["session_id"])
        entries = self.query("SELECT content_json FROM {app}.search_entries")
        text = json.dumps([first, second, entries, self.audit()], ensure_ascii=False)
        for marker in rs.PERSONAL_MARKERS.values():
            self.assertNotIn(marker, text)
        self.assertNotIn("demo-operator-a", json.dumps(entries))           # not sent to Search


# ---------------------------------------------------------------------- the command line


def arguments(**given):
    values = {"folder": None, "path": None, "card": None, "replace": False, "tree": None,
              "document": None, "raman_uploads": None, "instrument": "raman-portable",
              "scan_sessions": None, "server": None, "token_file": None}
    values.update(given)
    return argparse.Namespace(**values)


class CommandCases(StoreHelpers):

    def run_command(self, settings=None, **given):
        out = io.StringIO()
        code = registration.cmd_register(arguments(**given), settings or self.settings, out=out)
        text = out.getvalue()
        self.assertTrue(text.isascii())
        return code, text.splitlines()

    def work(self):
        return Path(self.settings.project_dir) / "work"

    def test_a_folder_with_path(self):
        made = rs.write_placed_folder(self.work() / rs.DEMO_SESSION)
        code, lines = self.run_command(folder=str(made["folder"]), path=rs.SIMULATED_PATH)
        self.assertEqual(code, 0)
        self.assertEqual(lines, [
            "created    %s  (%d file(s), %d bytes)" % (rs.SIMULATED_PATH, len(made["sizes"]),
                                                       sum(made["sizes"].values())),
            "Total: 1 session(s): 1 created, 0 updated, 0 unchanged, 0 refused."])
        row = self.session()
        self.assertEqual((row["source"], row["registered_by"]), ("upload", "local-cli"))
        code, lines = self.run_command(folder=str(made["folder"]), path=rs.SIMULATED_PATH)
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("unchanged  " + rs.SIMULATED_PATH))
        self.assertEqual(lines[-1],
                         "Total: 1 session(s): 0 created, 0 updated, 1 unchanged, 0 refused.")

    def test_a_folder_with_a_card_file(self):
        made = rs.write_placed_folder(self.work() / rs.DEMO_SESSION, with_card=False)
        code, lines = self.run_command(folder=str(made["folder"]), path=rs.SIMULATED_PATH)
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith("refused    %s: " % rs.SIMULATED_PATH))
        self.assertIn("pcl_session.json", lines[0])
        card = rs.write_bytes(self.work() / "card.json", rs.json_bytes(rs.make_card()))
        code, lines = self.run_command(folder=str(made["folder"]), path=rs.SIMULATED_PATH,
                                       card=str(card))
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("created"))

    def test_a_refusal_gives_exit_code_one_and_says_why(self):
        made = rs.write_placed_folder(self.work() / rs.DEMO_SESSION)
        code, lines = self.run_command(folder=str(made["folder"]), path=rs.REAL_PATH)
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith("refused    %s: " % rs.REAL_PATH))
        self.assertIn("simulated is true", lines[0])
        self.assertEqual(lines[-1],
                         "Total: 1 session(s): 0 created, 0 updated, 0 unchanged, 1 refused.")
        self.assertEqual([row["action"] for row in self.audit()], ["rejected"])

    def test_replace(self):
        folder = self.work() / rs.DEMO_SESSION
        rs.write_placed_folder(folder)
        self.run_command(folder=str(folder), path=rs.SIMULATED_PATH)
        rs.write_bytes(folder / "pcl_session.json", rs.json_bytes(rs.make_card(notes="changed")))
        code, lines = self.run_command(folder=str(folder), path=rs.SIMULATED_PATH)
        self.assertEqual(code, 1)
        self.assertIn("already registered with other contents", lines[0])
        code, lines = self.run_command(folder=str(folder), path=rs.SIMULATED_PATH, replace=True)
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("updated    " + rs.SIMULATED_PATH))
        self.assertEqual(self.session()["notes"], "changed")

    def test_a_tree(self):
        root = self.work() / "tree"
        rs.write_tree(root, [("raman-portable", False, "a_real"),
                             ("raman-portable", True, "c_simulated")])
        rs.write_placed_folder(root / "raw" / "raman-portable" / "b_no_card", with_card=False)
        code, lines = self.run_command(tree=str(root))
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith("created    raw/raman-portable/a_real  ("))
        self.assertTrue(lines[1].startswith("refused    raw/raman-portable/b_no_card: "))
        self.assertTrue(lines[2].startswith("created    raw/raman-portable/simulated/c_simulated"))
        self.assertEqual(lines[3],
                         "Total: 3 session(s): 2 created, 0 updated, 0 unchanged, 1 refused.")
        self.assertEqual({row["source"] for row in self.query("SELECT source FROM {pcl}.sessions")},
                         {"tree"})
        code, lines = self.run_command(tree=str(root))
        self.assertEqual(lines[-1],
                         "Total: 3 session(s): 0 created, 0 updated, 2 unchanged, 1 refused.")

    def test_an_empty_tree_registers_nothing(self):
        (self.work() / "empty" / "raw").mkdir(parents=True)
        code, lines = self.run_command(tree=str(self.work() / "empty"))
        self.assertEqual(code, 0)
        self.assertEqual(lines, ["Total: 0 session(s): 0 created, 0 updated, 0 unchanged, 0 refused.",
                                 "Nothing to register was found."])
        with self.assertRaises(PclError):
            self.run_command(tree=str(self.work() / "no such tree"))

    def test_a_document_file(self):
        document = rs.make_document(path=rs.OTHER_PATH, upload=rs.make_upload())
        path = rs.write_bytes(self.work() / "document.json", rs.json_bytes(document))
        code, lines = self.run_command(document=str(path))
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("created    " + rs.OTHER_PATH))
        self.assertEqual(self.session(rs.OTHER_PATH)["upload_json"], rs.make_upload())
        bom = rs.write_bytes(self.work() / "with_bom.json", b"\xef\xbb\xbf" + rs.json_bytes(document))
        self.assertEqual(self.run_command(document=str(bom))[0], 0)
        not_a_document = rs.write_bytes(self.work() / "list.json", b"[1, 2]\n")
        code, lines = self.run_command(document=str(not_a_document))
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith("refused    list.json: "))
        with self.assertRaises(PclError) as caught:
            self.run_command(document=str(self.work() / "missing.json"))
        self.assertIn("does not exist", caught.exception.message)
        broken = rs.write_bytes(self.work() / "broken.json", b"{")
        with self.assertRaises(PclError):
            self.run_command(document=str(broken))

    def test_raman_uploads(self):
        home = self.work() / "controller"
        rs.write_raman_record(home, "u00000000000a")
        rs.write_raman_record(home, "u00000000000b", demo=True, folder="single_20261115_101500_000")
        rs.write_raman_record(home, "u00000000000c", folder="2d_scan_20261116_120000_000",
                              arc_folder="/projects/another_lab/uploads")
        code, lines = self.run_command(raman_uploads=str(home))
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith(
            "created    raman_globus_upload/simulated/%s  (" % rs.DEMO_SESSION))
        self.assertTrue(lines[1].startswith("refused    upload u00000000000b: "))
        self.assertIn("demo upload", lines[1])
        self.assertTrue(lines[2].startswith("refused    upload u00000000000c: "))
        self.assertIn("not below the project folder", lines[2])
        self.assertEqual(lines[3],
                         "Total: 3 session(s): 1 created, 0 updated, 0 unchanged, 2 refused.")
        row = self.session("raman_globus_upload/simulated/" + rs.DEMO_SESSION)
        self.assertEqual(row["instrument"], "raman-portable")
        self.assertIsNone(row["operator"])                    # personal: only when asked for
        for marker in rs.PERSONAL_MARKERS.values():
            self.assertNotIn(marker, "\n".join(lines))

    def test_raman_uploads_again_after_a_pcldb_upgrade_are_unchanged(self):
        # COR-02: card.written_by holds pcldb's version; a new version must not turn the
        # normal re-run after an upload into "already registered with other contents".
        home = self.work() / "controller"
        rs.write_raman_record(home, "u00000000000a")
        code, lines = self.run_command(raman_uploads=str(home))
        self.assertEqual(code, 0, lines)
        path = "raman_globus_upload/simulated/" + rs.DEMO_SESSION
        before = self.session(path)
        with mock.patch.object(adapters_raman, "__version__", "99.0.0"):
            code, lines = self.run_command(raman_uploads=str(home))
        self.assertEqual(code, 0, lines)
        self.assertTrue(lines[0].startswith("unchanged  " + path), lines)
        after = self.session(path)
        self.assertEqual(after["doc_sha256"], before["doc_sha256"])
        self.assertEqual(after["card_json"], before["card_json"])
        actions = [row["action"] for row in self.query(
            "SELECT action FROM {app}.registrations WHERE session_path = ?", [path])]
        self.assertNotIn("rejected", actions)

    def test_an_upload_moved_by_hand_is_not_registered_twice(self):
        # COR-05: the same upload id at another path is a moved folder, not a new session.
        home = self.work() / "controller"
        made = rs.write_raman_record(home, "u00000000000a")
        code, lines = self.run_command(raman_uploads=str(home))
        self.assertEqual(code, 0, lines)
        record_path = Path(made["folder"]) / "record.json"
        record = json.loads(record_path.read_text(encoding="utf-8"))
        old = record["destination"]["final_path"]
        record["destination"]["final_path"] = old.replace("raman_globus_upload",
                                                          "test_folder_moved")
        record_path.write_text(json.dumps(record), encoding="utf-8")
        code, lines = self.run_command(raman_uploads=str(home))
        self.assertEqual(code, 1, lines)
        self.assertIn("moved or renamed by hand", lines[0])
        self.assertEqual([row["path"] for row in self.query(
            "SELECT path FROM {pcl}.sessions WHERE upload_id = ?", ["u00000000000a"])],
            ["raman_globus_upload/simulated/" + rs.DEMO_SESSION])

    def test_a_tree_and_the_raman_uploads_of_one_upload_in_either_order(self):
        # C2-01: tree first, then the upload's record, was refused as "moved by hand"; the
        # other order registered both. Both orders now register both.
        tree = self.work() / "tree_first"
        rs.write_tree(tree, [("raman-portable", True, rs.DEMO_SESSION)])     # DEMO_UPLOAD_ID
        code, lines = self.run_command(tree=str(tree))
        self.assertEqual(code, 0, lines)
        home = self.work() / "controller_second"
        rs.write_raman_record(home)                                           # DEMO_UPLOAD_ID
        code, lines = self.run_command(raman_uploads=str(home))
        self.assertEqual(code, 0, lines)
        self.assertTrue(lines[0].startswith("created    raman_globus_upload/simulated/"), lines)

        folder = "1d_y_scan_20261117_100000_000"
        home = self.work() / "controller_first"
        rs.write_raman_record(home, "u00000000000b", folder=folder)
        code, lines = self.run_command(raman_uploads=str(home))
        self.assertEqual(code, 0, lines)
        tree = self.work() / "tree_second"
        rs.write_tree(tree, [("raman-portable", True, folder,
                              rs.make_card(session=folder, upload_id="u00000000000b"))])
        code, lines = self.run_command(tree=str(tree))
        self.assertEqual(code, 0, lines)
        self.assertTrue(lines[0].startswith("created    raw/raman-portable/simulated/"), lines)
        for upload_id in (rs.DEMO_UPLOAD_ID, "u00000000000b"):
            self.assertEqual(len(self.query("SELECT path FROM {pcl}.sessions WHERE upload_id = ?",
                                            [upload_id])), 2, upload_id)

    def test_raman_uploads_with_another_instrument_and_the_scan_sessions(self):
        home = self.work() / "controller"
        rs.write_raman_record(home, "u00000000000a",
                              arc_folder="/projects/pcl_photonics/raw/raman-bench-2")
        code, lines = self.run_command(raman_uploads=str(home), instrument="raman-bench-2",
                                       scan_sessions=str(home / "scan_sessions"))
        self.assertEqual(code, 0, lines)
        row = self.session("raw/raman-bench-2/simulated/" + rs.DEMO_SESSION)
        self.assertEqual((row["instrument"], row["operator"]), ("raman-bench-2", "demo-operator-a"))
        code, lines = self.run_command(raman_uploads=str(home))       # the default slug
        self.assertEqual(code, 1)
        self.assertIn("instrument", lines[0])

    def test_exactly_one_source_must_be_given(self):
        for given in ({}, {"folder": "x", "path": "raw/a/b", "tree": "y"},
                      {"tree": "x", "document": "y"}, {"document": "x", "raman_uploads": "y"}):
            with self.subTest(given=given):
                with self.assertRaises(PclError) as caught:
                    self.run_command(**given)
                self.assertIn("exactly one", caught.exception.message)
        with self.assertRaises(PclError) as caught:
            self.run_command(folder="x")
        self.assertIn("--path", caught.exception.message)

    def test_lines_are_ascii_whatever_the_names_are(self):
        made = rs.write_placed_folder(self.work() / rs.DEMO_SESSION)
        path = "data/\u00b5-lab \u6e2c/" + rs.DEMO_SESSION
        code, lines = self.run_command(folder=str(made["folder"]), path=path)
        self.assertEqual(code, 0)
        self.assertIn("data/\\xb5-lab \\u6e2c/", lines[0])
        self.assertEqual(self.session(path)["path"], path)


class FakeClient:
    """Stands in for pcldb.client.Client, which the integrator writes later."""

    made = []

    def __init__(self, server_url, token):
        self.server_url, self.token, self.calls = server_url, token, []
        FakeClient.made.append(self)

    def register(self, document, replace=False):
        self.calls.append((document, replace))
        if document["card"].get("notes") == "refuse me":
            raise Conflict("The server said no.", code="already_registered")
        return {"action": "created", "path": document["location"]["path"],
                "subject": "urn:pcl:dataset:demo", "web_url": "https://app.globus.org/demo",
                "n_files": len(document["files"]), "bytes": 5}


class ServerBranchTests(unittest.TestCase):
    """`register --server URL`: the documents go through the HTTP client, not into a local
    store. The client module is replaced by a stub."""

    TOKEN = "pcl_demo-token-that-must-never-be-printed"

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.settings = Settings.for_test(self.tmp / "project")
        FakeClient.made = []
        stub = types.ModuleType("pcldb.client")
        stub.Client = FakeClient
        patcher = mock.patch.dict(sys.modules, {"pcldb.client": stub})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.token_file = rs.write_bytes(self.tmp / "token.txt", (self.TOKEN + "\n").encode())
        self.folder = rs.write_placed_folder(self.tmp / rs.DEMO_SESSION)["folder"]

    def run_command(self, settings=None, **given):
        out = io.StringIO()
        given.setdefault("server", "http://127.0.0.1:1")
        given.setdefault("token_file", str(self.token_file))
        code = registration.cmd_register(arguments(**given), settings or self.settings, out=out)
        self.assertNotIn(self.TOKEN, out.getvalue())
        return code, out.getvalue().splitlines()

    def test_documents_go_through_the_client(self):
        code, lines = self.run_command(folder=str(self.folder), path=rs.SIMULATED_PATH)
        self.assertEqual(code, 0)
        self.assertTrue(lines[0].startswith("created    " + rs.SIMULATED_PATH))
        (client,) = FakeClient.made
        self.assertEqual((client.server_url, client.token), ("http://127.0.0.1:1", self.TOKEN))
        ((document, replace),) = client.calls
        self.assertEqual(document, sources.document_from_folder(self.folder, rs.SIMULATED_PATH))
        self.assertIs(replace, False)
        self.assertFalse((self.settings.project_dir / "var").exists())   # no local store touched

    def test_replace_and_refusals(self):
        rs.write_bytes(self.folder / "pcl_session.json",
                       rs.json_bytes(rs.make_card(notes="refuse me")))
        code, lines = self.run_command(folder=str(self.folder), path=rs.SIMULATED_PATH,
                                       replace=True)
        self.assertEqual(code, 1)
        self.assertEqual(lines[0], "refused    %s: The server said no." % rs.SIMULATED_PATH)
        self.assertIs(FakeClient.made[0].calls[0][1], True)

    def test_the_server_address_may_come_from_the_settings(self):
        settings = dataclass_replace(self.settings, server_url="http://127.0.0.1:2",
                                     token="pcl_from-the-environment")
        code, _lines = self.run_command(settings, server=None, token_file=None,
                                        folder=str(self.folder), path=rs.SIMULATED_PATH)
        self.assertEqual(code, 0)
        (client,) = FakeClient.made
        self.assertEqual((client.server_url, client.token),
                         ("http://127.0.0.1:2", "pcl_from-the-environment"))

    def test_the_token_comes_from_the_variable_then_the_file_then_var(self):
        with_env = dataclass_replace(self.settings, token="pcl_from-the-environment")
        self.run_command(with_env, folder=str(self.folder), path=rs.SIMULATED_PATH)
        self.assertEqual(FakeClient.made[-1].token, "pcl_from-the-environment")
        rs.write_bytes(self.settings.var_dir / "admin_token.txt", b"pcl_from-var\n")
        self.run_command(token_file=None, folder=str(self.folder), path=rs.SIMULATED_PATH)
        self.assertEqual(FakeClient.made[-1].token, "pcl_from-var")
        self.run_command(folder=str(self.folder), path=rs.SIMULATED_PATH)
        self.assertEqual(FakeClient.made[-1].token, self.TOKEN)

    def test_without_a_token_nothing_is_sent(self):
        for token_file in (None, str(self.tmp / "missing.txt"),
                           str(rs.write_bytes(self.tmp / "empty.txt", b"\n"))):
            with self.subTest(token_file=token_file):
                with self.assertRaises(PclError) as caught:
                    self.run_command(token_file=token_file, folder=str(self.folder),
                                     path=rs.SIMULATED_PATH)
                self.assertIn("PCLDB_TOKEN", caught.exception.message)
                self.assertIn("--token-file", caught.exception.message)
        self.assertEqual(FakeClient.made, [])

    def test_usage_is_checked_before_anything_else(self):
        with self.assertRaises(PclError) as caught:
            self.run_command()
        self.assertIn("exactly one", caught.exception.message)
        self.assertEqual(FakeClient.made, [])

    def test_a_tree_through_the_server(self):
        root = self.tmp / "tree"
        rs.write_tree(root, [("raman-portable", False, "a_real"),
                             ("raman-portable", True, "b_simulated")])
        code, lines = self.run_command(tree=str(root))
        self.assertEqual(code, 0)
        self.assertEqual(len(FakeClient.made[0].calls), 2)
        self.assertEqual(lines[-1],
                         "Total: 2 session(s): 2 created, 0 updated, 0 unchanged, 0 refused.")


def dataclass_replace(settings, **changes):
    return dataclasses.replace(settings, **changes)


class NotSetUpTests(unittest.TestCase):

    def test_a_database_that_is_not_set_up(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        settings = Settings.for_test(Path(holder.name) / "project")
        made = rs.write_placed_folder(Path(holder.name) / rs.DEMO_SESSION)
        with self.assertRaises(PclError) as caught:
            registration.cmd_register(arguments(folder=str(made["folder"]),
                                                path=rs.SIMULATED_PATH), settings,
                                      out=io.StringIO())
        self.assertIn("pcldb setup", caught.exception.message)


class TestRegistrationSqlite(RegistrationCases, SqliteCase, unittest.TestCase):
    pass


class TestRegistrationPostgres(RegistrationCases, PostgresCase, unittest.TestCase):
    pass


class TestCommandSqlite(CommandCases, SqliteCase, unittest.TestCase):
    pass


class TestCommandPostgres(CommandCases, PostgresCase, unittest.TestCase):
    pass


if __name__ == "__main__":
    unittest.main()
