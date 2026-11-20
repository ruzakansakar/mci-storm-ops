"""Reads config/simulator.yaml (every number the simulator uses)."""
from functools import lru_cache

import yaml

from backend.settings import ROOT

CONFIG_PATH = ROOT / "config" / "simulator.yaml"
RULES_PATH = ROOT / "config" / "rules.yaml"
CHANNELS = ("app", "sms", "gate_screen", "call_center")


@lru_cache(maxsize=1)
def load_config() -> dict:
    cfg = yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))
    # Legal and safety numbers live only in rules.yaml. We read them here as plain YAML (never the rules code).
    cfg["limits"] = yaml.safe_load(RULES_PATH.read_text(encoding="utf-8"))["limits"]
    for key in ("day", "causes", "side_events", "scenarios", "physics", "baseline", "channels", "load", "labels", "metrics"):
        if key not in cfg:
            raise ValueError(f"config/simulator.yaml is missing the '{key}' section")
    for ch in CHANNELS:
        if ch not in cfg["channels"]:
            raise ValueError(f"config/simulator.yaml has no settings for channel '{ch}'")
    return cfg
