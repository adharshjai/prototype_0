"""Tests of pcldb.llm against the fake LLM server on 127.0.0.1. No real network, no real key."""

from __future__ import annotations

import json
import logging
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path

from pcldb import llm
from pcldb.errors import PclError, Unavailable
from tests.fake_llm import (DROP, FAKE_KEY, VPN_403, FakeLlmServer, content, error_reply,
                            json_reply, models_reply, slow, text_reply)

ALLOWED_BODY_KEYS = {"model", "messages", "max_tokens", "temperature", "reasoning_effort",
                     "response_format"}
HELLO = [{"role": "system", "content": "You are a test."}, {"role": "user", "content": "hello"}]


def closed_address():
    """An address on this computer where nothing listens (a port that was free a moment ago)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return "http://127.0.0.1:%d/api/v1" % port


class LlmCase(unittest.TestCase):
    """A fake server, a list of the waits the client asked for, and every error it raised."""

    def setUp(self):
        llm.reset_process_state()
        self.server = FakeLlmServer().start()
        self.addCleanup(self.server.close)
        self.addCleanup(llm.reset_process_state)
        self.waits = []
        self.raised = []

    def client(self, **options):
        options.setdefault("timeout", 5)
        options.setdefault("sleep", self.waits.append)
        key = options.pop("api_key", FAKE_KEY)
        base_url = options.pop("base_url", self.server.base_url)
        return llm.ChatClient(base_url, key, "test-model", **options)

    def fails(self, call, reason=None):
        with self.assertRaises(Unavailable) as caught:
            call()
        error = caught.exception
        self.raised.append(error)
        self.assertNotIn(FAKE_KEY, error.message)
        self.assertNotIn(FAKE_KEY, repr(error))
        self.assertNotIn(FAKE_KEY, json.dumps(error.to_dict()))
        self.assertNotIn("Bearer", error.message)
        self.assertEqual(error.status, 503)
        if reason is not None:
            self.assertEqual(error.details["reason"], reason)
            self.assertEqual(error.code, "llm_" + reason)
        return error


class TestRequestShape(LlmCase):
    def test_body_holds_only_the_allowed_keys(self):
        self.server.script(content("hi"))
        result = self.client().chat(HELLO, max_tokens=2000)
        self.assertEqual(result.text, "hi")
        self.assertEqual(result.usage, {"prompt_tokens": 11, "completion_tokens": 7,
                                        "total_tokens": 18})
        self.assertGreaterEqual(result.elapsed_ms, 0)
        request = self.server.chat_requests[0]
        self.assertEqual(request["path"], "/api/v1/chat/completions")
        self.assertEqual(set(request["body"]), {"model", "messages", "max_tokens", "temperature",
                                                "reasoning_effort"})
        self.assertEqual(request["body"]["model"], "test-model")
        self.assertEqual(request["body"]["messages"], HELLO)
        self.assertEqual(request["body"]["max_tokens"], 2000)
        self.assertEqual(request["body"]["temperature"], 0)
        self.assertEqual(request["body"]["reasoning_effort"], "low")
        self.assertEqual(request["headers"]["authorization"], "Bearer " + FAKE_KEY)
        self.assertEqual(request["headers"]["content-type"], "application/json")
        self.assertTrue(request["headers"]["user-agent"].startswith("pcldb/"))

    def test_extra_message_keys_are_not_sent(self):
        self.server.script(content("ok"))
        self.client().chat([{"role": "user", "content": "x", "name": "n", "tool_ids": ["a"],
                             "files": ["f"]}])
        self.assertEqual(self.server.messages(), [{"role": "user", "content": "x"}])
        self.assertLessEqual(set(self.server.chat_requests[0]["body"]), ALLOWED_BODY_KEYS)

    def test_reasoning_effort_left_out_when_empty(self):
        self.server.script(content("ok"))
        self.client(reasoning_effort="").chat(HELLO)
        self.assertNotIn("reasoning_effort", self.server.chat_requests[0]["body"])

    def test_max_tokens_is_always_sent_and_never_above_8000(self):
        self.server.script(content("a"), content("b"))
        client = self.client()
        client.chat(HELLO)
        client.chat(HELLO, max_tokens=50000)
        sent = [request["body"]["max_tokens"] for request in self.server.chat_requests]
        self.assertEqual(sent, [800, 8000])

    def test_response_format_only_when_the_setting_is_on(self):
        self.server.script(content("{}"), content("{}"), content("{}"))
        self.client().chat(HELLO, json_object=True)                       # setting off
        self.client(response_format=True).chat(HELLO)                     # not asked for
        self.client(response_format=True).chat(HELLO, json_object=True)   # on and asked for
        bodies = [request["body"] for request in self.server.chat_requests]
        self.assertNotIn("response_format", bodies[0])
        self.assertNotIn("response_format", bodies[1])
        self.assertEqual(bodies[2]["response_format"], {"type": "json_object"})

    def test_bad_messages_are_a_programming_error(self):
        with self.assertRaises(ValueError):
            self.client().chat([])
        with self.assertRaises(ValueError):
            self.client().chat([{"role": "tool", "content": "x"}])
        self.assertEqual(self.server.requests, [])


class TestStatusPolicy(LlmCase):
    def test_401_is_a_key_problem_and_is_not_retried(self):
        self.server.script(error_reply(401, "Invalid API key"))
        error = self.fails(lambda: self.client().chat(HELLO), "bad_key")
        self.assertIn("key was refused", error.message)
        self.assertIn("llm.arc.vt.edu", error.message)
        self.assertEqual(len(self.server.chat_requests), 1)
        self.assertEqual(self.waits, [])

    def test_403_with_the_vpn_body_says_vpn_and_is_not_retried(self):
        self.server.script(VPN_403)
        error = self.fails(lambda: self.client().chat(HELLO), "off_vpn")
        self.assertIn("The ARC LLM service only answers on the VT campus network or VPN",
                      error.message)
        self.assertNotIn("new one", error.message)       # never "make a new key"
        self.assertEqual(len(self.server.chat_requests), 1)
        self.assertEqual(self.waits, [])

    def test_403_without_vpn_shows_the_servers_text(self):
        self.server.script(text_reply(403, "Model access denied for this account"))
        error = self.fails(lambda: self.client().chat(HELLO), "refused")
        self.assertIn("HTTP 403", error.message)
        self.assertIn("Model access denied for this account", error.message)
        self.assertEqual(len(self.server.chat_requests), 1)

    def test_413_is_not_retried(self):
        self.server.script(error_reply(413, "too many tokens", code="buffered_max_tokens_exceeded"))
        self.fails(lambda: self.client().chat(HELLO), "too_large")
        self.assertEqual(len(self.server.chat_requests), 1)

    def test_429_waits_for_retry_after_and_tries_again(self):
        self.server.script(error_reply(429, "slow down", headers={"Retry-After": "3"}),
                           content("fine"))
        self.assertEqual(self.client().chat(HELLO).text, "fine")
        self.assertEqual(self.waits, [3.0])

    def test_429_waits_for_retry_after_s_in_the_body(self):
        self.server.script(error_reply(429, "queue full", code="queue_full", retry_after_s=7),
                           content("fine"))
        self.client().chat(HELLO)
        self.assertEqual(self.waits, [7.0])

    def test_429_waits_five_seconds_by_default_and_twenty_at_most(self):
        self.server.script(text_reply(429, "busy"),
                           error_reply(429, "busy", headers={"Retry-After": "600"}),
                           content("fine"))
        self.client().chat(HELLO)
        self.assertEqual(self.waits, [5.0, 20.0])

    def test_429_is_retried_at_most_twice(self):
        self.server.script(*[error_reply(429, "busy")] * 5)
        error = self.fails(lambda: self.client().chat(HELLO), "busy")
        self.assertIn("busy", error.message)
        self.assertEqual(len(self.server.chat_requests), 3)
        self.assertEqual(len(self.waits), 2)

    def test_502_and_503_are_retried_once(self):
        for status in (502, 503, 504):
            with self.subTest(status=status):
                self.server.reset()
                self.server.script(text_reply(status, "<html>bad gateway</html>"), content("ok"))
                self.assertEqual(self.client().chat(HELLO).text, "ok")
                self.assertEqual(len(self.server.chat_requests), 2)
                self.server.reset()
                self.server.script(*[error_reply(status, "down", code="queue_timeout")] * 4)
                error = self.fails(lambda: self.client().chat(HELLO), "server_error")
                self.assertIn("HTTP %d" % status, error.message)
                self.assertNotIn("<html>", error.message)
                self.assertEqual(len(self.server.chat_requests), 2)

    def test_500_is_not_retried(self):
        self.server.script(text_reply(500, "boom"), content("never"))
        self.fails(lambda: self.client().chat(HELLO), "server_error")
        self.assertEqual(len(self.server.chat_requests), 1)

    def test_timeout_is_retried_once_then_unavailable(self):
        self.server.script(slow(3, content("late")), slow(3, content("late")), content("never"))
        started = time.monotonic()
        error = self.fails(lambda: self.client(timeout=0.3).chat(HELLO), "timeout")
        self.assertLess(time.monotonic() - started, 2.5)
        self.assertIn("did not answer", error.message)
        self.assertEqual(len(self.server.chat_requests), 2)

    def test_timeout_then_success(self):
        self.server.script(slow(3, content("late")), content("on time"))
        self.assertEqual(self.client(timeout=0.3).chat(HELLO).text, "on time")

    def test_dropped_connection_is_retried_once(self):
        self.server.script(DROP, content("second try"))
        self.assertEqual(self.client().chat(HELLO).text, "second try")
        self.server.reset()
        self.server.script(DROP, DROP, content("never"))
        self.fails(lambda: self.client().chat(HELLO), "no_connection")
        self.assertEqual(len(self.server.chat_requests), 2)

    def test_closed_port_is_no_connection(self):
        client = self.client(base_url=closed_address())
        error = self.fails(lambda: client.chat(HELLO), "no_connection")
        self.assertIn("could not be reached", error.message)
        self.assertEqual(len(self.waits), 1)

    def test_404_names_the_model_and_the_settings(self):
        self.server.script(error_reply(404, "Model not found"))
        error = self.fails(lambda: self.client().chat(HELLO), "not_found")
        self.assertIn("test-model", error.message)
        self.assertIn("llm.model", error.message)

    def test_400_about_the_model_names_the_model(self):
        self.server.script(error_reply(400, "The model `test-model` does not exist"))
        self.fails(lambda: self.client().chat(HELLO), "not_found")

    def test_other_400_shows_the_servers_text_cut_and_cleaned(self):
        self.server.script(error_reply(400, "bad\x1b[31m thing\n" + "x" * 5000))
        error = self.fails(lambda: self.client().chat(HELLO), "http_error")
        self.assertIn("HTTP 400", error.message)
        self.assertIn("bad", error.message)
        self.assertNotIn("\x1b", error.message)
        self.assertNotIn("\n", error.message)
        self.assertLess(len(error.message), 400)

    def test_redirect_is_refused_and_the_key_does_not_travel(self):
        with FakeLlmServer() as other:
            self.server.script(text_reply(307, "", headers={
                "Location": other.base_url + "/chat/completions"}))
            self.fails(lambda: self.client().chat(HELLO), "http_error")
            self.assertEqual(other.requests, [])
        self.assertEqual(len(self.server.chat_requests), 1)


class TestResponseFormatSwitch(LlmCase):
    def test_400_switches_response_format_off_for_the_process(self):
        for status in (400, 422):
            with self.subTest(status=status):
                llm.reset_process_state()
                self.server.reset()
                self.server.script(error_reply(status, "response_format is not supported"),
                                   content("{}"), content("{}"), content("{}"))
                client = self.client(response_format=True)
                self.assertEqual(client.chat(HELLO, json_object=True).text, "{}")
                client.chat(HELLO, json_object=True)
                self.client(response_format=True).chat(HELLO, json_object=True)   # a new client
                sent = ["response_format" in request["body"]
                        for request in self.server.chat_requests]
                self.assertEqual(sent, [True, False, False, False])
                self.assertEqual(self.waits, [])

    def test_400_without_response_format_is_just_an_error(self):
        self.server.script(error_reply(400, "bad request"), content("never"))
        self.fails(lambda: self.client().chat(HELLO, json_object=True), "http_error")
        self.assertEqual(len(self.server.chat_requests), 1)


class TestReadingReplies(LlmCase):
    def test_reasoning_fields_are_ignored(self):
        self.server.script(content("the answer", reasoning="secret plan A"),
                           content("the answer", reasoning_content="secret plan B"))
        client = self.client()
        self.assertEqual(client.chat(HELLO).text, "the answer")
        self.assertEqual(client.chat(HELLO).text, "the answer")

    def test_think_tags_are_removed(self):
        self.server.script(content("<think>step 1\nstep 2</think>\n\nthe answer"))
        self.assertEqual(self.client().chat(HELLO).text, "the answer")

    def test_fences_and_trailing_text_are_kept_for_the_json_reader(self):
        text = "```json\n{\"keywords\": \"\"}\n```\nHope that helps!"
        self.server.script(content(text))
        result = self.client().chat(HELLO)
        self.assertEqual(result.text, text)
        self.assertEqual(llm.extract_json_object(result.text), {"keywords": ""})

    def test_content_parts_are_joined(self):
        self.server.script(json_reply(200, {"choices": [{"message": {"content": [
            {"type": "text", "text": "one "}, {"type": "text", "text": "two"}]},
            "finish_reason": "stop"}]}))
        result = self.client().chat(HELLO)
        self.assertEqual(result.text, "one two")
        self.assertEqual(result.usage, {})

    def test_empty_content_is_retried_once_with_double_max_tokens(self):
        self.server.script(content(None, reasoning="all the tokens went into thinking"),
                           content("now an answer"))
        self.assertEqual(self.client().chat(HELLO, max_tokens=2000).text, "now an answer")
        sent = [request["body"]["max_tokens"] for request in self.server.chat_requests]
        self.assertEqual(sent, [2000, 4000])
        self.assertEqual(self.waits, [])

    def test_finish_reason_length_is_a_failed_attempt(self):
        self.server.script(content('{"keywords": "cut', finish_reason="length"),
                           content('{"keywords": "whole"}'))
        self.assertEqual(self.client().chat(HELLO, max_tokens=1500).text, '{"keywords": "whole"}')
        self.assertEqual(self.server.chat_requests[1]["body"]["max_tokens"], 3000)

    def test_two_empty_answers_are_unavailable(self):
        self.server.script(content(""), content("<think>only thinking</think>"), content("never"))
        error = self.fails(lambda: self.client().chat(HELLO, max_tokens=6000), "empty")
        self.assertIn("no usable answer", error.message)
        sent = [request["body"]["max_tokens"] for request in self.server.chat_requests]
        self.assertEqual(sent, [6000, 8000])

    def test_a_200_that_is_not_a_chat_completion(self):
        for reply in (text_reply(200, "<html>Sign in to the network</html>"),
                      json_reply(200, {"choices": []}),
                      json_reply(200, {"detail": "ok"}),
                      json_reply(200, [1, 2, 3])):
            with self.subTest(body=reply.body[:30]):
                self.server.reset()
                self.server.script(reply)
                self.fails(lambda: self.client().chat(HELLO), "odd_reply")
                self.assertEqual(len(self.server.chat_requests), 1)


class TestSecrets(LlmCase):
    def test_no_key_is_unavailable_before_any_request(self):
        for key in (None, "", "   "):
            error = self.fails(lambda: self.client(api_key=key).chat(HELLO), "no_key")
            self.assertIn("No ARC LLM key", error.message)
        self.assertEqual(self.server.requests, [])

    def test_a_key_with_a_line_break_is_refused_without_showing_it(self):
        bad = "sk-first" + "\r\nX-Injected: yes"
        client = self.client(api_key=bad)
        error = self.fails(lambda: client.chat(HELLO), "bad_key_form")
        self.assertNotIn("sk-first", error.message)
        self.assertNotIn("Injected", error.message)
        self.assertEqual(self.server.requests, [])

    def test_disabled_client_does_not_call(self):
        self.fails(lambda: self.client(enabled=False).chat(HELLO), "disabled")
        self.assertEqual(self.server.requests, [])

    def test_bad_address_is_said_plainly(self):
        for address in ("", "ftp://example.invalid/x", "file:///etc/passwd", "llm-api"):
            error = self.fails(lambda: self.client(base_url=address).chat(HELLO), "bad_address")
            self.assertIn("llm.base_url", error.message)

    def test_repr_and_attributes_do_not_show_the_key(self):
        client = self.client()
        self.assertNotIn(FAKE_KEY, repr(client))
        self.assertNotIn(FAKE_KEY, str(client))
        self.assertIn("key=set", repr(client))
        self.assertIn("key=not set", repr(self.client(api_key=None)))
        public = {name: value for name, value in vars(client).items() if not name.startswith("_")}
        self.assertNotIn(FAKE_KEY, repr(public))

    def test_a_server_that_echoes_the_key_cannot_put_it_into_a_message(self):
        echo = "the key %s is not allowed to do this" % FAKE_KEY
        for status in (400, 403, 409, 418):
            self.server.script(error_reply(status, echo))
            error = self.fails(lambda: self.client().chat(HELLO))
            self.assertIn("[key]", error.message)

    def test_no_raised_message_and_no_log_line_holds_the_key(self):
        scenarios = [
            [error_reply(401, "bad key " + FAKE_KEY)], [VPN_403], [text_reply(403, FAKE_KEY)],
            [error_reply(413, FAKE_KEY)], [error_reply(429, FAKE_KEY)] * 3,
            [error_reply(503, FAKE_KEY)] * 2, [text_reply(500, FAKE_KEY)],
            [error_reply(404, FAKE_KEY)], [error_reply(400, FAKE_KEY)], [DROP, DROP],
            [content("")] * 2, [text_reply(200, FAKE_KEY)], [text_reply(302, FAKE_KEY)],
        ]
        logger = logging.getLogger("pcldb")
        with self.assertLogs(logger, level="DEBUG") as logs:
            for replies in scenarios:
                self.server.reset()
                self.server.script(*replies)
                self.fails(lambda: self.client().chat(HELLO))
                self.fails(lambda: self.client(api_key=None).models(), "no_key")
            self.server.models = text_reply(500, FAKE_KEY)
            self.fails(lambda: self.client().models())
            note = self.client().probe()["note"]
        self.assertNotIn(FAKE_KEY, note)
        self.assertGreaterEqual(len(self.raised), 2 * len(scenarios))
        for error in self.raised:
            self.assertNotIn(FAKE_KEY, str(error))
            self.assertNotIn(FAKE_KEY, repr(error.details))
        for line in logs.output:
            self.assertNotIn(FAKE_KEY, line)
            self.assertNotIn("Bearer", line)
        self.assertTrue(logs.output)


class TestConcurrency(LlmCase):
    def test_at_most_max_concurrent_requests_are_in_flight(self):
        self.server.default = content("ok", delay=0.15)
        client = self.client(max_concurrent=2)
        results = []

        def work():
            results.append(client.chat(HELLO).text)

        threads = [threading.Thread(target=work) for _ in range(7)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(results, ["ok"] * 7)
        self.assertEqual(self.server.max_in_flight, 2)

    def test_the_limit_is_shared_by_every_client_of_the_process(self):
        self.server.default = content("ok", delay=0.15)
        results = []

        def work():
            results.append(self.client(max_concurrent=1).chat(HELLO).text)

        threads = [threading.Thread(target=work) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        self.assertEqual(results, ["ok"] * 4)
        self.assertEqual(self.server.max_in_flight, 1)


    def test_a_status_check_does_not_wait_behind_long_answers(self):
        self.server.default = content("ok", delay=1.0)
        self.server.models = models_reply("m")
        client = self.client(max_concurrent=1)
        worker = threading.Thread(target=lambda: client.chat(HELLO))
        worker.start()
        time.sleep(0.2)
        started = time.monotonic()
        self.assertEqual(client.probe()["state"], "ready")
        self.assertLess(time.monotonic() - started, 0.7)
        worker.join(timeout=10)


class TestModelsAndProbe(LlmCase):
    def test_models_lists_ids(self):
        self.server.models = models_reply("gpt-oss-120b", "GLM-5.3")
        self.assertEqual(self.client().models(), ["gpt-oss-120b", "GLM-5.3"])
        request = self.server.requests[0]
        self.assertEqual((request["method"], request["path"]), ("GET", "/api/v1/models"))
        self.assertEqual(request["headers"]["authorization"], "Bearer " + FAKE_KEY)

    def test_models_tolerates_other_shapes(self):
        for payload, expected in (
                ({"models": [{"name": "a"}, {"id": "b"}]}, ["a", "b"]),
                (["x", "y", 3, None], ["x", "y"]),
                ({"data": "nonsense"}, []),
                ({"unexpected": True}, [])):
            self.server.models = json_reply(200, payload)
            self.assertEqual(self.client().models(), expected)
        self.server.models = text_reply(200, "<html>not a list</html>")
        self.assertEqual(self.client().models(), [])

    def test_models_tolerates_the_list_being_absent(self):
        for status in (404, 405):
            self.server.models = text_reply(status, "Not Found")
            self.assertEqual(self.client().models(), [])

    def test_models_reports_vpn_and_key_problems(self):
        self.server.models = VPN_403
        self.fails(lambda: self.client().models(), "off_vpn")
        self.server.models = error_reply(401, "bad key")
        self.fails(lambda: self.client().models(), "bad_key")

    def test_probe_states(self):
        cases = [
            (models_reply("gpt-oss-120b"), "ready", "answers"),
            (text_reply(404, "Not Found"), "ready", "no model list"),
            (VPN_403, "off_vpn", "VT VPN"),
            (error_reply(401, "bad"), "bad_key", "refused"),
            (text_reply(500, "boom"), "error", "HTTP 500"),
            (text_reply(403, "forbidden for you"), "error", "HTTP 403"),
            (error_reply(429, "busy"), "error", "HTTP 429"),
        ]
        for reply, state, words in cases:
            with self.subTest(state=state, words=words):
                llm.reset_process_state()
                self.server.models = reply
                result = self.client().probe()
                self.assertEqual(set(result), {"state", "note"})
                self.assertEqual(result["state"], state)
                self.assertIn(words, result["note"])
                self.assertNotIn(FAKE_KEY, result["note"])

    def test_probe_off_vpn_never_says_make_a_new_key(self):
        self.server.models = VPN_403
        note = self.client().probe()["note"]
        self.assertNotIn("new", note.lower().replace("network", ""))
        self.assertIn("keyword rules", note)

    def test_probe_without_key_or_disabled_makes_no_request(self):
        self.assertEqual(self.client(api_key=None).probe()["state"], "no_key")
        self.assertEqual(self.client(enabled=False).probe()["state"], "disabled")
        self.assertEqual(self.client(enabled=False, api_key=None).probe()["state"], "disabled")
        self.assertEqual(self.server.requests, [])

    def test_probe_never_raises(self):
        self.assertEqual(self.client(base_url=closed_address()).probe()["state"], "error")
        self.assertEqual(self.client(base_url="not an address").probe()["state"], "error")
        self.server.models = slow(3, models_reply("m"))
        result = self.client(timeout=0.3).probe()
        self.assertEqual(result["state"], "error")
        self.assertIn("in time", result["note"])
        self.server.models = DROP
        llm.reset_process_state()
        self.assertEqual(self.client().probe()["state"], "error")

    def test_probe_is_cached_for_a_minute(self):
        self.server.models = models_reply("m")
        client = self.client()
        first = client.probe()
        self.server.models = VPN_403
        self.assertEqual(client.probe(), first)
        self.assertEqual(self.client().probe(), first)             # another client, same service
        self.assertEqual(len(self.server.requests), 1)
        self.assertEqual(client.probe(max_age=0)["state"], "off_vpn")
        self.assertEqual(len(self.server.requests), 2)
        self.assertEqual(self.client(api_key="sk-another-key").probe(max_age=60)["state"],
                         "off_vpn")
        self.assertEqual(len(self.server.requests), 3)             # another key is another answer


class TestFromSettings(LlmCase):
    def test_from_settings_reads_the_key_file_and_the_options(self):
        from pcldb.config import Settings
        with tempfile.TemporaryDirectory() as folder:
            user = Path(folder) / "user"
            user.mkdir()
            (user / "arc_llm_api_key").write_text(FAKE_KEY + "\n", encoding="utf-8")
            settings = Settings.for_test(folder, changes={
                "llm.base_url": self.server.base_url, "llm.model": "some-model",
                "llm.reasoning_effort": "high", "llm.response_format": True,
                "llm.timeout_seconds": 7})
            client = llm.ChatClient.from_settings(settings, sleep=self.waits.append)
            self.assertEqual(client.model, "some-model")
            self.assertEqual(client.timeout, 7.0)
            self.assertTrue(client.has_key)
            self.assertNotIn(FAKE_KEY, repr(client))
            self.server.script(content("{}"))
            client.chat(HELLO, json_object=True)
            body = self.server.chat_requests[0]["body"]
            self.assertEqual(body["model"], "some-model")
            self.assertEqual(body["reasoning_effort"], "high")
            self.assertIn("response_format", body)
            self.assertEqual(self.server.chat_requests[0]["headers"]["authorization"],
                             "Bearer " + FAKE_KEY)

    def test_from_settings_without_a_key_and_with_an_empty_model(self):
        from pcldb.config import Settings
        with tempfile.TemporaryDirectory() as folder:
            settings = Settings.for_test(folder, changes={"llm.model": "", "llm.enabled": False})
            client = llm.ChatClient.from_settings(settings)
            self.assertFalse(client.has_key)
            self.assertFalse(client.enabled)
            self.assertEqual(client.model, llm.DEFAULT_MODEL)
            self.assertEqual(client.probe()["state"], "disabled")


class TestAssistantStatus(LlmCase):
    def settings(self, folder, key=True, **changes):
        from pcldb.config import Settings
        changes = {"llm." + name: value for name, value in changes.items()}
        changes.setdefault("llm.base_url", self.server.base_url)
        settings = Settings.for_test(folder, changes=changes)
        if key:
            settings.user_dir.mkdir(parents=True, exist_ok=True)
            settings.llm_api_key_path.write_text(FAKE_KEY + "\n", encoding="utf-8")
        return settings

    def test_the_states_a_status_page_shows(self):
        with tempfile.TemporaryDirectory() as folder:
            self.server.models = VPN_403
            status = llm.assistant_status(self.settings(folder))
            self.assertEqual(set(status), {"enabled", "key_present", "model", "state", "note"})
            self.assertEqual((status["enabled"], status["key_present"], status["model"],
                              status["state"]), (True, True, "gpt-oss-120b", "off_vpn"))
            self.assertNotIn(FAKE_KEY, json.dumps(status))
            llm.reset_process_state()
            self.server.models = models_reply("gpt-oss-120b")
            self.assertEqual(llm.assistant_status(self.settings(folder))["state"], "ready")
            off = llm.assistant_status(self.settings(folder, enabled=False))
            self.assertEqual((off["enabled"], off["state"]), (False, "disabled"))
        with tempfile.TemporaryDirectory() as folder:
            none = llm.assistant_status(self.settings(folder, key=False))
            self.assertEqual((none["key_present"], none["state"]), (False, "no_key"))
            self.assertIn("No ARC LLM key", none["note"])


# ---------------------------------------------------------------------- reading model text

BIG_OBJECT = {"keywords": "", "filters": [], "limit": 10, "explanation": "x" * 200_000}

# (name, text the model sent, the object that must be found; None = ValueError)
EXTRACT_CASES = [
    ("bare object", '{"keywords": "a"}', {"keywords": "a"}),
    ("spaces and newlines around", '\n\n  {"keywords": "a"}  \n', {"keywords": "a"}),
    ("json fence", '```json\n{"limit": 5}\n```', {"limit": 5}),
    ("plain fence", '```\n{"limit": 5}\n```', {"limit": 5}),
    ("fence with a sentence before and after",
     'Here is the plan:\n```json\n{"limit": 5}\n```\nLet me know if you need more.',
     {"limit": 5}),
    ("unclosed fence", '```json\n{"limit": 5}', {"limit": 5}),
    ("trailing text", '{"limit": 5} I hope this helps.', {"limit": 5}),
    ("leading text", 'Sure! {"limit": 5}', {"limit": 5}),
    ("think block before", '<think>The user wants {"x": 1} maybe</think>{"limit": 5}',
     {"limit": 5}),
    ("think block in upper case with newlines", '<THINK>\nhm\n</THINK>\n{"limit": 5}',
     {"limit": 5}),
    ("closing think tag only", 'so I filter by label {"draft": true}</think>\n{"limit": 5}',
     {"limit": 5}),
    ("two objects: the first wins", '{"a": 1} {"b": 2}', {"a": 1}),
    ("two objects on two lines", '{"a": 1}\n{"b": 2}\n', {"a": 1}),
    ("braces inside a string", '{"keywords": "a } b { c"}', {"keywords": "a } b { c"}),
    ("escaped quotes", '{"keywords": "say \\"hi\\" {now}"}', {"keywords": 'say "hi" {now}'}),
    ("escaped backslash before the quote", '{"k": "ends with \\\\"}', {"k": "ends with \\"}),
    ("unicode escapes", '{"k": "\\u00e9\\u007b"}', {"k": "é{"}),
    ("array first", '[1, 2, 3] {"limit": 5}', {"limit": 5}),
    ("object inside an array", '[{"a": 1}, {"b": 2}]', {"a": 1}),
    ("nested objects", '{"sort": {"field": "started", "order": "desc"}, "filters": [{"f": 1}]}',
     {"sort": {"field": "started", "order": "desc"}, "filters": [{"f": 1}]}),
    ("prose braces before the object", 'Use {field} and {op}. Plan: {"limit": 5}', {"limit": 5}),
    ("a set-like brace group before", '{1, 2} then {"limit": 5}', {"limit": 5}),
    ("empty object", "{}", {}),
    ("windows line ends and a byte order mark", '﻿{\r\n"limit": 5\r\n}\r\n', {"limit": 5}),
    ("harmony channels", '<|channel|>analysis<|message|>thinking {"no": 1}<|end|>'
     '<|start|>assistant<|channel|>final<|message|>{"limit": 5}<|return|>', {"limit": 5}),
    ("a 200 kB object", json.dumps(BIG_OBJECT), BIG_OBJECT),
    ("200 kB of text and then the object", "word " * 40_000 + '{"limit": 5}', {"limit": 5}),
    ("many stray braces first", "{ " * 150 + '{"limit": 5}', {"limit": 5}),
    ("no object at all", "I cannot help with that.", None),
    ("empty text", "", None),
    ("not text", None, None),
    ("only thinking", "<think>{\"limit\": 5}</think>", None),
    ("unclosed think block", '<think>I will answer {"limit": 5}', None),
    ("truncated object", '{"keywords": "a", "filters": [{"field": "instrument", "op": "any"}',
     None),
    ("truncated inside a string", '{"keywords": "polystyr', None),
    ("truncated after a key", '{"keywords":', None),
    ("an array only", "[1, 2, 3]", None),
    ("single quotes", "{'limit': 5}", None),
    ("trailing comma", '{"limit": 5,}', None),
    ("NaN is not JSON", '{"limit": NaN}', None),
    ("Infinity is not JSON", '{"limit": Infinity}', None),
    ("a string that looks like an object", '"{\\"limit\\": 5}"', None),
    ("ten thousand opening braces", "{" * 10_000, None),
    ("deep nesting that never closes", '{"a":' * 5_000, None),
    ("deep arrays inside an object", '{"a": ' + "[" * 100_000 + "]" * 100_000 + "}", None),
]

STRIP_CASES = [
    ("nothing to remove", "plain answer", "plain answer"),
    ("one block", "<think>abc</think>answer", "answer"),
    ("block in the middle", "before <think>abc</think> after", "before  after"),
    ("two blocks", "<think>a</think>one<think>b</think>two", "onetwo"),
    ("nested blocks", "<think>a<think>b</think>c</think>answer", "answer"),
    ("thinking spelling", "<thinking>a</thinking>answer", "answer"),
    ("mixed case and spaces", "< Think >a</ THINK >answer", "answer"),
    ("multi-line", "<think>\nline 1\nline 2\n</think>\n\nanswer", "\n\nanswer"),
    ("unclosed block runs to the end", "answer so far <think>and then", "answer so far "),
    ("closing tag only", "thoughts without a start</think>answer", "answer"),
    ("two closing tags", "a</think>b</think>answer", "answer"),
    ("an angle bracket that is no tag", "a < b and c > d", "a < b and c > d"),
    ("another tag is kept", "<b>bold</b>", "<b>bold</b>"),
    ("empty", "", ""),
    ("None", None, ""),
    ("a number", 12, ""),
    ("harmony final channel", "<|channel|>analysis<|message|>hm<|end|><|start|>assistant"
     "<|channel|>final<|message|>answer<|return|>", "answer"),
]


class TestReadingModelText(unittest.TestCase):
    def test_the_tables_are_big_enough(self):
        self.assertGreaterEqual(len(EXTRACT_CASES), 30)
        self.assertGreaterEqual(len(EXTRACT_CASES) + len(STRIP_CASES), 50)

    def test_extract_json_object(self):
        for name, text, expected in EXTRACT_CASES:
            with self.subTest(case=name):
                started = time.monotonic()
                if expected is None:
                    with self.assertRaises(ValueError) as caught:
                        llm.extract_json_object(text)
                    self.assertNotIsInstance(caught.exception, json.JSONDecodeError)
                    self.assertLess(len(str(caught.exception)), 100)
                else:
                    self.assertEqual(llm.extract_json_object(text), expected)
                self.assertLess(time.monotonic() - started, 5.0)

    def test_truncation_is_told_apart_from_no_object(self):
        with self.assertRaises(ValueError) as cut:
            llm.extract_json_object('{"keywords": "a", "filters": [{"field": "x"}')
        self.assertIn("cut off", str(cut.exception))
        with self.assertRaises(ValueError) as none:
            llm.extract_json_object("no braces here")
        self.assertIn("no JSON object", str(none.exception))

    def test_an_inner_object_of_a_truncated_one_is_not_taken_for_the_plan(self):
        text = '{"keywords": "", "filters": [{"field": "instrument", "op": "any"}], "sort"'
        with self.assertRaises(ValueError):
            llm.extract_json_object(text)

    def test_strip_reasoning(self):
        for name, text, expected in STRIP_CASES:
            with self.subTest(case=name):
                self.assertEqual(llm.strip_reasoning(text), expected)

    def test_strip_reasoning_is_fast_on_hostile_text(self):
        hostile = "<think>" * 50_000 + "x" * 100_000 + "</think>" * 10
        started = time.monotonic()
        llm.strip_reasoning(hostile)
        llm.strip_reasoning("<" * 200_000)
        self.assertLess(time.monotonic() - started, 3.0)


class TestErrorsAreThePublicKind(unittest.TestCase):
    def test_unavailable_is_a_pcl_error_with_status_503(self):
        client = llm.ChatClient("http://127.0.0.1:9/api/v1", None, "m")
        with self.assertRaises(PclError) as caught:
            client.chat(HELLO)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(caught.exception.code, "llm_no_key")


if __name__ == "__main__":
    unittest.main()
