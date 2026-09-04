// 注册页：提交给后端，角色和会话由后端决定。
const registerForm = document.getElementById("register-form");
const registerError = document.getElementById("register-error");
const registerButton = document.getElementById("register-btn");

function registerApiError(data) {
  if (typeof data.detail === "string") return data.detail;
  if (Array.isArray(data.detail)) return "请检查表单内容";
  return "注册失败，请稍后重试";
}

registerForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  registerError.hidden = true;
  registerButton.disabled = true;
  registerButton.textContent = "注册中...";

  try {
    const response = await fetch("/api/register", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("username").value.trim(),
        password: document.getElementById("password").value,
        confirm_password: document.getElementById("confirm-password").value,
      }),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(registerApiError(data));
    window.location.href = "/";
  } catch (error) {
    registerError.textContent = error.message;
    registerError.hidden = false;
  } finally {
    registerButton.disabled = false;
    registerButton.textContent = "注册并进入";
  }
});
