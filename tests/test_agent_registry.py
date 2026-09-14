"""Which model writes each aircraft's filings: a label on the screen, never a judgement.

Each operator process tells the runtime what it runs (model id, server) when it starts and every
30 s; the runtime keeps the latest per aircraft, gives it a human name, and drops the ones that
stopped talking. The direct world cannot reach the runtime (it is on the other network), so the
simulator carries its model per vehicle from the environment it was started with.
"""

import http.server
import json
import tempfile
import threading
import time
import unittest

from backend.runtime import agents as agents_module
from backend.runtime.tower import Runtime
from drone.agent.chooser import Choice
from drone.agent.loop import GuardedAgent, ModelHealth, Registration, identity
from drone.agent.propose import Proposer
from drone.agent.trace import form_part
from shared.config import model_display
from shared.llm.client import TieredLlm
from sim.world import Simulation

CONFIG = "configs/fleet.yaml"
FLEET = ("drone-01", "drone-02", "drone-03", "drone-04")


def make_runtime() -> Runtime:
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
    runtime.tick = 100
    # The world (telemetry) supplies the fleet roster. Registration accepts only aircraft on it.
    runtime.telemetry = {asset: {"lat": 40.70, "lon": -73.97, "alt_m": 0.0} for asset in FLEET}
    return runtime


def agent(asset: str = "drone-01", **fields) -> dict:
    return {"asset_id": asset, "world": "guarded", "model": "nemotron-3-nano:4b",
            "host": "ollama", "base_url_port": 11435, **fields}


def llm(base_url: str, nano: str, api_key: str = "") -> TieredLlm:
    """A client that ignores the environment. Fields are overwritten after construction so
    empty values are not filled in from the environment."""
    client = TieredLlm(base_url=base_url or "http://placeholder", models={"nano": nano})
    client.base_url, client.api_key = base_url, api_key
    return client


class DisplayNameTest(unittest.TestCase):
    def test_known_ids_read_as_names_unknown_ids_as_themselves_and_empty_as_rules(self):
        cases = {"nemotron-3-nano:4b": "Nemotron Nano 4B",
                 "nemotron-3-nano": "Nemotron Nano 30B",
                 "nemotron-3-nano:latest": "Nemotron Nano 30B",
                 "nvidia/Nemotron-3_5-Lightning": "Nemotron 3.5 Lightning",
                 "nvidia/nemotron-3-super-120b-a12b": "Nemotron Super 120B",
                 "gpt-oss-20b": "gpt-oss-20b", "": "rules", "   ": "rules", None: "rules"}
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(model_display(raw), expected)


class RegistryTest(unittest.TestCase):
    def setUp(self):
        self.runtime = make_runtime()

    def test_a_registration_appears_in_state_with_its_display_name(self):
        status, body = self.runtime.register_agent(agent())
        self.assertEqual((status, body["ok"], body["display"]), (200, True, "Nemotron Nano 4B"))
        self.assertEqual(self.runtime.snapshot()["agents"], {"drone-01": {
            "model": "nemotron-3-nano:4b", "host": "ollama", "world": "guarded",
            "last_seen_tick": 100, "display": "Nemotron Nano 4B", "base_url_port": 11435,
            "model_ok": None}})

    def test_an_agent_without_a_model_is_rules(self):
        self.runtime.register_agent(agent("drone-02", model="", host="off", base_url_port=None))
        row = self.runtime.snapshot()["agents"]["drone-02"]
        self.assertEqual((row["model"], row["host"], row["display"]), ("", "off", "rules"))

    def test_bad_forms_are_refused_at_the_door_and_the_direct_world_is_not_ours(self):
        for body in ({}, {"asset_id": "  "}, agent(host="mars"), agent(world="moon"),
                     agent(base_url_port="eleven")):
            with self.subTest(body=body):
                self.assertEqual(self.runtime.register_agent(body)[0], 400)
        status, body = self.runtime.register_agent(agent(world="direct"))
        self.assertEqual(status, 400)
        self.assertIn("DIRECT_MODEL", body["error"])
        self.assertEqual(self.runtime.snapshot()["agents"], {})

    def test_a_silent_agent_drops_after_the_stale_window_and_a_fresh_one_stays(self):
        self.runtime.register_agent(agent("drone-01"))
        self.runtime.register_agent(agent("drone-02"))
        self.runtime.tick = 100 + agents_module.AGENT_STALE_TICKS
        self.assertEqual(set(self.runtime.snapshot()["agents"]), {"drone-01", "drone-02"})
        self.runtime.register_agent(agent("drone-02"))
        self.runtime.tick += 1
        self.assertEqual(set(self.runtime.snapshot()["agents"]), {"drone-02"},
                         "a screen still showing a dead process's model name is lying")
        self.runtime.register_agent(agent("drone-01"))
        self.assertEqual(set(self.runtime.snapshot()["agents"]), {"drone-01", "drone-02"})

    def test_a_new_round_keeps_the_registry_on_the_new_clock(self):
        self.runtime._round = 0
        self.runtime.tick = 4900
        self.runtime.register_agent(agent())
        self.runtime.tick = 3
        self.runtime._follow_round(1)
        self.assertEqual(self.runtime.snapshot()["agents"]["drone-01"]["last_seen_tick"], 3,
                         "the process lives on — a new round does not drop its registration")
        self.runtime.tick = 3 + agents_module.AGENT_STALE_TICKS + 1
        self.assertEqual(self.runtime.snapshot()["agents"], {})


    def test_only_the_fleets_aircraft_register_and_only_once_the_world_is_known(self):
        status, body = self.runtime.register_agent(agent("drone-09"))
        self.assertEqual(status, 404, body)
        self.runtime.telemetry = {}
        status, body = self.runtime.register_agent(agent("drone-01"))
        self.assertEqual((status, body.get("retry")), (503, True),
                         "no roster until the world arrives — the aircraft retries within seconds")
        self.assertEqual(self.runtime.snapshot()["agents"], {})

    def test_a_model_that_stopped_answering_is_labelled_rules_and_keeps_its_id(self):
        self.runtime.register_agent(agent(model_ok=False))
        row = self.runtime.snapshot()["agents"]["drone-01"]
        self.assertEqual((row["model"], row["model_ok"], row["display"]),
                         ("nemotron-3-nano:4b", False, "rules"))


class IdentityTest(unittest.TestCase):
    def test_what_an_agent_says_about_itself(self):
        ollama = llm("http://127.0.0.1:11435/v1", "nemotron-3-nano:4b")
        self.assertEqual(identity("drone-01", ollama),
                         {"asset_id": "drone-01", "world": "guarded",
                          "model": "nemotron-3-nano:4b", "host": "ollama", "base_url_port": 11435,
                          "model_ok": None})
        nebius = identity("drone-02", llm("https://api.tokenfactory.nebius.com/v1",
                                          "nvidia/Nemotron-3_5-Lightning", api_key="k"))
        self.assertEqual((nebius["model"], nebius["host"], nebius["base_url_port"]),
                         ("nvidia/Nemotron-3_5-Lightning", "nebius", 443))

    def test_a_model_it_cannot_call_is_not_claimed(self):
        # Nebius without a key: every call is a 401 and the rules write the filings.
        keyless = identity("drone-01", llm("https://api.tokenfactory.nebius.com/v1",
                                           "nvidia/Nemotron-3_5-Lightning"))
        no_server = identity("drone-01", llm("", "nemotron-3-nano:4b"))
        no_nano = identity("drone-01", llm("http://127.0.0.1:11435/v1", ""))
        for said in (keyless, no_server, no_nano):
            with self.subTest(said=said):
                self.assertEqual((said["model"], said["host"], said["base_url_port"]),
                                 ("", "off", None))


    def test_whether_the_model_answers_rides_along_only_when_there_is_a_model(self):
        ollama = llm("http://127.0.0.1:11435/v1", "nemotron-3-nano:4b")
        self.assertIs(identity("drone-01", ollama, model_ok=False)["model_ok"], False)
        self.assertIsNone(identity("drone-01", llm("", "nemotron-3-nano:4b"),
                                   model_ok=True)["model_ok"])


class ModelHealthTest(unittest.TestCase):
    def test_answered_since_the_last_registration_is_true_and_all_missed_is_false(self):
        counts = [0, 0]
        health = ModelHealth(lambda: tuple(counts))
        self.assertIsNone(health.check(), "nothing asked yet, so unknown")
        counts[1] += 2
        self.assertIs(health.check(), False, "the rules wrote everything that was asked")
        self.assertIs(health.check(), False, "nothing asked since, so the last reading stands")
        counts[1] += 3
        counts[0] += 1
        self.assertIs(health.check(), True, "one used model answer means the model wrote it")

    def test_forms_and_choices_count_and_route_drafts_do_not(self):
        client = llm("http://127.0.0.1:9/v1", "nemotron-3-nano:4b")
        agent = GuardedAgent("drone-01", "http://127.0.0.1:9", Proposer(client))
        health = ModelHealth(lambda: (agent.model_answers, agent.model_misses))
        agent._count_form(form_part("nemotron-3-nano:4b", "delivery", "fly_route", "go", 900,
                                    True, None))
        self.assertIs(health.check(), True)
        client.stats["nano"].fallback += 5
        self.assertIs(health.check(), True,
                      "failed drafts (the last resort) alone must not flip the screen to rules")
        agent._count_form(form_part("", "delivery", "fly_route", "go", 6000, False, "timeout"))
        agent._count_choice(Choice("a", "rules: the shortest legal route", path="rules",
                                   asked=True, fallback_reason="timeout"))
        self.assertIs(health.check(), False, "asked, but the rules wrote it")
        agent._count_form(form_part("", "delivery", "fly_route", "go", 0, False, "no model"))
        agent._count_choice(Choice("a", "rules: the shortest legal route", path="rules"))
        self.assertIs(health.check(), False, "unasked ones don't count, so the last reading stands")
        agent._count_choice(Choice("c", "clear of traffic", model="nemotron-3-nano:4b",
                                   path="tools", asked=True))
        self.assertIs(health.check(), True)


class FakeRuntime(http.server.BaseHTTPRequestHandler):
    bodies: list[dict] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        type(self).bodies.append({"path": self.path, **json.loads(self.rfile.read(length))})
        payload = b'{"ok": true, "display": "Nemotron Nano 4B"}'
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


class RegistrationTest(unittest.TestCase):
    def setUp(self):
        FakeRuntime.bodies = []
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), FakeRuntime)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()

    def test_it_posts_once_per_period_once_accepted_and_says_what_it_is_each_time(self):
        said = iter([agent(model_ok=None), agent(model_ok=True)])
        registration = Registration(self.url, lambda: next(said), period_s=30.0, retry_s=3.0)
        self.assertTrue(registration.maybe_send(now=0.0))
        self.assertTrue(registration.accepted)
        self.assertFalse(registration.maybe_send(now=10.0))
        self.assertTrue(registration.maybe_send(now=31.0))
        self.assertEqual([b["path"] for b in FakeRuntime.bodies], ["/agents/register"] * 2)
        self.assertEqual([b["model_ok"] for b in FakeRuntime.bodies], [None, True],
                         "asks afresh on every send — whether the model is answering changes")

    def test_a_missing_runtime_is_tried_again_within_seconds(self):
        self.server.shutdown()
        self.server.server_close()
        self.server = None
        registration = Registration(self.url, agent, period_s=30.0, retry_s=3.0)
        self.assertTrue(registration.maybe_send(now=0.0))
        self.assertFalse(registration.accepted,
                         "with no runtime the registration fails and the aircraft keeps flying")
        self.assertFalse(registration.maybe_send(now=2.0))
        self.assertTrue(registration.maybe_send(now=3.0), "again in seconds, not half a minute")

    def test_it_runs_on_its_own_thread_and_stops(self):
        registration = Registration(self.url, agent, period_s=30.0, retry_s=0.05)
        thread = registration.start()
        deadline = time.monotonic() + 3.0
        while not FakeRuntime.bodies and time.monotonic() < deadline:
            time.sleep(0.01)
        registration.stop()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(len(FakeRuntime.bodies), 1, "once accepted, the next one is 30 s later")


class DirectModelTest(unittest.TestCase):
    def test_the_simulator_carries_the_direct_worlds_model_on_each_vehicle_and_only_there(self):
        simulation = Simulation(seed=7, direct_model="nemotron-3-nano:4b")
        direct = simulation.worlds["direct"].snapshot(0)["assets"]
        guarded = simulation.worlds["guarded"].snapshot(0)["assets"]
        self.assertEqual({v["agent_model"] for v in direct.values()}, {"nemotron-3-nano:4b"})
        self.assertTrue(all("agent_model" not in v for v in guarded.values()),
                        "in the guarded wiring the runtime's /state.agents knows the model")
        simulation.reset()
        self.assertEqual({v["agent_model"] for v in
                          simulation.worlds["direct"].snapshot(0)["assets"].values()},
                         {"nemotron-3-nano:4b"}, "a new round is still the same agent")

    def test_rules_is_an_empty_id_and_unknown_is_no_field(self):
        rules = Simulation(seed=7, direct_model="").worlds["direct"].snapshot(0)["assets"]
        self.assertEqual({v["agent_model"] for v in rules.values()}, {""})
        unknown = Simulation(seed=7).worlds["direct"].snapshot(0)["assets"]
        self.assertTrue(all("agent_model" not in v for v in unknown.values()))


if __name__ == "__main__":
    unittest.main()
