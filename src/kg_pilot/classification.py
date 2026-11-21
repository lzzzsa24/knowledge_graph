"""Paper-level taxonomy, independent of claim types and original catalog metadata."""
from __future__ import annotations

from collections import Counter
from pathlib import Path

from kg_pilot.pipeline import PILOT, read_json

VERSION = "1.0"
CATEGORIES = [
    ("structure_fabrication", "键合结构制备", "process", "介质层、金属焊盘、阻挡层与种子层及结构设计"),
    ("surface_planarization", "表面平坦化", "process", "CMP、粗糙度、铜凹陷与凸起及面内均匀性"),
    ("cleaning_activation", "键合前清洗与活化", "process", "污染去除、氧化层处理、活化及表面保护"),
    ("alignment_contact", "对准与接触键合", "process", "对准、放置、初始接触及接触条件"),
    ("post_bond_annealing", "键合后退火", "process", "温度、时间、气氛及退火引起的组织与界面演化"),
    ("post_bond_processing", "键合后加工与集成", "process", "后续减薄、背面加工及继续堆叠"),
    ("die_preparation_handling", "D2W 芯片制备与搬运", "support", "划片、挑片、转移与载具处理等路线专属研究"),
    ("characterization_evaluation", "检测表征与性能评价", "support", "测量方法、缺陷检测、性能及可靠性评价"),
    ("integrated_flow", "全流程与综合研究", "support", "多个主要阶段、流程比较及综合研究"),
]
CATEGORY_LABELS = {key: label for key, label, _, _ in CATEGORIES}
FACET_DEFINITIONS = [
    ("relevance", "课题相关性", False, {"core": "核心研究", "support": "专项支撑", "background": "基础背景", "peripheral": "外围线索"}),
    ("routes", "技术路线", True, {"W2W": "W2W · 晶圆对晶圆", "D2W": "D2W · 芯片对晶圆", "D2D": "D2D · 芯片对芯片"}),
    ("materials", "材料体系与界面材料", True, {}),
    ("topics", "关注问题", True, {}),
    ("methods", "研究方法", True, {"experimental": "实验", "simulation": "仿真", "theoretical": "理论分析", "literature_review": "文献综合"}),
    ("document_form", "资料形式", False, {"journal_paper": "期刊论文", "conference_paper": "会议论文", "thesis": "学位论文", "book": "教材 / 专著", "standard": "标准", "patent": "专利", "technical_report": "技术报告", "technical_manual": "技术手册 / 指南", "white_paper": "白皮书", "application_note": "应用说明", "datasheet": "产品技术规格", "test_method": "测试方法", "presentation": "技术演示 / 幻灯片", "webpage": "网页", "manuscript": "学术稿件"}),
    ("content_nature", "内容性质", False, {"original_research": "原创研究", "review": "综述", "tutorial": "教程", "commentary": "观点评论", "technical_guidance": "技术指导", "product_specification": "产品规格", "normative_document": "规范要求", "industry_overview": "行业技术概述"}),
    ("publication_status", "发表状态", False, {"published": "正式发表", "preprint": "预印本", "accepted": "已录用", "revised": "修订版", "unknown": "尚未确认"}),
    ("access_route", "获取途径", False, {"publisher": "出版平台", "institutional_repository": "机构知识库", "author_shared": "作者分享", "company_shared": "企业网站", "other": "其他来源"}),
]
FACET_IDS = {key for key, _, _, _ in FACET_DEFINITIONS}
MISSING = "__unspecified__"


def field_values(record: dict, field: str) -> list[str]:
    value = record.get(field)
    return list(value) if isinstance(value, list) else ([str(value)] if value else [])


def load_classifications(pilot: Path = PILOT) -> dict[str, dict]:
    path = pilot / "catalog" / "classifications.json"
    if not path.is_file():
        return {}
    data = read_json(path)
    if data.get("version") != VERSION or not isinstance(data.get("records"), list):
        raise ValueError("文献分类版本或格式不符合当前标准")
    rows = data["records"]
    result = {row["doc_id"]: row for row in rows}
    if len(result) != len(rows):
        raise ValueError("文献分类中存在重复文献 ID")
    return result


def classified_papers(papers: list[dict], claims: list[dict], pilot: Path = PILOT) -> list[dict]:
    annotations = load_classifications(pilot)
    counts = Counter(c["doc_id"] for c in claims)
    reviewed = Counter(c["doc_id"] for c in claims if c.get("review_status") == "independent_agent_reviewed")
    api_reviewed = Counter(c["doc_id"] for c in claims if c.get("review_status") == "api_model_reviewed")
    result = []
    for paper in papers:
        row = dict(paper)
        annotation = annotations.get(paper["doc_id"])
        # Never silently apply labels to a replaced PDF or a new unclassified paper.
        if annotation and annotation.get("source_sha256") == paper.get("sha256"):
            annotation = dict(annotation)
            annotation.pop("source_sha256", None)
            parsed = (pilot / "parsed" / paper["doc_id"] / "pages.json").is_file()
            annotation["processing"] = {
                "acquisition": "已获取全文",
                "parsing": "已保存全文解析，局部质量以原页为准" if parsed else "尚无全文解析",
                "extraction": f"试点抽取 {counts[paper['doc_id']]} 条，非全文穷尽",
                "claim_review": f"{reviewed[paper['doc_id']]} 条独立 agent 复核；{api_reviewed[paper['doc_id']]} 条 API 模型复核",
                "classification_review": "API 模型分类与原文定位核验" if annotation.get("classification_review") == "api_reviewed" else "agent 分类与原文定位核验",
            }
            row["classification"] = annotation
        else:
            row["classification"] = None
        result.append(row)
    return result


def taxonomy(papers: list[dict]) -> dict:
    records = [p["classification"] for p in papers if p.get("classification")]
    counts = Counter(c["primary_category"] for c in records)
    related = Counter(category for c in records for category in {c["primary_category"], *c.get("related_stages", [])})
    facets = []
    for field, label, multiple, names in FACET_DEFINITIONS:
        values = Counter(value for c in records for value in set(field_values(c, field) or [MISSING]))
        ordered = list(names) + sorted(values.keys() - names.keys())
        facets.append({"id": field, "label": label, "multiple": multiple,
                       "options": [{"value": value, "label": "未明确" if value == MISSING else names.get(value, value),
                                    "count": values[value]} for value in ordered]})
    return {"version": VERSION, "categories": [dict(id=key, label=label, group=group, description=description)
            for key, label, group, description in CATEGORIES], "facets": facets,
            "category_counts": {key: counts[key] for key in CATEGORY_LABELS},
            "related_category_counts": {key: related[key] for key in CATEGORY_LABELS},
            "total": len(papers), "classified": len(records), "unclassified": len(papers) - len(records),
            "filter_rule": "同一维度多选取并集，不同维度取交集；关联阶段浏览按文献去重。"}


def filter_papers(papers: list[dict], *, categories: list[str] | None = None,
                  include_related: bool = False, facets: dict[str, list[str]] | None = None,
                  query: str = "", year: int | None = None) -> list[dict]:
    """OR within a facet; AND across facets. Unknown selections never broaden results."""
    selected = set(categories or [])
    filters = {key: set(values) for key, values in (facets or {}).items() if values}
    if set(filters) - FACET_IDS:
        raise ValueError("未知的文献筛选维度")
    answer = []
    for paper in papers:
        c = paper.get("classification") or {}
        belongs = {c.get("primary_category", "__unclassified__")}
        if include_related:
            belongs.update(c.get("related_stages", []))
        if selected and not selected.intersection(belongs):
            continue
        if any(not wanted.intersection(field_values(c, key) or [MISSING]) for key, wanted in filters.items()):
            continue
        if year is not None and paper.get("year") != year:
            continue
        searchable = " ".join(str(paper.get(key) or "") for key in ("doc_id", "title", "topic", "doi", "publisher", "document_number", "version"))
        authors = paper.get("authors") or []
        searchable += " " + (" ".join(str(author) for author in authors) if isinstance(authors, list) else str(authors))
        searchable += " " + " ".join(str(value) for field in FACET_IDS for value in field_values(c, field))
        searchable += " " + " ".join(c.get("keywords", []))
        if query.strip().casefold() not in searchable.casefold():
            continue
        answer.append(paper)
    return answer
