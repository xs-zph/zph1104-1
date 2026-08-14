"""数据模型（Pydantic）：定义 API 接口的输入输出格式。

Pydantic 会自动校验数据，字段类型不对会直接报错，保证接口健壮。
"""
from typing import Optional

from pydantic import BaseModel


class TicketCreate(BaseModel):
    """创建工单 / 聊天消息时传入的内容。"""

    ticket_text: str  # 工单原文 / 聊天内容


class LoginRequest(BaseModel):
    """登录时传入的用户名和密码。"""

    username: str
    password: str


class FAQCreate(BaseModel):
    """实时新增一条知识库 FAQ 时传入的内容。"""

    question: str
    answer: str


class ResolveRequest(BaseModel):
    """人工客服处理升级工单时提交的内容。"""

    human_answer: str = ""   # 人工回答内容
    save_to_kb: bool = True  # 是否将问答存入知识库供 AI 学习


class FeedbackRequest(BaseModel):
    """用户对某条 AI 回复的满意度评价。"""

    feedback: str  # up（满意）/ down（不满意）


class OrderCreate(BaseModel):
    """管理员新增订单时传入的内容。"""

    order_id: str                    # 订单号（唯一）
    username: str = "user"           # 归属用户
    product: str = ""                # 商品名
    status: str = "待发货"           # 订单状态
    tracking_no: Optional[str] = None  # 运单号
    logistics: Optional[str] = None    # 物流进度
    refund_status: Optional[str] = None  # 退款状态


class OrderUpdate(BaseModel):
    """管理员编辑订单时传入的内容（仅提交需要修改的字段）。"""

    username: Optional[str] = None
    product: Optional[str] = None
    status: Optional[str] = None
    tracking_no: Optional[str] = None
    logistics: Optional[str] = None
    refund_status: Optional[str] = None


class LoginOut(BaseModel):
    """登录成功后返回的令牌与用户信息。"""

    token: str
    username: str
    role: str


class TicketOut(BaseModel):
    """返回给前端 / 调用方的完整工单信息。"""

    id: int
    ticket_text: str
    category: Optional[str] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None
    status: str                      # auto（自动处理）/ escalated（人工升级）
    reply: Optional[str] = None
    reply_source: Optional[str] = None   # template / rag / escalate
    latency_ms: Optional[int] = None
    created_at: Optional[str] = None
