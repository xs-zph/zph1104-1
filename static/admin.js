// ============================================================
// 客服后台 —— 前端交互
// 流程：校验管理员身份 → 加载指标 + 升级工单 → 人工标记已处理
// ============================================================

const token = localStorage.getItem("ai_ticket_token") || "";
const username = localStorage.getItem("ai_ticket_username") || "客服";

function authHeaders() {
  return {
    "Content-Type": "application/json",
    Authorization: "Bearer " + token,
  };
}

// ---------- 登录与权限校验 ----------
window.addEventListener("DOMContentLoaded", async () => {
  try {
    const resp = await fetch("/api/me", { headers: authHeaders() });
    if (!resp.ok) {
      window.location.href = "/login";
      return;
    }
    const me = await resp.json();
    if (me.role !== "admin") {
      // 非管理员，跳回聊天界面
      window.location.href = "/";
      return;
    }
    document.getElementById("admin-username").textContent = "客服：" + me.username;
    refreshMetrics();
    refreshEscalations();
    refreshFaq();
    loadOrders();
  } catch (err) {
    window.location.href = "/login";
  }
});

document.getElementById("refresh-btn").addEventListener("click", () => {
  refreshMetrics();
  refreshEscalations();
});

// ---------- 加载指标 ----------
async function refreshMetrics() {
  try {
    const resp = await fetch("/api/metrics", { headers: authHeaders() });
    const m = await resp.json();

    document.getElementById("m-total").textContent = m.total ?? "—";
    document.getElementById("m-auto-rate").textContent =
      m.auto_rate != null ? Math.round(m.auto_rate * 100) + "%" : "—";
    document.getElementById("m-accuracy").textContent =
      m.accuracy != null ? Math.round(m.accuracy * 100) + "%" : "—";
    document.getElementById("m-latency").textContent =
      m.avg_latency_ms != null ? m.avg_latency_ms + " ms" : "—";
  } catch (err) {
    console.error("加载指标失败", err);
  }
}

// ---------- 加载升级工单 ----------
async function refreshEscalations() {
  const list = document.getElementById("escalation-list");
  try {
    const resp = await fetch("/api/escalations", { headers: authHeaders() });
    if (!resp.ok) throw new Error(await resp.text());
    const tickets = await resp.json();

    if (!tickets.length) {
      list.innerHTML = '<div class="empty">暂无升级工单 🎉</div>';
      return;
    }

    list.innerHTML = tickets.map(renderCard).join("");

    // 绑定「标记已处理」按钮
    list.querySelectorAll("[data-resolve]").forEach((btn) => {
      btn.addEventListener("click", () => resolveTicket(btn.dataset.resolve));
    });
  } catch (err) {
    list.innerHTML = '<div class="empty">加载失败：' + escapeHtml(err.message) + "</div>";
  }
}

// ---------- 渲染单张升级工单卡片 ----------
function renderCard(t) {
  const isResolved = t.status === "resolved";
  const statusTag = isResolved
    ? '<span class="tag resolved">已处理</span>'
    : '<span class="tag escalated">待处理</span>';

  const body = isResolved
    ? `<div class="escalation-answer"><b>✓ 人工回答：</b>${escapeHtml(t.human_answer || "—")}</div>`
    : `
      <div class="human-answer-box">
        <textarea class="human-answer" data-answer="${t.id}" placeholder="请输入人工客服的答案…"></textarea>
        <label class="kb-check">
          <input type="checkbox" data-savekb="${t.id}" checked> 将回答存入知识库，供 AI 下次自动回答
        </label>
      </div>
      <div class="escalation-actions">
        <button class="btn btn-primary" data-resolve="${t.id}">提交回答</button>
      </div>`;

  return `
    <div class="escalation-card ${isResolved ? "resolved" : ""}">
      <div class="escalation-top">
        ${statusTag}
        <span class="tag">${escapeHtml(t.category || "未知")}</span>
        <span class="tag" style="background:rgba(100,116,139,0.28)">置信度 ${Math.round((t.confidence || 0) * 100)}%</span>
      </div>
      <div class="escalation-text">${escapeHtml(t.ticket_text)}</div>
      <div class="escalation-meta">
        <span>AI 回复：${escapeHtml(t.reply || "—")}</span>
      </div>
      ${body}
      <div class="escalation-meta">
        <span>#${t.id}</span>
        <span>${escapeHtml(t.created_at || "")}</span>
      </div>
    </div>`;
}

// ---------- 标记已处理 ----------
async function resolveTicket(id) {
  const answerEl = document.querySelector(`[data-answer="${id}"]`);
  const saveKbEl = document.querySelector(`[data-savekb="${id}"]`);
  const answer = answerEl ? answerEl.value.trim() : "";
  const saveToKb = saveKbEl ? saveKbEl.checked : true;

  if (!answer) {
    alert("请先填写人工回答内容");
    return;
  }

  try {
    const resp = await fetch("/api/escalations/" + id + "/resolve", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ human_answer: answer, save_to_kb: saveToKb }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    const data = await resp.json();
    refreshEscalations();
    refreshMetrics();
    alert(data.saved_to_kb
      ? "✅ 已处理，问答已存入知识库，AI 下次可自动回答同类问题"
      : "✅ 已处理（未存入知识库）");
  } catch (err) {
    alert("操作失败：" + err.message);
  }
}

// ---------- 知识库管理（实时更新） ----------
async function refreshFaq() {
  try {
    const resp = await fetch("/api/faq", { headers: authHeaders() });
    if (!resp.ok) throw new Error(await resp.text());
    const entries = await resp.json();
    document.getElementById("faq-count").textContent = "共 " + entries.length + " 条";
    const list = document.getElementById("faq-list");
    if (!entries.length) {
      list.innerHTML = '<div class="empty">暂无知识</div>';
      return;
    }
    list.innerHTML = entries.map((e) => `
      <div class="faq-item">
        <div class="faq-q">${escapeHtml(e.question)}</div>
        <div class="faq-a">${escapeHtml(e.answer)}</div>
      </div>`).join("");
  } catch (err) {
    console.error("加载知识库失败", err);
  }
}

document.getElementById("faq-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const question = document.getElementById("faq-question").value.trim();
  const answer = document.getElementById("faq-answer").value.trim();
  if (!question || !answer) return;

  const btn = e.target.querySelector("button[type=submit]");
  btn.disabled = true;
  btn.textContent = "新增中…";
  try {
    const resp = await fetch("/api/faq", {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ question, answer }),
    });
    if (!resp.ok) {
      const data = await resp.json().catch(() => ({}));
      throw new Error(data.detail || "新增失败");
    }
    document.getElementById("faq-question").value = "";
    document.getElementById("faq-answer").value = "";
    refreshFaq();
    alert("✅ 知识已实时写入知识库，可立即被检索到");
  } catch (err) {
    alert("新增失败：" + err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = "新增知识";
  }
});

// ---------- 订单管理（增删改查） ----------
async function loadOrders() {
  try {
    const resp = await fetch("/api/orders", { headers: authHeaders() });
    if (!resp.ok) throw new Error(await resp.text());
    const orders = await resp.json();
    document.getElementById("order-count").textContent = "共 " + orders.length + " 单";
    const list = document.getElementById("order-list");
    if (!orders.length) {
      list.innerHTML = '<div class="empty">暂无订单</div>';
      return;
    }
    list.innerHTML = `
      <table class="order-table">
        <thead>
          <tr>
            <th>订单号</th><th>归属用户</th><th>商品</th><th>状态</th>
            <th>运单号</th><th>退款状态</th><th>操作</th>
          </tr>
        </thead>
        <tbody>${orders.map(renderOrderRow).join("")}</tbody>
      </table>`;

    list.querySelectorAll("[data-order-edit]").forEach((btn) => {
      btn.addEventListener("click", () => editOrder(btn.dataset.orderEdit, orders));
    });
    list.querySelectorAll("[data-order-del]").forEach((btn) => {
      btn.addEventListener("click", () => deleteOrder(btn.dataset.orderDel));
    });
  } catch (err) {
    document.getElementById("order-list").innerHTML =
      '<div class="empty">加载失败：' + escapeHtml(err.message) + "</div>";
  }
}

function statusTagClass(status) {
  if (status === "已完成" || status === "已退货") return "resolved";
  if (status === "已发货") return "shipped";
  if (status === "已取消") return "cancelled";
  return "pending"; // 待发货
}

function renderOrderRow(o) {
  return `
    <tr>
      <td class="mono">${escapeHtml(o.order_id)}</td>
      <td>${escapeHtml(o.username)}</td>
      <td>${escapeHtml(o.product || "—")}</td>
      <td><span class="tag ${statusTagClass(o.status)}">${escapeHtml(o.status || "—")}</span></td>
      <td class="mono">${escapeHtml(o.tracking_no || "—")}</td>
      <td>${escapeHtml(o.refund_status || "—")}</td>
      <td class="order-ops">
        <button class="btn btn-ghost btn-sm" data-order-edit="${escapeHtml(o.order_id)}">编辑</button>
        <button class="btn btn-light btn-sm" data-order-del="${escapeHtml(o.order_id)}">删除</button>
      </td>
    </tr>`;
}

function editOrder(orderId, orders) {
  const o = orders.find((x) => x.order_id === orderId);
  if (!o) return;
  document.getElementById("order-edit-key").value = o.order_id;
  document.getElementById("order-id").value = o.order_id || "";
  document.getElementById("order-id").disabled = true; // 订单号是主键，编辑时锁定
  document.getElementById("order-user").value = o.username || "";
  document.getElementById("order-product").value = o.product || "";
  document.getElementById("order-status").value = o.status || "待发货";
  document.getElementById("order-tracking").value = o.tracking_no || "";
  document.getElementById("order-logistics").value = o.logistics || "";
  document.getElementById("order-refund").value = o.refund_status || "";
  document.getElementById("order-submit-btn").textContent = "保存修改";
}

function resetOrderForm() {
  document.getElementById("order-edit-key").value = "";
  document.getElementById("order-id").value = "";
  document.getElementById("order-id").disabled = false;
  document.getElementById("order-user").value = "user";
  document.getElementById("order-product").value = "";
  document.getElementById("order-status").value = "待发货";
  document.getElementById("order-tracking").value = "";
  document.getElementById("order-logistics").value = "";
  document.getElementById("order-refund").value = "";
  document.getElementById("order-submit-btn").textContent = "新增订单";
}

async function deleteOrder(orderId) {
  if (!confirm("确认删除订单 " + orderId + " ？")) return;
  try {
    const resp = await fetch("/api/orders/" + encodeURIComponent(orderId), {
      method: "DELETE",
      headers: authHeaders(),
    });
    if (!resp.ok) throw new Error(await resp.text());
    loadOrders();
  } catch (err) {
    alert("删除失败：" + err.message);
  }
}

document.getElementById("order-reset-btn").addEventListener("click", resetOrderForm);

document.getElementById("order-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const editKey = document.getElementById("order-edit-key").value;
  const payload = {
    username: document.getElementById("order-user").value.trim() || "user",
    product: document.getElementById("order-product").value.trim(),
    status: document.getElementById("order-status").value,
    tracking_no: document.getElementById("order-tracking").value.trim() || null,
    logistics: document.getElementById("order-logistics").value.trim() || null,
    refund_status: document.getElementById("order-refund").value.trim() || null,
  };
  const btn = document.getElementById("order-submit-btn");
  btn.disabled = true;
  try {
    if (editKey) {
      const resp = await fetch("/api/orders/" + encodeURIComponent(editKey), {
        method: "PUT",
        headers: authHeaders(),
        body: JSON.stringify(payload),
      });
      if (!resp.ok) throw new Error(await resp.text());
      alert("✅ 订单已更新");
      resetOrderForm();
    } else {
      const orderId = document.getElementById("order-id").value.trim();
      if (!orderId) {
        alert("请填写订单号");
        btn.disabled = false;
        return;
      }
      const resp = await fetch("/api/orders", {
        method: "POST",
        headers: authHeaders(),
        body: JSON.stringify({ ...payload, order_id: orderId }),
      });
      if (!resp.ok) {
        const d = await resp.json().catch(() => ({}));
        throw new Error(d.detail || "新增失败");
      }
      alert("✅ 订单已新增");
      resetOrderForm();
    }
    loadOrders();
  } catch (err) {
    alert("操作失败：" + err.message);
  } finally {
    btn.disabled = false;
  }
});

// ---------- 退出登录 ----------
document.getElementById("logout-btn").addEventListener("click", async () => {
  try {
    await fetch("/api/logout", { method: "POST", headers: authHeaders() });
  } catch (err) {
    // 忽略登出接口错误
  }
  localStorage.removeItem("ai_ticket_token");
  localStorage.removeItem("ai_ticket_username");
  localStorage.removeItem("ai_ticket_role");
  window.location.href = "/login";
});

function escapeHtml(str) {
  return (str || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
