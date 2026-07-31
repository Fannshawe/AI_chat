const $ = (selector) => document.querySelector(selector);
const state = { contactId: null, images: [], generationId: null, debugEnabled: true };

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

function updateLatestDebugLink() {
  const link = $("#latestDebugLink");
  if (!state.generationId) {
    link.classList.add("hidden");
    link.href = "/debug";
    return;
  }
  link.href = `/debug?generation_id=${state.generationId}`;
  link.classList.remove("hidden");
}

async function loadDebugSettings() {
  const settings = await api("/api/debug/settings");
  state.debugEnabled = Boolean(settings.enabled);
  $("#debugEnabledToggle").checked = state.debugEnabled;
}

async function updateDebugSettings(enabled) {
  const settings = await api("/api/debug/settings", {
    method: "PUT",
    body: JSON.stringify({ enabled }),
  });
  state.debugEnabled = Boolean(settings.enabled);
  $("#debugEnabledToggle").checked = state.debugEnabled;
}

function formatTime(value) {
  if (!value) return "";
  return new Date(value).toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}

function messageElement(message) {
  const item = document.createElement("article");
  item.className = `message ${message.role}`;
  if (message.image_paths?.length) {
    const images = document.createElement("div");
    images.className = "message-images";
    message.image_paths.forEach((path) => {
      const image = document.createElement("img");
      image.src = path;
      image.alt = "聊天图片";
      images.appendChild(image);
    });
    item.appendChild(images);
  }
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  bubble.textContent = message.content;
  item.appendChild(bubble);
  const time = document.createElement("time");
  time.textContent = formatTime(message.created_at);
  item.appendChild(time);
  return item;
}

function renderMessages(messages) {
  const container = $("#messages");
  container.innerHTML = "";
  if (!messages.length) {
    container.innerHTML = '<div class="empty-state"><strong>从真实对话开始</strong><span>粘贴对方发来的消息。系统会结合人格、关系、情绪、记忆和指导文档生成三个候选。</span></div>';
  } else {
    messages.forEach((message) => container.appendChild(messageElement(message)));
  }
  container.scrollTop = container.scrollHeight;
}

function finalCritiques(critic) {
  const reviews = critic?.reviews || [];
  const review = reviews[reviews.length - 1];
  return review?.candidates || [];
}

function renderCandidates(candidates, strategy, modes = [], critic = {}) {
  $("#strategyText").textContent = strategy;
  const grid = $("#candidateGrid");
  grid.innerHTML = "";
  candidates.forEach((candidate, index) => {
    const card = document.createElement("article");
    card.className = "candidate-card";
    const mode = document.createElement("span");
    mode.className = "candidate-mode";
    mode.textContent = modes[index] || "候选回复";
    const textarea = document.createElement("textarea");
    textarea.value = candidate;
    textarea.setAttribute("aria-label", `候选回复 ${index + 1}`);
    const isSilence = candidate === "（暂不回复）" || modes[index] === "暂不回复";
    if (isSilence) textarea.readOnly = true;
    card.append(mode, textarea);
    const critique = finalCritiques(critic).find(item => item.candidate_index === index);
    if (critique && !critique.passed) {
      const warning = document.createElement("p");
      warning.className = "candidate-warning";
      warning.textContent = critique.issues?.join("；") || "该候选仍有真人感质量预警";
      card.appendChild(warning);
    }
    const actions = document.createElement("div");
    actions.className = "candidate-actions";
    const copy = document.createElement("button");
    copy.className = "secondary";
    copy.textContent = isSilence ? "采纳不回复" : "复制";
    copy.onclick = async () => {
      if (isSilence) { $("#draftZone").classList.add("hidden"); return toast("已采纳暂不回复建议"); }
      await navigator.clipboard.writeText(textarea.value); toast("已复制候选回复");
    };
    const send = document.createElement("button");
    send.className = "small-primary";
    send.textContent = "确认已发送";
    send.onclick = () => confirmCandidate(textarea.value, index, send);
    actions.append(copy);
    if (!isSilence) actions.append(send);
    card.append(actions);
    grid.appendChild(card);
  });
  $("#draftZone").classList.remove("hidden");
}

async function confirmCandidate(content, candidateIndex, button) {
  if (!content.trim()) return toast("回复不能为空");
  button.disabled = true;
  try {
    const saved = await api("/api/chat/confirm", { method: "POST", body: JSON.stringify({ contact_id: state.contactId, content, generation_id: state.generationId, candidate_index: candidateIndex }) });
    $("#messages").appendChild(messageElement(saved));
    $("#messages").scrollTop = $("#messages").scrollHeight;
    $("#draftZone").classList.add("hidden");
    toast("已记录为真实发送内容");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}

function renderImagePreview() {
  const container = $("#imagePreview");
  container.innerHTML = "";
  state.images.forEach((image, index) => {
    const wrapper = document.createElement("div");
    wrapper.className = "preview-item";
    const preview = document.createElement("img");
    preview.src = image.data;
    const remove = document.createElement("button");
    remove.textContent = "×";
    remove.onclick = () => { state.images.splice(index, 1); renderImagePreview(); };
    wrapper.append(preview, remove);
    container.appendChild(wrapper);
  });
}

function readFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve({ name: file.name, mime: file.type, data: reader.result });
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

async function generate() {
  const input = $("#messageInput");
  const content = input.value.trim();
  if (!content && !state.images.length) return toast("请输入消息或选择图片");
  const button = $("#generateButton");
  button.disabled = true;
  button.classList.add("loading");
  button.textContent = "分析中";
  try {
    const result = await api("/api/chat/generate", { method: "POST", body: JSON.stringify({ contact_id: state.contactId, content, images: state.images }) });
    const empty = $("#messages .empty-state");
    if (empty) empty.remove();
    $("#messages").appendChild(messageElement(result.message));
    state.generationId = result.generation_id;
    updateLatestDebugLink();
    renderCandidates(result.candidates, result.strategy, result.dialogue_plan?.strategy || [], result.critic);
    $("#relationshipBadge").textContent = `关系：${result.relationship.current_level}`;
    input.value = "";
    input.style.height = "auto";
    state.images = [];
    renderImagePreview();
    if (result.warnings?.length) toast(result.warnings.join("；"));
  } catch (error) { toast(error.message); }
  finally {
    button.disabled = false;
    button.classList.remove("loading");
    button.textContent = "生成 3 个回复";
  }
}

async function load() {
  try {
    const data = await api("/api/bootstrap");
    await loadDebugSettings();
    state.contactId = data.active_contact.id;
    $("#contactName").textContent = data.active_contact.name;
    $("#chatTitle").textContent = data.active_contact.name;
    $("#relationshipBadge").textContent = `关系：${data.relationship.current_level}`;
    renderMessages(data.messages);
  } catch (error) { toast(error.message); }
}

$("#debugEnabledToggle").addEventListener("change", async (event) => {
  event.currentTarget.disabled = true;
  try {
    await updateDebugSettings(event.currentTarget.checked);
    toast(state.debugEnabled ? "已开启详细 Debug 记录" : "已关闭详细 Debug 记录");
  } catch (error) {
    event.currentTarget.checked = state.debugEnabled;
    toast(error.message);
  } finally {
    event.currentTarget.disabled = false;
  }
});

$("#generateButton").onclick = generate;
$("#messageInput").addEventListener("keydown", (event) => {
  if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); generate(); }
});
$("#messageInput").addEventListener("input", (event) => {
  event.target.style.height = "auto";
  event.target.style.height = `${Math.min(event.target.scrollHeight, 130)}px`;
});
$("#imageInput").addEventListener("change", async (event) => {
  const remaining = 4 - state.images.length;
  const files = [...event.target.files].slice(0, remaining);
  state.images.push(...await Promise.all(files.map(readFile)));
  renderImagePreview();
  event.target.value = "";
});
$("#proactiveButton").onclick = async (event) => {
  event.currentTarget.disabled = true;
  try {
    const result = await api(`/api/proactive/${state.contactId}`, { method: "POST" });
    $("#messageInput").value = result.draft;
    toast(`草稿原因：${result.reason}`);
  } catch (error) { toast(error.message); }
  finally { event.currentTarget.disabled = false; }
};
load();
