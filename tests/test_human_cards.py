"""A HUMAN verdict is a runtime decision: it is ledgered, and its card does not outlive the round.

The card's ledger entry opens when the runtime parks the request and closes with the person's
answer (approved / denied), or as lapsed when the round ends first. A repeat filing while the
card stands returns the same decision and still leaves a line (outcome waiting).
"""

import json
import tempfile
import unittest

from backend.runtime.tower import Runtime
from shared.models import Proposal, Verdict
from sim import world as sim_world

CONFIG = "configs/fleet.yaml"
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
    runtime.notice_async = False
    runtime.landing_areas = sim_world.LANDING_AREAS
    runtime.telemetry = {"drone-01": {"lat": HERE[0], "lon": HERE[1], "alt_m": 0.0}}
    runtime.tick = 600
    return runtime, adapter


def lines(runtime):
    with open(runtime.ledger.path, encoding="utf-8") as handle:
        return [json.loads(line) for line in handle]


def rows_for(runtime, proposal_id):
    return [(e["id"], e["outcome"], e["decision"]["verdict"], e["decision"]["code"])
            for e in lines(runtime) if e["proposal"]["id"] == proposal_id]


def open_ids(runtime):
    opened = {e["id"] for e in lines(runtime) if e["outcome"] == "pending"}
    closed = {e["id"] for e in lines(runtime) if e["outcome"] != "pending"}
    return opened - closed


def public_route(rationale="over people"):
    return Proposal(asset_id="drone-01", action="fly_route", cost_usd=12.0,
                    blast_radius="public", rationale=rationale,
                    params={"legs": [{"lat": HERE[0], "lon": HERE[1], "alt_m": 60},
                                     {"lat": 40.7000, "lon": -73.9855, "alt_m": 60}]})


class HumanCardLedgerTest(unittest.TestCase):
    def setUp(self):
        self.runtime, self.adapter = make_runtime()

    def test_the_card_opens_a_line_and_the_persons_answer_closes_it(self):
        first = public_route()
        decision = self.runtime.file(first.to_dict())
        self.assertIs(decision.verdict, Verdict.HUMAN)
        rows = rows_for(self.runtime, first.id)
        self.assertEqual(rows, [(rows[0][0], "pending", "human", "human_blast")])
        # A repeat filing while the same card stands: same decision, still its own line
        repeat = public_route()
        again = self.runtime.file(repeat.to_dict())
        self.assertEqual((again.verdict, again.proposal_id), (Verdict.HUMAN, first.id))
        self.assertEqual([r[1:] for r in rows_for(self.runtime, repeat.id)],
                         [("pending", "human", "human_blast"), ("waiting", "human", "human_blast")])
        self.assertEqual(len(self.runtime.snapshot()["awaiting_human"]), 1)
        # Approval: the card's line closes, and the execution leaves its own line
        approved = self.runtime.approve(first.id, "controller", allow=True)
        self.assertTrue(approved.committed)
        rows = rows_for(self.runtime, first.id)
        self.assertEqual([r[1] for r in rows], ["pending", "approved", "pending", "done"])
        self.assertEqual(rows[0][0], rows[1][0], "closes the same line that opened the card")
        closed_card = next(e for e in lines(self.runtime) if e["id"] == rows[0][0]
                           and e["outcome"] == "approved")
        self.assertEqual(closed_card["decision"]["approved_by"], "controller")
        self.assertEqual(closed_card["context"]["checks_run"][-1], "human")
        self.assertEqual(open_ids(self.runtime), set())

    def test_a_refusal_closes_the_card_line_as_denied(self):
        first = public_route()
        self.runtime.file(first.to_dict())
        denied = self.runtime.approve(first.id, "controller", allow=False)
        self.assertIs(denied.verdict, Verdict.DENIED)
        rows = rows_for(self.runtime, first.id)
        self.assertEqual([r[1] for r in rows], ["pending", "denied"])
        self.assertEqual(rows[0][0], rows[1][0])
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(open_ids(self.runtime), set())

    def test_a_round_change_takes_the_card_down_and_closes_it_as_lapsed(self):
        first = public_route()
        self.runtime.file(first.to_dict())
        self.assertEqual(len(self.runtime.snapshot()["awaiting_human"]), 1)
        self.runtime._follow_round(2)
        self.assertEqual(self.runtime.snapshot()["awaiting_human"], [])
        rows = rows_for(self.runtime, first.id)
        self.assertEqual([(r[1], r[2], r[3]) for r in rows],
                         [("pending", "human", "human_blast"), ("lapsed", "denied", "card_lapsed")])
        self.assertIsNone(self.runtime.approve(first.id, "controller", allow=True),
                          "a card that was taken down cannot be approved")
        self.assertEqual(open_ids(self.runtime), set())


if __name__ == "__main__":
    unittest.main()
