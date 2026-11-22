/* Research ideas: a page-local session using the existing question model. */
(function (root) {
  "use strict";

  const REQUEST_TIMEOUT = 300000;
  const MAX_PROMPT = 6000;
  const GENERAL_NOTICES = new Set([
    "研究假设与验证方案尚未经过实验验证；文献依据不等于已证实该假设。",
    "创新性需通过后续文献检索核实；本地检索未命中不代表无人做过。",
  ]);
  const text = (value) => typeof value === "string" ? value : "";
  const strings = (value) => Array.isArray(value) ? value.filter((item) => typeof item === "string" && item.trim()) : [];
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[character]));

  function normalizeResult(result) {
    if (!result || typeof result.generated !== "boolean" || !Array.isArray(result.ideas) || !Array.isArray(result.sources)) {
      throw new Error("服务返回的候选与来源结构不完整，请重试。");
    }
    const seen = new Set();
    const sources = result.sources.filter((source) => source && /^S\d+$/.test(source.id) && !seen.has(source.id) && (seen.add(source.id), true));
    const byId = new Set(sources.map((source) => source.id));
    const warnings = strings(result.warnings).slice();
    const ideas = result.generated ? result.ideas.map((idea) => {
      if (!idea || !text(idea.title).trim() || !text(idea.question).trim()) throw new Error("服务未返回完整的研究候选，请重试。");
      const sourceIds = strings(idea.source_ids);
      const fields = [idea.title, idea.question, idea.hypothesis, idea.basis, idea.novelty,
        ...strings(idea.validation), ...strings(idea.risks), ...strings(idea.search_queries)];
      if (sourceIds.some((id) => !byId.has(id)) || fields.some((field) => [...text(field).matchAll(/\[(S\d+)\]/g)].some((match) => !byId.has(match[1])))) {
        warnings.push("部分来源编号未关联到返回证据，不能作为可追溯依据。请检查该候选的文献依据。");
      }
      return { title: idea.title, question: idea.question, hypothesis: text(idea.hypothesis), basis: text(idea.basis), novelty: text(idea.novelty),
        validation: strings(idea.validation), risks: strings(idea.risks), search_queries: strings(idea.search_queries),
        source_ids: [...new Set(sourceIds.filter((id) => byId.has(id)))] };
    }) : [];
    if (result.generated && !ideas.length) throw new Error("服务未返回研究候选，请重试。");
    return { mode: result.mode === "random" ? "random" : "guided", focus: text(result.focus), ideas, sources,
      warnings: [...new Set(warnings)], provider: text(result.provider), model: text(result.model), generated: result.generated };
  }

  function ideaText(idea, sources = []) {
    const list = (values) => values.map((value, index) => `${index + 1}. ${value}`).join("\n");
    const linked = sources.filter((source) => idea.source_ids.includes(source.id));
    return `${idea.title}\n\n研究问题\n${idea.question}\n\n假设与思路（待验证）\n${idea.hypothesis}\n\n文献依据\n${idea.basis}\n\n可能创新之处（待验证）\n${idea.novelty}\n\n验证步骤\n${list(idea.validation)}\n\n风险与限制\n${list(idea.risks)}\n\n查重检索词\n${list(idea.search_queries)}${linked.length ? `\n\n原文来源\n${linked.map((source) => `[${source.id}] ${source.title || source.doc_id} · PDF 第 ${source.pdf_page} 页 · ${source.evidence_id || ""}`).join("\n")}` : ""}\n\n此候选是研究设想，创新性、可行性与文献覆盖仍需核验。`;
  }

  function importConversation(history, available = MAX_PROMPT) {
    const entries = (Array.isArray(history) ? history : []).filter((entry) => entry && ["user", "assistant"].includes(entry.role) && text(entry.content).trim()).slice(-12);
    const selected = [];
    let remaining = Math.max(0, available - 22);
    for (const entry of entries.slice().reverse()) {
      const value = `${entry.role === "user" ? "提问" : "回答"}：${entry.content}`;
      if (value.length + 2 > remaining) {
        if (!selected.length && remaining > 30) selected.unshift(value.slice(0, remaining - 10) + "\n[此条已截取]");
        break;
      }
      selected.unshift(value); remaining -= value.length + 2;
    }
    return { text: selected.length ? "已导入的问答（可编辑）：\n" + selected.join("\n\n") : "", count: selected.length, truncated: selected.length < entries.length || selected.some((entry) => entry.endsWith("[此条已截取]")) };
  }

  function createSession({ request, arch = "demo", channel = "api", setTimer = setTimeout, clearTimer = clearTimeout }) {
    const state = { draft: "", count: 3, status: null, statusLoading: false, statusError: "", busy: false,
      pending: null, error: "", lastRequest: null, batches: [], nextBatch: 1 };
    const listeners = new Set();
    const notify = () => listeners.forEach((listener) => listener());
    const subscribe = (listener) => { listeners.add(listener); return () => listeners.delete(listener); };
    function setDraft(value) { state.draft = String(value).slice(0, MAX_PROMPT); notify(); }
    function setCount(value) { const count = Number(value); if (Number.isInteger(count) && count >= 1 && count <= 5) state.count = count; notify(); }
    async function refreshStatus() {
      if (state.statusLoading || state.busy) return;
      state.statusLoading = true; state.statusError = ""; notify();
      const controller = new AbortController();
      const timer = setTimer(() => controller.abort(), 20000);
      try {
        const status = await request(channel === "api" ? "/api/chat/status" : "/api/chat/status?channel=subscription", { signal: controller.signal });
        if (typeof status?.configured !== "boolean") throw new Error("模型状态响应不完整。");
        state.status = status;
      } catch (error) {
        state.statusError = state.status?.configured
          ? `本次连接检查未完成，仍使用上次读取的模型配置。${error.message || "请稍后重试。"}`
          : `暂时无法检查模型：${error.message || "连接失败"}`;
      }
      finally { clearTimer(timer); state.statusLoading = false; notify(); }
    }
    async function generate(mode = "guided", retry = false) {
      if (state.busy || state.status?.configured !== true) return;
      const payload = retry && state.lastRequest ? { ...state.lastRequest } : { prompt: mode === "random" ? "" : state.draft.trim(), count: state.count };
      if (mode !== "random" && !retry && !payload.prompt) return;
      if (payload.prompt.length > MAX_PROMPT) return;
      state.busy = true; state.error = ""; state.lastRequest = payload;
      state.pending = { mode: payload.prompt ? "guided" : "random", prompt: payload.prompt, count: payload.count }; notify();
      const controller = new AbortController();
      let timedOut = false;
      const timer = setTimer(() => { timedOut = true; controller.abort(); }, channel === "subscription" ? 1320000 : REQUEST_TIMEOUT);
      try {
        const endpoint = arch === "raganything" ? "/api/native/ideas" : "/api/ideas";
        const result = normalizeResult(await request(endpoint, { method: "POST", headers: { "Content-Type": "application/json" }, signal: controller.signal, body: JSON.stringify({ ...payload, channel }) }));
        state.batches = [{ ...result, number: state.nextBatch++, prompt: payload.prompt, count: payload.count }, ...state.batches];
      } catch (error) {
        state.error = timedOut ? `等待生成超过 ${channel === "subscription" ? 22 : 5} 分钟，请稍后重试。本次输入已保留。` : `本次生成未完成：${error.message || "连接失败，请重试。"}`;
      } finally { clearTimer(timer); state.busy = false; state.pending = null; notify(); }
    }
    return { state, subscribe, setDraft, setCount, refreshStatus, generate };
  }

  const sessions = new Map();
  function mount(host, { request, icon, appendCitedText, openSource, copyText, getChatHistory, toast, mapPrompt = "", arch, channel, configHref, channelPicker }) {
    const key = arch + "/" + channel;
    if (!sessions.has(key)) sessions.set(key, createSession({ request, arch, channel }));
    const session = sessions.get(key);
    const s = session.state;
    host.innerHTML = `<div class="page ideas-page">
      <div class="page-head"><div><h1>创新点生成</h1><p>${arch === "raganything" ? "RAG-Anything · 从已发布文献的 PDF 原页" : "旧版 Demo · 从文献原文与知识记录"}寻找可检验的研究问题。</p></div><a class="btn" href="${configHref}">${icon("ask")}回到论文问答</a></div>
      <section class="card chat-status ideas-status" aria-live="polite"><div class="chat-status-copy" data-ideas-status></div><div class="links"><a class="btn" href="${configHref}" data-ideas-config>配置通用 API</a><button class="btn" type="button" data-ideas-refresh>检查连接</button></div></section>
      <form class="card ideas-composer" data-ideas-form>
        <div class="ideas-composer-head"><label for="ideas-prompt">从哪里开始？</label><button class="btn ghost sm" type="button" data-ideas-import>${icon("plus")}导入当前问答</button></div>
        <textarea id="ideas-prompt" rows="5" maxlength="${MAX_PROMPT}" placeholder="可以留空随机探索；也可以输入：低温 Cu/SiO₂ 混合键合、我能使用的设备与工艺条件，或粘贴一段讨论……" aria-describedby="ideas-input-hint"></textarea>
        <div class="ideas-input-meta"><span id="ideas-input-hint">导入问答只填入输入框；检查、修改后再生成。</span><span data-ideas-length></span></div>
        <div class="ideas-compose-footer"><label class="ideas-count">每次生成 <select class="select" data-ideas-count aria-label="候选数量"><option value="1">1 个候选</option><option value="3">3 个候选</option><option value="5">5 个候选</option></select></label><div class="links"><button class="btn" type="button" data-ideas-random>${icon("flask")}随机探索</button><button class="btn primary" type="submit" data-ideas-guided>${icon("arrow")}按输入生成</button></div></div>
      </form>
      <div class="ideas-examples" aria-label="输入示例"><span class="count">试一试</span><button class="example" type="button" data-ideas-example="低温 Cu/SiO₂ 混合键合：降低热预算，同时保持界面强度">低温键合</button><button class="example" type="button" data-ideas-example="铜焊盘下凹、退火与界面接触电阻之间有哪些值得验证的关系？">铜凹陷与退火</button><button class="example" type="button" data-ideas-example="混合键合的长期可靠性：结合热循环与界面表征，寻找可检验的问题">长期可靠性</button></div>
      <p class="count ideas-session-note">候选是待验证的研究设想；创新性需结合更广的文献与专利检索确认。随机探索不使用输入框内容。切换页面会保留本页草稿、结果与正在进行的请求；刷新后清空。</p>
      <div data-ideas-error role="alert"></div><div data-ideas-pending role="status" aria-live="polite"></div>
      <section class="ideas-results" data-ideas-results aria-label="研究候选，最新生成在前"></section>
    </div>`;
    const $ = (selector) => host.querySelector(selector);
    const input = $("#ideas-prompt"), results = $("[data-ideas-results]");
    let disposed = false, shownBatches = null;
    const cited = (container, value, sources) => appendCitedText(container, value, sources);
    function renderCard(idea, batch, index) {
      const el = document.createElement("article");
      el.className = "card idea-card";
      el.dataset.idea = `${batch.number}-${index + 1}`;
      el.innerHTML = `<div class="idea-card-head"><span class="ideas-candidate-number">候选 ${index + 1}</span><span class="chip idea-unverified">研究设想 · 待验证</span></div><h3></h3><div class="idea-question"><span>研究问题</span><p></p></div><div class="idea-sections"></div><div class="chat-sources idea-sources"></div><div class="idea-actions links"><button class="btn sm" type="button" data-idea-copy>${icon("copy")}复制候选</button><button class="btn sm" type="button" data-idea-refine>${icon("plus")}继续完善</button></div>`;
      el.querySelector("h3").textContent = idea.title;
      cited(el.querySelector(".idea-question p"), idea.question, batch.sources);
      const sections = el.querySelector(".idea-sections");
      [["假设与思路", idea.hypothesis], ["文献依据", idea.basis], ["可能创新之处 · 待验证", idea.novelty], ["验证步骤", idea.validation], ["风险与限制", idea.risks], ["查重检索词", idea.search_queries]].forEach(([label, value]) => {
        const section = document.createElement("section"), heading = document.createElement("h4");
        heading.textContent = label; section.append(heading);
        if (Array.isArray(value)) {
          const list = document.createElement(label === "验证步骤" ? "ol" : "ul");
          if (!value.length) { const item = document.createElement("li"); item.textContent = "尚未给出，需补充。"; list.append(item); }
          value.forEach((entry) => { const item = document.createElement("li"); cited(item, entry, batch.sources); list.append(item); });
          section.append(list);
        } else { const paragraph = document.createElement("p"); cited(paragraph, value || "尚未给出，需补充。", batch.sources); section.append(paragraph); }
        sections.append(section);
      });
      const linked = batch.sources.filter((source) => idea.source_ids.includes(source.id));
      const sourceBox = el.querySelector(".idea-sources");
      if (linked.length) {
        const label = document.createElement("p"); label.className = "count"; label.textContent = "原文来源 · 点击核对证据与实际页"; sourceBox.append(label);
        linked.forEach((source) => { const button = document.createElement("button"); button.type = "button"; button.className = "chat-source-button";
          button.textContent = `[${source.id}] ${source.title || source.doc_id || "来源文献"} · PDF 第 ${source.pdf_page || "?"} 页`;
          button.onclick = () => openSource(source); sourceBox.append(button); });
      } else { sourceBox.textContent = "未关联可追溯的原文来源，请先补充证据。"; sourceBox.classList.add("count"); }
      el.querySelector("[data-idea-copy]").onclick = () => copyText(ideaText(idea, batch.sources), "已复制研究候选");
      el.querySelector("[data-idea-refine]").onclick = () => {
        if (s.busy) return;
        session.setDraft(`请继续完善以下待验证的研究候选。请结合我在末尾补充的条件，核查依据、创新性与可行性，避免将假设当成已有事实。\n\n${ideaText(idea, batch.sources)}\n\n我补充的条件与问题：\n`);
        input.focus(); input.scrollIntoView({ behavior: "smooth", block: "center" });
        toast("候选已放入输入框，补充条件后再生成。");
      };
      return el;
    }
    function renderResults() {
      if (shownBatches === s.batches) return;
      shownBatches = s.batches;
      results.replaceChildren();
      if (!s.batches.length) { results.innerHTML = '<div class="ideas-empty"><span class="ideas-empty-mark" aria-hidden="true">↗</span><h2>下一步，从一个好问题开始</h2><p>点击“随机探索”，或写下你正在关注的方向。<br>每个候选都包含研究问题、文献依据和验证步骤。</p></div>'; return; }
      s.batches.forEach((batch) => {
        const group = document.createElement("section"); group.className = "ideas-batch"; group.dataset.ideasBatch = batch.number;
        group.innerHTML = `<div class="ideas-batch-head"><div><span class="count">第 ${batch.number} 轮 · ${batch.mode === "random" ? "随机探索" : "按输入生成"}</span><h2></h2></div><span class="count">${batch.ideas.length} 个候选</span></div><details class="ideas-focus"><summary>研究起点${batch.prompt ? "与本轮输入" : ""}</summary><div></div></details><div class="ideas-batch-warnings"></div><div class="ideas-cards"></div><p class="count ideas-model"></p>`;
        const summary = batch.prompt.trim().replace(/\s+/g, " ");
        group.querySelector("h2").textContent = batch.mode === "random" ? "从现有文献寻找新的研究切口" : summary ? summary.slice(0, 54) + (summary.length > 54 ? "…" : "") : "围绕你的输入继续探索";
        const focus = group.querySelector(".ideas-focus > div");
        [["研究起点", batch.focus], ["本轮输入", batch.prompt]].forEach(([label, value]) => {
          if (!value || label === "研究起点" && value === batch.prompt) return;
          const heading = document.createElement("h4"), content = document.createElement("p");
          heading.textContent = label; content.textContent = value; focus.append(heading, content);
        });
        if (!focus.children.length) group.querySelector(".ideas-focus").hidden = true;
        const warnings = group.querySelector(".ideas-batch-warnings");
        if (!batch.generated) { const message = document.createElement("div"); message.className = "notice"; message.textContent = "这次没有生成研究候选。可调整方向、补充条件后再试。"; warnings.append(message); }
        batch.warnings.filter((warning) => !GENERAL_NOTICES.has(warning)).forEach((warning) => { const note = document.createElement("div"); note.className = "notice"; note.textContent = warning; warnings.append(note); });
        const general = batch.warnings.filter((warning) => GENERAL_NOTICES.has(warning));
        if (general.length) {
          const details = document.createElement("details"); details.className = "ideas-caveats";
          const label = document.createElement("summary"), note = document.createElement("p");
          label.textContent = "研究设想与创新性的核验说明"; note.textContent = general.join("\n");
          details.append(label, note); warnings.append(details);
        }
        batch.ideas.forEach((idea, index) => group.querySelector(".ideas-cards").append(renderCard(idea, batch, index)));
        group.querySelector(".ideas-model").textContent = `模型：${batch.provider || "未报告提供方"} / ${batch.model || "未报告模型"} · 本轮来源编号独立`;
        results.append(group);
      });
    }
    function render() {
      if (disposed) return;
      if (input.value !== s.draft) input.value = s.draft;
      $("[data-ideas-count]").value = String(s.count);
      input.disabled = s.busy; $("[data-ideas-count]").disabled = s.busy;
      const available = s.status?.configured === true && !s.statusLoading && !s.busy;
      $("[data-ideas-random]").disabled = !available;
      $("[data-ideas-guided]").disabled = !available || !s.draft.trim();
      $("[data-ideas-import]").disabled = s.busy;
      host.querySelectorAll("[data-ideas-example], [data-idea-refine]").forEach((button) => { button.disabled = s.busy; });
      $("[data-ideas-refresh]").disabled = s.statusLoading || s.busy;
      $("[data-ideas-length]").textContent = `${s.draft.length} / ${MAX_PROMPT}`;
      const status = $("[data-ideas-status]");
      status.innerHTML = `<strong>${s.statusLoading ? "正在读取模型配置…" : s.status?.configured ? "可生成研究候选" : "生成接口暂不可用"}</strong><p></p>${s.status?.configured ? "<small></small>" : ""}`;
      status.querySelector("p").textContent = s.statusError || (s.status?.configured ? "生成时会把输入与选中的文献证据发送到下方服务。" : s.status?.message || "正在读取状态…");
      if (status.querySelector("small")) status.querySelector("small").textContent = `${s.status.provider || ""} / ${s.status.model || ""}${s.status.base_url ? ` · ${s.status.base_url}` : ""}`;
      $("[data-ideas-config]").hidden = channel === "subscription";
      $(".ideas-status").classList.toggle("unavailable", !s.statusLoading && !s.status?.configured);
      const error = $("[data-ideas-error]"); error.replaceChildren();
      if (s.error) {
        const note = document.createElement("div"); note.className = "notice ideas-error";
        const message = document.createElement("span"); message.textContent = s.error;
        const retry = document.createElement("button"); retry.type = "button"; retry.className = "btn sm"; retry.textContent = "重试本次生成";
        retry.disabled = !available; retry.onclick = () => session.generate("guided", true); note.append(message, retry); error.append(note);
      }
      const pending = $("[data-ideas-pending]"); pending.replaceChildren();
      if (s.busy) {
        pending.innerHTML = '<div class="card chat-wait"><span class="spinner"></span><div><strong></strong><p></p><small>正在检索文献并形成可检验的设想，可能需要几分钟。切换页面后会继续等待并保留结果。</small></div></div>';
        pending.querySelector("strong").textContent = `正在${s.pending.mode === "random" ? "随机探索" : "围绕输入生成"} ${s.pending.count} 个候选…`;
        pending.querySelector("p").textContent = s.pending.prompt || "从现有混合键合文献中寻找研究切口";
      }
      renderResults();
      host.querySelectorAll("[data-idea-refine]").forEach((button) => { button.disabled = s.busy; });
    }
    input.oninput = () => session.setDraft(input.value);
    input.onkeydown = (event) => { if (event.key === "Enter" && (event.ctrlKey || event.metaKey) && !event.isComposing) { event.preventDefault(); if (!$("[data-ideas-guided]").disabled) session.generate("guided"); } };
    $("[data-ideas-count]").onchange = (event) => session.setCount(event.target.value);
    $("[data-ideas-form]").onsubmit = (event) => { event.preventDefault(); session.generate("guided"); };
    $("[data-ideas-random]").onclick = () => session.generate("random");
    $("[data-ideas-refresh]").onclick = () => session.refreshStatus();
    $("[data-ideas-import]").onclick = () => {
      const imported = importConversation(getChatHistory(), MAX_PROMPT - s.draft.length - (s.draft ? 2 : 0));
      if (!imported.text) { toast(s.draft.length > MAX_PROMPT - 100 ? "输入框空间不足，请先精简内容。" : "当前还没有已完成的问答可导入。", "warn"); return; }
      session.setDraft((s.draft ? s.draft + "\n\n" : "") + imported.text); input.focus();
      toast(`已导入 ${imported.count} 条问答内容${imported.truncated ? "（已截取最近内容）" : ""}，请检查后生成。`);
    };
    host.querySelectorAll("[data-ideas-example]").forEach((button) => { button.onclick = () => { session.setDraft(button.dataset.ideasExample); input.focus(); }; });
    if (mapPrompt && mapPrompt !== s.draft) {
      if (!s.draft.trim()) {
        session.setDraft(mapPrompt);
      } else {
        const choice = document.createElement("div");
        choice.className = "links map-draft-choice";
        const label = document.createElement("span");
        label.textContent = "你在这里已有草稿。";
        const use = document.createElement("button");
        use.className = "btn sm"; use.type = "button"; use.textContent = "使用地图中的研究问题";
        use.onclick = () => { session.setDraft(mapPrompt); choice.remove(); input.focus(); };
        const keep = document.createElement("button");
        keep.className = "btn sm"; keep.type = "button"; keep.textContent = "保留草稿";
        keep.onclick = () => { choice.remove(); input.focus(); };
        choice.append(label, use, keep);
        input.before(choice);
      }
    }
    $(".ideas-status").prepend(channelPicker());
    const unsubscribe = session.subscribe(render);
    render(); if (!s.busy) session.refreshStatus();
    return () => { disposed = true; unsubscribe(); };
  }

  const exported = { mount, createSession, normalizeResult, ideaText, importConversation, esc, REQUEST_TIMEOUT, MAX_PROMPT };
  if (typeof module !== "undefined" && module.exports) module.exports = exported;
  else root.KGInnovation = exported;
})(typeof window === "undefined" ? globalThis : window);
