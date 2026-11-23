"""Shared cause names, without any simulator settings."""
from functools import lru_cache
import yaml
from backend.settings import ROOT


@lru_cache
def vocabulary():
    return yaml.safe_load((ROOT / "config" / "channel_causes.yaml").read_text(encoding="utf-8"))


def canonical(name):
    return next((key for key, value in vocabulary().items() if name in (key, value["legacy"])), "unknown")
