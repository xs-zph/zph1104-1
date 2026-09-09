// 经理运营仪表盘：只读取后端统计数据，图表使用原生 SVG/CSS 渲染。
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>\"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[char]));
let dashboardData = null;
async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "请求失败");
  return response.json();
}
function renderTrend(days) {
  const target = $("daily-trend");
  if (!days.length) { target.innerHTML = '<div class="empty">暂无趋势数据</div>'; return; }
  const width = 720; const height = 250; const pad = { top: 22, right: 22, bottom: 38, left: 34 };
  const max = Math.max(1, ...days.flatMap((item) => [item.total || 0, item.handoff || 0, item.resolved || 0]));
  const x = (index) => pad.left + (index * (width - pad.left - pad.right) / Math.max(1, days.length - 1));
  const y = (value) => height - pad.bottom - ((value || 0) / max) * (height - pad.top - pad.bottom);
  const points = (key) => days.map((item, index) => `${x(index).toFixed(1)},${y(item[key]).toFixed(1)}`).join(" ");
  const grid = [0, .5, 1].map((ratio) => { const gy = y(max * ratio); return `<line x1="${pad.left}" y1="${gy}" x2="${width - pad.right}" y2="${gy}" class="trend-grid"/><text x="${pad.left - 10}" y="${gy + 4}" text-anchor="end" class="trend-axis">${Math.round(max * ratio)}</text>`; }).join("");
  const labels = days.map((item, index) => `<text x="${x(index)}" y="${height - 10}" text-anchor="middle" class="trend-axis">${escapeHtml((item.date || "").slice(5))}</text>`).join("");
  const line = (key, color) => `<polyline points="${points(key)}" class="trend-line ${key}" stroke="${color}"/><polyline points="${points(key)}" class="trend-line-shadow ${key}" stroke="${color}"/>${days.map((item, index) => `<circle cx="${x(index)}" cy="${y(item[key])}" r="4.5" class="trend-point ${key}" fill="${color}"><title>${escapeHtml((item.date || "").slice(5))} ${key === "total" ? "全部" : key === "handoff" ? "转人工" : "已解决"} ${item[key] || 0}</title></circle>`).join("")}`;
  target.innerHTML = `<svg class="trend-svg" viewBox="0 0 ${width} ${height}" preserveAspectRatio="none" role="img" aria-label="近七日全部、转人工和已解决工单趋势"><defs><filter id="line-shadow"><feGaussianBlur stdDeviation="4"/></filter></defs>${grid}${line("total", "#67e8c3")}${line("handoff", "#f5b84b")}${line("resolved", "#82aaff")}${labels}</svg>`;
}
function renderAgentPerformance(agents) {
  const filter = ($("dashboard-filter").value || "").trim().toLowerCase();
  const filtered = agents.filter((item) => !filter || String(item.agent).toLowerCase().includes(filter));
  $("agent-performance").innerHTML = filtered.length ? filtered.map((item) => { const rate = item.handled ? Math.round(item.resolved / item.handled * 100) : 0; return `<div class="agent-row"><div class="agent-row-top"><b>${escapeHtml(item.agent)}</b><span>${item.resolved}/${item.handled} 已解决</span></div><div class="agent-progress"><i style="width:${rate}%"></i></div><small>${item.open} 条处理中 · 解决率 ${rate}%</small></div>`; }).join("") : '<div class="empty">没有匹配的坐席</div>';
}
function renderRoutingMix(data) {
  const total = Math.max(0, data.total || 0); const auto = Math.max(0, data.auto_count || 0); const human = Math.max(0, data.human_total ?? data.escalated_count ?? 0); const safeTotal = Math.max(1, auto + human, total);
  const autoRatio = Math.min(1, auto / safeTotal); const circumference = 2 * Math.PI * 78; const autoLength = circumference * autoRatio;
  $("routing-donut").innerHTML = `<circle cx="110" cy="110" r="78" class="donut-track"/><circle cx="110" cy="110" r="78" class="donut-ring donut-ring-human" stroke-dasharray="${circumference} ${circumference}" stroke-dashoffset="0"/><circle cx="110" cy="110" r="78" class="donut-ring donut-ring-auto" stroke-dasharray="${autoLength} ${circumference - autoLength}" stroke-dashoffset="0" transform="rotate(-90 110 110)"/>`;
  $("donut-rate").textContent = data.auto_rate == null ? "-" : `${Math.round(data.auto_rate * 100)}%`;
  $("mix-legend").innerHTML = `<div class="mix-legend-row"><i class="legend-swatch auto"></i><span>AI 自动处理</span><b>${auto}</b><em>${Math.round(autoRatio * 100)}%</em></div><div class="mix-legend-row"><i class="legend-swatch human"></i><span>转人工处理</span><b>${human}</b><em>${Math.round((human / safeTotal) * 100)}%</em></div><div class="mix-total">共计 ${total} 张工单</div>`;
}
function renderCategories(distribution) {
  const filter = ($("dashboard-filter").value || "").trim().toLowerCase();
  const entries = Object.entries(distribution || {}).filter(([name]) => !filter || name.toLowerCase().includes(filter));
  const max = Math.max(1, ...entries.map(([, count]) => count || 0));
  $("category-distribution").innerHTML = entries.length ? entries.map(([name, count], index) => `<div class="category-row"><div class="category-label"><span class="category-rank">${String(index + 1).padStart(2, "0")}</span><span>${escapeHtml(name)}</span><b>${count}</b></div><div class="category-track"><i style="width:${Math.max(4, (count || 0) / max * 100)}%"></i></div></div>`).join("") : '<div class="empty">没有匹配的分类</div>';
}
function percent(value) { return value == null ? "-" : `${Math.round(value * 100)}%`; }
function renderRagQuality(data) { const quality = data.rag_quality || {}; const cache = quality.cache || {}; const cacheRate = cache.hit_rate == null ? 0 : Math.max(0, Math.min(1, cache.hit_rate)); $("rag-recall").textContent = percent(quality.recall_at_5); $("rag-precision").textContent = percent(quality.precision_at_3); $("rag-hit-rate").textContent = percent(quality.hit_rate); $("rag-guard-rate").textContent = percent(quality.guard_pass_rate); $("rag-llm-saved").textContent = cache.llm_saved ?? "0"; $("rag-cache-rate").textContent = percent(cache.hit_rate); $("rag-memory-hit").textContent = cache.memory_hit ?? 0; $("rag-redis-hit").textContent = cache.redis_hit ?? 0; $("rag-eval-count").textContent = `${quality.case_count || 0} 条评测样本`; const orbit = document.querySelector(".cache-orbit"); if (orbit) orbit.style.setProperty("--cache-rate", `${cacheRate * 100}%`); }
function renderVisuals(data) { renderRoutingMix(data); renderTrend(data.daily_workload || []); renderCategories(data.category_distribution || {}); renderAgentPerformance(data.agent_performance || []); renderRagQuality(data); }
async function loadDashboard() {
  const data = await api("/api/metrics");
  dashboardData = data;
  $("m-total").textContent = data.total ?? "-"; $("m-auto-rate").textContent = data.auto_rate == null ? "-" : `${Math.round(data.auto_rate * 100)}%`;
  $("m-human-total").textContent = data.human_total ?? "-"; $("m-human-rate").textContent = data.human_resolution_rate == null ? "-" : `${Math.round(data.human_resolution_rate * 100)}%`;
  $("m-sla").textContent = data.sla_breached ?? "-"; $("m-latency").textContent = data.avg_latency_ms == null ? "-" : `${data.avg_latency_ms} ms`;
  renderVisuals(data);
}
async function loadStats() {
  const data = await api("/api/stats");
  const questions = (data.top_questions || []).slice(0, 5).map((item) => `<span>${escapeHtml(item.question)} (${item.count})</span>`).join("") || "暂无高频问题";
  const tags = Object.entries(data.feedback_tags || {}).map(([key, value]) => `<span>${escapeHtml(key)} ${value}</span>`).join("") || "暂无反馈标签";
  $("manager-stats").innerHTML = `<div class="stat-block"><b>高频问题</b><div class="stat-tags">${questions}</div></div><div class="stat-block"><b>反馈标签</b><div class="stat-tags">${tags}</div></div>`;
}
$("refresh-dashboard-btn").addEventListener("click", () => Promise.all([loadDashboard(), loadStats()]));
$("dashboard-filter").addEventListener("input", () => { if (dashboardData) { renderCategories(dashboardData.category_distribution || {}); renderAgentPerformance(dashboardData.agent_performance || []); } });
$("logout-btn").addEventListener("click", async () => { await fetch("/api/logout", { method: "POST" }); window.location.href = "/login"; });
window.addEventListener("DOMContentLoaded", async () => {
  try { const me = await api("/api/me"); if (me.role !== "manager") { window.location.href = me.role === "admin" ? "/admin" : me.role === "agent" ? "/staff" : "/"; return; } $("manager-username").textContent = `经理：${me.username}`; await Promise.all([loadDashboard(), loadStats()]); } catch (_) { window.location.href = "/login"; }
});
