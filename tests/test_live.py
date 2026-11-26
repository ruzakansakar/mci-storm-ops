"""Live human-in-the-loop mode: the user answers the co-pilot's questions while the simulated day runs.

Sessions here run without real-time pacing (pace=False): a test helper answers at once, so nothing sleeps."""
import re

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app
from backend.seed.build import build
from simulator import live as L

client = TestClient(app)
QUIET, HOLD = 11 * 60 + 25, 12 * 60 + 5      # the user stops answering at 11:25; the clock is held at 12:05


@pytest.fixture(scope="module")
def data():
    return build()


@pytest.fixture(scope="module")
def today(data):
    from simulator.run import simulate
    return simulate(7, "a31_storm", mode="today", data=data).metrics


# ------------------------------------------------------------ a scripted user: every kind of answer, 11:00-15:00
@pytest.fixture(scope="module")
def scripted(data, today):
    seen = {"crew": [], "delay": None, "delay_rejects": [], "wait": None, "hand": None, "approved": [], "blocked_wait": None,
            "errors": [], "d02": [], "d02_hand": None}

    def user(s, t):
        items = s.queue_items()
        if seen["blocked_wait"] is None:                     # before anything else this minute: a question others depend on
            b = next((q for q in items if not q["can_wait"]), None)
            if b is not None:
                seen["blocked_wait"] = {"id": b["id"], "blocked_by": b["blocked_by"], "r": s.decide([b["id"]], "wait", minutes=15),
                                        "deps": {x["id"]: list(s.question(x["id"]).depends_on) for x in b["blocked_by"]}}
        for q in items:
            lq = s.question(q["id"])
            if lq.status not in ("waiting", "snoozed"):
                continue
            if q["action"] == "extend_crew_duty" and (len(seen["d02"]) < 2 or seen["d02_hand"] is None):
                if len(seen["d02"]) < 2 and (not seen["d02"] or seen["d02"][0]["flight"] == q["target"]["flight_id"]):
                    r = s.decide([q["id"]], "reject", reason="no extension today")          # a human-only "no", twice
                    seen["d02"].append({"t": t, "id": q["id"], "flight": q["target"]["flight_id"], "last_option": q["last_option"],
                                        "r": r, "role": q["role"], "why": q["why"]})
                else:
                    seen["d02_hand"] = {"id": q["id"], "r": s.decide([q["id"]], "hand_off")}  # refused: you are the person
                    seen["approved"].append(q["id"])
                    s.decide([q["id"]], "approve")
            elif lq.proposal.get("human_only"):
                s.decide([q["id"]], "approve")
                seen["approved"].append(q["id"])
            elif q["action"] == "call_in_reserve_crew" and len(seen["crew"]) < 1:
                r = s.decide([q["id"]], "reject", reason="not this reserve")
                seen["crew"].append({"t": t, "id": q["id"], "flight": q["target"]["flight_id"], "crew": q["target"]["crew_id"], "r": r})
            elif q["action"] == "delay_flight" and seen["delay"] in (None, q["target"]["flight_id"]) and len(seen["delay_rejects"]) < 2:
                seen["delay"] = q["target"]["flight_id"]
                r = s.decide([q["id"]], "reject", reason="keep it on time")
                seen["delay_rejects"].append({"t": t, "id": q["id"], "last_option": q["last_option"], "r": r,
                                              "consequence": q["consequence_if_rejected"]})
            elif seen["wait"] is None and q["can_wait"] and q["minutes_left"] < 15:
                seen["wait"] = {"id": q["id"], "t": t, "deadline": lq.deadline_at, "r": s.decide([q["id"]], "wait", minutes=10)}
            elif seen["hand"] is None and t >= 700 and (seen["wait"] or {}).get("id") != q["id"] and not lq.proposal.get("human_only"):
                seen["hand"] = {"id": q["id"], "t": t, "urgent": lq.urgent, "r": s.decide([q["id"]], "hand_off")}
            elif (seen["wait"] or {}).get("id") != q["id"]:
                r = s.decide([q["id"]], "approve")
                seen["approved"].append(q["id"])
                seen["errors"] += [x for x in r if x["error"]]

    s = L.LiveSession("a31_storm-s7-live-test", data=data, pace=False, driver=user, until=15 * 60,
                      baseline=L.Baseline(today))
    s.run()
    assert s.status == "finished" and s.error is None, getattr(s, "trace_text", s.error)
    return s, seen


def human_events(s, kind):
    return [e for e in s.world.events if e["type"] == kind and e["actor"].startswith("human:")]


def test_each_answer_writes_its_event_and_the_run_goes_on(scripted):
    s, seen = scripted
    for kind, qid in (("human_approved", seen["approved"][0]), ("human_rejected", seen["crew"][0]["id"]),
                      ("human_waited", seen["wait"]["id"]), ("human_handed_off", seen["hand"]["id"])):
        ev = [e for e in human_events(s, kind) if e["payload"].get("question_id") == qid]
        assert ev, kind
        q = s.question(qid)
        assert ev[0]["actor"] == f"human:{q.role}" and ev[0]["payload"]["recommendation"] == q.rec_id
    assert s.minute == 15 * 60 and seen["errors"] == []                      # the clock went on to the end of the window
    assert not [e for e in s.world.events if e["type"] == "agent_error"]


def test_a_rejected_crew_option_is_never_proposed_again_and_the_next_option_follows(scripted):
    s, seen = scripted
    first = seen["crew"][0]
    assert first["r"][0]["status"] == "rejected"                           # another legal option existed: final at once
    ev = next(e for e in human_events(s, "human_rejected") if e["payload"].get("question_id") == first["id"])
    assert ev["payload"]["final"] and ev["payload"]["different_plan"] and ev["payload"]["reason"] == "not this reserve"
    acts = [v for v in s.copilot.network.verdicts if v["proposal"]["action"] in ("call_in_reserve_crew", "swap_crews", "deadhead_crew")
            and v["proposal"].get("flight_id") == first["flight"] and v["at"][11:16] > f"{first['t'] // 60:02d}:{first['t'] % 60:02d}"]
    assert not [v for v in acts if v["proposal"]["action"] == "call_in_reserve_crew" and v["proposal"].get("crew_id") == first["crew"]]
    assert acts and acts[0]["proposal"]["crew_id"] != first["crew"]          # a different plan: the next legal crew


def test_a_rejection_without_another_plan_comes_back_once_as_last_option_and_the_second_applies_it(scripted):
    s, seen = scripted
    one, two = seen["delay_rejects"]
    assert one["r"][0]["status"] == "rejected_once" and not one["last_option"]
    assert two["last_option"] and two["r"][0]["status"] == "rejected" and two["consequence"]
    assert s.question(two["id"]).previous == one["id"] and s.question(two["id"]).rec_id == s.question(one["id"]).rec_id
    final = next(e for e in human_events(s, "human_rejected") if e["payload"].get("question_id") == two["id"])
    assert final["payload"]["final"] and final["payload"]["consequence_applied"] == two["consequence"]
    f = seen["delay"]
    later = [v for v in s.copilot.network.verdicts if v["proposal"]["action"] == "delay_flight" and v["proposal"].get("flight_id") == f
             and v["at"][11:16] > f"{two['t'] // 60:02d}:{two['t'] % 60:02d}"]
    assert later == []                                                      # never proposed again
    assert f not in s.world.cp_delay                                        # the consequence: no co-pilot delay; today's process


def test_wait_is_refused_while_another_question_depends_on_it(scripted):
    s, seen = scripted
    b = seen["blocked_wait"]
    assert b is not None and b["r"][0]["code"] == 409 and b["r"][0]["blocked_by"]
    assert {x["id"] for x in b["r"][0]["blocked_by"]} == {x["id"] for x in b["blocked_by"]}
    assert b["id"] in b["deps"][b["r"][0]["blocked_by"][0]["id"]]          # at the moment the wait was refused


def test_a_snoozed_question_still_escalates_at_its_deadline(scripted):
    s, seen = scripted
    w = seen["wait"]
    assert w["r"][0]["status"] == "snoozed"
    q = s.question(w["id"])
    item = s.copilot.queue.by_rec[(q.agent, q.rec_id)]
    assert item.escalations and item.escalations[0]["at"][11:16] == L.hhmm(w["deadline"])
    assert q.status == "answered_by_duty_manager" and "director_of_operations" in q.answer["by"]


def test_hand_off_is_answered_by_the_duty_manager_in_its_response_time(scripted):
    from backend.execution.simulated_human import load_settings
    s, seen = scripted
    h = seen["hand"]
    q = s.question(h["id"])
    lo, hi = load_settings()["response_min"]["director_of_operations"]["urgent" if h["urgent"] else "normal"]
    took = (q.answered_at - L.at(h["t"])).total_seconds() / 60
    assert q.status == "answered_by_duty_manager" and q.handed_off and lo <= took <= hi + 1   # +1: answered at the next minute
    assert s.history()[[d["id"] for d in s.history()].index(h["id"])]["answer"] == "handed_off"


def test_live_wrong_screen_numbers_are_the_existing_metric_and_today_is_beside_them(scripted, today):
    s, _ = scripted
    m = s.result.metrics
    last = s.minutes[-1]
    assert last["wrong_minutes_total"] == m["passenger_minutes_of_disagreement"]   # the recorder's own count
    assert last["wrong_now"] == s.channels.passengers_disagreeing() == m["comparison_series"]["pax_disagreeing"][-1]
    series = dict(zip(today["comparison_series"]["t"], today["comparison_series"]["pax_disagreeing"]))
    for row in s.minutes[::37]:
        assert row["today_wrong_now"] == series[row["minute"]]
        assert row["today_wrong_minutes_total"] == sum(v for t, v in series.items() if t <= row["minute"])
    f = last["focus"]
    assert f["passenger_id"] == "PAX0001" and f["name"] == "Maya Torres" and f["true_status"]
    assert [x["channel"] for x in f["screens"]] == ["app", "sms", "gate_screen", "call_center"]   # Maya has app + SMS
    assert all(re.match(r"\d\d:\d\d$", x["since"]) and isinstance(x["wrong"], bool) for x in f["screens"])


# ------------------------------------------------------------ a user who stops answering at 11:25 (the stop starts 11:30)
@pytest.fixture(scope="module")
def paused(data):
    def user(s, t):
        if t < QUIET:
            for q in s.queue_items():
                s.decide([q["id"]], "approve")

    s = L.LiveSession("a31_storm-s7-live-paused", data=data, pace=False, driver=user, until=HOLD, roles=["station_manager"])
    s.run()
    assert s.status == "finished" and s.error is None, getattr(s, "trace_text", s.error)
    s.status = "paused"                                     # as if the user paused the clock at 12:05
    L.SESSIONS[s.run_id] = s
    yield s
    L.SESSIONS.pop(s.run_id, None)


def test_an_unanswered_question_escalates_to_the_duty_manager_at_its_deadline(paused):
    s = paused
    late = [q for q in s.humans.questions if q.asked_at >= L.at(QUIET) and q.deadline_at < L.at(HOLD) and q.status not in ("withdrawn", "auto")]
    assert late
    for q in late:
        item = s.copilot.queue.by_rec[(q.agent, q.rec_id)]
        assert item.escalations and item.escalations[0]["to"] in ("director_of_operations", "station_manager")
        assert q.escalated or q.status == "answered_by_duty_manager"
    assert [e for e in s.world.events if e["type"] == "decision_escalated"]


def test_queue_priority_order_role_filter_and_group_decide(paused):
    s = paused
    items = client.get(f"/api/live/{s.run_id}/queue?roles=all").json()["items"]
    assert len(items) >= 3
    keys = [(not i["safety_legal"], i["minutes_left"], -i["passengers_affected"]) for i in items]
    for a, b in zip(items, items[1:]):                      # safety or legal first, then the earliest deadline, then passengers
        assert a["priority_score"] >= b["priority_score"]
    assert keys == sorted(keys, key=lambda k: k) or all(x["priority_score"] >= y["priority_score"] for x, y in zip(items, items[1:]))
    default = client.get(f"/api/live/{s.run_id}/queue").json()["items"]          # the start roles are the default view
    assert default and {i["role"] for i in default} == {"station_manager"}
    one = client.get(f"/api/live/{s.run_id}/queue?roles=customer_service_supervisor").json()["items"]
    assert {i["role"] for i in one} <= {"customer_service_supervisor"}
    groups = {}
    for i in items:
        groups.setdefault(i["group_key"], []).append(i)
    group = next((g for g in groups.values() if len(g) > 1), None)
    assert group is not None and all(i["group_size"] == len(group) for i in group)
    r = client.post(f"/api/live/{s.run_id}/decide", json={"question_ids": [i["id"] for i in group], "action": "approve"})
    assert r.status_code == 200 and r.json()["ok"] and {x["status"] for x in r.json()["results"]} == {"approved"}
    again = client.post(f"/api/live/{s.run_id}/decide", json={"question_ids": [group[0]["id"]], "action": "approve"})
    assert again.status_code == 409                          # already answered


def test_api_wait_on_a_question_others_depend_on_is_409_with_who_blocks(paused):
    s = paused
    items = client.get(f"/api/live/{s.run_id}/queue?roles=all").json()["items"]
    blocked = next((i for i in items if not i["can_wait"]), None)
    if blocked is None:                                      # make one: a dependent question on the same flight
        pytest.fail("no dependent questions open at 12:05")
    r = client.post(f"/api/live/{s.run_id}/decide", json={"question_ids": [blocked["id"]], "action": "wait", "minutes": 10})
    assert r.status_code == 409 and r.json()["detail"]["blocked_by"] == blocked["blocked_by"]
    bad = client.post(f"/api/live/{s.run_id}/decide", json={"question_ids": [blocked["id"]], "action": "wait", "minutes": 7})
    assert bad.status_code in (409, 422)
    assert client.post(f"/api/live/{s.run_id}/decide", json={"question_ids": [blocked["id"]], "action": "reject"}).status_code == 422


def test_every_question_has_a_connected_trace_from_a_feed_or_agent_version(paused, scripted):
    for s in (paused, scripted[0]):
        for q in s.humans.questions:
            t = s.trace(q.id)
            assert t is not None and t["question_id"] == q.id
            ids = {n["id"]: n for n in t["nodes"]}
            qn = next(n for n in t["nodes"] if n["kind"] == "question")
            back, frontier = set(), {qn["id"]}
            while frontier:                                  # walk back from the question along the edges
                n = frontier.pop()
                back.add(n)
                frontier |= {e["from"] for e in t["edges"] if e["to"] == n} - back
            kinds = {ids[n]["kind"] for n in back}
            assert kinds & {"feed", "situation", "network_recovery", "passenger_care", "station_staff", "communication"}
            assert {"rule_check", "orchestrator"} <= kinds
    via_api = client.get(f"/api/live/{paused.run_id}/questions/1/trace").json()
    assert via_api["question_id"] == 1 and via_api["snapshot"] in ("asked", "answered") and via_api["edges"]


def test_state_flow_history_and_why_have_the_promised_shape(paused):
    s = paused
    st = client.get(f"/api/live/{s.run_id}/state").json()
    assert set(st) >= {"run_id", "status", "minute", "clock", "speed_idle", "speed_pending", "roles", "pending", "urgent_pending",
                       "incidents", "world", "truth", "focus"}
    assert st["clock"] == "12:05" and st["world"]["ground_stop"] is True and st["incidents"]
    assert set(st["truth"]) == {"wrong_now", "wrong_minutes_total", "today_wrong_now", "today_wrong_minutes_total"}
    item = client.get(f"/api/live/{s.run_id}/queue?roles=all").json()["items"][0]
    assert set(item["why"]) == {"plan", "trigger", "inputs", "options_considered", "rule_check", "why_human"}
    assert set(item["why"]["rule_check"]) == {"ok", "rules", "text"}
    fl = client.get(f"/api/live/{s.run_id}/flow?since=660").json()
    assert [a["key"] for a in fl["agents"]] == ["situation", "network_recovery", "passenger_care", "station_staff", "communication"]
    assert fl["edges"] and set(fl["orchestrator"]) == {"open_incidents", "queue"}
    h = client.get(f"/api/live/{s.run_id}/history").json()["decisions"]
    assert h and set(h[0]) >= {"id", "title", "role", "action", "answer", "reason", "asked", "answered", "waited_min"}
    assert client.get(f"/api/live/{s.run_id}/results").status_code == 409     # not the end of the day yet
    assert client.get("/api/live/no-such-run/state").status_code == 404


def test_control_pause_resume_speed(paused):
    s = paused
    r = client.post(f"/api/live/{s.run_id}/control", json={"action": "speed", "speed_idle": 10, "speed_pending": 0.5}).json()
    assert r["speed_idle"] == 10 and r["speed_pending"] == 0.5
    assert client.post(f"/api/live/{s.run_id}/control", json={"action": "resume"}).json()["status"] == "running"
    assert client.post(f"/api/live/{s.run_id}/control", json={"action": "pause"}).json()["status"] == "paused"
    assert client.post(f"/api/live/{s.run_id}/control", json={"action": "speed", "speed_idle": 10 ** 6}).status_code == 422
    assert client.post(f"/api/live/{s.run_id}/control", json={"action": "jump"}).status_code == 422


# ------------------------------------------------------------ the clock
def test_the_clock_pauses_itself_without_a_heartbeat(data):
    clock = {"now": 0.0}
    seen = {}

    def sleep(sec):
        clock["now"] += sec
        if s.status == "paused" and "at" not in seen:
            seen["at"], seen["real"] = s.minute, clock["now"]
            s.status = "stopped"                               # end the test session here

    s = L.LiveSession("a31_storm-s7-live-hb", data=data, sleep=sleep, clock=lambda: clock["now"], live_from="05:30",
                      until=7 * 60)
    s.run()
    assert s.status == "stopped" and s.auto_paused and s.error is None
    timeout = s.s["clock"]["heartbeat_timeout_s"]
    assert timeout <= seen["real"] <= timeout + 1                       # paused once no client asked for the state for 20 s
    assert 5 * 60 + 30 + 1 <= seen["at"] < 5 * 60 + 30 + timeout * s.speed_idle + 5   # minutes went by at speed_idle meanwhile


# ------------------------------------------------------------ the database and the API's writes
def test_a_session_is_saved_and_read_back_after_it_ends(db, data):
    from backend.database import connect

    def user(s, t):
        for q in s.queue_items():
            s.decide([q["id"]], "approve")

    conn = connect()
    L.ensure_tables(conn)
    rid = L.next_run_id(conn, 7, "a31_storm")
    conn.close()
    assert re.match(r"a31_storm-s7-live-\d+$", rid)
    s = L.LiveSession(rid, connect=connect, data=data, pace=False, driver=user, until=11 * 60 + 40)
    s.run()
    assert s.status == "finished" and s.error is None, getattr(s, "trace_text", s.error)
    assert s.baseline.by_t                                               # the today run was made first (the suite resets the db)
    for table in ("live_sessions", "live_questions", "live_decision_traces", "live_minutes"):
        db.execute(f"SELECT count(*) FROM {table} WHERE run_id=%s", (rid,))
        assert db.fetchone()[0] > 0, table
    db.execute("SELECT count(*) FROM events WHERE run_id=%s AND actor LIKE 'human:%%' AND type='human_approved'", (rid,))
    assert db.fetchone()[0] > 0
    db.execute("SELECT mode FROM metric_runs WHERE run_id=%s", (rid,))
    assert db.fetchone()[0] == "live"
    st = client.get(f"/api/live/{rid}/state").json()                     # not in memory: read-only from the database
    assert st["status"] == "finished" and st["truth"]["wrong_minutes_total"] == s.minutes[-1]["wrong_minutes_total"]
    assert client.get(f"/api/live/{rid}/history").json()["decisions"]
    assert client.post(f"/api/live/{rid}/decide", json={"question_ids": [1], "action": "approve"}).status_code == 409
    assert any(r["run_id"] == rid for r in client.get("/api/live/sessions").json())


def test_the_api_writes_only_through_start_control_and_decide(monkeypatch, db):
    paths = app.openapi()["paths"]
    writes = sorted(p for p, ops in paths.items() for m in ops if m not in ("get", "head"))
    assert writes == sorted(["/api/live/start", "/api/live/{run_id}/control", "/api/live/{run_id}/decide",
                             "/api/live/{run_id}/reports", "/api/live/{run_id}/flights/{flight_id}/notes",
                             "/api/live/{run_id}/customers/{pid}/rebook", "/api/live/{run_id}/customers/{pid}/refund"])
    from backend.settings import ROOT
    src = (ROOT / "backend" / "api" / "live.py").read_text(encoding="utf-8")
    assert not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|TRUNCATE)\b", src)
    monkeypatch.setattr(L.LiveSession, "start", lambda self, background=True: self)   # don't play a whole day here
    r = client.post("/api/live/start", json={"seed": 7, "scenario": "a31_storm", "roles": ["station_manager"]})
    assert r.status_code == 200 and re.match(r"a31_storm-s7-live-\d+$", r.json()["run_id"])
    L.SESSIONS.pop(r.json()["run_id"], None)
    assert client.post("/api/live/start", json={"seed": 7, "scenario": "nowhere"}).status_code == 422
    assert client.post("/api/live/start", json={"seed": 7, "roles": ["king"]}).status_code == 422


# ------------------------------------------------------------ the whole day
def test_a_full_seed7_live_day_with_every_question_approved_reaches_the_end(data):
    def approve_all(s, t):
        ids = [q["id"] for q in s.queue_items()]
        if ids:
            s.decide(ids, "approve")

    s = L.LiveSession("a31_storm-s7-live-full", data=data, pace=False, driver=approve_all)
    s.run()
    assert s.status == "finished" and s.error is None, getattr(s, "trace_text", s.error)
    assert s.minute == 23 * 60 + 59
    c = s.copilot
    assert not (c.situation.errors or c.network.errors or c.care.errors or c.station.errors)
    assert s.world.crew_violations == []
    r = s.results()
    assert set(r) == {"you", "today", "orchestrated", "decisions"} and r["you"]["cancelled"] is not None
    assert r["decisions"]["approved"] > 0 and r["you"]["focus_final_trip"]


# ------------------------------------------------------------ follow-up: the flowchart after the answer, human-only, Communication
def _reach(t, start):
    """Every node reachable forward from start."""
    seen, todo = set(), {start}
    while todo:
        n = todo.pop()
        seen.add(n)
        todo |= {e["to"] for e in t["edges"] if e["from"] == n} - seen
    return seen


def client_trace(s, qid):
    L.SESSIONS[s.run_id] = s
    try:
        r = client.get(f"/api/live/{s.run_id}/questions/{qid}/trace")
        assert r.status_code == 200
        return r.json()
    finally:
        L.SESSIONS.pop(s.run_id, None)


def test_an_answered_decision_continues_to_its_changes_messages_and_destinations(scripted):
    s, _ = scripted
    world = {(e["type"], e["t"]) for e in s.world.events}
    texts = {e["payload"].get("text") for e in s.world.events if e["type"] in ("message_delivered", "crew_message_delivered")}
    for action in ("delay_flight", "call_in_reserve_crew"):
        q = next(q for q in s.humans.questions if q.action == action and q.status == "approved")
        t = client_trace(s, q.id)
        assert "preview" not in t and [n["kind"] for n in t["nodes"]].count("human") == 1
        human = next(n for n in t["nodes"] if n["kind"] == "human")
        nodes = {n["id"]: n for n in t["nodes"]}
        after = [nodes[n] for n in _reach(t, human["id"])]
        changes = [n for n in after if n["kind"] == "change"]
        comms = [n for n in after if n["kind"] == "communication"]
        dests = [n for n in after if n["kind"] in ("channel", "staff")]
        assert changes and comms and dests, action
        for c in changes:                                    # every change is a real confirmation event, at its minute
            h, m = c["t"].split(":")
            assert (c["detail"]["event"], int(h) * 60 + int(m)) in world
        for d in dests:
            det = d["detail"]
            assert det["status"] in ("delivered", "failed", "waiting") and det["text"] and det["key"]
            assert det["key"] in ("app", "sms", "gate_screen", "call_center", "counter", "gate_agents", "call_center_agents",
                                  "crew", "ramp", "hotel_desk", "wheelchair_agents")
            assert (d["kind"] == "channel") == (det["key"] in ("app", "sms", "gate_screen", "call_center"))
            if det["status"] == "delivered" and det["key"] in ("app", "sms", "crew"):
                assert det["text"] in texts                  # the exact words that were delivered (never invented)
        assert {"sent to", "confirmed"} <= {e["label"] for e in t["edges"]}
    crew = next(q for q in s.humans.questions if q.action == "call_in_reserve_crew" and q.status == "approved")
    assert "crew" in {n["detail"].get("key") for n in client_trace(s, crew.id)["nodes"]}   # the reserve was told


def test_before_the_answer_the_trace_has_a_preview_from_the_proposal(paused):
    s = paused
    open_ = [q for q in s.humans.questions if q.status in ("waiting", "snoozed")]
    assert open_
    for q in open_:
        t = s.trace(q.id)
        assert set(t["preview"]) == {"changes", "destinations"}
        assert not [n for n in t["nodes"] if n["kind"] in ("change", "channel", "staff")]   # nothing has happened yet
        assert all(set(d) == {"key", "label"} for d in t["preview"]["destinations"])
    first = {i: tr for i, snap, tr in reversed(s.humans.traces) if snap == "asked"}
    delay = next(q for q in s.humans.questions if q.action == "delay_flight")
    assert delay.target["flight_id"] in first[delay.id]["preview"]["changes"][0]["label"]
    assert first[delay.id]["preview"]["destinations"]              # its passengers' screens
    msg = next((q for q in s.humans.questions if (q.proposal or {}).get("messages")), None)
    assert msg is not None
    chans = {m.get("channel") for m in msg.proposal["messages"]}
    assert {d["key"] for d in first[msg.id]["preview"]["destinations"]} <= chans | {"gate_agents", "call_center_agents"}


def test_human_only_decisions_reach_the_named_person_with_why_trace_and_last_option(scripted):
    s, seen = scripted
    hq = [q for q in s.humans.questions if (q.proposal or {}).get("human_only")]
    d02 = [q for q in hq if q.action == "extend_crew_duty"]
    assert d02 and {q.role for q in d02} == {"crew_scheduling"}
    q = d02[0]
    assert q.proposal["decision_ids"] == ["D02"] and q.why["why_human"].startswith("D02")
    rc = q.why["rule_check"]                                 # the rule check ran: only its human-only block, nothing else
    assert rc["ok"] and set(rc["rules"]) <= {"action_extend_crew_duty", "agent_contract"}
    assert {n["kind"] for n in s.trace(q.id)["nodes"]} >= {"rule_check", "orchestrator", "question"}
    one, two = seen["d02"]
    assert one["r"][0]["status"] == "rejected_once" and two["last_option"] and two["r"][0]["status"] == "rejected"
    rec = [e for e in s.world.events if e["type"] == "human_decision_recorded" and e["payload"].get("question_id") == two["id"]]
    assert rec and rec[0]["payload"]["decision_id"] == "D02" and rec[0]["payload"]["answer"] == "rejected"
    assert rec[0]["actor"] == "human:crew_scheduling"
    assert seen["d02_hand"]["r"][0]["code"] == 409            # it can't be handed to the duty manager
    assert not [x for x in hq if x.status == "answered_by_duty_manager"]   # the duty manager never decides them
    approved = [x for x in hq if x.status == "approved"]
    assert approved and all(any(e["type"] == "human_decision_recorded" and e["payload"].get("question_id") == x.id
                                for e in s.world.events) for x in approved)


def test_human_only_questions_exist_only_in_live_mode(data):
    from simulator.run import simulate
    r = simulate(7, "a31_storm", mode="orchestrated", data=data, until=12 * 60)
    assert not hasattr(r.copilot, "human_only_port")
    assert not [i for i in r.copilot.queue.items for m in i.members if (m[2] or {}).get("human_only")]
    assert not [e for e in r.world.events if e["type"] in ("human_only_question", "human_decision_recorded")]


def test_flow_gives_communication_versions_built_on_plan_and_situation_versions(scripted):
    s, _ = scripted
    L.SESSIONS[s.run_id] = s
    try:
        fl = client.get(f"/api/live/{s.run_id}/flow?since=660").json()
    finally:
        L.SESSIONS.pop(s.run_id, None)
    comm = next(a for a in fl["agents"] if a["key"] == "communication")
    assert comm["versions"]
    plans = {r["version"] for r in s.copilot.store.records["recovery_plans"]}
    sits = {r["version"] for r in s.copilot.store.records["situation_assessments"]}
    kinds = set()
    for v in comm["versions"]:
        assert re.match(r"\d\d:\d\d$", v["t"]) and v["used"]
        for u in v["used"]:
            kinds.add(u["kind"])
            assert u["ref"] in (plans if u["kind"] == "network_recovery" else sits)
            assert {"from": f"{u['kind']}:{u['ref']}", "to": f"communication:{v['v']}", "t": v["t"]} in fl["edges"]
    assert kinds == {"network_recovery", "situation"}
    assert [v["v"] for v in comm["versions"]] == sorted(v["v"] for v in comm["versions"])
