/* Project report: the tech stack and how data flows through it, at a glance.
 * Headline counts come from /api/summary; index counts are a dated snapshot (no API exposes them). */
(function () {
  "use strict";

  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const num = (n) => Number(n).toLocaleString("zh-CN");

  // Read from pilot/indexes and pytest on this date; refresh when the corpus grows.
  const SNAPSHOT = { date: "2026-11-16", evidence: 11731, vectors: 10919, nodes: 4166, edges: 5518, tests: 903 };

  // [monogram, name, role]
  const STACK = [
    { layer: "交互界面", tech: [["JS", "原生 Web 前端", "HTML · CSS · JS，无构建"], ["St", "Streamlit", "备用浏览界面"]] },
    { layer: "服务接口", tech: [["Fa", "FastAPI", "REST 接口"], ["Uv", "Uvicorn", "ASGI 服务"], ["MCP", "MCP", "agent 工具接口"]] },
    { layer: "生成接口", tech: [["CLI", "CLI 订阅", "本机 CLI 账号额度"], ["API", "兼容模型 API", "配置的服务与模型"]] },
    { layer: "证据检索", tech: [["M3", "BGE-M3", "语义向量"], ["BM", "FTS5 / BM25", "关键词检索"], ["RRF", "RRF", "多路结果融合"], ["中英", "术语映射", "中文问英文库"]] },
    { layer: "存储", tech: [["SQ", "SQLite", "证据与知识记录"], ["Qd", "Qdrant", "向量库"], ["Nx", "NetworkX", "关系图 GraphML"]] },
    { layer: "文档解析", tech: [["Mu", "PyMuPDF", "PDF 逐页文本"], ["OCR", "RapidOCR", "扫描页识别"], ["Dl", "Docling", "乱码恢复"], ["Mi", "MinerU", "表格抽取"]] },
    { layer: "校验", tech: [["Pd", "Pydantic", "结构校验"], ["Pt", "Pint", "单位换算"]] },
    { layer: "运行环境", tech: [["Py", "Python 3.12", ""], ["To", "PyTorch", "CPU 推理"], ["DP", "DPAPI", "密钥加密"]] },
  ];

  const FLOW = [
    { id: "collect", title: "采集", line: "公开全文，哈希去重", tech: ["SHA-256"] },
    { id: "parse", title: "解析", line: "逐页文本、OCR、表格", tech: ["PyMuPDF", "RapidOCR"] },
    { id: "extract", title: "抽取", line: "模型输出结构化 JSON", tech: ["大模型", "Pydantic"] },
    { id: "review", title: "复核", line: "独立 agent 逐条核对", tech: ["引文页码校验"] },
    { id: "classify", title: "分类", line: "9 个工艺 × 9 个维度", tech: ["受控词表"] },
    { id: "publish", title: "建库", line: "暂存校验后切换", tech: ["SQLite", "Qdrant", "BGE-M3"] },
  ];

  const RAG = [
    { title: "提问", line: "识别文献和检索词", tech: ["术语映射"] },
    { title: "检索", line: "召回原文段落", tech: ["FTS5", "BGE-M3"] },
    { title: "排序", line: "综合语义与关键词匹配", tech: ["RRF"] },
    { title: "组织证据", line: "关联知识记录与原文", tech: ["SQLite"] },
    { title: "回答", line: "生成带来源编号的答复", tech: ["通用 API / 订阅"] },
    { title: "核对", line: "从引用打开 PDF 原页", tech: ["来源校验"] },
  ];

  const STORES = [
    ["原文证据", SNAPSHOT.evidence, "段", "SQLite FTS5"],
    ["语义向量", SNAPSHOT.vectors, "个", "Qdrant · BGE-M3"],
    ["图谱节点", SNAPSHOT.nodes, "个", `${num(SNAPSHOT.edges)} 条关系 · NetworkX`],
  ];

  const APPS = [
    { id: "search", title: "全文检索", line: "在已发布文献中查原页", route: "#/search", tech: ["BGE-M3"] },
    { id: "ask", title: "论文问答", line: "回答带原文引用", route: "#/ask", tech: ["RAG", "引用校验"] },
    { id: "ideas", title: "创新点生成", line: "从原文提出设想", route: "#/ideas", tech: ["RAG", "大模型"] },
    { id: "library", title: "文献库", line: "多维分层浏览", route: "#/library", tech: ["分类标签"] },
  ];

  const GROWTH = [["11-02", 26, "试点"], ["11-03", 100, "扩充"], ["11-06", 113, "并行采集"], ["11-07", 213, "再增 100 份"], ["11-19", 216, "补充 3 份"]];

  const PRINCIPLES = [["可追溯", "每条知识回到原文与 PDF 页"], ["不合并", "不同样品的数值分开记录"], ["如实标注", "自动复核不称专家审核"], ["可恢复", "先暂存校验，再切换上线"]];

  // The RAG-Anything library: same page structure, its own stack, pipeline and live counts.
  const NATIVE_STACK = [
    { layer: "交互界面", tech: [["JS", "原生 Web 前端", "HTML · CSS · JS，无构建"]] },
    { layer: "服务接口", tech: [["Fa", "FastAPI", "REST 接口"], ["MCP", "MCP", "入库与 agent 工具"]] },
    { layer: "生成接口", tech: [["CLI", "CLI 订阅", "本机 CLI 账号额度"], ["API", "兼容模型 API", "配置的服务与模型"]] },
    { layer: "知识库框架", tech: [["RA", "RAG-Anything", "图文表公式统一入库"], ["LR", "LightRAG", "片段、实体与关系"]] },
    { layer: "文档解析", tech: [["Dl", "Docling", "版面、表格与公式"], ["Cr", "原页裁图", "模型审核后入库"]] },
    { layer: "证据检索", tech: [["M3", "BGE-M3", "本机语义向量"], ["KG", "图谱检索", "实体与关系召回"], ["Pg", "物理页回连", "片段回到 PDF 原页"]] },
  ];
  const NATIVE_FLOW = [
    { id: "crop", title: "裁图审核", line: "图表公式从原页裁取", tech: ["模型审核"] },
    { id: "parse", title: "解析预处理", line: "逐页解析正文与版面", tech: ["Docling"] },
    { id: "index", title: "同批建索引", line: "片段、实体与关系", tech: ["RAG-Anything", "LightRAG"] },
    { id: "check", title: "核验", line: "页码与检索结果复查", tech: ["原页校验"] },
    { id: "publish", title: "同批发布", line: "整批通过后上线", tech: ["断点续跑"] },
  ];
  const NATIVE_RAG = [
    { title: "提问", line: "中文或英文提问", tech: ["术语映射"] },
    { title: "检索", line: "向量与图谱混合召回", tech: ["BGE-M3", "LightRAG"] },
    { title: "回连原页", line: "片段对应 PDF 物理页", tech: ["页码映射"] },
    { title: "组织证据", line: "附上原页与同页表格", tech: ["原页图像"] },
    { title: "回答", line: "生成带来源编号的答复", tech: ["通用 API / 订阅"] },
    { title: "核对", line: "从引用打开 PDF 原页", tech: ["来源校验"] },
  ];

  const badge = (t) => `<span class="rp-badge">${esc(t)}</span>`;

  function mount(root, { summary = {}, native = null } = {}) {
    const docs = native?.documents || [];
    const sum = (key) => docs.reduce((n, d) => n + (Number(d[key]) || 0), 0);
    const kpis = native
      ? [[native.published, "篇已发布"], [sum("pages"), "页 PDF"], [sum("chunks"), "个索引片段"], [native.catalog, "篇入库清单"]]
      : [[summary.papers, "份资料"], [summary.pages, "页 PDF"], [summary.claims, "条知识记录"], [summary.samples, "个样品"]];
    const cli = native && (native.provider || native.model)
      ? [native.provider || "本机 CLI", native.model, native.effort && String(native.effort).replace(/^./, (c) => c.toUpperCase())].filter(Boolean).join(" · ") : "";
    const stack = (native ? NATIVE_STACK : STACK).map((row) => row.layer !== "生成接口" || !cli ? row
      : { ...row, tech: row.tech.map((t) => t[0] === "CLI" ? [t[0], t[1], `当前：${cli}`] : t) });
    const flow = native ? NATIVE_FLOW : FLOW, rag = native ? NATIVE_RAG : RAG;
    const stores = native
      ? [["已发布文献", native.published, "篇", `清单共 ${num(native.catalog)} 篇`], ["PDF 原页", sum("pages"), "页", "回答引用的核对单位"], ["索引片段", sum("chunks"), "个", "文本 · 图表 · 公式"]]
      : STORES;
    const max = Math.max(...GROWTH.map(([, n]) => n));
    root.className = "page report-page";
    root.innerHTML = `
      <header class="rp-hero">
        <div class="rp-hero-copy">
          <p class="rp-kicker">项目概况 · ${native ? "RAG-Anything" : "旧版 Demo"}</p>
          <h1>混合键合资料知识库</h1>
          <p class="rp-lead">${native ? "按 PDF 原页组织文本、图表与公式，回答引用可回到原页核对" : "自制检索架构 · 文献、知识记录与证据检索，作为对照保留"}</p>
        </div>
        <div class="rp-kpis">${kpis.filter(([n]) => n != null).map(([n, unit]) => `<div class="rp-kpi"><b>${num(n)}</b><span>${unit}</span></div>`).join("")}</div>
      </header>

      <section class="rp-section">
        <h2 class="rp-title">技术栈</h2>
        <div class="card rp-stack">${stack.map(({ layer, tech }) => `
          <div class="rp-layer"><div class="rp-layer-name">${esc(layer)}</div>
            <div class="rp-techs">${tech.map(([mono, name, role]) => `<div class="rp-tech"><span class="rp-mono">${esc(mono)}</span><span class="rp-tech-text"><b>${esc(name)}</b>${role ? `<small>${esc(role)}</small>` : ""}</span></div>`).join("")}</div>
          </div>`).join("")}
        </div>
      </section>

      <section class="rp-section">
        <h2 class="rp-title">${native ? "入库流程与已发布索引" : `入库流程与索引快照（${SNAPSHOT.date}）`}</h2>
        <div class="card rp-flow">
          <ol class="rp-steps" style="--steps:${flow.length}">${flow.map((s, i) => `<li class="rp-step" data-step="${s.id}"><span class="rp-step-num">${i + 1}</span><b>${esc(s.title)}</b><small>${esc(s.line)}</small><span class="rp-badges">${s.tech.map(badge).join("")}</span></li>`).join("")}</ol>
          <div class="rp-down" aria-hidden="true"></div>
          <div class="rp-stores">${stores.map(([label, n, unit, tech]) => `<div class="rp-store"><span>${esc(label)}</span><b>${num(n)}<small>${unit}</small></b><em>${esc(tech)}</em></div>`).join("")}</div>
          <div class="rp-down" aria-hidden="true"></div>
          <div class="rp-apps">${APPS.map((a) => `<a class="rp-app" href="${a.route}" data-app="${a.id}"><b>${esc(a.title)}</b><small>${esc(a.line)}</small><span class="rp-badges">${a.tech.map(badge).join("")}</span></a>`).join("")}</div>
        </div>
      </section>

      <section class="rp-section">
        <h2 class="rp-title">RAG 问答链路</h2>
        <div class="card rp-rag">
          <ol class="rp-rag-steps">${rag.map((s, i) => `<li class="rp-rag-step"><span class="rp-rag-num">${String(i + 1).padStart(2, "0")}</span><b>${esc(s.title)}</b><small>${esc(s.line)}</small><span class="rp-badges">${s.tech.map(badge).join("")}</span></li>`).join("")}</ol>
        </div>
      </section>

      <section class="rp-section rp-split">
        <div class="card rp-growth"><h2 class="rp-title">资料规模</h2>
          <div class="rp-bars">${GROWTH.map(([d, n, note]) => `<div class="rp-bar"><b>${n}</b><i style="height:${n / max * 100}%"></i><span>${d}</span><small>${esc(note)}</small></div>`).join("")}</div>
        </div>
        <div class="card rp-principles"><h2 class="rp-title">设计原则</h2>
          <ul>${PRINCIPLES.map(([k, v]) => `<li><b>${esc(k)}</b><span>${esc(v)}</span></li>`).join("")}</ul>
        </div>
      </section>

      <p class="rp-foot">${native ? `已发布数字实时读取自 RAG-Anything 发布清单${native.generation != null ? `（发布版本 ${esc(native.generation)}）` : ""}` : `${num(SNAPSHOT.tests)} 个 Python 测试用例 · 索引数字为 ${SNAPSHOT.date} 快照`} · Codex 负责后端，Claude Code 负责前端</p>`;
    return root;
  }

  window.KGReport = { mount, STACK, FLOW, RAG, APPS };
})();
