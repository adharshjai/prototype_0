"""Factories for the registry tests: lab cards, registration documents, placed session folders
and synthetic Raman upload record folders.

Everything here is made up. People are `demo-operator-a` and `demo-operator-b`; the values a
real upload record would hold about a person are marker texts (`PERSONAL_MARKERS`) that the
privacy test looks for. No file of a real session or a real upload record is ever copied.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path

REMOVE = object()   # make_card(field=REMOVE) leaves the field out

DEMO_SESSION = "1d_y_scan_20261116_225015_281"
DEMO_SESSION_ID = "0d0d0d0d-demo-4000-8000-000000000001"
DEMO_UPLOAD_ID = "u0123456789ab"
REAL_PATH = "raw/raman-portable/" + DEMO_SESSION
SIMULATED_PATH = "raw/raman-portable/simulated/" + DEMO_SESSION
OTHER_PATH = "raman_globus_upload/simulated/" + DEMO_SESSION

# Values that stand for personal data in a synthetic upload record. None of them may ever show
# up in a receipt, a registration document or a message.
PERSONAL_MARKERS = {
    "globus_identity": "MARKER-globus-identity@example.invalid",
    "ssh_username": "MARKERsshuser",
    "settings_ssh_username": "MARKERsettingsuser",
    "mac_collection": "0f0f0f0f-aaaa-4bbb-8ccc-0d0d0d0d0d0d",
    "demo_root": "/MARKER-demo-root/arc_demo",
    "local_final_path": "/MARKER-local-final/projects/pcl_photonics/x",
}


def sha256_hex(data):
    return hashlib.sha256(data).hexdigest()


def fake_sha(text):
    """A plausible SHA-256 for a file whose bytes do not matter in a test."""
    return sha256_hex(("fake:" + text).encode("utf-8"))


# ---------------------------------------------------------------------- cards


def make_card(**changes):
    """A valid pcl-session/1 card of a simulated Raman session; keyword arguments replace
    fields, and the value REMOVE leaves a field out."""
    card = {
        "schema": "pcl-session/1",
        "instrument": "raman-portable",
        "session": DEMO_SESSION,
        "session_id": DEMO_SESSION_ID,
        "started_utc": "2026-11-16T22:50:15.281Z",
        "ended_utc": "2026-11-16T22:51:40.072Z",
        "simulated": True,
        "sample_ids": ["demo-sample-129"],
        "operator": "demo-operator-a",
        "location": "demo-lab-1",
        "notes": "demo run, nothing was measured",
        "method": "1d-y",
        "items": 13,
        "items_planned": 13,
        "completion": "complete",
        "format": "raman-session/1 folder",
        "instrument_info": {"model": "demo-model", "serial": "demo-0001"},
        "uploaded_utc": "2026-11-17T03:50:15.281Z",
        "upload_id": DEMO_UPLOAD_ID,
        "written_by": {"name": "pcldb-tests", "version": "0.1.0"},
        "tags": ["demo"],
        "extra": {"kind": "simulated", "end": "finished"},
    }
    for name, value in changes.items():
        if value is REMOVE:
            card.pop(name, None)
        else:
            card[name] = value
    return card


def minimal_card(**changes):
    """A card with the required fields only."""
    card = make_card()
    for name in ("ended_utc", "operator", "location", "notes", "method", "items",
                 "items_planned", "instrument_info", "upload_id", "tags", "extra"):
        del card[name]
    card.update(changes)
    return card


def card_cases():
    """[(label, card, valid)]: the table the card checker is tested with.

    Every verdict, and every message, was compared once with the proof of concept's own
    checker (pcl_index validate.card_errors, run read-only): they agree on all of them.
    """
    good = [
        ("the full card", make_card()),
        ("required fields only", minimal_card()),
        ("ended_utc null", make_card(ended_utc=None)),
        ("ended_utc left out", make_card(ended_utc=REMOVE)),
        ("no sample ids", make_card(sample_ids=[])),
        ("no tags", make_card(tags=[])),
        ("a time without a fraction", make_card(started_utc="2026-11-17T03:50:15Z")),
        ("a time with nine fraction digits", make_card(started_utc="2026-11-17T03:50:15.123456789Z")),
        ("no items yet", make_card(items=0, items_planned=0)),
        ("a measured session", make_card(simulated=False)),
        ("completion partial", make_card(completion="partial")),
        ("completion failed", make_card(completion="failed")),
        ("completion unknown", make_card(completion="unknown")),
        ("notes with a newline and a tab", make_card(notes="line one\n\tline two\r\n")),
        ("a session name with dots and dashes", make_card(session="A.b-c_1")),
        ("a nested extra with a very large number", make_card(extra={"a": {"b": [1, 10 ** 30]}})),
        ("letters outside ASCII", make_card(notes="5 µm, résumé, 測試")),
        ("an empty operator", make_card(operator="")),
        ("a session id of 256 characters", make_card(session_id="s" * 256)),
        ("an instrument slug of 40 characters", make_card(instrument="a" * 40)),
        ("a one-letter upload id", make_card(upload_id="x")),
        ("a session name of 128 characters", make_card(session="s" * 128)),
        ("two sample ids", make_card(sample_ids=["129", "demo b"])),
        ("an empty instrument_info", make_card(instrument_info={})),
    ]
    bad = [
        ("a list instead of an object", [make_card()]),
        ("text instead of an object", "card"),
        ("no schema", make_card(schema=REMOVE)),
        ("another schema version", make_card(schema="pcl-session/2")),
        ("no session id", make_card(session_id=REMOVE)),
        ("no completion", make_card(completion=REMOVE)),
        ("no uploaded_utc", make_card(uploaded_utc=REMOVE)),
        ("an unknown field", make_card(colour="blue")),
        ("an instrument with capital letters", make_card(instrument="Raman-Portable")),
        ("an instrument slug of 41 characters", make_card(instrument="a" * 41)),
        ("a session named simulated", make_card(session="simulated")),
        ("a session named SiMuLaTeD", make_card(session="SiMuLaTeD")),
        ("a session name with a slash", make_card(session="a/b")),
        ("a session name that starts with an underscore", make_card(session="_x")),
        ("a session name of 129 characters", make_card(session="s" * 129)),
        ("an empty session id", make_card(session_id="")),
        ("a session id of 257 characters", make_card(session_id="s" * 257)),
        ("a time followed by a newline", make_card(started_utc="2026-11-17T03:50:15Z\n")),
        ("the 30th of February", make_card(started_utc="2026-02-30T00:00:00Z")),
        ("a time with +00:00", make_card(started_utc="2026-11-17T03:50:15+00:00")),
        ("a time with small t and z", make_card(started_utc="2026-11-17t03:50:15z")),
        ("a time with ten fraction digits", make_card(started_utc="2026-11-17T03:50:15.1234567890Z")),
        ("ended_utc as a number", make_card(ended_utc=5)),
        ("ended_utc at 24:00:00", make_card(ended_utc="2026-11-17T24:00:00Z")),
        ("uploaded_utc in month 13", make_card(uploaded_utc="2026-13-01T00:00:00Z")),
        ("simulated as text", make_card(simulated="true")),
        ("simulated null", make_card(simulated=None)),
        ("sample_ids as text", make_card(sample_ids="129")),
        ("a sample id that is a number", make_card(sample_ids=[129])),
        ("an empty sample id", make_card(sample_ids=[""])),
        ("an operator of 257 characters", make_card(operator="o" * 257)),
        ("notes of 20001 characters", make_card(notes="n" * 20001)),
        ("items as 13.0", make_card(items=13.0)),
        ("items below zero", make_card(items=-1)),
        ("items as true", make_card(items=True)),
        ("items_planned as text", make_card(items_planned="13")),
        ("a completion outside the list", make_card(completion="done")),
        ("an empty format", make_card(format="")),
        ("instrument_info as a list", make_card(instrument_info=["demo-model"])),
        ("an empty upload id", make_card(upload_id="")),
        ("an upload id of 65 characters", make_card(upload_id="u" * 65)),
        ("written_by without a version", make_card(written_by={"name": "pcldb-tests"})),
        ("written_by with an extra field", make_card(written_by={"name": "a", "version": "1", "host": "x"})),
        ("written_by with an empty name", make_card(written_by={"name": "", "version": "1"})),
        ("a tag with a capital letter", make_card(tags=["Demo"])),
        ("tags as text", make_card(tags="demo")),
        ("extra as text", make_card(extra="kind=simulated")),
        ("a NUL character in the notes", make_card(notes="a\x00b")),
        ("half of a surrogate pair in extra", make_card(extra={"deep": [{"x": "\ud800"}]})),
        ("a control character in a field name", make_card(extra={"a\x01": 1})),
        ("an escape character in the operator", make_card(operator="demo\x1boperator")),
    ]
    return [(label, card, True) for label, card in good] + \
           [(label, card, False) for label, card in bad]


# ---------------------------------------------------------------------- documents


def make_files(names=("session.json", "point_0001.json", "dark/dark_demo.txt"), with_sums=True,
               with_card=True):
    """A file list for a document: [{rel_path, bytes, sha256}] and the sums_sha256."""
    files = [{"rel_path": name, "bytes": 100 + index, "sha256": fake_sha(name)}
             for index, name in enumerate(names)]
    if with_card:
        files.append({"rel_path": "pcl_session.json", "bytes": 700,
                      "sha256": fake_sha("pcl_session.json")})
    sums = None
    if with_sums:
        sums = fake_sha("SHA256SUMS")
        files.append({"rel_path": "SHA256SUMS", "bytes": 300, "sha256": sums})
    return files, sums


def make_document(path=SIMULATED_PATH, card=None, files=None, sums_sha256="from files",
                  upload=None, **location):
    """A valid pcl-registration/1 document. `location` keyword arguments (`collection_id`,
    `project_path`) go into the location block."""
    made_files, made_sums = make_files()
    document = {
        "schema": "pcl-registration/1",
        "location": {"path": path, "collection_id": None, "project_path": None},
        "card": make_card() if card is None else card,
        "files": made_files if files is None else files,
        "sums_sha256": (made_sums if files is None else None) if sums_sha256 == "from files"
        else sums_sha256,
    }
    document["location"].update(location)
    if upload is not None:
        document["upload"] = upload
    return copy.deepcopy(document)


def make_upload(**changes):
    upload = {"transport": "ssh", "upload_id": DEMO_UPLOAD_ID, "checked": True,
              "check_method": "sha256-on-arc", "checked_utc": "2026-11-17T03:51:47.035Z"}
    upload.update(changes)
    return upload


# ---------------------------------------------------------------------- placed folders


def write_bytes(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as handle:
        handle.write(data)
    return path


def json_bytes(value):
    """A JSON file's bytes as the lab's tools write them: indent 2, UTF-8, LF, final newline."""
    return (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def walk_order(folder):
    """The relative POSIX paths of the files below `folder`, in the order SHA256SUMS lists
    them: depth first, each folder's entries sorted by name."""
    found = []

    def walk(directory, parts):
        for name in sorted(os.listdir(directory)):
            full = os.path.join(directory, name)
            if os.path.isdir(full) and not os.path.islink(full):
                walk(full, parts + [name])
            else:
                found.append("/".join(parts + [name]))

    walk(str(folder), [])
    return found


def sums_bytes(folder, leave_out=()):
    """The bytes of SHA256SUMS for a folder, in the exact format: one line per file,
    `<64 lower-case hex><two spaces><relative path>\\n`, every file except SHA256SUMS."""
    lines = []
    for relative in walk_order(folder):
        if relative == "SHA256SUMS" or relative in leave_out:
            continue
        with open(Path(folder).joinpath(*relative.split("/")), "rb") as handle:
            lines.append("%s  %s\n" % (sha256_hex(handle.read()), relative))
    return "".join(lines).encode("utf-8")


def write_placed_folder(folder, card=None, files=None, with_card=True, with_sums=True):
    """Write a placed session folder: data files, a dark/ subfolder, the card and SHA256SUMS.

    Returns {"folder", "card", "sizes": {rel: bytes}, "digests": {rel: sha256 of every file
    but SHA256SUMS}, "sums_sha256"}.
    """
    folder = Path(folder)
    if card is None:
        card = make_card(session=folder.name.split("_copy")[0])
    if files is None:
        files = {
            "session.json": json_bytes({"schema": "raman-session/1", "folder": folder.name,
                                        "operator": "demo-operator-a"}),
            "point_0001.json": json_bytes({"point_id": 1, "intensity": [1, 2, 3]}),
            "point_0002.json": json_bytes({"point_id": 2, "intensity": [4, 5, 6]}),
            "request.txt": b"demo request\n",
            "dark/dark_20261115_110940_535.txt": b"0 1\n1 2\n",
            "dark/dark_20261115_110940_535.json": json_bytes({"simulated": {"backend": True}}),
        }
    folder.mkdir(parents=True, exist_ok=True)
    for relative, data in files.items():
        write_bytes(folder.joinpath(*relative.split("/")), data)
    if with_card:
        write_bytes(folder / "pcl_session.json", json_bytes(card))
    sums_sha256 = None
    if with_sums:
        data = sums_bytes(folder)
        write_bytes(folder / "SHA256SUMS", data)
        sums_sha256 = sha256_hex(data)
    sizes, digests = {}, {}
    for relative in walk_order(folder):
        with open(folder.joinpath(*relative.split("/")), "rb") as handle:
            data = handle.read()
        sizes[relative] = len(data)
        if relative != "SHA256SUMS":
            digests[relative] = sha256_hex(data)
    return {"folder": folder, "card": card, "sizes": sizes, "digests": digests,
            "sums_sha256": sums_sha256}


def write_tree(root, sessions):
    """Write a tree laid out like ARC. `sessions` is a list of (instrument, simulated, folder)
    or (instrument, simulated, folder, card). Returns {rel_path: write_placed_folder result}."""
    made = {}
    for entry in sessions:
        instrument, simulated, name = entry[:3]
        card = entry[3] if len(entry) > 3 else make_card(
            instrument=instrument, simulated=simulated, session=name.split("_copy")[0])
        parts = ["raw", instrument] + (["simulated"] if simulated else []) + [name]
        made["/".join(parts)] = write_placed_folder(Path(root).joinpath(*parts), card=card)
    return made


# ---------------------------------------------------------------------- Raman upload records


def write_raman_record(home, upload_id=DEMO_UPLOAD_ID, *, transport="ssh", folder=DEMO_SESSION,
                       placed_as=None, area="simulated",
                       arc_folder="/projects/pcl_photonics/raman_globus_upload", demo=False,
                       state="verified", checks=(), session=None, record_changes=None,
                       manifest_changes=None, scan_session=True, operator="demo-operator-a",
                       notes="demo notes typed at the instrument"):
    """Write one synthetic upload record folder of the Raman controller under `home`.

    Made from the field list of the controller's documentation, never from a real record:
    `record.json` (raman-arc-upload/1), `raman_upload_manifest.json`
    (raman-arc-upload-manifest/1), `SHA256SUMS`, and the side files a real folder has. The
    personal values are PERSONAL_MARKERS. With `scan_session`, the session's own `session.json`
    is also written to `<home>/scan_sessions/<folder>/`, byte for byte the one "sent".

    Returns {"folder" (the record folder), "record", "manifest", "manifest_bytes",
    "sums_bytes", "final_path", "session_json" (bytes), "scan_sessions"}.
    """
    home = Path(home)
    placed_as = placed_as or folder
    base = arc_folder + ("/simulated" if area == "simulated" else "")
    final_path = base + "/" + placed_as
    session_json = json_bytes({
        "schema": "raman-session/1", "session_id": DEMO_SESSION_ID, "folder": folder,
        "scan_mode": "1d-y", "started_utc": "2026-11-16T22:50:15.281Z",
        "sample_id": "demo-sample-129", "operator": operator, "location": "demo-lab-1",
        "notes": notes, "laser_power_level": "LOW", "integration_time_us": 100000,
        "instrument": {"model": "demo-model", "serial": "demo-0001", "unit": "demo-unit",
                       "firmware": "0.0.1"},
        "simulated": {"backend": area == "simulated", "motor": area == "simulated"},
        "dark_file": "dark_20261115_110940_535.txt",
        "software": {"name": "demo-controller", "version": "0.0.1"}})
    sent = {
        "session.json": session_json,
        "point_0001.json": json_bytes({"point_id": 1, "intensity": [1, 2, 3]}),
        "point_0002.json": json_bytes({"point_id": 2, "intensity": [4, 5, 6]}),
        "request.txt": b"demo request\n",
        "response.txt": b"demo response\n",
        "dark/dark_20261115_110940_535.txt": b"0 1\n1 2\n",
        "dark/dark_20261115_110940_535.json": json_bytes({"simulated": {"backend": True}}),
    }
    session_block = {
        "folder": folder, "session_id": DEMO_SESSION_ID, "scan_mode": "1d-y",
        "started_utc": "2026-11-16T22:50:15.281Z", "sample_id": "demo-sample-129",
        "points": 13, "points_planned": 13, "end": "finished",
        "kind": "simulated" if area == "simulated" else "real",
        "kind_declared_by_uploader": False}
    session_block.update(session or {})
    manifest = {
        "schema": "raman-arc-upload-manifest/1", "upload_id": upload_id,
        "made_utc": "2026-11-17T03:51:23.412Z", "transport": transport, "demo": demo,
        "session": dict(session_block, simulated={"backend": True, "motor": True}),
        "darks": [{"file": "dark_20261115_110940_535.txt", "kind": "simulated", "sent": True,
                   "reason": None, "path": "dark/dark_20261115_110940_535.txt",
                   "description": "dark/dark_20261115_110940_535.json"},
                  {"file": "dark_missing.txt", "kind": None, "sent": False,
                   "reason": "not found", "path": None, "description": None}],
        "files": [{"path": name, "bytes": len(sent[name]), "sha256": sha256_hex(sent[name])}
                  for name in sorted(sent, key=lambda name: name.encode("utf-8"))],
        "totals": {"files": len(sent) + 2, "bytes": 0},
        "excluded": [], "adjusted": [],
        "destination": {"arc_folder": arc_folder, "area": area},
        "software": {"controller": {"name": "demo-controller", "version": "0.0.1"},
                     "data_layer": {"name": "demo-data-layer", "version": "0.0.1"}},
    }
    manifest.update(manifest_changes or {})
    manifest_bytes = json_bytes(manifest)
    digests = {entry["path"]: entry["sha256"] for entry in manifest["files"]
               if isinstance(entry, dict) and isinstance(entry.get("path"), str)}
    digests["raman_upload_manifest.json"] = sha256_hex(manifest_bytes)
    sums = "".join("%s  %s\n" % (digests[name], name)
                   for name in sorted(digests, key=lambda name: name.encode("utf-8"))
                   ).encode("utf-8")
    is_globus = transport == "globus"
    record = {
        "schema": "raman-arc-upload/1", "upload_id": upload_id,
        "created_utc": "2026-11-17T03:51:23.001Z", "updated_utc": "2026-11-17T03:51:47.050Z",
        "transport": transport, "demo": demo, "state": state,
        "state_since_utc": "2026-11-17T03:51:47.035Z",
        "message": "On ARC, checked.", "error": None, "warnings": [], "notes": [],
        "resume": None, "conflict_at": None,
        "session": session_block,
        "destination": {
            "key": "live:%s|%s" % (arc_folder, area), "arc_folder": arc_folder, "area": area,
            "base": base, "name": folder, "final_path": final_path,
            "incoming_path": "%s/_incoming/%s.%s" % (base, folder, upload_id),
            "globus_base": base[len("/projects"):] if is_globus else None,
            "globus_final_path": final_path[len("/projects"):] if is_globus else None,
            "globus_incoming_path": None,
            # A real record has these two only when it is a demo. The fixture always has
            # them, so that the privacy test sees every marker in every record.
            "demo_root": PERSONAL_MARKERS["demo_root"],
            "local_final_path": PERSONAL_MARKERS["local_final_path"],
            "new_copy": placed_as != folder, "placed_as": placed_as},
        "settings_used": {
            "folder": arc_folder, "collection_root": "/projects",
            "mac_collection": PERSONAL_MARKERS["mac_collection"],
            "arc_collection": "8e804f3a-5bac-4b6d-9836-bcd5b9c133ec" if is_globus else None,
            "client_id": "61338d24-54d5-408f-a10d-66c06b59f6d2" if is_globus else None,
            "deadline_hours": 6 if is_globus else None, "force_login": False,
            "ssh_username": PERSONAL_MARKERS["settings_ssh_username"],
            "ssh_host": "demo-host.invalid", "ssh_known_hosts": None,
            "include_simulated": True, "settle_seconds": 5},
        "payload": {"files": len(sent) + 2, "bytes": 0,
                    "manifest_sha256": sha256_hex(manifest_bytes), "kept": False},
        "progress": {"files": len(sent) + 2, "files_done": len(sent) + 2, "bytes": 0,
                     "bytes_done": 0},
        "requests": {}, "worker": None,
        "globus": {"identity": PERSONAL_MARKERS["globus_identity"], "attempt": 1,
                   "submission_id": "demo-submission", "label": "demo label",
                   "posts": [], "task_id": "demo-task", "task": {"status": "SUCCEEDED"},
                   "earlier_attempts": []},
        "ssh": {"attempt": 1, "host": "demo-host.invalid",
                "username": PERSONAL_MARKERS["ssh_username"], "known_hosts": None,
                "runs": [], "sent": True},
        "verification": ({"method": "globus-checksum", "algorithm": "SHA1", "files": 9,
                          "utc": "2026-11-17T03:51:47.035Z"} if is_globus else
                         {"method": "sha256-on-arc", "algorithm": "SHA-256", "files": 9,
                          "utc": "2026-11-17T03:51:47.035Z"}),
        "checks": list(checks),
        "demo_fault": None, "controller_idle_used": False,
    }
    for dotted, value in (record_changes or {}).items():
        target = record
        parts = dotted.split(".")
        for part in parts[:-1]:
            target = target[part]
        if value is REMOVE:
            target.pop(parts[-1], None)
        else:
            target[parts[-1]] = value
    record_folder = home / "data_catalog" / "arc_uploads" / upload_id
    write_bytes(record_folder / "record.json", json_bytes(record))
    write_bytes(record_folder / "raman_upload_manifest.json", manifest_bytes)
    write_bytes(record_folder / "SHA256SUMS", sums)
    write_bytes(record_folder / "events.jsonl",
                b'{"utc": "2026-11-17T03:51:47.035Z", "state": "verified", "text": "demo"}\n')
    write_bytes(record_folder / "worker.log", b"demo log\n")
    write_bytes(record_folder / "record.lock", b"")
    scan_sessions = home / "scan_sessions"
    if scan_session:
        write_bytes(scan_sessions / folder / "session.json", session_json)
    return {"folder": record_folder, "record": record, "manifest": manifest,
            "manifest_bytes": manifest_bytes, "sums_bytes": sums, "final_path": final_path,
            "session_json": session_json, "scan_sessions": scan_sessions}


def rewrite_record(made, **changes):
    """Change top-level fields of a written record.json (the manifest stays as it is)."""
    record = dict(made["record"])
    record.update(changes)
    write_bytes(made["folder"] / "record.json", json_bytes(record))
    return record
