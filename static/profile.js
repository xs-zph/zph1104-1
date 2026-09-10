const labels = {
  "person.name": "称呼",
  "device.preferred_device": "常用设备",
  "preference.favorite_category": "偏好品类",
  "preference.usage_preference": "使用偏好",
  "after_sale.issue": "售后事项",
};
const subjectFacts = document.getElementById("subject-facts");
const orderList = document.getElementById("order-list");
const ticketList = document.getElementById("ticket-list");
const afterSaleList = document.getElementById("after-sale-list");
function headers() { return { "Content-Type": "application/json" }; }
function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[char]));
}
function statusClass(value) { return String(value || "").replace(/[^a-zA-Z0-9_-]/g, ""); }

function renderFacts(facts) {
  if (!facts.length) {
    subjectFacts.innerHTML = '<div class="archive-empty">还没有保存的主体事实。与客服交流后，系统会在后台提取并等待确认。</div>';
    return;
  }
  subjectFacts.innerHTML = facts.map((fact) => {
    const key = `${fact.entity_type}.${fact.fact_key}`;
    return `<article class="subject-fact" data-id="${fact.id}"><div class="subject-fact-content"><span>${escapeHtml(labels[key] || fact.fact_key)}</span><strong data-value>${escapeHtml(fact.fact_value)}</strong><small class="fact-status ${fact.confirmed ? "confirmed" : "pending"}">${fact.confirmed ? "已确认" : "待确认"}</small></div><div class="fact-actions"><button type="button" class="text-action edit-fact">编辑</button><button type="button" class="text-action delete-fact">删除</button></div></article>`;
  }).join("");
  subjectFacts.querySelectorAll(".edit-fact").forEach((button) => button.addEventListener("click", () => editFact(button.closest(".subject-fact"))));
  subjectFacts.querySelectorAll(".delete-fact").forEach((button) => button.addEventListener("click", () => deleteFact(button.closest(".subject-fact").dataset.id)));
}
function editFact(card) {
  if (card.querySelector(".fact-editor")) return;
  const value = card.querySelector("[data-value]");
  const form = document.createElement("form");
  form.className = "fact-editor";
  form.innerHTML = `<input name="value" maxlength="255" required value="${escapeHtml(value.textContent)}"><button type="submit" class="text-action">保存</button><button type="button" class="text-action cancel-edit">取消</button>`;
  value.replaceWith(form);
  form.elements.value.focus();
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const response = await fetch(`/api/profile/${encodeURIComponent(card.dataset.id)}`, { method: "PUT", headers: headers(), body: JSON.stringify({ fact_value: form.elements.value.value.trim(), confirmed: true }) });
    if (response.ok) loadArchive();
  });
  form.querySelector(".cancel-edit").addEventListener("click", loadArchive);
}
async function deleteFact(id) {
  const response = await fetch(`/api/profile/${encodeURIComponent(id)}`, { method: "DELETE", headers: headers() });
  if (response.ok) loadArchive();
}
function renderOrders(orders) {
  document.getElementById("order-count").textContent = orders.length;
  if (!orders.length) { orderList.innerHTML = '<div class="archive-empty">暂无订单，回到咨询页可以直接查询。</div>'; return; }
  orderList.innerHTML = orders.map((order) => `<article class="object-card"><div class="object-card-head"><strong>${escapeHtml(order.product || "未命名商品")}</strong><span class="state ${statusClass(order.status)}">${escapeHtml(order.status || "未知")}</span></div><div class="object-id">${escapeHtml(order.order_id)}</div>${order.logistics ? `<p><b>物流</b>${escapeHtml(order.logistics)}</p>` : ""}${order.refund_status ? `<p><b>退款</b>${escapeHtml(order.refund_status)}</p>` : ""}</article>`).join("");
}
function renderTickets(tickets) {
  document.getElementById("ticket-count").textContent = tickets.length;
  if (!tickets.length) { ticketList.innerHTML = '<div class="archive-empty">暂无售后工单。</div>'; return; }
  ticketList.innerHTML = tickets.map((ticket) => `<article class="object-card ticket-card"><div class="object-card-head"><strong>${escapeHtml(ticket.topic || "售后进度")}</strong><span class="state">${escapeHtml(ticket.status_label || "处理中")}</span></div><p>${escapeHtml(ticket.summary || "暂无描述")}</p>${ticket.created_at ? `<small>提交时间：${escapeHtml(String(ticket.created_at).slice(0, 16))}</small>` : ""}</article>`).join("");
}
function renderAfterSales(requests) {
  document.getElementById("after-sale-count").textContent = requests.length;
  if (!requests.length) {
    afterSaleList.innerHTML = '<div class="archive-empty">暂无售后申请。</div>';
    return;
  }
  afterSaleList.innerHTML = requests.map((request) => `<article class="object-card ticket-card">
    <div class="object-card-head">
      <strong>${escapeHtml(request.request_type_label || "售后申请")}</strong>
      <span class="state">${escapeHtml(request.status_label || "处理中")}</span>
    </div>
    <div class="object-id">申请号：${escapeHtml(request.request_no || "—")}</div>
    <p>订单：${escapeHtml(request.order_id || "—")}${request.reason ? ` · 原因：${escapeHtml(request.reason)}` : ""}</p>
    ${request.result ? `<small>${escapeHtml(request.result)}</small>` : ""}
    ${request.created_at ? `<small>提交时间：${escapeHtml(String(request.created_at).slice(0, 16))}</small>` : ""}
  </article>`).join("");
}
async function loadArchive() {
  try {
    const [response, afterSaleResponse] = await Promise.all([
      fetch("/api/entity-archive", { headers: headers() }),
      fetch("/api/after-sales", { headers: headers() }),
    ]);
    if (response.status === 401) { window.location.href = "/login"; return; }
    if (afterSaleResponse.status === 401) { window.location.href = "/login"; return; }
    if (!response.ok) throw new Error("archive request failed");
    if (!afterSaleResponse.ok) throw new Error("after-sales request failed");
    const data = await response.json();
    const afterSales = await afterSaleResponse.json();
    renderFacts(data.subject?.facts || []); renderOrders(data.objects?.orders || []);
    renderTickets(data.objects?.tickets || []); renderAfterSales(afterSales || []);
  } catch (error) {
    subjectFacts.innerHTML = '<div class="archive-empty">档案暂时无法加载，请稍后刷新。</div>';
    orderList.innerHTML = ""; ticketList.innerHTML = ""; afterSaleList.innerHTML = "";
  }
}
async function logout() { await fetch("/api/logout", { method: "POST", headers: headers() }).catch(() => {}); window.location.href = "/login"; }
document.getElementById("archive-logout-btn").addEventListener("click", logout);
loadArchive();
