/* Real-DOM regression for the project report page, with mocked HTTP only.
 * Run: node tests/test_report_ui.cjs (UI_SCREENSHOTS=1 also writes .cache/report-ui-*.png).
 * No backend, index or model is opened.
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
const profile = fs.mkdtempSync(path.join(root, ".cache", "report-browser-"));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const browser = spawn(browserPath, ["--headless=new", "--disable-gpu", "--no-first-run",
  "--no-default-browser-check", "--disable-background-networking", "--disable-component-update",
  "--disable-breakpad", "--disable-crash-reporter", "--host-resolver-rules=MAP * ~NOTFOUND",
  "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"],
  { windowsHide: true, stdio: ["ignore", "ignore", "pipe"] });
let socket, spawnError, browserOutput = "";
browser.on("error", (error) => { spawnError = error; });
browser.stderr.on("data", (chunk) => { browserOutput = (browserOutput + chunk).slice(-3000); });

async function run(html, sources, css, summary) {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const settle = async (predicate) => {
    for (let i = 0; i < 200; i += 1) {
      if (predicate()) return;
      await new Promise((resolve) => setTimeout(resolve, 10));
    }
    throw new Error("Report UI did not reach expected state.");
  };
  document.open(); document.write(html); document.close();
  const style = document.createElement("style"); style.textContent = css; document.head.append(style);
  const errors = [], requests = [];
  window.addEventListener("error", (event) => errors.push(event.message));
  window.addEventListener("unhandledrejection", (event) => errors.push(String(event.reason)));
  const json = (value) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
  let width = 2;
  window.fetch = async (url, options) => {
    requests.push({ url, method: options?.method || "GET" });
    if (url === "/api/native/status?progress=true") return json({published:0, pending:2, held:0, catalog:2,
      phase:"running", model:"gpt-6.2-sol", concurrency:width, max_concurrency:4, legacy_runner:true,
      active:[{doc_id:"a", title:"试点论文", status:"running", stage:"indexing", milestones:1, parsed_pages:8, pages:8, visual_items:3, responses:9}],
      waiting:[{doc_id:"b",title:"排队论文",stage:"queued",pages:3},
        {doc_id:"c",title:"待复核论文",stage:"crop_review_held",pages:4,parsed_pages:4,visual_items:5,has_regions:true}], documents:[]});
    if (url === "/api/native/queue/settings") {
      width = JSON.parse(options.body).documents;
      return json({documents:width});
    }
    if (url === "/api/summary") return json(summary);
    if (url === "/api/native/status") return json({ published: 2, pending: 1, held: 0, catalog: 3,
      phase: "paused", model: "gpt-6.2-sol", concurrency: 2, active: [], documents: [] });
    if (url === "/api/papers" || url === "/api/claims") return json([]);
    if (url === "/api/taxonomy") return json({ categories: [], facets: [] });
    if (url === "/api/chat/status") return json({ configured: false });
    throw new Error(`Unexpected request: ${url}`);
  };
  location.hash = "#/report";
  for (const source of sources) (0, eval)(source);
  await settle(() => document.querySelector(".report-page .rp-stack"));
  const page = document.querySelector(".report-page");
  check(document.title.startsWith("项目概况"), "Route title missing.");
  check(document.querySelector('#nav a.active')?.dataset.route === "report", "Nav entry not active.");
  const kpis = [...page.querySelectorAll(".rp-kpi b")].map((el) => el.textContent);
  if (summary.samples == null) check(kpis.length === 3, "Missing summary fields should drop their KPI.");
  else check(JSON.stringify(kpis) === JSON.stringify(["213", "2,765", "1,439", "1,299"]), `Live counts not shown: ${kpis}`);
  const layers = [...page.querySelectorAll(".rp-layer")];
  check(!page.querySelector(".native-queue"), "Demo report mixed in the native queue.");
  for (const layer of layers) check(layer.querySelector(".rp-layer-name").textContent && layer.querySelectorAll(".rp-tech .rp-mono").length >= 2, "Each layer needs named technologies.");
  const names = [...page.querySelectorAll(".rp-tech b")].map((b) => b.textContent);
  for (const tech of ["FastAPI", "BGE-M3", "Qdrant", "SQLite", "PyMuPDF", "Pydantic"]) check(names.includes(tech), `${tech} missing from the stack.`);
  check(page.querySelectorAll(".rp-step").length === 6 && page.querySelectorAll(".rp-store").length === 3, "Flow steps or stores missing.");
  const routes = [...page.querySelectorAll(".rp-app")].map((a) => {
    check(a.hash.includes("arch=demo"), "Report link lost its workspace.");
    return a.hash.split("?")[0];
  });
  check(JSON.stringify(routes) === JSON.stringify(["#/search", "#/ask", "#/ideas", "#/library"]), `Feature links changed: ${routes}`);
  check(page.querySelectorAll(".rp-bar").length === 4 && page.querySelectorAll(".rp-principles li").length === 4, "Growth or principles missing.");
  check(page.querySelectorAll(".rp-rag-step").length === 6 && [...page.querySelectorAll(".rp-title")].some((h) => h.textContent.includes("RAG")), "RAG pipeline missing.");
  const visibleText = page.innerText.replace(/\s+/g, " ").trim();
  check(visibleText.length < 1900, `Report should stay concise (${visibleText.length} visible chars).`);
  check(!page.innerHTML.includes("undefined") && !page.innerHTML.includes("NaN"), "Unfilled template value.");
  check(errors.length === 0, `UI errors: ${errors.join(", ")}`);
  check(requests.every((r) => r.method === "GET"), "Report page must only read.");
  if (summary.samples == null) {
    location.hash = "#/ingestion?arch=raganything&channel=subscription";
    await settle(() => document.querySelector(".native-progress-card"));
    const card = document.querySelector(".native-progress-card");
    check(card.textContent.includes("试点论文") && card.textContent.includes("已解析 8/8 页") && card.textContent.includes("9 条模型响应"), "Missing progress checkpoints");
    const steps = [...card.querySelectorAll(".native-steps li")];
    check(steps.length === 5 && steps.filter(li => li.classList.contains("done")).length === 2 && steps[2].classList.contains("current"), "Indexing should mark the third of five steps current");
    const review = document.querySelector("[data-review]");
    check(!review.hidden && review.textContent.includes("待复核论文") && review.querySelector("a[href*='regions=c']"), "Held crop reviews must be listed with a link");
    check(document.querySelector("[data-waiting] summary").textContent.includes("1 篇"), "Waiting list should exclude held crop reviews");
    const field = document.querySelector("[data-native-width]");
    const form = field.closest("form");
    field.value = "4";
    field.dispatchEvent(new Event("change"));
    document.querySelector(".native-panel-head button").click();
    await settle(() => !document.querySelector(".native-panel-head button").disabled);
    check(field.value === "4" && width === 2, "Refresh lost the unsaved width");
    form.requestSubmit();
    await settle(() => width === 4 && !field.disabled);
    check(form.textContent.includes("暂停后") && form.textContent.includes("已设 4 篇"), "Missing effective-time notice");
    const waiting = document.querySelector("[data-waiting]");
    waiting.open = true;
    await settle(() => waiting.textContent.includes("排队论文"));
    check(requests.filter(r => r.method === "POST").length === 1, "Unexpected queue mutation");
    check(errors.length === 0, "Ingestion view raised a browser error");
  }
  return { passed: true, checks: ["route and nav", "live counts and native queue", "stack layers", "flow, stores and features", "growth and principles", "concise text", "read-only"] };
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
    const html = fs.readFileSync(path.join(root, "web/static/index.html"), "utf8")
      .replace(/<script\b[^>]*>[\s\S]*?<\/script>/gi, "").replace(/<link\b[^>]*>/gi, "");
    const sources = ["report.js", "app.js"].map((file) => fs.readFileSync(path.join(root, "web/static", file), "utf8"));
    const css = fs.readFileSync(path.join(root, "web/static/styles.css"), "utf8");
    const full = { papers: 213, pages: 2765, claims: 1439, samples: 1299, reviews: 14 };
    for (const [mode, summary] of [["full", full], ["partial", { papers: 213, pages: 2765, claims: 1439 }]]) {
      const { targetId } = await call("Target.createTarget", { url: "about:blank" });
      const { sessionId } = await call("Target.attachToTarget", { targetId, flatten: true });
      await call("Emulation.setDeviceMetricsOverride", { width: 1440, height: 1000, deviceScaleFactor: 1, mobile: false }, sessionId);
      await call("Emulation.setEmulatedMedia", { features: [{ name: "prefers-reduced-motion", value: "reduce" }] }, sessionId);
      const result = await call("Runtime.evaluate", { awaitPromise: true, returnByValue: true,
        expression: `(${run})(${JSON.stringify(html)},${JSON.stringify(sources)},${JSON.stringify(css)},${JSON.stringify(summary)})` }, sessionId);
      assert(!result.exceptionDetails, result.exceptionDetails?.exception?.description || result.exceptionDetails?.text);
      assert.equal(result.result.value?.passed, true);
      console.log(JSON.stringify({ mode, ...result.result.value }));
      if (mode === "partial") {
        await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true }, sessionId);
        const mobile = await call("Runtime.evaluate", { returnByValue: true, expression:
          'document.documentElement.scrollWidth <= innerWidth && !document.querySelector(".native-settings").hidden' }, sessionId);
        assert.equal(mobile.result.value, true, "Mobile ingestion controls overflow or are hidden");
        console.log(JSON.stringify({mode:"ingestion",passed:true,checks:["checkpoint cards","settings persistence","legacy runner notice","pending list","mobile layout"]}));
      }
      if (mode === "full") {
        // Phone width must not scroll the page sideways.
        await call("Emulation.setDeviceMetricsOverride", { width: 390, height: 844, deviceScaleFactor: 1, mobile: true }, sessionId);
        const overflow = await call("Runtime.evaluate", { returnByValue: true, awaitPromise: true,
          expression: 'new Promise(r => requestAnimationFrame(() => r([document.documentElement.scrollWidth, document.querySelector(".report-page").scrollWidth, innerWidth])))' }, sessionId);
        const [docWidth, pageWidth, viewport] = overflow.result.value;
        assert(docWidth <= viewport && pageWidth <= viewport, `Horizontal overflow at phone width: ${docWidth}/${pageWidth} > ${viewport}`);
        console.log(JSON.stringify({ mode: "phone", passed: true, checks: ["no horizontal overflow"] }));
        if (process.env.UI_SCREENSHOTS) {
          const shots = [["desktop-top", 1440, 1000, ".rp-hero"], ["desktop-flow", 1440, 1000, ".rp-flow"], ["desktop-rag", 1440, 1000, ".rp-rag"], ["desktop-bottom", 1440, 1000, ".rp-split"],
            ["mobile-top", 390, 844, ".rp-hero"], ["mobile-stack", 390, 844, ".rp-stack"], ["mobile-flow", 390, 844, ".rp-flow"]];
          for (const [name, width, height, selector] of shots) {
            await call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 }, sessionId);
            await call("Runtime.evaluate", { awaitPromise: true, expression: `document.querySelector(${JSON.stringify(selector)}).scrollIntoView({ behavior: "instant", block: "start" }); new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)))` }, sessionId);
            const shot = await call("Page.captureScreenshot", { format: "png" }, sessionId);
            fs.writeFileSync(path.join(root, ".cache", `report-ui-${name}.png`), Buffer.from(shot.data, "base64"));
          }
        }
      }
      await call("Target.closeTarget", { targetId });
    }
    await call("Browser.close");
  } finally {
    socket?.close(); browser.kill();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
