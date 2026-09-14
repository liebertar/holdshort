"""scripts/resolve_stack.sh picks the model and intake paths by itself — no flags.

Nebius when there is a key; else the Ollama fleet (one server per drone) when it answers; else a
single Ollama; else rules. Tavily only with a key, METAR shown on only when the network answers.
The probes run against fake servers on free local ports, so this never touches the machine's own
Ollama or the internet.
"""

import http.server
import os
import socket
import subprocess
import threading
import unittest

SCRIPT = "scripts/resolve_stack.sh"


class Answer(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        payload = b'{"version": "test"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):
        pass


def free_block(size: int = 6) -> int:
    """The first of size free ports in a row. The fleet looks at the base port + 1.."""
    for _ in range(50):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            base = probe.getsockname()[1]
        if base + size >= 65535:
            continue
        sockets = []
        try:
            for port in range(base, base + size):
                held = socket.socket()
                held.bind(("127.0.0.1", port))
                sockets.append(held)
            return base
        except OSError:
            continue
        finally:
            for held in sockets:
                held.close()
    raise unittest.SkipTest("could not find a run of free ports")


class ResolveTest(unittest.TestCase):
    def setUp(self):
        self.base = free_block(7)
        self.servers = []

    def tearDown(self):
        for server in self.servers:
            server.shutdown()
            server.server_close()

    def serve(self, port: int) -> None:
        server = http.server.ThreadingHTTPServer(("127.0.0.1", port), Answer)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)

    def resolve(self, *args: str, **env: str) -> dict | str:
        base = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"),
                "OLLAMA_BASE_PORT": str(self.base),
                "OLLAMA_TOWER_PORT": str(self.base + 6),
                "METAR_PROBE_URL": f"http://127.0.0.1:{self.base + 5}/metar"}
        done = subprocess.run(["bash", SCRIPT, *(args or ("--env",))], env={**base, **env},
                              capture_output=True, text=True, timeout=60, check=True)
        if args:
            return done.stdout
        return dict(line.split("=", 1) for line in done.stdout.splitlines())

    def test_nothing_answers_so_rules(self):
        found = self.resolve()
        self.assertEqual((found["STACK_MODEL"], found["LLM_BASE_URL"], found["PER_ASSET_URLS"]),
                         ("rules", "", ""))
        self.assertEqual((found["STACK_METAR"], found["STACK_TAVILY"], found["INTAKE_DB"],
                          found["DIRECT_MODEL"]), ("off", "off", ".run/intake.sqlite", ""))

    def test_a_nebius_key_brings_the_token_factory_and_the_nvidia_ids(self):
        found = self.resolve(NEBIUS_API_KEY="key")
        self.assertEqual(found["STACK_MODEL"], "nebius")
        self.assertEqual(found["LLM_BASE_URL"], "https://api.tokenfactory.nebius.com/v1")
        self.assertEqual((found["MODEL_NANO"], found["RUNTIME_SUPER"], found["MODEL_ULTRA"]),
                         ("nvidia/Nemotron-3_5-Lightning", "nvidia/nemotron-3-super-120b-a12b",
                          "nvidia/Nemotron-3-Ultra-550b-a55b"))

    def test_a_placeholder_key_or_a_keyless_nebius_url_is_not_nebius(self):
        self.assertEqual(self.resolve(NEBIUS_API_KEY="ollama")["STACK_MODEL"], "rules")
        keyless = self.resolve(LLM_BASE_URL="https://api.tokenfactory.nebius.com/v1")
        self.assertEqual((keyless["STACK_MODEL"], keyless["LLM_BASE_URL"]), ("rules", ""),
                         "keyless Nebius 401s every call — rules write it under a model's name")

    def test_one_ollama_serves_everyone_with_the_4b_and_thinking_off(self):
        self.serve(self.base)
        found = self.resolve()
        self.assertEqual((found["STACK_MODEL"], found["LLM_BASE_URL"]),
                         ("ollama", f"http://127.0.0.1:{self.base}/v1"))
        self.assertEqual(found["RUNTIME_SUPER"], "nemotron-3-nano:4b")
        self.assertEqual(found["LLM_REQUEST_EXTRA"], '{"reasoning_effort":"none"}')

    def test_the_fleet_gives_each_drone_its_own_server_and_the_runtime_rules_without_11434(self):
        self.serve(self.base + 1)
        self.serve(self.base + 2)
        found = self.resolve()
        self.assertEqual(found["STACK_MODEL"], "ollama-fleet")
        self.assertEqual(found["PER_ASSET_URLS"].split(),
                         [f"http://127.0.0.1:{self.base + 1}/v1",
                          f"http://127.0.0.1:{self.base + 2}/v1"])
        self.assertEqual((found["LLM_BASE_URL"], found["RUNTIME_SUPER"]), ("", ""))
        self.serve(self.base)
        with_runtime = self.resolve()
        self.assertEqual((with_runtime["LLM_BASE_URL"], with_runtime["RUNTIME_SUPER"]),
                         (f"http://127.0.0.1:{self.base}/v1", "nemotron-3-nano:4b"))

    def test_the_tower_server_takes_the_runtime_before_11434(self):
        self.serve(self.base)          # the Ollama app (256k context)
        self.serve(self.base + 1)
        self.serve(self.base + 6)      # the runtime's server (8k context)
        found = self.resolve()
        self.assertEqual(found["STACK_MODEL"], "ollama-fleet")
        self.assertEqual((found["LLM_BASE_URL"], found["RUNTIME_SUPER"]),
                         (f"http://127.0.0.1:{self.base + 6}/v1", "nemotron-3-nano:4b"),
                         "11434 has a 256k context: over 5 GB more KV cache for the same 4B")
        self.assertEqual(found["PER_ASSET_URLS"].split(), [f"http://127.0.0.1:{self.base + 1}/v1"])

    def test_the_fleet_script_lists_the_tower_and_refuses_an_overlap(self):
        self.serve(self.base + 6)
        env = {"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/tmp"),
               "OLLAMA_FLEET_BASE_PORT": str(self.base), "OLLAMA_TOWER_PORT": str(self.base + 6)}
        subprocess.run(["bash", "-n", "scripts/ollama_fleet.sh"], check=True)
        status = subprocess.run(["bash", "scripts/ollama_fleet.sh", "status", "4"], env=env,
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(status.returncode, 0, status.stderr)
        self.assertIn(f":{self.base + 6} (runtime) up", status.stdout)
        self.assertIn(f":{self.base + 1} down", status.stdout)
        overlap = subprocess.run(["bash", "scripts/ollama_fleet.sh", "status", "4"],
                                 env={**env, "OLLAMA_TOWER_PORT": str(self.base + 2)},
                                 capture_output=True, text=True, timeout=30)
        self.assertNotEqual(overlap.returncode, 0)
        self.assertIn("overlaps the runtime server", overlap.stderr)

    def test_intake_follows_the_key_and_the_network(self):
        self.serve(self.base + 5)
        found = self.resolve(TAVILY_API_KEY="tvly")
        self.assertEqual((found["STACK_METAR"], found["STACK_TAVILY"]), ("on", "on"))
        self.assertEqual(self.resolve(METAR="off")["STACK_METAR"], "off")

    def test_the_direct_world_is_rules_unless_asked_and_then_names_its_model(self):
        self.serve(self.base)
        self.assertEqual(self.resolve()["DIRECT_MODEL"], "")
        self.assertEqual(self.resolve(DIRECT_LLM="1")["DIRECT_MODEL"], "nemotron-3-nano:4b")

    def test_the_table_is_one_screen(self):
        table = self.resolve("--table")
        self.assertIn("model   rules", table)
        self.assertIn("intake  metar off", table)
        self.assertIn("tavily off · sim on", table)
        self.assertLessEqual(len(table.splitlines()), 8)

    def test_dev_sh_is_valid_bash_and_sources_the_resolver(self):
        subprocess.run(["bash", "-n", "scripts/dev.sh"], check=True)
        with open("scripts/dev.sh", encoding="utf-8") as handle:
            self.assertIn(". scripts/resolve_stack.sh", handle.read())


if __name__ == "__main__":
    unittest.main()
