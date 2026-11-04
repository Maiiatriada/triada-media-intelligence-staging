"""Bounded discovery from Reddit's public Atom post search, no account or paid API."""
import re
import urllib.parse as up
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup

NS={'a':'http://www.w3.org/2005/Atom'}

def records_from_feed(page, classify, published):
    root=ET.fromstring(page)
    if root.tag!='{http://www.w3.org/2005/Atom}feed':raise ValueError('not_an_atom_feed')
    entries=root.findall('a:entry',NS); records=[]
    for entry in entries:
        pid=entry.findtext('a:id','',NS)
        title=entry.findtext('a:title','',NS).strip()
        date=published(entry.findtext('a:published','',NS))
        author=entry.findtext('a:author/a:name','',NS).strip()
        author_url=entry.findtext('a:author/a:uri','',NS)
        links=[x.get('href','') for x in entry.findall('a:link',NS) if x.get('rel','alternate')=='alternate']
        url=next((u for u in links if re.fullmatch(r'(?:www\.)?reddit\.com',up.urlsplit(u).hostname or '') and re.search(r'/comments/[a-z0-9]+/',up.urlsplit(u).path)), '')
        match_id=re.search(r'/comments/([a-z0-9]+)/',up.urlsplit(url).path)
        if not match_id or pid!='t3_'+match_id.group(1) or not title or not date or not author:continue
        if not re.fullmatch(r'(?:www\.)?reddit\.com',up.urlsplit(author_url).hostname or '') or not re.match(r'/(?:u|user)/[^/]+/?$',up.urlsplit(author_url).path):continue
        # Only the native self-post body; exclude RSS footer, linked previews and recommendations.
        body=BeautifulSoup(entry.findtext('a:content','',NS),'html.parser').select_one('div.md')
        text=title+'\n'+(body.get_text(' ',strip=True) if body else '')
        match=classify(text)
        if not match:continue
        records.append({'url':url,'title':title,'source':author,'author_url':author_url,'platform':'Reddit','published_at':date,'post_text':text,'status':'relevant','date_source':'reddit_public_feed','verification_method':'reddit_public_feed',**match})
    return records,len(entries)

def collect(cfg, classify, fetch, published, save):
    info={'status':'ok','scope':'public Atom post search; bounded results, no private content or comments','requests':0,'errors':0,'items':0,'confirmed':0,'already_known':0,'limit_reached':False}
    # One combined daily request avoids spending the public RSS rate limit on six searches.
    for query in ['(' + ' OR '.join(cfg['queries']) + ')']:
        url='https://www.reddit.com/search.rss?'+up.urlencode({'q':query,'sort':'new','limit':25})
        info['requests']+=1
        try:
            page,final=fetch(url)
            if not re.fullmatch(r'(?:www\.)?reddit\.com',up.urlsplit(final).hostname or '') or up.urlsplit(final).path!='/search.rss':raise ValueError('unexpected_reddit_feed')
            records,count=records_from_feed(page,classify,published);info['items']+=count
            if count>=25:info['limit_reached']=True
            for record in records:
                result=save(record);info[result]=info.get(result,0)+1
        except Exception as exc:
            info['errors']+=1;info['last_error']='HTTP '+str(exc.code) if hasattr(exc,'code') else type(exc).__name__
    if info['errors']:info['status']='error' if info['errors']==info['requests'] else 'partial'
    elif info['limit_reached']:info['status']='partial'
    return info
