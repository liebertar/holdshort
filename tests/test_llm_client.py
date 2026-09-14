"""The client takes whatever shape a thinking model answers in, and never more than a form.

Ollama puts the thoughts in `message.reasoning`, some servers in `reasoning_content`, and
some inline as <think>…</think>. The answer must come out the same regardless, and a
JSON object that only exists inside the thoughts must not count as an answer.
"""

import json
import pathlib
import tempfile
import unittest
from unittest import mock

from shared.llm import client as client_module
from shared.llm.client import (
    LlmTier,
    TieredLlm,
    host_of,
    parse_choice,
    parse_json_object,
    reply_from,
    strip_think,
)


def completion(content=None, **extra_message):
    return {"choices": [{"message": {"role": "assistant", "content": content, **extra_message}}]}


def make(models=None, **kwargs) -> TieredLlm:
    return TieredLlm(base_url="http://model.test/v1", api_key="k",
                     models=models or {"nano": "nano-id", "ultra": "ultra-id"},
                     timeout_s=kwargs.pop("timeout_s", 3.0),
                     request_extra=kwargs.pop("request_extra", {}),
                     record_dir=kwargs.pop("record_dir", ""), **kwargs)


class ReplyParsingTest(unittest.TestCase):
    def test_content_comes_first(self):
        reply = reply_from(completion('{"a": 1}', reasoning="thinking about a"), "m")
        self.assertEqual((reply.text, reply.via), ('{"a": 1}', "content"))

    def test_reasoning_content_when_content_is_empty(self):
        reply = reply_from(completion("", reasoning_content='{"a": 2}'), "m")
        self.assertEqual((reply.text, reply.via), ('{"a": 2}', "reasoning_content"))

    def test_reasoning_when_nothing_else(self):
        reply = reply_from(completion(None, reasoning='{"a": 3}'), "m")
        self.assertEqual((reply.text, reply.via), ('{"a": 3}', "reasoning"))

    def test_a_leading_think_block_is_stripped(self):
        reply = reply_from(completion('<think>\nlet me see\n</think>\n{"a": 4}'), "m")
        self.assertEqual((reply.text, reply.via), ('{"a": 4}', "think-stripped"))

    def test_json_only_inside_think_is_not_an_answer(self):
        self.assertEqual(strip_think('<think>{"legs": []}</think>'), "")
        self.assertIsNone(parse_json_object('<think>{"legs": [1]}</think>'))
        # An unclosed think block is all thinking
        self.assertIsNone(parse_json_object('<think>maybe {"legs": [1]}'))
        self.assertIsNone(reply_from(completion('<think>{"a": 5}</think>'), "m"))

    def test_garbage_shapes_are_none(self):
        self.assertIsNone(reply_from(None, "m"))
        self.assertIsNone(reply_from({"choices": []}, "m"))
        self.assertIsNone(reply_from({"choices": [{"message": "text"}]}, "m"))
        self.assertIsNone(reply_from(completion("   "), "m"))

    def test_choice_is_a_bare_number_only(self):
        self.assertEqual(parse_choice("2", 3), 1)
        self.assertEqual(parse_choice(" #3. ", 3), 2)
        self.assertIsNone(parse_choice("7", 3))
        self.assertIsNone(parse_choice("not 7, go with 2", 3))
        self.assertIsNone(parse_choice("", 3))

    def test_host_is_read_off_the_url(self):
        self.assertEqual(host_of("http://localhost:11434/v1"), "ollama")
        self.assertEqual(host_of("http://127.0.0.1:11437/v1"), "ollama")     # fleet server
        self.assertEqual(host_of("http://host:114340/v1"), "other")
        self.assertEqual(host_of("https://api.tokenfactory.nebius.com/v1"), "nebius")
        self.assertEqual(host_of("http://llm-edge:8080/v1"), "other")
        self.assertEqual(host_of(""), "none")


class RequestShapeTest(unittest.TestCase):
    """What gets sent. The fake server swaps out post_json_status and nothing else."""

    def _capture(self, llm, responses):
        calls = []

        def fake(url, payload, timeout=20.0, headers=None):
            calls.append({"url": url, "payload": payload, "timeout": timeout,
                          "headers": headers})
            return responses.pop(0)

        return calls, mock.patch.object(client_module, "post_json_status", fake)

    def test_json_object_adds_response_format_and_extra_is_merged(self):
        llm = make(request_extra={"reasoning_effort": "none"})
        calls, patch = self._capture(llm, [(200, completion('{"ok": true}'))])
        with patch:
            reply = llm.ask(LlmTier.NANO, "sys", "usr", max_tokens=50, json_object=True)
        self.assertEqual(reply.text, '{"ok": true}')
        payload = calls[0]["payload"]
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["reasoning_effort"], "none")
        self.assertEqual(payload["model"], "nano-id")
        self.assertEqual(payload["max_tokens"], 50)
        self.assertEqual(calls[0]["timeout"], 3.0)
        self.assertEqual(calls[0]["headers"], {"Authorization": "Bearer k"})
        self.assertGreaterEqual(reply.latency_ms, 0)

    def test_without_json_object_there_is_no_response_format(self):
        llm = make()
        calls, patch = self._capture(llm, [(200, completion("3"))])
        with patch:
            llm.ask(LlmTier.ULTRA, "sys", "usr")
        self.assertNotIn("response_format", calls[0]["payload"])

    def test_a_400_on_response_format_retries_once_without_and_remembers(self):
        llm = make()
        calls, patch = self._capture(llm, [(400, None), (200, completion('{"a":1}')),
                                           (200, completion('{"a":2}'))])
        with patch:
            first = llm.ask(LlmTier.NANO, "s", "u", json_object=True)
            second = llm.ask(LlmTier.NANO, "s", "u", json_object=True)
        self.assertEqual(first.text, '{"a":1}')
        self.assertEqual(second.text, '{"a":2}')
        self.assertEqual(len(calls), 3)
        self.assertIn("response_format", calls[0]["payload"])
        self.assertNotIn("response_format", calls[1]["payload"])
        self.assertNotIn("response_format", calls[2]["payload"])   # remembered

    def test_a_timeout_is_not_retried(self):
        llm = make()
        calls, patch = self._capture(llm, [(0, None)])
        with patch:
            self.assertIsNone(llm.ask(LlmTier.NANO, "s", "u", json_object=True))
        self.assertEqual(len(calls), 1)
        self.assertEqual(llm.stats["nano"].fallback, 1)
        self.assertEqual(llm.stats["nano"].ok, 0)

    def test_a_call_can_bring_its_own_budget(self):
        """A draft is a longer question than a form, so it brings its own budget. Without
        one, the client default applies."""
        llm = make()
        calls, patch = self._capture(llm, [(200, completion("1")), (200, completion("2"))])
        with patch:
            llm.ask(LlmTier.NANO, "s", "u", timeout_s=30.0)
            llm.ask(LlmTier.NANO, "s", "u")
        self.assertEqual([c["timeout"] for c in calls], [30.0, 3.0])

    def test_it_remembers_when_the_server_was_last_unreachable(self):
        llm = make()
        self.assertFalse(llm.unreachable_within(60))
        calls, patch = self._capture(llm, [(0, None), (200, completion("ok"))])
        with patch:
            llm.ask(LlmTier.NANO, "s", "u")
            self.assertTrue(llm.unreachable_within(60))
            llm.ask(LlmTier.NANO, "s", "u")
        self.assertFalse(llm.unreachable_within(60))   # forgotten once an answer comes back

    def test_stats_count_kept_and_discarded_replies(self):
        llm = make()
        _, patch = self._capture(llm, [(200, completion("x")), (200, completion("y"))])
        with patch:
            llm.ask(LlmTier.NANO, "s", "u")
            llm.ask(LlmTier.NANO, "s", "u")
        llm.discard(LlmTier.NANO)
        self.assertEqual(llm.stats_dict()["nano"]["ok"], 1)
        self.assertEqual(llm.stats_dict()["nano"]["fallback"], 1)
        self.assertIsNotNone(llm.stats_dict()["nano"]["last_ms"])

    def test_disabled_client_never_calls_out(self):
        llm = TieredLlm(base_url="", models={"nano": "x"}, timeout_s=1, request_extra={},
                        record_dir="")
        calls, patch = self._capture(llm, [])
        with patch:
            self.assertIsNone(llm.ask(LlmTier.NANO, "s", "u"))
        self.assertEqual(calls, [])

    def test_record_dir_dumps_every_call(self):
        with tempfile.TemporaryDirectory() as folder:
            llm = make(record_dir=folder)
            _, patch = self._capture(llm, [(200, completion('<think>t</think>{"a":1}')),
                                           (0, None)])
            with patch:
                llm.ask(LlmTier.NANO, "system text", "user text", json_object=True)
                llm.ask(LlmTier.ULTRA, "s2", "u2")
            files = sorted(pathlib.Path(folder).glob("*.json"))
            self.assertEqual(len(files), 2)
            first = json.loads(files[0].read_text())
            keys = ("tier", "model", "system", "user", "text", "via")
            self.assertEqual({k: first[k] for k in keys},
                             {"tier": "nano", "model": "nano-id", "system": "system text",
                              "user": "user text", "text": '{"a":1}', "via": "think-stripped"})
            self.assertIsInstance(first["latency_ms"], int)
            second = json.loads(files[1].read_text())
            self.assertEqual((second["tier"], second["text"], second["via"]), ("ultra", None, None))

    def test_two_threads_can_share_one_client_without_losing_the_books(self):
        """The aircraft agent asks for drafts on a worker thread and for forms on the main
        thread, through the same client. The books (ok counts) and the record file numbers
        must not overwrite each other."""
        import threading
        import time

        with tempfile.TemporaryDirectory() as folder:
            llm = make(record_dir=folder)

            def fake(url, payload, timeout=20.0, headers=None):
                time.sleep(0.0005)          # releases the GIL so the threads really interleave
                return 200, completion('{"a": 1}')

            def work():
                for _ in range(40):
                    llm.ask(LlmTier.NANO, "s", "u", json_object=True)

            with mock.patch.object(client_module, "post_json_status", fake):
                threads = [threading.Thread(target=work) for _ in range(3)]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()
            self.assertEqual(llm.stats["nano"].ok, 120)
            self.assertEqual(llm.stats["nano"].fallback, 0)
            self.assertEqual(len(list(pathlib.Path(folder).glob("*.json"))), 120)

    def test_env_drives_timeout_extra_and_record_dir(self):
        env = {"LLM_BASE_URL": "http://localhost:11434/v1", "NEBIUS_API_KEY": "ollama",
               "LLM_TIMEOUT_S": "7.5", "LLM_REQUEST_EXTRA": '{"reasoning_effort": "none"}',
               "LLM_RECORD_DIR": "/tmp/nowhere"}
        with mock.patch.dict("os.environ", env, clear=False):
            llm = TieredLlm(models={"nano": "nemotron-3-nano"})
        self.assertEqual(llm.timeout_s, 7.5)
        self.assertEqual(llm.request_extra, {"reasoning_effort": "none"})
        self.assertEqual(llm.record_dir, "/tmp/nowhere")
        self.assertEqual(llm.host, "ollama")
        with mock.patch.dict("os.environ", {"LLM_REQUEST_EXTRA": "not json"}, clear=False):
            self.assertEqual(TieredLlm(models={"nano": "x"}, timeout_s=1).request_extra, {})


class FixtureLlmTest(unittest.TestCase):
    def test_answers_by_tier_and_needle_and_otherwise_nothing(self):
        from tests.fixture_llm import FixtureLlm

        llm = FixtureLlm(records=[
            {"tier": "nano", "needle": "goal 40.79850", "text": '{"legs": []}', "model": "rec"},
            {"tier": "ultra", "needle": "Resource:", "text": '{"choice": 2, "reason": "r"}'},
        ])
        self.assertEqual(llm.ask(LlmTier.NANO, "s", "origin x -> goal 40.79850,-73.955").text,
                         '{"legs": []}')
        self.assertIsNone(llm.ask(LlmTier.NANO, "s", "goal 40.70000"))
        self.assertIsNone(llm.ask(LlmTier.SUPER, "s", "goal 40.79850"))   # different tier
        self.assertEqual(llm.ask(LlmTier.ULTRA, "s", "Resource: pad").model, "nemotron-3-nano")
        self.assertEqual(llm.stats["nano"].fallback, 1)


class RecordedFormsTest(unittest.TestCase):
    """Runs forms and arbitration offline on recorded answers from the real Nemotron."""

    def setUp(self):
        from tests.fixture_llm import FixtureLlm, load_fixtures

        self.records = [r for r in load_fixtures() if r.get("kind") in ("form", "arbiter")]
        if not self.records:
            self.skipTest("no recorded form answers")
        self.llm = FixtureLlm(records=self.records)

    def test_the_model_writes_a_form_and_signs_it_with_its_id(self):
        from drone.agent.detect import Concern
        from drone.agent.propose import ALLOWED_ACTIONS, Proposer

        telemetry = {"id": "drone-01", "model": "dv-x500", "state": "ready", "battery": 88.0,
                     "vibration": 0.1, "autonomy_health": 1.0, "passengers": 0, "cargo": 6}
        proposer = Proposer(self.llm)
        written = proposer.write(Concern(kind="needs_route", urgency="normal",
                                         detail="delivering to Union Square, battery 88%"),
                                 telemetry, "pad:launch", frozenset(), ("pad:launch",))
        self.assertEqual(written.author, "nemotron-3-nano")
        self.assertEqual(written.action, "fly_route")
        self.assertIn(written.action, ALLOWED_ACTIONS)
        # A question missing from the recording is written by the rules
        by_rules = proposer.write(Concern(kind="motor_fault", urgency="high", detail="vibration"),
                                  telemetry, "pad:launch", frozenset(), ("pad:launch",))
        self.assertEqual(by_rules.author, "rules")

    def test_the_arbiter_uses_the_recorded_choice_and_keeps_the_reason(self):
        from backend.runtime.arbiter import Arbiter
        from shared.models import Proposal

        def candidate(asset_id, blast, why):
            return Proposal(asset_id=asset_id, action="reserve_pad", cost_usd=28.0,
                            blast_radius=blast, rationale=why, resource="pad:launch")

        candidates = [candidate("drone-01", "schedule", "maintenance check"),
                      candidate("drone-02", "cargo", "battery 9%")]
        choice = Arbiter(self.llm).pick(candidates, {"drone-01": {"battery": 40.0},
                                                     "drone-02": {"battery": 9.0}})
        self.assertEqual(choice.how, "ultra:nemotron-3-nano")
        self.assertIn(choice.proposal.asset_id, ("drone-01", "drone-02"))
        self.assertTrue(choice.reason)
        self.assertLessEqual(len(choice.reason), 140)


if __name__ == "__main__":
    unittest.main()
