"""Build source-bound review sidecars; never modifies the production corpus."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'src'))
from kg_pilot.map_relations import claims_digest
from kg_pilot.map_semantics import digest

PILOT = ROOT / 'pilot'
REPORT = PILOT / 'reports/quality_fixes_20261114'
ADEIA = 'doc_0086822b49e2d5df8658'
LEE = 'doc_b9a25e99b026b40039f4'
KIM = 'journals_mi13040537'
IPC = 'doc_e32cb835d14010241a5a'


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def qualification(identifier, kind, ids, pages, summary, usage, evidence, **kwargs):
    return dict(id=identifier, kind=kind, claim_ids=ids, pdf_pages=pages,
                summary=summary, usage_limit=usage, evidence=evidence, **kwargs)


def ev(page, quote, locator=None):
    result = dict(pdf_page=page, quote=quote)
    if locator:
        result['locator'] = locator
    return result


def build():
    claims = [json.loads(line) for line in (PILOT/'annotations/claims.jsonl').read_text('utf-8').splitlines() if line]
    papers = {p['doc_id']: p for p in read(PILOT/'catalog/papers.json')}
    preview = read(REPORT/'source_patch_plan.json')['documents'][0]['source_review']
    patched = {c['claim_id']: c for c in preview['claims_after']}
    postclaims = [patched.get(c['claim_id'], c) for c in claims]
    c = lambda suffix: ADEIA + '_' + suffix
    entries = {
        ADEIA: [
            qualification('symbol-font-units', 'transcription_correction', list(patched),
                [4, 6, 7, 8, 9, 10, 11, 13, 14, 16, 17, 18, 19],
                '已按原 PDF 的 SymbolMT 字形恢复微米和欧姆：焊盘直径2–15 µm，芯片厚度50/200 µm，每个互连约10 mΩ；芯片边长8×12 mm仍为毫米。',
                '使用修复后的单位。约80%是电测最大通过偏移与焊盘直径的比值，不能写成良率或设备对准精度；单位修复不增加原文未报告的材料和统计信息。',
                [ev(9, 'Pad size ranging from 2 - 15 µm; ~10 mΩ per link', '原页字形'),
                 ev(13, '8 x 12 mm Die with 10 µm bond pads, thinned to 50 or 200 µm', '原页字形'),
                 ev(16, '8 x 12 mm x 50 µm thick die with 15 µm bond pads', '原页字形'),
                 ev(17, '8 x 12 mm x 200 µm thick Die with 10 µm bond pads', '原页字形')]),
            qualification('thin-die-equivalence-limit', 'source_limit', [c('c004')], [13],
                '作者称50 µm和200 µm两组良率统计等效；该页未给出各批芯片数、等效界值或检验方法。',
                '可归因给作者并描述已测载具；不能将此表述作为经独立统计验证的普遍等效结论。',
                [ev(13, 'No decrease in yield for the thinner 50 µm die vs. 200 µm die (statistically equivalent)')]),
            qualification('twelve-die-yield-population', 'source_limit', [c('c005')], [16],
                '12芯片堆叠页的56%指整体无空洞堆叠良率；图中从48个堆叠到第12层剩27个，27/48≈56.25%。页面另报初始颗粒异常后的单层良率95%，未给出该95%的计算方法。',
                '不能把56%或95%称为每个互连的电学良率，也不能不加假设地用95%的幂重建56%。左侧照片沿用早期8层样品，不能作为12层横截面照片证据。',
                [ev(16, '12-die stack had overall yield of 56%'),
                 ev(16, 'Void Free Yield; Stack Count: 48 at Die 1, 27 at Die 12', '原图坐标和计数目读'),
                 ev(16, 'After an initial particle contamination excursion')]),
            qualification('anneal-text-plot-conflict', 'source_conflict', [c('c007')], [17],
                '正文称降至200 °C电学良率未下降；同页1-Center图在200 °C约94–95%，220/240 °C约100%，2-Right和3-Corner图约100%。图读为近似值。',
                '必须并列作者表述和图示差异，不能断言三个温度的中心阵列良率完全相同。未报告各组样本数、误差或检验，不能判断统计显著性；200 °C以下仅为后续计划。',
                [ev(17, 'No drop in electrical yield down to 200°C', '正文'),
                 ev(17, '1-Center electrical yield: about 94–95% at 200°C, about 100% at 220°C and 240°C', '原图近似目读')])],
        LEE: [qualification('alignment-text-figure-conflict', 'source_conflict', [LEE+'_c009'], [4],
                '正文写错位1.82 nm，Fig. 8横向标注1.81 µm，两者冲突；图中1.76 µm是竖向标注。当前不能确定唯一正确的错位数值。',
                '并列正文和图示，不选定单值，不作为设备对准精度、性能排名或工艺能力的定量基准。原文冲突不等于抽取乱码。',
                [ev(4, 'of 1.82 nm.', '正文'), ev(4, '1.81 µm', 'Fig. 8横向标注'), ev(4, '1.76 µm', 'Fig. 8竖向标注')], disputed_value=True)],
        KIM: [
            qualification('sam-threshold-scale-unit-conflicts', 'source_conflict', ['j036'], [5, 6],
                'SAM检出门槛正文印为200 mm；Fig. 4另有50nm和50 mm两种标尺文字。检出门槛与图像标尺不是同一个量，均不推定其正确单位。',
                '不得自动把门槛改成200 µm，也不得把图像标尺当作检出门槛。可保留p10关于翘曲与边缘空洞的定性趋势，但不据此推导检出限或可比空洞率。',
                [ev(5, 'Using this system, we could characterize voids larger than 200 mm in size.'), ev(6, '50nm / 50 mm', 'Fig. 4双标尺目读')], related_claim_only=True),
            qualification('edge-distance-text-axis-conflict', 'source_conflict', ['j036'], [12],
                '正文写距晶圆边缘向内2.5 mm，Fig. 12横轴标注µm，正文与图轴单位冲突。',
                '不得将2.5导入统一边缘排除距离或跨论文排序。可保留p10的定性趋势，不能把未知单位当成已知工艺窗口。',
                [ev(12, 'distance of 2.5 mm inward from the wafer edge.'), ev(12, 'Distance from wafer edge (μm)', 'Fig. 12横轴')], related_claim_only=True)],
        IPC: [qualification('body-appendix-filter-conflict', 'source_conflict', [IPC+'_c002',IPC+'_c006',IPC+'_c007',IPC+'_c008'], [1, 4],
                '正文5.2规定不用滤波器；附录A.2.2的50X、NA 0.95、200×1000 µm扫描配置列S-filter 2 µm、L-filter 0.2 mm及平面校正。两种配置在同份方法中并存。',
                '说明正文规则与附录配置各自出处和情境，不能无说明拼成一套实验步骤或全局禁止/要求滤波。附录参数本身可以引用，不能因此擅自判定正文规则失效。',
                [ev(1, 'No filters shall be used with this test method.', '5.2'),
                 ev(4, '50X, NA: 0.95; 200 x 1000 µm; S-filter: 2 µm; F-operation: Plane Correction; L-filter: 0.2 mm', 'A.2.2具体配置')])]
    }
    aliases = {
        'alignment-text-figure-conflict': [['1.82nm','Cu/polymer'],['1.81µm','Cu/polymer'],['1.82nm','Lee'],['nc-cu/polymer','错位']],
        'body-appendix-filter-conflict': [['ipc2.2.22','滤波'],['2.2.22','filter'],['ipc-tm-650','2.2.22','滤波']],
        'sam-threshold-scale-unit-conflicts': [['kim','nguyen','choa','sam'],['effect of the wafer warpage on the hybrid bonding','sam'],['晶圆翘曲','sam','空洞']],
        'edge-distance-text-axis-conflict': [['kim','nguyen','choa','晶圆边缘'],['kim','fig.12','edge'],['晶圆边缘','倒角区','空洞']],
        'anneal-text-plot-conflict': [['adeia','退火','200'],['adeia','anneal','200']],
    }
    for rows in entries.values():
        for row in rows:
            if row['id'] in aliases:
                row['query_aliases'] = aliases[row['id']]
    documents = []
    for doc, qs in entries.items():
        paper = papers[doc]
        assert sha(Path(paper['local_path'])) == paper['sha256']
        documents.append(dict(doc_id=doc, source_sha256=paper['sha256'],
            claims_digest=claims_digest([x for x in postclaims if x['doc_id']==doc]), qualifications=qs))
    manifest = dict(schema=1, reviewed_at='2026-11-14',
                    review_method='source_pages_and_independent_agent_review_not_human_gold', documents=documents)
    write(REPORT/'evidence_qualifications.prepared.json', manifest)
    updates = {}
    def add(relative, value):
        path = PILOT/relative
        updates[relative] = dict(expected_sha256=sha(path) if path.exists() else None, value=value)
    add('catalog/evidence_qualifications.json', manifest)
    # Keep the first pass as its original source record; explicitly re-review the
    # same two edges after the four claim corrections, with an attributed result.
    reviewed = read(PILOT/'research_map/reviewed_relations.json')
    adeia = next(d for d in reviewed['documents'] if d['doc_id']==ADEIA)
    old_doc = copy.deepcopy(adeia)
    new_digest = next(d['claims_digest'] for d in documents if d['doc_id']==ADEIA)
    adeia['claims_digest'] = new_digest
    add('research_map/reviewed_relations.json', reviewed)
    semantic = read(PILOT/'research_map/semantic_reviews.json')
    current = next(d for d in semantic['documents'] if d['doc_id']==ADEIA)
    before = copy.deepcopy(current)
    current['claims_digest'] = new_digest
    packet = dict(doc_id=ADEIA, source_sha256=papers[ADEIA]['sha256'], previous=before,
        claims_after=patched, qualification=entries[ADEIA][-1],
        verified_pages=[8,9,17], reviewer='Codex root', reviewed_at='2026-11-14')
    write(REPORT/'map_review_packet.json', packet)
    packet_sha = sha(REPORT/'map_review_packet.json')
    for idx, decision in enumerate(current['decisions']):
        decision['prior_assessment'] = decision['assessment']
        decision['assessment'] = dict(model='codex-root', candidate_id=idx, packet_sha256=packet_sha,
            reason='按原PDF复核：偏移比值仍成立；退火页必须区分作者表述和图示近似值。')
        if decision['claim_id'] == c('c007'):
            decision['status'] = 'replaced'
            decision['reason'] = '正文结论和图示不完全一致，改为有出处的作者表述并加入图示限制。'
            decision['issue'] = 'text_figure_conflict'
            decision['link']['relation'] = 'condition'
            decision['link']['scope'] = '第17页同一键合晶圆的分片在200、220、240 °C分别退火2小时；作者称降至200 °C电学良率未下降。'
            decision['link']['limit'] = entries[ADEIA][-1]['summary'] + entries[ADEIA][-1]['usage_limit']
            confirmation_path = REPORT/'adeia_c007_map_independent_review.json'
            if not confirmation_path.exists():
                write(REPORT/'map_candidate.prepared.json', decision['link'])
                raise SystemExit('Prepared manifest and map candidate; waiting for independent confirmation file.')
            confirmation = read(confirmation_path)
            assert confirmation.get('accepted') is True
            decision['confirmation'] = dict(model='gpt-6.1-sol-ultra', candidate_id=idx,
                packet_sha256=sha(confirmation_path), accepted=True, link_digest=digest(decision['link']),
                reason=confirmation['usage_limit'])
    add('research_map/semantic_reviews.json', semantic)
    write(REPORT/'map_revalidation.json', dict(before_reviewed=old_doc, before_semantic=before,
        after_reviewed=adeia, after_semantic=current, source_pages=[8,9,17]))
    write(REPORT/'extra_updates.json', updates)
    print(json.dumps(dict(documents=len(documents), qualifications=sum(len(v) for v in entries.values()),
        extra_updates=list(updates), post_claims_digest=new_digest), ensure_ascii=False))


if __name__ == '__main__':
    build()
