"""CLI.

  python3 -m router samples/leads/01_demo_request.json        JSON plan, nothing saved
  python3 -m router samples/leads/*.json --text               readable decision path per lead
  python3 -m router lead.json --commit                         also save the round-robin pointer
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .route import Context, route

ROOT = Path(__file__).resolve().parent.parent


def load_context(data: Path, state_file: Path) -> Context:
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    return Context(accounts=json.loads((data / "accounts.json").read_text()),
                   enrichment=json.loads((data / "enrichment.json").read_text()),
                   config=json.loads((data / "routing.json").read_text()),
                   rr_state=state)


def show(name: str, plan) -> str:
    out = [f"== {name}", *[f"   {p}" for p in plan.path], f"   -> {plan.outcome}, owner {plan.owner}"]
    for a in plan.actions:
        detail = ", ".join(f"{k}={v}" for k, v in a.items() if k != "type")
        out.append(f"   would {a['type']}: {detail}")
    return "\n".join(out)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="router", description="Plan how to route inbound leads. Writes nothing by default.")
    p.add_argument("leads", nargs="+", type=Path, help="lead JSON files")
    p.add_argument("--data", type=Path, default=ROOT / "data")
    p.add_argument("--state", type=Path, default=ROOT / "state" / "round_robin.json")
    p.add_argument("--text", action="store_true", help="readable decision path instead of JSON")
    p.add_argument("--commit", action="store_true", help="save the round-robin pointer after planning")
    args = p.parse_args(argv)

    ctx = load_context(args.data, args.state)
    plans = []
    for f in args.leads:
        plan = route(json.loads(f.read_text()), ctx)
        plans.append(plan)
        if args.text:
            print(show(f.name, plan) + "\n")
    if not args.text:
        body = [pl.as_dict() for pl in plans]
        print(json.dumps(body[0] if len(body) == 1 else body, indent=2))
    if args.commit:
        args.state.parent.mkdir(parents=True, exist_ok=True)
        args.state.write_text(json.dumps(ctx.rr_state, indent=2, sort_keys=True))
        print(f"saved round-robin state to {args.state}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
