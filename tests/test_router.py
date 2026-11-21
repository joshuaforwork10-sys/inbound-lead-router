import json
from pathlib import Path

import pytest

from router.__main__ import load_context, main
from router.route import company_key, route, sla_due

ROOT = Path(__file__).resolve().parent.parent
LEADS = ROOT / "samples" / "leads"


@pytest.fixture
def ctx(tmp_path):
    return load_context(ROOT / "data", tmp_path / "rr.json")


def lead(**kw):
    base = {"email": "chris@lucernefreight.example", "first_name": "Chris", "last_name": "Hale",
            "company": "Lucerne Freight", "source": "demo_request", "received_at": "2025-06-02T18:10:00Z"}
    base.update(kw)
    return base


def types(plan):
    return [a["type"] for a in plan.actions]


# ---------------------------------------------------------------- normalize + match
def test_normalize_trims_and_lowercases(ctx):
    plan = route(lead(email="  Dana.Lee@Northwind-Logistics.example ", first_name="dana", last_name="LEE"), ctx)
    assert plan.lead["email"] == "dana.lee@northwind-logistics.example"
    assert plan.lead["name"] == "Dana Lee"
    assert plan.account_id == "A-100"


def test_company_key_drops_suffixes_and_punctuation():
    assert company_key("Fabrikam Robotics, Inc.") == "fabrikam robotics"


def test_domain_match_beats_name(ctx):
    plan = route(lead(email="x@northwind-logistics.example", company="Totally Different Name"), ctx)
    assert plan.account_id == "A-100"
    assert any("domain northwind-logistics.example" in p for p in plan.path)


def test_fuzzy_name_match_above_threshold(ctx):
    plan = route(lead(email="pat@gmail.com", company="Fabricam Robotics Inc"), ctx)
    assert plan.account_id == "A-103"


def test_fuzzy_name_below_threshold_is_not_a_match(ctx):
    plan = route(lead(email="pat@gmail.com", company="Fabrikam Foods"), ctx)
    assert plan.account_id is None
    assert plan.outcome == "nurture"


def test_id_hint_alone_never_matches(ctx):
    plan = route(lead(crm_account_id="A-100"), ctx)
    assert plan.account_id is None
    assert plan.outcome == "new_account"
    assert any("ignored account id hint A-100" in p for p in plan.path)


# ---------------------------------------------------------------- guards
def test_guard_open_deal_routes_to_deal_owner_without_new_sequence(ctx):
    plan = route(lead(email="it-lead@contosohealth.example", company="Contoso Health"), ctx)
    assert plan.owner == "Morgan Avery"            # deal owner, not account owner Riley Chen
    assert plan.outcome == "routed_to_deal_owner"
    assert "enroll_sequence" not in types(plan)
    assert types(plan) == ["create_task", "notify"]


def test_guard_dnc_account_suppresses_everything(ctx):
    plan = route(lead(email="ops@tailspintravel.example", company="Tailspin Travel"), ctx)
    assert plan.outcome == "suppressed"
    assert plan.owner is None
    assert types(plan) == ["log_only"]


def test_guard_dnc_email_runs_before_matching(ctx):
    plan = route(lead(email="no-sales@litware.example", company="Litware Analytics"), ctx)
    assert plan.outcome == "suppressed"
    assert plan.account_id is None                 # stopped before any lookup
    assert not any(p.startswith("match") for p in plan.path)


def test_guard_dnc_domain(ctx):
    ctx.config["do_not_contact"]["domains"].append("lucernefreight.example")
    assert route(lead(), ctx).outcome == "suppressed"


def test_guard_personal_domain_skips_domain_match_and_enrichment(ctx):
    plan = route(lead(email="lee.w@yahoo.com", company=""), ctx)
    assert plan.outcome == "nurture"
    assert types(plan) == ["enroll_sequence"]
    assert "enrichment" not in plan.lead
    assert not any(p.startswith("enrich") for p in plan.path)


# ---------------------------------------------------------------- assignment + SLA
def test_round_robin_alternates_within_territory(ctx):
    owners = [route(lead(), ctx).owner for _ in range(3)]
    assert owners == ["Jordan Kim", "Taylor Brooks", "Jordan Kim"]


def test_unknown_region_goes_to_fallback(ctx):
    plan = route(lead(email="a@unknown-co.example", company="Unknown Co"), ctx)
    assert plan.owner == "Inbound queue"


def test_sla_due_by_source():
    assert sla_due("2025-06-02T18:10:00Z", 15) == "2025-06-02T18:25:00Z"
    assert sla_due("2025-06-02T23:30:00Z", 60) == "2025-06-03T00:30:00Z"


def test_cli_plan_mode_saves_no_state(tmp_path, capsys):
    state = tmp_path / "rr.json"
    main([str(LEADS / "06_new_company_stale_id_hint.json"), "--state", str(state)])
    plan = json.loads(capsys.readouterr().out)
    assert plan["owner"] == "Jordan Kim" and not state.exists()
    main([str(LEADS / "06_new_company_stale_id_hint.json"), "--state", str(state), "--commit"])
    assert json.loads(state.read_text()) == {"east-central": 1}
