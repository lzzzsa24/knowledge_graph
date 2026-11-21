/* Real-DOM search regression with mocked HTTP only.
 * Run: node tests/test_search_ui.cjs. No backend, index or model is opened.
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
const profile = fs.mkdtempSync(path.join(root, ".cache", "search-browser-"));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const browser = spawn(browserPath, ["--headless=new", "--disable-gpu", "--no-first-run",
  "--no-default-browser-check", "--disable-background-networking", "--disable-component-update",
  "--disable-breakpad", "--disable-crash-reporter", "--host-resolver-rules=MAP * ~NOTFOUND",
  "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"],
  { windowsHide: true, stdio: ["ignore", "ignore", "pipe"] });
let socket, spawnError, browserOutput = "";
browser.on("error", (error) => { spawnError = error; });
browser.stderr.on("data", (chunk) => { browserOutput = (browserOutput + chunk).slice(-3000); });

async function run(html, source, css) {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const settle = async (predicate) => {
    for (let i = 0; i < 100; i += 1) {
      if (predicate()) return;
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    throw new Error("Search UI did not reach expected state.");
  };
  document.open(); document.write(html); document.close();
  const style = document.createElement("style"); style.textContent = css; document.head.append(style);
  const errors = [], calls = [];
  window.addEventListener("error", (event) => errors.push(event.message));
  window.addEventListener("unhandledrejection", (event) => errors.push(String(event.reason)));
  const json = (value) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
  window.fetch = async (url) => {
    if (url === "/api/summary") return json({ papers: 1, claims: 0 });
    if (url === "/api/papers") return json([{ doc_id: "P1", title: "Original English Paper", page_count: 5, has_pdf: true }]);
    if (url === "/api/claims") return json([]);
    if (url === "/api/taxonomy") return json({ categories: [], facets: [] });
    if (url === "/api/native/status") return json({ published: 1, documents: [{doc_id:"P1",title:"Native Paper",pages:5,chunks:1,pdf_sha256:"rev"}] });
    if (url.startsWith("/api/native/search?")) {
      calls.push({mode: "native"});
      return json([{evidence_id: "chunk-test", doc_id: "P1", pdf_page: 3, page_end: 4, pdf_sha256: "rev",
        text: "Indexed content", method: "native", score: .74}]);
    }
    if (url.startsWith("/api/search?")) {
      const params = new URL(url, "http://mock.invalid").searchParams;
      calls.push(Object.fromEntries(params));
      const mode = params.get("mode");
      return json([{ evidence_id: "P1:p4:paragraph", doc_id: "P1", pdf_page: 4,
        text: "The original English paragraph reports annealing at 200 °C.",
        method: mode === "semantic" ? "dense" : mode === "keyword" ? "bm25" : "hybrid",
        score: mode === "keyword" ? -8.4 : .82,
        ...(mode === "auto" ? { rrf_score: .031, retrieval_score: .05,
          retrieval_reasons: [{ method: "bilingual_fts", terms: ["anneal <strong>"] }],
          retrieval_notice: "语义模型或向量检索暂不可用，本次使用本地术语与关键词检索。" } : {}) }]);
    }
    throw new Error(`Unexpected request: ${url}`);
  };
  location.hash = "#/search?arch=raganything";
  (0, eval)(source);
  await settle(() => document.querySelector(".search-box"));
  let scope = document.querySelector('.search-box [name="mode"]');
  check(scope.value === "native" && scope.options.length === 1, "Native retrieval should only search published native documents.");
  const submit = async (query, mode) => {
    if (mode) { scope.value = mode; scope.dispatchEvent(new Event("change")); }
    const previous = calls.length;
    document.querySelector('.search-box [name="q"]').value = query;
    document.querySelector(".search-box").requestSubmit();
    await settle(() => calls.length === previous + 1 && document.querySelector(".quote"));
  };
  await submit("铜键合");
  check(calls.at(-1).mode === "native" && document.querySelector(".result").textContent.includes("第 3–4 页"), "Native scope or physical page range was lost.");
  check(document.querySelector('.viewer [data-a="open"]').getAttribute("href") === "/api/papers/P1/pdf?revision=rev#page=3", "Native PDF revision lost.");
  const architecture = document.querySelector("#workspace-arch"); architecture.value = "demo"; architecture.dispatchEvent(new Event("change"));
  await settle(() => document.querySelector('.search-box [name="mode"]')?.value === "auto");
  scope = document.querySelector('.search-box [name="mode"]');
  check(!document.querySelector(".quote") && scope.options.length === 3, "Demo must not inherit native results.");
  await submit("铜焊盘下凹与退火条件", "auto");
  check(calls.at(-1).mode === "auto" && calls.at(-1).q === "铜焊盘下凹与退火条件", "Default Chinese request lost query/mode.");
  check(document.querySelector(".preview").textContent.includes("综合排序 0.0500"), "Hybrid result mislabeled as cosine/BM25.");
  check(document.querySelector(".preview").textContent.includes("本地术语与关键词检索"), "Fallback notice missing.");
  check(document.querySelector(".preview").textContent.includes("中英术语匹配：anneal <strong>"), "Retrieval reasons missing.");
  check(!document.querySelector(".preview p strong"), "Retrieval reasons were not HTML-escaped.");
  check(document.querySelector(".quote").textContent === "The original English paragraph reports annealing at 200 °C.", "Source quotation was changed.");
  check(document.querySelector('.viewer [data-a="open"]').getAttribute("href") === "/api/papers/P1/pdf#page=4", "Original physical page lost.");
  await submit("铜焊盘下凹", "keyword");
  check(calls.at(-1).mode === "keyword", "Explicit literal mode lost.");
  check(document.querySelector(".preview").textContent.includes("BM25 8.40"), "Literal result score label changed.");
  await submit("Cu pumping", "semantic");
  check(calls.at(-1).mode === "semantic", "Explicit English semantic mode lost.");
  check(document.querySelector(".preview").textContent.includes("语义相似度 0.820"), "Dense score label changed.");
  check(errors.length === 0, `UI errors: ${errors.join(", ")}`);
  return { passed: true, checks: ["native default and versioned PDF", "Chinese auto mode", "literal mode", "English semantic mode", "combined ranking label", "fallback and reasons", "original English quotation and physical PDF page", "no backend/model access"] };
}

(async () => {
  try {
    const portFile = path.join(profile, "DevToolsActivePort");
    for (let i = 0; !fs.existsSync(portFile); i += 1) {
      if (spawnError) throw spawnError;
      assert(i < 150 && browser.exitCode === null, `Headless browser did not start. ${browserOutput}`);
      await sleep(100);
    }
    const [port, endpoint] = fs.readFileSync(portFile, "utf8").trim().split(/\r?\n/);
    socket = new WebSocket(`ws://127.0.0.1:${port}${endpoint}`);
    await new Promise((resolve, reject) => {
      socket.addEventListener("open", resolve, { once: true });
      socket.addEventListener("error", (event) => reject(new Error(`DevTools connection failed. ${browserOutput}`, { cause: event.error || event })), { once: true });
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
    const { targetId } = await call("Target.createTarget", { url: "about:blank" });
    const { sessionId } = await call("Target.attachToTarget", { targetId, flatten: true });
    const html = fs.readFileSync(path.join(root, "web/static/index.html"), "utf8")
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").replace(/<link\b[^>]*>/gi, "");
    const source = fs.readFileSync(path.join(root, "web/static/app.js"), "utf8");
    const css = fs.readFileSync(path.join(root, "web/static/styles.css"), "utf8");
    const result = await call("Runtime.evaluate", { awaitPromise: true, returnByValue: true,
      expression: `(${run})(${JSON.stringify(html)},${JSON.stringify(source)},${JSON.stringify(css)})` }, sessionId);
    assert(!result.exceptionDetails, result.exceptionDetails?.exception?.description || result.exceptionDetails?.text);
    assert.equal(result.result.value?.passed, true);
    console.log(JSON.stringify(result.result.value));
    await call("Browser.close");
  } finally {
    socket?.close(); browser.kill();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
