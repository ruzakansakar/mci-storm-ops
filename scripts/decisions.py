"""Print a summary of the decision catalog (config/decisions.yaml).

Run:  python -m scripts.decisions
"""
import collections

from backend.contracts.decisions import load_catalog


def table(title, counter, order=None):
    print(f"\n{title}")
    for k in order or sorted(counter):
        print(f"  {k:32s} {counter.get(k, 0):4d}")


def main():
    cat = load_catalog()
    ds = cat["decisions"]
    print(f"{len(ds)} decisions in config/decisions.yaml")
    table("PER FAMILY", collections.Counter(f"{d['id'][0]} {d['family']}" for d in ds))
    levels = collections.Counter(d["decided_by"]["level"] for d in ds)
    table("PER LEVEL (who decides)", levels, ["auto", "needs_approval", "human_only"])
    table("PER AGENT (who raises it)", collections.Counter(d["proposed_by"] for d in ds),
          ["orchestrator", "situation", "network_recovery", "station_staff", "passenger_care", "communication"])
    story = [d["id"] for d in ds if d["in_story"]]
    print(f"\nIN THE A31_STORM STORY (seed 7): {len(story)} decisions")
    print("  " + ", ".join(story))
    print("\nHUMAN ONLY: " + ", ".join(f"{d['id']} ({d['decided_by']['role']})" for d in ds if d["decided_by"]["level"] == "human_only"))
    gaps = cat["gaps"]
    print("\nGAPS")
    missing = gaps["decisions_the_simulator_cannot_trigger"]
    print(f"  decisions the simulator can't trigger yet (simulator_event: missing): {len(missing)}")
    print("    " + ", ".join(missing))
    print(f"  rules a decision needed that were added as assumptions: {len(gaps['rules_added_as_assumptions'])}")
    print("    " + ", ".join(gaps["rules_added_as_assumptions"]))


if __name__ == "__main__":
    main()
