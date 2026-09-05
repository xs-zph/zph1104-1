// 忘记密码页：先申请服务端挑战，再提交一次性验证码。
const resetForm = document.getElementById("forgot-password-form");
const resetError = document.getElementById("forgot-password-error");
const resetSuccess = document.getElementById("forgot-password-success");
const requestButton = document.getElementById("request-reset-btn");
const resetStep = document.getElementById("reset-step");
const resetButton = document.getElementById("forgot-password-btn");
let challengeId = "";

function resetApiError(data) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return "请检查表单内容";
  return "重置失败，请稍后重试";
}

resetForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  resetError.hidden = true;
  resetSuccess.hidden = true;
  requestButton.disabled = true;
  requestButton.textContent = "发送中...";

  try {
    const response = await fetch("/api/password-reset/request", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("username").value.trim(),
        phone: document.getElementById("phone").value.trim(),
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(resetApiError(data));
    challengeId = data.challenge_id || "";
    resetStep.hidden = false;
    requestButton.hidden = true;
    document.getElementById("username").disabled = true;
    document.getElementById("phone").disabled = true;
    document.getElementById("reset-code").focus();
    if (data.demo_code) {
      document.getElementById("reset-code").value = data.demo_code;
      resetSuccess.textContent = `演示验证码：${data.demo_code}`;
    } else {
      resetSuccess.textContent = `验证码已发送至 ${data.phone_masked || "绑定手机号"}`;
    }
    resetSuccess.hidden = false;
  } catch (error) {
    resetError.textContent = error.message;
    resetError.hidden = false;
  } finally {
    requestButton.disabled = false;
    requestButton.textContent = "获取验证码";
  }
});

resetButton.addEventListener("click", async () => {
  resetError.hidden = true;
  resetSuccess.hidden = true;
  if (!challengeId) {
    resetError.textContent = "请先获取验证码";
    resetError.hidden = false;
    return;
  }
  resetButton.disabled = true;
  resetButton.textContent = "处理中...";
  try {
    const response = await fetch("/api/password-reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        challenge_id: challengeId,
        code: document.getElementById("reset-code").value.trim(),
        password: document.getElementById("password").value,
        confirm_password: document.getElementById("confirm-password").value,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(resetApiError(data));
    resetSuccess.textContent = "密码已重置，请使用新密码登录。";
    resetSuccess.hidden = false;
    resetButton.disabled = true;
  } catch (error) {
    resetError.textContent = error.message;
    resetError.hidden = false;
    resetButton.disabled = false;
  } finally {
    if (!resetButton.disabled) resetButton.textContent = "重置密码";
  }
});
