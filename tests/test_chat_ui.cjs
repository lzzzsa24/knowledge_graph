/* Real-DOM, mock-only regression. Run: node tests/test_chat_ui.cjs
 * Uses an isolated headless Chrome profile; never starts the QA backend or
 * reads its configuration. CHROME_PATH can override the browser executable.
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
assert(browserPath, "Install Chrome/Chromium or set CHROME_PATH to run the UI regression.");
fs.mkdirSync(path.join(root, ".cache"), { recursive: true });
const profile = fs.mkdtempSync(path.join(root, ".cache", "chat-sort-browser-"));
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const browser = spawn(browserPath, ["--headless=new", "--disable-gpu", "--no-first-run",
  "--no-default-browser-check", "--disable-background-networking", "--disable-component-update",
  "--disable-breakpad", "--disable-crash-reporter", "--host-resolver-rules=MAP * ~NOTFOUND",
  "--remote-debugging-port=0", `--user-data-dir=${profile}`, "about:blank"],
  { windowsHide: true, stdio: "ignore" });
let socket;
let spawnError;
browser.on("error", (error) => { spawnError = error; });

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
    const pending = new Map();
    let nextId = 0;
    socket.addEventListener("message", (event) => {
      const result = JSON.parse(event.data);
      const handler = pending.get(result.id);
      if (!handler) return;
      pending.delete(result.id);
      clearTimeout(handler.timeout);
      if (result.error) handler.reject(new Error(JSON.stringify(result.error)));
      else handler.resolve(result.result);
    });
    const call = (method, params = {}, sessionId) => new Promise((resolve, reject) => {
      const id = ++nextId;
      const timeout = setTimeout(() => { pending.delete(id); reject(new Error(`Timed out: ${method}`)); }, 15000);
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
      expression: `(${async function run(html, source, css) {
        const check = (condition, message) => { if (!condition) throw new Error(message); };
        const same = (actual, expected, message) => check(JSON.stringify(actual) === JSON.stringify(expected),
          `${message}: ${JSON.stringify(actual)} != ${JSON.stringify(expected)}`);
        const settle = async (predicate) => {
          for (let i = 0; i < 100; i += 1) {
            if (predicate()) return;
            await new Promise((resolve) => setTimeout(resolve, 10));
          }
          throw new Error("UI did not reach the expected state.");
        };
        document.open(); document.write(html); document.close();
        location.hash = "#/ask?source=api";
        const style = document.createElement("style"); style.textContent = css; document.head.append(style);
        const calls = [], configurations = [], stored = [], nativeCalls = [];
        const storageSet = Storage.prototype.setItem;
        Storage.prototype.setItem = function (key, value) { stored.push([key, value]); return storageSet.call(this, key, value); };
        const providerStatus = { configured: true, provider: "mock-ui-only", provider_id: "paratera", base_url: "https://llmapi.paratera.com/v1", model: "mock-sort", models: ["mock-sort"] };
        let rejectConfiguration = false;
        let resolveChat;
        const json = (value) => new Response(JSON.stringify(value), { headers: { "Content-Type": "application/json" } });
        window.fetch = async (url, options) => {
          if (url === "/api/summary") return json({ papers: 0, claims: 0 });
          if (url === "/api/papers" || url === "/api/claims") return json([]);
          if (url === "/api/native/status") return json({ published: 2, catalog: 3, cli_available: true, model: "gpt-6.2-sol" });
          if (url === "/api/native/chat") {
            const body = JSON.parse(options.body); nativeCalls.push(body);
            return json({ answer: "订阅回答。[S1]", sources: [{ id: "S1", doc_id: "sample", pdf_page: 2, verified_quote: "on this page" }],
              model: "gpt-6.2-sol", provider: "Codex CLI", retrieval: { mode: "native", pages: 1 } });
          }
          if (url === "/api/chat/status") return json(providerStatus);
          if (url === "/api/chat/status?channel=subscription") return json({configured:true, provider:"Codex CLI", model:"Sol High"});
          if (url === "/api/chat/configure") {
            const body = JSON.parse(options.body); configurations.push(body);
            if (rejectConfiguration) return new Response(JSON.stringify({ detail: `Synthetic rejection ${body.api_key}` }), { status: 503, headers: { "Content-Type": "application/json" } });
            Object.assign(providerStatus, { provider_id: body.provider, base_url: body.base_url, model: body.model, models: ["mock-suggested"], message: "Synthetic connection saved." });
            return json(providerStatus);
          }
          if (url === "/api/chat") {
            calls.push(JSON.parse(options.body));
            return new Promise((resolve) => { resolveChat = () => resolve(json({ answer: `回答：${calls.at(-1).question}`,
              sources: [], warnings: [], provider: "mock-ui-only", model: "mock-sort", answerability: "supported" })); });
          }
          throw new Error(`Unexpected network request: ${url}`);
        };
        (0, eval)(source);
        await settle(() => document.querySelector(".chat-composer") &&
          document.querySelector(".chat-status-copy strong")?.textContent === "问答模型已配置");
        const questionInput = document.querySelector(".chat-composer textarea");
        const questions = () => [...document.querySelectorAll(".chat-turn .chat-question")].map((el) => el.textContent);
        const numbers = () => [...document.querySelectorAll(".chat-turn")].map((el) => Number(el.dataset.chatTurn));
        const sort = (value) => document.querySelector(`[data-chat-sort="${value}"]`).click();
        const submit = async (question) => {
          questionInput.value = question;
          questionInput.dispatchEvent(new Event("input", { bubbles: true }));
          document.querySelector(".chat-composer").requestSubmit();
          await settle(() => document.querySelector(".chat-pending .chat-wait"));
          check(document.querySelector(".chat-pending p").textContent === question, "Waiting question was not visible.");
          check(Boolean(document.querySelector(".chat-pending").compareDocumentPosition(document.querySelector(".chat-history")) &
            Node.DOCUMENT_POSITION_FOLLOWING), "Waiting status must precede history.");
        };
        const finish = async () => { resolveChat(); await settle(() => !document.querySelector(".chat-pending .chat-wait")); };
        const chronological = (from, through) => Array.from({ length: through - from + 1 }, (_, i) => i + from)
          .flatMap((n) => [{ role: "user", content: `问题${n}` }, { role: "assistant", content: `回答：问题${n}` }]);
        check(document.querySelector('[data-chat-sort="latest"]').getAttribute("aria-pressed") === "true", "Default is not latest first.");
        await submit("问题1"); await finish();
        await submit("问题2"); await finish();
        same(questions(), ["问题2", "问题1"], "Latest-first display"); same(numbers(), [2, 1], "Stable numbering");
        sort("earliest");
        same(questions(), ["问题1", "问题2"], "Earliest-first display"); same(numbers(), [1, 2], "Chronological numbering");
        check(document.querySelector('[data-chat-sort="earliest"]').getAttribute("aria-pressed") === "true", "Sort state was not announced.");
        await submit("问题3");
        same(calls.at(-1).history, chronological(1, 2), "Model history after a display toggle");
        sort("latest");
        check(Boolean(document.querySelector(".chat-pending .chat-wait")), "Sorting lost the pending request.");
        await finish();
        for (let n = 4; n <= 7; n += 1) { await submit(`问题${n}`); await finish(); }
        same(questions(), ["问题7", "问题6", "问题5", "问题4", "问题3", "问题2"], "Six-turn retention");
        same(numbers(), [7, 6, 5, 4, 3, 2], "Retained turns keep their original numbers");
        sort("earliest"); same(numbers(), [2, 3, 4, 5, 6, 7], "Retained chronological numbers");
        await submit("问题8");
        same(calls.at(-1).history, chronological(2, 7), "Retained model history must stay chronological");
        document.querySelector("[data-chat-clear]").click();
        await finish();
        same(numbers(), [], "Clearing ignores an old in-flight response");
        await submit("重新开始");
        same(calls.at(-1).history, [], "Clearing removes request history");
        await finish();
        same(numbers(), [1], "Clearing resets turn numbers");
        check(!document.querySelector(".chat-answerability.supported"), "Positive answers should keep natural labels.");
        document.querySelector("[data-chat-settings]").click();
        const provider = document.querySelector("#chat-provider"), base = document.querySelector("#chat-base-url"), model = document.querySelector("#chat-model-select"), key = document.querySelector("#chat-api-key"), keyForm = document.querySelector(".chat-key-form");
        check(base.readOnly && !key.required, "Saved Paratera connection should keep its fixed URL and allow model-only changes.");
        provider.value = "openai_compatible"; provider.dispatchEvent(new Event("change", { bubbles: true }));
        base.value = "https://mock-compatible.invalid/gateway/v1"; base.dispatchEvent(new Event("input", { bubbles: true }));
        model.value = "mock-handwritten-model"; model.dispatchEvent(new Event("input", { bubbles: true }));
        check(model.tagName === "INPUT" && !base.readOnly && key.required, "Custom API needs an editable URL, free-text model and a new key.");
        keyForm.requestSubmit();
        check(!configurations.length, "Changing the API endpoint must not reuse an empty key.");
        key.value = "mock-chat-api-key"; rejectConfiguration = true; keyForm.requestSubmit();
        await settle(() => document.querySelector(".chat-config-error").textContent.includes("Synthetic rejection"));
        check(!document.body.textContent.includes("mock-chat-api-key"), "Provider failure exposed the key in the page.");
        rejectConfiguration = false; keyForm.requestSubmit();
        await settle(() => providerStatus.model === "mock-handwritten-model" && !key.value && !document.querySelector(".chat-key-form button").disabled);
        same(configurations.at(-1), { api_key: "mock-chat-api-key", provider: "openai_compatible", base_url: "https://mock-compatible.invalid/gateway/v1", model: "mock-handwritten-model" }, "Custom API payload");
        check(document.querySelector(".chat-status-copy").textContent.includes("https://mock-compatible.invalid/gateway/v1"), "Saved service address is not visible.");
        document.querySelector("[data-chat-settings]").click();
        model.value = "mock-other-handwritten-model"; model.dispatchEvent(new Event("input", { bubbles: true }));
        keyForm.requestSubmit();
        await settle(() => providerStatus.model === "mock-other-handwritten-model" && !document.querySelector(".chat-key-form button").disabled);
        check(configurations.at(-1).api_key === "" && configurations.at(-1).base_url === "https://mock-compatible.invalid/gateway/v1", "Model-only update lost the saved generic service.");
        check(!stored.some(([name, value]) => /mock-chat-api-key/.test(`${name} ${value}`)), "API key was persisted to browser storage.");
        check(!calls.some((body) => JSON.stringify(body).includes("mock-chat-api-key")), "API key entered chat history.");
        document.querySelector('[data-arch="raganything"]').click();
        await settle(() => document.querySelector(".page-head p")?.textContent.includes("RAG-Anything") && document.querySelector(".chat-status-copy strong")?.textContent === "问答模型已配置");
        const nativeApi = document.querySelector(".chat-composer textarea");
        nativeApi.value = "API 原生"; nativeApi.dispatchEvent(new Event("input")); document.querySelector(".chat-composer").requestSubmit();
        await settle(() => document.querySelector(".chat-citation"));
        document.querySelector('[data-via="subscription"]').click();
        await settle(() => document.querySelector(".chat-status-copy strong")?.textContent.includes("Codex CLI 订阅问答"));
        check(document.querySelector(".chat-config").hidden, "Subscription QA exposed legacy API configuration.");
        const nativeInput = document.querySelector(".chat-composer textarea");
        nativeInput.value = "原生问题"; nativeInput.dispatchEvent(new Event("input", { bubbles: true }));
        document.querySelector(".chat-composer").requestSubmit();
        await settle(() => document.querySelector(".chat-citation")?.textContent.includes("PDF 第 2 页"));
        same(nativeCalls, [{ question: "API 原生", history: [], channel: "api" }, { question: "原生问题", history: [], channel: "subscription" }], "Native generations must use their own histories");
        document.querySelector('[data-arch="demo"]').click();
        await settle(() => document.querySelector(".page-head p")?.textContent.includes("Demo") && document.querySelector(".chat-status-copy strong")?.textContent.includes("Codex"));
        const demoSub = document.querySelector(".chat-composer textarea");
        demoSub.value = "Demo 订阅"; demoSub.dispatchEvent(new Event("input")); document.querySelector(".chat-composer").requestSubmit();
        await settle(() => calls.at(-1)?.question === "Demo 订阅");
        same(calls.at(-1), {question:"Demo 订阅",history:[],channel:"subscription"}, "Demo subscription dispatch or history mixed");
        resolveChat();
        return { passed: true, mockRequests: calls.length, checks: ["default latest first", "both sort directions",
          "stable turn numbering", "waiting state", "six-turn retention", "chronological request history", "clear during request",
          "custom API address and manual model", "new key on endpoint change", "model-only changes", "safe configuration errors", "no browser key persistence",
          "default native subscription route", "isolated history", "verified page citation"] };
      }} )(${JSON.stringify(html)}, ${JSON.stringify(source)}, ${JSON.stringify(css)})` }, sessionId);
    assert(!result.exceptionDetails, result.exceptionDetails?.exception?.description || result.exceptionDetails?.text);
    assert.equal(result.result.value?.passed, true);
    console.log(JSON.stringify(result.result.value));
    await call("Browser.close");
  } finally {
    socket?.close();
    browser.kill();
  }
})().catch((error) => { console.error(error); process.exitCode = 1; });
