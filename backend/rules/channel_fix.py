"""Fail-closed validation for structured copies of confirmed trip records."""
FIELDS = {"itinerary", "status", "destination", "bag_routing"}
ACTIONS = {"fix_channel_mismatch", "resend_on_other_channel"}


def reasons(proposal, world):
    if proposal.get("action") not in ACTIONS or proposal.get("messages"):
        return []                                    # a message resend is checked by the message fact check instead
    out = []
    pid, channel = proposal.get("passenger_id"), proposal.get("channel")
    truth = world.get("confirmed_trips", {}).get(pid)
    copy = proposal.get("confirmed")
    held = set(proposal.get("held_fields") or ())
    if not isinstance(copy, dict) or set(copy) | held != FIELDS or set(copy) & held or truth is None:
        out.append("a fix needs exactly the confirmed trip fields (minus details held for an open conflict)")
    elif any(copy[k] != truth[k] for k in copy):
        out.append("the correction does not match the confirmed truth")
    if held and not held <= set(world.get("conflict_held_fields", {}).get(pid, ())):
        out.append("a detail may only be left out while a Situation conflict is open about it")
    if proposal.get("truth_as_of") != world.get("trip_versions", {}).get(pid):
        out.append("the trip changed after this correction was proposed")
    if not world.get("truth_feed_fresh") or not world.get("flight_feed_fresh"):
        out.append("the truth or flight operations feed is stale or unknown")
    if not world.get("conflicts_known"):
        out.append("source conflicts have not been checked")
    elif set(world.get("trip_flights", {}).get(pid, ())) & set(world.get("open_conflicts", ())) and \
            not set(world.get("conflict_held_fields", {}).get(pid, ())) <= held:
        out.append("a source conflict (A09) is open on this trip and the correction does not hold the conflicted details")
    if channel not in ("app", "sms", "gate_screen", "call_center"):
        out.append("unsupported delivery channel")
    elif channel not in world.get("screens", {}).get(pid, ()):
        out.append("this passenger does not have that screen")
    if proposal.get("action") == "resend_on_other_channel":
        if channel == proposal.get("original_channel") or channel not in world.get("contact_types", {}).get(pid, ()):
            out.append("resend requires another channel this passenger has")
    if any(proposal.get(k) for k in ("message", "offer", "draft_text", "promises", "promises_compensation")):
        out.append("screen corrections cannot add text, offers or promises")
    return out
