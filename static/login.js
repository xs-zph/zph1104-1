// 登录页只负责流程呈现；会话、角色和验证码校验全部由后端完成。
const modeStage = document.getElementById("mode-stage");
const credentialsStage = document.getElementById("credentials-stage");
const phoneStage = document.getElementById("phone-stage");
const credentialsForm = document.getElementById("login-form");
const phoneForm = document.getElementById("phone-form");
const loginError = document.getElementById("login-error");
const phoneError = document.getElementById("phone-error");
const phoneHint = document.getElementById("phone-hint");
const loginButton = document.getElementById("login-btn");
const verifyButton = document.getElementById("verify-phone-btn");
const sendCodeButton = document.getElementById("send-code-btn");
const registerLink = document.getElementById("register-link");
const modeLabel = document.getElementById("selected-mode-label");
const phoneDescription = document.getElementById("phone-description");

let selectedMode = "";
let challengeId = "";
let challengeRole = "";
let countdownTimer = null;

const rolePaths = {
  admin: "/admin",
  manager: "/manager",
  agent: "/staff",
  customer: "/",
};

function showStage(stage) {
  [modeStage, credentialsStage, phoneStage].forEach((item) => item.classList.add("hidden"));
  stage.classList.remove("hidden");
}

function showError(box, message) {
  box.textContent = message;
  box.hidden = false;
}

function clearMessages() {
  loginError.hidden = true;
  phoneError.hidden = true;
  phoneHint.hidden = true;
}

function apiError(data, fallback) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return "请检查输入内容";
  return fallback;
}

function roleMatchesMode(role) {
  return selectedMode === "personal"
    ? role === "customer"
    : ["admin", "manager", "agent"].includes(role);
}

function goToRole(role) {
  window.location.href = rolePaths[role] || "/";
}

function setSelectedMode(mode) {
  selectedMode = mode;
  document.querySelectorAll(".login-mode-card").forEach((card) => {
    card.classList.toggle("is-selected", card.dataset.mode === mode);
  });
  const isEnterprise = mode === "enterprise";
  modeLabel.innerHTML = `<span class="phone-step-number">01</span><span><strong>${isEnterprise ? "企业端登录" : "个人端登录"}</strong><small>${isEnterprise ? "管理员、经理与客服账号" : "个人客户服务空间"}</small></span>`;
  registerLink.hidden = isEnterprise;
  clearMessages();
  showStage(credentialsStage);
  window.setTimeout(() => document.getElementById("username").focus(), 80);
}

function resetToModes() {
  clearMessages();
  challengeId = "";
  challengeRole = "";
  stopCountdown();
  credentialsForm.reset();
  phoneForm.reset();
  document.querySelectorAll(".login-mode-card").forEach((card) => card.classList.remove("is-selected"));
  showStage(modeStage);
}

function stopCountdown() {
  if (countdownTimer) window.clearInterval(countdownTimer);
  countdownTimer = null;
  sendCodeButton.disabled = false;
  sendCodeButton.textContent = "发送验证码";
}

function startCountdown(seconds) {
  let remaining = Math.max(1, Math.min(60, Number(seconds) || 60));
  sendCodeButton.disabled = true;
  sendCodeButton.textContent = `${remaining}s 后重发`;
  countdownTimer = window.setInterval(() => {
    remaining -= 1;
    if (remaining <= 0) {
      stopCountdown();
      return;
    }
    sendCodeButton.textContent = `${remaining}s 后重发`;
  }, 1000);
}

async function requestJson(url, payload) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(apiError(data, "请求失败，请稍后重试"));
  return data;
}

document.querySelectorAll(".login-mode-card").forEach((card) => {
  card.addEventListener("click", () => setSelectedMode(card.dataset.mode));
});

document.getElementById("back-to-modes").addEventListener("click", resetToModes);
document.getElementById("back-to-credentials").addEventListener("click", () => {
  clearMessages();
  stopCountdown();
  phoneForm.reset();
  showStage(credentialsStage);
});

credentialsForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearMessages();
  loginButton.disabled = true;
  loginButton.innerHTML = "验证账号中 <span aria-hidden=\"true\">...</span>";

  try {
    const data = await requestJson("/api/login", {
      username: document.getElementById("username").value.trim(),
      password: document.getElementById("password").value,
    });
    if (!roleMatchesMode(data.role)) {
      throw new Error(selectedMode === "personal" ? "这是企业账号，请从企业端登录" : "这是个人账号，请从个人端登录");
    }
    if (data.requires_phone_verification) {
      challengeId = data.challenge_id;
      challengeRole = data.role;
      const masked = data.phone_masked && data.phone_masked !== "未绑定" ? `当前绑定手机号：${data.phone_masked}` : "请输入常用手机号接收验证码";
      phoneDescription.textContent = masked;
      phoneHint.hidden = true;
      showStage(phoneStage);
      window.setTimeout(() => document.getElementById("phone").focus(), 80);
      return;
    }
    goToRole(data.role);
  } catch (error) {
    showError(loginError, error.message);
  } finally {
    loginButton.disabled = false;
    loginButton.innerHTML = "继续登录 <span aria-hidden=\"true\">→</span>";
  }
});

sendCodeButton.addEventListener("click", async () => {
  phoneError.hidden = true;
  phoneHint.hidden = true;
  sendCodeButton.disabled = true;
  sendCodeButton.textContent = "发送中...";
  try {
    const data = await requestJson("/api/login/send-code", {
      challenge_id: challengeId,
      phone: document.getElementById("phone").value.trim(),
    });
    if (data.demo_code) {
      phoneHint.textContent = `演示环境验证码：${data.demo_code}`;
      phoneHint.hidden = false;
    } else {
      phoneHint.textContent = `验证码已发送至 ${data.phone_masked || "绑定手机号"}`;
      phoneHint.hidden = false;
    }
    startCountdown(60);
    document.getElementById("phone-code").focus();
  } catch (error) {
    showError(phoneError, error.message);
    stopCountdown();
  }
});

phoneForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearMessages();
  verifyButton.disabled = true;
  verifyButton.innerHTML = "核验中 <span aria-hidden=\"true\">...</span>";
  try {
    const data = await requestJson("/api/login/verify-phone", {
      challenge_id: challengeId,
      phone: document.getElementById("phone").value.trim(),
      code: document.getElementById("phone-code").value.trim(),
    });
    stopCountdown();
    goToRole(challengeRole || data.role);
  } catch (error) {
    showError(phoneError, error.message);
  } finally {
    verifyButton.disabled = false;
    verifyButton.innerHTML = "完成验证 <span aria-hidden=\"true\">→</span>";
  }
});
