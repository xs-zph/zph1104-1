// 经理运营仪表盘：只读取后端统计数据。
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>\"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[char]));
async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "请求失败");
  return response.json();
}
function renderWorkload(days) {
  const max = Math.max(1, ...days.map((item) => item.total || 0));
  $("daily-workload").innerHTML = days.length ? days.map((item) => `<div class="bar-day"><div class="bar-group"><i class="bar total" style="height:${Math.max(3, (item.total || 0) / max * 100)}%" title="全部 ${item.total || 0}"></i><i class="bar handoff" style="height:${Math.max(3, (item.handoff || 0) / max * 100)}%" title="转人工 ${item.handoff || 0}"></i><i class="bar resolved" style="height:${Math.max(3, (item.resolved || 0) / max * 100)}%" title="已解决 ${item.resolved || 0}"></i></div><span>${escapeHtml((item.date || "").slice(5))}</span></div>`).join("") : '<div class="empty">暂无趋势数据</div>';
}
function renderAgentPerformance(agents) {
  $("agent-performance").innerHTML = agents.length ? agents.map((item) => { const rate = item.handled ? Math.round(item.resolved / item.handled * 100) : 0; return `<div class="agent-row"><div class="agent-row-top"><b>${escapeHtml(item.agent)}</b><span>${item.resolved}/${item.handled} 已解决</span></div><div class="agent-progress"><i style="width:${rate}%"></i></div><small>${item.open} 条处理中 · 解决率 ${rate}%</small></div>`; }).join("") : '<div class="empty">暂无人工处理数据</div>';
}
async function loadDashboard() {
  const data = await api("/api/metrics");
  $("m-total").textContent = data.total ?? "-"; $("m-auto-rate").textContent = data.auto_rate == null ? "-" : `${Math.round(data.auto_rate * 100)}%`;
  $("m-human-total").textContent = data.human_total ?? "-"; $("m-human-rate").textContent = data.human_resolution_rate == null ? "-" : `${Math.round(data.human_resolution_rate * 100)}%`;
  $("m-sla").textContent = data.sla_breached ?? "-"; $("m-latency").textContent = data.avg_latency_ms == null ? "-" : `${data.avg_latency_ms} ms`;
  renderWorkload(data.daily_workload || []); renderAgentPerformance(data.agent_performance || []);
}
async function loadStats() {
  const data = await api("/api/stats");
  const questions = (data.top_questions || []).slice(0, 5).map((item) => `<span>${escapeHtml(item.question)} (${item.count})</span>`).join("") || "暂无高频问题";
  const tags = Object.entries(data.feedback_tags || {}).map(([key, value]) => `<span>${escapeHtml(key)} ${value}</span>`).join("") || "暂无反馈标签";
  $("manager-stats").innerHTML = `<div class="stat-block"><b>高频问题</b><div class="stat-tags">${questions}</div></div><div class="stat-block"><b>反馈标签</b><div class="stat-tags">${tags}</div></div>`;
}
$("refresh-dashboard-btn").addEventListener("click", () => Promise.all([loadDashboard(), loadStats()]));
$("logout-btn").addEventListener("click", async () => { await fetch("/api/logout", { method: "POST" }); window.location.href = "/login"; });
window.addEventListener("DOMContentLoaded", async () => {
  try { const me = await api("/api/me"); if (me.role !== "manager") { window.location.href = me.role === "admin" ? "/admin" : me.role === "agent" ? "/staff" : "/"; return; } $("manager-username").textContent = `经理：${me.username}`; await Promise.all([loadDashboard(), loadStats()]); } catch (_) { window.location.href = "/login"; }
});
