"""Daily monitoring of explicitly allowlisted public Telegram web previews.

No account, cookies, private links, channel joins or paid/global search. Missing
captions/dates never become invented records. Bounded scans report coverage gaps.
"""
import datetime as dt
import json
import re
import time
from collections import deque
from pathlib import Path
from urllib.parse import urlsplit, parse_qs, urlencode
from urllib.error import HTTPError
from bs4 import BeautifulSoup

CONFIG = Path('data/telegram_channels.json')
STATE = Path('telegram_monitor_state.json')
_PENDING_STATE = None
USERNAME = re.compile(r'[A-Za-z][A-Za-z0-9_]{3,63}')

def preview_url(channel, before=None, query=None):
    if not USERNAME.fullmatch(channel) or channel.casefold() in {'c','joinchat','s'}:
        raise ValueError('invalid_public_channel')
    params={}
    if before:params['before']=int(before)
    if query:params['q']=query
    return 'https://t.me/s/' + channel + ('?' + urlencode(params) if params else '')

def post_parts(url):
    p=urlsplit(str(url));parts=p.path.strip('/').split('/')
    if p.scheme!='https' or (p.hostname or '').casefold() not in {'t.me','www.t.me'}:return None
    if parts and parts[0]=='s':parts=parts[1:]
    if len(parts)!=2 or not USERNAME.fullmatch(parts[0]) or not parts[1].isdigit():return None
    return parts[0].casefold(),int(parts[1])

def parse_page(page, final_url, channel, published, now, allow_empty=False):
    expected=preview_url(channel);p=urlsplit(final_url)
    if (p.hostname or '').casefold() not in {'t.me','www.t.me'} or p.path.rstrip('/').casefold()!=('/s/'+channel).casefold():
        raise ValueError('no_public_preview')
    soup=BeautifulSoup(page,'html.parser');widgets=soup.select('.tgme_widget_message[data-post]')
    if not widgets:
        if allow_empty and soup.select_one('.tgme_channel_info_header_title'):
            return [],{'ids':[],'dates':[],'invalid_posts':0,'media_without_text':0,'next_before':None}
        raise ValueError('no_public_preview')
    title=soup.select_one('.tgme_channel_info_header_title')
    source=title.get_text(' ',strip=True) if title else channel
    rows=[];ids=[];dates=[];invalid=0;without_text=0
    for widget in widgets:
        raw=widget.get('data-post','');match=re.fullmatch(r'([A-Za-z][A-Za-z0-9_]{3,63})/(\d+)',raw)
        if not match or match[1].casefold()!=channel.casefold():invalid+=1;continue
        mid=int(match[2]);ids.append(mid)
        date_link=widget.select_one('a.tgme_widget_message_date[href]');stamp=date_link.select_one('time[datetime]') if date_link else None
        owner=widget.select_one('.tgme_widget_message_user a[href]')
        date=published(stamp.get('datetime')) if stamp else None
        if not date_link or post_parts(date_link['href'])!=(channel.casefold(),mid) or not date or not owner:
            invalid+=1;continue
        owner_url=urlsplit(owner['href'])
        if owner_url.scheme!='https' or (owner_url.hostname or '').casefold() not in {'t.me','www.t.me'} or owner_url.path.strip('/').casefold()!=channel.casefold():invalid+=1;continue
        when=dt.datetime.fromisoformat(date)
        if when>now+dt.timedelta(minutes=5):invalid+=1;continue
        dates.append(when)
        body=widget.select_one('.tgme_widget_message_text')
        if not body:without_text+=1;continue
        # Match only this post's native caption/body, never previews, headers or suggested content.
        for extra in body.select('.tgme_widget_message_link_preview,script,style'):extra.decompose()
        text=body.get_text(' ',strip=True)
        if not text or len(text)>20000:without_text+=1;continue
        rows.append({'url':f'https://t.me/{channel}/{mid}','title':text[:220], 'source':source[:500],'author_url':'https://t.me/'+channel,'platform':'Telegram','published_at':date,'post_text':text,'status':'relevant','date_source':'telegram_native_time','verification_method':'telegram_public_preview','telegram_post_id':f'{channel.casefold()}/{mid}'})
    if not ids:raise ValueError('unexpected_channel_identity')
    # Derive an older cursor only from observed post IDs; never follow arbitrary links.
    has_more=bool(soup.select_one('a.tme_messages_more[data-before]'))
    return rows,{'ids':ids,'dates':dates,'invalid_posts':invalid,'media_without_text':without_text,'next_before':min(ids) if has_more else None}

def collect(classify, fetch, published, save, *, now=None, config_path=CONFIG, state_path=STATE, sleep=None, monotonic=None):
    global _PENDING_STATE
    _PENDING_STATE=None
    if not config_path.exists():return {'status':'not_configured'}
    cfg=json.loads(config_path.read_text());state=json.loads(state_path.read_text()) if state_path.exists() else {'version':1,'channels':{}}
    now=now or dt.datetime.now(dt.timezone.utc);sleep=sleep or time.sleep;monotonic=monotonic or time.monotonic
    stamp=now.isoformat();deadline=monotonic()+cfg.get('max_runtime_seconds',600)
    report={'status':'ok','scope':'allowlisted public web previews only; text captions, bounded pagination','requests':0,'errors':0,'posts_read':0,'confirmed':0,'already_known':0,'excluded_private_links':cfg.get('private_links_skipped',0),'channels':{},'budget_exhausted':False}
    report['retries']=0;report['rate_limited']=False
    def read_public(url):
        try:return fetch(url)
        except HTTPError as exc:
            if exc.code not in {429,500,502,503,504}:raise
            delay=15 if exc.code==429 else 2
            try:delay=max(delay,int(exc.headers.get('Retry-After',str(delay))))
            except (ValueError,TypeError,AttributeError):pass
            if delay>30 or report['requests']>=cfg.get('max_requests',450) or monotonic()+delay>=deadline:
                if exc.code==429:report['rate_limited']=True
                raise
            sleep(delay);report['requests']+=1;report['retries']+=1
            try:return fetch(url)
            except HTTPError as retry:
                if retry.code==429:report['rate_limited']=True
                raise
    queue=deque();entries={}
    for item in cfg['channels']:
        name=item['channel']
        if not item.get('enabled'):report['channels'][name]={'status':'disabled_no_public_preview'};continue
        preview_url(name)
        previous=state['channels'].get(name.casefold(),{})
        last=published(previous.get('last_complete_at'))
        cutoff=(dt.datetime.fromisoformat(last)-dt.timedelta(days=cfg.get('overlap_days',2))) if last else (dt.datetime.fromisoformat(published(previous.get('first_scan_at'))) if published(previous.get('first_scan_at')) else now)-dt.timedelta(days=cfg.get('initial_lookback_days',7))
        cutoff=max(cutoff,dt.datetime.fromisoformat(cfg.get('start_date','2026-04-23')).replace(tzinfo=dt.timezone.utc))
        info={'status':'pending','pages':0,'posts_read':0,'confirmed':0,'already_known':0,'since':cutoff.isoformat(),'limit_reached':False,'invalid_posts':0,'media_without_text':0}
        report['channels'][name]=info;entries[name]={'previous':previous,'cutoff':cutoff,'seen':set(),'before':None,'oldest':None}
        queue.append(name)
    while queue:
        if report['rate_limited'] or monotonic()>=deadline or report['requests']>=cfg.get('max_requests',210):
            report['budget_exhausted']=True
            for name in queue:
                info=report['channels'][name];info['status']='partial' if info['pages'] else 'not_run_rate_limit' if report['rate_limited'] else 'not_run_budget';info['limit_reached']=True
            break
        name=queue.popleft();entry=entries[name];info=report['channels'][name]
        before=entry['before'];report['requests']+=1
        try:
            page,final=read_public(preview_url(name,before))
            rows,meta=parse_page(page,final,name,published,now)
            if before is not None and (not meta['ids'] or max(meta['ids'])>=before):raise ValueError('non_advancing_pagination')
            info['pages']+=1;info['posts_read']+=len(meta['ids']);report['posts_read']+=len(meta['ids'])
            info['invalid_posts']+=meta['invalid_posts'];info['media_without_text']+=meta['media_without_text']
            if meta['dates']:entry['oldest']=min(meta['dates']) if entry['oldest'] is None else min(entry['oldest'],min(meta['dates']))
            for record in rows:
                if record['telegram_post_id'] in entry['seen']:continue
                entry['seen'].add(record['telegram_post_id'])
                if dt.datetime.fromisoformat(record['published_at'])<entry['cutoff']:continue
                match=classify(record['post_text'])
                if not match:continue
                record.update(match)
                result=save(record);info[result]=info.get(result,0)+1;report[result]=report.get(result,0)+1
            complete=entry['oldest'] is not None and entry['oldest']<=entry['cutoff'] or meta['next_before'] is None
            if complete:info['status']='partial' if info['invalid_posts'] else 'ok'
            elif info['pages']>=cfg.get('max_pages_per_channel',5):info.update(status='partial',limit_reached=True)
            else:info['status']='scanning';entry['before']=meta['next_before'];queue.append(name)
        except Exception as exc:
            report['errors']+=1;info.update(status='unavailable' if not info['pages'] else 'partial',last_error='HTTP '+str(exc.code) if hasattr(exc,'code') else str(exc) if isinstance(exc,ValueError) else type(exc).__name__)
        sleep(cfg.get('request_delay_seconds',0.6))
    # Public channel search is exposed by Telegram's own preview search form.
    # It finds keyword posts in busy channels without downloading every intervening post.
    for name,entry in entries.items():
        info=report['channels'][name];queries=cfg.get('keyword_queries',[])
        if not queries:continue
        recent_limit=info['limit_reached'];info['keyword_search']={}
        for query in queries:
            result={'status':'not_run_budget','pages':0};info['keyword_search'][query]=result
            before=None
            for _ in range(cfg.get('max_search_pages',2)):
                if report['rate_limited'] or monotonic()>=deadline or report['requests']>=cfg.get('max_requests',450):
                    report['budget_exhausted']=True;break
                report['requests']+=1
                try:
                    page,final=read_public(preview_url(name,before,query))
                    if parse_qs(urlsplit(final).query).get('q')!=[query]:raise ValueError('native_search_not_applied')
                    rows,meta=parse_page(page,final,name,published,now,allow_empty=True)
                    if before is not None and meta['ids'] and max(meta['ids'])>=before:raise ValueError('non_advancing_pagination')
                    info['pages']+=1;result['pages']+=1;info['posts_read']+=len(meta['ids']);report['posts_read']+=len(meta['ids'])
                    info['invalid_posts']+=meta['invalid_posts'];info['media_without_text']+=meta['media_without_text']
                    if meta['dates']:entry['oldest']=min(meta['dates']) if entry['oldest'] is None else min(entry['oldest'],min(meta['dates']))
                    for record in rows:
                        if record['telegram_post_id'] in entry['seen']:continue
                        entry['seen'].add(record['telegram_post_id'])
                        if dt.datetime.fromisoformat(record['published_at'])<entry['cutoff']:continue
                        match=classify(record['post_text'])
                        if not match:continue
                        record.update(match)
                        saved=save(record);info[saved]=info.get(saved,0)+1;report[saved]=report.get(saved,0)+1
                    complete=meta['next_before'] is None or meta['dates'] and min(meta['dates'])<=entry['cutoff']
                    result['status']='ok' if complete and not meta['invalid_posts'] else 'partial'
                    before=meta['next_before']
                    if complete:break
                except Exception as exc:
                    report['errors']+=1;result.update(status='error',last_error='HTTP '+str(exc.code) if hasattr(exc,'code') else str(exc) if isinstance(exc,ValueError) else type(exc).__name__);break
                finally:sleep(cfg.get('request_delay_seconds',0.6))
        info['latest_feed_truncated']=recent_limit
        if all(x['status']=='ok' for x in info['keyword_search'].values()) and not info['invalid_posts']:
            info.update(status='ok',limit_reached=False,coverage_basis='native_keyword_search_plus_latest_posts')
        else:info.update(status='partial' if info['pages'] else 'unavailable',limit_reached=True)
    for name,entry in entries.items():
        info=report['channels'][name];previous=entry['previous'];updated={**previous,'first_scan_at':previous.get('first_scan_at',stamp),'last_attempt_at':stamp,'last_status':info['status']}
        if info['pages']:
            # Latest-page checks and incomplete historical coverage are explicitly distinct.
            updated['last_checked_at']=stamp
            updated['oldest_read_at']=entry['oldest'].isoformat() if entry['oldest'] else None
            if info['status']=='ok':updated['last_complete_at']=stamp
            elif info.get('limit_reached'):
                gaps=previous.get('coverage_gaps',[])
                updated['coverage_gaps']=(gaps+[{'from':info['since'],'to':updated['oldest_read_at'],'reason':'bounded_scan'}])[-10:]
        if info.get('last_error'):updated['last_error']=info['last_error']
        else:updated.pop('last_error',None)
        state['channels'][name.casefold()]=updated
    active=[i for i in report['channels'].values() if i['status']!='disabled_no_public_preview']
    report['available_channels']=sum(i.get('pages',0)>0 for i in active)
    report['complete_channels']=sum(i['status']=='ok' for i in active)
    report['configured_channels']=len(active)
    report['disabled_channels']=len(cfg['channels'])-len(active)
    if active and report['available_channels']==0 and report['errors']:report['status']='error'
    elif report['budget_exhausted'] or report['errors'] or any(i['status']!='ok' for i in active):report['status']='partial'
    state['last_run_at']=stamp
    _PENDING_STATE=(state_path.resolve(),state)
    return report

def commit_state():
    """Call only after the collector has safely saved mentions, flags and diagnostics."""
    global _PENDING_STATE
    if _PENDING_STATE is not None:
        path,state=_PENDING_STATE;tmp=path.with_suffix('.tmp');tmp.write_text(json.dumps(state,ensure_ascii=False,indent=2)+'\n');tmp.replace(path);_PENDING_STATE=None
