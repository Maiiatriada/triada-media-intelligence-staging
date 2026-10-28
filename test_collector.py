import contextlib
import io
import json
import os
import tempfile
import unittest
import urllib.parse
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch
import collector


class CollectorTests(unittest.TestCase):
    def test_stable_deduplication(self):
        self.assertEqual(
            collector.article_id("Triada Robotics’ Bravo!", "Defender Media"),
            collector.article_id("Triada Robotics Bravo", "Defender Media"),
        )
        self.assertNotEqual(
            collector.article_id("Triada Robotics Bravo", "Defender Media"),
            collector.article_id("Тріада Роботікс Bravo", "Defender Media"),
        )

    def test_company_and_ceo(self):
        self.assertIsNotNone(collector.classify("Triada Robotics develops drones"))
        self.assertIsNotNone(collector.classify("Interview with Artem Veselov"))
        self.assertIsNotNone(collector.classify("Інтерв’ю з Артемом Веселовим"))
        self.assertIsNone(collector.classify("BRAVO football team scores"))

    def test_article_body_and_no_navigation(self):
        page = "<nav>Triada Robotics</nav><article><h1>Drone news</h1><p>Artem Veselov said this.</p></article>"
        text = collector.extract_article(page)
        self.assertIn("Artem Veselov", text)
        self.assertNotIn("Triada Robotics", text)

    def test_publisher_url_extraction(self):
        page = '<meta property="og:url" content="https://example.org/article">'
        self.assertEqual(
            collector.publisher_url_from_google(page),
            "https://example.org/article",
        )

    def test_no_arbitrary_publisher_link(self):
        self.assertEqual(collector.publisher_url_from_google('<a href="https://other.org">Other</a>'), "")

    def test_publisher_feed_confirms_body_mention(self):
        from unittest.mock import patch
        entries = [("Drone technology update", "https://publisher.example/story",
                    "2026-10-21T09:00:00+00:00", "Example Publisher")]
        with patch("collector.read_article",
                   return_value=("Triada Robotics develops BRAVO", "https://publisher.example/story")):
            confirmed, pending = {}, {}
            added = collector.match_publisher_feed(confirmed, pending, entries)
        self.assertEqual(added, 1)
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(next(iter(confirmed.values()))["url"], "https://publisher.example/story")

    def test_publisher_feed_rejects_unrelated_body(self):
        from unittest.mock import patch
        entries = [("Bravo wins football match", "https://publisher.example/football",
                    "2026-10-21T09:00:00+00:00", "Example Publisher")]
        with patch("collector.read_article", return_value=("Sports match report", "https://publisher.example/football")):
            confirmed = {}
            added = collector.match_publisher_feed(confirmed, {}, entries)
        self.assertEqual(added, 0)
        self.assertEqual(confirmed, {})

    def test_publisher_feed_content_verifies_body_without_fetch(self):
        entries = [("New defence technology", "https://publisher.example/story",
                    "2026-10-21T09:00:00+00:00", "Publisher",
                    "<p>Artem Veselov of Triada Robotics spoke about drones.</p>")]
        with patch("collector.read_article", side_effect=AssertionError("No fetch needed")):
            confirmed = {}
            added = collector.match_publisher_feed(confirmed, {}, entries)
        self.assertEqual(added, 1)
        self.assertEqual(next(iter(confirmed.values()))["verification_method"],
                         "publisher_feed_body")

    def test_publisher_feed_ignores_unrelated_content(self):
        entries = [("Drone technology", "https://publisher.example/story",
                    "2026-10-21T09:00:00+00:00", "Publisher",
                    "<p>Generic BRAVO and KILO names only.</p>")]
        with patch("collector.read_article", return_value=("", "")):
            confirmed = {}
            added = collector.match_publisher_feed(confirmed, {}, entries)
        self.assertEqual(added, 0)
        self.assertFalse(confirmed)

    def test_rss_content_encoded(self):
        import io
        from unittest.mock import MagicMock
        feed = b"""<rss xmlns:content="http://purl.org/rss/1.0/modules/content/">
          <channel><item><title>New drone</title><link>https://publisher.example/news</link>
          <pubDate>Wed, 21 Oct 2026 09:00:00 GMT</pubDate>
          <content:encoded><![CDATA[<p>Triada Robotics is mentioned here.</p>]]></content:encoded>
          </item></channel></rss>"""
        response = MagicMock()
        response.read.return_value = feed
        response.__enter__.return_value = response
        with patch("collector.urllib.request.urlopen", return_value=response):
            entries = collector.fetch_publisher_feed("Publisher", "https://publisher.example/rss")
        self.assertEqual(len(entries), 1)
        self.assertIn("Triada Robotics", entries[0][4])

    def test_gdelt_verifies_body_before_publication(self):
        article = {"title": "New drone capabilities",
                   "url": "https://example.org/triada", "domain": "example.org",
                   "seendate": "20261022T110000Z"}
        with patch("collector.fetch_gdelt", return_value=[article]), patch(
            "collector.read_article",
            return_value=("Triada Robotics develops drones.", "https://example.org/triada")
        ):
            confirmed = {}
            count = collector.collect_gdelt(confirmed, {})
        self.assertEqual(count, 1)
        self.assertEqual(len(confirmed), 1)

    def test_gdelt_rejects_keyword_only_search_hit(self):
        article = {"title": "Random drone news", "url": "https://example.org/random",
                   "domain": "example.org", "seendate": "20261022T110000Z"}
        with patch("collector.fetch_gdelt", return_value=[article]), patch(
            "collector.read_article", return_value=("No company mention.", "https://example.org/random")
        ):
            confirmed = {}
            count = collector.collect_gdelt(confirmed, {})
        self.assertEqual(count, 0)
        self.assertFalse(confirmed)

    def test_bing_rss_rejects_unrelated_article(self):
        import xml.etree.ElementTree as ET
        xml = ET.fromstring("<item><title>Drone technology</title><link>https://example.org/drone</link></item>")
        with patch("collector.fetch_bing_rss", return_value=[xml]), patch(
            "collector.fetch_alert_feeds", return_value=iter(())
        ), patch("collector.read_publisher", return_value=("Other unrelated drones", "https://example.org/drone")):
            confirmed = {}
            count = collector.collect_web_rss(confirmed, {})
        self.assertEqual(count, 0)
        self.assertEqual(confirmed, {})

    def test_bing_rss_accepts_verified_company_article(self):
        import xml.etree.ElementTree as ET
        xml = ET.fromstring("<item><title>Drone technology</title><link>https://example.org/drone</link></item>")
        with patch("collector.fetch_bing_rss", return_value=[xml]), patch(
            "collector.fetch_alert_feeds", return_value=iter(())
        ), patch("collector.read_publisher", return_value=("Triada Robotics developed a drone", "https://example.org/drone")):
            confirmed = {}
            count = collector.collect_web_rss(confirmed, {})
        self.assertEqual(count, 1)
        self.assertEqual(next(iter(confirmed.values()))["categories"], ["Company"])

    def test_blocked_article_not_confirmed(self):
        with patch("collector.urllib.request.urlopen", side_effect=OSError("blocked")):
            self.assertEqual(collector.read_article("https://example.org"), ("", ""))


# ---------------------------------------------------------------------------
# Connector diagnostics (collector_status.json)
# ---------------------------------------------------------------------------

# Fake feed address used only to prove that secrets never reach any output.
SECRET_URL = ("https://www.google.com/alerts/feeds/"
              "12345678901234567890/09876543210987654321")
SECRET_TOKENS = ("12345678901234567890", "09876543210987654321")
OLD_STATUS_KEYS = (
    "last_run_utc", "confirmed_total", "pending_internal", "google_news_added",
    "publisher_feeds_added", "gdelt_added", "bing_and_alerts_added",
    "google_news_query_errors",
)


def rss_item(title, link, source="Example Source",
             date="Wed, 21 Oct 2026 09:00:00 GMT"):
    return ET.fromstring(
        f"<item><title>{title}</title><link>{link}</link>"
        f"<pubDate>{date}</pubDate><source>{source}</source></item>"
    )


def gdelt_article(title="Triada Robotics expands", url="https://example.org/t"):
    return {"title": title, "url": url, "domain": "example.org",
            "seendate": "20261022T110000Z"}


class ConnectorDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        collector.DIAG.reset()
        self.addCleanup(collector.DIAG.reset)
        quiet = contextlib.redirect_stdout(io.StringIO())  # keep CI logs free of collector output
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        env = patch.dict(os.environ, {
            "GOOGLE_ALERTS_RSS": "", "GITHUB_SERVER_URL": "",
            "GITHUB_REPOSITORY": "", "GITHUB_RUN_ID": "",
        })
        env.start()
        self.addCleanup(env.stop)

    def connector(self, name):
        return collector.DIAG.report({})["connectors"][name]

    def run_main(self, fetch=None, feed=None, gdelt=None, bing=None,
                 article=None, publisher=None, alerts_env="", previous=None):
        """Run main() in a temporary folder with every network call mocked."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original_dir = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, original_dir)
        if previous is not None:
            Path("collector_status.json").write_text(
                previous if isinstance(previous, str) else json.dumps(previous),
                encoding="utf-8")
        output = io.StringIO()
        with patch.dict(os.environ, {"GOOGLE_ALERTS_RSS": alerts_env}), \
                patch("collector.fetch", side_effect=fetch or (lambda q, r: [])), \
                patch("collector.fetch_publisher_feed", side_effect=feed or (lambda n, u: [])), \
                patch("collector.fetch_gdelt", side_effect=gdelt or (lambda q: [])), \
                patch("collector.fetch_bing_rss", side_effect=bing or (lambda q: [])), \
                patch("collector.read_article", side_effect=article or (lambda u: ("", ""))), \
                patch("collector.read_publisher", side_effect=publisher or (lambda u: ("", ""))), \
                patch("collector.resolve_google_news", side_effect=lambda url: url), \
                patch("collector.time.sleep"), \
                contextlib.redirect_stdout(output):
            collector.main()
        files = {p.name: p.read_text(encoding="utf-8") for p in Path(".").glob("*.json")}
        return json.loads(files["collector_status.json"]), files, output.getvalue()

    # ---- error cleaning and secrets -------------------------------------

    def test_safe_error_strips_urls_and_limits_length(self):
        text = collector.safe_error(
            OSError("failed https://example.org/a?token=abc " + "x" * 400))
        self.assertTrue(text.startswith("OSError:"))
        self.assertNotIn("example.org", text)
        self.assertNotIn("token=abc", text)
        self.assertLessEqual(len(text), 160)
        self.assertNotIn("\n", collector.safe_error(OSError("line one\nline two")))

    def test_safe_error_redacts_alert_secret_in_any_form(self):
        with patch.dict(os.environ, {"GOOGLE_ALERTS_RSS": SECRET_URL}):
            forms = [
                SECRET_URL,
                SECRET_URL.replace("https://", ""),
                "/alerts/feeds/12345678901234567890/09876543210987654321",
                "feed id 12345678901234567890 broke",
                urllib.parse.quote(SECRET_URL, safe=""),
            ]
            for form in forms:
                with self.subTest(form=form[:40]):
                    text = collector.safe_error(OSError(f"cannot read {form}"))
                    for token in SECRET_TOKENS:
                        self.assertNotIn(token, text)

    def test_alert_secret_never_reaches_any_output(self):
        def feed(name, url):
            if name == "Google Alerts":
                raise OSError(f"cannot read {url}")
            return []
        status, files, stdout = self.run_main(feed=feed, alerts_env=SECRET_URL)
        everything = stdout + "".join(files.values())
        for secret in SECRET_TOKENS + (SECRET_URL,):
            self.assertNotIn(secret, everything)
        self.assertIn("ALERT FEED UNAVAILABLE", stdout)
        self.assertEqual(status["connectors"]["google_alerts"]["status"], "error")

    # ---- status rules ------------------------------------------------------

    def test_status_rules(self):
        d = collector.Diagnostics()
        d.attempt("gdelt"); d.items("gdelt", [1, 2])
        d.attempt("gdelt"); d.items("gdelt", [])
        d.attempt("bing"); d.attempt("bing"); d.failure("bing", OSError("x"))
        d.attempt("google_news"); d.failure("google_news", OSError("x"))
        d.not_configured("google_alerts")
        report = d.report({})["connectors"]
        self.assertEqual(report["gdelt"]["status"], "ok")
        self.assertEqual(report["bing"]["status"], "partial")
        self.assertEqual(report["google_news"]["status"], "error")
        self.assertEqual(report["google_alerts"]["status"], "not_configured")
        self.assertEqual(report["publisher_rss"]["status"], "not_run")
        self.assertEqual(report["gdelt"]["items_fetched"], 2)
        self.assertEqual(report["bing"]["failed_requests"], 1)

    def test_ok_means_technical_success_not_complete_coverage(self):
        d = collector.Diagnostics()
        d.attempt("gdelt"); d.items("gdelt", [])  # answered, but returned nothing
        report = d.report({})
        self.assertEqual(report["connectors"]["gdelt"]["status"], "ok")
        self.assertEqual(report["connectors"]["gdelt"]["items_fetched"], 0)
        self.assertIn("does not guarantee", report["status_legend"]["ok"])
        for status in ("ok", "partial", "error", "not_configured", "not_run"):
            self.assertIn(status, report["status_legend"])

    def test_carry_over_success_time_and_failure_streak(self):
        before = "2026-10-01T00:00:00+00:00"
        previous = {"gdelt": {"last_success_utc": before, "last_attempt_utc": before,
                              "consecutive_failures": 2}}
        d = collector.Diagnostics()
        d.attempt("gdelt"); d.failure("gdelt", OSError("down"))
        failing = d.report(previous)["connectors"]["gdelt"]
        self.assertEqual(failing["consecutive_failures"], 3)
        self.assertEqual(failing["last_success_utc"], before)
        self.assertNotEqual(failing["last_attempt_utc"], before)

        d = collector.Diagnostics()
        d.attempt("gdelt")
        recovered = d.report(previous)["connectors"]["gdelt"]
        self.assertEqual(recovered["consecutive_failures"], 0)
        self.assertEqual(recovered["last_success_utc"], recovered["last_attempt_utc"])

        idle = collector.Diagnostics().report(previous)["connectors"]["gdelt"]
        self.assertEqual(idle["status"], "not_run")
        self.assertEqual(idle["consecutive_failures"], 2)
        self.assertEqual(idle["last_success_utc"], before)

    def test_malformed_previous_status_is_ignored(self):
        for previous in ("{not json", '["a list"]', '{"connectors": "text"}',
                         '{"connectors": {"gdelt": {"consecutive_failures": "x", '
                         '"last_success_utc": 5}}}'):
            with self.subTest(previous=previous[:20]):
                status, _, _ = self.run_main(previous=previous)
                gdelt = status["connectors"]["gdelt"]
                self.assertEqual(gdelt["status"], "ok")
                self.assertEqual(gdelt["consecutive_failures"], 0)
                self.assertIsInstance(gdelt["last_success_utc"], str)

    # ---- GDELT ---------------------------------------------------------------

    def test_gdelt_total_failure_is_reported_and_collection_continues(self):
        with patch("collector.fetch_gdelt", side_effect=OSError("boom")):
            self.assertEqual(collector.collect_gdelt({}, {}), 0)
        gdelt = self.connector("gdelt")
        self.assertEqual((gdelt["status"], gdelt["requests"], gdelt["failed_requests"]),
                         ("error", 3, 3))
        self.assertEqual(gdelt["last_error"], "OSError: boom")

    def test_gdelt_partial_success_counts_items_and_confirmations(self):
        calls = [OSError("boom"), [gdelt_article()], []]
        with patch("collector.fetch_gdelt", side_effect=calls), patch(
                "collector.read_article", return_value=("", "")):
            confirmed = {}
            self.assertEqual(collector.collect_gdelt(confirmed, {}), 1)
        gdelt = self.connector("gdelt")
        self.assertEqual(gdelt["status"], "partial")
        self.assertEqual((gdelt["items_fetched"], gdelt["new_confirmed"]), (1, 1))

    def test_gdelt_ok_with_no_results(self):
        with patch("collector.fetch_gdelt", return_value=[]):
            collector.collect_gdelt({}, {})
        self.assertEqual(self.connector("gdelt")["status"], "ok")

    # ---- Bing ------------------------------------------------------------------

    def test_bing_failures_and_confirmations(self):
        item = ET.fromstring("<item><title>Drone news</title>"
                             "<link>https://example.org/d</link></item>")
        with patch("collector.fetch_bing_rss", side_effect=OSError("down")), patch(
                "collector.fetch_alert_feeds", return_value=iter(())):
            self.assertEqual(collector.collect_web_rss({}, {}), 0)
        self.assertEqual(self.connector("bing")["status"], "error")
        self.assertEqual(self.connector("bing")["failed_requests"], 4)

        collector.DIAG.reset()
        with patch("collector.fetch_bing_rss",
                   side_effect=[[item], OSError("down"), [], []]), patch(
                "collector.fetch_alert_feeds", return_value=iter(())), patch(
                "collector.read_publisher",
                return_value=("Triada Robotics built a drone", "https://example.org/d")):
            confirmed = {}
            self.assertEqual(collector.collect_web_rss(confirmed, {}), 1)
        bing = self.connector("bing")
        self.assertEqual((bing["status"], bing["items_fetched"], bing["new_confirmed"]),
                         ("partial", 1, 1))
        self.assertEqual(self.connector("google_alerts")["new_confirmed"], 0)

    # ---- Google Alerts -------------------------------------------------------------

    def test_alerts_not_configured_without_secret(self):
        self.assertEqual(list(collector.fetch_alert_feeds()), [])
        alerts = self.connector("google_alerts")
        self.assertEqual((alerts["status"], alerts["requests"]), ("not_configured", 0))

    def test_alerts_configured_and_working(self):
        entries = [("T1", "https://x.example/1", "2026-10-21T09:00:00+00:00", "Google Alerts", ""),
                   ("T2", "https://x.example/2", "2026-10-21T09:00:00+00:00", "Google Alerts", "")]
        with patch.dict(os.environ, {"GOOGLE_ALERTS_RSS": SECRET_URL}), patch(
                "collector.fetch_publisher_feed", return_value=entries):
            self.assertEqual(len(list(collector.fetch_alert_feeds())), 2)
        alerts = self.connector("google_alerts")
        self.assertEqual((alerts["status"], alerts["items_fetched"]), ("ok", 2))

    def test_alerts_invalid_address_is_flagged_without_echoing_it(self):
        bad = "https://example.com/private-token-abcdefgh"
        with patch.dict(os.environ, {"GOOGLE_ALERTS_RSS": bad}):
            self.assertEqual(list(collector.fetch_alert_feeds()), [])
        alerts = self.connector("google_alerts")
        self.assertEqual(alerts["status"], "error")
        self.assertEqual(alerts["last_error"], collector.ALERTS_ADDRESS_IGNORED)
        self.assertNotIn("example.com", json.dumps(alerts))
        self.assertNotIn("abcdefgh", json.dumps(alerts))

    # ---- Publisher RSS ----------------------------------------------------------------

    def test_publisher_feeds_are_reported_individually(self):
        names = [name for name, _ in collector.PUBLISHER_FEEDS]
        broken = {names[1], names[3]}

        def feed(name, url):
            if name in broken:
                raise OSError("feed down")
            if name == names[0]:
                return [("Drone update", "https://publisher.example/story",
                         "2026-10-21T09:00:00+00:00", name,
                         "<p>Triada Robotics is mentioned here.</p>")]
            return []

        status, _, _ = self.run_main(feed=feed)
        publisher = status["connectors"]["publisher_rss"]
        self.assertEqual(publisher["status"], "partial")
        self.assertEqual([f["name"] for f in publisher["feeds"]], names)
        by_name = {f["name"]: f for f in publisher["feeds"]}
        self.assertEqual(by_name[names[0]]["status"], "ok")
        self.assertEqual((by_name[names[0]]["items_fetched"], by_name[names[0]]["new_confirmed"]), (1, 1))
        self.assertEqual(by_name[names[1]]["status"], "error")
        self.assertEqual(by_name[names[1]]["last_error"], "OSError: feed down")
        self.assertEqual((publisher["requests"], publisher["failed_requests"]), (len(names), 2))
        self.assertEqual(publisher["new_confirmed"], status["publisher_feeds_added"])

    # ---- Google News --------------------------------------------------------------------

    def test_google_news_total_failure(self):
        def fail(query, region):
            raise OSError("blocked")
        status, _, _ = self.run_main(fetch=fail)
        news = status["connectors"]["google_news"]
        total = len(collector.QUERIES) * len(collector.REGIONS)
        self.assertEqual((news["status"], news["requests"], news["failed_requests"]),
                         ("error", total, total))
        self.assertEqual(news["failed_requests"], status["google_news_query_errors"])
        self.assertEqual(news["last_error"], "OSError: blocked")

    def test_google_news_success_counts_items_and_confirmations(self):
        item = rss_item("Triada Robotics unveils BRAVO", "https://publisher.example/gn")
        status, _, _ = self.run_main(
            fetch=lambda q, r: [item],
            article=lambda url: ("Triada Robotics unveils BRAVO", "https://publisher.example/gn")
        )
        news = status["connectors"]["google_news"]
        total = len(collector.QUERIES) * len(collector.REGIONS)
        self.assertEqual((news["status"], news["items_fetched"], news["new_confirmed"]),
                         ("ok", total, 1))
        self.assertEqual(news["new_confirmed"], status["google_news_added"])

    def test_google_news_archives_older_hits_and_deduplicates_queries(self):
        from email.utils import format_datetime
        from datetime import datetime, timezone
        item = rss_item("Unrelated industry story", "https://publisher.example/old")
        item.find("pubDate").text = format_datetime(
            datetime(2026, 4, 2, tzinfo=timezone.utc))
        status, files, _ = self.run_main(fetch=lambda q, r: [item])
        public = json.loads(files["mentions_public.json"])
        self.assertEqual(len(public), 1)
        self.assertEqual(public[0]["status"], "review")
        self.assertEqual(len(public[0]["matched_queries"]), len(collector.QUERIES))
        self.assertEqual(status["google_news_verification"]["already_known"],
                         len(collector.QUERIES) * len(collector.REGIONS) - 1)

    def test_google_news_verification_funnel_distinguishes_outcomes(self):
        items = [
            rss_item("Triada Robotics launches BRAVO", "https://publisher.example/verified"),
            rss_item("Triada Robotics announces update", "https://publisher.example/blocked"),
            rss_item("Unrelated drone article", "https://publisher.example/unrelated"),
        ]

        def article(url):
            if url.endswith("/verified"):
                return "Triada Robotics launches BRAVO", url
            if url.endswith("/unrelated"):
                return "Article about a different company", url
            return "", ""

        status, _, _ = self.run_main(fetch=lambda q, r: items, article=article)
        funnel = status["google_news_verification"]
        self.assertEqual(funnel["candidates_checked"], 3)
        self.assertEqual(funnel["publisher_opened"], 2)
        self.assertEqual(funnel["publisher_unavailable"], 1)
        self.assertEqual(funnel["mention_absent"], 1)
        self.assertEqual(funnel["confirmed"], 1)
        self.assertGreater(funnel["already_known"], 0)

    # ---- whole-run guarantees ------------------------------------------------------------

    def test_old_status_keys_are_kept_and_agree_with_connector_totals(self):
        gn = rss_item("Triada Robotics unveils BRAVO", "https://publisher.example/gn")
        feed_entry = ("Update", "https://publisher.example/pf",
                      "2026-10-21T09:00:00+00:00", "Feed", "<p>Artem Veselov of Triada Robotics</p>")
        bing_item = ET.fromstring("<item><title>Defence news</title>"
                                  "<link>https://example.org/bing</link></item>")
        alert_entry = ("Alert story", "https://example.org/alert",
                       "2026-10-21T09:00:00+00:00", "Google Alerts", "")

        def feed(name, url):
            return [alert_entry] if name == "Google Alerts" else (
                [feed_entry] if name == collector.PUBLISHER_FEEDS[0][0] else [])

        def publisher(url):
            return ("Triada Robotics is here", url) if "bing" in url or "alert" in url else ("", "")

        status, _, _ = self.run_main(
            fetch=lambda q, r: [gn], feed=feed,
            gdelt=lambda q: [gdelt_article()] if "Triada" in q else [],
            bing=lambda q: [bing_item], publisher=publisher, alerts_env=SECRET_URL)

        for key in OLD_STATUS_KEYS:
            self.assertIn(key, status)
        c = status["connectors"]
        self.assertEqual(status["google_news_added"], c["google_news"]["new_confirmed"])
        self.assertEqual(status["publisher_feeds_added"], c["publisher_rss"]["new_confirmed"])
        self.assertEqual(status["gdelt_added"], c["gdelt"]["new_confirmed"])
        self.assertEqual(status["bing_and_alerts_added"],
                         c["bing"]["new_confirmed"] + c["google_alerts"]["new_confirmed"])
        self.assertEqual(status["bing_and_alerts_added"], 2)
        self.assertEqual(list(c), [key for key, _ in collector.CONNECTORS])
        self.assertEqual(status["schema_version"], 2)

    def test_run_section_and_github_run_url(self):
        status, _, _ = self.run_main()
        run = status["run"]
        self.assertIsNone(run["github_run_url"])
        self.assertLessEqual(run["started_utc"], run["finished_utc"])
        self.assertGreaterEqual(run["duration_seconds"], 0)
        env = {"GITHUB_SERVER_URL": "https://github.com", "GITHUB_REPOSITORY": "owner/repo",
               "GITHUB_RUN_ID": "42"}
        with patch.dict(os.environ, env):
            self.assertEqual(collector.github_run_url(), "https://github.com/owner/repo/actions/runs/42")

    def test_diagnostics_failure_does_not_change_results(self):
        gn = rss_item("Triada Robotics unveils BRAVO", "https://publisher.example/gn")
        kwargs = dict(fetch=lambda q, r: [gn], gdelt=lambda q: [gdelt_article()])
        _, normal_files, _ = self.run_main(**kwargs)

        def corrupt(self):
            self.data = None  # every diagnostics method now fails internally
            self.feeds = None

        with patch.object(collector.Diagnostics, "reset", corrupt):
            status, broken_files, _ = self.run_main(**kwargs)

        for name in ("mentions.json", "mentions_review.json", "mentions_public.json"):
            self.assertEqual(normal_files[name], broken_files[name], name)
        for key in OLD_STATUS_KEYS:
            self.assertIn(key, status)
        self.assertNotIn("connectors", status)  # diagnostics skipped, old keys intact


# ---------------------------------------------------------------------------
# Platform preservation, merging and duplicate handling
# ---------------------------------------------------------------------------

SOCIAL_PLATFORMS = ["Facebook", "Instagram", "X", "LinkedIn", "YouTube"]


def mention(title, source, platform="__missing__", **over):
    """An ordinary record, the kind the collector itself writes. `platform` is omitted unless given."""
    record = {
        "id": collector.article_id(title, source), "title": title, "source": source,
        "url": f"https://example.org/{collector.article_id(title, source)[:8]}",
        "published_at": "2026-10-01T09:00:00+00:00", "keyword": '"Triada Robotics"',
        "status": "relevant", "categories": ["Company"],
        "verification_method": "title", "matched_queries": ['"Triada Robotics"'],
    }
    if platform != "__missing__":
        record["platform"] = platform
    record.update(over)
    return record


# The ways hand-added records are marked, all of them seen in the real data.
MANUAL_KINDS = ["id", "verified", "notion", "keyword", "origin", "social"]
# Every verification method the collector itself writes.
COLLECTOR_METHODS = ["title", "article_body", "verified_publisher_title", "verified_web_article", "gdelt_title",
                     "gdelt_article_body", "publisher_feed_title", "publisher_feed_body", "publisher_article_body"]


def manual_mention(title, source, kind="id", **over):
    """A hand-added record. Only `kind` makes it recognisable as hand-added."""
    record = mention(title, source, **over)
    if kind == "id":          # hand-written id, e.g. manual-1-20260813
        record["id"] = "manual-" + collector.normalize(title).replace(" ", "-")[:40]
    elif kind == "verified":  # automatic id but verified by hand (13 of the real records)
        record["verification_method"] = "manual_publisher_review"
    elif kind == "notion":
        record["verification_method"] = "publisher_or_notion_source"
    elif kind == "keyword":
        record["keyword"] = "manual_research"
    elif kind == "origin":    # the explicit marker
        record["origin"] = "manual"
    elif kind == "social":
        record["platform"] = "LinkedIn"
    return record


class PlatformPreservationTests(unittest.TestCase):
    def setUp(self):
        collector.DIAG.reset()
        self.addCleanup(collector.DIAG.reset)
        quiet = contextlib.redirect_stdout(io.StringIO())  # keep CI logs free of collector output
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        env = patch.dict(os.environ, {"GOOGLE_ALERTS_RSS": ""})
        env.start()
        self.addCleanup(env.stop)

    def run_main(self, confirmed=(), review=(), public=None, fetch=None, feed=None,
                 gdelt=None, bing=None, article=None, publisher=None):
        """Run main() in a temporary folder seeded with the given files; network is mocked."""
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        original_dir = os.getcwd()
        os.chdir(tmp.name)
        self.addCleanup(os.chdir, original_dir)
        Path("mentions.json").write_text(json.dumps(list(confirmed)), encoding="utf-8")
        Path("mentions_review.json").write_text(json.dumps(list(review)), encoding="utf-8")
        if public is not None:
            Path("mentions_public.json").write_text(json.dumps(list(public)), encoding="utf-8")
        log = io.StringIO()
        with patch("collector.fetch", side_effect=fetch or (lambda q, r: [])), \
                patch("collector.fetch_publisher_feed", side_effect=feed or (lambda n, u: [])), \
                patch("collector.fetch_gdelt", side_effect=gdelt or (lambda q: [])), \
                patch("collector.fetch_bing_rss", side_effect=bing or (lambda q: [])), \
                patch("collector.read_article", side_effect=article or (lambda u: ("", ""))), \
                patch("collector.read_publisher", side_effect=publisher or (lambda u: ("", ""))), \
                patch("collector.resolve_google_news", side_effect=lambda url: url), \
                patch("collector.time.sleep"), \
                contextlib.redirect_stdout(log):
            collector.main()
        read = lambda name: json.loads(Path(name).read_text(encoding="utf-8"))
        return {"confirmed": read("mentions.json"), "review": read("mentions_review.json"),
                "public": read("mentions_public.json"), "status": read("collector_status.json"),
                "log": log.getvalue()}

    @staticmethod
    def without_platform(record):
        return {k: v for k, v in record.items() if k != "platform"}

    # ---- the platform default ---------------------------------------------

    def test_missing_or_blank_platform_becomes_media(self):
        for value in ("__missing__", None, "", "   "):
            with self.subTest(value=value):
                record = mention("T", "S", value)
                collector.ensure_platform(record)
                self.assertEqual(record["platform"], "Media")

    def test_explicit_platforms_are_never_changed(self):
        for value in ["Media", "media"] + SOCIAL_PLATFORMS + ["twitter", "X / Twitter", "Telegram", 5]:
            with self.subTest(value=value):
                record = mention("T", "S", value)
                collector.ensure_platform(record)
                self.assertEqual(record["platform"], value)  # not even an alias is rewritten

    def test_ensure_platform_touches_nothing_else(self):
        record = mention("T", "S")
        before = dict(record)
        collector.ensure_platform(record)
        self.assertEqual(self.without_platform(record), before)

    def test_every_manual_marker_protects_and_collector_records_do_not(self):
        for kind in MANUAL_KINDS:
            self.assertTrue(collector.is_protected(manual_mention("T", "S", kind)), kind)
        for value in ["twitter", "Telegram"] + SOCIAL_PLATFORMS:
            self.assertTrue(collector.is_protected(mention("T", "S", value)), value)
        for method in COLLECTOR_METHODS:
            self.assertFalse(collector.is_protected(mention("T", "S", verification_method=method)), method)
        for value in ("__missing__", None, "", " ", "Media", "media", "MEDIA"):
            self.assertFalse(collector.is_protected(mention("T", "S", value)), value)
        self.assertFalse(collector.is_protected(None))

    # ---- saving ------------------------------------------------------------

    def test_save_adds_platform_and_keeps_every_other_field(self):
        seed = [mention("A", "S1"), mention("B", "S2", "LinkedIn"),
                mention("C", "S3", None, published_at="2026-09-01T00:00:00+00:00")]
        originals = [dict(r) for r in seed]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            collector.save(path, {r["id"]: r for r in seed})
            saved = {r["id"]: r for r in json.loads(path.read_text(encoding="utf-8"))}
        self.assertEqual([saved[o["id"]]["platform"] for o in originals], ["Media", "LinkedIn", "Media"])
        for original in originals:
            self.assertEqual(self.without_platform(saved[original["id"]]), self.without_platform(original))

    def test_save_keeps_newest_first_order_and_tolerates_odd_dates(self):
        records = {r["id"]: r for r in [
            mention("old", "S", published_at="2026-01-01T00:00:00+00:00"),
            mention("new", "S", published_at="2026-10-01T00:00:00+00:00"),
            mention("none", "S", published_at=None), mention("gone", "S"), mention("num", "S", published_at=7)]}
        del records[mention("gone", "S")["id"]]["published_at"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "out.json"
            collector.save(path, records)  # must not raise
            titles = [r["title"] for r in json.loads(path.read_text(encoding="utf-8"))]
        self.assertLess(titles.index("new"), titles.index("old"))
        self.assertEqual(sorted(titles), ["gone", "new", "none", "num", "old"])

    # ---- identity ----------------------------------------------------------

    def test_record_key_is_unchanged_for_normal_records(self):
        record = mention("Some Title", "Some Source")
        self.assertEqual(collector.record_key(record), collector.article_id("Some Title", "Some Source"))
        self.assertEqual(collector.record_key({"title": "T", "source": ""}), collector.article_id("T", ""))

    def test_record_key_for_incomplete_hand_made_records(self):
        self.assertEqual(collector.record_key({"id": "manual-7", "url": "https://x.example"}), "manual-7")
        by_url = collector.record_key({"url": "https://x.example/post"})
        self.assertEqual(by_url, collector.record_key({"url": "https://x.example/post"}))
        self.assertTrue(by_url and by_url != collector.record_key({"url": "https://x.example/other"}))

    def test_automatic_ids_are_rebuilt_but_hand_written_ids_are_kept(self):
        key = collector.article_id("T", "S")
        for current in ("0" * 64, "", "   ", None):
            record = {"id": current}
            collector.rebuild_id(record, key)
            self.assertEqual(record["id"], key, current)
        for current in ("manual-1-20260813", "manual-what-battle-tested", 42):
            record = {"id": current}
            collector.rebuild_id(record, key)
            self.assertEqual(record["id"], current)
        record = {}
        collector.rebuild_id(record, key)
        self.assertEqual(record["id"], key)

    # ---- merging duplicates -------------------------------------------------

    def test_merge_keeps_first_record_and_fills_only_what_is_missing(self):
        kept = mention("T", "S", url="https://first.example", status="relevant", matched_queries=[])
        kept.pop("categories")
        other = mention("T", "S", url="https://second.example", status="review",
                        categories=["CEO"], matched_queries=["q"], note="extra")
        collector.merge_duplicate(kept, other)
        self.assertEqual((kept["url"], kept["status"]), ("https://first.example", "relevant"))
        self.assertEqual((kept["categories"], kept["matched_queries"], kept["note"]), (["CEO"], ["q"], "extra"))

    def test_merge_never_loses_a_social_platform(self):
        for first in ("__missing__", None, "", "Media"):
            kept = mention("T", "S", first)
            collector.merge_duplicate(kept, mention("T", "S", "LinkedIn"))
            self.assertEqual(kept["platform"], "LinkedIn", first)
        kept = mention("T", "S", "LinkedIn")
        collector.merge_duplicate(kept, mention("T", "S", "Media"))
        self.assertEqual(kept["platform"], "LinkedIn")
        kept = mention("T", "S", "LinkedIn")
        collector.merge_duplicate(kept, mention("T", "S", "X"))
        self.assertEqual(kept["platform"], "LinkedIn")  # two different social values: the first stays
        kept = mention("T", "S")
        collector.merge_duplicate(kept, mention("T", "S"))
        self.assertNotIn("platform", kept)  # nothing to merge: no platform invented here

    # ---- whole runs ----------------------------------------------------------

    def test_existing_media_mentions_become_media_and_nothing_else_changes(self):
        confirmed = [mention("Manual piece", "Мілітарний", id="manual-1-20260813", published_at="2026-08-13T12:00:00+00:00",
                             subject="Artem Veselov", source_type="publisher", first_seen_at="2026-10-21T10:00:00+00:00"),
                     mention("Collected piece", "Defender Media", published_at="2026-10-02T12:00:00+00:00",
                             legacy_ids=["a" * 64], urls=["https://a.example", "https://b.example"], keywords=["k1", "k2"],
                             products=["BRAVO"], matched_text="Triada Robotics", direct_url="https://direct.example/x")]
        review = [mention("Candidate", "Example Daily", status="review", categories=["CEO"],
                          published_at="2026-10-03T12:00:00+00:00")]
        out = self.run_main(confirmed=confirmed, review=review)
        for file, seed in (("confirmed", confirmed), ("review", review)):
            self.assertEqual([self.without_platform(r) for r in out[file]],
                             sorted(seed, key=lambda r: r["published_at"], reverse=True))
            self.assertTrue(all(r["platform"] == "Media" for r in out[file]))
        self.assertEqual(len(out["public"]), 3)
        self.assertTrue(all(r["platform"] == "Media" for r in out["public"]))
        self.assertEqual({r["title"] for r in out["public"]}, {"Manual piece", "Collected piece", "Candidate"})

    def test_hand_written_ids_survive_a_run(self):
        confirmed = [mention("Manual piece", "Мілітарний", id="manual-1-20260813")]
        out = self.run_main(confirmed=confirmed)
        self.assertEqual([r["id"] for r in out["confirmed"]], ["manual-1-20260813"])
        self.assertEqual([r["id"] for r in out["public"]], ["manual-1-20260813"])

    def test_a_second_run_changes_nothing(self):
        confirmed = [mention("A", "S1", "LinkedIn"), mention("B", "S2", id="manual-2"), mention("C", "S3", None)]
        first = self.run_main(confirmed=confirmed, review=[mention("D", "S4", status="review")])
        second = self.run_main(confirmed=first["confirmed"], review=first["review"], public=first["public"])
        for name in ("confirmed", "review", "public"):
            self.assertEqual(first[name], second[name], name)

    def test_explicit_social_platforms_survive_in_every_file(self):
        confirmed = [mention(f"{p} post", f"{p} account", p, url=f"https://social.example/{p}") for p in SOCIAL_PLATFORMS]
        confirmed.append(mention("Tweet", "x account", "twitter"))
        review = [mention("Unchecked post", "LinkedIn account", "LinkedIn", status="review")]
        out = self.run_main(confirmed=confirmed, review=review)
        by_title = {r["title"]: r for r in out["public"]}
        for p in SOCIAL_PLATFORMS:
            self.assertEqual(by_title[f"{p} post"]["platform"], p)
            self.assertEqual(by_title[f"{p} post"]["url"], f"https://social.example/{p}")
        self.assertEqual(by_title["Tweet"]["platform"], "twitter")  # preserved exactly as written
        self.assertEqual(by_title["Unchecked post"]["platform"], "LinkedIn")
        self.assertEqual([r["platform"] for r in out["review"]], ["LinkedIn"])
        self.assertEqual({r["platform"] for r in out["confirmed"]}, set(SOCIAL_PLATFORMS) | {"twitter"})

    def test_a_social_mention_in_review_is_not_verified_promoted_or_rewritten(self):
        record = mention("Triada Robotics unveils BRAVO", "LinkedIn account", "LinkedIn", status="review",
                         url="https://linkedin.example/post/1")
        record.pop("verification_method")
        article = lambda url: ("Triada Robotics said production grows.", "https://login-wall.example/redirect")
        out = self.run_main(review=[record], article=article)
        self.assertEqual(out["confirmed"], [])
        kept = out["review"][0]
        self.assertEqual((kept["url"], kept["status"], kept["platform"]),
                         ("https://linkedin.example/post/1", "review", "LinkedIn"))
        self.assertNotIn("verification_method", kept)
        self.assertEqual(self.without_platform(kept), self.without_platform(record))
        # control: the same record as a Media candidate IS verified and promoted
        media = dict(record, platform="Media", source="Example Daily")
        media["id"] = collector.article_id(media["title"], media["source"])
        out = self.run_main(review=[media], article=article)
        self.assertEqual(len(out["confirmed"]), 1)
        self.assertEqual(out["confirmed"][0]["url"], "https://login-wall.example/redirect")

    def _discovery_cannot_overwrite(self, **sources):
        """A social record sharing title+source with a discovered article is left alone."""
        for bucket in ("confirmed", "review"):
            with self.subTest(bucket=bucket):
                record = mention("Triada Robotics shows BRAVO", sources["source"], "LinkedIn",
                                 url="https://linkedin.example/post/9", status="relevant" if bucket == "confirmed" else "review")
                out = self.run_main(**{bucket: [record]}, **sources["mocks"])
                kept = [r for r in out["public"] if r["title"] == record["title"]]
                self.assertEqual(len(kept), 1)
                self.assertEqual(kept[0], dict(record))   # byte-for-byte what was stored
                if bucket == "review":
                    self.assertEqual(out["confirmed"], [])

    def test_publisher_feed_cannot_overwrite_a_social_record(self):
        feed_name = collector.PUBLISHER_FEEDS[0][0]
        entry = ("Triada Robotics shows BRAVO", "https://publisher.example/story", "2026-10-21T09:00:00+00:00",
                 feed_name, "<p>Triada Robotics is mentioned.</p>")
        self._discovery_cannot_overwrite(source=feed_name, mocks={
            "feed": lambda n, u: [entry] if n == feed_name else []})

    def test_gdelt_cannot_overwrite_a_social_record(self):
        article = {"title": "Triada Robotics shows BRAVO", "url": "https://gdelt.example/a", "domain": "gdelt.example",
                   "seendate": "20261022T110000Z"}
        self._discovery_cannot_overwrite(source="gdelt.example", mocks={"gdelt": lambda q: [article]})

    def test_bing_cannot_overwrite_a_social_record(self):
        item = ET.fromstring("<item><title>Triada Robotics shows BRAVO</title><link>https://bing.example/a</link></item>")
        self._discovery_cannot_overwrite(source="Bing Search", mocks={
            "bing": lambda q: [item], "publisher": lambda u: ("Triada Robotics is here", u)})

    def test_google_news_does_not_modify_or_duplicate_a_social_record(self):
        item = ET.fromstring(
            "<item><title>Triada Robotics shows BRAVO</title><link>https://news.example/a</link>"
            "<pubDate>Wed, 21 Oct 2026 09:00:00 GMT</pubDate><source>LinkedIn account</source></item>")
        record = mention("Triada Robotics shows BRAVO", "LinkedIn account", "LinkedIn", matched_queries=[])
        out = self.run_main(confirmed=[record], fetch=lambda q, r: [item])
        same = [r for r in out["public"] if r["title"] == record["title"]]
        self.assertEqual(same, [dict(record)])  # untouched: no matched_queries added, no second copy

    def test_the_same_article_found_again_is_not_duplicated_and_keeps_its_id(self):
        item = ET.fromstring(
            "<item><title>Design Written in Blood</title><link>https://news.example/a</link>"
            "<pubDate>Wed, 21 Oct 2026 09:00:00 GMT</pubDate><source>Мілітарний</source></item>")
        record = mention("Design Written in Blood", "Мілітарний", id="manual-1-20260813")
        out = self.run_main(confirmed=[record], fetch=lambda q, r: [item])
        same = [r for r in out["public"] if r["title"] == record["title"]]
        self.assertEqual(len(same), 1)
        self.assertEqual((same[0]["id"], same[0]["url"], same[0]["status"]), ("manual-1-20260813", record["url"], "relevant"))

    def test_incomplete_hand_made_records_do_not_break_the_run_or_the_feeds(self):
        odd = [{"id": "manual-no-title", "url": "https://linkedin.example/p/1", "platform": "LinkedIn"},
               {"title": "Post without a date", "source": "LinkedIn account", "platform": "LinkedIn",
                "url": "https://linkedin.example/p/2", "published_at": None}]
        out = self.run_main(confirmed=odd)
        ids = {r.get("id") for r in out["public"]}
        self.assertIn("manual-no-title", ids)
        self.assertEqual(len(out["public"]), 2)
        self.assertTrue(all(r["platform"] == "LinkedIn" for r in out["public"]))
        # the publisher feeds must not be reported as failing because of such a record
        self.assertEqual(out["status"]["connectors"]["publisher_rss"]["status"], "ok")

    # ---- duplicates -------------------------------------------------------------

    def test_duplicate_in_the_master_file_keeps_the_social_platform(self):
        first = mention("Same post", "Same source", url="https://first.example", status="relevant")
        second = mention("Same post", "Same source", "LinkedIn", url="https://second.example", note="from the copy")
        for order in ([first, second], [second, first]):
            out = self.run_main(confirmed=order)
            self.assertEqual(len(out["confirmed"]), 1)
            kept = out["confirmed"][0]
            self.assertEqual(kept["platform"], "LinkedIn")
            self.assertEqual(kept["url"], "https://second.example")   # the hand-added copy wins, in either order
            self.assertEqual(kept["note"], "from the copy")

    def test_a_hand_added_review_record_stays_in_the_review_file_even_if_confirmed_has_the_article(self):
        confirmed = mention("Same post", "Same source", url="https://confirmed.example")
        hand = manual_mention("Same post", "Same source", "social", status="review", url="https://hand.example/post")
        out = self.run_main(confirmed=[confirmed], review=[hand])
        self.assertEqual(out["confirmed"], [dict(confirmed, platform="Media")])
        self.assertEqual(out["review"], [dict(hand)])            # untouched, LinkedIn kept
        self.assertEqual([r["url"] for r in out["public"]], ["https://confirmed.example"])   # shown once

    def test_an_ordinary_review_duplicate_is_merged_into_the_confirmed_record(self):
        confirmed = mention("Same post", "Same source", url="https://confirmed.example")
        confirmed.pop("categories")
        review = mention("Same post", "Same source", status="review", url="https://review.example", categories=["CEO"])
        out = self.run_main(confirmed=[confirmed], review=[review])
        self.assertEqual(out["review"], [])
        kept = out["confirmed"][0]
        self.assertEqual((kept["url"], kept["status"], kept["categories"]), ("https://confirmed.example", "relevant", ["CEO"]))

    def test_duplicate_inside_the_review_file_keeps_the_social_platform(self):
        a = mention("Same post", "Same source", status="review")
        b = mention("Same post", "Same source", "Instagram", status="review")
        out = self.run_main(review=[a, b])
        self.assertEqual([r["platform"] for r in out["review"]], ["Instagram"])

    def test_two_different_social_platforms_on_one_article_keep_the_first(self):
        out = self.run_main(confirmed=[mention("Same post", "Same source", "X"), mention("Same post", "Same source", "LinkedIn")])
        self.assertEqual([r["platform"] for r in out["confirmed"]], ["X"])

    def test_different_articles_are_never_merged(self):
        out = self.run_main(confirmed=[mention("Post A", "Source", "LinkedIn"), mention("Post B", "Source", "Facebook"),
                                       mention("Post A", "Other source", None)])
        self.assertEqual(sorted((r["title"], r["source"], r["platform"]) for r in out["confirmed"]),
                         [("Post A", "Other source", "Media"), ("Post A", "Source", "LinkedIn"), ("Post B", "Source", "Facebook")])

    # ---- design that is relied on -----------------------------------------------

    def test_public_file_is_rebuilt_from_the_master_files_only(self):
        # Hand-added mentions belong in mentions.json (like the existing manual ones);
        # mentions_public.json is regenerated on every run.
        stale_public = [mention("Only in public", "Somewhere", "LinkedIn")]
        out = self.run_main(confirmed=[mention("Kept", "S")], public=stale_public)
        self.assertEqual([r["title"] for r in out["public"]], ["Kept"])

    def test_diagnostics_and_old_status_keys_are_unaffected(self):
        out = self.run_main(confirmed=[mention("A", "S", "LinkedIn")])
        for key in ("last_run_utc", "confirmed_total", "pending_internal", "google_news_added",
                    "publisher_feeds_added", "gdelt_added", "bing_and_alerts_added", "google_news_query_errors"):
            self.assertIn(key, out["status"])
        self.assertEqual(out["status"]["confirmed_total"], 1)
        self.assertEqual(len(out["status"]["connectors"]), 5)

    # ---- hand-added records are read-only -----------------------------------------------

    def _paths(self):
        """Every way the media pipeline could reach a record, each built to collide with title + source."""
        feed_name = collector.PUBLISHER_FEEDS[0][0]
        title = "Triada Robotics shows BRAVO"
        google = ET.fromstring(f"<item><title>{title}</title><link>https://news.example/a</link>"
                               "<pubDate>Wed, 21 Oct 2026 09:00:00 GMT</pubDate><source>Example Daily</source></item>")
        bing = ET.fromstring(f"<item><title>{title}</title><link>https://bing.example/a</link></item>")
        return {
            "re-check of unverified records": dict(source="Example Daily", promotes=True, mocks={
                "article": lambda u: ("Triada Robotics said so.", "https://redirect.example/x")}),
            "publisher feed": dict(source=feed_name, promotes=True, mocks={
                "feed": lambda n, u: [(title, "https://publisher.example/s", "2026-10-21T09:00:00+00:00", feed_name,
                                       "<p>Triada Robotics is mentioned.</p>")] if n == feed_name else []}),
            "GDELT": dict(source="gdelt.example", promotes=True, mocks={
                "gdelt": lambda q: [{"title": title, "url": "https://gdelt.example/a", "domain": "gdelt.example",
                                     "seendate": "20261022T110000Z"}]}),
            "Bing / Alerts": dict(source="Bing Search", promotes=True, mocks={
                "bing": lambda q: [bing], "publisher": lambda u: ("Triada Robotics is here", u)}),
            "Google News": dict(source="Example Daily", promotes=False, mocks={"fetch": lambda q, r: [google]}),
        }

    def test_hand_added_records_in_review_are_immune_to_every_discovery_path(self):
        title = "Triada Robotics shows BRAVO"
        for path, spec in self._paths().items():
            for kind in MANUAL_KINDS:
                with self.subTest(path=path, kind=kind):
                    record = manual_mention(title, spec["source"], kind, status="review", url="https://hand.example/post")
                    out = self.run_main(review=[record], **spec["mocks"])
                    self.assertEqual(out["confirmed"], [])
                    self.assertEqual(out["review"], [dict(record, platform=record.get("platform", "Media"))])
                    self.assertNotIn("WARNING", out["log"])  # prevented up front, not repaired afterwards

    def test_the_same_discoveries_do_change_ordinary_unverified_records(self):
        """Control for the test above: the guards apply to hand-added records only."""
        title = "Triada Robotics shows BRAVO"
        for path, spec in self._paths().items():
            with self.subTest(path=path):
                record = mention(title, spec["source"], status="review", url="https://candidate.example/post")
                out = self.run_main(review=[record], **spec["mocks"])
                if spec["promotes"]:
                    self.assertEqual((len(out["confirmed"]), out["review"]), (1, []))
                else:
                    self.assertNotEqual(out["review"][0]["matched_queries"], record["matched_queries"])

    def test_hand_verified_records_with_automatic_ids_are_never_touched(self):
        # like 13 of the real records: an automatic id, verified by hand, search phrases already recorded
        record = manual_mention("Cheap Is Not a Strategy", "Мілітарний", "verified", keyword='"Artem Veselov"',
                                matched_queries=['"Artem Veselov"'], categories=["CEO"])
        item = ET.fromstring("<item><title>Cheap Is Not a Strategy</title><link>https://news.example/a</link>"
                             "<pubDate>Wed, 21 Oct 2026 09:00:00 GMT</pubDate><source>Мілітарний</source></item>")
        out = self.run_main(confirmed=[record], fetch=lambda q, r: [item])
        self.assertEqual(out["confirmed"], [dict(record, platform="Media")])  # matched_queries not extended, no origin added
        self.assertNotIn("WARNING", out["log"])

    def test_records_without_an_id_are_marked_manual_and_stay_protected(self):
        title = "Triada Robotics shows BRAVO"
        bare = {"title": title, "source": "Example Daily", "url": "https://hand.example/p",
                "published_at": "2026-10-01T09:00:00+00:00", "status": "review", "categories": ["Company"]}
        first = self.run_main(review=[dict(bare)])
        kept = first["review"][0]
        self.assertEqual((kept["origin"], kept["platform"]), ("manual", "Media"))
        self.assertEqual(kept["id"], collector.article_id(title, "Example Daily"))
        # the id now looks automatic, but the marker keeps the record protected on every later run
        spec = self._paths()["re-check of unverified records"]
        second = self.run_main(review=first["review"], **spec["mocks"])
        self.assertEqual(second["confirmed"], [])
        self.assertEqual(second["review"], first["review"])

    def test_records_that_have_an_id_never_get_an_origin_added(self):
        out = self.run_main(confirmed=[manual_mention("A", "S", "id"), manual_mention("B", "S", "verified"), mention("C", "S")])
        self.assertTrue(all("origin" not in r for r in out["confirmed"]))

    def test_the_explicit_manual_marker_protects_an_otherwise_ordinary_record(self):
        record = mention("Triada Robotics shows BRAVO", "Example Daily", status="review", origin="manual")
        out = self.run_main(review=[record], **self._paths()["re-check of unverified records"]["mocks"])
        self.assertEqual((out["confirmed"], out["review"][0]["url"]), ([], record["url"]))

    # ---- duplicates and hand-added records -----------------------------------------------

    def test_a_hand_added_record_wins_over_an_automatic_duplicate(self):
        auto = mention("Same piece", "Same source", url="https://auto.example")
        hand = manual_mention("Same piece", "Same source", "id", url="https://hand.example", subject="Artem Veselov")
        for order in ([auto, hand], [hand, auto]):
            out = self.run_main(confirmed=order)
            self.assertEqual(len(out["confirmed"]), 1)
            kept = out["confirmed"][0]
            self.assertEqual((kept["url"], kept["id"], kept["subject"]), ("https://hand.example", hand["id"], "Artem Veselov"))

    def test_of_two_hand_added_copies_the_first_stays(self):
        one = manual_mention("Same piece", "Same source", "id", url="https://one.example")
        two = manual_mention("Same piece", "Same source", "verified", url="https://two.example")
        out = self.run_main(confirmed=[one, two])
        self.assertEqual([r["url"] for r in out["confirmed"]], ["https://one.example"])

    # ---- the safety net ----------------------------------------------------------------------

    def test_the_safety_net_restores_hand_added_records_if_any_step_alters_them(self):
        confirmed = [manual_mention(f"Post {i}", "Source", kind) for i, kind in enumerate(("id", "verified", "social"))]
        review = [manual_mention("Unchecked 1", "Source", "notion", status="review"),
                  manual_mention("Unchecked 2", "Source", "origin", status="review")]
        ordinary = mention("Ordinary piece", "Source")
        calls = []

        def buggy(confirmed_, pending_, entries):   # a collection step gone wrong, once
            if calls:
                return 0
            calls.append(1)
            keys = [k for k, r in confirmed_.items() if r["title"].startswith("Post")]
            confirmed_[keys[0]]["url"] = "https://wrong.example"
            confirmed_[keys[0]]["status"] = "review"
            del confirmed_[keys[1]]
            confirmed_[keys[2]] = {"id": keys[2], "title": "replaced", "source": "Source"}
            first, second = list(pending_)[:2]
            confirmed_[first] = pending_.pop(first)          # promoted out of the review file
            pending_[second]["subject"] = "changed"
            return 0

        with patch("collector.match_publisher_feed", side_effect=buggy):
            out = self.run_main(confirmed=confirmed + [ordinary], review=review)
        self.assertEqual(calls, [1])
        expected = lambda records: {r["title"]: dict(r, platform=r.get("platform", "Media")) for r in records}
        self.assertEqual({r["title"]: r for r in out["confirmed"]}, expected(confirmed + [ordinary]))
        self.assertEqual({r["title"]: r for r in out["review"]}, expected(review))
        self.assertEqual({r["title"]: r for r in out["public"]}, expected(confirmed + [ordinary] + review))
        self.assertEqual(out["log"].count("WARNING: restored a hand-added record"), 5)
        for title in ("Post 0", "Post 1", "Post 2", "Unchecked 1", "Unchecked 2"):
            self.assertIn(title, out["log"])

    def test_a_normal_run_never_needs_the_safety_net(self):
        confirmed = [manual_mention(f"Post {k}", "Source", k) for k in MANUAL_KINDS]
        review = [manual_mention(f"Unchecked {k}", "Source", k, status="review") for k in MANUAL_KINDS]
        out = self.run_main(confirmed=confirmed, review=review)
        self.assertNotIn("WARNING", out["log"])

    def test_a_second_run_changes_nothing_for_hand_added_records(self):
        confirmed = [manual_mention(f"Post {k}", "Source", k) for k in MANUAL_KINDS]
        review = [manual_mention(f"Unchecked {k}", "Source", k, status="review") for k in MANUAL_KINDS]
        first = self.run_main(confirmed=confirmed, review=review)
        second = self.run_main(confirmed=first["confirmed"], review=first["review"])
        for name in ("confirmed", "review", "public"):
            self.assertEqual(first[name], second[name], name)
        self.assertEqual({r["title"]: self.without_platform(r) for r in first["confirmed"]},
                         {r["title"]: self.without_platform(r) for r in confirmed})   # only the platform default was added

    def test_hand_added_records_survive_when_every_source_fails(self):
        def down(*args, **kwargs):
            raise OSError("down")
        confirmed = [manual_mention(f"Post {k}", "Source", k) for k in MANUAL_KINDS]
        review = [manual_mention(f"Unchecked {k}", "Source", k, status="review") for k in MANUAL_KINDS]
        out = self.run_main(confirmed=confirmed, review=review, fetch=down, feed=down, gdelt=down, bing=down)
        self.assertEqual({r["title"] for r in out["confirmed"]}, {r["title"] for r in confirmed})
        self.assertEqual({r["title"] for r in out["review"]}, {r["title"] for r in review})
        self.assertEqual(out["status"]["connectors"]["google_news"]["status"], "error")


if __name__ == "__main__":
    unittest.main()
