"""Route one inbound lead to an owner and return the plan of actions. Nothing here writes anywhere."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import List, Optional

SUFFIXES = {"inc", "llc", "ltd", "corp", "corporation", "co", "company", "gmbh", "plc", "the"}


@dataclass
class Context:
    accounts: list
    enrichment: dict
    config: dict
    rr_state: dict = field(default_factory=dict)  # territory -> next index, mutated by assign()


@dataclass
class Plan:
    lead: dict
    path: List[str] = field(default_factory=list)
    outcome: str = ""
    owner: Optional[str] = None
    account_id: Optional[str] = None
    sla_due: Optional[str] = None
    actions: List[dict] = field(default_factory=list)

    def step(self, stage: str, text: str):
        self.path.append(f"{stage:<9} {text}")

    def as_dict(self):
        return {"outcome": self.outcome, "owner": self.owner, "account_id": self.account_id,
                "sla_due": self.sla_due, "decision_path": self.path, "actions": self.actions,
                "lead": self.lead}


# ------------------------------------------------------------------ normalize + enrich
def normalize(raw: dict) -> dict:
    email = (raw.get("email") or "").strip().lower()
    return {
        "email": email,
        "domain": email.rsplit("@", 1)[1] if "@" in email else "",
        "name": " ".join(p.strip().title() for p in (raw.get("first_name", ""), raw.get("last_name", "")) if p.strip()),
        "company": re.sub(r"\s+", " ", (raw.get("company") or "").strip()),
        "source": (raw.get("source") or "contact_sales").strip().lower(),
        "received_at": raw["received_at"],
        "crm_account_id": raw.get("crm_account_id"),
    }


def company_key(name: str) -> str:
    """'Fabrikam Robotics, Inc.' -> 'fabrikam robotics'."""
    words = re.sub(r"[^a-z0-9 ]+", " ", name.lower()).split()
    return " ".join(w for w in words if w not in SUFFIXES)


# ------------------------------------------------------------------ match
def match_account(lead: dict, ctx: Context, plan: Plan, personal: bool):
    """Domain first, then fuzzy company name. An ID hint alone never makes a match."""
    if not personal and lead["domain"]:
        for a in ctx.accounts:
            if a["domain"] == lead["domain"]:
                plan.step("match", f"domain {lead['domain']} = {a['id']} {a['name']}")
                return a
        plan.step("match", f"no account with domain {lead['domain']}")
    best, best_score = None, 0.0
    key = company_key(lead["company"])
    if key:
        for a in ctx.accounts:
            s = SequenceMatcher(None, key, company_key(a["name"])).ratio()
            if s > best_score:
                best, best_score = a, s
        threshold = ctx.config["name_match_threshold"]
        if best and best_score >= threshold:
            plan.step("match", f"name '{lead['company']}' ~ {best['id']} {best['name']} "
                               f"(similarity {best_score:.2f} >= {threshold})")
            return best
        if best:
            plan.step("match", f"closest name {best['name']} at {best_score:.2f} is under {threshold}, no match")
    hint = lead.get("crm_account_id")
    if hint:
        plan.step("match", f"ignored account id hint {hint}: no domain or name evidence")
    return None


# ------------------------------------------------------------------ assign
def territory_for(region: str, employees: int, territories: list):
    for t in territories:
        if "min_employees" in t and employees < t["min_employees"]:
            continue
        if "regions" in t and region not in t["regions"]:
            continue
        return t
    return None


def round_robin(name: str, pool: list, ctx: Context) -> str:
    i = ctx.rr_state.get(name, 0) % len(pool)
    ctx.rr_state[name] = i + 1
    return pool[i]


def sla_due(received_at: str, minutes: int) -> str:
    t = datetime.fromisoformat(received_at.replace("Z", "+00:00")) + timedelta(minutes=minutes)
    return t.isoformat().replace("+00:00", "Z")


# ------------------------------------------------------------------ the router
def route(raw: dict, ctx: Context) -> Plan:
    cfg = ctx.config
    lead = normalize(raw)
    plan = Plan(lead=lead)
    plan.step("normalize", f"{lead['email']} | company '{lead['company']}' | source {lead['source']}")
    src = cfg["sources"].get(lead["source"], cfg["sources"]["contact_sales"])

    # guard: do not contact runs first, before any lookup decides an owner
    dnc = cfg["do_not_contact"]
    if lead["email"] in dnc["emails"] or lead["domain"] in dnc["domains"]:
        return suppress(plan, f"{lead['email']} is on the do-not-contact list")

    personal = lead["domain"] in cfg["personal_domains"]
    if personal:
        plan.step("guard", f"personal email domain {lead['domain']}: skip domain match and enrichment")
        info = None
    else:
        info = ctx.enrichment.get(lead["domain"])
        if info:
            lead["enrichment"] = info
            plan.step("enrich", f"{info['company']} | {info['region']} | {info['employees']} employees | {info['industry']}")
        else:
            plan.step("enrich", f"no enrichment record for {lead['domain']}")
        if not lead["company"] and info:
            lead["company"] = info["company"]
    acct = match_account(lead, ctx, plan, personal)

    if acct and acct.get("do_not_contact"):
        plan.account_id = acct["id"]
        return suppress(plan, f"account {acct['id']} is flagged do-not-contact")

    if acct:
        plan.account_id = acct["id"]
        deal = acct.get("open_deal")
        if deal:
            note = "" if deal["owner"] == acct.get("owner") else f", not account owner {acct.get('owner')}"
            plan.step("guard", f"open deal {deal['id']} ({deal['stage']}) owned by {deal['owner']}{note}: route to deal owner")
            plan.owner, plan.outcome = deal["owner"], "routed_to_deal_owner"
        elif acct.get("owner"):
            plan.step("assign", f"account owner {acct['owner']}")
            plan.owner, plan.outcome = acct["owner"], "routed_to_account_owner"
        else:
            plan.owner = assign_by_territory(acct["region"], acct["employees"], ctx, plan)
            plan.outcome = "routed_by_territory"
    elif personal:
        plan.step("assign", "personal email with no company match: nurture, no rep task")
        plan.outcome = "nurture"
        plan.actions.append({"type": "enroll_sequence", "sequence": cfg["nurture_sequence"], "email": lead["email"]})
        return plan
    else:
        region, employees = (info["region"], info["employees"]) if info else ("", 0)
        plan.owner = assign_by_territory(region, employees, ctx, plan)
        plan.outcome = "new_account"
        plan.actions.append({"type": "create_account", "domain": lead["domain"],
                             "name": (info or {}).get("company") or lead["company"] or lead["domain"],
                             "owner": plan.owner})

    plan.sla_due = sla_due(lead["received_at"], src["sla_minutes"])
    plan.step("sla", f"{lead['source']} = {src['sla_minutes']} min, due {plan.sla_due}")
    plan.actions.append({"type": "create_task", "owner": plan.owner, "due": plan.sla_due,
                         "title": f"Respond to {lead['source'].replace('_', ' ')}: {lead['name'] or lead['email']}"})
    if plan.outcome == "routed_to_deal_owner":
        plan.step("plan", "active deal: no new sequence, the deal owner follows up by hand")
    else:
        plan.actions.append({"type": "enroll_sequence", "sequence": src["sequence"], "email": lead["email"],
                             "sender": plan.owner})
    plan.actions.append({"type": "notify", "to": plan.owner,
                         "message": f"New {lead['source']} from {lead['email']}, respond by {plan.sla_due}"})
    return plan


def assign_by_territory(region: str, employees: int, ctx: Context, plan: Plan) -> str:
    t = territory_for(region, employees, ctx.config["territories"])
    if t is None:
        owner = round_robin("fallback", ctx.config["fallback_pool"], ctx)
        plan.step("assign", f"no territory for region '{region}': fallback {owner}")
        return owner
    owner = round_robin(t["name"], t["pool"], ctx)
    plan.step("assign", f"territory {t['name']} -> round-robin {owner} (pool of {len(t['pool'])})")
    return owner


def suppress(plan: Plan, reason: str) -> Plan:
    plan.step("guard", f"{reason}: suppress all outreach")
    plan.outcome = "suppressed"
    plan.actions.append({"type": "log_only", "reason": reason})
    return plan
