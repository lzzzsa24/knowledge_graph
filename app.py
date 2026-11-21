"""Source-linked literature browser and configured-model question answering."""
from __future__ import annotations

import json
import re
import sqlite3
from contextlib import closing
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import quote

import pymupdf
import streamlit as st

ROOT = Path(__file__).resolve().parent
PILOT = ROOT / "pilot"
sys.path.insert(0, str(ROOT / "src"))
from kg_pilot.pipeline import search  # noqa: E402
from kg_pilot.retrieval import is_crosslingual_query, retrieval_model as semantic_model, retrieve, semantic  # noqa: E402
from kg_pilot.effective_evidence import FIELD, load_effective_evidence  # noqa: E402

STAGES = {
    "surface_preparation": "表面准备",
    "contact_bonding": "接触键合",
    "post_bond_annealing": "键合后退火",
    "measurement": "测量",
    "reliability_test": "可靠性试验",
    "material_characterization": "材料表征",
    "background": "背景与综述",
}
CLAIM_TYPES = {
    "reported_experiment": "作者报告的实验",
    "reported_simulation": "作者报告的仿真",
    "author_interpretation": "作者解释",
    "review_summary": "综述归纳",
    "manufacturer_specification": "厂商声明规格",
    "recommended_process": "推荐工艺条件",
    "standard_requirement": "规范要求",
    "test_method": "测试方法",
    "cited_prior_work": "引述前人工作",
}


def read_json(path: Path, default=None):
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return default


def local_pdf(paper: dict) -> Path | None:
    """Rebase catalog paths when the project is moved; serve only project files."""
    raw = str(paper.get("local_path") or "").replace("\\", "/")
    match = re.search(r"(?:^|/)pilot/", raw, flags=re.IGNORECASE)
    if match:
        path = ROOT / raw[match.start():].lstrip("/")
    else:
        path = ROOT / raw
    path = path.resolve()
    if not path.is_relative_to(ROOT) or not path.is_file() or path.suffix.lower() != ".pdf":
        return None
    return path


@st.cache_data(max_entries=32, show_spinner=False)
def page_image(path: str, modified_ns: int, page: int) -> bytes:
    # The modification timestamp invalidates cached renderings after PDF replacement.
    with pymupdf.open(path) as document:
        if not 1 <= page <= len(document):
            raise ValueError("原文页码超出 PDF 范围")
        return document[page - 1].get_pixmap(matrix=pymupdf.Matrix(1.4, 1.4)).tobytes("png")


def original_evidence(evidence_id: str) -> dict | None:
    """Render source text, not the tokenizer-decoded text stored in vector payloads."""
    database = PILOT / "indexes" / "pilot.sqlite"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
        row = connection.execute("SELECT doc_id,pdf_page,text FROM evidence WHERE evidence_id=?",
                                 (evidence_id,)).fetchone()
    if row:
        return load_effective_evidence(PILOT).source(dict(zip(("doc_id", "pdf_page", "text"), row)))
    return None


def paper_links(paper: dict, key: str):
    columns = st.columns(3)
    doi = str(paper.get("doi") or "").removeprefix("https://doi.org/")
    if doi:
        columns[0].link_button("DOI / 出版页", "https://doi.org/" + quote(doi, safe="/():"))
    source = paper.get("pdf_url") or paper.get("publisher_url")
    if source and str(source).startswith(("https://", "http://")):
        columns[1].link_button("公开全文来源", source)
    path = local_pdf(paper)
    if path:
        columns[2].download_button("下载原始 PDF", path.read_bytes(), file_name=path.name,
                                   mime="application/pdf", key=f"download_{key}", on_click="ignore")
    else:
        st.warning("未找到此文献的本地原始 PDF。")


def show_page(paper: dict, page: int):
    path = local_pdf(paper)
    if path is None:
        st.warning("原始 PDF 当前不可读取。")
        return
    try:
        png = page_image(str(path), path.stat().st_mtime_ns, int(page))
        st.image(png, caption=f"{paper['doc_id']} · PDF 实际第 {page} 页（含封面，区别于印刷页码）",
                 width="stretch")
    except (OSError, ValueError, RuntimeError) as exc:
        st.error(f"该原页暂时无法显示：{exc}")


def show_claim(claim: dict, papers_by_id: dict):
    st.subheader(claim["statement"])
    paper = papers_by_id[claim["doc_id"]]
    st.caption(f"{claim['claim_id']} · {paper['title']} · {CLAIM_TYPES.get(claim['claim_type'], claim['claim_type'])}")
    st.write(f"**材料：** {claim['material_system']}　 **阶段：** {STAGES.get(claim['stage'], claim['stage'])}")
    st.write(f"**样品：** {claim['sample_id']}　 **数值原文：** {claim['value_text']}　 **单位：** {claim.get('unit') or '未单独给出'}")
    review_label = "API 模型复核" if claim.get("review_status") == "api_model_reviewed" else "agent 自动复核"
    st.caption(f"本记录已完成 {review_label}与引文定位检查；属于试点数据，尚未经领域专家确认。")
    st.write("**条件与适用范围**")
    st.json(claim.get("conditions", {}), expanded=True)
    for qualification in claim.get(FIELD, []):
        st.caption(" ".join(dict.fromkeys((qualification["summary"], qualification["usage_limit"]))))
    if claim.get("notes"):
        st.info(str(claim["notes"]))
    st.write("**原文证据**")
    for evidence in claim.get("evidence", []):
        st.caption(f"PDF 实际第 {evidence['pdf_page']} 页" + (" · OCR 文字" if evidence.get("parser") else ""))
        st.code(evidence["quote"], language=None, wrap_lines=True)
    paper_links(paper, f"claim_{claim['claim_id']}")
    pages = sorted({e["pdf_page"] for e in claim.get("evidence", [])}
                   | {page for q in claim.get(FIELD, []) for page in q["pdf_pages"]})
    if pages:
        selected = st.selectbox("选择证据原页", pages, key=f"claim_page_{claim['claim_id']}",
                                format_func=lambda n: f"PDF 第 {n} 页")
        show_page(paper, selected)


def overview(papers: list, claims: list):
    st.header("试点概览")
    cols = st.columns(4)
    cols[0].metric("全文文献", len(papers))
    cols[1].metric("知识记录", len(claims))
    cols[2].metric("原始 PDF 页数", sum(p.get("page_count", 0) for p in papers))
    cols[3].metric("综述文献", sum(bool(p.get("is_review")) for p in papers))
    st.info("知识记录由 agent 抽取并交叉自动复核，保留样品、阶段、条件和原文证据。它们不是专家金标准，也不代表自动抽取准确率达到 100%。")
    left, right = st.columns(2)
    with left:
        st.subheader("记录涉及的阶段")
        counts = Counter(c["stage"] for c in claims)
        st.dataframe([{"阶段": STAGES.get(k, k), "记录数": v} for k, v in counts.most_common()],
                     hide_index=True, width="stretch")
    with right:
        st.subheader("证据类型")
        counts = Counter(c["claim_type"] for c in claims)
        st.dataframe([{"类型": CLAIM_TYPES.get(k, k), "记录数": v} for k, v in counts.most_common()],
                     hide_index=True, width="stretch")
    st.write("从侧栏进入文献库、知识记录或全文检索。每条知识记录都可回看原文引文和 PDF 实际页图。")
    summary = read_json(PILOT / "reports" / "claim_validation_summary.json", {})
    if summary:
        with st.expander("原 26 篇试点的 agent 自动复核运行记录"):
            st.json(summary)


def literature(papers: list, claims: list):
    st.header("文献库")
    from kg_pilot.classification import classified_papers, taxonomy, filter_papers, CATEGORY_LABELS, FACET_DEFINITIONS
    try:
        papers = classified_papers(papers, claims)
        catalog = taxonomy(papers)
    except (ValueError, KeyError, OSError):
        st.warning("分类资料暂时不可读取，仍可浏览原始文献。")
        catalog = taxonomy([])

    def clear_filters():
        for field, _, _, _ in FACET_DEFINITIONS:
            st.session_state[f"paper_facet_{field}"] = []
        st.session_state.update(paper_keyword="", paper_kind="全部", paper_year=None,
                                paper_category=None, paper_related=False)

    st.caption("按工艺阶段浏览；配套入口收录芯片制备、检测评价和综合研究。")
    related = st.checkbox("包含关联阶段", key="paper_related")
    category_counts = catalog["related_category_counts"] if related else catalog["category_counts"]
    category = st.selectbox("工艺阶段 / 配套入口", [None, *CATEGORY_LABELS], key="paper_category",
                            format_func=lambda value: "全部文献" if value is None else
                            f"{CATEGORY_LABELS[value]} · {category_counts.get(value, 0)} 篇")
    left, right = st.columns([3, 1])
    term = left.text_input("标题 / 主题关键词", key="paper_keyword")
    kind = right.selectbox("文献类型", ["全部", "原始研究", "综述"], key="paper_kind")
    years = sorted({p.get("year") for p in papers if p.get("year")}, reverse=True)
    year = st.selectbox("发表年份", [None, *years], key="paper_year", format_func=lambda v: "全部" if v is None else str(v))
    facets = {}
    with st.expander("多维标签筛选", expanded=False):
        st.caption("同一维度多选取并集，不同维度取交集。未明确表示论文未充分说明该属性。")
        columns = st.columns(3)
        for i, facet in enumerate(catalog["facets"]):
            labels = {option["value"]: option["label"] for option in facet["options"]}
            facets[facet["id"]] = columns[i % 3].multiselect(
                facet["label"], list(labels), format_func=lambda value, names=labels: names[value],
                key=f"paper_facet_{facet['id']}")
    st.button("重置文献筛选", key="paper_reset", on_click=clear_filters)
    filtered = filter_papers(papers, categories=[category] if category else [], include_related=related,
                             facets=facets, query=term, year=year)
    filtered = [p for p in filtered if kind == "全部" or bool(p.get("is_review")) == (kind == "综述")]
    st.caption(f"当前 {len(filtered)} / {len(papers)} 篇")
    if not filtered:
        st.info("没有符合当前条件的文献。")
        return
    st.dataframe([{"文献ID": p["doc_id"], "标题": p["title"], "年份": p.get("year"),
                   "主分类": CATEGORY_LABELS.get((p.get("classification") or {}).get("primary_category"), "待分类"),
                   "类型": "综述" if p.get("is_review") else "原始研究", "PDF页数": p.get("page_count")}
                  for p in filtered], hide_index=True, width="stretch")
    by_id = {p["doc_id"]: p for p in filtered}
    selected = st.selectbox("选择文献", list(by_id), key="paper_choice", format_func=lambda d: by_id[d]["title"])
    paper = by_id[selected]
    st.subheader(paper["title"])
    st.caption(f"{selected} · {paper.get('year', '年份未给出')} · {sum(c['doc_id'] == selected for c in claims)} 条知识记录")
    if paper.get("relevance_reason"):
        st.write(paper["relevance_reason"])
    annotation = paper.get("classification")
    if annotation:
        st.write("**主分类：** " + CATEGORY_LABELS[annotation["primary_category"]])
        if annotation["related_stages"]:
            st.caption("关联阶段：" + "、".join(CATEGORY_LABELS[value] for value in annotation["related_stages"]))
        st.write(annotation["classification_reason"])
        with st.expander("文献标签与分类依据"):
            for facet in catalog["facets"]:
                names = {option["value"]: option["label"] for option in facet["options"]}
                value = annotation.get(facet["id"])
                values = value if isinstance(value, list) else ([value] if value else [])
                st.write(f"**{facet['label']}：** " + ("、".join(names.get(v, v) for v in values) or "未明确"))
            for source in annotation["evidence"]:
                st.caption(f"分类依据 · PDF 第 {source['pdf_page']} 页")
                st.code(source["quote"], language=None, wrap_lines=True)
            if st.checkbox("查看分类依据原页", key=f"classification_preview_{selected}"):
                source_page = st.selectbox("分类依据页码", sorted({e["pdf_page"] for e in annotation["evidence"]}),
                                           key=f"classification_page_{selected}")
                show_page(paper, source_page)
        with st.expander("资料处理进度"):
            for value in annotation["processing"].values():
                st.write(value)
    else:
        st.info("该资料尚未分类，或原始 PDF 已更新，需要重新核对。")
    paper_links(paper, f"paper_{selected}")
    if st.checkbox("查看 PDF 原页", key="paper_preview"):
        page = st.number_input("PDF 实际页码", min_value=1, max_value=paper.get("page_count", 1),
                               value=1, step=1, key=f"paper_page_{selected}")
        show_page(paper, page)


def knowledge(papers: list, claims: list):
    st.header("知识记录")
    by_id = {p["doc_id"]: p for p in papers}
    cols = st.columns(3)
    material = cols[0].selectbox("材料体系", [None, *sorted({c["material_system"] for c in claims})],
                                 format_func=lambda v: "全部" if v is None else v, key="claim_material")
    stage = cols[1].selectbox("工艺 / 测试阶段", [None, *STAGES],
                              format_func=lambda v: "全部" if v is None else STAGES[v], key="claim_stage")
    doc = cols[2].selectbox("来源文献", [None, *by_id], key="claim_doc",
                            format_func=lambda v: "全部" if v is None else f"{v} · {by_id[v]['title']}")
    filtered = [c for c in claims if (material is None or c["material_system"] == material)
                and (stage is None or c["stage"] == stage) and (doc is None or c["doc_id"] == doc)]
    st.caption(f"当前 {len(filtered)} / {len(claims)} 条 · agent 自动复核")
    if not filtered:
        st.info("没有同时满足这些条件的知识记录。")
        return
    st.dataframe([{"记录ID": c["claim_id"], "陈述": c["statement"], "文献ID": c["doc_id"],
                   "阶段": STAGES.get(c["stage"], c["stage"])} for c in filtered],
                 hide_index=True, width="stretch")
    records = {c["claim_id"]: c for c in filtered}
    selected = st.selectbox("选择记录查看原文与页图", list(records), key="claim_choice",
                            format_func=lambda v: f"{v} · {records[v]['statement']}")
    show_claim(records[selected], by_id)


def retrieval_reason_label(reason) -> str:
    if not isinstance(reason, dict):
        return str(reason)
    labels = {"bilingual_fts": "中英术语匹配", "domain_query_rerank": "工艺术语重排",
              "document_scoped_fts": "文献范围匹配"}
    label = labels.get(reason.get("method"), reason.get("method", "检索"))
    terms = reason.get("terms") or []
    return label + ("：" + "、".join(str(term) for term in terms) if terms else "")


def fulltext_search(papers: list):
    st.header("全文检索")
    mode = st.radio("检索模式", ["自动", "关键词", "语义检索（中英文）"], horizontal=True, key="search_mode")
    if mode == "关键词":
        st.caption("使用 SQLite FTS 匹配原文术语，输入不会被自动扩展；中文整句可使用自动或语义检索。")
    elif mode == "自动":
        st.caption("中文与中英混输会结合本地术语扩展、关键词和语义检索；纯英文保持原文关键词检索。语义模型不可用时保留术语检索。")
    else:
        st.caption("使用本地 BGE-M3 与 Qdrant；中文与中英混输同时使用术语扩展和关键词，纯英文保持语义检索。首次加载需要稍候。")
    st.caption("检索不调用外部模型 API，只返回原文证据和实际 PDF 页，不生成问答结论。")
    with st.form("fulltext_search_form"):
        query = st.text_input("检索词", placeholder="例如：Cu pumping 或 铜焊盘下凹与退火条件", key="search_query")
        limit = st.selectbox("最多显示段落数", [5, 10, 20], index=1, key="search_limit")
        submitted = st.form_submit_button("检索", type="primary", key="search_submit")
    if submitted:
        if not query.strip():
            st.session_state["search_results"] = []
            st.session_state["executed_query"] = ""
            st.warning("请输入检索内容。")
        else:
            try:
                notice = ""
                if mode != "关键词" and is_crosslingual_query(query):
                    with st.spinner("正在本地检索相关原文……"):
                        try:
                            results = retrieve(query, semantic_model(), limit=limit)
                        except Exception:
                            results = retrieve(query, model=None, limit=limit)
                            notice = "语义模型或向量检索暂不可用，本次使用本地术语与关键词检索。"
                elif mode == "语义检索（中英文）":
                    with st.spinner("正在本地检索相关原文……"):
                        results = semantic(query, semantic_model(), limit=limit)
                else:
                    results = search(query, limit=limit)
                st.session_state["search_results"] = results
                st.session_state["executed_query"] = query
                st.session_state["executed_mode"] = mode
                st.session_state["search_notice"] = notice
            except Exception as exc:
                st.session_state["search_results"] = []
                st.session_state["executed_query"] = ""
                st.error(f"检索暂时不可用，请检查本地索引与模型缓存：{exc}")
    rows = st.session_state.get("search_results", [])
    executed = st.session_state.get("executed_query", "")
    if not executed:
        return
    st.caption(f"已执行查询：{executed} · {st.session_state.get('executed_mode', '关键词')} · 返回 {len(rows)} 个原文片段")
    if st.session_state.get("search_notice"):
        st.info(st.session_state["search_notice"])
    if not rows:
        st.info("当前查询未返回证据。可以调整材料或工艺术语；未命中不等于不存在相关证据。")
        return
    by_id = {p["doc_id"]: p for p in papers}
    selected = st.selectbox("选择检索结果", range(len(rows)), key="search_result_choice",
                            format_func=lambda i: f"{i+1}. {by_id.get(rows[i]['doc_id'], {}).get('title', rows[i]['doc_id'])} · {rows[i]['evidence_id']}")
    result = rows[selected]
    # Dense payloads contain tokenizer-reconstructed segments. Resolve their source
    # element before presenting a quotation and a physical PDF page to the reader.
    source = original_evidence(result["evidence_id"])
    if source is None:
        st.warning("该段落在当前资料库中无法定位，请重新检索。")
        return
    result = {**result, **source}
    if result.get("retrieval_score") is not None:
        st.caption(f"综合排序：{result['retrieval_score']:.4f}（融合排名与术语匹配分数，不是事实正确概率）")
    elif result.get("rrf_score") is not None:
        st.caption(f"融合排序 RRF：{result['rrf_score']:.4f}（排序分数，不是事实正确概率）")
    elif result.get("method") == "dense":
        st.caption(f"语义相似度：{result['score']:.3f}（排序分数，不是事实正确概率）")
    if result.get("retrieval_reasons"):
        st.caption("检索依据：" + "；".join(retrieval_reason_label(reason) for reason in result["retrieval_reasons"]))
    for qualification in source.get(FIELD, []):
        st.caption(" ".join(dict.fromkeys((qualification["summary"], qualification["usage_limit"]))))
    st.code(result["text"], language=None, wrap_lines=True)
    page_match = re.search(r":p(\d+)(?::|$)", result["evidence_id"])
    pdf_page = result.get("pdf_page") or (int(page_match.group(1)) if page_match else None)
    paper = by_id.get(result["doc_id"])
    if paper:
        paper_links(paper, f"search_{selected}")
        if pdf_page:
            show_page(paper, int(pdf_page))
    st.caption("命中的原文段落不等于通过 agent 自动复核的知识陈述；使用数值前需检查样品与上下文。")


def qa_error_message(exc: Exception) -> str:
    """Do not expose raw provider errors, which may contain URLs or credentials."""
    try:
        from kg_pilot.qa import QAError
    except ImportError:
        QAError = None
    if QAError is not None and isinstance(exc, QAError):
        # The backend's QAError contract contains a sanitized, user-facing message.
        return str(exc) or "模型请求未完成，请检查配置后重试。"
    name = type(exc).__name__.lower()
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "问答组件尚未就绪，请检查项目依赖和问答后端。"
    if isinstance(exc, TimeoutError) or "timeout" in name:
        return "模型响应超时，本次没有生成回答。请稍后重试。"
    if any(word in name for word in ("authentication", "permission", "unauthorized")):
        return "模型服务未通过访问验证，请检查模型配置后重试。"
    if "ratelimit" in name or "quota" in name:
        return "模型服务当前请求过多或额度不足，本次没有生成回答。"
    if "connection" in name or "connect" in name:
        return "暂时无法连接模型服务，请检查服务状态与网络后重试。"
    return "本次回答生成失败。请稍后重试，或检查模型配置与本地证据索引。"


def qa_model_names(models) -> list[str]:
    names = []
    for item in models if isinstance(models, list) else []:
        name = item.get("id") or item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name.strip() and name not in names:
            names.append(name)
    return names


def show_qa_configuration(status: dict):
    """Configure the model endpoint and keep its key out of chat history."""
    configured = bool(status.get("configured"))
    previous_key = st.session_state.pop("qa_password_clear_on_rerun", None)
    if previous_key:
        # This is a new run, before the old widget is created. Never mutate a
        # password widget's session key after creating it in the same run.
        st.session_state.pop(previous_key, None)
    flash = st.session_state.pop("qa_configuration_success", None)
    if flash:
        st.success(flash)
    with st.expander("模型 API 配置", expanded=not configured):
        st.caption("填写服务商提供的 OpenAI 兼容 API 地址和聊天模型 ID。")
        try:
            from kg_pilot.qa import configure_provider
        except ImportError:
            configure_provider = None
        if configure_provider is None:
            st.info("本机配置组件尚未就绪，暂时无法连接或保存密钥。")
        if configured:
            st.caption(f"已配置模型：{status.get('model') or '未提供'}。使用当前地址时可留空密钥。")
        models = qa_model_names(status.get("models"))
        current_model = status.get("model") or ""
        if current_model and current_model not in models:
            models = [current_model, *models]
        default_url = "https://llmapi.paratera.com/v1"
        nonce = st.session_state.setdefault("qa_password_nonce", 0)
        password_key = f"qa_platform_password_{nonce}"
        with st.form("qa_provider_configuration", clear_on_submit=False):
            saved_url = (status.get("base_url") or default_url) if configured or current_model else ""
            base_url = st.text_input("API 地址（Base URL）", value=saved_url, max_chars=2048,
                                     placeholder="https://api.example.com/v1", help=f"并行智算云的地址为 {default_url}。")
            model_choice = st.selectbox("模型 ID", models, index=models.index(current_model) if current_model in models else None,
                                        accept_new_options=True, placeholder="输入模型 ID，或从列表选择",
                                        help="留空时尝试从服务商读取可用模型。")
            api_key = st.text_input("API 密钥", type="password", key=password_key,
                                    placeholder="沿用当前地址可留空，更换地址请填新密钥",
                                    disabled=configure_provider is None)
            submitted = st.form_submit_button("连接并保存", type="primary", disabled=configure_provider is None)
        if submitted:
            if not configured and not api_key.strip():
                st.warning("请先输入该服务的 API 密钥。")
            elif not base_url.strip():
                st.warning("请填写服务商的 API 地址。")
            else:
                try:
                    with st.spinner("正在验证连接并保存本机配置……"):
                        provider = "paratera" if base_url.strip().rstrip("/") == default_url else "openai_compatible"
                        saved = configure_provider(api_key=api_key.strip(), model=model_choice or "",
                                                   base_url=base_url.strip(), provider=provider)
                    if not isinstance(saved, dict):
                        raise ValueError("Invalid provider configuration status")
                except Exception as exc:
                    st.error(qa_error_message(exc))
                else:
                    if saved.get("configured"):
                        st.session_state["qa_password_clear_on_rerun"] = password_key
                        st.session_state["qa_password_nonce"] = nonce + 1
                        st.session_state["qa_configuration_success"] = (
                            f"已连接并保存本机配置。当前模型：{saved.get('model') or '未提供'}。可以开始提问。")
                        st.rerun()
                    else:
                        st.warning(str(saved.get("message") or "连接未完成，配置尚不可用于生成回答。"))


def show_qa_response(response: dict, papers_by_id: dict, turn_index: int):
    st.markdown(response["answer"])
    model = response.get("model") or "未提供模型名称"
    provider = response.get("provider") or "未提供服务名称"
    st.caption(f"模型：{model} · {provider}")
    answerability = response.get("answerability")
    labels = {
        "insufficient": "资料不足", "insufficient_evidence": "资料不足",
        "partial": "部分问题尚不能确定", "partially_answerable": "部分问题尚不能确定",
    }
    if answerability in labels:
        st.caption(labels[answerability])
    warnings = response.get("warnings") or []
    if isinstance(warnings, str):
        warnings = [warnings]
    for warning in warnings:
        st.warning(str(warning))
    sources = response.get("sources") or []
    if not sources:
        st.caption("暂无可查看的原文来源。")
        return
    st.write(f"**参考来源（{len(sources)}）**")
    for source_index, source in enumerate(sources):
        sid = str(source.get("id") or f"S{source_index + 1}")
        doc_id = source.get("doc_id")
        paper = papers_by_id.get(doc_id)
        title = source.get("title") or (paper or {}).get("title") or doc_id or "未注明文献"
        raw_page = source.get("pdf_page")
        page = int(raw_page) if str(raw_page).isdigit() and int(raw_page) > 0 else None
        page_label = f"PDF 实际第 {page} 页" if page else "未提供有效物理页码"
        source_key = f"qa_{turn_index}_{source_index}"
        with st.expander(f"[{sid}] {title} · {page_label}"):
            st.caption(str(doc_id or "文献 ID 未提供"))
            st.code(str(source.get("text") or "该来源未返回原文片段。"), language=None, wrap_lines=True)
            claim_ids = source.get("claim_ids") or []
            if claim_ids:
                st.caption("关联知识记录：" + "、".join(str(x) for x in claim_ids))
            if paper:
                paper_links(paper, source_key)
                if page and st.checkbox("显示 PDF 原页", key=f"{source_key}_preview"):
                    show_page(paper, page)
            else:
                st.warning("此来源未能对应到当前文献目录，暂时无法显示原始 PDF。")


def paper_qa(papers: list):
    st.header("论文问答")
    st.caption("基于本地论文证据回答，保留样品、工序与条件。展开来源可核对原引文和 PDF 实际页。")
    messages = st.session_state.setdefault("qa_messages", [])
    try:
        from kg_pilot.qa import answer_question, provider_status
        status = provider_status()
        if not isinstance(status, dict):
            raise ValueError("Invalid provider status")
    except Exception as exc:
        status = {"configured": False, "message": qa_error_message(exc)}
    configured = bool(status.get("configured"))
    show_qa_configuration(status)
    if configured:
        st.caption(f"当前模型：{status.get('model') or '未提供'} · {status.get('provider') or '未提供'}")
        if status.get("message"):
            st.info(str(status["message"]))
    else:
        st.warning(str(status.get("message") or "尚未配置可用的生成模型。"))
        st.info("当前不能生成论文回答。可以先使用“全文检索”查看证据，配置模型后再开始问答。")
    if messages and st.button("新对话", key="qa_clear"):
        st.session_state["qa_messages"] = []
        st.rerun()
    example_prompt = None
    if not messages:
        st.write("可以从这些问题开始：")
        examples = [
            ("键合与后退火", "(111)取向纳米孪晶Cu/SiO2研究中，200 °C键合与后退火分别持续多久？比接触电阻是多少？"),
            ("钝化组的对照条件", "Ti钝化与未钝化Cu/介质混合键合的电学良率和退火条件分别是什么，能否直接比较？"),
            ("可靠性证据的边界", "Cu/MoS2/Cu与SiCN混合键合的可靠性验证覆盖了多长时间，能否据此保证十年寿命？"),
        ]
        for column, (label, question) in zip(st.columns(3), examples):
            if column.button(label, key=f"qa_example_{label}", disabled=not configured):
                example_prompt = question
    newest_first = st.segmented_control("问答顺序", ["最新在前", "最早在前"],
                                        default="最新在前", required=True, key="qa_sort_order") == "最新在前"
    # Keep each user/assistant pair together and preserve original indexes for
    # source widget keys. Sorting affects display only, never model history.
    turns = []
    for index, message in enumerate(messages):
        if message["role"] == "user" or not turns:
            turns.append([])
        turns[-1].append((index, message))
    display_turns = list(enumerate(turns, 1))
    if newest_first:
        display_turns.reverse()
    by_id = {p["doc_id"]: p for p in papers}
    new_turn = st.empty() if newest_first else None
    for turn_number, turn in display_turns:
        for index, message in turn:
            with st.chat_message(message["role"]):
                if message.get("error"):
                    st.error(message["content"])
                elif message["role"] == "assistant":
                    show_qa_response(message["response"], by_id, index)
                else:
                    st.caption(f"提问 {turn_number}")
                    st.markdown(message["content"])
    if new_turn is None:
        new_turn = st.empty()
    prompt = st.chat_input("输入关于论文、工艺条件或实验结果的问题……", disabled=not configured, key="qa_input")
    prompt = prompt or example_prompt
    if not prompt or not prompt.strip() or not configured:
        return
    prompt = prompt.strip()
    # Only previous successful conversational turns are sent; historical framework
    # outputs and error messages are never substituted for a generated answer.
    history = [{"role": m["role"], "content": m["content"]}
               for m in messages if not m.get("error") and not m.get("failed")]
    user_message = {"role": "user", "content": prompt}
    messages.append(user_message)
    with new_turn.container():
        with st.chat_message("user"):
            st.caption(f"提问 {len(turns) + 1}")
            st.markdown(prompt)
        with st.chat_message("assistant"):
            try:
                with st.spinner("正在检索论文证据并生成回答……"):
                    response = answer_question(prompt, history=history)
                    if not isinstance(response, dict) or not isinstance(response.get("answer"), str) or not response["answer"].strip():
                        raise ValueError("No generated answer returned")
            except Exception as exc:
                user_message["failed"] = True
                message = qa_error_message(exc)
                messages.append({"role": "assistant", "content": message, "error": True})
                st.error(message)
            else:
                messages.append({"role": "assistant", "content": response["answer"], "response": response})
                show_qa_response(response, by_id, len(messages) - 1)


def framework_results():
    st.header("框架试验")
    st.info("此页展示已保存的历史框架试验。新问题请前往“论文问答”，可用性以该页模型状态为准。")
    report_paths = sorted((PILOT / "benchmarks" / "framework").glob("**/report.json"),
                          key=lambda p: p.stat().st_mtime_ns, reverse=True)
    reports = [(p, read_json(p)) for p in report_paths]
    reports = [(p, r) for p, r in reports if isinstance(r, dict)]
    if not reports:
        st.write("框架测试中，尚无可读取的结果。")
        return
    path, report = reports[0]
    status = report.get("status", "unknown")
    st.write("**运行状态：** " + {"running": "测试中", "completed": "运行完成", "complete": "运行完成", "failed": "运行失败"}.get(status, status))
    st.caption(str(path.relative_to(ROOT)))
    st.write("**本次试验范围**")
    st.write(report.get("scope", "尚未记录"))
    if report.get("llm_provider") == "session_agent_bridge":
        st.caption("本轮使用会话 agent 桥接返回模型回答；这不代表已部署独立模型 API 服务。")
    results = report.get("query_results", [])
    if not results:
        st.write("问题测试中，尚无已保存的回答。")
    for row in results:
        with st.expander(row.get("question", row.get("id", "试验问题")), expanded=True):
            st.markdown(row.get("answer") or "尚未生成回答。")
            if row.get("wall_seconds_including_live_agent") is not None:
                st.caption(f"本次耗时 {row['wall_seconds_including_live_agent']:.1f} 秒，包含会话 agent 等待时间。")
    with st.expander("试验配置与范围记录"):
        st.json({k: v for k, v in report.items() if k != "query_results"})


def _main():
    st.set_page_config(page_title="混合键合文献与证据", page_icon="🔬", layout="wide")
    st.title("混合键合 · 文献与证据")
    st.caption("从零收集的独立小规模试点 · 论文检索、证据浏览与问答")
    papers = read_json(PILOT / "catalog" / "papers.json")
    claim_path = PILOT / "annotations" / "claims.jsonl"
    if not isinstance(papers, list) or not claim_path.is_file():
        st.error("试点目录或知识记录尚未就绪，请先运行项目数据流程。")
        st.stop()
    try:
        raw_claims = [json.loads(line) for line in claim_path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
        view = load_effective_evidence(PILOT, papers=papers, claims=raw_claims)
        claims = [view.claim(claim) for claim in raw_claims]
    except (OSError, ValueError, KeyError):
        st.error("知识记录正在更新或暂时不可读取，请稍后刷新。")
        st.stop()
    page = st.sidebar.radio("浏览", ["论文问答", "概览", "文献库", "知识记录", "全文检索", "框架试验"], key="navigation")
    st.sidebar.caption(f"{len(papers)} 篇全文 · {len(claims)} 条知识记录")
    st.sidebar.caption("复核方式：agent 自动复核。尚未建立专家金标准。")
    if page == "论文问答":
        paper_qa(papers)
    elif page == "概览":
        overview(papers, claims)
    elif page == "文献库":
        literature(papers, claims)
    elif page == "知识记录":
        knowledge(papers, claims)
    elif page == "全文检索":
        fulltext_search(papers)
    else:
        framework_results()


def main():
    from kg_pilot.ingestion_publish import dataset_lock, DatasetBusy, maintenance_status
    try:
        with dataset_lock(PILOT):
            if maintenance_status(PILOT).get("active"):
                st.info("资料库正在切换或等待恢复，请稍后刷新；可在主 Web 的批量入库页面查看进度。")
                return
            _main()
    except DatasetBusy:
        st.info("资料库正在更新，请稍后刷新。")


if __name__ == "__main__":
    main()
