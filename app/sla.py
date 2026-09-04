"""人工工单 SLA 后台扫描与超时事件通知。"""

from __future__ import annotations

import logging
import threading

from app import db, events
from app.config import Config

logger = logging.getLogger("app.sla")

IDLE_CLOSE_REASON = "客户长时间无活动，系统自动结束人工会话"


class SLAWorker:
    """单实例后台扫描器；数据库标记位负责跨进程幂等。"""

    def __init__(
        self,
        interval_seconds: float | None = None,
        idle_timeout_minutes: int | None = None,
    ):
        self.interval_seconds = interval_seconds or Config.SLA_SCAN_INTERVAL_SECONDS
        self.idle_timeout_minutes = (
            Config.HUMAN_IDLE_TIMEOUT_MINUTES
            if idle_timeout_minutes is None else idle_timeout_minutes
        )
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def run_once(self) -> list[dict]:
        """扫描一次逾期工单，并发布后台刷新事件。"""
        overdue = db.mark_overdue_tickets()
        for ticket in overdue:
            ticket_id = ticket["id"]
            username = ticket.get("username")
            db.insert_ticket_log(
                ticket_id,
                "sla_breached",
                "人工工单超过 SLA 时限，系统提升为紧急优先级",
                "system",
            )
            events.broker.publish(
                "ticket_updated",
                {
                    "ticket_id": ticket_id,
                    "username": username,
                    "status": ticket.get("status"),
                    "priority": "urgent",
                    "sla_breached": True,
                },
            )
        if overdue:
            logger.warning("SLA 超时工单：%s", [ticket["id"] for ticket in overdue])
        return overdue

    def run_idle_close_once(self) -> list[dict]:
        """扫描并结束客户长时间无活动的人工工单。"""
        idle_tickets = db.close_idle_human_tickets(self.idle_timeout_minutes)
        for ticket in idle_tickets:
            ticket_id = ticket["id"]
            db.insert_ticket_log(
                ticket_id,
                "human_idle_closed",
                IDLE_CLOSE_REASON,
                "system",
            )
            events.broker.publish(
                "ticket_updated",
                {
                    "ticket_id": ticket_id,
                    "username": ticket.get("username"),
                    "status": "closed",
                    "reason": IDLE_CLOSE_REASON,
                },
            )
        if idle_tickets:
            logger.info("客户无活动自动结束人工工单：%s", [ticket["id"] for ticket in idle_tickets])
        return idle_tickets

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="sla-scanner",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=max(1.0, self.interval_seconds + 1.0))
        self._thread = None

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
                self.run_idle_close_once()
            except Exception:  # noqa: BLE001
                logger.exception("SLA 扫描失败，等待下一轮重试")
            self._stop.wait(self.interval_seconds)


worker = SLAWorker()
