"""The airline's autonomy settings for live mode (config/autonomy.yaml): what the co-pilot does on its own, logged.

A setting that would automate a safety or legal item (crew legality, diversion, cancellation, tarmac, lightning) is refused
when the file is loaded. Matching is on the proposal the agent made: its action, decision ids, kind and cost (options' cost_usd).
"""
import yaml

from backend.settings import ROOT


class AutonomyError(ValueError):
    """A setting that would make a safety or legal decision automatic."""


def load(path=None) -> dict:
    doc = yaml.safe_load((path or ROOT / "config" / "autonomy.yaml").read_text(encoding="utf-8"))
    validate(doc)
    return doc


def validate(doc: dict):
    never = {a: cat for cat, acts in (doc.get("never_automatic") or {}).items() for a in acts}
    for cat in ("crew_legality", "diversion", "cancellation", "tarmac", "lightning"):
        if cat not in (doc.get("never_automatic") or {}):
            raise AutonomyError(f"the never-automatic list must keep its '{cat}' category")
    for item in doc.get("automatic") or []:
        a = item.get("action")
        if a in never:
            raise AutonomyError(f"'{item.get('id')}' would make '{a}' automatic, but {never[a].replace('_', ' ')} is a safety or "
                                f"legal decision: a person must decide it")
        for k in ("id", "rule", "action"):
            if not item.get(k):
                raise AutonomyError(f"an automatic setting needs '{k}'")


def cost_of(proposal: dict) -> float:
    opts = (proposal or {}).get("options") or []
    if opts:
        return float(sum(o.get("cost_usd") or 0 for o in opts[:1]))
    return float((proposal or {}).get("owed_total_usd") or 0)


def match(doc: dict, agent: str, proposal: dict, rec: dict):
    """The automatic setting this proposal falls under, or None (then a person decides)."""
    p = proposal or {}
    for item in doc.get("automatic") or []:
        if p.get("action") != item["action"] or p.get("human_only"):
            continue
        if item.get("decision_ids") and not set(item["decision_ids"]) & set(p.get("decision_ids") or []):
            continue
        if item.get("kind") and p.get("kind") != item["kind"]:
            continue
        if item.get("max_cost_usd") is not None:
            cost = cost_of(rec) if rec else cost_of(p)
            if cost > float(item["max_cost_usd"]):
                continue
        return item
    return None
