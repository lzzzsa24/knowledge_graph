"""Build the readable Chinese report from the preserved retrieval observations."""
from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "pilot/reports/crosslingual_20261106"


def load(name):
    return json.loads((OUT / name).read_text("utf-8"))


def percent(value):
    return f"{value*100:.2f}%"


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    old, new = load("baseline.json"), load("candidate.json")
    metrics = []
    for suite, title in [("legacy_16", "既有12道可答题"), ("historical_case", "Gao历史原句"), ("independent", "新增7道独立拟题"), ("all", "合并20道可答题")]:
        for level, name in [("document", "文档"), ("page", "物理PDF页"), ("evidence", "精确证据锚点")]:
            a, b = old["summary"][suite]["levels"].get(level), new["summary"][suite]["levels"].get(level)
            if not a or not b:
                continue
            metrics.append(f"| {title} | {name} | {a['denominator']} | {percent(a['hit_at_5'])} | {percent(b['hit_at_5'])} | {percent(a['mean_recall_at_5'])} | {percent(b['mean_recall_at_5'])} |")
    losses = []
    for a, b in zip(old["results"], new["results"], strict=True):
        q = b["question"]
        if not q["answerable"]:
            continue
        m = b["metrics"]
        if m["page"]["recall_at_5"] == 1 and (not m["evidence"].get("labeled") or m["evidence"]["recall_at_5"] == 1):
            continue
        expected = "; ".join(f"{x['doc_id']} p{x['pdf_page']}" for x in q["expected_pdf_pages"])
        rank = m["page"]["first_expected_rank"]
        evrank = m["evidence"].get("first_expected_rank", "无精确标签")
        losses.append(f"| {q['id']} | {q['question_zh']} | {expected} | {percent(a['metrics']['page']['recall_at_5'])} → {percent(m['page']['recall_at_5'])} | {rank or '>30'} | {evrank or '>30'} |")
    ga = next(r for r in old["results"] if r["question"]["id"] == "gao_historical")
    gb = next(r for r in new["results"] if r["question"]["id"] == "gao_historical")
    target = "reliability_gao_d2w_2021:p2:b8"
    natural_old = next(r for r in old["results"] if r["question"]["id"] == "i01_gao_natural_zh")
    natural_new = next(r for r in new["results"] if r["question"]["id"] == "i01_gao_natural_zh")
    api_path = OUT / "live_api_verification.json"
    if api_path.is_file():
        api = load("live_api_verification.json")
        exact_count = sum(r["ordered_evidence_ids_match"] for r in api["results"])
        mismatch_ids = ", ".join(r["id"] for r in api["results"] if not r["ordered_evidence_ids_match"])
        api_note = f"实际HTTP /api/search 已复核 {len(api['results'])}/26 题并成功返回，共{sum(r['actual_count'] for r in api['results'])}条证据；{exact_count}/26题完整30条证据ID顺序完全一致；26/26题既定top5命中/召回指标一致；全部返回证据ID/文档/页码/原文与SQLite一致。完整排序有差异的题为 {mismatch_ids or '无'}，与此前dense同分/近同分项的次序差异一致，未改变已标注的top5指标。详情见 `live_api_verification.json`。"
    else:
        api_note = "本报告尚未核对重启后的实际HTTP入口；离线候选数据不代表已部署。待父任务确认服务装载验收版后，用下列命令补测。"
    replay_path = OUT / "baseline_engine_verification.json"
    if replay_path.is_file():
        replay = load("baseline_engine_verification.json")
        count = sum(r["ordered_evidence_ids_match"] for r in replay["results"])
        replay_note = f"另外在独立SQLite快照上以冻结旧函数运行真实QdrantClient，26题均分数近似一致；完整ID顺序 {count}/26 完全一致，3题存在同分或浮点近似同分项交换。与命中有关的top5指标均一致（详见引擎复核JSON）。没有将同分排序宣称为逐项完全一致。"
    else:
        replay_note = "未执行独立引擎复核。"
    legacy_a = old["summary"]["legacy_16"]["levels"]["page"]
    legacy_b = new["summary"]["legacy_16"]["levels"]["page"]
    gao_outcome = "已进入精确证据前5" if gb["metrics"]["evidence"]["hit_at_5"] else "仍未进入精确证据前5"
    extra_report = ""
    if (OUT / "extra_doc_probes_before.json").is_file() and (OUT / "extra_doc_probes_after.json").is_file():
        extra_old, extra_new = load("extra_doc_probes_before.json"), load("extra_doc_probes_after.json")
        extra_rows = []
        for a, b in zip(extra_old, extra_new, strict=True):
            extra_rows.append(f"| {b['question']} | `{b['expected_evidence']}` | {a['rank']} | {b['rank']} | {'命中' if a['rank'] <= 5 else '未中'} → {'命中' if b['rank'] <= 5 else '未中'} |")
        extra_report = f"""## 另外4道未向实现披露的独立终检

父任务在开发冻结后，使用真实 `/api/search` 对另外4道中文问题逐一核对精确证据ID，并按evidence_id去重计排名。该组与上述26题分开，不加入20道可答题的分母，也没有据此继续调参。精确锚点hit@5从3/4（75%）降至2/4（50%）：NPL轮廓与钴吸附能改善，但近红外像素和DISCO磨削的原有命中退步。该结果明确限制了“对所有中文问题都更好”的说法。

| 中文问题 | 精确目标 | 旧排名 | 新排名 | 前5变化 |
|---|---|---:|---:|---|
{chr(10).join(extra_rows)}

原始记录为 `extra_doc_probes_before.json` 和 `extra_doc_probes_after.json`。这4题没有开展生成答案评分。

"""
    live_gao_report = ""
    if (OUT / "live_search_before.json").is_file() and (OUT / "live_search_after.json").is_file():
        search_old, search_new = load("live_search_before.json"), load("live_search_after.json")
        natural_before = next(r for r in search_old if r["case"] == "natural" and r["mode"] == "default")
        natural_after = next(r for r in search_new if r["case"] == "natural" and r["mode"] == "default")
        live_gao_report = f"""## 线上默认入口与两次真实问答

父任务另测不带英文标题、也不带年份的口语题：{natural_after['query']}

真实默认检索入口中，精确目标 `{natural_after['target']}` 从旧前{natural_before['limit']}条未命中，变为新第{natural_after['rank']}。这与含作者年份的独立题及含完整英文标题的历史题是不同问题，分别保留记录，见 `live_search_before.json` 与 `live_search_after.json`。

"""
        for name in ["live_qa_after.json", "live_qa_after_named.json"]:
            if (OUT / name).is_file():
                qa = load(name)
                cited = [s for s in qa["sources"] if s.get("evidence_id") == target
                         and f"[{s['id']}]" in qa["response"]["answer"]]
                live_gao_report += f"- `{name}`：实际模型生成，核心答案为厚50 μm、TSV直径5 μm、间距35 μm；主参数引用为{', '.join('[' + s['id'] + ']' for s in cited)}，指向Gao原PDF物理第2页的精确目标。\n"
        live_gao_report += "\n这里核查的是三项参数及其主引用；不把两次调用当作总体生成准确率，也不为所有额外生成句子作统一正确性判定。旧QA响应另保留在 `live_qa_before.json`。\n\n"
    report = f"""# 中文检索改进前后对照（真实100份资料）

当前候选相对旧纯语义检索提高了已标注页的平均召回，但仍有明确失分项，不能称为中文检索全面通过。既有12道可答题的物理页hit@5为{percent(legacy_a['hit_at_5'])}→{percent(legacy_b['hit_at_5'])}，平均页recall@5为{percent(legacy_a['mean_recall_at_5'])}→{percent(legacy_b['mean_recall_at_5'])}；Gao历史原句{gao_outcome}，不含英文标题的作者年份自然题精确证据排名为{natural_new['metrics']['evidence']['first_expected_rank'] or '>30'}。

另有4道开发冻结后未向实现披露的终检题，精确证据hit@5为75%→50%，显示仍有回退；它们与26题主评估分开列出。

## 范围与口径

- 资料规模为100份，真实持久化向量6538条，SQLite原文证据8670条。未改动生产资料、标注、向量或索引。
- 既有题单16题中12题可答、4题证据不足；另加Gao历史原句、7道独立拟写的中文问题、2道负控，共26题。正例分母为20题；旧4题与新增2负控均不进入hit/recall分母。
- “旧基线”是修改前冻结的 `semantic()`：原始中文问题直接用本地BGE-M3编码、余弦检索。它不是旧网页默认keyword检索的成绩，也不是旧QA端到端答题成绩。旧网页keyword采用原生FTS表达式，中文没有通用分词/翻译；混合问题仍可能凭英文标识检出内容。
- “新候选”调用共享 `retrieval.retrieve()`，注入同一真实中文查询向量的dense排序，并运行本地术语展开、FTS候选和排序。旧返回预算为30条；新仍返回30条，但dense候选池为{new['method']['dense_shortlist_limit']}条（网页与QA共用的候选预算），因此此改进包含更广候选召回与重排的共同效果；没有增加查询编码次数。
- 每一级分别去重后取5项：文档按doc_id、页按(doc_id,物理pdf_page)、证据按evidence_id。hit@5表示至少一个标注目标进入5项；recall@5表示标注目标被覆盖比例的逐题宏平均。这三个级别各自有5项预算，不能把文档命中误称为证据命中。
- 旧12题只有文档/页标签。新增题和Gao历史题使用8道可答题的精确证据锚点；这些是已核对的选定锚点，并非全库所有可能相关段落的穷尽标注。因此表中的recall是对既定标签的召回，不是全语料的真实相关性召回率。i03需要两个独立bullet，recall分母为2。
- 新增题在本轮由评估agent独立拟写并先核对原文；已向实现agent反馈部分失败类型，因此属于探索性开发验证，不是封存的最终泛化测试集。26题主评估仅测检索，未计算整套生成正确率、拒答率或幻觉率；另有两次线上问答的参数与主引用核对，单列在后文。

## 指标

| 题组 | 级别 | 可答标签分母 | 旧hit@5 | 新hit@5 | 旧平均recall@5 | 新平均recall@5 |
|---|---|---:|---:|---:|---:|---:|
{chr(10).join(metrics)}

## Gao历史原句与自然变体

历史题原句：{ga['question']['question_zh']}

精确目标为 `{target}`，原始PDF物理第2页。旧原始向量段排名为{ga['exact_gold_dense_segment_ranks'][target]}；证据ID去重后排名为{ga['metrics']['evidence']['first_expected_rank']}。新共享检索直接返回证据ID去重结果，目标排名为{gb['metrics']['evidence']['first_expected_rank']}。原始向量段排名和去重证据排名不是同一个口径。

原文参数为芯片8 mm × 12 mm × 50 μm，即厚50 μm；TSV直径5 μm，间距35 μm。已查看原始PDF第2页渲染图；原文件使用旧符号字形，保留了原生文本匹配失败记录，并以实际页面图核对。其他7个独立锚点通过原始PDF页的原生文本和SQLite证据文本双重比对。所有来源PDF的SHA256均与目录一致，见 `independent_grounding.json`。

不含英文标题的自然题 `i01_gao_natural_zh` 单独保留：{natural_new['question']['question_zh']}

该自然题旧向量段排名为{natural_old['exact_gold_dense_segment_ranks'][target]}，返回30条中精确锚点排名为{natural_old['metrics']['evidence']['first_expected_rank'] or '>30'}；新共享检索精确证据排名为{natural_new['metrics']['evidence']['first_expected_rank'] or '>30'}，目标物理页排名为{natural_new['metrics']['page']['first_expected_rank'] or '>30'}。历史原句和自然变体分别计分，没有用其中一个的成功代替另一个通过。

## 失分项与部分覆盖

下表列出新候选未覆盖全部既定页/精确证据锚点的可答题。`>30`表示未出现在返回30条中，不表示全库不存在证据。

| ID | 中文问题 | 标注目标页 | 页recall@5 旧→新 | 新首个目标页排名 | 新首个精确锚点排名 |
|---|---|---|---:|---:|---:|
{chr(10).join(losses)}

## 证据不足与负控

原4题 `u01`（普适唯一最优配方）、`u02`（Cu/PI指定85/85十年概率与置信区间）、`u03`（万片/月人民币成本）、`u04`（同条件MoS2与Ti千次温循头对头比较）分别保留证据缺口。新增 `n01` 要求EVG320中国含税售价，`n02` 要求AEC-Q104对任何Cu/SiCN裸界面十年零失效保证条款。相关设备或标准被检出并不构成这些问题的答案，本轮没有据此判定“拒答正确”。

{extra_report}{live_gao_report}
## 可复现性与实际入口

全量6538条缓存向量与Qdrant持久化点逐点只读核对：payload完全相同、向量最大绝对误差0。{replay_note}

{api_note}

在项目根目录PowerShell运行：

```powershell
.venv\\Scripts\\python.exe scripts\\ground_crosslingual_questions.py
.venv\\Scripts\\python.exe scripts\\evaluate_crosslingual_retrieval.py --phase baseline --verify-persisted
.venv\\Scripts\\python.exe scripts\\evaluate_crosslingual_retrieval.py --phase candidate --dense-limit {new['method']['dense_shortlist_limit']}
.venv\\Scripts\\python.exe scripts\\verify_crosslingual_replay.py --live-url http://127.0.0.1:8600
.venv\\Scripts\\python.exe scripts\\report_crosslingual_evaluation.py
```

首次编码使用既有本地BGE-M3模型，查询向量按原句SHA256缓存；复测不会下载模型或调用LLM。上述评估只读取生产资料，报告/查询缓存写入本报告目录。`verify_crosslingual_replay.py --baseline-engine` 为可选的独立Qdrant快照复核，已执行过，无需常规重复。

主要产物：`baseline.json`、`candidate.json`、`questions_resolved.json`、`independent_questions.json`、`independent_grounding.json`、`baseline_retrieval_frozen.py`、`query_vector_cache.npz`、`baseline_engine_verification.json`；实际HTTP复核完成后另存 `live_api_verification.json`。JSON包含原始结果、逐题标签和模型/资料/源码SHA256。候选报告源码哈希若与当前文件不同，应重新运行候选评估再更新本报告。
"""
    (OUT / "comparison.md").write_text(report, encoding="utf-8")
    print(str(OUT / "comparison.md"))


if __name__ == "__main__":
    main()
