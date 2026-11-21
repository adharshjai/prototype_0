"""Tests of the keyword rules (pcldb.ask_rules): a table of questions with the plan each one
must give. No network, no database: `now` and the dictionary are passed in."""

from __future__ import annotations

import datetime
import random
import socket
import unittest
from unittest import mock

from pcldb import ask, ask_rules

EASTERN = datetime.timezone(datetime.timedelta(hours=-5))
NOW = datetime.datetime(2026, 11, 20, 15, 30, tzinfo=EASTERN)       # a Friday

DICTIONARY = {
    "values": {
        "instrument": ["raman-portable", "jeol-jsm-it800-shl", "sem-benchtop"],
        "method": ["1d_y_scan", "2d_xy_map", "single_point"],
        "completion": ["complete", "partial", "failed", "unknown"],
        "review_state": ["none", "pending", "done"],
        "labels": ["adenine", "methylene_blue", "r6g", "UNKNOWN"],
        "tags": ["demo", "calibration", "week-46"],
        "models": ["CNN3_1_pure_only"],
        "sample_ids": ["130", "131", "VS-001", "polystyrene bead A"],
    },
}

NEWEST = {"field": "started", "order": "desc"}
OLDEST = {"field": "started", "order": "asc"}


def any_of(field, *values):
    return {"field": field, "op": "any", "values": list(values)}


def all_of(field, *values):
    return {"field": field, "op": "all", "values": list(values)}


def none_of(field, *values):
    return {"field": field, "op": "none", "values": list(values)}


def between(field, low, high):
    return {"field": field, "op": "range", "from": low, "to": high}


def expect(filters=(), keywords="", sort=NEWEST, limit=10, count_by=None):
    return {"keywords": keywords, "filters": list(filters), "sort": sort, "limit": limit,
            "count_by": count_by}


RAMAN = any_of("instrument", "raman-portable")

# (question, the plan it must give). NOW is Friday 2026-11-20, 15:30, UTC-05:00.
CASES = [
    # sample ids
    ("sample 130", expect([any_of("sample_ids", "130")])),
    ("show me sessions of sample id 131 please", expect([any_of("sample_ids", "131")])),
    ("anything with VS-001?", expect([any_of("sample_ids", "VS-001")])),
    ("sessions for vs-001", expect([any_of("sample_ids", "VS-001")])),
    ("samples 130 and 131", expect([any_of("sample_ids", "130", "131")])),
    ("sample X-77b", expect([any_of("sample_ids", "X-77b")])),
    ("polystyrene bead a scans", expect([any_of("sample_ids", "polystyrene bead A")])),
    # labels, with spaces for underscores and in any letter case
    ("methylene blue", expect([any_of("labels", "methylene_blue")])),
    ("sessions with Methylene_Blue", expect([any_of("labels", "methylene_blue")])),
    ("R6G sessions", expect([any_of("labels", "r6g")])),
    ("adenine or r6g", expect([any_of("labels", "adenine", "r6g")])),
    ("sessions with adenine and r6g", expect([all_of("labels", "adenine", "r6g")])),
    ("sessions without adenine", expect([none_of("labels", "adenine")])),
    ("which sessions do not contain methylene blue",
     expect([none_of("labels", "methylene_blue")])),
    ("unknown labels", expect([any_of("labels", "UNKNOWN")])),
    # instruments: the slug, the slug with spaces, a loose name
    ("raman-portable", expect([RAMAN])),
    ("raman portable sessions", expect([RAMAN])),
    ("raman", expect([RAMAN])),
    ("what did we run on the sem", expect([any_of("instrument", "sem-benchtop")])),
    ("jeol images", expect([any_of("instrument", "jeol-jsm-it800-shl")], keywords="images",
                           sort=None)),
    ("everything except raman", expect([none_of("instrument", "raman-portable")])),
    # COR-03: "but", "other than" and spelled-out contractions negate too
    ("everything but raman", expect([none_of("instrument", "raman-portable")])),
    ("all sessions other than raman", expect([none_of("instrument", "raman-portable")])),
    ("sessions that haven't been reviewed", expect([any_of("review_state", "pending")])),
    ("sessions that weren\u2019t simulated", expect([any_of("simulated", False)])),
    ("raman sessions that aren't failed", expect([RAMAN, none_of("completion", "failed")])),
    ("scans that did not finish", expect([none_of("completion", "complete")])),
    ("runs where r6g was not detected", expect([none_of("labels", "r6g")])),
    ("sessions in which adenine has not been found", expect([none_of("labels", "adenine")])),
    # K-03: common verbs are no search words
    ("which sessions found adenine", expect([any_of("labels", "adenine")])),
    ("which runs detected r6g", expect([any_of("labels", "r6g")])),
    ("please list sessions where r6g was identified", expect([any_of("labels", "r6g")])),
    ("show sessions that showed adenine", expect([any_of("labels", "adenine")])),
    ("show me data where r6g appears", expect([any_of("labels", "r6g")])),
    ("raman scans that didn't fail this month",
     expect([RAMAN, none_of("completion", "failed"),
             between("started", "2026-11-01", "2026-11-30")])),
    # other known values
    ("1d y scan sessions", expect([any_of("method", "1d_y_scan")])),
    ("sessions tagged calibration", expect([any_of("tags", "calibration")])),
    ("cnn3 1 pure only", expect([any_of("models", "CNN3_1_pure_only")])),
    # dates
    ("sessions today", expect([between("started", "2026-11-20", "2026-11-20")])),
    ("raman sessions yesterday", expect([RAMAN, between("started", "2026-11-19", "2026-11-19")])),
    ("this week", expect([between("started", "2026-11-16", "2026-11-22")])),
    ("adenine last week", expect([any_of("labels", "adenine"),
                                  between("started", "2026-11-09", "2026-11-15")])),
    ("sessions this month", expect([between("started", "2026-11-01", "2026-11-30")])),
    ("last month", expect([between("started", "2026-10-01", "2026-10-31")])),
    ("r6g in the last 7 days", expect([any_of("labels", "r6g"),
                                       between("started", "2026-11-13", None)])),
    ("past 2 weeks", expect([between("started", "2026-11-06", None)])),
    # COR-07: hours are exact (NOW is 15:30 at UTC-05:00, so 20:30 UTC)
    ("raman scans uploaded in the last 24 hours",
     expect([RAMAN, between("uploaded", "2026-11-19T20:30:00Z", None)])),
    ("sessions of the past 3 hours", expect([between("started", "2026-11-20T17:30:00Z", None)])),
    ("since 2026-11-01", expect([between("started", "2026-11-01", None)])),
    ("sessions after 2026-11-10", expect([between("started", "2026-11-11", None)])),
    ("before November 13", expect([between("started", None, "2026-11-12")])),
    ("sessions in November", expect([between("started", "2026-11-01", "2026-11-30")])),
    ("in december", expect([between("started", "2025-12-01", "2025-12-31")])),
    ("October 2026 on the sem", expect([between("started", "2026-10-01", "2026-10-31"),
                                        any_of("instrument", "sem-benchtop")])),
    ("on 13 November 2026", expect([between("started", "2026-11-13", "2026-11-13")])),
    ("between 2026-11-01 and 2026-11-10",
     expect([between("started", "2026-11-01", "2026-11-10")])),
    ("from 11/01/2026 to 11/15/2026", expect([between("started", "2026-11-01", "2026-11-15")])),
    ("since monday", expect([between("started", "2026-11-16", None)])),
    ("3 days ago", expect([between("started", "2026-11-17", "2026-11-17")])),
    ("sessions in 2025", expect([between("started", "2025-01-01", "2025-12-31")])),
    ("uploaded yesterday", expect([between("uploaded", "2026-11-19", "2026-11-19")])),
    ("may I see the sessions in may", expect([between("started", "2026-05-01", "2026-05-31")])),
    # simulated and real
    ("simulated sessions", expect([any_of("simulated", True)])),
    ("real sessions only", expect([any_of("simulated", False)])),
    ("raman sessions that are not simulated", expect([RAMAN, any_of("simulated", False)])),
    # completion words
    ("failed sessions", expect([any_of("completion", "failed")])),
    ("partial runs this month", expect([any_of("completion", "partial"),
                                        between("started", "2026-11-01", "2026-11-30")])),
    ("completed raman scans", expect([any_of("completion", "complete"), RAMAN])),
    ("incomplete sessions", expect([none_of("completion", "complete")])),
    # review
    ("which sessions need review", expect([any_of("review_state", "pending")])),
    ("sessions that still need to be reviewed", expect([any_of("review_state", "pending")])),
    ("unreviewed adenine", expect([any_of("review_state", "pending"),
                                   any_of("labels", "adenine")])),
    ("reviewed sessions", expect([any_of("review_state", "done")])),
    # other fixed words
    ("sessions with problems", expect([{"field": "problems", "op": "exists"}])),
    ("sessions without problems", expect([{"field": "problems", "op": "missing"}])),
    ("unclassified raman sessions", expect([{"field": "labels", "op": "missing"}, RAMAN])),
    # counting
    ("how many sessions per instrument", expect(count_by="instrument")),
    ("count by label", expect(count_by="labels")),
    ("how many sessions failed in October 2026",
     expect([any_of("completion", "failed"), between("started", "2026-10-01", "2026-10-31")])),
    ("number of sessions for each sample this week",
     expect([between("started", "2026-11-16", "2026-11-22")], count_by="sample_ids")),
    ("how many labels did raman find", expect([RAMAN], count_by="labels")),
    ("sessions by tag", expect(count_by="tags")),
    # newest, oldest, how many to show
    ("newest sessions", expect()),
    ("what was the latest session", expect(limit=1)),
    ("oldest raman session", expect([RAMAN], sort=OLDEST, limit=1)),
    ("the 3 most recent failed sessions", expect([any_of("completion", "failed")], limit=3)),
    ("last 5 sessions", expect(limit=5)),
    ("top 20 sessions with adenine", expect([any_of("labels", "adenine")], limit=20)),
    ("first three sessions", expect(sort=OLDEST, limit=3)),
    ("show 500 sessions", expect(limit=50)),
    ("largest sessions", expect(sort={"field": "bytes", "order": "desc"})),
    # nothing recognisable: keywords only
    ("polystyrene fluorescence", expect(keywords="polystyrene fluorescence", sort=None)),
    ("anything about the blue laser alignment?",
     expect(keywords="blue laser alignment", sort=None)),
    ("show me the sessions", expect()),
    ("", expect()),
]


class TestRulesTable(unittest.TestCase):
    def test_the_table_is_big_enough(self):
        self.assertGreaterEqual(len(CASES), 25)
        self.assertEqual(len({question for question, _ in CASES}), len(CASES))

    def test_every_question_gives_its_plan(self):
        for question, expected in CASES:
            with self.subTest(question=question):
                plan = ask_rules.plan(question, DICTIONARY, NOW)
                self.assertEqual(plan.pop("explanation"), ask_rules.EXPLANATION)
                self.assertEqual(plan, expected)

    def test_every_plan_compiles_to_a_request(self):
        fields = ask.plan_fields()
        for question, _ in CASES:
            with self.subTest(question=question):
                request = ask.compile_plan(ask_rules.plan(question, DICTIONARY, NOW), fields,
                                           tz=EASTERN)
                self.assertEqual(request["@version"], "query#1.0.0")

    def test_the_same_question_always_gives_the_same_plan(self):
        for question, _ in CASES:
            first = ask_rules.plan(question, DICTIONARY, NOW)
            self.assertEqual(ask_rules.plan(question, DICTIONARY, NOW), first)

    def test_no_network_is_touched(self):
        with mock.patch.object(socket, "socket", side_effect=AssertionError("network used")), \
                mock.patch.object(socket, "create_connection",
                                  side_effect=AssertionError("network used")):
            for question, _ in CASES:
                ask_rules.plan(question, DICTIONARY, NOW)


class TestRulesDetails(unittest.TestCase):
    def plan(self, question, dictionary=DICTIONARY, now=NOW):
        plan = ask_rules.plan(question, dictionary, now)
        plan.pop("explanation")
        return plan

    def test_now_decides_what_today_means(self):
        late = datetime.datetime(2026, 12, 31, 23, 59, tzinfo=EASTERN)
        self.assertEqual(self.plan("today", now=late)["filters"],
                         [between("started", "2026-12-31", "2026-12-31")])
        self.assertEqual(self.plan("this week", now=late)["filters"],
                         [between("started", "2026-12-28", "2027-01-03")])
        january = datetime.datetime(2027, 1, 2, 9, 0, tzinfo=EASTERN)
        self.assertEqual(self.plan("last month", now=january)["filters"],
                         [between("started", "2026-12-01", "2026-12-31")])
        self.assertEqual(self.plan("in november", now=january)["filters"],
                         [between("started", "2026-11-01", "2026-11-30")])
        leap = datetime.datetime(2028, 3, 31, 12, 0, tzinfo=EASTERN)
        self.assertEqual(self.plan("last 1 months", now=leap)["filters"],
                         [between("started", "2028-02-29", None)])

    def test_the_local_day_is_used_not_the_utc_day(self):
        # 23:30 on the 20th in Blacksburg is already the 21st in UTC.
        evening = datetime.datetime(2026, 11, 20, 23, 30, tzinfo=EASTERN)
        self.assertEqual(self.plan("today", now=evening)["filters"],
                         [between("started", "2026-11-20", "2026-11-20")])
        as_utc = evening.astimezone(datetime.timezone.utc)
        self.assertEqual(self.plan("today", now=as_utc)["filters"],
                         [between("started", "2026-11-21", "2026-11-21")])

    def test_now_must_be_a_datetime(self):
        with self.assertRaises(TypeError):
            ask_rules.plan("today", DICTIONARY, "2026-11-20")
        naive = ask_rules.plan("today", DICTIONARY, datetime.datetime(2026, 11, 20, 12, 0))
        self.assertEqual(naive["filters"], [between("started", "2026-11-20", "2026-11-20")])

    def test_two_date_phrases_narrow_one_range(self):
        self.assertEqual(self.plan("after 2026-11-01 and before 2026-11-10")["filters"],
                         [between("started", "2026-11-02", "2026-11-09")])
        # a second phrase that contradicts the first is dropped, not turned into nonsense
        self.assertEqual(self.plan("before 2026-11-01 since 2026-11-10")["filters"],
                         [between("started", None, "2026-10-31")])

    def test_far_dates_stay_inside_the_years_the_rules_use(self):
        self.assertEqual(self.plan("last 9999 months")["filters"],
                         [between("started", "1971-01-01", None)])
        self.assertEqual(self.plan("after 2200-12-31")["filters"],
                         [between("started", "2200-12-31", None)])
        self.assertEqual(self.plan("before 1971-01-01")["filters"],
                         [between("started", None, "1971-01-01")])
        for question in ("in 1950", "since 0001-01-01", "on 12/31/9999"):
            self.assertEqual(self.plan(question)["filters"], [], question)
        odd_now = datetime.datetime(1, 1, 1, 0, 0, tzinfo=datetime.timezone.utc)
        for question in ("today", "this month", "last year", "last 3 days", "in may"):
            ask_rules.plan(question, DICTIONARY, odd_now)

    def test_a_date_that_does_not_exist_is_left_as_a_word(self):
        plan = self.plan("sessions on 2026-02-30")
        self.assertEqual(plan["filters"], [])
        self.assertEqual(plan["keywords"], "2026-02-30")

    def test_a_bare_number_is_not_a_sample(self):
        plan = self.plan("130")
        self.assertEqual(plan["filters"], [])
        self.assertEqual(plan["keywords"], "130")

    def test_a_value_inside_a_longer_word_is_not_matched(self):
        for question in ("adenine-like spectra", "sub-raman things", "methylene"):
            with self.subTest(question=question):
                self.assertEqual(self.plan(question)["filters"], [])

    def test_an_everyday_word_in_the_dictionary_is_not_a_value(self):
        odd = {"values": {"tags": ["sessions", "the", "last", "week", "show"],
                          "labels": ["with", "top"], "instrument": ["the-new-one"]}}
        plan = self.plan("show the last sessions with new data", odd)
        self.assertEqual(plan["filters"], [])

    def test_a_loose_name_needs_a_slug_that_has_it(self):
        self.assertEqual(self.plan("sem images")["filters"],
                         [any_of("instrument", "sem-benchtop")])
        no_sem = {"values": {"instrument": ["raman-portable", "jeol-jsm-it800-shl"]}}
        plan = self.plan("sem images", no_sem)
        self.assertEqual((plan["filters"], plan["keywords"]), ([], "sem images"))
        two = {"values": {"instrument": ["raman-portable", "raman-benchtop", "sem-benchtop"]}}
        self.assertEqual(self.plan("raman", two)["filters"],
                         [any_of("instrument", "raman-benchtop", "raman-portable")])
        self.assertEqual(self.plan("benchtop but not raman", two)["filters"], [
            any_of("instrument", "raman-benchtop", "sem-benchtop"),
            none_of("instrument", "raman-benchtop", "raman-portable")])

    def test_without_a_dictionary_the_fixed_words_still_work(self):
        for dictionary in (None, {}, {"values": {}}, {"values": None}):
            plan = self.plan("failed raman sessions this week", dictionary)
            self.assertEqual(plan["filters"], [any_of("completion", "failed"),
                                               between("started", "2026-11-16", "2026-11-22")])
            self.assertEqual(plan["keywords"], "raman")

    def test_fields_the_dictionary_does_not_list_are_not_used(self):
        narrow = dict(DICTIONARY, fields=[{"name": "instrument"}, {"name": "started"}])
        plan = self.plan("failed adenine raman sessions per label today", narrow)
        self.assertEqual(plan["filters"], [RAMAN, between("started", "2026-11-20", "2026-11-20")])
        self.assertIsNone(plan["count_by"])

    def test_a_known_location_is_a_filter(self):
        # NEW-05: "demo-bench-1" was three loose words, and "1" matched every Raman entry.
        known = {"values": dict(DICTIONARY["values"],
                                location=["demo-bench-1", "demo-bench-2"])}
        for question in ("sessions at demo-bench-1", "what ran at demo bench 1"):
            with self.subTest(question=question):
                plan = ask_rules.plan(question, known, NOW)
                self.assertEqual((plan["keywords"], plan["filters"]),
                                 ("", [any_of("location", "demo-bench-1")]))

    def test_a_negated_word_that_is_no_known_value_is_left_out_with_a_notice(self):
        # COR-03: "not from the SEM" must never become a search FOR "SEM".
        known = {"values": dict(DICTIONARY["values"], instrument=["raman-portable"])}
        for question in ("sessions not from the SEM", "sessions without the SEM lab",
                         "everything but sem", "anything other than SEM"):
            with self.subTest(question=question):
                plan, notices = ask_rules.plan_with_notices(question, known, NOW)
                self.assertEqual(plan["keywords"], "")
                self.assertEqual(len(notices), 1)
                self.assertIn("left out of the search", notices[0])
        plan, notices = ask_rules.plan_with_notices("polystyrene not from the SEM", known, NOW)
        self.assertEqual((plan["keywords"], len(notices)), ("polystyrene", 1))
        self.assertIn('"SEM"', notices[0])
        self.assertEqual(ask_rules.plan_with_notices("polystyrene", known, NOW)[1], [])
        plan, notices = ask_rules.plan_with_notices("adenine is not what we want", known, NOW)
        self.assertEqual(plan["keywords"], "")
        self.assertEqual(len(notices), 1)
        self.assertIn('The word "not" was not understood', notices[0])
        collected = []
        ask._rules_request("sessions not from the SEM", known, NOW, ask.plan_fields(), EASTERN,
                           collected)
        self.assertEqual(len(collected), 1)

    def test_withdrawn_and_copies_become_filters_that_the_server_will_overrule(self):
        self.assertEqual(self.plan("withdrawn sessions")["filters"], [any_of("withdrawn", True)])
        self.assertEqual(self.plan("copies")["filters"], [any_of("is_copy", True)])

    def test_limits_of_the_plan_language_are_kept(self):
        many = " ".join("sample %d" % number for number in range(200, 260))
        plan = self.plan(many)
        self.assertLessEqual(len(plan["filters"][0]["values"]), ask_rules.MAX_VALUES)
        long_words = " ".join("word%03d" % number for number in range(400))
        plan = self.plan(long_words)
        self.assertLessEqual(len(plan["keywords"]), ask_rules.MAX_KEYWORD_CHARS)
        self.assertTrue(plan["keywords"].startswith("word000 word001"))
        everything = ("failed simulated corrected unreviewed raman adenine sample 130 demo "
                      "1d y scan cnn3 1 pure only with problems today uploaded yesterday "
                      "verified today withdrawn copies")
        self.assertLessEqual(len(self.plan(everything)["filters"]), ask_rules.MAX_FILTERS)

    def test_odd_input_never_raises(self):
        for question in (None, 5, "   ", "?!.,;", "\x00\x1b[31m", "a" * 5000, "' OR 1=1 --",
                         "‮evil", "sample", "between and", "last", "top", "since",
                         "per", "by by by", "how many how many", "-1 days ago",
                         "last 99999999 days", "9999-99-99", "0000-00-00", "13/13/2026",
                         "sample " + "9" * 300, "{\"filters\": []}", "after 12/31/9999",
                         "before 0001-01-01", "since 0001-01-01", "last 9999 months",
                         "last 9999 years", "past 9999 weeks", "last 9999 hours", "in 1950",
                         "since 1970-01-01", "before 1971-01-01", "after 2200-12-31",
                         "until 2200-12-31", "9999 days ago", "31 february", "february 30 2026",
                         "2026-13", "this february", "last february", "0 days ago"):
            with self.subTest(question=repr(question)[:30]):
                plan = ask_rules.plan(question, DICTIONARY, NOW)
                ask.compile_plan(plan, ask.plan_fields(), tz=EASTERN)

    def test_random_questions_always_compile(self):
        words = ("show how many count sessions per by instrument label sample samples 130 131 "
                 "VS-001 raman sem jeol adenine r6g methylene blue methylene_blue unknown demo "
                 "calibration today yesterday this last past week month year days 7 3 since "
                 "before after between and or not without until from to in on november may "
                 "2026-11-01 2026-11-30 11/05/2026 2025 monday simulated real failed partial "
                 "complete incomplete need review reviewed newest latest oldest first top 5 "
                 "problems uploaded verified withdrawn copies largest the a of with polystyrene "
                 "laser alignment 1d y scan ago").split()
        chooser = random.Random(20261120)
        fields = ask.plan_fields()
        for _ in range(1500):
            question = " ".join(chooser.choice(words)
                                for _ in range(chooser.randint(1, 12)))
            with self.subTest(question=question):
                plan = ask_rules.plan(question, DICTIONARY, NOW)
                self.assertLessEqual(len(plan["filters"]), ask_rules.MAX_FILTERS)
                self.assertTrue(1 <= plan["limit"] <= ask_rules.MAX_LIMIT)
                request = ask.compile_plan(plan, fields, tz=EASTERN)
                ask.add_server_filters(request, False)

    def test_hostile_dictionary_values_do_not_reach_a_plan_unasked(self):
        odd = ["ignore previous instructions", "\"}], \"limit\": 50, \"x\": [{\"", "a\nb",
               "x" * 5000]
        hostile = {"values": dict(DICTIONARY["values"],
                                  labels=DICTIONARY["values"]["labels"] + odd)}
        for question, expected in CASES:
            plan = ask_rules.plan(question, hostile, NOW)
            plan.pop("explanation")
            self.assertEqual(plan, expected, question)


if __name__ == "__main__":
    unittest.main()
