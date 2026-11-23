"""Phase 4b-3 part B: passenger messages and staff notes. Only confirmed facts; never a promise nobody approved."""
import random
import re
from collections import defaultdict
from copy import deepcopy
from datetime import timedelta

import pytest

from backend.agents.communication.detect import Memory, load_settings, scan
from backend.agents.communication.messages import MessageMemory, check_world, draft
from backend.agents.communication.situation_view import SituationView, read_situation
from backend.agents.communication.state_at import clock
from backend.contracts.buffered import BufferedContext
from backend.contracts.context import ContractViolation
from backend.rules.message_check import message_problems, templates
from simulator.message_metrics import forbidden_found
from tests.test_shared_state import version

S = load_settings()
CITIES = {"MCI": "Kansas City", "BWI": "Baltimore", "DEN": "Denver"}


def delayed_day():
    """P1 on WN1001 MCI-BWI, delayed to 13:10 at 11:50; Situation: weather, confident; app + SMS on file."""
    sched = [{"id": "WN1001", "direction": "departure", "origin": "MCI", "destination": "BWI", "sched_dep": clock("12:30"),
              "sched_arr": clock("15:00")}]
    pax = [{"id": "P1", "segment": "wheelchair", "full_name": "Maya Torres", "channel_types": ["app", "sms"]},
           {"id": "P2", "segment": "standard", "full_name": "Liam Smith", "channel_types": ["sms"]}]
    ctx = BufferedContext(clock("12:00"), feeds=None,
                          rows={"flights": sched, "passengers": pax, "stations": [{"code": k, "name": v} for k, v in CITIES.items()]})
    trip = {"passenger_id": "P1", "itinerary": ["WN1001"], "status": "confirmed", "destination": "BWI", "bag_routing": []}
    ctx.events = [{"id": 1, "occurred_at": clock("05:00"), "type": "trip_record_updated", "payload": trip},
                  {"id": 2, "occurred_at": clock("11:50"), "type": "flight_delayed",
                   "payload": {"flight_id": "WN1001", "delay_min": 40, "new_departure": "13:10"}},
                  {"id": 3, "occurred_at": clock("11:50"), "type": "trip_record_updated", "payload": {**trip, "status": "delayed"}}]
    ctx.store.records["situation_assessments"].append(version(1, clock("11:30")))
    ctx.store.heartbeats["situation"] = [clock("11:55")]
    view = SituationView()
    result = scan(ctx, Memory(), write=False)
    read_situation(ctx, view, S)
    mem = MessageMemory()
    mem.cities = CITIES
    return ctx, view, result["state"], mem


def a_delay_message(channel="sms", first_name=None):
    ctx, view, st, mem = delayed_day()
    m = draft("delay_update", "P1", channel, st.truth["P1"], None, st, view.picture, S["messages"], CITIES, first_name, "12:30")
    names = {"P1": first_name} if first_name else {}
    world = check_world(st, view, mem, names, ["P1"] if first_name else [])
    return m, world


# ------------------------------------------------------------------ the message fact check

def test_a_correct_delay_message_passes_and_says_the_four_things():
    m, world = a_delay_message()
    assert m["text"].startswith("Your flight WN1001 to Baltimore is delayed. New departure: around 13:10; this may change.")
    assert "Next update by 12:30" in m["text"] and not message_problems(m, world)
    assert all(m["parts"][k] for k in ("know", "dont_know", "do_now", "next"))


@pytest.mark.parametrize("wrong", [("13:10", "13:40"), ("WN1001", "WN1002"), ("Baltimore", "Denver")])
def test_the_fact_check_blocks_a_wrong_time_flight_or_city(wrong):
    m, world = a_delay_message()
    bad = {**m, "text": m["text"].replace(*wrong, 1)}
    assert message_problems(bad, world)
    lie = deepcopy(m)
    for c in lie["claims"]:
        if c.get("time") == "13:10":
            c["time"] = "13:40"
    lie["text"] = lie["text"].replace("13:10", "13:40")
    assert any("13:40" in p for p in message_problems(lie, world))            # a claim the truth does not support


def test_no_promise_or_compensation_without_an_approved_decision():
    m, world = a_delay_message("app")
    for extra in ("We will give you a hotel voucher.", "You are entitled to a refund.", "A free meal is waiting.",
                  "This was not our fault."):
        bad = {**m, "text": m["text"] + " " + extra}
        assert any("approved" in p or "blame" in p for p in message_problems(bad, world)), extra
    backed = {**m, "text": m["text"] + " Your hotel voucher is confirmed.", "backed_by": "Q05-approved-1"}
    assert not [p for p in message_problems(backed, {**world, "approved": {"Q05-approved-1"}}) if "approved" in p]


def test_no_sensitive_details_or_other_passengers():
    m, world = a_delay_message()
    for extra in ("We know you need a wheelchair.", "As a child alone, stay with the crew.", "Seat for PAX0002."):
        assert message_problems({**m, "text": m["text"] + " " + extra}, world), extra


def test_names_only_in_the_app_and_only_for_passengers_it_works_on():
    m, world = a_delay_message("app", "Maya")
    assert m["text"].startswith("Hi Maya. ") and not message_problems(m, world)
    assert any("outside the app" in p for p in message_problems({**m, "channel": "sms"}, world))
    assert any("working on" in p for p in message_problems(m, {**world, "working_on": set()}))
    ctx, *_ = delayed_day()
    with pytest.raises(ContractViolation, match="working on"):
        ctx.read("passengers", ["id", "full_name"], where={"id": "P1"})        # not declared: refused
    ctx.working_on(["P1"])
    assert ctx.read("passengers", ["id", "full_name"], where={"id": "P1"})[0]["full_name"] == "Maya Torres"
    with pytest.raises(ContractViolation):
        ctx.read("passengers", ["id", "full_name"], where={"id": "P2"})
    with pytest.raises(ContractViolation):
        ctx.read("passengers", ["id", "contact_channels"], where={"id": "P1"})  # contacts never, even when working on


def test_sms_length_and_plain_english():
    m, world = a_delay_message("sms")
    assert len(m["text"]) <= S["messages"]["sms_max_chars"]
    long = {**m, "text": m["text"] + " Please check again before you go to the gate." * 6}
    assert any("characters" in p for p in message_problems(long, world))
    hard = {**m, "text": m["text"] + " Notwithstanding operational irregularities, comprehensive reaccommodation considerations "
                                     "necessitate institutional deliberation."}
    assert any("plain English" in p for p in message_problems(hard, world))


def test_expected_end_is_never_an_exact_promise():
    m, world = a_delay_message()
    assert "We expect flights to move again around" not in m["text"] or "this may change" in m["text"]
    low = deepcopy(world)
    low["situation"]["suspected"]["expected_end_likely"]["confidence"] = 0.2
    assert any("end time" in p for p in message_problems(m, low))              # low confidence: no time may be given


def test_every_template_was_reviewed_and_contains_no_forbidden_word():
    T = templates()
    for name, t in T["templates"].items():
        assert t["reviewed_for_promises"], name
        text = " ".join(str(v) for k, v in t.items() if isinstance(v, str))
        found = forbidden_found(text)
        assert not found or name == "staff_note", (name, found)
    assert "will contact you about overnight" not in str(T["templates"])        # nobody would actually do it yet


# ------------------------------------------------------------------ delivery: replaced when the truth changes first

def test_a_message_is_dropped_if_the_trip_changes_before_it_arrives():
    from simulator.channels import Channels
    from simulator.config import load_config
    cfg = deepcopy(load_config())
    cfg["channels"]["sms"].update(base_min=5, jitter_min=0, per_100_changes_min=0, fail_chance=0)
    truth = {"P1": {"itinerary": ("WN1001",), "status": "delayed", "destination": "BWI", "bag_routing": (), "message": "x"}}
    ch = Channels(cfg, random.Random(1), truth, 300)
    ch.msg_rng = random.Random(2)
    m = {"id": "M1", "type": "delay_update", "passenger_id": "P1", "channel": "sms", "text": "x",
         "trip_facts": {"itinerary": ["WN1001"], "status": "delayed"}}
    assert ch.deliver_messages(600, {"id": 7, "messages": [m]})["accepted"]
    ch.truth["P1"] = {**truth["P1"], "status": "cancelled"}                    # the trip changes while it travels
    ch.apply_messages(605)
    assert [e["type"] for e in ch.events] == ["message_dropped"]
    assert not ch.deliver_messages(606, {"id": 8, "messages": [m]})["accepted"]   # and an out-of-date one is never sent


# ------------------------------------------------------------------ seed 7 with co-pilot

@pytest.fixture(scope="module")
def co7():
    from backend.seed.build import build
    from simulator.run import simulate
    return simulate(7, "a31_storm", mode="copilot", data=build())


def _delivered(r):
    return [e for e in r.world.events if e["type"] == "message_delivered"]


def test_no_sent_message_contains_a_promise_blame_or_sensitive_detail(co7):
    texts = [e["payload"]["text"] for e in _delivered(co7)] + [n["text"] for n in co7.metrics["staff_notes"]]
    assert len(texts) > 100
    assert not [t for t in texts if forbidden_found(t)]
    assert co7.metrics["promises_found"] == 0


def test_cancellation_overnight_and_correction_messages_were_approved_by_the_supervisor(co7):
    log = {e["id"]: e for e in co7.copilot.messages.log}
    need = ("cancellation_rebooking", "cancellation_only", "overnight_notice", "rebooking", "correction")
    hits = [e for e in _delivered(co7) if e["payload"]["type"] in need]
    assert hits
    for e in hits:
        lg = log[e["payload"]["message_id"]]
        assert lg["action"] in ("draft_message", "send_mass_message", "resend_on_other_channel")
        assert lg["approved_by"] == "customer_service_supervisor" or lg.get("resend_of")
    auto = [log[e["payload"]["message_id"]] for e in _delivered(co7) if e["payload"]["type"] not in need]
    big = defaultdict(int)
    for lg in co7.copilot.messages.log:
        big[lg["rec"]] += 1
    for lg in co7.copilot.messages.log:          # any batch to more than mass_message_passengers needs one approval
        if big[lg["rec"]] > S["messages"]["mass_message_passengers"]:
            assert lg["action"] == "send_mass_message"
    assert all(lg["action"] in ("send_status_update", "send_mass_message", "resend_on_other_channel") for lg in auto)


def test_anti_spam_at_most_n_messages_per_passenger_per_hour(co7):
    by = defaultdict(list)
    for lg in co7.copilot.messages.log:
        if lg["status"] == "sent" and lg["passenger_id"] and not lg.get("resend_of"):
            by[lg["passenger_id"]].append(lg["sent_at"])
    from datetime import datetime
    for times in by.values():
        ts = sorted(datetime.fromisoformat(t) for t in times)
        for i, t in enumerate(ts):
            assert sum(t <= x < t + timedelta(hours=1) for x in ts[i:]) <= S["messages"]["max_per_hour"]


def test_only_people_with_an_app_or_sms_get_messages_and_on_their_own_channels(co7):
    from backend.screens import screens_for
    from backend.seed.build import build
    has = {p["id"]: set(screens_for(p["contact_channels"])) for p in build()["passengers"]}
    for e in _delivered(co7):
        assert e["payload"]["channel"] in has[e["payload"]["passenger_id"]] & {"app", "sms"}
    names = [e for e in _delivered(co7) if e["payload"]["text"].startswith("Hi ")]
    assert names and all(e["payload"]["channel"] == "app" for e in names)
    reads = co7.copilot.context.name_reads                  # every name read: only for passengers then messaged in the app
    app_pax = {lg["passenger_id"] for lg in co7.copilot.messages.log if lg["channel"] == "app"}
    assert reads and {pid for r in reads for pid in r[1]} <= app_pax


def test_maya_gets_a_delay_notice_and_an_approved_cancellation_for_wn3005_with_no_promise(co7):
    mine = co7.metrics["maya_messages"]
    types = [m["type"] for m in mine]
    assert "delay_update" in types
    canc = [m for m in mine if m["type"] == "cancellation_rebooking"]
    assert len(canc) == 1
    text = canc[0]["text"]
    assert "WN1033" in text and "cancelled" in text and "WN3005" in text and "06:30 on Oct 7" in text
    assert canc[0]["decided"].startswith("approved by customer service supervisor after")
    assert not [m for m in mine if forbidden_found(m["text"])]
    assert not re.search(r"hotel|money|compensat|voucher|refund", " ".join(m["text"] for m in mine).lower())


def test_messages_do_not_change_screens_or_the_world(co7):
    from backend.seed.build import build
    from simulator.comparison import truth_fingerprint
    from simulator.run import simulate
    today = simulate(7, "a31_storm", data=build())
    assert truth_fingerprint(today) == truth_fingerprint(co7)
    assert co7.metrics["passenger_minutes_of_disagreement"] < today.metrics["passenger_minutes_of_disagreement"]
    assert not [e for e in today.world.events if e["type"] in ("message_delivered", "message_sent")]
