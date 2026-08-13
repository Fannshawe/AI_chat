const $ = (selector) => document.querySelector(selector);
let contactId = null;
let categories = [];
let aiCategories = [];
let levels = [];

const dimensionLabels = {
  trust: "Trust",
  familiarity: "Familiarity",
  intimacy: "Intimacy",
  attraction: "Attraction",
  attachment: "Attachment",
  comfort: "Comfort",
};

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try {
      detail = (await response.json()).detail || detail;
    } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

function toast(message) {
  const element = $("#toast");
  element.textContent = message;
  element.classList.add("show");
  clearTimeout(toast.timer);
  toast.timer = setTimeout(() => element.classList.remove("show"), 3200);
}

async function logout() {
  await fetch("/gateway/logout", { method: "POST" });
  window.location.href = "/";
}

function escapeText(value) {
  const span = document.createElement("span");
  span.textContent = value ?? "";
  return span.innerHTML;
}

function buildPersonaProfileForm() {
  const form = $("#personaProfileForm");
  form.innerHTML =
    '<div class="field full-row"><textarea id="personaYamlEditor" rows="30" style="font-family:monospace;font-size:12px;line-height:1.55" placeholder="# persona.yaml 内容"></textarea></div>' +
    '<div class="form-actions full-row"><button class="primary">保存 Persona YAML</button></div>';
}

async function loadPersonaProfile() {
  const data = await api("/api/persona-yaml");
  const editor = $("#personaYamlEditor");
  if (editor) editor.value = data.text || "";
}

async function savePersonaProfile(event) {
  event.preventDefault();
  const editor = $("#personaYamlEditor");
  await api("/api/persona-yaml", {
    method: "PUT",
    body: JSON.stringify({ text: editor ? editor.value : "" }),
  });
  toast("Persona YAML 已保存");
}

function renderProposals(items) {
  const list = $("#proposalList");
  list.innerHTML = "";
  items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "list-item proposal-item";
    const body = document.createElement("div");
    const statusText = item.status === "approved" ? "已确认" : item.status === "rejected" ? "已忽略" : "待处理";
    body.innerHTML = `<h3>${escapeText(item.field_key)} <span class="badge-status">${statusText}</span></h3><p>${escapeText(item.reason || "无说明")}</p><div class="code-note">建议值：${escapeText(item.proposed_value || "空")}</div>`;
    row.appendChild(body);
    if (item.status === "pending") {
      const actions = document.createElement("div");
      actions.className = "list-actions";
      const approve = document.createElement("button");
      approve.className = "primary";
      approve.textContent = "确认参考";
      approve.onclick = async () => {
        await api(`/api/persona-proposals/${item.id}/resolve`, {
          method: "POST",
          body: JSON.stringify({ approved: true }),
        });
        await loadProposals();
        toast("提案已标记为已确认（请手动更新 Persona YAML）");
      };
      const reject = document.createElement("button");
      reject.className = "secondary";
      reject.textContent = "忽略";
      reject.onclick = async () => {
        await api(`/api/persona-proposals/${item.id}/resolve`, {
          method: "POST",
          body: JSON.stringify({ approved: false }),
        });
        await loadProposals();
        toast("提案已忽略");
      };
      actions.append(approve, reject);
      row.appendChild(actions);
    }
    list.appendChild(row);
  });
  if (!items.length) {
    list.innerHTML = '<div class="code-note">暂无 Core 修改提案。</div>';
  }
}

async function loadProposals() {
  const items = await api(`/api/persona-proposals/${contactId}`);
  renderProposals(items);
}

async function loadMemories() {
  const data = await api(`/api/memories/${contactId}`);
  categories = data.contact_categories;
  aiCategories = data.ai_categories;
  $("#memoryCategory").innerHTML = categories
    .map((category) => `<option>${escapeText(category)}</option>`)
    .join("");
  renderAiMemories(data.ai_profile || {});
  const list = $("#memoryList");
  list.innerHTML = "";
  data.contact_items.forEach((item) => {
    const row = document.createElement("div");
    row.className = "list-item";
    const body = document.createElement("div");
    body.innerHTML = `<h3>${escapeText(item.category)} ${item.sensitive ? '<span class="badge-sensitive">敏感</span>' : ""}</h3><p>${escapeText(item.content)}</p><div class="code-note">创建时间：${escapeText(item.created_at || "")}</div><div class="code-note">来源：${escapeText(item.source_label || "contact memory / reflection")}</div>`;
    const actions = document.createElement("div");
    actions.className = "list-actions";
    const edit = document.createElement("button");
    edit.className = "secondary";
    edit.textContent = "编辑";
    edit.onclick = async () => {
      const content = prompt("修改记忆", item.content);
      if (content === null || !content.trim()) return;
      await api(`/api/memories/${item.id}`, {
        method: "PUT",
        body: JSON.stringify({
          contact_id: contactId,
          category: item.category,
          content,
          sensitive: Boolean(item.sensitive),
        }),
      });
      await loadMemories();
    };
    const remove = document.createElement("button");
    remove.className = "danger";
    remove.textContent = "删除";
    remove.onclick = async () => {
      if (confirm("确定删除这条记忆？")) {
        await api(`/api/memories/${item.id}`, { method: "DELETE" });
        await loadMemories();
      }
    };
    actions.append(edit, remove);
    row.append(body, actions);
    list.appendChild(row);
  });
  if (!data.contact_items.length) {
    list.innerHTML = '<div class="code-note">暂无 Contact memory。聊天后系统会自动提取，也可手工添加。</div>';
  }
}

function normalizeAiMemoryItem(item) {
  if (item && typeof item === "object") {
    return {
      content: String(item.content || "").trim(),
      source: String(item.source || "admin").trim() || "admin",
    };
  }
  return {
    content: String(item || "").trim(),
    source: "admin",
  };
}

function renderAiMemories(profile) {
  const list = $("#aiMemoryList");
  list.innerHTML = "";
  let count = 0;
  aiCategories.forEach((category) => {
    const items = Array.isArray(profile[category]) ? profile[category] : [];
    items.forEach((rawItem, index) => {
      const item = normalizeAiMemoryItem(rawItem);
      if (!item.content) return;
      count += 1;
      const row = document.createElement("div");
      row.className = "list-item";
      const body = document.createElement("div");
      body.innerHTML = `<h3>${escapeText(category)}</h3><p>${escapeText(item.content)}</p><div class="code-note">来源：${escapeText(item.source)}</div>`;
      const actions = document.createElement("div");
      actions.className = "list-actions";
      const edit = document.createElement("button");
      edit.className = "secondary";
      edit.textContent = "编辑";
      edit.onclick = async () => {
        const content = prompt("修改 AI memory", item.content);
        if (content === null || !content.trim()) return;
        const source = prompt("来源", item.source) || item.source;
        const latest = await api("/api/ai-memory");
        latest[category][index] = {
          content: content.trim(),
          source: source.trim() || "admin",
        };
        await api("/api/ai-memory", {
          method: "PUT",
          body: JSON.stringify(latest),
        });
        await loadMemories();
      };
      const remove = document.createElement("button");
      remove.className = "danger";
      remove.textContent = "删除";
      remove.onclick = async () => {
        if (!confirm("确定删除这条 AI memory？")) return;
        const latest = await api("/api/ai-memory");
        latest[category] = (Array.isArray(latest[category]) ? latest[category] : []).filter((_, itemIndex) => itemIndex !== index);
        await api("/api/ai-memory", {
          method: "PUT",
          body: JSON.stringify(latest),
        });
        await loadMemories();
      };
      actions.append(edit, remove);
      row.append(body, actions);
      list.appendChild(row);
    });
  });
  if (!count) {
    list.innerHTML = '<div class="code-note">暂无 AI memory。这里只显示人工维护或导入的 AI 事实记忆。</div>';
  }
}

function renderRelationshipDimensions(relationship) {
  const container = $("#relationshipDimensions");
  const items = Object.entries(dimensionLabels).map(([key, label]) => {
    const value = Number(relationship[key] ?? 0);
    return `<div class="metric-card"><div class="metric-head"><span>${label}</span><strong>${value}</strong></div><div class="metric-track"><span style="width:${Math.max(0, Math.min(100, value))}%"></span></div></div>`;
  });
  container.innerHTML = items.join("");
}

function renderRelationshipEvents(events) {
  const list = $("#relationshipEventList");
  list.innerHTML = "";
  events.forEach((item) => {
    const row = document.createElement("div");
    row.className = "list-item";
    const delta = item.delta > 0 ? `+${item.delta}` : `${item.delta}`;
    row.innerHTML = `<div><h3>${escapeText(dimensionLabels[item.dimension] || item.dimension)} · ${delta}</h3><p>${item.previous_value} → ${item.current_value}</p><p>${escapeText(item.reason || "无说明")}</p></div><div class="code-note">${escapeText(item.created_at || "")}</div>`;
    list.appendChild(row);
  });
  if (!events.length) {
    list.innerHTML = '<div class="code-note">暂无关系事件记录。</div>';
  }
}

async function loadState() {
  const [relationship, emotions, events] = await Promise.all([
    api(`/api/relationship/${contactId}`),
    api(`/api/emotions/${contactId}`),
    api(`/api/relationship/${contactId}/events`),
  ]);
  $("#relationshipLevel").innerHTML = levels
    .map((level) => `<option ${level === relationship.current_level ? "selected" : ""}>${level}</option>`)
    .join("");
  $("#relationshipSuggestion").textContent = relationship.reason || relationship.suggested_level || "暂无阶段说明";
  renderRelationshipDimensions(relationship);
  renderRelationshipEvents(events);

  const list = $("#emotionList");
  list.innerHTML = "";
  emotions.forEach((item) => {
    const row = document.createElement("div");
    row.className = "list-item";
    const body = document.createElement("div");
    body.innerHTML = `<h3>${escapeText(item.subject)} · ${escapeText(item.emotion)} ${item.confirmed ? "" : '<span class="badge-warn">待确认</span>'}</h3><p>强度 ${Math.round(item.intensity * 100)}% · 置信度 ${Math.round(item.confidence * 100)}% · ${escapeText(item.reason)}</p>`;
    row.appendChild(body);
    if (!item.confirmed) {
      const button = document.createElement("button");
      button.className = "secondary";
      button.textContent = "确认";
      button.onclick = async () => {
        await api(`/api/emotions/${item.id}/confirm`, { method: "POST" });
        await loadState();
      };
      row.appendChild(button);
    }
    list.appendChild(row);
  });
  if (!emotions.length) {
    list.innerHTML = '<div class="code-note">暂无情绪记录。</div>';
  }
}

async function loadGuidance() {
  const docs = await api("/api/guidance/documents");
  const list = $("#guidanceList");
  list.innerHTML = "";
  docs.forEach((doc) => {
    const row = document.createElement("div");
    row.className = "list-item";
    row.innerHTML = `<div><h3>${escapeText(doc.name)}</h3><p>${doc.size} bytes</p></div>`;
    const actions = document.createElement("div");
    actions.className = "list-actions";
    const edit = document.createElement("button");
    edit.className = "secondary";
    edit.textContent = "打开";
    edit.onclick = async () => {
      const data = await api(`/api/guidance/documents/${encodeURIComponent(doc.name)}`);
      $("#guidanceName").value = data.name;
      $("#guidanceContent").value = data.content;
    };
    const remove = document.createElement("button");
    remove.className = "danger";
    remove.textContent = "删除";
    remove.onclick = async () => {
      if (confirm("确定删除文档？")) {
        await api(`/api/guidance/documents/${encodeURIComponent(doc.name)}`, { method: "DELETE" });
        await loadGuidance();
      }
    };
    actions.append(edit, remove);
    row.appendChild(actions);
    list.appendChild(row);
  });
  if (!docs.length) {
    list.innerHTML = '<div class="code-note">指导文档尚未准备好。可以在右侧直接创建 Markdown 文档。</div>';
  }
}

async function loadModels() {
  const data = await api("/api/models");
  const list = $("#modelList");
  list.innerHTML = "";
  data.providers.forEach((provider) => {
    const row = document.createElement("label");
    row.className = "list-item";
    row.innerHTML = `<div><h3>${escapeText(provider.name)} · ${escapeText(provider.model)}</h3><p>${escapeText(provider.base_url || "默认 OpenAI 地址")} · ${provider.configured ? "密钥已配置" : "密钥未配置"}</p></div><input type="radio" name="provider" value="${escapeText(provider.name)}" ${provider.name === data.active ? "checked" : ""}>`;
    row.querySelector("input").onchange = async () => {
      await api("/api/models", { method: "PUT", body: JSON.stringify({ provider: provider.name }) });
      toast("模型供应商已切换");
    };
    list.appendChild(row);
  });
}

const AGENT_DISPLAY_NAMES = {
  emotion: "Emotion（用户情绪）",
  self_emotion: "Self Emotion（自身情绪）",
  relationship: "Relationship（关系）",
  memory_planner: "Memory Planner（记忆规划）",
  dialogue_composer: "Dialogue Composer（对话规划）",
  prompt_composer: "Prompt Composer（生成回复）",
  rewrite: "Rewrite（重写）",
  critic: "Critic（质检）",
  reflection: "Reflection（反思）",
  proactive: "Proactive（主动消息）",
};

async function loadAgentModels() {
  const data = await api("/api/agent-models");
  const list = $("#agentModelList");
  list.innerHTML = "";
  data.forEach((agent) => {
    const row = document.createElement("div");
    row.className = "list-item";
    row.style.cssText = "display:flex;align-items:center;gap:12px;flex-wrap:wrap";
    const label = document.createElement("div");
    label.style.cssText = "flex:1;min-width:160px";
    label.innerHTML = `<h3 style="margin:0">${escapeText(AGENT_DISPLAY_NAMES[agent.agent] || agent.agent)}</h3><p style="margin:2px 0 0;font-size:12px;color:var(--text-muted,#888)">${agent.configured ? "密钥已配置" : "密钥未配置"}${agent.error ? " · <span style=color:red>" + escapeText(agent.error) + "</span>" : ""}</p>`;
    const modelInput = document.createElement("input");
    modelInput.type = "text";
    modelInput.value = agent.model;
    modelInput.placeholder = "模型名称";
    modelInput.style.cssText = "width:200px;padding:4px 8px;border:1px solid var(--border,#ccc);border-radius:4px;font-size:13px";
    const thinkingLabel = document.createElement("label");
    thinkingLabel.style.cssText = "display:flex;align-items:center;gap:5px;white-space:nowrap;font-size:13px;cursor:pointer";
    const thinkingCheck = document.createElement("input");
    thinkingCheck.type = "checkbox";
    thinkingCheck.checked = agent.enable_thinking;
    thinkingLabel.appendChild(thinkingCheck);
    thinkingLabel.appendChild(document.createTextNode("Thinking"));
    const saveBtn = document.createElement("button");
    saveBtn.className = "primary";
    saveBtn.textContent = "保存";
    saveBtn.style.cssText = "padding:4px 14px;font-size:13px";
    saveBtn.onclick = async () => {
      await api("/api/agent-models", {
        method: "PUT",
        body: JSON.stringify({ agent: agent.agent, model: modelInput.value, enable_thinking: thinkingCheck.checked }),
      });
      toast(`${agent.agent} 已保存`);
    };
    row.appendChild(label);
    row.appendChild(modelInput);
    row.appendChild(thinkingLabel);
    row.appendChild(saveBtn);
    list.appendChild(row);
  });
}

buildPersonaProfileForm();
$("#personaProfileForm").addEventListener("submit", savePersonaProfile);

document.querySelectorAll(".tab-button").forEach((button) => {
  button.onclick = () => {
    document.querySelectorAll(".tab-button,.tab-panel").forEach((element) => element.classList.remove("active"));
    button.classList.add("active");
    $(`#tab-${button.dataset.tab}`).classList.add("active");
  };
});

$("#memoryForm").onsubmit = async (event) => {
  event.preventDefault();
  await api("/api/memories", {
    method: "POST",
    body: JSON.stringify({
      contact_id: contactId,
      category: $("#memoryCategory").value,
      content: $("#memoryContent").value,
      sensitive: $("#memorySensitive").checked,
    }),
  });
  event.target.reset();
  await loadMemories();
  toast("记忆已添加");
};

$("#aiMemoryForm").onsubmit = async (event) => {
  event.preventDefault();
  const latest = await api("/api/ai-memory");
  const category = $("#aiMemoryCategory").value;
  const content = $("#aiMemoryContent").value.trim();
  const source = $("#aiMemorySource").value.trim() || "admin";
  latest[category] = Array.isArray(latest[category]) ? latest[category] : [];
  latest[category].push({ content, source });
  await api("/api/ai-memory", {
    method: "PUT",
    body: JSON.stringify(latest),
  });
  event.target.reset();
  await loadMemories();
  toast("AI memory 已添加");
};

$("#saveRelationship").onclick = async () => {
  await api(`/api/relationship/${contactId}`, {
    method: "PUT",
    body: JSON.stringify({ level: $("#relationshipLevel").value }),
  });
  await loadState();
  toast("关系阶段已更新");
};

$("#guidanceForm").onsubmit = async (event) => {
  event.preventDefault();
  await api("/api/guidance/documents", {
    method: "POST",
    body: JSON.stringify({ name: $("#guidanceName").value, content: $("#guidanceContent").value }),
  });
  await loadGuidance();
  toast("指导文档已保存");
};

$("#importForm").onsubmit = async (event) => {
  event.preventDefault();
  const result = await api("/api/import", {
    method: "POST",
    body: JSON.stringify({ contact_id: contactId, text: $("#importText").value }),
  });
  toast(`已导入 ${result.imported} 条聊天`);
};

(async function init() {
  try {
    const boot = await api("/api/bootstrap");
    contactId = boot.active_contact.id;
    levels = boot.levels;
    await Promise.all([
      loadPersonaProfile(),
      loadProposals(),
      loadMemories(),
      loadState(),
      loadGuidance(),
      loadModels(),
      loadAgentModels(),
    ]);
  } catch (error) {
    toast(error.message);
  }
})();

const logoutButton = $("#logoutButton");
if (logoutButton) {
  logoutButton.onclick = logout;
}