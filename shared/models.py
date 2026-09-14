"""The eight nouns. Shared by agents and runtime; neither side may add fields at will."""

import time
import uuid
from dataclasses import asdict, dataclass, field
from enum import Enum

from shared.config import model_display

# Blast radius ranking. When the arbiter falls back to rules, it picks in this order.
BLAST_RANK = {"none": 0, "schedule": 1, "cargo": 2, "passenger": 3, "public": 4}


class Verdict(str, Enum):
    AUTO = "auto"        # within limits; it runs
    QUEUED = "queued"    # waiting for a resource; nothing has happened yet
    HUMAN = "human"      # a person must look at it
    DENIED = "denied"


@dataclass
class Proposal:
    """The only thing an agent can submit. It carries no power to execute."""

    asset_id: str
    action: str
    cost_usd: float
    blast_radius: str
    rationale: str
    params: dict = field(default_factory=dict)
    resource: str | None = None
    author: str = "rules"
    world: str = "guarded"
    id: str = field(default_factory=lambda: f"p_{uuid.uuid4().hex[:10]}")
    filed_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict) -> "Proposal":
        allowed = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in allowed})

    def validate(self) -> list[str]:
        problems = []
        if not self.asset_id or not self.action:
            problems.append("asset_id and action are required")
        if self.blast_radius not in BLAST_RANK:
            problems.append(f"unknown blast_radius: {self.blast_radius}")
        if self.cost_usd < 0:
            problems.append("cost_usd must not be negative")
        return problems


@dataclass
class Decision:
    proposal_id: str
    verdict: Verdict
    reason: str
    policy_hit: str | None = None
    forbids: str | None = None      # what is forbidden: an action name or a resource name
    authority_hit: str | None = None
    arbiter: str | None = None
    approved_by: str | None = None
    ledger_id: str | None = None
    committed: bool = False
    # The human-readable sentence stays in reason. The screen builds its own wording from
    # code and detail — so rewording never silently breaks the screen.
    code: str = ""
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        out = asdict(self)
        out["verdict"] = self.verdict.value
        return out


@dataclass
class LedgerEntry:
    """The record is written first, then the action runs. If the order flips, nothing runs."""

    proposal: dict
    decision: dict
    id: str = field(default_factory=lambda: f"l_{uuid.uuid4().hex[:12]}")
    at: float = field(default_factory=time.time)
    outcome: str = "pending"
    # Judgement context: at which tick, on which airspace revision, under which policies, and
    # which checks ran in which order. {tick, airspace_revision, policies: [id], intent_id,
    # checks_run: [name]}. "Why was it judged that way then?" must be answerable from the
    # ledger alone — the current state says nothing about the state back then.
    context: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


# The form an aircraft process registers itself with. It only says what it runs (model,
# server), and the runtime never uses it in judgement — it's a label so the screen can show
# which model writes each aircraft's filings.
AGENT_WORLDS = ("guarded", "direct")
AGENT_HOSTS = ("ollama", "nebius", "other", "off")


@dataclass
class AgentIdentity:
    asset_id: str
    world: str                       # guarded | direct
    model: str = ""                  # id of the model writing filings; empty means rules only
    host: str = "off"                # ollama | nebius | other | off
    base_url_port: int | None = None
    # Has the model's answer been used in a filing even once? False means it's only configured
    # and the rules write the filings (fake key, network down, answers always discarded).
    # Older agents that don't say (None) are trusted.
    model_ok: bool | None = None

    @property
    def display(self) -> str:
        """Screen label. Naming a model that has never answered would be a lie, so then it
        is rules."""
        return model_display(self.model if self.model_ok is not False else "")

    def to_dict(self) -> dict:
        return {**asdict(self), "display": self.display}

    @classmethod
    def from_dict(cls, raw: dict) -> "AgentIdentity":
        """Form check: ValueError on a missing aircraft name or an unlisted world or host."""
        asset_id = " ".join(str(raw.get("asset_id") or "").split())[:80]
        if not asset_id:
            raise ValueError("asset_id is empty")
        world = str(raw.get("world") or "guarded")
        if world not in AGENT_WORLDS:
            raise ValueError(f"world must be one of {'|'.join(AGENT_WORLDS)}")
        host = str(raw.get("host") or "off")
        if host not in AGENT_HOSTS:
            raise ValueError(f"host must be one of {'|'.join(AGENT_HOSTS)}")
        port = raw.get("base_url_port")
        try:
            port = int(port) if port not in (None, "") else None
        except (TypeError, ValueError) as error:
            raise ValueError("base_url_port must be a number") from error
        model_ok = raw.get("model_ok")
        if model_ok is not None and not isinstance(model_ok, bool):
            raise ValueError("model_ok must be true | false")
        return cls(asset_id=asset_id, world=world, model=str(raw.get("model") or "")[:120],
                   host=host, base_url_port=port, model_ok=model_ok)
