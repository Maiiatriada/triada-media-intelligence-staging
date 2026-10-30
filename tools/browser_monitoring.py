"""Import evidence captured from public posts by the authorized browser monitor."""
import datetime as dt
import json
import re
from pathlib import Path
from urllib.parse import urlsplit
from tools import monitoring as m


def post_key(url):
    """X identity survives username changes and twitter.com aliases."""
    if m.platform(url) == "X":
        hit = re.fullmatch(r"/(?:[^/]+/status|i/web/status)/(\d+)", urlsplit(url).path.rstrip('/'))
        return "x:" + hit[1] if hit else ""
    return m.identity(url)


def collect(confirmed, pending, classify, path=Path("data/browser_mentions.json"), known_records=()):
    report = dict(status="not_configured", imported=0, already_known=0, held_for_review=0, rejected=0)
    if not path.exists(): return report
    entries = json.loads(path.read_text())
    if not isinstance(entries, list): raise ValueError("browser_mentions must be a list")
    cfg = m.config()
    known = {post_key(r.get("url", "")) for r in [*confirmed.values(), *known_records] if r.get("url")}
    report["status"] = "ok"
    for raw in entries:
        url = raw.get("url", "")
        name = m.platform(url)
        author_url = raw.get("author_url", "")
        date = raw.get("published_at", "")
        text = raw.get("post_text", "")
        try: day = dt.date.fromisoformat(date[:10])
        except (ValueError, TypeError): day = None
        if (name not in {"X", "Facebook", "Instagram"} or not m.post_url(url)
            or not post_key(url) or not raw.get("source") or m.platform(author_url) != name
            or m.owned(url, cfg) or m.owned(author_url, cfg)):
            report["rejected"] += 1; continue
        # Never accept search snippets, translations or dates inferred from collection.
        evidence = raw.get("evidence", {})
        verified = (raw.get("verification_method") == "native_browser_post"
                    and evidence.get("post_url") == url
                    and evidence.get("author_url") == author_url
                    and evidence.get("publication_date") == date
                    and evidence.get("text_complete") is True
                    and evidence.get("original_language") is True
                    and evidence.get("public_post") is True
                    and evidence.get("observed_at")
                    and day and cfg["start_date"] <= date[:10] <= dt.datetime.now(dt.timezone.utc).date().isoformat())
        match = classify(text) if text else None
        key = m.identity(url)
        if post_key(url) in known:
            report["already_known"] += 1; continue
        record = {"id": key, "url": url, "source": raw["source"], "author_url": author_url,
                  "title": raw.get("title") or text[:220] or url, "platform": name,
                  "published_at": date if day else "", "origin": "collector", "identity_version": "url-v1",
                  "discovery_method": "authorized_browser_search", "discovered_at": raw.get("discovered_at", ""),
                  "verification_method": "native_browser_post" if verified and match else "unverified_browser_candidate",
                  "evidence": evidence, "post_text": text,
                  "status": "relevant" if verified and match else "review"}
        if verified and match:
            record.update(match, date_source="native_browser_publication")
            confirmed[key] = record; pending.pop(key, None)
            known.add(post_key(url)); report["imported"] += 1
        else:
            record["review_reason"] = "native_post_evidence_or_relevance_required"
            pending[key] = record; report["held_for_review"] += 1
    return report
