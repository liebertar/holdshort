"""The only code that touches the world. Imported by backend.runtime.commit and nothing else."""

from backend.adapters.fleet_sim import FleetSimAdapter

__all__ = ["FleetSimAdapter", "build"]


def build(kind: str, **kwargs):
    """Which world the runtime is wired to. Everything above this line stays the same."""
    if kind == "mavlink":
        from backend.adapters.mavlink_fleet import from_env

        return from_env()
    if kind == "composite":
        # The simulator is the world of record for all four aircraft; one of them also flies
        # on a real PX4.
        from backend.adapters.composite import from_env

        return from_env(kwargs["sim_url"], world=kwargs.get("world", "guarded"),
                        journal_path=kwargs.get("journal_path"))
    return FleetSimAdapter(kwargs["sim_url"], world=kwargs.get("world", "guarded"))
