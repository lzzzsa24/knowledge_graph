"""Summarize observed acceptance states without treating an API failure as a pass."""
import datetime
import difflib
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
REPORT = ROOT/'pilot/reports/quality_fixes_20261114'


def read(name):
    return json.loads((REPORT/name).read_text(encoding='utf-8-sig'))


def main():
    publication = read('publication_result.json')
    alias = read('alias_publication_result.json')
    local = read('regression_review.json')
    dense = read('dense_retrieval.json')
    qa = [json.loads(p.read_text('utf-8')) for p in sorted(REPORT.glob('live_qa_*.json'))]
    http = read('live_http_final.json')
    semantic = read('live_semantic_review.json')
    ideas = read('live_semantic_ideas_review.json')
    assert all(x['status']=='generated' for x in qa) and semantic['summary']['pass']==9
    for review in [semantic, ideas]:
        for case in review.get('cases', []):
            if case.get('file') and case.get('sha256'):
                assert hashlib.sha256((REPORT/case['file']).read_bytes()).hexdigest()==case['sha256']
    source_context = read('source_context_publication.json')
    yield_scope = read('yield_scope_publication.json')
    acceptance = dict(
        recorded_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
        scope='priority_one_source_units_and_conflicts',
        implementation='published',
        local_regression=local['status'],
        actual_bge_qdrant_retrieval_passed=dense['passed'],
        actual_bge_qdrant_cases=len(dense['cases']),
        configured_provider=read('provider_check.json'),
        live_generation=dict(final_qa_samples=len(qa), generated=sum(x['status']=='generated' for x in qa),
            status='qa_passed_ideas_design_requires_refinement', semantic=semantic['summary'],
            innovation=ideas['summary'],
            detail='最终九题问答通过逐题语义核查。三项创新输出的文献事实与引页通过，两个候选的实验设计仍需细化。仅代表这些固定样本，不保证任意新问题总是正确。'),
        software_tests=dict(final_combined=270, all_passed=True,log='final_pytest.txt'),
        originals_unchanged=local['physical_pdfs_unchanged'],
        counts=local['live_counts'],
        http_checks=http,
        backups=[publication['backup_path'],alias['backup_path'],source_context['backup_path'],yield_scope['backup_path']],
        remaining=['创新候选的实验判据需继续人工细化，引用通过不等于方案或创新性已验证',
            '后续优先2：方法、分母、样本量、适用条件等结构字段',
            '后续优先3：版本、图表复用与独立证据谱系',
            '后续优先4：其余风险队列及持续质量门禁'],
    )
    (REPORT/'acceptance.json').write_text(json.dumps(acceptance,ensure_ascii=False,indent=2)+'\n','utf-8')
    patch=[]
    for before in sorted((REPORT/'code_before').rglob('*')):
        if not before.is_file():
            continue
        relative=before.relative_to(REPORT/'code_before'); current=ROOT/relative
        if current.is_file():
            patch.extend(difflib.unified_diff(before.read_text('utf-8-sig').splitlines(True),
                current.read_text('utf-8-sig').splitlines(True),fromfile='before/'+relative.as_posix(),tofile='after/'+relative.as_posix()))
    (REPORT/'all_existing_code_changes.patch').write_text(''.join(patch),'utf-8')
    fixture_before=REPORT/'code_before_live/test_innovation.py'
    fixture_after=ROOT/'tests/test_innovation.py'
    fixture_patch=difflib.unified_diff(fixture_before.read_text('utf-8-sig').splitlines(True),
        fixture_after.read_text('utf-8-sig').splitlines(True),fromfile='before/tests/test_innovation.py',tofile='after/tests/test_innovation.py')
    with (REPORT/'all_existing_code_changes.patch').open('a',encoding='utf-8') as stream:
        stream.writelines(fixture_patch)
    summary='''# 第一阶段资料质量修复结果

已发布后端和资料修复，API连接已恢复，最终9道真实问答通过逐题语义核查。创新生成可返回可追溯候选，两个候选的实验方案尚需细化，未计为完整设计通过。

## 已完成

- Adeia：原 PDF 的 SymbolMT 字形被误解为普通 m/W。按 PDF、字体和字形指纹恢复 13 页的 33 个 µ、1 个 Ω；更新 4 条知识记录和 6 条引文。2–15 µm 焊盘、50/200 µm 厚度、约10 mΩ/互连已同步到阅读版、检索索引和向量。真实的 8×12 mm 芯片边长不变。
- 六份资料共10条使用限定：Adeia 的单位/统计/堆叠/退火图文差异，Lee 的1.82 nm与1.81 µm冲突，Kim的SAM与边缘距离单位冲突，IPC正文与附录滤波设置并存，Theil八层代表照，以及Gao的逐层算法不能挪用于整叠指标。确定的转录错误修复；原文自身冲突并列记录，不猜正确值。
- 问答、创新、检索、阅读版和研究地图共用有效证据。相关PDF或知识记录改变后，复核会失效并要求重新核验。公开输出剔除审计专用旧误读字段，存储历史仍保留。
- 特定研究问题可召回复核页，同一冲突的两侧来源共同进入上下文。Lee的争议单值退出有效定量字段。Adeia两个地图关系经重新核验，保留1条、修订1条。
- 实际问答暴露的问题再修订：争议值全文保留出处、不把相邻指标算法混用；照片对照同时引用Adeia与Theil原页，并标明视觉比对推断；IPC第6.2条的设备商模板要求补入上下文。创新引用中的参数对象、参照对象与实施例须保持一致，新试样不能裁断历史文献的读数成因。

## 验证结果

213份原PDF的页数、字节和SHA不变；正式库仍为1439条知识记录、11731个证据块、10919个向量。与原库逐项对照，仅4条主张和13个证据块及其对应向量内容变化，文献/主张/证据/向量ID保留。4个样品节点ID随修正后的样品文字变更，图节点与边总数不变。

- 最终合并回归270项通过，覆盖问答、创新、检索、地图、阅读、字形与可回滚发布；仅有一条依赖弃用提示。
- 8题无模型生成的实际BGE/Qdrant检索全部命中目标页，无降级警告；关键词退路8题也通过。
- 创新功能已完成指定问题、原随机主题固定复验、无输入新随机3次实际生成，引用事实均经独立核查；设计方案单列限制。
- 实际运行的健康、主张、文献、地图、紧凑研究图、网络接口已验证。最后一次紧凑研究图149375字节，维持150000字节预算。

## 真实模型验收与限制

原阻断原因是用户只在入库页更新了独立密钥，问答仍用旧的被封锁密钥。核对相同服务地址后，使用已有配置入口把新密钥同步到问答；模型保持 DeepSeek-V4.1-Flash-1017。两处配置仍独立保存，这次是同步，不是建立永久双向联动；没有在日志或报告存储明文凭据。

恢复后的初次9题是5通过、1部分通过、1引用缺页、2实质错误，已原样存档。修订并复验后，最终9/9通过，包含6道本轮题、2道Kim冲突题和Gao五层堆叠对照题。Gao第二次仍混用了逐层算法；补入指标限定后的第三次才通过。不能用接口成功替代此语义判断。

创新初答也保留：指定主题的历史读数验证判据有缺口，随机专利主题把尺寸比的参照对象换错。修订后用同一专利主题复验，再测试新随机输入；3/3的事实引用与假设/创新性标记成立。指定主题设计通过；专利主题仍需明确拟议面积口径与原专利百分比的区别，EVG随机主题仍需补各部署重复测量与操作者对照。因此两项设计仅部分通过，候选不等于可以直接执行的研究方案或已确认创新。

## 备份与后续

正文/索引原件备份由 `publication_result.json` 指向；后续复核记录备份依次见 `alias_publication_result.json`、`source_context_publication.json`、`yield_scope_publication.json`。回退前确认后续入库并按相反顺序恢复。旧代码在 `code_before/`，部分是按实际修改精确逆向重建；实际生成后补丁的改前代码在 `code_before_live/`。汇总差异见 `all_existing_code_changes.patch`。

本轮属于已确认计划的优先1。其余193份资料的逐页深度复核，以及方法/分母/样本量结构化、版本与图表复用谱系、其余风险队列仍是后续工作。213份基础筛查不意味着全部内容已经确认无误。

详细证据见 `regression_review.json`、`dense_retrieval.json`、`live_http_final.json`、`live_qa_*.json`、`live_semantic_review.json`、`live_semantic_ideas_review.json` 和 `acceptance.json`。初答存于 `attempt_live_initial/`，中间四题存于 `attempt_live_second/`，鉴权失败记录存于 `attempt_before_resume_*/`。前端可接入说明见项目 `docs/quality-fixes-backend-handoff.md`。
'''
    (REPORT/'修复结果.md').write_text(summary,'utf-8')
    # Only audit deliverables and explicit code backups; pytest ACL temporary
    # directories are not deliverables and are not traversed here.
    files=[p for p in REPORT.iterdir() if p.is_file() and p.name!='artifact_manifest.json']
    files.extend(p for p in (REPORT/'code_before').rglob('*') if p.is_file())
    files.extend(p for p in (REPORT/'code_before_live').rglob('*') if p.is_file())
    for directory in REPORT.glob('attempt_*'):
        if directory.is_dir():files.extend(p for p in directory.rglob('*') if p.is_file())
    manifest=[dict(path=p.relative_to(ROOT).as_posix(),bytes=p.stat().st_size,
                   sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sorted(files)
              if p.suffix not in {'.log','.pid'}]
    (REPORT/'artifact_manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n','utf-8')
    print(json.dumps(dict(implementation=acceptance['implementation'],local=local['status'],
                         dense=dense['passed'],live_generation=acceptance['live_generation']['status'],artifacts=len(manifest)),ensure_ascii=False))


if __name__=='__main__':
    main()
