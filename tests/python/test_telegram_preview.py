import datetime as dt
import json
import tempfile
import unittest
from pathlib import Path
from tools import telegram_preview as t
from tools.monitoring import published
from tools.tmi_keys import canonical_url,DuplicateIndex
from collector import classify

NOW=dt.datetime(2026,11,4,16,tzinfo=dt.timezone.utc)
def message(channel='NewsOne',mid=10,date='2026-11-04T10:00:00Z',text='Triada Robotics news',extra=''):
 return f'<div class="tgme_widget_message" data-post="{channel}/{mid}"><div class="tgme_widget_message_user"><a href="https://t.me/{channel}">Channel</a></div><div class="tgme_widget_message_text">{text}</div>{extra}<a class="tgme_widget_message_date" href="https://t.me/{channel}/{mid}"><time datetime="{date}">10:00</time></a></div>'
def page(*messages,more=False):
 return '<div class="tgme_channel_info_header_title">News channel</div>'+''.join(messages)+('<a class="tme_messages_more" data-before="10" href="/s/NewsOne?before=10">More</a>' if more else '')
class TelegramPreviewTests(unittest.TestCase):
 def tearDown(self):t._PENDING_STATE=None
 def test_only_native_caption_confirms_not_preview_or_channel_description(self):
  p=page(message(text='Other news',extra='<div class="tgme_widget_message_link_preview">Triada Robotics</div>'))
  rows,meta=t.parse_page(p,'https://t.me/s/NewsOne','NewsOne',published,NOW)
  self.assertIsNone(classify(rows[0]['post_text']))
  self.assertEqual(rows[0]['published_at'],'2026-11-04T10:00:00+00:00')
 def test_permalink_owner_identity_and_date_must_match_this_post(self):
  native=message()
  for bad in [native.replace('href="https://t.me/NewsOne/10"','href="https://t.me/Other/10"'),native.replace('href="https://t.me/NewsOne"','href="https://evil.example/NewsOne"'),native.replace('datetime="2026-11-04T10:00:00Z"','datetime=""')]:
   rows,meta=t.parse_page(page(bad),'https://t.me/s/NewsOne','NewsOne',published,NOW)
   self.assertEqual(rows,[]);self.assertEqual(meta['invalid_posts'],1)
 def test_future_timestamp_is_not_published(self):
  rows,meta=t.parse_page(page(message(date='2026-11-06T10:00:00Z')),'https://t.me/s/NewsOne','NewsOne',published,NOW)
  self.assertEqual(rows,[]);self.assertEqual(meta['invalid_posts'],1)
 def test_foreign_post_widgets_and_login_landing_pages_cannot_be_imported(self):
  for p in [page(message(channel='OtherOne')),'<html>Join or log in to Telegram</html>']:
   with self.assertRaises(ValueError):t.parse_page(p,'https://t.me/s/NewsOne','NewsOne',published,NOW)
  with self.assertRaises(ValueError):t.parse_page(page(message()),'https://other.example/s/NewsOne','NewsOne',published,NOW)
 def test_media_without_caption_is_counted_but_never_invented(self):
  rows,meta=t.parse_page(page(message().replace('<div class="tgme_widget_message_text">Triada Robotics news</div>','')),'https://t.me/s/NewsOne','NewsOne',published,NOW)
  self.assertEqual(rows,[]);self.assertEqual(meta['media_without_text'],1);self.assertEqual(len(meta['dates']),1)
 def test_preview_and_case_aliases_deduplicate_by_real_post(self):
  self.assertEqual(canonical_url('https://t.me/s/NewsOne/10?before=2'),canonical_url('https://t.me/newsone/10'))
  old={'url':'https://t.me/NewsOne/10','platform':'Telegram'}
  self.assertIsNotNone(DuplicateIndex([old]).find({'url':'https://t.me/s/newsone/10','platform':'Telegram'}))
 def setup_scan(self,folder,channels=None,**options):
  cfg={'channels':channels or [{'channel':'NewsOne','enabled':True}],'request_delay_seconds':0,'initial_lookback_days':7,'overlap_days':2,'max_pages_per_channel':5,'max_requests':10,'max_runtime_seconds':60,**options}
  p=Path(folder)/'config.json';p.write_text(json.dumps(cfg));return p,Path(folder)/'state.json'
 def run_scan(self,cfg,state,fetch,save=lambda r:'confirmed'):
  return t.collect(classify,fetch,published,save,now=NOW,config_path=cfg,state_path=state,sleep=lambda _:None)
 def test_state_not_written_until_successful_collector_commit(self):
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d);r=self.run_scan(cfg,state,lambda u:(page(message()),u))
   self.assertEqual(r['confirmed'],1);self.assertFalse(state.exists());t.commit_state()
   s=json.loads(state.read_text());self.assertEqual(s['channels']['newsone']['last_complete_at'],NOW.isoformat())
 def test_failure_preserves_last_successful_checkpoint(self):
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d);old='2026-11-01T10:00:00+00:00';state.write_text(json.dumps({'version':1,'channels':{'newsone':{'last_checked_at':old}}}))
   def fail(u):raise OSError('connection unavailable')
   r=self.run_scan(cfg,state,fail);t.commit_state()
   self.assertEqual(r['status'],'error');self.assertEqual(json.loads(state.read_text())['channels']['newsone']['last_checked_at'],old)
 def test_initial_window_skips_old_posts_and_later_runs_overlap(self):
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d);r=self.run_scan(cfg,state,lambda u:(page(message(date='2026-09-01T10:00:00Z')),u))
   self.assertEqual(r['confirmed'],0);t.commit_state()
   r=self.run_scan(cfg,state,lambda u:(page(message()),u));self.assertEqual(r['channels']['NewsOne']['since'],'2026-11-02T16:00:00+00:00')
 def test_latest_page_of_each_channel_is_checked_before_older_pages(self):
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d,[{'channel':'NewsOne','enabled':True},{'channel':'NewsTwo','enabled':True}],max_pages_per_channel=2)
   calls=[]
   def fetch(u):
    calls.append(u);name='NewsTwo' if 'NewsTwo' in u else 'NewsOne'
    return page(message(channel=name,mid=9 if 'before' in u else 10),more='before' not in u),u
   r=self.run_scan(cfg,state,fetch)
   self.assertEqual(calls[:2],['https://t.me/s/NewsOne','https://t.me/s/NewsTwo']);self.assertEqual(r['requests'],4)
 def test_page_limit_is_reported_as_incomplete_coverage(self):
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d,max_pages_per_channel=1);r=self.run_scan(cfg,state,lambda u:(page(message(),more=True),u));t.commit_state()
   self.assertEqual(r['status'],'partial');self.assertTrue(r['channels']['NewsOne']['limit_reached']);self.assertIn('coverage_gaps',json.loads(state.read_text())['channels']['newsone'])
 def test_cursor_must_advance_and_cannot_loop_or_follow_external_urls(self):
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d);r=self.run_scan(cfg,state,lambda u:(page(message(),more=True),u))
   self.assertEqual(r['requests'],2);self.assertEqual(r['channels']['NewsOne']['last_error'],'non_advancing_pagination')
 def test_disabled_private_or_no_preview_sources_are_not_requested(self):
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d,[{'channel':'NewsOne','enabled':True},{'channel':'private','enabled':False}])
   calls=[]
   def fetch(u):calls.append(u);return page(message()),u
   r=self.run_scan(cfg,state,fetch);self.assertEqual(len(calls),1);self.assertEqual(r['disabled_channels'],1)
 def test_global_request_budget_is_explicit_and_never_marks_unread_sources_successful(self):
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d,[{'channel':'NewsOne','enabled':True},{'channel':'NewsTwo','enabled':True}],max_requests=1)
   r=self.run_scan(cfg,state,lambda u:(page(message()),u));t.commit_state()
   self.assertTrue(r['budget_exhausted']);self.assertEqual(r['channels']['NewsTwo']['status'],'not_run_budget');self.assertNotIn('last_checked_at',json.loads(state.read_text())['channels']['newstwo'])
 def test_no_configuration_never_fetches_or_creates_state(self):
  with tempfile.TemporaryDirectory() as d:
   cfg=Path(d)/'missing';state=Path(d)/'state';r=self.run_scan(cfg,state,lambda _:self.fail('must not fetch'));self.assertEqual(r['status'],'not_configured');self.assertFalse(state.exists())
 def test_private_urls_are_rejected_before_any_network_request(self):
  for value in ['c','joinchat','s','../private','https://evil.example']:
   with self.assertRaises(ValueError):t.preview_url(value)

 def test_native_keyword_search_recovers_a_mention_beyond_busy_latest_page(self):
  from urllib.parse import parse_qs,urlsplit
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d,max_pages_per_channel=1,keyword_queries=['triada','веселов'])
   def fetch(u):
    query=parse_qs(urlsplit(u).query).get('q',[''])[0]
    if query=='triada':return page(message(mid=7,date='2026-11-01T10:00:00Z')),u
    if query:return '<div class="tgme_channel_info_header_title">News channel</div>',u
    return page(message(text='Other news'),more=True),u
   r=self.run_scan(cfg,state,fetch)
   self.assertEqual(r['confirmed'],1);self.assertEqual(r['channels']['NewsOne']['status'],'ok');self.assertTrue(r['channels']['NewsOne']['latest_feed_truncated'])
 def test_native_keyword_query_must_not_silently_redirect_to_unfiltered_feed(self):
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d,keyword_queries=['triada'])
   def fetch(u):return page(message(text='Other news')),'https://t.me/s/NewsOne'
   r=self.run_scan(cfg,state,fetch)
   self.assertEqual(r['confirmed'],0);self.assertEqual(r['channels']['NewsOne']['keyword_search']['triada']['last_error'],'native_search_not_applied')
 def test_incomplete_search_cannot_advance_full_coverage_checkpoint(self):
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d,keyword_queries=['triada'])
   old='2026-10-30T10:00:00+00:00';state.write_text(json.dumps({'version':1,'channels':{'newsone':{'last_complete_at':old}}}))
   def fetch(u):
    if 'q=' in u:raise OSError('temporarily unavailable')
    return page(message()),u
   r=self.run_scan(cfg,state,fetch);t.commit_state()
   self.assertEqual(r['status'],'partial');self.assertEqual(json.loads(state.read_text())['channels']['newsone']['last_complete_at'],old)

 def test_rate_limit_is_retried_once_then_unread_channels_are_deferred(self):
  from urllib.error import HTTPError
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d,[{'channel':'NewsOne','enabled':True},{'channel':'NewsTwo','enabled':True}],keyword_queries=['triada'])
   calls=[]
   def limited(u):calls.append(u);raise HTTPError(u,429,'rate limited',{'Retry-After':'1'},None)
   r=self.run_scan(cfg,state,limited)
   self.assertEqual(len(calls),2);self.assertTrue(r['rate_limited']);self.assertEqual(r['available_channels'],0);self.assertEqual(r['retries'],1)
 def test_long_retry_after_defers_without_exceeding_time_budget(self):
  from urllib.error import HTTPError
  with tempfile.TemporaryDirectory() as d:
   cfg,state=self.setup_scan(d);calls=[]
   def limited(u):calls.append(u);raise HTTPError(u,429,'rate limited',{'Retry-After':'3600'},None)
   r=self.run_scan(cfg,state,limited);self.assertEqual(len(calls),1);self.assertTrue(r['rate_limited'])
