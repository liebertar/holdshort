"""Which intake item opened which rule row, and how that row is closed again.

Kept in backend/intake because an item is what opens a rule. It stays out of backend/store:
that layer is the repository and should be importable with no Runtime mixin in it.
"""


class RulesMixin:
    def _rule_open(self, item_id: str, kind: str, from_tick, until_tick, applied: bool) -> None:
        self._rule_ids[item_id] = self.store.open_rule(item_id, kind, from_tick, until_tick,
                                                       applied)

    def _rule_apply(self, item_id: str, kind: str | None = None, from_tick=None,
                    until_tick=None) -> None:
        """A rule applied. Mark its held row applied if there is one; otherwise (given kind)
        open a new row."""
        self.store.decide_item(item_id, "approved")
        rule = self._rule_ids.get(item_id)
        if rule is not None:
            self.store.apply_rule(rule, from_tick)
        elif kind is not None:
            self._rule_open(item_id, kind, from_tick, until_tick, True)

    def _rule_extend(self, item_id: str, until_tick: int) -> None:
        self.store.extend_rule(self._rule_ids.get(item_id), until_tick)

    def _rule_close(self, item_id: str, lifted_by: str, until_tick: int | None = None) -> None:
        """Close the rule row and, if the item was waiting for a human, record how it ended
        (refused, lapsed, round changed). Otherwise a restart raises an answered card again."""
        self.store.close_rule(self._rule_ids.pop(item_id, None), lifted_by, until_tick)
        self.store.decide_item(item_id, lifted_by)

    def _close_rules(self, lifted_by: str) -> None:
        for key in list(self._rule_ids):
            self._rule_close(key, lifted_by)
