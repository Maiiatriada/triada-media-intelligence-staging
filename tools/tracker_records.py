"""Turns the records in mentions_public.json into the rows the tracker page shows. Standard library only.

Curated records already carry a `tracker` block and are used exactly as they are. Records the collector found are converted here:
no information is invented (the date label, timestamp and Company / CEO flags are computed from published_at and categories).

Which collector records appear in the Mentions Library and the Overview (the same rows feed both):
  * status "relevant" (the collector verified the mention itself); "review" candidates stay out unless include_review is set;
  * not excluded (data/exclusions.json), and not a duplicate of a curated record (same link, or the same article between Media records);
  * hand-added records the collector carried forward are shown (they are protected records), whatever their status;
  * a record the collector flagged as a POSSIBLE DUPLICATE of a curated mention is HELD for manual review: it is not shown (so it is in
    neither the Library nor the Overview) until the owner answers "not_duplicate" in data/review_decisions.json. "duplicate" keeps it hidden.
    Curated mentions are never held.
"""
import datetime as dt

try:
    from tmi_keys import DuplicateIndex, is_excluded
except ImportError:                                  # imported as tools.tracker_records
    from tools.tmi_keys import DuplicateIndex, is_excluded

MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sept", "Oct", "Nov", "Dec"]     # "Sept", as the Library already shows it
KNOWN_PLATFORMS = {"Media", "Facebook", "Instagram", "X", "LinkedIn", "YouTube", "Telegram", "Threads", "TikTok", "Forum", "Bluesky", "Reddit"}
REQUIRED = ("title", "source", "url", "published_at")


def parse_published(value):
    d = dt.datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    return d.replace(tzinfo=dt.timezone.utc) if d.tzinfo is None else d.astimezone(dt.timezone.utc)


def to_tracker_record(record, import_source="collector"):
    """A copy of the record with the `tracker` block the page needs. A record that already has one is returned unchanged."""
    if isinstance(record.get("tracker"), dict):
        return record
    d = parse_published(record["published_at"])
    cats = record.get("categories") or []
    out = dict(record)
    out.setdefault("platform", "Media")
    out["tracker"] = {"ts": int(d.timestamp() * 1000), "day": d.strftime("%Y-%m-%d"), "date_label": f"{d.day:02d} {MONTHS[d.month - 1]} {d.year}",
                      "company": "Company" in cats, "ceo": "CEO" in cats, "import_source": import_source}
    return out


def _protected(record):
    return isinstance(record.get("tracker"), dict) or record.get("origin") == "manual"


def held_ids(flags):
    """{record id: flag} for the records of every possible_duplicate flag that the collector found (curated records are never held)."""
    held = {}
    for flag in (flags or {}).get("flags", []):
        if flag.get("type") == "possible_duplicate":
            for r in flag.get("records", []):
                if not r.get("curated"):
                    held[r.get("id")] = flag
    return held


def library_records(public, exclusions=None, include_review=False, flags=None, decisions=None):
    """(records for the page, report). Input order is kept (the collector already sorts the public file newest first)."""
    report = {"input": len(public), "curated_or_protected": 0, "collector_shown": 0, "review_held_back": 0, "excluded": 0,
              "duplicates_hidden": 0, "held_for_review": [], "confirmed_duplicates_hidden": 0, "skipped_invalid": [], "unknown_platform": []}
    held, decisions = held_ids(flags), decisions or {}
    protected = [r for r in public if isinstance(r, dict) and _protected(r)]
    index = DuplicateIndex(protected)
    out = []
    for record in public:
        if not isinstance(record, dict) or any(not str(record.get(k) or "").strip() for k in REQUIRED):
            report["skipped_invalid"].append(str((record or {}).get("id") if isinstance(record, dict) else record)[:60]); continue
        if _protected(record):
            report["curated_or_protected"] += 1
            try:
                out.append(to_tracker_record(record, "carried"))
            except ValueError:
                report["skipped_invalid"].append(str(record.get("id"))); report["curated_or_protected"] -= 1
            continue
        if exclusions is not None and is_excluded(record, exclusions):
            report["excluded"] += 1; continue
        if str(record.get("status") or "").lower() != "relevant" and not include_review:
            report["review_held_back"] += 1; continue
        hit = index.find(record)
        if hit is not None and hit.get("id") != record.get("id"):
            report["duplicates_hidden"] += 1; continue
        decision = decisions.get(record.get("id"))
        if decision == "duplicate":
            report["confirmed_duplicates_hidden"] += 1; continue
        if record.get("id") in held and decision != "not_duplicate":
            twins = [r.get("title") for r in held[record["id"]].get("records", []) if r.get("curated")]
            report["held_for_review"].append({"id": record.get("id"), "title": record.get("title"), "source": record.get("source"),
                                              "published_at": record.get("published_at"), "possible_duplicate_of": twins})
            continue
        try:
            out.append(to_tracker_record(record))
        except ValueError:
            report["skipped_invalid"].append(str(record.get("id"))); continue
        report["collector_shown"] += 1
    report["unknown_platform"] = sorted({r["platform"] for r in out if r["platform"] not in KNOWN_PLATFORMS})
    report["output"] = len(out)
    return out, report
