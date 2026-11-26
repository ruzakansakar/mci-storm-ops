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
from simulator.world import WAITING_DEP

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
        if t == 12 * 60 + 30:                                 # the two fixes: crew legal time and gates held by waiting flights
            from simulator.world import CREW_ROLES
            w = s.world
            seen["at_1230"] = {
                "flights": {d["id"]: s.views.flight(d["id"]) for d in w.departures if d["status"] in WAITING_DEP and d.get("crew_id")
                            and not d.get("_next_day")},
                "duty": {d["id"]: max(w.t, d["sched_dep"]) + min(w._crew_for(d, r)[f] for r, f, _ in CREW_ROLES
                                                                  if w._crew_for(d, r) and w._crew_for(d, r).get(f) is not None)
                         for d in w.departures if d["status"] in WAITING_DEP and d.get("crew_id") and not d.get("_next_day")},
                "est": {d["id"]: d["est_dep"] for d in w.departures},
                "at_gate": [(d["id"], s.views.gate_of(d["id"]), d["status"]) for d in s.views.at_gate()],
                "station": s.views.station()}
        for i in items:
            if i["agent"] == "passenger_care" and (i.get("care") or {}).get("customers") and "care_item" not in seen:
                seen["care_item"] = i
        if t == 13 * 60:
            seen["customer_1300"] = {pid: s.views.customer(pid) for pid in sorted(s.world.truth)}
            seen["sent"] = s.views.sent(11 * 60 + 25)
        if t == 13 * 60:
            seen["station"], seen["clocks"] = s.views.station(), s.views.clocks()
            seen["handover"], seen["flight"] = s.views.handover(11 * 60), s.views.flight("WN1033")

    s = L.LiveSession("a31_storm-s7-live-ui", data=data, pace=False, driver=user, until=17 * 60, roles=["station_manager"])
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
    assert set(one) == {"id", "name", "trip", "true_status", "screens", "needs", "rebooking", "hotel", "refund_due", "messages",
                        "refunds"}
    fl = client.get(f"/api/live/{rid}/flights/WN1033").json()
    assert set(fl) == {"id", "from", "to", "sched_dep", "est_dep", "sched_arr", "est_arr", "status", "gate", "crew", "customers",
                       "connections", "script", "notes", "on_screens", "truth", "one_story"}
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


def test_flight_crew_legal_until_is_when_the_crew_runs_out_of_duty_after_the_departure(ui):
    s, seen = ui
    snap = seen["at_1230"]
    f = snap["flights"]["WN1027"]
    assert f["status"] == "held" and f["crew"]["id"] == "C27"
    h, m = map(int, f["crew"]["legal_until"].split(":"))
    assert h * 60 + m > snap["est"]["WN1027"]                    # after the departure (13:00), not "08:07"
    for fid, view in snap["flights"].items():                     # every waiting flight: the duty left, from the later of now
        assert re.match(r"^\d\d:\d\d$", view["crew"]["legal_until"]), fid      # and the scheduled departure, local HH:MM
        assert view["crew"]["legal_until"] == f"{(snap['duty'][fid] // 60) % 24:02d}:{snap['duty'][fid] % 60:02d}", fid


def test_a_gate_is_in_use_while_a_flight_waiting_at_it_is_there(ui):
    s, seen = ui
    snap = seen["at_1230"]
    gates = {g["gate"]: g for g in snap["station"]["gates"]}
    assert ("WN1027", "G01", "held") in snap["at_gate"]
    assert gates["G01"]["status"] == "in_use" and gates["G01"]["flight_id"] == "WN1027"
    for fid, gate, status in snap["at_gate"]:                     # every departure still at its gate, whatever its schedule says
        if gate:
            assert gates[gate]["status"] == "in_use" and gates[gate]["flight_id"] == fid, (fid, gate, status)


# ------------------------------------------------------------ Passenger Care detail, the Customer, one story, everything sent
def test_a_passenger_care_question_lists_each_customer_and_nothing_is_rebooked_before_approval(ui):
    s, seen = ui
    item = seen["care_item"]
    c = item["care"]
    assert set(c) == {"customers", "message_text", "auto_done"}
    assert [x["id"] for x in c["customers"]] == item["target"]["passenger_ids"]
    rec = s.copilot.care.context.saved[("recommendations", s.question(item["id"]).rec_id)]
    for x in c["customers"]:
        assert x["name"] == s.world.passengers[x["id"]]["full_name"] and x["action"] in ("rebook", "hotel", "refund", "ride")
        assert set(x["detail"]) == {"flights", "hotel", "amount", "currency"} and x["why"]
        if x["action"] == "rebook":
            assert x["detail"]["flights"][0]["flight_id"] == rec["options"][0]["flight_id"]
    assert any(a["action"] == "refund" and a["count"] > 0 for a in c["auto_done"])
    approved = [(q.rec_id, q.answered_at) for q in s.humans.questions if q.agent == "passenger_care"
                and q.action == "rebook_passenger" and q.status == "approved"]
    for e in s.world.events:                                  # every rebooking carried out came after a person said yes
        p = e.get("payload") or {}
        if e["type"] == "decision_applied" and p.get("action") == "rebook_passenger" and p.get("agent") == "passenger_care":
            assert any(rid == p["recommendation"] and L.at(e["t"]) >= at for rid, at in approved), e


def test_approve_with_change_moves_one_customer_to_another_of_their_options(data):
    """Seed 7 never has a second flight with seats for a stranded Customer (approved rebookings fill them), so the per-Customer
    choice is offered only when one exists. Here that Customer's options are given another real flight to their city to show
    the path: that Customer goes through the same rebooking path (K01, the rule check, the reservation system), the rest stay."""
    seen = {}

    def user(s, t):
        if "done" in seen:
            return
        for q in s.queue_items():
            if q["action"] != "rebook_passenger":
                continue
            lq = s.question(q["id"])
            pid = lq.passenger_ids[0]
            proposed = s.humans.proposed_flight(lq)
            ids = (q["allowed_changes"] or {}).get("option_id", {}).get(pid, [])
            assert all(o["flights"][0]["flight_id"] != proposed for o in s.views.rebooking(pid) if o["option_id"] in ids)
            dest = s.world.truth[pid]["destination"]
            other = next(f for f in s.world.departures if f["destination"] == dest and f["status"] in WAITING_DEP
                         and not f.get("_next_day") and f["est_dep"] > t and f["id"] != proposed)
            real = s.views.rebooking
            s.views.rebooking = lambda p, real=real: ([{"option_id": "r9", "flights": [{"flight_id": other["id"]}], "recommended": False}]
                                                      if p == pid else real(p))
            seen.update(pid=pid, to=other["id"], rest=[x for x in lq.passenger_ids if x != pid], rid=lq.rec_id,
                        r=s.decide([q["id"]], "approve_with_change", change={"customer_id": pid, "option_id": "r9"}))
            seen["again"] = s.decide([q["id"]], "approve_with_change", change={"customer_id": pid, "option_id": "r9"})
            seen["done"] = t
            return

    s = L.LiveSession("a31_storm-s7-live-care", data=data, pace=False, driver=user, until=11 * 60 + 40)
    s.run()
    assert s.status == "finished", getattr(s, "trace_text", s.error)
    assert seen["r"][0]["status"] == "approved" and seen["to"] in seen["r"][0]["done"]
    assert s.copilot.care.options[seen["rid"]]["passenger_ids"] == seen["rest"]
    moved = [e for e in s.world.events if e["type"] == "customer_option_applied" and e["payload"]["passenger_ids"] == [seen["pid"]]]
    assert moved and moved[0]["payload"]["flight_id"] == seen["to"]          # tried through the rebooking path at the next minute
    assert moved[0]["payload"]["applied"] or moved[0]["payload"]["reason"]    # carried out, or refused with the reason
    assert seen["again"][0]["code"] == 409                                    # already answered


def test_the_customer_view_has_their_messages_refunds_and_hotel(ui):
    s, seen = ui
    views = seen["customer_1300"]
    delivered = {(e["payload"]["passenger_id"], e["payload"]["text"]) for e in s.world.events if e["type"] == "message_delivered"}
    with_msgs = [v for v in views.values() if v["messages"]]
    assert with_msgs
    for v in with_msgs:
        for m in v["messages"]:
            assert set(m) == {"t", "channel", "text"} and (v["id"], m["text"]) in delivered
    refunds = [r for v in views.values() for r in v["refunds"]]
    assert refunds and all(set(r) == {"amount", "status", "t"} and r["status"] in ("issued", "due") for r in refunds)
    assert any(r["status"] == "issued" and r["t"] for r in refunds)
    hotels = [h for h in (s.views.hotel(pid) for pid in s.world.truth) if h]   # rooms come later (at the end of the run)
    assert hotels and all({"name", "price", "shuttle", "status"} <= set(h) for h in hotels)
    assert all(isinstance(h["price"], (int, float)) and h["name"] for h in hotels)


def test_the_flight_has_its_truth_and_one_story_across_every_place(ui):
    s, seen = ui
    f = seen["flight"]
    assert f["truth"].startswith("WN1033 to Baltimore is ") and not SNAKE.search(f["truth"])
    places = [x["place"] for x in f["one_story"]]
    assert places == ["app", "sms", "gate_screen", "call_center", "counter", "gate_agents", "crew"]
    for x in f["one_story"]:
        assert set(x) == {"place", "says", "matches", "since"}
        assert (x["says"] is None) == (x["matches"] is None) == (x["since"] is None)
        if x["says"] is not None:
            assert isinstance(x["matches"], bool) and re.match(r"\d\d:\d\d$", x["since"])
    staff = next(x for x in f["one_story"] if x["place"] == "gate_agents")
    notes = {h["text"] for h in s.copilot.recovery.history if h.get("flight_id") == "WN1033" and h.get("audience") == "staff"}
    assert staff["says"] in notes                                              # the latest staff note about this flight
    assert next(x for x in f["one_story"] if x["place"] == "counter")["says"] is None      # nothing is sent to the counter


def test_everything_sent_newest_last_with_mass_sends_grouped(ui):
    s, seen = ui
    rows = seen["sent"]
    assert rows and [r["minute"] for r in rows] == sorted(r["minute"] for r in rows)
    for r in rows:
        assert set(r) == {"t", "minute", "to", "channel", "kind", "text", "count"} and r["minute"] >= 11 * 60 + 25
        assert r["kind"] in ("message", "refund", "hotel", "rebook") and r["count"] >= 1
    mass = [r for r in rows if r["kind"] == "message" and r["count"] > 1]
    assert mass and all(r["to"].endswith("Customers") or " Customers on " in r["to"] for r in mass)
    texts = {e["payload"].get("text") for e in s.world.events if e["type"] in ("message_delivered", "staff_note_published",
                                                                               "crew_message_delivered")}
    assert all(r["text"] in texts for r in rows if r["kind"] == "message")    # the exact words
    assert any(r["channel"] == "staff_note" for r in rows) and any(r["channel"] == "crew" for r in rows)
    keys = [(r["minute"], r["kind"], r["text"]) for r in rows if r["kind"] != "message"]
    assert len(keys) == len(set(keys))                                         # confirmations in one minute: one row
    assert client.get(f"/api/live/{s.run_id}/sent?since=700").status_code == 200
