"""Measuring the co-pilot's messages (Phase 4b-3). Measurement only: it reads the simulator's record of what was delivered and
the co-pilot's own log, never feeds anything back to an agent."""
import re
from collections import Counter
from statistics import median

from functools import lru_cache

import yaml

from backend.settings import ROOT


@lru_cache
def templates() -> dict:
    """The forbidden-word lists, read straight from the config (the simulator never imports the rule code)."""
    return yaml.safe_load((ROOT / "config" / "message_templates.yaml").read_text(encoding="utf-8"))

INFORM = ("cancellation_rebooking", "cancellation_only", "overnight_notice", "rebooking")


def forbidden_found(text: str) -> list[str]:
    """Every forbidden word or phrase in a sent text (promises, blame, sensitive details). Must always be empty."""
    T = templates()
    body = text.replace(T["staff_allowed_sentence"], "")
    return [w for g in ("promises", "blame", "sensitive") for w in T["forbidden"][g]
            if re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", body.lower())]


def message_metrics(world, copilot, focus, cancellations: dict, screens: dict) -> dict:
    log = {e["id"]: e for e in copilot.messages.log}
    delivered = [e for e in world.events if e["type"] == "message_delivered"]
    failed = [e for e in world.events if e["type"] == "channel_update_failed" and (e["payload"] or {}).get("kind") == "message"]
    dropped = [e for e in world.events if e["type"] == "message_dropped"]
    staff = [e for e in world.events if e["type"] == "staff_note_published"]
    agent = [e for e in world.events if e["actor"] == "agent" and e["actor_name"] == "communication"]
    by_pax = Counter(e["payload"]["passenger_id"] for e in delivered)
    reachable = [p for p, s in screens.items() if {"app", "sms"} & set(s)]
    # time from a cancellation to a correct cancellation message reaching the passenger
    inform, never = [], []
    for pid, times in cancellations.items():
        if not {"app", "sms"} & set(screens.get(pid, ())):
            continue
        for tc in times:
            got = next((e["t"] for e in delivered if e["payload"]["passenger_id"] == pid and e["t"] >= tc
                        and e["payload"]["type"] in INFORM), None)
            (inform.append(got - tc) if got is not None else never.append(pid))
    blocked = Counter()
    for e in agent:
        if e["type"] == "message_blocked":
            blocked.update(r.split(" (")[0][:70] for r in e["payload"]["reasons"])
        if e["type"] == "rule_check_blocked" and "messages" in (e["payload"].get("proposal") or {}):
            blocked.update("rule check: " + r.split(": ", 1)[-1][:60] for r in e["payload"]["verdict"]["reasons"])
    promises = [{"message_id": e["payload"]["message_id"], "found": forbidden_found(e["payload"]["text"])}
                for e in delivered + [{"payload": {"message_id": s["payload"]["message_id"], "text": s["payload"]["text"]}}
                                      for s in staff]]
    promises = [p for p in promises if p["found"]]
    # "next update by HH:MM": was another message sent to that passenger by then?
    kept = broken = released = 0
    gone = {(pid, by[:16]) for pid, by, _ in copilot.messages.released}
    late = []
    sent_at = {}
    for e in copilot.messages.log:
        if e["status"] == "sent" and e["passenger_id"]:
            sent_at.setdefault(e["passenger_id"], []).append(e["sent_at"])
    for e in copilot.messages.log:
        m = re.search(r"Next update by (\d\d:\d\d)", e["text"])
        if e["status"] != "sent" or not m or not e["passenger_id"]:
            continue
        due = e["sent_at"][:11] + m.group(1)
        later = [x for x in sent_at[e["passenger_id"]] if x > e["sent_at"]]
        if later and min(later)[:16] <= due:
            kept += 1
        elif (e["passenger_id"], due) in gone:
            released += 1                                # at that time the flight had left or the trip had settled
        else:
            broken += 1
            late.append({"passenger_id": e["passenger_id"], "promised": due[11:], "next": min(later)[11:16] if later else None})
    waits = [w for _, w in copilot.waits]
    maya = []
    for e in delivered:
        p = e["payload"]
        if p["passenger_id"] != focus:
            continue
        lg = log.get(p["message_id"], {})
        maya.append({"t": e["t"], "channel": p["channel"], "type": p["type"], "text": p["text"],
                     "decided": f"approved by {lg['approved_by'].replace('_', ' ')} after {lg['waited_min']:.0f} min"
                     if lg.get("approved_by") else "automatic (restates the confirmed truth)"})
    types = Counter(e["payload"]["type"] for e in delivered)
    return {
        "messages_delivered": len(delivered), "messages_by_type": dict(types),
        "messages_per_passenger_avg": round(len(delivered) / max(1, len(reachable)), 2),
        "messages_per_passenger_max": max(by_pax.values(), default=0),
        "messages_failed_delivery": len(failed), "messages_resent": sum(1 for e in copilot.messages.log if e.get("resend_of")),
        "messages_dropped_truth_changed": len(dropped),
        "messages_withdrawn": sum(1 for e in agent if e["type"] == "message_withdrawn"),
        "approvals_void_truth_changed": len(copilot.voided),
        "messages_blocked": sum(blocked.values()), "blocked_reasons": dict(blocked.most_common(10)),
        "approval_waits_min": {"count": len(waits), "median": median(waits) if waits else None, "max": max(waits, default=None)},
        "corrections_sent": types.get("correction", 0),
        "cancellation_inform_min": {"count": len(inform), "median": median(inform) if inform else None,
                                    "max": max(inform, default=None), "never": len(never)},
        "promises_found": len(promises), "promises_detail": promises[:5],
        "next_update_promises": {"kept": kept, "no_longer_needed": released, "broken": broken, "broken_examples": late[:5]},
        "staff_notes": [{"t": e["t"], "text": e["payload"]["text"]} for e in staff],
        "maya_messages": maya,
        "humans": copilot.humans.summary() if copilot.humans else None,
    }


def conflict_metrics(copilot) -> list[dict]:
    """Each conflict hold Communication saw: how long, and how it ended (later evidence, a person, or still open)."""
    sit = copilot.situation_summary()
    out = []
    for c in sit["cleared"]:
        how = c.get("status") or "gone"
        out.append({"flight_id": c["flight_id"], "since": c["since"], "cleared_at": c["cleared_at"],
                    "held_from": c.get("held_from"), "how": c.get("how"), "closed_by": {"settled": "evidence",
                    "resolved_by_human": "a person"}.get(how, how)})
    for fid, c in sit["open_at_end"].items():
        out.append({"flight_id": fid, "since": c["since"], "cleared_at": None, "closed_by": "still open at day end"})
    return out
