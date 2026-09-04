"""Web 应用入口：FastAPI 提供登录、客户聊天、客服工作台、管理员后台和经理仪表盘。

启动方式（二选一）：
  1. python run.py
  2. uvicorn app.main:app --reload

页面：
  /login  登录页
  /       聊天式 AI 客服（客户对话，处理不了自动转人工）
  /admin  系统管理员后台（账号、密码与权限）
  /manager  经理运营仪表盘（只读运营数据）
  /staff  客服工作台（仅处理转人工工单）

演示账号（首次启动自动创建，密码均为 123456）：
  admin / 123456   —— 系统管理员（账号、密码与权限）
  manager / 123456 —— 经理（运营仪表盘）
  agent / 123456   —— 客服（处理转人工工单）
  user  / 123456   —— 客户（聊天界面）
"""
import asyncio
import json
import logging
import threading
import time
import uuid
from collections import Counter
from datetime import datetime, timedelta

from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from app import auth, config, db, events, feedback, memory, metrics, privacy, profile, rag, redis_store, router, sla, vision, wechat
from app.config import setup_logging
from app.schemas import FAQCreate, FAQUpdate, FeedbackRequest, FeedbackTagRequest, LoginOut, LoginRequest, ManagedPasswordReset, ManagedUserCreate, ManagedUserUpdate, OrderCreate, OrderUpdate, PasswordResetRequest, PhoneCodeSendRequest, PhoneCodeVerifyRequest, ProfileFactUpdate, RegisterRequest, ResolveRequest, TicketAssignRequest, TicketCreate, TicketStatusRequest

logger = logging.getLogger("app.main")

# 初始化日志 + 数据库 + 演示账号 + 演示订单
setup_logging()
db.init_db()
if config.Config.DEMO_DATA_ENABLED:
    auth.seed_users()
    db.seed_orders()
    db.seed_profile_facts()

# 后台预热 RAG 向量化模型 + 确保知识库已从 faq.md 导入 MySQL 并同步到向量库
# （避免首个 FAQ 请求等待模型冷加载；老库迁移时补种知识库表）
threading.Thread(target=rag.warmup, daemon=True).start()
threading.Thread(target=rag.ensure_faq_seeded, daemon=True).start()

app = FastAPI(
    title="AI客服工单自动化系统",
    description="登录 → 聊天式 AI 客服 → 分类路由 → 模板 / RAG 回复 / 人工升级",
    version="2.0.0",
)


@app.on_event("startup")
def start_background_workers():
    """启动不依赖前端的 SLA 扫描线程。"""
    sla.worker.start()


@app.on_event("shutdown")
def stop_background_workers():
    """优雅停止后台线程，避免测试或重启遗留扫描任务。"""
    sla.worker.stop()

# 静态资源（CSS / JS）
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    """为每次请求生成可串联日志与前端报错的 request id。"""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        logger.exception(
            "request failed id=%s method=%s path=%s",
            request_id, request.method, request.url.path,
        )
        raise
    response.headers["X-Request-ID"] = request_id
    logger.info(
        "request completed id=%s method=%s path=%s status=%s duration_ms=%d",
        request_id, request.method, request.url.path,
        response.status_code, int((time.perf_counter() - started) * 1000),
    )
    return response


# ---------------- 网页界面 ----------------

@app.get("/", include_in_schema=False)
def chat_page(user: dict | None = Depends(auth.get_current_user)):
    """聊天式 AI 客服界面。"""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(str(config.BASE_DIR / "templates" / "index.html"))


@app.get("/login", include_in_schema=False)
def login_page():
    """登录页。"""
    return FileResponse(str(config.BASE_DIR / "templates" / "login.html"))


@app.get("/register", include_in_schema=False)
def register_page():
    """普通客户注册页。"""
    return FileResponse(str(config.BASE_DIR / "templates" / "register.html"))


@app.get("/forgot-password", include_in_schema=False)
def forgot_password_page():
    """密码重置页。"""
    return FileResponse(str(config.BASE_DIR / "templates" / "forgot_password.html"))


@app.get("/profile", include_in_schema=False)
def profile_page(user: dict | None = Depends(auth.get_current_user)):
    """客户实体档案页；经理与客服工作台由独立页面控制。"""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    return FileResponse(str(config.BASE_DIR / "templates" / "profile.html"))


@app.get("/admin", include_in_schema=False)
def admin_page(user: dict | None = Depends(auth.get_current_user)):
    """系统管理员后台：账号、密码与权限管理。"""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.get("role") != "admin" or not user.get("active", 1):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return FileResponse(str(config.BASE_DIR / "templates" / "admin.html"))


@app.get("/manager", include_in_schema=False)
def manager_page(user: dict | None = Depends(auth.get_current_user)):
    """经理运营仪表盘。"""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.get("role") != "manager" or not user.get("active", 1):
        raise HTTPException(status_code=403, detail="需要经理权限")
    return FileResponse(str(config.BASE_DIR / "templates" / "manager.html"))


@app.get("/staff", include_in_schema=False)
def staff_page(user: dict | None = Depends(auth.get_current_user)):
    """客服人工工作台。"""
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.get("role") != "agent" or not user.get("active", 1):
        raise HTTPException(status_code=403, detail="需要客服权限")
    return FileResponse(str(config.BASE_DIR / "templates" / "staff.html"))


# ---------------- 认证 API ----------------

@app.post("/api/login", response_model=LoginOut)
def login(payload: LoginRequest, response: Response):
    """登录：密码正确后先通过手机号核验，成功才设置 HttpOnly 会话 Cookie。"""
    user = auth.authenticate(payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    if not auth.is_phone_verified(user):
        try:
            challenge_id = auth.create_phone_challenge(user)
        except ValueError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return {
            "username": user["username"],
            "role": user["role"],
            "status": "phone_verification_required",
            "requires_phone_verification": True,
            "challenge_id": challenge_id,
            "phone_masked": auth.mask_phone(user.get("phone")),
            "active": True,
            "permissions": sorted(auth.user_permissions(user)),
        }
    return _complete_login(user, response)


def _complete_login(user: dict, response: Response) -> dict:
    """仅在密码和手机号均通过服务端校验后建立正式会话。"""
    token = auth.create_session(user["username"])
    response.set_cookie(
        key=config.Config.SESSION_COOKIE_NAME,
        value=token,
        max_age=config.Config.SESSION_TTL_SECONDS,
        httponly=True,
        samesite="lax",
        secure=config.Config.SESSION_COOKIE_SECURE,
        path="/",
    )
    return {
        "username": user["username"],
        "role": user["role"],
        "status": "authenticated",
        "requires_phone_verification": False,
        "active": bool(user.get("active", 1)),
        "permissions": sorted(auth.user_permissions(user)),
    }


@app.post("/api/login/send-code")
def send_login_phone_code(payload: PhoneCodeSendRequest):
    """发送登录手机号验证码；验证码本身只在演示模式回传。"""
    try:
        return auth.send_phone_code(payload.challenge_id, payload.phone)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/api/login/verify-phone", response_model=LoginOut)
def verify_login_phone(payload: PhoneCodeVerifyRequest, response: Response):
    """完成手机号核验并建立正式会话。"""
    try:
        user = auth.verify_phone_challenge(
            payload.challenge_id, payload.phone, payload.code
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return _complete_login(user, response)


@app.post("/api/register", response_model=LoginOut)
def register(payload: RegisterRequest, response: Response):
    """注册普通客户；注册完成后同样必须先完成手机号核验。"""
    try:
        user = auth.register_customer(
            payload.username, payload.password, payload.confirm_password
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        challenge_id = auth.create_phone_challenge(user)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    return {
        "username": user["username"],
        "role": "customer",
        "status": "phone_verification_required",
        "requires_phone_verification": True,
        "challenge_id": challenge_id,
        "phone_masked": auth.mask_phone(user.get("phone")),
        "active": True,
        "permissions": [],
    }


@app.post("/api/password-reset")
def password_reset(payload: PasswordResetRequest):
    """使用服务端配置的重置口令修改密码。"""
    if not auth.reset_password(
        payload.username, payload.reset_code, payload.password, payload.confirm_password
    ):
        raise HTTPException(status_code=400, detail="重置失败，请检查信息后重试")
    return {"status": "ok"}


# ---------------- 系统管理员账号与权限管理 ----------------

def _validate_managed_role(role: str) -> str:
    role = (role or "").strip()
    if role not in {"admin", "manager", "agent", "customer"}:
        raise HTTPException(status_code=400, detail="角色只能是 admin、manager、agent 或 customer")
    return role


def _validate_managed_permissions(role: str, permissions: list[str]) -> list[str]:
    permissions = list(dict.fromkeys(permissions or []))
    invalid = [item for item in permissions if item not in auth.PERMISSIONS]
    if invalid:
        raise HTTPException(status_code=400, detail="存在无效客服权限")
    if role != "agent" and permissions:
        raise HTTPException(status_code=400, detail="只有客服账号可以配置工单权限")
    return permissions


@app.get("/api/admin/users")
def list_managed_users(username: str = Depends(auth.require_admin)):
    """管理员查看账号列表，不返回密码哈希。"""
    return db.list_users()


@app.post("/api/admin/users")
def create_managed_user(payload: ManagedUserCreate,
                        username: str = Depends(auth.require_admin)):
    """管理员创建受管账号。"""
    target_role = _validate_managed_role(payload.role)
    permissions = _validate_managed_permissions(target_role, payload.permissions)
    try:
        phone = auth.normalize_phone(
            payload.phone, required=target_role in {"admin", "manager", "agent"}
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    target_username = payload.username.strip()
    if not auth.validate_username(target_username):
        raise HTTPException(status_code=400, detail="用户名须为 3-32 位字母、数字或下划线")
    if not auth.validate_password(payload.password):
        raise HTTPException(status_code=400, detail="密码至少 8 位，且必须包含字母和数字")
    if db.get_user_by_username(target_username) is not None:
        raise HTTPException(status_code=409, detail="用户名已存在")
    try:
        db.create_user(target_username, auth.hash_password(payload.password),
                       role=target_role, permissions=permissions, phone=phone)
    except Exception as exc:  # noqa: BLE001
        if db.get_user_by_username(target_username) is not None:
            raise HTTPException(status_code=409, detail="用户名已存在") from exc
        raise
    db.insert_account_audit(
        target_username, "account_created",
        f"创建账号，角色={target_role}，手机号={auth.mask_phone(phone)}", username,
    )
    return {"status": "ok", "username": target_username, "role": target_role}


@app.patch("/api/admin/users/{target_username}")
def update_managed_user(target_username: str, payload: ManagedUserUpdate,
                        username: str = Depends(auth.require_admin)):
    """管理员调整账号角色、启停用状态和客服权限。"""
    target = db.get_user_by_username(target_username)
    if target is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    target_role = _validate_managed_role(payload.role)
    permissions = _validate_managed_permissions(target_role, payload.permissions)
    try:
        requested_phone = auth.normalize_phone(payload.phone, required=False)
        existing_phone = auth.normalize_phone(target.get("phone"), required=False)
        phone = requested_phone or existing_phone
        if target_role in {"admin", "manager", "agent"} and not phone:
            raise ValueError("企业账号必须绑定手机号")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if target_username == username and (not payload.active or target_role != "admin"):
        raise HTTPException(status_code=400, detail="不能停用或降级当前登录的管理员账号")
    if target.get("role") == "admin" and (not payload.active or target_role != "admin"):
        active_admins = sum(
            1 for item in db.list_users()
            if item.get("role") == "admin" and item.get("active", True)
        )
        if active_admins <= 1:
            raise HTTPException(status_code=400, detail="至少保留一个启用中的管理员账号")
    if not db.update_user_account(
        target_username, target_role, payload.active, permissions, phone=phone
    ):
        raise HTTPException(status_code=404, detail="账号不存在")
    db.insert_account_audit(
        target_username,
        "account_updated",
        f"角色={target_role}，启用={payload.active}，权限={','.join(permissions) or '无'}，手机号={auth.mask_phone(phone)}",
        username,
    )
    return {"status": "ok", "username": target_username, "role": target_role,
            "active": payload.active, "permissions": permissions}


@app.post("/api/admin/users/{target_username}/password")
def reset_managed_password(target_username: str, payload: ManagedPasswordReset,
                           username: str = Depends(auth.require_admin)):
    """管理员重置指定账号密码。"""
    if db.get_user_by_username(target_username) is None:
        raise HTTPException(status_code=404, detail="账号不存在")
    if not auth.validate_password(payload.password):
        raise HTTPException(status_code=400, detail="密码至少 8 位，且必须包含字母和数字")
    if not db.update_user_password(target_username, auth.hash_password(payload.password)):
        raise HTTPException(status_code=404, detail="账号不存在")
    db.insert_account_audit(target_username, "password_reset", "管理员重置密码", username)
    return {"status": "ok", "username": target_username}


@app.get("/api/admin/account-audits")
def list_account_audits(username: str = Depends(auth.require_admin)):
    """管理员查看账号变更审计记录。"""
    return db.list_account_audits()


def _sanitize_agent_audit_value(value, depth: int = 0):
    """为管理员审计输出做递归脱敏和长度限制。"""
    if depth > 5:
        return "[已省略]"
    if isinstance(value, dict):
        return {str(key): _sanitize_agent_audit_value(item, depth + 1) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_agent_audit_value(item, depth + 1) for item in value[:30]]
    if isinstance(value, str):
        return privacy.mask_sensitive(value[:1000])
    return value


def _agent_audit_runs(entries: list[dict]) -> list[dict]:
    """把黑板事件聚合成管理员可扫描的运行记录。"""
    grouped = {}
    for entry in entries:
        task_id = entry.get("task_id")
        if not task_id:
            continue
        run = grouped.setdefault(task_id, {
            "task_id": task_id,
            "status": "running",
            "specialist": "",
            "question_summary": "",
            "event_count": 0,
            "created_at": entry.get("created_at"),
            "completed_at": None,
            "confidence": None,
            "events": [],
        })
        run["event_count"] += 1
        run["events"].append({
            "id": entry.get("id"),
            "entry_type": entry.get("entry_type"),
            "source_agent": entry.get("source_agent"),
            "payload": _sanitize_agent_audit_value(entry.get("payload", {})),
            "confidence": entry.get("confidence"),
            "created_at": entry.get("created_at"),
        })
        payload = entry.get("payload") or {}
        if entry.get("entry_type") == "task_created":
            run["question_summary"] = privacy.mask_sensitive(str(payload.get("question", ""))[:240])
        if entry.get("entry_type") == "task_dispatched":
            run["specialist"] = str(payload.get("receiver", ""))[:64]
        if entry.get("entry_type") == "task_completed":
            run["status"] = "success"
            run["completed_at"] = entry.get("created_at")
            run["confidence"] = entry.get("confidence")
        elif entry.get("entry_type") == "task_rejected":
            run["status"] = "failed"
            run["completed_at"] = entry.get("created_at")
    return sorted(grouped.values(), key=lambda item: (item.get("created_at") or "", item["task_id"]), reverse=True)


@app.get("/api/admin/agent-runs")
def list_agent_runs(limit: int = 50, username: str = Depends(auth.require_admin)):
    """管理员查看最近多 Agent 运行摘要，不开放给其他角色。"""
    limit = max(1, min(limit, 100))
    entries = db.list_recent_blackboard_entries(limit=max(limit * 8, 100))
    runs = _agent_audit_runs(entries)
    return [{key: value for key, value in run.items() if key != "events"} for run in runs[:limit]]


@app.get("/api/admin/agent-runs/{task_id}")
def get_agent_run(task_id: str, username: str = Depends(auth.require_admin)):
    """管理员查看单次多 Agent 运行的脱敏 JSON 轨迹。"""
    task_id = task_id.strip()
    if not task_id or len(task_id) > 96:
        raise HTTPException(status_code=400, detail="无效的 Agent 任务 ID")
    runs = _agent_audit_runs(db.list_blackboard_entries(task_id, limit=100))
    if not runs:
        raise HTTPException(status_code=404, detail="Agent 任务不存在")
    return runs[0]


@app.post("/api/logout")
def logout(
    response: Response,
    authorization: str = Header(default=""),
    session_token: str = Cookie(default="", alias=config.Config.SESSION_COOKIE_NAME),
):
    """登出：结束当前用户人工会话后销毁服务端会话和 Cookie。"""
    token = auth.extract_token(authorization, session_token)
    username = auth.get_username(token)
    if username:
        try:
            _end_human_session(username, reason="用户退出登录，结束人工会话")
        except Exception:  # noqa: BLE001
            # 登出不能因为工单收尾失败而被阻断，异常保留在服务端日志中排查。
            logger.exception("logout close human session failed username=%s", username)
    auth.delete_session(token)
    response.delete_cookie(key=config.Config.SESSION_COOKIE_NAME, path="/")
    return {"status": "ok"}


@app.get("/api/me")
def me(user: dict | None = Depends(auth.get_current_user)):
    """当前登录用户信息（未登录返回 401）。"""
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已失效")
    return {
        "username": user["username"],
        "role": user["role"],
        "active": bool(user.get("active", 1)),
        "permissions": sorted(auth.user_permissions(user)),
    }


@app.get("/api/events")
async def event_stream(
    request: Request,
    last_event_id: str = Header(default="", alias="Last-Event-ID"),
    user: dict | None = Depends(auth.get_current_user),
):
    """SSE 实时事件流；事件按服务端会话身份过滤。"""
    if user is None:
        raise HTTPException(status_code=401, detail="未登录或登录已失效")

    try:
        cursor = max(0, int(last_event_id or 0))
    except ValueError:
        cursor = 0
    is_admin = user.get("role") == "admin"
    is_manager = user.get("role") == "manager"
    is_agent = user.get("role") == "agent"
    event_user = user.get("username")

    def can_receive(event: events.Event) -> bool:
        if is_admin or is_manager or is_agent:
            return event.event_type in (
                "ticket_escalated", "customer_message", "human_replied", "ticket_updated"
            )
        return (
            event.event_type == "human_replied"
            and event.payload.get("username") == event_user
        )

    async def generate():
        nonlocal cursor
        yield ": connected\n\n"
        while not await request.is_disconnected():
            new_events, cursor = await asyncio.to_thread(
                events.broker.wait_for, cursor, can_receive
            )
            if not new_events:
                yield ": heartbeat\n\n"
                continue
            for event in new_events:
                payload = json.dumps(event.payload, ensure_ascii=False)
                yield f"id: {event.id}\nevent: {event.event_type}\ndata: {payload}\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@app.post("/api/conversation/clear")
def clear_conversation(username: str = Depends(auth.require_user)):
    """新对话：清空短期记忆并结束未完成的人工接管。"""
    memory.clear(username)
    return _end_human_session(username, reason="客户开启新对话，结束人工会话")


def _end_human_session(username: str, reason: str) -> dict:
    """关闭客户全部未结束人工工单，并同步审计与后台事件。"""
    ticket_ids = db.close_active_human_tickets(username)
    for ticket_id in ticket_ids:
        db.insert_ticket_log(ticket_id, "customer_closed", reason, username)
        events.broker.publish(
            "ticket_updated",
            {
                "ticket_id": ticket_id,
                "username": username,
                "status": "closed",
                "closed_by": "customer",
            },
        )
    return {"status": "ok", "closed_ticket_ids": ticket_ids}


@app.post("/api/human-session/end")
def end_human_session(username: str = Depends(auth.require_user)):
    """客户主动结束当前人工会话，之后的新消息重新进入 AI 路由。"""
    return _end_human_session(username, reason="客户主动结束人工会话")


@app.get("/api/profile")
def get_profile(username: str = Depends(auth.require_user)):
    """查看当前用户的实体画像事实卡片。"""
    return profile.list_facts(username)


@app.delete("/api/profile/{fact_id}")
def delete_profile_fact(fact_id: int, username: str = Depends(auth.require_user)):
    """删除当前用户的一条实体画像事实。"""
    if not profile.delete_fact(username, fact_id):
        raise HTTPException(status_code=404, detail="画像事实不存在")
    return {"status": "ok", "id": fact_id}


@app.put("/api/profile/{fact_id}")
def update_profile_fact(fact_id: int, payload: ProfileFactUpdate,
                        username: str = Depends(auth.require_user)):
    """手工修正当前用户的一条实体画像事实。"""
    try:
        updated = profile.update_fact(username, fact_id, payload.fact_value, payload.confirmed)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if updated is None:
        raise HTTPException(status_code=404, detail="画像事实不存在")
    return updated


@app.get("/api/entity-archive")
def get_entity_archive(username: str = Depends(auth.require_user)):
    """返回当前客户的主体事实和业务客体；客体状态以业务表为准且只读。"""
    facts = profile.list_facts(username)
    orders = db.list_orders_for_user(username)
    tickets = db.list_tickets_for_user(username, limit=20)

    def public_order(order: dict) -> dict:
        return {key: order.get(key) for key in (
            "order_id", "product", "status", "tracking_no", "logistics", "refund_status", "created_at"
        )}

    def public_ticket(ticket: dict) -> dict:
        return {key: ticket.get(key) for key in (
            "id", "category", "status", "ticket_text", "created_at"
        )}

    return {
        "subject": {"username": username, "facts": facts},
        "objects": {
            "orders": [public_order(order) for order in orders],
            "tickets": [public_ticket(ticket) for ticket in tickets],
        },
    }


# ---------------- 业务 API（需登录） ----------------

def _save_ticket_record(record: dict, username: str) -> dict:
    """落库并发布需要实时通知的工单事件。"""
    if record.get("status") == "escalated":
        record.setdefault("priority", "normal")
        record.setdefault(
            "sla_due_at",
            datetime.now() + timedelta(minutes=config.Config.DEFAULT_SLA_MINUTES),
        )
    persisted = {
        key: value for key, value in record.items()
        if key not in {"image_analysis", "image_analysis_status"}
    }
    ticket_id = router.save_processed(persisted)
    record["id"] = ticket_id
    db.insert_ticket_message(ticket_id, "customer", record.get("ticket_text", ""), username)
    if record.get("reply"):
        db.insert_ticket_message(ticket_id, "assistant", record["reply"])
    db.insert_ticket_log(
        ticket_id,
        "created",
        f"工单创建，状态 {record['status']}，来源 {record['reply_source']}",
        username,
    )
    if record.get("status") == "escalated":
        events.broker.publish(
            "ticket_escalated",
            {"ticket_id": ticket_id, "username": username},
        )
    return record


def _continue_human_handoff(ticket_text: str, username: str,
                            image_analysis: str | None = None,
                            image_analysis_status: str | None = None) -> dict | None:
    """人工接管期间追加客户消息，禁止再次进入 AI 路由。"""
    safe_text = privacy.mask_sensitive(ticket_text)
    if not safe_text:
        return None

    active = db.get_active_human_ticket_for_user(username)
    if active is None:
        return None
    updated = db.append_customer_message(active["id"], username, safe_text)
    if updated is None:
        # 人工可能刚好已经结束工单；让本次消息按新会话重新处理。
        return None

    ticket_id = updated["id"]
    status = updated.get("status") or active.get("status")
    if status != active.get("status"):
        db.log_status_change(ticket_id, status, username)
    db.insert_ticket_log(
        ticket_id,
        "customer_message",
        "客户在人工处理中补充消息",
        username,
    )
    events.broker.publish(
        "customer_message",
        {
            "ticket_id": ticket_id,
            "username": username,
            "message": safe_text,
        },
    )

    record = {
        "id": ticket_id,
        "ticket_text": safe_text,
        "username": username,
        "category": updated.get("category") or "人工处理工单",
        "confidence": updated.get("confidence") or 1.0,
        "reason": "人工客服处理中，客户补充消息已追加到原工单",
        "status": status,
        "reply": (
            f"已收到您的补充信息，已追加到人工工单 #{ticket_id}。"
            "人工客服正在处理中，后续消息将由人工客服回复。"
        ),
        "reply_source": "human_queue",
        "human_answer": None,
        "assigned_to": updated.get("assigned_to"),
        "priority": updated.get("priority"),
        "sla_due_at": updated.get("sla_due_at"),
    }
    if image_analysis_status:
        record["image_analysis"] = image_analysis
        record["image_analysis_status"] = image_analysis_status
    return record


@app.post("/api/tickets")
def create_ticket(payload: TicketCreate, username: str = Depends(auth.require_user)):
    """发送一条客服消息 / 提交一张工单，返回 AI 处理结果。"""
    handoff = _continue_human_handoff(payload.ticket_text, username)
    if handoff is not None:
        return handoff
    record = router.process_ticket(payload.ticket_text, username=username,
                                   phone=payload.phone,
                                   device=payload.device or "web")
    return _save_ticket_record(record, username)


@app.post("/api/tickets/multimodal")
async def create_multimodal_ticket(
    text: str = Form(default=""),
    image: UploadFile = File(...),
    username: str = Depends(auth.require_user),
):
    """接收图片并识别；图片本身不落库，只保存文本化识别结果。"""
    user_text = (text or "").strip()[:4000]
    image_data = await image.read(config.Config.MAX_IMAGE_BYTES + 1)
    try:
        media_type = vision.validate_image(image.content_type, image_data)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    analysis = None
    analysis_status = "unavailable"
    try:
        analysis = vision.analyze(image_data, media_type, user_text)
        analysis_status = "analyzed"
    except Exception as exc:  # noqa: BLE001
        # 视觉模型失败时保留文字工单；纯图片则明确升级人工查看。
        logger.warning("图片识别失败，降级处理：%s", exc)

    if analysis:
        ticket_text = "\n\n".join(
            part for part in (
                f"用户文字：{user_text}" if user_text else "",
                f"[图片识别结果]\n{analysis}",
            ) if part
        )
    elif user_text:
        ticket_text = user_text
    else:
        ticket_text = "用户发送了一张图片，但图片识别暂不可用，请转人工处理。"

    handoff = _continue_human_handoff(
        ticket_text,
        username,
        image_analysis=analysis,
        image_analysis_status=analysis_status,
    )
    if handoff is not None:
        return handoff

    record = router.process_ticket(ticket_text, username=username, device="web-image")
    record["image_analysis"] = analysis
    record["image_analysis_status"] = analysis_status
    return _save_ticket_record(record, username)


@app.get("/api/tickets")
def list_tickets(username: str = Depends(auth.require_user)):
    """最近的工单列表。"""
    if auth.is_admin(username):
        return db.list_tickets()
    return db.list_tickets(username=username)


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
    ticket = (db.get_ticket(ticket_id) if auth.is_admin(username)
              else db.get_ticket_for_user(username, ticket_id))
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
    admin = auth.is_admin(username)
    ticket = db.get_ticket(ticket_id) if admin else db.get_ticket_for_user(username, ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if payload.feedback not in ("up", "down"):
        raise HTTPException(status_code=400, detail="feedback 只能是 up 或 down")
    db.update_ticket_feedback(
        ticket_id,
        payload.feedback,
        username=None if admin else username,
    )
    return {"status": "ok", "id": ticket_id, "feedback": payload.feedback}


# ---------------- 客服人工工作台（仅客服账号） ----------------

@app.get("/api/escalations")
def list_escalations(username: str = Depends(auth.require_agent_permission("ticket.view"))):
    """查询所有升级给人工的工单（含待处理 + 已处理）。"""
    return db.list_escalations()


@app.get("/api/agents")
def list_agents(username: str = Depends(auth.require_agent_permission("ticket.transfer"))):
    """返回可转派的启用客服账号。"""
    return db.list_staff_users()


@app.post("/api/escalations/{ticket_id}/claim")
def claim_escalation(ticket_id: int, username: str = Depends(auth.require_agent_permission("ticket.claim"))):
    """坐席原子接单，防止多人同时处理同一工单。"""
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.get("status") == "in_progress" and ticket.get("assigned_to") == username:
        return {"status": "ok", "id": ticket_id, "assigned_to": username}
    if ticket.get("status") != "escalated":
        raise HTTPException(status_code=409, detail="该工单已被其他坐席接单或已结束")
    if not db.claim_ticket(ticket_id, username):
        raise HTTPException(status_code=409, detail="该工单已被其他坐席接单")
    db.log_status_change(ticket_id, "in_progress", username)
    events.broker.publish(
        "ticket_updated",
        {"ticket_id": ticket_id, "status": "in_progress", "assigned_to": username},
    )
    return {"status": "ok", "id": ticket_id, "assigned_to": username}


@app.post("/api/escalations/{ticket_id}/assign")
def assign_escalation(ticket_id: int, payload: TicketAssignRequest,
                      username: str = Depends(auth.require_agent_permission("ticket.transfer"))):
    """将人工工单转派给另一名客服账号。"""
    target = (payload.assigned_to or "").strip()
    if not target:
        raise HTTPException(status_code=400, detail="处理人不能为空")
    target_user = db.get_user_by_username(target)
    if target_user is None or target_user.get("role") != "agent" or not target_user.get("active", 1):
        raise HTTPException(status_code=400, detail="处理人必须是有效的客服账号")
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.get("status") in ("resolved", "closed"):
        raise HTTPException(status_code=409, detail="已结束工单不能转派")
    if not db.transfer_ticket(ticket_id, username, target):
        raise HTTPException(status_code=409, detail="当前工单已由其他坐席处理")
    db.log_status_change(ticket_id, "in_progress", username)
    events.broker.publish(
        "ticket_updated",
        {"ticket_id": ticket_id, "status": "in_progress", "assigned_to": target},
    )
    return {"status": "ok", "id": ticket_id, "assigned_to": target}


@app.patch("/api/escalations/{ticket_id}/status")
def update_escalation_status(ticket_id: int, payload: TicketStatusRequest,
                             username: str = Depends(auth.require_agent_permission("ticket.reply"))):
    """推进人工工单协作状态。"""
    status = (payload.status or "").strip()
    if status not in {"in_progress", "waiting_customer", "closed"}:
        raise HTTPException(status_code=400, detail="不支持的工单状态")
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if not db.update_ticket_workflow_status(ticket_id, username, status):
        raise HTTPException(status_code=409, detail="当前状态不允许此操作，或工单不属于当前坐席")
    db.log_status_change(ticket_id, status, username)
    events.broker.publish(
        "ticket_updated",
        {"ticket_id": ticket_id, "status": status, "assigned_to": username},
    )
    return {"status": "ok", "id": ticket_id, "workflow_status": status}


@app.post("/api/escalations/{ticket_id}/reply")
def reply_escalation(ticket_id: int, payload: ResolveRequest,
                     username: str = Depends(auth.require_agent_permission("ticket.reply"))):
    """发送人工回复但保持人工接管，等待客户继续沟通。"""
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.get("status") not in ("escalated", "in_progress", "waiting_customer"):
        raise HTTPException(status_code=409, detail="该工单不是可处理状态")
    if ticket.get("assigned_to") and ticket.get("assigned_to") != username:
        raise HTTPException(status_code=409, detail="该工单正在由其他坐席处理")

    answer = (payload.human_answer or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="人工回答不能为空")
    if not db.update_ticket_reply(ticket_id, answer, username):
        raise HTTPException(status_code=409, detail="当前状态不允许此操作，或工单不属于当前坐席")
    db.insert_ticket_message(ticket_id, "agent", answer, username)
    if ticket.get("status") != "in_progress":
        db.log_status_change(ticket_id, "in_progress", username)
    events.broker.publish(
        "human_replied",
        {
            "ticket_id": ticket_id,
            "username": ticket.get("username"),
            "human_answer": answer,
            "status": "in_progress",
            "handoff_active": True,
        },
    )

    result = {
        "status": "ok",
        "id": ticket_id,
        "workflow_status": "in_progress",
        "handoff_active": True,
        "saved_to_kb": False,
    }
    if payload.save_to_kb:
        rag.add_entry(ticket["ticket_text"], answer)
        result["saved_to_kb"] = True
    return result


@app.post("/api/escalations/{ticket_id}/resolve")
def resolve_escalation(ticket_id: int, payload: ResolveRequest,
                       username: str = Depends(auth.require_agent_permission("ticket.resolve"))):
    """人工客服处理完毕：写入回答，并可把问答回流到知识库供 AI 学习。"""
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if ticket.get("status") not in ("escalated", "in_progress", "waiting_customer"):
        raise HTTPException(status_code=409, detail="该工单不是可处理状态")
    if ticket.get("assigned_to") and ticket.get("assigned_to") != username:
        raise HTTPException(status_code=409, detail="该工单正在由其他坐席处理")

    answer = (payload.human_answer or "").strip()
    if not answer:
        raise HTTPException(status_code=400, detail="人工回答不能为空")
    db.update_ticket_answer(ticket_id, answer, "resolved", operator=username)
    db.insert_ticket_message(ticket_id, "agent", answer, username)
    db.log_status_change(ticket_id, "resolved", username)
    events.broker.publish(
        "human_replied",
        {
            "ticket_id": ticket_id,
            "username": ticket.get("username"),
            "human_answer": answer,
            "status": "resolved",
            "handoff_active": False,
        },
    )

    result = {"status": "ok", "id": ticket_id, "saved_to_kb": False}
    if answer and payload.save_to_kb:
        rag.add_entry(ticket["ticket_text"], answer)
        result["saved_to_kb"] = True
    return result


@app.get("/api/metrics")
def get_metrics(username: str = Depends(auth.require_manager)):
    """经理仪表盘核心指标。"""
    return metrics.manager_dashboard()


@app.get("/api/stats")
def get_stats(username: str = Depends(auth.require_manager)):
    """后台统计：TOP 问题 + 情绪趋势 + 负反馈标签分布。"""
    tags = db.list_all_feedback_tags()
    tag_dist = dict(Counter(t["tag"] for t in tags if t.get("tag")))
    return {
        "top_questions": metrics.top_questions(10),
        "emotion_trend": metrics.emotion_trend(7),
        "feedback_tags": tag_dist,
    }


@app.post("/api/tickets/{ticket_id}/tag")
def tag_ticket(ticket_id: int, payload: FeedbackTagRequest,
               username: str = Depends(auth.require_agent_permission("ticket.reply"))):
    """人工坐席给错误工单打标签，系统返回一条优化建议（模块⑦）。"""
    ticket = db.get_ticket(ticket_id)
    if ticket is None:
        raise HTTPException(status_code=404, detail="工单不存在")
    if payload.tag not in feedback.TAG_LABELS:
        raise HTTPException(status_code=400, detail="标签只能是：" + "/".join(feedback.TAG_LABELS))
    db.add_feedback_tag(ticket_id, payload.tag, payload.note or "", username)
    db.insert_ticket_log(ticket_id, "feedback_tag", f"人工标注：{payload.tag}", username)
    suggestion = feedback.suggest(payload.tag, ticket)
    return {"status": "ok", "id": ticket_id, "tag": payload.tag, "suggestion": suggestion}


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
    redis_available = redis_store.get_client() is not None
    return {
        "status": "ok",
        "model": config.Config.MODEL,
        "api_key_configured": bool(config.Config.DEEPSEEK_API_KEY),
        "vision_model": config.Config.VISION_MODEL or None,
        "vision_configured": bool(config.Config.VISION_MODEL and config.Config.VISION_API_KEY),
        "redis_configured": bool(config.Config.REDIS_URL),
        "redis_available": redis_available,
    }
