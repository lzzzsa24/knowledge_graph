"""Build the read-only audit records for the eight core studies."""
import hashlib
import json
from pathlib import Path
import re
import sys
import unicodedata

import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from kg_pilot.map_source_kinds import load_source_kind_reviews, source_kind_review

PILOT = ROOT / "pilot"
REPORT = PILOT / "reports/quality_review_20261113"
RENDER = REPORT / "renders/core"


def read_json(relative_path):
    return json.loads((PILOT / relative_path).read_text(encoding="utf-8"))


PAPERS = {row.get("doc_id") or row.get("id"): row for row in read_json("catalog/papers.json")}
CLAIMS = {
    row["claim_id"]: row
    for line in (PILOT / "annotations/claims.jsonl").read_text(encoding="utf-8").splitlines()
    if line.strip() and (row := json.loads(line))
}
OVERRIDES = read_json("catalog/paper_metadata_overrides.json")["documents"]
KINDS = load_source_kind_reviews(PILOT / "research_map/source_kind_reviews.json")
ERRATA = {row["doc_id"]: row for row in read_json("research_map/source_errata.json")["documents"]}
AUDIT = []


def evidence(*entries):
    return [
        {"pdf_page": page, "quote": quote, **({"locator": rest[0]} if rest else {})}
        for page, quote, *rest in entries
    ]


def claim(cid, conclusion, refs, *, sample, conditions, denominator, method, controls,
          replicates, uncertainty, limitations, comparison, alignment, decision="条件限定可用"):
    return {
        "claim_id": cid,
        "conclusion": conclusion,
        "source_evidence": evidence(*refs),
        "sample": sample,
        "conditions": conditions,
        "outcome": conclusion,
        "denominator": denominator,
        "method": method,
        "controls": controls,
        "replicates": replicates,
        "uncertainty": uncertainty,
        "limitations": limitations,
        "system_comparison": comparison,
        "system_alignment": alignment,
        "decision": decision,
    }


def issue(severity, description, ids, *refs):
    return {"severity": severity, "description": description, "claim_ids": ids,
            "evidence": evidence(*refs)}


def document(doc_id, pages, visual, identity, method, fidelity, applicability, claims, issues,
             use, primary_sources=None):
    paper = PAPERS[doc_id]
    pdf_path = Path(paper["local_path"])
    digest = hashlib.sha256(pdf_path.read_bytes()).hexdigest()
    assert digest == paper["sha256"], doc_id
    metadata = OVERRIDES.get(doc_id)
    if metadata and metadata.get("sha256") != digest:
        metadata = None
    for entry in claims:
        if entry["claim_id"] is None:
            entry["system_claim"] = None
            continue
        existing = CLAIMS[entry["claim_id"]]
        assert existing["doc_id"] == doc_id
        binding = source_kind_review(paper, existing, KINDS)
        fallback = {"reported_experiment": "experiment", "author_interpretation": "interpretation"}
        entry["system_claim"] = {
            "statement": existing["statement"],
            "sample_id": existing.get("sample_id"),
            "conditions": existing.get("conditions"),
            "original_claim_type": existing["claim_type"],
            "effective_source_kind": binding["kind"] if binding else fallback[existing["claim_type"]],
            "source_kind_review": binding,
            "review_status": existing.get("review_status"),
            "verification_label": existing.get("verification_label"),
            "evidence": existing["evidence"],
        }
    with pymupdf.open(pdf_path) as pdf:
        assert set(visual).issubset(pages) and max(pages) <= len(pdf)
        for number in visual:
            assert (RENDER / f"{doc_id}_p{number}.png").is_file()
        for entry in [ref for row in claims for ref in row["source_evidence"]] + [
            ref for row in issues for ref in row["evidence"]
        ]:
            assert entry["pdf_page"] in pages
            if entry.get("locator", "").startswith("目读图表"):
                assert entry["pdf_page"] in visual
                continue
            source = normalize(pdf[entry["pdf_page"] - 1].get_text())
            assert normalize(entry["quote"]) in source, (doc_id, entry)
    AUDIT.append({
        "doc_id": doc_id, "title": paper["title"], "group": "core",
        "pdf": str(pdf_path), "pdf_sha256": digest, "catalog_doi": paper.get("doi"),
        "catalog_source_type": paper.get("source_type"), "registered_metadata_type": paper.get("metadata_type"),
        "primary_sources": primary_sources or [], "identity_review_overlay": metadata,
        "source_errata_overlay": ERRATA.get(doc_id),
        "reviewed_pages": pages, "visual_pages": visual,
        "document_assessment": {
            "identity_source": identity, "research_method": method,
            "extraction_fidelity": fidelity, "applicability": applicability,
        },
        "claims": claims, "issues": issues, "recommended_use": use,
    })


def normalize(text):
    text = unicodedata.normalize("NFKC", text)
    text = re.sub(r"(?<=\w)[\u00ad-]\s*\n\s*(?=\w)", "", text)
    return re.sub(r"\s+", "", text).replace("−", "-")


def hou():
    pad = "1 µm pitch Cu/SiCN W2W 载具。实测最小间距 320 nm，SiCN 盖层约 98 nm。"
    document(
        "imec_01", list(range(1, 7)), [2, 3, 4, 5, 6],
        "Hou 等 IEEE IRPS 2022 原研会议稿，PDF p1 印最终版 DOI 10.1109/IRPS48227.2022.9764478。",
        "controlled-IV 扫速×温度实验，设置完整/下半焊盘比较。另以电流密度–电场扫温拟合竞争机制，恒流应力为辅助。",
        "i006、i004、i005 为作者的模型解释。i001 在原库 reported_experiment，研究图谱有效类型 test_method，须区分标称与实测尺寸。",
        "适用于特定 SiCN W2W 介质漏电和电压加速方法。界面位置、Cu 漂移和十年寿命属于解释或外推，不能与连续性良率、实测长时耐久并列。",
        [
            claim("i001", "上/下焊盘设计直径 200/600 nm，pitch 1 µm。TEM 所见下焊盘 680 nm，间距由设计 400 nm 校正为 320 nm。",
                  [(1, "The top and bottom pad diameter is 200 and 600 nm, respectively"), (2, "The critical pad spacing at the bonding interface is 320 nm.")],
                  sample=pad, conditions="截面位于 daisy-chain 区域，TEM 用于修正关键间距。", denominator="一处示例 TEM 截面与版图设计。",
                  method="设计尺寸和截面 TEM。", controls="完整与下半焊盘的关键间距保持相同。", replicates="截面复测及晶圆批次未报告。",
                  uncertainty="未报告 TEM 定量不确定度。", limitations="不把版图尺寸用作电场距离。", comparison="可作为电场归一化的载具条件。",
                  alignment="原声明明确是设计值，与图文一致。有效标签 test_method。", decision="仅方法背景"),
            claim("i006", "幂律外推完整结构在 100/200 °C 的 Vmax 分别为 26.9 ± 0.4/11.4 ± 1.2 V。",
                  [(3, "Assuming a Power-law model"), (3, "200 | 9.0 ± 0.4 | 11.4 ± 1.2 | 7.5 ± 0.1 | 9.7 ± 0.4", "目读图表 Table I 200 °C 行")],
                  sample=pad, conditions="100–200 °C，1.15、0.26、0.06 V/s，full/bottom-half。",
                  denominator="22,500 个 pad pairs 是一个 TDDB 结构的构成。每组独立结构数未明确。",
                  method="升压击穿分布经 Weibull 拟合，取 63% TTF，再提取场加速因子并按幂律外推。",
                  controls="下半焊盘不含上金属，余几何相同。200 °C 的下半结构 Vmax 为 9.7 ± 0.4 V。",
                  replicates="Fig. 4/6 显示多个数据点，批次与 die 层级重复未报告。",
                  uncertainty="表 I 给 ± 值，统计定义和外推模型结构误差未说明。",
                  limitations="Vmax 定义对应十年以上的模型估计。Fig. 3 在 200 °C 的 114 V 是一次升压例图。",
                  comparison="寿命比较需匹配应力模型、场强、实际间距及测试结构规模。", alignment="库对 11.4 V 及外推性质的表达正确，补上表 I 不确定度。"),
            claim("i004", "E>1 MV/cm 的 SiCN 漏电更符合 Poole–Frenkel 拟合。2.5 MV/cm 在 200 °C 附近出现转折。",
                  [(4, "PF emission falls within a range of 4.6 to 6.3."), (4, "a transition point of current density vs. temperature could be seen at 200 ◦C.")],
                  sample=pad, conditions="25–300 °C 完整焊盘中心 die，低/高场例 1.5/2.5 MV/cm。",
                  denominator="Fig. 7 标中心 die。温度/电场扫描条件不是独立试样。",
                  method="比较 PF/Schottky 模型提取的介电常数。PF 4.6–6.3 接近 bulk SiCN 4.9，SE 1.1–1.5。",
                  controls="Schottky 为竞争拟合；1.5 MV/cm 温扫接近线性，2.5 MV/cm 高温偏离。",
                  replicates="未给独立 die 数与晶圆批次。", uncertainty="未给拟合置信区间或模型选择检验。",
                  limitations="Cu 漂移是作者解释。200 °C、300 µA 软击穿支持导电路径变化，原文未直接化学定位铜丝。",
                  comparison="不能把 1.5 MV/cm 当连续扫描得到的通用漂移临界场。", alignment="原库保留了高场高温附加机制，仍需保持解释层级。"),
            claim("i005", "采用 PF 假设从激活能对 √E 线性段的截距得到约 0.95 eV 陷阱深度。",
                  [(4, "the barrier height (trap depth) was found to be around 0.95 eV."), (5, "Trap depth: 0.95 eV")],
                  sample=pad, conditions="完整结构高场 I–V 温扫。", denominator="由同组扫温曲线拟合。",
                  method="Fig. 8 的 Ea–√E 拟合外推。", controls="用 Table II 中 PF/Schottky 结果选择拟合解释。",
                  replicates="独立拟合样本数未报告。", uncertainty="0.95 eV 为约值，无误差。",
                  limitations="拟合参数可能随几何、材料与拟合区间变化。", comparison="只有相同模型和类似 SiCN 介质才有直接可比性。",
                  alignment="原库保留了模型来源。"),
        ],
        [issue("限定", "需保留 320 nm 实测间距、单结构焊盘数及幂律外推性质。禁止把 11.4 V 当量产额定值或直接测得的十年寿命。",
               ["i001", "i006"], (2, "The critical pad spacing at the bonding interface is 320 nm."), (3, "Assuming a Power-law model"))],
        "用作特定 Cu/SiCN W2W 电应力方法和机制解释，对寿命与漂移结论保留测试条件。",
    )


def roshanghias():
    pad = "300 mm 试验晶圆的 Cu/SiO₂ 自由表面，4 µm Cu 焊盘、10 µm pitch、Cu 面积分数 12.5%。"
    conditions = "N₂ 环境，升至 400 °C 并冷却到 22 °C，Berkovich 触针 2 µN、1 Hz，25 × 25 µm 扫描。"
    denom = "选取视野的 8 个 Cu 焊盘平均；不是 8 片晶圆。"
    document(
        "imec_06", [1, 2, 3, 4], [2, 3],
        "PDF p1 注明 EMPC2023。IMAPS DOI 10.4071/001c.94821 与 IEEE DOI 10.23919/EMPC55870.2023.10418382 对应同题同作者同摘要的会议研究，按一个研究计。",
        "两种初始 CMP 形貌的高温接触 SPM；与 ANSYS 21.2 FEM 对照，参数拟合使用这组实测输入，非独立验证。",
        "i024/i025 的高温值是相对 SiO₂ 的有符号高度，i026 的 51 ± 3 nm 是相对于初始的热位移。Fig. 4 与 Table 1 对部分 ± 数值有出入。",
        "仅支持自由表面热形貌的局部比较，不能直接推断受对面约束的已键合接头或 TSV 的实际膨胀、良率或可靠性。",
        [
            claim("i024", "初始凹陷 −9 nm，400 °C 的相对高度为 +40 nm，冷却后 +9 nm。",
                  [(2, "expands to 40 nm at 400 °C and retains 9 nm above the SiO2"), (3, "−9 ±1 | 40 ±3 | +9 ±3", "目读图表 Table 1 凹陷组 Experiment")],
                  sample=pad, conditions=conditions, denominator=denom, method="Fig. 3 热形貌、Fig. 4 截面轮廓、Table 1 中心 Cu 对 SiO₂ 高度差。",
                  controls="相对 SiO₂，逐个形貌比较初始/加热/冷却。", replicates="只给 8 个 pad 的局部平均。独立区域、晶圆和批次未报。",
                  uncertainty="Table 1 400 °C 给 ±3，Fig. 4 标 ±2。± 的统计定义未说明。",
                  limitations="本文未给加热保温扫描的重复验证，也未分离两种 CMP 路线所伴随的其他变化。",
                  comparison="保留高度零点。加热相对起点的位移约 49 nm，冷却残余位移约 18 nm。",
                  alignment="原值正确，保留高度/位移的区别。"),
            claim("i025", "初始突起 +13 nm，400 °C 时相对高度 +65 nm，冷却后 +28 nm。",
                  [(2, "pumping reaches 65 nm at 400 °C and remains 28 nm above"), (3, "13 ±1 | 65 ±3 | 28 ±4", "目读图表 Table 1 突起组 Experiment")],
                  sample=pad, conditions=conditions, denominator=denom, method="原位接触 SPM。",
                  controls="以同组起点及附近 SiO₂ 为参照。", replicates="未报晶圆级重复。",
                  uncertainty="Table 1 冷却给 ±4，Fig. 4 标 ±3。",
                  limitations="这是高度而非位移。高温相对初值的位移约 52 nm。",
                  comparison="相同高温 SPM 条件下可和凹陷组比较。", alignment="原值正确，补充基准及图表 ± 差异。"),
            claim("i026", "作者估计 400 °C 两种起始形貌的最大热挤出位移均约 51 ± 3 nm。",
                  [(3, "pumping (hmax@ 400 °C) was around 51 ± 3 nm for both recessed"), (3, "reported measurement values are the average of those for 8 Cu")],
                  sample=pad, conditions=conditions, denominator=denom, method="SPM 相对初值变化，由局部平均推算。",
                  controls="两种起始高度均有比较，并以 FEM 模拟对应热历程。",
                  replicates="未报独立配方批次。", uncertainty="报告 ±3 nm 但统计含义未说明。",
                  limitations="FEM 与实测同源，不把两者算作独立实证。",
                  comparison="和不同 pad 尺寸或不同热预算比较时只作为机制线索。", alignment="原声明保留作者估计，应显式加上相对初始基准。"),
        ],
        [issue("限定", "Fig. 4 与 Table 1 的 ± 数值分别出现 40 ±2/±3 和 28 ±3/±4，保留各自定位，不合并为统一不确定度。",
               ["i024", "i025"], (3, "40 ±3 / 28 ±4；40 ±2 / 28 ±3", "目读图表 Table 1 / Fig. 4"))],
        "用来选取 SPM 测量方案与核对自由表面热位移。按一个会议研究计。",
        [
            {"url": "https://imapsource.org/article/94821-cu-pumping-analysis-during-cu-sio2-hybrid-bonding-using-in-situ-spm-imaging", "fact": "同题同作者 IMAPS 文章与本地 PDF 一致，PDF 首页注明 EMPC2023。"},
            {"url": "https://ieeexplore.ieee.org/document/10418382/", "fact": "IEEE 会议记录同题、同摘要，正式会议 DOI 为 10.23919/EMPC55870.2023.10418382。"},
            {"url": "https://silicon-austria-labs.elsevierpure.com/en/publications/cu-pumping-analysis-during-cu-sio2-hybrid-bonding-using-in-situ-s/", "fact": "作者机构将同题同作者记录标为 2023 EMPC 会议论文。"},
        ],
    )


def nakayama():
    coupons = "单材料 2 × 2 cm blanket coupon，500 nm 电沉积 Cu、10 nm PVD Ta、120 nm PECVD SiCN。"
    cmp = "商用碱性浆，1 psi，盘/头 80/79 rpm，30 mL/min，IC1000 聚氨酯垫。"
    document(
        "imec_09", [1, 2, 3, 4, 5, 6], [4, 5, 6],
        "Nakayama 等 ECS J. Solid State Sci. Technol. 2024 原研期刊。p1 为出版封面，正文/DOI 从物理 p2 开始。",
        "以分立膜 coupon 的 XRR 厚差、动态电化学、zeta 电位和 XPS 评估浆料/清洗/活化，三组浆料对比可区分稀释与加 H₂O₂ 的部分作用。",
        "i035/i036 去除速率正确，数值正文 p4、图8位于 p5。i033/i034 原标签 reported_experiment、有效 process_setting。XPS 结果在抽样声明中缺席。",
        "可作 Cu/Ta/SiCN blanket coupon CMP 与清洗筛选。实际图形 Cu recess、全片均匀性与键合强度需要独立数据。",
        [
            claim("i035", "原碱性浆无 H₂O₂ 时，Cu/Ta/SiCN 去除速率 28.2/28.8/84.2 nm/min。",
                  [(4, "the removal rates of Cu and Ta are 28.2 and 28.8 nm min−1"), (4, "84.2 nm min−1"), (5, "blanket samples.")],
                  sample=coupons, conditions=cmp, denominator="单位去除厚度/时间，分材料 coupon。",
                  method="CMP 前后 XRR 膜厚。", controls="对照 1:2 稀释无 H₂O₂ 与 1:2 稀释加 0.4 wt% H₂O₂。",
                  replicates="独立 coupon、重复运行及各取样点数未报告。", uncertainty="Fig. 8 柱图无误差条。",
                  limitations="相近 Cu/Ta 速率不意味着三材料等速。", comparison="同设备与消耗品条件下可比较膜去除选择比。",
                  alignment="原值准确，补上 blanket 和对照组。"),
            claim("i036", "浆料:DIW 1:2 加 0.4 wt% H₂O₂ 后，Cu/Ta/SiCN 为 20/17/30.5 nm/min，Cu:SiCN 约 1:1.5。",
                  [(4, "the removal rates of Cu, Ta, and SiCN are 20, 17, and 30.5 nm"), (6, "selectivity of 1:1.5 was achieved under this")],
                  sample=coupons, conditions=cmp, denominator="单位厚度/时间，30.5/20 ≈ 1.525 为反向 SiCN/Cu 比。",
                  method="Fig. 8 三组 XRR 厚差。", controls="同稀释、不加 H₂O₂ 为 9.26/9.45/30.1 nm/min。缺原浓度加 0.4 wt% 的完整交叉组。",
                  replicates="未报独立 coupon/批次。", uncertainty="图无误差，不能判定小差异显著性。",
                  limitations="同配方 300 mm blanket bonding 引自 Ref. 31。本文称正在推进 300 mm 和 patterned CMP。",
                  comparison="可比较 coupon 选择比，不可作为 <5 nm 实测凹陷或图形键合的证据。", alignment="原值准确，需保留选择比方向和 coupon 证据范围。"),
            claim("i034", "N₂ plasma 15 s、100 W、35 sccm、50 Pa 是活化设定。",
                  [(3, "The radiofrequency power was set to 100 W, and the flow rate was 35 sccm under 50 Pa working pressure.")],
                  sample="Cu coupon 浸泡以模拟 CMP/P-CMP 表面。", conditions="碱性浆或 P-CMP 碱性清洗液浸泡 1 min，再按所述条件激活。",
                  denominator="单套工艺设定。", method="等离子体活化后 XPS。",
                  controls="见下条，比较处理序列中的 N 1s 峰。", replicates="未报。", uncertainty="未给设备实际值偏差。",
                  limitations="不是活化效率测量。", comparison="跨设备功率、流量不可直接等效。", alignment="原值准确，有效 process_setting。",
                  decision="仅方法背景"),
            claim(None, "XPS N 1s 的 399.7 eV Cu–BTA 峰在碱性清洗后减弱，398.7 eV 峰在 N₂ 活化后消失，并出现 397.4 eV Cu₄N 指认峰。",
                  [(5, "at 399.7 eV corresponds to Cu-BTA."), (6, "sponding to a Cu4N film appears at 397.4 eV.", "目读图表 Fig. 9(c) 与正文断词")],
                  sample="以无 H₂O₂ 碱性浆及碱性清洗液短时浸泡模拟的 Cu 表面。", conditions="处理序列为浸泡、P-CMP 清洗与 100 W 15 s N₂ 活化。",
                  denominator="XPS 局部归一化谱，图有三种处理条件。", method="峰位及归一化谱比较。",
                  controls="浸泡浆、清洗后、等离子体后；未给无等离子体的时间匹配对照。",
                  replicates="未报独立谱重复/样品数。", uncertainty="未报峰拟合误差和定量检出限。",
                  limitations="对应化学态为作者指认；未测接头电性能，Cu₄N 后续转化是推测。",
                  comparison="可比较表面化学流程，不能由谱峰直接外推电连续性。", alignment="现有 i034 只覆盖设定，当前审核新增独立原文证据。"),
        ],
        [issue("限定", "标题及 <5 nm 要求容易被读成已测图形凹陷。去除结果是 blanket coupon，全片实际图形仍在研究。",
               ["i035", "i036"], (5, "studying CMP of 300 mm blanket wafers and patterned wafers"))],
        "选择比及表面化学可作为工艺筛选证据，晶圆级验证需回到各原始研究。",
    )


def ong():
    sample = "12 英寸 20 µm pitch W2W (111) 纳米孪晶 Cu/SiO₂，切割后做局部电测和成像。"
    process = "N₂ plasma，室温真空约 10⁻³ mbar 下以 75 kN（1.06 MPa）预键合，200 °C 1 h，随后 200 °C 4 h 后退火。"
    document(
        "journals_ma15051888", list(range(1, 10)), [2, 4, 5, 6, 7],
        "Ong 等，Materials 2022 15(5) 1888，PDF p1 DOI 匹配，元数据覆盖层还原全名。",
        "特定 W2W 载具的 CMP、键合与电学示范。AFM、FIB/EBSD、CSAM、SEM 互补。普通与交叉条改进 Kelvin 测量不同几何。",
        "j003/j004/j005/j006 的主值能在原文定位，需分别标 6 个粗糙度样品、典型 Cu bump、一个 die pair、50 个接头。j001/j002 有效为 process_setting。",
        "支持 20 µm pitch、特定 (111) Cu 和后退火流程的局部示范。更细 pitch、晶圆级成品率和寿命缺直接数据。",
        [
            claim("j003", "CMP 后 AFM 测得 Cu/SiO₂ 的 Rq 平均分别 1.65/0.5 nm。",
                  [(4, "0.5 nm in average of six samples each, respectively.")],
                  sample=sample, conditions="两步 CMP 后、键合前。", denominator="Cu 和 SiO₂ 各 6 个样品的平均。",
                  method="AFM 高度相对均线的均方根，Fig. 4 展示典型 bump。",
                  controls="同流程的两种表面。", replicates="报告各 6 个样品，晶圆/区域及批次关系未说明。",
                  uncertainty="未报 Rq SD/CI；文中约 3 nm 凹陷是另一个量。",
                  limitations="Fig. 4 典型视野不证明全片最大凹陷。", comparison="与 Ra 指标或不同面积 AFM 扫描不能直接并列。",
                  alignment="原声明正确。"),
            claim("j005", "一个键合芯片对的 CSAM 图像显示 >95% 键合面积良好，放大图仍见小空洞。",
                  [(6, "Figure 5a shows the CSAM image for a bonded die pair"), (6, "more than 95% of the bonding area was well bonded.")],
                  sample=sample, conditions=process, denominator="Fig. 5 展示一个 die pair 的键合面积。",
                  method="CSAM；Fig. 6 用局部 SEM 辅证。", controls="示例图无未键合对照。",
                  replicates="未报面积结果的 die/wafer 重复。", uncertainty="未给 CSAM 分割阈值、空间检出限或跨样品置信区间。",
                  limitations="一个样品的面积比例，与按芯片数统计的无空洞良率不同。",
                  comparison="不得与 Gao 60% 或 Edge 73% 同列。", alignment="原声明已限定面积和单个 die pair。"),
            claim("j006", "交叉条改进 Kelvin 结构的 50 个接头平均 1.5 mΩ，80 µm² 面积换算为 1.2×10⁻⁹ Ω·cm²。",
                  [(7, "50 joints is 1.5 mΩ, which corresponds to a speciﬁc contact resistance of 1.2 × 10−9 Ω·cm2."), (7, "10 µm × 8 µm")],
                  sample=sample, conditions=process, denominator="50 个改进结构接头，10 × 8 µm²。",
                  method="后退火四点探针，1.5 mΩ × 8×10⁻⁷ cm²。",
                  controls="普通 Kelvin 的另外 50 个结构平均 6.7 mΩ，±1.75 mΩ 是该组；几何不同。",
                  replicates="50 个接头，晶圆数、die 间分布、批次和随机抽取未报。",
                  uncertainty="未给改进 Kelvin 组的 ± 或置信区间。Fig. 7a 为普通 Kelvin。",
                  limitations="材料选择因果归因缺同载具不同取向控制；方法段只明确 1.06 MPa 的预键合条件。p8 图8的无外压写法属于理想机制图。",
                  comparison="只与匹配电阻测量几何、面积归一化与温度的接头比较。",
                  alignment="原声明正确，分清 1.5 与 6.7 mΩ 和两种结构。"),
        ],
        [issue("限定", "1.06 MPa 在方法段明确对应室温预键合，200 °C 热键合及后退火压力缺独立数值。不能给热阶段自动补 1.06 MPa。",
                  ["j001", "j002", "j006"], (2, "temperature for pre-bonding, and the bonded pair was bonded at 200 ◦C for 1 h."),
                  (8, "No external pressure is needed at the third stage."))],
        "可引用局部 Cu/SiO₂ 低接触电阻与形貌证据，引用时同时交代后退火和测试结构。",
    )


def kim():
    wafer = "300 mm、厚 775 µm 的 Cu/氮化硅 W2W，2 µm pad 宽、5 µm pitch，EVG 工艺，接触后 300 °C 退火。"
    document(
        "journals_mi13040537", list(range(1, 13)), [4, 5, 6, 7, 8, 10, 11, 12],
        "Kim、Nguyen、Choa，Micromachines 2022 13(4) 537，PDF p1 DOI 匹配，元数据覆盖层还原全名。",
        "改变活化气体与低频功率，DCB 测整对晶圆键合能，椭偏测氮化硅氧化层增量。另改变沉积获得翘曲状态，SAM 计晶圆边缘空洞。",
        "j034 数值正确但原证据漏最直接的 Table 1（p7）；j035 与 Table 2 匹配。j036 保留趋势，图显示组内明显离散且原文单位有冲突。",
        "可引用设备、材料和窗口内的 DCB 能量与翘曲趋势。Wafer 边缘空洞不能等同 die-edge 电良率。",
        [
            claim("j034", "O₂、Ar、Ar/O₂ 对应 DCB 键合能 1.69/1.04/1.36 J/m²，氧化增厚 33.4/2.7/21.3 Å。",
                  [(6, "the top high power and bottom low power were correspondingly ﬁxed at 60 W and 25 W."), (7, "O₂ | 1.69 | 33.4；Ar | 1.04 | 2.7；Ar/O₂ | 1.36 | 21.3", "目读图表 Table 1")],
                  sample=wafer, conditions="高/低频 400/40 kHz，功率 60/25 W。",
                  denominator="气体 5 种条件。DCB J/m² 为单位新界面面积能量，样本数未给。",
                  method="晶圆边缘剃刀插入 DCB；椭偏前后测增厚。", controls="固定频率和功率，比较 O₂、N₂、Ar、Ar/O₂、Ar/N₂。",
                  replicates="未报每条件的 wafer pairs 或裂纹位置重复。", uncertainty="Table 1 与 Fig. 5 未示离散/误差。",
                  limitations="原文所谓 oxide growth rate 实为厚度增量，单位 Å。悬键并未直接测得。",
                  comparison="氮化硅氧化与整片键合能，不能作为单一 Cu–Cu 接头强度。",
                  alignment="保留定量条目并补 Table 1 p7。"),
            claim("j035", "O₂ 活化下 25、75、100、150 W 的 DCB 能量为 1.69、1.82、1.84、1.80 J/m²。",
                  [(8, "25 | 33.4 | 1.69；75 | 43.4 | 1.82；100 | 44.4 | 1.84；150 | 64.4 | 1.80", "目读图表 Table 2")],
                  sample=wafer, conditions="O₂，低频 40 kHz，高频 400 kHz/60 W。",
                  denominator="四个实验设定，DCB 每设定样品数未报。",
                  method="DCB 与椭偏。另测 AFM Rmax；AFM 平均 9 点不能视作 DCB n。",
                  controls="其余活化气体与高频设定固定。", replicates="DCB 未报。",
                  uncertainty="无误差条，100/150 W 的 0.04 J/m² 差不能判显著。",
                  limitations="可说 100 W 为本文测点中的最大，粗糙度导致强度回落属于机制解释。",
                  comparison="跨腔体不能直接采用同一瓦数作全局最优。",
                  alignment="原声明与表一致，补完整功率档及 100 W 限定。"),
            claim("j036", "凸形压应力上晶圆在本组较少边缘空洞；正翘曲较大时整体空洞计数偏高。",
                  [(10, "in a convex shape) show fewer edge voids than those with tensile stress."), (11, "Relationship between number of edge voids and wafer warpage.")],
                  sample=wafer, conditions="下晶圆凹形固定，改变上晶圆沉积与残余应力。真空卡盘夹持后中心针接触。",
                  denominator="Fig. 11 的晶圆边缘 void 个数，计数非面积。图见 8 点，晶圆批次及每条件 n 未给。",
                  method="激光 X/Y 双线扫翘曲，110 MHz SAM 查空洞。",
                  controls="凹形下片保持，工艺改变沉积条件，材料状态可能伴随变化。",
                  replicates="未报同翘曲重复，图上负翘曲仅一显式点。",
                  uncertainty="近零正翘曲点明显散布，不当作严格单调响应。",
                  limitations="作者提出 Joule–Thomson 和键合波机制，未直接测量水滴或波速。SAM 原文门槛单位冲突。",
                  comparison="与 die-edge TSV 电链良率分开用，剔除范围的尺寸单位待核。",
                  alignment="原趋势可用，需限定图中离散和研究设备。"),
        ],
        [
            issue("需修正标注", "p5 SAM 检出门槛正文印 200 mm，p6 Fig. 4 显示 50nm 大字与 50 mm 小字。图尺度和仪器检出门槛是不同量，当前均不推定正确单位。",
                  ["j036"], (5, "Using this system, we could characterize voids larger than 200 mm in size."), (6, "50nm / 50 mm", "目读图表 Fig. 4 双标尺")),
            issue("待澄清", "p12 正文距晶圆边 2.5 mm，Fig. 12 横轴写 µm，当前不将该数值导入 wafer 边缘排除距离。",
                  ["j036"], (12, "distance of 2.5 mm inward from the wafer edge."), (12, "Distance from wafer edge (μm)")),
        ],
        "使用材料与工艺受限的 DCB 能量、气体和翘曲趋势。定量 SAM 门槛与边缘距离需作者或版本确认。",
        [{"url": "https://www.mdpi.com/2072-666x/13/4/537", "fact": "出版社在线正文同样写 SAM 可表征大于 200 mm 的空洞。"}],
    )


def gao():
    sample = "Cu/oxide DBI 五层 D2W，四层 TSV die 加 Cap，主 TSV 链在 die 中心。"
    anneal = "环境条件下依次接触键合，整叠 200–250 °C 1 h 共退火。"
    document(
        "reliability_gao_d2w_2021", list(range(1, 8)), [3, 4, 5, 6],
        "Gao 等 ECTC 2021 原研，作者分享 PDF 书目与会议 DOI 匹配。",
        "同五层堆叠以 CSAM 按界面统计无空洞，电测主 TSV 链并对切割模块分组应力前后测量。SEM 为示例截面。",
        "r002/r003 需明确两种分母，r004 与图吻合，r005 应改称链电阻读数且保留温循进度。Table I 第四行层名重复；Table II 的 HTS 进度与正文不同步。",
        "适于这个 D2W 五层载具的原型组装和应力读数。旧单层、独立气密腔、五层 TSV 的试验对象各异。",
        [
            claim("r002", "五层堆叠累计无空洞 60%。Table I 逐行 90%、87%、92%、93%、89%。",
                  [(3, "void-free yield for the entire stack is 60%."), (3, "Layer 1 90% | Layer 2 87% | Layer 3 92% | Layer 5 93% | Layer 5 (CAP) 89%", "目读图表 Table I")],
                  sample=sample, conditions=anneal, denominator="累计为全部 5 界面均无可见空洞的堆叠占比；每层为该层无空洞 die/该层键合 die。",
                  method="CSAM 分析完整 5 层堆叠的每个界面。", controls="同载具跨层比较。",
                  replicates="未给堆叠数、独立晶圆或批次数。", uncertainty="未报 CI 或 CSAM 阈值，层间显著性未给统计检验。",
                  limitations="原表把 93% 行写 Layer 5；不能静默改 Layer 4。", comparison="Edge 2022 的 73% 只做同端点文献并列，样本/分母与因果不能合并。",
                  alignment="原库 60% 端点正确。"),
            claim("r003", "中心主 TSV 菊链电测良率 66%，链贯穿五层并含 >47,000 个互连。",
                  [(3, "die with >47,000 interconnects through the 5-layer stack"), (3, "resulted in test yield of 66%.")],
                  sample=sample, conditions=anneal, denominator="分母是进入主链电测试的模块/主链，精确计数未报告。47,000 是链中互连数。",
                  method="电连续性/电阻测试，中心主链。", controls="用同载具的 CSAM 对照，非一一相同终点。",
                  replicates="未给受测模块 n。", uncertainty="未报误差、通过阈值和批次。",
                  limitations="部分边缘空洞可不穿过中心主链。", comparison="不可与单互连良率、Edge 2022 归一化边缘链同列。",
                  alignment="原声明描述主链正确，检索展示应明确不能将 47,000 作分母。"),
            claim("r004", "22 个五层模块通过 168 h 高压蒸煮，末次平均电阻上升 <1%。",
                  [(4, "The 5-die stack modules (22 parts) completed autoclave"), (4, "The average resistance increase is less than 1%.")],
                  sample=sample, conditions="121 °C，100% RH，15 psi，168 h。文章报告通过要求为 ΔR/R₀ <10%。",
                  denominator="受测模块 22，过关 22/22。", method="Fig. 5 与 T0 比较，96、168 h；另有 CSAM。",
                  controls="同件 T0，未见并行不同材料或未暴露对照。", replicates="22 模块，批次独立性未报。",
                  uncertainty="图有分布与异常点，未说明绿色菱形统计定义。",
                  limitations="截面和独立 cavity 氦漏证据不能替代五层组在其他应力下的耐久数据。",
                  comparison="仅同预处理与读数端点的 autoclave 可对照。", alignment="原记录准确。"),
            claim("r005", "45 个 MSL3 预处理五层模块，−40 至 125 °C 温循的 250 次首读未见链电阻变化。1000 次在当时待完成。",
                  [(5, "First read-out point at 250 cycles showed no change in resistivity."), (6, "Temperature Cycling with MSL3 Pre-conditioning | 45 | In progress", "目读图表 Table II")],
                  sample=sample, conditions="125 °C 24 h 烘、30 °C/60%RH 192 h 浸润、260 °C 三次回流，之后温循。",
                  denominator="45 模块，文中未给 250 次逐个数据。", method="相对基线链电阻/论文称 resistivity。",
                  controls="同件前测，旧单层 2000 次为既往数据。", replicates="45 模块，晶圆/批次未报。",
                  uncertainty="无量化变化上限或完整分布。",
                  limitations="表 II in progress；不要从该 PDF 推断已通过 1000 次。",
                  comparison="与聚合物 C-SAT 热冲击结果的检测方式和样本规模不同。", alignment="原记录保留了首读限制。物理量宜称链电阻。"),
        ],
        [
            issue("待澄清", "Table I 第四行和第五行均为 Layer 5，第五行明确 (CAP)，93% 层身份需作者/更正核实。",
                  ["r002"], (3, "Layer 5 93% | Layer 5 (CAP) 89%", "目读图表 Table I")),
            issue("限定", "Fig. 6a 给五层 150 °C 至 1000 h 数据，但 Table II 状态仍写 No failure after 500 hours / In progress。Fig. 6b/c 2000 h/225 °C 属单层既往数据。",
                  [], (5, "Resistance change of a single layer part"), (6, "No failure after 500 hours", "目读图表 Table II")),
        ],
        "引用 60%/66% 时注明不同分母和 CSAM/电链端点。应力数据按已完成读数引用。",
    )


def theil():
    sample = "DBI 铜/介质 D2W 载具，8 × 12 mm die，15 µm pad/35 µm pitch。方法段主阵列链 9450 link，边缘每列 315 link，85–330 µm。"
    process = "Logic host 上放 0–7 个 TSV Thru die 和 1 个 Cap；室温空气接触，堆后 250 °C 1 h。"
    document(
        "reliability_die_edge_2022", list(range(1, 8)), [3, 4, 5, 6],
        "Theil 等 ECTC 2022 原研作者分享稿，保留 p3 交叉引用错误。参考 [6] 明列 Gao 2020、[7] 为 Gao 2021。",
        "按边缘距离对列链作两探针测量，以 330 µm 归一化。CSAM 和截面给界面证据。已有良好边链的五层件分组做应力前后读数。",
        "r007/r008 正确，r009 原文位于 p5、直接图证在 p6。r006 有效为 process_setting。失效链不能参加导通链电阻均值。",
        "可比较同载具堆高和边缘距离，不能拿归一化链结果当绝对互连良率。边链可靠性为同套 parts 的附加观测。",
        [
            claim("r007", "五层 CSAM 单层无空洞率 85–99%，平均 94%，整叠累计 73%。",
                  [(4, "the cumulative void-free die yield is 73%."), (5, "The bars indicate cumulative void yield, while the points show per layer void yield.")],
                  sample=sample, conditions=process, denominator="单层为该层 die，累计为五层界面均无可见空洞的堆叠；原文未给精确分母。",
                  method="Fig. 8 CSAM 点和柱。", controls="同一载具按 bond layer 比较。",
                  replicates="CSAM 组 n 和独立批次未报。", uncertainty="85–99% 是层间范围，非误差区间，拟合线仅视觉辅助。",
                  limitations="Fig. 13 的 415 是导通堆叠电阻样本量，不能借作 CSAM 分母。",
                  comparison="Gao 2021 60% 属类似定义端点，工艺/总体关系未证，不可直接判提升 13 个百分点。",
                  alignment="原记录正确，保留两级分母。"),
            claim("r008", "五层距 die 边缘 155 µm 时归一化链良率 <90%，85 µm 约 28%。",
                  [(5, "The data was normalized against the yield of the innermost chain (330 μm)."), (5, "below 90% electrical yield at 155 µm from the edge and drop to about 28% at 85 μm.")],
                  sample=sample, conditions=process, denominator="每一边缘距离的链通过率除以同堆高最内 330 µm 链通过率；绝对值/受测 n 未给。",
                  method="逐列两探针，Fig. 10 单链归一化。",
                  controls="D2W/两层对照；D2W 85 µm 约 100%，Fig. 11 的 N=1213 明确为 D2W。",
                  replicates="未报五层 Fig. 10 各列受测数量及批次；同 die 多列相依。",
                  uncertainty="图 10 未给 CI，约 28% 来自正文和曲线。",
                  limitations="无法断言每个 pad 失效率或生产安全边距，摘要/总结 99% 不应扩到两层每列。",
                  comparison="不同于 Kim 晶圆边缘 void 数、Gao 中心主链绝对值。",
                  alignment="原记录已注明归一化，可补每列链和通过基准。"),
            claim("r009", "在导通结构中，完整 stack link 电阻未见随 die 边距变化的趋势。",
                  [(5, "For every stack type, no link resistance dependence with respect to the distance from the die edge is observed."), (6, "Full stack link resistance as a function of distance from the die")],
                  sample=sample, conditions=process, denominator="只在可测的导通链上，从链电阻推算 per-link（扣除 probe pad/lead）；Fig. 13 N=1213/308/415。",
                  method="两探针、RDL/DBI/TSV 合计贡献，Fig. 12 各堆型拟合。",
                  controls="按堆高和边距比较，并和中心主阵列对照。",
                  replicates="N 属导通堆叠分布，未报 Fig. 12 单列的重复或独立批次。",
                  uncertainty="未给斜率置信区间；Fig. 14 是另一关系，主阵列随 TSV 数增约 0.05 Ω/层。",
                  limitations="幸存者筛选，不能说所有边缘焊盘等效或零电阻贡献。",
                  comparison="须与同结构 per-link 电阻对比，不能与 Kelvin 界面比接触电阻直接比较。",
                  alignment="原记录保留组成，补 p6 Fig.12 和导通组限定。"),
            claim(None, "各可靠性试验中有可用边缘链的五层件被纳入，作者报告所测边缘链均通过，边缘数据来自同一套做主阵列可靠性的 parts。",
                  [(6, "ancillary data was collected for the edge arrays as well."), (7, "array within this set of parts showed all samples passing [6]."), (7, "Die to Wafer Stacking with Low Temperature Hybrid Bonding")],
                  sample=sample, conditions="TCT、autoclave、HTS、MSL3，未为边缘组列出各项应力读数细节。",
                  denominator="原文仅称 edge yielding array 的样本数比 main 少。每项 n、通过阈值未报。",
                  method="同件主阵列与边缘阵列附加测量。", controls="匹配主阵列是同套 parts 的比较，不能做独立研究加权。",
                  replicates="各试验的样本独立性与总批次未报。", uncertainty="无效应差或 CI。",
                  limitations="文内把主阵列指向 Gao 2020 [6]，不能无核实改成 Gao 2021 [7]；具体跨论文样本/批次身份待核。",
                  comparison="保留作者同套 parts 描述，与其他研究样本独立性分开。",
                  alignment="现有四条未覆盖此可靠性结论；仅可描述性引用。", decision="待澄清"),
        ],
        [
            issue("待澄清", "原文把同套 parts 的主阵列结果指向 [6] Gao 2020。Gao 2021 列 [7]，两篇样本是否相交缺独立标识。边缘链数据只计同套件的附加测量。",
                  [], (6, "Discussion and methods for the main array data are found in ref [6]."), (7, "array within this set of parts showed all samples passing [6].")),
            issue("限定", "摘要写中心 9,480 direct bond interconnects，方法写主链 9,450 links。Gao 2021 另称主阵列有 9,480 TSVs。须保留量名，不能从相近数字确定版图、样本或批次等同。",
                  ["r008", "r009"], (1, "9,480 direct"), (2, "9450 links")),
        ],
        "有条件引用边缘链归一化良率和导通链电阻。用原文样本关系标注边缘可靠性，禁止重复计权。",
    )


def lee():
    did = "doc_b9a25e99b026b40039f4"
    sample = "约 80 nm 晶粒 nc-Cu、可光图形化聚合物，文中称 10 µm fine pitch，方法亦用 10 µm 指 pad/via 尺寸。"
    process = "定制 CMP 后 150 °C 60 min，预键合 2 MPa、gang bond 40 MPa；2 MPa 原文写 pre-bond force。"
    document(
        did, [1, 2, 3, 4, 5], [2, 3, 4],
        "Lee 等 ECTC 2024 原研会议 PDF p1 印 DOI 10.1109/ECTC51529.2024.00312，目录原始 URL 为作者单位托管。",
        "三组 slurry/压力标签以 AFM 比较，局部 FIB/SEM/TEM 观测；150 °C 试验采用剪切和两个热冲击样品的 C-SAT，电学为后续计划。",
        "c002、c003、c006、c010/11、c012 大体贴原文。c009 正文 1.82 nm 与 Fig. 8 的 1.81 µm 水平标注冲突。",
        "局部演示可供 polymer/CMP 路线参考。D2W/W2W 归属、图形尺度与电学良率仍应限缩。",
        [
            claim(f"{did}_c002", "选用凹陷 7 nm 的 CMP 表面，文中给 Ra 0.26 nm。",
                  [(3, "with well achieved roughness of Ra 0.26 nm."), (3, "Slurry C | Low pressure | 7 nm", "目读图表 Table II")],
                  sample=sample, conditions="Slurry C / Low pressure，AFM 测 post-CMP 形貌。",
                  denominator="表列三个工艺组合，测点、芯片及晶圆总数未报。",
                  method="AFM，Fig. 5 展示 7 nm 与 1 µm 例图。",
                  controls="同 Slurry C 的 High pressure 凹陷 14 nm；Slurry A/High pressure 1 µm。缺 A/Low。",
                  replicates="未报告独立样本、视野重复或批次。",
                  uncertainty="Ra/凹陷无离散值；压力只有高/低标签，未给数值。",
                  limitations="无法区分完整 slurry×压力交互项，也无各 CMP 条件下同批键合结果。",
                  comparison="Ra 和 Rq 不直接比；聚合物 CTE 与氧化物/氮化物不同。",
                  alignment="与 c002/c003–c005 一致。"),
            claim(f"{did}_c006", "论文报告 nc-Cu/polymer 150 °C、60 min 成功键合。",
                  [(2, "bonding temperature of 150°C with a pre-bond force of 2 MPa"), (4, "with 150°C for 60 min.")],
                  sample=sample, conditions=process, denominator="局部截面示例；150 °C 下键合对数与失效数未报告。",
                  method="FIB/SEM 和 SAT 图支持局部接触/界面连续。",
                  controls="Fig. 10 有 80–150 °C 剪切变化线，未报系统化完整对照与 n。",
                  replicates="未报告独立热工艺和试样批次。", uncertainty="温度/压力实际误差未给。",
                  limitations="摘要称 W2W 与 D2W，p1 引言强调 D2W，p4 把截面称 post-wafer bonding。各样品不能据此同一化。",
                  comparison="可作为 150 °C 机械/形貌演示，p5 电学留待后续。不可与低压 SiO₂ 路线视为等条件。",
                  alignment="c006 数值与文本一致，补 c007/c008 工艺参数范围。"),
            claim(f"{did}_c009", "正文称错位 1.82 nm，Fig. 8 的右侧横向错位尺寸标 1.81 µm。",
                  [(4, "of 1.82 nm."), (4, "1.81 µm", "目读图表 Fig. 8 右侧水平标注；同图 10.2 µm 宽、1.76 µm 竖向")],
                  sample=sample, conditions=process, denominator="单张 FIB-SEM 例图。",
                  method="截面 SEM 尺寸标记。", controls="无独立几何校准或多位置对照报告。",
                  replicates="未报。", uncertainty="量级单位冲突，且尾数不同。",
                  limitations="保留两个原值，待正式更正/作者澄清后再发布定量值。",
                  comparison="当前不纳入对位精度量化比较。",
                  alignment="原库忠实转录正文，缺图文冲突标注。", decision="需修正标注"),
            claim(f"{did}_c010", "总共两件分别做 250 次和 500 次热冲击，后续 C-SAT 均未见可见裂纹。",
                  [(4, "2 samples were sent to the chamber for reliability"), (4, "no visible cracks were observed on the chips"), (4, "500 cycles.")],
                  sample="键合后 10 × 10 mm 芯片，聚合物介质。", conditions="−55 到 125 °C，空气，驻留 5 min，转移 <1 min。",
                  denominator="总计 2 件，正文 respectively 支持两种终点各 1 件。两条库声明是同次小样本试验的两个端点。",
                  method="Fig. 9 C-SAT 目视判读。", controls="无应力前后逐件量化与阴性对照。",
                  replicates="每终点未见重复。", uncertainty="未给可见裂纹/空洞检出限。",
                  limitations="未测电性能及大样本合格率，不能外推寿命或无缺陷。",
                  comparison="不同于 Gao 用链电阻判温循，应分别保留应力条件和终点。",
                  alignment="c010 和 c011 语义正确，合看必须说明总 n=2。"),
            claim(f"{did}_c012", "150 °C 键合样品报告剪切强度 10 MPa。",
                  [(4, "corresponding shear strength is 10 Mpa with 150°C bonding")],
                  sample=sample, conditions="文中剪切设置 500 g 与 1000 µm/s，Fig. 10 示意约 90° 接触工具。",
                  denominator="MPa 为力/面积，未给实际受剪面积及样品 n。",
                  method="剪切试验，Fig. 10 强度–温度线。",
                  controls="图呈现温度比较；未明确独立样品或温循与未温循对照。",
                  replicates="未报告。", uncertainty="无误差条，流程未明确剪切样品与热冲击两件的对应关系。",
                  limitations="剪切强度不能换算成 DCB 界面能或温循电连续性。",
                  comparison="只能与相同试样尺寸和剪切几何条件比较。",
                  alignment="c012 数值正确，补试验与分母局限。"),
        ],
        [
            issue("需修正标注", "c009 暂停 1.82 nm 引用。正文与原图横向标注的 1.81 µm 冲突。图中 1.76 µm 为左侧竖向标注。",
                  [f"{did}_c009"], (4, "of 1.82 nm."), (4, "1.81 µm", "目读图表 Fig. 8 裁剪")),
            issue("限定", "c010 与 c011 是总 n=2 试验的不同终点，不能相加成四件，也不能作为电学/寿命验收。",
                  [f"{did}_c010", f"{did}_c011"], (4, "2 samples were sent to the chamber for reliability"), (5, "Further electric characteristics will be carrier out")),
        ],
        "可引用 CMP 对照和低温机械/截面演示，冻结 c009 数值并注明 C-SAT 样本规模。",
    )


def write_outputs():
    destination = REPORT / "deep"
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "core.json").write_text(json.dumps(AUDIT, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    lines = [
        "# 核心原研全文复核（8 份）",
        "",
        "页码为本地 PDF 物理页。原文短引按实际 PDF 核验，图表单元格用目读转录。这里逐项给出证据适用范围与库内声明差异。",
        "",
    ]
    dimensions = {"identity_source": "身份与来源", "research_method": "研究方法",
                  "extraction_fidelity": "抽取保真", "applicability": "适用性"}
    for doc in AUDIT:
        visual = "、".join(f"[{page}](../renders/core/{doc['doc_id']}_p{page}.png)" for page in doc["visual_pages"])
        lines += [f"## {doc['title']}", "",
                  f"`{doc['doc_id']}` · 文本复核物理页 {', '.join(map(str, doc['reviewed_pages']))} · 图表实看物理页 {visual}", ""]
        if doc["doc_id"] == "doc_b9a25e99b026b40039f4":
            lines += ["[Fig. 8 错位单位裁剪](../renders/core/doc_b9a25e99b026b40039f4_p4_fig8_detail.png)", ""]
        if doc["primary_sources"]:
            for ref in doc["primary_sources"]:
                lines.append(f"- {ref['fact']} [原始来源]({ref['url']})")
            lines.append("")
        for key, label in dimensions.items():
            lines.append(f"- **{label}**　{doc['document_assessment'][key]}")
        lines += ["", f"**建议使用范围**　{doc['recommended_use']}", ""]
        for record in doc["claims"]:
            cid = record["claim_id"] or "补充原文证据"
            lines += [f"### {cid} · {record['decision']}", "", record["conclusion"], ""]
            existing = record["system_claim"]
            if existing:
                lines += [f"- **库内类型**　`{existing['original_claim_type']}` → `{existing['effective_source_kind']}`。{record['system_alignment']}"]
            else:
                lines += [f"- **库内覆盖**　{record['system_alignment']}"]
            for key, label in [
                ("sample", "样品"), ("conditions", "条件"), ("method", "方法"),
                ("denominator", "分母"), ("controls", "比较条件"), ("replicates", "重复"),
                ("uncertainty", "不确定性"), ("limitations", "局限"), ("system_comparison", "可比性"),
            ]:
                lines.append(f"- **{label}**　{record[key]}")
            for ref in record["source_evidence"]:
                locator = f" · {ref['locator']}" if ref.get("locator") else ""
                lines.append(f"- **PDF p{ref['pdf_page']}{locator}**　“{ref['quote']}”")
            lines.append("")
        if doc["issues"]:
            lines += ["**需处理的证据问题**", ""]
            for item in doc["issues"]:
                anchors = "、".join(f"p{ref['pdf_page']}" for ref in item["evidence"])
                lines.append(f"- **{item['severity']}**　{item['description']}（{anchors}）")
            lines.append("")
    (destination / "core.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    for review in (hou, roshanghias, nakayama, ong, kim, gao, theil, lee):
        review()
    write_outputs()
    print(f"{len(AUDIT)} PDFs, {sum(len(doc['claims']) for doc in AUDIT)} conclusions")
