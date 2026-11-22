/* Real-DOM, isolated headless-browser regression; all API responses are mocked.
 * Run: node tests/test_classification_ui.cjs. No backend, model, or key is used.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { spawn } = require("node:child_process");
const root = path.resolve(__dirname, "..");
const browserPath = [process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe",
  "/usr/bin/chromium", "/usr/bin/google-chrome"].find((candidate) => candidate && fs.existsSync(candidate));
assert(browserPath, "Install Chrome/Chromium or set CHROME_PATH.");
fs.mkdirSync(path.join(root, ".cache"), { recursive: true });
const profile = fs.mkdtempSync(path.join(root, ".cache", "classification-browser-"));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const browser = spawn(browserPath, ["--headless=new", "--disable-gpu", "--no-first-run",
  "--no-default-browser-check", "--disable-background-networking", "--disable-component-update",
  "--disable-breakpad", "--disable-crash-reporter", "--host-resolver-rules=MAP * ~NOTFOUND",
  "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"],
  { windowsHide: true, stdio: "ignore" });
let socket;
let spawnError;
browser.on("error", (error) => { spawnError = error; });

async function run(html, source, css, mode) {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const same = (actual, expected, message) => check(JSON.stringify(actual) === JSON.stringify(expected),
    `${message}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
  const settle = async (predicate) => {
    for (let i = 0; i < 100; i += 1) {
      if (predicate()) return;
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    throw new Error("UI did not reach expected state.");
  };
  document.open(); document.write(html); document.close();
  const style = document.createElement("style"); style.textContent = css; document.head.append(style);
  const errors = [];
  window.addEventListener("error", (event) => errors.push(event.message));
  window.addEventListener("unhandledrejection", (event) => errors.push(String(event.reason)));
  const categories = [["structure_fabrication", "结构形成"], ["surface_planarization", "表面平坦化"],
    ["cleaning_activation", "清洗与活化"], ["alignment_contact", "对准与接触"],
    ["post_bond_annealing", "键合后退火"], ["post_bond_processing", "键合后加工"],
    ["die_preparation_handling", "芯片制备与操作"], ["characterization_evaluation", "表征与评价"],
    ["integrated_flow", "集成流程"]].map(([id, label], index) => ({ id, label,
      group: index < 6 ? "process" : "support", description: `${label}的相关文献` }));
  const facet = (id, label, values, multiple = true) => ({ id, label, multiple,
    options: values.map(([value, name]) => ({ value, label: name, count: 0 })) });
  const taxonomy = { version: "mock-v1", total: 5, categories, category_counts: {}, related_category_counts: {}, facets: [
    facet("routes", "材料路线", [["inorganic", "无机混合键合"], ["polymer", "聚合物混合键合"], ["__unspecified__", "未明确"]]),
    facet("materials", "材料", [["cu", "铜"], ["pi", "聚酰亚胺"], ["__unspecified__", "未明确"]]),
    facet("topics", "主题", [["surface", "表面"], ["thermal", "热处理"]]),
    facet("methods", "方法", [["plasma", "等离子体"], ["thermal", "热退火"], ["simulation", "仿真"], ["__unspecified__", "未明确"]]),
    facet("relevance", "相关性", [["direct", "直接相关"], ["context", "背景相关"]], false),
    facet("document_form", "资料形式", [["article", "期刊论文"], ["slides", "报告幻灯片"]], false),
    facet("content_nature", "内容性质", [["research", "研究"], ["review", "综述"]], false),
    facet("publication_status", "发表状态", [["published", "已发表"]], false),
    facet("access_route", "获取途径", [["open", "公开获取"]], false),
  ] };
  const classification = (primary, related, route, material, method, relevance = "direct") => ({
    primary_category: primary, related_stages: related, routes: route, materials: material,
    methods: method, topics: [method.includes("thermal") ? "thermal" : "surface"], relevance,
    document_form: "article", content_nature: "research", publication_status: "published", access_route: "open",
    classification_reason: "依据原文的表面处理实验划入此阶段。<script>bad()</script>",
    evidence: [{ pdf_page: 2, quote: "The surface was activated with plasma. <img src=x onerror=bad()>" }],
    processing: { private_implementation_marker: "DO_NOT_RENDER" }, classification_review: "internal_review_marker",
  });
  const papers = [
    { doc_id: "P1", title: "Copper bonding experiment", year: 2024, classification: classification("cleaning_activation", ["alignment_contact"], ["inorganic"], ["cu"], ["plasma"]) },
    { doc_id: "P2", title: "Copper bonding thermal study", year: 2023, classification: classification("post_bond_annealing", ["cleaning_activation"], ["inorganic"], ["cu"], ["thermal"]) },
    { doc_id: "P3", title: "Polymer bonding review", year: 2024, is_review: true, classification: { ...classification("cleaning_activation", [], ["polymer"], ["pi"], ["plasma"], "context"), content_nature: "review" } },
    { doc_id: "P4", title: "Bonding measurement", year: 2022, classification: { ...classification("characterization_evaluation", ["cleaning_activation"], [], [], [], "context"), document_form: "slides" } },
    { doc_id: "P5", title: "Unclassified bonding source", year: 2021 },
  ].map((paper) => ({ authors: ["Test Author"], topic: "hybrid bonding", source_type: "internal_source_code", doi: "", page_count: 3,
    claim_count: 0, has_pdf: true, is_review: false, ...paper }));
  papers[0].classification.keywords = ["铜凹陷", "细分界面应力"];
  papers[0].has_markdown = true;
  const json = (value, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
  const requests = [];
  let resolveTaxonomy;
  window.fetch = async (url, options) => {
    requests.push({ url, method: options?.method || "GET" });
    if (url === "/api/summary") return json({ papers: papers.length, claims: 0 });
    if (url === "/api/papers") return json(papers);
    if (url === "/api/claims") return json([]);
    if (url === "/api/papers/P1/markdown/rendered") return json({ doc_id: "P1", html:
      '<h2>PDF 第 2 页</h2><p><a href="/api/papers/P1/pdf#page=2" target="_blank" rel="noopener noreferrer" data-pdf-page="2">查看原 PDF 第 2 页</a></p><table><thead><tr><th>条件</th><th>数值</th></tr></thead><tbody><tr><td>Temperature</td><td>200 °C</td></tr></tbody></table>'
        + '<p>Representative source paragraph for the long-document reading area.</p>'.repeat(60) });
    if (url === "/api/taxonomy") {
      if (mode === "unavailable") return json({ detail: "mock offline" }, 503);
      if (mode === "delayed") return new Promise((resolve) => { resolveTaxonomy = () => resolve(json(taxonomy)); });
      return json(taxonomy);
    }
    if (url === "/api/chat/status") return json({ configured: true, provider: "mock", model: "mock" });
    if (url === "/api/native/status") return json({ published: 2, pending: 0, held: 0, catalog: 5, generation: 7, active: [],
      documents: [{ doc_id: "P1", title: "Copper bonding experiment", pages: 3, chunks: 11, pdf_sha256: "a" },
                  { doc_id: "P3", title: "Polymer bonding review", pages: 3, chunks: 4, pdf_sha256: "b" }] });
    throw new Error(`Unexpected request: ${url}`);
  };
  // The library lists each paper once per group it belongs to; ids() keeps first-appearance order.
  const ids = () => [...new Set([...document.querySelectorAll(".lib-paper[data-doc]")].map((row) => row.dataset.doc))];
  const input = (selector, value) => {
    const element = document.querySelector(selector); element.value = value;
    element.dispatchEvent(new Event("input", { bubbles: true }));
  };
  const layer = (id, value) => {
    const element = document.querySelector(`[data-layer="${id}"]`); element.value = value;
    element.dispatchEvent(new Event("change", { bubbles: true }));
  };
  const cell = (a, b) => document.querySelector(`.lib-cell[data-pick-a="${a}"][data-pick-b="${b}"]`).click();
  const rowLabels = () => [...document.querySelectorAll(".lib-matrix tbody th")].map((th) => th.textContent.trim());
  location.hash = mode === "delayed" ? "#/ask?arch=demo" : "#/library?arch=demo";
  (0, eval)(source);
  if (mode === "delayed") {
    await settle(() => document.querySelector(".chat-composer"));
    check(!document.querySelector(".lib-matrix"), "Delayed classification blocked the original page.");
    resolveTaxonomy();
    location.hash = "#/library?arch=demo";
    await settle(() => document.querySelectorAll('[data-layer="l1"] option').length === 8);
    same(ids(), ["P1", "P3", "P2", "P4", "P5"], "Catalog after delayed taxonomy");
    return { mode, passed: true, checks: ["optional taxonomy does not block questions"] };
  }
  await settle(() => document.querySelector(".lib-matrix tbody tr"));
  if (mode === "unavailable") {
    await settle(() => document.querySelector(".library-page .notice")?.textContent.includes("暂不可用"));
    same([...document.querySelectorAll('[data-layer="l1"] option')].map((o) => o.value), ["period"], "Only year layering without taxonomy");
    same(ids(), ["P4", "P5", "P1", "P3", "P2"], "Fallback catalog by period");
    document.querySelector('.lib-paper[data-doc="P5"]').click();
    check(document.querySelector(".classification-detail").textContent.includes("尚未分类"), "Missing classification should be explicit.");
    check(document.querySelector('#drawer-body a[href="/api/papers/P5/pdf"]'), "Fallback lost original PDF.");
    location.hash = "#/ask?arch=demo";
    await settle(() => document.querySelector(".chat-composer"));
    check(errors.length === 0, `Fallback errors: ${errors.join(", ")}`);
    return { mode, passed: true, checks: ["taxonomy failure", "period fallback", "missing classification", "original PDF", "questions unaffected"] };
  }
  // Default layering: primary category × route; zero-count values stay visible, support stages set apart.
  same(rowLabels(), ["结构形成", "表面平坦化", "清洗与活化", "对准与接触", "键合后退火", "键合后加工", "芯片制备与操作", "表征与评价", "集成流程", "未标注"], "Category rows include zero counts");
  check(document.querySelector('tr[data-row="die_preparation_handling"]').classList.contains("lib-break"), "Support stages should be set apart.");
  const zeroRow = document.querySelector('tr[data-row="structure_fabrication"]');
  check(zeroRow.classList.contains("zero") && zeroRow.querySelector("th button").disabled && zeroRow.querySelector(".lib-total b").textContent === "0", "Zero row should show 0 and not be selectable.");
  same([...document.querySelectorAll(".lib-col")].map((th) => th.dataset.col), ["inorganic", "polymer", "__unspecified__"], "Route columns in taxonomy order, one unlabelled column last");
  check(document.querySelector('.lib-col[data-col="__unspecified__"] small').textContent === "2", "Unlabelled column should merge the backend's 未明确 option.");
  layer("l2", "methods");
  same([...document.querySelectorAll(".lib-col")].map((th) => [th.dataset.col, th.querySelector("small").textContent]), [["plasma", "2"], ["thermal", "1"], ["simulation", "0"], ["__unspecified__", "2"]], "Methods columns keep zero-count options");
  check(document.querySelector('.lib-col[data-col="simulation"]').classList.contains("zero"), "Zero column should be marked.");
  layer("l2", "topics");
  check(document.querySelector('.lib-col[data-col="thermal"].zero') === null && document.querySelectorAll(".lib-col").length === 3, "Topic columns");
  layer("l2", "routes");
  check(document.querySelectorAll(".lib-group-empty").length === 6, "Empty first-layer groups should stay listed.");
  check(document.querySelector('.lib-col[data-col="inorganic"] small').textContent === "2", "Column total missing.");
  check(document.querySelector(".lib-note").textContent.includes("多个分组"), "Multi-valued note missing.");
  same(ids(), ["P1", "P3", "P2", "P4", "P5"], "Layered list order");
  cell("cleaning_activation", "inorganic"); same(ids(), ["P1"], "Cell selection");
  check(location.hash.includes("a=cleaning_activation") && location.hash.includes("b=inorganic"), "Selection not kept in the URL.");
  cell("cleaning_activation", "inorganic"); same(ids(), ["P1", "P3", "P2", "P4", "P5"], "Second click clears the cell");
  document.querySelector('.lib-col [data-pick-b="polymer"]').click(); same(ids(), ["P3"], "Column selection");
  document.querySelector("[data-lib-clear]").click();
  document.querySelector('.lib-group-head [data-pick-a="cleaning_activation"]').click(); same(ids(), ["P1", "P3"], "Row selection from group head");
  document.querySelector("[data-lib-clear]").click();
  input("[data-lib-q]", "细分界面应力"); same(ids(), ["P1"], "Detailed keywords remain searchable");
  input("[data-lib-q]", "no-such-paper"); check(document.querySelector(".lib-groups .empty"), "No-result message missing.");
  input("[data-lib-q]", "");
  layer("l1", "period"); layer("l2", "");
  same(rowLabels(), ["2009 年及以前", "2010–2014", "2015–2019", "2020–2022", "2023–2024", "2025 年以来"], "Period rows include zero counts");
  same(ids(), ["P4", "P5", "P1", "P3", "P2"], "Single-layer list");
  check(!document.querySelector(".lib-col"), "No columns without a second layer.");
  layer("l1", "methods"); layer("l2", "methods");
  check(document.querySelector('[data-layer="l2"]').value === "", "Second layer cannot repeat the first.");
  layer("l1", "category"); layer("l2", "routes");
  location.hash = "#/ask?arch=demo";
  await settle(() => document.querySelector(".chat-composer"));
  location.hash = "#/library?l1=document_form&l2=access_route&arch=demo";
  await settle(() => document.querySelector('[data-layer="l1"]')?.value === "document_form");
  check(document.querySelector('[data-layer="l2"]').value === "", "Unknown second layer should fall back to a single layer.");
  same(rowLabels(), ["期刊论文", "报告幻灯片", "未标注"], "Layer from URL");
  layer("l1", "relevance"); layer("l2", "");
  same(rowLabels(), ["直接相关", "背景相关", "未标注"], "Relevance layer");
  document.querySelector('.lib-matrix [data-pick-a="context"]').click(); same(ids(), ["P3", "P4"], "Relevance selection");
  document.querySelector("[data-lib-clear]").click();
  layer("l1", "category");
  document.querySelector('.lib-paper[data-doc="P1"]').click();
  await settle(() => document.querySelector(".markdown-content table"));
  check(!document.querySelector(".markdown-content").hidden, "Markdown should be the initial reading mode when available.");
  check(document.querySelector(".markdown-content table").textContent.includes("200 °C"), "Markdown table cells missing.");
  check(document.querySelector(".classification-detail").compareDocumentPosition(document.querySelector(".paper-reader")) & Node.DOCUMENT_POSITION_FOLLOWING,
    "Full reading should follow the classification and knowledge sections.");
  check(document.querySelector(".markdown-content").scrollHeight > document.querySelector(".markdown-content").clientHeight,
    "Long Markdown should scroll in a bounded reading region.");
  check(document.querySelector('a[href="/api/papers/P1/markdown"][download]'), "Markdown download missing.");
  check(document.querySelector('[data-reader-share]').getAttribute("href") === "#/papers?doc=P1&read=markdown", "Shareable reading URL missing.");
  document.querySelector(".markdown-content [data-pdf-page]").click();
  check(document.querySelector(".viewer-bar input").value === "2", "Markdown link did not select the physical PDF page.");
  check(!document.querySelector("[data-reader-pdf]").hidden, "Markdown page click should reveal PDF.");
  check(document.querySelector('.viewer [data-a="open"]').getAttribute("href") === "/api/papers/P1/pdf#page=2", "Markdown original-PDF href lost physical page.");
  document.querySelector("[data-read-markdown]").click();
  check(!document.querySelector(".markdown-content").hidden, "Prominent reading entry should return to Markdown.");
  check(document.querySelector(".classification-primary").textContent.includes("清洗与活化"), "Readable primary category missing.");
  const reason = document.querySelector(".classification-reason");
  check(reason && !reason.open, "Classification evidence should start collapsed.");
  reason.open = true;
  check(reason.textContent.includes("依据原文的表面处理实验"), "Classification reason missing.");
  check(reason.querySelector(".classification-keywords").textContent.includes("铜凹陷"), "Detailed keywords should remain in the folded detail.");
  check(!reason.querySelector("script,img"), "Classification text must remain inert.");
  check(!document.querySelector("#drawer-body").textContent.includes("DO_NOT_RENDER"), "Implementation processing leaked.");
  check(!document.querySelector("#drawer-body").textContent.includes("internal_review_marker"), "Review metadata leaked.");
  check(!document.querySelector("#drawer-body").textContent.includes("internal_source_code"), "Legacy source code should be hidden for classified papers.");
  document.querySelector("[data-classification-page]").click();
  check(document.querySelector(".viewer-bar input").value === "2", "Evidence did not select actual PDF page.");
  check(document.querySelector('.viewer [data-a="open"]').getAttribute("href") === "/api/papers/P1/pdf#page=2", "Evidence PDF link lost the physical page.");
  check(document.querySelector('#drawer-body a[href="/api/papers/P1/pdf"]'), "Original PDF entry missing.");
  document.querySelector("#drawer-close").click();
  location.hash = "#/papers?doc=P1&read=markdown";
  await settle(() => document.querySelector("#drawer.open .markdown-content table"));
  check(!document.querySelector(".markdown-content").hidden, "Shareable URL failed to open Markdown reading.");
  document.querySelector("#drawer-close").click();
  // RAG-Anything reuses the layered library, limited to its published documents and counting index chunks.
  location.hash = "#/library?arch=raganything&channel=subscription";
  await settle(() => document.querySelector(".lib-matrix") && location.hash.includes("arch=raganything") && ids().length === 2);
  same(ids(), ["P1", "P3"], "RAG-Anything library lists only published documents");
  check(document.querySelector('.lib-paper[data-doc="P1"] .lib-paper-claims').textContent.includes("11"), "Index chunk count missing.");
  check(document.querySelector(".page-head p").textContent.includes("2 篇已发布文献"), "RAG-Anything library headline missing.");
  check(document.querySelector(".lib-tabs a:last-child").textContent.includes("实体与关系"), "RAG-Anything second tab should be the entity view.");
  location.hash = "#/library?arch=demo&channel=subscription";
  await settle(() => ids().length === 5);
  check(errors.length === 0, `Browser errors: ${errors.join(", ")}`);
  check(requests.every((request) => request.method === "GET"), "UI regression must not call a model.");
  return { mode, passed: true, checks: ["category x route matrix", "zero counts shown", "support stages set apart", "unspecified last",
    "multi-valued note", "cell/row/column selection", "URL layer state", "keyword search", "single layer",
    "classification reason", "inert text", "evidence physical page", "original PDF", "Markdown table",
    "Markdown physical PDF page", "Markdown download and share URL", "no model calls"] };
}

(async () => {
  try {
    const portFile = path.join(profile, "DevToolsActivePort");
    for (let i = 0; !fs.existsSync(portFile); i += 1) {
      if (spawnError) throw spawnError;
      assert(i < 150 && browser.exitCode === null, "Headless browser did not start.");
      await sleep(100);
    }
    const [port, endpoint] = fs.readFileSync(portFile, "utf8").trim().split(/\r?\n/);
    socket = new WebSocket(`ws://127.0.0.1:${port}${endpoint}`);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", reject, { once: true });
    });
    const pending = new Map(); let nextId = 0;
    socket.addEventListener("message", (event) => {
      const result = JSON.parse(event.data), handler = pending.get(result.id);
      if (!handler) return;
      pending.delete(result.id); clearTimeout(handler.timeout);
      result.error ? handler.reject(new Error(JSON.stringify(result.error))) : handler.resolve(result.result);
    });
    const call = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
      const id = ++nextId;
      const timeout = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out: ${method}`)); }, 20000);
      pending.set(id, { resolve, reject, timeout });
      socket.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
    });
    const html = fs.readFileSync(path.join(root, "web/static/index.html"), "utf8")
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").replace(/<link\b[^>]*>/gi, "");
    const source = fs.readFileSync(path.join(root, "web/static/app.js"), "utf8");
    const css = fs.readFileSync(path.join(root, "web/static/styles.css"), "utf8");
    for (const mode of ["normal", "unavailable", "delayed"]) {
      const { targetId } = await call("Target.createTarget", { url: "about:blank" });
      const { sessionId } = await call("Target.attachToTarget", { targetId, flatten: true });
      await call("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1100, deviceScaleFactor: 1, mobile: false }, sessionId);
      await call("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] }, sessionId);
      const result = await call("Runtime.evaluate", { awaitPromise: true, returnByValue: true,
        expression: `(${run})(${JSON.stringify(html)},${JSON.stringify(source)},${JSON.stringify(css)},${JSON.stringify(mode)})` }, sessionId);
      assert(!result.exceptionDetails, result.exceptionDetails?.exception?.description || result.exceptionDetails?.text);
      assert.equal(result.result.value?.passed, true);
      console.log(JSON.stringify(result.result.value));
      if (mode === "normal" && process.env.UI_SCREENSHOTS) {
        await call("Runtime.evaluate", { expression: 'document.querySelector("#drawer-close").click(); location.hash = "#/library"' }, sessionId);
        for (const [name, width, height] of [["desktop", 1440, 1100], ["mobile", 390, 844]]) {
          await call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 }, sessionId);
          await call("Runtime.evaluate", { awaitPromise: true, expression: 'new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))' }, sessionId);
          const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false }, sessionId);
          fs.writeFileSync(path.join(root, ".cache", `library-ui-${name}.png`), Buffer.from(screenshot.data, "base64"));
        }
        await call("Runtime.evaluate", { awaitPromise: true,
          expression: 'document.querySelector(\'.lib-paper[data-doc="P1"]\').click(); new Promise(resolve => setTimeout(resolve, 50))' }, sessionId);
        for (const [name, width, height] of [["desktop", 1440, 1100], ["mobile", 390, 844]]) {
          await call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 }, sessionId);
          await call("Runtime.evaluate", { awaitPromise: true,
            expression: 'document.querySelector(".paper-reader").scrollIntoView({block:"start"}); new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))' }, sessionId);
          const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false }, sessionId);
          fs.writeFileSync(path.join(root, ".cache", `markdown-ui-${name}.png`), Buffer.from(screenshot.data, "base64"));
        }
      }
      await call("Target.closeTarget", { targetId });
    }
    await call("Browser.close");
  } finally {
    socket?.close(); browser.kill();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
