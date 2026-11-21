/* Real-DOM, isolated headless-browser regression; all API responses are mocked.
 * Run: node tests/test_ingestion_ui.cjs. No backend, model, or key is used.
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
const profile = fs.mkdtempSync(path.join(root, ".cache", "ingestion-browser-"));
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
  const settle = async (predicate) => {
    for (let i = 0; i < 180; i += 1) { if (predicate()) return; await new Promise((resolve) => setTimeout(resolve, 10)); }
    throw new Error("Ingestion UI did not reach expected state.");
  };
  document.open(); document.write(html); document.close();
  const style = document.createElement("style"); style.textContent = css; document.head.append(style);
  const $ = (selector) => document.querySelector(selector);
  const errors = [], requests = [], stored = [], timers = new Map();
  window.addEventListener("error", (event) => errors.push(event.message));
  window.addEventListener("unhandledrejection", (event) => errors.push(String(event.reason)));
  const nativeSetTimeout = window.setTimeout.bind(window), nativeClearTimeout = window.clearTimeout.bind(window);
  let timerId = 900000;
  window.setTimeout = (callback, duration, ...args) => {
    if (duration !== 2000) return nativeSetTimeout(callback, duration, ...args);
    const id = ++timerId; timers.set(id, callback); return id;
  };
  window.clearTimeout = (id) => { timers.delete(id); nativeClearTimeout(id); };
  const originalStorageSet = Storage.prototype.setItem;
  Storage.prototype.setItem = function (key, value) { stored.push([key, value]); return originalStorageSet.call(this, key, value); };
  const json = (value, responseStatus = 200) => new Response(JSON.stringify(value), { status: responseStatus, headers: { "Content-Type": "application/json" } });
  const status = { provider: { configured: mode !== "unavailable", provider: "并行智算云", provider_id: "paratera", base_url: "https://llmapi.paratera.com/v1", credential_source: "independent", model: "", models: [] }, qa_configured: mode !== "unavailable", limits: { max_files: 2, max_file_mb: 1, max_total_mb: 1.5, max_claims: 8 }, maintenance: mode.startsWith("maintenance"), recovery_available: mode.startsWith("maintenance") && mode !== "maintenance-external" };
  const batch = (id, executionMode, state = "queued") => ({ id, mode: executionMode, status: state, stage: state, running: state === "running", auto_publish: true, created_at: "2026-11-03T06:20:00Z", progress: { done: 0, total: 2 }, documents: [{ id: "doc1", filename: "paper.pdf", status: "queued", warnings: [] }, { id: "doc2", filename: "copy.pdf", status: "duplicate", duplicate_of: "existing-document", warnings: [] }], events: [{ time: "2026-11-03T06:20:01Z", message: "已创建批次 <img src=x onerror=bad()>" }] });
  let batches = [batch("legacy-api", "paratera", "completed")], nextBatch = 0, rejectConfig = false, rejectRun = false, catalogCount = 0;
  window.fetch = async (url, options = {}) => {
    const body = options.body ? JSON.parse(options.body) : undefined;
    requests.push({ url, method: options.method || "GET", body });
    if (status.maintenance && !url.startsWith("/api/ingestion")) return json({ detail: "资料索引正在切换或等待恢复，请稍后重试。" }, 503);
    if (url === "/api/summary") { catalogCount += 1; return json({ papers: 0, claims: 0 }); }
    if (url === "/api/papers" || url === "/api/claims") return json([]);
    if (url === "/api/taxonomy") return json({ categories: [], facets: [] });
    if (url === "/api/chat/status") return json({ configured: true, model: "question-model" });
    if (url === "/api/ingestion/status") return json(status);
    if (url === "/api/ingestion/configure") {
      if (rejectConfig) return json({ detail: "RAW_CONFIG_SECRET_MUST_NOT_APPEAR" }, 401);
      status.provider.configured = true; status.provider.models = ["mock-model", "mock-other"]; status.provider.model = body.model || "mock-model"; status.provider.credential_source = body.reuse_qa_key ? "qa" : "independent";
      status.provider.provider_id = body.reuse_qa_key ? "openai_compatible" : body.provider;
      status.provider.base_url = body.reuse_qa_key ? "https://qa-mock.invalid/v1" : body.base_url;
      return json(status.provider);
    }
    if (url === "/api/ingestion/recover") { status.maintenance = false; return json({ recovered: true }); }
    if (url === "/api/ingestion/batches") {
      if (!body) return json({ batches: [...batches, batch("hidden-agent-batch", "codex", "running"), batch("unknown-mode-batch", "unknown")] });
      const entry = batch(`batch-${++nextBatch}`, body.mode);
      entry.auto_publish = body.auto_publish; batches.unshift(entry); return json(entry);
    }
    const match = url.match(/^\/api\/ingestion\/batches\/([^/]+)\/(run|cancel|publish|documents\/[^/]+\/skip)$/);
    if (match) {
      const entry = batches.find((candidate) => candidate.id === match[1]);
      if (match[2] === "run") { if (rejectRun) { rejectRun = false; return json({ detail: "RAW_RUN_ERROR_MUST_NOT_APPEAR" }, 500); } entry.status = "running"; entry.running = true; entry.stage = "extracting"; }
      if (match[2] === "cancel") { entry.status = "cancelled"; entry.running = false; entry.stage = "cancelled"; }
      if (match[2] === "publish") { entry.status = "running"; entry.running = true; entry.stage = "publishing"; }
      if (match[2].endsWith("/skip")) { entry.documents[0].status = "skipped"; entry.documents[0].error = null; }
      return json(entry);
    }
    throw new Error(`Unexpected API ${url}`);
  };
  const selectFiles = (names) => { const transfer = new DataTransfer(); names.forEach((value) => transfer.items.add(value instanceof File ? value : new File(["%PDF-1.4\nmock"], value, { type: value.endsWith(".pdf") ? "application/pdf" : "text/plain" }))); const input = $("[data-ingestion-files]"); input.files = transfer.files; input.dispatchEvent(new Event("change", { bubbles: true })); };
  const poll = async () => { const [id, callback] = [...timers][0] || []; check(callback, "No running-batch poll scheduled."); timers.delete(id); await callback(); };
  location.hash = mode === "maintenance-navigation" ? "#/ask" : "#/ingestion?source=api";
  (0, eval)(source);
  if (mode === "maintenance-navigation") {
    await settle(() => $("[data-ingestion-recovery-link]"));
    $("[data-ingestion-recovery-link]").click();
  }
  await settle(() => $("[data-ingestion-limits]")?.textContent.includes("合计 1.5 MB") && !$("[data-ingestion-refresh]").disabled);
  check($("#nav [data-route=ingestion]").classList.contains("active"), "Missing active ingestion navigation.");
  check(!$("[name=ingestion-mode]") && !/codex/i.test(document.body.textContent), "Agent ingestion entry is still visible.");
  check(!$("[data-batch=hidden-agent-batch]") && !$("[data-batch=unknown-mode-batch]"), "Non-API batches are exposed in the workspace.");
  check($("[data-batch=legacy-api]"), "Legacy Paratera batch is missing.");
  check($("[data-ingestion-start]").disabled, "Empty batch can start.");
  check(!timers.size, "Idle workspace must not poll.");
  check(!requests.some((entry) => entry.method === "POST"), "Page load must not execute a batch.");
  if (mode === "maintenance-external") {
    check(!$("[data-ingestion-maintenance]").hidden && !$("[data-ingestion-recover]"), "External task maintenance must not expose recovery controls.");
    selectFiles(["paper.pdf"]);
    check($("[data-ingestion-start]").disabled && $("[data-ingestion-start-hint]").textContent.includes("资料库正在维护"), "External maintenance must block uploads and explain busy status.");
    $("[data-ingestion-refresh]").click();
    await settle(() => !$("[data-ingestion-refresh]").disabled);
    check(!$("[data-ingestion-recover]") && !requests.some((entry) => entry.method === "POST"), "Refresh enabled or executed external maintenance recovery.");
    return { mode, passed: true, checks: ["external task busy status", "no recovery entry", "no upload execution", "refresh remains read-only"] };
  }
  if (mode.startsWith("maintenance")) {
    check($("[data-ingestion-recover]"), "Maintenance recovery is missing.");
    selectFiles(["paper.pdf"]); check($("[data-ingestion-start]").disabled, "Maintenance allowed a new batch.");
    check(!requests.some((entry) => entry.url.endsWith("/recover")), "Recovery must require a click.");
    $("[data-ingestion-recover]").click();
    await settle(() => $("[data-ingestion-maintenance]").hidden && !$("[data-ingestion-start]").disabled);
    check(requests.filter((entry) => entry.url.endsWith("/recover")).length === 1, "Recovery click did not execute once.");
    location.hash = "#/library";
    await settle(() => $(".library-page"));
    await settle(() => $("#sidebar-foot b")?.textContent === "0");
    check(catalogCount > 0 && $("#sidebar-foot").querySelectorAll("b").length === 2, "Recovery did not reload the regular catalog counts.");
    check(errors.length === 0, `Recovery browser errors: ${errors.join(", ")}`);
    return { mode, passed: true, checks: ["maintenance cold start with unavailable dataset", "recovery route remains reachable", "maintenance blocks new batches", "explicit recovery", "catalog navigation after recovery"] };
  }
  if (mode === "unavailable") {
    selectFiles(["paper.pdf"]);
    check($("[data-ingestion-start]").disabled && $("[data-ingestion-engine]").textContent.includes("尚未配置入库模型 API"), "Unconfigured API should be explained.");
    check($("[data-ingestion-config]").open && $("[data-ingestion-start]").disabled, "Missing independent provider configuration.");
    check($("[data-ingestion-reuse]").disabled, "Unavailable QA key should disable reuse.");
  } else {
    check($("[data-ingestion-engine]").textContent.includes("入库 API 已配置"), "API connection status is missing.");
    selectFiles(["one.pdf", "two.pdf", "three.pdf"]);
    check($("[data-ingestion-file-error]").textContent.includes("每批最多 2"), "File-count restriction missing.");
    selectFiles([new File([new Uint8Array(1024 * 1024 + 1)], "large.pdf")]);
    check($("[data-ingestion-file-error]").textContent.includes("超过每个 1 MB"), "Per-file size restriction missing.");
    selectFiles([new File([new Uint8Array(900000)], "one.pdf"), new File([new Uint8Array(900000)], "two.pdf")]);
    check($("[data-ingestion-file-error]").textContent.includes("合计超过 1.5 MB"), "Aggregate size restriction missing.");
    selectFiles(["paper.pdf", "notes.txt"]);
    check($("[data-ingestion-file-error]").textContent.includes("只处理 PDF"), "Non-PDF exclusion is not explicit.");
    check(document.querySelectorAll("[data-ingestion-remove]").length === 1, "Non-PDF entered batch.");
    $("[data-ingestion-claims]").value = "0"; $("[data-ingestion-start]").click();
    check($("[data-ingestion-setup-error]").textContent.includes("整数") && !requests.some((entry) => entry.method === "POST"), "Invalid claim limit started execution.");
    $("[data-ingestion-claims]").value = "8";
    check($("[data-ingestion-auto]").checked, "Auto publication should default on.");
    $("[data-ingestion-auto]").checked = false;
    $("[data-ingestion-start]").click();
    await settle(() => $("[data-batch-action=cancel]") && !$("[data-ingestion-files]").disabled);
    const created = requests.find((entry) => entry.url === "/api/ingestion/batches" && entry.body);
    check(created.body.mode === "api" && created.body.max_claims === 8 && created.body.files.length === 1 && !created.body.auto_publish, "API creation payload is wrong.");
    check(atob(created.body.files[0].content_base64).startsWith("%PDF"), "PDF was not encoded.");
    check(requests.some((entry) => entry.url.endsWith("/run")), "API batch must start execution.");
    check($("[data-ingestion-batches]").textContent.includes("重复，已跳过"), "Duplicate state missing.");
    check(!$("[data-ingestion-batches]").querySelector("img,script"), "Batch text is executable.");
    check(timers.size === 1, "Running batches must share one poll.");
    const first = batches[0];
    first.running = false; first.status = "needs_attention"; first.stage = "needs_attention";
    first.documents[0].status = "needs_attention"; first.documents[0].error = "OCR 未提取到可用文本 <img src=x onerror=bad()>";
    first.documents[0].warnings = ["请检查扫描质量"];
    await poll();
    check(!timers.size && $("[data-ingestion-batches]").textContent.includes("OCR 未提取到可用文本"), "OCR attention or stopped poll missing.");
    check($("[data-document-skip]") && !$("[data-batch-action=publish]"), "Invalid document should await handling.");
    $("[data-document-skip]").click();
    await settle(() => !$("[data-document-skip]") && $("[data-batch-action=run]"));
    $("[data-batch-action=run]").click();
    await settle(() => $("[data-batch-action=cancel]"));
    first.status = "ready"; first.running = false; first.stage = "ready";
    await poll();
    await settle(() => $("[data-batch-action=publish]"));
    $("[data-batch-action=publish]").click();
    await settle(() => $("[data-batch-action=cancel]"));
    first.running = false; first.status = "completed"; first.stage = "completed"; first.progress.done = 2; first.result = { added_documents: 1, added_claims: 8 };
    first.documents[0].status = "prepared";
    await poll();
    check($(".ingestion-result").textContent.includes("新增文献") && $(".ingestion-result").textContent.includes("新增记录"), "Completion statistics missing.");
    check($(`[data-batch="${first.id}"] .ingestion-document`).textContent.includes("已入库"), "Published prepared documents should show their final state.");
    await settle(() => catalogCount > 1);
    status.provider.configured = false;
    $("[data-ingestion-refresh]").click();
    await settle(() => !$("[data-ingestion-refresh]").disabled && !$("[data-ingestion-base-url]").value);
  }
  check(/入库模型单独[设选]/.test($("[data-ingestion-config]").textContent), "Independent ingestion model scope missing.");
  check(!requests.some((entry) => entry.body?.reuse_qa_key), "QA key reuse must require an explicit action.");
  check($("[data-ingestion-provider]").value === "openai_compatible" && !$("[data-ingestion-base-url]").value, "First API configuration should open a generic connection with no assumed service address.");
  check(!$("[data-ingestion-model]").required, "Model discovery should remain available when a model is left blank.");
  $("[data-ingestion-provider]").value = "paratera";
  $("[data-ingestion-provider]").dispatchEvent(new Event("change", { bubbles: true }));
  const key = $("[data-ingestion-key]");
  key.value = "mock-ingestion-key"; rejectConfig = true;
  $("[data-ingestion-config-form]").requestSubmit();
  await settle(() => $("[data-ingestion-config-message]").textContent.includes("未通过验证"));
  check(!document.body.textContent.includes("RAW_CONFIG_SECRET"), "Raw HTTP configuration error leaked.");
  rejectConfig = false; $("[data-ingestion-config-form]").requestSubmit();
  await settle(() => $("[data-ingestion-config-message]").textContent.includes("已保存"));
  check(!key.value, "Successful configuration must clear password.");
  const parateraRequest = requests.filter((entry) => entry.url === "/api/ingestion/configure").at(-1);
  check(parateraRequest.body.provider === "paratera" && parateraRequest.body.base_url === "https://llmapi.paratera.com/v1", "Default API connection is not explicitly routed to Paratera.");
  check(!stored.some(([name, value]) => /ingestion|mock-ingestion-key/.test(`${name} ${value}`)), "Ingestion key persisted to storage.");
  check(!requests.some((entry) => entry.url === "/api/chat/configure"), "Question configuration was changed.");
  const customProvider = $("[data-ingestion-provider]"), customBase = $("[data-ingestion-base-url]"), customModel = $("[data-ingestion-model]");
  customProvider.value = "openai_compatible"; customProvider.dispatchEvent(new Event("change", { bubbles: true }));
  customBase.value = "https://ingestion-mock.invalid/gateway/v1"; customBase.dispatchEvent(new Event("input", { bubbles: true }));
  customModel.value = "mock-handwritten-model"; customModel.dispatchEvent(new Event("input", { bubbles: true }));
  check(customModel.tagName === "INPUT" && !customBase.readOnly && key.required, "Custom provider requires a free-text model, editable URL and new credential.");
  const beforeMissingKey = requests.filter((entry) => entry.url === "/api/ingestion/configure").length;
  $("[data-ingestion-config-form]").requestSubmit();
  check(requests.filter((entry) => entry.url === "/api/ingestion/configure").length === beforeMissingKey, "Endpoint change reused an empty key.");
  key.value = "mock-custom-key"; $("[data-ingestion-config-form]").requestSubmit();
  await settle(() => status.provider.model === "mock-handwritten-model" && !$("[data-ingestion-save]").disabled);
  const customRequest = requests.filter((entry) => entry.url === "/api/ingestion/configure").at(-1);
  check(customRequest.body.provider === "openai_compatible" && customRequest.body.base_url === "https://ingestion-mock.invalid/gateway/v1" && customRequest.body.model === "mock-handwritten-model", "Custom endpoint and manual model were not sent together.");
  check(!key.value && !key.required, "Saved custom credential should be cleared and reusable at the same endpoint.");
  if (mode === "unavailable") { status.qa_configured = true; $("[data-ingestion-refresh]").click(); await settle(() => !$("[data-ingestion-reuse]").disabled); }
  $("[data-ingestion-model]").value = "mock-other";
  $("[data-ingestion-model]").dispatchEvent(new Event("input", { bubbles: true }));
  key.value = "MUST_NOT_SEND_WHEN_REUSING_QA";
  $("[data-ingestion-reuse]").click();
  await settle(() => $("[data-ingestion-source]").textContent.includes("正在复用问答地址与密钥"));
  const reuseRequest = requests.filter((entry) => entry.url === "/api/ingestion/configure").at(-1);
  check(reuseRequest.body.reuse_qa_key === true && reuseRequest.body.api_key === "" && reuseRequest.body.model === "mock-other", "Explicit QA reuse payload is wrong.");
  check(!("provider" in reuseRequest.body) && !("base_url" in reuseRequest.body), "QA reuse must take its endpoint from the server-side QA connection.");
  check(!key.value && $("[data-ingestion-privacy]").textContent.includes("复用论文问答地址与密钥") && $("[data-ingestion-base-url]").value === "https://qa-mock.invalid/v1", "Reused QA source must show its endpoint and clear the password.");
  $("[data-ingestion-model]").value = "mock-model";
  $("[data-ingestion-model]").dispatchEvent(new Event("input", { bubbles: true }));
  $("[data-ingestion-config-form]").requestSubmit();
  await settle(() => status.provider.model === "mock-model" && !$("[data-ingestion-save]").disabled);
  check(requests.filter((entry) => entry.url === "/api/ingestion/configure").at(-1).body.reuse_qa_key === true, "Changing ingestion model should preserve explicit QA reuse.");
  key.value = "mock-independent-key";
  $("[data-ingestion-config-form]").requestSubmit();
  await settle(() => $("[data-ingestion-source]").textContent.includes("正在使用入库独立连接"));
  check(requests.filter((entry) => entry.url === "/api/ingestion/configure").at(-1).body.reuse_qa_key === false, "A new independent key should leave QA reuse mode.");
  check(!requests.some((entry) => entry.url === "/api/chat/configure" || JSON.stringify(entry).includes("MUST_NOT_SEND_WHEN_REUSING_QA")), "Reuse accessed or changed the QA key in the browser.");
  selectFiles(["paratera.pdf"]); rejectRun = true; $("[data-ingestion-auto]").checked = true;
  $("[data-ingestion-start]").click();
  await settle(() => $("[data-ingestion-setup-error]").textContent.includes("批次已保存"));
  check(!document.body.textContent.includes("RAW_RUN_ERROR"), "Raw HTTP upload/run error leaked.");
  const latest = batches[0];
  check(requests.some((entry) => entry.body?.mode === "api"), "API batch was not created.");
  $(`[data-batch="${latest.id}"] [data-batch-action=run]`).click();
  await settle(() => $(`[data-batch="${latest.id}"] [data-batch-action=cancel]`));
  $(`[data-batch="${latest.id}"] [data-batch-action=cancel]`).click();
  await settle(() => $(`[data-batch="${latest.id}"] [data-batch-action=run]`));
  check(!timers.size, "Cancelled batch must not poll.");
  $(`[data-batch="${latest.id}"] [data-batch-action=run]`).click();
  await settle(() => timers.size === 1);
  location.hash = "#/library";
  await settle(() => $(".library-page"));
  check(!timers.size, "Leaving ingestion failed to clear polling.");
  location.hash = "#/ingestion?source=api";
  await settle(() => timers.size === 1 && $("[data-ingestion-engine]"));
  check(!$("[name=ingestion-mode]") && !/codex/i.test(document.body.textContent), "Returning workspace exposed an agent entry.");
  check(!requests.some((entry) => /hidden-agent-batch|unknown-mode-batch/.test(entry.url) || entry.body?.mode && entry.body.mode !== "api"), "Workspace sent a non-API batch request.");
  check(!$("[data-ingestion-key]").value, "Password survived navigation.");
  check(errors.length === 0, `Browser errors: ${errors.join(", ")}`);
  check(!stored.some(([name, value]) => /mock-(ingestion|custom|independent)-key/.test(`${name} ${value}`)), "An API credential was persisted to browser storage.");
  return { mode, passed: true, checks: ["API-only creation and execution", "no agent entry or batch controls", "legacy Paratera history", "PDF base64", "independent configuration", "custom endpoint and manual model", "new key on endpoint change", "explicit QA endpoint and key reuse", "independent model and source changes", "safe errors", "no key storage", "duplicate and OCR status", "skip and publish", "catalog refresh", "cancel and retry", "poll lifecycle"] };
}

(async () => {
  try {
    const portFile = path.join(profile, "DevToolsActivePort");
    let connection = [];
    for (let i = 0; connection.length < 2 || !/^\d+$/.test(connection[0]) || !connection[1].startsWith("/devtools/"); i += 1) {
      if (spawnError) throw spawnError;
      assert(i < 150 && browser.exitCode === null, "Headless browser did not start.");
      await sleep(100);
      if (fs.existsSync(portFile)) connection = fs.readFileSync(portFile, "utf8").trim().split(/\r?\n/);
    }
    const [port, endpoint] = connection;
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
    const source = fs.readFileSync(path.join(root, "web/static/ingestion.js"), "utf8") + "\n" + fs.readFileSync(path.join(root, "web/static/app.js"), "utf8");
    const css = fs.readFileSync(path.join(root, "web/static/styles.css"), "utf8");
    for (const mode of ["normal", "unavailable", "maintenance", "maintenance-navigation", "maintenance-external"]) {
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
        await call("Runtime.evaluate", { expression: 'location.hash = "#/ingestion?source=api"' }, sessionId);
        for (const [name, width, height] of [["desktop", 1440, 1100], ["mobile", 390, 844]]) {
          await call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 }, sessionId);
          
          await call("Runtime.evaluate", { awaitPromise: true, expression: 'new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))' }, sessionId);
          const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false }, sessionId);
          fs.writeFileSync(path.join(root, ".cache", `ingestion-ui-${name}.png`), Buffer.from(screenshot.data, "base64"));
        }
        await call("Runtime.evaluate", { expression: 'document.querySelector("[data-ingestion-config]").open = true' }, sessionId);
        for (const [name, width, height] of [["desktop", 1440, 1100], ["mobile", 390, 844]]) {
          await call("Emulation.setDeviceMetricsOverride", { width, height, deviceScaleFactor: 1, mobile: width < 600 }, sessionId);
          await call("Runtime.evaluate", { awaitPromise: true, expression: 'new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))' }, sessionId);
          const screenshot = await call("Page.captureScreenshot", { format: "png", captureBeyondViewport: false }, sessionId);
          fs.writeFileSync(path.join(root, ".cache", `ingestion-ui-paratera-${name}.png`), Buffer.from(screenshot.data, "base64"));
        }
      }
      await call("Target.closeTarget", { targetId });
    }
    await call("Browser.close");
  } finally {
    socket?.close(); browser.kill();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });

