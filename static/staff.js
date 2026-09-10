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
const ANNOTATION_TYPES = [
  ["return", "退货申请"],
  ["refund", "退款申请"],
  ["exchange", "换货申请"],
  ["repair", "售后维修"],
  ["cancel", "取消订单"],
];
const ANNOTATION_STAGES = [
  ["pending_info", "待补充信息"],
  ["pending_order_check", "待核对订单"],
  ["pending_customer_confirmation", "待用户确认"],
  ["processing", "处理中"],
  ["closed", "已关闭"],
];
function optionList(items, selected) {
  return items.map(([value, label]) => `<option value="${value}" ${value === selected ? "selected" : ""}>${label}</option>`).join("");
}
function renderAfterSaleAnnotation(ticket, active, mine) {
  const annotation = ticket.after_sale_annotation || {};
  const order = ticket.order_context || {};
  const request = ticket.after_sale_request || {};
  const editable = active && mine && permissions.has("ticket.reply") && !["submitted", "completed"].includes(annotation.stage);
  const stageField = annotation.stage === "submitted"
    ? `<input type="hidden" data-annotation-field="stage" value="submitted" /><div class="annotation-readonly-stage">已提交售后</div>`
    : annotation.stage === "completed"
      ? `<input type="hidden" data-annotation-field="stage" value="completed" /><div class="annotation-readonly-stage">已完成</div>`
    : `<select data-annotation-field="stage">${optionList(ANNOTATION_STAGES, annotation.stage || "pending_info")}</select>`;
  const summary = annotation.request_type_label
    ? `<div class="annotation-summary"><span class="tag after-sale">${escapeHtml(annotation.request_type_label)}</span><span class="annotation-stage">${escapeHtml(annotation.stage_label || "处理中")}</span>${annotation.order_id ? `<span class="annotation-order">订单 ${escapeHtml(annotation.order_id)}</span>` : ""}</div>`
    : `<div class="annotation-empty">还没有售后标注，先记录用户需要办理的业务类型。</div>`;
  const submitted = request.request_no
    ? `<div class="after-sale-submitted"><span>${escapeHtml(request.request_type_label || annotation.request_type_label || "售后申请")}${request.status === "completed" ? "已完成" : request.status === "failed" ? "提交失败" : "已提交"}</span><b>${escapeHtml(request.request_no)}</b><small>${escapeHtml(request.status_label || "处理中")}</small>${request.last_attempt_error ? `<em class="after-sale-failure">${escapeHtml(request.last_attempt_error)}</em>` : ""}<button class="btn btn-ghost btn-xs" data-action="after-sale-audit" data-id="${ticket.id}">查看处理记录</button>${request.status === "failed" && active && mine && permissions.has("ticket.reply") ? `<button class="btn btn-primary btn-xs" data-action="retry-after-sale" data-id="${ticket.id}" data-request-no="${escapeHtml(request.request_no)}">重新提交</button>` : ""}</div>`
    : "";
  if (!editable) {
    return `<section class="after-sale-annotation"><div class="annotation-head"><b>售后标注</b>${annotation.operator ? `<small>最近由 ${escapeHtml(annotation.operator)} 更新</small>` : ""}</div>${summary}${submitted}${annotation.reason || annotation.note ? `<div class="annotation-detail">${annotation.reason ? `原因：${escapeHtml(annotation.reason)}` : ""}${annotation.note ? `备注：${escapeHtml(annotation.note)}` : ""}</div>` : ""}</section>`;
  }
  const submitConfig = {
    cancel: { label: "取消订单", endpoint: "cancel-request", statuses: ["待发货", "已取消"], requiresReason: false },
    return: { label: "退货申请", endpoint: "return-request", statuses: ["已签收", "已完成"] },
    refund: { label: "退款申请", endpoint: "refund-request", statuses: ["待发货", "已发货", "运输中", "已签收", "已完成"] },
    repair: { label: "维修申请", endpoint: "repair-request", statuses: ["已发货", "运输中", "已签收", "已完成"] },
  }[annotation.request_type];
  const canSubmitAfterSale = submitConfig
    && order.order_id
    && (submitConfig.requiresReason === false || annotation.reason)
    && submitConfig.statuses.includes(order.status)
    && !(annotation.request_type === "refund" && order.refund_status);
  const retryButton = request.status === "failed" && request.request_no
    ? `<button class="btn btn-primary btn-sm" data-action="retry-after-sale" data-request-no="${escapeHtml(request.request_no)}" data-id="${ticket.id}">重新提交</button>`
    : "";
  const submitButton = canSubmitAfterSale && request.status !== "failed"
    ? `<button class="btn btn-primary btn-sm" data-action="submit-after-sale" data-request-type="${annotation.request_type}" data-id="${ticket.id}">提交${submitConfig.label}</button>`
    : "";
  return `<section class="after-sale-annotation"><div class="annotation-head"><b>售后标注</b><small>先核验订单，商品由订单自动匹配</small></div>${summary}${request.status === "failed" && request.last_attempt_error ? `<div class="annotation-error">上次提交失败：${escapeHtml(request.last_attempt_error)}</div>` : ""}<div class="annotation-form" data-annotation-form="${ticket.id}"><label><span>售后类型</span><select data-annotation-field="request_type">${optionList(ANNOTATION_TYPES, annotation.request_type || "return")}</select></label><label><span>处理阶段</span>${stageField}</label><label class="annotation-order-field"><span>订单号</span><div class="annotation-order-input"><input data-annotation-field="order_id" value="${escapeHtml(annotation.order_id || "")}" placeholder="输入客户订单号" maxlength="64" /><button class="btn btn-ghost btn-sm" type="button" data-action="verify-order" data-id="${ticket.id}">核验订单</button></div><small class="annotation-order-status ${order.order_id ? "verified" : ""}" data-order-status="${ticket.id}">${order.order_id ? `已匹配：${escapeHtml(order.product || "商品")} · ${escapeHtml(order.status_label || "未知状态")}` : "尚未核验"}</small></label><label><span>商品</span><input data-annotation-field="product" data-verified-order-id="${escapeHtml(order.order_id || "")}" value="${escapeHtml(order.product || annotation.product || "")}" placeholder="核验订单后自动填充" maxlength="255" readonly /></label><label><span>售后原因</span><input data-annotation-field="reason" value="${escapeHtml(annotation.reason || "")}" placeholder="例如：商品不合适、质量问题" maxlength="255" /></label><label><span>商品状态</span><select data-annotation-field="item_status"><option value="">未记录</option>${optionList([["未发货", "未发货"], ["运输中", "运输中"], ["已签收", "已签收"], ["已使用", "已使用"], ["存在质量问题", "存在质量问题"]], annotation.item_status || "")}</select></label><label class="annotation-note"><span>客服备注</span><textarea data-annotation-field="note" placeholder="补充核对结果或下一步动作" maxlength="2000">${escapeHtml(annotation.note || "")}</textarea></label><div class="annotation-actions"><button class="btn btn-light btn-sm" data-action="save-annotation" data-id="${ticket.id}">保存标注</button>${submitButton}${retryButton}</div></div></section>`;
}
function renderOrderContext(ticket) {
  const order = ticket.order_context;
  if (!order) return "";
  const refund = order.refund_status
    ? `<div class="order-context-alert">退款记录：${escapeHtml(order.refund_status)}${order.refund_hint ? `。${escapeHtml(order.refund_hint)}` : ""}</div>`
    : "";
  return `<section class="order-context"><div class="order-context-head"><b>订单核验</b><span>${escapeHtml(order.order_id || "")}</span></div><div class="order-context-grid"><div><small>商品</small><strong>${escapeHtml(order.product || "未记录")}</strong></div><div><small>订单状态</small><strong>${escapeHtml(order.status_label || "未知")}</strong></div><div><small>物流</small><strong>${escapeHtml(order.logistics || "暂无物流信息")}</strong></div><div><small>运单号</small><strong>${escapeHtml(order.tracking_no || "暂无")}</strong></div></div>${order.handling_hint ? `<div class="order-context-hint">${escapeHtml(order.handling_hint)}</div>` : ""}${refund}</section>`;
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
  return `<article class="escalation-card ${active ? "" : "resolved"} ${overdue ? "overdue" : ""}"><div class="escalation-top"><span class="tag ${ticket.status}">${status}</span>${overdue ? '<span class="tag overdue">SLA 已超时</span>' : ""}<span class="tag">${escapeHtml(ticket.category || "未知")}</span></div>${renderThread(ticket)}${renderAfterSaleAnnotation(ticket, active, mine)}${renderOrderContext(ticket)}<div class="workflow-actions">${claim}${workflow}</div>${answer}${transfer}<div class="escalation-meta"><span>处理人：${escapeHtml(ticket.assigned_to || "未分配")}</span><span>SLA：${escapeHtml(ticket.sla_due_at || "未设置")}</span><span>#${ticket.id}</span></div></article>`;
}

function ticketIsActive(ticket) { return ["escalated", "in_progress", "waiting_customer"].includes(ticket.status); }
function ticketIsOverdue(ticket) {
  return ticketIsActive(ticket) && (ticket.sla_breached === true || ticket.sla_breached === 1 || (ticket.sla_due_at && new Date(String(ticket.sla_due_at).replace(" ", "T")) < new Date()));
}
function ticketColumn(ticket) { return ticketIsActive(ticket) ? ticket.status : "done"; }
function ticketSearchText(ticket) {
  const annotation = ticket.after_sale_annotation || {};
  const order = ticket.order_context || {};
  return [ticket.id, ticket.username, ticket.ticket_text, ticket.category, ticket.assigned_to, annotation.request_type_label, annotation.stage_label, annotation.order_id, annotation.product, annotation.reason, annotation.note, order.order_id, order.product, order.status_label, order.logistics, order.tracking_no, order.refund_status, ...(ticket.messages || []).map((message) => message.content)].join(" ").toLowerCase();
}
function afterSaleFilterValue(ticket) {
  const annotation = ticket.after_sale_annotation || {};
  const request = ticket.after_sale_request || {};
  return {
    type: annotation.request_type || request.request_type || "",
    status: request.status || "",
  };
}
function filteredTickets() {
  const search = $("staff-search").value.trim().toLowerCase();
  const priority = $("staff-priority-filter").value;
  const sla = $("staff-sla-filter").value;
  const assignee = $("staff-assignee-filter").value;
  const afterSaleType = $("staff-after-sale-type").value;
  const afterSaleStatus = $("staff-after-sale-status").value;
  const activeOnly = $("staff-active-only").checked;
  return tickets.filter((ticket) => {
    if (activeOnly && !ticketIsActive(ticket)) return false;
    if (search && !ticketSearchText(ticket).includes(search)) return false;
    if (priority && (ticket.priority || "normal") !== priority) return false;
    if (sla === "overdue" && !ticketIsOverdue(ticket)) return false;
    if (sla === "active" && ticketIsOverdue(ticket)) return false;
    if (assignee === "unassigned" && ticket.assigned_to) return false;
    if (assignee && assignee !== "unassigned" && ticket.assigned_to !== assignee) return false;
    const afterSale = afterSaleFilterValue(ticket);
    if (afterSaleType && afterSale.type !== afterSaleType) return false;
    if (afterSaleStatus && afterSale.status !== afterSaleStatus) return false;
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
    if (action === "verify-order") {
      const form = document.querySelector(`[data-annotation-form="${id}"]`);
      const orderId = form.querySelector('[data-annotation-field="order_id"]').value.trim();
      if (!orderId) throw new Error("请先输入订单号");
      const result = await api(`/api/escalations/${id}/order-context?order_id=${encodeURIComponent(orderId)}`);
      const product = form.querySelector('[data-annotation-field="product"]');
      const status = form.querySelector(`[data-order-status="${id}"]`);
      product.value = result.order_context.product || "";
      product.dataset.verifiedOrderId = orderId;
      status.textContent = `已匹配：${result.order_context.product || "商品"} · ${result.order_context.status_label || "未知状态"}`;
      status.classList.add("verified");
      return;
    }
    if (action === "after-sale-audit") {
      const result = await api(`/api/escalations/${id}/after-sale-audit`);
      const audit = (result.audit || []).map((item) => `<div class="audit-timeline-row"><time>${escapeHtml(item.created_at || "")}</time><b>${escapeHtml(item.action_label || "系统记录")}</b><span>${escapeHtml(item.operator || "系统")}</span><p>${escapeHtml(item.detail || "")}</p></div>`).join("");
      const attempts = (result.attempts || []).map((item) => `<div class="audit-timeline-row attempt-row"><time>${escapeHtml(item.created_at || "")}</time><b>第 ${escapeHtml(item.attempt_no || "")} 次提交：${escapeHtml(item.status === "failed" ? "失败" : item.status === "succeeded" ? "成功" : "执行中")}</b><span>${escapeHtml(item.operator || "系统")}</span><p>${escapeHtml(item.error_message || item.result || "")}</p></div>`).join("");
      const dialog = document.createElement("dialog");
      dialog.className = "audit-dialog";
      dialog.innerHTML = `<form method="dialog"><div class="after-sale-audit-head"><b>${escapeHtml(result.request?.request_type_label || "售后申请")} ${escapeHtml(result.request?.request_no || "")}</b><button class="btn btn-ghost btn-xs">关闭</button></div><div class="audit-dialog-body">${attempts}${audit || '<div class="empty">暂无处理记录</div>'}</div></form>`;
      document.body.appendChild(dialog);
      dialog.addEventListener("close", () => dialog.remove(), { once: true });
      dialog.showModal();
      return;
    }
    if (action === "retry-after-sale") {
      const requestNo = document.querySelector(`[data-action="retry-after-sale"][data-id="${id}"]`)?.dataset.requestNo;
      if (!requestNo) throw new Error("缺少原售后申请号，无法重试");
      if (!window.confirm(`确认使用原申请号 ${requestNo} 重新提交吗？不会生成新的售后申请。`)) return;
      await api(`/api/escalations/${id}/after-sale-retry`, {
        method: "POST",
        body: JSON.stringify({ request_no: requestNo, confirmed: true }),
      });
    }
    if (action === "save-annotation") {
      const form = document.querySelector(`[data-annotation-form="${id}"]`);
      const value = (field) => form.querySelector(`[data-annotation-field="${field}"]`)?.value.trim() || null;
      const orderId = value("order_id");
      const product = form.querySelector('[data-annotation-field="product"]');
      if (orderId && product.dataset.verifiedOrderId !== orderId) throw new Error("请先点击“核验订单”，确认商品后再保存");
      await api(`/api/escalations/${id}/after-sale-annotation`, {
        method: "PUT",
        body: JSON.stringify({
          request_type: value("request_type"),
          stage: value("stage"),
          order_id: value("order_id"),
          product: product.value.trim() || null,
          reason: value("reason"),
          item_status: value("item_status"),
          note: value("note"),
        }),
      });
    }
    if (action === "submit-after-sale") {
      const submitConfig = {
        cancel: { label: "取消订单", endpoint: "cancel-request" },
        return: { label: "退货申请", endpoint: "return-request" },
        refund: { label: "退款申请", endpoint: "refund-request" },
        repair: { label: "维修申请", endpoint: "repair-request" },
      }[document.querySelector(`[data-action="submit-after-sale"][data-id="${id}"]`)?.dataset.requestType];
      if (!submitConfig) throw new Error("不支持的售后提交类型");
      if (!window.confirm(`确认已核对订单并提交${submitConfig.label}吗？提交后会生成真实售后申请。`)) return;
      const button = document.querySelector(`[data-action="submit-after-sale"][data-id="${id}"]`);
      if (button) {
        button.disabled = true;
        button.textContent = "提交中…";
      }
      await api(`/api/escalations/${id}/${submitConfig.endpoint}`, {
        method: "POST",
        body: JSON.stringify({ confirmed: true }),
      });
    }
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
$("staff-after-sale-type").addEventListener("change", renderBoard);
$("staff-after-sale-status").addEventListener("change", renderBoard);
$("staff-active-only").addEventListener("change", renderBoard);
$("escalation-board").addEventListener("change", (event) => {
  const input = event.target.closest('[data-annotation-field="order_id"]');
  if (!input || !input.value.trim()) return;
  const form = input.closest("[data-annotation-form]");
  if (form?.dataset.annotationForm) handleAction("verify-order", form.dataset.annotationForm);
});
$("escalation-board").addEventListener("input", (event) => {
  const input = event.target.closest('[data-annotation-field="order_id"]');
  if (!input) return;
  const form = input.closest("[data-annotation-form]");
  const product = form?.querySelector('[data-annotation-field="product"]');
  const status = form?.querySelector(`[data-order-status="${form.dataset.annotationForm}"]`);
  if (product) {
    delete product.dataset.verifiedOrderId;
    product.value = "";
  }
  if (status) {
    status.textContent = "订单号已变化，请重新核验";
    status.classList.remove("verified");
  }
});
$("logout-btn").addEventListener("click", async () => { await fetch("/api/logout", { method: "POST" }); window.location.href = "/login"; });
window.addEventListener("DOMContentLoaded", async () => { try { const me = await api("/api/me"); if (me.role !== "agent") { window.location.href = me.role === "admin" ? "/admin" : "/"; return; } currentUser = me.username; permissions = new Set(me.permissions || []); $("staff-username").textContent = `客服：${currentUser}`; await loadAgents(); await refreshTickets(); startRealtime(); } catch (_) { window.location.href = "/login"; } });
