import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import collector
from tools import browser_monitoring as b, monitoring as m


class BrowserImportTests(unittest.TestCase):
    def setUp(self):
        self.r = dict(url="https://x.com/Independent/status/123456", source="Independent", author_url="https://x.com/Independent",
                      published_at="2026-08-04", post_text="Triada Robotics builds Bravo", verification_method="native_browser_post")
        self.r["evidence"] = dict(post_url=self.r["url"], author_url=self.r["author_url"], publication_date=self.r["published_at"],
                                  text_complete=True, original_language=True, public_post=True, observed_at="2026-10-30")
    def run_import(self, records, c=None, p=None, known=()):
        c = {} if c is None else c; p = {} if p is None else p
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"browser.json"; path.write_text(json.dumps(records))
            result=b.collect(c,p,collector.classify,path,known)
        return result,c,p
    def test_verified_import_and_repeat(self):
        result,c,p=self.run_import([self.r]); self.assertEqual(result["imported"],1)
        self.assertEqual(self.run_import([self.r],c,p)[0]["already_known"],1)
        m.enforce_scope(c,p,collector.is_protected); self.assertEqual(len(c),1)
    def test_incomplete_or_translation_only_is_held(self):
        for field in ["text_complete","original_language","public_post","observed_at"]:
            r=copy.deepcopy(self.r); r["evidence"].pop(field)
            result,c,p=self.run_import([r]); self.assertFalse(c); self.assertEqual(len(p),1)
    def test_unrelated_post_and_missing_date_are_held(self):
        for field,value in [("post_text","Different company"),("published_at","")]:
            r=copy.deepcopy(self.r);r[field]=value
            self.assertFalse(self.run_import([r])[1])
    def test_owned_and_profile_links_are_rejected(self):
        for url in ["https://x.com/TriadaRobotics/status/42","https://x.com/Independent"]:
            r=copy.deepcopy(self.r);r["url"]=url
            result,c,p=self.run_import([r]);self.assertEqual(result["rejected"],1);self.assertFalse(c)
    def test_curated_alias_is_not_duplicated(self):
        result,c,p=self.run_import([self.r],known=[dict(url="https://twitter.com/OldName/status/123456")])
        self.assertEqual(result["already_known"],1);self.assertFalse(c)
    def test_same_text_different_posts_remain_separate(self):
        r=copy.deepcopy(self.r);r["url"]=r["url"].replace("123456","123457");r["evidence"]["post_url"]=r["url"]
        self.assertEqual(len(self.run_import([self.r,r])[1]),2)
    def test_future_and_preperiod_dates_are_held(self):
        for day in ["2026-01-01","2999-01-01"]:
            r=copy.deepcopy(self.r);r["published_at"]=r["evidence"]["publication_date"]=day
            self.assertFalse(self.run_import([r])[1])
    def test_mismatched_post_evidence_is_held(self):
        r=copy.deepcopy(self.r);r["evidence"]["post_url"]="https://x.com/Other/status/42"
        self.assertFalse(self.run_import([r])[1])

if __name__ == '__main__': unittest.main()
