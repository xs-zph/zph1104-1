// 系统管理员后台：管理账号安全、权限、知识库和 Agent 审计。
const permissionLabels = {
  "ticket.view": "查看工单", "ticket.reply": "回复工单", "ticket.claim": "接单",
  "ticket.transfer": "转派工单", "ticket.resolve": "结束工单",
};
let managedUsers = [];
let faqEntries = [];
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>\"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[char]));
async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "请求失败");
  return response.json();
}
function roleName(role) { return ({ admin: "系统管理员", manager: "经理", agent: "客服", customer: "客户" })[role] || role; }
function renderPermissionPicker(selected = []) {
  $("permission-picker").innerHTML = Object.entries(permissionLabels).map(([key, label]) => `<label><input type="checkbox" value="${key}" ${selected.includes(key) ? "checked" : ""}> ${label}</label>`).join("");
  updatePermissionVisibility();
}
function updatePermissionVisibility() {
  const enabled = $("account-role").value === "agent";
  $("permission-picker").classList.toggle("disabled", !enabled);
  $("permission-picker").querySelectorAll("input").forEach((input) => { input.disabled = !enabled; });
}
function selectedPermissions() { return [...$("permission-picker").querySelectorAll("input:checked")].map((input) => input.value); }
function resetAccountForm() {
  $("account-form").reset(); $("account-edit-key").value = ""; $("account-username").disabled = false; $("account-password").required = true;
  $("account-submit-btn").textContent = "创建账号"; $("account-cancel-btn").classList.add("hidden"); renderPermissionPicker();
}
function editAccount(user) {
  $("account-edit-key").value = user.username; $("account-username").value = user.username; $("account-username").disabled = true;
  $("account-password").value = ""; $("account-password").required = false; $("account-role").value = user.role;
  $("account-submit-btn").textContent = "保存账号设置"; $("account-cancel-btn").classList.remove("hidden"); renderPermissionPicker(user.permissions || []);
  window.scrollTo({ top: $("account-form").getBoundingClientRect().top + window.scrollY - 30, behavior: "smooth" });
}
async function loadAccounts() {
  managedUsers = await api("/api/admin/users"); $("account-count").textContent = `${managedUsers.length} 个账号`;
  $("account-list").innerHTML = managedUsers.map((user) => `<div class="account-row"><div class="account-main"><b>${escapeHtml(user.username)}</b><span class="role-badge ${user.role}">${roleName(user.role)}</span><span class="account-status ${user.active ? "active" : "inactive"}">${user.active ? "启用" : "停用"}</span></div><div class="account-permissions">${(user.permissions || []).map((p) => `<span>${escapeHtml(permissionLabels[p] || p)}</span>`).join("") || "无客服操作权限"}</div><div class="account-actions"><button class="btn btn-ghost btn-sm" data-edit="${escapeHtml(user.username)}">编辑</button><button class="btn btn-ghost btn-sm" data-reset="${escapeHtml(user.username)}">重置密码</button><button class="btn btn-ghost btn-sm" data-toggle="${escapeHtml(user.username)}">${user.active ? "停用" : "启用"}</button></div></div>`).join("") || '<div class="empty">暂无账号</div>';
  $("account-list").querySelectorAll("[data-edit]").forEach((button) => button.addEventListener("click", () => editAccount(managedUsers.find((u) => u.username === button.dataset.edit))));
  $("account-list").querySelectorAll("[data-reset]").forEach((button) => button.addEventListener("click", () => resetPassword(button.dataset.reset)));
  $("account-list").querySelectorAll("[data-toggle]").forEach((button) => button.addEventListener("click", () => toggleAccount(button.dataset.toggle)));
}
async function resetPassword(username) {
  const password = window.prompt(`请输入 ${username} 的新密码（至少 8 位，含字母和数字）`); if (!password) return;
  try { await api(`/api/admin/users/${encodeURIComponent(username)}/password`, { method: "POST", body: JSON.stringify({ password }) }); await loadAccounts(); await loadAudits(); window.alert("密码已更新"); } catch (error) { window.alert(error.message); }
}
async function toggleAccount(username) {
  const user = managedUsers.find((item) => item.username === username); if (!user) return;
  try { await api(`/api/admin/users/${encodeURIComponent(username)}`, { method: "PATCH", body: JSON.stringify({ role: user.role, active: !user.active, permissions: user.permissions || [] }) }); await loadAccounts(); await loadAudits(); } catch (error) { window.alert(error.message); }
}
async function loadAudits() {
  const audits = await api("/api/admin/account-audits");
  $("account-audits").innerHTML = audits.length ? audits.map((item) => `<div class="audit-row"><span>${escapeHtml(item.created_at || "")}</span><b>${escapeHtml(item.operator)}</b><span>${escapeHtml(item.action)}</span><span>${escapeHtml(item.target_username)} · ${escapeHtml(item.detail)}</span></div>`).join("") : '<div class="empty">暂无账号变更</div>';
}
function agentStatusName(status) { return ({ success: "完成", failed: "失败", running: "处理中" })[status] || status; }
async function loadAgentRuns() {
  const runs = await api("/api/admin/agent-runs");
  $("agent-runs").innerHTML = runs.length ? runs.map((run) => `<div class="agent-run-row"><div class="agent-run-main"><b>${escapeHtml(run.specialist || "未分派")}</b><span class="agent-run-status ${escapeHtml(run.status)}">${agentStatusName(run.status)}</span><small>${escapeHtml(run.created_at || "")}</small></div><div class="agent-run-question">${escapeHtml(run.question_summary || "无文字摘要")}</div><div class="agent-run-meta">${escapeHtml(run.task_id)} · ${run.event_count} 条事件<button class="btn btn-ghost btn-sm" data-agent-run="${escapeHtml(run.task_id)}">查看轨迹</button></div></div>`).join("") : '<div class="empty">暂无 Agent 运行记录</div>';
  $("agent-runs").querySelectorAll("[data-agent-run]").forEach((button) => button.addEventListener("click", () => showAgentRun(button.dataset.agentRun)));
}
async function showAgentRun(taskId) {
  const detail = $("agent-run-detail"); detail.classList.remove("hidden"); detail.innerHTML = '<div class="empty">加载轨迹中...</div>';
  try {
    const run = await api(`/api/admin/agent-runs/${encodeURIComponent(taskId)}`);
    detail.innerHTML = `<div class="agent-detail-head"><b>${escapeHtml(run.task_id)}</b><button class="btn btn-ghost btn-sm" id="close-agent-detail">关闭</button></div>${run.events.map((event) => `<details class="agent-event" open><summary>${escapeHtml(event.entry_type)} · ${escapeHtml(event.source_agent)} · ${escapeHtml(event.created_at || "")}</summary><pre>${escapeHtml(JSON.stringify(event.payload, null, 2))}</pre></details>`).join("")}`;
    $("close-agent-detail").addEventListener("click", () => detail.classList.add("hidden"));
  } catch (error) { detail.innerHTML = `<div class="empty error-text">${escapeHtml(error.message)}</div>`; }
}
function resetFaqForm() {
  $("faq-form").reset(); $("faq-edit-key").value = "";
  $("faq-submit-btn").textContent = "新增知识"; $("faq-cancel-btn").classList.add("hidden");
}
function editFaq(entry) {
  $("faq-edit-key").value = entry.id; $("faq-question").value = entry.question || ""; $("faq-answer").value = entry.answer || "";
  $("faq-submit-btn").textContent = "保存知识"; $("faq-cancel-btn").classList.remove("hidden");
  window.scrollTo({ top: $("faq-form").getBoundingClientRect().top + window.scrollY - 30, behavior: "smooth" });
}
async function loadFaq() {
  faqEntries = await api("/api/faq?include_disabled=true");
  const enabled = faqEntries.filter((entry) => entry.enabled).length;
  $("faq-count").textContent = `${enabled}/${faqEntries.length} 条启用`;
  $("faq-list").innerHTML = faqEntries.map((entry) => `<article class="faq-item ${entry.enabled ? "" : "disabled"}"><div class="faq-q">${escapeHtml(entry.question)}</div><div class="faq-a">${escapeHtml(entry.answer)}</div><div class="faq-item-actions"><span class="panel-tip">${entry.enabled ? "已启用" : "已停用"}</span><button class="btn btn-ghost btn-sm" data-faq-history="${entry.id}">历史</button><button class="btn btn-ghost btn-sm" data-faq-edit="${entry.id}">编辑</button><button class="btn btn-ghost btn-sm" data-faq-toggle="${entry.id}" data-enabled="${entry.enabled ? "1" : "0"}">${entry.enabled ? "停用" : "恢复"}</button></div></article>`).join("") || '<div class="empty">暂无知识条目</div>';
  $("faq-list").querySelectorAll("[data-faq-edit]").forEach((button) => button.addEventListener("click", () => editFaq(faqEntries.find((entry) => String(entry.id) === button.dataset.faqEdit))));
  $("faq-list").querySelectorAll("[data-faq-history]").forEach((button) => button.addEventListener("click", () => showFaqHistory(button.dataset.faqHistory)));
  $("faq-list").querySelectorAll("[data-faq-toggle]").forEach((button) => button.addEventListener("click", () => toggleFaq(button.dataset.faqToggle, button.dataset.enabled === "1")));
}
async function showFaqHistory(id) {
  const detail = $("faq-history-detail"); detail.classList.remove("hidden"); detail.innerHTML = '<div class="empty">加载知识变更历史...</div>';
  const actionNames = { created: "新增", updated: "编辑", disabled: "停用", restored: "恢复" };
  try {
    const rows = await api(`/api/faq/${id}/history`);
    detail.innerHTML = `<div class="agent-detail-head"><b>知识 #${escapeHtml(id)} 的变更历史</b><button class="btn btn-ghost btn-sm" id="close-faq-history">关闭</button></div>${rows.length ? rows.map((row) => `<details class="agent-event" open><summary>${escapeHtml(actionNames[row.action] || row.action)} · ${escapeHtml(row.operator)} · ${escapeHtml(row.created_at || "")}</summary><div class="faq-audit-snapshot"><b>问题：${escapeHtml(row.question)}</b><p>答案：${escapeHtml(row.answer)}</p><span>${row.enabled ? "该版本启用" : "该版本停用"}</span></div></details>`).join("") : '<div class="empty">暂无变更记录</div>'}`;
    $("close-faq-history").addEventListener("click", () => detail.classList.add("hidden"));
  } catch (error) { detail.innerHTML = `<div class="empty error-text">${escapeHtml(error.message)}</div>`; }
}
async function toggleFaq(id, enabled) {
  try {
    await api(enabled ? `/api/faq/${id}` : `/api/faq/${id}/restore`, { method: enabled ? "DELETE" : "POST" });
    await loadFaq();
  } catch (error) { window.alert(error.message); }
}
$("account-role").addEventListener("change", updatePermissionVisibility);
$("account-cancel-btn").addEventListener("click", resetAccountForm);
$("faq-cancel-btn").addEventListener("click", resetFaqForm);
$("refresh-accounts-btn").addEventListener("click", () => Promise.all([loadAccounts(), loadAudits()]));
$("refresh-audits-btn").addEventListener("click", loadAudits);
$("refresh-agent-runs-btn").addEventListener("click", loadAgentRuns);
$("refresh-faq-btn").addEventListener("click", loadFaq);
$("account-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const key = $("account-edit-key").value; const role = $("account-role").value; const permissions = role === "agent" ? selectedPermissions() : [];
  try {
    if (key) await api(`/api/admin/users/${encodeURIComponent(key)}`, { method: "PATCH", body: JSON.stringify({ role, active: true, permissions }) });
    else await api("/api/admin/users", { method: "POST", body: JSON.stringify({ username: $("account-username").value.trim(), password: $("account-password").value, role, permissions }) });
    resetAccountForm(); await loadAccounts(); await loadAudits();
  } catch (error) { window.alert(error.message); }
});
$("faq-form").addEventListener("submit", async (event) => {
  event.preventDefault(); const key = $("faq-edit-key").value;
  const body = { question: $("faq-question").value.trim(), answer: $("faq-answer").value.trim() };
  try {
    await api(key ? `/api/faq/${key}` : "/api/faq", { method: key ? "PUT" : "POST", body: JSON.stringify(body) });
    resetFaqForm(); await loadFaq();
  } catch (error) { window.alert(error.message); }
});
$("logout-btn").addEventListener("click", async () => { await fetch("/api/logout", { method: "POST" }); window.location.href = "/login"; });
window.addEventListener("DOMContentLoaded", async () => {
  try { const me = await api("/api/me"); if (me.role !== "admin") { window.location.href = me.role === "manager" ? "/manager" : me.role === "agent" ? "/staff" : "/"; return; } $("admin-username").textContent = `管理员：${me.username}`; renderPermissionPicker(); await Promise.all([loadAccounts(), loadAudits(), loadAgentRuns(), loadFaq()]); } catch (_) { window.location.href = "/login"; }
});
