/* Mock-only regression for the ideas page session and application wiring.
 * Run: node tests/test_innovation_ui.cjs
 * No browser, server, configuration files, credentials, or model API is used.
 */
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const { createSession, normalizeResult, importConversation, ideaText, REQUEST_TIMEOUT, MAX_PROMPT } = require("../web/static/innovation.js");

const result = (title = "可检验的候选") => ({ generated: true, mode: "guided", focus: "低温混合键合", provider: "mock", model: "mock-only",
  ideas: [{ title, question: "退火窗口如何改变？", hypothesis: "一个待验证的假设", basis: "原文给出了实验条件 [S1]，未定位编号 [S99]。", novelty: "可能存在研究切口，需查重。",
    validation: ["保留对照样品", "测量接触电阻"], risks: ["样品不可直接比较"], search_queries: ["hybrid bonding thermal budget"], source_ids: ["S1", "S99"] }],
  sources: [{ id: "S1", doc_id: "P1", title: "<img src=x onerror=bad()>", pdf_page: 2, evidence_id: "P1:p2" }, { id: "javascript:bad()" }], warnings: [] });

async function main() {
  const calls = [], pending = [], timers = [];
  let configured = false, statusFailure = false;
  const session = createSession({
    request: async (url, options) => {
      calls.push({ url, options });
      if (url === "/api/chat/status") {
        if (statusFailure) throw new Error("mock status offline");
        return { configured, provider: "mock", model: "mock-only" };
      }
      assert.equal(url, "/api/ideas");
      return new Promise((resolve, reject) => {
        options.signal.addEventListener("abort", () => reject(new Error("mock aborted")), { once: true });
        pending.push({ resolve, reject });
      });
    },
    setTimer: (callback, milliseconds) => { const timer = { callback, milliseconds, cleared: false }; timers.push(timer); return timer; },
    clearTimer: (timer) => { timer.cleared = true; },
  });
  await session.refreshStatus();
  await session.generate("random");
  assert.equal(calls.filter((call) => call.url === "/api/ideas").length, 0, "Unconfigured page must not call the model.");
  configured = true;
  await session.refreshStatus();
  await session.generate("guided");
  assert.equal(pending.length, 0, "Empty guided input must not generate.");
  session.setDraft("保留的研究方向");
  session.setCount(5);
  const random = session.generate("random");
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), { prompt: "", count: 5, channel: "api" }, "Random exploration must not send the visible draft or hidden QA history.");
  assert.equal(session.state.draft, "保留的研究方向");
  assert.equal(session.state.pending.mode, "random");
  assert.equal(session.state.busy, true);
  assert(REQUEST_TIMEOUT >= 240000 && timers.at(-1).milliseconds === REQUEST_TIMEOUT, "Research generation needs a long request timeout.");
  await session.generate("guided");
  assert.equal(pending.length, 1, "Double submission must not create a second request.");
  const callsBeforeReturn = calls.length;
  await session.refreshStatus();
  assert.equal(calls.length, callsBeforeReturn, "Returning during generation must keep cached status instead of queueing a blocked status request.");
  assert.equal(session.state.status.configured, true);

  let oldViewRenders = 0, newViewRenders = 0;
  const unmountOld = session.subscribe(() => { oldViewRenders += 1; });
  unmountOld();
  const unmountNew = session.subscribe(() => { newViewRenders += 1; });
  pending.shift().resolve({ ...result("随机候选"), mode: "random" });
  await random;
  assert.equal(oldViewRenders, 0, "A disposed page must not receive result updates.");
  assert(newViewRenders > 0, "A returned page must observe completion of the same session.");
  assert.equal(session.state.busy, false);
  assert.equal(session.state.batches[0].mode, "random");
  assert.equal(session.state.draft, "保留的研究方向");
  assert.deepEqual(session.state.batches[0].ideas[0].source_ids, ["S1"]);
  assert.deepEqual(session.state.batches[0].sources.map((source) => source.id), ["S1"]);
  assert(session.state.batches[0].warnings.some((warning) => warning.includes("未关联")), "Unknown evidence citations need a visible warning.");

  session.setDraft("  低温键合\n限定设备  ");
  session.setCount(3);
  const guided = session.generate("guided");
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), { prompt: "低温键合\n限定设备", count: 3, channel: "api" });
  pending.shift().resolve(result("新的候选"));
  await guided;
  assert.deepEqual(session.state.batches.map((batch) => batch.ideas[0].title), ["新的候选", "随机候选"], "New results belong before earlier generations.");
  assert.deepEqual(session.state.batches.map((batch) => batch.number), [2, 1]);
  assert.equal(session.state.batches[1].ideas[0].title, "随机候选", "A later generation must preserve earlier candidates.");

  const failed = session.generate("guided");
  pending.shift().reject(new Error("mock connection failed"));
  await failed;
  assert.match(session.state.error, /mock connection failed/);
  assert.equal(session.state.draft, "  低温键合\n限定设备  ", "Failure must preserve an editable draft.");
  assert.equal(session.state.batches.length, 2, "Failure must preserve previous results.");
  session.setDraft("用户在失败后修改的新草稿");
  session.setCount(1);
  const retried = session.generate("guided", true);
  assert.deepEqual(JSON.parse(calls.at(-1).options.body), { prompt: "低温键合\n限定设备", count: 3, channel: "api" }, "Retry should repeat the failed request, even when the visible draft changed.");
  pending.shift().resolve(result("重试候选")); await retried;
  assert.equal(session.state.draft, "用户在失败后修改的新草稿");

  const timeout = session.generate("random");
  timers.at(-1).callback();
  pending.shift();
  await timeout;
  assert.match(session.state.error, /5 分钟/);
  assert.equal(session.state.busy, false, "Timeout must release the composer for retry.");
  assert(timers.every((timer) => timer.cleared), "All completed request timers must be released.");

  const malformed = session.generate("guided");
  pending.shift().resolve({ generated: true, ideas: [], sources: [] }); await malformed;
  assert.match(session.state.error, /未返回研究候选/);
  assert.equal(session.state.batches.length, 3, "Malformed responses must not create a successful candidate.");
  const noGeneration = normalizeResult({ generated: false, ideas: [result().ideas[0]], sources: [], warnings: ["证据不足"] });
  assert.deepEqual(noGeneration.ideas, [], "A generated:false result must not display generated ideas.");

  statusFailure = true;
  await session.refreshStatus();
  assert.equal(session.state.status.configured, true, "A transient status check failure must preserve the prior configuration.");
  assert.match(session.state.statusError, /mock status offline/);
  assert.match(session.state.statusError, /上次读取/);
  unmountNew();

  const history = Array.from({ length: 16 }, (_, index) => ({ role: index % 2 ? "assistant" : "user", content: `内容${index}` }));
  const imported = importConversation(history);
  assert.equal(imported.count, 12);
  assert(imported.text.includes("内容4") && imported.text.includes("内容15") && !imported.text.includes("内容0"));
  const longImport = importConversation([{ role: "assistant", content: "字".repeat(7000) }], 100);
  assert(longImport.truncated && longImport.text.length <= 100, "Long imports must be visibly clipped to the available editable space.");
  const wholeCopy = ideaText(session.state.batches[0].ideas[0], session.state.batches[0].sources);
  ["研究问题", "假设与思路（待验证）", "文献依据", "可能创新之处（待验证）", "验证步骤", "风险与限制", "查重检索词", "PDF 第 2 页"].forEach((label) => assert(wholeCopy.includes(label), `Full-card copy is missing ${label}`));
  session.setDraft("字".repeat(MAX_PROMPT + 10));
  assert.equal(session.state.draft.length, MAX_PROMPT);
  const countBefore = session.state.count; session.setCount(6); assert.equal(session.state.count, countBefore);

  const root = path.resolve(__dirname, "..");
  const app = fs.readFileSync(path.join(root, "web/static/app.js"), "utf8");
  const html = fs.readFileSync(path.join(root, "web/static/index.html"), "utf8");
  assert.match(html, /href="#\/ideas" data-route="ideas"/);
  assert(html.indexOf('/static/innovation.js?v=') < html.indexOf('/static/app.js?v='), "Ideas module must load before the app router.");
  assert.match(app, /ideas: ideasView/); assert.match(app, /ideas: "创新点生成"/);
  assert.match(app, /getChatHistory: \(\) => session\.turns/);
  assert.match(app, /disposeIdeas\(\)/);
  console.log(JSON.stringify({ passed: true, checks: ["existing model configuration", "empty random and guided requests", "visible draft and no hidden history", "count bounds", "no duplicate requests", "long timeout", "detached subscriber cleanup", "persistent session", "latest results first", "failure and retry", "malformed response", "source allowlist", "editable import clipping", "full-card copy", "navigation and route integration"] }));
}

main().catch((error) => { console.error(error); process.exitCode = 1; });
