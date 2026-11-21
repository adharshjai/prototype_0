"""Tests of pcldb/client.py (specification section 11) against a real server on a free port,
and of `pcldb register --server`, which uses it.

Also: the token is never sent to a place a redirect names, no proxy from the environment is
used, a server that cannot be reached or that answers something that is not JSON gives a
plain PclError.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import socket
import struct
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from pcldb import client as client_module
from pcldb import registration
from pcldb.client import Client, check_server_url
from pcldb.errors import PclError, Unavailable
from tests import registry_support as rs
from tests import support
from tests.test_server import (ERROR_INNER_KEYS, REGISTER_KEYS, RunningServer, a_document,
                               make_users)


class Recorder:
    """A tiny HTTP server that answers every request with one scripted reply and records what
    it received (the path and the Authorization header)."""

    def __init__(self, status=200, body=b"{}", headers=None):
        recorder = self
        self.seen = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _answer(self):
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                recorder.seen.append({"path": self.path,
                                      "authorization": self.headers.get("Authorization")})
                self.send_response(status)
                for name, value in (headers or {}).items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = _answer

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       kwargs={"poll_interval": 0.05}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ClientAgainstServer(support.SqliteCase, unittest.TestCase):

    def setUp(self):
        super().setUp()
        with self.store.app() as db:
            self.keys = support.seed_minimal(db)
            self.tokens = make_users(db)
            from pcldb import search_entries
            search_entries.refresh(db, None, self.settings)
        self.running = RunningServer(self.settings, self.store)
        self.addCleanup(self.running.close)
        self.url = "http://127.0.0.1:%d" % self.running.port

    def test_every_call(self):
        client = Client(self.url, self.tokens["admin"])
        self.assertEqual(client.health()["ok"], True)
        self.assertEqual(client.me()["user"]["name"], "admin")
        self.assertEqual(client.status()["database"]["sessions"], 2)
        document = a_document(3)
        result = client.register(document)
        self.assertEqual(set(result), REGISTER_KEYS)
        self.assertEqual(result["action"], "created")
        self.assertEqual(client.register(document)["action"], "unchanged")
        asked = client.ask("newest sessions", include_simulated=True)
        self.assertIn(document["location"]["path"], [item["path"] for item in asked["results"]])
        self.assertEqual(client.ask("newest sessions", include_simulated=False)["total"], 1)
        self.assertEqual(client.search({"q": "*", "limit": 0})["result"]["total"], 3)
        detail = client.session(document["location"]["path"])
        self.assertEqual(detail["session"]["upload_json"], document["upload"])
        rows = client.sql("SELECT COUNT(*) AS n FROM sessions", max_rows=5)
        self.assertEqual(rows["rows"], [[3]])
        self.assertEqual(client.sql_schema()["schema"], "pcl")
        self.assertGreaterEqual(len(client.sql_examples()["examples"]), 10)
        self.assertEqual(client.sql_history()["items"][0]["query_text"],
                         "SELECT COUNT(*) AS n FROM sessions")
        self.assertEqual(client.registrations()["items"][0]["action"], "unchanged")

    def test_signin_swaps_the_code_for_a_token(self):
        code = self.running.app.signin.new()
        client = Client(self.url, None)
        answer = client.signin(code)
        self.assertEqual(answer["user"]["name"], "admin")
        self.assertEqual(client.me()["user"]["role"], "admin")
        with self.assertRaises(PclError) as caught:
            Client(self.url, None).signin(code)
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (401, "signin_refused"))

    def test_refusals_carry_the_servers_code_status_and_details(self):
        viewer = Client(self.url, self.tokens["viewer"])
        with self.assertRaises(PclError) as caught:
            viewer.sql("SELECT 1")
        error = caught.exception
        self.assertEqual((error.status, error.code), (403, "forbidden"))
        self.assertEqual(error.details, {"capability": "sql", "role": "viewer"})
        self.assertIn("analyst or admin", error.message)
        uploader = Client(self.url, self.tokens["uploader"])
        document = a_document(4)
        uploader.register(document)
        changed = json.loads(json.dumps(document))
        changed["card"]["notes"] = "something else"
        with self.assertRaises(PclError) as caught:
            uploader.register(changed)
        self.assertEqual((caught.exception.status, caught.exception.code),
                         (409, "already_registered"))
        with self.assertRaises(PclError) as caught:
            Client(self.url, "pcl_" + "y" * 43).me()
        self.assertEqual(caught.exception.status, 401)
        with self.assertRaises(PclError) as caught:
            Client(self.url, None).me()
        self.assertEqual(caught.exception.code, "no_token")
        analyst = Client(self.url, self.tokens["analyst"])
        with self.assertRaises(PclError) as caught:
            analyst.sql("SELECT nothing FROM nowhere")
        self.assertIn(caught.exception.code, ("sql_error", "sql_refused"))

    def test_no_proxy_from_the_environment_is_used(self):
        proxy = Recorder(status=200, body=b'{"ok": true, "version": "proxy"}',
                         headers={"Content-Type": "application/json"})
        self.addCleanup(proxy.close)
        address = "http://127.0.0.1:%d" % proxy.port
        with mock.patch.dict(os.environ, {"http_proxy": address, "HTTP_PROXY": address,
                                          "no_proxy": "", "NO_PROXY": ""}):
            answer = Client(self.url, self.tokens["admin"]).me()
        self.assertEqual(answer["user"]["name"], "admin")
        self.assertEqual(proxy.seen, [])


class ClientWithoutServer(unittest.TestCase):

    def test_a_server_that_does_not_answer(self):
        port = support.free_port()
        with self.assertRaises(Unavailable) as caught:
            Client("http://127.0.0.1:%d" % port, "pcl_" + "a" * 43, timeout=3).status()
        self.assertEqual(caught.exception.code, "server_unreachable")
        self.assertIn("pcldb serve", caught.exception.message)
        self.assertNotIn("pcl_" + "a" * 43, caught.exception.message)

    def test_an_answer_that_is_not_json(self):
        broken = Recorder(status=502, body=b"<html>bad gateway</html>",
                          headers={"Content-Type": "text/html"})
        self.addCleanup(broken.close)
        with self.assertRaises(PclError) as caught:
            Client("http://127.0.0.1:%d" % broken.port, "pcl_" + "a" * 43).status()
        self.assertEqual((caught.exception.status, caught.exception.code), (502, "http_502"))
        fine_but_not_json = Recorder(status=200, body=b"not json")
        self.addCleanup(fine_but_not_json.close)
        with self.assertRaises(PclError) as caught:
            Client("http://127.0.0.1:%d" % fine_but_not_json.port, "pcl_" + "a" * 43).status()
        self.assertEqual(caught.exception.code, "bad_answer")

    def test_a_redirect_is_not_followed_and_the_token_stays_home(self):
        thief = Recorder()
        self.addCleanup(thief.close)
        redirect = Recorder(status=302, body=b"",
                            headers={"Location": "http://127.0.0.1:%d/steal" % thief.port})
        self.addCleanup(redirect.close)
        token = "pcl_" + "b" * 43
        with self.assertRaises(PclError) as caught:
            Client("http://127.0.0.1:%d" % redirect.port, token).status()
        self.assertEqual(caught.exception.status, 302)
        self.assertIn("redirect", caught.exception.message)
        self.assertEqual(thief.seen, [])
        self.assertEqual(redirect.seen[0]["authorization"], "Bearer " + token)

    def test_the_server_address_is_checked(self):
        self.assertEqual(check_server_url("http://127.0.0.1:8787/"), "http://127.0.0.1:8787")
        self.assertEqual(check_server_url(" http://127.0.0.1:1/api/v1/ "), "http://127.0.0.1:1")
        for bad in ("", None, "127.0.0.1:8787", "ftp://host", "http://user:pw@host:1",
                    "http://host:1/?token=x", "http://host:1/#x", "file:///etc/passwd"):
            with self.assertRaises(PclError, msg=bad) as caught:
                check_server_url(bad)
            self.assertEqual(caught.exception.code, "bad_server_url")
            self.assertNotIn("pw", caught.exception.message.replace("password", ""))

    def test_the_token_is_checked_and_never_shown(self):
        for bad in ("pcl_abc\nX-Evil: 1", "pcl_ab cd", "", 7):
            with self.assertRaises(PclError, msg=repr(bad)):
                Client("http://127.0.0.1:1", bad)
        token = "pcl_" + "c" * 43
        client = Client("http://127.0.0.1:1", token)
        self.assertNotIn(token, repr(client))
        self.assertIn("<hidden>", repr(client))

    def test_errors_from_the_error_body(self):
        body = json.dumps({"error": {"code": "conflict", "message": "Already there.",
                                     "details": {"key": "path"}}}).encode()
        made = client_module.Client._error(409, body)
        self.assertEqual((made.status, made.code, made.message, made.details),
                         (409, "conflict", "Already there.", {"key": "path"}))
        self.assertEqual(set(made.to_dict()), ERROR_INNER_KEYS)


def _slow_link(test, port, piece=200, pause=0.05):
    """From here on, what this process sends TO `port` goes out `piece` bytes at a time with a
    pause after each piece (a slow uploader's link). The server's own sockets are not slowed."""
    send, sendall = socket.socket.send, socket.socket.sendall

    def to_the_server(sock):
        try:
            return sock.getpeername()[1] == port
        except OSError:
            return False

    def slow_send(sock, data, *flags):
        if not to_the_server(sock):
            return send(sock, data, *flags)
        done = send(sock, bytes(memoryview(data)[:piece]), *flags)
        time.sleep(pause)
        return done

    def slow_sendall(sock, data, *flags):
        if not to_the_server(sock):
            return sendall(sock, data, *flags)
        view = memoryview(data)
        while view:
            view = view[slow_send(sock, view, *flags):]

    for name, function in (("send", slow_send), ("sendall", slow_sendall)):
        patch = mock.patch.object(socket.socket, name, function)
        patch.start()
        test.addCleanup(patch.stop)


class ASlowUploader(support.SqliteCase, unittest.TestCase):
    """SEC2-02, second round: the server cuts off a body that comes too slowly with 408
    "timeout". The client must get that answer while it is still sending, not a broken pipe
    reported as "could not be reached". (The socket timeout is 1 s here, 30 s by default.)"""

    def setUp(self):
        super().setUp()
        with self.store.app() as db:
            self.tokens = make_users(db)
        self.running = RunningServer(self.settings, self.store, socket_timeout=1.0)
        self.addCleanup(self.running.close)
        self.url = "http://127.0.0.1:%d" % self.running.port

    def test_the_408_reaches_a_client_that_is_still_sending(self):
        client = Client(self.url, self.tokens["admin"], timeout=30)
        _slow_link(self, self.running.port)      # about 4 KB/s: less than 16 KB per second
        started = time.monotonic()
        with self.assertRaises(PclError) as caught:
            client.register({"pad": "x" * (100 * 1024)})
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual((caught.exception.status, caught.exception.code), (408, "timeout"),
                         caught.exception.message)

    def test_a_refusal_before_the_body_ends_is_that_refusal(self):
        # A wrong token is refused at once; the client stops sending and reads the 401.
        client = Client(self.url, "pcl_" + "z" * 43, timeout=30)
        _slow_link(self, self.running.port)
        with self.assertRaises(PclError) as caught:
            client.register({"pad": "x" * (100 * 1024)})
        self.assertEqual(caught.exception.status, 401, caught.exception.message)

    def test_a_server_that_closes_while_the_body_is_sent(self):
        # No answer at all, the connection is just reset: the message says that the server
        # closed it while the request was being sent, not that it could not be reached.
        listener = socket.create_server(("127.0.0.1", 0))
        self.addCleanup(listener.close)

        def rude():
            connection, _ = listener.accept()
            connection.recv(1024)
            linger = struct.pack("HH" if os.name == "nt" else "ii", 1, 0)   # reset, no FIN
            connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, linger)
            connection.close()

        thread = threading.Thread(target=rude, daemon=True)
        thread.start()
        client = Client("http://127.0.0.1:%d" % listener.getsockname()[1], "pcl_" + "z" * 43,
                        timeout=30)
        with self.assertRaises(Unavailable) as caught:
            client.register({"pad": "x" * (32 * 1024 * 1024)})
        thread.join(10)
        self.assertEqual(caught.exception.code, "server_closed", caught.exception.message)
        self.assertIn("closed the connection while the request was being sent",
                      caught.exception.message)
        self.assertIn("var/log/server.log", caught.exception.message)


class RegisterThroughTheServer(support.SqliteCase, unittest.TestCase):
    """`pcldb register --server URL` with the real client (not the stub of
    tests/test_registration.py): documents go through POST /api/v1/registrations."""

    def setUp(self):
        super().setUp()
        with self.store.app() as db:
            self.tokens = make_users(db)
        self.running = RunningServer(self.settings, self.store)
        self.addCleanup(self.running.close)
        self.url = "http://127.0.0.1:%d" % self.running.port

    def args(self, **given):
        values = {"folder": None, "path": None, "card": None, "replace": False, "tree": None,
                  "document": None, "raman_uploads": None, "instrument": "raman-portable",
                  "scan_sessions": None, "server": self.url, "token_file": None}
        values.update(given)
        return argparse.Namespace(command="register", dir=None, **values)

    def run_register(self, **given):
        out = io.StringIO()
        code = registration.cmd_register(self.args(**given), self.settings, out=out)
        return code, out.getvalue().splitlines()

    def test_a_tree_and_a_raman_upload_through_the_server(self):
        token_file = self.project.path / "uploader_token.txt"
        token_file.write_text(self.tokens["uploader"] + "\n", encoding="utf-8")
        tree = self.project.path / "arc copy"
        rs.write_tree(tree, [("raman-portable", True, "1d_y_scan_20261114_100000_001"),
                             ("raman-portable", False, "1d_y_scan_20261114_100000_002")])
        code, lines = self.run_register(tree=str(tree), token_file=str(token_file))
        self.assertEqual(code, 0, lines)
        self.assertEqual(lines[-1], "Total: 2 session(s): 2 created, 0 updated, 0 unchanged, "
                                    "0 refused.")
        home = self.project.path / "controller home"
        # C2-01: the same upload id as the tree's raw/ copies (the fixture's own): a copy under
        # raw/ and the upload's own place stand side by side, never "moved by hand"
        made = rs.write_raman_record(home)
        code, lines = self.run_register(raman_uploads=str(home),
                                        scan_sessions=str(made["scan_sessions"]),
                                        token_file=str(token_file))
        self.assertEqual(code, 0, lines)
        self.assertTrue(lines[0].startswith("created    raman_globus_upload/simulated/"), lines)
        with self.store.app() as db:
            rows = db.query("SELECT path, registered_by FROM {pcl}.sessions")
        self.assertEqual(sorted(row["registered_by"] for row in rows), ["demo-uploader"] * 3)
        for marker in rs.PERSONAL_MARKERS.values():
            self.assertNotIn(marker, "\n".join(lines))

    def test_a_refusal_of_the_server_is_one_line_and_exit_1(self):
        token_file = self.project.path / "viewer_token.txt"
        token_file.write_text(self.tokens["viewer"], encoding="utf-8")
        document_file = self.project.path / "document.json"
        document_file.write_text(json.dumps(a_document(9)), encoding="utf-8")
        code, lines = self.run_register(document=str(document_file), token_file=str(token_file))
        self.assertEqual(code, 1)
        self.assertTrue(lines[0].startswith("refused    raw/raman-portable/simulated/"), lines)
        self.assertIn("may not register sessions", lines[0])
        self.assertNotIn(self.tokens["viewer"], "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
