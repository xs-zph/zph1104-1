// ============================================================
// 登录页交互：校验账号 → 保存 token → 按角色跳转
// ============================================================

const form = document.getElementById("login-form");
const errBox = document.getElementById("login-error");
const btn = document.getElementById("login-btn");

form.addEventListener("submit", async (e) => {
  e.preventDefault();

  const username = document.getElementById("username").value.trim();
  const password = document.getElementById("password").value.trim();

  errBox.hidden = true;
  btn.disabled = true;
  btn.textContent = "登录中…";

  try {
    const resp = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
    });

    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || "登录失败");
    }

    const data = await resp.json();
    localStorage.setItem("ai_ticket_token", data.token);
    localStorage.setItem("ai_ticket_username", data.username);
    localStorage.setItem("ai_ticket_role", data.role);

    // 管理员进客服后台，客户进聊天界面
    if (data.role === "admin") {
      window.location.href = "/admin";
    } else {
      window.location.href = "/";
    }
  } catch (err) {
    errBox.textContent = err.message;
    errBox.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = "登 录";
  }
});
