"""数据库模块：用 MySQL 存储工单记录。

使用 pymysql 连接 MySQL（连接信息在 config.py / .env 中配置），
表结构简单直观，方便后续做效果统计（自动处理率、平均耗时等）。
"""
import logging
from datetime import datetime

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
    reply_source  VARCHAR(16),        -- template / rag / agent / chat / escalate
    human_answer  TEXT,               -- 人工客服的回答（升级工单处理后填写）
    latency_ms    INT,                -- 处理耗时（毫秒）
    phone         VARCHAR(32),        -- 客户联系电话（若有）
    device        VARCHAR(64),        -- 渠道/设备信息（web / wechat）
    emotion       VARCHAR(16),        -- 情绪识别结果：负面 / 中性 / 正面
    emotion_intensity VARCHAR(16),    -- 负面情绪强度：normal / extreme
    multi_intent  TINYINT(1) DEFAULT 0, -- 是否多诉求混杂（复合请求 → 转人工）
    rag_chunks    TEXT,               -- 命中的 RAG 片段（JSON 数组）
    route_trace   TEXT,               -- 路由决策链路（JSON，全链路留痕）
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

# 用户表（登录用）
_USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INT AUTO_INCREMENT PRIMARY KEY,
    username      VARCHAR(64) NOT NULL UNIQUE,
    password_hash VARCHAR(128) NOT NULL,
    role          VARCHAR(16) NOT NULL DEFAULT 'customer',  -- admin(客服) / customer(客户)
    created_at    DATETIME
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


_DEFAULT_DB = object()


def _connect(database=_DEFAULT_DB):
    """建立 MySQL 连接。

    参数：
      database 为默认值时使用 .env 里的 MYSQL_DB；
      传 None 表示不指定数据库（用于「建库」阶段）。
    """
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
            cur.execute(_ORDERS_SCHEMA)
            cur.execute(_TICKET_LOGS_SCHEMA)
            cur.execute(_FEEDBACK_TAGS_SCHEMA)
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


def list_tickets(limit: int = 50) -> list[dict]:
    """查询最近的工单列表（按 id 倒序）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
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
    """查询所有升级给人工的工单（含待处理 escalated + 已处理 resolved）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM tickets WHERE status IN ('escalated', 'resolved') "
                "ORDER BY id DESC LIMIT %s",
                (limit,),
            )
            return cur.fetchall()
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


def create_user(username: str, password_hash: str, role: str = "customer") -> int:
    """创建用户，返回自增 id。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO users (username, password_hash, role, created_at) "
                "VALUES (%s, %s, %s, %s)",
                (username, password_hash, role,
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


def update_ticket_status(ticket_id: int, status: str) -> None:
    """更新工单状态（如：人工处理后标记为 resolved）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE tickets SET status = %s WHERE id = %s",
                        (status, ticket_id))
    finally:
        conn.close()


def update_ticket_answer(ticket_id: int, human_answer: str, status: str = "resolved") -> None:
    """人工客服处理升级工单：写入人工回答，并把状态标记为已处理。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE tickets SET status = %s, human_answer = %s WHERE id = %s",
                (status, human_answer, ticket_id),
            )
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
    """初始化演示订单：绑定到演示用户 user（仅当订单号不存在时插入）。"""
    sample = [
        ("A20240812001", "user", "无线蓝牙耳机", "已发货",
         "SF1234567890", "已到达长沙分拨中心，预计明天送达", None),
        ("A20240815002", "user", "智能空气炸锅", "待发货",
         None, None, None),
        ("A20240720003", "user", "运动跑鞋", "已完成",
         "YT9876543210", "已签收", "退款处理中，预计 1-3 个工作日到账"),
        ("A20240801004", "user", "保温杯", "已退货",
         None, None, "退款已到账"),
    ]
    for order_id, username, product, status, tracking_no, logistics, refund_status in sample:
        if get_order_by_id(order_id) is None:
            insert_order(order_id, username, product, status, tracking_no,
                         logistics, refund_status)
            logger.info("初始化演示订单：%s（用户 %s）", order_id, username)


def update_ticket_feedback(ticket_id: int, feedback: str) -> None:
    """记录用户对某张工单回复的满意度（up 满意 / down 不满意）。"""
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE tickets SET feedback = %s WHERE id = %s",
                        (feedback, ticket_id))
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
