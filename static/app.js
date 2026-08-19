"use strict";

const state = {
  files: [],
  ledger: null,
  batchId: null,
  batch: null,
  records: [],
  filter: "ALL",
  query: "",
  selectedId: null,
  selectedDetail: null,
  reviewTasks: new Map(),
  pollTimer: null,
  pollFailures: 0,
  capabilities: null,
  batchHistory: [],
  historyLoading: false,
  modelSettings: null,
};

const $ = (id) => document.getElementById(id);

const FIELD_LABELS = {
  certificate_number: "证书编号",
  issuer_name: "发证机构",
  institution: "发证机构",
  instrument_name: "器具名称",
  unified_number: "统一编号",
  equipment_number: "统一编号",
  model: "型号规格",
  model_specification: "型号规格",
  serial_number: "出厂编号",
  sample_number: "样品编号（内部）",
  verification_record_id: "二维码验真记录号",
  factory_number: "出厂编号",
  calibration_date: "检定/校准日期",
  verification_date: "检定/校准日期",
  due_date: "有效期至",
  valid_until: "有效期至",
  expiry_date: "有效期至",
  verification_result: "检定/校准结论",
  conclusion: "结论",
  reference_document: "依据规程/文件",
  regulation: "依据规程",
  traceability: "溯源信息",
  seal_signature_status: "印章/签名",
  measurement_results: "测量结果",
  issue_date: "签发日期",
  client_name: "委托单位",
  manufacturer: "制造单位",
};

const STATUS_LABELS = {
  UPLOADED: "已上传",
  PRECHECKED: "预检完成",
  QR_VISION_RUNNING: "图像识别中",
  "QR/VISION_RUNNING": "图像识别中",
  COMPARING: "规则比对中",
  SEMANTIC_REVIEW: "语义主审中",
  MODEL_ARBITRATION: "模型仲裁中",
  QUEUED: "等待处理",
  PROCESSING: "处理中",
  AUTO_PASSED: "自动通过",
  PASS: "通过",
  MANUAL_PASSED: "人工通过",
  HUMAN_REVIEW: "待人工复核",
  REVIEW: "待人工复核",
  AUTO_FAILED: "自动不通过",
  MANUAL_FAILED: "人工不通过",
  FAILED: "不通过",
  PROCESSING_FAILED: "技术失败",
  PARTIAL_FAILED: "部分处理失败",
  FINALIZED: "已完成",
  COMPLETED: "已完成",
};

const TERMINAL_BATCH_STATUSES = new Set(["FINALIZED", "COMPLETED", "FAILED", "PARTIAL_FAILED"]);
const FIELD_PRIORITY = Object.keys(FIELD_LABELS);

function createElement(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function firstDefined(...values) {
  return values.find((value) => value !== undefined && value !== null && value !== "");
}

function asObject(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function asArray(value) {
  return Array.isArray(value) ? value : [];
}

function apiDetail(body, fallback) {
  if (typeof body?.detail === "string") return body.detail;
  if (typeof body?.message === "string") return body.message;
  return fallback;
}

async function apiFetch(url, options = {}) {
  const response = await fetch(url, { cache: "no-store", ...options });
  let body = null;
  const contentType = response.headers.get("content-type") || "";
  if (contentType.includes("application/json")) {
    try { body = await response.json(); } catch { body = null; }
  }
  if (!response.ok) throw new Error(apiDetail(body, `请求失败（HTTP ${response.status}）`));
  return body;
}

function safeApiUrl(candidate, fallback = "") {
  const value = typeof candidate === "string" && candidate.trim() ? candidate.trim() : fallback;
  if (!value) return "";
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin !== window.location.origin || !url.pathname.startsWith("/api/")) return "";
    return `${url.pathname}${url.search}`;
  } catch {
    return "";
  }
}

function showToast(message, kind = "info") {
  const region = $("toastRegion");
  const toast = createElement("div", `toast is-${kind}`, message);
  toast.setAttribute("role", kind === "error" ? "alert" : "status");
  region.append(toast);
  window.setTimeout(() => toast.remove(), 4800);
}

function humanSize(bytes) {
  const size = Number(bytes) || 0;
  if (size < 1024) return `${size} B`;
  if (size < 1024 ** 2) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 ** 3) return `${(size / 1024 ** 2).toFixed(1)} MB`;
  return `${(size / 1024 ** 3).toFixed(2)} GB`;
}

function updateClock() {
  const now = new Date();
  $("clock").textContent = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hour12: false,
  }).format(now).replaceAll("/", "-");
}

function modelDescriptor(capabilities, name) {
  const models = asObject(capabilities.models);
  if (models[name]) return asObject(models[name]);
  if (name === "qwen") {
    const legacy = asObject(capabilities.vision_fallback);
    return { configured: Boolean(legacy.configured), status: legacy.configured ? "configured" : "unconfigured", model: legacy.model };
  }
  return { configured: false, status: "unconfigured" };
}

function renderModelStatus(containerId, descriptor) {
  const chip = $(containerId);
  const light = chip.querySelector(".status-light");
  const stateText = chip.querySelector(".model-state");
  const configured = descriptor.configured !== false && descriptor.enabled !== false;
  const rawStatus = String(firstDefined(descriptor.status, configured ? "configured" : "unconfigured")).toLowerCase();
  let css = "is-degraded";
  let label = "已配置";
  if (!configured || ["offline", "unconfigured", "missing", "disabled"].includes(rawStatus)) {
    css = "is-offline";
    label = "未配置";
  } else if (["online", "healthy", "ok", "available", "connected"].includes(rawStatus)) {
    css = "is-online";
    label = "服务正常";
  } else if (["error", "unhealthy", "failed"].includes(rawStatus)) {
    css = "is-offline";
    label = "连接异常";
  }
  light.className = `status-light ${css}`;
  stateText.textContent = label;
  const modelName = firstDefined(descriptor.model, descriptor.model_name);
  if (modelName) chip.title = String(modelName);
}

async function loadCapabilities() {
  try {
    const capabilities = await apiFetch("/api/capabilities");
    state.capabilities = asObject(capabilities);
    let health = {};
    try {
      health = asObject(await apiFetch("/api/models/health"));
    } catch {
      health = {};
    }
    const healthModels = asObject(firstDefined(health.models, health.providers, health));
    const mergedModel = (name) => ({
      ...modelDescriptor(state.capabilities, name),
      ...asObject(healthModels[name]),
    });
    renderModelStatus("modelQwen", mergedModel("qwen"));
    renderModelStatus("modelGlm", mergedModel("glm"));
    renderModelStatus("modelDeepseek", mergedModel("deepseek"));
    const extensions = asArray(state.capabilities.supported_extensions).join("、") || "PDF、ZIP";
    const maxFiles = firstDefined(state.capabilities.max_batch_files, "—");
    const maxSize = firstDefined(state.capabilities.max_file_size_mb, "—");
    $("capabilityText").textContent = `支持 ${extensions} · 单批最多 ${maxFiles} 个附件 · 单文件不超过 ${maxSize} MB`;
  } catch (error) {
    ["modelQwen", "modelGlm", "modelDeepseek"].forEach((id) => renderModelStatus(id, { configured: false, status: "error" }));
    $("capabilityText").textContent = "本机审核能力读取失败，请检查服务状态";
    showToast(error.message, "error");
  }
}

const MODEL_SETTING_IDS = {
  qwen: { base: "qwenBaseUrl", model: "qwenModel", key: "qwenApiKey", clear: "qwenClearKey", state: "qwenConfigState", result: "qwenTestResult" },
  glm: { base: "glmBaseUrl", model: "glmModel", key: "glmApiKey", clear: "glmClearKey", state: "glmConfigState", result: "glmTestResult" },
  deepseek: { base: "deepseekBaseUrl", model: "deepseekModel", key: "deepseekApiKey", clear: "deepseekClearKey", state: "deepseekConfigState", result: "deepseekTestResult" },
};

function keySourceLabel(descriptor) {
  const source = String(descriptor.api_key_source || "not_configured");
  if (source === "secure_store") return "已安全保存";
  if (source === "environment") return "来自环境变量";
  return "未配置";
}

function renderSavedProviderSetting(kind, descriptor) {
  const ids = MODEL_SETTING_IDS[kind];
  $(ids.base).value = String(descriptor.base_url || "");
  $(ids.model).value = String(descriptor.model || "");
  $(ids.key).value = "";
  $(ids.clear).checked = false;
  $(ids.key).disabled = false;
  const stateNode = $(ids.state);
  stateNode.textContent = keySourceLabel(descriptor);
  stateNode.className = `config-state ${descriptor.configured ? "is-configured" : "is-missing"}`;
  $(ids.key).placeholder = descriptor.configured ? "已配置；留空保持不变" : "输入新的 API Key";
}

async function loadModelSettings({ quiet = false } = {}) {
  try {
    const payload = asObject(await apiFetch("/api/model-settings"));
    state.modelSettings = payload;
    const providers = asObject(payload.providers);
    Object.keys(MODEL_SETTING_IDS).forEach((kind) => renderSavedProviderSetting(kind, asObject(providers[kind])));
    const storage = asObject(payload.storage);
    if (storage.protection) {
      $("settingsStorageText").textContent = `API Key 使用 ${storage.protection} 加密，保存于 ${storage.api_keys}；地址和模型保存于 ${storage.non_secret}。${storage.effective}。`;
    }
    $("apiSettingsFeedback").textContent = "设置已载入";
  } catch (error) {
    $("apiSettingsFeedback").textContent = "设置读取失败";
    if (!quiet) showToast(error.message, "error");
  }
}

function providerDraft(kind) {
  const ids = MODEL_SETTING_IDS[kind];
  const apiKey = $(ids.key).value.trim();
  const draft = {
    base_url: $(ids.base).value.trim(),
    model: $(ids.model).value.trim(),
    clear_api_key: $(ids.clear).checked,
  };
  if (apiKey) draft.api_key = apiKey;
  return draft;
}

async function saveApiSettings(event) {
  event.preventDefault();
  const form = event.currentTarget;
  if (!form.reportValidity()) return;
  const button = $("saveApiSettingsButton");
  button.disabled = true;
  button.textContent = "正在安全保存…";
  $("apiSettingsFeedback").textContent = "正在保存";
  try {
    await apiFetch("/api/model-settings", {
      method: "PUT",
      headers: {
        "Content-Type": "application/json",
        "X-Certificate-Review-Action": "model-settings-save",
      },
      body: JSON.stringify({
        qwen: providerDraft("qwen"),
        glm: providerDraft("glm"),
        deepseek: providerDraft("deepseek"),
      }),
    });
    showToast("API 设置已安全保存并立即生效", "success");
    await Promise.all([loadModelSettings({ quiet: true }), loadCapabilities()]);
    $("apiSettingsFeedback").textContent = "保存成功，已立即生效";
  } catch (error) {
    $("apiSettingsFeedback").textContent = "保存失败，请检查输入";
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "保存 API 设置";
  }
}

async function testProviderConnection(kind, button) {
  const ids = MODEL_SETTING_IDS[kind];
  const baseUrl = $(ids.base);
  const model = $(ids.model);
  if ($(ids.clear).checked) {
    $(ids.result).textContent = "已选择清除密钥，无法测试连接";
    $(ids.result).className = "inline-feedback is-error";
    return;
  }
  if (!baseUrl.reportValidity() || !model.reportValidity()) return;
  const result = $(ids.result);
  button.disabled = true;
  button.textContent = "连接中…";
  result.textContent = "正在请求模型网关";
  result.className = "inline-feedback is-checking";
  try {
    const draft = providerDraft(kind);
    const payload = await apiFetch("/api/model-settings/test", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Certificate-Review-Action": "model-settings-test",
      },
      body: JSON.stringify({ provider: kind, ...draft }),
    });
    result.textContent = `连接成功 · ${Number(payload.latency_ms) || 0} ms`;
    result.className = "inline-feedback is-success";
    showToast(`${kind === "qwen" ? "千问" : kind === "glm" ? "GLM" : "DeepSeek"}连接测试成功`, "success");
  } catch (error) {
    result.textContent = error.message;
    result.className = "inline-feedback is-error";
  } finally {
    button.disabled = false;
    button.textContent = "测试连接";
  }
}

function bindApiSettingsControls() {
  $("apiSettingsForm").addEventListener("submit", saveApiSettings);
  $("apiSettingsForm").addEventListener("input", () => {
    $("apiSettingsFeedback").textContent = "有未保存的修改";
  });
  document.querySelectorAll("[data-key-toggle]").forEach((button) => {
    button.addEventListener("click", () => {
      const input = $(button.dataset.keyToggle);
      const reveal = input.type === "password";
      input.type = reveal ? "text" : "password";
      button.textContent = reveal ? "隐藏" : "显示";
    });
  });
  document.querySelectorAll("[data-provider-test]").forEach((button) => {
    button.addEventListener("click", () => testProviderConnection(button.dataset.providerTest, button));
  });
  Object.values(MODEL_SETTING_IDS).forEach((ids) => {
    $(ids.clear).addEventListener("change", () => {
      $(ids.key).disabled = $(ids.clear).checked;
      if ($(ids.clear).checked) $(ids.key).value = "";
    });
  });
}

function allowedCertificateFile(file) {
  return /\.(pdf|zip)$/i.test(file.name);
}

function mergeFiles(fileList) {
  const existing = new Set(state.files.map((file) => `${file.name}|${file.size}|${file.lastModified}`));
  let rejected = 0;
  Array.from(fileList || []).forEach((file) => {
    if (!allowedCertificateFile(file)) { rejected += 1; return; }
    const key = `${file.name}|${file.size}|${file.lastModified}`;
    if (!existing.has(key)) {
      state.files.push(file);
      existing.add(key);
    }
  });
  if (rejected) showToast(`已忽略 ${rejected} 个非 PDF/ZIP 文件`, "error");
  renderPreflight();
}

function renderPreflight() {
  const hasFiles = state.files.length > 0;
  $("preflight").classList.toggle("hidden", !hasFiles);
  $("startButton").disabled = !hasFiles;
  $("clearButton").disabled = !hasFiles && !state.ledger;
  const totalBytes = state.files.reduce((total, file) => total + file.size, 0);
  $("selectionSummary").textContent = `${state.files.length} 个附件 · ${humanSize(totalBytes)}`;
  const list = $("fileList");
  list.replaceChildren();
  state.files.forEach((file, index) => {
    const item = createElement("li");
    item.append(
      createElement("span", "file-name", file.name),
      createElement("span", "file-size", humanSize(file.size)),
    );
    const remove = createElement("button", "", "移除");
    remove.type = "button";
    remove.setAttribute("aria-label", `移除 ${file.name}`);
    remove.addEventListener("click", () => {
      state.files.splice(index, 1);
      renderPreflight();
    });
    item.append(remove);
    list.append(item);
  });
}

function clearSelection() {
  state.files = [];
  state.ledger = null;
  $("fileInput").value = "";
  $("ledgerInput").value = "";
  $("folderInput").value = "";
  $("ledgerName").textContent = "未选择台账，仍可进行证书内部一致性审核";
  renderPreflight();
}

function bindUploadControls() {
  $("fileInput").addEventListener("change", (event) => mergeFiles(event.target.files));
  $("folderInput").addEventListener("change", (event) => mergeFiles(event.target.files));
  $("ledgerInput").addEventListener("change", (event) => {
    const file = event.target.files?.[0] || null;
    if (file && !/\.(json|csv)$/i.test(file.name)) {
      event.target.value = "";
      state.ledger = null;
      showToast("台账仅支持 JSON 或 CSV", "error");
    } else {
      state.ledger = file;
      $("ledgerName").textContent = file ? `${file.name} · ${humanSize(file.size)}` : "未选择台账，仍可进行证书内部一致性审核";
    }
    $("clearButton").disabled = !state.files.length && !state.ledger;
  });
  $("clearButton").addEventListener("click", clearSelection);

  const dropzone = $("dropzone");
  ["dragenter", "dragover"].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.add("is-dragging");
  }));
  ["dragleave", "drop"].forEach((name) => dropzone.addEventListener(name, (event) => {
    event.preventDefault();
    dropzone.classList.remove("is-dragging");
  }));
  dropzone.addEventListener("drop", (event) => mergeFiles(event.dataTransfer?.files));
  dropzone.addEventListener("keydown", (event) => {
    if (event.key === "Enter" || event.key === " ") {
      event.preventDefault();
      $("fileInput").click();
    }
  });
}

function unwrapBatch(payload) {
  const wrapper = asObject(payload);
  const core = asObject(wrapper.batch);
  const batch = Object.keys(core).length ? { ...core } : { ...wrapper };
  batch.summary = asObject(firstDefined(wrapper.summary, batch.summary));
  batch.records = asArray(firstDefined(wrapper.certificates, batch.certificates, wrapper.records, batch.records, wrapper.items, batch.items));
  return batch;
}

function batchIdOf(batch) {
  return String(firstDefined(batch.id, batch.batch_id, ""));
}

function formatLocalDate(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit",
  }).format(date);
}

function renderBatchHistory() {
  const section = $("historySection");
  const list = $("historyList");
  list.replaceChildren();
  section.classList.toggle("hidden", !state.batchHistory.length);
  state.batchHistory.slice(0, 12).forEach((batch) => {
    const id = batchIdOf(batch);
    if (!id) return;
    const counts = normalizedSummary(batch);
    const status = firstDefined(batch.status, batch.workflow_state, "QUEUED");
    const button = createElement("button", `history-item ${id === state.batchId ? "is-active" : ""}`);
    button.type = "button";
    button.setAttribute("role", "listitem");
    button.setAttribute("aria-label", `打开批次 ${id}`);
    button.append(
      createElement("strong", "", `批次 ${id}`),
      createElement("span", `status-badge ${statusClass(status)}`, statusLabel(status)),
      createElement("small", "", formatLocalDate(firstDefined(batch.created_at, batch.updated_at))),
      createElement("small", "history-counts", `${counts.total} 份 · 通过 ${counts.pass} · 待复核 ${counts.review} · 不通过 ${counts.fail}`),
    );
    button.addEventListener("click", () => openHistoricalBatch(id));
    list.append(button);
  });
}

async function openHistoricalBatch(batchId, { quiet = false } = {}) {
  if (!batchId) return;
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.batchId = String(batchId);
  state.selectedId = null;
  state.selectedDetail = null;
  $("detailPanel").classList.add("hidden");
  renderBatchHistory();
  try {
    const payload = await apiFetch(`/api/batches/${encodeURIComponent(batchId)}`);
    renderBatch(payload);
    await loadReviewTasks();
    const status = String(firstDefined(state.batch.status, state.batch.workflow_state, "PROCESSING")).toUpperCase();
    if (!TERMINAL_BATCH_STATUSES.has(status)) schedulePoll(1200);
    if (!quiet) $("workspace").scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    if (!quiet) showToast(`无法恢复历史批次：${error.message}`, "error");
  }
}

async function loadBatchHistory({ restoreLatest = false, manual = false } = {}) {
  if (state.historyLoading) return;
  state.historyLoading = true;
  const refreshButton = $("refreshHistoryButton");
  if (manual) refreshButton.disabled = true;
  try {
    const payload = await apiFetch("/api/batches");
    const items = asArray(firstDefined(payload?.items, payload?.batches, payload));
    state.batchHistory = items.map(unwrapBatch).sort((a, b) => {
      const left = new Date(firstDefined(a.created_at, a.updated_at, 0)).getTime() || 0;
      const right = new Date(firstDefined(b.created_at, b.updated_at, 0)).getTime() || 0;
      return right - left;
    });
    renderBatchHistory();
    if (restoreLatest && !state.batchId && state.batchHistory.length) {
      await openHistoricalBatch(batchIdOf(state.batchHistory[0]), { quiet: true });
    } else if (manual) {
      showToast("历史批次已刷新", "success");
    }
  } catch (error) {
    if (manual) showToast(`读取历史批次失败：${error.message}`, "error");
  } finally {
    state.historyLoading = false;
    refreshButton.disabled = false;
  }
}

function recordId(record) {
  return String(firstDefined(record.id, record.certificate_id, record.record_id, ""));
}

function recordFields(record) {
  return asObject(firstDefined(record.fields, record.extracted_fields, record.vision_fields));
}

function effectiveRecordStatus(record) {
  const status = String(firstDefined(record.status, record.workflow_state, "QUEUED")).toUpperCase();
  if (status !== "FINALIZED") return status;
  const metadata = asObject(record.metadata);
  const decision = String(firstDefined(record.final_decision, metadata.final_decision, "")).toUpperCase();
  if (["PASS", "PASSED", "APPROVE", "APPROVED"].includes(decision)) return "MANUAL_PASSED";
  if (["FAIL", "FAILED", "REJECT", "REJECTED"].includes(decision)) return "MANUAL_FAILED";
  if (["NEEDS_EVIDENCE", "UNCERTAIN", "REVIEW"].includes(decision)) return "HUMAN_REVIEW";
  return status;
}

function fieldRawValue(field) {
  if (field === undefined || field === null) return "";
  if (typeof field === "object") return firstDefined(field.value, field.text, field.normalized_value, field.raw_value, "");
  return field;
}

function statusCategory(statusValue) {
  const status = String(statusValue || "QUEUED").toUpperCase();
  if (status.includes("PASSED") || status === "PASS") return "PASS";
  if (["HUMAN_REVIEW", "REVIEW"].includes(status)) return "REVIEW";
  if (status.includes("FAILED") || status === "FAIL") return "FAIL";
  return "PROCESSING";
}

function statusClass(statusValue) {
  const status = String(statusValue || "").toUpperCase();
  if (status === "PROCESSING_FAILED") return "is-technical";
  const category = statusCategory(status);
  return { PASS: "is-pass", REVIEW: "is-review", FAIL: "is-fail", PROCESSING: "is-processing" }[category] || "is-neutral";
}

function statusLabel(statusValue) {
  const status = String(statusValue || "QUEUED").toUpperCase();
  return STATUS_LABELS[status] || status.replaceAll("_", " ");
}

function riskValue(record) {
  const direct = firstDefined(record.risk, record.risk_level, record.severity);
  if (direct) return String(direct).toUpperCase();
  const issues = asArray(record.issues);
  if (issues.some((item) => ["HIGH", "ERROR", "CRITICAL"].includes(String(item.severity).toUpperCase()))) return "HIGH";
  if (issues.length) return "MEDIUM";
  if (statusCategory(effectiveRecordStatus(record)) === "PASS") return "LOW";
  return "UNKNOWN";
}

function riskLabel(risk) {
  return { HIGH: "高风险", MEDIUM: "中风险", LOW: "低风险", UNKNOWN: "待评估", CRITICAL: "高风险" }[risk] || risk;
}

function riskClass(risk) {
  if (["HIGH", "CRITICAL"].includes(risk)) return "is-high";
  if (risk === "MEDIUM") return "is-medium";
  if (risk === "LOW") return "is-low";
  return "is-unknown";
}

function fieldText(record, keys) {
  const fields = recordFields(record);
  for (const key of keys) {
    const value = fieldRawValue(fields[key]);
    if (value !== "") return String(value);
    if (record[key] !== undefined && record[key] !== null && record[key] !== "") return String(record[key]);
  }
  return "";
}

function calculateCounts(records) {
  const counts = {
    total: records.length, pass: 0, review: 0, fail: 0, processing: 0, technical: 0,
    queued: 0, active: 0, vision: 0, comparing: 0, semantic: 0,
  };
  records.forEach((record) => {
    const status = effectiveRecordStatus(record);
    if (status === "PROCESSING_FAILED") {
      counts.technical += 1;
      return;
    }
    const category = statusCategory(status);
    counts[category.toLowerCase()] += 1;
    if (category !== "PROCESSING") return;
    if (["QUEUED", "UPLOADED"].includes(status)) counts.queued += 1;
    else {
      counts.active += 1;
      if (["PRECHECKED", "QR_VISION_RUNNING", "QR/VISION_RUNNING"].includes(status)) counts.vision += 1;
      else if (status === "COMPARING") counts.comparing += 1;
      else if (["SEMANTIC_REVIEW", "MODEL_ARBITRATION"].includes(status)) counts.semantic += 1;
    }
  });
  return counts;
}

function normalizedSummary(batch) {
  const calculated = calculateCounts(batch.records);
  const summary = asObject(batch.summary);
  const queuedAndProcessing = Number(summary.queued || 0) + Number(summary.processing || 0);
  const hasRecords = batch.records.length > 0;
  return {
    total: Number(firstDefined(summary.total, summary.certificate_count, calculated.total)) || 0,
    pass: hasRecords ? calculated.pass : Number(firstDefined(summary.pass, summary.passed, summary.auto_passed, 0)) || 0,
    review: hasRecords ? calculated.review : Number(firstDefined(summary.review, summary.human_review, summary.pending_review, 0)) || 0,
    fail: hasRecords ? calculated.fail : Number(firstDefined(summary.fail, summary.failed, summary.auto_failed, 0)) || 0,
    processing: hasRecords ? calculated.processing : Number(firstDefined(queuedAndProcessing || undefined, summary.in_progress, 0)) || 0,
    technical: hasRecords ? calculated.technical : Number(firstDefined(summary.technical, summary.processing_failed, 0)) || 0,
    queued: hasRecords ? calculated.queued : Number(summary.queued || 0),
    active: hasRecords ? calculated.active : Number(summary.processing || 0),
    vision: hasRecords ? calculated.vision : 0,
    comparing: hasRecords ? calculated.comparing : 0,
    semantic: hasRecords ? calculated.semantic : 0,
  };
}

function liveStageDescription(statusValue, counts) {
  const status = String(statusValue || "QUEUED").toUpperCase();
  if (status !== "PROCESSING") return stageDescription(status);
  const completed = counts.pass + counts.review + counts.fail + counts.technical;
  const stages = [];
  if (counts.vision) stages.push(`${counts.vision}份图像识别`);
  if (counts.comparing) stages.push(`${counts.comparing}份规则比对`);
  if (counts.semantic) stages.push(`${counts.semantic}份文本分析`);
  const otherActive = Math.max(0, counts.active - counts.vision - counts.comparing - counts.semantic);
  if (otherActive) stages.push(`${otherActive}份处理中`);
  if (counts.queued) stages.push(`${counts.queued}份排队`);
  stages.push(`${completed}份已完成`);
  return stages.join(" · ");
}

function stageDescription(statusValue) {
  const status = String(statusValue || "QUEUED").toUpperCase();
  const labels = {
    QUEUED: "等待审核工作线程",
    UPLOADED: "已保存原始附件",
    PRECHECKED: "格式与安全预检完成",
    PROCESSING: "正在处理证书",
    QR_VISION_RUNNING: "正在执行二维码与千问视觉识别",
    "QR/VISION_RUNNING": "正在执行二维码与千问视觉识别",
    COMPARING: "正在执行三方规则比对",
    SEMANTIC_REVIEW: "GLM 正在分析规则无法确定的差异",
    MODEL_ARBITRATION: "DeepSeek 正在独立仲裁高风险差异",
    FINALIZED: "审核结果与审计记录已固化",
    COMPLETED: "批次处理完成",
    FAILED: "批次处理失败",
  };
  return labels[status] || "正在更新审核状态";
}

function deriveProgress(batch, counts) {
  const explicit = Number(firstDefined(batch.progress, batch.progress_percent, batch.percent));
  if (Number.isFinite(explicit)) return Math.max(0, Math.min(100, explicit <= 1 ? explicit * 100 : explicit));
  if (!counts.total) return 0;
  const completed = counts.pass + counts.review + counts.fail + counts.technical;
  const weightedActive = counts.vision * 0.45 + counts.comparing * 0.72
    + counts.semantic * 0.88
    + Math.max(0, counts.active - counts.vision - counts.comparing - counts.semantic) * 0.2;
  return Math.round(((completed + weightedActive) / counts.total) * 100);
}

function setExportLinks() {
  const batchPrefix = `/api/batches/${encodeURIComponent(state.batchId)}`;
  const prefix = `${batchPrefix}/export`;
  $("csvExport").href = `${prefix}.csv`;
  $("jsonExport").href = `${prefix}.json`;
  $("auditExport").href = `${batchPrefix}/audit.zip`;
}

function renderSummaryCards(counts) {
  const cards = [
    ["证书总数", counts.total, "is-total"],
    ["审核通过", counts.pass, "is-pass"],
    ["待人工复核", counts.review, "is-review"],
    ["审核不通过", counts.fail, "is-fail"],
    ["处理中 / 排队", `${counts.active} / ${counts.queued}`, "is-processing"],
    ["技术失败", counts.technical, "is-technical"],
  ];
  const container = $("summaryCards");
  container.replaceChildren();
  cards.forEach(([label, value, className]) => {
    const card = createElement("article", `summary-card ${className}`);
    card.append(createElement("span", "", label), createElement("strong", "", value));
    container.append(card);
  });
}

function updateFilterCounts(records) {
  const counts = calculateCounts(records);
  const map = { ALL: counts.total, PASS: counts.pass, REVIEW: counts.review, FAIL: counts.fail, PROCESSING: counts.processing };
  $("filterBar").querySelectorAll(".filter-button").forEach((button) => {
    const count = button.querySelector("span");
    count.textContent = String(map[button.dataset.filter] || 0);
  });
}

function searchableText(record) {
  return [
    record.filename,
    record.display_name,
    fieldText(record, ["instrument_name"]),
    fieldText(record, ["unified_number", "equipment_number"]),
    fieldText(record, ["certificate_number"]),
    fieldText(record, ["issuer_name", "institution"]),
  ].filter(Boolean).join(" ").toLocaleLowerCase("zh-CN");
}

function filteredRecords() {
  const query = state.query.trim().toLocaleLowerCase("zh-CN");
  return state.records.filter((record) => {
    const status = effectiveRecordStatus(record);
    const categoryMatches = state.filter === "ALL" || statusCategory(status) === state.filter;
    return categoryMatches && (!query || searchableText(record).includes(query));
  });
}

function renderRecordRows() {
  const tbody = $("recordRows");
  tbody.replaceChildren();
  const records = filteredRecords();
  $("emptyRecords").classList.toggle("hidden", records.length > 0);
  records.forEach((record) => {
    const id = recordId(record);
    const row = createElement("tr", id === state.selectedId ? "is-selected" : "");
    row.tabIndex = 0;
    row.dataset.recordId = id;
    const index = state.records.indexOf(record) + 1;
    const filename = String(firstDefined(record.filename, record.original_filename, record.display_name, `证书 ${index}`));
    const instrument = fieldText(record, ["instrument_name"]) || filename;
    const certNo = fieldText(record, ["certificate_number", "unified_number", "equipment_number"]) || "证书号待识别";
    const issuer = fieldText(record, ["issuer_name", "institution"]) || "待识别";
    const status = effectiveRecordStatus(record);
    const risk = riskValue(record);

    const main = createElement("td", "record-main");
    main.append(createElement("strong", "", instrument), createElement("small", "", certNo));
    const riskBadge = createElement("span", `risk-badge ${riskClass(risk)}`, riskLabel(risk));
    const statusBadge = createElement("span", `status-badge ${statusClass(status)}`, statusLabel(status));
    const action = createElement("button", "open-record", "查看");
    action.type = "button";
    action.setAttribute("aria-label", `查看 ${instrument} 审核详情`);
    action.addEventListener("click", (event) => {
      event.stopPropagation();
      openRecord(record);
    });
    row.append(
      createElement("td", "", index), main, createElement("td", "", issuer),
      createElement("td", ""), createElement("td", ""), createElement("td", ""),
    );
    row.children[3].append(riskBadge);
    row.children[4].append(statusBadge);
    row.children[5].append(action);
    row.addEventListener("click", () => openRecord(record));
    row.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        openRecord(record);
      }
    });
    tbody.append(row);
  });
}

function renderBatch(payload) {
  const batch = unwrapBatch(payload);
  state.batch = batch;
  state.records = batch.records;
  state.batchId = String(firstDefined(batch.id, batch.batch_id, state.batchId));
  const historyIndex = state.batchHistory.findIndex((item) => batchIdOf(item) === state.batchId);
  if (historyIndex >= 0) state.batchHistory[historyIndex] = batch;
  else state.batchHistory.unshift(batch);
  renderBatchHistory();
  const counts = normalizedSummary(batch);
  const progress = deriveProgress(batch, counts);
  const status = firstDefined(batch.status, batch.workflow_state, "PROCESSING");
  $("workspace").classList.remove("hidden");
  $("batchIdentity").textContent = `批次 ${state.batchId} · ${counts.total} 份证书`;
  $("batchStatus").textContent = statusLabel(status);
  $("batchStage").textContent = liveStageDescription(status, counts);
  $("lastUpdated").textContent = `更新于 ${new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
  }).format(new Date())}`;
  $("progressValue").textContent = `${progress}%`;
  $("progressBar").style.width = `${progress}%`;
  $("progressBar").parentElement.setAttribute("aria-valuenow", String(progress));
  renderSummaryCards(counts);
  updateFilterCounts(state.records);
  renderRecordRows();
  setExportLinks();
  if (state.selectedId) {
    const current = state.records.find((record) => recordId(record) === state.selectedId);
    if (current && state.selectedDetail) refreshOpenDetailStatus(current);
  }
}

function refreshOpenDetailStatus(record) {
  const status = effectiveRecordStatus(record);
  $("detailStatus").className = `status-badge ${statusClass(status)}`;
  $("detailStatus").textContent = statusLabel(status);
  $("decisionText").textContent = statusLabel(status);
  $("decisionReason").textContent = firstDefined(
    record.final_reason, record.reason, stageDescription(status),
  );
}

async function createBatch() {
  if (!state.files.length) return;
  const startButton = $("startButton");
  startButton.disabled = true;
  startButton.textContent = "正在创建批次…";
  const form = new FormData();
  state.files.forEach((file) => form.append("files", file, file.name));
  if (state.ledger) form.append("ledger", state.ledger, state.ledger.name);
  try {
    const payload = await apiFetch("/api/batches", { method: "POST", body: form });
    const batch = unwrapBatch(payload);
    state.batchId = String(firstDefined(batch.id, batch.batch_id, payload?.batch_id));
    if (!state.batchId || state.batchId === "undefined") throw new Error("服务器未返回批次编号");
    renderBatch(payload);
    await loadReviewTasks();
    await loadBatchHistory();
    showToast(`批次已创建，共接收 ${state.files.length} 个附件`, "success");
    $("workspace").scrollIntoView({ behavior: "smooth", block: "start" });
    schedulePoll(500);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    startButton.disabled = false;
    startButton.textContent = "开始智能审核";
  }
}

function schedulePoll(delay = 1200) {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollTimer = window.setTimeout(refreshBatch, delay);
}

async function refreshBatch({ manual = false } = {}) {
  if (!state.batchId) return;
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  try {
    const payload = await apiFetch(`/api/batches/${encodeURIComponent(state.batchId)}`);
    state.pollFailures = 0;
    renderBatch(payload);
    await loadReviewTasks();
    const status = String(firstDefined(state.batch.status, state.batch.workflow_state, "PROCESSING")).toUpperCase();
    if (!TERMINAL_BATCH_STATUSES.has(status)) schedulePoll(1200);
    else if (manual) showToast("批次状态已刷新", "success");
  } catch (error) {
    state.pollFailures += 1;
    if (manual || state.pollFailures === 1) showToast(error.message, "error");
    if (state.pollFailures < 5) schedulePoll(Math.min(8000, 1200 * 2 ** state.pollFailures));
  }
}

async function loadReviewTasks() {
  try {
    const payload = await apiFetch("/api/review-tasks?status=OPEN");
    const items = asArray(firstDefined(payload?.items, payload?.review_tasks, payload));
    state.reviewTasks = new Map();
    items.forEach((item) => {
      const certificateId = String(firstDefined(item.certificate_id, item.certificate?.id, item.record_id, ""));
      if (certificateId) state.reviewTasks.set(certificateId, item);
    });
  } catch {
    state.reviewTasks = new Map();
  }
}

function unwrapDetail(payload, fallbackRecord) {
  const wrapper = asObject(payload);
  const directCertificate = wrapper.record_id || wrapper.certificate_id || wrapper.id ? wrapper : null;
  const certificate = asObject(firstDefined(wrapper.certificate, wrapper.record, directCertificate, fallbackRecord));
  return {
    ...wrapper,
    certificate,
    fields: firstDefined(wrapper.fields, wrapper.extracted_fields, certificate.fields, certificate.extracted_fields, {}),
    qr_fields: firstDefined(wrapper.qr_fields, certificate.qr_fields, {}),
    ledger_snapshot: firstDefined(wrapper.ledger_snapshot, wrapper.ledger, certificate.ledger_snapshot, certificate.ledger_fields, {}),
    comparisons: firstDefined(wrapper.comparisons, certificate.comparisons, []),
    issues: firstDefined(wrapper.issues, certificate.issues, []),
    model_decisions: firstDefined(wrapper.model_decisions, certificate.model_decisions, []),
    review_decisions: firstDefined(wrapper.review_decisions, certificate.review_decisions, []),
    review_task: firstDefined(wrapper.review_task, certificate.review_task, null),
  };
}

async function openRecord(record) {
  const id = recordId(record);
  if (!id) { showToast("该记录缺少证书编号，无法打开详情", "error"); return; }
  state.selectedId = id;
  renderRecordRows();
  $("detailPanel").classList.remove("hidden");
  $("detailTitle").textContent = "正在载入证书详情…";
  $("detailPanel").scrollIntoView({ behavior: "smooth", block: "start" });
  try {
    const payload = await apiFetch(`/api/certificates/${encodeURIComponent(id)}`);
    const detail = unwrapDetail(payload, record);
    state.selectedDetail = detail;
    renderDetail(detail, record);
  } catch (error) {
    const detail = unwrapDetail(record, record);
    state.selectedDetail = detail;
    renderDetail(detail, record);
    showToast(`详情接口暂不可用，已展示批次内证据：${error.message}`, "info");
  }
}

function valueMap(source) {
  const object = asObject(source);
  if (asObject(object.fields) !== object && Object.keys(asObject(object.fields)).length) return asObject(object.fields);
  const result = {};
  if (Array.isArray(source)) {
    source.forEach((item) => {
      const key = firstDefined(item.field, item.name, item.key);
      if (key) result[key] = item;
    });
    return result;
  }
  return object;
}

function deriveQrFields(detail) {
  const direct = valueMap(detail.qr_fields);
  if (Object.keys(direct).length) return direct;
  const codes = asArray(detail.certificate.qr_codes);
  const result = {};
  codes.forEach((code) => {
    Object.assign(result, valueMap(firstDefined(code.fields, code.parsed_fields, code.parsed, {})));
  });
  return result;
}

function orderedFieldKeys(...maps) {
  const all = new Set();
  maps.forEach((map) => Object.keys(map).forEach((key) => all.add(key)));
  return [...all].sort((a, b) => {
    const ai = FIELD_PRIORITY.indexOf(a);
    const bi = FIELD_PRIORITY.indexOf(b);
    if (ai === -1 && bi === -1) return a.localeCompare(b, "zh-CN");
    if (ai === -1) return 1;
    if (bi === -1) return -1;
    return ai - bi;
  });
}

function fieldLabel(key) {
  return FIELD_LABELS[key] || key.replaceAll("_", " ");
}

function fieldSourceInfo(rawSource, sourceKind) {
  const source = String(firstDefined(rawSource, sourceKind, "")).toLowerCase();
  if (source.includes("qwen")) return { label: "千问视觉", className: "is-qwen" };
  if (["pdf_text", "text_layer", "digital_text", "local_text"].some((name) => source.includes(name))) {
    return { label: "本地文字层", className: "is-local" };
  }
  if (source.includes("ocr")) return { label: "本地 OCR", className: "is-local" };
  if (sourceKind === "qr" || source.includes("qr")) return { label: "本地二维码解码", className: "is-local" };
  if (sourceKind === "ledger" || source.includes("ledger")) return { label: "冻结台账", className: "" };
  if (source && sourceKind === "vision") return { label: source, className: "" };
  return null;
}

function appendFieldValue(container, source, sourceKind) {
  const raw = source;
  const value = fieldRawValue(raw);
  if (value === "") {
    container.classList.add("is-missing");
    container.textContent = "未识别 / 无台账值";
    return;
  }
  const main = createElement("strong", "", typeof value === "object" ? JSON.stringify(value) : value);
  container.append(main);
  const meta = asObject(raw);
  const confidence = meta.confidence === undefined || meta.confidence === null || meta.confidence === ""
    ? Number.NaN : Number(meta.confidence);
  const page = firstDefined(meta.page, meta.page_number);
  const sourceInfo = fieldSourceInfo(meta.source, sourceKind);
  if (sourceInfo || Number.isFinite(confidence) || page || meta.evidence) {
    const line = createElement("small");
    const pieces = [];
    if (page) pieces.push(`第 ${page} 页`);
    if (meta.evidence) pieces.push(String(meta.evidence));
    if (sourceInfo) line.append(createElement("span", `source-tag ${sourceInfo.className}`, sourceInfo.label));
    if (pieces.length) line.append(document.createTextNode(pieces.join(" · ")));
    if (Number.isFinite(confidence)) {
      const badge = createElement("span", `confidence ${confidence < .9 ? "is-low" : ""}`, `${Math.round(confidence * 100)}%`);
      line.append(badge);
    }
    container.append(line);
  }
}

function renderFieldMatrix(detail) {
  const matrix = $("fieldMatrix");
  matrix.replaceChildren();
  const qr = deriveQrFields(detail);
  const vision = valueMap(detail.fields);
  const ledgerSource = asObject(detail.ledger_snapshot);
  const ledger = valueMap(firstDefined(ledgerSource.fields, ledgerSource.data, ledgerSource.snapshot, ledgerSource));
  const keys = orderedFieldKeys(qr, vision, ledger);

  const header = createElement("div", "field-row is-header");
  ["核验字段", "二维码", "版面识别", "冻结台账"].forEach((label) => header.append(createElement("div", "field-cell", label)));
  matrix.append(header);
  if (!keys.length) {
    const row = createElement("div", "field-row");
    const cell = createElement("div", "field-cell field-value is-missing", "当前尚无结构化字段证据");
    cell.style.gridColumn = "1 / -1";
    row.append(cell);
    matrix.append(row);
    return;
  }
  keys.forEach((key) => {
    const row = createElement("div", "field-row");
    row.append(createElement("div", "field-cell field-key", fieldLabel(key)));
    [[qr[key], "qr"], [vision[key], "vision"], [ledger[key], "ledger"]].forEach(([source, kind]) => {
      const cell = createElement("div", "field-cell field-value");
      appendFieldValue(cell, source, kind);
      row.append(cell);
    });
    matrix.append(row);
  });
}

function comparisonClass(statusValue) {
  const status = String(statusValue || "UNCERTAIN").toUpperCase();
  if (["MATCH", "EQUIVALENT", "CONSISTENT", "FORM_EQUIVALENT", "一致", "等价"].includes(status)) return "is-match";
  if (["MISMATCH", "DIFFERENT", "SUBSTANTIVE_DIFF", "不一致"].includes(status)) return "is-mismatch";
  if (status === "NOT_COMPARABLE") return "is-neutral";
  return "is-uncertain";
}

function comparisonLabel(statusValue) {
  const status = String(statusValue || "UNCERTAIN").toUpperCase();
  if (["MATCH", "EQUIVALENT", "CONSISTENT", "FORM_EQUIVALENT"].includes(status)) return "一致";
  if (["MISMATCH", "DIFFERENT", "SUBSTANTIVE_DIFF"].includes(status)) return "不一致";
  if (status === "MISSING") return "缺失 / 需复核";
  if (status === "NOT_COMPARABLE") return "独立编号（不比较）";
  return STATUS_LABELS[status] || "不确定";
}

function renderComparisons(detail) {
  const list = $("comparisonList");
  list.replaceChildren();
  const comparisons = asArray(detail.comparisons);
  const issues = asArray(detail.issues);
  comparisons.forEach((item) => {
    const status = firstDefined(item.status, item.result, item.decision, "UNCERTAIN");
    const block = createElement("article", `comparison-item ${comparisonClass(status)}`);
    const field = createElement("strong", "", fieldLabel(firstDefined(item.field, item.field_name, "字段比较")));
    const values = createElement("div", "comparison-values");
    const documentValue = firstDefined(item.document_value, item.vision_value, item.left_value, "—");
    const qrValue = firstDefined(item.qr_value, item.right_value, "—");
    const ledgerValue = firstDefined(item.ledger_value, "—");
    values.append(createElement("span", "", `版面：${documentValue} ｜ 二维码：${qrValue} ｜ 台账：${ledgerValue}`));
    const basis = firstDefined(item.basis, item.reason, item.detail);
    if (basis) values.append(createElement("small", "", basis));
    block.append(field, values, createElement("span", "comparison-result", comparisonLabel(status)));
    list.append(block);
  });
  issues.forEach((issue) => {
    const severity = String(firstDefined(issue.severity, issue.risk, "UNCERTAIN")).toUpperCase();
    const css = ["ERROR", "HIGH", "CRITICAL"].includes(severity) ? "is-mismatch" : "is-uncertain";
    const block = createElement("article", `comparison-item ${css}`);
    block.append(
      createElement("strong", "", firstDefined(issue.title, issue.code, "审核提示")),
      createElement("div", "comparison-values", firstDefined(issue.detail, issue.evidence, "需要人工核对")),
      createElement("span", "comparison-result", severity === "ERROR" ? "异常" : "复核"),
    );
    list.append(block);
  });
  if (!comparisons.length && !issues.length) {
    list.append(createElement("p", "empty-state", "暂无规则比较结果，可能仍在处理中。"));
  }
  const concerns = globalThis.CertificateReviewSummary?.countActiveConcerns(detail) ?? 0;
  $("comparisonSummary").textContent = comparisons.length || issues.length || concerns
    ? `${comparisons.length} 项比较 · ${concerns} 个需关注问题（按字段/问题去重）`
    : "等待规则引擎输出";
}

function modelName(decision) {
  const provider = String(firstDefined(decision.provider, decision.model_role, decision.model, "模型")).toLowerCase();
  if (provider.includes("glm")) {
    const configuredModel = String(firstDefined(decision.model, "")).toLowerCase();
    return configuredModel.includes("52") || configuredModel.includes("5.2")
      ? "GLM 5.2 主审"
      : "GLM 主审";
  }
  if (provider.includes("deepseek") || provider.includes("arbiter")) return "DeepSeek V4 仲裁";
  if (provider.includes("qwen")) return "千问视觉";
  return firstDefined(decision.model, decision.provider, "模型判断");
}

function renderModelDecisions(detail) {
  const container = $("modelDecisions");
  container.replaceChildren();
  const decisions = asArray(detail.model_decisions);
  if (!decisions.length) {
    const empty = createElement("article", "model-decision");
    empty.append(createElement("div", "model-decision-head", "文本模型尚未参与"));
    empty.append(createElement("p", "", "规则可直接确定，或当前记录尚未进入语义审核阶段。"));
    container.append(empty);
    return;
  }
  decisions.forEach((decision) => {
    const card = createElement("article", "model-decision");
    const head = createElement("div", "model-decision-head");
    head.append(createElement("strong", "", modelName(decision)));
    const outcome = firstDefined(decision.decision, decision.result, decision.verdict, "不确定");
    head.append(createElement("span", "", String(outcome)));
    card.append(head, createElement("p", "", firstDefined(decision.reason, decision.rationale, decision.explanation, "未提供模型理由")));
    const dl = createElement("dl");
    const confidence = Number(decision.confidence);
    if (Number.isFinite(confidence)) {
      dl.append(createElement("dt", "", "置信度"), createElement("dd", "", `${Math.round(confidence * 100)}%`));
    }
    if (decision.risk || decision.risk_level) {
      dl.append(createElement("dt", "", "风险"), createElement("dd", "", riskLabel(String(firstDefined(decision.risk, decision.risk_level)).toUpperCase())));
    }
    if (decision.model_version || decision.model) {
      dl.append(createElement("dt", "", "模型版本"), createElement("dd", "", firstDefined(decision.model_version, decision.model)));
    }
    if (dl.children.length) card.append(dl);
    container.append(card);
  });
}

function reviewDecisionLabel(value) {
  const decision = String(value || "").toUpperCase();
  return {
    PASS: "人工通过",
    FAIL: "人工不通过",
    NEEDS_EVIDENCE: "证据不足",
  }[decision] || decision || "人工决定";
}

function correctedFieldMap(decision) {
  const raw = firstDefined(decision.corrected_fields, decision.corrections, decision.corrected_fields_json, {});
  if (typeof raw === "string") {
    try { return asObject(JSON.parse(raw)); } catch { return {}; }
  }
  return asObject(raw);
}

function renderReviewHistory(detail) {
  const section = $("reviewHistorySection");
  const list = $("reviewHistoryList");
  const decisions = asArray(detail.review_decisions);
  list.replaceChildren();
  section.classList.toggle("hidden", !decisions.length);
  decisions.forEach((decision, index) => {
    const version = firstDefined(decision.version, index + 1);
    const card = createElement("article", "review-history-item");
    const head = createElement("div", "review-history-head");
    head.append(
      createElement("strong", "", `第 ${version} 版 · ${reviewDecisionLabel(firstDefined(decision.decision, decision.result))}`),
      createElement("span", "", `${firstDefined(decision.reviewer, "本机用户")} · ${formatLocalDate(decision.created_at)}`),
    );
    card.append(head);
    const comment = firstDefined(decision.comment, decision.reason, decision.rationale);
    if (comment) card.append(createElement("p", "", comment));
    const corrections = correctedFieldMap(decision);
    const entries = Object.entries(corrections);
    if (entries.length) {
      card.append(createElement("p", "correction-title", "人工修正字段（新版本）"));
      const values = createElement("dl", "correction-list");
      entries.forEach(([key, value]) => {
        values.append(
          createElement("dt", "", fieldLabel(key)),
          createElement("dd", "", typeof value === "object" ? JSON.stringify(value) : value),
        );
      });
      card.append(values);
    }
    list.append(card);
  });
}

function renderPreview(detail, id) {
  const frame = $("previewFrame");
  const url = safeApiUrl(
    firstDefined(detail.pdf_preview_url, detail.preview_url, detail.certificate.pdf_preview_url, detail.certificate.file_url),
    `/api/certificates/${encodeURIComponent(id)}/file`,
  );
  const type = String(firstDefined(detail.certificate.file_type, detail.certificate.mime_type, "pdf")).toLowerCase();
  if (frame.dataset.previewId === id && frame.dataset.previewUrl === url && frame.childElementCount) return;
  frame.replaceChildren();
  frame.dataset.previewId = id;
  frame.dataset.previewUrl = url;
  if (!url) {
    const placeholder = createElement("div", "preview-placeholder");
    placeholder.append(createElement("span", "", "▧"), createElement("strong", "", "暂无可用预览"), createElement("p", "", "原始文件链接未通过本地安全校验。"));
    frame.append(placeholder);
    return;
  }
  if (type.includes("image")) {
    const image = createElement("img");
    image.src = url;
    image.alt = "证书原始图像预览";
    frame.append(image);
  } else {
    const iframe = createElement("iframe");
    iframe.src = url;
    iframe.title = "证书 PDF 证据预览";
    frame.append(iframe);
  }
}

function findReviewTask(detail, id) {
  return detail.review_task || state.reviewTasks.get(id) || null;
}

function renderReviewForm(detail, id, status) {
  const task = findReviewTask(detail, id);
  const shouldReview = Boolean(task) || statusCategory(status) === "REVIEW";
  $("reviewSection").classList.toggle("hidden", !shouldReview);
  $("reviewForm").reset();
  $("correctedFields").value = "";
  if (shouldReview) {
    const taskId = firstDefined(task?.id, task?.review_task_id, detail.certificate.review_task_id, "");
    $("reviewTaskMeta").textContent = taskId ? `复核任务：${taskId}` : "复核任务正在建立";
    $("reviewForm").dataset.taskId = String(taskId);
    $("reviewForm").dataset.certificateId = id;
    $("submitReviewButton").disabled = !taskId;
  }
}

function renderDetail(detail, fallbackRecord) {
  const certificate = detail.certificate;
  const id = recordId(certificate) || recordId(fallbackRecord) || state.selectedId;
  const status = effectiveRecordStatus({ ...fallbackRecord, ...certificate });
  const filename = firstDefined(certificate.filename, certificate.original_filename, fallbackRecord.filename, "证书详情");
  const instrument = fieldText({ ...fallbackRecord, ...certificate, fields: detail.fields }, ["instrument_name"]);
  $("detailTitle").textContent = instrument || filename;
  $("detailStatus").className = `status-badge ${statusClass(status)}`;
  $("detailStatus").textContent = statusLabel(status);
  const sha = firstDefined(certificate.sha256, fallbackRecord.sha256, "未生成");
  const pageCount = firstDefined(certificate.page_count, certificate.pages, detail.page_count, "—");
  $("detailMeta").textContent = `${filename} · ${pageCount} 页 · SHA-256 ${sha}`;
  $("pageIndicator").textContent = `${pageCount} 页 · 原始只读证据`;
  $("decisionText").textContent = statusLabel(status);
  $("decisionReason").textContent = firstDefined(certificate.final_reason, certificate.reason, detail.final_reason, detail.summary, stageDescription(status));
  renderPreview(detail, id);
  renderFieldMatrix(detail);
  renderComparisons(detail);
  renderModelDecisions(detail);
  renderReviewHistory(detail);
  renderReviewForm(detail, id, status);
}

async function retrySelected() {
  if (!state.selectedId) return;
  const button = $("retryButton");
  button.disabled = true;
  button.textContent = "正在提交…";
  try {
    await apiFetch(`/api/certificates/${encodeURIComponent(state.selectedId)}/retry`, { method: "POST" });
    showToast("已创建新的处理运行，旧运行记录仍保留", "success");
    await refreshBatch({ manual: true });
    const record = state.records.find((item) => recordId(item) === state.selectedId);
    if (record) await openRecord(record);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "重新处理";
  }
}

async function submitReview(event) {
  event.preventDefault();
  const form = event.currentTarget;
  const taskId = form.dataset.taskId;
  const decision = new FormData(form).get("decision");
  const comment = $("reviewComment").value.trim();
  if (!taskId) { showToast("复核任务尚未生成，请刷新后重试", "error"); return; }
  if (!decision || !comment) { showToast("请选择复核结论并填写复核说明", "error"); return; }
  let correctedFields = {};
  const rawCorrections = $("correctedFields").value.trim();
  if (rawCorrections) {
    try {
      correctedFields = JSON.parse(rawCorrections);
      if (!correctedFields || typeof correctedFields !== "object" || Array.isArray(correctedFields)) throw new Error();
    } catch {
      showToast("修正字段必须是 JSON 对象，例如 {\"serial_number\":\"123\"}", "error");
      $("correctedFields").focus();
      return;
    }
  }
  const button = $("submitReviewButton");
  button.disabled = true;
  button.textContent = "正在提交…";
  try {
    await apiFetch(`/api/review-tasks/${encodeURIComponent(taskId)}/decision`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ decision, reason: comment, corrected_fields: correctedFields }),
    });
    showToast("人工复核决定已提交，新版本审计记录已生成", "success");
    await refreshBatch({ manual: true });
    const record = state.records.find((item) => recordId(item) === state.selectedId);
    if (record) await openRecord(record);
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
    button.textContent = "提交人工决定";
  }
}

function bindWorkspaceControls() {
  $("startButton").addEventListener("click", createBatch);
  $("refreshHistoryButton").addEventListener("click", () => loadBatchHistory({ manual: true }));
  $("refreshButton").addEventListener("click", () => refreshBatch({ manual: true }));
  $("searchInput").addEventListener("input", (event) => {
    state.query = event.target.value;
    renderRecordRows();
  });
  $("filterBar").addEventListener("click", (event) => {
    const button = event.target.closest(".filter-button");
    if (!button) return;
    state.filter = button.dataset.filter;
    $("filterBar").querySelectorAll(".filter-button").forEach((item) => {
      const active = item === button;
      item.classList.toggle("is-active", active);
      item.setAttribute("aria-pressed", String(active));
    });
    renderRecordRows();
  });
  $("retryButton").addEventListener("click", retrySelected);
  $("closeDetailButton").addEventListener("click", () => {
    state.selectedId = null;
    state.selectedDetail = null;
    $("detailPanel").classList.add("hidden");
    renderRecordRows();
    $("refreshButton").focus();
  });
  $("reviewForm").addEventListener("submit", submitReview);
}

function initialize() {
  updateClock();
  window.setInterval(updateClock, 30000);
  bindUploadControls();
  bindWorkspaceControls();
  bindApiSettingsControls();
  renderPreflight();
  loadCapabilities();
  loadModelSettings();
  loadBatchHistory({ restoreLatest: true });
}

initialize();
