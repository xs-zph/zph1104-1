// 客服工作台：只处理转人工会话。
let currentUser = "客服";
let permissions = new Set();
let agents = [];
let tickets = [];
const BOARD_COLUMNS = [
  { key: "escalated", label: "待接单", hint: "等待坐席接手" },
  { key: "in_progress", label: "处理中", hint: "正在跟进客户" },
  { key: "waiting_customer", label: "等待客户", hint: "等待客户补充" },
  { key: "done", label: "已完成", hint: "已解决或已关闭" },
];
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "").replace(/[&<>\"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[char]));
async function api(path, options = {}) {
  const response = await fetch(path, { headers: { "Content-Type": "application/json", ...(options.headers || {}) }, ...options });
  if (!response.ok) throw new Error((await response.json().catch(() => ({}))).detail || "请求失败");
  return response.json();
}
function renderThread(ticket) {
  const labels = { customer: "客户", assistant: "AI", agent: "人工客服", system: "系统" };
  if (!ticket.messages?.length) return `<div class="escalation-text">${escapeHtml(ticket.ticket_text || "")}</div><div class="escalation-meta">AI 回复：${escapeHtml(ticket.reply || "—")}</div>`;
  return `<div class="ticket-thread">${ticket.messages.map((message) => `<div class="ticket-message ${message.sender_type}"><div class="ticket-message-head">${labels[message.sender_type] || "系统"}${message.sender_username ? ` · ${escapeHtml(message.sender_username)}` : ""}</div><div class="ticket-message-content">${escapeHtml(message.content)}</div></div>`).join("")}</div>`;
}
function renderTicket(ticket) {
  const active = ["escalated", "in_progress", "waiting_customer"].includes(ticket.status);
  const status = { escalated: "待接单", in_progress: "处理中", waiting_customer: "等待客户", resolved: "已解决", closed: "已关闭" }[ticket.status] || ticket.status;
  const overdue = active && (ticket.sla_breached === true || ticket.sla_breached === 1 || (ticket.sla_due_at && new Date(String(ticket.sla_due_at).replace(" ", "T")) < new Date()));
  const mine = !ticket.assigned_to || ticket.assigned_to === currentUser;
  const claim = !ticket.assigned_to && permissions.has("ticket.claim") ? `<button class="btn btn-light btn-sm" data-action="claim" data-id="${ticket.id}">接单</button>` : "";
  const workflow = ticket.status === "in_progress" && permissions.has("ticket.reply") ? `<button class="btn btn-ghost btn-sm" data-action="waiting" data-id="${ticket.id}">等待客户</button>` : ticket.status === "waiting_customer" && permissions.has("ticket.reply") ? `<button class="btn btn-ghost btn-sm" data-action="continue" data-id="${ticket.id}">继续处理</button>` : "";
  const transfer = active && permissions.has("ticket.transfer") && agents.length ? `<div class="transfer-actions"><select class="agent-select" data-agent-select="${ticket.id}">${agents.map((agent) => `<option value="${escapeHtml(agent.username)}" ${agent.username === ticket.assigned_to ? "selected" : ""}>${escapeHtml(agent.username)}</option>`).join("")}</select><button class="btn btn-ghost btn-sm" data-action="transfer" data-id="${ticket.id}">转派</button></div>` : "";
  const answer = active && mine && permissions.has("ticket.reply") ? `<div class="human-answer-box"><textarea class="human-answer" data-answer="${ticket.id}" placeholder="请输入人工回复…"></textarea><label class="kb-check"><input type="checkbox" data-savekb="${ticket.id}" checked> 将回答存入知识库</label><div class="escalation-actions">${permissions.has("ticket.reply") ? `<button class="btn btn-ghost btn-sm" data-action="reply" data-id="${ticket.id}">发送并继续</button>` : ""}${permissions.has("ticket.resolve") ? `<button class="btn btn-primary" data-action="resolve" data-id="${ticket.id}">回复并结束</button>` : ""}</div></div>` : ticket.assigned_to && !mine ? `<div class="assignment-notice">该工单正在由「${escapeHtml(ticket.assigned_to)}」处理</div>` : "";
  return `<article class="escalation-card ${active ? "" : "resolved"} ${overdue ? "overdue" : ""}"><div class="escalation-top"><span class="tag ${ticket.status}">${status}</span>${overdue ? '<span class="tag overdue">SLA 已超时</span>' : ""}<span class="tag">${escapeHtml(ticket.category || "未知")}</span></div>${renderThread(ticket)}<div class="workflow-actions">${claim}${workflow}</div>${answer}${transfer}<div class="escalation-meta"><span>处理人：${escapeHtml(ticket.assigned_to || "未分配")}</span><span>SLA：${escapeHtml(ticket.sla_due_at || "未设置")}</span><span>#${ticket.id}</span></div></article>`;
}

function ticketIsActive(ticket) { return ["escalated", "in_progress", "waiting_customer"].includes(ticket.status); }
function ticketIsOverdue(ticket) {
  return ticketIsActive(ticket) && (ticket.sla_breached === true || ticket.sla_breached === 1 || (ticket.sla_due_at && new Date(String(ticket.sla_due_at).replace(" ", "T")) < new Date()));
}
function ticketColumn(ticket) { return ticketIsActive(ticket) ? ticket.status : "done"; }
function ticketSearchText(ticket) {
  return [ticket.id, ticket.username, ticket.ticket_text, ticket.category, ticket.assigned_to, ...(ticket.messages || []).map((message) => message.content)].join(" ").toLowerCase();
}
function filteredTickets() {
  const search = $("staff-search").value.trim().toLowerCase();
  const priority = $("staff-priority-filter").value;
  const sla = $("staff-sla-filter").value;
  const assignee = $("staff-assignee-filter").value;
  const activeOnly = $("staff-active-only").checked;
  return tickets.filter((ticket) => {
    if (activeOnly && !ticketIsActive(ticket)) return false;
    if (search && !ticketSearchText(ticket).includes(search)) return false;
    if (priority && (ticket.priority || "normal") !== priority) return false;
    if (sla === "overdue" && !ticketIsOverdue(ticket)) return false;
    if (sla === "active" && ticketIsOverdue(ticket)) return false;
    if (assignee === "unassigned" && ticket.assigned_to) return false;
    if (assignee && assignee !== "unassigned" && ticket.assigned_to !== assignee) return false;
    return true;
  });
}
function renderAssigneeFilter() {
  const select = $("staff-assignee-filter");
  const selected = select.value;
  const names = [...new Set([...agents.map((agent) => agent.username), ...tickets.map((ticket) => ticket.assigned_to).filter(Boolean)])].sort();
  select.innerHTML = '<option value="">全部处理人</option><option value="unassigned">未分配</option>' + names.map((name) => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join("");
  if (["", "unassigned", ...names].includes(selected)) select.value = selected;
}
function renderBoard() {
  const visible = filteredTickets();
  const counts = BOARD_COLUMNS.reduce((result, column) => ({ ...result, [column.key]: visible.filter((ticket) => ticketColumn(ticket) === column.key).length }), {});
  $("escalation-board").innerHTML = BOARD_COLUMNS.map((column) => {
    const columnTickets = visible.filter((ticket) => ticketColumn(ticket) === column.key);
    return `<section class="board-column board-${column.key}"><div class="board-column-head"><div><h3>${column.label}</h3><span>${column.hint}</span></div><b>${counts[column.key]}</b></div><div class="board-column-list">${columnTickets.length ? columnTickets.map(renderTicket).join("") : '<div class="board-empty">暂无工单</div>'}</div></section>`;
  }).join("");
  bindActions();
  const active = tickets.filter(ticketIsActive).length;
  $("escalation-count").textContent = `${active} 条未结束 · ${visible.length} 条显示中`;
}
async function refreshTickets() {
  try { tickets = await api("/api/escalations"); renderAssigneeFilter(); renderBoard(); $("board-updated").textContent = `刚刚更新 · ${tickets.length} 条工单`; } catch (error) { $("escalation-board").innerHTML = `<div class="empty">加载失败：${escapeHtml(error.message)}</div>`; }
}
async function loadAgents() { if (!permissions.has("ticket.transfer")) return; try { agents = await api("/api/agents"); } catch (_) { agents = []; } }
function bindActions() { $("escalation-board").querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => handleAction(button.dataset.action, button.dataset.id))); }
async function handleAction(action, id) {
  const ticket = tickets.find((item) => String(item.id) === String(id)); if (!ticket) return;
  try {
    if (action === "claim") await api(`/api/escalations/${id}/claim`, { method: "POST" });
    if (action === "waiting" || action === "continue") await api(`/api/escalations/${id}/status`, { method: "PATCH", body: JSON.stringify({ status: action === "waiting" ? "waiting_customer" : "in_progress" }) });
    if (action === "transfer") { const select = document.querySelector(`[data-agent-select="${id}"]`); await api(`/api/escalations/${id}/assign`, { method: "POST", body: JSON.stringify({ assigned_to: select.value }) }); }
    if (action === "reply" || action === "resolve") { const text = document.querySelector(`[data-answer="${id}"]`).value.trim(); if (!text) throw new Error("人工回复不能为空"); const save = document.querySelector(`[data-savekb="${id}"]`).checked; await api(`/api/escalations/${id}/${action === "reply" ? "reply" : "resolve"}`, { method: "POST", body: JSON.stringify({ human_answer: text, save_to_kb: save }) }); }
    await refreshTickets();
  } catch (error) { window.alert(error.message); }
}
let realtimeSource = null;
let realtimeRetryTimer = null;
let realtimePollTimer = null;
let realtimeRetryDelay = 1000;
let realtimeCursor = "";
const REALTIME_EVENTS = ["ticket_escalated", "customer_message", "ticket_updated", "human_replied"];

function startRealtimePolling() {
  if (realtimePollTimer === null) realtimePollTimer = window.setInterval(refreshTickets, 3000);
}

function stopRealtimePolling() {
  if (realtimePollTimer !== null) {
    window.clearInterval(realtimePollTimer);
    realtimePollTimer = null;
  }
}

function scheduleRealtimeReconnect() {
  if (realtimeRetryTimer !== null) return;
  const delay = realtimeRetryDelay;
  realtimeRetryDelay = Math.min(realtimeRetryDelay * 2, 30000);
  realtimeRetryTimer = window.setTimeout(() => {
    realtimeRetryTimer = null;
    connectRealtime();
  }, delay);
}

function connectRealtime() {
  if (typeof EventSource === "undefined" || realtimeSource !== null) {
    startRealtimePolling();
    return;
  }
  const query = realtimeCursor ? `?since=${encodeURIComponent(realtimeCursor)}` : "";
  const source = new EventSource("/api/events" + query);
  realtimeSource = source;
  source.onopen = () => {
    realtimeRetryDelay = 1000;
    stopRealtimePolling();
  };
  REALTIME_EVENTS.forEach((eventType) => source.addEventListener(eventType, (event) => {
    realtimeCursor = event.lastEventId || realtimeCursor;
    refreshTickets();
  }));
  source.onerror = () => {
    if (realtimeSource !== source) return;
    source.close();
    realtimeSource = null;
    startRealtimePolling();
    scheduleRealtimeReconnect();
  };
}

function startRealtime() {
  if (typeof EventSource === "undefined") return startRealtimePolling();
  connectRealtime();
}

window.addEventListener("beforeunload", () => {
  realtimeSource?.close();
  if (realtimeRetryTimer !== null) window.clearTimeout(realtimeRetryTimer);
  stopRealtimePolling();
});
$("refresh-btn").addEventListener("click", refreshTickets);
$("staff-search").addEventListener("input", renderBoard);
$("staff-priority-filter").addEventListener("change", renderBoard);
$("staff-sla-filter").addEventListener("change", renderBoard);
$("staff-assignee-filter").addEventListener("change", renderBoard);
$("staff-active-only").addEventListener("change", renderBoard);
$("logout-btn").addEventListener("click", async () => { await fetch("/api/logout", { method: "POST" }); window.location.href = "/login"; });
window.addEventListener("DOMContentLoaded", async () => { try { const me = await api("/api/me"); if (me.role !== "agent") { window.location.href = me.role === "admin" ? "/admin" : "/"; return; } currentUser = me.username; permissions = new Set(me.permissions || []); $("staff-username").textContent = `客服：${currentUser}`; await loadAgents(); await refreshTickets(); startRealtime(); } catch (_) { window.location.href = "/login"; } });
