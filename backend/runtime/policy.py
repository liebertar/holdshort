"""Fleet-wide bans. Checked before limits, and before any model is consulted."""

from shared.config import Policy


class PolicyBook:
    def __init__(self, policies: list[Policy] | None = None):
        self._policies: list[Policy] = list(policies or [])

    def add(self, policy: Policy) -> None:
        self._policies = [p for p in self._policies if p.id != policy.id] + [policy]

    def remove(self, policy_id: str) -> None:
        """The loosening side. Called only by a human's answer or the end of the window.

        The code never lifts a ban on its own.
        """
        self._policies = [p for p in self._policies if p.id != policy_id]

    def all(self) -> list[Policy]:
        return list(self._policies)

    def clear(self) -> None:
        """Emptied on a new round; notices are reposted in that round and apply again."""
        self._policies = []

    def active(self, tick: int) -> list[Policy]:
        return [p for p in self._policies if tick >= p.active_from_tick]

    def hit(
        self, action: str, resource: str | None, asset: dict, tick: int
    ) -> Policy | None:
        for policy in self._policies:
            if policy.matches(action, resource, asset, tick):
                return policy
        return None
