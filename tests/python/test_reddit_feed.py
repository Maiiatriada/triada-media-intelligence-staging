import unittest
from tools.reddit_feed import records_from_feed,collect
from tools.monitoring import published,platform,enforce_scope
from collector import classify

T='''<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>t3_abc123</id><title>{title}</title><author><name>/u/Writer</name><uri>https://www.reddit.com/user/Writer</uri></author><published>2026-06-24T12:00:00Z</published><link href="https://www.reddit.com/r/ukraine/comments/abc123/post/"/><content type="html">{body}</content></entry></feed>'''
class RedditFeedTests(unittest.TestCase):
 def test_native_self_post_body_and_original_publication_date(self):
  page=T.format(title='A report',body='&lt;div class="md"&gt;Triada Robotics builds drones.&lt;/div&gt;')
  rows,n=records_from_feed(page,classify,published)
  self.assertEqual(n,1);self.assertEqual(len(rows),1);self.assertEqual(rows[0]['platform'],'Reddit');self.assertEqual(rows[0]['published_at'],'2026-06-24T12:00:00+00:00')
 def test_result_membership_and_feed_footer_do_not_confirm_a_mention(self):
  rows,n=records_from_feed(T.format(title='Other news',body='&lt;footer&gt;Triada Robotics search&lt;/footer&gt;'),classify,published)
  self.assertEqual(rows,[])
 def test_missing_date_and_mismatched_post_id_are_rejected(self):
  page=T.format(title='Triada Robotics',body='')
  for invalid in [page.replace('t3_abc123','t3_other'),page.replace('<published>2026-06-24T12:00:00Z</published>',''),page.replace('https://www.reddit.com/user/Writer','https://other.example/user/Writer')]:
   self.assertEqual(records_from_feed(invalid,classify,published)[0],[])
 def test_feed_failure_cannot_claim_success_or_leak_request_details(self):
  def fail(url):raise ValueError('private-input-not-for-logs')
  info=collect({'queries':['Triada']},classify,fail,published,lambda _:None)
  self.assertEqual(info['status'],'error');self.assertEqual(info['last_error'],'ValueError')
 def test_one_combined_request_and_existing_posts_use_identity_dedup(self):
  calls=[]
  def fetch(u):calls.append(u);return T.format(title='Triada Robotics',body=''),u
  info=collect({'queries':['Triada','Artem']},classify,fetch,published,lambda r:'already_known')
  self.assertEqual(info['confirmed'],0);self.assertEqual(info['already_known'],1);self.assertEqual(len(calls),1)
 def test_reddit_posts_are_classified_as_social(self):
  self.assertEqual(platform('https://www.reddit.com/r/ukraine/comments/abc123/post/'),'Reddit')
