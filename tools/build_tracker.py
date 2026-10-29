#!/usr/bin/env python3
"""Builds the TRIADA Media Tracker page from the template and the JSON database. Standard library only.

    python tools/build_tracker.py --template tracker/tracker.template.html --data mentions_public.json \\
        --exclusions data/exclusions.json --curated data/mentions_curated.json --out site/tracker/index.html --report site/tracker/build_report.json

The template is the approved 311-mention page with its table rows replaced by <!--TMI:ROWS--> and its two
"311 of 311" counters replaced by {{COUNT}}. Everything else (design, scripts, Library, Overview) is copied untouched, and
both the Mentions Library and the Overview read the table rows this script writes: one database, one set of rows.
"""
import argparse, html, json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from tracker_records import library_records

ROWS_MARK, COUNT_MARK = "<!--TMI:ROWS-->", "{{COUNT}}"
SEP = "\n        "

def _a(v):  return html.escape(str(v), quote=True)
def _t(v):  return html.escape(str(v), quote=False)

def render_row(rec):
    """One <tr> exactly as the Mentions Library and the Overview expect it."""
    x = rec["tracker"]
    attrs = [("data-ts", x["ts"]), ("data-day", x["day"]), ("data-platform", rec["platform"]), ("data-company", int(x["company"])), ("data-ceo", int(x["ceo"]))]
    if x.get("import_source"):                       attrs.append(("data-origin", x["import_source"]))
    if x.get("excel_row") is not None:               attrs.append(("data-excel-row", x["excel_row"]))
    if rec.get("date_source") == "assigned_manual":  attrs += [("data-date-source", "assigned"), ("data-date-note", x["date_note"])]
    if x.get("added"):                               attrs.append(("data-added", x["added"]))
    tr = " ".join(f'{k}="{_a(v)}"' for k, v in attrs)
    date_tip = f' title="{_a(x["date_tooltip"])}"' if x.get("date_tooltip") else ""
    src_tip = f' title="{_a(x["tooltip_source"])}"' if x.get("tooltip_source") is not None else ""
    t_tip = f' title="{_a(x["tooltip_title"])}"' if x.get("tooltip_title") is not None else ""
    return (f'<tr {tr}><td class="tmi-col-date"{date_tip}>{_t(x["date_label"])}</td><td class="tmi-col-platform">{_t(rec["platform"])}</td>'
            f'<td class="tmi-col-source"{src_tip}><span class="s">{_t(rec["source"])}</span></td>'
            f'<td class="tmi-col-title"><span class="t"{t_tip}>{_t(rec["title"])}</span></td>'
            f'<td class="tmi-col-link"><a class="tmi-link" href="{_a(rec["url"])}" target="_blank" rel="noopener noreferrer" aria-label="Open article: {_a(rec["title"])}">Open article</a></td></tr>')

def build(template_text, records):
    if template_text.count(ROWS_MARK) != 1 or template_text.count(COUNT_MARK) != 4:
        raise SystemExit("template is not in the expected shape (1 rows marker, 4 count markers)")
    rows = SEP.join(render_row(r) for r in records)
    return template_text.replace(ROWS_MARK, rows).replace(COUNT_MARK, str(len(records)))

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--template", required=True); ap.add_argument("--data", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--exclusions", help="data/exclusions.json: excluded mentions are never shown")
    ap.add_argument("--flags", help="mentions_flags.json: records flagged as possible duplicates of a curated mention are HELD for manual review (not shown)")
    ap.add_argument("--decisions", help="data/review_decisions.json: the owner's answers (not_duplicate releases a held record, duplicate hides it)")
    ap.add_argument("--curated", help="data/mentions_curated.json: the build FAILS if any curated mention is missing from the page")
    ap.add_argument("--include-review", action="store_true", help="also show collector candidates with status 'review' (default: only 'relevant')")
    ap.add_argument("--report", help="write a JSON summary of what was shown and what was held back")
    ap.add_argument("--emit-records", help="write the records exactly as shown (tracker format), for tests")
    a = ap.parse_args(argv)
    public = json.loads(Path(a.data).read_text(encoding="utf-8"))
    if not isinstance(public, list) or not public: raise SystemExit("database must be a non-empty JSON list")
    exclusions = json.loads(Path(a.exclusions).read_text(encoding="utf-8")) if a.exclusions else None
    flags = json.loads(Path(a.flags).read_text(encoding="utf-8")) if a.flags and Path(a.flags).exists() else None
    if a.flags and flags is None: raise SystemExit(f"BUILD STOPPED: {a.flags} is missing, so records flagged for review cannot be held back")
    decisions = {}
    if a.decisions and Path(a.decisions).exists():
        raw = json.loads(Path(a.decisions).read_text(encoding="utf-8")); decisions = {d["id"]: d["decision"] for d in raw.get("decisions", [])}
        if any(v not in ("not_duplicate", "duplicate") for v in decisions.values()): raise SystemExit("BUILD STOPPED: invalid decision in " + a.decisions)
    records, report = library_records(public, exclusions, a.include_review, flags, decisions)
    if not records: raise SystemExit("nothing to show: refusing to build an empty page")
    if a.curated:
        curated = json.loads(Path(a.curated).read_text(encoding="utf-8")); shown = {r.get("id") for r in records}
        missing = [c["id"] for c in curated if c["id"] not in shown]
        if missing: raise SystemExit(f"BUILD STOPPED: {len(missing)} curated mention(s) are missing from the data, for example {missing[:3]}")
    out = build(Path(a.template).read_text(encoding="utf-8"), records)
    Path(a.out).parent.mkdir(parents=True, exist_ok=True); Path(a.out).write_text(out, encoding="utf-8")
    if a.report: Path(a.report).parent.mkdir(parents=True, exist_ok=True); Path(a.report).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    if a.emit_records: Path(a.emit_records).write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"built {a.out}: {len(records)} mentions ({report['curated_or_protected']} curated/protected, {report['collector_shown']} found by the collector; "
          f"held back: {report['review_held_back']} review, {report['excluded']} excluded, {report['duplicates_hidden']} duplicates, "
          f"{len(report['held_for_review'])} held for manual review)")

if __name__ == "__main__":
    main()
