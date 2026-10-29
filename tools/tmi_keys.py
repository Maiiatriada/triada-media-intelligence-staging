"""Shared identity helpers. article_id() is a copy of the collector's own function (collector.py), so ids stay compatible."""
import hashlib, re
from urllib.parse import urlsplit, parse_qsl

def normalize(text):                      # same as collector.normalize
    return " ".join(re.findall(r"\w+", str(text).casefold()))

def article_id(title, source):            # same as collector.article_id: sha256(normalised title | normalised source)
    return hashlib.sha256((normalize(title) + "|" + normalize(source)).encode()).hexdigest()

# Query keys that identify a post on a platform (everything else, e.g. tracking parameters, is dropped).
_KEEP = {"youtube.com": {"v", "list"}, "facebook.com": {"id", "story_fbid", "fbid", "v", "comment_id", "reply_comment_id", "multi_permalinks"}, "t.me": {"comment"}}

def canonical_url(url):
    """Platform-aware link identity: host without www/m, path without trailing slash, plus only the query keys that identify a post."""
    p = urlsplit(str(url).strip())
    host = re.sub(r"^(www\.|m\.|mobile\.)", "", (p.hostname or "").lower())
    keep = _KEEP.get(host, set())
    q = "&".join(f"{k}={v}" for k, v in sorted(parse_qsl(p.query)) if k in keep)
    return host + re.sub(r"/+$", "", p.path) + ("?" + q if q else "")

def title_key(title, source):
    """Title identity that ignores a trailing ' - <source>' suffix (Google News adds one)."""
    t = re.sub(r"\s*[-\u2013\u2014|]\s*" + re.escape(str(source).strip()) + r"\s*$", "", str(title).strip(), flags=re.I)
    return normalize(t) + "|" + normalize(source)


def _platform(r):
    return str(r.get("platform") or "Media").strip().casefold()          # a record without a platform is Media, as in the collector

def matching_exclusion(mention, exclusions):
    """The exclusion entry that matches a mention, or None. Match = canonical link, or collector id.
    A collector id is a fingerprint of the collector's exact title+source text, so a re-found record with identical text is caught.
    A differently worded title is never matched (two entries are duplicate copies of articles the library keeps under another
    link, and those kept records must not be excluded); such cases are handled by DuplicateIndex."""
    url = canonical_url(mention["url"]) if mention.get("url") else None
    mid, aid = mention.get("id"), article_id(mention.get("title", ""), mention.get("source", ""))
    for e in exclusions.get("entries", []):
        if (url and url in {canonical_url(u) for u in e["urls"]}) or (mid and mid in e["collector_ids"]) or aid in e["collector_ids"]:
            return e
    return None

def is_excluded(mention, exclusions):
    return matching_exclusion(mention, exclusions) is not None

class DuplicateIndex:
    """Finds the protected / curated record that a found mention duplicates.
    Same canonical link (any platform), or the same title + source ONLY when both are Media. Posts on social platforms are never
    merged on text alone: two different Telegram posts, or a Telegram post and a TikTok video, can carry identical text."""
    def __init__(self, records):
        self.by_url, self.by_title = {}, {}
        for r in records:
            if r.get("url"): self.by_url.setdefault(canonical_url(r["url"]), r)
            if _platform(r) == "media" and r.get("title"): self.by_title.setdefault(title_key(r["title"], r.get("source", "")), r)
    def find(self, mention):
        if mention.get("url"):
            hit = self.by_url.get(canonical_url(mention["url"]))
            if hit is not None: return hit
        if _platform(mention) == "media" and mention.get("title"):
            return self.by_title.get(title_key(mention["title"], mention.get("source", "")))
        return None

def known_duplicate(mention, curated):
    """The curated record a found mention duplicates, or None. `curated` may be a list or a DuplicateIndex."""
    return (curated if isinstance(curated, DuplicateIndex) else DuplicateIndex(curated)).find(mention)
