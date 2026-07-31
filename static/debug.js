const $ = (selector) => document.querySelector(selector);

const state = { contactId: null, generations: [], selectedGenerationId: null, selectedDetail: null, nodeFilter: "", targetGenerationId: null };

async function api(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 3600);
}

async function copyText(text, message = "已复制") {
  await navigator.clipboard.writeText(text);
  toast(message);
}

function tryParseJsonText(value) {
  if (typeof value !== "string") return value;
  const text = value.trim();
  if (!text || (!["{", "["].includes(text[0]))) return value;
  try {
    return JSON.parse(text);
  } catch (_) {
    return value;
  }
}

function formatDebugValue(value) {
  const parsed = tryParseJsonText(value);
  if (Array.isArray(parsed)) {
    return parsed.map((item) => formatDebugValue(item));
  }
  if (parsed && typeof parsed === "object") {
    return Object.fromEntries(Object.entries(parsed).map(([key, item]) => [key, formatDebugValue(item)]));
  }
  return parsed;
}

function stringifyDebugValue(value) {
  return JSON.stringify(formatDebugValue(value), null, 2);
}

function formatTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function renderGenerationList() {
  const container = $("#generationList");
  container.innerHTML = "";
  if (!state.generations.length) {
    container.innerHTML = '<div class="empty-state"><strong>暂无调试数据</strong><span>先去聊天页生成一次回复，这里才会出现 node trace。</span></div>';
    return;
  }
  state.generations.forEach((item) => {
    const row = document.createElement("button");
    row.className = "list-item debug-generation-item";
    if (item.id === state.selectedGenerationId) row.classList.add("active");
    row.type = "button";
    const legacy = item.trace_count === 0 ? '<p class="debug-legacy">旧记录或未开启详细 trace</p>' : "";
    row.innerHTML = `<div><h3>#${item.id} · ${formatTime(item.created_at)}</h3><p>策略：${item.strategy || "无"}</p><p>trace ${item.trace_count} 条 · warning ${item.warning_count} 条 · 候选 ${item.candidate_count} 个</p>${legacy}</div><span class="badge-status">查看详情</span>`;
    row.onclick = () => loadGenerationDetail(item.id);
    container.appendChild(row);
  });
}

function renderNodeFilter() {
  const select = $("#nodeFilter");
  const current = state.nodeFilter;
  const names = [...new Set((state.selectedDetail?.debug_trace || []).map((item) => item.node))];
  select.innerHTML = '<option value="">全部节点</option>';
  names.forEach((name) => {
    const option = document.createElement("option");
    option.value = name;
    option.textContent = name;
    if (name === current) option.selected = true;
    select.appendChild(option);
  });
}

function renderTraceItem(trace) {
  const item = document.createElement("details");
  item.className = "debug-trace-item";
  if (trace.warnings?.length) item.classList.add("has-warning");
  item.open = trace.status === "error";
  const summary = document.createElement("summary");
  const statusClass = trace.status === "error" ? "error" : (trace.warnings?.length ? "warn" : "");
  const warningPill = trace.warnings?.length ? `<span class="debug-pill warn">warning ${trace.warnings.length}</span>` : "";
  summary.innerHTML = `<strong>${trace.node}</strong><span class="debug-pill ${statusClass}">${trace.status}</span><span>${trace.duration_ms} ms</span>${warningPill}`;
  const content = document.createElement("div");
  content.className = "debug-trace-content";
  const copyButton = document.createElement("button");
  copyButton.className = "debug-copy";
  copyButton.type = "button";
  copyButton.textContent = "复制本节点";
  const traceForCopy = { ...trace };
  delete traceForCopy.state_changes;
  copyButton.onclick = async () => copyText(stringifyDebugValue(traceForCopy), `已复制节点 ${trace.node}`);
  content.appendChild(copyButton);
  const sections = [
    ["节点输入", trace.input],
    ["节点输出", trace.output],
    ["LLM 输入", trace.llm?.input],
    ["LLM 输出", trace.llm?.output],
  ];
  if (trace.llm?.error) sections.push(["LLM 异常", trace.llm.error]);
  if (trace.error) sections.push(["异常", trace.error]);
  if (trace.warnings?.length) sections.push(["警告", trace.warnings]);
  sections.forEach(([label, value]) => {
    if (value === undefined || value === null) return;
    const block = document.createElement("section");
    block.className = "debug-block";
    block.innerHTML = `<h3>${label}</h3><pre>${stringifyDebugValue(value)}</pre>`;
    content.appendChild(block);
  });
  item.append(summary, content);
  return item;
}

function renderGenerationDetail(detail) {
  state.selectedDetail = detail;
  renderNodeFilter();
  const container = $("#debugDetail");
  container.innerHTML = "";
  const header = document.createElement("div");
  header.className = "panel-card";
  header.innerHTML = `<div class="debug-section-head"><div><h2>Generation #${detail.generation.id}</h2><p>创建时间：${formatTime(detail.generation.created_at)} · 候选 ${detail.generation.candidates?.length || 0} 个</p></div><button class="debug-copy" id="copyGenerationButton" type="button">复制这次调试</button></div><div class="debug-meta"><div class="code-note">strategy\n${detail.generation.strategy || ""}</div><div class="code-note">warnings\n${JSON.stringify(detail.warnings || [], null, 2)}</div></div>`;
  const banner = document.createElement("div");
  banner.className = `debug-banner ${detail.debug?.detail_enabled ? "" : "warn"}`.trim();
  banner.textContent = detail.debug?.detail_enabled
    ? "当前 DEBUG=true，已记录节点输入输出，以及 LLM 输入输出。"
    : "当前 DEBUG=false，只保留节点元信息、耗时、异常和 warning；详细输入输出未落库。";
  if (!detail.debug_trace?.length) {
    banner.classList.add("warn");
    banner.textContent += " 这条 generation 没有 trace，通常是旧记录。";
  }
  header.appendChild(banner);
  const traceCard = document.createElement("div");
  traceCard.className = "panel-card";
  traceCard.innerHTML = "<h2>Node Trace</h2>";
  const traces = (detail.debug_trace || []).filter((trace) => !state.nodeFilter || trace.node === state.nodeFilter);
  if (!traces.length) {
    traceCard.innerHTML += '<div class="empty-state"><strong>没有 trace</strong><span>这条 generation 尚未保存节点调试信息。</span></div>';
  } else {
    traces.forEach((trace) => traceCard.appendChild(renderTraceItem(trace)));
  }
  container.append(header, traceCard);
  $("#copyGenerationButton").onclick = async () => {
    const detailForCopy = {
      ...detail,
      debug_trace: (detail.debug_trace || []).map((trace) => {
        const traceItem = { ...trace };
        delete traceItem.state_changes;
        return traceItem;
      }),
    };
    await copyText(stringifyDebugValue(detailForCopy), `已复制 Generation #${detail.generation.id}`);
  };
}

async function loadGenerationDetail(generationId) {
  try {
    state.selectedGenerationId = generationId;
    renderGenerationList();
    renderGenerationDetail(await api(`/api/debug/generations/${generationId}`));
  } catch (error) {
    toast(error.message);
  }
}

async function loadGenerations() {
  try {
    const limit = Math.min(Math.max(Number($("#limitInput").value) || 20, 1), 100);
    state.generations = await api(`/api/debug/generations?contact_id=${state.contactId}&limit=${limit}`);
    renderGenerationList();
    const preferred = state.targetGenerationId || state.selectedGenerationId || state.generations[0]?.id;
    if (preferred && state.generations.some((item) => item.id === preferred)) {
      await loadGenerationDetail(preferred);
      state.targetGenerationId = null;
    }
  } catch (error) {
    toast(error.message);
  }
}

async function loadBootstrap() {
  try {
    const params = new URLSearchParams(window.location.search);
    const generationId = Number(params.get("generation_id") || 0);
    state.targetGenerationId = generationId > 0 ? generationId : null;
    const data = await api("/api/bootstrap");
    const select = $("#contactSelect");
    select.innerHTML = "";
    data.contacts.forEach((contact) => {
      const option = document.createElement("option");
      option.value = contact.id;
      option.textContent = contact.name;
      if (contact.id === data.active_contact.id) option.selected = true;
      select.appendChild(option);
    });
    state.contactId = data.active_contact.id;
    await loadGenerations();
  } catch (error) {
    toast(error.message);
  }
}

$("#contactSelect").addEventListener("change", (event) => {
  state.contactId = Number(event.target.value);
  loadGenerations();
});

$("#nodeFilter").addEventListener("change", (event) => {
  state.nodeFilter = event.target.value;
  if (state.selectedDetail) renderGenerationDetail(state.selectedDetail);
});

$("#refreshDebugButton").onclick = loadGenerations;

loadBootstrap();