"""Publish two source-page clarifications found in live-answer review."""
from copy import deepcopy
import json
from pathlib import Path
import sys
import uuid

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'src'))
from kg_pilot import ingestion_publish as publication
from kg_pilot.pipeline import jsonl, read_json, write_json
from kg_pilot.map_relations import claims_digest
from kg_pilot.effective_evidence import load_effective_evidence


def main():
    pilot=ROOT/'pilot';report=pilot/'reports/quality_fixes_20261114'
    relative='catalog/evidence_qualifications.json'
    original=read_json(pilot/relative); updated=deepcopy(original)
    for document in updated['documents']:
        for item in document['qualifications']:
            if item['id']=='twelve-die-yield-population':
                item['usage_limit']='不能把56%或95%称为每个互连的电学良率，也不能不加假设地用95%的幂重建56%。Adeia第16页照片与Theil 2022第2页Fig. 1的8层代表照在视觉上相同；这是跨来源图像比对推断，两份原文没有明示照片复用关系或试验样品身份。不能把该照片作为12层独立图证，也不能由照片推断56%的数据来自8层试验。回答照片归属时须同时引用两页，区分原图注与比对推断。'
            if item['id']=='body-appendix-filter-conflict':
                item['pdf_pages']=[1,2,4]
                item['usage_limit']='说明正文规则与附录配置各自出处和情境，不能无说明拼成一套实验步骤，也不能自行宣布二者可任选或一条覆盖另一条。第2页6.2要求咨询设备制造商确定保证符合本方法的具体测量模板；实际采用哪套配置还应向方法采用方或发布方澄清，并记录条款与参数。附录参数可以如实引用。'
                item['evidence']=[e for e in item['evidence'] if e['pdf_page']!=2]+[{
                    'pdf_page':2,'quote':'The equipment manufacturer should be consulted to define the specific measurement machine template needed to assure compliance with this measurement procedure.','locator':'6.2'}]
    doc='reliability_die_edge_2022'
    papers=read_json(pilot/'catalog/papers.json');claims=jsonl(pilot/'annotations/claims.jsonl')
    paper=next(p for p in papers if p['doc_id']==doc)
    entry={'doc_id':doc,'source_sha256':paper['sha256'],
           'claims_digest':claims_digest([c for c in claims if c['doc_id']==doc]),
           'qualifications':[{
               'id':'representative-eight-die-photo','kind':'source_limit','claim_ids':[],'pdf_pages':[2],
               'summary':'Theil 2022第2页Fig. 1的原图注明确写为代表性的DBI键合8芯片堆叠照片。',
               'usage_limit':'该照片不是12层的原始图证。与Adeia 2023第16页照片视觉上相同属于跨来源比对推断，应同时引用两处并说明推断性质；两文未明示照片复用关系、试验样品身份或12层数据来自8层试验。',
               'evidence':[{'pdf_page':2,'quote':'Photograph of a representative DBI bonded 8-die stack.','locator':'Fig. 1 caption'}],
               'query_aliases':[['12层','56%','照片'],['12芯片','照片'],['Adeia','堆叠','照片'],['12-die','photograph'],['Theil','8','照片']]
           }]}
    existing=next((d for d in updated['documents'] if d['doc_id']==doc),None)
    if existing is None:updated['documents'].append(entry)
    elif existing!=entry:raise ValueError('Theil source review changed; review before replacing it')
    if updated==original:
        print('already_published');return
    fingerprint={relative:publication._sha(pilot/relative),
                 'annotations/claims.jsonl':publication._sha(pilot/'annotations/claims.jsonl'),
                 'catalog/papers.json':publication._sha(pilot/'catalog/papers.json')}
    for d in updated['documents']:
        p=next(p for p in papers if p['doc_id']==d['doc_id']);path=Path(p['local_path'])
        assert publication._sha(path)==p['sha256']==d['source_sha256']
        fingerprint[str(path.resolve())]=p['sha256']
    batch='quality-source-context-'+uuid.uuid4().hex;stage=pilot/'ingestion/staging'/batch
    write_json(stage/relative,updated)
    backup=publication._commit(stage,pilot,[Path(relative)],fingerprint,batch)
    view=load_effective_evidence(pilot)
    assert all(q['status']=='active' for d in updated['documents'] for q in view.qualifications(d['doc_id']))
    result=dict(status='committed',batch_id=batch,backup_path=str(backup),documents=len(updated['documents']),
                qualifications=sum(len(d['qualifications']) for d in updated['documents']),
                reason='Expose original eight-die photo caption and IPC section 6.2, keep image comparison as inference')
    write_json(report/'source_context_publication.json',result)
    write_json(report/'evidence_qualifications.final.json',updated)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':main()
