"""Web 应用入口：FastAPI 提供 登录 + 聊天式 AI 客服 + 客服后台。

启动方式（二选一）：
  1. python run.py
  2. uvicorn app.main:app --reload

页面：
  /login  登录页
  /       聊天式 AI 客服（客户对话，处理不了自动转人工）
  /admin  客服后台（查看升级工单 + 系统指标）

演示账号（首次启动自动创建，密码均为 123456）：
  admin / 123456   —— 管理员（客服后台）
  user  / 123456   —— 客户（聊天界面）
"""
import threading

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app import auth, config, db, memory, metrics, rag, router, wechat
from app.config import setup_logging
from app.schemas import FAQCreate, FAQUpdate, FeedbackRequest, LoginOut, LoginRequest, OrderCreate, OrderUpdate, ResolveRequest, TicketCreate

# 初始化日志 + 数据库 + 演示账号 + 演示订单
setup_logging()
db.init_db()
auth.seed_users()
db.seed_orders()

# 后台预热 RAG 向量化模型 + 确保知识库已从 faq.md 导入 MySQL 并同步到向量库
# （避免首个 FAQ 请求等待模型冷加载；老库迁移时补种知识库表）
threading.Thread(target=rag.warmup, daemon=True).start()
threading.Thread(target=rag.ensure_faq_seeded, daemon=True).start()

app = FastAPI(
    title="AI客服工单自动化系统",
    description="登录 → 聊天式 AI 客服 → 分类路由 → 模板 / RAG 回复 / 人工升级",
    version="2.0.0",
)

# 静态资源（CSS / JS）
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")


# ---------------- 网页界面 ----------------

@app.get("/", include_in_schema=False)
def chat_page():
    """聊天式 AI 客服界面。"""
    return FileResponse(str(config.BASE_DIR / "templates" / "index.html"))


@app.get("/login", include_in_schema=False)
def login_page():
    """登录页。"""
    return FileResponse(str(config.BASE_DIR / "templates" / "login.html"))


@app.get("/admin", include_in_schema=False)
def admin_page():
    """客服后台。"""
    return FileResponse(str(config.BASE_DIR / "templates" / "admin.html"))


# ---------------- 认证 API ----------------

@app.post("/api/login", response_model=LoginOut)
def login(payload: LoginRequest):
    """登录：校验用户名密码，返回会话 token。"""
    user = auth.authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = auth.create_session(user["username"])
    return {"token": token, "username": user["username"], "role": user["role"]}


@app.post("/api/logout")
def logout(authorization: str = Header(default="")):
    """登出：销毁当前会话 token。"""
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    auth.delete_session(token)
    return {"status": "ok"}


@app.get("/api/me")
def me(user: dict | None = Depends(auth.get_current_user)):
    """当前登录用户信息（未登录返回 401）。"""
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已失效")
    return {"username": user["username"], "role": user["role"]}


@app.post("/api/conversation/clear")
def clear_conversation(username: str = Depends(auth.require_user)):
    """清空当前用户的多轮对话记忆（前端「新对话」按钮）。"""
    memory.clear(username)
    return {"status": "ok"}


# ---------------- 业务 API（需登录） ----------------

@app.post("/api/tickets")
def create_ticket(payload: TicketCreate, username: str = Depends(auth.require_user)):
    """发送一条客服消息 / 提交一张工单，返回 AI 处理结果。"""
    record = router.process_ticket(payload.ticket_text, username=username,
                                   phone=payload.phone,
                                   device=payload.device or "web")
    ticket_id = router.save_processed(record)
    record["id"] = ticket_id
    # 全链路留痕：记录工单创建 + 最终状态（供状态流转追溯）
    db.insert_ticket_log(ticket_id, "created",
                         f"工单创建，状态 {record['status']}，来源 {record['reply_source']}",
                         username)
    return record


@app.get("/api/tickets")
def list_tickets(username: str = Depends(auth.require_user)):
    """最近的工单列表。"""
    return db.list_tickets()


@app.get("/api/tickets/search")
def search_tickets(keyword: str | None = None, category: str | None = None,
                   status: str | None = None, emotion: str | None = None,
                   username: str = Depends(auth.require_admin)):
    """多维度检索工单：按关键词 / 分类 / 状态 / 情绪 / 用户（仅管理员）。

    注意：必须定义在 /api/tickets/{ticket_id} 之前，否则 "search" 会被当成
    ticket_id 尝试解析成 int 而返回 422。
    """
    return db.search_tickets(keyword=keyword, category=category, status=status, emotion=emotion)


@app.get("/api/tickets/{ticket_id}")
def get_ticket(ticket_id: int, username: str = Depends(auth.require_user)):
    """查询单张工单详情。"""
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return ticket


@app.get("/api/tickets/{ticket_id}/logs")
def ticket_logs(ticket_id: int, username: str = Depends(auth.require_admin)):
    """查询某张工单的状态流转 / 审计日志（仅管理员）。"""
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    return db.list_ticket_logs(ticket_id)


@app.post("/api/tickets/{ticket_id}/feedback")
def ticket_feedback(ticket_id: int, payload: FeedbackRequest, username: str = Depends(auth.require_user)):
    """用户对某条 AI 回复点 👍/👎，记录满意度。"""
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if payload.feedback not in ("up", "down"):
        raise HTTPException(status_code=400, detail="feedback 只能是 up 或 down")
    db.update_ticket_feedback(ticket_id, payload.feedback)
    return {"status": "ok", "id": ticket_id, "feedback": payload.feedback}


# ---------------- 客服后台（仅管理员） ----------------

@app.get("/api/escalations")
def list_escalations(username: str = Depends(auth.require_admin)):
    """查询所有升级给人工的工单（含待处理 + 已处理）。"""
    return db.list_escalations()


@app.post("/api/escalations/{ticket_id}/resolve")
def resolve_escalation(ticket_id: int, payload: ResolveRequest, username: str = Depends(auth.require_admin)):
    """人工客服处理完毕：写入回答，并可把问答回流到知识库供 AI 学习。"""
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")

    answer = (payload.human_answer or "").strip()
    db.update_ticket_answer(ticket_id, answer, "resolved")
    db.log_status_change(ticket_id, "resolved", username)

    result = {"status": "ok", "id": ticket_id, "saved_to_kb": False}
    if answer and payload.save_to_kb:
        rag.add_entry(ticket["ticket_text"], answer)
        result["saved_to_kb"] = True
    return result


@app.get("/api/metrics")
def get_metrics(username: str = Depends(auth.require_user)):
    """系统核心指标（自动处理率、分类准确率、平均耗时等）。"""
    return metrics.overview()


# ---------------- 订单管理（仅管理员，增删改查） ----------------

@app.get("/api/orders")
def list_orders(username: str = Depends(auth.require_admin)):
    """管理员查看全部订单。"""
    return db.list_all_orders()


@app.post("/api/orders")
def create_order(payload: OrderCreate, username: str = Depends(auth.require_admin)):
    """管理员新增订单。"""
    if db.get_order_by_id(payload.order_id):
        raise HTTPException(status_code=409, detail="订单号已存在")
    db.insert_order(payload.order_id, payload.username, payload.product, payload.status,
                    payload.tracking_no, payload.logistics, payload.refund_status)
    return db.get_order_by_id(payload.order_id)


@app.put("/api/orders/{order_id}")
def update_order(order_id: str, payload: OrderUpdate, username: str = Depends(auth.require_admin)):
    """管理员编辑订单（按订单号，只更新提交的字段）。"""
    if db.get_order_by_id(order_id) is None:
        raise HTTPException(status_code=404, detail="订单不存在")
    fields = payload.dict(exclude_none=True)
    return db.update_order(order_id, **fields)


@app.delete("/api/orders/{order_id}")
def delete_order(order_id: str, username: str = Depends(auth.require_admin)):
    """管理员删除订单。"""
    if not db.delete_order(order_id):
        raise HTTPException(status_code=404, detail="订单不存在")
    return {"status": "ok", "order_id": order_id}


# ---------------- 知识库管理（仅管理员，实时更新 RAG） ----------------

@app.get("/api/faq")
def list_faq(include_disabled: bool = False, username: str = Depends(auth.require_admin)):
    """列出知识库条目；include_disabled=true 时含软删除的条目。"""
    return rag.list_entries(include_disabled=include_disabled)


@app.post("/api/faq")
def add_faq(payload: FAQCreate, username: str = Depends(auth.require_admin)):
    """实时新增一条知识：写入 MySQL 并同步向量库，无需重启即可生效。"""
    try:
        return rag.add_entry(payload.question, payload.answer)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/faq/{faq_id}")
def update_faq(faq_id: int, payload: FAQUpdate, username: str = Depends(auth.require_admin)):
    """编辑一条知识库条目（问题/答案可部分更新），并同步向量库。"""
    try:
        return rag.update_entry(faq_id, question=payload.question, answer=payload.answer)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.delete("/api/faq/{faq_id}")
def disable_faq(faq_id: int, username: str = Depends(auth.require_admin)):
    """软删除一条知识库条目（从向量库移除，数据保留可恢复）。"""
    try:
        return rag.disable_entry(faq_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/faq/{faq_id}/restore")
def restore_faq(faq_id: int, username: str = Depends(auth.require_admin)):
    """重新启用一条被软删除的知识库条目。"""
    try:
        return rag.enable_entry(faq_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


# ---------------- 微信公众号接入（无需登录，供微信服务器回调） ----------------

@app.get("/wechat")
def wechat_verify(signature: str, timestamp: str, nonce: str, echostr: str):
    """微信服务器首次配置时验证签名，返回 echostr。"""
    if wechat.verify_signature(signature, timestamp, nonce):
        return Response(content=echostr, media_type="text/plain")
    raise HTTPException(status_code=403, detail="签名校验失败")


@app.post("/wechat")
async def wechat_message(request: Request):
    """接收微信发来的消息，复用工单处理流程后返回文本回复。"""
    body = await request.body()
    msg = wechat.parse_message(body)
    if msg.get("MsgType") != "text":
        # 只处理文本消息，其他类型直接返回 success
        return Response(content="success", media_type="text/plain")
    reply = router.process_ticket(msg.get("Content", ""), device="wechat")["reply"]
    xml = wechat.build_text_reply(msg["FromUserName"], msg["ToUserName"], reply)
    return Response(content=xml, media_type="application/xml")


@app.get("/health")
def health():
    """健康检查（无需登录）。"""
    return {
        "status": "ok",
        "model": config.Config.MODEL,
        "api_key_configured": bool(config.Config.DEEPSEEK_API_KEY),
    }
