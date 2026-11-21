"""Read the decision catalog (config/decisions.yaml) and the shared AI guardrails (config/agents/_shared.yaml)."""
from functools import lru_cache

import yaml

from backend.settings import ROOT

DECISIONS_FILE = ROOT / "config" / "decisions.yaml"
SHARED_FILE = ROOT / "config" / "agents" / "_shared.yaml"
DECISION_FIELDS = ("id", "family", "question", "trigger", "deadline", "proposed_by", "action", "decided_by", "checked_by", "safety_rung",
                   "rules", "data_needed", "affects", "in_story", "simulator_event", "status")


@lru_cache
def load_decisions() -> dict[str, dict]:
    doc = yaml.safe_load(DECISIONS_FILE.read_text(encoding="utf-8"))
    return {d["id"]: d for d in doc["decisions"]}


@lru_cache
def load_catalog() -> dict:
    return yaml.safe_load(DECISIONS_FILE.read_text(encoding="utf-8"))


@lru_cache
def load_shared() -> dict:
    return yaml.safe_load(SHARED_FILE.read_text(encoding="utf-8"))
