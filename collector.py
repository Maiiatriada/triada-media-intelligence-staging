"""TRIADA Media Intelligence — media monitoring."""

import json
import os
import re
import time
import copy
import functools
import hashlib
import html
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

from pathlib import Path
from datetime import timezone
from datetime import datetime as _datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser

DATABASE = Path("mentions.json")
REVIEW = Path("mentions_review.json")
PUBLIC = Path("mentions_public.json")
STATUS_FILE = Path("collector_status.json")
START_DATE = "2026-06-01"

QUERIES = [
    '"Triada Robotics"',
    '"TriadaRobotics"',
    '"Тріада Роботікс"',
    '"Триада Роботикс"',
    '"Artem Veselov"',
    '"Artem Veselow"',
    '"Артем Веселов"',
    '"Артема Веселова"',
    '"Artem Veselov" "drone"',
    '"Артемом Веселовим"',
    '"Веселов" "Triada"',
    '"Veselov" "Triada Robotics"',
    '"TRIADA ROBOTICS" BRAVO',
    '"TRIADA ROBOTICS" KILO',
    '"Triada" "BRAVO"',
    '"Тріада" "Браво"',
    '"Триада" "Браво"',
    '"Triada" "KILO"',
    '"Тріада" "Кіло"',
    '"BRAVO" "Triada Robotics"',
    '"KILO" "Triada Robotics"',
]

# Publisher feeds supply direct article links that Google News RSS does not.
# Inaccessible feeds are skipped without interrupting other discovery.
PUBLISHER_FEEDS = [
    ("Мілітарний", "https://militarnyi.com/uk/feed/"),
    ("Militarnyi", "https://militarnyi.com/en/feed/"),
    ("Defender Media", "https://thedefender.media/feed/"),
    ("dev.ua", "https://dev.ua/rss"),
    ("ITC.ua", "https://itc.ua/feed/"),
    ("Euromaidan Press", "https://euromaidanpress.com/feed/"),
    ("РБК-Україна", "https://www.rbc.ua/ukr/rss"),
    ("UNITED24 Media", "https://united24media.com/rss"),
]

REGIONS = [
    ("en", "US", "US:en"),
    ("uk", "UA", "UA:uk"),
]

COMPANY = re.compile(
    r"\btriada[\s-]*robotics\b|"
    r"\bтріада[\s-]*робот[іи]кс\b|"
    r"\bтриада[\s-]*робот[иі]кс\b",
    re.I
)

CEO = re.compile(
    r"\bartem\s+vesel(?:ov|ow)\b|"
    r"\bартем(?:а|ом|у|ові)?\s+веселов(?:а|им|у|і|им|им)?\b",
    re.I
)

PRODUCTS = {
    "BRAVO": re.compile(r"\bbravo\b|\bбраво\b", re.I),
    "KILO": re.compile(r"\bkilo\b|\bкіло\b", re.I),
}


# ---------------------------------------------------------------------------
# Connector diagnostics
#
# Observe-only bookkeeping for collector_status.json. It never changes what is
# collected or saved: every Diagnostics method swallows its own errors, so a
# bug in this block cannot stop a run.
#
# Status "ok" means the requests completed without a technical error. It does
# NOT guarantee complete monitoring: a source can answer successfully and still
# miss articles. Error text is cleaned before it is stored, because this
# repository is public and the Google Alerts feed addresses are secret.
# ---------------------------------------------------------------------------

CONNECTORS = (
    ("google_news", "Google News"),
    ("bing", "Bing"),
    ("gdelt", "GDELT"),
    ("publisher_rss", "Publisher RSS"),
    ("google_alerts", "Google Alerts"),
)

STATUS_LEGEND = {
    "ok": (
        "Every request completed without a technical error. "
        "This does not guarantee complete monitoring coverage."
    ),
    "partial": "Some requests completed and some failed.",
    "error": "Every request failed.",
    "not_configured": "The connector needs configuration that is missing.",
    "not_run": "The connector made no requests in this run.",
}

ALERTS_ADDRESS_IGNORED = (
    "A configured feed address was ignored: it is not a Google Alerts feed URL."
)

_URL_PATTERN = re.compile(r"(?:https?://|www\.)\S+", re.I)
_ALERTS_PATH_PATTERN = re.compile(r"/?alerts/feeds/\S*", re.I)


def redact(text):
    """Remove URLs and anything that came from the Google Alerts secret."""
    text = str(text)

    secrets = set()
    for line in os.environ.get("GOOGLE_ALERTS_RSS", "").splitlines():
        line = line.strip()
        if not line:
            continue
        secrets.add(line)
        secrets.add(urllib.parse.quote(line, safe=""))
        secrets.add(urllib.parse.quote(line))
        parsed = urllib.parse.urlparse(line)
        for part in (parsed.netloc + parsed.path, parsed.path, parsed.query):
            if len(part) >= 8:
                secrets.add(part)
        for segment in parsed.path.split("/") + parsed.query.replace("&", "/").split("/"):
            if len(segment) >= 8 and segment not in ("alerts", "feeds"):
                secrets.add(segment)
    for secret in sorted(secrets, key=len, reverse=True):
        text = text.replace(secret, "<redacted>")

    text = _URL_PATTERN.sub("<url>", text)
    return _ALERTS_PATH_PATTERN.sub("<redacted>", text)


def safe_error(error):
    """Short, single-line, secret-free description of an error."""
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    else:
        text = str(error)
    return " ".join(redact(text).split())[:160]


def _safe_len(collection):
    try:
        return len(collection)
    except TypeError:
        return 0


def _now_iso():
    return _datetime.now(timezone.utc).isoformat()


def _text_or_none(value):
    return value if isinstance(value, str) else None


def _count_or_zero(value):
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return 0


def github_run_url():
    server = os.environ.get("GITHUB_SERVER_URL", "").strip()
    repository = os.environ.get("GITHUB_REPOSITORY", "").strip()
    run_id = os.environ.get("GITHUB_RUN_ID", "").strip()
    if server and repository and run_id:
        return f"{server}/{repository}/actions/runs/{run_id}"
    return None


def _never_raise(method):
    @functools.wraps(method)
    def wrapper(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except Exception:
            return None
    return wrapper


def _connector_status(entry):
    if not entry["configured"]:
        return "not_configured"
    if entry["requests"] == 0:
        return "not_run"
    if entry["failed_requests"] == 0:
        return "ok"
    if entry["failed_requests"] >= entry["requests"]:
        return "error"
    return "partial"


class Diagnostics:
    """Per-connector counters for one collector run."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.data = {key: self._blank() for key, _ in CONNECTORS}
        self.feeds = {}  # publisher feed name -> per-feed detail
        self.verification = {key: 0 for key in (
            "candidates_checked", "publisher_opened", "publisher_unavailable",
            "mention_absent", "confirmed", "already_known",
        )}
        self.started_utc = _now_iso()
        self._started = time.monotonic()

    @staticmethod
    def _blank():
        return {
            "configured": True,
            "requests": 0,
            "failed_requests": 0,
            "items_fetched": 0,
            "new_confirmed": 0,
            "last_error": None,
            "last_attempt_utc": None,
        }

    @_never_raise
    def verification_event(self, event):
        self.verification[event] += 1

    @_never_raise
    def start(self):
        self.reset()

    @_never_raise
    def attempt(self, connector):
        entry = self.data[connector]
        entry["requests"] += 1
        entry["last_attempt_utc"] = _now_iso()

    @_never_raise
    def items(self, connector, collection):
        self.data[connector]["items_fetched"] += _safe_len(collection)

    @_never_raise
    def confirmed(self, connector, count=1):
        self.data[connector]["new_confirmed"] += count

    @_never_raise
    def failure(self, connector, error):
        entry = self.data[connector]
        entry["failed_requests"] += 1
        entry["last_error"] = safe_error(error)

    @_never_raise
    def not_configured(self, connector):
        self.data[connector]["configured"] = False

    # Publisher RSS: one request per feed, with per-feed detail.
    def _feed(self, name):
        return self.feeds.setdefault(name, {
            "name": name,
            "status": "ok",
            "items_fetched": 0,
            "new_confirmed": 0,
            "last_error": None,
        })

    @_never_raise
    def feed_attempt(self, name):
        self._feed(name)
        self.attempt("publisher_rss")

    @_never_raise
    def feed_items(self, name, collection):
        self._feed(name)["items_fetched"] += _safe_len(collection)
        self.items("publisher_rss", collection)

    @_never_raise
    def feed_confirmed(self, name, count):
        self._feed(name)["new_confirmed"] += count
        self.confirmed("publisher_rss", count)

    @_never_raise
    def feed_failure(self, name, error):
        feed = self._feed(name)
        feed["status"] = "error"
        feed["last_error"] = safe_error(error)
        self.failure("publisher_rss", error)

    @_never_raise
    def report(self, previous):
        """New status keys. `previous` is the connectors section of the last run."""
        finished = _now_iso()
        connectors = {}
        for key, label in CONNECTORS:
            entry = self.data[key]
            old = previous.get(key) if isinstance(previous.get(key), dict) else {}
            status = _connector_status(entry)

            if entry["requests"] > 0:
                last_attempt = entry["last_attempt_utc"]
            else:
                last_attempt = _text_or_none(old.get("last_attempt_utc"))

            # A success means at least one request completed technically in this run.
            if status in ("ok", "partial"):
                last_success = entry["last_attempt_utc"]
            else:
                last_success = _text_or_none(old.get("last_success_utc"))

            streak = _count_or_zero(old.get("consecutive_failures"))
            if status == "error":
                streak += 1
            elif status != "not_run":
                streak = 0

            connector = {
                "label": label,
                "status": status,
                "last_attempt_utc": last_attempt,
                "last_success_utc": last_success,
                "consecutive_failures": streak,
                "requests": entry["requests"],
                "failed_requests": entry["failed_requests"],
                "items_fetched": entry["items_fetched"],
                "new_confirmed": entry["new_confirmed"],
                "last_error": entry["last_error"],
            }
            if key == "publisher_rss":
                connector["feeds"] = [dict(feed) for feed in self.feeds.values()]
            connectors[key] = connector

        return {
            "schema_version": 2,
            "status_legend": STATUS_LEGEND,
            "run": {
                "started_utc": self.started_utc,
                "finished_utc": finished,
                "duration_seconds": round(time.monotonic() - self._started, 1),
                "github_run_url": github_run_url(),
            },
            "connectors": connectors,
            "google_news_verification": dict(self.verification),
        }


DIAG = Diagnostics()


def load_previous_connectors():
    """Connector section of the previous status file, or {} if unavailable."""
    try:
        data = json.loads(STATUS_FILE.read_text(encoding="utf-8"))
        connectors = data.get("connectors")
        return connectors if isinstance(connectors, dict) else {}
    except Exception:
        return {}


def build_diagnostics_report():
    """New status keys, or {} if they could not be built (old keys still get written)."""
    try:
        return DIAG.report(load_previous_connectors()) or {}
    except Exception:
        return {}


def normalize(text):
    return " ".join(re.findall(r"\w+", str(text).casefold()))


def article_id(title, source):
    value = normalize(title) + "|" + normalize(source)
    return hashlib.sha256(value.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Platform handling, hand-added records and record identity
#
# Every mention has a `platform`. Mentions found by this collector are Media.
#
# Hand-added records are READ-ONLY for the collector. A record counts as
# hand-added when it has any of these markers (all already used in the data):
#   - "origin": "manual"                      (the explicit marker, recommended)
#   - a platform other than Media             (Facebook, Instagram, X, LinkedIn, YouTube, ...)
#   - a hand-written id                       (anything that is not an automatic 64-character hash)
#   - verification_method "manual_*" or "publisher_or_notion_source"
#   - keyword "manual_*"
# A record in a source file without any id can only have been written by a
# person, so it is marked "origin": "manual" when it is first seen.
#
# The media pipeline never verifies, promotes, updates, replaces or reclassifies
# such a record, and as a final safety net the collector restores every
# hand-added record to exactly what was loaded, in the file it came from,
# before anything is saved.
# ---------------------------------------------------------------------------

DEFAULT_PLATFORM = "Media"
AUTOMATIC_ID = re.compile(r"[0-9a-f]{64}")


def has_explicit_platform(record):
    value = record.get("platform")
    return not (value is None or (isinstance(value, str) and not value.strip()))


def has_social_platform(record):
    """An explicit platform other than Media."""
    return (isinstance(record, dict) and has_explicit_platform(record)
            and str(record["platform"]).strip().casefold() != DEFAULT_PLATFORM.casefold())


def has_no_id(record):
    current = record.get("id")
    return current is None or (isinstance(current, str) and not current.strip())


def has_hand_written_id(record):
    current = record.get("id")
    return (isinstance(current, str) and bool(current.strip())
            and not AUTOMATIC_ID.fullmatch(current))


def is_protected(record):
    """True for a hand-added (or hand-verified) record, which the collector never changes."""
    if not isinstance(record, dict):
        return False
    if record.get("origin") == "manual" or has_social_platform(record) or has_hand_written_id(record):
        return True
    method = str(record.get("verification_method") or "")
    if method.startswith("manual") or method == "publisher_or_notion_source":
        return True
    return str(record.get("keyword") or "").startswith("manual")


def ensure_platform(record):
    """Records without a platform are Media. An existing value is never touched."""
    if not has_explicit_platform(record):
        record["platform"] = DEFAULT_PLATFORM
    return record


def record_key(record):
    """Identity of a record. Normal records: title + source, exactly as before.
    A hand-made record without a title or source keeps its own id (or url)."""
    if "title" in record and "source" in record:
        return article_id(record["title"], record["source"])
    fallback = record.get("id") or hashlib.sha256(
        (record.get("url") or json.dumps(record, sort_keys=True)).encode()
    ).hexdigest()
    return str(fallback)


def rebuild_id(record, key):
    """Automatic ids are rebuilt from the key, as before. A hand-written id such
    as "manual-1-20260813" is kept exactly as written."""
    if has_no_id(record) or AUTOMATIC_ID.fullmatch(str(record.get("id"))):
        record["id"] = key
    return record


def adopt_record(record):
    """Called when a record is loaded from a source file: mark id-less records as
    hand-added (only a person writes a record without an id)."""
    if has_no_id(record):
        record.setdefault("origin", "manual")
    return record


def merge_duplicate(kept, other, prefer_manual=True):
    """Two records with the same identity in the same file. A hand-added record wins
    over an automatic one; between two of the same kind the first stays. What the
    winner lacks is filled in from the other, and an explicit social platform is
    never lost."""
    if prefer_manual and is_protected(other) and not is_protected(kept):
        winner, loser = dict(other), kept
    else:
        winner, loser = kept, other
    for field, value in loser.items():
        if field == "platform":
            continue
        if field not in winner or winner[field] in (None, "", [], {}):
            winner[field] = value
    if has_social_platform(loser) and not has_social_platform(winner):
        winner["platform"] = loser["platform"]
    if winner is not kept:
        kept.clear()
        kept.update(winner)
    return kept


def snapshot_manual(confirmed, pending):
    """Every hand-added record exactly as loaded, plus which keys each file held at load time."""
    return {
        "records": [(name, key, copy.deepcopy(record))
                    for name, bucket in (("confirmed", confirmed), ("pending", pending))
                    for key, record in bucket.items() if is_protected(record)],
        "loaded": {"confirmed": set(confirmed), "pending": set(pending)},
    }


def restore_manual(confirmed, pending, snapshot):
    """Safety net. Whatever happened during the run, every hand-added record leaves
    exactly as it came in, in the file it came from. Returns what had to be restored."""
    buckets = {"confirmed": confirmed, "pending": pending}
    restored = []
    for name, key, original in snapshot["records"]:
        if buckets[name].get(key) != original:
            buckets[name][key] = copy.deepcopy(original)
            restored.append(original.get("title") or key)
        # A copy that crossed over to the other file during the run is removed;
        # a record that was already in the other file when loaded is left alone.
        other = "pending" if name == "confirmed" else "confirmed"
        if key in buckets[other] and key not in snapshot["loaded"][other]:
            del buckets[other][key]
    return restored


def classify(text):
    company = bool(COMPANY.search(text))
    ceo = bool(CEO.search(text))

    if not (company or ceo):
        return None

    categories = []

    if company:
        categories.append("Company")

    if ceo:
        categories.append("CEO")

    products = [
        name for name, pattern in PRODUCTS.items()
        if pattern.search(text)
    ]

    if products and company:
        categories.append("Products")

    match = COMPANY.search(text) or CEO.search(text)

    return {
        "categories": categories,
        "products": products,
        "matched_text": text[
            max(0, match.start() - 80):
            min(len(text), match.end() + 120)
        ].strip()
    }


class TextExtractor(HTMLParser):
    """Extract article text without navigation, scripts, or site footers."""

    SKIP = {"script", "style", "noscript", "nav", "footer", "header"}

    def __init__(self):
        super().__init__()
        self.parts = []
        self.skipped = 0
        self.article_depth = 0
        self.article_parts = []
        self.article_seen = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self.skipped += 1
        if tag == "article":
            self.article_depth += 1
            self.article_seen = True

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skipped = max(0, self.skipped - 1)
        if tag == "article":
            self.article_depth = max(0, self.article_depth - 1)

    def handle_data(self, data):
        if not self.skipped and data.strip():
            self.parts.append(data)
            if self.article_depth:
                self.article_parts.append(data)

    def text(self):
        # Prefer article text to prevent matches in "related articles" menus.
        return " ".join(self.article_parts if self.article_parts else self.parts)


def publisher_url_from_google(page):
    """Use explicit publisher metadata, never an arbitrary external page link."""
    patterns = (
        r'<meta[^>]+(?:property|name)="og:url"[^>]+content="([^"]+)"',
        r'<link[^>]+rel="canonical"[^>]+href="([^"]+)"',
    )
    for pattern in patterns:
        for candidate in re.findall(pattern, page, re.I):
            candidate = html.unescape(candidate)
            host = urllib.parse.urlparse(candidate).hostname or ""
            if host and not host.endswith(
                ("google.com", "googleusercontent.com", "gstatic.com")
            ):
                return candidate
    return ""



def resolve_google_news(url):
    """Decode Google News RSS links to original publishers when possible."""
    if "news.google.com/" not in url:
        return url
    try:
        from googlenewsdecoder import gnewsdecoder
        result = gnewsdecoder(url, interval=0)
        if result.get("status") and result.get("decoded_url"):
            target = result["decoded_url"]
            host = urllib.parse.urlparse(target).hostname or ""
            if host and not host.endswith("google.com"):
                return target
    except Exception as error:
        print(f"GOOGLE NEWS DECODE FAILED: {str(error)[:160]}", flush=True)
    return url


def fetch_bing_rss(query):
    """Bing web search RSS gives a separate search index and direct links."""
    url = "https://www.bing.com/search?" + urllib.parse.urlencode({
        "q": query, "format": "rss", "count": 50
    })
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0"}
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        root = ET.fromstring(response.read(2_000_000))
    return root.findall("./channel/item")


def fetch_alert_feeds():
    """Optional Google Alerts RSS URLs stored in a GitHub Actions secret."""
    urls = [x.strip() for x in os.environ.get("GOOGLE_ALERTS_RSS", "").splitlines() if x.strip()]
    if not urls:
        DIAG.not_configured("google_alerts")
    for url in urls:
        if not url.startswith("https://www.google.com/alerts/feeds/"):
            DIAG.attempt("google_alerts")
            DIAG.failure("google_alerts", ALERTS_ADDRESS_IGNORED)
            continue
        try:
            DIAG.attempt("google_alerts")
            entries = fetch_publisher_feed("Google Alerts", url)
            DIAG.items("google_alerts", entries)
            for title, link, published, source, body in entries:
                yield title, link, published, source, body
        except Exception as error:
            print(f"ALERT FEED UNAVAILABLE: {redact(error)}", flush=True)
            DIAG.failure("google_alerts", error)


def collect_web_rss(confirmed, pending):
    """Verify Bing web search and user-configured Google Alerts against articles."""
    added = 0
    entries = list(fetch_alert_feeds())
    for query in ('"Triada Robotics"', '"Artem Veselov"',
                  '"Тріада Роботікс"', '"Артем Веселов"'):
        try:
            DIAG.attempt("bing")
            items = fetch_bing_rss(query)
            DIAG.items("bing", items)
            for item in items:
                title = (item.findtext("title") or "").strip()
                link = (item.findtext("link") or "").strip()
                if title and link:
                    entries.append((title, link, "", "Bing Search", ""))
        except Exception as error:
            print(f"BING RSS UNAVAILABLE {query}: {error}", flush=True)
            DIAG.failure("bing", error)
    for title, link, published, source, body in entries:
        if not link.startswith(("http://", "https://")):
            continue
        resolved = resolve_google_news(link)
        if "news.google.com/" in resolved:
            continue
        # Verify the *original article* rather than trusting search snippets.
        text, direct = read_publisher(resolved)
        title_match = classify(title)
        body_match = classify(text) if text else None
        match = body_match or (title_match if text else None)
        if not match:
            continue
        key = article_id(title, source)
        if key in confirmed or is_protected(pending.get(key)):
            continue
        # Bing RSS may omit dates: use collection date only as fallback.
        if not published:
            published = __import__("datetime").datetime.now(timezone.utc).isoformat()
        record = {"id": key, "title": title, "source": source,
                  "published_at": published, "url": direct or resolved,
                  "direct_url": direct or resolved, "keyword": source,
                  "status": "relevant", "verification_method": "verified_web_article"}
        record.update(match)
        confirmed[key] = record
        pending.pop(key, None)
        added += 1
        DIAG.confirmed("bing" if source == "Bing Search" else "google_alerts")
    return added


def read_article(url):
    """Only treat publisher HTML as verifiable article text."""
    try:
        url = resolve_google_news(url)
        request = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0"}
        )
        with urllib.request.urlopen(request, timeout=12) as response:
            final_url = response.geturl()
            content_type = response.headers.get("Content-Type", "").lower()
            if "html" not in content_type:
                return "", ""
            raw = response.read(2_000_000)
            charset = response.headers.get_content_charset() or "utf-8"
        page = raw.decode(charset, errors="replace")
        host = urllib.parse.urlparse(final_url).hostname or ""
        if host.endswith("news.google.com"):
            publisher = publisher_url_from_google(page)
            if publisher:
                return read_publisher(publisher)
            return "", ""
        return extract_article(page), final_url
    except Exception:
        return "", ""


def extract_article(page):
    parser = TextExtractor()
    parser.feed(page)
    return html.unescape(parser.text())


def read_publisher(url):
    """Avoid recursive Google News redirects."""
    host = urllib.parse.urlparse(url).hostname or ""
    if host.endswith("google.com"):
        return "", ""
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(request, timeout=12) as response:
            final_url = response.geturl()
            if (urllib.parse.urlparse(final_url).hostname or "").endswith("google.com"):
                return "", ""
            if "html" not in response.headers.get("Content-Type", "").lower():
                return "", ""
            raw = response.read(2_000_000)
            charset = response.headers.get_content_charset() or "utf-8"
        return extract_article(raw.decode(charset, errors="replace")), final_url
    except Exception:
        return "", ""


def fetch(query, region):
    lang, country, edition = region

    params = urllib.parse.urlencode({
        "q": f"{query} after:2026-05-31",
        "hl": lang,
        "gl": country,
        "ceid": edition,
    })

    url = "https://news.google.com/rss/search?" + params

    request = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0"}
    )

    with urllib.request.urlopen(request, timeout=15) as response:
        return ET.fromstring(response.read()).findall("./channel/item")


def fetch_publisher_feed(name, url):
    """Read RSS/Atom, including publisher-authored article content."""
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(request, timeout=12) as response:
        root = ET.fromstring(response.read(2_000_000))

    entries = []
    atom = "{http://www.w3.org/2005/Atom}"
    content_ns = "{http://purl.org/rss/1.0/modules/content/}"

    if root.tag == atom + "feed":
        items = root.findall(atom + "entry")
        for item in items:
            title = " ".join(item.findtext(atom + "title", default="").split())
            link_element = next(
                (x for x in item.findall(atom + "link")
                 if x.get("rel", "alternate") == "alternate"), None
            )
            link = link_element.get("href", "") if link_element is not None else ""
            date = item.findtext(atom + "published") or item.findtext(atom + "updated")
            raw_body = (item.findtext(atom + "content") or
                        item.findtext(atom + "summary") or "")
            if not (title and link and date):
                continue
            try:
                published = __import__("datetime").datetime.fromisoformat(
                    date.replace("Z", "+00:00")
                ).astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                continue
            if published[:10] >= START_DATE:
                entries.append((title, link, published, name, raw_body))
    else:
        for item in root.findall("./channel/item"):
            title = " ".join((item.findtext("title") or "").split())
            link = (item.findtext("link") or "").strip()
            date = item.findtext("pubDate")
            raw_body = (item.findtext(content_ns + "encoded") or
                        item.findtext("description") or "")
            if not (title and link and date):
                continue
            try:
                published = parsedate_to_datetime(date).astimezone(timezone.utc).isoformat()
            except (ValueError, TypeError):
                continue
            if published[:10] >= START_DATE:
                entries.append((title, link, published, name, raw_body))
    return entries


def match_publisher_feed(confirmed, pending, entries):
    """Verify direct article text or publisher-authored RSS content."""
    added = 0
    existing = {record_key(x): x
                for x in list(confirmed.values()) + list(pending.values())}
    for entry in entries:
        title, link, published, source = entry[:4]
        feed_body = entry[4] if len(entry) > 4 else ""
        key = article_id(title, source)
        record = existing.get(key)
        if is_protected(record):
            continue  # a hand-added record is never updated by the media pipeline
        if key in confirmed:
            if not (urllib.parse.urlparse(confirmed[key].get("url", "")).hostname or "").endswith("news.google.com"):
                continue

        # A publisher's own feed is trusted as the source, not Google snippets.
        feed_text = extract_article(html.unescape(feed_body)) if feed_body else ""
        title_match = classify(title)
        feed_match = classify(feed_text) if feed_text else None
        match = title_match or feed_match
        verification_method = "publisher_feed_title" if title_match else "publisher_feed_body"

        # Read the original article if the feed does not contain a relevant mention.
        direct = ""
        if not match:
            text, direct = read_article(link)
            match = classify(text) if text else None
            verification_method = "publisher_article_body"
        if not match:
            continue
        if record is None:
            record = {"id": key, "title": title, "source": source,
                      "published_at": published, "keyword": "publisher RSS"}
        record.update(match)
        record.update({"status": "relevant", "url": direct or link,
                       "direct_url": direct or link,
                       "verification_method": verification_method})
        confirmed[key] = record
        pending.pop(key, None)
        added += 1
    return added



def fetch_gdelt(query):
    """GDELT DOC API: independent discovery with publisher URLs."""
    params = urllib.parse.urlencode({
        "query": query,
        "mode": "artlist",
        "maxrecords": 100,
        "format": "json",
        "timespan": "3months",
        "sort": "datedesc",
    })
    request = urllib.request.Request(
        "https://api.gdeltproject.org/api/v2/doc/doc?" + params,
        headers={"User-Agent": "TRIADA-Media-Monitor/1.0"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        data = json.load(response)
    return data.get("articles", [])


def collect_gdelt(confirmed, pending):
    """Publish only articles verified in the title or publisher body."""
    added = 0
    for query in ('"Triada Robotics"', '"Artem Veselov"', '"TriadaRobotics"'):
        DIAG.attempt("gdelt")
        try:
            articles = fetch_gdelt(query)
        except Exception as error:
            print(f"GDELT UNAVAILABLE {query}: {error}", flush=True)
            DIAG.failure("gdelt", error)
            continue
        DIAG.items("gdelt", articles)
        for article in articles:
            title = html.unescape(article.get("title") or "").strip()
            url = article.get("url") or ""
            source = article.get("domain") or "GDELT"
            date = article.get("seendate") or ""
            if not (title and url and date):
                continue
            key = article_id(title, source)
            if key in confirmed or is_protected(pending.get(key)):
                continue
            # A GDELT query match alone is not evidence of a mention.
            title_match = classify(title)
            body, direct_url = read_article(url)
            body_match = classify(body) if body else None
            match = body_match or title_match
            if not match:
                continue
            # GDELT dates are UTC YYYYMMDDTHHMMSSZ.
            try:
                published = __import__("datetime").datetime.strptime(
                    date[:15], "%Y%m%dT%H%M%S"
                ).replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                continue
            if published[:10] < START_DATE:
                continue
            record = {
                "id": key, "title": title, "source": source,
                "url": direct_url or url, "direct_url": direct_url or url,
                "published_at": published, "keyword": query,
                "status": "relevant",
                "verification_method": "gdelt_article_body" if body_match else "gdelt_title",
            }
            record.update(match)
            confirmed[key] = record
            pending.pop(key, None)
            added += 1
            DIAG.confirmed("gdelt")
    return added


def load(path):
    if not path.exists():
        return []

    data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, list):
        raise ValueError(f"Invalid JSON database: {path}")

    return data


def save(path, records):
    for record in records.values():
        ensure_platform(record)

    data = sorted(
        records.values(),
        key=lambda x: str(x.get("published_at") or ""),
        reverse=True
    )

    temp = path.with_name(path.name + ".tmp")

    temp.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )

    temp.replace(path)


def main():
    DIAG.start()
    confirmed = {}
    pending = {}

    # Rebuild stable IDs to eliminate duplicates. Hand-written ids are kept, and
    # a duplicate never discards what the first copy lacks (nor a hand-added record).
    for record in load(DATABASE):
        adopt_record(record)
        key = record_key(record)
        rebuild_id(record, key)
        if key in confirmed:
            merge_duplicate(confirmed[key], record)
        else:
            confirmed[key] = record

    for record in load(REVIEW):
        adopt_record(record)
        key = record_key(record)
        rebuild_id(record, key)
        if key in pending:
            merge_duplicate(pending[key], record)
        elif key in confirmed and not is_protected(record):
            merge_duplicate(confirmed[key], record, prefer_manual=False)
        else:
            pending[key] = record  # a hand-added record stays in the file it was written in

    # Hand-added records are read-only: remember them exactly as loaded.
    manual_snapshot = snapshot_manual(confirmed, pending)

    # Recheck previously unverified articles.
    for key, record in list(pending.items()):
        if is_protected(record):
            continue  # hand-added records are not media candidates
        title_match = classify(record["title"])

        if title_match:
            # A decoded link alone does not prove the publisher article exists.
            text, direct_url = read_article(record.get("direct_url") or record["url"])
            if not (text and direct_url):
                continue
            record["url"] = direct_url
            record["direct_url"] = direct_url
            record.update(title_match)
            record["status"] = "relevant"
            record["verification_method"] = "verified_publisher_title"
            confirmed[key] = record
            del pending[key]
            continue

        text, direct_url = read_article(
            record.get("direct_url") or record["url"]
        )

        match = classify(text) if text else None

        if match and direct_url and "news.google.com/" not in direct_url:
            record.update(match)
            record["status"] = "relevant"
            record["verification_method"] = "article_body"

            if direct_url:
                record["direct_url"] = direct_url
                record["url"] = direct_url

            confirmed[key] = record
            del pending[key]

    added = 0
    errors = 0

    for query in QUERIES:
        for region in REGIONS:
            try:
                DIAG.attempt("google_news")
                items = fetch(query, region)
                DIAG.items("google_news", items)

                for item in items:
                    title = (item.findtext("title") or "").strip()
                    url = (item.findtext("link") or "").strip()
                    date = item.findtext("pubDate")

                    if not (title and url and date):
                        continue

                    published = parsedate_to_datetime(
                        date
                    ).astimezone(timezone.utc).isoformat()

                    # Archive every dated search hit, including older results.
                    # Search hits are candidates, not verified media mentions.
                    source = item.find("source")
                    source_name = (
                        source.text.strip()
                        if source is not None and source.text
                        else "Unknown"
                    )

                    key = article_id(title, source_name)

                    if key in confirmed or key in pending:
                        # Keep every search phrase that found this article,
                        # without duplicating the article in the public library.
                        record = confirmed.get(key) or pending[key]
                        if not is_protected(record):
                            queries = set(record.get("matched_queries") or [])
                            if record.get("keyword"):
                                queries.add(record["keyword"])
                            queries.add(query)
                            record["matched_queries"] = sorted(queries)
                        DIAG.verification_event("already_known")
                        continue

                    DIAG.verification_event("candidates_checked")
                    match = classify(title)
                    direct_url = ""

                    verification_method = "title" if match else ""
                    if not match:
                        text, direct_url = read_article(url)
                        match = classify(text) if text else None
                        if match:
                            verification_method = "article_body"

                    record = {
                        "id": key,
                        "title": title,
                        "source": source_name,
                        "url": direct_url or url,
                        "published_at": published,
                        "keyword": query,
                        "matched_queries": [query],
                    }

                    if match:
                        # Google News titles and decoded URLs are not sufficient
                        # evidence: require a successful publisher HTML fetch.
                        if not direct_url:
                            text, direct_url = read_article(url)
                        if not (text and direct_url):
                            DIAG.verification_event("publisher_unavailable")
                            record["status"] = "review"
                            pending[key] = record
                            continue
                        DIAG.verification_event("publisher_opened")
                        record["url"] = direct_url
                        record["direct_url"] = direct_url
                        record.update(match)
                        record["status"] = "relevant"
                        record["verification_method"] = (
                            "verified_publisher_title" if verification_method == "title"
                            else "article_body"
                        )
                        confirmed[key] = record

                        added += 1
                        DIAG.confirmed("google_news")
                        DIAG.verification_event("confirmed")
                        print(f"CONFIRMED: {title}", flush=True)

                    else:
                        if direct_url:
                            DIAG.verification_event("publisher_opened")
                            DIAG.verification_event("mention_absent")
                        else:
                            DIAG.verification_event("publisher_unavailable")
                        record["status"] = "review"
                        pending[key] = record

            except Exception as error:
                errors += 1
                print(f"ERROR {query}: {error}", flush=True)
                DIAG.failure("google_news", error)

            time.sleep(1)

    if errors == len(QUERIES) * len(REGIONS):
        print("Google News unavailable; continuing with publisher feeds and GDELT", flush=True)

    publisher_added = 0
    for source, feed in PUBLISHER_FEEDS:
        feed_added_before = publisher_added
        try:
            DIAG.feed_attempt(source)
            entries = fetch_publisher_feed(source, feed)
            DIAG.feed_items(source, entries)
            publisher_added += match_publisher_feed(confirmed, pending, entries)
            DIAG.feed_confirmed(source, publisher_added - feed_added_before)
        except Exception as error:
            print(f"FEED UNAVAILABLE {source}: {error}", flush=True)
            DIAG.feed_failure(source, error)

    gdelt_added = collect_gdelt(confirmed, pending)
    print(f'Confirmed via GDELT: {gdelt_added}', flush=True)

    web_added = collect_web_rss(confirmed, pending)
    print(f"Verified from Bing and Alerts: {web_added}", flush=True)

    for title in restore_manual(confirmed, pending, manual_snapshot):
        print(f"WARNING: restored a hand-added record that a collection step changed or removed: {title}", flush=True)

    save(DATABASE, confirmed)
    save(REVIEW, pending)
    # Public All library includes both verified mentions and search candidates.
    # Keep internal status for transparency and retain candidates across runs.
    all_records = dict(confirmed)
    for key, record in pending.items():
        if key not in all_records:
            candidate = dict(record)
            if not candidate.get("categories") and not is_protected(candidate):
                q = candidate.get("keyword", "").lower()
                candidate["categories"] = (
                    ["CEO"] if any(x in q for x in ("veselov", "veselow", "веселов"))
                    else ["Company"] if any(x in q for x in ("triada", "тріада", "триада"))
                    else []
                )
            all_records[key] = candidate
    save(PUBLIC, all_records)
    report = {
        "last_run_utc": __import__("datetime").datetime.now(timezone.utc).isoformat(),
        "confirmed_total": len(confirmed),
        "pending_internal": len(pending),
        "google_news_added": added,
        "publisher_feeds_added": publisher_added,
        "gdelt_added": gdelt_added,
        "bing_and_alerts_added": web_added,
        "google_news_query_errors": errors,
    }
    report.update(build_diagnostics_report())
    STATUS_FILE.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(f"New confirmed from Google News: {added}")
    print(f"Confirmed via publisher feeds: {publisher_added}")
    print(f"Total confirmed: {len(confirmed)}")
    print(f"Pending verification: {len(pending)}")
    print(f"Search errors: {errors}")


if __name__ == "__main__":
    main()
