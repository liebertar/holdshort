#!/usr/bin/env python3
"""Ask what a rule would have done, before you make it real.

    python3 scripts/what_if.py --ledger .run/ledger.jsonl \
        --forbid-action reserve_pad --model dv-x500

    python3 scripts/what_if.py --ledger .run/ledger.jsonl --per-asset 100

Every row the ledger already holds is judged again under the rule, and the answer says which
decisions would have changed. Nothing is written, and no aircraft hears about it.
"""

import argparse

from backend.store.replay import replay
from shared import config as config_module
from shared.config import Policy


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ledger", default=".run/ledger.jsonl")
    parser.add_argument("--config", default="configs/fleet.yaml")
    parser.add_argument("--forbid-action")
    parser.add_argument("--forbid-resource")
    parser.add_argument("--model", help="apply to this model only")
    parser.add_argument("--per-asset", type=float)
    parser.add_argument("--fleet", type=float)
    args = parser.parse_args()

    fleet = config_module.load(args.config)
    authority = fleet.authority
    if args.per_asset is not None:
        authority.per_asset_usd = args.per_asset
    if args.fleet is not None:
        authority.fleet_usd = args.fleet

    policies = list(fleet.policies)
    if args.forbid_action or args.forbid_resource:
        policies.append(Policy(
            id="what-if", reason="rule under review",
            forbid_action=args.forbid_action,
            forbid_resource=args.forbid_resource,
            applies_to={"model": args.model} if args.model else {},
        ))

    # The model (--model) is applied to every asset in the ledger.
    result = replay(args.ledger, authority, policies,
                    telemetry={a: {"model": args.model} for a in _assets(args.ledger)}
                    if args.model else None)

    print("What this rule would have done to the past record\n")
    for key, value in result.summary().items():
        print(f"  {key:17} {value}")
    for label, changes in (("newly denied", result.newly_denied),
                           ("newly to a human", result.newly_human),
                           ("newly allowed", result.newly_allowed)):
        if not changes:
            continue
        print(f"\n{label}:")
        for change in changes[:10]:
            print(f"  {change.asset_id:8} {change.action:18} ${change.cost_usd:>5.0f}"
                  f"  {change.was} → {change.now}")
            print(f"           {change.reason}")
    return 0


def _assets(ledger_path: str) -> set:
    from backend.store.replay import read_commits

    return {e["proposal"]["asset_id"] for e in read_commits(ledger_path)}


if __name__ == "__main__":
    raise SystemExit(main())
