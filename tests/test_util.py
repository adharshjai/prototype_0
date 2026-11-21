"""util.py: the one time format, canonical JSON, hashes, atomic writes, ports."""

from __future__ import annotations

import datetime
import json
import os
import socket
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pcldb import util
from pcldb.errors import PclError
from tests import support


class TestTime(unittest.TestCase):
    def test_every_card_form_becomes_fixed_width_milliseconds(self):
        base = "2026-11-17T03:50:15"
        digits = "123456789"
        self.assertEqual(util.utc_text(base + "Z"), "2026-11-17T03:50:15.000Z")
        for count in range(1, 10):
            text = "%s.%sZ" % (base, digits[:count])
            expected = "%s.%sZ" % (base, (digits[:count] + "000")[:3])
            self.assertEqual(util.utc_text(text), expected, text)
            self.assertEqual(len(util.utc_text(text)), 24)

    def test_it_cuts_and_never_rounds(self):
        self.assertEqual(util.utc_text("2026-11-17T03:50:15.9999Z"), "2026-11-17T03:50:15.999Z")
        self.assertEqual(util.utc_text("2026-12-31T23:59:59.999999999Z"),
                         "2026-12-31T23:59:59.999Z")
        moment = datetime.datetime(2026, 11, 19, 2, 31, 50, 999999, tzinfo=datetime.timezone.utc)
        self.assertEqual(util.utc_text(moment), "2026-11-19T02:31:50.999Z")

    def test_an_aware_datetime_is_converted_to_utc(self):
        east = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
        moment = datetime.datetime(2026, 11, 17, 9, 20, 15, 281000, tzinfo=east)
        self.assertEqual(util.utc_text(moment), "2026-11-17T03:50:15.281Z")

    def test_a_naive_datetime_is_refused_instead_of_read_as_local_time(self):
        with self.assertRaises(PclError) as caught:
            util.utc_text(datetime.datetime(2026, 11, 17, 3, 50, 15))
        self.assertIn("time zone", str(caught.exception))

    def test_other_forms_are_refused(self):
        for bad in ("2026-11-17T03:50:15+00:00", "2026-11-17 03:50:15Z", "2026-11-17t03:50:15z",
                    "2026-13-45T99:99:99Z", "2026-02-30T00:00:00Z", "2026-11-17T24:00:00Z",
                    "2026-11-17T03:50:60Z", "2026-11-17T03:50:15Z\n", "2026-11-17T03:50:15.Z",
                    "2026-11-17T03:50:15.1234567890Z", "2026-11-17", "", None, 5, 1794887440.07,
                    b"2026-11-17T03:50:15Z", "0000-01-01T00:00:00Z"):
            with self.assertRaises(PclError, msg=repr(bad)):
                util.utc_text(bad)
            with self.assertRaises(PclError, msg=repr(bad)):
                util.parse_utc(bad)

    def test_years_before_1000_keep_four_digits(self):
        self.assertEqual(util.utc_text("0999-01-02T03:04:05Z"), "0999-01-02T03:04:05.000Z")

    def test_parse_utc_gives_an_aware_datetime_and_keeps_microseconds(self):
        moment = util.parse_utc("2026-11-17T03:50:15.123456789Z")
        self.assertEqual(moment, datetime.datetime(2026, 11, 17, 3, 50, 15, 123456,
                                                   tzinfo=datetime.timezone.utc))
        self.assertEqual(moment.utcoffset(), datetime.timedelta(0))
        self.assertEqual(util.parse_utc("2026-11-17T03:50:15.2Z").microsecond, 200000)

    def test_second_text(self):
        self.assertEqual(util.second_text("2026-11-17T03:50:15.999Z"), "2026-11-17T03:50:15Z")
        self.assertEqual(util.second_text("2026-11-17T03:50:15Z"), "2026-11-17T03:50:15Z")
        moment = datetime.datetime(2026, 11, 17, 3, 50, 15, 5, tzinfo=datetime.timezone.utc)
        self.assertEqual(util.second_text(moment), "2026-11-17T03:50:15Z")
        with self.assertRaises(PclError):
            util.second_text("yesterday")

    def test_now_utc_text_has_the_fixed_form_and_is_now(self):
        before = datetime.datetime.now(datetime.timezone.utc)
        text = util.now_utc_text()
        after = datetime.datetime.now(datetime.timezone.utc)
        self.assertRegex(text, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
        moment = util.parse_utc(text)
        self.assertLessEqual(before - datetime.timedelta(milliseconds=1), moment)
        self.assertLessEqual(moment, after)

    def test_texts_sort_like_times(self):
        forms = ["2026-11-17T03:50:15Z", "2026-11-17T03:50:15.9Z", "2026-11-17T03:50:15.09Z",
                 "2026-11-17T03:50:16Z", "2025-12-31T23:59:59.999999Z"]
        texts = [util.utc_text(form) for form in forms]
        self.assertEqual(sorted(texts), [util.utc_text(moment) for moment in
                                         sorted(util.parse_utc(form) for form in forms)])


class TestJsonHashIds(unittest.TestCase):
    def test_canonical_json_is_sorted_compact_and_keeps_letters(self):
        value = {"b": [1, 2.5, None, True], "a": "caf\u00e9 \u6f22", "c": {"z": 1, "y": []}}
        self.assertEqual(util.canonical_json(value),
                         '{"a":"caf\u00e9 \u6f22","b":[1,2.5,null,true],"c":{"y":[],"z":1}}')

    def test_canonical_json_is_stable(self):
        first = {"x": 1, "y": {"b": 2, "a": [3, {"q": 1, "p": 2}]}}
        second = {"y": {"a": [3, {"p": 2, "q": 1}], "b": 2}, "x": 1}
        self.assertEqual(util.canonical_json(first), util.canonical_json(second))
        self.assertEqual(json.loads(util.canonical_json(first)), first)
        self.assertEqual(util.canonical_json("text"), '"text"')
        self.assertEqual(util.canonical_json(None), "null")

    def test_canonical_json_refuses_what_is_not_json(self):
        for bad in ({"x": float("nan")}, [1, [float("inf")]], {"deep": {"er": -float("inf")}},
                    {"x": object()}, {"x": {1, 2}}, {"a": "\udc80"}, {"\ud800": 1},
                    {1: "a", "b": 2}):
            with self.assertRaises(PclError, msg=repr(bad)):
                util.canonical_json(bad)
        loop = []
        loop.append(loop)
        with self.assertRaises(PclError):
            util.canonical_json(loop)

    def test_sha256_hex(self):
        self.assertEqual(util.sha256_hex(b""),
                         "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855")
        self.assertEqual(util.sha256_hex(b"abc"),
                         "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")
        with self.assertRaises(TypeError):
            util.sha256_hex("abc")

    def test_new_id(self):
        ids = {util.new_id() for _ in range(200)}
        self.assertEqual(len(ids), 200)
        for value in ids:
            self.assertRegex(value, r"^[0-9a-f]{32}$")

    def test_like_escape(self):
        self.assertEqual(util.like_escape(r"50%_off\now"), r"50\%\_off\\now")


class TestFiles(unittest.TestCase):
    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="pcldb util "))
        self.addCleanup(support.remove_tree, self.folder)

    def _names(self):
        return sorted(path.name for path in self.folder.rglob("*") if path.is_file())

    def test_text_is_written_exactly_with_unix_line_ends(self):
        target = self.folder / "deep" / "er" / "note.txt"
        util.write_text_atomic(target, "line one\nline two \u00e9\u6f22\n")
        self.assertEqual(target.read_bytes(), "line one\nline two \u00e9\u6f22\n".encode("utf-8"))
        self.assertNotIn(b"\r", target.read_bytes())
        self.assertEqual(self._names(), ["note.txt"])

    def test_an_existing_file_is_replaced_and_no_temporary_file_stays(self):
        target = self.folder / "state.json"
        util.write_text_atomic(target, "old")
        util.write_text_atomic(target, "new")
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertEqual(self._names(), ["state.json"])

    def test_it_goes_through_os_replace(self):
        target = self.folder / "state.json"
        util.write_text_atomic(target, "old")
        calls = []
        real = os.replace

        def spy(source, destination):
            # At this moment the old file is still whole and the new one is complete beside it.
            calls.append((Path(source).read_text(encoding="utf-8"),
                          Path(destination).read_text(encoding="utf-8"),
                          Path(source).parent == Path(destination).parent))
            real(source, destination)

        with mock.patch.object(util.os, "replace", spy):
            util.write_text_atomic(target, "new")
        self.assertEqual(calls, [("new", "old", True)])

    def test_a_busy_file_is_retried_as_on_windows(self):
        target = self.folder / "busy.txt"
        util.write_text_atomic(target, "old")
        real = os.replace
        attempts = []

        def flaky(source, destination):
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError(13, "The process cannot access the file")
            real(source, destination)

        with mock.patch.object(util.os, "replace", flaky), \
                mock.patch.object(util, "_REPLACE_WAIT_SECONDS", 0.001):
            util.write_text_atomic(target, "new")
        self.assertEqual(len(attempts), 3)
        self.assertEqual(target.read_text(encoding="utf-8"), "new")
        self.assertEqual(self._names(), ["busy.txt"])

    def test_a_file_that_stays_busy_gives_a_plain_error_and_keeps_the_old_content(self):
        target = self.folder / "busy.txt"
        util.write_text_atomic(target, "old")

        def always(_source, _destination):
            raise PermissionError(13, "The process cannot access the file")

        with mock.patch.object(util.os, "replace", always), \
                mock.patch.object(util, "_REPLACE_WAIT_SECONDS", 0.001):
            with self.assertRaises(PclError) as caught:
                util.write_text_atomic(target, "new")
        self.assertEqual(caught.exception.code, "file_in_use")
        self.assertIn("busy.txt", str(caught.exception))
        self.assertIn("open", str(caught.exception))
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertEqual(self._names(), ["busy.txt"])

    def test_the_retry_lasts_about_a_second(self):
        self.assertAlmostEqual(util._REPLACE_TRIES * util._REPLACE_WAIT_SECONDS, 1.0, delta=0.5)

    @unittest.skipIf(os.name == "nt", "Windows has no file modes")
    def test_mode_is_applied_where_the_system_has_modes(self):
        target = self.folder / "secret.json"
        util.write_json_atomic(target, {"k": "v"}, mode=0o600)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        util.write_json_atomic(target, {"k": "w"}, mode=0o600)
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_json_is_readable_utf8_with_a_final_newline(self):
        target = self.folder / "data.json"
        util.write_json_atomic(target, {"name": "caf\u00e9", "list": [1, 2]})
        raw = target.read_bytes()
        self.assertTrue(raw.endswith(b"\n"))
        self.assertNotIn(b"\r", raw)
        self.assertIn("caf\u00e9".encode("utf-8"), raw)
        self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))      # never a byte order mark
        self.assertEqual(json.loads(raw.decode("utf-8")), {"name": "caf\u00e9", "list": [1, 2]})
        self.assertEqual(raw.decode("utf-8").splitlines()[1], '  "name": "caf\u00e9",')

    def test_json_that_cannot_be_written_leaves_nothing_behind(self):
        target = self.folder / "data.json"
        with self.assertRaises(PclError):
            util.write_json_atomic(target, {"x": float("nan")})
        self.assertEqual(self._names(), [])

    def test_read_json_accepts_a_byte_order_mark(self):
        target = self.folder / "edited.json"
        target.write_bytes(b"\xef\xbb\xbf" + b'{"a": 1}')
        self.assertEqual(util.read_json(target), {"a": 1})
        target.write_bytes(b'{"a": 2}')
        self.assertEqual(util.read_json(target), {"a": 2})

    def test_read_json_problems_name_the_file(self):
        target = self.folder / "broken.json"
        with self.assertRaises(PclError) as caught:
            util.read_json(target)
        self.assertIn("broken.json", str(caught.exception))
        for content in (b"{not json", b'{"x": NaN}', b'{"x": -Infinity}', b"\xff\xfe\x00{"):
            target.write_bytes(content)
            with self.assertRaises(PclError, msg=repr(content)) as caught:
                util.read_json(target)
            self.assertIn("broken.json", str(caught.exception))


class TestErrors(unittest.TestCase):
    def test_the_base_error(self):
        from pcldb import errors
        error = PclError("Something was asked that cannot be done.")
        self.assertEqual((error.code, error.status, error.details), ("error", 400, None))
        self.assertEqual(str(error), "Something was asked that cannot be done.")
        self.assertEqual(error.message, str(error))
        self.assertEqual(error.to_dict(), {"code": "error", "details": None,
                                           "message": "Something was asked that cannot be done."})
        positional = PclError("no", "sql_refused", 422, {"position": 7})
        self.assertEqual((positional.code, positional.status, positional.details),
                         ("sql_refused", 422, {"position": 7}))
        named = PclError("no", code="sql_timeout", details=["a"])
        self.assertEqual((named.code, named.status, named.details), ("sql_timeout", 400, ["a"]))
        self.assertIsInstance(error, Exception)
        for name, status, code in (("NotFound", 404, "not_found"), ("Forbidden", 403, "forbidden"),
                                   ("Unauthorized", 401, "unauthorized"),
                                   ("Conflict", 409, "conflict"), ("TooLarge", 413, "too_large"),
                                   ("RateLimited", 429, "rate_limited"),
                                   ("Unavailable", 503, "unavailable")):
            kind = getattr(errors, name)
            self.assertTrue(issubclass(kind, PclError), name)
            made = kind("why")
            self.assertEqual((made.status, made.code, made.message), (status, code, "why"), name)
            other = kind("why", code="special", details={"k": 1})
            self.assertEqual((other.status, other.code, other.details),
                             (status, "special", {"k": 1}), name)
            with self.assertRaises(PclError):
                raise made
        self.assertEqual(PclError.status, 400)           # making one did not change the class
        self.assertEqual(errors.Unavailable.code, "unavailable")

    def test_the_version(self):
        import pcldb
        self.assertEqual(pcldb.__version__, "0.1.0")


class TestPorts(unittest.TestCase):
    def test_free_port_is_free_and_never_a_default_port(self):
        seen = set()
        for _ in range(20):
            port = util.free_port()
            self.assertNotIn(port, (8787, 15439))
            self.assertNotIn(port, seen)
            seen.add(port)
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("127.0.0.1", port))        # really free
        self.assertIs(support.free_port, util.free_port)

    def test_a_listening_port_is_in_use(self):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            self.assertTrue(util.port_answers(port))
            self.assertTrue(util.port_in_use(port))
        self.assertFalse(util.port_answers(port))
        self.assertFalse(util.port_in_use(port))


if __name__ == "__main__":
    unittest.main()
