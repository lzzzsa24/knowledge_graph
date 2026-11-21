"""A conservative, reproducible process/response map of reviewed assertions.

This is an index of the extracted assertions, not a text-mined causal graph.
Paper tags may filter routes and describe source types; they never create cells.
The pure builder deliberately does no I/O, generation, or corpus mutation.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict

from .map_source_kinds import resolve_map_source_kind, source_kind_review


ROWS = [
    {"id": "surface_planarization", "label": "CMP / 表面形貌", "description": "抛光条件、凹陷与粗糙度；仅在本条记录声明为工艺或样品条件时关联结果。"},
    {"id": "cleaning_activation", "label": "清洗与活化", "description": "去污、去氧化、等离子与表面活化；与保护层钝化分别统计。"},
    {"id": "surface_passivation", "label": "钝化与保护层", "description": "防氧化层、选择性钝化及保护层材料，不等同于活化。"},
    {"id": "copper_microstructure", "label": "铜组织", "description": "纳米孪晶铜、晶向、晶粒及电镀组织；不把测得的晶粒尺寸自动当输入变量。"},
    {"id": "dielectric_interface", "label": "介质与界面设计", "description": "本条记录明确的介质组成、界面或中间层条件；纯金属与纯介质另分体系。"},
    {"id": "alignment_contact", "label": "对准与接触条件", "description": "错位、对准、键合压力与接触条件，不含拉伸测试的施加载荷。"},
    {"id": "annealing", "label": "退火与热预算", "description": "键合退火温度、时长及气氛；不把可靠性温度应力当退火。"},
]
COLUMNS = [
    {"id": "surface_state", "label": "表面状态", "description": "粗糙度、凹陷、润湿与污染等中间状态；不是最终性能。"},
    {"id": "voids", "label": "空洞与未键合", "description": "原研究报告的界面空洞、裂纹、脱层或未键合；保留检测分辨率边界。"},
    {"id": "bond_strength", "label": "结合强度 / 键合能", "description": "剪切、拉伸、断裂或界面键合能；不同测试口径不可直接横比。"},
    {"id": "electrical_resistance", "label": "电阻与电连续性", "description": "接触/链电阻、电阻率及连续性；膜层片电阻与接头电阻分别追溯。"},
    {"id": "electrical_reliability", "label": "电学可靠性", "description": "漏电、击穿、电迁移与电应力寿命；模型外推与实测分别标注。"},
    {"id": "thermomechanical_reliability", "label": "热机械 / 环境可靠性", "description": "温循、湿热、存储老化、应力与疲劳结果，不含单独的退火设定。"},
    {"id": "yield", "label": "良率", "description": "本条记录定义的电学或键合通过率；不混用面积比例、芯片与链良率。"},
    {"id": "throughput", "label": "吞吐与节拍", "description": "明确口径的处理速率或周期；厂商规格与实测分别统计。"},
]

_ROW_PATTERNS = {
    "surface_planarization": r"(?<![a-z])CMP(?![a-z])|化学机械|平坦化|抛光|polish|planari[sz]|dishing|recess|凹陷|粗糙度|roughness",
    "cleaning_activation": r"清洗|活化|去氧化|去污|等离子|(?<![a-z])(?:ICP|CCP)(?![a-z])|甲酸|formic.acid|plasma|cleaning|cleaned|activation|activated|(?<![a-z])SAB(?![a-z])|轰击|(?:UV|VUV).{0,5}(?:处理|改性)",
    "surface_passivation": r"钝化|保护层|防氧化|抗氧化|passivat|capping.layer|protective.layer|MoS2\s*保护",
    "copper_microstructure": r"纳米孪晶|纳米晶铜|纳米晶Cu|nt[-– ]?Cu|nc[-– ]?Cu|nanotwin|Cu\s*\(111\)|[（(]111[）)]取向|铜[（(]111[）)]|晶粒尺寸.{0,12}(?:改变|影响|控制)|grain.size.{0,12}(?:effect|depend)",
    "dielectric_interface": r"SiCN|SiO2|SiO₂|SiN\b|Si3N4|Si₃N₄|Al2O3|Al₂O₃|氮化硅|氮碳化硅|氧化硅|介质(?:厚度|组成|材料|界面)|dielectric.(?:thickness|composition|interface)|中间层|interlayer",
    "alignment_contact": r"对准|错位|失配|套刻|misalign|alignment|overlay|键合压力|接触压力|热压|压载|键合载荷|键合力|bonding.pressure|bond.force|contact.pressure|thermocompression",
    "annealing": r"退火|anneal|post.bond.heat|后热处理",
}
_OUTCOME_PATTERNS = {
    "surface_state": r"roughness|dishing|recess|surface.topograph|contact.angle|wettability|contamin|residu|Cu.height|Cu.pumping|粗糙|凹陷|表面形貌|长程形貌|接触角|润湿|表面污染|残留|表面高度|悬挂键",
    "voids": r"void|non.?bond(?:ing|ed)|delamination|bonded.area|空洞|未键合|未接合|脱层|界面.{0,5}(?:间隙|裂纹|缺陷)|无缺陷(?:键合)?界面",
    "bond_strength": r"(?:bond|bonding|interfacial)[_ ]?(?:energy|strength)|shear.strength|tensile.strength|剪切强度|拉伸强度|断裂强度|键合强度|接合强度|结合强度|键合能|结合能|黏附强度|粘附强度|表面能",
    "electrical_resistance": r"resistan|resistiv|电阻|方阻|电连续性|electrical.continuity|electrical.connection|filament|导电细丝",
    "electrical_reliability": r"leakage|breakdown|(?<![a-z])TDDB(?![a-z])|electromigration|漏电|泄漏电流|击穿|电迁移|电流应力|电应力|Vmax|time.to.failure|失效时间|外推寿命|connection.retention|电连接保持|电连接稳定",
    "thermomechanical_reliability": r"thermal.cycl|temperature.cycl|HTS|HAST|fatigue|peeling.stress|heating.stress|cooling.stress|thermal.stress|mechanical.stress|von.Mises|温循|温度循环|热循环|高温存储|湿热|疲劳|剥离应力|热应力|残余应力|热机械|循环后|老化",
    "yield": r"(?<![a-z])yield(?![_ ]strength)|良率|通过率|成功率",
    "throughput": r"throughput|\bUPH\b|cycle.time|吞吐|产能|节拍|处理速率",
}
_ROW_RE = {key: re.compile(value, re.I) for key, value in _ROW_PATTERNS.items()}
_OUTCOME_RE = {key: re.compile(value, re.I) for key, value in _OUTCOME_PATTERNS.items()}
_REVIEWED = {"independent_agent_reviewed", "api_model_reviewed"}
_NON_FACTOR_KEYS = re.compile(r"scope|limit|note|comparability|warning|caveat|method|measurement|interpret|status|not_|unreported|disclosure|局限|限制|说明|测量方法|测试方法", re.I)
_DISCLAIMER = re.compile(r"[；;。](?:这|该|此|本|未|不|并非|不能|而非|非)|(?:，|,)(?:不是|而非|不能|未声称|不代表|不等同|不能推断|未给|未报告)")
_NONRESULT_QUANTITY = re.compile(r"测量方法|测试方法|检测方法|计数方法|评价方法|计算方法|定义|测量功能|检测项目|分辨率|可检|可导致|detectable|detection.limit|输入|model.input|conditions|^simulation_thermal_cycle$|plasma.activation|活化条件|退火条件|退火温度|处理时间|接合时间|应力时长|可靠性证据覆盖|设计中的", re.I)
_METROLOGY_QUANTITY = re.compile(
    r"预测误差|预测.{0,6}离散|测量误差|表观|伪影|重复性|不确定度|标定|测量结果的数值偏差|"
    r"测量最大差|相关性的R²|两种方法.{0,8}一致|测量结果标准差|准确度|检测精度|漏检|"
    r"\bRMSE\b|relative.error|prediction.error|repeatability|uncertainty|calibration", re.I)
_STRESS_SETPOINT_QUANTITY = re.compile(r"^(?:温度|热)循环次数$|^(?:temperature|thermal).cycl(?:e|ing).count$", re.I)
UNMAPPED_REASON_LABELS = {
    "no_process_factor": "未明确当前矩阵行定义的工艺或样品条件",
    "no_outcome": "未识别到当前矩阵列定义的结果指标",
    "background": "背景、综述或转述其他工作",
    "spec_only": "厂商规格、推荐工艺或标准要求",
    "ambiguous": "原页证据不足，或条件、指标及样品对应仍有歧义",
    "out_of_scope": "该记录的材料体系不在本次筛选范围内",
}


def _factor_conditions(value: object) -> str:
    if not isinstance(value, dict):
        return ""
    return " ".join(f"{key} {item}" for key, item in value.items()
                    if not _NON_FACTOR_KEYS.search(key) and item is not None and item is not False
                    and not (isinstance(item, str) and re.fullmatch(r"无|否|none|no|false|not.yet.performed|not.performed|not.applied", item.strip(), re.I)))


def _positive_statement(statement: str) -> str:
    """Do not turn a disclaimer (e.g. 'not a contact resistance') into a link."""
    return _DISCLAIMER.split(statement, maxsplit=1)[0]


def _source_kind(paper: dict, claim: dict) -> str:
    classification = paper.get("classification") or {}
    nature = classification.get("content_nature", "")
    kind = claim.get("claim_type", "")
    if paper.get("is_review") or nature == "review" or kind in {"review_summary", "cited_prior_work"}:
        return "review_or_cited"
    if kind == "manufacturer_specification" or nature == "product_specification":
        return "manufacturer"
    if nature in {"technical_guidance", "industry_overview", "normative_document"}:
        return "technical_guidance"
    if kind == "reported_experiment":
        return "experiment"
    if kind == "reported_simulation":
        return "simulation"
    if kind == "author_interpretation":
        return "interpretation"
    return "guidance_or_requirement"


def _system(claim: dict) -> tuple[str, str]:
    """Keep common hybrid, pure-metal and dielectric-only systems distinct.

    Scope text may explicitly narrow a hybrid paper to a separate model coupon.
    Unknown and multi-material systems remain visible but cannot anchor ideas.
    """
    material = str(claim.get("material_system") or "")
    statement = _positive_statement(str(claim.get("statement") or ""))
    conditions = claim.get("conditions") or {}
    scope = str(conditions.get("scope") or "") if isinstance(conditions, dict) else ""
    narrowing = statement + " " + scope
    if re.search(r"(?:EMC|PID)(?:/|[–−—-])(?:EMC|PID)表面|EMC/PID表面", narrowing, re.I):
        return "dielectric_polymer", "纯聚合物表面"
    if re.search(r"(?:独立|纯|同质).{0,8}Co[–−—-]Co|Co[–−—-]Co同质", narrowing, re.I):
        return "co_only", "纯 Co–Co"
    if re.search(r"独立Co[–−—-]SiO2|Co[–−—-]SiO2异质", narrowing, re.I):
        return "co_oxide_component", "独立 Co–SiO₂ 界面"
    if re.search(r"(?:独立|纯|同质).{0,8}Cu[–−—-]Cu|Cu[–−—-]Cu同质|pure.copper", narrowing, re.I):
        return "cu_only", "纯 Cu–Cu"
    # Explicit oxide model vehicles must not inherit a paper's Cu/oxide label.
    if re.search(r"TOx/TOx|氧化层模型|oxide.model|SiCN[–−—/-]SiCN样品|SiCN[–−—/-]SiCN介质|SiO[2₂][/–−—-]SiO[2₂]\s*blanket", narrowing, re.I):
        return ("dielectric_sicn", "纯 SiCN 介质") if re.search("SiCN", narrowing, re.I) else ("dielectric_oxide", "纯氧化物介质")
    text = material.casefold().replace("₂", "2").replace("₃", "3").replace("₄", "4")
    if re.search(r"cu[/–−—-]sn|solder|焊料", text):
        return "solder_hybrid", "含焊料的 Cu/Sn 体系"
    copper = bool(re.search(r"(?<![a-z])cu(?![a-z])|copper|铜", text))
    cobalt = bool(re.search(r"(?<![a-z])co(?![a-z])|cobalt|钴", text))
    if cobalt:
        return ("co_oxide", "Co / 氧化物") if re.search(r"sio2|氧化硅", text) else ("co_other", "Co 其他体系")
    interfaces = [("sicn", "SiCN", r"sicn|氮碳化硅"), ("sin", "SiN", r"sin\b|si3n4|氮化硅"),
                  ("oxide", "SiO₂", r"sio2|silicon.oxide|氧化硅|\boxide\b|氧化物"),
                  ("alumina", "Al₂O₃", r"al2o3|氧化铝"), ("polymer", "聚合物", r"polymer|polyimide|聚合物|聚酰亚胺|(?<![a-z])(?:bcb|pbo|pi|pid|emc)(?![a-z])")]
    found = [(key, label) for key, label, pattern in interfaces if re.search(pattern, text)]
    if len(found) > 1:
        # SiO2 underlying a SiCN cap is frequent, but a generic parser cannot
        # verify which is the active interface in a multi-layer description.
        return "mixed_or_uncertain", "多材料 / 界面待确认"
    if found:
        key, label = found[0]
        return (f"cu_{key}", f"Cu / {label}") if copper else (f"dielectric_{key}", f"纯 {label} 介质")
    if copper and re.search(r"cu.{0,3}cu|copper.{0,5}copper|铜铜|纯铜|nanotwin|nt.cu", text):
        return "cu_only", "纯 Cu–Cu"
    return "unspecified", "体系未充分明确"


def _specimen_scope(claim: dict, system: str) -> str:
    quantity = str(claim.get("quantity") or "")
    if re.search(r"sheet.resistance|片电阻|方阻", quantity, re.I):
        return "thin_film_transport"
    if re.search(r"witness|见证", str(claim.get("sample_id") or "") + quantity, re.I):
        return "witness_coupon"
    if re.search(r"height.*(?:heating|cooling)", quantity, re.I):
        return "unbonded_surface_evolution"
    if system.startswith("dielectric_") or system.endswith("_component"):
        return "component_or_dielectric"
    if system in {"cu_only", "co_only"}:
        return "metal_only"
    if system.startswith(("cu_", "co_")):
        return "hybrid_material_system"
    return "unconfirmed"


def _outcome_matches(claim: dict) -> dict[str, str]:
    quantity = str(claim.get("quantity") or "")
    if not quantity or _NONRESULT_QUANTITY.search(quantity):
        return {}
    # The metric must be this assertion's subject, not an unrelated sentence in
    # a long supporting quote or a warning in conditions.scope.
    # In "resistance as a function of recess", recess is the input rather
    # than a second measured response. Preserve the metric's grammatical role.
    metric = re.split(r"随|as.a.function.of", quantity, maxsplit=1, flags=re.I)[0]
    influenced = re.search(r"对(.+?)(?:的影响|影响)$", metric)
    if influenced:
        metric = influenced.group(1)
    outcomes = {key: match.group() for key, pattern in _OUTCOME_RE.items()
                if (match := pattern.search(metric))}
    if re.search(r"observation|观察|界面情况|界面状态", quantity, re.I):
        match = _OUTCOME_RE["voids"].search(_positive_statement(str(claim.get("statement") or "")))
        if match:
            outcomes["voids"] = match.group()
    if "thermal_resistance" in quantity.casefold() or "热阻" in quantity:
        outcomes.pop("electrical_resistance", None)
    if re.search(r"屈服|yield.strength|峰强度|结合能变化|binding.energy", quantity, re.I):
        outcomes.pop("bond_strength", None)
        outcomes.pop("yield", None)
    return outcomes


def _claim_matches(claim: dict) -> tuple[dict[str, str], dict[str, str]]:
    outcomes = _outcome_matches(claim)
    if not outcomes:
        return {}, {}
    quantity = str(claim.get("quantity") or "")
    statement = _positive_statement(str(claim.get("statement") or ""))
    statement = re.split(r"[，,；;](?:作者注明|作者指出.{0,8}局限|测量局限)", statement, maxsplit=1)[0]
    factors = statement + " " + _factor_conditions(claim.get("conditions"))
    factors = re.sub(r"\bno\s+CMP\b|未(?:经|进行|采用).{0,3}CMP|不经(?:抛光|平坦化)", "", factors, flags=re.I)
    factors = re.sub(r"(?:未(?:经|进行|采用)?|无)\s*(?:钝化|活化|退火|等离子体处理)|(?:without|no)\s+(?:passivat\w*|activation|annealing)", "", factors, flags=re.I)
    rows = {key: match.group() for key, pattern in _ROW_RE.items()
            if (match := pattern.search(factors))}
    if re.search(r"Cu.height.*(?:heating|cooling)|加热.*高度|热处理.*高度", quantity, re.I) and re.search(r"冷却|cool", statement, re.I):
        rows["annealing"] = "加热 / 冷却表面演化"
    if "surface_state" in outcomes and "surface_planarization" in rows:
        # Roughness as the measured response cannot itself establish that a
        # polishing/roughness input was studied.
        explicit = re.search(r"(?<![a-z])CMP(?![a-z])|化学机械|平坦化|抛光|polish|planari[sz]", factors, re.I)
        if explicit:
            rows["surface_planarization"] = explicit.group()
        elif len(outcomes) == 1:
            rows.pop("surface_planarization")
    if re.search(r"未(?:经|进行|完成|做)?.{0,3}(?:后)?退火|退火(?:前|之前)|before.anneal|not.yet.performed", statement, re.I):
        rows.pop("annealing", None)
    # Claims about merely a test instrument's detection/measurement performance
    # are outside this process-response map even when the method is reviewed.
    if claim.get("claim_type") == "test_method":
        return {}, {}
    return rows, outcomes


# Only predeclared, physically interpretable comparisons may become candidates.
# Supporting counts determine whether any template is exposed at all.
_DESIGNS = {
    ("surface_planarization", "electrical_reliability"): (
        "在相同介质、焊盘与退火条件下，CMP 后的铜凹陷/粗糙度分组是否与漏电和击穿寿命分布有关？",
        ["固定介质组成与厚度、焊盘尺寸/节距及电场换算口径", "以原 CMP 流程为基线；分组改变凹陷或粗糙度，避免两者同步变化", "固定清洗活化、接触与退火热预算，跨批次重复并记录初始空洞"],
        "同一批样品测 AFM 形貌、漏电 I–V 与恒场 TDDB；报告场强、温度、失效定义及寿命分布，区分空洞与介质损伤两种解释。"),
    ("surface_planarization", "bond_strength"): (
        "在同一材料与退火窗口内，降低凹陷或粗糙度是否改变界面断裂能及破坏位置？",
        ["将凹陷与粗糙度分别控制，其余 CMP 残留及活化条件保持一致", "固定接触压力、退火、样品尺寸和测试气氛", "用同一种断裂方法及可比裂纹区域，并设原流程对照"],
        "AFM 配合 DCB/PBC-DCB 或适用的剪切试验；同时报告破坏位置、重复性和硅体断裂的测量上限。"),
    ("cleaning_activation", "thermomechanical_reliability"): (
        "同一体系中，清洗/活化剂量或活化后等待时间的改变，是否影响老化后的界面强度保持率与空洞增长？",
        ["分别扫描剂量与等待时间，设置原流程及未处理基线", "固定初始粗糙度、残留、对准和退火热预算", "固定老化温湿度或温循条件，比较老化前后的同口径指标"],
        "记录表面接触角/化学状态，老化前后以相同 SAM 和断裂测试评估空洞增长、强度保持率及电阻漂移；不用初始强度代替可靠性。"),
    ("surface_passivation", "electrical_reliability"): (
        "保护层种类或厚度改变后，在相同退火热预算下，接头初始电阻与后续漏电/电应力寿命能否同时保持？",
        ["同一材料中设置未钝化对照，统一退火温度与时长，避免把热预算差异归于钝化", "分开控制保护层厚度、活化剂量和等待时间", "固定结构尺寸、电场、温度与失效判据，检查残留层及竞争导电路径"],
        "用 Kelvin 电阻与截面成分分析核实导通路径，再测漏电、TDDB 或适用的电迁移寿命；报告保护层残留和批次重复性。"),
    ("surface_passivation", "voids"): (
        "相同退火与接触条件下，保护层厚度是否改变空洞面积分数及其随老化的演化？",
        ["设置无保护层基线，固定 CMP、清洗活化和对准", "控制保护层厚度，并统一接触压力及退火热预算", "保持检测分辨率和抽样位置一致，区分残留层与表面形貌差异"],
        "键合前测 AFM/表面化学，键合及老化后以 SAM 面积统计加截面 TEM/SEM 验证；无可检空洞仅在该检测极限内成立。"),
    ("copper_microstructure", "electrical_reliability"): (
        "同一介质与结构中，铜组织改变后，初始低电阻能否在相同电流应力下保持，失效位置是否发生变化？",
        ["比较可获得的铜组织基线，固定镀层厚度、杂质及表面形貌", "固定介质、焊盘、清洗活化与退火热预算", "按实际导通截面积统一电流密度，并控制温度/焦耳热"],
        "EBSD/TEM 确认初始组织，Kelvin 电阻随电流应力跟踪，结合失效截面区分晶界迁移、界面残留与空洞竞争解释。"),
    ("copper_microstructure", "voids"): (
        "铜组织在相同热预算下是否改变界面空洞闭合与循环后空洞增长，而非仅改变初始强度？",
        ["固定材料、铜厚度、凹陷及粗糙度，设置常规铜组织对照", "固定接触、活化及退火，避免组织和热预算同步改变", "采用相同界面抽样位置及分辨率，跨批次重复"],
        "退火及循环前后做同口径空洞面积统计与 EBSD/TEM，联合电阻和强度，区分组织变化、塑性变形与检测抽样差异。"),
    ("dielectric_interface", "electrical_resistance"): (
        "同一材料体系内，介质层厚度或界面处理的改变是否在相同接触条件下改变接头电阻分布？",
        ["仅改变同体系介质厚度或界面处理，保持金属、焊盘与导通面积一致", "固定铜凹陷、活化、对准和退火，设置原介质流程对照", "将膜层片电阻与接头接触电阻分开测量，排除几何串联电阻"],
        "以 Kelvin 接头分布和菊花链连续性为主，配合截面间隙/残留层及 AFM，检验介质状态与金属闭合的关联。"),
    ("alignment_contact", "electrical_reliability"): (
        "在固定接触面积或显式校正面积后，可控错位是否改变局部漏电、击穿或电流应力失效分布？",
        ["设置零错位基线及可控平移/旋转分组，分别处理两类错位", "固定焊盘、介质厚度、凹陷、接触压力及退火", "实测有效重叠面积，区分面积收缩、电场集中与初始空洞效应"],
        "对准计量结合 Kelvin、漏电 I–V 与 TDDB/电流应力；以实际面积归一，必要时电热仿真定位热点并用失效截面验证。"),
    ("alignment_contact", "thermomechanical_reliability"): (
        "同一材料、重叠面积与热预算下，可控错位或接触压力是否改变循环后的脱层位置和电阻稳定性？",
        ["错位与压力分开扫描，设置零错位及原压力基线", "固定材料、铜凹陷、焊盘、堆叠厚度与退火", "用相同温循条件并测实际重叠面积，比较初始与循环后结果"],
        "以温循前后 SAM、链电阻和截面失效定位评估；有限元仅作应力位置预测，须与实测裂纹/脱层位置比较。"),
    ("annealing", "electrical_reliability"): (
        "在初始接头电阻相近时，不同退火温度/时长组合是否仍产生不同的漏电或电应力寿命分布？",
        ["在同一体系中扫描温度与时长，设置原流程基线", "固定表面、保护层、介质厚度、结构及接触条件", "实测初始空洞与电阻，固定电场/电流密度及应力温度"],
        "联合初始 Kelvin/空洞、漏电及 TDDB 或适用电流应力寿命分布；区分铜连接改善与介质损伤，不能仅由低电阻推断寿命改善。"),
}


def _identity(paper: dict) -> str:
    doi = str(paper.get("doi") or "").strip().casefold()
    doi = re.sub(r"^https?://(?:dx\.)?doi.org/", "", doi)
    return doi or str(paper.get("sha256") or paper["doc_id"])


def _cell_source_kind(cell: dict, detail: dict) -> str:
    relation = cell.get("reviewed_records", {}).get(detail["claim_id"], {}).get("relation")
    if detail["source_kind"] in {"experiment","simulation"} and relation in {"method", "interpretation"}:
        return relation
    return detail["source_kind"]


def _directions(cells: list[dict], details: dict[str, dict], papers: dict[str, dict]) -> list[dict]:
    by_cell = {(cell["row"], cell["column"]): cell for cell in cells}
    result = []
    row_labels = {row["id"]: row["label"] for row in ROWS}
    column_labels = {column["id"]: column["label"] for column in COLUMNS}
    for (row, column), (question, controls, measurement) in _DESIGNS.items():
        target = by_cell[row, column]
        if target["experiments"] > 2:
            continue
        # Each side must be an original measured assertion in its own cell.
        row_support, column_support = [], []
        for cell in cells:
            if cell["row"] == row and cell["column"] != column:
                row_support.extend((cell, details[cid]) for cid in cell["records"] if _cell_source_kind(cell, details[cid]) == "experiment")
            if cell["column"] == column and cell["row"] != row:
                column_support.extend((cell, details[cid]) for cid in cell["records"] if _cell_source_kind(cell, details[cid]) == "experiment")
        pairs = []
        for row_cell, a in row_support:
            if a["system"] in {"unspecified", "mixed_or_uncertain", "solder_hybrid"}:
                continue
            if row == "dielectric_interface" and a["system"] in {"cu_only", "co_only", "co_other"}:
                continue
            for column_cell, b in column_support:
                # A paper spanning two cells is not two independent anchors.
                if a["system"] != b["system"] or _identity(papers[a["doc_id"]]) == _identity(papers[b["doc_id"]]):
                    continue
                if column in {"electrical_resistance", "electrical_reliability"} and b["specimen_scope"] in {"thin_film_transport", "witness_coupon"}:
                    continue
                routes_a = set((papers[a["doc_id"]].get("classification") or {}).get("routes") or [])
                routes_b = set((papers[b["doc_id"]].get("classification") or {}).get("routes") or [])
                if routes_a and routes_b and not routes_a.intersection(routes_b):
                    continue
                # Prefer measured intermediate state and final response anchors.
                score = (row_cell["experiments"] + column_cell["experiments"],
                         row_cell["column"] == "surface_state", bool(routes_a.intersection(routes_b)))
                pairs.append((score, row_cell, a, column_cell, b))
        if not pairs:
            continue
        _, row_cell, a, column_cell, b = max(pairs, key=lambda item: item[0])
        result.append({"row": row, "column": column, "question": question, "status": "potential_direction",
                       "rationale": f"潜在方向（待验证）：本库当前抽取中该格有 {target['experiments']} 篇原创实测资料。同行的「{row_labels[row]} × {column_labels[row_cell['column']]}」与同列的「{row_labels[column_cell['row']]} × {column_labels[column]}」各有同体系、不同资料的实测锚点；可据此设计交叉验证，尚不构成目标关联或机制证据。",
                       "controls": list(controls), "measurement": measurement,
                       "anchors": [{"row": cell["row"], "column": cell["column"], "claim_id": detail["claim_id"],
                                    "doc_id": detail["doc_id"], "role": role, "source_kind": detail["source_kind"]}
                                   for cell, detail, role in [(row_cell, a, "same_process"), (column_cell, b, "same_response")]],
                       "system": a["system"], "system_label": a["system_label"],
                       "experimental_documents": target["experiments"],
                       "boundary": "仅表示本库已抽取记录的低覆盖；需先复核锚点原页与体系条件，并检索全文及外部文献查重，创新性尚待确认。"})
    return sorted(result, key=lambda item: (item["experimental_documents"], item["row"], item["column"]))[:8]


def _claim_detail(claim: dict, paper: dict, rows: dict, columns: dict, kind_reviews) -> dict:
    system, system_label = _system(claim)
    review = source_kind_review(paper, claim, kind_reviews)
    source = (review["kind"] if review else resolve_map_source_kind(paper,claim)) or _source_kind(paper,claim)
    return {"claim_id": claim["claim_id"], "doc_id": claim["doc_id"], "source_kind": source,
            "source_review": {k:review[k] for k in ("reason","evidence")} if review else None,
            "claim_type": claim.get("claim_type"), "system": system, "system_label": system_label,
            "specimen_scope": _specimen_scope(claim, system), "relation_scope": "same_claim_association",
            "material_system": claim.get("material_system"), "sample_id": claim.get("sample_id"),
            "quantity": claim.get("quantity"), "value_text": claim.get("value_text"), "unit": claim.get("unit"),
            "conditions": claim.get("conditions"), "process_matches": rows, "response_matches": columns,
            "evidence": [{key: e.get(key) for key in ("evidence_id", "pdf_page", "quote")}
                         for e in claim.get("evidence", []) if isinstance(e, dict)]}


def _unmapped_reason(claim: dict, source_kind: str, disputed: set[str], system: str | None) -> str:
    if system and _system(claim)[0] != system:
        return "out_of_scope"
    kind = claim.get("claim_type")
    if kind in {"manufacturer_specification", "recommended_process", "standard_requirement"}:
        return "spec_only"
    if kind in {"review_summary", "cited_prior_work"} or source_kind == "review_or_cited":
        return "background"
    if claim["claim_id"] in disputed or claim.get("review_status") not in _REVIEWED or not _has_page_evidence(claim):
        return "ambiguous"
    quantity = str(claim.get("quantity") or "")
    if (kind == "test_method" or _METROLOGY_QUANTITY.search(quantity)
            or _STRESS_SETPOINT_QUANTITY.search(quantity) or not _outcome_matches(claim)):
        return "no_outcome"
    if not _claim_matches(claim)[0]:
        return "no_process_factor"
    return "ambiguous"


def _has_page_evidence(claim: dict) -> bool:
    return any(isinstance(e, dict) and isinstance(e.get("pdf_page"), int) and e["pdf_page"] > 0 and e.get("quote")
               for e in claim.get("evidence", []))


def build_research_map(papers: list[dict], claims: list[dict], *, route: str | None = None,
                       reviewed: list[dict] | None = None, withheld: set | None = None,
                       kind_reviews: dict | None = None, system: str | None = None,
                       coverage_reviewed: list[dict] | None = None) -> dict:
    """Build counts from the current snapshot; route labels only select papers."""
    if route not in {None, "W2W", "D2W", "D2D"}:
        raise ValueError("技术路线只能是 W2W、D2W 或 D2D。")
    selected = [paper for paper in papers if not route or route in (paper.get("classification") or {}).get("routes", [])]
    by_doc = {paper["doc_id"]: paper for paper in selected}
    scoped_claims = [claim for claim in claims if claim.get("doc_id") in by_doc]
    cells = {(row["id"], column["id"]): {"row": row["id"], "column": column["id"], "records": [], "reviewed_records": {}}
             for row in ROWS for column in COLUMNS}
    details = {}
    withheld = withheld or set()
    for claim in scoped_claims:
        claim_id = claim.get("claim_id")
        if not claim_id or claim_id in details or claim.get("review_status") not in _REVIEWED:
            continue
        if not _has_page_evidence(claim):
            continue
        rows, columns = _claim_matches(claim)
        if not rows or not columns:
            continue
        pairs = [(row, column) for row in rows for column in columns if (claim_id, row, column) not in withheld]
        if not pairs:
            continue
        details[claim_id] = _claim_detail(claim, by_doc[claim["doc_id"]], rows, columns, kind_reviews)
        for row, column in pairs:
            cells[row, column]["records"].append(claim_id)
    # This optional input must be validated against current files and source
    # pages by its loader. It supplies individual reviewed edges, never labels
    # to expand across this assertion's other rows/columns.
    scoped_by_claim = {claim.get("claim_id"): claim for claim in scoped_claims}
    existing_claims = set(details)
    existing_claims.update(r.get("claim_id") for r in reviewed or [] if isinstance(r, dict))
    # Coverage reviews fill previously unmapped claims only. Existing reviews
    # retain precedence and an explicit withheld pair remains blocked.
    additions = [r for r in coverage_reviewed or [] if isinstance(r, dict)
                 and r.get("claim_id") not in existing_claims]
    for relation in [*(reviewed or []), *additions]:
        if not isinstance(relation, dict):
            continue
        pair = relation.get("row"), relation.get("column")
        claim_id = relation.get("claim_id")
        claim = scoped_by_claim.get(claim_id)
        if (claim_id, *pair) in withheld or pair not in cells or not claim or relation.get("doc_id") != claim.get("doc_id"):
            continue
        if claim.get("review_status") not in _REVIEWED or relation.get("relation") not in {"contrast", "condition", "method", "interpretation"}:
            continue
        if not all(isinstance(relation.get(key), dict) and isinstance(relation[key].get("pdf_page"), int)
                   and relation[key]["pdf_page"] > 0 and relation[key].get("quote")
                   for key in ("process_evidence", "result_evidence")):
            continue
        if claim_id not in details:
            rows, columns = _claim_matches(claim)
            details[claim_id] = _claim_detail(claim, by_doc[claim["doc_id"]], rows, columns, kind_reviews)
        found = cells[pair]
        if claim_id not in found["records"]:
            found["records"].append(claim_id)
        found["reviewed_records"][claim_id] = dict(relation)
    materials = {}
    for d in details.values():
        facet = materials.setdefault(d["system"], {"label":d["system_label"], "docs":set()})
        facet["docs"].add(d["doc_id"])
    if system:
        details = {cid:d for cid,d in details.items() if d["system"] == system}
        for cell in cells.values():
            cell["records"] = [cid for cid in cell["records"] if cid in details]
            cell["reviewed_records"] = {cid:r for cid,r in cell["reviewed_records"].items() if cid in details}
    system_options = [{"id":key, "label":value["label"], "documents":len(value["docs"])}
                      for key,value in sorted(materials.items(), key=lambda kv:(-len(kv[1]["docs"]),kv[0]))]
    for cell in cells.values():
        records = [details[cid] for cid in cell["records"]]
        kinds = defaultdict(set)
        systems = defaultdict(list)
        for detail in records:
            kinds[_cell_source_kind(cell, detail)].add(detail["doc_id"])
            systems[detail["system"]].append(detail)
        contrast_docs = {d["doc_id"] for d in records if d["source_kind"] == "experiment"
                         and cell["reviewed_records"].get(d["claim_id"], {}).get("relation") == "contrast"}
        cell.update(contrasts=len(contrast_docs), documents=len({d["doc_id"] for d in records}), claims=len(records),
                    experiments=len(kinds["experiment"]), simulations=len(kinds["simulation"]),
                    source_counts={key: len(value) for key, value in kinds.items()},
                    claim_type_counts=dict(Counter(d["claim_type"] for d in records)),
                    systems=[{"id": key, "label": ds[0]["system_label"], "documents": len({d["doc_id"] for d in ds}),
                              "claims": len(ds), "experiments": len({d["doc_id"] for d in ds if _cell_source_kind(cell, d) == "experiment"})}
                             for key, ds in sorted(systems.items())])
    matrix = list(cells.values())
    types = {c["claim_id"]:(details[c["claim_id"]]["source_kind"] if c["claim_id"] in details
             else resolve_map_source_kind(by_doc[c["doc_id"]],c,kind_reviews) or _source_kind(by_doc[c["doc_id"]],c))
             for c in scoped_claims if c.get("claim_id")}
    disputed = {claim_id for claim_id, _, _ in withheld}
    unmapped = {cid: _unmapped_reason(scoped_by_claim[cid], kind, disputed, system)
                for cid, kind in types.items() if cid not in details}
    return {"rows": [dict(row) for row in ROWS], "columns": [dict(column) for column in COLUMNS], "cells": matrix,
            "directions": _directions(matrix, details, by_doc), "record_details": details,"record_types":types,
            "unmapped": unmapped, "unmapped_reason_labels": dict(UNMAPPED_REASON_LABELS),
            "totals": {"documents": len(selected), "claims": len(scoped_claims),
                       "mapped_documents": len({detail["doc_id"] for detail in details.values()}), "mapped_claims": len(details)},
            "route": route, "system":system,"system_options":system_options, "version": "2.0",
            "method": "基础映射仅用已复核且有原页证据的单条结构化论断：工艺/样品条件由本条正文及非免责声明条件识别，结果由本条研究指标或明确观察识别；不跨论断、不用整篇标签作笛卡尔积。如提供另经原页语境核验的关系，仅逐对加入指定格，不向其他行列扩张。每格按不同资料去重，多格计数不可相加。实测仅指原创 reported_experiment，仿真、综述转述、厂商规格、技术指导、方法及作者解释分列。工艺条件与结果共现只表示研究覆盖，不证明因果；各体系、样品和测量口径须逐条核对。候选只从预定义可检验问题中，筛选实测资料≤2篇且存在同行/同列、同体系不同独立资料实测锚点的格；低覆盖仅是本库已抽取记录空白，不是全文或领域空白。",
            "limitations": ["规则仅识别列出的工艺与结果词，可能漏掉同义表达；未入图不表示原文未研究。", "目录路线为文献级筛选，同一文献内的子试验路线仍需原页复核。", "资料复核为自动化 agent 复核，未经专家穷尽审查；计数不衡量效果大小、证据质量或创新性。"]}
