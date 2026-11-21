/* PDF ingestion workspace. Keys stay in the password field until explicitly submitted. */
(function () {
  "use strict";

  const BATCH_STATUS = { queued: "等待执行", running: "执行中", needs_attention: "需要处理", ready: "可写入", completed: "已入库", cancelled: "已停止", failed: "执行失败" };
  const DOC_STATUS = { queued: "等待处理", pending: "等待处理", running: "处理中", extracting: "提取文本", extracted: "文本已提取", processing: "模型处理中", prepared: "已准备", ready: "校验通过", validated: "校验通过", completed: "已入库", published: "已入库", duplicate: "重复，已跳过", skipped: "已跳过", needs_attention: "需要处理", failed: "处理失败", cancelled: "已停止" };
  const STAGES = { queued: "等待执行", received: "PDF 已接收", parsing: "提取 PDF 文本", extracting: "模型提取与分类", extract: "提取 PDF 文本", extraction: "提取 PDF 文本", preparing: "处理文档", model: "模型提取与分类", generating: "模型提取与分类", processing: "模型提取与分类", classification: "文献分类", validating: "证据与格式校验", validation: "证据与格式校验", validated: "证据与格式校验通过", review: "自动复核", reviewing: "自动复核", review_required: "等待处理问题", ready: "等待写入", publishing: "写入文献库", publish: "写入文献库", publication_failed: "写入待恢复", completed: "入库完成", cancelled: "已停止", failed: "执行失败", needs_attention: "等待处理问题", skipped: "已跳过" };
  const esc = (value) => String(value ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const label = (value, dictionary) => dictionary[value] || "处理中";
  const running = (batch) => batch.running === true || batch.status === "running";
  const apiBatch = (batch) => ["api", "paratera"].includes(batch.mode);
  const DEFAULT_BASE = "https://llmapi.paratera.com/v1";
  const stamp = (value) => { const d = new Date(value); return Number.isNaN(d.getTime()) ? "" : d.toLocaleString("zh-CN", { hour12: false }); };

  function mount(root, options = {}) {
    let disposed = false, timer = null, status = null, batches = [], files = [];
    let loading = true, submitting = false, configuring = false, refreshing = false, reading = "";
    let setupError = "", batchError = "", fileError = "", configError = "", configSuccess = "", batchRevision = 0;
    let connectionDraft = { provider: "openai_compatible", base_url: "", model: "" }, connectionEdited = false, modelEdited = false;
    const busy = new Set(), completed = new Set(), controller = new AbortController();
    const $ = (selector) => root.querySelector(selector);
    const limits = () => ({ max_files: 20, max_file_mb: 30, max_total_mb: 60, max_claims: 8, ...(status?.limits || {}) });
    const canExecute = () => status && !status.maintenance && status.provider?.configured;

    root.className = "page ingestion-page";
    root.innerHTML = `<div class="page-head"><div><h1>批量入库</h1><p>简易自制架构 Demo · 上传 PDF，生成证据、分类与知识记录。</p></div><button class="btn" type="button" data-ingestion-refresh>刷新状态</button></div>
      <div data-ingestion-maintenance hidden></div>
      <div class="ingestion-layout">
        <section class="card ingestion-setup" aria-labelledby="ingestion-new-title">
          <div class="ingestion-section-head"><span class="ingestion-step">1</span><h2 id="ingestion-new-title">新建批次</h2></div>
          <p class="muted">使用模型 API 处理 PDF，支持并行或 OpenAI-compatible 服务。</p>
          <div class="ingestion-engine" data-ingestion-engine role="status"></div>
          <details class="ingestion-config" data-ingestion-config><summary>配置入库模型 API</summary>
            <p class="muted">可复用论文问答的服务地址与密钥，也可设置独立连接；入库模型单独选择。</p>
            <button class="btn" type="button" data-ingestion-reuse>复用论文问答连接</button><p class="muted" data-ingestion-reuse-hint></p><p class="ingestion-credential-source" data-ingestion-source></p>
            <form data-ingestion-config-form autocomplete="off">
              <label>API 服务<select class="input" data-ingestion-provider><option value="paratera">并行智算云 · Paratera</option><option value="openai_compatible">OpenAI-compatible · 自定义服务</option></select></label>
              <label>API base URL<input class="input" name="base_url" type="url" autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="https://api.example.com/v1" data-ingestion-base-url required></label>
              <label>模型名称<input class="input" name="model" type="text" list="ingestion-model-options" autocomplete="off" spellcheck="false" autocapitalize="off" placeholder="填写完整模型名，或选择建议" data-ingestion-model aria-label="入库模型"><datalist id="ingestion-model-options"></datalist></label>
              <p class="muted" data-ingestion-connection-hint></p>
              <label>API key<input class="input" name="ingestion-api-key" type="password" autocomplete="new-password" spellcheck="false" placeholder="输入入库专用密钥" data-ingestion-key></label>
              <button class="btn" type="submit" data-ingestion-save>保存入库配置</button><p class="ingestion-inline-message" data-ingestion-config-message role="status"></p>
            </form>
          </details>
          <div class="ingestion-divider"></div>
          <label class="ingestion-dropzone" data-ingestion-dropzone><input type="file" multiple accept="application/pdf,.pdf" data-ingestion-files><span class="ingestion-upload-icon" aria-hidden="true">↑</span><strong>选择或拖入 PDF</strong><span data-ingestion-limits>正在读取文件限制…</span></label>
          <div class="ingestion-selected" data-ingestion-selected></div><p class="ingestion-inline-message ingestion-error" data-ingestion-file-error role="alert"></p>
          <div class="ingestion-options"><label>每篇最多提取 <input class="input" type="number" min="1" max="8" value="8" data-ingestion-claims> 条知识记录</label><label class="ingestion-checkbox"><input type="checkbox" checked data-ingestion-auto> 通过复核后自动入库</label><p class="muted">扫描件会尝试本机 OCR；有文本、证据或校验问题时，批次会停下等待处理。</p></div>
          <p class="ingestion-privacy" data-ingestion-privacy></p>
          <p class="ingestion-inline-message ingestion-error" data-ingestion-setup-error role="alert"></p>
          <button class="btn primary ingestion-start" type="button" data-ingestion-start disabled>开始入库</button>
          <p class="ingestion-start-hint" data-ingestion-start-hint></p>
        </section>
        <section class="ingestion-history" aria-labelledby="ingestion-history-title"><div class="ingestion-section-head"><span class="ingestion-step">2</span><h2 id="ingestion-history-title">批次与进度</h2><span class="count" data-ingestion-batch-count></span></div>
          <p class="ingestion-inline-message ingestion-error" data-ingestion-batch-error role="alert"></p><div data-ingestion-batches aria-live="polite"></div>
        </section>
      </div>`;

    function failure(responseStatus, operation) {
      if (responseStatus === 401 || responseStatus === 403) return operation === "configure" ? "入库配置未通过验证，请检查 API 密钥与模型。" : "当前执行方式未授权，请检查入库专用配置。";
      if (responseStatus === 413) return "文件超过服务端限制，请减少数量或选择更小的 PDF。";
      if (responseStatus === 409) return "批次状态已变化或入库维护尚未完成，请刷新状态后重试。";
      if (responseStatus === 400 || responseStatus === 422) return operation === "configure" ? "配置未保存，请检查服务地址、密钥与完整模型名；复用问答时地址须与问答一致。" : "请求未通过检查，请检查 PDF 文件、数量与批次状态。";
      if (responseStatus === 429) return "请求过于频繁或额度暂不可用，请稍后重试。";
      if (responseStatus === 404) return "入库服务或该批次暂不可用，请刷新状态。";
      if (responseStatus >= 500) return "入库服务暂不可用，已保留当前选择，请稍后重试。";
      return "无法连接入库服务，请检查服务状态后重试。";
    }
    async function request(path, body, operation) {
      let response;
      try {
        response = await fetch(`/api/ingestion${path}`, { signal: controller.signal, credentials: "same-origin", ...(body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }) });
      } catch (error) {
        if (error.name === "AbortError") throw error;
        throw new Error(failure(0, operation));
      }
      if (!response.ok) throw new Error(failure(response.status, operation));
      try { return await response.json(); } catch (_) { throw new Error("入库服务返回了无效状态，请刷新后重试。"); }
    }
    function stopTimer() { if (timer !== null) clearTimeout(timer); timer = null; }
    function schedule() {
      stopTimer();
      if (disposed || !batches.some(running)) return;
      timer = setTimeout(async () => {
        timer = null;
        try { await loadBatches(); } catch (_) { /* message is rendered by loadBatches */ }
        schedule();
      }, 2000);
    }
    function updateBatch(batch) {
      if (!apiBatch(batch)) return;
      batchRevision += 1;
      const index = batches.findIndex((entry) => entry.id === batch.id);
      if (index < 0) batches.unshift(batch); else batches[index] = batch;
      renderBatches(); schedule();
    }
    function notifyCompleted() {
      const newlyCompleted = batches.filter((batch) => batch.status === "completed" && !completed.has(batch.id));
      newlyCompleted.forEach((batch) => completed.add(batch.id));
      if (newlyCompleted.length) options.onCompleted?.();
    }
    function renderSetup() {
      if (disposed) return;
      const l = limits(), engine = status?.provider;
      const engineMessage = loading ? "正在检查执行环境…" : !status ? "无法读取执行环境，请刷新状态。"
        : engine?.configured ? `入库 API 已配置 · ${engine.model || "已选择模型"} · ${engine.credential_source === "qa" ? "问答连接" : "独立连接"}${engine.base_url ? ` · ${engine.base_url}` : ""}` : engine?.message || "尚未配置入库模型 API，可在下方复用问答连接或填写独立配置。";
      $("[data-ingestion-engine]").textContent = engineMessage;
      $("[data-ingestion-engine]").classList.toggle("is-ready", !!engine?.configured);
      $("[data-ingestion-limits]").textContent = `仅 PDF · 最多 ${l.max_files} 个 · 每个 ${l.max_file_mb} MB · 合计 ${l.max_total_mb} MB`;
      $("[data-ingestion-claims]").max = l.max_claims;
      if (+$("[data-ingestion-claims]").value > l.max_claims) $("[data-ingestion-claims]").value = l.max_claims;
      $("[data-ingestion-privacy]").textContent = `所选 PDF 的文本将发送到${status?.provider?.base_url ? ` ${status.provider.base_url}` : "已配置的模型服务"}，${status?.provider?.credential_source === "qa" ? "复用论文问答地址与密钥，入库模型单独设置" : "使用本页保存的独立连接"}。仅处理本批 PDF，不读取或发送其他项目文件。`;
      $("[data-ingestion-setup-error]").textContent = setupError;
      $("[data-ingestion-start]").disabled = loading || submitting || configuring || !files.length || !canExecute();
      $("[data-ingestion-start]").textContent = submitting ? reading || "正在创建批次…" : "开始入库";
      $("[data-ingestion-start-hint]").textContent = submitting ? "页面可离开，已启动的批次会在后台继续。" : !files.length ? "选择 PDF 后即可启动执行。" : status?.maintenance ? status.recovery_available ? "请先恢复未完成的入库。" : "资料库正在维护，请稍后刷新。" : !canExecute() ? "入库 API 配置就绪后即可开始。" : `将使用模型 API 处理 ${files.length} 个 PDF。`;
      $("[data-ingestion-refresh]").disabled = refreshing || submitting;
      $("[data-ingestion-refresh]").textContent = refreshing ? "刷新中…" : "刷新状态";
      $("[data-ingestion-files]").disabled = submitting;
      $("[data-ingestion-claims]").disabled = submitting;
      $("[data-ingestion-auto]").disabled = submitting;
      $("[data-ingestion-save]").disabled = configuring || submitting || !status;
      $("[data-ingestion-reuse]").disabled = configuring || submitting || !status?.qa_configured;
      $("[data-ingestion-reuse-hint]").textContent = status?.qa_configured ? `${status.qa_connection?.base_url ? `问答地址：${status.qa_connection.base_url}。` : ""}可沿用问答模型，也可填写独立的入库模型。` : "论文问答尚无可用连接，可先配置问答，或在下方填写独立配置。";
      $("[data-ingestion-source]").textContent = status?.provider?.configured ? status.provider.credential_source === "qa" ? "正在复用问答地址与密钥，入库模型独立设置。修改服务地址时需填写独立密钥。" : "正在使用入库独立连接，密钥留空可保留当前连接。" : "";
      const independentKeyRequired = !status?.provider?.configured || endpointChanged();
      $("[data-ingestion-key]").disabled = configuring || submitting;
      $("[data-ingestion-key]").required = independentKeyRequired;
      $("[data-ingestion-key]").placeholder = independentKeyRequired ? "首次连接或更换地址时填写此服务的密钥" : "留空保留当前密钥来源";
      $("[data-ingestion-provider]").value = connectionDraft.provider;
      $("[data-ingestion-base-url]").value = connectionDraft.base_url;
      $("[data-ingestion-model]").value = connectionDraft.model;
      $("[data-ingestion-provider]").disabled = $("[data-ingestion-base-url]").disabled = $("[data-ingestion-model]").disabled = configuring || submitting;
      $("[data-ingestion-base-url]").readOnly = connectionDraft.provider === "paratera";
      $("[data-ingestion-model]").required = false;
      $("[data-ingestion-connection-hint]").textContent = connectionDraft.provider === "paratera" ? "并行使用固定地址；模型可留空自动选择，也可手动填写。密钥在本机加密保存。" : "填写 API 基础地址，可直接输入完整模型名。留空模型时仅在服务支持 /models 的情况下自动选择。密钥在本机加密保存。";
      $("[data-ingestion-save]").textContent = configuring ? "正在保存…" : "保存入库配置";
      const configMessage = $("[data-ingestion-config-message]");
      configMessage.textContent = configError || configSuccess; configMessage.classList.toggle("ingestion-error", !!configError);
      const maintenance = $("[data-ingestion-maintenance]");
      maintenance.hidden = !status?.maintenance;
      maintenance.innerHTML = !status?.maintenance ? "" : status.recovery_available ? '<div class="notice ingestion-maintenance"><div><strong>资料库维护尚未完成</strong><p>恢复可读取的资料库；已准备的批次会保留，恢复后可继续入库。</p></div><button class="btn" type="button" data-ingestion-recover>恢复未完成的入库</button></div>' : '<div class="notice ingestion-maintenance"><div><strong>资料库正在维护</strong><p>其他任务正在更新资料库，请稍后刷新状态。</p></div></div>';
      const recover = $("[data-ingestion-recover]");
      if (recover) { recover.disabled = busy.has("recover"); recover.onclick = recoverPublication; }
      renderFiles();
    }
    function endpointChanged() {
      const saved = status?.provider;
      return !!saved?.configured && ((saved.provider_id || "paratera") !== connectionDraft.provider || (saved.base_url || DEFAULT_BASE).replace(/\/+$/, "") !== connectionDraft.base_url.trim().replace(/\/+$/, ""));
    }
    function syncConnection(provider) {
      const hasConnection = provider?.has_saved_config ?? (!!provider?.configured || !!provider?.model);
      connectionDraft = hasConnection
        ? { provider: provider.provider_id || "paratera", base_url: provider.base_url || (provider.provider_id === "openai_compatible" ? "" : DEFAULT_BASE), model: provider.model || "" }
        : { provider: "openai_compatible", base_url: "", model: "" };
      connectionEdited = false; modelEdited = false;
    }
    function renderModels() {
      const models = (status?.provider?.models || []).map((model) => typeof model === "string" ? { id: model, label: model } : { id: model.id || model.value || model.model, label: model.label || model.name || model.id || model.value || model.model }).filter((model) => model.id);
      if (!models.length && status?.provider?.model) models.push({ id: status.provider.model, label: status.provider.model });
      $("#ingestion-model-options").innerHTML = models.map((model) => `<option value="${esc(model.id)}">${esc(model.label)}</option>`).join("");
    }
    function renderFiles() {
      $("[data-ingestion-selected]").innerHTML = files.length ? `<div class="ingestion-file-summary"><strong>已选择 ${files.length} 个 PDF</strong><button class="btn ghost sm" type="button" data-ingestion-clear ${submitting ? "disabled" : ""}>清空</button></div><ul>${files.map((file, index) => `<li><span class="ingestion-filename">${esc(file.name)}</span><span class="muted">${(file.size / 1024 / 1024).toFixed(1)} MB</span><button class="icon-btn" type="button" data-ingestion-remove="${index}" aria-label="移除 ${esc(file.name)}" ${submitting ? "disabled" : ""}>×</button></li>`).join("")}</ul>` : "";
      $("[data-ingestion-file-error]").textContent = fileError;
      $("[data-ingestion-clear]")?.addEventListener("click", () => { files = []; fileError = ""; $("[data-ingestion-files]").value = ""; renderSetup(); });
      root.querySelectorAll("[data-ingestion-remove]").forEach((button) => { button.onclick = () => { files.splice(+button.dataset.ingestionRemove, 1); renderSetup(); }; });
    }
    function documentHTML(doc, batch) {
      const problem = !!doc.error || ["failed", "needs_attention"].includes(doc.status);
      const published = batch.status === "completed" && doc.status === "prepared";
      return `<li class="ingestion-document ${problem ? "has-problem" : ""}"><div class="ingestion-document-top"><strong>${esc(doc.filename || doc.title || doc.id)}</strong><span class="ingestion-state ${problem ? "is-problem" : ""}">${doc.duplicate_of ? "重复，已跳过" : published ? "已入库" : esc(label(doc.status, DOC_STATUS))}</span></div>
        ${doc.title && doc.title !== doc.filename ? `<p class="ingestion-document-title">${esc(doc.title)}</p>` : ""}
        <div class="ingestion-document-meta">${doc.stage ? `<span>${published ? "入库完成" : esc(label(doc.stage, STAGES))}</span>` : ""}${Number.isFinite(doc.claim_count) ? `<span>${doc.claim_count} 条知识记录</span>` : ""}${doc.duplicate_of ? `<span>已存在：${esc(doc.duplicate_of)}</span>` : ""}</div>
        ${doc.error ? `<p class="ingestion-document-error">${esc(doc.error)}</p>` : ""}${(doc.warnings || []).length ? `<ul class="ingestion-warnings">${doc.warnings.map((warning) => `<li>${esc(typeof warning === "string" ? warning : warning.message || "请复核该文档。")}</li>`).join("")}</ul>` : ""}${!running(batch) && ["needs_attention", "failed"].includes(doc.status) ? `<button class="btn ghost sm" type="button" data-document-skip="${esc(doc.id)}" data-batch-id="${esc(batch.id)}" ${busy.has(batch.id) ? "disabled" : ""}>跳过此资料</button>` : ""}</li>`;
    }
    function resultHTML(batch) {
      const result = batch.result;
      if (!result || typeof result !== "object") return "";
      const metrics = [["added_documents", "新增文献"], ["papers_added", "新增文献"], ["documents_added", "新增文献"], ["published", "入库文献"], ["added_claims", "新增记录"], ["claims_added", "新增记录"], ["claims", "知识记录"], ["added_evidence", "原文证据"], ["evidence_added", "原文证据"], ["skipped", "跳过"], ["duplicates", "重复"]];
      const used = new Set();
      const values = metrics.filter(([key, title]) => Number.isFinite(result[key]) && !used.has(title) && (used.add(title), true));
      return values.length ? `<div class="ingestion-result">${values.map(([key, title]) => `<span><strong>${result[key]}</strong>${title}</span>`).join("")}</div>` : "";
    }
    function renderBatches() {
      if (disposed) return;
      notifyCompleted();
      const open = new Set([...root.querySelectorAll("details[data-batch-details][open]")].map((node) => node.dataset.batchDetails));
      $("[data-ingestion-batch-error]").textContent = batchError;
      $("[data-ingestion-batch-count]").textContent = batches.length ? `${batches.length} 个批次` : "";
      $("[data-ingestion-batches]").innerHTML = !batches.length ? `<div class="card ingestion-empty"><span class="ingestion-empty-symbol" aria-hidden="true">▤</span><h3>${loading ? "正在读取批次…" : "还没有入库批次"}</h3><p>${loading ? "请稍候。" : "选择 PDF 并开始后，可在这里查看每篇文档的处理结果。"}</p></div>` : batches.map((batch) => {
        const active = running(batch), locked = busy.has(batch.id), documents = batch.documents || [], total = Number(batch.progress?.total) || documents.length, done = Math.min(total, Math.max(0, Number(batch.progress?.done) || 0));
        const attention = ["needs_attention", "failed"].includes(batch.status), retry = ["queued", "needs_attention", "failed", "cancelled"].includes(batch.status) && !active;
        return `<article class="card ingestion-batch" data-batch="${esc(batch.id)}"><div class="ingestion-batch-head"><div><span class="ingestion-batch-id">模型 API · ${esc(stamp(batch.created_at))}</span><h3>${documents.length} 个 PDF <span class="ingestion-state ${attention ? "is-problem" : ""} ${active ? "is-running" : ""}">${active ? '<span class="spinner"></span>' : ""}${esc(label(batch.status, BATCH_STATUS))}</span></h3></div><span class="ingestion-progress-text">${done} / ${total}</span></div>
          <progress class="ingestion-progress" max="${Math.max(1, total)}" value="${done}" aria-label="批次文件进度 ${done} / ${total}"></progress>
          <div class="ingestion-batch-stage"><span>${esc(label(batch.stage || batch.status, STAGES))}</span><span>${batch.auto_publish ? "通过后自动入库" : "手动确认入库"}</span></div>
          ${batch.error ? `<p class="ingestion-document-error">${esc(batch.error)}</p>` : ""}${attention ? '<p class="ingestion-attention">请查看下方文档问题。已完成的准备会保留；处理问题后可继续或重试。</p>' : ""}
          ${batch.status === "completed" ? '<p class="ingestion-complete-note">入库已完成。<a href="#/papers">查看工艺与文献 →</a></p>' : ""}${resultHTML(batch)}
          <ul class="ingestion-documents">${documents.map((doc) => documentHTML(doc, batch)).join("")}</ul>
          <div class="ingestion-batch-actions">${active ? `<button class="btn sm" type="button" data-batch-action="cancel" data-id="${esc(batch.id)}" ${locked || batch.cancel_requested ? "disabled" : ""}>${batch.cancel_requested ? "已请求停止，等待当前步骤完成…" : locked ? "正在请求…" : "完成当前步骤后停止"}</button>` : ""}${retry ? `<button class="btn sm" type="button" data-batch-action="run" data-id="${esc(batch.id)}" ${locked || status?.maintenance ? "disabled" : ""}>${locked ? "正在启动…" : batch.status === "queued" ? "启动执行" : "继续 / 重试"}</button>` : ""}${batch.status === "ready" && !active ? `<button class="btn primary sm" type="button" data-batch-action="publish" data-id="${esc(batch.id)}" ${locked || status?.maintenance ? "disabled" : ""}>${locked ? "正在启动…" : "将合格文档写入库"}</button>` : ""}<span class="ingestion-batch-ref">批次 ${esc(batch.id)}</span></div>
          ${(batch.events || []).length ? `<details class="ingestion-events" data-batch-details="${esc(batch.id)}" ${open.has(batch.id) ? "open" : ""}><summary>执行记录（${batch.events.length}）</summary><ol>${batch.events.slice(-30).map((event) => `<li><time>${esc(stamp(event.time))}</time><span>${esc(event.message)}</span></li>`).join("")}</ol></details>` : ""}</article>`;
      }).join("");
      root.querySelectorAll("[data-batch-action]").forEach((button) => { button.onclick = () => batchAction(button.dataset.id, button.dataset.batchAction); });
      root.querySelectorAll("[data-document-skip]").forEach((button) => { button.onclick = () => batchAction(button.dataset.batchId, `documents/${encodeURIComponent(button.dataset.documentSkip)}/skip`); });
    }
    function selectFiles(incoming) {
      if (submitting) return;
      const selected = [...incoming], l = limits();
      if (selected.length > l.max_files) { fileError = `每批最多 ${l.max_files} 个 PDF，请重新选择。`; renderFiles(); return; }
      const invalid = selected.filter((file) => !/\.pdf$/i.test(file.name));
      const tooLarge = selected.filter((file) => file.size > l.max_file_mb * 1024 * 1024);
      if (tooLarge.length) { fileError = `${tooLarge.map((file) => file.name).join("、")} 超过每个 ${l.max_file_mb} MB 的限制，请重新选择。`; renderFiles(); return; }
      if (selected.reduce((total, file) => total + file.size, 0) > l.max_total_mb * 1024 * 1024) { fileError = `所选文件合计超过 ${l.max_total_mb} MB，请减少文件后重新选择。`; renderFiles(); return; }
      files = selected.filter((file) => /\.pdf$/i.test(file.name));
      fileError = invalid.length ? `目前只处理 PDF，已跳过：${invalid.map((file) => file.name).join("、")}。` : "";
      setupError = ""; renderSetup();
    }
    function readFile(file) {
      return new Promise((resolve, reject) => {
        const reader = new FileReader();
        const abort = () => { if (reader.readyState === FileReader.LOADING) reader.abort(); };
        const cleanup = () => controller.signal.removeEventListener("abort", abort);
        reader.onload = () => { cleanup(); const content = String(reader.result || "").split(",")[1]; content ? resolve({ name: file.name, content_base64: content }) : reject(new Error("PDF 读取失败，请重新选择文件。")); };
        reader.onerror = () => { cleanup(); reject(new Error("PDF 读取失败，请重新选择文件。")); };
        reader.onabort = () => { cleanup(); reject(new Error("PDF 读取已中止。")); };
        controller.signal.addEventListener("abort", abort, { once: true });
        reader.readAsDataURL(file);
      });
    }
    async function start() {
      if (disposed || submitting || !files.length || !canExecute()) return;
      const l = limits(), maxClaims = +$("[data-ingestion-claims]").value;
      if (!Number.isInteger(maxClaims) || maxClaims < 1 || maxClaims > l.max_claims) { setupError = `每篇知识记录数须为 1 至 ${l.max_claims} 的整数。`; renderSetup(); return; }
      if (files.length > l.max_files || files.some((file) => file.size > l.max_file_mb * 1024 * 1024) || files.reduce((total, file) => total + file.size, 0) > l.max_total_mb * 1024 * 1024) { setupError = "所选文件超过当前限制，请重新选择。"; renderSetup(); return; }
      submitting = true; setupError = ""; const autoPublish = $("[data-ingestion-auto]").checked;
      renderSetup(); let created = null;
      try {
        const encoded = [];
        for (let index = 0; index < files.length; index += 1) { reading = `读取 PDF ${index + 1} / ${files.length}…`; renderSetup(); encoded.push(await readFile(files[index])); }
        reading = "正在创建批次…"; renderSetup();
        created = await request("/batches", { mode: "api", files: encoded, max_claims: maxClaims, auto_publish: autoPublish });
        if (disposed) return;
        files = []; $("[data-ingestion-files]").value = ""; updateBatch(created);
        reading = "正在启动执行…"; renderSetup();
        updateBatch(await request(`/batches/${encodeURIComponent(created.id)}/run`, {}));
      } catch (error) {
        if (!disposed) setupError = created ? `批次已保存，但尚未启动。${error.message} 可在右侧点击“启动执行”。` : error.message;
      } finally { submitting = false; reading = ""; renderSetup(); }
    }
    async function configure(event, reuseQaKey = false) {
      event?.preventDefault();
      if (configuring || submitting) return;
      const key = $("[data-ingestion-key]");
      let model = connectionDraft.model.trim();
      if (reuseQaKey && !status?.qa_configured) return;
      if (reuseQaKey && !modelEdited) {
        const saved = status?.provider, qa = status?.qa_connection;
        const sameEndpoint = !!saved?.configured && !!qa && (saved.provider_id || "paratera") === (qa.provider_id || "paratera") && (saved.base_url || DEFAULT_BASE).replace(/\/+$/, "") === (qa.base_url || DEFAULT_BASE).replace(/\/+$/, "");
        if (!sameEndpoint) model = qa?.model || "";
      }
      if (!reuseQaKey && !key.value.trim() && (!status?.provider?.configured || endpointChanged())) { configError = "请复用论文问答连接，或填写此服务的独立 API 密钥。更换服务地址时也需填写该服务的密钥。"; configSuccess = ""; renderSetup(); return; }
      if (!reuseQaKey && !connectionDraft.base_url.trim()) { configError = "请填写 API base URL。"; configSuccess = ""; renderSetup(); return; }
      const preserveQaSource = status?.provider?.credential_source === "qa" && !key.value.trim() && !endpointChanged();
      const reuse = reuseQaKey || preserveQaSource;
      const payload = { api_key: reuse ? "" : key.value.trim(), model, reuse_qa_key: reuse };
      if (!reuse) Object.assign(payload, { provider: connectionDraft.provider, base_url: connectionDraft.base_url.trim() });
      configuring = true; configError = ""; configSuccess = ""; renderSetup();
      try {
        const provider = await request("/configure", payload, "configure");
        if (disposed) return;
        status.provider = provider.provider && typeof provider.provider === "object" ? provider.provider : provider; key.value = ""; configSuccess = status.provider.credential_source === "qa" ? "配置已保存。正在复用问答地址与密钥，入库模型独立设置。" : "入库独立配置已保存，密码框已清空。"; syncConnection(status.provider); renderModels();
      } catch (error) { if (!disposed) configError = error.message; }
      finally { configuring = false; renderSetup(); }
    }
    async function batchAction(id, action) {
      if (disposed || busy.has(id)) return;
      const batch = batches.find((entry) => entry.id === id);
      if (!batch || action === "publish" && (batch.status !== "ready" || running(batch))) return;
      busy.add(id); batchError = ""; renderBatches();
      try { updateBatch(await request(`/batches/${encodeURIComponent(id)}/${action}`, {})); }
      catch (error) { if (!disposed) batchError = error.message; }
      finally { busy.delete(id); renderBatches(); }
    }
    async function recoverPublication() {
      if (!status?.maintenance || !status.recovery_available || busy.has("recover")) return;
      busy.add("recover"); setupError = ""; renderSetup();
      try { await request("/recover", {}); await refresh(); }
      catch (error) { if (!disposed) setupError = error.message; }
      finally { busy.delete("recover"); renderSetup(); }
    }
    async function loadBatches() {
      const revision = batchRevision, previouslyRunning = new Set(batches.filter(running).map((batch) => batch.id));
      try {
        const result = await request("/batches");
        if (disposed || revision !== batchRevision) return;
        batches = (result.batches || []).filter(apiBatch).sort((a, b) => String(b.created_at).localeCompare(String(a.created_at)));
        batchError = ""; renderBatches();
        if (batches.some((batch) => previouslyRunning.has(batch.id) && !running(batch))) { await loadStatus(); renderSetup(); renderBatches(); }
      } catch (error) { if (!disposed) { batchError = error.message; renderBatches(); } throw error; }
    }
    async function loadStatus() {
      try { const result = await request("/status"); if (!disposed) { status = result; setupError = ""; if (!connectionEdited) syncConnection(status.provider); if (!status.provider?.configured) $("[data-ingestion-config]").open = true; renderModels(); } }
      catch (error) { if (!disposed) setupError = error.message; }
    }
    async function refresh() {
      if (disposed || refreshing) return;
      refreshing = true; renderSetup();
      await Promise.allSettled([
        loadStatus(),
        loadBatches(),
      ]);
      loading = false; refreshing = false; renderSetup(); renderBatches(); schedule();
    }
    $("[data-ingestion-provider]").onchange = (event) => {
      $("[data-ingestion-key]").value = "";
      connectionDraft = { provider: event.target.value, base_url: event.target.value === "paratera" ? DEFAULT_BASE : "", model: "" };
      connectionEdited = true; modelEdited = false; configError = ""; configSuccess = ""; renderSetup();
    };
    $("[data-ingestion-base-url]").oninput = (event) => {
      const key = $("[data-ingestion-key]"); key.value = "";
      connectionDraft.base_url = event.target.value; connectionEdited = true;
      key.required = !status?.provider?.configured || endpointChanged();
      key.placeholder = key.required ? "首次连接或更换地址时填写此服务的密钥" : "留空保留当前密钥来源";
    };
    $("[data-ingestion-model]").oninput = (event) => { connectionDraft.model = event.target.value; connectionEdited = true; modelEdited = true; };
    $("[data-ingestion-files]").onchange = (event) => selectFiles(event.target.files);
    $("[data-ingestion-start]").onclick = start;
    $("[data-ingestion-config-form]").onsubmit = (event) => configure(event);
    $("[data-ingestion-reuse]").onclick = () => configure(null, true);
    $("[data-ingestion-refresh]").onclick = refresh;
    const dropzone = $("[data-ingestion-dropzone]");
    ["dragenter", "dragover"].forEach((event) => dropzone.addEventListener(event, (e) => { e.preventDefault(); if (!submitting) dropzone.classList.add("is-dragging"); }));
    ["dragleave", "drop"].forEach((event) => dropzone.addEventListener(event, (e) => { e.preventDefault(); dropzone.classList.remove("is-dragging"); }));
    dropzone.addEventListener("drop", (event) => selectFiles(event.dataTransfer.files));
    renderSetup(); renderBatches(); refresh();
    return () => { disposed = true; stopTimer(); controller.abort(); files = []; $("[data-ingestion-key]").value = ""; };
  }

  window.KGIngestion = { mount };
})();
