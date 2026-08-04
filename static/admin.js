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

const personaProfileSections = [
  {
    key: "core",
    title: "Core",
    fields: [
      ["identity", "身份定位", "例如：更偏温柔、克制、会留白的伴侣表达"],
      ["values", "核心价值", "例如：真诚、尊重边界、稳定回应"],
      ["relationship_goal", "关系目标", "例如：建立安全感，而不是追求高强度黏着"],
      ["hard_limits", "硬边界", "每行一条，禁止自动突破"],
    ],
  },
  {
    key: "personality",
    title: "Personality",
    fields: [
      ["tone", "整体语气", "例如：自然、轻松、不过度解释"],
      ["pace", "节奏偏好", "例如：先回应对方，再轻推话题"],
      ["humor", "幽默方式", "例如：轻微调侃，不做夸张人设"],
      ["taboos", "禁忌风格", "每行一条，例如：不要鸡汤式安慰"],
    ],
  },
  {
    key: "habits",
    title: "Habits",
    fields: [
      ["openings", "开场习惯", "每行一条，例如：先接情绪，再回事实"],
      ["callbacks", "回扣习惯", "每行一条，例如：会偶尔提及之前的小细节"],
      ["emoji", "Emoji 使用", "每行一条，例如：偶尔用 :) 而不是堆表情"],
      ["topics", "常用话题偏好", "每行一条，例如：生活细节、共同计划"],
    ],
  },
  {
    key: "expression",
    title: "Expression",
    fields: [
      ["dos", "推荐表达", "每行一条，系统更倾向模仿"],
      ["donts", "避免表达", "每行一条，系统应尽量回避"],
      ["examples", "示例回复", "每行一条，供风格参考"],
      ["notes", "补充说明", "可写自由文本"],
    ],
  },
];

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

function escapeText(value) {
  const span = document.createElement("span");
  span.textContent = value ?? "";
  return span.innerHTML;
}

function toLines(value) {
  if (Array.isArray(value)) return value.join("\n");
  if (value && typeof value === "object") {
    return Object.entries(value)
      .map(([key, item]) => `${key}: ${item}`)
      .join("\n");
  }
  return value || "";
}

function fromLines(value) {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function buildPersonaProfileForm() {
  const form = $("#personaProfileForm");
  form.innerHTML =
    personaProfileSections
      .map(
        (section) =>
          `<section class="persona-section full-row"><div class="section-heading"><h3>${section.title}</h3><p>直接对应 persona.yaml 的 ${section.key} 段。</p></div><div class="form-grid">${section.fields
            .map(
              ([field, label, placeholder]) =>
                `<div class="field ${field === "notes" ? "full-row" : ""}"><label>${label}</label><textarea data-section="${section.key}" data-field="${field}" placeholder="${placeholder}"></textarea></div>`,
            )
            .join("")}</div></section>`,
      )
      .join("") +
    '<div class="form-actions full-row"><button class="primary">保存 Persona YAML</button></div>';
}

function fillPersonaProfile(data) {
  personaProfileSections.forEach((section) => {
    const values = data[section.key] || {};
    section.fields.forEach(([field]) => {
      const input = document.querySelector(`[data-section="${section.key}"][data-field="${field}"]`);
      if (!input) return;
      input.value = toLines(values[field]);
    });
  });
}

function collectPersonaProfile() {
  const payload = {};
  personaProfileSections.forEach((section) => {
    payload[section.key] = {};
    section.fields.forEach(([field]) => {
      const input = document.querySelector(`[data-section="${section.key}"][data-field="${field}"]`);
      const raw = input ? input.value.trim() : "";
      if (["hard_limits", "taboos", "openings", "callbacks", "emoji", "topics", "dos", "donts", "examples"].includes(field)) {
        payload[section.key][field] = fromLines(raw);
      } else {
        payload[section.key][field] = raw;
      }
    });
  });
  return payload;
}

async function loadPersonaProfile() {
  const data = await api("/api/persona-profile");
  fillPersonaProfile(data);
}

async function savePersonaProfile(event) {
  event.preventDefault();
  await api("/api/persona-profile", {
    method: "PUT",
    body: JSON.stringify(collectPersonaProfile()),
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
    const status = item.resolved_at ? (item.approved ? "已批准" : "已拒绝") : "待处理";
    body.innerHTML = `<h3>${escapeText(item.field)} <span class="badge-status">${status}</span></h3><p>${escapeText(item.reason || "无说明")}</p><div class="code-note">当前值：${escapeText(item.old_value || "空")}</div><div class="code-note">建议值：${escapeText(item.new_value || "空")}</div>`;
    row.appendChild(body);
    if (!item.resolved_at) {
      const actions = document.createElement("div");
      actions.className = "list-actions";
      const approve = document.createElement("button");
      approve.className = "primary";
      approve.textContent = "批准";
      approve.onclick = async () => {
        await api(`/api/persona-proposals/${item.id}/resolve`, {
          method: "POST",
          body: JSON.stringify({ approved: true }),
        });
        await Promise.all([loadProposals(), loadPersonaProfile()]);
        toast("提案已批准并写回 Persona YAML");
      };
      const reject = document.createElement("button");
      reject.className = "secondary";
      reject.textContent = "拒绝";
      reject.onclick = async () => {
        await api(`/api/persona-proposals/${item.id}/resolve`, {
          method: "POST",
          body: JSON.stringify({ approved: false }),
        });
        await loadProposals();
        toast("提案已拒绝");
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
    ]);
  } catch (error) {
    toast(error.message);
  }
})();