"""Use the actual local BGE/Qdrant retriever without invoking the chat provider."""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT/'src'), str(ROOT)]
from kg_pilot import qa, innovation

REPORT = ROOT/'pilot/reports/quality_fixes_20261114'
TARGETS = {
    'unit_pad': ('doc_0086822b49e2d5df8658', [9]),
    'unit_die': ('doc_0086822b49e2d5df8658', [13]),
    'misalignment_conflict': ('doc_b9a25e99b026b40039f4', [4]),
    'stack_12': ('doc_0086822b49e2d5df8658', [16]),
    'no_drop': ('doc_0086822b49e2d5df8658', [17]),
    'ipc_filters': ('doc_e32cb835d14010241a5a', [1,4]),
    'kim_sam': ('journals_mi13040537', [5,6]),
    'kim_distance': ('journals_mi13040537', [12]),
}


def main():
    assert qa.retrieval_model() is not None, 'The real local retrieval model must be available.'
    rows = []
    for case_id, (doc_id, pages) in TARGETS.items():
        question = json.loads((REPORT/f'live_qa_{case_id}.json').read_text('utf-8'))['case']['question']
        started = time.monotonic()
        sources, warnings = qa.retrieve_sources(question)
        held = {s['pdf_page'] for s in sources if s['doc_id']==doc_id}
        contexts = [qa._source_context(s) for s in sources]
        result = dict(case_id=case_id,question=question,target_doc_id=doc_id,expected_pages=pages,
                      retrieved_target_pages=sorted(held), warnings=warnings, seconds=round(time.monotonic()-started,2),
                      passed=set(pages)<=held and not any('失败' in w or '不可用' in w for w in warnings),
                      contexts=contexts)
        rows.append(result)
        print(json.dumps({k:result[k] for k in ['case_id','passed','retrieved_target_pages','warnings','seconds']},ensure_ascii=False),flush=True)
    previews = {}
    for mode, prompt in [('guided','这项Cu/polymer键合证明了1.82nm对准精度吗？'),('random','')]:
        preview = innovation.preview_ideas(prompt,count=1)
        previews[mode] = preview
    report = dict(method='actual_local_model_and_qdrant_no_chat_generation',cases=rows,
                  innovation_previews=previews,passed=all(x['passed'] for x in rows))
    (REPORT/'dense_retrieval.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n','utf-8')
    return 0 if report['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
