"""The cancellation guard (fix-seats): a cancellation must say how many passengers it strands, compared with the best
alternative (delay, or keep as scheduled), and may not strand more than that alternative unless crew or aircraft legality
leaves no alternative. Rule: cancellation_must_not_strand_more (config/rules/operations.yaml)."""


def cancellation_guard(proposal: dict) -> str | None:
    """None if the cancellation may go to a person; otherwise the reason (with the numbers)."""
    if proposal.get("action") != "cancel_flight":
        return None
    c = (proposal.get("option") or {}).get("cancellation")
    if not isinstance(c, dict) or c.get("stranded") is None:
        return "a cancellation without its estimate of passengers stranded (and the alternative's) can't be approved"
    if c.get("no_alternative"):
        return None
    alt = c.get("alternative_stranded")
    if alt is None:
        return "a cancellation without the stranded estimate of its alternative (delay or keep) can't be approved"
    if c["stranded"] > alt:
        return (f"cancelling strands about {c['stranded']} passengers overnight; {c.get('alternative', 'the alternative')} "
                f"strands about {alt}")
    return None
