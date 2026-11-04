import io
import json
import unittest
from urllib.error import HTTPError
from urllib.parse import parse_qs,urlsplit
from tools.google_news import fetch_items
from tools.publish_tracker import publish,NoRedirect

class FreePipelineTests(unittest.TestCase):
    def test_news_recovers_503_and_includes_april_history(self):
        requests=[];delays=[]
        def open_(req,timeout):
            requests.append(req)
            if len(requests)==1:raise HTTPError(req.full_url,503,'temporary',{},None)
            return io.BytesIO(b'<rss><channel><item><title>Triada Robotics</title></item></channel></rss>')
        self.assertEqual(len(fetch_items('"Triada Robotics"',('en','US','US:en'),'2026-04-23',opener=open_,sleep=delays.append)),1)
        params=parse_qs(urlsplit(requests[0].full_url).query)
        self.assertIn('after:2026-04-22',params['q'][0]);self.assertEqual(params['hl'],['en-US']);self.assertEqual(delays,[2])
    def test_news_retry_is_bounded_and_respects_retry_after(self):
        calls=[];delays=[]
        def open_(req,timeout):
            calls.append(req);raise HTTPError(req.full_url,429,'limited',{'Retry-After':'9999'},None)
        with self.assertRaises(HTTPError):fetch_items('Triada',('uk','UA','UA:uk'),'2026-04-23',opener=open_,sleep=delays.append)
        self.assertEqual(len(calls),3);self.assertEqual(delays,[30,30])
    def test_news_does_not_retry_permanent_access_errors(self):
        def open_(req,timeout):raise HTTPError(req.full_url,403,'denied',{},None)
        with self.assertRaises(HTTPError):fetch_items('Triada',('uk','UA','UA:uk'),'2026-04-23',opener=open_,sleep=lambda _:self.fail('must not retry403'))
    def test_news_does_not_treat_html_error_page_as_empty_success(self):
        with self.assertRaises(ValueError):fetch_items('Triada',('uk','UA','UA:uk'),'2026-04-23',opener=lambda *a,**k:io.BytesIO(b'<html/>'))
    def test_portal_checks_exact_approved_rows_and_keeps_token_out_of_payload(self):
        record={'id':'one','title':'TRIADA','source':'source','url':'https://example.org','platform':'Media'}
        def open_(req,timeout):
            data=json.loads(req.data);self.assertEqual(data['records'],[record]);self.assertNotIn('test-secret',req.data.decode());self.assertEqual(req.get_header('Authorization'),'Bearer test-secret')
            return io.BytesIO(b'{"updated":true,"total":1}')
        self.assertEqual(publish('<!doctype html><tr data-ts="1">',[record],'test-secret',opener=open_)['total'],1)
        with self.assertRaises(ValueError):publish('<!doctype html>',[record],'test-secret',opener=open_)
        with self.assertRaises(ValueError):publish('<!doctype html>',[record],'',opener=open_)
    def test_portal_retries_transient_errors_but_not_auth_errors(self):
        calls=[]
        def open_(req,timeout):
            calls.append(req)
            if len(calls)==1:raise HTTPError(req.full_url,503,'temporary',{},None)
            return io.BytesIO(b'{"updated":true,"total":1}')
        publish('<!doctype html><tr data-ts="1">',[{'id':'one'}],'test-secret',opener=open_,sleep=lambda _:None)
        self.assertEqual(len(calls),2);self.assertEqual(calls[0].data,calls[1].data)
        def denied(req,timeout):raise HTTPError(req.full_url,401,'denied',{},None)
        with self.assertRaises(RuntimeError) as err:publish('<!doctype html><tr data-ts="1">',[{'id':'one'}],'test-secret',opener=denied,sleep=lambda _:self.fail('must not retry401'))
        self.assertNotIn('test-secret',str(err.exception))
    def test_portal_rejects_partial_ack_and_credential_redirect(self):
        with self.assertRaises(ValueError):publish('<!doctype html><tr data-ts="1">',[{'id':'one'}],'test-secret',opener=lambda *a,**k:io.BytesIO(b'{"updated":true,"total":0}'))
        self.assertIsNone(NoRedirect().redirect_request(None,None,302,'redirect',{},'https://other.example'))
