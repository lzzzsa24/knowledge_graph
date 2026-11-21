"""Build review ledgers and a reproducible acceptance check, without corpus changes."""
from __future__ import annotations
import json,hashlib
from collections import Counter
from datetime import datetime,timezone
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2]
OUT=ROOT/'pilot/reports/quality_review_20261113'
def read(p):return json.loads(p.read_text(encoding='utf-8'))
def dump(p,x):p.write_text(json.dumps(x,ensure_ascii=False,indent=2),encoding='utf-8')
def sha(p):return hashlib.sha256(p.read_bytes()).hexdigest()
def link(p,label):return f'[{label}](<{p.as_posix()}>)'
def extract(x):return x if isinstance(x,list) else x['records']

def main():
    papers=read(ROOT/'pilot/catalog/papers.json');pb={p['doc_id']:p for p in papers}
    local=read(OUT/'local_screen.json');lb={d['doc_id']:d for d in local['documents']}
    online=read(OUT/'online_screen_final.json');ob={d['doc_id']:d for d in online['records']}
    deep=[d for name in ['core','risk','root'] for d in extract(read(OUT/'deep'/f'{name}.json'))]
    db={d['doc_id']:d for d in deep}; sample=read(OUT/'deep_sample.json')
    assert len(papers)==213 and len(lb)==213 and len(ob)==213
    assert len(deep)==len(db)==20 and set(db)=={d['doc_id'] for d in sample['records']}
    assert Counter(d['group'] for d in deep)==Counter({'core':8,'risk':6,'guidance':4,'comparison':2})
    claims={c['claim_id']:c for c in map(json.loads,(ROOT/'pilot/annotations/claims.jsonl').read_text(encoding='utf-8').splitlines())}
    mismatches=[]
    for rel,digest in local['frozen_input_sha256'].items():
        actual=sha(ROOT/rel)
        if actual!=digest:mismatches.append({'path':rel,'before':digest,'after':actual})
    for d in local['physical_source_pdf_manifest']:
        path=d.get('path') or d.get('local_path');actual=sha(ROOT/path)
        if actual!=d['sha256']:mismatches.append({'path':path,'before':d['sha256'],'after':actual})
    existing=[]; quote_pages=[]
    for d in deep:
        i=d['doc_id'];d['source_path']=str((ROOT/pb[i]['local_path']).resolve());d['source_sha256']=pb[i]['sha256']
        for c in d['claims']:
            cid=c.get('claim_id')
            if cid:
                assert cid in claims and claims[cid]['doc_id']==i,(i,cid)
                existing.append(cid)
            for e in c['source_evidence']:
                target=e.get('doc_id',i)
                page=e.get('pdf_page')
                if target in pb and page is not None:assert 1<=page<=pb[target]['page_count'],(target,page)
                quote_pages.append((target,page))
    dump(OUT/'deep_reviews_20.json',{'checked_at':datetime.now(timezone.utc).isoformat(),'selection':sample,'review_kind':'agent_review_not_expert_certification','records':deep})
    issue_rows=[]
    for d in deep:
        for k,x in enumerate(d['issues'],1):issue_rows.append({'issue_id':f"{d['doc_id']}:review:{k}",'doc_id':d['doc_id'],**x})
    dump(OUT/'review_findings.json',{'count':len(issue_rows),'warning':'审查事项含限制/版本/共享研究，非错误篇数；同一共享事件可在两篇分别记录，不能简单当独立事件数。','records':issue_rows})

    ledger=[]
    source_labels={'source_page_identity_match':'来源页题名/DOI匹配','source_pdf_hash_match':'在线PDF与存档哈希一致','source_access_failed':'直接来源访问失败'}
    for p in papers:
        i=p['doc_id'];l=lb[i];o=ob[i];d=db.get(i)
        actions=[]
        if o['source']['status']=='source_access_failed':actions.append('复核来源访问与永久链接；保留历史采集证据')
        if l['remaining_active_text_quality_issues']:actions.append('回看活动文字质量候选页，按图/公式/单位分流')
        if d and d['issues']:actions.append('按深审使用条件与修订建议处理相关结论')
        if i=='doc_af40346ca9b93056cc27':actions.append('校样版标注并关联正式版')
        if i in ['doc_37f8496745bd913db74a','doc_aafbbc0171d017de2b61']:actions.append('官方原PDF已匹配，官方目录现行性待确认')
        ledger.append({'doc_id':i,'title':p['title'],'source_type':p.get('source_type'),'source_sha256':p['sha256'],'physical_pages':p['page_count'],'basic_screen':'completed','file_integrity':'catalog_and_acquisition_manifest_match','classification_bindings':l['bindings'],'claim_count':l['claim_count'],'citation_locations':l['claim_evidence_results'],'source_check':o['source']['status'],'registration_check':o['registration']['status'],'additional_doi_check':o.get('additional_doi_check',{}).get('status'),'post_publication_check':o['retraction_watch']['result'],'text_flags':l['flags'],'deep_review':'completed' if d else 'not_selected','recommended_use':d['recommended_use'] if d else '基础筛查已完成，科学结论与方法质量未深度核查；按原证据类型使用。','actions':actions,'followup':o['followup']})
    dump(OUT/'screening_213.json',{'checked_at':datetime.now(timezone.utc).isoformat(),'count':len(ledger),'records':ledger})
    lines=['# 213份资料基础筛查台账','', '逐条检查存档、引文位置、来源及发布后状态。基础通过不代表实验结论已证实。在线来源页匹配是自动身份核对；深审只覆盖所列20份。详细页级记录见local_screen.json，在线快照见online/。','', '| 资料 | 物理页/知识记录 | 本轮直接来源 | 深审/后续 |','| --- | --- | --- | --- |']
    for r in ledger:
        title=r['title'].replace('|','/');actions='；'.join(r['actions']) or '未触发上述队列；仍保留筛查边界'
        lines.append(f"| `{r['doc_id']}` {title} | {r['physical_pages']} / {r['claim_count']} | {source_labels.get(r['source_check'],r['source_check'])} | {'已深审' if r['deep_review']=='completed' else '基础筛查'}；{actions} |")
    (OUT/'213份基础筛查.md').write_text('\n'.join(lines),encoding='utf-8')
    lines=['# 20份深度复核导航','', '有目的分层选样：8篇核心、6篇风险、4份标准/计量/厂商、2篇不同研究对照。风险组刻意选择疑点资料，发现比例不能外推为213份资料的总体错误率。第2篇对照与部分核心作者重叠，已明确标注，不算完全独立团队复现。','', '| 组别 | 文献 | 复核结论数 | 重点使用结论 |','| --- | --- | ---: | --- |']
    groupnames={'core':'核心研究','risk':'风险资料','guidance':'标准/计量/厂商','comparison':'不同研究对照'}
    for d in deep:
        lines.append(f"| {groupnames[d['group']]} | `{d['doc_id']}` {d['title'].replace('|','/')} | {len(d['claims'])} | {d['recommended_use']} |")
    lines+=['','## 详细记录','',link(OUT/'deep/core.md','8篇核心研究'),'',link(OUT/'deep/risk.md','6篇风险资料'),'',link(OUT/'deep/root.md','4份规范/厂商及2篇对照'),'',link(OUT/'deep/local_independent_check.json','核心与风险组的独立复验'),'',link(OUT/'deep/root_independent_check.md','标准与对照组的独立复验'),'','复核以物理页、图表及上下文为依据。agent复核不能替代原始实验复现或专家签字；未在论文报告的批次数/误差定义明确保留为未知。']
    (OUT/'20份深度复核.md').write_text('\n'.join(lines),encoding='utf-8')

    stats={'documents_screened':len(ledger),'physical_pages':local['summary']['physical_pages'],'claims_location_checked':len(claims),'evidence_location_checked':local['summary']['evidence_entries'],'deep_documents':len(deep),'deep_conclusions':sum(len(d['claims']) for d in deep),'deep_existing_claims_unique':len(set(existing)),'deep_additional_observations':sum(c.get('claim_id') is None for d in deep for c in d['claims']),'deep_visual_page_records':sum(len(d['visual_pages']) for d in deep),'review_findings':len(issue_rows),'online':online['summary'],'frozen_inputs_checked':len(local['frozen_input_sha256']),'source_pdf_hashes_checked':len(local['physical_source_pdf_manifest']),'formal_input_mismatches':mismatches}
    regression=read(OUT/'qa_regression_cases.json')
    assert regression['status']=='prepared_not_run_after_repairs'
    assert len(regression['cases'])==16
    for case in regression['cases']:
        for cid in case['claim_ids']:assert cid in claims, cid
    stats['prepared_regression_cases']=len(regression['cases'])
    stats['generation_regression_executed']=False
    for required in ['总报告.md','后续改进方案.md','deep/local_independent_check.json','deep/root_independent_check.json','deep/downstream_risk_check.json']:
        assert (OUT/required).exists(),required
    independent=read(OUT/'deep/root_independent_check.json')
    stats['root_independent_review_matches_current_report']=independent['reviewed_root_sha256']==sha(OUT/'deep/root.json')
    assert stats['root_independent_review_matches_current_report']
    dump(OUT/'acceptance.json',stats)
    assert not mismatches, mismatches
    print(json.dumps({k:v for k,v in stats.items() if k!='online'},ensure_ascii=False))

if __name__=='__main__':main()
