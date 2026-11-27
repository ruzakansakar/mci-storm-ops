"""Live mode at the counter: plain errors, hand-off flags, who sees out-of-date information, a Customer's legs and connections,
rebooking on request (ranked), the new trip reaching the Customer, a flight's Customers and rebooking several at once."""
import re

import pytest
from fastapi.testclient import TestClient

from backend.api.app import app
from backend.seed.build import build
from backend.settings import read_limits
from simulator import live as L
from simulator.world import WAITING_DEP

client = TestClient(app)
SNAKE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")


@pytest.fixture(scope="module")
def counter():
    seen = {}

    def user(s, t):
        if t < 12 * 60 + 20:                                       # answer everything until 12:20, then leave the queue open
            for q in s.queue_items():
                s.decide([q["id"]], "approve")
        if t == 12 * 60 and "single" not in seen:
            seen["ood"] = s.views.out_of_date()
            seen["wrong_now"] = len(s.channels.dis_pax)
            for pid in sorted(s.world.truth):                       # a Customer on a flight that is NOT late asks to change
                leg = s.views.leaving_on(pid)
                f = s.world.flights.get(leg or "")
                if f and f["est_dep"] == f["sched_dep"] and s.views.request_options(pid):
                    opts = s.views.request_options(pid)
                    seen["single"] = {"pid": pid, "from": leg, "opts": opts, "t": t,
                                      "out": s.rebook(pid, opts[0]["option_id"])}
                    break
            fl = next((f for f in s.world.departures if f["status"] in WAITING_DEP and not f.get("_next_day")
                       and len([c for c in s.views.flight_customers(f["id"])["customers"] if c["best_option"]]) >= 2), None)
            if fl is not None:
                fc = s.views.flight_customers(fl["id"])
                two = [c for c in fc["customers"] if c["best_option"]][:2]
                seen["many"] = {"flight": fl["id"], "before": fc, "t": t,
                                "out": s.rebook_many(fl["id"], [{"customer_id": c["id"], "option_id": c["best_option"]["option_id"]}
                                                                for c in two] + [{"customer_id": "PAX9999", "option_id": "c1"}])}
                seen["after"] = s.views.flight_customers(fl["id"])

    s = L.LiveSession("a31_storm-s7-live-counter", data=build(), pace=False, driver=user, until=12 * 60 + 40)
    s.run()
    assert s.status == "finished" and s.error is None, getattr(s, "trace_text", s.error)
    s.status = "paused"
    L.SESSIONS[s.run_id] = s
    yield s, seen
    L.SESSIONS.pop(s.run_id, None)


def test_decide_ignores_unknown_fields_and_every_error_is_one_plain_sentence(counter):
    s, _ = counter
    rid = s.run_id
    items = client.get(f"/api/live/{rid}/queue?roles=all").json()["items"]
    q = next(i for i in items if i["can_hand_off"])
    r = client.post(f"/api/live/{rid}/decide", json={"question_ids": [q["id"]], "action": "approve", "to_role": "duty_manager",
                                                     "anything": {"x": 1}})
    assert r.status_code == 200 and r.json()["ok"]
    for bad in (client.post(f"/api/live/{rid}/decide", json={"question_ids": [q["id"]], "action": "approve"}),     # answered
                client.post(f"/api/live/{rid}/decide", json={"question_ids": [], "action": "approve"}),             # invalid
                client.post(f"/api/live/{rid}/decide", json={"question_ids": [1], "action": "fly"}),                # invalid
                client.get("/api/live/no-such-run/state"),
                client.get(f"/api/live/{rid}/customers?q=a&role=station_manager"),
                client.post(f"/api/live/{rid}/control", json={"action": "speed"})):
        assert bad.status_code in (403, 404, 409, 422) and isinstance(bad.json()["detail"], str) and bad.json()["detail"]
    blocked = next((i for i in items if not i["can_wait"]), None)
    if blocked is not None:
        w = client.post(f"/api/live/{rid}/decide", json={"question_ids": [blocked["id"]], "action": "wait", "minutes": 10})
        assert w.status_code == 409 and isinstance(w.json()["detail"], str) and w.json()["blocked_by"]


def test_every_queue_item_says_whether_it_can_be_handed_off(counter):
    s, _ = counter
    items = client.get(f"/api/live/{s.run_id}/queue?roles=all").json()["items"]
    assert items
    for i in items:
        human_only = bool((s.question(i["id"]).proposal or {}).get("human_only"))
        assert i["can_hand_off"] is (not human_only)
        assert (i["hand_off_reason"] is not None) is human_only


def test_out_of_date_uses_the_same_count_as_wrong_now(counter):
    s, seen = counter
    o = seen["ood"]
    assert set(o) == {"total", "ten_min_ago", "flights"}
    assert o["total"] == seen["wrong_now"]
    assert sum(f["customers"] for f in o["flights"]) == o["total"]
    assert [f["customers"] for f in o["flights"]] == sorted((f["customers"] for f in o["flights"]), reverse=True)
    row = next(r for r in s.minutes if r["minute"] == 12 * 60 - 10)
    assert o["ten_min_ago"] == row["wrong_now"]
    for f in o["flights"]:
        assert set(f) == {"flight_id", "label", "customers", "channels", "truth"} and f["channels"]
        assert all(c["channel"] in ("app", "sms", "gate_screen", "call_center") for c in f["channels"])
    live = client.get(f"/api/live/{s.run_id}/out_of_date").json()
    assert live["total"] == client.get(f"/api/live/{s.run_id}/state").json()["truth"]["wrong_now"]


def test_a_customer_trip_has_times_gates_and_connections(counter):
    s, _ = counter
    mct = int(read_limits()["min_connection_min"])
    connecting = [p for p in s.world.truth if len(s.world.truth[p]["itinerary"]) > 1]
    assert connecting
    for pid in connecting[:10]:
        c = client.get(f"/api/live/{s.run_id}/customers/{pid}").json()
        assert c["international"] in (True, False, None) and isinstance(c["needs"], list)
        for i, leg in enumerate(c["trip"]):
            assert {"sched_dep", "sched_arr", "est_dep", "est_arr", "gate", "status", "connection"} <= set(leg)
            if i + 1 < len(c["trip"]):
                conn = leg["connection"]
                assert conn["to_flight"] == c["trip"][i + 1]["flight_id"] and conn["at_risk"] == (conn["minutes"] < mct)
            else:
                assert leg["connection"] is None
    intl = next(p for p, x in s.world.passengers.items() if x.get("segment") == "international" and p in s.world.truth)
    assert client.get(f"/api/live/{s.run_id}/customers/{intl}").json()["international"] is True


def test_rebooking_on_request_is_ranked_even_for_an_on_time_flight(counter):
    s, seen = counter
    one = seen["single"]
    opts = one["opts"]
    assert opts and [o["rank"] for o in opts] == list(range(1, len(opts) + 1))
    assert opts[0]["label"] == "Best option" and opts[0]["recommended"] and (len(opts) < 2 or opts[1]["label"] == "Next best")
    arrivals = [s.world.flights[o["flights"][0]["flight_id"]]["est_arr"] for o in opts]
    assert arrivals == sorted(arrivals)                                   # earliest arrival first
    for o in opts:
        assert o["seats_left"] >= 1 and o["why"].startswith("Arrives in ") and not SNAKE.search(o["why"])
        assert o["flights"][0]["flight_id"] != one["from"]
    api = client.get(f"/api/live/{s.run_id}/customers/{one['pid']}/rebooking?reason=customer_request").json()
    assert api["reason"] == "customer_request" and isinstance(api["options"], list)


def test_a_counter_rebooking_sends_the_customer_their_new_trip(counter):
    s, seen = counter
    one = seen["single"]
    pid, to = one["pid"], one["opts"][0]["flights"][0]["flight_id"]
    assert one["out"]["ok"] and any(leg["flight_id"] == to for leg in one["out"]["trip"])
    words = [e for e in s.world.events if e["type"] == "message_delivered" and e["payload"]["passenger_id"] == pid
             and e["t"] >= one["t"] and to in (e["payload"].get("text") or "")]
    assert words, "Communication told the Customer their new trip"
    sent = client.get(f"/api/live/{s.run_id}/sent?since={one['t']}").json()
    assert any(r["customer_id"] == pid and r["kind"] == "message" and r["text"] == words[0]["payload"]["text"] for r in sent)
    mine = client.get(f"/api/live/{s.run_id}/customers/{pid}").json()
    assert any(m["text"] == words[0]["payload"]["text"] for m in mine["messages"])
    assert all(to in sc["shows"] for sc in mine["screens"]) or any(to in sc["shows"] for sc in mine["screens"])


def test_a_flights_customers_and_rebooking_several_at_once(counter):
    s, seen = counter
    m = seen["many"]
    before = m["before"]
    assert set(before) == {"flight", "customers"} and before["flight"]["id"] == m["flight"] and before["flight"]["truth"]
    for c in before["customers"]:
        assert set(c) == {"id", "name", "status", "needs", "international", "connection", "best_option", "rebooked"}
    res = m["out"]["results"]
    assert [r["ok"] for r in res] == [True, True, False] and res[2]["error"]   # an unknown Customer fails on its own
    moved = {r["customer_id"] for r in res if r["ok"]}
    after = {c["id"]: c for c in seen["after"]["customers"]}
    assert all(after[pid]["rebooked"] for pid in moved)
    for pid in moved:
        assert m["flight"] not in s.world.pax_truth(pid)["itinerary"]
    rows = client.post(f"/api/live/{s.run_id}/flights/{m['flight']}/rebook",
                       json={"assignments": [{"customer_id": "PAX9999", "option_id": "c1"}], "extra": 1})
    assert rows.status_code == 200 and rows.json()["results"][0]["ok"] is False


def test_sent_rows_for_one_customer_carry_their_id(counter):
    s, _ = counter
    rows = client.get(f"/api/live/{s.run_id}/sent?since=690").json()
    assert rows and all("customer_id" in r for r in rows)
    for r in rows:
        if r["count"] == 1 and r["kind"] in ("message", "hotel", "rebook", "refund") and r["channel"] not in ("staff_note", "crew"):
            assert r["customer_id"] in s.world.passengers, r
        if r["count"] > 1:
            assert r["customer_id"] is None
