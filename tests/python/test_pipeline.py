"""Tests for the page-build pipeline: converting collector records, choosing what appears, the build guards, and the workflows.
Run from the repository root:   python -m unittest discover -s tests/python -v      (pyyaml and beautifulsoup4 are optional: those tests skip)"""
import calendar, copy, json, re, sys, tempfile, unittest
from html.parser import HTMLParser
from pathlib import Path
sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT / "tools"))
import build_tracker as B
from tracker_records import library_records, to_tracker_record
import review
try:
    import yaml
except ImportError:
    yaml = None
try:
    from bs4 import BeautifulSoup, NavigableString
except ImportError:
    BeautifulSoup = None
FIX = ROOT / "tests" / "fixtures"
TPL = (ROOT / "tracker" / "tracker.template.html").read_text(encoding="utf-8")
CURATED = json.loads((ROOT / "data" / "mentions_curated.json").read_text(encoding="utf-8"))
EXCL = json.loads((ROOT / "data" / "exclusions.json").read_text(encoding="utf-8"))
APPROVED = (FIX / "approved_311_page.html").read_text(encoding="utf-8")


def found(title, source, url, published="2026-10-21T16:38:20+00:00", **over):
    r = {"id": "f" * 64, "title": title, "source": source, "url": url, "published_at": published, "keyword": '"Triada Robotics"',
         "status": "relevant", "categories": ["Company"], "verification_method": "title", "matched_queries": ['"Triada Robotics"'], "platform": "Media"}
    r.update(over)
    return r


def outside_rows(h):
    a = re.search(r"<tr\b[^>]*\bdata-ts=", h).start(); b = h.rindex("</tr>", 0, h.index("</tbody>", a)) + 5
    return h[:a] + "<!--ROWS-->" + h[b:]


class Rows(HTMLParser):
    def __init__(self): super().__init__(); self.rows, self._cur = [], None
    def handle_starttag(self, tag, attrs):
        if tag == "tr": self._cur = {"attrs": dict(attrs), "cells": []}; self.rows.append(self._cur)
        elif tag == "td" and self._cur is not None: self._cur["cells"].append("")
    def handle_data(self, d):
        if self._cur is not None and self._cur["cells"]: self._cur["cells"][-1] += d
    def handle_endtag(self, tag):
        if tag == "tr": self._cur = None


def parse_rows(page):
    p = Rows(); p.feed(page[page.index("<tbody"):page.index("</tbody>")]); return p.rows


class ConverterTests(unittest.TestCase):
    def test_a_collector_record_becomes_a_tracker_record(self):
        out = to_tracker_record(found("Cheap Is Not a Strategy - Мілітарний", "Мілітарний", "https://m.example/a", categories=["Company", "CEO"]))
        ts = calendar.timegm((2026, 10, 21, 16, 38, 20)) * 1000
        self.assertEqual(out["tracker"], {"ts": ts, "day": "2026-10-21", "date_label": "21 Oct 2026", "company": True, "ceo": True, "import_source": "collector"})
        self.assertEqual(out["title"], "Cheap Is Not a Strategy - Мілітарний")

    def test_september_is_written_sept_like_the_library_does_and_days_are_two_digits(self):
        self.assertEqual(to_tracker_record(found("t", "s", "https://x.example/1", "2026-09-05T08:00:00+00:00"))["tracker"]["date_label"], "05 Sept 2026")

    def test_z_suffix_offsets_and_naive_dates_are_read_as_utc(self):
        for value, day in (("2026-10-21T23:30:00Z", "2026-10-21"), ("2026-10-22T01:30:00+02:00", "2026-10-21"), ("2026-10-21T10:00:00", "2026-10-21")):
            self.assertEqual(to_tracker_record(found("t", "s", "https://x.example/1", value))["tracker"]["day"], day, value)

    def test_categories_become_the_subject_flags_and_nothing_is_invented(self):
        for cats, flags in ((["Company"], (True, False)), (["CEO"], (False, True)), (["Company", "Products"], (True, False)), ([], (False, False))):
            t = to_tracker_record(found("t", "s", "https://x.example/1", categories=cats))["tracker"]
            self.assertEqual((t["company"], t["ceo"]), flags, cats)

    def test_a_record_without_a_platform_is_media(self):
        r = found("t", "s", "https://x.example/1"); del r["platform"]
        self.assertEqual(to_tracker_record(r)["platform"], "Media")

    def test_a_curated_record_is_used_exactly_as_it_is(self):
        self.assertIs(to_tracker_record(CURATED[0]), CURATED[0])

    def test_an_unreadable_date_is_refused(self):
        with self.assertRaises(ValueError): to_tracker_record(found("t", "s", "https://x.example/1", "yesterday"))


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.curated = [copy.deepcopy(c) for c in CURATED[:3]]

    def test_relevant_collector_records_appear_and_review_candidates_are_held_back(self):
        a, b = found("New verified article", "Pub", "https://p.example/a"), found("A candidate", "Pub", "https://p.example/b", status="review", id="e" * 64)
        out, rep = library_records(self.curated + [a, b], EXCL)
        self.assertEqual([r["title"] for r in out][-1], "New verified article"); self.assertEqual((rep["collector_shown"], rep["review_held_back"]), (1, 1))
        out2, _ = library_records(self.curated + [a, b], EXCL, include_review=True); self.assertEqual(len(out2), 5)

    def test_excluded_records_never_appear_even_if_the_public_file_lists_them(self):
        e = next(x for x in EXCL["entries"] if x["collector_ids"])
        bad = found(e["collector_title"], e["source"], e["urls"][-1], id=e["collector_ids"][0])
        out, rep = library_records(self.curated + [bad], EXCL); self.assertEqual((len(out), rep["excluded"]), (3, 1))

    def test_a_copy_of_a_curated_article_under_another_link_is_hidden_but_distinct_posts_are_not(self):
        c = [{"id": "manual-1", "title": "Same Story", "source": "Pub", "url": "https://p.example/s", "published_at": "2026-08-02T12:00:00+00:00", "platform": "Media", "origin": "manual",
              "tracker": {"ts": 1, "day": "2026-08-02", "date_label": "02 Aug 2026", "company": True, "ceo": False}},
             {"id": "hist-tg", "title": "Identical text", "source": "Chan", "url": "https://t.me/c/1", "published_at": "2026-08-02T12:00:00+00:00", "platform": "Telegram", "origin": "manual",
              "tracker": {"ts": 1, "day": "2026-08-02", "date_label": "02 Aug 2026", "company": False, "ceo": False}}]
        copy_ = found("Same Story - Pub", "Pub", "https://news.google.com/rss/articles/CBM1?oc=5", id="1" * 64)
        same_text_media = found("Identical text", "Chan", "https://n.example/other", id="2" * 64)
        out, rep = library_records(c + [copy_, same_text_media], EXCL)
        self.assertEqual(rep["duplicates_hidden"], 1); self.assertEqual(len(out), 3)
        self.assertIn("Identical text", [r["title"] for r in out if r["platform"] == "Media"])

    def test_a_hand_added_record_without_a_tracker_block_is_converted_and_shown(self):
        orphan = found("Hand-added LinkedIn post", "LinkedIn", "https://www.linkedin.com/posts/x", platform="LinkedIn", origin="manual", status="review", id="manual-9")
        out, rep = library_records(self.curated + [orphan], EXCL)
        self.assertEqual(out[-1]["platform"], "LinkedIn"); self.assertEqual(rep["curated_or_protected"], 4)

    def test_invalid_records_are_skipped_and_reported_not_fatal(self):
        out, rep = library_records(self.curated + [found("", "s", "https://x.example/1"), {"id": "x"}, found("t", "s", "https://x.example/2", "not a date")], EXCL)
        self.assertEqual((len(out), len(rep["skipped_invalid"])), (3, 3))

    def test_a_platform_the_library_does_not_list_is_reported(self):
        out, rep = library_records(self.curated + [found("t", "s", "https://x.example/9", platform="Mastodon", origin="manual", id="manual-m")], EXCL)
        self.assertEqual(rep["unknown_platform"], ["Mastodon"])

    def test_the_order_of_the_public_file_is_kept(self):
        out, _ = library_records(CURATED, EXCL); self.assertEqual([r["id"] for r in out], [c["id"] for c in CURATED])


class BuildTests(unittest.TestCase):
    def build(self, public, extra=(), **kw):
        d = tempfile.mkdtemp(); self.addCleanup(lambda: __import__("shutil").rmtree(d, ignore_errors=True))
        Path(d, "public.json").write_text(json.dumps(public, ensure_ascii=False), encoding="utf-8")
        args = ["--template", str(ROOT / "tracker/tracker.template.html"), "--data", f"{d}/public.json", "--out", f"{d}/p/index.html", "--report", f"{d}/r.json", "--emit-records", f"{d}/rec.json", *extra]
        B.main(args)
        return Path(d, "p/index.html").read_text(encoding="utf-8"), json.loads(Path(d, "r.json").read_text()), json.loads(Path(d, "rec.json").read_text())

    def test_the_curated_mentions_alone_rebuild_the_approved_page(self):
        page, rep, _ = self.build(CURATED)
        self.assertEqual(outside_rows(page), outside_rows(APPROVED))                     # design, styles, scripts: byte-identical
        self.assertEqual(len(parse_rows(page)), 311)

    @unittest.skipIf(BeautifulSoup is None, "needs beautifulsoup4")
    def test_every_row_equals_the_approved_row(self):
        def norm(el):
            if isinstance(el, NavigableString): s = str(el); return s if s.strip() else None
            return (el.name, tuple(sorted(((k, tuple(v) if isinstance(v, list) else v) for k, v in el.attrs.items()), key=lambda kv: kv[0])), tuple(x for x in (norm(c) for c in el.children) if x is not None))
        rows = lambda h: [norm(t) for t in BeautifulSoup(h, "html.parser").select("table.tmi-table tbody tr")]
        self.assertEqual(rows(self.build(CURATED)[0]), rows(APPROVED))

    def test_a_new_collector_record_becomes_a_row_with_the_right_data(self):
        new = found("Brand new verified article", "Defense Express", "https://de.example/new", "2026-10-29T08:15:00+00:00", categories=["Company", "CEO"])
        page, rep, recs = self.build(CURATED + [new], ["--exclusions", str(ROOT / "data/exclusions.json")])
        rows = parse_rows(page); self.assertEqual(len(rows), 312)
        row = next(r for r in rows if r["cells"][3] == "Brand new verified article")
        self.assertEqual(row["attrs"]["data-day"], "2026-10-29"); self.assertEqual(row["attrs"]["data-platform"], "Media")
        self.assertEqual((row["attrs"]["data-company"], row["attrs"]["data-ceo"]), ("1", "1")); self.assertEqual(row["cells"][0], "29 Oct 2026")
        self.assertEqual(row["attrs"]["data-ts"], str(calendar.timegm((2026, 10, 29, 8, 15, 0)) * 1000)); self.assertEqual(row["attrs"]["data-origin"], "collector")
        self.assertEqual(page.count('<span data-count="shown">312</span> of 312 mentions'), 2)         # the Library's counters follow
        self.assertEqual(len(recs), 312)

    def test_the_build_stops_if_a_curated_mention_is_missing(self):
        d = tempfile.mkdtemp(); Path(d, "p.json").write_text(json.dumps(CURATED[:-1]))
        with self.assertRaises(SystemExit) as c:
            B.main(["--template", str(ROOT / "tracker/tracker.template.html"), "--data", f"{d}/p.json", "--curated", str(ROOT / "data/mentions_curated.json"), "--out", f"{d}/o.html"])
        self.assertIn("BUILD STOPPED", str(c.exception)); self.assertFalse(Path(d, "o.html").exists())

    def test_the_build_refuses_to_publish_an_empty_page(self):
        d = tempfile.mkdtemp(); Path(d, "p.json").write_text(json.dumps([found("t", "s", "https://x.example/1", status="review")]))
        with self.assertRaises(SystemExit): B.main(["--template", str(ROOT / "tracker/tracker.template.html"), "--data", f"{d}/p.json", "--out", f"{d}/o.html"])

    def test_the_library_and_the_overview_read_the_same_rows_and_nothing_else(self):
        """Both pages take their records from the table rows only, so one database cannot give two answers."""
        self.assertEqual(TPL.count("<tbody><!--TMI:ROWS-->"), 1)     # the page's one data table holds the rows (the other <tbody> strings are the calendar widget's)
        self.assertNotIn("fetch(", TPL); self.assertNotIn("XMLHttpRequest", TPL)    # neither page can load a second data source
        self.assertGreaterEqual(TPL.count('.tmi-table tbody tr'), 2)                                    # the Library's script and the Overview's script
        self.assertIn("var ROWS = $$(\".tmi-table tbody tr\")", TPL)


def flag_for(record, curated_twin):
    side = lambda r, c: {"id": r["id"], "title": r["title"], "source": r["source"], "platform": "Media", "published_at": r["published_at"], "curated": c, "excluded_by": None}
    return {"schema_version": 1, "flags": [{"type": "possible_duplicate", "link": "x.example/a", "records": [side(record, False), side(curated_twin, True)]}]}


class HoldForReviewTests(unittest.TestCase):
    """A new mention flagged as a possible duplicate of a curated mention is held: not in the Mentions Library, not in the Overview."""
    def setUp(self):
        self.curated = [copy.deepcopy(c) for c in CURATED[:3]]
        self.twin = self.curated[0]
        self.rec = found("Artem Veselov, Triada Robotics: the market is moving", "Militär Aktuell", "https://news.google.com/rss/articles/CBMx?oc=5", id="a" * 64)
        self.flags = flag_for(self.rec, self.twin)

    def test_a_flagged_record_is_held_and_listed_in_the_report(self):
        out, rep = library_records(self.curated + [self.rec], EXCL, flags=self.flags)
        self.assertEqual(len(out), 3); self.assertEqual(rep["held_for_review"][0]["id"], self.rec["id"])
        self.assertEqual(rep["held_for_review"][0]["possible_duplicate_of"], [self.twin["title"]])

    def test_an_unflagged_record_is_still_shown(self):
        other = found("Another verified article", "Pub", "https://p.example/other", id="b" * 64)
        out, rep = library_records(self.curated + [self.rec, other], EXCL, flags=self.flags)
        self.assertEqual([r["title"] for r in out][-1], "Another verified article"); self.assertEqual(len(rep["held_for_review"]), 1)

    def test_not_duplicate_releases_it_and_duplicate_keeps_it_hidden(self):
        out, rep = library_records(self.curated + [self.rec], EXCL, flags=self.flags, decisions={self.rec["id"]: "not_duplicate"})
        self.assertEqual((len(out), rep["held_for_review"]), (4, []))
        out, rep = library_records(self.curated + [self.rec], EXCL, flags=self.flags, decisions={self.rec["id"]: "duplicate"})
        self.assertEqual((len(out), rep["confirmed_duplicates_hidden"]), (3, 1))

    def test_curated_mentions_are_never_held_even_if_a_flag_names_one_wrongly(self):
        wrong = flag_for(self.twin, self.curated[1]); wrong["flags"][0]["records"][0]["curated"] = False
        out, rep = library_records(self.curated, EXCL, flags=wrong)
        self.assertEqual((len(out), rep["held_for_review"]), (3, []))

    def test_including_review_candidates_does_not_override_the_hold(self):
        cand = dict(self.rec, status="review")
        out, rep = library_records(self.curated + [cand], EXCL, include_review=True, flags=flag_for(cand, self.twin))
        self.assertEqual((len(out), len(rep["held_for_review"])), (3, 1))

    def test_the_build_stops_if_the_flags_file_is_missing(self):
        d = tempfile.mkdtemp(); Path(d, "p.json").write_text(json.dumps(CURATED + [self.rec]))
        with self.assertRaises(SystemExit) as c:
            B.main(["--template", str(ROOT / "tracker/tracker.template.html"), "--data", f"{d}/p.json", "--flags", f"{d}/missing.json", "--out", f"{d}/o.html"])
        self.assertIn("BUILD STOPPED", str(c.exception)); self.assertFalse(Path(d, "o.html").exists())

    def test_the_built_page_does_not_contain_a_held_record_and_does_after_release(self):
        d = tempfile.mkdtemp(); Path(d, "p.json").write_text(json.dumps(CURATED + [self.rec])); Path(d, "f.json").write_text(json.dumps(self.flags))
        run = lambda: (B.main(["--template", str(ROOT / "tracker/tracker.template.html"), "--data", f"{d}/p.json", "--flags", f"{d}/f.json", "--decisions", f"{d}/d.json", "--out", f"{d}/o.html", "--report", f"{d}/r.json"]), Path(d, "o.html").read_text(encoding="utf-8"))[1]
        Path(d, "d.json").write_text(json.dumps({"decisions": []})); page = run()
        self.assertNotIn(self.rec["title"], page); self.assertEqual(len(parse_rows(page)), 311)
        Path(d, "d.json").write_text(json.dumps({"decisions": [{"id": self.rec["id"], "decision": "not_duplicate"}]})); page = run()
        self.assertIn(self.rec["title"], page); self.assertEqual(len(parse_rows(page)), 312)
        self.assertEqual(page.count('<span data-count="shown">312</span> of 312 mentions'), 2)


class ReviewToolTests(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp()); self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))
        self.rec = found("The held article", "Militär Aktuell", "https://x.example/h", id="c" * 64); self.flags = self.d / "flags.json"; self.dec = self.d / "sub" / "decisions.json"
        self.flags.write_text(json.dumps(flag_for(self.rec, CURATED[0])))
        self.args = ["--flags", str(self.flags), "--decisions", str(self.dec)]

    def run_tool(self, *a):
        import io, contextlib
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = review.main(self.args + list(a))
        return code, out.getvalue(), err.getvalue()

    def test_list_shows_the_held_record_and_the_curated_mention_it_may_duplicate(self):
        code, out, _ = self.run_tool("list")
        self.assertEqual(code, 0); self.assertIn("1 mention(s) held for manual review", out); self.assertIn(self.rec["id"], out); self.assertIn(CURATED[0]["title"], out)

    def test_decide_saves_the_answer_and_the_record_is_no_longer_listed(self):
        code, out, _ = self.run_tool("decide", self.rec["id"], "not_duplicate", "--note", "different article")
        data = json.loads(self.dec.read_text(encoding="utf-8")); self.assertEqual(code, 0); self.assertIn("SHOWN", out)
        self.assertEqual([(d["id"], d["decision"], d["note"]) for d in data["decisions"]], [(self.rec["id"], "not_duplicate", "different article")])
        self.assertIn("0 mention(s) held", self.run_tool("list")[1])

    def test_a_changed_answer_replaces_the_old_one(self):
        self.run_tool("decide", self.rec["id"], "not_duplicate"); self.run_tool("decide", self.rec["id"], "duplicate")
        self.assertEqual([d["decision"] for d in json.loads(self.dec.read_text(encoding="utf-8"))["decisions"]], ["duplicate"])

    def test_an_answer_can_be_changed_after_the_collector_has_cleared_the_flag(self):
        self.run_tool("decide", self.rec["id"], "not_duplicate")
        self.flags.write_text(json.dumps({"schema_version": 1, "flags": []}))             # the next collector run no longer flags a decided record
        code, out, _ = self.run_tool("decide", self.rec["id"], "duplicate")
        self.assertEqual(code, 0); self.assertIn("hidden", out)
        self.assertEqual([d["decision"] for d in json.loads(self.dec.read_text(encoding="utf-8"))["decisions"]], ["duplicate"])

    def test_an_id_that_is_not_held_is_refused_and_nothing_is_written(self):
        code, _, err = self.run_tool("decide", "not-a-held-record", "duplicate")
        self.assertEqual(code, 2); self.assertIn("is not a record held for review", err); self.assertFalse(self.dec.exists())

    def test_only_the_two_valid_answers_are_accepted(self):
        with self.assertRaises(SystemExit): self.run_tool("decide", self.rec["id"], "maybe")


@unittest.skipIf(yaml is None, "needs pyyaml")
class WorkflowTests(unittest.TestCase):
    """Understands the two-step rollout: in step 1 the collector workflow only protects the data; in step 2 the deploy is chained."""
    def load(self, name):
        doc = yaml.safe_load((ROOT / ".github/workflows" / name).read_text(encoding="utf-8")); doc["_on"] = doc.get(True) or doc.get("on"); return doc

    def test_the_collector_workflow_protects_the_data_in_every_stage(self):
        w = self.load("main.yml"); steps = {s["name"]: s for s in w["jobs"]["search-mentions"]["steps"]}
        self.assertEqual(steps["Run media monitoring"]["env"]["TRIADA_REQUIRE_CURATED"], "1")
        self.assertIn("test_collector", steps["Test collector and tracker tools"]["run"]); self.assertIn("tests/python", steps["Test collector and tracker tools"]["run"])
        self.assertIn("mentions_flags.json", steps["Save mentions database"]["run"])
        for p in ("data/exclusions.json", "data/mentions_curated.json", "data/review_decisions.json", "collector.py"): self.assertIn(p, w["_on"]["push"]["paths"])

    def test_if_the_deploy_is_chained_the_called_workflow_is_reusable_and_gets_the_right_permissions(self):
        w = self.load("main.yml"); deploy = w["jobs"].get("deploy-site")
        if deploy is None: self.skipTest("site publishing is not switched on in this stage")
        self.assertEqual(deploy["needs"], "search-mentions"); self.assertEqual(deploy["uses"], "./.github/workflows/dashboard-deploy.yml")
        self.assertEqual(deploy["permissions"], {"contents": "read", "pages": "write", "id-token": "write"})
        self.assertIn("workflow_call", self.load("dashboard-deploy.yml")["_on"])             # otherwise GitHub refuses to start the collector workflow at all

    def test_if_the_site_is_built_it_is_built_from_the_latest_data_with_every_safeguard(self):
        w = self.load("dashboard-deploy.yml"); build = w["jobs"]["build"]["steps"]; text = json.dumps(build)
        if "tools/build_tracker.py" not in text: self.skipTest("the original deploy workflow (stage 1)")
        self.assertEqual(build[0]["with"]["ref"], "main")
        for needle in ("--curated data/mentions_curated.json", "--exclusions data/exclusions.json", "--flags mentions_flags.json", "--decisions data/review_decisions.json", "site/tracker/index.html", "cp -r frontend/dist/. site/"): self.assertIn(needle, text)
        upload = next(s for s in build if s.get("uses", "").startswith("actions/upload-pages-artifact")); self.assertEqual(upload["with"]["path"], "site")

    def test_every_script_the_workflows_call_exists(self):
        for f in (ROOT / ".github/workflows").glob("*.yml"):
            for m in re.findall(r"(?:python|node) ((?:tools|tests)/[\w./-]+\.py)", f.read_text(encoding="utf-8")): self.assertTrue((ROOT / m).exists(), (f.name, m))
        for rel in ("collector.py", "tracker/tracker.template.html", "data/mentions_curated.json", "data/exclusions.json", "data/review_decisions.json"): self.assertTrue((ROOT / rel).exists(), rel)

if __name__ == "__main__":
    unittest.main()
