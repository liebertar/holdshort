"""Talks to the simulated city. Swap this file for real pads and the runtime does not change."""

from shared.http import get_json, post_json


class FleetSimAdapter:
    def __init__(self, base_url: str, world: str = "guarded"):
        self.base_url = base_url.rstrip("/")
        self.world = world

    def execute(
        self,
        asset_id: str,
        action: str,
        params: dict,
        ledger_id: str,
        blast: str = "none",
        approved_by: str | None = None,
    ) -> dict:
        response = post_json(
            f"{self.base_url}/act",
            {
                "world": self.world,
                "asset": asset_id,
                "action": action,
                "params": params,
                "ledger_id": ledger_id,
                "blast": blast,
                "approved_by": approved_by,
            },
        )
        return response or {"ok": False, "error": "sim unreachable"}

    def telemetry(self) -> dict:
        state = get_json(f"{self.base_url}/state?world={self.world}")
        return state or {}
