"""Free, bounded public discovery. Search snippets are candidates, never proof.

Only native structured post metadata (or YouTube API metadata) with an actual
publication date can confirm a social mention. No login scraping or inferred dates.
"""
import datetime as dt
import hashlib
import html
import json
import os
import re
import time
import urllib.parse as up
import urllib.request as ur
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from email.utils import parsedate_to_datetime
from pathlib import Path
from tools.tmi_keys import canonical_url

PLATFORMS = {"facebook.com": "Facebook", "instagram.com": "Instagram", "x.com": "X", "twitter.com": "X", "youtube.com": "YouTube", "youtu.be": "YouTube", "linkedin.com": "LinkedIn", "t.me": "Telegram", "tiktok.com": "TikTok", "threads.net": "Threads", "threads.com": "Threads"}
PUBLICATION_DATES = {}

def remember_date(page, url):
    class Dates(HTMLParser):
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "meta" and (a.get("property") or a.get("name")) in ("article:published_time", "datePublished", "date"):
                date = published(a.get("content"))
                if date: PUBLICATION_DATES[url] = date
    Dates().feed(page)
    if url not in PUBLICATION_DATES:
        for block in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.S | re.I):
            try: data = json.loads(block)
            except ValueError: continue
            for n in nodes(data):
                kind = n.get("@type", [])
                if isinstance(kind, str): kind = [kind]
                if not set(kind) & {"NewsArticle", "Article", "BlogPosting"}: continue
                target = n.get("url") or n.get("mainEntityOfPage")
                if isinstance(target, dict): target = target.get("@id") or target.get("url")
                if not isinstance(target, str) or identity(target) != identity(url): continue
                date = published(n.get("datePublished"))
                if date: PUBLICATION_DATES[url] = date

def host(url):
    return (up.urlsplit(url).hostname or "").lower()

def under(hostname, domain):
    return hostname == domain or hostname.endswith("." + domain)

def platform(url):
    return next((p for d, p in PLATFORMS.items() if under(host(url), d)), "Media")

def identity(url):
    key = canonical_url(url)
    key = re.sub(r"^twitter\.com/", "x.com/", key)
    return hashlib.sha256(key.encode()).hexdigest()

def config():
    p = Path("data/monitoring.json")
    return json.loads(p.read_text()) if p.exists() else None

def owned(url, cfg):
    if any(under(host(url), d) for d in cfg["owned_domains"]):
        return True
    key = canonical_url(url).casefold().replace("twitter.com/", "x.com/")
    for profile in cfg["owned_profiles"]:
        owner = canonical_url(profile).casefold()
        if "facebook.com/profile.php?id=" in owner:
            pid = up.parse_qs(up.urlsplit(profile).query)["id"][0]
            if under(host(url), "facebook.com") and (up.parse_qs(up.urlsplit(url).query).get("id") == [pid] or up.urlsplit(url).path.startswith("/" + pid + "/")):
                return True
        elif key == owner or key.startswith(owner + "/"):
            return True
    return False

def post_url(url):
    p = up.urlsplit(url); path = p.path; name = platform(url)
    if name == "X": return bool(re.search(r"/status/\d+", path))
    if name == "Instagram": return bool(re.match(r"/(p|reel|tv)/[^/]+", path))
    if name == "Facebook": return bool(re.search(r"/(posts|videos|reel)/", path) or p.path in ("/story.php", "/permalink.php", "/photo.php", "/watch/") and p.query)
    if name == "YouTube": return bool(up.parse_qs(p.query).get("v") or re.match(r"/(shorts|live)/", path) or under(host(url), "youtu.be") and path.strip("/"))
    if name == "LinkedIn": return "/posts/" in path or "/feed/update/" in path
    if name == "Telegram": return bool(re.match(r"/(?:s/)?[^/]+/\d+", path))
    return False

def fetch(url):
    req = ur.Request(url, headers={"User-Agent": "Mozilla/5.0 (compatible; TRIADA-Monitor/2.0)"})
    with ur.urlopen(req, timeout=15) as response:
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000: raise ValueError("response_too_large")
        return raw.decode(response.headers.get_content_charset() or "utf-8", errors="replace"), response.geturl()

def published(value):
    try:
        value = str(value or "")
        if not re.match(r"^\d{4}-\d{2}-\d{2}", value): return None
        d = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        if d.tzinfo is None: d = d.replace(tzinfo=dt.timezone.utc)
        if d > dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=1): return None
        return d.isoformat()
    except ValueError:
        return None

def nodes(value):
    if isinstance(value, list):
        for item in value: yield from nodes(item)
    elif isinstance(value, dict):
        yield value
        yield from nodes(value.get("@graph", []))

def native_metadata(page, url, classify):
    # Deliberately reject generic page text and OpenGraph previews: they can describe
    # recommended posts, a login wall, or an account rather than this specific post.
    for block in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', page, re.S | re.I):
        try: data = json.loads(block)
        except (ValueError, TypeError): continue
        for n in nodes(data):
            kind = n.get("@type", [])
            if isinstance(kind, str): kind = [kind]
            if not set(kind) & {"SocialMediaPosting", "VideoObject"}: continue
            target = n.get("url") or n.get("mainEntityOfPage")
            if isinstance(target, dict): target = target.get("@id") or target.get("url")
            if not isinstance(target, str) or identity(target) != identity(url): continue
            text = " ".join(str(n.get(k) or "") for k in ("headline", "name", "articleBody", "description"))
            match = classify(html.unescape(text)); date = published(n.get("datePublished") or n.get("uploadDate"))
            author = n.get("author") or n.get("creator") or {}
            if isinstance(author, list): author = author[0] if author else {}
            if not isinstance(author, dict): continue
            author_url = author.get("url") or author.get("@id")
            if not match or not date or not author.get("name") or not isinstance(author_url, str) or platform(author_url) != platform(url): continue
            return {"title": html.unescape(str(n.get("headline") or n.get("name") or text[:200])), "source": str(author["name"]), "author_url": author_url, "published_at": date, **match}
    return None

def error_name(exc):
    # Never publish raw request URLs: an API key can be embedded in one.
    return "HTTP " + str(exc.code) if hasattr(exc, "code") else type(exc).__name__

def author_articles(cfg):
    """The author's RSS can be empty even while the profile lists articles."""
    page, final = fetch(cfg["author_page"])
    if identity(final) != identity(cfg["author_page"]): raise ValueError("unexpected_author_page")
    class Links(HTMLParser):
        def __init__(self): super().__init__(); self.urls = []
        def handle_starttag(self, tag, attrs):
            a = dict(attrs)
            if tag == "a" and "news_item-link" in a.get("class", "").split():
                url = up.urljoin(final, a.get("href", ""))
                if under(host(url), "militarnyi.com") and "/blogs/" in up.urlsplit(url).path:
                    self.urls.append(url)
    links = Links(); links.feed(page)
    records, errors = [], 0
    for url in list(dict.fromkeys(links.urls))[:20]:
        try:
            article, final_url = fetch(url)
            if identity(final_url) != identity(url): continue
            for block in re.findall(r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', article, re.S | re.I):
                try: data = json.loads(block)
                except ValueError: continue
                for n in nodes(data):
                    kind = n.get("@type", [])
                    if isinstance(kind, str): kind = [kind]
                    if not set(kind) & {"BlogPosting", "NewsArticle", "Article"}: continue
                    author = n.get("author") or {}; target = n.get("mainEntityOfPage") or n.get("url")
                    if isinstance(target, dict): target = target.get("@id") or target.get("url")
                    if not isinstance(author, dict) or not isinstance(target, str) or identity(target) != identity(url): continue
                    author_url = author.get("@id") or author.get("url") or ""
                    date = published(n.get("datePublished")); title = n.get("headline") or n.get("name")
                    if identity(author_url) != identity(cfg["author_page"]) or not date or not title: continue
                    if date[:10] < cfg["author_start_date"]: continue
                    records.append({"url": url, "title": html.unescape(title), "source": "Militarnyi", "platform": "Media", "published_at": date, "status": "relevant", "date_source": "publisher_metadata", "verification_method": "verified_author_page", "categories": ["CEO"], "products": [], "matched_text": "Author: Artem Veselov", "content_type": "authored_article"})
        except Exception:
            errors += 1
    return records, len(links.urls), errors

def enforce_scope(confirmed, pending, protected):
    """Legacy media discovery must not bypass native social verification or ownership rules."""
    cfg = config()
    if not cfg: return
    for bucket in (confirmed, pending):
        for key, record in list(bucket.items()):
            if protected(record): continue
            if owned(record.get("url", ""), cfg) or owned(record.get("author_url", ""), cfg):
                del bucket[key]; continue
            name = platform(record.get("url", ""))
            if name != "Media" and record.get("verification_method") not in ("native_structured_post", "youtube_api_title_description"):
                record.update(platform=name, origin="collector", status="review", review_reason="native_content_author_and_publication_date_required")
                if bucket is confirmed:
                    del confirmed[key]; pending[key] = record

def collect(confirmed, pending, classify, search):
    cfg = config()
    if not cfg: return {"status": "not_configured"}
    report = {"scope": "public indexed posts only; no private posts or complete social coverage", "platforms": {}, "author_feed": {}, "youtube_api": {"status": "not_configured"}}
    known = {identity(r["url"]) for r in confirmed.values() if r.get("url")}
    counts = {}

    def save(record):
        if owned(record["url"], cfg) or owned(record.get("author_url", ""), cfg): return "owned_skipped"
        key = identity(record["url"])
        if key in known: return "already_known"
        record.update(id=key, origin="collector", identity_version="url-v1", discovered_at=dt.datetime.now(dt.timezone.utc).isoformat())
        if record["status"] == "relevant":
            if record["published_at"][:10] < cfg["start_date"]: return "outside_period"
            confirmed[key] = record; pending.pop(key, None); known.add(key)
            return "confirmed"
        pending[key] = record
        return "held_for_review"

    seen = set()
    for domain in cfg["social_domains"]:
        name = PLATFORMS[domain]
        info = report["platforms"].setdefault(name, {"requests": 0, "errors": 0, "candidates": 0, "confirmed": 0, "held_for_review": 0, "owned_skipped": 0, "verification_attempts": 0, "verification_errors": 0, "limit_reached": False, "queries": []})
        for query in cfg["queries"]:
            info["requests"] += 1
            audit = {"query": query + " site:" + domain, "results": 0, "matching_post_links": 0, "out_of_scope_results": 0}
            info["queries"].append(audit)
            try:
                items = search(audit["query"])
                audit["results"] = len(items)
                audit["status"] = "ok"
            except Exception as exc:
                info["errors"] += 1; info["last_error"] = error_name(exc); audit["status"] = "error"; continue
            for item in items:
                url = (item.findtext("link") or "").strip()
                if not under(host(url), domain):
                    audit["out_of_scope_results"] += 1
                    continue
                if not post_url(url): continue
                audit["matching_post_links"] += 1
                key = identity(url)
                if key in seen or key in known: continue
                seen.add(key)
                if owned(url, cfg): info["owned_skipped"] += 1; continue
                if counts.get(name, 0) >= cfg["max_candidates_per_platform"]:
                    info["limit_reached"] = True; continue
                counts[name] = counts.get(name, 0) + 1; info["candidates"] += 1
                record = {"url": url, "platform": name, "title": item.findtext("title") or url, "source": name, "published_at": "", "status": "review", "review_reason": "native_content_author_and_publication_date_required", "discovery_method": "bing_site_search", "matched_queries": [query], "verification_method": "unverified_search_candidate"}
                info["verification_attempts"] += 1
                try:
                    page, final = fetch(url)
                    evidence = native_metadata(page, url, classify) if identity(final) == key else None
                    if evidence:
                        record.update(evidence, status="relevant", date_source="native_publication", verification_method="native_structured_post")
                        record.pop("review_reason", None)
                except Exception as exc:
                    info["verification_errors"] += 1; record["verification_error"] = error_name(exc)
                result = save(record); info[result] = info.get(result, 0) + 1
            time.sleep(0.3)
        info["query_scope_violations"] = sum(q["out_of_scope_results"] for q in info["queries"])
        info["status"] = "error" if info["errors"] == info["requests"] else "partial" if info["errors"] or info["verification_errors"] or info["limit_reached"] or info["query_scope_violations"] else "ok"

    info = report["author_feed"]
    try:
        page, final = fetch(cfg["author_feed"])
        if not under(host(final), "militarnyi.com"): raise ValueError("unexpected_feed_host")
        root = ET.fromstring(page); info.update(status="ok", confirmed=0, items=0)
        for item in root.findall("./channel/item"):
            info["items"] += 1
            name = item.findtext("{http://purl.org/dc/elements/1.1/}creator") or ""
            if name.casefold() not in [s.casefold() for s in cfg["author_names"]]: continue
            date = parsedate_to_datetime(item.findtext("pubDate") or "").isoformat()
            if not published(date) or date[:10] < cfg["author_start_date"]: continue
            url = item.findtext("link") or ""; title = item.findtext("title") or ""
            if not under(host(url), "militarnyi.com") or not title: continue
            record = {"url": url, "title": title, "source": "Militarnyi", "platform": "Media", "published_at": date, "status": "relevant", "date_source": "publisher_feed", "verification_method": "verified_author_feed", "categories": ["CEO"], "products": [], "matched_text": "Author: " + name, "content_type": "authored_article"}
            result = save(record); info[result] = info.get(result, 0) + 1
    except Exception as exc: info.update(status="error", last_error=error_name(exc))

    if cfg.get("author_page"):
        info = report["author_page"] = {"status": "ok", "confirmed": 0}
        try:
            records, count, errors = author_articles(cfg)
            info.update(links=count, errors=errors)
            if not count or errors or count > 20: info["status"] = "partial"
            for record in records:
                result = save(record); info[result] = info.get(result, 0) + 1
        except Exception as exc: info.update(status="error", last_error=error_name(exc))

    api_key = os.environ.get("YOUTUBE_API_KEY", "").strip()
    if api_key:
        info = report["youtube_api"]; info.update(status="ok", requests=0, errors=0, confirmed=0)
        for query in cfg["queries"]:
            token = ""
            for _ in range(cfg["youtube_max_pages_per_query"]):
                params = {"part": "snippet", "type": "video", "maxResults": 50, "order": "date", "q": query, "publishedAfter": cfg["start_date"] + "T00:00:00Z", "key": api_key}
                if token: params["pageToken"] = token
                info["requests"] += 1
                try:
                    body, _ = fetch("https://www.googleapis.com/youtube/v3/search?" + up.urlencode(params)); response = json.loads(body)
                    ids = [x.get("id", {}).get("videoId") for x in response.get("items", [])]
                    ids = [x for x in ids if x]
                    # videos.list supplies the full public description instead of search's truncated snippet.
                    if ids:
                        body, _ = fetch("https://www.googleapis.com/youtube/v3/videos?" + up.urlencode({"part": "snippet", "id": ",".join(ids), "key": api_key}))
                        for item in json.loads(body).get("items", []):
                            s = item["snippet"]; match = classify(html.unescape(s["title"] + " " + s.get("description", ""))); date = published(s.get("publishedAt"))
                            if not match or not date: continue
                            record = {"url": "https://www.youtube.com/watch?v=" + item["id"], "title": html.unescape(s["title"]), "source": s["channelTitle"], "author_url": "https://www.youtube.com/channel/" + s["channelId"], "platform": "YouTube", "published_at": date, "status": "relevant", "date_source": "youtube_api", "verification_method": "youtube_api_title_description", **match}
                            result = save(record); info[result] = info.get(result, 0) + 1
                    token = response.get("nextPageToken", "")
                    if not token: break
                except Exception as exc:
                    info["errors"] += 1; info["last_error"] = error_name(exc); break
            if token: info["limit_reached"] = True
        if info["errors"]: info["status"] = "error" if info["errors"] == info["requests"] else "partial"
        elif info.get("limit_reached"): info["status"] = "partial"
    return report
