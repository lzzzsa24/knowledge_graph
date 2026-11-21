/* Hybrid-bonding pilot: source-linked browsing and evidence-grounded questions. */
(function () {
  "use strict";

  const STAGES = {
    surface_preparation: "表面准备",
    contact_bonding: "接触键合",
    post_bond_annealing: "键合后退火",
    measurement: "测量",
    reliability_test: "可靠性试验",
    material_characterization: "材料表征",
    background: "背景与综述",
  };
  const CLAIM_TYPES = {
    reported_experiment: "作者报告的实验",
    reported_simulation: "作者报告的仿真",
    author_interpretation: "作者解释",
    review_summary: "综述归纳",
    cited_prior_work: "引述前人工作",
    manufacturer_specification: "厂商声明规格",
    recommended_process: "推荐工艺条件",
    standard_requirement: "规范要求",
    test_method: "测试方法",
  };
  const PROCESS = ["surface_preparation", "contact_bonding", "post_bond_annealing", "measurement", "reliability_test"];
  const REVIEW_NOTE = "agent 自动复核 · 试点数据，尚未经领域专家确认";
  function reviewNote(claim) { return claim.review_status === "api_model_reviewed" ? "API 模型复核（非专家审核）" : REVIEW_NOTE; }

  const state = { summary: null, papers: [], claims: [], paperById: {}, claimById: {}, claimsByEvidence: {},
                  taxonomy: null, taxonomyError: "" };
  const ui = { claims: { q: "", stage: null, material: "", doc: "", type: "" },
               library: { l1: "category", l2: "routes", a: "", b: "", q: "" },
               search: { q: "", mode: "auto", limit: 10, results: null, selected: 0, executed: "" },
               ask: { draft: "", turns: [], sortOrder: "latest", nextTurnNumber: 1, status: null, statusError: "", statusLoading: false, statusVersion: 0,
                      busy: false, pendingQuestion: "", error: "", controller: null, requestVersion: 0, render: null,
                      configureBusy: false, configOpen: false, configError: "", availableModels: [],
                      connectionDraft: null, connectionEdited: false } };
  const chatSessions = new Map();
  const nativeSearch = { ...ui.search, mode: "native" };
  const archNames = { demo: "简易自制架构 Demo", raganything: "RAG-Anything" };
  let arch = "demo", channel = "subscription";
  try {
    if (localStorage.getItem("kg-arch") === "raganything") arch = "raganything";
    if (localStorage.getItem("kg-channel") === "api") channel = "api";
  } catch (_) {}
  function chatSession() {
    const key = arch + "/" + channel;
    if (!chatSessions.has(key)) chatSessions.set(key, { ...ui.ask, turns: [], availableModels: [] });
    return chatSessions.get(key);
  }
  function modeHref(path, params = new URLSearchParams(), architecture = arch, via = channel) {
    const query = new URLSearchParams(params);
    query.delete("source");
    query.set("arch", architecture);
    query.set("channel", via);
    return "#/" + path + "?" + query;
  }
  function switchMode(architecture, via) {
    const [path, query] = location.hash.slice(2).split("?");
    const params = architecture === arch ? new URLSearchParams(query) : new URLSearchParams();
    location.hash = modeHref(path || "ask", params, architecture, via);
  }
  function channelPicker() {
    const box = h(`<div class="channel-picker"><span>生成接口</span>
      <div class="segmented channel-options" role="group" aria-label="选择生成接口">
        <button type="button" data-via="api">通用 API</button><button type="button" data-via="subscription">Codex 订阅</button></div>
      <select class="select" aria-label="生成接口"><option value="api">通用 API</option><option value="subscription">Codex 订阅</option></select></div>`);
    box.querySelectorAll("[data-via]").forEach(button => {
      button.classList.toggle("on", button.dataset.via === channel);
      button.setAttribute("aria-pressed", String(button.dataset.via === channel));
      button.onclick = () => { if (button.dataset.via !== channel) switchMode(arch, button.dataset.via); };
    });
    const select = $("select", box);
    select.value = channel;
    select.onchange = () => switchMode(arch, select.value);
    return box;
  }
  function paperFor(id) {
    return arch === "raganything" ? state.nativePapers?.get(id) : state.paperById[id];
  }

  const $ = (sel, root = document) => root.querySelector(sel);
  const view = $("#view");

  /* ---------- helpers ---------- */
  function esc(value) {
    return String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }
  function h(html) { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstElementChild; }
  function stageColor(stage) { return `var(--st-${stage in STAGES ? stage : "background"})`; }
  function cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function stageChip(stage) { return `<span class="chip"><span class="dot" style="background:${stageColor(stage)}"></span>${esc(STAGES[stage] || stage)}</span>`; }
  function icon(name) {
    const paths = {
      warn: '<path d="M12 4 2.5 20h19z"/><path d="M12 10v4.5M12 17.3v.2"/>',
      ext: '<path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/>',
      pdf: '<path d="M6 3h8l4 4v14H6z"/><path d="M14 3v4h4"/>',
      graph: '<circle cx="5" cy="6" r="2"/><circle cx="19" cy="6" r="2"/><circle cx="12" cy="18" r="2"/><path d="M6.7 7 10.6 16.3M17.3 7 13.4 16.3"/>',
      prev: '<path d="m15 6-6 6 6 6"/>', next: '<path d="m9 6 6 6-6 6"/>',
      plus: '<path d="M12 5v14M5 12h14"/>', minus: '<path d="M5 12h14"/>', fit: '<path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/>',
      search: '<circle cx="10.5" cy="10.5" r="6"/><path d="m15 15 5 5"/>', doc: '<path d="M5 4h14v16H5z"/><path d="M8 8h8M8 12h8M8 16h5"/>',
      ask: '<path d="M20 14a3 3 0 0 1-3 3H9l-5 4V6a3 3 0 0 1 3-3h10a3 3 0 0 1 3 3z"/><path d="M8 8h8M8 12h5"/>',
      copy: '<rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/>',
      check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>', arrow: '<path d="M5 12h14M13 6l6 6-6 6"/>',
      sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M4.6 4.6 6 6M18 18l1.4 1.4M2.5 12h2M19.5 12h2M4.6 19.4 6 18M18 6l1.4-1.4"/>',
      moon: '<path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/>',
      auto: '<rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/>',
      clear: '<path d="M6 6l12 12M18 6 6 18"/>', home: '<path d="M4 11 12 4l8 7v9H4z"/><path d="M10 20v-5h4v5"/>',
      book: '<path d="M4 5.5C6 4.5 9 4.5 12 6c3-1.5 6-1.5 8-.5V19c-2-1-5-1-8 .5-3-1.5-6-1.5-8-.5z"/><path d="M12 6v13.5"/>',
      flask: '<path d="M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.8 3h10.4A2 2 0 0 0 19 18l-5-9V3"/><path d="M7.5 14h9"/>',
      inbox: '<path d="M3 13h5l1.5 3h5L16 13h5"/><path d="M5 5h14l2 8v6H3v-6z"/>',
    };
    return `<svg viewBox="0 0 24 24">${paths[name] || ""}</svg>`;
  }
  const reducedMotion = () => matchMedia("(prefers-reduced-motion: reduce)").matches;
  function toast(message, iconName = "check") {
    const el = h(`<div class="toast">${icon(iconName)}<span></span></div>`);
    $("span", el).textContent = message;
    $("#toasts").append(el);
    setTimeout(() => { el.classList.add("out"); setTimeout(() => el.remove(), 320); }, 1800);
  }
  async function copyText(text, label = "已复制") {
    try { await navigator.clipboard.writeText(text); toast(label); }
    catch (_) { toast("复制失败，请手动选择文本", "warn"); }
  }
  // Give children an index so CSS can stagger their entrance.
  function stagger(container) {
    [...container.children].forEach((child, i) => child.style.setProperty("--i", i));
    container.classList.add("stagger");
  }
  function emptyState(text, iconName = "inbox") { return `<div class="card empty fade-in">${icon(iconName)}${esc(text)}</div>`; }
  function searchInput(attrs, placeholder, hint = "/") {
    return `<label class="input-icon grow">${icon("search")}<input class="input" type="search" ${attrs} placeholder="${esc(placeholder)}" data-page-search>${hint ? `<kbd>${hint}</kbd>` : ""}</label>`;
  }
  function highlight(text, query) {
    const terms = [...new Set((query || "").toLowerCase().match(/[\p{L}\p{N}]{2,}/gu) || [])].filter((t) => /^[\x00-\x7f]+$/.test(t));
    let out = esc(text);
    if (!terms.length) return out;
    const re = new RegExp(`(${terms.map((t) => t.replace(/[.*+?^${}()|[\]\\]/g, "\\$&")).join("|")})`, "gi");
    return out.replace(re, "<mark>$1</mark>");
  }
  function doiUrl(doi) { return doi ? "https://doi.org/" + String(doi).replace(/^https?:\/\/doi\.org\//, "") : null; }
  async function api(path, options) {
    const res = await fetch(path, options);
    if (!res.ok) {
      let detail = res.statusText;
      try { detail = (await res.json()).detail || detail; } catch (_) { /* non-JSON error */ }
      const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      error.status = res.status;
      throw error;
    }
    return res.json();
  }
  const nativeStatusRequests = {};
  function readNativeStatus(progress = false) {
    const url = "/api/native/status" + (progress ? "?progress=true" : "");
    nativeStatusRequests[url] ??= api(url).then((data) => {
      state.native = data;
      state.nativePapers = new Map((data.documents || []).map(doc => [doc.doc_id, {
        ...doc, page_count: doc.pages, has_pdf: true
      }]));
      renderSidebarSummary();
      return data;
    }).catch((error) => {
      state.native = null;
      state.nativePapers = new Map();
      renderSidebarSummary();
      throw error;
    }).finally(() => { delete nativeStatusRequests[url]; });
    return nativeStatusRequests[url];
  }
  // Catalog entries store authors either as a list or as a preformatted string.
  function authorsText(paper) { const a = paper.authors; return Array.isArray(a) ? a.join(", ") : String(a || ""); }
  function shortTitle(title, n = 34) { return title.length > n ? title.slice(0, n - 1) + "…" : title; }
  function categoryLabel(value) {
    if (value && !state.taxonomy) return "分类标签暂不可用";
    return state.taxonomy?.categories.find((category) => category.id === value)?.label || "待分类";
  }
  function facetLabel(id, value) {
    if (!state.taxonomy) return "标签暂不可用";
    return state.taxonomy?.facets.find((facet) => facet.id === id)?.options.find((option) => option.value === value)?.label || String(value || "未标注");
  }
  function classificationValues(classification, id) {
    const value = classification?.[id];
    const values = (Array.isArray(value) ? value : value ? [value] : []).map(String).filter(Boolean);
    return values.length ? values : ["__unspecified__"];
  }
  function classificationDetail(paper) {
    const c = paper.classification;
    if (!c) return '<section class="classification-detail"><h4>工艺与标签</h4><p class="muted">此文献尚未分类；仍可查看原始 PDF 与知识记录。</p></section>';
    const evidence = (Array.isArray(c.evidence) ? c.evidence : []).filter((entry) => entry && typeof entry === "object");
    const facets = (state.taxonomy?.facets || []).filter((facet) => classificationValues(c, facet.id).length);
    const keywords = classificationValues(c, "keywords").filter((value) => value !== "__unspecified__");
    return `<section class="classification-detail">
      <h4>工艺与标签</h4>
      <dl class="kv"><dt>主分类</dt><dd><span class="chip classification-primary">${esc(categoryLabel(c.primary_category))}</span></dd>
        ${c.related_stages?.length ? `<dt>关联阶段</dt><dd class="chips">${c.related_stages.map((id) => `<span class="chip">${esc(categoryLabel(id))}</span>`).join("")}</dd>` : ""}
        ${facets.map((facet) => `<dt>${esc(facet.label)}</dt><dd class="chips">${classificationValues(c, facet.id).map((value) => `<span class="chip">${esc(facetLabel(facet.id, value))}</span>`).join("")}</dd>`).join("")}
      </dl>
      <details class="classification-reason"><summary>分类理由与原文证据${evidence.length ? `（${evidence.length} 处）` : ""}</summary>
        <p>${esc(c.classification_reason || "尚未记录分类理由。")}</p>
        ${keywords.length ? `<div class="classification-keywords"><p class="count">文内关键词</p><div class="chips">${keywords.map((keyword) => `<span class="chip">${esc(keyword)}</span>`).join("")}</div></div>` : ""}
        ${evidence.map((entry) => {
          const page = Number(entry.pdf_page);
          const validPage = Number.isInteger(page) && page > 0 && (!paper.page_count || page <= paper.page_count);
          return `<div class="classification-evidence"><div class="quote-head"><span>${validPage ? `PDF 第 ${page} 页` : "原页尚未定位"}</span>${validPage && paper.has_pdf ? `<button class="btn sm" type="button" data-classification-page="${page}">查看原页</button>` : ""}</div><blockquote class="quote">${esc(entry.quote || "未附摘录，请查看原页。")}</blockquote></div>`;
        }).join("") || '<p class="muted">暂未附原页证据。</p>'}
      </details>
    </section>`;
  }

  /* ---------- page viewer ---------- */
  function pageViewer(docId, page, { pages = null, pageCount = null, pdfHash = "" } = {}) {
    const paper = paperFor(docId) || {};
    pdfHash ||= paper.pdf_sha256 || "";
    const total = pageCount || paper.page_count || 1;
    const el = h(`<div class="viewer">
      <div class="viewer-bar">
        <button class="btn" data-a="prev" aria-label="上一页">${icon("prev")}</button>
        <span>PDF 第</span><input class="input" type="number" min="1" max="${total}" value="${page}"><span>/ ${total} 页</span>
        <button class="btn" data-a="next" aria-label="下一页">${icon("next")}</button>
        <span class="grow">实际物理页（含封面），区别于印刷页码</span>
        ${paper.has_pdf ? `<a class="btn" target="_blank" rel="noopener" data-a="open">${icon("pdf")}原 PDF</a>` : ""}
      </div>
      <div class="viewer-img"></div>
    </div>`);
    const input = $("input", el), box = $(".viewer-img", el);
    let current = page, generation = 0, retryTimer;
    const show = (n) => {
      clearTimeout(retryTimer);
      const request = ++generation;
      n = Math.min(total, Math.max(1, n | 0));
      current = n; input.value = n;
      $("[data-a=prev]", el).disabled = n <= 1;
      $("[data-a=next]", el).disabled = n >= total;
      const open = $("[data-a=open]", el);
      const revision = pdfHash ? `?revision=${encodeURIComponent(pdfHash)}` : "";
      if (open) open.href = `/api/papers/${encodeURIComponent(docId)}/pdf${revision}#page=${n}`;
      if (!paper.has_pdf) { box.innerHTML = `<div class="empty">原始 PDF 当前不可读取。</div>`; return; }
      box.innerHTML = `<div class="skeleton page-skel"></div>`;
      const url = `/api/papers/${encodeURIComponent(docId)}/pages/${n}.png${revision}`;
      function load(attempt) {
        const img = new Image();img.alt = `${docId} PDF 第 ${n} 页`;
        img.onload = () => { if (request === generation) box.replaceChildren(img); };
        img.onerror = () => {
          if (request !== generation || !el.isConnected) return;
          if (attempt < 2) { retryTimer=setTimeout(()=>{if (request===generation && el.isConnected) load(attempt+1);},200*(attempt+1)); return; }
          const notice=h('<div class="empty">原页暂时未载入。 <button type="button" class="btn sm">重新读取</button></div>');
          notice.querySelector("button").onclick=()=>show(n);
          box.replaceChildren(notice);
        };
        img.onclick=()=>window.open(url,"_blank","noopener");
        img.src=attempt ? url+(revision ? "&" : "?")+"retry="+attempt : url;
      }
      load(0);
    };
    $("[data-a=prev]", el).onclick = () => show(current - 1);
    $("[data-a=next]", el).onclick = () => show(current + 1);
    input.onchange = () => show(+input.value);
    el.show = show;
    show(page);
    return el;
  }

  /* ---------- drawer ---------- */
  const drawer = $("#drawer"), backdrop = $("#drawer-backdrop");
  let drawerNav = null;
  function openDrawer(kicker, content, nav = null) {
    $("#drawer-kicker").textContent = kicker;
    const body = $("#drawer-body");
    body.firstElementChild?.dispose?.();
    body.replaceChildren(content);
    body.scrollTop = 0;
    drawerNav = nav;
    const navBox = $("#drawer-nav");
    navBox.replaceChildren();
    if (nav && nav.ids.length > 1) {
      const i = nav.ids.indexOf(nav.current);
      const prev = h(`<button class="icon-btn" aria-label="上一条（←）" title="上一条（←）">${icon("prev")}</button>`);
      const next = h(`<button class="icon-btn" aria-label="下一条（→）" title="下一条（→）">${icon("next")}</button>`);
      prev.disabled = i <= 0; next.disabled = i >= nav.ids.length - 1;
      prev.onclick = () => stepDrawer(-1); next.onclick = () => stepDrawer(1);
      navBox.append(prev, h(`<span>${i + 1} / ${nav.ids.length}</span>`), next);
    }
    drawer.classList.add("open");
    drawer.setAttribute("aria-hidden", "false");
    backdrop.classList.add("show");
  }
  function stepDrawer(delta) {
    if (!drawerNav || !drawer.classList.contains("open")) return;
    const i = drawerNav.ids.indexOf(drawerNav.current) + delta;
    if (i >= 0 && i < drawerNav.ids.length) drawerNav.open(drawerNav.ids[i]);
  }
  function closeDrawer() {
    $("#drawer-body").firstElementChild?.dispose?.();
    drawer.classList.remove("open");
    drawer.setAttribute("aria-hidden", "true");
    backdrop.classList.remove("show");
    drawerNav = null;
  }
  $("#drawer-close").onclick = closeDrawer;
  backdrop.onclick = closeDrawer;

  function claimDetail(claim) {
    const paper = state.paperById[claim.doc_id] || { title: claim.doc_id };
    const conditions = Object.entries(claim.conditions || {});
    const pages = [...new Set((claim.evidence || []).map((e) => e.pdf_page))].sort((a, b) => a - b);
    const el = h(`<div class="detail">
      <h2>${esc(claim.statement)}</h2>
      <div class="chips">${stageChip(claim.stage)}<span class="chip">${esc(CLAIM_TYPES[claim.claim_type] || claim.claim_type)}</span><span class="chip review">${esc(reviewNote(claim))}</span></div>
      <h4>结构化字段</h4>
      <dl class="kv">
        <dt>材料体系</dt><dd>${esc(claim.material_system)}</dd>
        <dt>样品 / 适用对象</dt><dd><code>${esc(claim.sample_id)}</code></dd>
        <dt>物理量</dt><dd><code>${esc(claim.quantity)}</code></dd>
        <dt>数值原文</dt><dd><code>${esc(claim.value_text || "—")}</code></dd>
        <dt>单位</dt><dd>${esc(claim.unit || "未单独给出")}</dd>
      </dl>
      <h4>条件与适用范围</h4>
      ${conditions.length ? `<dl class="kv">${conditions.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(typeof v === "object" ? JSON.stringify(v) : v)}</dd>`).join("")}</dl>` : `<p class="muted">未报告条件。</p>`}
      ${claim.notes ? `<h4>复核备注</h4><div class="note">${esc(claim.notes)}</div>` : ""}
      <h4>原文证据（${(claim.evidence || []).length}）</h4>
      <div class="quotes">${(claim.evidence || []).map((e) => `
        <div><div class="quote-head"><span>PDF 第 ${e.pdf_page} 页${e.parser ? " · OCR 文字" : ""} · <code>${esc(e.evidence_id || "")}</code></span>
        <span class="links"><button class="btn sm ghost" data-copy="${esc(e.quote)}">${icon("copy")}复制</button><button class="btn sm" data-page="${e.pdf_page}">查看原页</button></span></div>
        <blockquote class="quote">${esc(e.quote)}</blockquote></div>`).join("")}</div>
      <h4>来源文献</h4>
      <a class="paper-ref" data-paper="${esc(claim.doc_id)}">${esc(paper.title)}<small>${esc(claim.doc_id)}${paper.year ? " · " + paper.year : ""}</small></a>
      ${paper.doi ? `<div class="links" style="margin-top:10px"><a class="btn" target="_blank" rel="noopener" href="${esc(doiUrl(paper.doi))}">${icon("ext")}DOI</a></div>` : ""}
      <h4>证据原页</h4>
    </div>`);
    if (pages.length) {
      const viewer = pageViewer(claim.doc_id, pages[0]);
      el.append(viewer);
      el.querySelectorAll("[data-page]").forEach((b) => (b.onclick = () => { viewer.show(+b.dataset.page); viewer.scrollIntoView({ behavior: "smooth", block: "start" }); }));
    }
    el.querySelectorAll("[data-copy]").forEach((b) => (b.onclick = () => copyText(b.dataset.copy, "已复制原文引文")));
    $("[data-paper]", el).onclick = () => openPaper(claim.doc_id);
    return el;
  }
  // `ids` is the list the claim was opened from, so ←/→ can walk through it.
  function openClaim(id, ids = null) {
    const c = state.claimById[id];
    if (!c) return;
    const list = ids && ids.includes(id) ? ids : null;
    openDrawer(`知识记录 · ${id}`, claimDetail(c), list && { ids: list, current: id, open: (next) => openClaim(next, list) });
  }

  function paperReader(paper, { mode = paper.has_markdown ? "markdown" : "pdf", page = 1 } = {}) {
    const docId = encodeURIComponent(paper.doc_id);
    const el = h(`<section class="paper-reader" aria-label="文献全文阅读">
      <div class="paper-reader-head"><h3>全文阅读</h3><div class="links">
        ${paper.has_markdown ? `<button class="btn" type="button" data-reader-mode="markdown">${icon("book")}Markdown 阅读</button>` : ""}
        <button class="btn" type="button" data-reader-mode="pdf">${icon("pdf")}PDF 原页</button>
      </div></div>
      ${paper.has_markdown ? "" : '<p class="count reader-note">此文献尚未生成 Markdown 阅读版，可先查看原始 PDF。</p>'}
      ${paper.has_markdown ? `<div class="links reader-downloads"><a class="btn sm" href="/api/papers/${docId}/markdown" download>下载 Markdown</a><a class="btn sm" href="#/papers?doc=${docId}&read=markdown" data-reader-share>阅读直达链接</a></div>` : ""}
      <div class="markdown-content" aria-live="polite" hidden></div>
      <div data-reader-pdf></div>
    </section>`);
    const markdown = $(".markdown-content", el);
    const pdf = $("[data-reader-pdf]", el);
    const viewer = pageViewer(paper.doc_id, page);
    pdf.append(viewer);
    let loaded = false, loading = false;
    const loadMarkdown = async () => {
      if (loaded || loading) return;
      loading = true;
      markdown.textContent = "正在读取 Markdown…";
      try {
        const data = await api(`/api/papers/${docId}/markdown/rendered`);
        // This endpoint renders the local file with HTML disabled, images
        // suppressed and a strict link policy. Never insert raw Markdown here.
        if (typeof data.html !== "string") throw new Error("Markdown 阅读版返回格式有误。");
        markdown.innerHTML = data.html;
        markdown.querySelectorAll("a[data-pdf-page]").forEach((link) => {
          link.onclick = (event) => {
            if (event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
            event.preventDefault();
            select("pdf", Number(link.dataset.pdfPage));
            el.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
          };
        });
        loaded = true;
      } catch (error) {
        markdown.replaceChildren();
        const message = document.createElement("p"); message.className = "notice"; message.textContent = error.message;
        const retry = h('<button class="btn sm" type="button">重新读取 Markdown</button>');
        retry.onclick = loadMarkdown;
        markdown.append(message, retry);
      } finally { loading = false; }
    };
    const select = (next, selectedPage = null) => {
      const useMarkdown = next === "markdown" && paper.has_markdown;
      markdown.hidden = !useMarkdown;
      pdf.hidden = useMarkdown;
      el.querySelectorAll("[data-reader-mode]").forEach((button) => {
        const active = button.dataset.readerMode === (useMarkdown ? "markdown" : "pdf");
        button.classList.toggle("primary", active);
        button.setAttribute("aria-pressed", String(active));
      });
      if (useMarkdown) loadMarkdown();
      else if (selectedPage !== null) viewer.show(selectedPage);
    };
    el.querySelectorAll("[data-reader-mode]").forEach((button) => (button.onclick = () => select(button.dataset.readerMode)));
    el.showPdf = (selectedPage) => select("pdf", selectedPage);
    el.showMarkdown = () => select("markdown");
    select(mode, page);
    return el;
  }

  function paperDetail(paper, readerOptions = {}) {
    const claims = state.claims.filter((c) => c.doc_id === paper.doc_id);
    const source = paper.pdf_url || paper.publisher_url;
    const el = h(`<div class="detail">
      <h2>${esc(paper.title)}</h2>
      <div class="chips"><span class="chip">${esc(paper.classification?.document_form ? facetLabel("document_form", paper.classification.document_form) : paper.is_review ? "综述" : "原始研究")}</span>${paper.year ? `<span class="chip">${paper.year}</span>` : ""}<span class="chip">${paper.page_count} 页</span><span class="chip">${claims.length} 条知识记录</span></div>
      <h4>元数据</h4>
      <dl class="kv">
        ${authorsText(paper) ? `<dt>作者</dt><dd>${esc(authorsText(paper))}</dd>` : ""}
        ${paper.publisher ? `<dt>发布机构</dt><dd>${esc(paper.publisher)}</dd>` : ""}
        ${paper.document_number ? `<dt>文件编号</dt><dd>${esc(paper.document_number)}</dd>` : ""}
        ${paper.version ? `<dt>版本</dt><dd>${esc(paper.version)}</dd>` : ""}
        ${paper.status ? `<dt>文件状态</dt><dd>${esc(paper.status)}</dd>` : ""}
        ${paper.topic ? `<dt>主题</dt><dd>${esc(paper.topic)}</dd>` : ""}
        ${paper.source_type && !paper.classification ? `<dt>来源类型</dt><dd><code>${esc(paper.source_type)}</code></dd>` : ""}
        ${paper.doi ? `<dt>DOI</dt><dd><code>${esc(paper.doi)}</code></dd>` : ""}
        ${paper.relevance_reason ? `<dt>纳入理由</dt><dd>${esc(paper.relevance_reason)}</dd>` : ""}
      </dl>
      <div class="links" style="margin-top:14px">
        ${paper.has_markdown ? `<a class="btn primary" href="#/papers?doc=${encodeURIComponent(paper.doc_id)}&read=markdown" data-read-markdown>${icon("book")}阅读 Markdown</a>` : ""}
        ${paper.doi ? `<a class="btn" target="_blank" rel="noopener" href="${esc(doiUrl(paper.doi))}">${icon("ext")}DOI / 出版页</a>` : ""}
        ${source && /^https?:/.test(source) ? `<a class="btn" target="_blank" rel="noopener" href="${esc(source)}">${icon("ext")}公开全文来源</a>` : ""}
        ${paper.has_pdf ? `<a class="btn" target="_blank" rel="noopener" href="/api/papers/${encodeURIComponent(paper.doc_id)}/pdf">${icon("pdf")}本地原始 PDF</a>` : `<span class="notice">未找到此文献的本地原始 PDF。</span>`}
      </div>
      ${classificationDetail(paper)}
      <h4>本文知识记录</h4>
      ${claims.length ? `<ul class="side-list">${claims.map((c) => `<li data-claim="${esc(c.claim_id)}"><span class="dot" style="background:${stageColor(c.stage)}"></span>${esc(c.statement)}</li>`).join("")}</ul>` : `<p class="muted">本文尚无抽取的知识记录。</p>`}
      <div data-paper-reader></div>
    </div>`);
    const reader = paperReader(paper, readerOptions);
    $("[data-paper-reader]", el).replaceWith(reader);
    $("[data-read-markdown]", el)?.addEventListener("click", (event) => {
      event.preventDefault();
      reader.showMarkdown();
      reader.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
    });
    el.querySelectorAll("[data-classification-page]").forEach((button) => (button.onclick = () => {
      reader.showPdf(Number(button.dataset.classificationPage));
      reader.scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
    }));
    const ids = claims.map((c) => c.claim_id);
    el.querySelectorAll("[data-claim]").forEach((li) => (li.onclick = () => openClaim(li.dataset.claim, ids)));
    return el;
  }
  function openPaper(id, ids = null, readerOptions = {}) {
    const p = paperFor(id);
    if (!p) return;
    if (arch === "raganything") {
      const detail = h(`<div class="detail"><h2>${esc(p.title)}</h2><p class="count">${p.pages} 页 · ${p.chunks} 个索引片段</p></div>`);
      detail.append(pageViewer(id, readerOptions.page || 1));
      openDrawer("RAG-Anything · 原始文献", detail);
      return;
    }
    const list = ids && ids.includes(id) ? ids : null;
    openDrawer(`文献 · ${id}`, paperDetail(p, readerOptions), list && { ids: list, current: id, open: (next) => openPaper(next, list) });
    if (readerOptions.mode) $(".paper-reader", drawer).scrollIntoView({ block: "start" });
  }

  /* ---------- live paper questions ---------- */
  const CHAT_HISTORY_LIMIT = 6;
  const CHAT_EXAMPLES = [
    "铜焊盘下凹与退火条件有什么关系？请区分仿真与实验结果。",
    "低温 Cu/SiO2 混合键合有哪些工艺条件？请保留样品和工艺阶段。",
    "PI–PI 与 Cu/PI 混合键合的后退火时长是否相同？",
    "现有论文能保证混合键合连续服役十年吗？还有哪些证据缺口？",
  ];

  function openChatSource(source, context = "回答") {
    const paper = paperFor(source.doc_id);
    const page = Number(source.pdf_page);
    const linked = (Array.isArray(source.claim_ids) ? source.claim_ids : [])
      .map((id) => state.claimById[id]).filter(Boolean);
    const el = h(`<div class="detail">
      <div class="chips"><span class="chip mono">[${esc(source.id)}]</span><span class="chip">PDF 第 ${Number.isInteger(page) && page > 0 ? page : "未标明"} 页</span></div>
      <h2 class="chat-source-title"></h2>
      <h4>原文摘录</h4><blockquote class="quote chat-source-quote"></blockquote>
      <p class="muted chat-evidence-note">引用编号对应本轮${esc(context)}，下方可查看完整原页。</p>
      <div class="chat-source-links links"></div>
      ${linked.length ? `<h4>关联知识记录</h4><ul class="side-list">${linked.map((c) => `<li><button type="button" class="chat-claim-link" data-claim="${esc(c.claim_id)}">${esc(c.claim_id)} · ${esc(c.statement)}</button></li>`).join("")}</ul>` : ""}
      <h4>PDF 实际原页</h4>
    </div>`);
    $(".chat-source-title", el).textContent = source.title || paper?.title || source.doc_id || "来源文献";
    $(".chat-source-quote", el).textContent = String(source.verified_quote || source.text || "此来源未返回引文，请查看原页。");
    if (paper) {
      const button = h(`<button class="btn" type="button">${icon("doc")}文献详情</button>`);
      button.onclick = () => openPaper(paper.doc_id);
      $(".chat-source-links", el).append(button);
    }
    el.querySelectorAll("[data-claim]").forEach((button) => (button.onclick = () => openClaim(button.dataset.claim)));
    if (paper && Number.isInteger(page) && page > 0 && page <= paper.page_count) {
      el.append(pageViewer(paper.doc_id, page, { pdfHash: source.pdf_sha256 }));
    } else {
      el.append(h('<div class="notice">此来源的文献或实际页码无法定位，不能视为已完成原页追溯。</div>'));
    }
    openDrawer(`${context}来源 · ${source.id}`, el);
  }

  function appendCitedText(container, answer, sources, onSource = openChatSource) {
    // Model output is always plain text. Only known [S1]-style references become
    // buttons; no model-supplied HTML, Markdown links or event handlers execute.
    const byId = new Map(sources.map((source) => [source.id, source]));
    const text = String(answer || "");
    const pattern = /\[(S\d+)\]/g;
    let cursor = 0;
    for (const match of text.matchAll(pattern)) {
      container.append(document.createTextNode(text.slice(cursor, match.index)));
      const source = byId.get(match[1]);
      if (source) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "chat-citation";
        button.textContent = source.verified_quote ? `[${source.id} · PDF 第 ${source.pdf_page} 页]` : match[0];
        button.setAttribute("aria-label", `查看来源 ${source.id}：${source.title || source.doc_id}`);
        button.onclick = () => onSource(source);
        container.append(button);
      } else {
        container.append(document.createTextNode(match[0]));
      }
      cursor = match.index + match[0].length;
    }
    container.append(document.createTextNode(text.slice(cursor)));
  }

  function chatTurn(turn, number) {
    const answerLabels = { partial: "部分问题尚不能确定", insufficient: "资料不足" };
    const el = h(`<article class="card chat-turn" data-chat-turn="${number}">
      <div class="chat-question-head"><span class="chat-role">提问 ${number}</span></div>
      <div class="chat-question"></div>
      <div class="chat-answer-head"><span class="chat-role">回答</span>${answerLabels[turn.answerability] ? `<span class="chip chat-answerability ${esc(turn.answerability)}">${esc(answerLabels[turn.answerability])}</span>` : ""}</div>
      <div class="chat-answer"></div><div class="chat-warnings"></div>
      <div class="chat-sources"></div><p class="chat-model count"></p>
    </article>`);
    $(".chat-question", el).textContent = turn.question;
    appendCitedText($(".chat-answer", el), turn.answer, turn.sources);
    const warnings = [...turn.warnings];
    const sourceIds = new Set(turn.sources.map((source) => source.id));
    if ([...turn.answer.matchAll(/\[(S\d+)\]/g)].some((match) => !sourceIds.has(match[1]))) {
      warnings.push("回答包含未关联到返回证据的来源编号，请勿把该编号视为可追溯引用。");
    }
    for (const text of warnings) {
      const note = h('<div class="notice"></div>');
      note.textContent = String(text);
      $(".chat-warnings", el).append(note);
    }
    const sources = $(".chat-sources", el);
    if (turn.sources.length) {
      const label = document.createElement("p");
      label.className = "count";
      label.textContent = `参考来源 · ${turn.sources.length} 处（点击查看原文）`;
      sources.append(label);
      for (const source of turn.sources) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "chat-source-button";
        button.textContent = `[${source.id}] ${source.title || source.doc_id} · PDF 第 ${source.pdf_page} 页`;
        button.onclick = () => openChatSource(source);
        sources.append(button);
      }
    } else {
      sources.textContent = "暂无可查看的原文来源。";
      sources.classList.add("count");
    }
    const retrieval = turn.retrieval;
    const usage = retrieval?.usage;
    const tokens = usage?.reported_tokens ? ` · ${usage.reported_tokens.toLocaleString()} tokens${usage.unreported_calls ? "（部分调用）" : ""}` : "";
    const detail = retrieval?.mode === "native" ? ` · RAG-Anything · 核对 ${retrieval.pages} 个原页` :
      retrieval?.mode === "framework" ? ` · 全库＋框架 · ${retrieval.pages} 个原页 · ${retrieval.images} 张页图${tokens}` : "";
    $(".chat-model", el).textContent = `模型：${turn.provider || "未报告提供方"} / ${turn.model || "未报告模型"}${detail}`;
    return el;
  }

  function askView(params = new URLSearchParams()) {
    const native = arch === "raganything", via = channel, subscription = via === "subscription";
    const s = chatSession();
    const el = h(`<div class="page ask-page">
      <div class="page-head"><div><h1>论文问答</h1><p>${native ? `RAG-Anything · ${state.native?.published ?? 0} 篇已发布文献 · PDF 原页引用` : `简易自制架构 Demo · ${state.summary.papers} 篇文献 · 原文与知识记录`}</p></div>
        <button class="btn" type="button" data-chat-clear>清空会话</button></div>
      <section class="card chat-status" aria-live="polite"><div class="chat-status-copy"></div><div class="links"><button class="btn" type="button" data-chat-settings>模型设置</button><button class="btn" type="button" data-chat-refresh>重新检查连接</button></div></section>
      <section class="card chat-config" hidden>
        <h2>连接模型 API</h2>
        <p class="chat-config-note">支持 OpenAI-compatible 服务和并行智算云。问答与创新点生成共用此连接。密钥在本机加密保存，仅发送到所选服务；保存成功后清空密码框。</p>
        <form class="chat-key-form" autocomplete="off">
          <div class="chat-connection-grid">
            <div><label for="chat-provider">API 服务</label><select class="select" id="chat-provider" name="provider"><option value="paratera">并行智算云 · Paratera</option><option value="openai_compatible">OpenAI-compatible · 自定义服务</option></select></div>
            <div><label for="chat-base-url">API base URL</label><input class="input" id="chat-base-url" name="base_url" type="url" autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="https://api.example.com/v1" aria-describedby="chat-connection-hint" required></div>
            <div class="chat-model-picker"><label for="chat-model-select">模型名称</label><input class="input" id="chat-model-select" name="model" type="text" list="chat-model-options" autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="输入完整模型名称，或选择建议"><datalist id="chat-model-options"></datalist></div>
          </div>
          <p class="chat-connection-hint" id="chat-connection-hint"></p>
          <label for="chat-api-key">API key</label>
          <div class="chat-key-row"><input class="input" id="chat-api-key" name="api_key" type="password" autocomplete="new-password" spellcheck="false" autocapitalize="off" placeholder="粘贴 API key"><button class="btn primary" type="submit">连接并保存</button></div>
        </form>
        <div class="chat-config-error" role="alert"></div>
      </section>
      <form class="card chat-composer">
        <label for="chat-question-input">你想了解什么？</label>
        <textarea id="chat-question-input" name="question" maxlength="3000" rows="4" placeholder="例如：铜焊盘下凹怎样影响退火条件？请保留材料、样品与阶段信息。" required></textarea>
        <div class="chat-compose-footer"><span class="count">Enter 换行 · Ctrl / ⌘ + Enter 发送</span><button class="btn primary" type="submit">${icon("ask")}提问</button></div>
      </form>
      <div class="chat-examples" aria-label="示例问题"></div>
      <p class="count chat-session-note">保留本页会话内最近 ${CHAT_HISTORY_LIMIT} 轮问答，刷新后清空；后续问题会携带最近对话。点击回答中的 [S1] 可查看原文与 PDF 实际页。</p>
      <div class="chat-history-toolbar"><span class="count">问答顺序</span><div class="segmented" role="group" aria-label="问答顺序"><button type="button" data-chat-sort="latest">最新在前</button><button type="button" data-chat-sort="earliest">最早在前</button></div></div>
      <div class="chat-error" role="alert"></div>
      <div class="chat-pending" role="status" aria-live="polite"></div>
      <section class="chat-history" aria-label="最近问答"></section>
    </div>`);
    const form = $(".chat-composer", el), input = $("textarea", el), send = $("button[type=submit]", form);
    $(".chat-status", el).prepend(channelPicker());
    const statusBox = $(".chat-status-copy", el), refresh = $("[data-chat-refresh]", el);
    const clear = $("[data-chat-clear]", el), history = $(".chat-history", el);
    const config = $(".chat-config", el), settings = $("[data-chat-settings]", el);
    const keyForm = $(".chat-key-form", el), keyInput = $("#chat-api-key", el), connect = $("button[type=submit]", keyForm);
    const providerInput = $("#chat-provider", el), baseInput = $("#chat-base-url", el), modelSelect = $("#chat-model-select", el);
    const defaultBase = "https://llmapi.paratera.com/v1";
    const connectionFrom = (value) => {
      const hasConnection = value?.has_saved_config ?? (!!value?.configured || !!value?.model);
      return hasConnection
        ? { provider: value.provider_id || "paratera", base_url: value.base_url || (value.provider_id === "openai_compatible" ? "" : defaultBase), model: value.model || "" }
        : { provider: "openai_compatible", base_url: "", model: "" };
    };
    s.connectionDraft ||= connectionFrom(s.status);
    function syncConnection(value) {
      s.connectionDraft = connectionFrom(value);
      s.connectionEdited = false;
    }
    function connectionChanged() {
      const saved = connectionFrom(s.status), draft = s.connectionDraft;
      return saved.provider !== draft.provider || saved.base_url.replace(/\/+$/, "") !== draft.base_url.trim().replace(/\/+$/, "");
    }
    const examples = $(".chat-examples", el);
    CHAT_EXAMPLES.forEach((question) => {
      const button = document.createElement("button");
      button.className = "chat-example";
      button.type = "button";
      button.textContent = question;
      button.onclick = () => { if (!s.busy) { s.draft = question; render(); input.focus(); } };
      examples.append(button);
    });
    const updateSend = () => { send.disabled = s.busy || s.configureBusy || s.statusLoading || s.status?.configured !== true || !s.draft.trim(); };
    function render() {
      if (input.value !== s.draft) input.value = s.draft;
      input.disabled = s.busy;
      send.innerHTML = s.busy ? '<span class="spinner"></span>等待回答' : `${icon("ask")}提问`;
      updateSend();
      clear.disabled = !s.busy && !s.turns.length && !s.draft && !s.error;
      refresh.disabled = s.statusLoading || s.configureBusy;
      settings.disabled = s.configureBusy;
      examples.querySelectorAll("button").forEach((button) => { button.disabled = s.busy; });
      statusBox.replaceChildren();
      const label = document.createElement("strong"), message = document.createElement("p");
      const configured = s.status?.configured === true;
      label.textContent = s.statusLoading ? "正在检查问答模型……" : configured ? "问答模型已配置" : "问答模型尚不可用";
      message.textContent = s.statusError || s.status?.message || (s.statusLoading ? "请稍候。" : configured ? "已连接模型，可开始问答和生成研究候选。" : "尚未接入可用模型；当前不会生成答案。可继续使用全文检索查看证据。");
      statusBox.append(label, message);
      if (configured) {
        const model = document.createElement("small");
        model.textContent = `${s.status.provider || "未报告提供方"} / ${s.status.model || "未报告模型"}${s.status.base_url ? ` · ${s.status.base_url}` : ""}`;
        statusBox.append(model);
      }
      $(".chat-status", el).classList.toggle("unavailable", !configured && !s.statusLoading);
      settings.hidden = subscription;
      config.hidden = subscription || (configured && !s.configOpen);
      if (subscription) {
        label.textContent = s.statusLoading ? "正在读取订阅状态…" : configured ? "Codex 订阅问答" : "订阅问答暂不可用";
        message.textContent = s.statusError || s.status?.message || "正在读取状态…";
        refresh.textContent = "刷新状态";
      }
      keyInput.disabled = s.configureBusy || s.busy;
      connect.disabled = s.configureBusy || s.busy;
      connect.textContent = s.configureBusy ? "正在连接并验证……" : "连接并保存";
      providerInput.value = s.connectionDraft.provider;
      baseInput.value = s.connectionDraft.base_url;
      modelSelect.value = s.connectionDraft.model;
      providerInput.disabled = baseInput.disabled = modelSelect.disabled = s.configureBusy || s.busy;
      baseInput.readOnly = s.connectionDraft.provider === "paratera";
      modelSelect.required = false;
      $("#chat-connection-hint", el).textContent = s.connectionDraft.provider === "paratera"
        ? "并行使用固定服务地址；模型可留空自动选择，也可填写平台支持的完整模型名。"
        : "填写服务商提供的 API 基础地址（如以 /v1 结尾）。可直接输入完整模型名；留空时仅在服务支持 /models 的情况下自动选择。";
      keyInput.required = !configured || connectionChanged();
      keyInput.placeholder = keyInput.required ? "粘贴此服务的 API key" : "留空保留当前密钥，可直接修改模型";
      $("#chat-model-options", el).replaceChildren(...s.availableModels.map((name) => {
        const option = document.createElement("option"); option.value = name; option.textContent = name; return option;
      }));
      const configError = $(".chat-config-error", el);
      configError.replaceChildren();
      if (s.configError) { const note = h('<div class="notice"></div>'); note.textContent = s.configError; configError.append(note); }
      const errorBox = $(".chat-error", el);
      errorBox.replaceChildren();
      if (s.error) { const note = h('<div class="notice"></div>'); note.textContent = s.error; errorBox.append(note); }
      const pending = $(".chat-pending", el);
      pending.replaceChildren();
      if (s.busy) {
        const wait = h('<div class="card chat-wait"><span class="spinner"></span><div><strong>正在检索文献并等待模型回答……</strong><p></p><small>切换页面后仍保留本轮请求；清空会话会停止本页等待。</small></div></div>');
        $("p", wait).textContent = s.pendingQuestion;
        pending.append(wait);
      }
      el.querySelectorAll("[data-chat-sort]").forEach((button) => {
        const selected = button.dataset.chatSort === s.sortOrder;
        button.classList.toggle("on", selected);
        button.setAttribute("aria-pressed", String(selected));
      });
      // Reverse only a display copy; request history stays chronological.
      const displayTurns = s.sortOrder === "latest" ? s.turns.slice().reverse() : s.turns;
      history.replaceChildren(...displayTurns.map((turn) => chatTurn(turn, turn.number)));
      if (!s.turns.length && !s.busy) {
        history.append(h('<div class="chat-empty">可以询问概念、工艺参数或实验结果。点击回答中的引用，即可查看论文原文。</div>'));
      }
    }
    s.render = render;
    el.querySelectorAll("[data-chat-sort]").forEach((button) => (button.onclick = () => {
      s.sortOrder = button.dataset.chatSort;
      render();
    }));

    async function refreshStatus() {
      if (s.statusLoading || s.configureBusy) return;
      const statusVersion = ++s.statusVersion;
      s.statusLoading = true;
      s.statusError = "";
      s.render?.();
      try {
        let status;
        if (subscription) {
          status = await api("/api/chat/status?channel=subscription");
          if (status.configured) status.message = native ? "提问会将问题、最近对话和命中页的文字与图像发送给 OpenAI。" :
            "提问会将问题、最近对话和检索到的证据文本发送给 OpenAI。";
        } else {
          status = await api("/api/chat/status");
          if (status.configured) status.message = native ? "提问会将问题、最近对话及命中 PDF 页的文字发送至下方 API。" :
            "提问会将问题、最近对话及检索到的证据文本发送至下方 API。";
        }
        if (statusVersion !== s.statusVersion) return;
        if (typeof status?.configured !== "boolean") throw new Error("模型状态响应格式不完整，请检查问答服务。");
        s.status = status;
        if (Array.isArray(status.models)) s.availableModels = [...new Set(status.models.filter((name) => typeof name === "string"))];
        if (!s.connectionEdited) syncConnection(status);
      } catch (error) {
        if (statusVersion !== s.statusVersion) return;
        s.status = null;
        s.statusError = `无法取得模型状态：${error.message || "连接失败"}`;
      } finally {
        if (statusVersion === s.statusVersion) {
          s.statusLoading = false;
          s.render?.();
        }
      }
    }
    refresh.onclick = refreshStatus;
    settings.onclick = () => { s.configOpen = !s.configOpen; render(); if (!config.hidden) providerInput.focus(); };
    providerInput.onchange = () => {
      keyInput.value = "";
      s.connectionDraft = { provider: providerInput.value, base_url: providerInput.value === "paratera" ? defaultBase : "", model: "" };
      s.connectionEdited = true; s.configError = ""; render();
      (providerInput.value === "openai_compatible" ? baseInput : modelSelect).focus();
    };
    baseInput.oninput = () => {
      keyInput.value = "";
      s.connectionDraft.base_url = baseInput.value; s.connectionEdited = true;
      keyInput.required = !s.status?.configured || connectionChanged();
      keyInput.placeholder = keyInput.required ? "粘贴此服务的 API key" : "留空保留当前密钥，可直接修改模型";
    };
    modelSelect.oninput = () => { s.connectionDraft.model = modelSelect.value; s.connectionEdited = true; };
    async function configureModel(apiKey) {
      if (s.configureBusy || s.busy) return;
      const { provider, base_url, model } = s.connectionDraft;
      if (!apiKey && (!s.status?.configured || connectionChanged())) { s.configError = "首次连接或更换服务地址时，请填写该服务的 API key。"; render(); keyInput.focus(); return; }
      if (!base_url.trim()) { s.configError = "请填写 API base URL。"; render(); baseInput.focus(); return; }
      s.statusVersion += 1;
      s.statusLoading = false;
      s.configureBusy = true;
      s.configError = "";
      s.render?.();
      const controller = new AbortController();
      let timedOut = false;
      const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, 60000);
      try {
        const result = await api("/api/chat/configure", {
          method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
          body: JSON.stringify({ api_key: apiKey, provider, base_url: base_url.trim(), model: model.trim() }),
        });
        if (result?.configured !== true) throw new Error(result?.message || "平台连接尚未就绪，请检查密钥后重试。");
        // Never copy the credential into state, storage, messages or error output.
        keyInput.value = "";
        s.status = { configured: true, provider: result.provider, provider_id: result.provider_id || provider, base_url: result.base_url || base_url.trim(), model: result.model,
                     message: apiKey ? String(result.message || "连接成功。").split(apiKey).join("[已隐藏]") : result.message };
        s.statusError = "";
        s.availableModels = [...new Set((Array.isArray(result.models) ? result.models : [])
          .filter((name) => typeof name === "string"))];
        syncConnection(s.status);
        s.configOpen = false;
      } catch (error) {
        const message = timedOut ? "等待平台连接验证超时，请稍后重新检查连接。" : String(error.message || "连接失败，请稍后重试。");
        s.configError = apiKey ? message.split(apiKey).join("[已隐藏]") : message;
        s.configOpen = true;
      } finally {
        clearTimeout(timeout);
        s.configureBusy = false;
        s.render?.();
      }
    }
    keyForm.onsubmit = (event) => { event.preventDefault(); configureModel(keyInput.value.trim()); };
    input.oninput = () => { s.draft = input.value; updateSend(); clear.disabled = false; };
    input.onkeydown = (event) => {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) {
        event.preventDefault();
        if (!send.disabled) form.requestSubmit();
      }
    };
    clear.onclick = () => {
      s.requestVersion += 1;
      s.controller?.abort();
      Object.assign(s, { turns: [], nextTurnNumber: 1, draft: "", error: "", busy: false, pendingQuestion: "", controller: null });
      closeDrawer();
      s.render?.();
      input.focus();
    };
    form.onsubmit = async (event) => {
      event.preventDefault();
      const question = s.draft.trim();
      if (!question || s.busy || s.status?.configured !== true) return;
      const recent = s.turns.slice(-CHAT_HISTORY_LIMIT).flatMap((turn) => [
        { role: "user", content: turn.question }, { role: "assistant", content: turn.answer },
      ]);
      const requestVersion = ++s.requestVersion;
      const controller = new AbortController();
      let timedOut = false;
      const timeout = setTimeout(() => { timedOut = true; controller.abort(); }, subscription ? 660000 : 180000);
      Object.assign(s, { busy: true, pendingQuestion: question, draft: "", error: "", controller });
      s.render?.();
      if (el.isConnected) $(".chat-pending", el).scrollIntoView({ behavior: "smooth", block: "nearest" });
      try {
        const result = await api(native ? "/api/native/chat" : "/api/chat", {
          method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal,
          body: JSON.stringify({ question, history: recent, channel: via }),
        });
        if (requestVersion !== s.requestVersion) return;
        if (typeof result?.answer !== "string" || !result.answer.trim() || !Array.isArray(result.sources)) {
          throw new Error("问答服务未返回完整的答案与来源结构，请重试。");
        }
        const sources = result.sources.filter((source) => source && typeof source.id === "string" && /^S\d+$/.test(source.id));
        s.turns.push({ number: s.nextTurnNumber++, question, answer: result.answer, sources, provider: result.provider, model: result.model,
                       answerability: result.answerability, retrieval: result.retrieval,
                       warnings: Array.isArray(result.warnings) ? result.warnings : [] });
        s.turns = s.turns.slice(-CHAT_HISTORY_LIMIT);
      } catch (error) {
        if (requestVersion !== s.requestVersion) return;
        s.draft = question;
        s.error = timedOut ? "等待回答超时，请稍后重试。"
          : `本次提问未完成：${error.message || "网络连接失败，请重试。"}`;
      } finally {
        clearTimeout(timeout);
        if (requestVersion === s.requestVersion) {
          Object.assign(s, { busy: false, pendingQuestion: "", controller: null });
          s.render?.();
          if (el.isConnected && !s.error) {
            $(`[data-chat-turn="${s.nextTurnNumber - 1}"]`, el)?.scrollIntoView({ behavior: "smooth", block: "start" });
          }
        }
      }
    };
    render();
    refreshStatus();
    return el;
  }

  /* ---------- views ---------- */
  /* ---------- library: papers layered by two chosen dimensions ---------- */
  const NONE = "__unspecified__"; // same key the backend taxonomy uses for "no value"
  const PERIODS = [["early", "2009 年及以前"], ["2010", "2010–2014"], ["2015", "2015–2019"], ["2020", "2020–2022"], ["2023", "2023–2024"], ["2025", "2025 年以来"]];
  function periodOf(year) {
    const y = Number(year);
    if (!y) return NONE;
    return y < 2010 ? "early" : y < 2015 ? "2010" : y < 2020 ? "2015" : y < 2023 ? "2020" : y < 2025 ? "2023" : "2025";
  }
  // Each dimension yields the list of group keys a paper belongs to (multi-valued facets may yield several).
  const LIB_DIMS = {
    category: { label: "主工艺分类", values: (p) => [p.classification?.primary_category || NONE],
      options: () => (state.taxonomy?.categories || []).map((c) => ({ value: c.id, label: c.label, group: c.group })) },
    relevance: { label: "课题相关性", facet: "relevance" },
    routes: { label: "技术路线", facet: "routes" },
    topics: { label: "关注问题", facet: "topics" },
    methods: { label: "研究方法", facet: "methods" },
    content_nature: { label: "内容性质", facet: "content_nature" },
    document_form: { label: "资料形式", facet: "document_form" },
    period: { label: "发表年代", values: (p) => [periodOf(p.year)], options: () => PERIODS.map(([value, label]) => ({ value, label })), always: true },
  };
  function libDim(id) {
    const dim = LIB_DIMS[id];
    const facet = dim.facet && state.taxonomy?.facets.find((f) => f.id === dim.facet);
    const values = dim.values || ((p) => {
      const raw = p.classification?.[dim.facet];
      const list = (Array.isArray(raw) ? raw : raw ? [raw] : []).map(String).filter(Boolean);
      return list.length ? [...new Set(list)] : [NONE];
    });
    // The unlabelled bucket is always appended last by ordered(), so drop the backend's own "未明确" option here.
    const options = (dim.options ? dim.options() : (facet?.options || []).map((o) => ({ value: o.value, label: o.label }))).filter((o) => o.value !== NONE);
    return { id, label: dim.label, values, options, multi: !!(facet?.multiple), labelOf: (v) => v === NONE ? "未标注" : options.find((o) => o.value === v)?.label || v };
  }
  // "W2W · 晶圆对晶圆" reads well in a title but is too wide for a column head.
  const shortLabel = (label) => String(label).split(" · ")[0];

  function libraryTabs(current) {
    const native = arch === "raganything";
    return `<nav class="lib-tabs" aria-label="文献库视图"><a href="${modeHref("library")}" ${current === "library" ? 'aria-current="page"' : ""}>文献 <span>${native ? state.native.published : state.papers.length}</span></a><a href="${modeHref("claims")}" ${current === "claims" ? 'aria-current="page"' : ""}>${native ? "实体与关系" : `知识记录 <span>${state.claims.length}</span>`}</a></nav>`;
  }

  function nativeLibraryView(params, graph = false) {
    const docs = [...state.nativePapers.values()].sort((a, b) => a.title.localeCompare(b.title));
    const el = h(`<div class="page library-page"><div class="page-head"><div><h1>${graph ? "实体与关系" : "文献库"}</h1>
      <p>RAG-Anything · ${docs.length} 篇已发布文献</p></div>${libraryTabs(graph ? "claims" : "library")}</div></div>`);
    if (!docs.length) { el.append(h(emptyState("原生索引尚未发布文献。"))); return el; }
    const panel = h('<section class="card native-panel"></section>');
    el.append(panel);
    if (!graph) {
      panel.innerHTML = `${searchInput("data-filter-native", "按文献标题筛选")}<ul class="native-documents"></ul>`;
      const input = $("[data-filter-native]", panel), list = $("ul", panel);
      input.value = params.get("q") || "";
      const render = () => {
        const shown = docs.filter(d => d.title.toLowerCase().includes(input.value.trim().toLowerCase()));
        list.innerHTML = shown.map(d => `<li><div><a href="${modeHref("papers", new URLSearchParams({doc:d.doc_id,read:"pdf"}))}">${esc(d.title)}</a>
          <small>${d.pages} 页 · ${d.chunks} 个索引片段</small></div>
          <a class="btn sm" href="${modeHref("claims", new URLSearchParams({doc:d.doc_id}))}">实体与关系</a></li>`).join("") || "<li>没有匹配的已发布文献。</li>";
      };
      input.oninput = render; render();
      return el;
    }
    panel.innerHTML = `<label class="native-doc-picker">文献<select class="select">${docs.map(d => `<option value="${esc(d.doc_id)}">${esc(d.title)}</option>`).join("")}</select></label>
      <div data-graph-body aria-live="polite"></div>`;
    const pick = $("select", panel), body = $("[data-graph-body]", panel);
    if (state.nativePapers.has(params.get("doc"))) pick.value = params.get("doc");
    let version = 0;
    const load = async () => {
      const id = pick.value, request = ++version;
      body.textContent = "正在读取文献图谱…";
      try {
        const data = await api("/api/native/documents/" + encodeURIComponent(id) + "/graph");
        if (version !== request) return;
        body.innerHTML = `<p class="native-links"><a href="${modeHref("papers", new URLSearchParams({doc:id,read:"pdf"}))}">打开论文原页</a></p>
          ${searchInput("data-graph-filter", "筛选实体或关系")}<div class="native-graph-results"></div>`;
        const filter = $("input", body), rows = $(".native-graph-results", body);
        const render = () => {
          const term = filter.value.trim().toLowerCase();
          const names = data.entities.filter(n => n.toLowerCase().includes(term));
          const pairs = data.relations.filter(pair => pair.some(n => n.toLowerCase().includes(term)));
          rows.innerHTML = `<h3>实体 · ${names.length}</h3><div class="chips">${names.map(n => `<span class="chip">${esc(n)}</span>`).join("") || "无匹配实体"}</div>
            <h3>关系 · ${pairs.length}</h3><ul class="native-pairs">${pairs.map(([a,b]) => `<li>${esc(a)} ↔ ${esc(b)}</li>`).join("") || "<li>无匹配关系</li>"}</ul>`;
        };
        filter.oninput = render; render();
      } catch (error) { if (request === version) body.textContent = error.message; }
    };
    pick.onchange = () => { history.replaceState(null, "", modeHref("claims", new URLSearchParams({doc:pick.value}))); load(); };
    el.dispose = () => { version++; }; load();
    return el;
  }

  function libraryView(params = new URLSearchParams()) {
    // Without the taxonomy only the period layer works; use a copy so that fallback doesn't overwrite the saved choice.
    const f = state.taxonomy ? ui.library : { ...ui.library };
    const available = Object.keys(LIB_DIMS).filter((id) => LIB_DIMS[id].always || state.taxonomy);
    for (const id of ["l1", "l2", "a", "b", "q"]) if (params.has(id)) f[id] = params.get(id);
    if (!available.includes(f.l1)) f.l1 = available.includes("category") ? "category" : "period";
    if (f.l2 && (!available.includes(f.l2) || f.l2 === f.l1)) f.l2 = "";
    const s = state.summary;
    const el = h(`<div class="page library-page">
      <div class="page-head"><div><h1>文献库</h1><p>${[`${s.papers} 份资料`, s.pages != null && `${s.pages} 页 PDF 原页`, `${s.claims} 条知识记录`].filter(Boolean).join(" · ")}</p></div>${libraryTabs("library")}</div>
      ${state.taxonomy ? "" : `<div class="notice" style="margin-bottom:14px">${icon("warn")}<span>${state.taxonomyError ? "工艺分类暂不可用" : "正在载入工艺分类"}，暂时只能按发表年代分层。</span></div>`}
      <section class="card lib-panel">
        <div class="lib-controls">
          <label class="lib-layer"><span>第一层</span><select class="select" data-layer="l1">${available.map((id) => `<option value="${id}">${LIB_DIMS[id].label}</option>`).join("")}</select></label>
          <span class="lib-arrow" aria-hidden="true">${icon("next")}</span>
          <label class="lib-layer"><span>第二层</span><select class="select" data-layer="l2"><option value="">不再细分</option>${available.map((id) => `<option value="${id}">${LIB_DIMS[id].label}</option>`).join("")}</select></label>
          ${searchInput('data-lib-q', "筛选标题、作者、材料、关键词…")}
        </div>
        <div class="lib-matrix-wrap"><table class="lib-matrix"></table></div>
        <p class="lib-note count"></p>
      </section>
      <div class="count-row lib-status"><span class="count" role="status" aria-live="polite"></span><button class="btn sm ghost" type="button" data-lib-clear hidden>${icon("clear")}查看全部</button></div>
      <div class="lib-groups"></div>
    </div>`);
    const l1Select = $('[data-layer="l1"]', el), l2Select = $('[data-layer="l2"]', el), q = $("[data-lib-q]", el);
    l1Select.value = f.l1; l2Select.value = f.l2; q.value = f.q;
    const expanded = new Set();
    const sync = () => {
      if (f !== ui.library) return; // a temporary fallback must not rewrite the shared link either
      const query = new URLSearchParams();
      query.set("l1", f.l1);
      for (const id of ["l2", "a", "b", "q"]) if (f[id]) query.set(id, f[id]);
      history.replaceState(history.state, "", modeHref("library", query));
    };
    l1Select.onchange = () => {
      f.l1 = l1Select.value; f.a = ""; f.b = "";
      if (f.l2 === f.l1) { f.l2 = ""; l2Select.value = ""; }
      expanded.clear(); render();
    };
    l2Select.onchange = () => { f.l2 = l2Select.value === f.l1 ? "" : l2Select.value; l2Select.value = f.l2; f.b = ""; expanded.clear(); render(); };
    q.addEventListener("input", () => { f.q = q.value; render(); });
    $("[data-lib-clear]", el).onclick = () => { f.a = ""; f.b = ""; render(); };
    const pick = (a, b) => {
      const same = f.a === a && f.b === b;
      f.a = same ? "" : a; f.b = same ? "" : b;
      render();
      if (!same) $(".lib-status", el).scrollIntoView({ behavior: reducedMotion() ? "auto" : "smooth", block: "start" });
    };

    function ordered(dim, counts) {
      // Every taxonomy value in taxonomy order, zero counts included; then any value the data uses that the
      // taxonomy lacks, and "未标注" last — those two only when some paper actually has them.
      const known = dim.options.map((o) => o.value);
      const extra = Object.keys(counts).filter((k) => k !== NONE && !known.includes(k)).sort();
      return [...known, ...[...extra, NONE].filter((k) => counts[k])];
    }

    function render() {
      sync();
      l2Select.querySelectorAll("option").forEach((o) => (o.disabled = o.value === f.l1));
      const d1 = libDim(f.l1), d2 = f.l2 ? libDim(f.l2) : null;
      const term = f.q.trim().toLowerCase();
      const searchText = (p) => [p.title, p.doc_id, p.doi, p.short_label, p.topic, authorsText(p),
        ...["materials", "topics", "keywords"].flatMap((k) => p.classification?.[k] || [])].join(" ").toLowerCase();
      const pool = state.papers.filter((p) => !term || searchText(p).includes(term));
      const v1 = new Map(pool.map((p) => [p.doc_id, d1.values(p)]));
      const v2 = d2 ? new Map(pool.map((p) => [p.doc_id, d2.values(p)])) : null;
      const rowCount = {}, colCount = {}, cell = {};
      for (const p of pool) {
        for (const a of v1.get(p.doc_id)) {
          rowCount[a] = (rowCount[a] || 0) + 1;
          if (v2) for (const b of v2.get(p.doc_id)) cell[a + "\u0000" + b] = (cell[a + "\u0000" + b] || 0) + 1;
        }
        if (v2) for (const b of v2.get(p.doc_id)) colCount[b] = (colCount[b] || 0) + 1;
      }
      const rows = ordered(d1, rowCount), cols = v2 ? ordered(d2, colCount) : [];
      if (f.a && !rowCount[f.a]) f.a = "";
      if (f.b && !colCount[f.b]) f.b = "";

      // Matrix: one sequential hue, shade = share of the busiest cell; every cell also prints its number.
      const cellMax = Math.max(1, ...Object.values(cell)), rowMax = Math.max(1, ...Object.values(rowCount));
      const shade = (n, max) => n ? Math.round(10 + 78 * Math.sqrt(n / max)) : 0;
      const groupBreak = (k, i) => d1.id === "category" && i > 0 && d1.options.find((o) => o.value === k)?.group !== d1.options.find((o) => o.value === rows[i - 1])?.group;
      $(".lib-matrix", el).innerHTML = `
        <thead><tr><th class="lib-corner" scope="col">${esc(d1.label)}${d2 ? ` <span>×</span> ${esc(d2.label)}` : ""}</th>
          ${cols.map((b) => `<th scope="col" class="lib-col ${f.b === b && !f.a ? "on" : ""} ${colCount[b] ? "" : "zero"}" data-col="${esc(b)}"><button type="button" ${colCount[b] ? `data-pick-b="${esc(b)}"` : "disabled"} title="${esc(d2.labelOf(b))}：${colCount[b] || 0} 份">${esc(shortLabel(d2.labelOf(b)))}<small>${colCount[b] || 0}</small></button></th>`).join("")}
          <th scope="col" class="lib-total-head">${d2 ? "合计" : "资料数"}</th></tr></thead>
        <tbody>${rows.map((a, i) => `<tr class="${groupBreak(a, i) ? "lib-break" : ""} ${f.a === a ? "on" : ""} ${rowCount[a] ? "" : "zero"}" data-row="${esc(a)}">
          <th scope="row"><button type="button" ${rowCount[a] ? `data-pick-a="${esc(a)}"` : "disabled"} title="${esc(d1.labelOf(a))}">${esc(d1.labelOf(a))}</button></th>
          ${cols.map((b) => {
            const n = cell[a + "\u0000" + b] || 0, t = shade(n, cellMax);
            return `<td><button type="button" class="lib-cell ${f.a === a && f.b === b ? "on" : ""} ${t > 52 ? "deep" : ""}" data-col="${esc(b)}" ${n ? `data-pick-a="${esc(a)}" data-pick-b="${esc(b)}"` : "disabled"} style="--t:${t}%" title="${esc(d1.labelOf(a))} · ${esc(d2.labelOf(b))}：${n} 份">${n}</button></td>`;
          }).join("")}
          <td class="lib-total"><button type="button" ${rowCount[a] ? `data-pick-a="${esc(a)}"` : "disabled"}><span class="lib-total-bar"><i style="width:${(rowCount[a] || 0) / rowMax * 100}%"></i></span><b>${rowCount[a] || 0}</b></button></td></tr>`).join("")}</tbody>`;
      $(".lib-note", el).textContent = [
        "点击格子、行名或列名查看对应文献，再次点击取消。",
        d1.multi || d2?.multi ? "一份资料可同时属于多个分组，各组合计可能大于资料总数。" : "",
      ].filter(Boolean).join("");
      el.querySelectorAll(".lib-matrix [data-pick-a], .lib-matrix [data-pick-b]").forEach((b) => (b.onclick = () => pick(b.dataset.pickA || "", b.dataset.pickB || "")));
      // Crosshair: hovering a cell lights up its row and column heads.
      const matrix = $(".lib-matrix", el);
      matrix.onmouseover = (e) => {
        const c = e.target.closest("[data-col]"), r = e.target.closest("[data-row]");
        matrix.querySelectorAll(".hover").forEach((x) => x.classList.remove("hover"));
        if (c && r) { r.classList.add("hover"); matrix.querySelector(`th[data-col="${CSS.escape(c.dataset.col)}"]`)?.classList.add("hover"); }
      };
      matrix.onmouseleave = () => matrix.querySelectorAll(".hover").forEach((x) => x.classList.remove("hover"));

      // Layered list: first-layer sections, second-layer sub-groups, papers newest first.
      const inA = (p, a) => v1.get(p.doc_id).includes(a), inB = (p, b) => v2.get(p.doc_id).includes(b);
      const newest = (x, y) => (y.year || 0) - (x.year || 0) || x.title.localeCompare(y.title);
      const shownRows = f.a ? [f.a] : rows;
      const visible = pool.filter((p) => (!f.a || inA(p, f.a)) && (!f.b || inB(p, f.b)));
      const label = [f.a && d1.labelOf(f.a), f.b && d2.labelOf(f.b)].filter(Boolean).join(" · ");
      $(".lib-status .count", el).textContent = `${visible.length} / ${state.papers.length} 份资料${label ? " · " + label : ""}${term ? ` · 含“${f.q.trim()}”` : ""}`;
      $("[data-lib-clear]", el).hidden = !(f.a || f.b);
      const focused = !!(f.a || f.b);
      const PREVIEW = 4;
      const paperRow = (p) => `<button type="button" class="lib-paper" data-doc="${esc(p.doc_id)}">
          <span class="lib-paper-year">${esc(p.year || "—")}</span>
          <span class="lib-paper-main"><span class="lib-paper-title">${esc(p.title)}</span>
          <span class="lib-paper-meta">${esc(p.short_label || p.doc_id)}${p.classification?.document_form ? " · " + esc(facetLabel("document_form", p.classification.document_form)) : p.is_review ? " · 综述" : ""}</span></span>
          <span class="lib-paper-claims" title="知识记录">${p.claim_count ?? 0}<small>条记录</small></span></button>`;
      const block = (key, list) => {
        const open = focused || expanded.has(key) || list.length <= PREVIEW + 1;
        return `${(open ? list : list.slice(0, PREVIEW)).map(paperRow).join("")}${open ? "" : `<button type="button" class="lib-more" data-expand="${esc(key)}">展开其余 ${list.length - PREVIEW} 份</button>`}`;
      };
      const html = shownRows.map((a) => {
        const inRow = visible.filter((p) => inA(p, a)).sort(newest);
        // Empty first-layer groups stay listed as a one-line heading so the layering is complete.
        if (!inRow.length) return f.b ? "" : `<section class="lib-group lib-group-empty"><header class="lib-group-head"><h2>${esc(d1.labelOf(a))}</h2><span class="lib-group-count">0 份 · 暂无文献</span></header></section>`;
        const subs = d2 ? (f.b ? [f.b] : cols).map((b) => [b, inRow.filter((p) => inB(p, b))]).filter(([, list]) => list.length) : null;
        return `<section class="lib-group">
          <header class="lib-group-head"><h2>${esc(d1.labelOf(a))}</h2><span class="lib-group-count">${inRow.length} 份</span>${f.a ? "" : `<button type="button" class="btn sm ghost" data-pick-a="${esc(a)}">只看这一组</button>`}</header>
          ${subs ? subs.map(([b, list]) => `<div class="lib-sub"><h3>${esc(d2.labelOf(b))}<span>${list.length}</span></h3>${block(a + "\u0000" + b, list)}</div>`).join("") : `<div class="lib-sub">${block(a, inRow)}</div>`}
        </section>`;
      }).join("");
      const groups = $(".lib-groups", el);
      groups.innerHTML = visible.length ? html : `<div class="card empty">${icon("inbox")}没有符合条件的文献。</div>`;
      const ids = [...new Set([...groups.querySelectorAll("[data-doc]")].map((b) => b.dataset.doc))];
      groups.querySelectorAll("[data-doc]").forEach((b) => (b.onclick = () => openPaper(b.dataset.doc, ids)));
      groups.querySelectorAll("[data-expand]").forEach((b) => (b.onclick = () => { expanded.add(b.dataset.expand); render(); }));
      groups.querySelectorAll("[data-pick-a]").forEach((b) => (b.onclick = () => pick(b.dataset.pickA, "")));
    }
    render();
    return el;
  }

  function claimsView(params) {
    if (params.get("stage")) ui.claims.stage = params.get("stage");
    if (params.get("type")) ui.claims.type = params.get("type");
    if (params.get("doc")) ui.claims.doc = params.get("doc");
    const materials = [...new Set(state.claims.map((c) => c.material_system))].sort();
    const el = h(`<div class="page">
      <div class="page-head"><div><h1>文献库</h1><p>知识记录：材料 · 样品 · 阶段 · 条件 · 原文引文。点击卡片查看原页，← → 切换上下条</p></div>${libraryTabs("claims")}</div>
      <div class="card filter-bar">
        <div class="toolbar">
          ${searchInput('data-f="q"', "筛选陈述、数值、物理量…")}
          <select class="select" data-f="material"><option value="">全部材料体系</option>${materials.map((m) => `<option>${esc(m)}</option>`).join("")}</select>
          <select class="select" data-f="doc"><option value="">全部来源文献</option>${state.papers.map((p) => `<option value="${esc(p.doc_id)}">${esc(p.doc_id)} · ${esc(shortTitle(p.title, 40))}</option>`).join("")}</select>
          <select class="select" data-f="type"><option value="">全部证据类型</option>${Object.entries(CLAIM_TYPES).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        </div>
        <div class="stage-chips"></div>
      </div>
      <div class="count-row"><span class="count"></span><button class="btn sm ghost" data-reset hidden>${icon("clear")}清除筛选</button></div>
      <div class="claim-list"></div>
    </div>`);
    const f = ui.claims;
    const inputs = el.querySelectorAll("[data-f]");
    inputs.forEach((input) => {
      input.value = f[input.dataset.f] || "";
      input.addEventListener("input", () => { f[input.dataset.f] = input.value; render(); });
    });
    $("[data-reset]", el).onclick = () => {
      Object.assign(f, { q: "", stage: null, material: "", doc: "", type: "" });
      inputs.forEach((input) => (input.value = ""));
      render();
    };
    let firstRender = true;
    const render = () => {
      const q = f.q.trim().toLowerCase();
      const base = state.claims.filter((c) => (!f.material || c.material_system === f.material) && (!f.doc || c.doc_id === f.doc) &&
        (!f.type || c.claim_type === f.type) &&
        (!q || [c.statement, c.value_text, c.quantity, c.material_system, c.sample_id, c.claim_id].join(" ").toLowerCase().includes(q)));
      const rows = base.filter((c) => !f.stage || c.stage === f.stage);
      const counts = {}; base.forEach((c) => (counts[c.stage] = (counts[c.stage] || 0) + 1));
      $(".stage-chips", el).innerHTML = `<span class="chip ${!f.stage ? "on" : ""}" data-s="">全部阶段 · ${base.length}</span>` +
        Object.keys(STAGES).map((k) => `<span class="chip ${f.stage === k ? "on" : ""}" data-s="${k}" style="${f.stage === k ? `color:${stageColor(k)}` : ""}"><span class="dot" style="background:${stageColor(k)}"></span>${STAGES[k]} · ${counts[k] || 0}</span>`).join("");
      $(".stage-chips", el).querySelectorAll("[data-s]").forEach((c) => (c.onclick = () => { f.stage = c.dataset.s || null; render(); }));
      $(".count", el).textContent = `当前 ${rows.length} / ${state.claims.length} 条 · 自动复核，尚未经领域专家确认`;
      $("[data-reset]", el).hidden = !(f.q || f.stage || f.material || f.doc || f.type);
      const list = $(".claim-list", el);
      list.classList.remove("stagger", "fade-in");
      if (!rows.length) { list.innerHTML = emptyState("没有同时满足这些条件的知识记录。"); return; }
      list.innerHTML = rows.map((c) => `<div class="card claim-card lift" data-id="${esc(c.claim_id)}" style="--stage-color:${stageColor(c.stage)}" tabindex="0">
        <span class="go">${icon("arrow")}</span>
        <p class="statement">${esc(c.statement)}</p>
        <div class="meta"><span class="chip mono">${esc(c.claim_id)}</span>${stageChip(c.stage)}<span class="chip">${esc(c.material_system)}</span><span class="chip" title="${esc(state.paperById[c.doc_id]?.title)}">${icon("pdf")}${esc(c.doc_id)} · p${[...new Set(c.evidence.map((e) => e.pdf_page))].join(",")}</span></div>
        ${c.value_text ? `<div class="value">${esc(c.value_text)}</div>` : ""}
      </div>`).join("");
      const ids = rows.map((c) => c.claim_id);
      list.querySelectorAll("[data-id]").forEach((card) => {
        card.onclick = () => openClaim(card.dataset.id, ids);
        card.onkeydown = (e) => { if (e.key === "Enter") openClaim(card.dataset.id, ids); };
      });
      // Stagger only the first paint; filter updates use a quick fade so typing stays calm.
      if (firstRender) stagger(list); else { void list.offsetWidth; list.classList.add("fade-in"); }
      firstRender = false;
    };
    render();
    return el;
  }

  function searchView(params = new URLSearchParams()) {
    const native = arch === "raganything";
    const s = native ? nativeSearch : ui.search;
    const incoming = (params.get("q") || "").trim().slice(0,500);
    if (incoming && incoming !== s.executed) { s.q = incoming; s.results = null; }
    if (incoming) s.q = incoming;
    let searchGeneration = 0;
    const el = h(`<div class="page">
      <div class="page-head"><div><h1>全文检索</h1><p>在本机检索已发布文献，点击结果可核对 PDF 原页。</p></div></div>
      <form class="card search-box">
        <label class="input-icon">${icon("search")}<input class="input" name="q" placeholder="例如：Cu pumping、plasma SiCN，或中文：退火温度对接触电阻的影响" autocomplete="off" data-page-search></label>
        <select class="select" name="mode" aria-label="检索方式" ${native ? "hidden" : ""}>${native ? '<option value="native">原生向量检索</option>' : '<option value="auto">自动检索</option><option value="keyword">关键词</option><option value="semantic">语义检索</option>'}</select>
        <select class="select" name="limit"><option value="5">5 条</option><option value="10">10 条</option><option value="20">20 条</option></select>
        <button class="btn primary" type="submit">${icon("search")}检索</button>
      </form>
      <p class="count mode-hint" style="margin:8px 2px 0"></p>
      <div class="results"></div>
    </div>`);
    const form = $("form", el);
    form.q.value = s.q; form.limit.value = String(s.limit);
    const syncMode = () => {
      form.mode.value = s.mode;
      $(".mode-hint", el).textContent = s.mode === "native"
        ? `本地 BGE-M3 检索原生图文索引${state.native ? `，覆盖 ${state.native.published} 篇已发布文献` : ""}。结果可包含图表描述，引用时请核对原页。`
        : s.mode === "keyword"
        ? "关键词模式使用 SQLite FTS 匹配原文术语，输入不会被自动扩展；中文整句可使用自动或语义检索。"
        : s.mode === "auto"
          ? "中文与中英混输会结合本地术语扩展、关键词和语义检索；纯英文保持原文关键词检索。语义模型不可用时保留术语检索；不调用外部模型 API。"
          : "使用本地 BGE-M3 与 Qdrant；中文与中英混输同时使用术语扩展和关键词，纯英文保持语义检索。不调用外部模型 API；首次载入需要稍候。";
    };
    form.mode.onchange = () => {
      searchGeneration++; s.mode = form.mode.value; s.results = null; s.executed = ""; s.selected = 0;
      syncMode(); renderResults();
    };
    syncMode();
    const results = $(".results", el);

    const SEARCH_EXAMPLES = ["Cu pumping", "plasma activation SiCN", "bond strength anneal",
      "退火温度对接触电阻的影响", "晶圆边缘空洞如何减少"];
    const renderResults = () => {
      if (s.results === null) {
        results.innerHTML = `<div class="examples">${SEARCH_EXAMPLES.map((q) => `<button type="button" class="example" data-q="${esc(q)}">${esc(q)}</button>`).join("")}</div>`;
        stagger($(".examples", results));
        results.querySelectorAll("[data-q]").forEach((b) => (b.onclick = () => {
          form.q.value = b.dataset.q; form.requestSubmit();
        }));
        return;
      }
      if (!s.results.length) { results.innerHTML = `<div style="margin-top:16px">${emptyState("当前查询未返回证据。可以调整材料或工艺术语；未命中不等于不存在相关证据。", "search")}</div>`; return; }
      const rankingScore = (r) => Math.abs(r.retrieval_score ?? r.rrf_score ?? r.score ?? 0);
      const scoreLabel = (r) => r.retrieval_score != null ? `综合排序 ${r.retrieval_score.toFixed(4)}`
        : r.rrf_score != null ? `融合排序 RRF ${r.rrf_score.toFixed(4)}`
        : ["native", "dense"].includes(r.method) ? `语义相似度 ${r.score.toFixed(3)}` : `BM25 ${(-r.score).toFixed(2)}`;
      const reasonLabel = (reason) => {
        if (!reason || typeof reason !== "object") return String(reason ?? "");
        const names = { bilingual_fts: "中英术语匹配", domain_query_rerank: "工艺术语重排", document_scoped_fts: "文献范围匹配" };
        const label = names[reason.method] || reason.method || "检索";
        return label + (reason.terms?.length ? `：${reason.terms.join("、")}` : "");
      };
      const max = Math.max(...s.results.map(rankingScore), 1e-9);
      const layout = h(`<div class="search-layout"><div class="result-list"></div><div class="card card-pad preview"></div></div>`);
      const list = $(".result-list", layout);
      list.innerHTML = `<div class="count">「${esc(s.executed)}」· ${s.results.length} 个${s.mode === "native" ? "索引" : "原文"}片段</div>` + s.results.map((r, i) => {
        const paper = paperFor(r.doc_id) || {};
        const pct = rankingScore(r) / max;
        return `<div class="card result ${i === s.selected ? "on" : ""}" data-i="${i}">
          <div class="r-head"><span>#${i + 1} · PDF 第 ${r.pdf_page ?? "?"}${r.page_end > r.pdf_page ? "–" + r.page_end : ""} 页</span>${paper.has_pdf && r.pdf_page
            ? `<a target="_blank" rel="noopener" href="/api/papers/${encodeURIComponent(r.doc_id)}/pdf${r.pdf_sha256 ? "?revision=" + encodeURIComponent(r.pdf_sha256) : ""}#page=${r.pdf_page}">打开原页 ↗</a>` : ""}</div>
          <div class="r-title">${esc(paper.title || r.doc_id)}</div>
          <div class="r-text">${highlight(r.text, s.executed)}</div>
          <div class="score-bar"><i style="width:${Math.max(6, pct * 100)}%"></i></div></div>`;
      }).join("");
      list.querySelectorAll("[data-i]").forEach((c) => (c.onclick = (event) => {
        if (event.target.closest("a")) return;
        s.selected = +c.dataset.i; s.listScroll = list.scrollTop; renderResults();
        if (matchMedia("(max-width: 1100px)").matches) $(".preview", results)?.scrollIntoView({ block: "start" });
      }));
      if (s.animate) { stagger(list); s.animate = false; }
      const r = s.results[s.selected];
      const paper = paperFor(r.doc_id) || { title: r.doc_id };
      const linked = native ? [] : state.claimsByEvidence[r.evidence_id] || [];
      const preview = $(".preview", layout);
      preview.append(h(`<div class="detail">
        <div class="chips"><span class="chip">${r.method === "native" ? "原生索引片段" : esc(r.evidence_id)}</span><span class="chip">${scoreLabel(r)}</span></div>
        ${r.retrieval_notice ? `<p class="notice">${esc(r.retrieval_notice)}</p>` : ""}
        ${r.retrieval_reasons?.length ? `<p class="muted" style="font-size:12px">检索依据：${r.retrieval_reasons.map((reason) => esc(reasonLabel(reason))).join("；")}</p>` : ""}
        <blockquote class="quote">${highlight(r.text, s.executed)}</blockquote>
        <a class="paper-ref" data-paper="${esc(r.doc_id)}">${esc(paper.title)}<small>${esc(r.doc_id)}${paper.year ? " · " + paper.year : ""}</small></a>
        ${linked.length ? `<h4>此段落支持的知识记录</h4><ul class="side-list">${linked.map((c) => `<li data-claim="${esc(c.claim_id)}"><span class="dot" style="background:${stageColor(c.stage)}"></span>${esc(c.statement)}</li>`).join("")}</ul>` : ""}
        <h4>PDF 原页</h4>
      </div>`));
      if (r.pdf_page) preview.append(pageViewer(r.doc_id, r.pdf_page, { pdfHash: r.pdf_sha256 }));
      $("[data-paper]", preview).onclick = () => openPaper(r.doc_id);
      const linkedIds = linked.map((c) => c.claim_id);
      preview.querySelectorAll("[data-claim]").forEach((li) => (li.onclick = () => openClaim(li.dataset.claim, linkedIds)));
      results.replaceChildren(layout);
      list.scrollTop = s.listScroll || 0;
    };
    // ↑/↓ step through results when the search box is not focused.
    viewKeyHandler = (e) => {
      if (!s.results?.length || (e.key !== "ArrowDown" && e.key !== "ArrowUp")) return false;
      s.selected = Math.max(0, Math.min(s.results.length - 1, s.selected + (e.key === "ArrowDown" ? 1 : -1)));
      s.listScroll = $(".result-list", el)?.scrollTop || 0;
      renderResults();
      $(`.result[data-i="${s.selected}"]`, el)?.scrollIntoView({ block: "nearest", behavior: "smooth" });
      return true;
    };

    form.onsubmit = async (e) => {
      e.preventDefault();
      const generation = ++searchGeneration;
      s.q = form.q.value.trim(); s.limit = +form.limit.value;
      if (!s.q) { results.innerHTML = `<div class="notice" style="margin-top:16px">${icon("warn")}请输入检索内容。</div>`; return; }
      const usesModel = ["native", "semantic"].includes(s.mode) || (s.mode === "auto" && /[\u3400-\u9fff\uf900-\ufaff]/u.test(s.q));
      results.innerHTML = `<div class="loading"><span class="spinner"></span>　${usesModel ? "正在本地检索相关原文（首次需载入语义模型）……" : "正在检索……"}</div>`;
      try {
        const query = s.q;
        const url = s.mode === "native"
          ? "/api/native/search?" + new URLSearchParams({ q: query, limit: s.limit })
          : "/api/search?" + new URLSearchParams({ q: query, mode: s.mode, limit: s.limit });
        const matches = await api(url);
        if (generation !== searchGeneration) return;
        s.results = matches;
        s.executed = query; s.selected = 0; s.listScroll = 0; s.animate = true;
        renderResults();
        form.q.blur();
      } catch (err) {
        if (generation !== searchGeneration) return;
        s.results = null;
        results.innerHTML = `<div class="notice" style="margin-top:16px">${icon("warn")}${esc(err.message)}</div>`;
      }
    };
    renderResults();
    if (incoming && s.results === null) el.afterMount = () => form.requestSubmit();
    el.dispose = () => { searchGeneration++; };
    return el;
  }

  /* ---------- router ---------- */
  let disposeIngestion = null;
  let disposeIdeas = null;
  let refreshingLibrary = null;
  async function refreshLibrary() {
    if (refreshingLibrary) return refreshingLibrary;
    loadTaxonomy();
    refreshingLibrary = (async () => {
      try {
        const [summary, papers, claims] = await Promise.all([api("/api/summary"), api("/api/papers"), api("/api/claims")]);
        const paperById = {}, claimById = {}, claimsByEvidence = {};
        papers.forEach((paper) => { paperById[paper.doc_id] = paper; });
        claims.forEach((claim) => { claimById[claim.claim_id] = claim; (claim.evidence || []).forEach((evidence) => { if (evidence.evidence_id) (claimsByEvidence[evidence.evidence_id] ||= []).push(claim); }); });
        Object.assign(state, { summary, papers, claims, paperById, claimById, claimsByEvidence });
        renderSidebarSummary();
      } catch (_) { /* The next refresh can retry; keep the currently readable library. */ }
    })().finally(() => { refreshingLibrary = null; });
    return refreshingLibrary;
  }
  const nativeStage = {queued:"排队中", parsing:"正在解析 PDF 页", indexing:"正在写入本批索引",
    crop_review:"正在审核裁图", crop_review_held:"裁图待复核",
    multimodal:"正在处理图表与公式", checking:"正在核验页码与检索结果", verified:"索引已核验",
    prepared:"预处理完成，等待写入", merging:"正在合并到检索库", publishing:"正在发布",
    preflight_checking:"正在验收预处理结果",
    preflight_review_held:"预处理待复核",
    batch_ready:"已验收，待正式入库",
    batch_verified:"核验通过，等待本批一起发布",
    saved:"已保存进度", published:"已发布", interrupted:"已中断", held:"新版本待授权"};
  // The scheduler moves each batch through these steps; RAG-Anything indexes and publishes a batch together.
  const NATIVE_STEPS = ["裁图审核", "解析预处理", "同批建索引", "核验", "同批发布"];
  const STAGE_DONE = {queued:0, crop_review:0, crop_review_held:0, held:0, parsing:1,
    preflight_checking:1, preflight_review_held:1, batch_ready:2, prepared:2,
    indexing:2, multimodal:2, checking:3, verified:4, batch_verified:4, merging:4, publishing:4, published:5};
  function stepsDone(row) {
    if (row.stage in STAGE_DONE) return STAGE_DONE[row.stage];
    // Saved or interrupted work: infer from the checkpoints the status already reports.
    return row.milestones >= 2 ? 4 : row.milestones >= 1 ? 2 : row.has_regions ? 1 : 0;
  }
  function elapsedTime(seconds) {
    const total = Math.max(0, Math.floor(seconds));
    const hours = Math.floor(total / 3600), minutes = Math.floor(total / 60) % 60;
    return (hours ? hours + " 小时 " : "") + (hours || minutes ? minutes + " 分 " : "") + total % 60 + " 秒";
  }
  function paperDetails(row) {
    const parts = [row.parsed_pages != null ? `已解析 ${row.parsed_pages}/${row.pages} 页` : `共 ${row.pages} 页`];
    if (row.visual_items != null) parts.push(`图表与公式 ${row.visual_items} 项`);
    if (row.stage === "published") parts.push(`已发布 ${row.chunks} 个索引块`);
    else if (row.responses) parts.push(`已保存 ${row.responses} 条模型响应`);
    if (Number.isFinite(row.elapsed_seconds)) parts.push(`已处理 ${elapsedTime(row.elapsed_seconds)}`);
    if (row.review_issue) parts.push(row.review_issue);
    return parts.join(" · ");
  }
  function regionLink(row, label = "查看裁图") {
    if (!(row.visual_items > 0 || row.has_regions)) return row.has_regions === false ? "暂无裁图记录" : "";
    return `<a class="native-region-link" href="${modeHref("ingestion", new URLSearchParams({regions:row.doc_id}))}">${label}</a>`;
  }
  function nativeSteps(row, running) {
    const done = stepsDone(row);
    const flag = ["crop_review_held", "preflight_review_held"].includes(row.stage) ? "warn"
      : row.stage === "interrupted" ? "error" : running ? "current" : "next";
    return `<ol class="native-steps" aria-label="已完成 ${done} 步，共 ${NATIVE_STEPS.length} 步">${NATIVE_STEPS.map((step, i) =>
      `<li class="${i < done ? "done" : i === done ? flag : ""}"${i === done && done < NATIVE_STEPS.length ? ' aria-current="step"' : ""}>${step}</li>`).join("")}</ol>`;
  }
  function nativePaperCard(row) {
    const running = row.status === "running";
    return `<li class="native-progress-card${row.stage === "interrupted" ? " is-error" : ""}"><div class="native-paper-head"><strong>${esc(row.title || row.doc_id)}</strong>
      <span class="chip">${esc(nativeStage[row.stage] || "等待处理")}</span></div>
      ${nativeSteps(row, running)}
      <small>${esc(paperDetails(row))} ${regionLink(row)}</small></li>`;
  }
  function nativeCounts(data) {
    const waiting = data.waiting || [];
    const needsReview = row => ["crop_review_held", "preflight_review_held"].includes(row.stage);
    const ready = waiting.filter(row => row.stage === "batch_ready");
    const review = waiting.filter(needsReview);
    const queued = waiting.filter(row => !needsReview(row) && !["batch_ready", "held"].includes(row.stage));
    const preflight = data.active.filter(row => row.preflight ?? (data.batch_stage === "preflight"));
    const indexing = data.active.filter(row => !preflight.includes(row));
    return {review, ready, queued, preflight, indexing};
  }
  function nativeBar(data, counts) {
    const total = Math.max(data.catalog, 1), pct = (n) => (100 * n / total).toFixed(2) + "%";
    const parts = [["published", data.published], ["active", counts.indexing.length], ["ready", counts.ready.length],
      ["preflight", counts.preflight.length], ["review", counts.review.length]];
    return `<div class="native-bar" role="img" aria-label="清单 ${data.catalog} 篇，已发布 ${data.published}，正式入库中 ${counts.indexing.length}，已验收待入库 ${counts.ready.length}，预处理中 ${counts.preflight.length}，待复核 ${counts.review.length}">
      ${parts.map(([name, n]) => n ? `<span class="is-${name}" style="width:${pct(n)}"></span>` : "").join("")}</div>`;
  }
  function nativePanel(controls = false) {
    const panel = h(`<section class="card native-panel" aria-label="RAG-Anything 入库队列">
      <div class="native-panel-head"><h2>RAG-Anything 入库队列</h2><button type="button" class="btn sm">刷新状态</button></div>
      <div data-native-summary role="status" aria-live="polite">正在读取发布清单…</div>
      ${controls ? `<div class="native-controls"><button class="btn primary" data-queue-action hidden></button><span role="status" data-queue-message></span></div>
        <div data-native-alerts></div>
        <div data-native-current></div>
        <details data-ready hidden><summary></summary><ul class="native-waiting-list"></ul></details>
        <section class="native-review" data-review hidden><h3></h3>
          <p>调度器会自动复核已标记的裁图。复核后仍有疑点的文献保留问题记录。</p>
          <ul class="native-waiting-list"></ul></section>
        <form class="native-settings" hidden><label>每批处理的文献数 <select data-native-width></select></label>
          <button type="submit" class="btn sm" disabled>保存</button>
          <span data-width-note></span><span role="status" data-width-message></span></form>
        <details data-waiting hidden><summary></summary><ul class="native-waiting-list"></ul></details>` : ""}
      <details data-published><summary>查看已发布文献</summary><ul class="native-documents"></ul></details></section>`);
    const phases = { paused: "已暂停", pausing: "正在暂停", starting: "正在启动", running: "入库中", idle: "待运行",
      pass_complete: "本轮结束", interrupted: "运行已中断", needs_attention: "需要处理",
      waiting_for_subscription_reset: "等待订阅额度恢复" };
    let disposed = false, timer, savedWidth, savingWidth = false, refreshId = 0;
    const form = $(".native-settings", panel);
    const width = $("[data-native-width]", panel);
    function syncWidth() {
      $("button", form).disabled = savingWidth || Number(width.value) === savedWidth;
    }
    const refresh = async () => {
      const id = ++refreshId;
      clearTimeout(timer);
      const button = $("button", panel);
      button.disabled = true;
      try {
        const data = await readNativeStatus(controls);
        if (disposed || id !== refreshId) return;
        const detailed = controls && Array.isArray(data.waiting);
        const counts = detailed ? nativeCounts(data) : null;
        const metrics = detailed
          ? [[data.published, "篇已发布"], [counts.indexing.length, "篇正式入库中"], [counts.ready.length, "篇已验收待入库"],
             [counts.preflight.length, "篇预处理中"], [counts.review.length, "篇待复核", counts.review.length ? "warn" : ""],
             [counts.queued.length, "篇排队"]].concat(data.held ? [[data.held, "篇新版本待授权", "warn"]] : [])
          : [[data.published, "篇可检索"], [data.pending + data.held, "篇待发布"], [data.catalog, "篇清单总数"]];
        const scoped = data.scope === "selected_documents";
        $("[data-native-summary]", panel).innerHTML = `
          <div class="native-metrics">${metrics.map(([n, label, tone]) => `<div${tone ? ` class="is-${tone}"` : ""}><strong>${n}</strong><span>${label}</span></div>`).join("")}</div>
          ${detailed ? nativeBar(data, counts) + `<p class="native-bar-note">清单共 ${data.catalog} 篇 PDF</p>`
            : `<progress aria-label="原生入库进度" value="${data.published}" max="${Math.max(data.catalog, 1)}"></progress>`}
          <div class="native-queue"><span class="chip${["needs_attention","interrupted"].includes(data.phase) ? " warn" : ""}">${esc(phases[data.phase] || "队列状态待确认")}</span>
            ${scoped ? `<span class="chip">仅处理指定文献</span>` : ""}
            <span>Codex CLI · ${esc(data.model)} · ${data.effort === "high" || !data.effort ? "High" : esc(data.effort)}</span>
            ${controls && Number.isFinite(data.run_elapsed_seconds) ? `<span>本轮用时 ${elapsedTime(data.run_elapsed_seconds)}</span>` : ""}</div>
          ${!controls && data.active.length ? `<p>当前文献 · ${data.active.map(row => esc(row.doc_id)).join("、")}</p>` : ""}
          ${!detailed && data.held ? `<p>${data.held} 篇等待授权。</p>` : ""}`;
        $(".native-documents", panel).innerHTML = data.documents.slice().sort((a,b) => a.title.localeCompare(b.title)).map(doc =>
          `<li><a href="${modeHref("papers", new URLSearchParams({doc:doc.doc_id,read:"pdf",page:"1"}))}">${esc(doc.title)}</a><small>${doc.pages} 页${controls && Number.isFinite(doc.elapsed_seconds) ? ` · 已处理 ${elapsedTime(doc.elapsed_seconds)}` : ""} ${controls ? regionLink(doc) : ""}</small></li>`).join("");
        $("[data-published]", panel).hidden = !data.documents.length;
        $("[data-published] summary", panel).textContent = `查看已发布文献（${data.documents.length} 篇）`;
        if (controls) {
          const alerts = [];
          if (data.phase === "interrupted") alerts.push("入库程序已退出，本批未发布的进度保存在检查点中。点“继续文献入库”会从检查点接着处理。");
          if (data.phase === "needs_attention") alerts.push("调度器停在需要处理的状态，暂时不会自动继续。请查看下方文献的阶段，或让后端检查入库日志。");
          if (data.phase === "waiting_for_subscription_reset") alerts.push("Codex 订阅额度已用完，额度恢复后会自动接着处理同一批文献。");
          if (scoped && data.global_queue_paused) alerts.push("当前只处理指定的文献，完整队列仍保持暂停。");
          $("[data-native-alerts]", panel).innerHTML = alerts.map(text => `<p class="notice">${esc(text)}</p>`).join("");
          const current = $("[data-native-current]", panel);
          current.innerHTML = [["正式入库", counts.indexing], ["预处理与复核", counts.preflight]].map(([title, rows]) =>
            rows.length ? `<div class="native-section-head"><h3>${title} · ${rows.length} 篇</h3></div>
            <ul class="native-progress-grid">${rows.map(nativePaperCard).join("")}</ul>` : "").join("");
          const ready = $("[data-ready]", panel);
          ready.hidden = !counts.ready.length;
          $("summary", ready).textContent = `已验收，待正式入库（${counts.ready.length} 篇）`;
          $("ul", ready).innerHTML = counts.ready.map(row => `<li><div>${esc(row.title)}<small>${esc(paperDetails(row))} ${regionLink(row)}</small></div></li>`).join("");
          const review = $("[data-review]", panel);
          review.hidden = !counts.review.length;
          $("h3", review).textContent = `待复核 · ${counts.review.length} 篇`;
          $("ul", review).innerHTML = counts.review.map(row => `<li><div>${esc(row.title)}<small>${esc(paperDetails(row))}</small></div>${regionLink(row, "查看待复核项")}</li>`).join("");
          const waiting = $("[data-waiting]", panel);
          const rows = counts.queued;  // catalogue order, as the scheduler reports it
          waiting.hidden = !rows.length;
          const resumable = rows.filter(row => row.stage !== "queued").length;
          $("summary", waiting).textContent = `排队中的文献（${rows.length} 篇${resumable ? `，其中 ${resumable} 篇已有进度` : ""}）`;
          const fillWaiting = () => {
            if (waiting.open) $("ul", waiting).innerHTML = rows.map(row => `<li><div>${esc(row.title)}<small>${esc(paperDetails(row))} ${regionLink(row)}</small></div><span>${esc(nativeStage[row.stage] || "排队中")}</span></li>`).join("");
          };
          waiting.ontoggle = fillWaiting;
          fillWaiting();
          if (data.max_concurrency) {
            const edited = savedWidth != null && Number(width.value) !== savedWidth;
            if (width.options.length !== data.max_concurrency) {
              width.innerHTML = Array.from({length:data.max_concurrency}, (_,i) => `<option value="${i+1}">${i+1} 篇</option>`).join("");
            }
            savedWidth = data.concurrency;
            if (!edited) width.value = String(savedWidth);
            form.hidden = false;
            syncWidth();
            $("[data-width-note]", form).textContent = data.legacy_runner
              ? `已设 ${savedWidth} 篇。当前队列使用旧版调度器，暂停后点“继续文献入库”即可应用。`
              : data.shared_workspace !== false ? `已设 ${savedWidth} 篇。从下一批开始生效。`
              : `已设 ${savedWidth} 篇。${["running","pausing","waiting_for_subscription_reset"].includes(data.phase) ? "后续调度按此上限补位。" : "下次入库时应用。"}`;
          } else form.hidden = true;
          const b = $("[data-queue-action]", panel);
          const running = ["running","waiting_for_subscription_reset"].includes(data.phase);
          b.dataset.queueAction = running ? "pause" : "resume";
          b.textContent = running ? "暂停并保存进度" : "继续文献入库";
          b.hidden = !running && (!data.pending || ["starting","pausing"].includes(data.phase));
        }
        if (["running", "starting", "pausing", "waiting_for_subscription_reset"].includes(data.phase)) timer = setTimeout(refresh, controls ? 5000 : 15000);
      } catch (err) {
        if (!disposed && id === refreshId) {
          $("[data-native-summary]", panel).textContent = err.message;
          $("[data-published]", panel).hidden = true;
          if (controls) {
            $("[data-queue-action]", panel).hidden = true;
            $("[data-native-alerts]", panel).replaceChildren();
            $("[data-native-current]", panel).replaceChildren();
            $("[data-review]", panel).hidden = true;
            $("[data-waiting]", panel).hidden = true;
            timer = setTimeout(refresh, 5000);
          }
        }
      } finally { if (id === refreshId) button.disabled = false; }
    };
    if (controls) {
      width.onchange = () => { $("[data-width-message]", form).textContent = ""; syncWidth(); };
      form.onsubmit = async (event) => {
        event.preventDefault();
        if (savingWidth || Number(width.value) === savedWidth) return;
        savingWidth = true;
        width.disabled = true;
        syncWidth();
        const feedback = $("[data-width-message]", form);
        try {
          const result = await api("/api/native/queue/settings", {method:"POST", headers:{"Content-Type":"application/json"},
            body:JSON.stringify({documents:Number(width.value)})});
          // A status read started before the save may still hold the old width.
          await nativeStatusRequests["/api/native/status?progress=true"]?.catch(() => {});
          savedWidth = result.documents;
          width.value = String(savedWidth);
          feedback.textContent = `已保存为 ${savedWidth} 篇。`;
          if (!disposed) await refresh();
        } catch (error) { feedback.textContent = error.message; }
        finally { savingWidth = false; width.disabled = false; syncWidth(); }
      };
      const b = $("[data-queue-action]", panel), message = $("[data-queue-message]", panel);
      b.onclick = async () => {
        const action = b.dataset.queueAction;
        b.disabled = true;
        try {
          await api("/api/native/queue", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({action})});
          message.textContent = action === "resume" ? "已请求继续，正在读取检查点。" : "已请求暂停，正在保存进度。";
          if (!disposed) await refresh();
        } catch (error) { message.textContent = error.message; }
        finally { b.disabled = false; }
      };
    }
    $("button", panel).onclick = refresh;
    panel.dispose = () => { disposed = true; clearTimeout(timer); };
    refresh();
    return panel;
  }
  async function regionGallery(docId) {
    const el = h(`<div class="page region-gallery"><a href="${modeHref("ingestion")}">‹ 返回文献入库</a>
      <div class="page-head"><div><h1>入库裁图</h1><p data-region-title></p></div></div>
      <div data-region-body></div></div>`);
    const body = $("[data-region-body]", el);
    const base = `/api/native/documents/${encodeURIComponent(docId)}/regions`;
    try {
      const data = await api(base);
      $("[data-region-title]", el).textContent = data.title;
      if (data.crop_review) {
        const review = document.createElement("p");
        review.textContent = "模型已审核 PDF 第 " + data.crop_review.selected_pages.join("、") + " 页。";
        for (const finding of data.crop_review.held) {
          const line = document.createElement("span");
          line.textContent = (finding.page ? "第 " + finding.page + " 页 · " : "") + (finding.detail || finding.reason);
          review.append(document.createElement("br"), line);
        }
        body.before(review);
      }
      const skipped = data.skipped || [];
      if (!data.items.length && !skipped.length) { body.textContent = "本次解析没有收录图、表或公式裁图。"; return el; }
      const newlyRecognized = data.items.filter(r => r.current_decoration).length;
      const allItems = [...data.items, ...skipped];
      const labels = {image:"图片", table:"表格", equation:"公式"};
      body.innerHTML = `<div class="region-filter"><div class="segmented" role="group" aria-label="过滤结果">
        <button type="button" data-region-group="kept"></button><button type="button" data-region-group="filtered"></button></div>
        ${newlyRecognized ? `<label><input type="checkbox" data-filter-preview>预览按现行规则补过滤 ${newlyRecognized} 项的结果</label>` : ""}
        </div><div data-region-pages></div>`;
      const groupButtons = [...body.querySelectorAll("[data-region-group]")];
      const preview = $("[data-filter-preview]", body);
      let selected = data.items.length ? "kept" : "filtered";
      const render = () => {
        const previewing = Boolean(preview?.checked);
        const extra = previewing ? newlyRecognized : 0;
        const counts = {kept:data.items.length-extra, filtered:skipped.length+extra};
        const names = previewing ? {kept:"将保留", filtered:"将过滤"} : {kept:"未过滤", filtered:"已过滤"};
        groupButtons.forEach(button => {
          const group = button.dataset.regionGroup;
          button.textContent = `${names[group]}（${counts[group]}）`;
          button.classList.toggle("on", group === selected);
          button.setAttribute("aria-pressed", String(group === selected));
        });
        const pages = new Map();
        allItems.forEach((item, index) => {
          const filtered = index >= data.items.length || (previewing && Boolean(item.current_decoration));
          if (filtered !== (selected === "filtered")) return;
          if (!pages.has(item.page)) pages.set(item.page, []);
          const img = `${base}/${encodeURIComponent(item.sha256)}.png`;
          const name = `${labels[item.kind]} · 裁图 ${index+1}`;
          const provenance = {archived_image:"旧版保存的原图", pdf_crop:"按旧版坐标从原 PDF 补绘",
            pmc_figure:"同版 PMC 独立图文件", pdffigures2:"按 PDFFigures 2 坐标从原页补裁",
            pdffigures2_pdf_image:"按原页完整图像边界校正二次裁图",
            pdf_form:"按原 PDF 内嵌图框补裁"}[item.preview_source];
          pages.get(item.page).push(`<figure><a href="${img}" target="_blank" rel="noopener" title="打开原尺寸裁图">
            <img loading="lazy" alt="${esc(name)}，PDF 第 ${item.page} 页" src="${img}"></a>
            <figcaption><strong>${esc(name)}</strong>${provenance ? `<small>${esc(provenance)}</small>` : ""}
            ${item.reason ? `<p>已过滤 · ${esc(item.reason)}</p>` : item.current_decoration ? `<p>入库时保留，现可过滤 · ${esc(item.current_decoration)}</p>` : ""}
            ${item.caption ? `<p>${esc(item.caption)}</p>` : ""}</figcaption></figure>`);
        });
        $("[data-region-pages]", body).innerHTML = [...pages.entries()].sort(([a],[b]) => a-b).map(([page, figures]) =>
          `<section class="region-page"><header><h2>PDF 第 ${page} 页</h2><a href="/api/papers/${encodeURIComponent(docId)}/pdf?revision=${encodeURIComponent(data.revision)}#page=${page}" target="_blank" rel="noopener">查看原页 ↗</a></header>
          <div class="region-grid">${figures.join("")}</div></section>`).join("") || `<p role="status">“${names[selected]}”中没有裁图。</p>`;
      };
      groupButtons.forEach(button => {
        button.onclick = () => { selected = button.dataset.regionGroup; render(); };
      });
      if (preview) preview.onchange = render;
      render();
    } catch (err) { body.textContent = err.message; }
    return el;
  }
  function ingestionView(params) {
    const el = document.createElement("div");
    if (arch === "demo") {
      disposeIngestion = window.KGIngestion.mount(el, { onCompleted: refreshLibrary });
      return el;
    }
    if (params.get("regions")) return regionGallery(params.get("regions"));
    el.className = "page";
    el.innerHTML = `<div class="page-head"><div><h1>文献入库</h1><p>RAG-Anything 按批处理清单中的 PDF：先审核裁图、解析页面，再同批建立索引，核验通过后一起发布到检索库。</p></div></div>`;
    const panel = nativePanel(true);
    el.append(panel, h(`<div class="native-links"><a href="${modeHref("search")}">检索已发布文献</a><span>继续入库时，Codex 订阅会按清单顺序处理剩余 PDF；已有进度的文献从检查点接着做。</span></div>`));
    el.dispose = panel.dispose;
    return el;
  }
  function ideasView() {
    const el = document.createElement("div");
    const openSource = (source) => openChatSource(source, "创新候选");
    const session = chatSession();
    disposeIdeas = window.KGInnovation.mount(el, {
      request: api, icon, copyText, toast, openSource,
      arch, channel, configHref: modeHref("ask"), channelPicker,
      appendCitedText: (container, text, sources) => appendCitedText(container, text, sources, openSource),
      getChatHistory: () => session.turns.slice(-CHAT_HISTORY_LIMIT).flatMap((turn) => [
        { role: "user", content: turn.question }, { role: "assistant", content: turn.answer },
      ]),
    });
    return el;
  }
  function reportView() {
    if (arch === "demo") {
      const el = window.KGReport.mount(document.createElement("div"), { summary: state.summary, icon });
      el.querySelectorAll("[data-app]").forEach(link => link.href = modeHref(link.dataset.app));
      return el;
    }
    const docs = state.native.documents;
    const el = h(`<div class="page report-page"><header class="page-head"><div><h1>项目概况</h1><p>RAG-Anything · 发布版本 ${state.native.generation}</p></div></header></div>`);
    const panel = nativePanel();
    el.append(panel, h(`<section class="card native-panel"><h2>已发布索引</h2><p>${docs.reduce((n,d) => n+d.pages, 0)} 页 PDF · ${docs.reduce((n,d) => n+d.chunks, 0)} 个图文索引片段</p>
      <h3>从文献到回答</h3><p>RAG-Anything 按原始 PDF 页组织文本、图表与公式，LightRAG 保存片段、实体与关系。提问时由本机 BGE-M3 召回片段，再定位原页，用所选接口生成并核对引用。</p>
      <div class="native-links"><a href="${modeHref("library")}">浏览已发布文献</a><a href="${modeHref("claims")}">查看实体与关系</a></div></section>`));
    el.dispose = panel.dispose;
    return el;
  }
  const routes = { ask: askView, ideas: ideasView, library: libraryView, claims: claimsView, ingestion: ingestionView, search: searchView, report: reportView };
  const ROUTE_LABELS = { ask: "论文问答", ideas: "创新点生成", library: "文献库", claims: "知识记录", ingestion: "文献入库", search: "全文检索", report: "项目概况" };
  // Older links (#/overview, #/papers, #/graph) all land on the library.
  const ROUTE_ALIASES = { overview: "library", papers: "library", graph: "library" };
  let viewKeyHandler = null;
  let routeVersion = 0;

  function moveNavIndicator() {
    const active = $("#nav a.active"), bar = $(".nav-indicator");
    if (!active || !bar) return;
    bar.style.width = active.offsetWidth + "px";
    bar.style.height = active.offsetHeight + "px";
    bar.style.transform = `translate(${active.offsetLeft}px, ${active.offsetTop}px)`;
    requestAnimationFrame(() => bar.classList.add("ready"));
    active.scrollIntoView({ block: "nearest", inline: "nearest" });
  }
  window.addEventListener("resize", moveNavIndicator);

  async function route() {
    const version = ++routeVersion;
    const leavingIngestion = !!disposeIngestion;
    if (disposeIngestion) { disposeIngestion(); disposeIngestion = null; }
    if (disposeIdeas) { disposeIdeas(); disposeIdeas = null; }
    const [path, query] = location.hash.replace(/^#\/?/, "").split("?");
    const params = new URLSearchParams(query || "");
    const previousArch = arch;
    if (Object.hasOwn(archNames, params.get("arch"))) arch = params.get("arch");
    else if (params.get("source") === "api" || (path === "papers" && params.get("read") === "markdown")) arch = "demo";
    if (["api","subscription"].includes(params.get("channel"))) channel = params.get("channel");
    else if (params.get("source") === "api") channel = "api";
    try { localStorage.setItem("kg-arch", arch); localStorage.setItem("kg-channel", channel); } catch (_) {}
    const name = routes[path] ? path : ROUTE_ALIASES[path] || "ask";
    const canonical = modeHref(path || "ask", params);
    if (location.hash !== canonical) history.replaceState(null, "", canonical);
    $("#workspace-arch").value = arch;
    $("#workspace-arch").onchange = e => switchMode(e.target.value, channel);
    document.querySelectorAll(".workspace-options button").forEach(b => {
      b.setAttribute("aria-pressed", String(b.dataset.arch === arch));
      b.onclick = () => { if (b.dataset.arch !== arch) switchMode(b.dataset.arch, channel); };
    });
    $("#nav").querySelectorAll("[data-route]").forEach(a => a.href = modeHref(a.dataset.route));
    $(".brand").href = modeHref("ask");
    const navName = name === "claims" ? "library" : name;
    document.querySelectorAll("#nav a").forEach((a) => a.classList.toggle("active", a.dataset.route === navName));
    moveNavIndicator();
    document.title = `${ROUTE_LABELS[name]} · ${archNames[arch]} · 混合键合知识图谱`;
    view.firstElementChild?.dispose?.();
    closeDrawer();
    viewKeyHandler = null;
    view.innerHTML = '<div class="page"><div class="loading">正在载入…</div></div>';
    renderSidebarSummary();
    try {
      if (arch === "raganything" && name !== "ingestion") await readNativeStatus();
      if (version !== routeVersion) return;
      if (arch === "demo" && (leavingIngestion || !state.summary || previousArch !== arch)) await refreshLibrary();
      if (version !== routeVersion) return;
      if (arch === "demo" && name !== "ingestion" && !state.summary) {
          view.innerHTML = '<div class="page"><div class="notice"><span>文献库正在更新或暂不可读取。<a href="#/ingestion?source=api" data-ingestion-recovery-link>查看旧库批次与恢复</a>，完成后可重新打开本页。</span></div></div>';
          return;
      }
      // A #/papers link carries reading options (doc, read, page), not library filters.
      const pageParams = path === "papers" ? new URLSearchParams() : params;
      const el = arch === "raganything" && ["library","claims"].includes(name) ?
        nativeLibraryView(pageParams, name === "claims") : await routes[name](pageParams);
      if (version !== routeVersion) { el.dispose?.(); return; }
      el.classList.add("page-enter");
      view.replaceChildren(el);
      el.afterMount?.();
      view.scrollTop = 0;
      if (path === "papers") {
        const params = new URLSearchParams(query || "");
        const docId = params.get("doc");
        if (docId && paperFor(docId)) {
          const page = Number(params.get("page"));
          openPaper(docId, null, { mode: params.get("read") || undefined,
            page: Number.isInteger(page) && page > 0 ? page : 1 });
        }
      }
    } catch (err) {
      if (version !== routeVersion) return;
      view.innerHTML = `<div class="page"><div class="notice">${icon("warn")}${esc(err.message)}</div></div>`;
    }
  }

  /* ---------- theme ---------- */
  const THEMES = { auto: ["跟随系统", "auto"], light: ["浅色", "sun"], dark: ["深色", "moon"] };
  function currentTheme() { return document.documentElement.dataset.theme || "auto"; }
  function renderThemeToggle() {
    const [label, iconName] = THEMES[currentTheme()];
    $("#theme-toggle").innerHTML = `${icon(iconName)}<span>主题：${label}</span>`;
    $("#theme-toggle").title = "切换主题";
  }
  $("#theme-toggle").onclick = () => {
    const order = ["auto", "light", "dark"];
    const next = order[(order.indexOf(currentTheme()) + 1) % order.length];
    if (next === "auto") delete document.documentElement.dataset.theme; else document.documentElement.dataset.theme = next;
    try { next === "auto" ? localStorage.removeItem("kg-theme") : localStorage.setItem("kg-theme", next); } catch (_) { /* storage unavailable */ }
    renderThemeToggle();
    toast(`已切换为${THEMES[next][0]}主题`, THEMES[next][1]);
  };
  renderThemeToggle();

  /* ---------- global shortcuts ---------- */
  document.addEventListener("keydown", (e) => {
    const typing = e.target.closest?.("input, textarea, select, [contenteditable]");
    if (e.key === "Escape") {
      if (drawer.classList.contains("open")) { closeDrawer(); return; }
      if (typing) { e.target.blur(); return; }
    }
    if (typing || e.ctrlKey || e.metaKey || e.altKey) return;
    if (drawer.classList.contains("open")) {
      if (e.key === "ArrowLeft") { e.preventDefault(); stepDrawer(-1); }
      if (e.key === "ArrowRight") { e.preventDefault(); stepDrawer(1); }
      return;
    }
    if (e.key === "/") {
      const target = view.querySelector("[data-page-search]") || view.querySelector("textarea");
      if (target) { e.preventDefault(); target.focus(); target.select?.(); }
      return;
    }
    if (viewKeyHandler?.(e)) e.preventDefault();
  });

  let taxonomyPending = false;
  function loadTaxonomy() {
    if (taxonomyPending || state.taxonomy) return;
    taxonomyPending = true;
    // Classification is optional: its failure or delay must not block questions,
    // the overview, or the existing catalog. No model request is involved.
    api("/api/taxonomy").then((taxonomy) => {
      if (!Array.isArray(taxonomy?.categories) || !Array.isArray(taxonomy?.facets) ||
        !taxonomy.categories.every((category) => category && typeof category.id === "string" && typeof category.label === "string") ||
        !taxonomy.facets.every((facet) => facet && typeof facet.id === "string" && Array.isArray(facet.options) &&
          facet.options.every((option) => option && typeof option.value === "string" && typeof option.label === "string"))) {
        throw new Error("分类数据格式不完整");
      }
      state.taxonomy = taxonomy;
    }).catch(() => { state.taxonomyError = "工艺分类暂不可用"; }).finally(() => {
      taxonomyPending = false;
      if (arch === "demo" && state.summary && !refreshingLibrary && /^#\/?(library|papers|overview|graph)(?:\?|$)/.test(location.hash)) route();
    });
  }

  function renderSidebarSummary() {
    $("#sidebar-foot").innerHTML = arch === "raganything"
      ? `<p>RAG-Anything · ${state.native ? `<b>${state.native.published}</b> 篇已发布` : "正在读取索引"}</p><a href="${modeHref("ingestion")}">查看发布进度</a>`
      : state.summary ? `<p>文献目录 · <b>${state.summary.papers}</b> 篇</p><p>知识记录 · <b>${state.summary.claims}</b> 条</p>` : "<p>正在读取 Demo 目录</p>";
  }

  window.addEventListener("hashchange", route);
  route();
})();
