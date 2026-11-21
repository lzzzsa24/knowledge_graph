"""Run one guided and one random innovation request through the real local API."""
import datetime
import json
from pathlib import Path
import time
import urllib.request
import urllib.error

ROOT=Path(__file__).resolve().parents[2]
REPORT=ROOT/'pilot/reports/quality_fixes_20261114'
CASES=[('guided','围绕低温Cu/polymer混合键合的对准与聚合物形变生成一个研究方向。这项Cu/polymer键合证明了1.82nm对准精度吗？请区分原文事实与待验证设想。'),('random','')]


def main():
    cases=list(CASES)
    original_path=REPORT/'attempt_live_initial/live_ideas_random.json'
    if original_path.exists():
        original=json.loads(original_path.read_text('utf-8'))['response']
        title=original['sources'][0]['title']
        cases.insert(1,('random_focus_retest',f"围绕原随机起点‘{original['focus']}’和资料《{title}》生成一个具体研究候选，区分文献事实和待验证假设。"))
    for mode,prompt in cases:
        started=time.monotonic()
        result=dict(mode=mode,prompt=prompt,started_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    method='real_http_configured_provider',semantic_review='pending')
        request=urllib.request.Request('http://127.0.0.1:8600/api/ideas',
            data=json.dumps(dict(prompt=prompt,history=[],count=1),ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type':'application/json'})
        try:
            with urllib.request.urlopen(request,timeout=240) as response:
                result['response']=json.load(response)
            result['status']='generated' if result['response'].get('generated') else 'no_generation'
        except (urllib.error.HTTPError,urllib.error.URLError,TimeoutError,ValueError) as error:
            result['status']='request_failed'
            result['error']=error.read().decode('utf-8',errors='replace') if isinstance(error,urllib.error.HTTPError) else type(error).__name__
        result['seconds']=round(time.monotonic()-started,2)
        (REPORT/f'live_ideas_{mode}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n','utf-8')
        print(json.dumps({k:result[k] for k in ['mode','status','seconds']},ensure_ascii=False),flush=True)


if __name__=='__main__':main()
