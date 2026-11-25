"""fix-seats: real seats shared by Network Recovery and Passenger Care, the cancellation guard, no cancelling on a guess, and
today's thinning waiting for a pending recommendation (orchestrated mode)."""
from datetime import datetime, timedelta

import pytest

from backend.rules import load_rules
from backend.rules.check import check
from backend.rules.seats import free, held_by_advice, inventory, stranded_if_cancelled

T = datetime.fromisoformat("2026-10-06T14:00:00-05:00")


def ev(i, fid, sellable, booked=100, seats=143, minute=0):
    return {"id": i, "occurred_at": T + timedelta(minutes=minute), "type": "seats_updated",
            "payload": {"flight_id": fid, "seats": seats, "booked": booked, "sellable": sellable}}


def test_the_seat_helper_counts_the_latest_inventory_minus_what_is_promised():
    inv = inventory([ev(1, "WN1041", 30), ev(2, "WN1041", 12, minute=5), ev(3, "WN1044", 4)])
    assert inv["WN1041"]["sellable"] == 12                                   # the latest inventory up to T
    held = held_by_advice([{"flight_id": "WN1041", "passengers": ["P1", "P2"]}])
    assert free("WN1041", inv, held) == 10 and free("WN9999", inv, held) == 0  # unknown is never free
    est = stranded_if_cancelled(30, ["WN1041", "WN1044"], inv, held)
    assert est == {"passengers": 30, "rebooked_today": 14, "stranded": 16}


def test_the_rule_check_blocks_a_cancellation_without_its_estimate_or_one_that_strands_more():
    rules = load_rules()
    base = {"agent": "network_recovery", "action": "cancel_flight", "decision_ids": ["B03"], "data_as_of": T, "flight_id": "WN1032"}
    no_est = check({**base, "option": {}}, [], {"now": T}, rules)
    assert no_est["result"] == "block" and "cancellation_must_not_strand_more" in no_est["rule_ids"]
    worse = check({**base, "option": {"cancellation": {"stranded": 40, "alternative": "delay or keep as scheduled",
                                                       "alternative_stranded": 0, "no_alternative": False}}}, [], {"now": T}, rules)
    assert worse["result"] == "block" and worse["needs_human"] == "network_operations_control"
    assert any("40" in r and "0" in r for r in worse["reasons"])           # the numbers go to the person
    fine = check({**base, "option": {"cancellation": {"stranded": 0, "alternative_stranded": 0, "no_alternative": False}}},
                 [], {"now": T}, rules)
    assert fine["result"] == "pass"
    legal = check({**base, "option": {"cancellation": {"stranded": 40, "alternative_stranded": None, "no_alternative": True}}},
                  [], {"now": T}, rules)
    assert legal["result"] == "pass"                                        # crew or aircraft leave no alternative
    entry = next(e for e in rules["entries"] if e["id"] == "cancellation_must_not_strand_more")
    assert entry["source"] == "assumption" and entry["enforced"] and entry["checked_by"] == "cancellation_guard"


def test_both_agents_count_seats_with_the_shared_helper_not_the_published_estimate():
    import inspect
    from backend.agents.network_recovery import plan
    from backend.agents.passenger_care import care
    for module in (plan, care):
        src = inspect.getsource(module)
        assert "backend.rules.seats" in src
    assert "open_seats_later" not in inspect.getsource(plan.Planner.delay_cancel)
    assert "seats_by_destination" not in inspect.getsource(care.Care.rebooking)


@pytest.fixture(scope="module")
def seed7():
    from backend.seed.build import build
    from simulator.run import simulate
    return simulate(7, "a31_storm", mode="orchestrated", data=build())


def test_seed7_every_cancellation_carries_its_estimate_and_maya_still_flies(seed7):
    o, w = seed7.copilot, seed7.world
    for p in o.network.memory.plans:
        for c in p["cancels"]:
            if c.get("applied"):
                continue
            assert c["stranded_estimate"] is not None and c["alternative"], c
            assert c["alternative_stranded"] is None or c["stranded_estimate"] <= c["alternative_stranded"], c
    assert not [v for v in o.network.verdicts if v["proposal"]["action"] == "cancel_flight" and v["verdict"]["result"] != "pass"]
    assert w.flights["WN1033"]["status"] in ("departed", "arrived") and w.truth["PAX0001"]["status"] == "confirmed"
    assert w.crew_violations == [] and seed7.metrics["three_ways"]["promises_found"] == 0


def test_day_6001_the_closure_that_never_held_a_flight_cancels_nothing():
    from chaos.three_ways import _fresh, kwargs_for
    from backend.seed.build import build
    from simulator.run import simulate
    d = build()
    kw = kwargs_for(6001, d)
    r = simulate(6001, "chaos", mode="orchestrated", data=d, **{**kw, "prebuilt": _fresh(6001)})
    assert not [a for a in r.world.applied if a["action"] == "cancel_flight"]
    assert r.metrics["flights_cancelled"] == 0 and r.metrics["passengers_stranded_overnight"] == 0


@pytest.mark.parametrize("deadline,outcome", [(11 * 60 + 31, "not approved by its deadline: today's thinning acted"),
                                              (11 * 60 + 50, "replaced by the approved co-pilot decision")])
def test_todays_thinning_waits_for_a_pending_recommendation_until_its_deadline(monkeypatch, deadline, outcome):
    """Force a co-pilot proposal for WN1032 to be 'waiting' from 11:20: today's thinning (at 11:30) waits until its deadline.
    With 11:31 nobody can approve in time, so thinning cancels it then (logged); with 11:50, Network Recovery's own delay for
    WN1032 is approved first and replaces the thinning."""
    import backend.orchestrator.loop as L
    from backend.seed.build import build
    from simulator.run import simulate
    orig = L.Orchestrator.tick

    def tick(self, now, published, deliver):
        out = orig(self, now, published, deliver)
        if self.pending_port is not None and now.strftime("%H:%M") >= "11:20":
            self.pending_port({"WN1032": deadline})
        return out
    monkeypatch.setattr(L.Orchestrator, "tick", tick)
    r = simulate(7, "a31_storm", mode="orchestrated", data=build(), until=12 * 60 + 30)
    w = r.world
    waits = [x for x in w.thin_waits if x["flight_id"] == "WN1032"]
    assert waits and waits[0]["from"] == 690 and waits[0]["until"] == deadline and waits[0]["outcome"] == outcome
    f = w.flights["WN1032"]
    if outcome.startswith("not approved"):
        cancelled_at = next(e["t"] for e in w.events if e["type"] == "flight_cancelled" and e["payload"]["flight_id"] == "WN1032")
        assert f["status"] == "cancelled" and cancelled_at == deadline
        assert any(x["flight_id"] == "WN1032" and x["t"] == deadline and "deadline" in x["what"] for x in w.fallbacks)
    else:
        assert "WN1032" in w.cp_delay and f["cancel_reason"] is None or "thinning" not in (f["cancel_reason"] or "")
