import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from app import auth, db
from app import main
from fastapi import HTTPException, Response


class AuthSecurityTests(unittest.TestCase):
    def setUp(self):
        auth.SESSIONS.clear()
        auth.PHONE_CHALLENGES.clear()
        auth.PASSWORD_RESET_CHALLENGES.clear()

    def tearDown(self):
        auth.SESSIONS.clear()
        auth.PHONE_CHALLENGES.clear()
        auth.PASSWORD_RESET_CHALLENGES.clear()

    def test_password_hash_uses_random_bcrypt_salt(self):
        first = auth.hash_password("correct horse")
        second = auth.hash_password("correct horse")

        self.assertTrue(first.startswith("$2b$"))
        self.assertNotEqual(first, second)
        self.assertTrue(auth.verify_password("correct horse", first))
        self.assertFalse(auth.verify_password("wrong", first))

    def test_legacy_password_is_upgraded_after_successful_login(self):
        legacy = hashlib.sha256((auth._SALT + "123456").encode("utf-8")).hexdigest()
        user = {"username": "user", "password_hash": legacy, "role": "customer"}

        with patch.object(auth.db, "get_user_by_username", return_value=user), \
             patch.object(auth.db, "update_user_password") as update_password:
            result = auth.authenticate("user", "123456")

        self.assertIsNotNone(result)
        update_password.assert_called_once()
        upgraded = update_password.call_args.args[1]
        self.assertTrue(upgraded.startswith("$2b$"))
        self.assertTrue(auth.verify_password("123456", upgraded))

    def test_session_expires_and_is_removed(self):
        with patch.object(auth.Config, "SESSION_TTL_SECONDS", 60), \
             patch.object(auth.time, "time", return_value=100.0):
            token = auth.create_session("user")
            self.assertEqual(auth.get_username(token), "user")

        with patch.object(auth.time, "time", return_value=160.0):
            self.assertIsNone(auth.get_username(token))
            self.assertNotIn(token, auth.SESSIONS)

    def test_session_store_uses_redis_adapter_when_available(self):
        class FakeRedis:
            def __init__(self):
                self.values = {}
                self.ttls = {}

            def setex(self, key, ttl, value):
                self.values[key] = value
                self.ttls[key] = ttl

            def get(self, key):
                return self.values.get(key)

            def delete(self, key):
                self.values.pop(key, None)

        fake = FakeRedis()
        with patch.object(auth.redis_store, "get_client", return_value=fake), \
             patch.object(auth.Config, "SESSION_TTL_SECONDS", 60):
            token = auth.create_session("user")
            self.assertEqual(auth.get_username(token), "user")
            self.assertEqual(fake.ttls[auth.redis_store.session_key(token)], 60)
            auth.delete_session(token)

        self.assertIsNone(fake.get(auth.redis_store.session_key(token)))

    def test_cookie_session_takes_precedence_over_bearer(self):
        cookie_token = auth.create_session("cookie-user")
        bearer_token = auth.create_session("bearer-user")

        self.assertEqual(
            auth.extract_token("Bearer " + bearer_token, cookie_token), cookie_token
        )

    def test_login_sets_httponly_cookie_and_does_not_return_token(self):
        response = Response()
        user = {
            "username": "user", "password_hash": "hash", "role": "customer",
            "phone": "13800000000", "phone_verified_at": "2026-09-04 00:00:00",
        }
        with patch.object(main.auth, "authenticate", return_value=user):
            result = main.login(main.LoginRequest(username="user", password="123456"), response)

        self.assertEqual(result["username"], "user")
        self.assertNotIn("token", result)
        cookie = response.headers["set-cookie"]
        self.assertIn("ai_ticket_session=", cookie)
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=lax", cookie)

    def test_unverified_login_returns_challenge_without_cookie(self):
        response = Response()
        user = {"username": "user", "role": "customer", "phone": None}
        with patch.object(main.auth, "authenticate", return_value=user):
            result = main.login(main.LoginRequest(username="user", password="123456"), response)

        self.assertEqual(result["status"], "phone_verification_required")
        self.assertTrue(result["requires_phone_verification"])
        self.assertIn(result["challenge_id"], auth.PHONE_CHALLENGES)
        self.assertNotIn("set-cookie", response.headers)

    def test_customer_phone_verification_binds_phone_and_is_one_time(self):
        user = {"username": "new_user", "role": "customer", "phone": None}
        challenge_id = auth.create_phone_challenge(user)
        with patch.object(auth.Config, "PHONE_VERIFICATION_TEST_CODE", "123456"), \
             patch.object(auth.Config, "DEMO_DATA_ENABLED", True), \
             patch.object(auth.db, "mark_user_phone_verified", return_value=True), \
             patch.object(auth.db, "get_user_by_username", return_value={
                 "username": "new_user", "role": "customer",
                 "phone": "13800000000", "phone_verified_at": "now",
             }) as get_user:
            sent = auth.send_phone_code(challenge_id, "13800000000")
            verified = auth.verify_phone_challenge(
                challenge_id, "13800000000", sent["demo_code"]
            )

        self.assertEqual(sent["phone_masked"], "138****0000")
        self.assertEqual(verified["phone"], "13800000000")
        self.assertNotIn(challenge_id, auth.PHONE_CHALLENGES)
        with self.assertRaises(ValueError):
            auth.verify_phone_challenge(challenge_id, "13800000000", "123456")

    def test_enterprise_phone_must_match_admin_binding(self):
        user = {
            "username": "agent", "role": "agent", "phone": "13800000001",
            "phone_verified_at": None,
        }
        challenge_id = auth.create_phone_challenge(user)
        with patch.object(auth.Config, "PHONE_VERIFICATION_TEST_CODE", "123456"):
            with self.assertRaisesRegex(ValueError, "绑定信息"):
                auth.send_phone_code(challenge_id, "13800000002")
            auth.send_phone_code(challenge_id)
            with self.assertRaisesRegex(ValueError, "不匹配"):
                auth.verify_phone_challenge(challenge_id, "13800000002", "123456")

    def test_wrong_phone_code_expires_after_max_attempts(self):
        challenge_id = auth.create_phone_challenge(
            {"username": "user", "role": "customer", "phone": None}
        )
        with patch.object(auth.Config, "PHONE_VERIFICATION_TEST_CODE", "123456"), \
             patch.object(auth.Config, "PHONE_VERIFICATION_MAX_ATTEMPTS", 2):
            auth.send_phone_code(challenge_id, "13800000000")
            with self.assertRaisesRegex(ValueError, "验证码错误"):
                auth.verify_phone_challenge(challenge_id, "13800000000", "000000")
            with self.assertRaisesRegex(ValueError, "次数过多"):
                auth.verify_phone_challenge(challenge_id, "13800000000", "000000")
        self.assertNotIn(challenge_id, auth.PHONE_CHALLENGES)

    def test_login_page_lists_agent_demo_account(self):
        login_html = (Path(main.config.BASE_DIR) / "templates" / "login.html").read_text(
            encoding="utf-8"
        )

        self.assertIn("agent / 123456", login_html)
        self.assertIn("客服工作台", login_html)

    def test_register_customer_forces_customer_role(self):
        with patch.object(
            auth.db,
            "get_user_by_username",
            side_effect=[None, {"username": "new_user", "role": "customer"}],
        ) as get_user, patch.object(auth.db, "create_user", return_value=1) as create_user:
            result = auth.register_customer("new_user", "Password1", "Password1")

        self.assertEqual(result["role"], "customer")
        self.assertEqual(create_user.call_args.kwargs["role"], "customer")
        self.assertTrue(auth.verify_password("Password1", create_user.call_args.args[1]))
        self.assertEqual(get_user.call_count, 2)

    def test_register_rejects_duplicate_and_weak_credentials(self):
        with patch.object(auth.db, "get_user_by_username", return_value={"username": "user"}):
            with self.assertRaises(ValueError):
                auth.register_customer("user", "Password1", "Password1")

        with patch.object(auth.db, "get_user_by_username", return_value=None):
            with self.assertRaises(ValueError):
                auth.register_customer("bad-name", "Password1", "Password1")
            with self.assertRaises(ValueError):
                auth.register_customer("valid_user", "12345678", "12345678")

    def test_password_reset_uses_verified_phone_and_one_time_code(self):
        user = {
            "username": "user", "role": "customer", "active": 1,
            "phone": "13800000000", "phone_verified_at": "2026-09-05 00:00:00",
        }
        with patch.object(auth.Config, "PHONE_VERIFICATION_TEST_CODE", "654321"), \
             patch.object(auth.Config, "DEMO_DATA_ENABLED", True), \
             patch.object(auth.db, "get_user_by_username", return_value=user), \
             patch.object(auth.db, "update_user_password", return_value=True) as update_password, \
             patch.object(auth.db, "insert_account_audit") as insert_audit:
            challenge = auth.request_password_reset("user", "13800000000")
            self.assertEqual(challenge["phone_masked"], "138****0000")
            result = auth.verify_password_reset(
                challenge["challenge_id"], "654321", "NewPass1", "NewPass1"
            )

        self.assertTrue(result)
        insert_audit.assert_called_once_with(
            "user", "password_reset", "用户通过已绑定手机号重置密码", "self-service"
        )
        new_hash = update_password.call_args.args[1]
        self.assertTrue(new_hash.startswith("$2b$"))
        self.assertTrue(auth.verify_password("NewPass1", new_hash))
        with self.assertRaisesRegex(ValueError, "挑战"):
            auth.verify_password_reset(
                challenge["challenge_id"], "654321", "NewPass1", "NewPass1"
            )

    def test_password_reset_rejects_unverified_phone_without_revealing_account(self):
        user = {
            "username": "user", "role": "customer", "active": 1,
            "phone": "13800000000", "phone_verified_at": None,
        }
        with patch.object(auth.db, "get_user_by_username", return_value=user):
            challenge = auth.request_password_reset("user", "13800000000")

        self.assertIn("challenge_id", challenge)
        with self.assertRaisesRegex(ValueError, "重置失败"):
            auth.verify_password_reset(
                challenge["challenge_id"], challenge["demo_code"], "NewPass1", "NewPass1"
            )

    def test_admin_and_manager_permissions_are_separate(self):
        self.assertEqual(auth.user_permissions({"role": "admin"}), {"account.manage"})
        self.assertEqual(auth.user_permissions({"role": "manager"}), {"dashboard.view", "stats.view"})

    def test_manager_dependency_rejects_admin(self):
        with patch.object(auth, "get_username", return_value="admin"), \
             patch.object(auth.db, "get_user_by_username", return_value={"username": "admin", "role": "admin", "active": 1}):
            with self.assertRaises(HTTPException) as ctx:
                auth.require_manager()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_dependency_rejects_manager_for_knowledge_operations(self):
        with patch.object(auth, "get_username", return_value="manager"), \
             patch.object(auth.db, "get_user_by_username", return_value={"username": "manager", "role": "manager", "active": 1}):
            with self.assertRaises(HTTPException) as ctx:
                auth.require_admin()
        self.assertEqual(ctx.exception.status_code, 403)

    def test_manager_page_rejects_admin(self):
        with self.assertRaises(HTTPException) as ctx:
            main.manager_page({"username": "admin", "role": "admin", "active": 1})
        self.assertEqual(ctx.exception.status_code, 403)

    def test_admin_page_rejects_manager(self):
        with self.assertRaises(HTTPException) as ctx:
            main.admin_page({"username": "manager", "role": "manager", "active": 1})
        self.assertEqual(ctx.exception.status_code, 403)

    def test_inactive_current_user_is_rejected(self):
        with patch.object(auth, "get_username", return_value="user"), \
             patch.object(auth.db, "get_user_by_username", return_value={"username": "user", "role": "customer", "active": 0}):
            with self.assertRaises(HTTPException) as ctx:
                auth.require_user()
        self.assertEqual(ctx.exception.status_code, 401)

    def test_resolve_rejects_already_resolved_ticket(self):
        with patch.object(main.db, "get_ticket", return_value={"id": 7, "status": "resolved"}):
            with self.assertRaises(HTTPException) as ctx:
                main.resolve_escalation(7, main.ResolveRequest(human_answer="已处理"), "admin")

        self.assertEqual(ctx.exception.status_code, 409)

    def test_resolve_rejects_empty_human_answer(self):
        with patch.object(main.db, "get_ticket", return_value={"id": 7, "status": "escalated"}):
            with self.assertRaises(HTTPException) as ctx:
                main.resolve_escalation(7, main.ResolveRequest(human_answer="  "), "admin")

        self.assertEqual(ctx.exception.status_code, 400)


class TicketOwnershipQueryTests(unittest.TestCase):
    class Cursor:
        def __init__(self, result=None, rowcount=1):
            self.result = result
            self.rowcount = rowcount
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchone(self):
            return self.result

        def fetchall(self):
            return self.result or []

    class Connection:
        def __init__(self, cursor):
            self.cursor_obj = cursor

        def cursor(self):
            return self.cursor_obj

        def close(self):
            pass

    def test_get_ticket_for_user_filters_by_username(self):
        cursor = self.Cursor(result={"id": 7, "username": "alice"})
        connection = self.Connection(cursor)

        with patch.object(db, "_connect", return_value=connection):
            result = db.get_ticket_for_user("alice", 7)

        self.assertEqual(result["username"], "alice")
        self.assertIn("username = %s", cursor.calls[0][0])
        self.assertEqual(cursor.calls[0][1], (7, "alice"))

    def test_list_tickets_for_user_filters_by_username(self):
        cursor = self.Cursor(result=[{"id": 7, "username": "alice"}])
        connection = self.Connection(cursor)

        with patch.object(db, "_connect", return_value=connection):
            result = db.list_tickets(username="alice")

        self.assertEqual(len(result), 1)
        self.assertIn("WHERE username = %s", cursor.calls[0][0])
        self.assertEqual(cursor.calls[0][1], ("alice", 50))

    def test_update_feedback_filters_by_username(self):
        cursor = self.Cursor(rowcount=1)
        connection = self.Connection(cursor)

        with patch.object(db, "_connect", return_value=connection):
            updated = db.update_ticket_feedback(7, "down", username="alice")

        self.assertTrue(updated)
        self.assertIn("AND username = %s", cursor.calls[0][0])
        self.assertEqual(cursor.calls[0][1], ("down", 7, "alice"))


class EntityArchiveTests(unittest.TestCase):
    def test_archive_uses_current_user_and_hides_internal_fields(self):
        order = {
            "order_id": "A1", "username": "alice", "product": "耳机", "status": "已发货",
            "tracking_no": "SF1", "logistics": "运输中", "refund_status": None, "secret": "nope",
        }
        ticket = {
            "id": 7, "username": "alice", "category": "售后", "status": "in_progress",
            "ticket_text": "有杂音", "assigned_to": "admin",
        }
        with patch.object(main.profile, "list_facts", return_value=[{"id": 1}]) as facts, \
             patch.object(main.db, "list_orders_for_user", return_value=[order]) as orders, \
             patch.object(main.db, "list_tickets_for_user", return_value=[ticket]) as tickets:
            archive = main.get_entity_archive("alice")

        facts.assert_called_once_with("alice")
        orders.assert_called_once_with("alice")
        tickets.assert_called_once_with("alice", limit=20)
        self.assertEqual(archive["subject"]["username"], "alice")
        self.assertNotIn("username", archive["objects"]["orders"][0])
        self.assertNotIn("secret", archive["objects"]["orders"][0])
        self.assertNotIn("assigned_to", archive["objects"]["tickets"][0])
        self.assertEqual(archive["objects"]["tickets"][0]["topic"], "售后进度")
        self.assertEqual(archive["objects"]["tickets"][0]["status_label"], "人工处理中")
        self.assertNotIn("id", archive["objects"]["tickets"][0])
        self.assertNotIn("category", archive["objects"]["tickets"][0])
        self.assertNotIn("status", archive["objects"]["tickets"][0])
        self.assertNotIn("ticket_text", archive["objects"]["tickets"][0])


if __name__ == "__main__":
    unittest.main()
