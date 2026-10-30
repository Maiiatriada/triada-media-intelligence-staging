import copy
import json
import unittest
import xml.etree.ElementTree as ET
from unittest.mock import patch

import collector
from tools import monitoring as m
from tools.tracker_records import library_records


CFG = {"owned_domains": ["triada-robotics.com"], "owned_profiles": ["https://x.com/TriadaRobotics", "https://facebook.com/profile.php?id=61594069029868"], "social_domains": ["x.com"], "queries": ['"Triada Robotics"'], "max_candidates_per_platform": 2, "start_date": "2026-04-23", "author_start_date": "2026-10-29", "author_names": ["Artem Veselov"], "author_feed": "https://militarnyi.com/en/author/artem_veselov/feed/", "youtube_max_pages_per_query": 1}
URL = "https://x.com/independent/status/123"

def page(url=URL, **changes):
    n = {"@type": "SocialMediaPosting", "url": url, "headline": "Triada Robotics unveils a drone", "datePublished": "2026-10-28T10:00:00Z", "author": {"name": "Independent", "url": "https://x.com/independent"}}
    n.update(changes)
    return '<script type="application/ld+json">' + json.dumps(n) + '</script>'


class MonitoringTests(unittest.TestCase):
    def test_platform_boundary(self):
        for url, expected in [(URL, "X"), ("https://m.facebook.com/a/posts/12", "Facebook"), ("https://instagram.com/reel/abc/", "Instagram"), ("https://youtube.com/watch?v=abc", "YouTube"), ("https://notfacebook.com/posts/1", "Media"), ("https://facebook.com.evil.test/posts/1", "Media")]:
            self.assertEqual(m.platform(url), expected)

    def test_own_profiles_and_external_mentions(self):
        self.assertTrue(m.owned("https://twitter.com/TriadaRobotics/status/12", CFG))
        self.assertTrue(m.owned("https://facebook.com/permalink.php?story_fbid=7&id=61594069029868", CFG))
        self.assertFalse(m.owned(URL + "?text=TriadaRobotics", CFG))
        self.assertFalse(m.owned("https://x.com/TriadaRoboticsFans/status/12", CFG))

    def test_profile_is_not_a_post(self):
        self.assertFalse(m.post_url("https://instagram.com/triada"))
        self.assertFalse(m.post_url("https://facebook.com/profile.php?id=1"))
        self.assertTrue(m.post_url(URL))

    def test_metadata_belongs_to_post_and_author(self):
        self.assertIsNotNone(m.native_metadata(page(), URL, collector.classify))
        for content in ["Triada Robotics - Log in", page(url="https://x.com/other/status/4"), page(datePublished=""), page(author={"name": "Someone"}), page(headline="Unrelated drones"), page(author={"name": "Someone", "url": "https://other.test/"})]:
            self.assertIsNone(m.native_metadata(content, URL, collector.classify))

    def test_automatic_social_is_not_protected_and_preserves_post_identity(self):
        record = {"origin": "collector", "identity_version": "url-v1", "platform": "X", "url": URL, "id": m.identity(URL), "title": "Same text", "source": "Same author"}
        self.assertFalse(collector.is_protected(record))
        self.assertEqual(collector.record_key(record), m.identity(URL))
        other = dict(record, url=URL + "4")
        self.assertNotEqual(collector.record_key(record), collector.record_key(other))
        record.pop("origin")
        self.assertTrue(collector.is_protected(record))

    def test_social_collection_repeat_and_library(self):
        item = ET.fromstring('<item><title>Triada Robotics</title><link>' + URL + '</link></item>')
        def fetch(url):
            return ("<rss><channel/></rss>", url) if url == CFG["author_feed"] else (page(), url)
        confirmed, pending = {}, {}
        with patch.object(m, "config", return_value=CFG), patch.object(m, "fetch", side_effect=fetch), patch.object(m.time, "sleep"), patch.dict(m.os.environ, {"YOUTUBE_API_KEY": ""}):
            r = m.collect(confirmed, pending, collector.classify, lambda q: [item])
            self.assertEqual(r["platforms"]["X"]["confirmed"], 1)
            original = copy.deepcopy(confirmed)
            m.collect(confirmed, pending, collector.classify, lambda q: [item])
            self.assertEqual(original, confirmed)
        rows, report = library_records(list(confirmed.values()))
        self.assertEqual((len(rows), rows[0]["platform"], report["collector_shown"]), (1, "X", 1))

    def test_blocked_page_remains_review_and_does_not_inflate_library(self):
        item = ET.fromstring('<item><title>Triada Robotics</title><link>' + URL + '</link></item>')
        with patch.object(m, "config", return_value=CFG), patch.object(m, "fetch", side_effect=OSError("blocked")), patch.object(m.time, "sleep"), patch.dict(m.os.environ, {"YOUTUBE_API_KEY": ""}):
            confirmed, pending = {}, {}
            r = m.collect(confirmed, pending, collector.classify, lambda q: [item])
        self.assertFalse(confirmed)
        self.assertEqual(next(iter(pending.values()))["published_at"], "")
        self.assertEqual(r["platforms"]["X"]["status"], "partial")
        self.assertFalse(library_records(list(pending.values()))[0])

    def test_legacy_media_discovery_cannot_bypass_social_checks(self):
        r = {"id": "a" * 64, "url": URL, "platform": "Media", "status": "relevant"}
        c, p = {r["id"]: r}, {}
        with patch.object(m, "config", return_value=CFG): m.enforce_scope(c, p, collector.is_protected)
        self.assertFalse(c)
        self.assertEqual(p[r["id"]]["platform"], "X")
        self.assertEqual(p[r["id"]]["status"], "review")

    def test_author_exception_requires_name_and_new_date(self):
        items = ''.join('<item><title>Author column</title><link>https://militarnyi.com/en/' + str(i) + '</link><dc:creator>' + author + '</dc:creator><pubDate>' + date + '</pubDate></item>' for i, (author, date) in enumerate([("Artem Veselov", "Thu, 29 Oct 2026 09:00:00 GMT"), ("Other", "Thu, 29 Oct 2026 09:00:00 GMT"), ("Artem Veselov", "Wed, 28 Oct 2026 09:00:00 GMT")]))
        feed = '<rss xmlns:dc="http://purl.org/dc/elements/1.1/"><channel>' + items + '</channel></rss>'
        c, p = {}, {}
        with patch.object(m, "config", return_value=CFG), patch.object(m, "fetch", return_value=(feed, CFG["author_feed"])), patch.object(m.time, "sleep"), patch.dict(m.os.environ, {"YOUTUBE_API_KEY": ""}):
            m.collect(c, p, collector.classify, lambda q: [])
        self.assertEqual(len(c), 1)
        self.assertEqual(next(iter(c.values()))["content_type"], "authored_article")

    def test_bing_without_date_held(self):
        m.PUBLICATION_DATES.clear()
        item = ET.fromstring('<item><title>Triada Robotics</title><link>https://example.org/no-date</link></item>')
        c, p = {}, {}
        with patch.object(collector, "fetch_bing_rss", return_value=[item]), patch.object(collector, "fetch_alert_feeds", return_value=[]), patch.object(collector, "read_publisher", return_value=("Triada Robotics", "https://example.org/no-date")):
            self.assertEqual(collector.collect_web_rss(c, p), 0)
        self.assertFalse(c)
        self.assertEqual(next(iter(p.values()))["review_reason"], "publication_date_required")

    def test_publication_metadata_ignores_modified_date(self):
        m.PUBLICATION_DATES.clear()
        m.remember_date('<meta property="article:modified_time" content="2026-10-29T09:00:00Z">', URL)
        self.assertNotIn(URL, m.PUBLICATION_DATES)
        m.remember_date('<meta content="2026-10-28T09:00:00Z" property="article:published_time">', URL)
        self.assertTrue(m.PUBLICATION_DATES[URL].startswith("2026-10-28"))
        m.PUBLICATION_DATES.clear()

    def test_related_link_is_not_an_article_mention(self):
        body = '<p>News about a different company.</p><div><a href="/triada"><strong>Читайте також:</strong> Triada Robotics BRAVO</a></div>'
        self.assertIsNone(collector.classify(collector.extract_article(body)))
        self.assertIsNotNone(collector.classify(collector.extract_article('<p>Triada Robotics makes BRAVO.</p>' + body)))

    def test_api_error_does_not_leak_key(self):
        exc = ValueError("https://api.test?key=secret")
        self.assertEqual(m.error_name(exc), "ValueError")

    def test_youtube_api_full_description_and_repeat(self):
        c, p = {}, {}
        def fetch(url):
            if "youtube/v3/search?" in url:
                return json.dumps({"items": [{"id": {"videoId": "test123"}}]}), url
            if "youtube/v3/videos?" in url:
                return json.dumps({"items": [{"id": "test123", "snippet": {"title": "Drone news", "description": "Triada Robotics builds BRAVO", "publishedAt": "2026-10-28T10:00:00Z", "channelTitle": "Independent", "channelId": "UCtest"}}]}), url
            return "<rss><channel/></rss>", url
        with patch.object(m, "config", return_value=CFG), patch.object(m, "fetch", side_effect=fetch), patch.object(m.time, "sleep"), patch.dict(m.os.environ, {"YOUTUBE_API_KEY": "fake-test-key"}):
            report = m.collect(c, p, collector.classify, lambda q: [])
            self.assertEqual(report["youtube_api"]["confirmed"], 1)
            m.collect(c, p, collector.classify, lambda q: [])
        self.assertEqual(len(c), 1)
        self.assertEqual(library_records(list(c.values()))[0][0]["platform"], "YouTube")
        self.assertNotIn("fake-test-key", json.dumps(report))

    def test_author_page_checks_article_author_and_date(self):
        cfg = dict(CFG, author_page="https://militarnyi.com/en/author/artem_veselov/")
        url = "https://militarnyi.com/en/blogs/new-column/"
        n = {"@type": "BlogPosting", "headline": "A new column", "datePublished": "2026-10-29T09:00:00Z", "author": {"@id": cfg["author_page"]}, "mainEntityOfPage": {"@id": url + "#webpage"}}
        def fetch(target):
            if target == cfg["author_page"]: return '<a class="news_item-link" href="' + url + '">Column</a>', target
            return '<script type="application/ld+json">' + json.dumps(n) + '</script>', target
        with patch.object(m, "fetch", side_effect=fetch):
            self.assertEqual(len(m.author_articles(cfg)[0]), 1)
            n["author"]["@id"] = "https://militarnyi.com/en/author/other/"
            self.assertEqual(m.author_articles(cfg)[0], [])


if __name__ == "__main__": unittest.main()
