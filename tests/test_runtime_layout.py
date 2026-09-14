"""The tower is one object assembled from mixins. This pins what the assembly must not lose.

Splitting backend/service.py into per-concern mixins moves method bodies between files. Two
things can go wrong without any other test going red: two mixins can define the same name (the
later base silently wins, with no error), and a decorator sitting one line above its `def` can
be left behind by a line-range cut. `ready` losing @property would make it a bound method —
always truthy in the judgement gate, and unserialisable in GET /health.
"""

import inspect
import unittest

from backend.runtime.tower import Runtime

# Every method class Runtime had before the split. All of them must still resolve on it.
BEFORE_THE_SPLIT = (
    "__init__", "ready", "file", "_judge_and_commit", "_park_for_human", "_close_card",
    "_deny", "_record_refusal", "_judge_legs", "_notice_until", "_advise",
    "_ledger_advisory", "_airspace_denial", "_traffic_denial", "_dark_denial",
    "_contingency_problem", "_context", "check_route", "_endpoint_problem", "_note_endpoint",
    "_note_block", "_column_breach", "_departure", "_intend", "_check_traffic", "_others",
    "_occupants", "_end_intent", "_observe", "_ledger_nonconformance", "_withdraw",
    "_on_committed", "_rejudge", "_fresh_refusal", "_queue_or_commit", "approve",
    "_answer_card", "_settle_contended", "_settle_batches", "_pull_world", "_load_airspace",
    "_follow_round", "_expire_cards", "service_bbox", "absorb", "_settle_notice",
    "_read_later", "_collect_read_notices", "_enforce_policy", "_apply_notices",
    "_apply_notices_locked", "_lapse_held", "_hold_notice", "_confirm_notice",
    "_ledger_notice", "take_in", "_note_fetch", "_ledger_source_change", "take_metar",
    "_note_metar_fetch", "submit_intake", "_drain_intake_inbox", "_take_intake",
    "_read_intake_later", "_collect_read_intake", "_settle_intake", "_apply_intake",
    "_take_weather", "_open_weather_hold", "_ground_for_hold", "_raise_lift_card",
    "_refresh_lift_card", "_hold_weather", "_confirm_weather", "_confirm_lift", "_lift_hold",
    "_tick_intake", "_ledger_hold_end", "_drop_card", "_take_incident", "_take_notice",
    "_ledger_incident", "_ledger_intake", "_store_intake", "_rule_open", "_rule_apply",
    "_rule_extend", "_rule_close", "_close_rules", "background", "settle_forever",
    "start_background", "recall_flights", "revoke_under", "register_agent",
    "agents_snapshot", "watch_links", "_link_lost", "_raise_link_card", "_link_restored",
    "_ledger_link", "_ledger_link_nonconformance", "_drop_link_card", "_confirm_lost_link",
    "snapshot", "report", "_intake_snapshot", "_links_snapshot", "_autopilots_snapshot",
)

STATIC = ("_airspace_denial", "_traffic_denial", "_note_endpoint", "_note_block")


def mixins():
    return [base for base in Runtime.__mro__ if base.__name__.endswith("Mixin")]


class MixinNamesAreUniqueTest(unittest.TestCase):
    def test_no_method_name_is_defined_by_two_mixins(self):
        """MRO shadowing is silent: the first base wins and nothing complains. A name defined
        twice would run one body while a reader reads the other."""
        seen = {}
        clashes = []
        for base in mixins():
            for name, value in vars(base).items():
                if name.startswith("__"):
                    continue
                if not callable(value) and not isinstance(value, (property, staticmethod)):
                    continue
                if name in seen:
                    clashes.append(f"{name}: {seen[name]} and {base.__name__}")
                seen[name] = base.__name__
        self.assertEqual(clashes, [])

    def test_every_method_from_before_the_split_still_resolves(self):
        missing = [name for name in BEFORE_THE_SPLIT if not hasattr(Runtime, name)]
        self.assertEqual(missing, [])
        self.assertEqual(len(set(BEFORE_THE_SPLIT)), 109)


class DecoratorsSurvivedTheCutTest(unittest.TestCase):
    def test_ready_is_still_a_property(self):
        """As a bound method it is always truthy, so the judgement gate would pass while the
        airspace is still loading, and GET /health could not be serialised."""
        self.assertIsInstance(inspect.getattr_static(Runtime, "ready"), property)

    def test_the_four_staticmethods_are_still_static(self):
        for name in STATIC:
            with self.subTest(method=name):
                self.assertIsInstance(inspect.getattr_static(Runtime, name), staticmethod)


if __name__ == "__main__":
    unittest.main()
