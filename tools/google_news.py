"""Bounded retries for the public Google News RSS endpoint; no paid API."""
from datetime import date, timedelta
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree as ET
import time

RETRYABLE = {429, 500, 502, 503, 504}

def fetch_items(query, region, start_date, *, opener=None, sleep=None):
    opener, sleep = opener or urlopen, sleep or time.sleep
    lang, country, edition = region
    # Google expects a BCP47 language; keep the configured news edition unchanged.
    language = f"{lang}-{country}" if "-" not in lang else lang
    after = (date.fromisoformat(start_date) - timedelta(days=1)).isoformat()
    url = "https://news.google.com/rss/search?" + urlencode({"q":f"{query} after:{after}","hl":language,"gl":country,"ceid":edition})
    request = Request(url, headers={"User-Agent":"Mozilla/5.0", "Accept":"application/rss+xml, application/xml;q=0.9, */*;q=0.8"})
    for attempt in range(3):
        try:
            with opener(request, timeout=20) as response:
                root = ET.fromstring(response.read(8_000_000))
                if root.tag != "rss" or root.find("channel") is None:
                    raise ValueError("Google News returned a non-RSS response")
                return root.findall("./channel/item")
        except (HTTPError, URLError, TimeoutError) as error:
            if attempt == 2 or isinstance(error, HTTPError) and error.code not in RETRYABLE:
                raise
            delay = (2, 5)[attempt]
            retry = error.headers.get("Retry-After") if isinstance(error, HTTPError) and error.headers else None
            if retry:
                try: delay = max(delay, min(30, float(retry)))
                except ValueError: pass
            sleep(delay)
