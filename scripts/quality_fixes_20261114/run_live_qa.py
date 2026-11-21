"""Exercise the running local API with the configured provider; never reads keys."""
import argparse
import datetime
import json
from pathlib import Path
import time
import urllib.request
import urllib.error

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT/'pilot/reports/quality_fixes_20261114'
BASE = 'http://127.0.0.1:8600'
CASES = ['unit_pad','unit_die','misalignment_conflict','stack_12','no_drop','ipc_filters','gaostack']


def call(path, payload=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode('utf-8')
    request = urllib.request.Request(BASE+path, data=data, headers={'Content-Type':'application/json'})
    with urllib.request.urlopen(request, timeout=240) as response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--only', nargs='*')
    options = parser.parse_args()
    cases = json.loads((ROOT/'pilot/reports/quality_review_20261113/qa_regression_cases.json').read_text('utf-8'))['cases']
    selected = [c for c in cases if c['case_id'] in CASES]
    selected.extend([
        dict(case_id='kim_sam', question='Kim、Nguyen、Choa 2022年那篇关于晶圆边缘空洞的论文，SAM能检出多大的空洞？200 mm能直接改成200 µm吗？',
             expected_boundary='正文200 mm及图50nm/50 mm不得混同检出限，不猜200 µm。'),
        dict(case_id='kim_distance', question='Kim、Nguyen、Choa 2022年那篇论文图12，空洞距晶圆边缘是2.5 mm还是2.5 µm？',
             expected_boundary='并列正文与图轴单位冲突，不选定正确单位。'),
    ])
    if options.only:
        selected = [c for c in selected if c['case_id'] in options.only]
    for case in selected:
        started = time.monotonic()
        record = dict(case=case, started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                      method='real_http_configured_provider', semantic_review='pending')
        try:
            record['response'] = call('/api/chat', {'question':case['question'],'history':[]})
            record['status'] = 'generated' if record['response'].get('generated') else 'no_generation'
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError) as error:
            record['status'] = 'request_failed'
            # HTTP detail is from the local application's sanitized exception handler.
            record['error'] = error.read().decode('utf-8', errors='replace') if isinstance(error, urllib.error.HTTPError) else type(error).__name__
        record['seconds'] = round(time.monotonic()-started, 2)
        (REPORT/f'live_qa_{case["case_id"]}.json').write_text(json.dumps(record,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
        print(json.dumps(dict(case_id=case['case_id'],status=record['status'],seconds=record['seconds']),ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()
