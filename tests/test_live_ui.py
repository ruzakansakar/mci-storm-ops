"""Live mode for airport employees (branch live-ui): human-only decisions take effect, plain language, the airline's autonomy
settings, event bundles, more ways to decide, reports from the floor, flight and Customer views, the station, clocks and
handover. Sessions run without real-time pacing; a scripted user answers at once (nothing sleeps)."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app
from backend.seed.build import build
from simulator import live as L

client = TestClient(app)
SNAKE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
VERSION = re.compile(r"\bv\d+\b")


@pytest.fixture(scope="module")
def data():
    return build()


@pytest.fixture(scope="module")
def ui(data):
    seen = {"queue_actions": set(), "items": [], "errors": []}

    def user(s, t):
        items = s.queue_items()
        seen["queue_actions"] |= {i["action"] for i in items}
        if 690 <= t <= 700:
            seen["items"] += items
        if t == 11 * 60 + 5:
            seen["report"] = s.report("gate_unavailable", gate="G03", text="jet bridge out of service", role="counter_gate_agent")
            s.report("long_queue", gate="G05", text="long line at the podium", role="counter_gate_agent")
            s.report("equipment_issue", text="a printer is down", role="counter_gate_agent")
            seen["note"] = s.add_note("WN1033", "Captain asked for an update at 12:00", "counter_gate_agent")
        if t >= 11 * 60 + 31 and "rebook" not in seen:
            for pid in sorted(s.world.truth):
                if True:
                    opts = s.views.customer(pid)["rebooking"]
                    if opts:
                        seen["rebook"] = {"pid": pid, "t": t, "to": opts[0]["flights"][0]["flight_id"],
                                          "out": s.rebook(pid, opts[0]["option_id"])}
                        break
        for q in items:
            lq = s.question(q["id"])
            if lq.status not in ("waiting", "snoozed"):
                continue
            a = q["action"]
            try:
                if a == "extend_crew_duty" and "d02" not in seen:
                    seen["d02"] = {"q": q, "t": t, "r": s.decide([q["id"]], "approve")}
                elif a in ("call_in_reserve_crew",) and "option" not in seen and len(q["options"]) > 1:
                    seen["option"] = {"q": q, "t": t, "r": s.decide([q["id"]], "approve_option", option_id=q["options"][1]["id"])}
                elif a == "delay_flight" and "bad_delay" not in seen:
                    seen["bad_delay"] = s.decide([q["id"]], "approve_with_change", change={"delay_min": 60})
                    s.decide([q["id"]], "approve")
                elif a == "assign_gates" and "gate" not in seen and (q["allowed_changes"] or {}).get("gate"):
                    g = q["allowed_changes"]["gate"][-1]
                    seen["gate"] = {"q": q, "gate": g, "t": t, "r": s.decide([q["id"]], "approve_with_change", change={"gate": g})}
                elif a == "send_staff_briefing" and "text" not in seen and (q["allowed_changes"] or {}).get("text"):
                    old = lq.proposal["messages"][0]["text"]
                    seen["bad_text"] = s.decide([q["id"]], "approve_with_change", change={"text": old + " Boarding at 16:55."})
                    seen["text"] = {"q": q, "new": old + " Thank you for your patience.",
                                    "r": s.decide([q["id"]], "approve_with_change", change={"text": old + " Thank you for your patience."})}
                else:
                    r = s.decide([q["id"]], "approve")
                    seen["errors"] += [x for x in r if x["error"]]
            except Exception as err:                              # noqa: BLE001
                seen["errors"].append(repr(err))
        if t == 13 * 60:
            seen["station"], seen["clocks"] = s.views.station(), s.views.clocks()
            seen["handover"], seen["flight"] = s.views.handover(11 * 60), s.views.flight("WN1033")

    s = L.LiveSession("a31_storm-s7-live-ui", data=data, pace=False, driver=user, until=15 * 60, roles=["station_manager"])
    s.run()
    assert s.status == "finished" and s.error is None, getattr(s, "trace_text", s.error)
    s.status = "paused"
    L.SESSIONS[s.run_id] = s
    yield s, seen
    L.SESSIONS.pop(s.run_id, None)


def test_an_approved_human_only_duty_extension_lets_the_flight_depart(ui):
    s, seen = ui
    d = seen["d02"]
    fid = d["q"]["target"]["flight_id"]
    assert d["r"][0]["status"] == "approved"
    ext = [e for e in s.world.events if e["type"] == "pilot_duty_extended" and e["payload"]["flight_id"] == fid
           and str(e["payload"].get("decided_by", "")).startswith("you")]
    assert ext and ext[0]["t"] >= d["t"]
    assert s.world.flights[fid]["status"] in ("departed", "arrived")          # the flight could leave
    assert not [e for e in s.world.events if e["type"] == "flight_cancelled" and e["payload"].get("flight_id") == fid]
    labels = [n["label"] for n in s.trace(d["q"]["id"])["nodes"] if n["kind"] == "change"]
    assert any("duty extended" in x for x in labels) and any("departed" in x for x in labels)


def test_a_human_only_rejection_keeps_todays_behaviour(data):
    from simulator.run import simulate
    s = L.LiveSession("a31_storm-s7-live-no", data=data, pace=False, until=14 * 60 + 30,
                      driver=lambda s, t: [s.decide([q["id"]], "reject" if q["action"] == "extend_crew_duty" else "approve",
                                                    reason="no") for q in s.queue_items()])
    s.run()
    assert s.status == "finished"
    assert not [e for e in s.world.events if e["type"] == "pilot_duty_extended" and str(e["payload"].get("decided_by", "")).startswith("you")]


def test_automatic_refunds_and_rides_never_reach_the_queue_and_are_logged(ui):
    s, seen = ui
    auto = [q for q in s.humans.questions if q.status == "auto"]
    assert {q.action for q in auto} >= {"offer_refund"}
    assert "offer_refund" not in seen["queue_actions"]
    ev = [e for e in s.world.events if e["type"] == "copilot_auto_done"]
    assert ev and all(e["actor"] == "copilot:auto" for e in ev) and len(ev) == len(auto)
    h = {d["id"]: d for d in s.history()}
    assert all(h[q.id]["answer"] == "auto" for q in auto)
    assert s.humans.decisions()["auto"] == len(auto)                          # the day summary
    t = s.trace(auto[0].id)
    assert any(n["kind"] == "human" and n["label"].startswith("Done automatically:") for n in t["nodes"])
    for q in auto:                                                            # within the limits only
        if q.action == "arrange_ground_transport":
            assert s.tracer.rec(q.agent, q.rec_id)["options"][0]["cost_usd"] <= 70


def test_a_setting_that_automates_a_safety_or_legal_item_is_refused(tmp_path):
    from backend.execution.live_autonomy import AutonomyError, load
    from backend.settings import ROOT
    good = (ROOT / "config" / "autonomy.yaml").read_text(encoding="utf-8")
    load()                                                                    # the shipped settings load
    for action in ("cancel_flight", "call_in_reserve_crew", "divert_flight", "start_deplaning", "ramp_closure"):
        bad = tmp_path / f"{action}.yaml"
        bad.write_text(good.replace("action: offer_refund", f"action: {action}"), encoding="utf-8")
        with pytest.raises(AutonomyError):
            load(bad)
    missing = tmp_path / "missing.yaml"
    missing.write_text(good.replace("  tarmac: [", "  tarmac_x: ["), encoding="utf-8")
    with pytest.raises(AutonomyError):
        load(missing)


def test_every_question_has_a_bundle_and_a_section_and_hard_decisions_are_never_grouped(ui):
    s, seen = ui
    items = seen["items"]
    assert items
    hard = set(s.s["bundles"]["never_grouped"])
    for i in items:
        b = i["bundle"]
        assert set(b) == {"id", "title", "started"} and i["section"] in ("urgent", "routine")
        if i["urgent"] or i["safety_legal"]:
            assert i["section"] == "urgent"
        if i["action"] in hard:
            assert i["group_key"].count(":") == 4 and i["group_key"].endswith(f":{i['id']}")
    assert any(i["bundle"]["title"] == "Ground stop at MCI, 11:30" for i in items)
    groups = {}
    for i in items:
        groups.setdefault((i["group_key"], tuple(sorted(i.items(), key=str))[:0]), []).append(i)
    for key in {i["group_key"] for i in items}:
        same = [i for i in items if i["group_key"] == key]
        assert len({(i["agent"], i["action"], i["role"], i["bundle"]["id"]) for i in same}) == 1
    normal = [q for q in s.humans.questions if q.asked_at < L.at(11 * 60 + 30) and q.bundle]
    assert any(q.bundle["title"] == "Normal operations" for q in normal)


def test_approve_option_carries_out_the_other_option(ui):
    s, seen = ui
    o = seen["option"]
    alt = o["q"]["options"][1]
    fid = o["q"]["target"]["flight_id"]
    assert o["r"][0]["status"] == "approved" and alt["crew_id"] in o["r"][0]["done"]
    done = [e for e in s.world.events if e["type"] in ("reserve_crew_assigned", "crew_reassigned")
            and e["payload"]["flight_id"] == fid and e["t"] >= o["t"]]
    assert done and done[0]["payload"]["crew_id"] == alt["crew_id"]          # the other crew, not the recommended one
    assert o["q"]["options"][0]["recommended"] and not alt["recommended"]


def test_approve_with_change_applies_the_change_and_a_rule_breaking_change_is_refused(ui):
    s, seen = ui
    g = seen["gate"]
    assert g["r"][0]["status"] == "approved"
    assigned = [e for e in s.world.events if e["type"] == "gate_assigned" and e["payload"]["recommendation"] == g["q"]["id"] - g["q"]["id"]
                + s.question(g["q"]["id"]).rec_id]
    assert assigned and assigned[0]["payload"]["gate"] == g["gate"]
    bad = seen["bad_delay"][0]
    assert bad["code"] == 409 and bad["error"].startswith("This change breaks a rule:")
    assert not SNAKE.search(bad["error"]) and "duty" in bad["error"]
    assert seen["bad_text"][0]["code"] == 409 and "16:55" in seen["bad_text"][0]["error"]
    tx = seen["text"]
    assert tx["r"][0]["status"] == "approved"
    rid = s.question(tx["q"]["id"]).rec_id
    sent = [e for e in s.world.events if e["type"] == "staff_note_published" and e["payload"].get("text") == tx["new"]]
    assert sent, rid


def test_a_gate_reported_unavailable_is_no_longer_used_and_reports_are_evidence(ui):
    s, seen = ui
    t0 = 11 * 60 + 5
    later = [e for e in s.world.events if e["type"] == "gate_assigned" and e["t"] > t0 + 1 and e["payload"]["gate"] == "G03"]
    assert later == []
    st = seen["station"]
    assert next(g for g in st["gates"] if g["gate"] == "G03")["status"] == "closed"
    plans = [r for r in s.copilot.store.records["station_plans"] if r["planned_at"] > L.at(t0)]
    assert any(any(x.get("where") == "G05" for x in r.get("staff_shortages") or []) for r in plans)   # a long queue = a gap
    sit = [r for r in s.copilot.store.records["situation_assessments"] if r["assessed_at"] > L.at(t0)]
    kinds = {e.get("points_to") for r in sit for e in r.get("evidence") or [] if e.get("type") == "floor_report"}
    assert {"gate_unavailable", "long_queue", "equipment_issue"} <= kinds
    assert seen["note"][-1]["text"].startswith("Captain") and seen["flight"]["notes"]


def test_a_counter_rebooking_reaches_every_screen(ui):
    s, seen = ui
    r = seen["rebook"]
    pid, to = r["pid"], r["to"]
    assert r["out"]["ok"] and any(leg["flight_id"] == to for leg in r["out"]["trip"])
    assert to in s.world.truth[pid]["itinerary"]
    for ch in s.channels.screens[pid]:                                        # spread through shared state to all screens
        assert to in s.channels.shown[(pid, ch)]["itinerary"], ch
    assert [e for e in s.world.events if e["type"] == "customer_rebooked" and e["payload"]["passenger_id"] == pid]


def test_the_clocks(ui):
    s, seen = ui
    c = seen["clocks"]
    assert set(c) == {"tarmac", "crew", "refunds", "special_assistance"}
    assert all(x["limit"] == 180 and x["minutes"] >= 0 for x in c["tarmac"])
    assert all(x["minutes_left"] >= 0 and re.match(r"\d\d:\d\d$", x["legal_until"]) for x in c["crew"])
    assert c["refunds"] and {x["status"] for x in c["refunds"]} <= {"auto_issued", "due"}
    assert any(x["status"] == "auto_issued" for x in c["refunds"])
    assert all(set(x) == {"customer_id", "need", "flight_id", "waiting_min"} for x in c["special_assistance"])


def test_the_handover(ui):
    s, seen = ui
    h = seen["handover"]
    assert set(h) == {"since", "decisions", "open", "watch", "auto_done", "text"} and h["since"] == "11:00"
    assert h["decisions"] > 0 and h["auto_done"] > 0 and h["text"].startswith("Since 11:00")
    assert all(not SNAKE.search(x) for x in h["watch"] + [h["text"]])
    api = client.get(f"/api/live/{s.run_id}/handover?since=11:00").json()
    assert set(api) == set(h)


def test_plain_language_everywhere(ui):
    s, _ = ui
    from backend.rules import load_rules
    rule_ids = {r["id"] for r in load_rules()["entries"]}
    bad_titles = ("staff note for staff", "first notice messages", "Controllable or not", "Staff for WN", "Confirm the type of event")

    def ok(text):
        text = str(text or "")
        assert not SNAKE.search(text), text
        assert not VERSION.search(text), text
        assert not any(r in text for r in rule_ids), text

    for q in s.humans.questions:
        p = q.plain
        assert set(p) == {"headline", "story", "recommendation", "options", "effect", "if_declined"}
        ok(p["headline"])
        assert not any(b in p["headline"] for b in bad_titles), p["headline"]
        assert 1 <= len(p["story"]) <= 4 and all(re.match(r"\d\d:\d\d$", x["t"]) for x in p["story"])
        for x in p["story"]:
            ok(x["text"])
        assert p["options"][0]["id"] == "opt1" and set(p["effect"]) == {"customers", "connections", "flights", "text"}
        assert p["if_declined"]
        for n in s.trace(q.id)["nodes"]:
            ok(n["label"])
            ok(n["detail"].get("says"))
    sit = next(n for q in s.humans.questions for n in s.trace(q.id)["nodes"] if n["kind"] == "situation")
    assert sit["label"].startswith("Weather update ") and "ground stop" in sit["detail"]["says"]
    assert any("Customers" in (q.plain["headline"] or "") for q in s.humans.questions)


def test_frontline_role_and_no_contact_details_anywhere(ui):
    s, _ = ui
    rid = s.run_id
    assert client.get(f"/api/live/{rid}/queue?roles=counter_gate_agent").json()["items"] == []
    assert client.get(f"/api/live/{rid}/customers?q=maya&role=station_manager").status_code == 403
    found = client.get(f"/api/live/{rid}/customers?q=maya&role=counter_gate_agent").json()
    assert found and set(found[0]) == {"id", "name", "booking_ref", "flight_id", "status"}
    one = client.get(f"/api/live/{rid}/customers/{found[0]['id']}?role=counter_gate_agent").json()
    assert set(one) == {"id", "name", "trip", "true_status", "screens", "needs", "rebooking", "hotel", "refund_due"}
    fl = client.get(f"/api/live/{rid}/flights/WN1033").json()
    assert set(fl) == {"id", "from", "to", "sched_dep", "est_dep", "sched_arr", "est_arr", "status", "gate", "crew", "customers",
                       "connections", "script", "notes", "on_screens"}
    assert set(fl["on_screens"]) == {"app", "sms", "gate_screen", "call_center"}
    st = client.get(f"/api/live/{rid}/station").json()
    assert set(st) == {"ramp", "gates", "staffing_gaps"}
    responses = [client.get(f"/api/live/{rid}/{p}").json() for p in
                 ("state", "queue?roles=all", "flow?since=600", "history", "clocks", "station", "handover",
                  "flights/WN1033", "flights/WN1033/notes", "questions/1/trace")]
    responses += [found, one]
    blob = json.dumps(responses)
    for word in ("contact_channels", "channel_types", "email", "phone", "@"):
        assert word not in blob, word
    data = s.world.passengers
    assert not any(str(p.get(k)) in blob for p in data.values() for k in ("contact_channels",) if p.get(k) and len(str(p.get(k))) > 8)


def test_day_page_works_for_a_live_run_in_progress(db, data):
    from backend.api import day as day_data
    from backend.database import connect
    seen = {}

    def user(s, t):
        if t == 11 * 60 + 36 and "day" not in seen:
            seen["runs"] = [r for r in day_data.runs() if r["run_id"] == s.run_id]
            seen["day"] = day_data.day(s.run_id)

    conn = connect()
    L.ensure_tables(conn)
    rid = L.next_run_id(conn, 7, "a31_storm")
    conn.close()
    s = L.LiveSession(rid, connect=connect, data=data, pace=False, driver=user, until=11 * 60 + 40)
    s.run()
    assert s.status == "finished", getattr(s, "trace_text", s.error)
    assert seen["runs"] and seen["runs"][0]["mode"] == "live"
    assert seen["day"]["world"]                                               # events so far
