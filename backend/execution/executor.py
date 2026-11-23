"""Check each proposal, then dispatch only automatic, passing actions.

The caller supplies the delivery adapter and durable event/audit recorder. This
module never writes operational truth or bypasses the rule checker.
"""
from backend.rules.autonomy import autonomy_for
from backend.rules.check import check
from backend.rules import load_rules
from backend.contracts.loader import load_contracts


def check_only(proposal, world, pending=(), rules=None):
    """The rule check plus the contract check, without dispatching anything (also used again when an approval arrives)."""
    rules = rules or load_rules()
    verdict = check(proposal, list(pending), world, rules)
    contract = load_contracts().get(proposal.get("agent"))
    action = proposal.get("action")
    if contract is None or action not in contract.actions or not contract.enabled(action):
        reason = "the proposing agent's current contract does not enable this action"
        verdict = {**verdict, "result": "block", "reasons": verdict["reasons"] + [reason],
                   "rule_ids": verdict["rule_ids"] + ["agent_contract"], "needs_human": "customer_service_supervisor"}
    return verdict


def execute(proposal, world, deliver, record, pending=(), rules=None):
    rules = rules or load_rules()
    verdict = check_only(proposal, world, pending, rules)
    record("rule_check_passed" if verdict["result"] == "pass" else "rule_check_blocked",
           {"proposal": proposal, "verdict": verdict})
    automatic = autonomy_for(proposal.get("action"), rules,
                             safety_rung=proposal.get("safety_rung"))["level"] == "auto"
    if verdict["result"] != "pass" or not automatic:
        return {"status": "pending", "verdict": verdict}
    receipt = deliver(proposal)
    if isinstance(receipt, dict) and receipt.get("accepted") is False:
        record("channel_fix_blocked", {"proposal": proposal, "receipt": receipt})
        return {"status": "pending", "verdict": verdict, "receipt": receipt}
    record("channel_fix_applied" if "messages" not in proposal else "message_sent", {"proposal": proposal, "receipt": receipt})
    return {"status": "applied", "verdict": verdict, "receipt": receipt}
