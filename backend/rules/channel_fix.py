"""Fail-closed validation for structured copies of confirmed trip records."""
FIELDS = {"itinerary", "status", "destination", "bag_routing"}
ACTIONS = {"fix_channel_mismatch", "resend_on_other_channel"}


def reasons(proposal, world):
    if proposal.get("action") not in ACTIONS:
        return []
    out = []
    pid, channel = proposal.get("passenger_id"), proposal.get("channel")
    truth = world.get("confirmed_trips", {}).get(pid)
    copy = proposal.get("confirmed")
    if not isinstance(copy, dict) or set(copy) != FIELDS or truth is None:
        out.append("a fix needs exactly the confirmed trip fields")
    elif any(copy[k] != truth[k] for k in FIELDS):
        out.append("the correction does not match the confirmed truth")
    if proposal.get("truth_as_of") != world.get("trip_versions", {}).get(pid):
        out.append("the trip changed after this correction was proposed")
    if not world.get("truth_feed_fresh") or not world.get("flight_feed_fresh"):
        out.append("the truth or flight operations feed is stale or unknown")
    if not world.get("conflicts_known"):
        out.append("source conflicts have not been checked")
    elif set(world.get("trip_flights", {}).get(pid, ())) & set(world.get("open_conflicts", ())):
        out.append("a source conflict (A09) is open on this trip")
    if channel not in ("app", "sms", "gate_screen", "call_center"):
        out.append("unsupported delivery channel")
    if proposal.get("action") == "resend_on_other_channel":
        if channel == proposal.get("original_channel") or channel not in world.get("contact_types", {}).get(pid, ()):
            out.append("resend requires another channel this passenger has")
    if any(proposal.get(k) for k in ("message", "offer", "draft_text", "promises", "promises_compensation")):
        out.append("screen corrections cannot add text, offers or promises")
    return out
