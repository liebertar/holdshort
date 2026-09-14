"""The ledger report folds ledger lines back into flights. It reads nothing but the ledger."""

import json
import tempfile
import unittest

from backend.runtime.tower import Runtime
from backend.store.ledger import Ledger
from backend.store.reports.ledger import build_report, to_markdown
from shared.models import Proposal, Verdict
from shared.notam import format_dms
from sim import world as sim_world

CONFIG = "configs/fleet.yaml"
ZONE_ITEM = {"id": "nofly-t", "kind": "notam", "name": "test corridor", "text": sim_world.ZONE_TEXT,
             "published_tick": 525, "until_tick": 900}
HERE = (40.7100, -73.9855)


class RecordingAdapter:
    def __init__(self):
        self.sent = []

    def execute(self, asset_id, action, params, ledger_id, blast="none", approved_by=None):
        json.dumps(params)
        self.sent.append((asset_id, action, params))
        return {"ok": True}

    def telemetry(self):
        return {}


def make_runtime():
    with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
        runtime = Runtime(CONFIG, "http://unused", handle.name, 0.0)
    adapter = RecordingAdapter()
    runtime.adapter = adapter
    runtime.committer.adapter = adapter
    runtime.advisory_async = False
    runtime.landing_areas = sim_world.LANDING_AREAS
    runtime.telemetry = {"drone-01": {"lat": HERE[0], "lon": HERE[1], "alt_m": 0.0},
                         "drone-02": {"lat": 40.7100, "lon": -73.9700, "alt_m": 0.0}}
    runtime.tick = 600
    return runtime, adapter


def legs(points, alt_m):
    return [{"lat": lat, "lon": lon, "alt_m": alt_m} for lat, lon in points]


THROUGH_ZONE = legs([HERE, (40.7225, -73.9855), (40.7350, -73.9855)], 60)
DETOUR = legs([HERE, (40.7100, -73.9950), (40.7350, -73.9950)], 60)


class ReportTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()
        self.runtime.absorb([ZONE_ITEM])
        # drone-01: the straight line is refused and a detour filed under the same id is
        # approved — one flight, four ledger lines.
        self.first = Proposal(asset_id="drone-01", action="fly_route", cost_usd=12.0,
                              blast_radius="schedule", rationale="delivery",
                              params={"legs": THROUGH_ZONE, "drafter": "straight",
                                      "draft_attempts": 0})
        self.assertIs(self.runtime.file(self.first.to_dict()).verdict, Verdict.DENIED)
        self.first.params = {"legs": DETOUR, "drafter": "astar", "draft_attempts": 2}
        approved = self.runtime.file(self.first.to_dict())
        self.assertTrue(approved.committed)
        self.intent = self.runtime.intents.get("drone-01")
        # In flight, a new zone closes over the detour → recall. Before that, one record of
        # taking off ahead of its window too.
        self.runtime._ledger_nonconformance(self.intent, self.intent.depart_tick)
        self.runtime.tick = 650
        self.runtime.telemetry["drone-01"] = {"lat": 40.7150, "lon": -73.9950, "alt_m": 60.0,
                                              "route": [{"lat": 40.7350, "lon": -73.9950,
                                                         "alt_m": 60.0}]}
        corners = " ".join(format_dms(lat, lon) for lat, lon in
                           ((40.7180, -73.9970), (40.7180, -73.9930), (40.7220, -73.9930),
                            (40.7220, -73.9970)))
        self.runtime.absorb([ZONE_ITEM, {"id": "nofly-r", "kind": "notam", "name": "second zone",
                                         "text": f"AREA BOUNDED BY {corners} SFC-400FT AGL "
                                                 "TICK 640-800", "until_tick": 800}])
        self.assertEqual([s[:2] for s in self.adapter.sent if s[1] == "divert_ground"],
                         [("drone-01", "divert_ground")])
        # drone-02: refused three times, then gets an advisory.
        self.runtime.tick = 660
        for _ in range(3):
            self.runtime.file(Proposal(asset_id="drone-02", action="fly_route", cost_usd=12.0,
                                       blast_radius="schedule", rationale="",
                                       params={"legs": legs([(40.7100, -73.9700),
                                                             (40.7225, -73.9855),
                                                             (40.7350, -73.9855)], 60)}
                                       ).to_dict())

    def test_flights_fold_refusals_approval_conformance_and_recall(self):
        report = self.runtime.report()
        self.assertEqual((report["generated_tick"], report["airspace_revision"]),
                         (660, self.runtime.airspace.revision))
        self.assertEqual([a["asset"] for a in report["assets"]], ["drone-01", "drone-02"])
        flights = report["assets"][0]["flights"]
        self.assertEqual(len(flights), 1, "a refiling under the same id is one flight")
        flight = flights[0]
        self.assertEqual((flight["proposal"], flight["intent"], flight["action"]),
                         (self.first.id, self.intent.id, "fly_route"))
        self.assertEqual((flight["filed_tick"], flight["author"], flight["drafter"],
                          flight["draft_attempts"]), (600, "rules", "astar", 2))
        self.assertEqual(flight["filed_at"], self.first.filed_at)
        self.assertEqual(flight["checks_run"][:3], ["dedupe", "form", "endpoints"])
        self.assertEqual([(r["tick"], r["code"], r["blocked_kind"], r["blocked_volume"],
                           r["blocked_asset"]) for r in flight["refusals"]],
                         [(600, "airspace", "forbidden", "nofly-t", None)])
        self.assertEqual((flight["approved"]["tick"], flight["approved"]["code"],
                          flight["approved"]["resolution"], flight["approved"]["holding_for"],
                          flight["approved"]["altitude_shift_m"]),
                         (600, "within_limits", None, None, None))
        self.assertEqual([c["planned_depart_tick"] for c in flight["conformance"]],
                         [self.intent.depart_tick])
        self.assertEqual((flight["recalled"]["tick"], flight["recalled"]["policy"],
                          flight["recalled"]["outcome"]), (650, "nofly-r", "done"))
        self.assertIsNone(flight["withdrawn"])
        second = report["assets"][1]
        self.assertEqual(len(second["flights"]), 3)
        self.assertTrue(all(f["approved"] is None and len(f["refusals"]) == 1
                            for f in second["flights"]))
        self.assertEqual([(a["trigger"], a["chosen"], a["source"], a["refusals"])
                          for a in second["advisories"]],
                         [("refusals", "notice_window", "rules", 3)])
        self.assertEqual(report["assets"][0]["advisories"], [])

    def test_one_asset_and_markdown(self):
        only = self.runtime.report("drone-02")
        self.assertEqual([a["asset"] for a in only["assets"]], ["drone-02"])
        self.assertEqual(self.runtime.report("drone-09")["assets"], [])
        text = self.runtime.report(fmt="md")
        self.assertTrue(text.startswith("# Ledger report — tick 660"))
        rows = [line for line in text.splitlines() if line.startswith("| drone-01 |")]
        self.assertEqual(len(rows), 1, "one flight is one row")
        row = rows[0]
        for piece in (f"fly_route {self.first.id}", "tick 600", "rules", "astar", "| 2 |",
                      "t600 forbidden:nofly-t", "tick 600 (within_limits)", "tick 650 (nofly-r)"):
            self.assertIn(piece, row)
        self.assertEqual(len([line for line in text.splitlines()
                              if line.startswith("| drone-02 |")]), 3 + 1)
        self.assertIn("## Advisories", text)
        self.assertIn("| drone-02 | 660 | refusals | notice_window | rules |", text)

    def test_duplicates_and_failed_executions_are_their_own_columns_and_cells_are_escaped(self):
        entries = self.runtime.ledger.read_all()
        flight = next(e for e in reversed(entries)
                      if e["proposal"]["action"] == "fly_route")["proposal"]
        duplicate = {"id": "l_dup", "outcome": "denied", "context": {"tick": 661},
                     "proposal": flight, "decision": {"verdict": "denied", "code": "duplicate"}}
        failed = {"id": "l_fail", "outcome": "failed: not on a pad", "context": {"tick": 662},
                  "proposal": {**flight, "id": "p_failed"},
                  "decision": {"verdict": "auto", "code": "within_limits", "committed": False}}
        advisory = {"id": "l_adv", "outcome": "noted", "context": {"tick": 663},
                    "proposal": {"asset_id": "drone-02", "action": "advisory",
                                 "params": {"trigger": "refusals", "chosen": "hold",
                                            "source": "super",
                                            "summary": "Hold now | then\nrefile"}},
                    "decision": {"verdict": "auto", "code": "advisory"}}
        report = build_report(entries + [duplicate, failed, advisory], 700, 1, "drone-02")
        flights = report["assets"][0]["flights"]
        last = next(f for f in flights if f["proposal"] == flight["id"])
        self.assertEqual((last["duplicates"], len(last["refusals"])), (1, 1),
                         "duplicate refusals are counted apart from the refusal list")
        broken = next(f for f in flights if f["proposal"] == "p_failed")
        self.assertIsNone(broken["approved"])
        self.assertEqual(broken["failed"]["outcome"], "failed: not on a pad")
        text = to_markdown(report)
        table = text.split("## Advisories")[0]
        rows = [line for line in table.splitlines() if line.startswith("| drone-02 |")]
        header = next(line for line in table.splitlines() if line.startswith("| asset |"))
        self.assertTrue(all(row.count("|") == header.count("|") for row in rows), rows)
        advice_row = next(line for line in text.splitlines() if "Hold now" in line)
        self.assertIn("Hold now \\| then refile", advice_row)
        self.assertEqual(advice_row.count("|") - advice_row.count("\\|"), 7)
        self.assertIn("(failed: not on a pad)", text)

    def test_it_is_built_from_the_file_not_from_memory(self):
        self.runtime.ledger._recent.clear()
        self.assertEqual(len(self.runtime.report()["assets"]), 2)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as handle:
            empty = Ledger(handle.name)
        self.assertEqual(empty.read_all(), [])
        self.assertEqual(build_report([], 0, 0)["assets"], [])
        self.assertTrue(to_markdown(build_report([], 0, 0)).startswith("# Ledger report"))
        lines = self.runtime.ledger.read_all()
        with open(self.runtime.ledger.path, encoding="utf-8") as handle:
            self.assertEqual(len(lines), sum(1 for _ in handle))
        self.assertTrue(all("proposal" in line and "decision" in line for line in lines))


if __name__ == "__main__":
    unittest.main()
