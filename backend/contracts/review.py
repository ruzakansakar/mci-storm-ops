"""The orchestrator's side of the rule check: run check() on a saved proposal, record pass/block, and show humans only what passed.

This is plumbing, not agent logic: it decides nothing itself. check() (backend/rules/check.py) gives the verdict; the orchestrator
records it; a block can be lifted only by a named human (backend/rules/override.py), never here.
"""
from backend.contracts.context import AgentContext
from backend.rules.check import check


def run_rule_check(orchestrator: AgentContext, recommendation_id: int, proposal: dict, pending: list[dict], world: dict) -> dict:
    if orchestrator.agent != "orchestrator":
        raise PermissionError("only the orchestrator runs the rule check")
    result = check(proposal, pending, world, orchestrator.rules)
    orchestrator.record_rule_check(recommendation_id, result)
    return result


def shown_to_humans(cur, run_id: str | None = None) -> list[int]:
    """The proposals a human may see: only those the orchestrator's rule check passed. Unchecked or blocked ones never show
    (a blocked one is shown separately, with its reasons, as a block)."""
    q = "SELECT id FROM recommendations WHERE rule_check_ok IS TRUE AND status = 'pending'"
    args = ()
    if run_id:
        q += " AND run_id = %s"
        args = (run_id,)
    cur.execute(q + " ORDER BY id", args)
    return [r[0] for r in cur.fetchall()]
