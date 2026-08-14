// ============================================================
// 聊天式 AI 客服 —— 前端交互
// 流程：登录校验 → 发消息 → 调 /api/tickets → 渲染 AI 回复
// ============================================================

const chatMessages = document.getElementById("chat-messages");
const chatForm = document.getElementById("chat-form");
const chatInput = document.getElementById("chat-input");
const sendBtn = document.getElementById("send-btn");

const token = localStorage.getItem("ai_ticket_token") || "";
const username = localStorage.getItem("ai_ticket_username") || "用户";
const role = localStorage.getItem("ai_ticket_role") || "customer";

function authHeaders() {
  return {
    "Content-Type": "application/json",
    Authorization: "Bearer " + token,
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

    document.getElementById("current-username").textContent = me.username;
    document.getElementById("current-role").textContent =
      me.role === "admin" ? "管理员" : "客户";

    // 管理员显示「进入客服后台」入口
    if (me.role === "admin") {
      document.getElementById("admin-link").hidden = false;
    }

    appendMessage("ai", "您好，我是 AI 客服小助手 👋\n请问有什么可以帮您的？", null);
  } catch (err) {
    window.location.href = "/login";
  }
});

// ---------- 发送消息 ----------
chatForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const text = chatInput.value.trim();
  if (!text) return;

  appendMessage("user", text, null);
  chatInput.value = "";
  autoResize();
  sendBtn.disabled = true;

  const typing = appendMessage("ai", "", "typing");

  try {
    const resp = await fetch("/api/tickets", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ ticket_text: text }),
    });
    if (!resp.ok) {
      if (resp.status === 401) {
        window.location.href = "/login";
        return;
      }
      throw new Error(await resp.text());
    }

    const data = await resp.json();
    typing.remove();

    const escalated = data.reply_source === "escalate";
    appendMessage(
      "ai",
      data.reply || "（无回复）",
      escalated ? "escalate" : null,
      data
    );
  } catch (err) {
    typing.remove();
    appendMessage("ai", "抱歉，系统开小差了，请稍后再试。", null);
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

// ---------- 渲染消息 ----------
function appendMessage(role, text, type, data) {
  const msg = document.createElement("div");
  msg.className = "msg " + role + (type ? " " + type : "");

  const avatar = document.createElement("div");
  avatar.className = "msg-avatar";
  avatar.textContent = role === "ai" ? "AI" : username.charAt(0).toUpperCase();

  const wrap = document.createElement("div");
  const bubble = document.createElement("div");
  bubble.className = "msg-bubble";

  // 转人工时显示醒目徽章
  if (type === "escalate") {
    const badge = document.createElement("span");
    badge.className = "msg-badge";
    badge.textContent = "已转人工客服";
    bubble.appendChild(badge);
    bubble.appendChild(document.createElement("br"));
  }
  bubble.appendChild(document.createTextNode(text));
  wrap.appendChild(bubble);

  // 自动回复时附带分类 / 置信度小字（方便演示时讲解）；闲聊回复不展示
  if (data && data.category && data.reply_source !== "chat") {
    const meta = document.createElement("div");
    meta.className = "msg-meta";
    meta.textContent =
      "分类：" + data.category +
      " · 置信度 " + Math.round((data.confidence || 0) * 100) + "%";
    wrap.appendChild(meta);
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
    await fetch("/api/conversation/clear", { method: "POST", headers: authHeaders() });
  } catch (err) {
    // 忽略清空接口错误，直接清本地聊天记录
  }
  chatMessages.innerHTML = "";
  appendMessage("ai", "已开启新对话，我会忘记之前的聊天内容。请问有什么可以帮您的？", null);
});

// ---------- 退出登录 ----------
document.getElementById("logout-btn").addEventListener("click", async () => {
  try {
    await fetch("/api/logout", { method: "POST", headers: authHeaders() });
  } catch (err) {
    // 忽略登出接口错误，直接清本地状态
  }
  localStorage.removeItem("ai_ticket_token");
  localStorage.removeItem("ai_ticket_username");
  localStorage.removeItem("ai_ticket_role");
  window.location.href = "/login";
});
