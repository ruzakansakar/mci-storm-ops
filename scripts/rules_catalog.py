"""Print every rule as a table: id, value, source, enforced.

Run:  python -m scripts.rules_catalog
"""
from backend.rules import load_rules


def short(value, width=44):
    text = str(value).replace("\n", " ")
    return text if len(text) <= width else text[: width - 3] + "..."


def main():
    entries = load_rules()["entries"]
    rows = [(e["id"], short(e["value"]), e["source"], "yes" if e["enforced"] else "no", e["file"].replace(".yaml", "")) for e in entries]
    head = ("id", "value", "source", "enforced", "file")
    widths = [max(len(str(r[i])) for r in rows + [head]) for i in range(len(head))]
    line = lambda r: "  ".join(str(c).ljust(w) for c, w in zip(r, widths))
    print(line(head))
    print("  ".join("-" * w for w in widths))
    for r in rows:
        print(line(r))
    by_source = {s: sum(e["source"] == s for e in entries) for s in ("law", "southwest", "assumption", "class_files")}
    enforced = sum(e["enforced"] for e in entries)
    print(f"\n{len(entries)} rules | {enforced} enforced by a check, {len(entries) - enforced} not enforced | "
          + ", ".join(f"{n} {s}" for s, n in by_source.items()))


if __name__ == "__main__":
    main()
