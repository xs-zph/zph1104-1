"""数据库模块：用 MySQL 存储工单记录。

使用 pymysql 连接 MySQL（连接信息在 config.py / .env 中配置），
表结构简单直观，方便后续做效果统计（自动处理率、平均耗时等）。
"""
import logging
import queue
import threading
from datetime import datetime
from uuid import uuid4

import pymysql
from pymysql.cursors import DictCursor

from app import config

logger = logging.getLogger("app.db")

# 建表语句：工单表
_SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    ticket_text   TEXT NOT NULL,      -- 工单原文
    category      VARCHAR(64),        -- 系统分类结果
    ground_truth  VARCHAR(64),        -- 真实标签（仅评测/演示时填写）
    confidence    FLOAT,              -- 分类置信度 0~1
    reason        TEXT,               -- 分类理由（模型自述）
    status        VARCHAR(16),        -- auto（自动）/ escalated（人工）
    reply         TEXT,               -- 系统回复内容
    reply_source  VARCHAR(16),        -- template / rag / agent / chat / escalate / service_error
    human_answer  TEXT,               -- 人工客服的回答（升级工单处理后填写）
    latency_ms    INT,                -- 处理耗时（毫秒）
    phone         VARCHAR(32),        -- 客户联系电话（若有）
    device        VARCHAR(64),        -- 渠道/设备信息（web / wechat）
    emotion       VARCHAR(16),        -- 情绪识别结果：负面 / 中性 / 正面
    emotion_intensity VARCHAR(16),    -- 负面情绪强度：normal / extreme
    multi_intent  TINYINT(1) DEFAULT 0, -- 是否多诉求混杂（复合请求 → 转人工）
    rag_chunks    TEXT,               -- 命中的 RAG 片段（JSON 数组）
    route_trace   TEXT,               -- 路由决策链路（JSON，全链路留痕）
    priority      VARCHAR(16),        -- low / normal / high / urgent
    sla_breached  TINYINT(1) DEFAULT 0, -- SLA 已超时，后台扫描器幂等标记
    assigned_to   VARCHAR(64),        -- 当前处理坐席
    assigned_at   DATETIME,
    first_response_at DATETIME,
    resolved_at   DATETIME,
    sla_due_at    DATETIME,
    created_at    DATETIME            -- 创建时间
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 工单状态流转 / 审计日志表（每次状态变化、关键动作都留痕）
_TICKET_LOGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_logs (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    ticket_id     INT NOT NULL,        -- 关联工单
    action        VARCHAR(64),         -- 动作类型：created / escalated / resolved / feedback_tag ...
    detail        TEXT,                -- 动作详情
    operator      VARCHAR(64),         -- 操作者（系统 / 人工客服用户名）
    created_at    DATETIME,
    INDEX idx_logs_ticket (ticket_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 工单消息线程：保存客户补充、AI 回复和人工回复，保证人工接管后上下文连续
_TICKET_MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_messages (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    ticket_id       INT NOT NULL,
    sender_type     VARCHAR(16) NOT NULL,  -- customer / assistant / agent / system
    sender_username VARCHAR(64),
    content         TEXT NOT NULL,
    created_at      DATETIME NOT NULL,
    INDEX idx_messages_ticket (ticket_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 人工负反馈标注表（人工坐席对错误工单打标签，用于迭代优化）
_FEEDBACK_TAGS_SCHEMA = """
CREATE TABLE IF NOT EXISTS ticket_feedback_tags (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    ticket_id     INT NOT NULL,        -- 关联工单
    tag           VARCHAR(32),         -- 分类错误 / 知识库无答案 / AI回答有误 / 安抚不合适
    note          TEXT,                -- 人工补充说明
    operator      VARCHAR(64),         -- 标注人
    created_at    DATETIME,
    INDEX idx_fbt_ticket (ticket_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 知识库条目表（FAQ 的结构化源，支持软删除 / 编辑 / 审计）
_FAQ_ENTRIES_SCHEMA = """
CREATE TABLE IF NOT EXISTS faq_entries (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    question      TEXT NOT NULL,
    answer        TEXT NOT NULL,
    enabled       TINYINT(1) NOT NULL DEFAULT 1,  -- 1 启用 / 0 软删除
    created_at    DATETIME,
    updated_at    DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# FAQ 变更快照：保留操作者和内容版本，便于审计与人工回滚。
_FAQ_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS faq_audit_logs (
    id          BIGINT AUTO_INCREMENT PRIMARY KEY,
    faq_id      INT NOT NULL,
    action      VARCHAR(32) NOT NULL, -- created / updated / disabled / restored
    question    TEXT NOT NULL,
    answer      TEXT NOT NULL,
    enabled     TINYINT(1) NOT NULL,
    operator    VARCHAR(64) NOT NULL,
    created_at  DATETIME NOT NULL,
    INDEX idx_faq_audit (faq_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 用户表（登录用）
_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(128) NOT NULL,
    role          VARCHAR(16) NOT NULL DEFAULT 'customer',  -- admin(管理员) / manager(经理) / agent(客服) / customer(客户)
    active        TINYINT(1) NOT NULL DEFAULT 1,
    permissions   TEXT,
    phone         VARCHAR(32),
    phone_verified_at DATETIME,
    created_at    DATETIME
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

_ACCOUNT_AUDIT_SCHEMA = """
CREATE TABLE IF NOT EXISTS account_audit_logs (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    target_username VARCHAR(64) NOT NULL,
    action        VARCHAR(64) NOT NULL,
    detail        TEXT,
    operator      VARCHAR(64) NOT NULL,
    created_at    DATETIME NOT NULL,
    INDEX idx_account_audit_target (target_username),
    INDEX idx_account_audit_created (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 订单表：绑定到用户，用于「订单归属」校验（查订单/物流/退款时只返回本人订单）
_ORDERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    order_id      VARCHAR(64) NOT NULL UNIQUE,
    username      VARCHAR(64) NOT NULL,      -- 归属用户（对应 users.username）
    product       VARCHAR(128),
    status        VARCHAR(32),
    tracking_no   VARCHAR(64),
    logistics     VARCHAR(255),
    refund_status VARCHAR(255),
    created_at    DATETIME,
    INDEX idx_orders_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 用户实体画像事实表：保存可精确引用的稳定事实，不替代订单 / 工单业务表
_PROFILE_FACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS user_profile_facts (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(64) NOT NULL,
    entity_type   VARCHAR(32) NOT NULL,       -- person / device / preference / after_sale
    fact_key      VARCHAR(64) NOT NULL,       -- 如 preferred_device / favorite_category
    fact_value    VARCHAR(255) NOT NULL,
    source        VARCHAR(32) NOT NULL,       -- conversation / manual / demo
    evidence      TEXT,                       -- 脱敏后的原始依据，便于审计
    confidence    FLOAT NOT NULL DEFAULT 0,
    confirmed     TINYINT(1) NOT NULL DEFAULT 0,
    created_at    DATETIME,
    updated_at    DATETIME,
    UNIQUE KEY uq_profile_fact (username, entity_type, fact_key),
    INDEX idx_profile_username (username)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 中心式多 Agent 的工单级共享黑板；payload 保存协议 JSON，便于审计和重放。
_AGENT_BLACKBOARD_SCHEMA = """
CREATE TABLE IF NOT EXISTS agent_blackboard_entries (
    id            BIGINT AUTO_INCREMENT PRIMARY KEY,
    task_id       VARCHAR(96) NOT NULL,
    entry_type    VARCHAR(64) NOT NULL,
    source_agent  VARCHAR(64) NOT NULL,
    payload       LONGTEXT NOT NULL,
    confidence    FLOAT,
    created_at    DATETIME NOT NULL,
    INDEX idx_blackboard_task (task_id, id),
    INDEX idx_blackboard_source (source_agent, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 当前用户会话状态：新对话通过更换 conversation_id 隔离旧上下文。
_CONVERSATION_STATE_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_states (
    username       VARCHAR(64) PRIMARY KEY,
    conversation_id CHAR(36) NOT NULL,
    updated_at     DATETIME NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""

# 持久化短期对话记忆；只保存脱敏后的文本，保留最近 12 轮由 DAO 控制。
_CONVERSATION_MESSAGES_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversation_memory_messages (
    id              BIGINT AUTO_INCREMENT PRIMARY KEY,
    username        VARCHAR(64) NOT NULL,
    conversation_id CHAR(36) NOT NULL,
    role            VARCHAR(16) NOT NULL,
    content         TEXT NOT NULL,
    created_at      DATETIME NOT NULL,
    INDEX idx_conversation_memory (username, conversation_id, id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
"""


_DEFAULT_DB = object()


class _PooledConnection:
    """将连接 close 操作改为归还连接池，兼容现有 DAO 写法。"""

    def __init__(self, pool, raw):
        self._pool = pool
        self._raw = raw
        self._returned = False

    def __getattr__(self, name):
        return getattr(self._raw, name)

    def close(self):
        if not self._returned:
            self._returned = True
            self._pool.release(self._raw)


class _ConnectionPool:
    def __init__(self, maxsize: int):
        self._maxsize = max(1, maxsize)
        self._available = queue.LifoQueue(maxsize=self._maxsize)
        self._created = 0
        self._lock = threading.Lock()

    def acquire(self):
        try:
            raw = self._available.get_nowait()
        except queue.Empty:
            with self._lock:
                if self._created < self._maxsize:
                    self._created += 1
                    create = True
                else:
                    create = False
            if create:
                try:
                    raw = _new_connection()
                except Exception:
                    with self._lock:
                        self._created = max(0, self._created - 1)
                    raise
            else:
                raw = self._available.get(
                    timeout=config.Config.DB_POOL_TIMEOUT_SECONDS
                )
        return _PooledConnection(self, raw)

    def release(self, raw):
        try:
            raw.ping(reconnect=True)
            self._available.put(raw, timeout=config.Config.DB_POOL_TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            with self._lock:
                self._created = max(0, self._created - 1)
            try:
                raw.close()
            except Exception:
                pass


_pool = None
_pool_lock = threading.Lock()


def _new_connection(database=_DEFAULT_DB):
    kwargs = dict(
        host=config.Config.MYSQL_HOST,
        port=config.Config.MYSQL_PORT,
        user=config.Config.MYSQL_USER,
        password=config.Config.MYSQL_PASSWORD,
        charset="utf8mb4",
        cursorclass=DictCursor,
        autocommit=True,
    )
    if database is _DEFAULT_DB:
        database = config.Config.MYSQL_DB
    if database is not None:
        kwargs["database"] = database
    return pymysql.connect(**kwargs)


def _get_pool():
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _ConnectionPool(config.Config.DB_POOL_SIZE)
    return _pool


def _connect(database=_DEFAULT_DB):
    """建立 MySQL 连接。

    参数：
      database 为默认值时使用 .env 里的 MYSQL_DB；
      传 None 表示不指定数据库（用于「建库」阶段）。
    """
    if database is not _DEFAULT_DB:
        return _new_connection(database)
    return _get_pool().acquire()


def init_db():
    """初始化数据库（建库 + 建表）。启动时调用一次即可。"""
    # 1. 先创建数据库（若不存在）
    conn = _connect(database=None)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{config.Config.MYSQL_DB}` "
                "DEFAULT CHARACTER SET utf8mb4"
            )
    finally:
        conn.close()

    # 2. 再创建表
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_SCHEMA)
            cur.execute(_USERS_SCHEMA)
            cur.execute(_ACCOUNT_AUDIT_SCHEMA)
            cur.execute(_ORDERS_SCHEMA)
            cur.execute(_TICKET_LOGS_SCHEMA)
            cur.execute(_TICKET_MESSAGES_SCHEMA)
            cur.execute(_FEEDBACK_TAGS_SCHEMA)
            cur.execute(_FAQ_ENTRIES_SCHEMA)
            cur.execute(_FAQ_AUDIT_SCHEMA)
            cur.execute(_PROFILE_FACTS_SCHEMA)
            cur.execute(_AGENT_BLACKBOARD_SCHEMA)
            cur.execute(_CONVERSATION_STATE_SCHEMA)
            cur.execute(_CONVERSATION_MESSAGES_SCHEMA)
            _migrate(cur)
    finally:
        conn.close()


def _ensure_column(cur, table: str, column: str, ddl: str):
    """若某列不存在则添加（幂等：已存在则跳过）。"""
    cur.execute(
        "SELECT COUNT(*) AS c FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = %s AND TABLE_NAME = %s AND COLUMN_NAME = %s",
        (config.Config.MYSQL_DB, table, column),
    )
    if cur.fetchone()["c"] == 0:
        cur.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")


def _migrate(cur):
    """给老库补充新增字段（幂等：字段已存在则跳过）。"""
    _ensure_column(cur, "tickets", "human_answer", "human_answer TEXT")
    _ensure_column(cur, "tickets", "feedback", "feedback VARCHAR(8)")
    _ensure_column(cur, "tickets", "username", "username VARCHAR(64)")
    _ensure_column(cur, "tickets", "phone", "phone VARCHAR(32)")
    _ensure_column(cur, "tickets", "device", "device VARCHAR(64)")
    _ensure_column(cur, "tickets", "emotion", "emotion VARCHAR(16)")
    _ensure_column(cur, "tickets", "emotion_intensity", "emotion_intensity VARCHAR(16)")
    _ensure_column(cur, "tickets", "multi_intent", "multi_intent TINYINT(1) DEFAULT 0")
    _ensure_column(cur, "tickets", "rag_chunks", "rag_chunks TEXT")
    _ensure_column(cur, "tickets", "route_trace", "route_trace TEXT")
    _ensure_column(cur, "tickets", "priority", "priority VARCHAR(16)")
    _ensure_column(cur, "tickets", "sla_breached", "sla_breached TINYINT(1) DEFAULT 0")
    _ensure_column(cur, "tickets", "assigned_to", "assigned_to VARCHAR(64)")
    _ensure_column(cur, "tickets", "assigned_at", "assigned_at DATETIME")
    _ensure_column(cur, "tickets", "first_response_at", "first_response_at DATETIME")
    _ensure_column(cur, "tickets", "resolved_at", "resolved_at DATETIME")
    _ensure_column(cur, "tickets", "sla_due_at", "sla_due_at DATETIME")
    _ensure_column(cur, "users", "active", "active TINYINT(1) NOT NULL DEFAULT 1")
    _ensure_column(cur, "users", "permissions", "permissions TEXT")
    _ensure_column(cur, "users", "phone", "phone VARCHAR(32)")
    _ensure_column(cur, "users", "phone_verified_at", "phone_verified_at DATETIME")
    cur.execute(
        "UPDATE tickets SET priority = COALESCE(priority, 'normal'), "
        "sla_due_at = COALESCE(sla_due_at, TIMESTAMPADD(MINUTE, %s, created_at)) "
        "WHERE status IN ('escalated', 'in_progress', 'waiting_customer')",
        (config.Config.DEFAULT_SLA_MINUTES,),
    )


def mark_overdue_tickets(limit: int = 100) -> list[dict]:
    """原子标记逾期人工工单，返回本次首次被标记的工单。"""
    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, status, assigned_to, sla_due_at "
                "FROM tickets WHERE status IN "
                "('escalated', 'in_progress', 'waiting_customer') "
                "AND sla_due_at IS NOT NULL AND sla_due_at <= NOW() "
                "AND COALESCE(sla_breached, 0) = 0 "
                "ORDER BY sla_due_at ASC LIMIT %s FOR UPDATE",
                (limit,),
            )
            rows = list(cur.fetchall())
            if rows:
                placeholders = ", ".join(["%s"] * len(rows))
                cur.execute(
                    "UPDATE tickets SET sla_breached = 1, priority = 'urgent' "
                    f"WHERE id IN ({placeholders}) "
                    "AND status IN ('escalated', 'in_progress', 'waiting_customer') "
                    "AND COALESCE(sla_breached, 0) = 0",
                    [row["id"] for row in rows],
                )
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def insert_ticket(**fields) -> int:
    """插入一张工单，返回自增 id。"""
    fields.setdefault("created_at", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    columns = ", ".join(fields.keys())
    placeholders = ", ".join(["%s"] * len(fields))
    sql = f"INSERT INTO tickets ({columns}) VALUES ({placeholders})"
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, list(fields.values()))
            return cur.lastrowid
    finally:
        conn.close()


def get_ticket(ticket_id: int) -> dict | None:
    """按 id 查询单张工单。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tickets WHERE id = %s", (ticket_id,))
            return cur.fetchone()
    finally:
        conn.close()


def get_ticket_for_user(username: str, ticket_id: int) -> dict | None:
    """按 id 查询当前用户自己的工单。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tickets WHERE id = %s AND username = %s",
                (ticket_id, username),
            )
            return cur.fetchone()
    finally:
        conn.close()


def list_tickets(limit: int = 50, username: str | None = None) -> list[dict]:
    """查询最近的工单；传 username 时只查询该用户自己的工单。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if username:
                cur.execute(
                    "SELECT * FROM tickets WHERE username = %s "
                    "ORDER BY id DESC LIMIT %s",
                    (username, limit),
                )
            else:
                cur.execute("SELECT * FROM tickets ORDER BY id DESC LIMIT %s", (limit,))
            return cur.fetchall()
    finally:
        conn.close()


def list_all_tickets() -> list[dict]:
    """返回全部工单（供统计 / 复盘脚本使用）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM tickets ORDER BY id ASC")
            return cur.fetchall()
    finally:
        conn.close()


def list_escalations(limit: int = 50) -> list[dict]:
    """查询所有人工工单（含处理中、等待客户、已解决和已关闭）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tickets WHERE status IN "
                "('escalated', 'in_progress', 'waiting_customer', 'resolved', 'closed') "
                "ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            tickets = cur.fetchall()
            for ticket in tickets:
                ticket["messages"] = _list_ticket_messages(cur, ticket["id"])
            return tickets
    finally:
        conn.close()


def get_active_human_ticket_for_user(username: str) -> dict | None:
    """查询当前用户最新的未结束人工工单。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tickets WHERE username = %s AND status IN "
                "('escalated', 'in_progress', 'waiting_customer') "
                "ORDER BY id DESC LIMIT 1",
                (username,),
            )
            return cur.fetchone()
    finally:
        conn.close()


def close_active_human_tickets(username: str) -> list[int]:
    """原子结束某个客户全部未结束的人工会话，并返回关闭的工单 ID。"""
    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id FROM tickets WHERE username = %s AND status IN "
                "('escalated', 'in_progress', 'waiting_customer') "
                "ORDER BY id DESC FOR UPDATE",
                (username,),
            )
            ticket_ids = [row["id"] for row in cur.fetchall()]
            if ticket_ids:
                placeholders = ", ".join(["%s"] * len(ticket_ids))
                cur.execute(
                    "UPDATE tickets SET status = 'closed', resolved_at = NOW() "
                    f"WHERE id IN ({placeholders})",
                    ticket_ids,
                )
        conn.commit()
        return ticket_ids
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def close_idle_human_tickets(
    idle_minutes: int | None = None, limit: int = 100,
) -> list[dict]:
    """原子结束长时间没有客户消息的人工工单。"""
    idle_minutes = (
        config.Config.HUMAN_IDLE_TIMEOUT_MINUTES
        if idle_minutes is None else idle_minutes
    )
    if idle_minutes <= 0:
        return []
    limit = max(1, min(int(limit), 1000))

    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT t.id, t.username, t.status, t.assigned_to, "
                "COALESCE(last_customer.last_customer_activity_at, t.created_at) "
                "AS last_customer_activity_at "
                "FROM tickets t "
                "LEFT JOIN ("
                "SELECT ticket_id, MAX(created_at) AS last_customer_activity_at "
                "FROM ticket_messages WHERE sender_type = 'customer' "
                "GROUP BY ticket_id"
                ") last_customer ON last_customer.ticket_id = t.id "
                "WHERE t.status IN ('escalated', 'in_progress', 'waiting_customer') "
                "AND COALESCE(last_customer.last_customer_activity_at, t.created_at) "
                "<= DATE_SUB(NOW(), INTERVAL %s MINUTE) "
                "ORDER BY last_customer_activity_at ASC LIMIT %s FOR UPDATE",
                (idle_minutes, limit),
            )
            rows = list(cur.fetchall())
            if rows:
                placeholders = ", ".join(["%s"] * len(rows))
                cur.execute(
                    "UPDATE tickets SET status = 'closed', resolved_at = NOW() "
                    f"WHERE id IN ({placeholders}) "
                    "AND status IN ('escalated', 'in_progress', 'waiting_customer')",
                    [row["id"] for row in rows],
                )
                for row in rows:
                    row["status"] = "closed"
        conn.commit()
        return rows
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def count_by_status() -> dict:
    """统计自动处理 / 人工升级的数量。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) AS n FROM tickets GROUP BY status")
            rows = cur.fetchall()
    finally:
        conn.close()
    return {r["status"]: r["n"] for r in rows}


def create_user(username: str, password_hash: str, role: str = "customer",
                permissions: list[str] | None = None, phone: str | None = None) -> int:
    """创建用户，返回自增 id。"""
    import json

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, role, permissions, phone, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (username, password_hash, role,
                 json.dumps(permissions or [], ensure_ascii=False),
                 phone,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            return cur.lastrowid
    finally:
        conn.close()


def get_user_by_username(username: str) -> dict | None:
    """按用户名查询用户。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE username = %s", (username,))
            return cur.fetchone()
    finally:
        conn.close()


def list_staff_users() -> list[dict]:
    """返回客服账号列表，不返回密码哈希。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, role, active, permissions, phone, phone_verified_at, created_at "
                "FROM users WHERE role = 'agent' AND active = 1 "
                "ORDER BY username ASC"
            )
            rows = cur.fetchall()
            for row in rows:
                row["permissions"] = _decode_permissions(row.get("permissions"))
            return rows
    finally:
        conn.close()


def list_admin_users() -> list[dict]:
    """兼容旧调用名；新逻辑只返回启用中的客服账号。"""
    return list_staff_users()


def _decode_permissions(value) -> list[str]:
    """将用户权限 JSON 解码为稳定的列表。"""
    import json

    if not value:
        return []
    if isinstance(value, (list, tuple)):
        return list(value)
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def list_users() -> list[dict]:
    """列出账号管理所需字段，绝不返回密码哈希。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, username, role, active, permissions, phone, phone_verified_at, created_at "
                "FROM users ORDER BY role, username"
            )
            rows = cur.fetchall()
            for row in rows:
                row["active"] = bool(row.get("active", 1))
                row["permissions"] = _decode_permissions(row.get("permissions"))
            return rows
    finally:
        conn.close()


def update_user_account(username: str, role: str, active: bool,
                        permissions: list[str], phone: str | None = None) -> bool:
    """更新账号角色、启用状态、权限和管理员绑定的手机号。"""
    import json

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET role = %s, active = %s, permissions = %s, "
                "phone = COALESCE(%s, phone) "
                "WHERE username = %s",
                (role, 1 if active else 0, json.dumps(permissions, ensure_ascii=False),
                 phone, username),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def insert_account_audit(target_username: str, action: str, detail: str,
                         operator: str) -> None:
    """记录账号和权限变更，便于经理追溯。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO account_audit_logs "
                "(target_username, action, detail, operator, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (target_username, action, detail, operator,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
    finally:
        conn.close()


def list_account_audits(limit: int = 100) -> list[dict]:
    """返回最近账号变更记录。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, target_username, action, detail, operator, created_at "
                "FROM account_audit_logs ORDER BY id DESC LIMIT %s", (limit,)
            )
            return cur.fetchall()
    finally:
        conn.close()


def update_user_password(username: str, password_hash: str) -> bool:
    """更新用户密码哈希，供旧密码格式登录成功后迁移。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET password_hash = %s WHERE username = %s",
                (password_hash, username),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def set_user_phone_if_empty(username: str, phone: str) -> bool:
    """仅为演示账号补齐空手机号，不覆盖管理员已维护的号码。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET phone = %s WHERE username = %s "
                "AND (phone IS NULL OR phone = '')",
                (phone, username),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def mark_user_phone_verified(username: str, phone: str) -> bool:
    """记录手机号核验结果；调用方负责先校验角色绑定规则。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET phone = %s, phone_verified_at = %s "
                "WHERE username = %s AND active = 1",
                (phone, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), username),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def update_ticket_status(ticket_id: int, status: str) -> None:
    """更新工单状态（如：人工处理后标记为 resolved）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE tickets SET status = %s WHERE id = %s",
                        (status, ticket_id))
    finally:
        conn.close()


def update_ticket_answer(ticket_id: int, human_answer: str, status: str = "resolved",
                         operator: str | None = None) -> None:
    """人工客服处理升级工单：写入人工回答，并把状态标记为已处理。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tickets SET status = %s, human_answer = %s, "
                "assigned_to = COALESCE(assigned_to, %s), "
                "assigned_at = COALESCE(assigned_at, NOW()), "
                "first_response_at = COALESCE(first_response_at, NOW()), "
                "resolved_at = NOW() WHERE id = %s",
                (status, human_answer, operator, ticket_id),
            )
    finally:
        conn.close()


def update_ticket_reply(ticket_id: int, human_answer: str, operator: str) -> bool:
    """发送人工回复但保持人工接管，只有当前坐席或未分配工单可操作。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tickets SET status = 'in_progress', human_answer = %s, "
                "assigned_to = COALESCE(NULLIF(assigned_to, ''), %s), "
                "assigned_at = COALESCE(assigned_at, NOW()), "
                "first_response_at = COALESCE(first_response_at, NOW()), "
                "resolved_at = NULL WHERE id = %s "
                "AND status IN ('escalated', 'in_progress', 'waiting_customer') "
                "AND (assigned_to IS NULL OR assigned_to = '' OR assigned_to = %s)",
                (human_answer, operator, ticket_id, operator),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def insert_ticket_message(ticket_id: int, sender_type: str, content: str,
                          sender_username: str | None = None) -> int:
    """保存工单消息，不保存图片二进制，只保存文本化内容。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ticket_messages "
                "(ticket_id, sender_type, sender_username, content, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (ticket_id, sender_type, sender_username, content,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            return cur.lastrowid
    finally:
        conn.close()


def _list_ticket_messages(cur, ticket_id: int) -> list[dict]:
    """使用已有游标读取工单消息，供升级工单列表复用。"""
    cur.execute(
        "SELECT id, ticket_id, sender_type, sender_username, content, created_at "
        "FROM ticket_messages WHERE ticket_id = %s ORDER BY id ASC",
        (ticket_id,),
    )
    return cur.fetchall()


def list_ticket_messages(ticket_id: int) -> list[dict]:
    """读取一张工单的完整消息线程。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            return _list_ticket_messages(cur, ticket_id)
    finally:
        conn.close()


def list_conversation_history(username: str, max_messages: int = 24) -> list[dict]:
    """读取用户当前会话的最近消息，按时间正序返回。"""
    max_messages = max(1, min(int(max_messages), 100))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT conversation_id FROM conversation_states WHERE username = %s",
                (username,),
            )
            state = cur.fetchone()
            if not state:
                return []
            cur.execute(
                "SELECT role, content FROM conversation_memory_messages "
                "WHERE username = %s AND conversation_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (username, state["conversation_id"], max_messages),
            )
            rows = cur.fetchall()
            return list(reversed(rows))
    finally:
        conn.close()


def append_conversation_message(username: str, role: str, content: str,
                                max_messages: int = 24) -> None:
    """向当前会话追加消息并裁剪旧上下文，事务内创建会话状态。"""
    if role not in {"user", "assistant"}:
        raise ValueError("会话消息角色不合法")
    max_messages = max(1, min(int(max_messages), 100))
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT conversation_id FROM conversation_states WHERE username = %s FOR UPDATE",
                (username,),
            )
            state = cur.fetchone()
            conversation_id = state["conversation_id"] if state else str(uuid4())
            if state is None:
                cur.execute(
                    "INSERT INTO conversation_states (username, conversation_id, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (username, conversation_id, now),
                )
            else:
                cur.execute(
                    "UPDATE conversation_states SET updated_at = %s WHERE username = %s",
                    (now, username),
                )
            cur.execute(
                "INSERT INTO conversation_memory_messages "
                "(username, conversation_id, role, content, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (username, conversation_id, role, content, now),
            )
            cur.execute(
                "SELECT id FROM conversation_memory_messages "
                "WHERE username = %s AND conversation_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (username, conversation_id, max_messages + 1),
            )
            stale_ids = [row["id"] for row in cur.fetchall()][max_messages:]
            if stale_ids:
                placeholders = ", ".join(["%s"] * len(stale_ids))
                cur.execute(
                    "DELETE FROM conversation_memory_messages "
                    f"WHERE username = %s AND conversation_id = %s AND id IN ({placeholders})",
                    [username, conversation_id, *stale_ids],
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def clear_conversation_memory(username: str) -> None:
    """切换到新会话并删除旧会话消息，避免新对话继承旧上下文。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    new_conversation_id = str(uuid4())
    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT conversation_id FROM conversation_states WHERE username = %s FOR UPDATE",
                (username,),
            )
            state = cur.fetchone()
            if state:
                cur.execute(
                    "DELETE FROM conversation_memory_messages WHERE username = %s AND conversation_id = %s",
                    (username, state["conversation_id"]),
                )
                cur.execute(
                    "UPDATE conversation_states SET conversation_id = %s, updated_at = %s "
                    "WHERE username = %s",
                    (new_conversation_id, now, username),
                )
            else:
                cur.execute(
                    "INSERT INTO conversation_states (username, conversation_id, updated_at) "
                    "VALUES (%s, %s, %s)",
                    (username, new_conversation_id, now),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def append_customer_message(ticket_id: int, username: str, content: str) -> dict | None:
    """把客户补充消息追加到未结束人工工单，并唤醒等待客户状态。"""
    conn = _connect()
    try:
        conn.begin()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tickets WHERE id = %s AND username = %s "
                "AND status IN ('escalated', 'in_progress', 'waiting_customer') "
                "FOR UPDATE",
                (ticket_id, username),
            )
            ticket = cur.fetchone()
            if ticket is None:
                conn.rollback()
                return None

            if ticket.get("status") == "waiting_customer":
                cur.execute(
                    "UPDATE tickets SET status = 'in_progress' WHERE id = %s",
                    (ticket_id,),
                )
                ticket["status"] = "in_progress"

            cur.execute(
                "INSERT INTO ticket_messages "
                "(ticket_id, sender_type, sender_username, content, created_at) "
                "VALUES (%s, 'customer', %s, %s, %s)",
                (ticket_id, username, content,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
        conn.commit()
        return ticket
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def claim_ticket(ticket_id: int, username: str) -> bool:
    """原子接单：只有未分配的待处理工单可以被抢占。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tickets SET status = 'in_progress', assigned_to = %s, "
                "assigned_at = COALESCE(assigned_at, NOW()), "
                "first_response_at = COALESCE(first_response_at, NOW()) "
                "WHERE id = %s AND status = 'escalated' "
                "AND (assigned_to IS NULL OR assigned_to = '')",
                (username, ticket_id),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def transfer_ticket(ticket_id: int, current_user: str, target_user: str) -> bool:
    """原子转派：未分配或当前坐席持有的非终态工单才允许转派。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tickets SET status = 'in_progress', assigned_to = %s, "
                "assigned_at = COALESCE(assigned_at, NOW()) "
                "WHERE id = %s AND status IN ('escalated', 'in_progress', 'waiting_customer') "
                "AND (assigned_to IS NULL OR assigned_to = '' OR assigned_to = %s)",
                (target_user, ticket_id, current_user),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def update_ticket_workflow_status(ticket_id: int, current_user: str, status: str) -> bool:
    """原子更新协作状态，只有当前处理坐席可以推进工单。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if status == "waiting_customer":
                cur.execute(
                    "UPDATE tickets SET status = %s WHERE id = %s "
                    "AND status = 'in_progress' AND assigned_to = %s",
                    (status, ticket_id, current_user),
                )
            elif status == "in_progress":
                cur.execute(
                    "UPDATE tickets SET status = %s WHERE id = %s "
                    "AND status = 'waiting_customer' AND assigned_to = %s",
                    (status, ticket_id, current_user),
                )
            elif status == "closed":
                cur.execute(
                    "UPDATE tickets SET status = %s WHERE id = %s "
                    "AND status = 'resolved' AND assigned_to = %s",
                    (status, ticket_id, current_user),
                )
            else:
                return False
            return cur.rowcount > 0
    finally:
        conn.close()


# ---------------- 订单（归属校验） ----------------

def insert_order(order_id: str, username: str, product: str, status: str,
                 tracking_no: str | None, logistics: str | None,
                 refund_status: str | None) -> int:
    """插入一条订单，返回自增 id。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO orders (order_id, username, product, status, tracking_no, "
                "logistics, refund_status, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                (order_id, username, product, status, tracking_no, logistics,
                 refund_status, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            return cur.lastrowid
    finally:
        conn.close()


def get_order_by_id(order_id: str) -> dict | None:
    """按订单号查询订单（不校验归属，用于未登录/微信等无身份场景）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders WHERE order_id = %s", (order_id,))
            return cur.fetchone()
    finally:
        conn.close()


def get_order_for_user(username: str, order_id: str) -> dict | None:
    """按订单号查询订单，并校验订单归属当前用户（核心的「这是他的订单」判断）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM orders WHERE order_id = %s AND username = %s",
                (order_id, username),
            )
            return cur.fetchone()
    finally:
        conn.close()


def list_orders_for_user(username: str) -> list[dict]:
    """列出某个用户名下的所有订单。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM orders WHERE username = %s ORDER BY id",
                (username,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def list_all_orders() -> list[dict]:
    """列出全部订单（仅用于未登录场景的兜底查询）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM orders ORDER BY id")
            return cur.fetchall()
    finally:
        conn.close()


def update_order(order_id: str, **fields) -> dict | None:
    """更新订单（按订单号），只允许更新白名单字段。返回更新后的订单。"""
    allowed = {"username", "product", "status", "tracking_no", "logistics", "refund_status"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return get_order_by_id(order_id)
    cols = ", ".join(f"{k} = %s" for k in updates)
    sql = f"UPDATE orders SET {cols} WHERE order_id = %s"
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, list(updates.values()) + [order_id])
        return get_order_by_id(order_id)
    finally:
        conn.close()


def delete_order(order_id: str) -> bool:
    """删除订单（按订单号），返回是否删除成功。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM orders WHERE order_id = %s", (order_id,))
            return cur.rowcount > 0
    finally:
        conn.close()


def seed_orders() -> None:
    """初始化可重复执行的演示订单（仅当订单号不存在时插入）。

    数据覆盖两个客户和常见售后状态，方便直接验证 MCP 的订单归属、物流和
    退款查询。该函数只插入，不会覆盖管理员在后台修改过的测试订单。
    """
    sample = [
        ("A20240812001", "user", "无线蓝牙耳机", "已发货",
         "SF1234567890", "已到达长沙分拨中心，预计明天送达", None),
        ("A20240815002", "user", "智能空气炸锅", "待发货",
         None, None, None),
        ("A20240720003", "user", "运动跑鞋", "已完成",
         "YT9876543210", "已签收", "退款处理中，预计 1-3 个工作日到账"),
        ("A20240801004", "user", "保温杯", "已退货",
         None, None, "退款已到账"),
        ("B20240901001", "demo_user", "智能手表", "已发货",
         "YT2000000001", "已到达武汉转运中心，预计 2 天送达", None),
        ("B20240902002", "demo_user", "机械键盘", "待发货",
         None, None, None),
        ("B20240903003", "demo_user", "扫地机器人", "已完成",
         "JD2000000003", "已签收", "退款审核中，预计 3-5 个工作日到账"),
        ("B20240904004", "demo_user", "手机支架", "已取消",
         None, None, None),
    ]
    for order_id, username, product, status, tracking_no, logistics, refund_status in sample:
        if get_order_by_id(order_id) is None:
            insert_order(order_id, username, product, status, tracking_no,
                         logistics, refund_status)
            logger.info("初始化演示订单：%s（用户 %s）", order_id, username)


def seed_profile_facts() -> None:
    """初始化可重复执行的演示画像事实，不覆盖用户已有画像。"""
    sample = [
        ("user", "device", "preferred_device", "无线蓝牙耳机"),
        ("user", "preference", "favorite_category", "数码配件"),
        ("demo_user", "device", "preferred_device", "智能手表"),
        ("demo_user", "after_sale", "issue", "扫地机器人退款审核中"),
    ]
    for username, entity_type, fact_key, fact_value in sample:
        if get_profile_fact(username, entity_type, fact_key) is None:
            upsert_profile_fact(
                username=username,
                entity_type=entity_type,
                fact_key=fact_key,
                fact_value=fact_value,
                source="demo",
                evidence="本地 P0 演示数据",
                confidence=1.0,
                confirmed=True,
            )
            logger.info("初始化演示画像：%s.%s（用户 %s）", entity_type, fact_key, username)


def update_ticket_feedback(ticket_id: int, feedback: str,
                           username: str | None = None) -> bool:
    """记录满意度；传 username 时同时校验工单归属。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if username:
                cur.execute(
                    "UPDATE tickets SET feedback = %s "
                    "WHERE id = %s AND username = %s",
                    (feedback, ticket_id, username),
                )
            else:
                cur.execute("UPDATE tickets SET feedback = %s WHERE id = %s",
                            (feedback, ticket_id))
            return cur.rowcount > 0
    finally:
        conn.close()


def list_tickets_for_user(username: str, limit: int = 20) -> list[dict]:
    """列出某个用户名下的工单（供「查我的工单进度」自助服务）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, ticket_text, category, status, created_at "
                "FROM tickets WHERE username = %s ORDER BY id DESC LIMIT %s",
                (username, limit),
            )
            return cur.fetchall()
    finally:
        conn.close()


def cancel_order(username: str, order_id: str) -> dict | None:
    """取消用户自己的订单（仅「待发货」可在线取消）。返回更新后的订单或 None。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE orders SET status = '已取消' "
                "WHERE order_id = %s AND username = %s AND status = '待发货'",
                (order_id, username),
            )
            if cur.rowcount == 0:
                return None
        return get_order_for_user(username, order_id)
    finally:
        conn.close()


# ---------------- 审计日志 / 状态流转（全链路留痕） ----------------

def insert_ticket_log(ticket_id: int, action: str, detail: str = "",
                      operator: str | None = None) -> int:
    """给某张工单写一条审计/状态流转日志，返回日志自增 id。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ticket_logs (ticket_id, action, detail, operator, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (ticket_id, action, detail, operator,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            return cur.lastrowid
    finally:
        conn.close()


def list_ticket_logs(ticket_id: int) -> list[dict]:
    """按工单查询其全部审计日志（时间正序）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ticket_logs WHERE ticket_id = %s ORDER BY id ASC",
                (ticket_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def log_status_change(ticket_id: int, to_status: str, operator: str | None = None) -> int:
    """记录一次工单状态流转（便于追溯「待 AI → 人工 → 已完结」的完整轨迹）。"""
    return insert_ticket_log(ticket_id, "status_change", f"状态变更为 {to_status}", operator)


# ---------------- 多维度工单检索 ----------------

def search_tickets(keyword: str | None = None, category: str | None = None,
                   status: str | None = None, emotion: str | None = None,
                   username: str | None = None, limit: int = 100) -> list[dict]:
    """多维度检索工单：按关键词 / 分类 / 状态 / 情绪 / 用户名筛选。

    全部条件可选，未传则不过滤；关键词对「工单原文」做模糊匹配。
    """
    clauses = []
    params: list = []
    if keyword:
        clauses.append("ticket_text LIKE %s")
        params.append(f"%{keyword}%")
    if category:
        clauses.append("category = %s")
        params.append(category)
    if status:
        clauses.append("status = %s")
        params.append(status)
    if emotion:
        clauses.append("emotion = %s")
        params.append(emotion)
    if username:
        clauses.append("username = %s")
        params.append(username)

    where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"SELECT * FROM tickets {where} ORDER BY id DESC LIMIT %s"
    params.append(limit)

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


# ---------------- 人工负反馈标注（迭代优化） ----------------

def add_feedback_tag(ticket_id: int, tag: str, note: str = "",
                     operator: str | None = None) -> int:
    """人工坐席给错误工单打一个标签（分类错误 / 知识库无答案 / AI回答有误 / 安抚不合适）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ticket_feedback_tags (ticket_id, tag, note, operator, created_at) "
                "VALUES (%s, %s, %s, %s, %s)",
                (ticket_id, tag, note, operator,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            return cur.lastrowid
    finally:
        conn.close()


def list_feedback_tags(ticket_id: int) -> list[dict]:
    """查询某张工单上的人工标注标签。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM ticket_feedback_tags WHERE ticket_id = %s ORDER BY id ASC",
                (ticket_id,),
            )
            return cur.fetchall()
    finally:
        conn.close()


def list_all_feedback_tags(limit: int = 200) -> list[dict]:
    """查询全部人工标注（供后台统计「哪类错误最多」）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM ticket_feedback_tags ORDER BY id DESC LIMIT %s",
                        (limit,))
            return cur.fetchall()
    finally:
        conn.close()


# ---------------- 知识库条目（FAQ 结构化源，软删除 / 编辑） ----------------

def list_faq_entries(enabled_only: bool = False) -> list[dict]:
    """列出知识库条目。enabled_only=True 时只返回启用中的条目。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            if enabled_only:
                cur.execute("SELECT * FROM faq_entries WHERE enabled = 1 ORDER BY id")
            else:
                cur.execute("SELECT * FROM faq_entries ORDER BY id")
            return cur.fetchall()
    finally:
        conn.close()


def get_faq_entry(faq_id: int) -> dict | None:
    """按 id 查询单条知识库条目。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM faq_entries WHERE id = %s", (faq_id,))
            return cur.fetchone()
    finally:
        conn.close()


def insert_faq_entry(question: str, answer: str) -> int:
    """新增一条知识库条目，返回自增 id。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO faq_entries (question, answer, enabled, created_at, updated_at) "
                "VALUES (%s, %s, 1, %s, %s)",
                (question, answer, now, now),
            )
            return cur.lastrowid
    finally:
        conn.close()


def update_faq_entry(faq_id: int, question: str | None = None,
                     answer: str | None = None) -> dict | None:
    """更新知识库条目的问题/答案（只更新非空字段），返回更新后的条目。"""
    updates = {}
    if question is not None:
        updates["question"] = question
    if answer is not None:
        updates["answer"] = answer
    if updates:
        updates["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cols = ", ".join(f"{k} = %s" for k in updates)
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute(f"UPDATE faq_entries SET {cols} WHERE id = %s",
                            list(updates.values()) + [faq_id])
        finally:
            conn.close()
    return get_faq_entry(faq_id)


def set_faq_enabled(faq_id: int, enabled: bool) -> dict | None:
    """启用 / 软删除一条知识库条目（软删除 = enabled 置 0，不物理删数据）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE faq_entries SET enabled = %s, updated_at = %s WHERE id = %s",
                (1 if enabled else 0,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), faq_id),
            )
    finally:
        conn.close()
    return get_faq_entry(faq_id)


def clear_faq_entries() -> None:
    """清空知识库条目表（重置知识库时用）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM faq_entries")
    finally:
        conn.close()


def count_faq_entries() -> int:
    """知识库条目总数（含软删除）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS n FROM faq_entries")
            return cur.fetchone()["n"]
    finally:
        conn.close()


def insert_faq_audit(faq_id: int, action: str, question: str, answer: str,
                     enabled: bool, operator: str) -> int:
    """记录 FAQ 内容快照和变更操作者。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO faq_audit_logs "
                "(faq_id, action, question, answer, enabled, operator, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (faq_id, action, question, answer, 1 if enabled else 0, operator,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
            )
            return cur.lastrowid
    finally:
        conn.close()


def list_faq_audits(faq_id: int, limit: int = 50) -> list[dict]:
    """按知识条目查询变更历史，最新版本在前。"""
    limit = max(1, min(int(limit), 200))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM faq_audit_logs WHERE faq_id = %s "
                "ORDER BY id DESC LIMIT %s",
                (faq_id, limit),
            )
            return cur.fetchall()
    finally:
        conn.close()


# ---------------- 用户实体画像事实 ----------------

def list_profile_facts(username: str, include_unconfirmed: bool = False) -> list[dict]:
    """列出用户画像事实；默认只返回可供模型精确引用的事实。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            sql = (
                "SELECT * FROM user_profile_facts WHERE username = %s "
                + ("" if include_unconfirmed else "AND (confirmed = 1 OR confidence >= 0.8) ")
                + "ORDER BY entity_type, fact_key"
            )
            cur.execute(sql, (username,))
            return cur.fetchall()
    finally:
        conn.close()


def get_profile_fact(username: str, entity_type: str, fact_key: str) -> dict | None:
    """按用户、实体类型和事实键查询画像事实。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM user_profile_facts "
                "WHERE username = %s AND entity_type = %s AND fact_key = %s",
                (username, entity_type, fact_key),
            )
            return cur.fetchone()
    finally:
        conn.close()


def upsert_profile_fact(username: str, entity_type: str, fact_key: str,
                        fact_value: str, source: str, evidence: str,
                        confidence: float, confirmed: bool) -> dict:
    """新增或更新一条画像事实，唯一键保证同一事实不会重复。"""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO user_profile_facts "
                "(username, entity_type, fact_key, fact_value, source, evidence, confidence, confirmed, created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
                "ON DUPLICATE KEY UPDATE fact_value = VALUES(fact_value), "
                "source = VALUES(source), evidence = VALUES(evidence), "
                "confidence = VALUES(confidence), confirmed = VALUES(confirmed), "
                "updated_at = VALUES(updated_at)",
                (username, entity_type, fact_key, fact_value, source, evidence,
                 confidence, 1 if confirmed else 0, now, now),
            )
    finally:
        conn.close()
    return get_profile_fact(username, entity_type, fact_key) or {}


def delete_profile_fact(username: str, fact_id: int) -> bool:
    """删除当前用户自己的画像事实。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "DELETE FROM user_profile_facts WHERE id = %s AND username = %s",
                (fact_id, username),
            )
            return cur.rowcount > 0
    finally:
        conn.close()


def update_profile_fact(username: str, fact_id: int, fact_value: str,
                        confirmed: bool = True) -> dict | None:
    """手工修正当前用户的画像事实，并标记为已确认。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_profile_facts SET fact_value = %s, source = 'manual', "
                "confidence = 1.0, confirmed = %s, updated_at = %s "
                "WHERE id = %s AND username = %s",
                (fact_value, 1 if confirmed else 0,
                 datetime.now().strftime("%Y-%m-%d %H:%M:%S"), fact_id, username),
            )
    finally:
        conn.close()

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM user_profile_facts WHERE id = %s AND username = %s",
                (fact_id, username),
            )
            return cur.fetchone()
    finally:
        conn.close()


# ---------------- 中心式多 Agent 共享黑板 ----------------

def insert_blackboard_entry(task_id: str, entry_type: str,
                            payload: dict, source_agent: str,
                            confidence: float | None = None) -> dict:
    """写入黑板；数据库层也拒绝非主 Agent 写入。"""
    import json

    if source_agent != "main_agent":
        raise ValueError("只有主 Agent 可以写入共享黑板")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO agent_blackboard_entries "
                "(task_id, entry_type, source_agent, payload, confidence, created_at) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (task_id, entry_type, source_agent,
                 json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                 confidence, now),
            )
            return {
                "id": cur.lastrowid,
                "task_id": task_id,
                "entry_type": entry_type,
                "source_agent": source_agent,
                "payload": payload,
                "confidence": confidence,
                "created_at": now,
            }
    finally:
        conn.close()


def list_blackboard_entries(task_id: str, limit: int = 100) -> list[dict]:
    """读取指定工单黑板快照，返回已解析的协议 JSON 内容。"""
    import json

    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, task_id, entry_type, source_agent, payload, confidence, created_at "
                "FROM agent_blackboard_entries WHERE task_id = %s ORDER BY id ASC LIMIT %s",
                (task_id, limit),
            )
            rows = cur.fetchall()
            for row in rows:
                try:
                    row["payload"] = json.loads(row.get("payload") or "{}")
                except (TypeError, ValueError):
                    row["payload"] = {"raw": row.get("payload", "")}
            return rows
    finally:
        conn.close()


def list_recent_blackboard_entries(limit: int = 500) -> list[dict]:
    """读取最近的多 Agent 黑板事件，供管理员审计聚合使用。"""
    import json

    limit = max(1, min(int(limit), 2000))
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT id, task_id, entry_type, source_agent, payload, confidence, created_at "
                "FROM agent_blackboard_entries ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            rows = list(cur.fetchall())
            rows.reverse()
            for row in rows:
                try:
                    row["payload"] = json.loads(row.get("payload") or "{}")
                except (TypeError, ValueError):
                    row["payload"] = {"raw": row.get("payload", "")}
            return rows
    finally:
        conn.close()
