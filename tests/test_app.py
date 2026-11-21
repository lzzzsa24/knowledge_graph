"""UI regressions with real pilot sources and explicitly mocked QA responses.

These tests never read the user's saved API key or call a model service.
The synthetic chat fixture tests rendering/history only, not answer quality.
"""
from pathlib import Path
import json
import os
import sqlite3

import pytest

from streamlit.testing.v1 import AppTest
from streamlit.proto.TextInput_pb2 import TextInput as TextInputProto

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def isolated_qa(monkeypatch):
    """Patch before AppTest runs, even when a real user is configuring the app."""
    from kg_pilot import qa

    status = {"configured": False, "provider": "mock-ui-only", "model": "",
              "models": [], "message": "合成界面测试：尚未配置模型。"}
    calls = []

    def no_live_access(*args, **kwargs):
        pytest.fail("UI tests must not read credentials, save configuration, or call an API")

    monkeypatch.setattr(qa, "provider_status", lambda: dict(status))
    monkeypatch.setattr(qa, "configure_provider", no_live_access)
    monkeypatch.setattr(qa, "answer_question", no_live_access)
    # Guard lower-level paths as well: accidental fallback must fail before any
    # real configuration/secret read, DPAPI operation, or network request.
    for name in ("_config", "_api_key", "protect_secret", "_request"):
        monkeypatch.setattr(qa, name, no_live_access)
    return {"module": qa, "status": status, "calls": calls}


def launch():
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=25).run()
    assert not app.exception
    return app


def test_overview_loads_real_catalog_and_reviewed_claims():
    app = launch()
    app.radio(key="navigation").set_value("概览").run()
    assert not app.exception
    papers = json.loads((ROOT / "pilot/catalog/papers.json").read_text(encoding="utf-8-sig"))
    claims = (ROOT / "pilot/annotations/claims.jsonl").read_text(encoding="utf-8-sig").splitlines()
    assert app.metric[0].value == str(len(papers))
    assert app.metric[1].value == str(len([line for line in claims if line.strip()]))
    assert "不是专家金标准" in app.info[0].value


def test_qa_unconfigured_disables_chat_and_has_password_configuration():
    app = launch()
    assert app.radio(key="navigation").value == "论文问答"
    assert app.chat_input(key="qa_input").disabled
    password = app.text_input(key="qa_platform_password_0")
    assert password.label == "API 密钥"
    assert password.proto.type == TextInputProto.PASSWORD
    assert password.value == ""
    assert any(button.label == "连接并保存" for button in app.button)
    assert any("当前不能生成论文回答" in item.value for item in app.info)
    examples = [button for button in app.button if (button.key or "").startswith("qa_example_")]
    assert len(examples) == 3 and all(button.disabled for button in examples)
    assert app.session_state["qa_messages"] == []


def test_qa_synthetic_response_shows_source_pdf_and_passes_history(monkeypatch, isolated_qa):
    """Mock-only UI test; the answer is not produced by DeepSeek or any live model."""
    claims = [json.loads(line) for line in
              (ROOT / "pilot/annotations/claims.jsonl").read_text(encoding="utf-8-sig").splitlines()
              if line.strip()]
    claim = next(item for item in claims if item["claim_id"] == "j001")
    papers = json.loads((ROOT / "pilot/catalog/papers.json").read_text(encoding="utf-8-sig"))
    paper = next(item for item in papers if item["doc_id"] == claim["doc_id"])
    evidence = claim["evidence"][0]
    response = {
        "answer": "【合成UI测试，非模型结果】这是用于界面回归的占位回答。[S1]",
        "sources": [{"id": "S1", "doc_id": paper["doc_id"], "title": paper["title"],
                     "pdf_page": evidence["pdf_page"], "text": evidence["quote"], "claim_ids": ["j001"]}],
        "provider": "mock-ui-only", "model": "deepseek-ui-mock",
        "answerability": "answerable", "warnings": ["合成响应仅验证界面，不代表真实模型质量。"],
    }
    isolated_qa["status"].update(configured=True, model="deepseek-ui-mock",
                                 models=["deepseek-ui-mock"], message="合成界面测试：已启用mock响应。")

    def synthetic_answer(question, history=None):
        isolated_qa["calls"].append({"question": question, "history": list(history or [])})
        return json.loads(json.dumps(response, ensure_ascii=False))

    monkeypatch.setattr(isolated_qa["module"], "answer_question", synthetic_answer)
    app = launch()
    assert not app.chat_input(key="qa_input").disabled
    app.chat_input(key="qa_input").set_value("合成测试问题：显示这条记录的来源。").run()
    assert not app.exception
    assert len(isolated_qa["calls"]) == 1
    assert isolated_qa["calls"][0]["history"] == []
    assert len(app.get("chat_message")) == 2
    assert any(response["answer"] in item.value for item in app.markdown)
    assert any(item.label.startswith("[S1]") and paper["title"] in item.label for item in app.expander)
    assert any(block.value == evidence["quote"].strip() for block in app.code)
    assert any("j001" in item.value for item in app.caption)
    app.checkbox(key="qa_1_0_preview").check().run()
    assert not app.exception
    assert len(app.get("image")) == 1
    assert len(isolated_qa["calls"]) == 1  # Expanding a source must not regenerate.
    app.chat_input(key="qa_input").set_value("合成测试追问：保留上一轮历史。").run()
    assert not app.exception
    assert len(isolated_qa["calls"]) == 2
    assert isolated_qa["calls"][1]["history"] == [
        {"role": "user", "content": "合成测试问题：显示这条记录的来源。"},
        {"role": "assistant", "content": response["answer"]},
    ]


def test_qa_sort_groups_turns_without_reordering_model_history(monkeypatch, isolated_qa):
    isolated_qa["status"].update(configured=True, model="mock-sort-only", models=["mock-sort-only"])

    def synthetic_answer(question, history=None):
        isolated_qa["calls"].append({"question": question, "history": list(history or [])})
        return {"answer": f"合成回答：{question}", "sources": [], "provider": "mock-ui-only",
                "model": "mock-sort-only", "answerability": "supported", "warnings": []}

    monkeypatch.setattr(isolated_qa["module"], "answer_question", synthetic_answer)
    app = launch()
    assert app.segmented_control(key="qa_sort_order").value == "最新在前"

    def rendered_messages():
        return [message.markdown[0].value for message in app.get("chat_message")]

    app.chat_input(key="qa_input").set_value("第一问").run()
    app.chat_input(key="qa_input").set_value("第二问").run()
    assert not app.exception
    assert rendered_messages() == ["第二问", "合成回答：第二问", "第一问", "合成回答：第一问"]
    chronological = [
        {"role": "user", "content": "第一问"}, {"role": "assistant", "content": "合成回答：第一问"},
        {"role": "user", "content": "第二问"}, {"role": "assistant", "content": "合成回答：第二问"},
    ]
    assert [{"role": item["role"], "content": item["content"]}
            for item in app.session_state["qa_messages"]] == chronological
    assert [item.value for item in app.caption if item.value.startswith("提问 ")] == ["提问 2", "提问 1"]
    app.segmented_control(key="qa_sort_order").set_value("最早在前").run()
    assert not app.exception
    assert rendered_messages() == [item["content"] for item in chronological]
    assert len(isolated_qa["calls"]) == 2
    app.chat_input(key="qa_input").set_value("第三问").run()
    assert not app.exception
    assert isolated_qa["calls"][-1]["history"] == chronological
    assert rendered_messages()[-2:] == ["第三问", "合成回答：第三问"]
    app.segmented_control(key="qa_sort_order").set_value("最新在前").run()
    assert not app.exception
    assert rendered_messages() == ["第三问", "合成回答：第三问", "第二问", "合成回答：第二问", "第一问", "合成回答：第一问"]
    assert [item.value for item in app.caption if item.value.startswith("提问 ")] == ["提问 3", "提问 2", "提问 1"]
    assert len(isolated_qa["calls"]) == 3


def test_knowledge_filter_preserves_source_evidence_and_renders_page():
    app = launch()
    app.radio(key="navigation").set_value("知识记录").run()
    app.selectbox(key="claim_doc").set_value("imec_06").run()
    app.selectbox(key="claim_stage").set_value("measurement").run()
    assert not app.exception
    table = app.dataframe[0].value
    assert len(table) > 0
    assert set(table["文献ID"]) == {"imec_06"}
    assert set(table["阶段"]) == {"测量"}
    assert app.code and any("400" in block.value for block in app.code)
    assert len(app.get("image")) == 1
    assert any("agent 自动复核" in item.value for item in app.caption)


def test_keyword_search_executes_and_returns_source_pages():
    app = launch()
    app.radio(key="navigation").set_value("全文检索").run()
    assert app.radio(key="search_mode").value == "自动"
    app.radio(key="search_mode").set_value("关键词").run()
    app.text_input(key="search_query").set_value("pumping")
    app.button(key="search_submit").click().run()
    assert not app.exception
    assert app.session_state["executed_query"] == "pumping"
    assert app.session_state["search_results"]
    assert "pumping" in app.code[0].value.casefold()
    assert len(app.get("image")) == 1


def test_hybrid_search_result_renders_source_and_structured_reasons():
    with sqlite3.connect((ROOT / "pilot/indexes/pilot.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        evidence_id, doc_id, page, text = db.execute(
            "SELECT evidence_id,doc_id,pdf_page,text FROM evidence WHERE doc_id=? ORDER BY evidence_id LIMIT 1",
            ("imec_06",)).fetchone()
    app = launch()
    app.radio(key="navigation").set_value("全文检索").run()
    assert app.radio(key="search_mode").value == "自动"
    app.session_state["search_results"] = [{
        "evidence_id": evidence_id, "doc_id": doc_id, "pdf_page": page,
        "text": "mock tokenizer reconstruction", "method": "dense", "score": .8,
        "rrf_score": .031, "retrieval_score": .05,
        "retrieval_reasons": [{"method": "bilingual_fts", "terms": ["annealing"]}],
    }]
    app.session_state["executed_query"] = "退火条件"
    app.session_state["executed_mode"] = "自动"
    app.session_state["search_notice"] = "语义模型或向量检索暂不可用，本次使用本地术语与关键词检索。"
    app.run()
    assert not app.exception
    assert app.code[0].value.strip() == text.strip()
    assert any("综合排序：0.0500" in caption.value for caption in app.caption)
    assert any("中英术语匹配：annealing" in caption.value for caption in app.caption)
    assert any("本地术语与关键词检索" in info.value for info in app.info)
    assert len(app.get("image")) == 1


def test_literature_stage_facets_evidence_empty_and_reset():
    app = launch()
    app.radio(key="navigation").set_value("文献库").run()
    assert not app.exception
    assert len(app.dataframe[0].value) == len(json.loads((ROOT / "pilot/catalog/papers.json").read_text(encoding="utf-8-sig")))
    app.selectbox(key="paper_category").set_value("characterization_evaluation").run()
    app.multiselect(key="paper_facet_relevance").set_value(["support"]).run()
    assert not app.exception
    assert "imec_05" in set(app.dataframe[0].value["文献ID"])
    assert app.code and any("分类依据" in caption.value for caption in app.caption)
    app.multiselect(key="paper_facet_methods").set_value(["simulation"]).run()
    assert not app.exception
    # This route now contains simulations. Check the three intersecting tags
    # against the current catalog; exercise the empty state with a distinct query.
    catalog = json.loads((ROOT / "pilot/catalog/classifications.json").read_text(encoding="utf-8-sig"))
    expected = {c["doc_id"] for c in catalog["records"]
                if c.get("primary_category") == "characterization_evaluation"
                and c.get("relevance") == "support" and "simulation" in c.get("methods", [])}
    actual = set(app.dataframe[0].value["文献ID"]) if app.dataframe else set()
    assert actual == expected
    app.text_input(key="paper_keyword").set_value("__fixture_no_matching_scientific_title_33ad40ef__").run()
    assert not app.exception
    assert not app.dataframe
    assert any("没有符合" in info.value for info in app.info)
    app.button(key="paper_reset").click().run()
    assert not app.exception
    assert len(app.dataframe[0].value) == len(json.loads((ROOT / "pilot/catalog/papers.json").read_text(encoding="utf-8-sig")))
    assert app.selectbox(key="paper_category").value is None
    assert app.multiselect(key="paper_facet_methods").value == []


@pytest.mark.skipif(os.environ.get("KG_PILOT_RUN_SEMANTIC_TEST") != "1",
                    reason="Opt-in integration test uses the existing local BGE-M3 cache; no download.")
def test_semantic_chinese_query_uses_original_evidence_and_page():
    app = launch()
    app.radio(key="navigation").set_value("全文检索").run()
    assert app.radio(key="search_mode").value == "自动"
    app.radio(key="search_mode").set_value("语义检索（中英文）").run()
    app.text_input(key="search_query").set_value("铜焊盘下凹与退火条件")
    app.button(key="search_submit").click().run(timeout=180)
    assert not app.exception
    assert not app.error
    assert app.session_state["executed_mode"] == "语义检索（中英文）"
    rows = app.session_state["search_results"]
    assert rows and all("rrf_score" in row for row in rows)
    first = rows[0]
    with sqlite3.connect((ROOT / "pilot/indexes/pilot.sqlite").as_uri() + "?mode=ro", uri=True) as db:
        source = db.execute("SELECT doc_id,pdf_page,text FROM evidence WHERE evidence_id=?",
                            (first["evidence_id"],)).fetchone()
    assert source is not None
    assert (first["doc_id"], first["pdf_page"]) == source[:2]
    assert first["text"] == source[2]
    assert app.code[0].value.strip() == source[2].strip()
    assert len(app.get("image")) == 1
    assert any("综合排序" in item.value for item in app.caption)
    print(f"Chinese semantic query: {len(rows)} hits; first source {source[0]}, PDF page {source[1]}; original quote and page image displayed.")
