import asyncio
import unittest

from fastapi import HTTPException

from app import main
from app import events
from app.events import EventBroker


class EventBrokerTests(unittest.TestCase):
    def test_publish_and_filter_advances_cursor(self):
        broker = EventBroker()
        broker.publish("ticket_escalated", {"ticket_id": 1})
        broker.publish("human_replied", {"ticket_id": 2, "username": "alice"})

        events, cursor = broker.wait_for(
            0,
            lambda event: event.event_type == "human_replied",
            timeout=0,
        )

        self.assertEqual([event.payload["ticket_id"] for event in events], [2])
        self.assertEqual(cursor, 2)

    def test_wait_for_returns_empty_after_timeout(self):
        broker = EventBroker()
        events, cursor = broker.wait_for(0, lambda event: True, timeout=0)

        self.assertEqual(events, [])
        self.assertEqual(cursor, 0)


class EventStreamTests(unittest.TestCase):
    class Request:
        async def is_disconnected(self):
            return False

    def test_event_stream_requires_login(self):
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(main.event_stream(self.Request(), last_event_id="", user=None))

        self.assertEqual(ctx.exception.status_code, 401)

    def test_event_stream_starts_with_sse_handshake(self):
        response = asyncio.run(
            main.event_stream(
                self.Request(),
                last_event_id="",
                user={"username": "admin", "role": "admin"},
            )
        )
        first_chunk = asyncio.run(response.body_iterator.__anext__())
        asyncio.run(response.body_iterator.aclose())

        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(first_chunk, ": connected\n\n")

    def test_event_stream_emits_matching_event(self):
        async def consume_event():
            baseline = events.broker.publish("test_baseline", {})
            response = await main.event_stream(
                self.Request(),
                last_event_id=str(baseline.id),
                user={"username": "alice", "role": "customer"},
            )
            iterator = response.body_iterator
            await iterator.__anext__()
            events.broker.publish(
                "human_replied",
                {"ticket_id": 12, "username": "alice", "human_answer": "已处理"},
            )
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            await iterator.aclose()
            return chunk

        chunk = asyncio.run(consume_event())
        self.assertIn("event: human_replied", chunk)
        self.assertIn('"ticket_id": 12', chunk)

    def test_event_stream_accepts_query_cursor_for_manual_reconnect(self):
        async def consume_event():
            baseline = events.broker.publish("test_baseline", {})
            response = await main.event_stream(
                self.Request(),
                last_event_id="",
                since=str(baseline.id),
                user={"username": "alice", "role": "customer"},
            )
            iterator = response.body_iterator
            await iterator.__anext__()
            events.broker.publish(
                "human_replied",
                {"ticket_id": 15, "username": "alice", "human_answer": "补发的回复"},
            )
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            await iterator.aclose()
            return chunk

        chunk = asyncio.run(consume_event())
        self.assertIn("event: human_replied", chunk)
        self.assertIn('"ticket_id": 15', chunk)

    def test_admin_event_stream_emits_customer_message(self):
        async def consume_event():
            baseline = events.broker.publish("test_baseline", {})
            response = await main.event_stream(
                self.Request(),
                last_event_id=str(baseline.id),
                user={"username": "admin", "role": "admin"},
            )
            iterator = response.body_iterator
            await iterator.__anext__()
            events.broker.publish(
                "customer_message",
                {"ticket_id": 13, "username": "alice", "message": "补充信息"},
            )
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            await iterator.aclose()
            return chunk

        chunk = asyncio.run(consume_event())
        self.assertIn("event: customer_message", chunk)
        self.assertIn('"ticket_id": 13', chunk)

    def test_admin_event_stream_emits_ticket_updates(self):
        async def consume_event():
            baseline = events.broker.publish("test_baseline", {})
            response = await main.event_stream(
                self.Request(),
                last_event_id=str(baseline.id),
                user={"username": "admin", "role": "admin"},
            )
            iterator = response.body_iterator
            await iterator.__anext__()
            events.broker.publish(
                "ticket_updated",
                {"ticket_id": 14, "status": "waiting_customer"},
            )
            chunk = await asyncio.wait_for(iterator.__anext__(), timeout=1)
            await iterator.aclose()
            return chunk

        chunk = asyncio.run(consume_event())
        self.assertIn("event: ticket_updated", chunk)
        self.assertIn('"ticket_id": 14', chunk)


if __name__ == "__main__":
    unittest.main()
