"""One holder per resource. A landing pad is not a suggestion."""

import time
from dataclasses import dataclass


@dataclass
class Hold:
    resource: str
    asset_id: str
    proposal_id: str
    since: float


class LockTable:
    def __init__(self, resources: list[str] | None = None):
        self.known = set(resources or [])
        self._holds: dict[str, Hold] = {}

    def holder(self, resource: str) -> Hold | None:
        return self._holds.get(resource)

    def acquire(self, resource: str, asset_id: str, proposal_id: str) -> bool:
        current = self._holds.get(resource)
        if current and current.asset_id != asset_id:
            return False
        self._holds[resource] = Hold(resource, asset_id, proposal_id, time.time())
        return True

    def release(self, resource: str, asset_id: str) -> bool:
        current = self._holds.get(resource)
        if current and current.asset_id == asset_id:
            del self._holds[resource]
            return True
        return False

    def release_all(self, asset_id: str) -> None:
        for resource, hold in list(self._holds.items()):
            if hold.asset_id == asset_id:
                del self._holds[resource]

    def snapshot(self) -> dict:
        return {
            resource: {"asset_id": hold.asset_id, "since": hold.since}
            for resource, hold in self._holds.items()
        }
