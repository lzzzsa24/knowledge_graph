"""Read-only, checkpointed primary-source/registration screening. Not certification."""
from __future__ import annotations
import csv, hashlib, io, json, re, threading, time, unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from difflib import SequenceMatcher
from pathlib import Path
from urllib.parse import quote, urlparse
import requests

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / 'pilot/reports/quality_review_20261113'
NET = OUT / 'online'
NET.mkdir(parents=True, exist_ok=True)
LOCK = threading.Lock()
HOSTS = {}
RW_URL = 'https://gitlab.com/crossref/retraction-watch-data/-/raw/main/retraction_watch.csv?ref_type=heads&inline=false'

def now(): return datetime.now(timezone.utc).isoformat()
def dump(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')
def norm(s):
    import html
    clean=re.sub('<[^>]+>', '', html.unescape(str(s or '')))
    return ''.join(c for c in unicodedata.normalize('NFKD', clean).casefold() if c.isalnum())
def doi_norm(s): return re.sub(r'^https?://(?:dx\.)?doi.org/', '', str(s or '').strip().lower())
def fetch(url, max_bytes=40_000_000):
    with LOCK: sem = HOSTS.setdefault(urlparse(url).netloc, threading.Semaphore(3))
    with sem:
        for attempt in range(2):
            result = {'requested_url':url, 'checked_at':now()}
            try:
                with requests.get(url, timeout=(12,35), stream=True,
                                  headers={'User-Agent':'LiteratureQualityAudit/1.0 (research provenance verification)'}) as r:
                    result.update(status=r.status_code, resolved_url=r.url, content_type=r.headers.get('content-type',''))
                    if r.status_code in (429,502,503,504) and attempt == 0:
                        time.sleep(2); continue
                    chunks=[]; size=0
                    for c in r.iter_content(65536):
                        size+=len(c)
                        if size>max_bytes:
                            result['error']='response_exceeds_limit'; return result,b''
                        chunks.append(c)
                    data=b''.join(chunks)
                    result.update(bytes=len(data),sha256=hashlib.sha256(data).hexdigest())
                    return result,data
            except Exception as e:
                result['error']=type(e).__name__+': '+str(e)[:400]
                if attempt == 0: time.sleep(1)
        return result,b''

def retraction_database():
    p=NET/'retraction_watch.csv'; meta=NET/'retraction_watch_fetch.json'
    if not p.exists():
        m,b=fetch(RW_URL,110_000_000); dump(meta,m)
        if m.get('status')==200 and b: p.write_bytes(b)
    if not p.exists(): return [],{},{}
    rows=list(csv.DictReader(io.StringIO(p.read_text(encoding='utf-8-sig'))))
    bydoi={}; bytitle={}
    for row in rows:
        d=doi_norm(row.get('OriginalPaperDOI'))
        if d and d!='unavailable': bydoi.setdefault(d,[]).append(row)
        t=norm(row.get('Title'))
        if t: bytitle.setdefault(t,[]).append(row)
    dump(NET/'retraction_watch_summary.json',{'retrieved':json.loads(meta.read_text()),'rows':len(rows),'columns':list(rows[0]) if rows else [],'warning':'Dataset coverage is incomplete, especially corrections and expressions of concern. No match does not establish absence of notices.'})
    return rows,bydoi,bytitle

def metadata(d):
    doi=doi_norm(d.get('doi')); result={'doi':doi or None}
    if not doi: result['status']='no_doi_recorded'; return result
    provider='datacite' if doi.startswith('10.48550/') else 'crossref'
    url=('https://api.datacite.org/dois/' if provider=='datacite' else 'https://api.crossref.org/works/')+quote(doi,safe='')
    m,b=fetch(url); result.update(provider=provider,fetch=m)
    if m.get('status') !=200:
        result['status']='metadata_unavailable'; return result
    try:
        raw=json.loads(b); dump(NET/(d['doc_id']+'_registration.json'),raw)
        obj=raw['data']['attributes'] if provider=='datacite' else raw['message']
        titles=([x['title'] for x in obj.get('titles',[])] if provider=='datacite' else obj.get('title',[]))
        similarity=max([SequenceMatcher(None,norm(d['title']),norm(t)).ratio() for t in titles] or [0])
        result.update(titles=titles,title_similarity=round(similarity,4),status='registered_title_match' if similarity>=.85 else 'title_needs_review')
        if provider=='crossref':
            result['bibliography']={k:obj.get(k) for k in ['DOI','type','publisher','container-title','author','published','published-print','published-online','created','URL','link','relation']}
            result['update_fields']={k:obj.get(k) for k in ['updated-by','update-to','update-policy','assertion'] if k in obj}
            result['notice_result']='update_fields_present_review_required' if any(obj.get(k) for k in ['updated-by','update-to']) else 'no_update_link_returned'
        else:
            result['bibliography']={k:obj.get(k) for k in ['doi','creators','publisher','publicationYear','version','relatedIdentifiers','url','dates','types']}
            result['notice_result']='datacite_relations_saved_not_a_retraction_check'
    except Exception as e: result.update(status='metadata_parse_failed',error=str(e))
    return result

def source(d):
    provenance=d.get('provenance') or {}
    urls=list(dict.fromkeys(u for u in [d.get('source_url'),provenance.get('source_url'),d.get('publisher_url'),d.get('pdf_url')] if u and str(u).startswith(('http://','https://'))))
    result={'attempts':[],'status':'no_source_url'}
    for u in urls[:3]:
        m,b=fetch(u); m['identity_evidence']=None
        if m.get('status')==200 and b:
            if b[:5]==b'%PDF-':
                if m.get('sha256')==d.get('sha256'):
                    m['identity_evidence']='byte_identical_to_catalog_pdf'; result['status']='source_pdf_hash_match'
                else:
                    try:
                        import pymupdf
                        pdf=pymupdf.open(stream=b,filetype='pdf'); text='\n'.join(p.get_text() for p in list(pdf)[:3]); m['pdf_page_count']=len(pdf)
                        m['pdf_title_in_first_pages']=norm(d['title']) in norm(text)
                        m['first_pages_excerpt']=text[:5000]
                        if m['pdf_title_in_first_pages']: result['status']='source_pdf_title_match_different_bytes'; m['identity_evidence']='title_in_pdf_different_hash'
                    except Exception as e: m['pdf_parse_error']=str(e)[:200]
            else:
                text=b.decode('utf-8',errors='replace')
                text=re.sub(r'<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>',' ',text,flags=re.S|re.I)
                text=re.sub('<[^>]+>',' ',text)
                import html
                text=html.unescape(text); text=re.sub(r'\s+',' ',text)
                m['page_title_matches']=norm(d['title']) in norm(text)
                doi=doi_norm(d.get('doi')); m['doi_present']=bool(doi and doi in text.lower())
                m['text_excerpt']=text[:2500]
                if m['page_title_matches'] or m['doi_present']:
                    result['status']='source_page_identity_match';m['identity_evidence']='title_or_doi_on_source_page'
                    (NET/(d['doc_id']+'_source.txt')).write_text(text,encoding='utf-8')
                elif result['status']=='no_source_url':result['status']='reachable_identity_unconfirmed'
        result['attempts'].append(m)
        if m.get('identity_evidence'):break
    if urls and not any(a.get('status')==200 for a in result['attempts']): result['status']='source_access_failed'
    return result

def main():
    papers=json.loads((ROOT/'pilot/catalog/papers.json').read_text(encoding='utf-8'))
    rows,bydoi,bytitle=retraction_database()
    def run(d):
        doc_id=d.get('doc_id') or d['id'];d['doc_id']=doc_id
        checkpoint=NET/(doc_id+'.json')
        if checkpoint.exists():return json.loads(checkpoint.read_text(encoding='utf-8'))
        r={'doc_id':doc_id,'title':d['title'],'checked_at':now(),'registration':metadata(d),'source':source(d)}
        hits=bydoi.get(doi_norm(d.get('doi')),[]) or bytitle.get(norm(d['title']),[])
        r['retraction_watch']={'result':'matched_record_requires_review' if hits else ('no_exact_doi_or_title_match' if rows else 'database_unavailable'),'matches':hits,'scope':'Exact DOI and normalized exact title. No match is not a clean bill of health.'}
        dump(checkpoint,r);print(doc_id,r['registration']['status'],r['source']['status'],flush=True)
        return r
    results=[]
    with ThreadPoolExecutor(max_workers=10) as pool:
        fs={pool.submit(run,d):d for d in papers}
        for f in as_completed(fs):
            try:results.append(f.result())
            except Exception as e:
                d=fs[f]; results.append({'doc_id':d['doc_id'],'error':type(e).__name__+': '+str(e)})
    results.sort(key=lambda x:x['doc_id'])
    from collections import Counter
    summary={'count':len(results),'registration':dict(Counter(r.get('registration',{}).get('status','error') for r in results)), 'source':dict(Counter(r.get('source',{}).get('status','error') for r in results)),'retraction_watch':dict(Counter(r.get('retraction_watch',{}).get('result','error') for r in results))}
    dump(OUT/'online_screen.json',{'method_version':1,'finished_at':now(),'summary':summary,'records':results})
    print(json.dumps(summary,ensure_ascii=False),flush=True)

if __name__=='__main__':main()
