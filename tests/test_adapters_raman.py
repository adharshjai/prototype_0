"""Tests of pcldb.adapters_raman: upload records of the Raman controller -> receipts ->
registration documents. Every record folder is synthetic (tests/registry_support.py)."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from pcldb import adapters_raman, registration
from pcldb.config import Settings
from pcldb.errors import PclError
from tests import registry_support as rs

OTHER_ID = "ufedcba987654"
THIRD_ID = "u00000000000a"


class RecordCase(unittest.TestCase):

    def setUp(self):
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)
        self.home = self.tmp / "controller home"       # a space in the path, as on real disks
        self.settings = Settings.for_test(self.tmp / "project")

    def receipts(self):
        return dict(adapters_raman.list_receipts(self.home))

    def one_receipt(self, **options):
        made = rs.write_raman_record(self.home, **options)
        receipt = self.receipts()[made["record"]["upload_id"]]
        self.assertIsInstance(receipt, dict, receipt)
        return made, receipt

    def refusal(self, upload_id=rs.DEMO_UPLOAD_ID):
        item = self.receipts()[upload_id]
        self.assertIsInstance(item, PclError, "the record was accepted")
        self.assertIn(upload_id, item.message)
        self.assertIn("was not registered", item.message)
        return item.message

    def document(self, receipt, **options):
        return adapters_raman.document_from_receipt(receipt, self.settings, **options)


class ReceiptTests(RecordCase):

    def test_an_ssh_upload_gives_a_receipt(self):
        made, receipt = self.one_receipt(transport="ssh")
        self.assertEqual(receipt["schema"], "pcl-upload-receipt/1")
        self.assertEqual(receipt["upload_id"], rs.DEMO_UPLOAD_ID)
        self.assertEqual(receipt["transport"], "ssh")
        self.assertEqual(receipt["destination"], {
            "final_path": "/projects/pcl_photonics/raman_globus_upload/simulated/" + rs.DEMO_SESSION,
            "placed_as": rs.DEMO_SESSION, "area": "simulated"})
        self.assertEqual(receipt["verification"], {"method": "sha256-on-arc",
                                                   "algorithm": "SHA-256",
                                                   "utc": "2026-11-17T03:51:47.035Z"})
        self.assertFalse(receipt["last_check_failed"])
        self.assertEqual(receipt["made_utc"], "2026-11-17T03:51:23.412Z")
        self.assertEqual(receipt["darks_sent"], 1)
        self.assertEqual(receipt["manifest_sha256"], rs.sha256_hex(made["manifest_bytes"]))
        self.assertEqual(receipt["sums_sha256"], rs.sha256_hex(made["sums_bytes"]))

    def test_the_receipt_lists_every_file_that_was_sent(self):
        made, receipt = self.one_receipt()
        files = {entry["rel_path"]: entry for entry in receipt["files"]}
        sent = {entry["path"]: entry for entry in made["manifest"]["files"]}
        self.assertEqual(set(files), set(sent) | {"raman_upload_manifest.json", "SHA256SUMS"})
        for path, entry in sent.items():
            self.assertEqual(files[path], {"rel_path": path, "bytes": entry["bytes"],
                                           "sha256": entry["sha256"]})
        self.assertEqual(files["raman_upload_manifest.json"],
                         {"rel_path": "raman_upload_manifest.json",
                          "bytes": len(made["manifest_bytes"]),
                          "sha256": made["record"]["payload"]["manifest_sha256"]})
        self.assertEqual(files["SHA256SUMS"],
                         {"rel_path": "SHA256SUMS", "bytes": len(made["sums_bytes"]),
                          "sha256": rs.sha256_hex(made["sums_bytes"])})

    def test_a_globus_upload_gives_a_receipt(self):
        _made, receipt = self.one_receipt(transport="globus")
        self.assertEqual(receipt["transport"], "globus")
        self.assertEqual(receipt["verification"]["method"], "globus-checksum")
        self.assertEqual(receipt["verification"]["algorithm"], "SHA1")

    def test_the_walk_goes_on_past_a_bad_record(self):
        rs.write_raman_record(self.home, THIRD_ID, demo=True)
        rs.write_raman_record(self.home, rs.DEMO_UPLOAD_ID)
        rs.write_raman_record(self.home, OTHER_ID, state="placing")
        found = list(adapters_raman.list_receipts(self.home))
        self.assertEqual([upload_id for upload_id, _item in found],
                         [THIRD_ID, rs.DEMO_UPLOAD_ID, OTHER_ID])       # in name order
        self.assertIsInstance(found[0][1], PclError)
        self.assertIsInstance(found[1][1], dict)
        self.assertIsInstance(found[2][1], PclError)

    def test_names_that_are_not_uploads_are_skipped_silently(self):
        made = rs.write_raman_record(self.home)
        uploads = made["folder"].parent
        shutil.copytree(made["folder"], uploads / ".creating-abc123")
        shutil.copytree(made["folder"], uploads / (".%s" % OTHER_ID))
        shutil.copytree(made["folder"], uploads / "u123")                 # too short
        shutil.copytree(made["folder"], uploads / "uABCDEF012345")        # capital letters
        shutil.copytree(made["folder"], uploads / "x0123456789ab")
        rs.write_bytes(uploads / "requests.lock", b"")
        rs.write_bytes(uploads / THIRD_ID, b"a file with an upload's name is not a record")
        found = self.receipts()
        self.assertEqual(set(found), {rs.DEMO_UPLOAD_ID, THIRD_ID})
        self.assertIsInstance(found[rs.DEMO_UPLOAD_ID], dict)
        self.assertIsInstance(found[THIRD_ID], PclError)                 # not a folder: said so

    def test_a_home_without_upload_records_is_a_clear_error(self):
        with self.assertRaises(PclError) as caught:
            list(adapters_raman.list_receipts(self.tmp / "no such home"))
        self.assertIn("data_catalog/arc_uploads", caught.exception.message)
        (self.home / "data_catalog" / "arc_uploads").mkdir(parents=True)
        self.assertEqual(list(adapters_raman.list_receipts(self.home)), [])
        self.assertEqual(list(adapters_raman.list_receipts(str(self.home))), [])


class RefusalTests(RecordCase):

    def test_a_demo_upload_is_refused(self):
        rs.write_raman_record(self.home, demo=True)
        self.assertIn("demo upload", self.refusal())
        self.assertIn("not on ARC", self.refusal())

    def test_a_record_that_does_not_say_demo_false_is_refused(self):
        for value in (rs.REMOVE, None, 0, "false"):
            with self.subTest(value=repr(value)):
                rs.write_raman_record(self.home, record_changes={"demo": value})
                self.assertIn("demo", self.refusal())

    def test_a_state_other_than_verified_is_refused(self):
        for state in ("queued", "sending", "placing", "failed", "conflict", "stopped"):
            with self.subTest(state=state):
                rs.write_raman_record(self.home, state=state)
                self.assertIn("verified", self.refusal())

    def test_another_record_schema_is_refused(self):
        rs.write_raman_record(self.home, record_changes={"schema": "raman-arc-upload/2"})
        self.assertIn("raman-arc-upload/1", self.refusal())

    def test_an_upload_id_that_is_not_the_folder_name_is_refused(self):
        rs.write_raman_record(self.home, record_changes={"upload_id": OTHER_ID})
        self.assertIn("another upload id than the folder", self.refusal())

    def test_a_manifest_that_is_not_the_recorded_one_is_refused(self):
        made = rs.write_raman_record(self.home)
        rs.write_bytes(made["folder"] / "raman_upload_manifest.json",
                       made["manifest_bytes"] + b"\n")
        message = self.refusal()
        self.assertIn("raman_upload_manifest.json", message)
        self.assertIn("payload.manifest_sha256", message)

    def test_a_record_without_the_manifest_hash_is_refused(self):
        for changes in ({"payload": None}, {"payload.manifest_sha256": "ABC"},
                        {"payload.manifest_sha256": rs.REMOVE}):
            with self.subTest(changes=str(changes)):
                rs.write_raman_record(self.home, record_changes=changes)
                self.assertIn("payload.manifest_sha256", self.refusal())

    def test_another_manifest_schema_is_refused(self):
        rs.write_raman_record(self.home,
                              manifest_changes={"schema": "raman-arc-upload-manifest/2"})
        self.assertIn("raman-arc-upload-manifest/1", self.refusal())

    def test_a_manifest_of_another_upload_is_refused(self):
        rs.write_raman_record(self.home, manifest_changes={"upload_id": OTHER_ID})
        self.assertIn("another upload id than the record", self.refusal())

    def test_a_sums_file_that_does_not_match_the_manifest_is_refused(self):
        made = rs.write_raman_record(self.home)
        lines = made["sums_bytes"].split(b"\n")[:-1]
        variants = {
            "a line is missing": b"\n".join(lines[1:]) + b"\n",
            "the manifest's line is missing": b"\n".join(
                line for line in lines if not line.endswith(b"raman_upload_manifest.json")) + b"\n",
            "an extra file": made["sums_bytes"] + ("%s  extra.txt\n" % ("c" * 64)).encode(),
            "another digest": ("%s  %s\n" % ("d" * 64, lines[0][66:].decode())).encode()
            + b"\n".join(lines[1:]) + b"\n",
            "a line that cannot be read": made["sums_bytes"] + b"not a checksum line\n",
            "empty": b"",
        }
        for label, data in variants.items():
            with self.subTest(label):
                rs.write_bytes(made["folder"] / "SHA256SUMS", data)
                message = self.refusal()
                self.assertIn("SHA256SUMS", message)
                self.assertIn("manifest", message)
        rs.write_bytes(made["folder"] / "SHA256SUMS", made["sums_bytes"])
        self.assertIsInstance(self.receipts()[rs.DEMO_UPLOAD_ID], dict)

    def test_a_missing_file_is_refused(self):
        for name in ("record.json", "raman_upload_manifest.json", "SHA256SUMS"):
            with self.subTest(name=name):
                made = rs.write_raman_record(self.home)
                os.remove(made["folder"] / name)
                self.assertIn(name, self.refusal())

    def test_a_record_that_is_not_a_json_object_is_refused(self):
        made = rs.write_raman_record(self.home)
        for data in (b"{broken", b"[1, 2, 3]\n", b'{"schema": NaN}', b"\xff\xfe"):
            with self.subTest(data=data):
                rs.write_bytes(made["folder"] / "record.json", data)
                self.assertIn("record.json", self.refusal())

    def test_a_linked_record_folder_is_refused(self):
        made = rs.write_raman_record(self.tmp / "elsewhere")
        uploads = self.home / "data_catalog" / "arc_uploads"
        uploads.mkdir(parents=True)
        try:
            os.symlink(made["folder"], uploads / rs.DEMO_UPLOAD_ID, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("this system does not let the test make a link")
        self.assertIn("link", self.refusal())

    def test_linked_files_inside_a_record_folder_are_refused(self):
        for name in ("record.json", "raman_upload_manifest.json", "SHA256SUMS"):
            with self.subTest(name=name):
                shutil.rmtree(self.home, ignore_errors=True)    # the link of the last round
                made = rs.write_raman_record(self.home)
                real = self.tmp / ("real_" + name)
                os.replace(made["folder"] / name, real)
                try:
                    os.symlink(real, made["folder"] / name)
                except (OSError, NotImplementedError):
                    self.skipTest("this system does not let the test make a link")
                message = self.refusal()
                self.assertIn(name, message)
                self.assertIn("link", message)

    def test_a_record_without_a_place_is_refused(self):
        cases = {
            "destination.placed_as": None, "destination.final_path": None,
            "destination": None,
        }
        for dotted, value in cases.items():
            with self.subTest(dotted=dotted):
                rs.write_raman_record(self.home, record_changes={dotted: value})
                self.assertIn("destination", self.refusal())
        rs.write_raman_record(self.home, record_changes={"destination.final_path": "relative/path"})
        self.assertIn("absolute path", self.refusal())
        rs.write_raman_record(self.home, record_changes={
            "destination.final_path": "/projects/pcl_photonics/x/../../other/" + rs.DEMO_SESSION})
        self.assertIn("absolute path", self.refusal())
        rs.write_raman_record(self.home, record_changes={"destination.placed_as": "another_name"})
        self.assertIn("does not end with", self.refusal())
        rs.write_raman_record(self.home, record_changes={"destination.area": "elsewhere"})
        self.assertIn("real or the simulated area", self.refusal())

    def test_a_record_without_a_verification_is_refused(self):
        rs.write_raman_record(self.home, record_changes={"verification": None})
        self.assertIn("verification", self.refusal())
        rs.write_raman_record(self.home, record_changes={"verification.method": "looked-ok"})
        self.assertIn("way of checking", self.refusal())

    def test_other_shapes_that_are_refused(self):
        cases = {
            "transport": ("carrier-pigeon", "transport"),
            "session": (None, "session"),
            "session.folder": ("../../etc", "session name"),
        }
        for dotted, (value, expected) in cases.items():
            with self.subTest(dotted=dotted):
                rs.write_raman_record(self.home, record_changes={dotted: value})
                self.assertIn(expected, self.refusal())

    def test_manifest_file_lists_that_cannot_be_used_are_refused(self):
        good = {"path": "session.json", "bytes": 3, "sha256": "a" * 64}
        cases = {
            "not a list": "files",
            "an absolute path": [dict(good, path="/etc/passwd")],
            "a path that climbs": [dict(good, path="../outside.txt")],
            "a backslash": [dict(good, path="dark\\x.txt")],
            "listed twice": [good, good],
            "the manifest itself": [dict(good, path="raman_upload_manifest.json")],
            "no size": [dict(good, bytes=None)],
            "a negative size": [dict(good, bytes=-1)],
            "upper-case digest": [dict(good, sha256="A" * 64)],
            "not an entry": ["session.json"],
        }
        for label, files in cases.items():
            with self.subTest(label):
                rs.write_raman_record(self.home, manifest_changes={"files": files})
                self.assertIn("manifest", self.refusal())


class DocumentTests(RecordCase):

    def test_the_document_of_an_ssh_upload(self):
        made, receipt = self.one_receipt(transport="ssh")
        document = self.document(receipt)
        self.assertEqual(document["schema"], "pcl-registration/1")
        self.assertEqual(document["location"], {
            "path": "raman_globus_upload/simulated/" + rs.DEMO_SESSION,
            "collection_id": None, "project_path": None})
        self.assertEqual(document["card"], {
            "schema": "pcl-session/1", "instrument": "raman-portable",
            "session": rs.DEMO_SESSION, "session_id": rs.DEMO_SESSION_ID,
            "started_utc": "2026-11-16T22:50:15.281Z", "simulated": True,
            "sample_ids": ["demo-sample-129"], "method": "1d-y", "items": 13,
            "items_planned": 13, "completion": "complete", "format": "raman-session/1 folder",
            "uploaded_utc": "2026-11-17T03:51:23.412Z", "upload_id": rs.DEMO_UPLOAD_ID,
            "written_by": {"name": "pcldb", "version": "0.1.0"},
            "extra": {"kind": "simulated", "end": "finished",
                      "kind_declared_by_uploader": False, "darks_sent": 1}})
        self.assertEqual(registration.validate_card(document["card"]), [])
        self.assertEqual(document["files"], receipt["files"])
        self.assertEqual(document["sums_sha256"], rs.sha256_hex(made["sums_bytes"]))
        self.assertEqual(document["upload"], {
            "transport": "ssh", "upload_id": rs.DEMO_UPLOAD_ID, "checked": True,
            "check_method": "sha256-on-arc", "checked_utc": "2026-11-17T03:51:47.035Z"})

    def test_the_document_passes_every_check_of_registration(self):
        for transport in ("ssh", "globus"):
            with self.subTest(transport=transport):
                _made, receipt = self.one_receipt(transport=transport)
                parts = registration.check_document(self.document(receipt), self.settings)
                self.assertEqual(parts["area"], "simulated")
                self.assertFalse(parts["is_copy"])
                self.assertEqual(parts["where"]["storage_path"],
                                 "/projects/pcl_photonics/raman_globus_upload/simulated/"
                                 + rs.DEMO_SESSION)

    def test_the_document_of_a_globus_upload(self):
        _made, receipt = self.one_receipt(transport="globus")
        upload = self.document(receipt)["upload"]
        self.assertEqual(upload["transport"], "globus")
        self.assertEqual(upload["check_method"], "globus-checksum")
        self.assertTrue(upload["checked"])

    def test_a_real_session_goes_to_the_real_area(self):
        _made, receipt = self.one_receipt(area="real")
        document = self.document(receipt)
        self.assertEqual(document["location"]["path"], "raman_globus_upload/" + rs.DEMO_SESSION)
        self.assertFalse(document["card"]["simulated"])
        self.assertEqual(registration.check_document(document, self.settings)["area"], "real")

    def test_a_folder_placed_as_a_copy(self):
        _made, receipt = self.one_receipt(placed_as=rs.DEMO_SESSION + "_copy2")
        document = self.document(receipt)
        self.assertEqual(document["location"]["path"],
                         "raman_globus_upload/simulated/%s_copy2" % rs.DEMO_SESSION)
        self.assertEqual(document["card"]["session"], rs.DEMO_SESSION)   # never the copy's name
        parts = registration.check_document(document, self.settings)
        self.assertTrue(parts["is_copy"])
        self.assertEqual(parts["folder"], rs.DEMO_SESSION + "_copy2")

    def test_an_upload_into_the_lab_layout(self):
        _made, receipt = self.one_receipt(arc_folder="/projects/pcl_photonics/raw/raman-portable")
        document = self.document(receipt)
        self.assertEqual(document["location"]["path"], rs.SIMULATED_PATH)
        registration.check_document(document, self.settings)

    def test_another_instrument_slug(self):
        _made, receipt = self.one_receipt()
        self.assertEqual(self.document(receipt, instrument="raman-bench-2")["card"]["instrument"],
                         "raman-bench-2")

    def test_last_check_failed_means_not_checked(self):
        checks = [{"utc": "2026-11-18T00:00:00.000Z", "ok": True, "looked": True},
                  {"utc": "2026-11-19T00:00:00.000Z", "ok": False, "looked": True,
                   "message": "one file is missing"}]
        _made, receipt = self.one_receipt(checks=checks)
        self.assertTrue(receipt["last_check_failed"])
        upload = self.document(receipt)["upload"]
        self.assertFalse(upload["checked"])
        self.assertEqual(upload["check_method"], "sha256-on-arc")

    def test_a_check_that_could_not_look_says_nothing(self):
        checks = [{"utc": "2026-11-18T00:00:00.000Z", "ok": False, "looked": True},
                  {"utc": "2026-11-19T00:00:00.000Z", "ok": True, "looked": True},
                  {"utc": "2026-11-20T00:00:00.000Z", "ok": False, "looked": False}]
        _made, receipt = self.one_receipt(checks=checks)
        self.assertFalse(receipt["last_check_failed"])
        self.assertTrue(self.document(receipt)["upload"]["checked"])

    def test_last_check_failed_rule(self):
        failed = adapters_raman.last_check_failed
        self.assertFalse(failed([]))
        self.assertFalse(failed(None))
        self.assertFalse(failed("checks"))
        self.assertFalse(failed([{"ok": False, "looked": False}]))
        self.assertTrue(failed([{"ok": False}]))                      # looked is not false
        self.assertTrue(failed([{"ok": True, "looked": True}, {"ok": False, "looked": True}]))
        self.assertFalse(failed([{"ok": False, "looked": True}, {"ok": True, "looked": True}]))
        self.assertTrue(failed([{"ok": False, "looked": True}, "odd", {"looked": False}]))

    def test_completion_follows_how_the_session_ended(self):
        expected = {"finished": "complete", "stopped": "partial", "failed": "failed",
                    "ended": "unknown", "something-new": "unknown", None: "unknown"}
        for end, completion in expected.items():
            with self.subTest(end=end):
                _made, receipt = self.one_receipt(session={"end": end})
                card = self.document(receipt)["card"]
                self.assertEqual(card["completion"], completion)
                self.assertEqual(card["extra"]["end"], end)
                self.assertEqual(registration.validate_card(card), [])

    def test_sample_ids(self):
        for sample_id, expected in ((129, ["129"]), ("  demo b ", ["demo b"]), ("", []),
                                    ("   ", []), (None, []), (True, []), (["a"], [])):
            with self.subTest(sample_id=sample_id):
                _made, receipt = self.one_receipt(session={"sample_id": sample_id})
                self.assertEqual(self.document(receipt)["card"]["sample_ids"], expected)

    def test_values_the_record_does_not_have_are_left_out_of_the_card(self):
        _made, receipt = self.one_receipt(session={"scan_mode": None, "points": None,
                                                   "points_planned": None})
        card = self.document(receipt)["card"]
        for name in ("method", "items", "items_planned"):
            self.assertNotIn(name, card)
        self.assertEqual(registration.validate_card(card), [])

    def test_a_record_without_a_start_time_is_a_clear_error(self):
        for value in (None, "", "yesterday", "2026-02-30T00:00:00Z"):
            with self.subTest(value=value):
                _made, receipt = self.one_receipt(session={"started_utc": value})
                with self.assertRaises(PclError) as caught:
                    self.document(receipt)
                self.assertIn(rs.DEMO_UPLOAD_ID, caught.exception.message)
                self.assertIn("no start time", caught.exception.message)
                self.assertIn("started_utc", caught.exception.message)

    def test_a_record_without_a_session_id_or_copy_time_is_an_error(self):
        _made, receipt = self.one_receipt(session={"session_id": None})
        with self.assertRaises(PclError) as caught:
            self.document(receipt)
        self.assertIn("no session id", caught.exception.message)
        _made, receipt = self.one_receipt(manifest_changes={"made_utc": None})
        with self.assertRaises(PclError) as caught:
            self.document(receipt)
        self.assertIn("made_utc", caught.exception.message)

    def test_a_folder_outside_the_project_is_an_error(self):
        _made, receipt = self.one_receipt(arc_folder="/projects/another_lab/uploads")
        with self.assertRaises(PclError) as caught:
            self.document(receipt)
        self.assertIn("not below the project folder", caught.exception.message)
        self.assertIn("storage.project_path", caught.exception.message)
        _made, receipt = self.one_receipt(arc_folder="/projects/pcl_photonics_other/uploads")
        with self.assertRaises(PclError):
            self.document(receipt)

    def test_the_project_folder_comes_from_the_settings(self):
        _made, receipt = self.one_receipt(arc_folder="/projects/another_lab/uploads")
        settings = self.settings.changed({"storage.project_path": "/projects/another_lab"})
        document = adapters_raman.document_from_receipt(receipt, settings)
        self.assertEqual(document["location"]["path"], "uploads/simulated/" + rs.DEMO_SESSION)


class EnrichmentTests(RecordCase):

    PERSONAL = ("operator", "location", "notes", "instrument_info")

    def test_without_the_scan_sessions_folder_nothing_personal_is_taken(self):
        _made, receipt = self.one_receipt()
        card = self.document(receipt)["card"]
        for name in self.PERSONAL:
            self.assertNotIn(name, card)

    def test_the_session_file_is_read_when_it_is_the_one_that_was_sent(self):
        made, receipt = self.one_receipt()
        card = self.document(receipt, scan_sessions_dir=made["scan_sessions"])["card"]
        self.assertEqual(card["operator"], "demo-operator-a")
        self.assertEqual(card["location"], "demo-lab-1")
        self.assertEqual(card["notes"], "demo notes typed at the instrument")
        self.assertEqual(card["instrument_info"], {"model": "demo-model", "serial": "demo-0001",
                                                   "unit": "demo-unit", "firmware": "0.0.1"})
        self.assertEqual(registration.validate_card(card), [])
        self.assertEqual(self.document(receipt, scan_sessions_dir=str(made["scan_sessions"]))["card"],
                         card)

    def test_a_session_file_that_changed_is_never_read(self):
        made, receipt = self.one_receipt()
        changed = json.loads(made["session_json"])
        changed["operator"] = "demo-operator-b"
        rs.write_bytes(made["scan_sessions"] / rs.DEMO_SESSION / "session.json",
                       rs.json_bytes(changed))
        card = self.document(receipt, scan_sessions_dir=made["scan_sessions"])["card"]
        for name in self.PERSONAL:
            self.assertNotIn(name, card)
        self.assertNotIn("demo-operator-b", json.dumps(card))

    def test_a_missing_or_linked_session_file_is_not_read(self):
        made, receipt = self.one_receipt(scan_session=False)
        self.assertNotIn("operator", self.document(
            receipt, scan_sessions_dir=made["scan_sessions"])["card"])
        self.assertNotIn("operator", self.document(
            receipt, scan_sessions_dir=self.tmp / "no such folder")["card"])
        real = rs.write_bytes(self.tmp / "session_elsewhere.json", made["session_json"])
        target = made["scan_sessions"] / rs.DEMO_SESSION / "session.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(real, target)
        except (OSError, NotImplementedError):
            return
        self.assertNotIn("operator", self.document(
            receipt, scan_sessions_dir=made["scan_sessions"])["card"])

    def test_values_that_do_not_fit_a_card_are_left_out(self):
        made, receipt = self.one_receipt(operator="o" * 300, notes="")
        card = self.document(receipt, scan_sessions_dir=made["scan_sessions"])["card"]
        self.assertNotIn("operator", card)          # longer than a card allows
        self.assertNotIn("notes", card)             # empty
        self.assertEqual(card["location"], "demo-lab-1")
        self.assertEqual(registration.validate_card(card), [])

    def test_a_copy_reads_the_session_file_of_the_original_folder(self):
        made, receipt = self.one_receipt(placed_as=rs.DEMO_SESSION + "_copy3")
        card = self.document(receipt, scan_sessions_dir=made["scan_sessions"])["card"]
        self.assertEqual(card["operator"], "demo-operator-a")


class PrivacyTests(RecordCase):
    """A record holds the uploader's Globus identity, ARC user name, this computer's collection
    id and local paths. None of them may reach a receipt, a document or a message."""

    def assert_clean(self, value, what):
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        for name, marker in rs.PERSONAL_MARKERS.items():
            self.assertNotIn(marker, text, "%s holds the record's %s" % (what, name))
            self.assertNotIn(marker.lower(), text.lower())

    def test_the_fixture_really_holds_every_marker(self):
        made = rs.write_raman_record(self.home)
        text = (made["folder"] / "record.json").read_text(encoding="utf-8")
        for marker in rs.PERSONAL_MARKERS.values():
            self.assertIn(marker, text)
        record = made["record"]
        self.assertEqual(record["globus"]["identity"], rs.PERSONAL_MARKERS["globus_identity"])
        self.assertEqual(record["ssh"]["username"], rs.PERSONAL_MARKERS["ssh_username"])
        self.assertEqual(record["settings_used"]["ssh_username"],
                         rs.PERSONAL_MARKERS["settings_ssh_username"])
        self.assertEqual(record["settings_used"]["mac_collection"],
                         rs.PERSONAL_MARKERS["mac_collection"])
        self.assertEqual(record["destination"]["demo_root"], rs.PERSONAL_MARKERS["demo_root"])
        self.assertEqual(record["destination"]["local_final_path"],
                         rs.PERSONAL_MARKERS["local_final_path"])

    def test_no_receipt_and_no_document_holds_a_personal_value(self):
        for transport in ("ssh", "globus"):
            for placed_as in (None, rs.DEMO_SESSION + "_copy2"):
                with self.subTest(transport=transport, placed_as=placed_as):
                    made, receipt = self.one_receipt(transport=transport, placed_as=placed_as)
                    self.assert_clean(receipt, "the receipt")
                    self.assert_clean(self.document(receipt), "the document")
                    self.assert_clean(self.document(
                        receipt, scan_sessions_dir=made["scan_sessions"]), "the enriched document")

    def test_no_refusal_message_holds_a_personal_value(self):
        refusals = [
            dict(demo=True), dict(state="failed"),
            dict(record_changes={"schema": "raman-arc-upload/9"}),
            dict(record_changes={"upload_id": OTHER_ID}),
            dict(record_changes={"destination.placed_as": None}),
            dict(record_changes={"verification": None}),
            dict(record_changes={"transport": rs.PERSONAL_MARKERS["ssh_username"]}),
            dict(record_changes={"destination.area": rs.PERSONAL_MARKERS["demo_root"]}),
            dict(record_changes={"session.folder": rs.PERSONAL_MARKERS["local_final_path"]}),
            dict(record_changes={"verification.method": rs.PERSONAL_MARKERS["globus_identity"]}),
            dict(manifest_changes={"upload_id": rs.PERSONAL_MARKERS["mac_collection"]}),
        ]
        for options in refusals:
            with self.subTest(options=str(options)[:60]):
                rs.write_raman_record(self.home, **options)
                item = self.receipts()[rs.DEMO_UPLOAD_ID]
                self.assertIsInstance(item, PclError)
                self.assert_clean(item.message, "the refusal")
                self.assert_clean(item.details, "the refusal's details")

    def test_unknown_keys_of_the_record_are_not_carried_along(self):
        made = rs.write_raman_record(self.home, record_changes={
            "session.typed_by": rs.PERSONAL_MARKERS["ssh_username"],
            "destination.ssh_target": rs.PERSONAL_MARKERS["ssh_username"] + "@demo-host.invalid:/x",
            "verification.by": rs.PERSONAL_MARKERS["globus_identity"]})
        receipt = self.receipts()[made["record"]["upload_id"]]
        self.assert_clean(receipt, "the receipt")
        self.assert_clean(self.document(receipt), "the document")


if __name__ == "__main__":
    unittest.main()
