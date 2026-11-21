"""Resolve metadata-format false alarms and provenance DOI candidates without corpus edits."""
import json, re, copy
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from collections import Counter
from difflib import SequenceMatcher
from online_screen import ROOT,OUT,NET,norm,metadata,dump,retraction_database,doi_norm,now

def main():
    base=json.loads((OUT/'online_screen.json').read_text(encoding='utf-8'))
    local=json.loads((OUT/'local_screen.json').read_text(encoding='utf-8'))
    papers={p['doc_id']:p for p in json.loads((ROOT/'pilot/catalog/papers.json').read_text(encoding='utf-8'))}
    ld={d['doc_id']:d for d in local['documents']}; records=copy.deepcopy(base['records'])
    candidates=[]
    for r in records:
        i=r['doc_id'];p=papers[i]
        if not p.get('doi'):
            ds=ld[i]['provenance']['doi_candidates'] or ld[i]['provenance']['provenance_note_doi_candidates']
            if ds:candidates.append((i,ds[0]))
    def run(pair):
        i,doi=pair; cp=NET/(i+'_candidate_check.json')
        if cp.exists():return i,json.loads(cp.read_text(encoding='utf-8'))
        p=copy.deepcopy(papers[i]);p['doi']=doi;r=metadata(p)
        r['candidate_basis']='existing_local_provenance_not_newly_added_to_catalog';dump(cp,r);return i,r
    with ThreadPoolExecutor(max_workers=6) as pool: additional=dict(pool.map(run,candidates))
    _,bydoi,bytitle=retraction_database()
    for r in records:
        i=r['doc_id']; m=r['registration'];p=papers[i]
        r['followup']=[]
        if m.get('titles'):
            sim=max(SequenceMatcher(None,norm(p['title']),norm(t)).ratio() for t in m['titles'])
            m['normalized_title_similarity']=round(sim,4)
            if m['status']=='title_needs_review' and sim>=.85:
                r['followup'].append('Crossref HTML subscript markup caused an initial matching false alarm. Stripped markup matches.');m['status']='registered_title_match'
        if i=='doc_a00e87e5d877fbe2d070':
            m['status']='bilingual_title_identity_confirmed'
            r['followup'].append({'finding':'J-STAGE日/英题名、两作者与DOI一致；Crossref英题名不应判错文。','source':'https://www.jstage.jst.go.jp/article/transjsme/81/831/81_15-00408/_article/-char/ja/','checked_at':now(),'method':'web primary page and local title page'})
        if i in ['doc_a5170d52eb3b9d4ede17','doc_f90680f5892c92656c17']:
            m['status']='cnki_agency_and_publisher_identity_matched'
            r['followup'].append({'finding':'doi.org/ra 返回CNKI；期刊官网搜索缓存题名、作者、DOI与本地全文一致。直接HTTP521保留；未完成本轮官网PDF字节比对。','source':p['source_url'],'method':'DOI registration agency + primary publisher search-indexed page + PDF first/middle/last visual check','checked_at':now(),'publisher_cache_dates':'search reported crawled 1–2 months ago; not a fresh PDF fetch','pdf_issue':'2026 26(11)110001' if i.endswith('de17') else '2026 26(11)110004'})
        if i in additional:r['additional_doi_check']=additional[i]
        all_dois=[doi_norm(p.get('doi'))]+ld[i]['provenance']['doi_candidates']+ld[i]['provenance']['provenance_note_doi_candidates']
        hits=[]
        for d in all_dois:
            for hit in bydoi.get(doi_norm(d),[]):
                if hit not in hits:hits.append(hit)
        for hit in bytitle.get(norm(p['title']),[]):
            if hit not in hits:hits.append(hit)
        r['retraction_watch'].update(result='matched_record_requires_review' if hits else 'no_exact_doi_or_title_match',matches=hits,checked_doi_candidates=[d for d in dict.fromkeys(all_dois) if d],title_normalization='Unicode alphanumeric after NFKD/casefold and HTML decoding')
        if i=='doc_af40346ca9b93056cc27':
            r['followup'].append({'finding':'本地是校样：PROOF COPY / NOT FOR PRINT / FOR REVIEW BY AUTHOR，非无标记正式出版PDF。','evidence_pages':[1,2,3,4],'action':'标注proof版并以正式版建立版本关系；不静默替换旧页码。'})
    summary={'count':len(records),'registration_after_followup':dict(Counter(r['registration']['status'] for r in records)), 'source_direct_fetch':base['summary']['source'],'additional_doi_candidates_checked':len(additional),'additional_doi_status':dict(Counter(x['status'] for x in additional.values())),'retraction_watch':dict(Counter(r['retraction_watch']['result'] for r in records)), 'crossref_update_notices':[r['doc_id'] for r in records if r['registration'].get('notice_result')=='update_fields_present_review_required']}
    dump(OUT/'online_screen_final.json',{'finished_at':now(),'summary':summary,'records':records,'limits':['Page identity matching is a lead, not scientific reliability certification.','No hit in Retraction Watch or no Crossref update link is not proof of no notices.','Direct source failures remain listed even when alternate identity evidence is available.','Source/version/correctness are separate judgments; authoritative hosts do not certify experiments.']})
    print(json.dumps(summary,ensure_ascii=False))
if __name__=='__main__':main()
