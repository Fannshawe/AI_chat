const $ = (selector) => document.querySelector(selector);
const state = { contactId: null, generationId: null, debugEnabled: true, pendingPreviewMessage: null };
const DRAFT_CACHE_KEY = "persona-chat:draft:v1";
const JOB_CACHE_KEY = "persona-chat:active-job:v1";
const INPUT_CACHE_KEY = "persona-chat:input:v1";
const JOB_POLL_INTERVAL_MS = 1000;
const STAGE_STEPS = [
  { name: "读取上下文", target: 14 },
  { name: "识别情绪", target: 30 },
  { name: "关系评估", target: 46 },
  { name: "记忆规划", target: 62 },
  { name: "整合策略", target: 76 },
  { name: "生成候选", target: 88 },
  { name: "质检与收尾", target: 95 },
  { name: "完成", target: 100 },
];
let progressTimer = null;
let progressValue = 0;
let progressHasServerSignal = false;

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

async function logout() {
  try {
    await fetch("/gateway/logout", { method: "POST" });
  } finally {
    window.location.href = "/";
  }
}

async function copyText(value) {
  const text = String(value ?? "");
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(text);
    return;
  }
  const helper = document.createElement("textarea");
  helper.value = text;
  helper.setAttribute("readonly", "readonly");
  helper.style.position = "fixed";
  helper.style.opacity = "0";
  document.body.appendChild(helper);
  helper.select();
  try {
    document.execCommand("copy");
  } finally {
    helper.remove();
  }
}

function readCachedDraft() {
  try {
    const raw = localStorage.getItem(DRAFT_CACHE_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (_) {
    return null;
  }
}

function saveDraftCache(payload) {
  try {
    localStorage.setItem(DRAFT_CACHE_KEY, JSON.stringify(payload));
  } catch (_) {}
}

function clearDraftCache() {
  try {
    localStorage.removeItem(DRAFT_CACHE_KEY);
  } catch (_) {}
}

function readCachedInput() {
  try {
    const raw = localStorage.getItem(INPUT_CACHE_KEY);
    if (!raw) return null;
    return JSON.parse(raw);
  } catch (_) {
    return null;
  }
}

function saveInputCache(value) {
  try {
    localStorage.setItem(INPUT_CACHE_KEY, JSON.stringify({
      contactId: state.contactId,
      content: String(value ?? ""),
      savedAt: Date.now(),
    }));
  } catch (_) {}
}

function clearInputCache() {
  try {
    localStorage.removeItem(INPUT_CACHE_KEY);
  } catch (_) {}
}

function restoreInputCache() {
  const cached = readCachedInput();
  if (!cached || cached.contactId !== state.contactId) return;
  if (Date.now() - Number(cached.savedAt || 0) > 1000 * 60 * 60 * 12) {
    clearInputCache();
    return;
  }
  const text = String(cached.content || "");
  if (!text.trim()) return;
  const input = $("#messageInput");
  input.value = text;
  input.dispatchEvent(new Event("input"));
}

function saveActiveJobId(jobId) {
  try {
    localStorage.setItem(JOB_CACHE_KEY, String(jobId || ""));
  } catch (_) {}
}

function readActiveJobId() {
  try {
    const value = localStorage.getItem(JOB_CACHE_KEY);
    return value ? value.trim() : "";
  } catch (_) {
    return "";
  }
}

function clearActiveJobId() {
  try {
    localStorage.removeItem(JOB_CACHE_KEY);
  } catch (_) {}
}

function currentProgressStage(percent) {
  for (const step of STAGE_STEPS) {
    if (percent <= step.target) return step.name;
  }
  return STAGE_STEPS[STAGE_STEPS.length - 1].name;
}

function setProgress(percent, explicitStage) {
  const safePercent = Math.max(0, Math.min(100, Math.round(percent)));
  // Keep progress monotonic to avoid back-and-forth jitter between updates.
  progressValue = Math.max(progressValue, safePercent);
  const stage = explicitStage || currentProgressStage(progressValue);
  $("#progressStageText").textContent = stage;
  $("#progressPercentText").textContent = `${progressValue}%`;
  $("#progressFill").style.width = `${progressValue}%`;
  $("#generationProgress .generation-progress-track").setAttribute("aria-valuenow", String(progressValue));
}

function startProgress() {
  stopProgress(false);
  progressHasServerSignal = false;
  progressValue = 0;
  $("#generationProgress").classList.remove("hidden");
  setProgress(4, "读取上下文");
  progressTimer = setInterval(() => {
    if (progressHasServerSignal) return;
    const next = progressValue + Math.max(1, Math.round(Math.random() * 4));
    setProgress(Math.min(next, 92));
  }, 420);
}

function stopProgress(success) {
  if (progressTimer) {
    clearInterval(progressTimer);
    progressTimer = null;
  }
  if (!success) {
    progressHasServerSignal = false;
    progressValue = 0;
    $("#generationProgress").classList.add("hidden");
    return;
  }
  setProgress(100, "完成");
  setTimeout(() => {
    progressHasServerSignal = false;
    progressValue = 0;
    $("#generationProgress").classList.add("hidden");
  }, 700);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function setGenerateButtonBusy(busy, busyText = "生成中") {
  const button = $("#generateButton");
  if (busy) {
    button.disabled = true;
    button.classList.add("loading");
    button.textContent = busyText;
    return;
  }
  button.disabled = false;
  button.classList.remove("loading");
}

async function createGenerateJob(content) {
  return api("/api/chat/generate/jobs", {
    method: "POST",
    body: JSON.stringify({ contact_id: state.contactId, content }),
  });
}

async function waitForGenerateJob(jobId) {
  while (true) {
    const job = await api(`/api/chat/generate/jobs/${jobId}`);
    if (typeof job.progress === "number") {
      progressHasServerSignal = true;
      setProgress(job.progress, job.stage || undefined);
    }
    if (job.status === "succeeded") {
      return job.result;
    }
    if (job.status === "failed") {
      throw new Error(job.error || "生成任务失败");
    }
    await sleep(JOB_POLL_INTERVAL_MS);
  }
}

function applyGenerationResult(result, inputElement) {
  const empty = $("#messages .empty-state");
  if (empty) empty.remove();
  if (result.message && !hasSameUserBubble(result.message.content)) {
    $("#messages").appendChild(messageElement(result.message));
  }
  state.pendingPreviewMessage = result.message || null;
  state.generationId = result.generation_id;
  updateLatestDebugLink();
  renderCandidates(result.candidates, result.strategy, result.dialogue_plan?.strategy || [], result.critic);
  if (result.relationship?.current_level) {
    $("#relationshipBadge").textContent = `关系：${result.relationship.current_level}`;
  }
  inputElement.value = "";
  inputElement.style.height = "auto";
  clearActiveJobId();
  clearInputCache();
  if (result.warnings?.length) toast(result.warnings.join("；"));
}

async function resumeActiveGenerationIfAny() {
  let targetJob = null;
  const cachedJobId = readActiveJobId();
  if (cachedJobId) {
    try {
      targetJob = await api(`/api/chat/generate/jobs/${cachedJobId}`);
    } catch (_) {
      clearActiveJobId();
    }
  }
  if (!targetJob) {
    targetJob = await api("/api/chat/generate/jobs/active");
  }
  if (!targetJob) return;

  if (targetJob.status === "succeeded" && targetJob.result) {
    applyGenerationResult(targetJob.result, $("#messageInput"));
    stopProgress(true);
    toast("已恢复上次生成结果");
    return;
  }
  if (targetJob.status === "failed") {
    clearActiveJobId();
    toast(targetJob.error || "上次生成任务失败");
    return;
  }
  if (!["pending", "running"].includes(targetJob.status)) return;

  saveActiveJobId(targetJob.job_id);
  const button = $("#generateButton");
  const initialText = button.textContent || "生成 3 个回复";
  setGenerateButtonBusy(true, "生成中");
  $("#generationProgress").classList.remove("hidden");
  setProgress(targetJob.progress || 4, targetJob.stage || "处理中");
  try {
    const result = await waitForGenerateJob(targetJob.job_id);
    applyGenerationResult(result, $("#messageInput"));
    stopProgress(true);
    toast("已恢复进行中的生成任务");
  } catch (error) {
    clearActiveJobId();
    stopProgress(false);
    toast(error.message);
  } finally {
    setGenerateButtonBusy(false);
    button.textContent = initialText;
  }
}

function persistCurrentDraftState() {
  const zone = $("#draftZone");
  if (zone.classList.contains("hidden")) return;
  const candidates = [...document.querySelectorAll("#candidateGrid .candidate-card textarea")].map((node) => node.value);
  if (!candidates.length) return;
  const modes = [...document.querySelectorAll("#candidateGrid .candidate-card .candidate-mode")].map((node) => node.textContent || "候选回复");
  saveDraftCache({
    contactId: state.contactId,
    generationId: state.generationId,
    strategy: $("#strategyText").textContent || "",
    candidates,
    modes,
    previewMessage: state.pendingPreviewMessage,
    savedAt: Date.now(),
  });
}

function restoreDraftCache() {
  const cached = readCachedDraft();
  if (!cached || cached.contactId !== state.contactId) return;
  if (!Array.isArray(cached.candidates) || !cached.candidates.length) return;
  if (Date.now() - Number(cached.savedAt || 0) > 1000 * 60 * 60 * 12) {
    clearDraftCache();
    return;
  }
  if (cached.previewMessage?.content && !hasSameUserBubble(cached.previewMessage.content)) {
    $("#messages").appendChild(messageElement(cached.previewMessage));
    $("#messages").scrollTop = $("#messages").scrollHeight;
  }
  state.pendingPreviewMessage = cached.previewMessage || null;
  state.generationId = cached.generationId || state.generationId;
  updateLatestDebugLink();
  renderCandidates(cached.candidates, cached.strategy || "已恢复上次候选", cached.modes || []);
  toast("已恢复上次未确认的候选回复");
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
  const segments = String(message.content ?? "").split("\n").map((segment) => segment.trim()).filter(Boolean);
  (segments.length ? segments : [""]).forEach((segment) => {
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = segment;
    item.appendChild(bubble);
  });
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

function hasSameUserBubble(content) {
  const normalized = String(content ?? "").trim();
  if (!normalized) return false;
  const nodes = [...document.querySelectorAll("#messages .message.user .bubble")];
  return nodes.some((node) => String(node.textContent ?? "").trim() === normalized);
}

function finalCritiques(critic) {
  const reviews = critic?.reviews || [];
  const review = reviews[reviews.length - 1];
  return review?.candidates || [];
}

function segmentCount(text) {
  return String(text ?? "").split("\n").map((segment) => segment.trim()).filter(Boolean).length || 1;
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
    const segmentBadge = document.createElement("span");
    segmentBadge.className = "candidate-segments";
    const isSilence = candidate === "（暂不回复）" || modes[index] === "暂不回复";
    const updateSegmentBadge = (value) => {
      segmentBadge.textContent = isSilence ? "" : `将发送 ${segmentCount(value)} 条`;
    };
    const textarea = document.createElement("textarea");
    textarea.value = candidate;
    textarea.setAttribute("aria-label", `候选回复 ${index + 1}`);
    textarea.rows = Math.max(2, segmentCount(candidate) + 1);
    if (isSilence) textarea.readOnly = true;
    textarea.addEventListener("input", () => {
      updateSegmentBadge(textarea.value);
      persistCurrentDraftState();
    });
    updateSegmentBadge(candidate);
    card.append(mode, segmentBadge, textarea);
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
      if (isSilence) {
        $("#draftZone").classList.add("hidden");
        state.pendingPreviewMessage = null;
        clearDraftCache();
        return toast("已采纳暂不回复建议");
      }
      try {
        await copyText(textarea.value);
        toast("已复制候选回复");
      } catch (_) {
        toast("复制失败，请手动复制");
      }
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
  persistCurrentDraftState();
}

async function confirmCandidate(content, candidateIndex, button) {
  if (!content.trim()) return toast("回复不能为空");
  button.disabled = true;
  try {
    const saved = await api("/api/chat/confirm", { method: "POST", body: JSON.stringify({ contact_id: state.contactId, content, generation_id: state.generationId, candidate_index: candidateIndex }) });
    const assistantMessage = saved?.assistant_message || saved;
    const userMessage = saved?.user_message || null;
    if (userMessage && !hasSameUserBubble(userMessage.content)) {
      $("#messages").appendChild(messageElement(userMessage));
    }
    $("#messages").appendChild(messageElement(assistantMessage));
    $("#messages").scrollTop = $("#messages").scrollHeight;
    if (saved.generation_id != null) { state.generationId = saved.generation_id; updateLatestDebugLink(); }
    $("#draftZone").classList.add("hidden");
    state.pendingPreviewMessage = null;
    clearDraftCache();
    toast("已记录为真实发送内容");
  } catch (error) { toast(error.message); }
  finally { button.disabled = false; }
}

async function generate() {
  const input = $("#messageInput");
  const content = input.value.trim();
  if (!content) return toast("请输入消息");
  const button = $("#generateButton");
  const initialText = button.textContent;
  let generationSucceeded = false;
  setGenerateButtonBusy(true, "生成中");
  startProgress();
  try {
    const created = await createGenerateJob(content);
    saveActiveJobId(created.job_id);
    if (!created.accepted) {
      toast("已有进行中的任务，已接管该任务进度");
    }
    const result = await waitForGenerateJob(created.job_id);
    applyGenerationResult(result, input);
    generationSucceeded = true;
  } catch (error) {
    clearActiveJobId();
    toast(error.message);
  }
  finally {
    stopProgress(generationSucceeded);
    setGenerateButtonBusy(false);
    button.textContent = initialText || "生成 3 个回复";
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
    restoreInputCache();
    restoreDraftCache();
    await resumeActiveGenerationIfAny();
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
$("#messageInput").addEventListener("input", (event) => {
  event.target.style.height = "auto";
  event.target.style.height = `${Math.min(event.target.scrollHeight, 130)}px`;
  saveInputCache(event.target.value);
});
$("#proactiveButton").onclick = async (event) => {
  event.currentTarget.disabled = true;
  const originalLabel = event.currentTarget.textContent;
  event.currentTarget.textContent = "草稿生成中";
  try {
    const result = await api(`/api/proactive/${state.contactId}`, { method: "POST" });
    $("#messageInput").value = result.draft;
    $("#messageInput").dispatchEvent(new Event("input"));
    toast(`草稿原因：${result.reason}`);
  } catch (error) { toast(error.message); }
  finally {
    event.currentTarget.disabled = false;
    event.currentTarget.textContent = originalLabel || "✦ 生成主动聊天草稿";
  }
};
const logoutButton = $("#logoutButton");
if (logoutButton) {
  logoutButton.onclick = logout;
}
load();
