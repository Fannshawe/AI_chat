const $ = (selector) => document.querySelector(selector);
let contactId = null;
let categories = [];
let levels = [];

async function api(url, options = {}) {
  const response = await fetch(url, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) {
    let detail = `请求失败 (${response.status})`;
    try { detail = (await response.json()).detail || detail; } catch (_) {}
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}
function toast(message) { const e = $("#toast"); e.textContent = message; e.classList.add("show"); clearTimeout(toast.timer); toast.timer = setTimeout(() => e.classList.remove("show"), 3200); }
function escapeText(value) { const span = document.createElement("span"); span.textContent = value ?? ""; return span.innerHTML; }

const personaFields = [
  ["mbti", "MBTI", "例如 INFP"], ["personality", "性格", "稳定性格特征"],
  ["values", "价值观", "重要原则和价值排序"], ["love_view", "爱情观", "如何看待亲密关系"],
  ["style", "措辞与语气", "回复长度、语气、Emoji习惯"], ["catchphrases", "口头禅", "常用词和习惯表达"],
  ["boundaries", "禁区与边界", "不能模仿或不能答应的内容"],
];
function buildPersonaForm(form) {
  form.innerHTML = personaFields.map(([key, label, placeholder], index) => `<div class="field ${index >= 4 ? "full-row" : ""}"><label>${label}</label><textarea name="${key}" placeholder="${placeholder}"></textarea></div>`).join("") + '<div class="field full-row"><label>示例回复（每行一条）</label><textarea name="examples" placeholder="哈哈哈你怎么这样\n那你早点休息"></textarea></div><div class="form-actions full-row"><button class="primary">保存人格档案</button></div>';
}
async function loadPersona(scope) {
  const data = await api(`/api/personas/${scope}`);
  const form = $(`form[data-scope="${scope}"]`);
  personaFields.forEach(([key]) => { form.elements[key].value = data.content[key] || ""; });
  form.elements.examples.value = (data.examples || []).join("\n");
}
async function savePersona(event) {
  event.preventDefault(); const form = event.currentTarget; const content = {};
  personaFields.forEach(([key]) => { content[key] = form.elements[key].value.trim(); });
  const examples = form.elements.examples.value.split("\n").map(v => v.trim()).filter(Boolean);
  await api(`/api/personas/${form.dataset.scope}`, { method: "PUT", body: JSON.stringify({ content, examples }) }); toast("人格档案已保存");
}

async function loadMemories() {
  const data = await api(`/api/memories/${contactId}`); categories = data.categories;
  $("#memoryCategory").innerHTML = categories.map(c => `<option>${escapeText(c)}</option>`).join("");
  const list = $("#memoryList"); list.innerHTML = "";
  data.items.forEach(item => {
    const row = document.createElement("div"); row.className = "list-item";
    const body = document.createElement("div"); body.innerHTML = `<h3>${escapeText(item.category)} ${item.sensitive ? '<span class="badge-sensitive">敏感</span>' : ""}</h3><p>${escapeText(item.content)}</p>`;
    const actions = document.createElement("div"); actions.className = "list-actions";
    const edit = document.createElement("button"); edit.className = "secondary"; edit.textContent = "编辑"; edit.onclick = async () => {
      const content = prompt("修改记忆", item.content); if (content === null || !content.trim()) return;
      await api(`/api/memories/${item.id}`, { method: "PUT", body: JSON.stringify({ contact_id: contactId, category: item.category, content, sensitive: Boolean(item.sensitive) }) }); loadMemories();
    };
    const remove = document.createElement("button"); remove.className = "danger"; remove.textContent = "删除"; remove.onclick = async () => { if (confirm("确定删除这条记忆？")) { await api(`/api/memories/${item.id}`, { method: "DELETE" }); loadMemories(); } };
    actions.append(edit, remove); row.append(body, actions); list.appendChild(row);
  });
  if (!data.items.length) list.innerHTML = '<div class="code-note">暂无长期记忆。聊天后系统会自动提取，也可手工添加。</div>';
}

async function loadState() {
  const [relationship, emotions] = await Promise.all([api(`/api/relationship/${contactId}`), api(`/api/emotions/${contactId}`)]);
  $("#relationshipLevel").innerHTML = levels.map(level => `<option ${level === relationship.current_level ? "selected" : ""}>${level}</option>`).join("");
  $("#relationshipSuggestion").textContent = relationship.suggested_level ? `建议调整为「${relationship.suggested_level}」\n${relationship.reason}` : "暂无待确认建议";
  const list = $("#emotionList"); list.innerHTML = "";
  emotions.forEach(item => {
    const row = document.createElement("div"); row.className = "list-item";
    const body = document.createElement("div"); body.innerHTML = `<h3>${escapeText(item.subject)} · ${escapeText(item.emotion)} ${item.confirmed ? "" : '<span class="badge-warn">待确认</span>'}</h3><p>强度 ${Math.round(item.intensity * 100)}% · 置信度 ${Math.round(item.confidence * 100)}% · ${escapeText(item.reason)}</p>`;
    row.appendChild(body);
    if (!item.confirmed) { const button = document.createElement("button"); button.className = "secondary"; button.textContent = "确认"; button.onclick = async () => { await api(`/api/emotions/${item.id}/confirm`, { method: "POST" }); loadState(); }; row.appendChild(button); }
    list.appendChild(row);
  });
  if (!emotions.length) list.innerHTML = '<div class="code-note">暂无情绪记录。</div>';
}

async function loadGuidance() {
  const docs = await api("/api/guidance/documents"); const list = $("#guidanceList"); list.innerHTML = "";
  docs.forEach(doc => {
    const row = document.createElement("div"); row.className = "list-item";
    row.innerHTML = `<div><h3>${escapeText(doc.name)}</h3><p>${doc.size} bytes</p></div>`;
    const actions = document.createElement("div"); actions.className = "list-actions";
    const edit = document.createElement("button"); edit.className = "secondary"; edit.textContent = "打开"; edit.onclick = async () => { const data = await api(`/api/guidance/documents/${encodeURIComponent(doc.name)}`); $("#guidanceName").value = data.name; $("#guidanceContent").value = data.content; };
    const remove = document.createElement("button"); remove.className = "danger"; remove.textContent = "删除"; remove.onclick = async () => { if (confirm("确定删除文档？")) { await api(`/api/guidance/documents/${encodeURIComponent(doc.name)}`, { method: "DELETE" }); loadGuidance(); } };
    actions.append(edit, remove); row.appendChild(actions); list.appendChild(row);
  });
  if (!docs.length) list.innerHTML = '<div class="code-note">指导文档尚未准备好。可以在右侧直接创建 Markdown 文档。</div>';
}

async function loadModels() {
  const data = await api("/api/models"); const list = $("#modelList"); list.innerHTML = "";
  data.providers.forEach(provider => {
    const row = document.createElement("label"); row.className = "list-item";
    row.innerHTML = `<div><h3>${escapeText(provider.name)} · ${escapeText(provider.model)}</h3><p>${escapeText(provider.base_url || "默认 OpenAI 地址")} · ${provider.configured ? "密钥已配置" : "密钥未配置"}</p></div><input type="radio" name="provider" value="${escapeText(provider.name)}" ${provider.name === data.active ? "checked" : ""}>`;
    row.querySelector("input").onchange = async () => { await api("/api/models", { method: "PUT", body: JSON.stringify({ provider: provider.name }) }); toast("模型供应商已切换"); };
    list.appendChild(row);
  });
}

document.querySelectorAll(".persona-form").forEach(form => { buildPersonaForm(form); form.addEventListener("submit", savePersona); });
document.querySelectorAll(".tab-button").forEach(button => button.onclick = () => {
  document.querySelectorAll(".tab-button,.tab-panel").forEach(e => e.classList.remove("active")); button.classList.add("active"); $(`#tab-${button.dataset.tab}`).classList.add("active");
});
$("#memoryForm").onsubmit = async event => { event.preventDefault(); await api("/api/memories", { method: "POST", body: JSON.stringify({ contact_id: contactId, category: $("#memoryCategory").value, content: $("#memoryContent").value, sensitive: $("#memorySensitive").checked }) }); event.target.reset(); await loadMemories(); toast("记忆已添加"); };
$("#saveRelationship").onclick = async () => { await api(`/api/relationship/${contactId}`, { method: "PUT", body: JSON.stringify({ level: $("#relationshipLevel").value }) }); await loadState(); toast("关系阶段已确认"); };
$("#guidanceForm").onsubmit = async event => { event.preventDefault(); await api("/api/guidance/documents", { method: "POST", body: JSON.stringify({ name: $("#guidanceName").value, content: $("#guidanceContent").value }) }); await loadGuidance(); toast("指导文档已保存"); };
$("#importForm").onsubmit = async event => { event.preventDefault(); const result = await api("/api/import", { method: "POST", body: JSON.stringify({ contact_id: contactId, text: $("#importText").value }) }); toast(`已导入 ${result.imported} 条聊天`); };

(async function init() {
  try {
    const boot = await api("/api/bootstrap"); contactId = boot.active_contact.id; levels = boot.levels;
    await Promise.all([loadPersona("self"), loadPersona("other"), loadMemories(), loadState(), loadGuidance(), loadModels()]);
  } catch (error) { toast(error.message); }
})();
