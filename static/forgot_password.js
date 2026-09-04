// 忘记密码页：重置口令只提交给后端校验。
const resetForm = document.getElementById("forgot-password-form");
const resetError = document.getElementById("forgot-password-error");
const resetSuccess = document.getElementById("forgot-password-success");
const resetButton = document.getElementById("forgot-password-btn");

function resetApiError(data) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return "请检查表单内容";
  return "重置失败，请稍后重试";
}

resetForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  resetError.hidden = true;
  resetSuccess.hidden = true;
  resetButton.disabled = true;
  resetButton.textContent = "处理中...";

  try {
    const response = await fetch("/api/password-reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("username").value.trim(),
        reset_code: document.getElementById("reset-code").value,
        password: document.getElementById("password").value,
        confirm_password: document.getElementById("confirm-password").value,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(resetApiError(data));
    resetSuccess.textContent = "密码已重置，请使用新密码登录。";
    resetSuccess.hidden = false;
    resetForm.reset();
  } catch (error) {
    resetError.textContent = error.message;
    resetError.hidden = false;
  } finally {
    resetButton.disabled = false;
    resetButton.textContent = "重置密码";
  }
});
