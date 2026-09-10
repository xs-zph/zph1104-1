// ============================================================
// 聊天式 AI 客服 —— 前端交互
// 流程：登录校验 → 发消息 → 调 /api/tickets → 渲染 AI 回复
// ============================================================

const chatMessages = document.getElementById("chat-messages");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");
const imageInput = document.getElementById("image-input");
const imageUploadBtn = document.getElementById("image-upload-btn");
const imagePreview = document.getElementById("image-preview");
const MAX_IMAGE_BYTES = 8 * 1024 * 1024;
let selectedImage = null;

let username = "用户";
let humanHandoffActive = false;
let activeHumanTicketId = null;
const HUMAN_HANDOFF_STATUSES = new Set(["escalated", "in_progress", "waiting_customer"]);
const humanResponseTicketIds = new Set();
let humanResponsePollTimer = null;
const lastHumanAnswers = new Map();
let humanResponseEventSource = null;
let humanResponseReconnectTimer = null;
let humanResponseRetryDelay = 1000;
let humanResponseCursor = "";
const seenAfterSaleRequests = new Set();

function authHeaders() {
  return {
    "Content-Type": "application/json",
  };
}

// ---------- 登录校验 ----------
window.addEventListener("DOMContentLoaded", async () => {
  try {
    const resp = await fetch("/api/me", { headers: authHeaders() });
    if (!resp.ok) {
      window.location.href = "/login";
      return;
    }
    const me = await resp.json();
    username = me.username || "用户";

    document.getElementById("current-username").textContent = me.username;
    const sidebarAvatar = document.getElementById("sidebar-avatar");
    if (sidebarAvatar) sidebarAvatar.textContent = me.username.charAt(0).toUpperCase();
    startHumanResponseRealtime();
    loadActiveHumanHandoff();
    appendMessage("ai", "您好，我是 AI 客服小助手 👋\n请问有什么可以帮您的？", null);
  } catch (err) {
    window.location.href = "/login";
  }
});

// ---------- 发送消息 ----------
chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = chatInput.value.trim();
  if (!text && !selectedImage) return;

  const sentImage = selectedImage;
  appendMessage("user", text || "请帮我识别这张图片", null, {
    image_name: sentImage?.name,
  });
  chatInput.value = "";
  autoResize();
  clearSelectedImage();
  sendBtn.disabled = true;

  const typing = humanHandoffActive ? null : appendMessage("ai", "", "typing");

  try {
    const request = sentImage
      ? (() => {
          const body = new FormData();
          body.append("text", text);
          body.append("image", sentImage);
          return { url: "/api/tickets/multimodal", body };
        })()
      : { url: "/api/tickets", body: JSON.stringify({ ticket_text: text }) };
    const resp = await fetch(request.url, {
      method: "POST",
      headers: sentImage ? undefined : authHeaders(),
      body: request.body,
    });
    if (!resp.ok) {
      if (resp.status === 401) {
        window.location.href = "/login";
        return;
      }
      let message = "";
      try {
        const errorData = await resp.json();
        message = typeof errorData?.detail === "string"
          ? errorData.detail
          : (typeof errorData?.reply === "string" ? errorData.reply : "");
      } catch {
        // 非 JSON 的 5xx 响应可能包含技术性文本，不直接展示给客户。
      }
      const error = new Error(message);
      error.isApiError = true;
      throw error;
    }

    const data = await resp.json();
    typing?.remove();

    const escalated = data.reply_source === "escalate";
    const queuedForHuman = data.reply_source === "human_queue";
    const serviceError = data.reply_source === "service_error";
    if (humanHandoffActive && !escalated && !queuedForHuman) {
      // 后端发现这是新的 AI 自助诉求并结束了旧人工会话；
      // 页面也要同步解除人工状态，避免下一轮仍显示为人工处理中。
      resetHumanHandoff();
    }
    const responseType = queuedForHuman
      ? "human-pending"
      : (escalated ? "escalate" : (serviceError ? "service-error" : null));
    appendMessage(
      "ai",
      data.reply || "（无回复）",
      responseType,
      data
    );
    if ((escalated || queuedForHuman) && data.id) {
      setHumanHandoff(data.id, true);
      watchHumanResponse(data.id);
    }
  } catch (err) {
    typing?.remove();
    const message = err?.isApiError && err.message
      ? `抱歉，${err.message}。`
      : (err?.isApiError
        ? "抱歉，客服服务暂时无法处理这条消息，请稍后重试。"
        : "抱歉，暂时无法连接客服服务，请稍后重试。");
    appendMessage("ai", message, null);
  } finally {
    sendBtn.disabled = false;
    chatInput.focus();
  }
});

// ---------- 侧边栏快速提问 ----------
document.querySelectorAll(".quick-q").forEach((btn) => {
  btn.addEventListener("click", () => {
    chatInput.value = btn.dataset.q;
    chatForm.dispatchEvent(new Event("submit"));
  });
});

// ---------- 移动端工作区 ----------
const mobileToggle = document.getElementById("mobile-sidebar-toggle");
const sidebar = document.querySelector(".chat-sidebar");
mobileToggle?.addEventListener("click", () => {
  const open = sidebar.classList.toggle("is-open");
  mobileToggle.setAttribute("aria-expanded", String(open));
});

// ---------- 图片上传 ----------
imageUploadBtn?.addEventListener("click", () => imageInput?.click());
imageInput?.addEventListener("change", () => {
  const file = imageInput.files?.[0];
  if (!file) return;
  const allowed = ["image/jpeg", "image/png", "image/webp", "image/gif"];
  if (!allowed.includes(file.type)) {
    clearSelectedImage();
    alert("仅支持 JPG、PNG、WEBP 或 GIF 图片");
    return;
  }
  if (file.size > MAX_IMAGE_BYTES) {
    clearSelectedImage();
    alert("图片大小不能超过 8 MB");
    return;
  }
  selectedImage = file;
  renderImagePreview();
});

function renderImagePreview() {
  if (!imagePreview || !selectedImage) return;
  imagePreview.hidden = false;
  imagePreview.innerHTML = "";
  const name = document.createElement("span");
  name.textContent = "已选择图片：" + selectedImage.name;
  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "image-remove-btn";
  remove.title = "移除图片";
  remove.setAttribute("aria-label", "移除图片");
  remove.textContent = "×";
  remove.addEventListener("click", clearSelectedImage);
  imagePreview.append(name, remove);
}

function clearSelectedImage() {
  selectedImage = null;
  if (imageInput) imageInput.value = "";
  if (imagePreview) {
    imagePreview.hidden = true;
    imagePreview.innerHTML = "";
  }
}

// ---------- 人工回复回传 ----------
function setHumanHandoff(ticketId, active) {
  humanHandoffActive = active;
  activeHumanTicketId = active && ticketId ? String(ticketId) : null;
  const endButton = document.getElementById("end-human-session-btn");
  if (endButton) endButton.hidden = !active;
  const tip = document.querySelector(".chat-header-tip");
  if (tip) {
    tip.textContent = active
      ? "人工客服处理中，后续消息将直接发送给人工客服"
      : "处理不了的问题会自动转接人工客服";
  }
}

function resetHumanHandoff() {
  humanResponseTicketIds.clear();
  lastHumanAnswers.clear();
  if (humanResponsePollTimer !== null) {
    clearInterval(humanResponsePollTimer);
    humanResponsePollTimer = null;
  }
  setHumanHandoff(null, false);
}

async function endHumanSession() {
  const resp = await fetch("/api/human-session/end", {
    method: "POST",
    headers: authHeaders(),
  });
  if (resp.status === 401) {
    window.location.href = "/login";
    return false;
  }
  if (!resp.ok) throw new Error(await resp.text());
  resetHumanHandoff();
  return true;
}

async function loadActiveHumanHandoff() {
  try {
    const resp = await fetch("/api/tickets", { headers: authHeaders() });
    if (!resp.ok) return;
    const tickets = await resp.json();
    const active = tickets.find((ticket) => HUMAN_HANDOFF_STATUSES.has(ticket.status));
    if (active) {
      setHumanHandoff(active.id, true);
      watchHumanResponse(active.id);
    }
  } catch (err) {
    // 后端发送消息时仍会强制校验人工状态，初始化查询失败不影响使用。
  }
}

function watchHumanResponse(ticketId) {
  humanResponseTicketIds.add(String(ticketId));
  if (humanResponseEventSource === null && humanResponsePollTimer === null) {
    humanResponsePollTimer = window.setInterval(pollHumanResponses, 3000);
  }
}

function handleHumanResponse(ticketId, answer, handoffActive = false) {
  ticketId = String(ticketId);
  if (!humanResponseTicketIds.has(ticketId)) return;
  if (handoffActive && lastHumanAnswers.get(ticketId) === answer) return;
  lastHumanAnswers.set(ticketId, answer);
  if (handoffActive) {
    setHumanHandoff(ticketId, true);
  } else {
    humanResponseTicketIds.delete(ticketId);
    setHumanHandoff(null, false);
  }
  appendMessage("ai", answer || "人工客服已处理您的问题。", "human-resolved");
}

function handleAfterSaleUpdate(data) {
  const request = data?.after_sale;
  if (!request?.request_no || seenAfterSaleRequests.has(request.request_no)) return;
  seenAfterSaleRequests.add(request.request_no);
  const label = request.request_type_label || "售后申请";
  const status = request.status_label || "处理中";
  appendMessage(
    "ai",
    `${label}已提交，申请号：${request.request_no}，当前状态：${status}。您可以在“我的售后”中查看进度。`,
    "after-sale-update",
  );
}

function startHumanResponseRealtime() {
  if (typeof EventSource === "undefined" || humanResponseEventSource !== null) return;
  const query = humanResponseCursor ? `?since=${encodeURIComponent(humanResponseCursor)}` : "";
  const source = new EventSource("/api/events" + query);
  humanResponseEventSource = source;
  source.onopen = () => {
    humanResponseRetryDelay = 1000;
    if (humanResponsePollTimer !== null) {
      window.clearInterval(humanResponsePollTimer);
      humanResponsePollTimer = null;
    }
  };
  source.addEventListener("human_replied", (event) => {
    try {
      humanResponseCursor = event.lastEventId || humanResponseCursor;
      const data = JSON.parse(event.data);
      handleHumanResponse(data.ticket_id, data.human_answer, data.handoff_active === true);
    } catch (err) {
      // 格式异常时由轮询补偿。
    }
  });
  source.addEventListener("after_sale_updated", (event) => {
    try {
      humanResponseCursor = event.lastEventId || humanResponseCursor;
      handleAfterSaleUpdate(JSON.parse(event.data));
    } catch (err) {
      // 格式异常时不影响人工回复监听。
    }
  });
  source.onerror = () => {
    if (humanResponseEventSource !== source) return;
    source.close();
    humanResponseEventSource = null;
    if (humanResponseTicketIds.size && humanResponsePollTimer === null) {
      humanResponsePollTimer = window.setInterval(pollHumanResponses, 3000);
    }
    if (humanResponseReconnectTimer === null) {
      const delay = humanResponseRetryDelay;
      humanResponseRetryDelay = Math.min(humanResponseRetryDelay * 2, 30000);
      humanResponseReconnectTimer = window.setTimeout(() => {
        humanResponseReconnectTimer = null;
        startHumanResponseRealtime();
      }, delay);
    }
  };
}

async function pollHumanResponses() {
  if (document.hidden || humanResponseTicketIds.size === 0) return;
  const ticketIds = [...humanResponseTicketIds];
  await Promise.all(ticketIds.map(async (ticketId) => {
    try {
      const resp = await fetch("/api/tickets/" + encodeURIComponent(ticketId), {
        headers: authHeaders(),
      });
      if (resp.status === 401) {
        window.location.href = "/login";
        return;
      }
      if (resp.status === 404) {
        humanResponseTicketIds.delete(ticketId);
        lastHumanAnswers.delete(ticketId);
        return;
      }
      if (!resp.ok) return;
      const ticket = await resp.json();
      if (!ticket.human_answer || lastHumanAnswers.get(ticketId) === ticket.human_answer) return;
      handleHumanResponse(
        ticketId,
        ticket.human_answer,
        HUMAN_HANDOFF_STATUSES.has(ticket.status),
      );
    } catch (err) {
      // 网络短暂波动时保留监听，下一轮继续查询。
    }
  }));
  if (humanResponseTicketIds.size === 0 && humanResponsePollTimer !== null) {
    clearInterval(humanResponsePollTimer);
    humanResponsePollTimer = null;
  }
}

// ---------- 渲染消息 ----------
function appendMessage(role, text, type, data) {
  const msg = document.createElement("div");
  msg.className = "msg " + role + (type ? " " + type : "");

  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.textContent = role === "ai"
    ? ((type === "human-pending" || type === "human-resolved") ? "人工" : "AI")
    : username.charAt(0).toUpperCase();

  const wrap = document.createElement("div");
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";

  // 服务异常与人工接管分开显示，避免把可重试问题误认为已转人工。
  if (type === "service-error") {
    const badge = document.createElement("span");
    badge.className = "msg-badge";
    badge.textContent = "实时查询异常 · 可重试";
    bubble.appendChild(badge);
    bubble.appendChild(document.createElement("br"));
  } else if (type === "escalate" || type === "human-pending" || type === "human-resolved") {
    const badge = document.createElement("span");
    badge.className = "msg-badge";
    badge.textContent = type === "human-resolved"
      ? "人工客服已回复"
      : (type === "human-pending" ? "人工客服处理中" : "已转人工客服");
    bubble.appendChild(badge);
    bubble.appendChild(document.createElement("br"));
  }
  bubble.appendChild(document.createTextNode(text));
  wrap.appendChild(bubble);

  if (data?.image_name) {
    const attachment = document.createElement("div");
    attachment.className = "msg-attachment";
    attachment.textContent = "图片：" + data.image_name;
    wrap.appendChild(attachment);
  }
  if (data?.image_analysis_status) {
    const analysis = document.createElement("div");
    analysis.className = "msg-image-analysis";
    analysis.textContent = data.image_analysis
      ? "图片识别：" + data.image_analysis
      : "图片识别暂不可用，已按文字内容继续处理。";
    wrap.appendChild(analysis);
  }
  if (data?.ocr_status && data.ocr_status !== "empty") {
    const ocr = document.createElement("div");
    ocr.className = "msg-image-analysis";
    ocr.textContent = data.ocr_text
      ? "图片文字：" + data.ocr_text
      : "图片文字识别暂不可用。";
    wrap.appendChild(ocr);
  }

  if (data?.quick_replies?.length) {
    const options = document.createElement("div");
    options.className = "quick-replies";
    data.quick_replies.forEach((option) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "quick-reply-btn";
      button.textContent = option.label || option.value || option;
      button.addEventListener("click", () => {
        chatInput.value = option.value || option.label || option;
        chatForm.dispatchEvent(new Event("submit"));
        options.querySelectorAll("button").forEach((item) => { item.disabled = true; });
      });
      options.appendChild(button);
    });
    wrap.appendChild(options);
  }

  // 自动业务回复（Agent / 模板）附带 👍👎 满意度按钮，形成评价闭环
  if (data && data.id && (data.reply_source === "agent" || data.reply_source === "template")) {
    addFeedbackButtons(wrap, data.id);
  }

  msg.appendChild(avatar);
  msg.appendChild(wrap);
  chatMessages.appendChild(msg);
  chatMessages.scrollTop = chatMessages.scrollHeight;
  return msg;
}

// ---------- 满意度评价 👍👎 ----------
function addFeedbackButtons(wrap, ticketId) {
  const row = document.createElement("div");
  row.className = "msg-feedback";

  const up = document.createElement("button");
  up.className = "fb-btn fb-up";
  up.textContent = "👍 有帮助";
  const down = document.createElement("button");
  down.className = "fb-btn fb-down";
  down.textContent = "👎 没帮助";

  async function send(feedback, btn) {
    try {
      await fetch("/api/tickets/" + ticketId + "/feedback", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ feedback: feedback }),
      });
      up.disabled = true;
      down.disabled = true;
      btn.classList.add("selected");
    } catch (err) {
      // 评价失败静默处理，不影响主流程
    }
  }

  up.addEventListener("click", () => send("up", up));
  down.addEventListener("click", () => send("down", down));
  row.appendChild(up);
  row.appendChild(down);
  wrap.appendChild(row);
}

// ---------- 输入框自适应高度 ----------
chatInput.addEventListener("input", autoResize);
chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    chatForm.dispatchEvent(new Event("submit"));
  }
});

function autoResize() {
  chatInput.style.height = "auto";
  chatInput.style.height = Math.min(chatInput.scrollHeight, 120) + "px";
}

// ---------- 新对话（清空多轮记忆） ----------
document.getElementById("new-chat-btn").addEventListener("click", async () => {
  try {
    const resp = await fetch("/api/conversation/clear", { method: "POST", headers: authHeaders() });
    if (!resp.ok) throw new Error(await resp.text());
    resetHumanHandoff();
  } catch (err) {
    appendMessage("ai", "新对话开启失败，请稍后重试。", null);
    return;
  }
  chatMessages.innerHTML = "";
  appendMessage("ai", "已开启新对话，我会忘记之前的聊天内容。请问有什么可以帮您的？", null);
});

document.getElementById("end-human-session-btn").addEventListener("click", async () => {
  const button = document.getElementById("end-human-session-btn");
  button.disabled = true;
  try {
    if (await endHumanSession()) {
      appendMessage("ai", "已结束人工会话。接下来的问题将由 AI 客服继续处理。", null);
    }
  } catch (err) {
    appendMessage("ai", "结束人工会话失败，请稍后重试。", null);
  } finally {
    button.disabled = false;
  }
});

// ---------- 退出登录 ----------
document.getElementById("logout-btn").addEventListener("click", async () => {
  try {
    await fetch("/api/logout", { method: "POST", headers: authHeaders() });
  } catch (err) {
    // 忽略登出接口错误，直接清本地状态
  }
  window.location.href = "/login";
});
