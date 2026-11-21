"""Prepare anonymous pilot review and audit usage without invoking any model."""
from __future__ import annotations

import argparse
from collections import defaultdict
import hashlib
import json
from pathlib import Path
import random

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / 'pilot/benchmarks/rag_comparison_20261116'


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf8')


def prepare(run_id):
    run = BENCH / 'runs' / run_id
    manifest = read(BENCH / 'frozen/manifest.json')
    gold = {x['id']: x for x in read(BENCH / 'frozen/gold.dev.json')}
    cases = []
    for system in ('current', 'lightrag', 'raganything'):
        for qid in manifest['pilot_question_ids']:
            path = run / system / 'queries' / (qid + '.json')
            if not path.is_file():
                raise ValueError(f'Missing attempted question: {system}/{qid}')
            row = read(path)
            cases.append((system, qid, row, path))
    random.Random(20261116).shuffle(cases)
    directory = BENCH / 'review' / ('blind-' + run_id)
    directory.mkdir(exist_ok=False)
    mapping = []
    for i, (system, qid, row, path) in enumerate(cases, 1):
        case = f'case-{i:02d}'
        payload = {
            'case_id': case,
            'question': row['question'],
            'runtime_status': row['status'],
            'answer': row.get('answer'),
            'raw_content': row.get('raw_content'),
            'delivered_context': row.get('context'),
            'included': row.get('included', []),
            'gold': {k: v for k, v in gold[qid].items() if k != 'id'},
            'review_instructions': (
                'Read the exact provided source excerpts and original PDF pages where necessary. '
                'Assess all required facts and qualifications, per-claim citation support and '
                'whether necessary evidence was actually delivered. Grade pass/partial/fail; '
                'mark critical metric/object/condition/unit errors. A valid citation ID alone '
                'is not evidence of semantic support. Missing gold facts count against full pass. '
                'Do not look at the identity mapping or run folders until review is submitted.'
            ),
        }
        write(directory / (case + '.json'), payload)
        mapping.append({'case_id': case, 'system': system, 'question_id': qid,
                        'result_path': path.relative_to(ROOT).as_posix(),
                        'result_sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
    write(BENCH / 'review' / ('identity-' + run_id + '.json'), mapping)
    print(json.dumps({'blind_cases': len(mapping), 'path': str(directory)}, ensure_ascii=False))


def usage(run_id):
    run = BENCH / 'runs' / run_id
    pricing = read(BENCH / 'review/pricing_basis.json')['reference']
    groups = defaultdict(lambda: {'attempts': 0, 'failures': 0, 'retry_attempts': 0,
                                   'unknown_usage_attempts': 0, 'input_tokens': 0,
                                   'output_tokens': 0, 'total_tokens': 0,
                                   'explicit_cached_input_tokens': 0, 'seconds': 0.0})
    for line in (run / 'events.jsonl').read_text(encoding='utf8').splitlines():
        event = json.loads(line)
        if event['event'] != 'llm':
            continue
        key = event['system'] + '/' + event['stage']
        group = groups[key]
        group['attempts'] += 1
        group['failures'] += bool(event.get('error'))
        group['retry_attempts'] += event.get('retry', 0) > 0
        group['seconds'] += event['seconds']
        u = event.get('usage') or {}
        if not all(type(u.get(k)) is int for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')):
            group['unknown_usage_attempts'] += 1
            continue
        group['input_tokens'] += u['prompt_tokens']
        group['output_tokens'] += u['completion_tokens']
        group['total_tokens'] += u['total_tokens']
        cached = u.get('prompt_cache_hit_tokens')
        if type(cached) is not int:
            cached = (u.get('prompt_tokens_details') or {}).get('cached_tokens')
        if type(cached) is int:
            if not 0 <= cached <= u['prompt_tokens']:
                raise ValueError('Provider cache usage is inconsistent with prompt usage.')
            group['explicit_cached_input_tokens'] += cached
    total = {key: sum(g[key] for g in groups.values()) for key in next(iter(groups.values()))}
    for group in [*groups.values(), total]:
        group['reference_no_cache_CNY'] = round((group['input_tokens'] * pricing['input_per_million_tokens']
                                                + group['output_tokens'] * pricing['output_per_million_tokens']) / 1e6, 6)
        group['reference_explicit_cache_CNY'] = round(group['reference_no_cache_CNY']
            - group['explicit_cached_input_tokens'] * (pricing['input_per_million_tokens']
                                                       - pricing['cached_input_per_million_tokens']) / 1e6, 6)
    result = {'run_id': run_id, 'cost_status': 'Reference tariff estimate; not account invoice',
              'pricing_reference': pricing, 'groups': dict(groups), 'total': total,
              'unknown_usage_rule': 'No usage means unknown cost, not zero. See unknown_usage_attempts.',
              'exclusions': ['Codex agent/subscription usage', 'local electricity', 'account-specific credits/discounts']}
    write(BENCH / 'reports' / ('usage-' + run_id + '.json'), result)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=['blind', 'usage'])
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    if Path(args.run_id).name != args.run_id or any(x in args.run_id for x in '/\\:'):
        raise ValueError('Run ID must be a simple directory name.')
    (prepare if args.action == 'blind' else usage)(args.run_id)


if __name__ == '__main__':
    main()
