"""数据模型（Pydantic）：定义 API 接口的输入输出格式。

Pydantic 会自动校验数据，字段类型不对会直接报错，保证接口健壮。
"""
from typing import Optional

from pydantic import BaseModel, Field


class TicketCreate(BaseModel):
    """创建工单 / 聊天消息时传入的内容。"""

    ticket_text: str  # 工单原文 / 聊天内容
    phone: Optional[str] = None    # 客户联系电话（若有）
    device: Optional[str] = None   # 渠道/设备信息（web / wechat）


class LoginRequest(BaseModel):
    """登录时传入的用户名和密码。"""

    username: str
    password: str


class PhoneCodeSendRequest(BaseModel):
    """为密码校验成功但手机号未核验的登录挑战发送验证码。"""

    challenge_id: str
    phone: Optional[str] = None


class PhoneCodeVerifyRequest(BaseModel):
    """提交登录挑战的手机号验证码。"""

    challenge_id: str
    phone: str
    code: str


class RegisterRequest(BaseModel):
    """注册普通客户账号；角色由服务端固定，不接受前端传入。"""

    username: str
    password: str
    confirm_password: str


class PasswordResetChallengeRequest(BaseModel):
    """申请密码重置验证码。"""

    username: str
    phone: str


class PasswordResetRequest(BaseModel):
    """提交密码重置挑战验证码并设置新密码。"""

    challenge_id: str
    code: str
    password: str
    confirm_password: str


class ManagedUserCreate(BaseModel):
    """管理员创建受管账号。"""

    username: str
    password: str
    role: str = "agent"
    permissions: list[str] = Field(default_factory=list)
    phone: Optional[str] = None


class ManagedUserUpdate(BaseModel):
    """管理员调整账号状态、角色和账号级客服权限。"""

    role: str
    active: bool = True
    permissions: list[str] = Field(default_factory=list)
    phone: Optional[str] = None


class ManagedPasswordReset(BaseModel):
    """管理员直接重置指定账号密码。"""

    password: str


class FAQCreate(BaseModel):
    """实时新增一条知识库 FAQ 时传入的内容。"""

    question: str
    answer: str


class FAQUpdate(BaseModel):
    """编辑一条知识库 FAQ 时传入的内容（仅提交需要修改的字段）。"""

    question: Optional[str] = None
    answer: Optional[str] = None


class ResolveRequest(BaseModel):
    """人工客服处理升级工单时提交的内容。"""

    human_answer: str = ""   # 人工回答内容
    save_to_kb: bool = True  # 是否将问答存入知识库供 AI 学习


class TicketStatusRequest(BaseModel):
    """坐席更新人工工单状态。"""

    status: str


class TicketAssignRequest(BaseModel):
    """坐席接单或转派人工工单。"""

    assigned_to: str


class AfterSaleAnnotationRequest(BaseModel):
    """人工客服记录工单上的结构化售后标注，不直接提交售后申请。"""

    request_type: str
    stage: str
    order_id: Optional[str] = Field(default=None, max_length=64)
    product: Optional[str] = Field(default=None, max_length=255)
    reason: Optional[str] = Field(default=None, max_length=255)
    item_status: Optional[str] = Field(default=None, max_length=64)
    note: Optional[str] = Field(default=None, max_length=2000)


class ReturnRequestSubmit(BaseModel):
    """客服确认提交真实退货申请。"""

    confirmed: bool = False


class AfterSaleRequestSubmit(BaseModel):
    """客服确认提交真实退款或维修申请。"""

    confirmed: bool = False


class AfterSaleRetryRequest(BaseModel):
    """客服确认重试一条已失败的售后申请。"""

    request_no: str = Field(min_length=1, max_length=40)
    confirmed: bool = False


class FeedbackRequest(BaseModel):
    """用户对某条 AI 回复的满意度评价。"""

    feedback: str  # up（满意）/ down（不满意）


class FeedbackTagRequest(BaseModel):
    """人工坐席给错误工单打标签时传入的内容。"""

    tag: str                      # 分类错误 / 知识库无答案 / AI回答有误 / 安抚不合适
    note: Optional[str] = None    # 人工补充说明


class ProfileFactUpdate(BaseModel):
    """手工修正实体画像事实。"""

    fact_value: str
    confirmed: bool = True


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
    """登录成功后返回的非敏感用户信息；会话令牌只通过 HttpOnly Cookie 下发。"""

    username: str
    role: str
    status: str = "authenticated"
    requires_phone_verification: bool = False
    challenge_id: Optional[str] = None
    phone_masked: Optional[str] = None
    demo_code: Optional[str] = None
    active: bool = True
    permissions: list[str] = Field(default_factory=list)


class TicketOut(BaseModel):
    """返回给前端 / 调用方的完整工单信息。"""

    id: int
    ticket_text: str
    category: Optional[str] = None
    confidence: Optional[float] = None
    reason: Optional[str] = None
    status: str                      # auto（自动处理）/ escalated（人工升级）
    reply: Optional[str] = None
    reply_source: Optional[str] = None   # template / rag / agent / chat / clarification / escalate / service_error
    latency_ms: Optional[int] = None
    created_at: Optional[str] = None
