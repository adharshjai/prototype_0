"""Tests of pcldb.sources: registration documents from session folders on this computer."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path, PurePosixPath, PureWindowsPath

from pcldb import sources
from pcldb.errors import PclError
from tests import registry_support as rs


class FolderCase(unittest.TestCase):

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def placed(self, name=rs.DEMO_SESSION, **options):
        return rs.write_placed_folder(self.tmp / name, **options)

    def by_path(self, document):
        return {entry["rel_path"]: entry for entry in document["files"]}

    def rewrite_sums(self, made, change):
        path = made["folder"] / "SHA256SUMS"
        data = change(path.read_bytes())
        rs.write_bytes(path, data)
        return data


class DocumentFromFolderTests(FolderCase):

    def test_a_placed_folder_gives_a_complete_document(self):
        made = self.placed()
        document = sources.document_from_folder(made["folder"], rs.SIMULATED_PATH)
        self.assertEqual(document["schema"], "pcl-registration/1")
        self.assertEqual(document["location"],
                         {"path": rs.SIMULATED_PATH, "collection_id": None, "project_path": None})
        self.assertEqual(document["card"], made["card"])
        self.assertEqual(document["sums_sha256"], made["sums_sha256"])
        self.assertNotIn("upload", document)
        files = self.by_path(document)
        self.assertEqual(set(files), set(made["sizes"]))
        for relative, size in made["sizes"].items():
            self.assertEqual(files[relative]["bytes"], size, relative)
        for relative, digest in made["digests"].items():
            self.assertEqual(files[relative]["sha256"], digest, relative)

    def test_the_card_and_the_dark_folder_are_listed(self):
        document = sources.document_from_folder(self.placed()["folder"], rs.SIMULATED_PATH)
        files = self.by_path(document)
        self.assertIn("pcl_session.json", files)
        self.assertIn("dark/dark_20261115_110940_535.txt", files)
        self.assertIn("dark/dark_20261115_110940_535.json", files)
        self.assertIsNotNone(files["pcl_session.json"]["sha256"])

    def test_the_entry_for_sha256sums_carries_the_hash_of_that_file(self):
        made = self.placed()
        document = sources.document_from_folder(made["folder"], rs.SIMULATED_PATH)
        entry = self.by_path(document)["SHA256SUMS"]
        self.assertEqual(entry["sha256"], made["sums_sha256"])
        self.assertEqual(entry["sha256"], document["sums_sha256"])
        self.assertEqual(entry["bytes"], made["sizes"]["SHA256SUMS"])

    def test_files_are_in_path_order_with_forward_slashes(self):
        document = sources.document_from_folder(self.placed()["folder"], rs.SIMULATED_PATH)
        paths = [entry["rel_path"] for entry in document["files"]]
        self.assertEqual(paths, sorted(paths))
        self.assertFalse(any("\\" in path for path in paths))
        self.assertTrue(all(PurePosixPath(path).as_posix() == path for path in paths))

    def test_the_sums_file_made_by_the_factory_has_the_exact_format(self):
        made = self.placed()
        lines = (made["folder"] / "SHA256SUMS").read_bytes().decode("utf-8").split("\n")
        self.assertEqual(lines[-1], "")                       # a final newline
        names = [line[66:] for line in lines[:-1]]
        self.assertTrue(all(line[64:66] == "  " and len(line) > 66 for line in lines[:-1]))
        self.assertNotIn("SHA256SUMS", names)                 # it never lists itself
        self.assertEqual(len(names), len(made["sizes"]) - 1)
        # depth first, each folder's entries by name: dark/... comes before pcl_session.json
        self.assertLess(names.index("dark/dark_20261115_110940_535.txt"),
                        names.index("pcl_session.json"))

    def test_an_extra_file_that_is_not_listed_has_no_hash(self):
        made = self.placed()
        rs.write_bytes(made["folder"] / "added_later.txt", b"not in the list\n")
        files = self.by_path(sources.document_from_folder(made["folder"], rs.SIMULATED_PATH))
        self.assertEqual(files["added_later.txt"], {"rel_path": "added_later.txt", "bytes": 16,
                                                    "sha256": None})
        self.assertIsNotNone(files["session.json"]["sha256"])

    def test_a_listed_file_that_is_missing_is_a_clear_error(self):
        made = self.placed()
        os.remove(made["folder"] / "point_0002.json")
        with self.assertRaises(PclError) as caught:
            sources.document_from_folder(made["folder"], rs.SIMULATED_PATH)
        message = caught.exception.message
        self.assertIn("point_0002.json", message)
        self.assertIn("does not hold", message)
        self.assertIn("nothing was registered", message)
        self.assertEqual(caught.exception.details["missing"], ["point_0002.json"])

    def test_hashes_come_from_the_list_and_no_file_is_hashed(self):
        made = self.placed()
        wrong = "ab" * 32
        right = made["digests"]["request.txt"]
        self.rewrite_sums(made, lambda data: data.replace(right.encode(), wrong.encode()))
        files = self.by_path(sources.document_from_folder(made["folder"], rs.SIMULATED_PATH))
        self.assertEqual(files["request.txt"]["sha256"], wrong)

    def test_crlf_line_ends_are_read(self):
        made = self.placed()
        data = self.rewrite_sums(made, lambda data: data.replace(b"\n", b"\r\n"))
        document = sources.document_from_folder(made["folder"], rs.SIMULATED_PATH)
        files = self.by_path(document)
        for relative, digest in made["digests"].items():
            self.assertEqual(files[relative]["sha256"], digest, relative)
        self.assertEqual(document["sums_sha256"], rs.sha256_hex(data))
        self.assertNotEqual(document["sums_sha256"], made["sums_sha256"])

    def test_a_byte_order_mark_makes_the_first_line_unreadable(self):
        made = self.placed()
        first = (made["folder"] / "SHA256SUMS").read_bytes().split(b"\n")[0][66:].decode()
        self.rewrite_sums(made, lambda data: b"\xef\xbb\xbf" + data)
        files = self.by_path(sources.document_from_folder(made["folder"], rs.SIMULATED_PATH))
        self.assertIsNone(files[first]["sha256"])            # shows as not listed
        others = [name for name in made["digests"] if name != first]
        self.assertTrue(all(files[name]["sha256"] == made["digests"][name] for name in others))

    def test_the_binary_marker_is_read(self):
        made = self.placed()
        self.rewrite_sums(made, lambda data: b"".join(
            line[:64] + b" *" + line[66:] + b"\n" for line in data.split(b"\n") if line))
        files = self.by_path(sources.document_from_folder(made["folder"], rs.SIMULATED_PATH))
        for relative, digest in made["digests"].items():
            self.assertEqual(files[relative]["sha256"], digest, relative)

    def test_upper_case_hex_blank_lines_dot_slash_and_no_final_newline(self):
        made = self.placed()

        def change(data):
            lines = [line for line in data.split(b"\n") if line]
            lines = [line[:64].upper() + b"  ./" + line[66:] for line in lines]
            return b"\n\n".join(lines)                        # blank lines, no final newline
        self.rewrite_sums(made, change)
        files = self.by_path(sources.document_from_folder(made["folder"], rs.SIMULATED_PATH))
        for relative, digest in made["digests"].items():
            self.assertEqual(files[relative]["sha256"], digest, relative)

    def test_without_a_sums_file_nothing_has_a_hash(self):
        made = self.placed(with_sums=False)
        document = sources.document_from_folder(made["folder"], rs.SIMULATED_PATH)
        self.assertIsNone(document["sums_sha256"])
        self.assertTrue(document["files"])
        self.assertTrue(all(entry["sha256"] is None for entry in document["files"]))

    def test_a_card_given_beside_the_folder(self):
        made = self.placed(with_card=False)
        card = rs.make_card(notes="the card from --card")
        card_path = rs.write_bytes(self.tmp / "cards" / "card.json", rs.json_bytes(card))
        document = sources.document_from_folder(made["folder"], rs.SIMULATED_PATH, card_path)
        self.assertEqual(document["card"], card)
        self.assertNotIn("pcl_session.json", self.by_path(document))
        self.assertNotIn("card.json", self.by_path(document))

    def test_a_given_card_wins_over_the_one_in_the_folder(self):
        made = self.placed()
        card = rs.make_card(notes="the given one")
        card_path = rs.write_bytes(self.tmp / "card.json", rs.json_bytes(card))
        document = sources.document_from_folder(made["folder"], rs.SIMULATED_PATH, card_path)
        self.assertEqual(document["card"]["notes"], "the given one")

    def test_a_card_saved_with_a_byte_order_mark_is_read(self):
        made = self.placed(with_card=False)
        card_path = rs.write_bytes(self.tmp / "card.json",
                                   b"\xef\xbb\xbf" + rs.json_bytes(rs.make_card()))
        document = sources.document_from_folder(made["folder"], rs.SIMULATED_PATH, card_path)
        self.assertEqual(document["card"], rs.make_card())

    def test_a_folder_without_a_card_is_refused(self):
        made = self.placed(with_card=False)
        with self.assertRaises(PclError) as caught:
            sources.document_from_folder(made["folder"], rs.SIMULATED_PATH)
        self.assertEqual(caught.exception.code, "card_missing")
        self.assertIn("pcl_session.json", caught.exception.message)
        self.assertIn("--card", caught.exception.message)

    def test_cards_that_cannot_be_read_are_refused(self):
        made = self.placed(with_card=False)
        cases = {
            "not json": b"{not json",
            "a list": b"[1, 2]\n",
            "NaN": b'{"schema": "pcl-session/1", "extra": {"x": NaN}}',
            "Infinity": b'{"extra": -Infinity}',
            "not UTF-8": b'{"notes": "\xff\xfe"}',
        }
        for label, data in cases.items():
            with self.subTest(label):
                card_path = rs.write_bytes(self.tmp / "bad_card.json", data)
                with self.assertRaises(PclError) as caught:
                    sources.document_from_folder(made["folder"], rs.SIMULATED_PATH, card_path)
                self.assertIn("bad_card.json", caught.exception.message)
        with self.assertRaises(PclError) as caught:
            sources.document_from_folder(made["folder"], rs.SIMULATED_PATH,
                                         self.tmp / "no_such_card.json")
        self.assertIn("does not exist", caught.exception.message)

    def test_hidden_files_and_operating_system_files_are_left_out(self):
        made = self.placed()
        rs.write_bytes(made["folder"] / ".DS_Store", b"finder")
        rs.write_bytes(made["folder"] / "dark" / ".hidden", b"x")
        rs.write_bytes(made["folder"] / ".cache" / "inside.txt", b"x")
        rs.write_bytes(made["folder"] / "Thumbs.db", b"x")
        rs.write_bytes(made["folder"] / "desktop.ini", b"x")
        files = self.by_path(sources.document_from_folder(made["folder"], rs.SIMULATED_PATH))
        self.assertEqual(set(files), set(made["sizes"]))

    def test_a_link_inside_the_folder_is_refused(self):
        made = self.placed()
        try:
            os.symlink(made["folder"] / "request.txt", made["folder"] / "link.txt")
        except (OSError, NotImplementedError):
            self.skipTest("this system does not let the test make a link")
        with self.assertRaises(PclError) as caught:
            sources.document_from_folder(made["folder"], rs.SIMULATED_PATH)
        self.assertIn("link.txt", caught.exception.message)
        self.assertIn("plain files only", caught.exception.message)

    def test_a_junction_is_refused_on_every_python(self):
        # WIN-08: Python 3.11 on Windows reports a junction as a plain folder; its reparse tag
        # (st_reparse_tag) still says what it is.
        import types
        from unittest import mock
        from pcldb import util
        self.assertTrue(util.is_junction(types.SimpleNamespace(
            st_reparse_tag=0xA0000003)))
        self.assertFalse(util.is_junction(types.SimpleNamespace(st_reparse_tag=0)))
        self.assertFalse(util.is_junction(os.lstat(self.tmp)))
        made = self.placed()
        inner = made["folder"] / "j"
        inner.mkdir()
        (inner / "outside.txt").write_text("not of this session", encoding="utf-8")
        number = os.lstat(inner).st_ino
        with mock.patch.object(util, "is_junction",
                               lambda info: getattr(info, "st_ino", None) == number):
            with self.assertRaises(PclError) as caught:
                sources.document_from_folder(made["folder"], rs.SIMULATED_PATH)
            self.assertIn("j", caught.exception.message)
            self.assertIn("plain files only", caught.exception.message)
            self.assertFalse(sources._is_real_folder(inner))

    def test_the_folder_must_be_a_real_folder(self):
        made = self.placed()
        with self.assertRaises(PclError):
            sources.document_from_folder(self.tmp / "no_such_folder", rs.SIMULATED_PATH)
        with self.assertRaises(PclError):
            sources.document_from_folder(made["folder"] / "request.txt", rs.SIMULATED_PATH)
        try:
            os.symlink(made["folder"], self.tmp / "link_to_session", target_is_directory=True)
        except (OSError, NotImplementedError):
            return
        with self.assertRaises(PclError) as caught:
            sources.document_from_folder(self.tmp / "link_to_session", rs.SIMULATED_PATH)
        self.assertIn("link", caught.exception.message)

    def test_a_windows_style_path_object_becomes_posix_text(self):
        made = self.placed()
        given = PureWindowsPath(r"raw\raman-portable\simulated") / rs.DEMO_SESSION
        document = sources.document_from_folder(made["folder"], given)
        self.assertEqual(document["location"]["path"], rs.SIMULATED_PATH)
        document = sources.document_from_folder(made["folder"], PurePosixPath(rs.REAL_PATH))
        self.assertEqual(document["location"]["path"], rs.REAL_PATH)

    def test_path_text_is_kept_exactly_as_given(self):
        made = self.placed()
        for text in ("raw/Raman-Portable/" + rs.DEMO_SESSION, "raw//x/", "raw\\x\\y", "./raw/x"):
            document = sources.document_from_folder(made["folder"], text)
            self.assertEqual(document["location"]["path"], text)   # registration judges it

    def test_the_folder_argument_may_be_text_or_a_path(self):
        made = self.placed()
        first = sources.document_from_folder(str(made["folder"]), rs.SIMULATED_PATH)
        second = sources.document_from_folder(made["folder"], rs.SIMULATED_PATH)
        self.assertEqual(first, second)


class PosixTextTests(unittest.TestCase):

    def test_windows_paths(self):
        self.assertEqual(sources.posix_text(PureWindowsPath(r"raw\raman-portable\x")),
                         "raw/raman-portable/x")
        self.assertEqual(sources.posix_text(PureWindowsPath("raw/raman-portable") / "sim" / "x"),
                         "raw/raman-portable/sim/x")
        self.assertEqual(sources.posix_text(PureWindowsPath(r"raw\A b\x.y")), "raw/A b/x.y")

    def test_posix_paths_and_text(self):
        self.assertEqual(sources.posix_text(PurePosixPath("raw/a/b")), "raw/a/b")
        self.assertEqual(sources.posix_text("raw/a/b"), "raw/a/b")
        self.assertEqual(sources.posix_text("Raw/A/../b"), "Raw/A/../b")


class ParseSumsTests(unittest.TestCase):
    """The reader's behaviour, row by row as the proof of concept's reader was observed."""

    A = "a" * 64
    B = "b" * 64

    def parse(self, text):
        return sources.parse_sums(text.encode("utf-8") if isinstance(text, str) else text)

    def test_the_plain_format(self):
        self.assertEqual(self.parse("%s  a.txt\n%s  dark/b.txt\n" % (self.A, self.B)),
                         ({"a.txt": self.A, "dark/b.txt": self.B}, []))

    def test_crlf_blank_lines_and_no_final_newline(self):
        self.assertEqual(self.parse("%s  a.txt\r\n\r\n   \r\n%s  b.txt" % (self.A, self.B)),
                         ({"a.txt": self.A, "b.txt": self.B}, []))

    def test_upper_case_hex_is_lowered(self):
        self.assertEqual(self.parse("%s  a.txt\n" % self.A.upper()), ({"a.txt": self.A}, []))

    def test_the_binary_marker(self):
        self.assertEqual(self.parse("%s *a.txt\n" % self.A), ({"a.txt": self.A}, []))

    def test_leading_dot_slash_is_dropped(self):
        self.assertEqual(self.parse("%s  ./a.txt\n%s  ././/dark/b.txt\n" % (self.A, self.B)),
                         ({"a.txt": self.A, "dark/b.txt": self.B}, []))

    def test_lines_that_cannot_be_read(self):
        text = "\n".join([
            "%s  /abs.txt" % self.A,            # 1: an absolute path
            "%s  up/../x.txt" % self.A,         # 2: a .. part
            "%s one_space.txt" % self.A,        # 3: one space only
            "%s  short.txt" % self.A[:63],      # 4: 63 hex digits
            "\\%s  escaped.txt" % self.A,       # 5: GNU's escaped form
            "%s  ./" % self.A,                  # 6: no name left
            "%s  good.txt" % self.B,            # 7
        ]) + "\n"
        self.assertEqual(self.parse(text), ({"good.txt": self.B}, [1, 2, 3, 4, 5, 6]))

    def test_a_byte_order_mark_spoils_line_one_only(self):
        data = b"\xef\xbb\xbf" + ("%s  a.txt\n%s  b.txt\n" % (self.A, self.B)).encode()
        self.assertEqual(self.parse(data), ({"b.txt": self.B}, [1]))

    def test_a_backslash_path_is_kept_as_text(self):
        self.assertEqual(self.parse("%s  dark\\b.txt\n" % self.A), ({"dark\\b.txt": self.A}, []))

    def test_the_later_line_wins(self):
        self.assertEqual(self.parse("%s  a.txt\n%s  a.txt\n" % (self.A, self.B)),
                         ({"a.txt": self.B}, []))

    def test_bytes_that_are_not_utf8_do_not_stop_the_reader(self):
        data = ("%s  a.txt\n" % self.A).encode() + b"\xff\xfe junk\n"
        entries, malformed = self.parse(data)
        self.assertEqual(entries, {"a.txt": self.A})
        self.assertEqual(malformed, [2])


class TreeTests(FolderCase):

    def test_real_and_simulated_areas_are_walked(self):
        made = rs.write_tree(self.tmp, [
            ("raman-portable", False, "single_20261115_101500_000"),
            ("raman-portable", True, rs.DEMO_SESSION),
            ("raman-portable", True, rs.DEMO_SESSION + "_copy2"),
            ("jeol-jsm-it800-shl", False, "demo_sem_1"),
        ])
        found = list(sources.documents_from_tree(self.tmp))
        self.assertEqual([path for path, _item in found], sorted(made))
        self.assertEqual([path for path, _item in found], [
            "raw/jeol-jsm-it800-shl/demo_sem_1",
            "raw/raman-portable/simulated/" + rs.DEMO_SESSION,
            "raw/raman-portable/simulated/" + rs.DEMO_SESSION + "_copy2",
            "raw/raman-portable/single_20261115_101500_000",
        ])
        for path, document in found:
            self.assertIsInstance(document, dict, path)
            self.assertEqual(document["location"]["path"], path)
            self.assertEqual(document["card"], made[path]["card"])
            self.assertEqual(document["sums_sha256"], made[path]["sums_sha256"])
            self.assertNotIn("\\", path)

    def test_machinery_and_hidden_names_are_skipped(self):
        rs.write_tree(self.tmp, [("raman-portable", False, "kept_session"),
                                 ("raman-portable", True, "kept_simulated")])
        raw = self.tmp / "raw"
        rs.write_placed_folder(raw / "raman-portable" / "_incoming" / "being_copied.u0123456789ab")
        rs.write_placed_folder(raw / "raman-portable" / "simulated" / "_incoming" / "x.u0123456789ab")
        rs.write_placed_folder(raw / "raman-portable" / "_set_aside")
        rs.write_placed_folder(raw / "raman-portable" / ".hidden_session")
        rs.write_placed_folder(raw / "raman-portable" / "simulated" / ".hidden_session")
        rs.write_placed_folder(raw / "_incoming" / "odd" / "session")
        rs.write_placed_folder(raw / ".trash" / "old_session")
        rs.write_placed_folder(raw / "_tools" / "session")
        rs.write_bytes(raw / "README.txt", b"read me\n")
        rs.write_bytes(raw / "raman-portable" / "README.txt", b"read me\n")
        rs.write_bytes(raw / "raman-portable" / "simulated" / "README.txt", b"read me\n")
        found = [path for path, _item in sources.documents_from_tree(self.tmp)]
        self.assertEqual(found, ["raw/raman-portable/kept_session",
                                 "raw/raman-portable/simulated/kept_simulated"])

    def test_a_folder_without_a_card_yields_an_error_and_the_walk_goes_on(self):
        rs.write_tree(self.tmp, [("raman-portable", False, "a_first"),
                                 ("raman-portable", False, "c_third")])
        rs.write_placed_folder(self.tmp / "raw" / "raman-portable" / "b_no_card", with_card=False)
        found = list(sources.documents_from_tree(self.tmp))
        self.assertEqual([path for path, _item in found],
                         ["raw/raman-portable/a_first", "raw/raman-portable/b_no_card",
                          "raw/raman-portable/c_third"])
        self.assertIsInstance(found[0][1], dict)
        self.assertIsInstance(found[1][1], PclError)
        self.assertIn("pcl_session.json", found[1][1].message)
        self.assertIsInstance(found[2][1], dict)

    def test_an_incomplete_copy_yields_an_error_and_the_walk_goes_on(self):
        made = rs.write_tree(self.tmp, [("raman-portable", True, "a_broken"),
                                        ("raman-portable", True, "b_fine")])
        os.remove(made["raw/raman-portable/simulated/a_broken"]["folder"] / "request.txt")
        found = dict(sources.documents_from_tree(self.tmp))
        self.assertIsInstance(found["raw/raman-portable/simulated/a_broken"], PclError)
        self.assertIsInstance(found["raw/raman-portable/simulated/b_fine"], dict)

    def test_a_linked_session_folder_is_not_followed(self):
        rs.write_tree(self.tmp, [("raman-portable", False, "real_session")])
        outside = rs.write_placed_folder(self.tmp / "elsewhere" / "outside_session")["folder"]
        try:
            os.symlink(outside, self.tmp / "raw" / "raman-portable" / "linked_session",
                       target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this system does not let the test make a link")
        found = [path for path, _item in sources.documents_from_tree(self.tmp)]
        self.assertEqual(found, ["raw/raman-portable/real_session"])

    def test_a_tree_without_raw_is_refused(self):
        (self.tmp / "results").mkdir()
        with self.assertRaises(PclError) as caught:
            list(sources.documents_from_tree(self.tmp))
        self.assertIn("raw", caught.exception.message)

    def test_an_empty_tree_gives_nothing(self):
        (self.tmp / "raw" / "raman-portable" / "simulated").mkdir(parents=True)
        self.assertEqual(list(sources.documents_from_tree(self.tmp)), [])

    def test_the_root_may_be_text(self):
        rs.write_tree(self.tmp, [("raman-portable", False, "one")])
        self.assertEqual([path for path, _item in sources.documents_from_tree(str(self.tmp))],
                         ["raw/raman-portable/one"])


if __name__ == "__main__":
    unittest.main()
