#!/usr/bin/env python3
"""See the mentions that are held for manual review, and record your answer. Standard library only.

    python tools/review.py list
    python tools/review.py decide <record id> not_duplicate --note "different article"     (the mention is shown)
    python tools/review.py decide <record id> duplicate     --note "same as the curated one"  (the mention stays hidden for good)

A mention the collector finds that looks like a duplicate of a curated mention (same source, same day) is NOT shown in the Mentions
Library or the Overview until you answer. Answers are saved in data/review_decisions.json; commit that file to apply them.
"""
import argparse, datetime as dt, json, sys
from pathlib import Path

FLAGS, DECISIONS = "mentions_flags.json", "data/review_decisions.json"
VALUES = ("not_duplicate", "duplicate")


def read(path, default):
    p = Path(path)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else default


def pending(flags, decisions):
    """[(record, [curated records it may duplicate])] for every held record that has no answer yet."""
    out, seen = [], set()
    for flag in flags.get("flags", []):
        if flag.get("type") != "possible_duplicate":
            continue
        twins = [r for r in flag["records"] if r.get("curated")]
        for r in flag["records"]:
            if not r.get("curated") and r["id"] not in decisions and r["id"] not in seen:
                seen.add(r["id"]); out.append((r, twins, flag["link"]))
    return out


def cmd_list(a):
    flags, decisions = read(a.flags, {"flags": []}), {d["id"]: d for d in read(a.decisions, {"decisions": []})["decisions"]}
    items = pending(flags, decisions)
    print(f"{len(items)} mention(s) held for manual review, {len(decisions)} already answered.\n")
    for r, twins, link in items:
        print(f"  id:     {r['id']}\n  title:  {r['title']}\n  source: {r['source']}   date: {str(r['published_at'])[:10]}\n  link:   {link}")
        for t in twins:
            print(f"  may be the same story as the curated mention: {t['title']}   ({t['id']})")
        print(f"  answer: python tools/review.py decide {r['id']} not_duplicate|duplicate\n")
    return 0


def cmd_decide(a):
    flags = read(a.flags, {"flags": []})
    data = read(a.decisions, {"schema_version": 1, "decisions": []})
    known = {r["id"] for f in flags.get("flags", []) if f.get("type") == "possible_duplicate" for r in f["records"] if not r.get("curated")}
    known |= {d["id"] for d in data["decisions"]}          # an answer can be changed later, after the collector has cleared the flag
    if a.id not in known and not a.force:
        print(f"'{a.id}' is not a record held for review (see: python tools/review.py list). Use --force to record an answer anyway.", file=sys.stderr)
        return 2
    data["decisions"] = [d for d in data["decisions"] if d["id"] != a.id]
    entry = {"id": a.id, "decision": a.decision, "decided_on": dt.date.today().isoformat()}
    if a.note:
        entry["note"] = a.note
    data["decisions"].append(entry); data["decisions"].sort(key=lambda d: d["id"])
    Path(a.decisions).parent.mkdir(parents=True, exist_ok=True)
    Path(a.decisions).write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    what = "will be SHOWN in the Mentions Library and the Overview" if a.decision == "not_duplicate" else "will stay hidden (confirmed duplicate)"
    print(f"Saved. This mention {what} after the next collector run.\nNext: commit {a.decisions} to main (on GitHub: open the file, click the pencil, commit). That starts the run.")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--flags", default=FLAGS); ap.add_argument("--decisions", default=DECISIONS)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("list")
    d = sub.add_parser("decide"); d.add_argument("id"); d.add_argument("decision", choices=VALUES); d.add_argument("--note"); d.add_argument("--force", action="store_true")
    a = ap.parse_args(argv)
    return cmd_list(a) if a.cmd == "list" else cmd_decide(a)


if __name__ == "__main__":
    sys.exit(main())
