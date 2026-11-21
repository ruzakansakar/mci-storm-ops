"""Print a one-page summary of the seven agent contracts.

Run:  python -m scripts.contracts
"""
import textwrap

from backend.contracts import load_contracts
from backend.contracts.loader import AGENT_NAMES, PII_AGENTS


def cell(text, width):
    return textwrap.wrap(str(text), width) or [""]


def main():
    contracts = load_contracts()
    cols = [("agent", 17, lambda c: c.agent),
            ("purpose", 44, lambda c: c.purpose),
            ("may propose", 26, lambda c: ", ".join(c.actions) or "nothing (coordinates / checks)"),
            ("writes", 22, lambda c: ", ".join(c.writes)),
            ("names?", 6, lambda c: "yes" if c.agent in PII_AGENTS else "no"),
            ("age s", 5, lambda c: c.max_data_age_sec),
            ("time s", 6, lambda c: c.time_budget_sec),
            ("AI?", 3, lambda c: "yes" if c.ai_allowed else "no")]
    line = "  ".join("-" * w for _, w, _ in cols)
    print("  ".join(h.ljust(w) for h, w, _ in cols))
    print(line)
    for name in AGENT_NAMES:
        c = contracts[name]
        cells = [cell(f(c), w) for _, w, f in cols]
        for i in range(max(len(x) for x in cells)):
            print("  ".join((x[i] if i < len(x) else "").ljust(w) for x, (_, w, _) in zip(cells, cols)))
        print()
    print("NEVER (one line each):")
    for name in AGENT_NAMES:
        print(f"  {name:17s} " + contracts[name]["never"][0])
    print("\nReads are limited to listed tables and columns. Passenger names and contact details: only passenger_care and communication.")
    print("Nobody writes to truth tables (flights, bookings, crews, ...); agents write proposals, their own events and the audit log.")
    print("Only the orchestrator writes incidents. A language model is allowed only for situation summaries and message drafts (with a template fallback).")


if __name__ == "__main__":
    main()
