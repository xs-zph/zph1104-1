"""中心式多 Agent 编排。

主 Agent 是唯一的调度和黑板写入者。专业 Agent 只能接收主 Agent 的
JSON 任务并返回 JSON 结果，路由层不暴露子 Agent 之间的调用能力。
"""
from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from app import agent, db, skills

logger = logging.getLogger("app.multi_agent")

MAIN_AGENT = "main_agent"
PROTOCOL_VERSION = "1.0"


class AgentProtocolError(ValueError):
    """Agent 间消息不符合中心协议。"""


class AgentMessage(BaseModel):
    """主 Agent 与子 Agent 之间唯一允许传递的结构化消息。"""

    protocol_version: Literal["1.0"] = PROTOCOL_VERSION
    message_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    sender: str = Field(min_length=1)
    receiver: str = Field(min_length=1)
    message_type: Literal["task_request", "task_result", "task_reject"]
    payload: dict[str, Any] = Field(default_factory=dict)
    context_refs: list[str] = Field(default_factory=list)
    status: Literal["pending", "success", "failed", "rejected"] = "pending"
    confidence: float | None = Field(default=None, ge=0, le=1)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @model_validator(mode="after")
    def validate_central_route(self):
        if self.sender == self.receiver:
            raise AgentProtocolError("Agent 不能向自己发送消息")
        if self.sender != MAIN_AGENT and self.receiver != MAIN_AGENT:
            raise AgentProtocolError("子 Agent 之间禁止直接通信")
        if self.sender != MAIN_AGENT and self.message_type == "task_request":
            raise AgentProtocolError("子 Agent 不能向其他 Agent 分派任务")
        if self.sender == MAIN_AGENT and self.message_type == "task_result":
            raise AgentProtocolError("主 Agent 不能伪造子 Agent 结果消息")
        return self

    def to_json(self) -> str:
        """序列化为协议 JSON；不允许使用自然语言对象替代消息。"""
        return self.model_dump_json()

    @classmethod
    def from_json(cls, value: str) -> "AgentMessage":
        return cls.model_validate_json(value)


class Blackboard:
    """工单级共享黑板。

    黑板对所有专业 Agent 可读，但写入入口明确要求调用者是主 Agent，
    并且每次写入都落库，便于审计和重放。
    """

    def read(self, task_id: str) -> list[dict[str, Any]]:
        return db.list_blackboard_entries(task_id)

    def publish(
        self,
        task_id: str,
        entry_type: str,
        payload: dict[str, Any],
        *,
        source_agent: str = MAIN_AGENT,
        confidence: float | None = None,
    ) -> dict[str, Any]:
        if source_agent != MAIN_AGENT:
            raise AgentProtocolError("只有主 Agent 可以写入共享黑板")
        return db.insert_blackboard_entry(
            task_id=task_id,
            entry_type=entry_type,
            payload=payload,
            source_agent=source_agent,
            confidence=confidence,
        )


class SpecialistAgent:
    """一个受中心监督的专业 Agent。"""

    def __init__(self, name: str, scenes: set[str], tool_names: set[str], mandate: str):
        self.name = name
        self.scenes = scenes
        self.tool_names = tool_names
        self.mandate = mandate

    def handle(self, request: AgentMessage, blackboard: list[dict[str, Any]]) -> AgentMessage:
        if request.sender != MAIN_AGENT or request.receiver != self.name:
            raise AgentProtocolError("子 Agent 只接受主 Agent 的任务")
        question = request.payload.get("question")
        username = request.payload.get("username")
        if not isinstance(question, str) or not question.strip():
            return self._reject(request, "任务缺少 question")
        if username is not None and not isinstance(username, str):
            return self._reject(request, "任务中的 username 类型无效")

        supervisor_context = json.dumps(
            {
                "supervisor": MAIN_AGENT,
                "specialist": self.name,
                "mandate": self.mandate,
                "blackboard": blackboard,
            },
            ensure_ascii=False,
            separators=(",", ":"),
            default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
        )
        reply = agent.run_agent(
            question,
            username,
            tool_names=self.tool_names,
            system_context=(
                "本次任务由主 Agent 监督。你是专业子 Agent，只处理职责范围内的任务。"
                "你的返回值将作为 customer_reply 交给主 Agent，不得描述内部协作。\n"
                f"【专业职责】{self.mandate}\n"
                f"【结构化共享黑板】{supervisor_context}"
            ),
        )
        return AgentMessage(
            message_id=uuid.uuid4().hex,
            task_id=request.task_id,
            sender=self.name,
            receiver=MAIN_AGENT,
            message_type="task_result",
            payload={
                "result_type": "customer_reply",
                "customer_reply": reply,
                "specialist": self.name,
            },
            context_refs=[f"blackboard:{request.task_id}"],
            status="success",
        )

    def _reject(self, request: AgentMessage, reason: str) -> AgentMessage:
        return AgentMessage(
            message_id=uuid.uuid4().hex,
            task_id=request.task_id,
            sender=self.name,
            receiver=MAIN_AGENT,
            message_type="task_reject",
            payload={"reason_code": "invalid_task", "reason": reason},
            status="rejected",
        )


def _children() -> tuple[SpecialistAgent, ...]:
    return (
        SpecialistAgent(
            "order_agent",
            {"order", "logistics", "refund", "order_action", "after_sale", "ticket"},
            {
                "list_my_orders", "query_order", "query_logistics", "check_refund",
                "cancel_order", "apply_after_sale", "check_my_tickets",
            },
            "处理订单、物流、退款、售后和客户工单查询。",
        ),
        SpecialistAgent(
            "knowledge_agent",
            {"knowledge"},
            {"search_faq"},
            "只依据知识库回答政策、流程、保修、退货和产品使用问题。",
        ),
        SpecialistAgent(
            "realtime_agent",
            {"realtime"},
            {"get_time", "get_weather"},
            "处理时间和天气等实时信息查询。",
        ),
        SpecialistAgent(
            "profile_agent",
            set(),
            set(),
            "依据主 Agent 提供的实体画像事实回答主体偏好、设备和称呼问题。",
        ),
        SpecialistAgent(
            "general_agent",
            set(),
            set(),
            "处理未能准确归类的普通咨询，并在信息不足时给出可审计的转人工建议。",
        ),
    )


class MainAgent:
    """中心监督器：一次任务只选择一个子 Agent。"""

    def __init__(self, blackboard: Blackboard | None = None,
                 children: tuple[SpecialistAgent, ...] | None = None):
        self.blackboard = blackboard or Blackboard()
        self.children = {child.name: child for child in (children or _children())}

    def select_child(self, question: str) -> SpecialistAgent:
        text = (question or "").strip().lower()
        profile_terms = ("画像", "偏好", "喜欢", "常用", "设备", "称呼", "记得我")
        if any(term in text for term in profile_terms):
            return self.children["profile_agent"]
        for spec in skills.retrieve(question, top_k=3):
            for child in self.children.values():
                if spec.scene in child.scenes:
                    return child
        return self.children["general_agent"]

    def run(self, question: str, username: str | None = None) -> str:
        task_id = f"task_{uuid.uuid4().hex}"
        child = self.select_child(question)
        self._publish(task_id, "task_created", {
            "question_type": "customer_request",
            "question": (question or "")[:240],
            "username": username,
        })
        request = AgentMessage(
            message_id=uuid.uuid4().hex,
            task_id=task_id,
            sender=MAIN_AGENT,
            receiver=child.name,
            message_type="task_request",
            payload={
                "question": question,
                "username": username,
                "child_role": child.name,
            },
            context_refs=[f"blackboard:{task_id}"],
        )
        self._publish(task_id, "task_dispatched", {
            "receiver": child.name,
            "message": json.loads(request.to_json()),
        })
        try:
            result = child.handle(request, self.blackboard.read(task_id))
        except Exception as exc:
            # 路由层会接住异常并转人工；这里必须把失败闭环写进审计黑板，
            # 不能让任务永远停留在“已分派”。
            self._publish(task_id, "task_rejected", {
                "sender": child.name,
                "reason_code": "child_execution_failed",
                "error_type": type(exc).__name__,
            })
            raise
        if result.receiver != MAIN_AGENT or result.sender != child.name:
            raise AgentProtocolError("子 Agent 返回路径不符合中心协议")
        if result.message_type != "task_result" or result.status != "success":
            self._publish(task_id, "task_rejected", {
                "sender": result.sender,
                "payload": result.payload,
            })
            raise AgentProtocolError("子 Agent 未返回成功结果")
        reply = result.payload.get("customer_reply")
        if not isinstance(reply, str) or not reply.strip():
            raise AgentProtocolError("子 Agent 返回结果缺少 customer_reply")
        self._publish(task_id, "task_completed", {
            "sender": result.sender,
            "result": result.payload,
        })
        return reply

    def _publish(self, task_id: str, entry_type: str, payload: dict[str, Any]) -> None:
        try:
            self.blackboard.publish(task_id, entry_type, payload)
        except Exception as exc:  # noqa: BLE001 - 黑板故障不能阻断主客服链路
            logger.warning("共享黑板写入失败，继续处理任务：%s", exc)


_main_agent = MainAgent()


def run(question: str, username: str | None = None) -> str:
    """客服主链路调用的中心 Agent 入口。"""
    return _main_agent.run(question, username)
