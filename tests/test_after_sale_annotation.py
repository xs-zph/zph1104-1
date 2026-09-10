import unittest
from unittest.mock import patch

from fastapi import HTTPException

from app import db, main


class AfterSaleAnnotationRulesTests(unittest.TestCase):
    def test_annotation_payload_accepts_return_workflow_fields(self):
        payload = main.AfterSaleAnnotationRequest(
            request_type="return",
            stage="pending_order_check",
            order_id="A100",
            product="耳机",
            reason="商品不合适",
            item_status="已签收",
            note="客户明确要求退货",
        )

        self.assertEqual(payload.request_type, "return")
        self.assertEqual(payload.stage, "pending_order_check")
        self.assertEqual(payload.product, "耳机")


class AfterSaleAnnotationApiTests(unittest.TestCase):
    def test_escalation_list_returns_customer_facing_annotation_labels(self):
        raw_ticket = {
            "id": 7,
            "status": "in_progress",
            "after_sale_annotation": {
                "ticket_id": 7,
                "request_type": "return",
                "stage": "pending_order_check",
                "order_id": "A100",
            },
        }
        with patch.object(main.db, "list_escalations", return_value=[raw_ticket]):
            result = main.list_escalations("agent")

        annotation = result[0]["after_sale_annotation"]
        self.assertEqual(annotation["request_type_label"], "退货申请")
        self.assertEqual(annotation["stage_label"], "待核对订单")
        self.assertNotIn("id", annotation)

    def test_escalation_list_returns_order_context_for_annotation_order(self):
        raw_ticket = {
            "id": 7,
            "status": "in_progress",
            "after_sale_annotation": {
                "ticket_id": 7,
                "request_type": "return",
                "stage": "pending_order_check",
                "order_id": "A100",
            },
            "order_context": {
                "order_id": "A100",
                "username": "alice",
                "product": "无线耳机",
                "status": "已签收",
                "status_label": "已签收",
                "logistics": "已签收",
                "tracking_no": "SF100",
                "refund_status": None,
            },
        }
        with patch.object(main.db, "list_escalations", return_value=[raw_ticket]):
            result = main.list_escalations("agent")

        self.assertEqual(result[0]["order_context"]["product"], "无线耳机")
        self.assertEqual(result[0]["order_context"]["status_label"], "已签收")
        self.assertNotIn("username", result[0]["order_context"])

    def test_escalation_list_does_not_include_order_context_without_order_id(self):
        raw_ticket = {
            "id": 7,
            "status": "in_progress",
            "after_sale_annotation": {
                "ticket_id": 7,
                "request_type": "return",
                "stage": "pending_info",
                "order_id": None,
            },
            "order_context": None,
        }
        with patch.object(main.db, "list_escalations", return_value=[raw_ticket]):
            result = main.list_escalations("agent")

        self.assertIsNone(result[0]["order_context"])

    def test_saving_annotation_does_not_create_after_sale_request(self):
        ticket = {
            "id": 7,
            "username": "alice",
            "status": "in_progress",
            "assigned_to": "agent",
        }
        annotation = {
            "ticket_id": 7,
            "request_type": "return",
            "stage": "pending_order_check",
            "order_id": "A100",
            "product": "耳机",
            "reason": "商品不合适",
            "item_status": "已签收",
            "note": "客户明确要求退货",
            "operator": "agent",
        }
        with patch.object(main.db, "get_ticket", return_value=ticket), \
             patch.object(main.db, "get_order_for_user", return_value={
                 "order_id": "A100", "username": "alice", "product": "无线耳机",
             }), \
             patch.object(main.db, "upsert_ticket_after_sale_annotation",
                          return_value=annotation) as upsert, \
             patch.object(main.db, "insert_ticket_log") as insert_log, \
             patch.object(main.events.broker, "publish") as publish, \
             patch.object(main.db, "create_after_sale_request") as create_request:
            result = main.save_ticket_after_sale_annotation(
                7,
                main.AfterSaleAnnotationRequest(
                    request_type="return",
                    stage="pending_order_check",
                    order_id="A100",
                    product="耳机",
                    reason="商品不合适",
                    item_status="已签收",
                    note="客户明确要求退货",
                ),
                "agent",
            )

        upsert.assert_called_once()
        insert_log.assert_called_once()
        publish.assert_called_once()
        create_request.assert_not_called()
        self.assertEqual(result["annotation"]["request_type"], "return")
        self.assertEqual(upsert.call_args.kwargs["product"], "无线耳机")

    def test_order_context_endpoint_matches_product_from_customer_order(self):
        ticket = {
            "id": 7,
            "username": "alice",
            "status": "in_progress",
        }
        order = {
            "order_id": "A100",
            "username": "alice",
            "product": "无线耳机",
            "status": "已签收",
            "tracking_no": "SF100",
            "logistics": "已签收",
            "refund_status": None,
        }
        with patch.object(main.db, "get_ticket", return_value=ticket), \
             patch.object(main.db, "get_order_for_user", return_value=order):
            result = main.verify_ticket_order_context(7, "A100", "agent")

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["order_context"]["product"], "无线耳机")
        self.assertEqual(result["order_context"]["status_label"], "已签收")

    def test_order_context_endpoint_does_not_expose_cross_customer_order(self):
        ticket = {"id": 7, "username": "alice", "status": "in_progress"}
        with patch.object(main.db, "get_ticket", return_value=ticket), \
             patch.object(main.db, "get_order_for_user", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                main.verify_ticket_order_context(7, "B200", "agent")

        self.assertEqual(ctx.exception.status_code, 404)

    def test_annotation_rejects_order_owned_by_another_customer(self):
        ticket = {
            "id": 7,
            "username": "alice",
            "status": "in_progress",
        }
        with patch.object(main.db, "get_ticket", return_value=ticket), \
             patch.object(main.db, "get_order_for_user", return_value=None), \
             patch.object(main.db, "upsert_ticket_after_sale_annotation") as upsert:
            with self.assertRaises(HTTPException) as ctx:
                main.save_ticket_after_sale_annotation(
                    7,
                    main.AfterSaleAnnotationRequest(
                        request_type="return",
                        stage="pending_order_check",
                        order_id="B200",
                    ),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 400)
        upsert.assert_not_called()

    def test_annotation_rejects_unknown_stage(self):
        ticket = {"id": 7, "username": "alice", "status": "in_progress"}
        with patch.object(main.db, "get_ticket", return_value=ticket):
            with self.assertRaises(HTTPException) as ctx:
                main.save_ticket_after_sale_annotation(
                    7,
                    main.AfterSaleAnnotationRequest(
                        request_type="return",
                        stage="refund_now",
                    ),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 400)


class AfterSaleQueueApiTests(unittest.TestCase):
    def setUp(self):
        self.request = {
            "id": 12,
            "ticket_id": 7,
            "username": "alice",
            "request_no": "AS-QUEUE-1",
            "order_id": "A100",
            "request_type": "refund",
            "status": "processing",
            "reason": "商品有质量问题",
            "result": "等待人工审核",
            "source_ticket_text": "用户申请退款",
            "source_ticket_status": "in_progress",
            "source_assigned_to": "agent",
        }

    def test_agent_queue_filters_and_returns_business_summary(self):
        with patch.object(main.db, "search_after_sale_requests", return_value=[self.request]) as search:
            result = main.list_after_sale_queue(
                request_type=" refund ",
                status="processing",
                order_id=" A100 ",
                limit=12,
                username="agent",
            )

        search.assert_called_once_with(
            request_type="refund",
            status="processing",
            order_id="A100",
            limit=12,
        )
        self.assertEqual(result[0]["request_type_label"], "退款申请")
        self.assertEqual(result[0]["status_label"], "处理中")
        self.assertNotIn("username", result[0])
        self.assertNotIn("customer_username", result[0])

    def test_queue_rejects_unknown_filters(self):
        with patch.object(main.db, "search_after_sale_requests") as search:
            with self.assertRaises(HTTPException) as ctx:
                main.list_after_sale_queue(
                    request_type="exchange",
                    status="processing",
                    username="agent",
                )

        self.assertEqual(ctx.exception.status_code, 400)
        search.assert_not_called()

    def test_manager_queue_uses_same_validated_filters(self):
        with patch.object(main.db, "search_after_sale_requests", return_value=[self.request]) as search:
            result = main.list_manager_after_sales(
                request_type="refund",
                status="processing",
                order_id="A100",
                limit=20,
                username="manager",
            )

        search.assert_called_once_with(
            request_type="refund",
            status="processing",
            order_id="A100",
            limit=20,
        )
        self.assertEqual(result[0]["ticket_id"], 7)

    def test_staff_audit_returns_request_and_business_labeled_logs(self):
        ticket = {"id": 7, "status": "in_progress"}
        logs = [
            {
                "id": 1,
                "action": "after_sale_submitted",
                "detail": "已提交退款申请 AS-QUEUE-1",
                "operator": "agent",
                "created_at": "2026-09-10 10:00:00",
            }
        ]
        with patch.object(main.db, "get_ticket", return_value=ticket), \
             patch.object(main.db, "get_after_sale_request_for_ticket", return_value=self.request), \
             patch.object(main.db, "list_ticket_logs", return_value=logs):
            result = main.get_ticket_after_sale_audit(7, "agent")

        self.assertEqual(result["request"]["request_no"], "AS-QUEUE-1")
        self.assertEqual(result["audit"][0]["action_label"], "提交售后申请")
        self.assertNotIn("username", result["request"])

    def test_manager_audit_returns_not_found_for_unknown_request(self):
        with patch.object(main.db, "get_after_sale_request", return_value=None), \
             patch.object(main.db, "list_ticket_logs") as logs:
            with self.assertRaises(HTTPException) as ctx:
                main.get_manager_after_sale_audit("AS-MISSING", "manager")

        self.assertEqual(ctx.exception.status_code, 404)
        logs.assert_not_called()


class ReturnRequestSubmissionApiTests(unittest.TestCase):
    def setUp(self):
        self.ticket = {
            "id": 7,
            "username": "alice",
            "status": "in_progress",
            "assigned_to": "agent",
        }
        self.annotation = {
            "ticket_id": 7,
            "request_type": "return",
            "stage": "pending_order_check",
            "order_id": "A100",
            "reason": "商品不合适",
        }
        self.signed_order = {
            "order_id": "A100",
            "username": "alice",
            "product": "无线耳机",
            "status": "已签收",
            "tracking_no": "SF100",
            "logistics": "已签收",
            "refund_status": None,
        }
        self.created_request = {
            "request_no": "AS202609100001",
            "idempotency_key": "key-1",
            "username": "alice",
            "order_id": "A100",
            "request_type": "return",
            "status": "draft",
            "reason": "商品不合适",
            "result": None,
        }
        self.processing_request = {
            **self.created_request,
            "status": "processing",
            "result": "客服已提交退货申请，等待人工审核",
        }

    def _patch_common(self, order=None, annotation=None):
        return patch.multiple(
            main.db,
            get_ticket=patch.DEFAULT,
            get_ticket_after_sale_annotation=patch.DEFAULT,
            get_order_for_user=patch.DEFAULT,
        )

    def test_requires_confirmed_submission(self):
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation):
            with self.assertRaises(HTTPException) as ctx:
                main.submit_ticket_return_request(
                    7,
                    main.ReturnRequestSubmit(confirmed=False),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 400)

    def test_rejects_non_return_annotation(self):
        annotation = {**self.annotation, "request_type": "refund"}
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=annotation):
            with self.assertRaises(HTTPException) as ctx:
                main.submit_ticket_return_request(
                    7,
                    main.ReturnRequestSubmit(confirmed=True),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 409)

    def test_rejects_missing_order_or_reason(self):
        cases = [
            {**self.annotation, "order_id": None},
            {**self.annotation, "reason": None},
        ]
        for annotation in cases:
            with self.subTest(annotation=annotation), \
                 patch.object(main.db, "get_ticket", return_value=self.ticket), \
                 patch.object(main.db, "get_ticket_after_sale_annotation", return_value=annotation):
                with self.assertRaises(HTTPException) as ctx:
                    main.submit_ticket_return_request(
                        7,
                        main.ReturnRequestSubmit(confirmed=True),
                        "agent",
                    )
                self.assertEqual(ctx.exception.status_code, 400)

    def test_rejects_order_that_does_not_belong_to_customer(self):
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "get_order_for_user", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                main.submit_ticket_return_request(
                    7,
                    main.ReturnRequestSubmit(confirmed=True),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 404)

    def test_does_not_create_return_for_unshipped_order(self):
        order = {**self.signed_order, "status": "待发货"}
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "get_order_for_user", return_value=order), \
             patch.object(main.db, "create_after_sale_request") as create_request:
            with self.assertRaises(HTTPException) as ctx:
                main.submit_ticket_return_request(
                    7,
                    main.ReturnRequestSubmit(confirmed=True),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("取消", ctx.exception.detail)
        create_request.assert_not_called()

    def test_does_not_create_return_for_in_transit_order(self):
        order = {**self.signed_order, "status": "运输中"}
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "get_order_for_user", return_value=order), \
             patch.object(main.db, "create_after_sale_request") as create_request:
            with self.assertRaises(HTTPException) as ctx:
                main.submit_ticket_return_request(
                    7,
                    main.ReturnRequestSubmit(confirmed=True),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("签收", ctx.exception.detail)
        create_request.assert_not_called()

    def test_submits_real_return_and_marks_annotation_submitted(self):
        submitted_annotation = {
            **self.annotation,
            "stage": "submitted",
            "product": "无线耳机",
            "operator": "agent",
        }
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "get_order_for_user", return_value=self.signed_order), \
             patch.object(main.db, "find_after_sale_by_idempotency", return_value=None), \
             patch.object(main.db, "create_after_sale_request", return_value=self.created_request) as create_request, \
             patch.object(main.db, "update_after_sale_request", return_value=self.processing_request) as update_request, \
             patch.object(main.db, "upsert_ticket_after_sale_annotation", return_value=submitted_annotation) as upsert, \
             patch.object(main.db, "insert_ticket_log") as insert_log, \
             patch.object(main.events.broker, "publish") as publish:
            result = main.submit_ticket_return_request(
                7,
                main.ReturnRequestSubmit(confirmed=True),
                "agent",
            )

        create_request.assert_called_once()
        self.assertEqual(create_request.call_args.kwargs["ticket_id"], 7)
        self.assertEqual(create_request.call_args.kwargs["status"], "draft")
        update_request.assert_called_once_with(
            "AS202609100001",
            "alice",
            "processing",
            "客服已提交退货申请，等待人工审核",
        )
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.kwargs["stage"], "submitted")
        self.assertEqual(upsert.call_args.kwargs["product"], "无线耳机")
        insert_log.assert_called_once()
        self.assertEqual(insert_log.call_args.args[1], "after_sale_submitted")
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(
            {call.args[0] for call in publish.call_args_list},
            {"ticket_updated", "after_sale_updated"},
        )
        self.assertEqual(result["after_sale"]["request_no"], "AS202609100001")
        self.assertEqual(result["after_sale"]["status"], "processing")
        self.assertEqual(result["annotation"]["stage"], "submitted")

    def test_repeated_submission_returns_existing_request_without_duplicate_create(self):
        existing = {**self.processing_request, "idempotency_key": "key-1"}
        annotation = {**self.annotation, "stage": "submitted"}
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=annotation), \
             patch.object(main.db, "get_order_for_user", return_value=self.signed_order), \
             patch.object(main.db, "find_after_sale_by_idempotency", return_value=existing), \
             patch.object(main.db, "create_after_sale_request") as create_request, \
             patch.object(main.db, "update_after_sale_request") as update_request, \
             patch.object(main.db, "upsert_ticket_after_sale_annotation") as upsert, \
             patch.object(main.db, "insert_ticket_log") as insert_log, \
             patch.object(main.events.broker, "publish") as publish:
            result = main.submit_ticket_return_request(
                7,
                main.ReturnRequestSubmit(confirmed=True),
                "agent",
            )

        create_request.assert_not_called()
        update_request.assert_not_called()
        upsert.assert_not_called()
        insert_log.assert_not_called()
        publish.assert_not_called()
        self.assertEqual(result["after_sale"]["request_no"], "AS202609100001")
        self.assertTrue(result["idempotent"])

    def test_reuses_existing_draft_and_advances_it_to_processing(self):
        existing = {
            **self.created_request,
            "idempotency_key": "key-1",
            "status": "draft",
        }
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "get_order_for_user", return_value=self.signed_order), \
             patch.object(main.db, "find_after_sale_by_idempotency", return_value=existing), \
             patch.object(main.db, "update_after_sale_request", return_value=self.processing_request) as update_request, \
             patch.object(main.db, "upsert_ticket_after_sale_annotation", return_value={
                 **self.annotation, "stage": "submitted", "product": "无线耳机",
             }) as upsert, \
             patch.object(main.db, "insert_ticket_log"), \
             patch.object(main.events.broker, "publish"):
            result = main.submit_ticket_return_request(
                7,
                main.ReturnRequestSubmit(confirmed=True),
                "agent",
            )

        update_request.assert_called_once_with(
            "AS202609100001",
            "alice",
            "processing",
            "客服已提交退货申请，等待人工审核",
        )
        upsert.assert_called_once()
        self.assertEqual(result["after_sale"]["status"], "processing")
        self.assertTrue(result["idempotent"])

    def test_repairs_annotation_when_existing_processing_request_was_partially_submitted(self):
        existing = {
            **self.processing_request,
            "idempotency_key": "key-1",
            "status": "processing",
        }
        submitted_annotation = {
            **self.annotation,
            "stage": "submitted",
            "product": "无线耳机",
        }
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "get_order_for_user", return_value=self.signed_order), \
             patch.object(main.db, "find_after_sale_by_idempotency", return_value=existing), \
             patch.object(main.db, "update_after_sale_request") as update_request, \
             patch.object(main.db, "upsert_ticket_after_sale_annotation", return_value=submitted_annotation) as upsert, \
             patch.object(main.db, "insert_ticket_log"), \
             patch.object(main.events.broker, "publish"):
            result = main.submit_ticket_return_request(
                7,
                main.ReturnRequestSubmit(confirmed=True),
                "agent",
            )

        update_request.assert_not_called()
        upsert.assert_called_once()
        self.assertEqual(result["annotation"]["stage"], "submitted")
        self.assertEqual(result["after_sale"]["status"], "processing")
        self.assertTrue(result["idempotent"])

    def test_submission_failure_does_not_mark_annotation_submitted(self):
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "get_order_for_user", return_value=self.signed_order), \
             patch.object(main.db, "find_after_sale_by_idempotency", return_value=None), \
             patch.object(main.db, "create_after_sale_request", side_effect=RuntimeError("database unavailable")), \
             patch.object(main.db, "upsert_ticket_after_sale_annotation") as upsert:
            with self.assertRaises(RuntimeError):
                main.submit_ticket_return_request(
                    7,
                    main.ReturnRequestSubmit(confirmed=True),
                    "agent",
                )

        upsert.assert_not_called()

    def test_annotation_cannot_manually_mark_submitted(self):
        with patch.object(main.db, "get_ticket", return_value=self.ticket):
            with self.assertRaises(HTTPException) as ctx:
                main.save_ticket_after_sale_annotation(
                    7,
                    main.AfterSaleAnnotationRequest(
                        request_type="return",
                        stage="submitted",
                        order_id="A100",
                        reason="商品不合适",
                    ),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 400)
        self.assertIn("真实", ctx.exception.detail)


class RefundRepairRequestSubmissionApiTests(unittest.TestCase):
    def setUp(self):
        self.ticket = {
            "id": 8,
            "username": "alice",
            "status": "in_progress",
            "assigned_to": "agent",
        }
        self.order = {
            "order_id": "A200",
            "username": "alice",
            "product": "智能手表",
            "status": "已签收",
            "tracking_no": "SF200",
            "logistics": "已签收",
            "refund_status": None,
        }

    def test_submits_real_refund_from_refund_annotation(self):
        annotation = {
            "ticket_id": 8,
            "request_type": "refund",
            "stage": "pending_order_check",
            "order_id": "A200",
            "reason": "商品有质量问题",
        }
        created = {
            "request_no": "AS-REFUND-1",
            "username": "alice",
            "order_id": "A200",
            "request_type": "refund",
            "status": "draft",
            "reason": "商品有质量问题",
        }
        processing = {
            **created,
            "status": "processing",
            "result": "客服已提交退款申请，等待人工审核",
        }
        submitted_annotation = {**annotation, "stage": "submitted", "product": "智能手表"}
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=annotation), \
             patch.object(main.db, "get_order_for_user", return_value=self.order), \
             patch.object(main.db, "find_after_sale_by_idempotency", return_value=None), \
             patch.object(main.db, "create_after_sale_request", return_value=created) as create_request, \
             patch.object(main.db, "update_after_sale_request", return_value=processing) as update_request, \
             patch.object(main.db, "upsert_ticket_after_sale_annotation", return_value=submitted_annotation) as upsert, \
             patch.object(main.db, "insert_ticket_log") as insert_log, \
             patch.object(main.events.broker, "publish") as publish:
            result = main.submit_ticket_refund_request(
                8,
                main.AfterSaleRequestSubmit(confirmed=True),
                "agent",
            )

        create_request.assert_called_once()
        self.assertEqual(create_request.call_args.kwargs["request_type"], "refund")
        self.assertEqual(create_request.call_args.kwargs["ticket_id"], 8)
        update_request.assert_called_once_with(
            "AS-REFUND-1",
            "alice",
            "processing",
            "客服已提交退款申请，等待人工审核",
        )
        self.assertEqual(upsert.call_args.kwargs["stage"], "submitted")
        self.assertEqual(insert_log.call_args.args[1], "after_sale_submitted")
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(result["after_sale"]["request_type"], "refund")
        self.assertEqual(result["annotation"]["stage"], "submitted")

    def test_submits_real_repair_from_repair_annotation(self):
        annotation = {
            "ticket_id": 8,
            "request_type": "repair",
            "stage": "pending_order_check",
            "order_id": "A200",
            "reason": "无法开机",
        }
        created = {
            "request_no": "AS-REPAIR-1",
            "username": "alice",
            "order_id": "A200",
            "request_type": "repair",
            "status": "draft",
            "reason": "无法开机",
        }
        processing = {
            **created,
            "status": "processing",
            "result": "客服已提交维修申请，等待人工处理",
        }
        submitted_annotation = {**annotation, "stage": "submitted", "product": "智能手表"}
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=annotation), \
             patch.object(main.db, "get_order_for_user", return_value=self.order), \
             patch.object(main.db, "find_after_sale_by_idempotency", return_value=None), \
             patch.object(main.db, "create_after_sale_request", return_value=created) as create_request, \
             patch.object(main.db, "update_after_sale_request", return_value=processing), \
             patch.object(main.db, "upsert_ticket_after_sale_annotation", return_value=submitted_annotation), \
             patch.object(main.db, "insert_ticket_log"), \
             patch.object(main.events.broker, "publish"):
            result = main.submit_ticket_repair_request(
                8,
                main.AfterSaleRequestSubmit(confirmed=True),
                "agent",
            )

        create_request.assert_called_once()
        self.assertEqual(create_request.call_args.kwargs["request_type"], "repair")
        self.assertEqual(result["after_sale"]["request_type"], "repair")
        self.assertEqual(result["annotation"]["stage"], "submitted")

    def test_refund_submission_rejects_order_with_existing_refund(self):
        annotation = {
            "ticket_id": 8,
            "request_type": "refund",
            "stage": "pending_order_check",
            "order_id": "A200",
            "reason": "不想要了",
        }
        order = {**self.order, "refund_status": "退款处理中"}
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=annotation), \
             patch.object(main.db, "get_order_for_user", return_value=order), \
             patch.object(main.db, "create_after_sale_request") as create_request:
            with self.assertRaises(HTTPException) as ctx:
                main.submit_ticket_refund_request(
                    8,
                    main.AfterSaleRequestSubmit(confirmed=True),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("退款", ctx.exception.detail)
        create_request.assert_not_called()

    def test_repair_submission_rejects_non_repair_annotation(self):
        annotation = {
            "ticket_id": 8,
            "request_type": "return",
            "stage": "pending_order_check",
            "order_id": "A200",
            "reason": "商品不合适",
        }
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=annotation):
            with self.assertRaises(HTTPException) as ctx:
                main.submit_ticket_repair_request(
                    8,
                    main.AfterSaleRequestSubmit(confirmed=True),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 409)


class CancelRequestSubmissionApiTests(unittest.TestCase):
    def setUp(self):
        self.ticket = {
            "id": 9,
            "username": "alice",
            "status": "in_progress",
            "assigned_to": "agent",
        }
        self.annotation = {
            "ticket_id": 9,
            "request_type": "cancel",
            "stage": "pending_customer_confirmation",
            "order_id": "A300",
            "reason": None,
            "item_status": "未发货",
            "note": "客户确认取消",
        }
        self.order = {
            "order_id": "A300",
            "username": "alice",
            "product": "智能音箱",
            "status": "待发货",
            "tracking_no": None,
            "logistics": None,
            "refund_status": None,
        }
        self.request = {
            "request_no": "AS-CANCEL-1",
            "username": "alice",
            "order_id": "A300",
            "request_type": "cancel",
            "status": "completed",
            "reason": "客户申请取消订单",
            "result": "订单已取消",
        }
        self.completed_annotation = {
            **self.annotation,
            "stage": "completed",
            "product": "智能音箱",
        }

    def test_requires_confirmation_before_cancelling(self):
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "cancel_order_with_request") as cancel:
            with self.assertRaises(HTTPException) as ctx:
                main.submit_ticket_cancel_request(
                    9,
                    main.AfterSaleRequestSubmit(confirmed=False),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 400)
        cancel.assert_not_called()

    def test_rejects_already_shipped_order_without_creating_request(self):
        shipped = {**self.order, "status": "已发货"}
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "get_order_for_user", return_value=shipped), \
             patch.object(main.db, "cancel_order_with_request") as cancel:
            with self.assertRaises(HTTPException) as ctx:
                main.submit_ticket_cancel_request(
                    9,
                    main.AfterSaleRequestSubmit(confirmed=True),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 409)
        self.assertIn("快递", ctx.exception.detail)
        cancel.assert_not_called()

    def test_cancels_unshipped_order_and_completes_annotation(self):
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "get_order_for_user", return_value=self.order), \
             patch.object(main.db, "cancel_order_with_request", return_value={
                 "outcome": "completed",
                 "idempotent": False,
                 "order": {**self.order, "status": "已取消"},
                 "request": self.request,
             }) as cancel, \
             patch.object(main.db, "upsert_ticket_after_sale_annotation", return_value=self.completed_annotation) as upsert, \
             patch.object(main.db, "insert_ticket_log") as insert_log, \
             patch.object(main.events.broker, "publish") as publish:
            result = main.submit_ticket_cancel_request(
                9,
                main.AfterSaleRequestSubmit(confirmed=True),
                "agent",
            )

        cancel.assert_called_once()
        self.assertEqual(cancel.call_args.kwargs["ticket_id"], 9)
        self.assertEqual(cancel.call_args.kwargs["order_id"], "A300")
        upsert.assert_called_once()
        self.assertEqual(upsert.call_args.kwargs["stage"], "completed")
        self.assertEqual(insert_log.call_args.args[1], "after_sale_completed")
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(result["after_sale"]["request_type"], "cancel")
        self.assertEqual(result["after_sale"]["status"], "completed")
        self.assertEqual(result["annotation"]["stage"], "completed")
        self.assertEqual(result["order"]["status_label"], "已取消")

    def test_repeated_cancellation_is_idempotent(self):
        existing = {**self.request, "request_no": "AS-CANCEL-EXISTING"}
        completed_annotation = {**self.completed_annotation}
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=completed_annotation), \
             patch.object(main.db, "get_order_for_user", return_value={**self.order, "status": "已取消"}), \
             patch.object(main.db, "cancel_order_with_request", return_value={
                 "outcome": "completed",
                 "idempotent": True,
                 "order": {**self.order, "status": "已取消"},
                 "request": existing,
             }) as cancel, \
             patch.object(main.db, "upsert_ticket_after_sale_annotation", return_value=completed_annotation), \
             patch.object(main.db, "insert_ticket_log"), \
             patch.object(main.events.broker, "publish"):
            result = main.submit_ticket_cancel_request(
                9,
                main.AfterSaleRequestSubmit(confirmed=True),
                "agent",
            )

        cancel.assert_called_once()
        self.assertTrue(result["idempotent"])
        self.assertEqual(result["after_sale"]["request_no"], "AS-CANCEL-EXISTING")


class AfterSaleRetryApiTests(unittest.TestCase):
    def setUp(self):
        self.ticket = {
            "id": 8,
            "username": "alice",
            "status": "in_progress",
            "assigned_to": "agent",
        }
        self.annotation = {
            "ticket_id": 8,
            "request_type": "refund",
            "stage": "pending_order_check",
            "order_id": "A200",
            "reason": "商品有质量问题",
        }
        self.order = {
            "order_id": "A200",
            "username": "alice",
            "product": "智能手表",
            "status": "已签收",
            "refund_status": None,
        }
        self.failed_request = {
            "request_no": "AS-FAILED-1",
            "idempotency_key": "key-1",
            "username": "alice",
            "order_id": "A200",
            "request_type": "refund",
            "status": "failed",
            "reason": "商品有质量问题",
            "result": "退款审核工单创建失败，请重试",
        }
        self.processing_request = {
            **self.failed_request,
            "status": "processing",
            "result": "客服已重新提交退款申请，等待人工审核",
        }

    def test_retry_reuses_failed_request_without_creating_new_application(self):
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "get_order_for_user", return_value=self.order), \
             patch.object(main.db, "get_after_sale_request", return_value=self.failed_request), \
             patch.object(main.db, "create_after_sale_request") as create_request, \
             patch.object(main.db, "start_after_sale_attempt", return_value={
                 "request_no": "AS-FAILED-1", "attempt_no": 2,
             }), \
             patch.object(main.db, "finish_after_sale_attempt") as finish_attempt, \
             patch.object(main.db, "update_after_sale_request", return_value=self.processing_request) as update_request, \
             patch.object(main.db, "upsert_ticket_after_sale_annotation", return_value={
                 **self.annotation, "stage": "submitted", "product": "智能手表",
             }) as upsert, \
             patch.object(main.db, "insert_ticket_log") as insert_log, \
             patch.object(main.events.broker, "publish") as publish:
            result = main.retry_ticket_after_sale_request(
                8,
                main.AfterSaleRetryRequest(
                    request_no="AS-FAILED-1",
                    confirmed=True,
                ),
                "agent",
            )

        create_request.assert_not_called()
        update_request.assert_called_once_with(
            "AS-FAILED-1",
            "alice",
            "processing",
            "客服已重新提交退款申请，等待人工审核",
        )
        upsert.assert_called_once()
        insert_log.assert_called_once()
        self.assertEqual(publish.call_count, 2)
        self.assertEqual(result["after_sale"]["request_no"], "AS-FAILED-1")
        self.assertEqual(result["after_sale"]["status"], "processing")
        finish_attempt.assert_called_once()

    def test_retry_rejects_non_failed_application(self):
        processing = {**self.failed_request, "status": "processing"}
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_ticket_after_sale_annotation", return_value=self.annotation), \
             patch.object(main.db, "get_after_sale_request", return_value=processing), \
             patch.object(main.db, "update_after_sale_request") as update_request:
            with self.assertRaises(HTTPException) as ctx:
                main.retry_ticket_after_sale_request(
                    8,
                    main.AfterSaleRetryRequest(
                        request_no="AS-FAILED-1",
                        confirmed=True,
                    ),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 409)
        update_request.assert_not_called()

    def test_retry_rejects_missing_request(self):
        with patch.object(main.db, "get_ticket", return_value=self.ticket), \
             patch.object(main.db, "get_after_sale_request", return_value=None):
            with self.assertRaises(HTTPException) as ctx:
                main.retry_ticket_after_sale_request(
                    8,
                    main.AfterSaleRetryRequest(
                        request_no="AS-MISSING",
                        confirmed=True,
                    ),
                    "agent",
                )

        self.assertEqual(ctx.exception.status_code, 404)


class AfterSaleAttemptDbTests(unittest.TestCase):
    class Cursor:
        rowcount = 1
        lastrowid = 31

        def __init__(self, fetchone_values=None):
            self.calls = []
            self.fetchone_values = list(fetchone_values or [])

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchone(self):
            return self.fetchone_values.pop(0) if self.fetchone_values else None

    class Connection:
        def __init__(self, cursor):
            self.cursor_obj = cursor

        def cursor(self):
            return self.cursor_obj

        def begin(self):
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

        def close(self):
            pass

    def test_start_attempt_allocates_next_number_for_existing_request(self):
        cursor = self.Cursor([
            {"request_no": "AS-1", "username": "alice"},
            {"next_attempt": 2},
        ])
        with patch.object(db, "_connect", return_value=self.Connection(cursor)):
            result = db.start_after_sale_attempt(
                request_no="AS-1",
                username="alice",
                operator="agent",
                ticket_id=8,
            )

        self.assertEqual(result["attempt_no"], 2)
        self.assertEqual(result["status"], "started")
        self.assertEqual(result["operator"], "agent")
        self.assertIn("FOR UPDATE", cursor.calls[0][0])
        self.assertIn("MAX(attempt_no)", cursor.calls[1][0])
        self.assertIn("INSERT INTO after_sale_attempts", cursor.calls[2][0])

    def test_start_attempt_can_atomically_claim_failed_request(self):
        cursor = self.Cursor([
            {"request_no": "AS-1", "username": "alice", "status": "failed"},
            {"next_attempt": 1},
        ])
        with patch.object(db, "_connect", return_value=self.Connection(cursor)):
            result = db.start_after_sale_attempt(
                request_no="AS-1",
                username="alice",
                operator="agent",
                require_status="failed",
                processing_result="正在重试",
            )

        self.assertEqual(result["status"], "started")
        self.assertIn("UPDATE after_sale_requests SET status = 'processing'", cursor.calls[3][0])

    def test_finish_attempt_preserves_failure_reason(self):
        cursor = self.Cursor()
        with patch.object(db, "_connect", return_value=self.Connection(cursor)):
            result = db.finish_after_sale_attempt(
                request_no="AS-1",
                username="alice",
                attempt_no=2,
                status="failed",
                error_code="ticket_create_failed",
                error_message="退款审核工单创建失败",
                operator="agent",
            )

        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["error_code"], "ticket_create_failed")
        self.assertEqual(result["error_message"], "退款审核工单创建失败")
        self.assertIn("UPDATE after_sale_attempts", cursor.calls[0][0])

    def test_attempt_summary_reads_error_from_latest_attempt(self):
        cursor = self.Cursor([
            {"attempt_count": 2},
            {
                "last_attempt_status": "succeeded",
                "last_attempt_error": None,
                "last_attempt_operator": "agent",
            },
        ])
        with patch.object(db, "_connect", return_value=self.Connection(cursor)):
            result = db.get_after_sale_attempt_summary("AS-1")

        self.assertEqual(result["attempt_count"], 2)
        self.assertEqual(result["last_attempt_status"], "succeeded")
        self.assertIsNone(result["last_attempt_error"])
        self.assertEqual(result["last_attempt_operator"], "agent")
        self.assertIn("ORDER BY attempt_no DESC", cursor.calls[1][0])


class AfterSaleAnnotationDbTests(unittest.TestCase):
    class Cursor:
        rowcount = 1
        lastrowid = 12

        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchone(self):
            return {
                "ticket_id": 7,
                "request_type": "return",
                "stage": "pending_info",
            }

    class Connection:
        def __init__(self, cursor):
            self.cursor_obj = cursor

        def cursor(self):
            return self.cursor_obj

        def close(self):
            pass

        def begin(self):
            pass

        def commit(self):
            pass

        def rollback(self):
            pass

    def test_upsert_keeps_one_current_annotation_per_ticket(self):
        cursor = self.Cursor()
        with patch.object(db, "_connect", return_value=self.Connection(cursor)):
            result = db.upsert_ticket_after_sale_annotation(
                ticket_id=7,
                request_type="return",
                stage="pending_info",
                order_id=None,
                product="耳机",
                reason="不合适",
                item_status="已签收",
                note="待补订单号",
                operator="agent",
            )

        self.assertEqual(result["ticket_id"], 7)
        sql, params = cursor.calls[0]
        self.assertIn("ON DUPLICATE KEY UPDATE", sql)
        self.assertIn("ticket_id", sql)
        self.assertEqual(params[0], 7)
        self.assertEqual(params[-1], "agent")

    def test_list_escalations_loads_order_context_for_customer_ticket(self):
        class EscalationCursor(self.Cursor):
            def __init__(self):
                super().__init__()
                self._fetch_index = 0

            def fetchall(self):
                return [{
                    "id": 7,
                    "username": "alice",
                    "after_sale_annotation": None,
                }]

            def fetchone(self):
                self._fetch_index += 1
                if self._fetch_index == 1:
                    return {"request_type": "return", "order_id": "A100"}
                return {
                    "order_id": "A100",
                    "username": "alice",
                    "product": "无线耳机",
                    "status": "已签收",
                    "tracking_no": "SF100",
                    "logistics": "已签收",
                    "refund_status": None,
                }

        cursor = EscalationCursor()
        with patch.object(db, "_connect", return_value=self.Connection(cursor)):
            result = db.list_escalations()

        self.assertEqual(result[0]["order_context"]["order_id"], "A100")
        self.assertEqual(result[0]["order_context"]["product"], "无线耳机")
        self.assertIn("username = %s", cursor.calls[-1][0])

    def test_create_after_sale_request_persists_ticket_id(self):
        cursor = self.Cursor()

        class RequestCursor(self.Cursor):
            def fetchone(self):
                if len(self.calls) == 1:
                    return None
                return {
                    "request_no": "AS202609100001",
                    "ticket_id": 7,
                    "username": "alice",
                    "order_id": "A100",
                    "request_type": "return",
                    "status": "draft",
                }

        cursor = RequestCursor()
        with patch.object(db, "_connect", return_value=self.Connection(cursor)), \
             patch.object(db, "_new_after_sale_request_no", return_value="AS202609100001"):
            result = db.create_after_sale_request(
                username="alice",
                order_id="A100",
                request_type="return",
                status="draft",
                idempotency_key="key-1",
                reason="商品不合适",
                detail="客服工单 #7 提交退货申请",
                ticket_id=7,
            )

        insert_sql, insert_params = cursor.calls[1]
        self.assertIn("ticket_id", insert_sql)
        self.assertEqual(insert_params[2], 7)
        self.assertEqual(result["ticket_id"], 7)

    def test_cancel_order_with_request_updates_order_and_inserts_completed_request(self):
        class CancelCursor(self.Cursor):
            def __init__(self):
                super().__init__()
                self._select_count = 0

            def fetchone(self):
                self._select_count += 1
                if self._select_count == 1:
                    return {
                        "order_id": "A300",
                        "username": "alice",
                        "product": "智能音箱",
                        "status": "待发货",
                    }
                if self._select_count == 2:
                    return None
                return None

        cursor = CancelCursor()
        connection = self.Connection(cursor)
        with patch.object(db, "_connect", return_value=connection), \
             patch.object(db, "_new_after_sale_request_no", return_value="AS-CANCEL-DB"):
            result = db.cancel_order_with_request(
                username="alice",
                order_id="A300",
                idempotency_key="cancel-key",
                ticket_id=9,
                reason="客户申请取消订单",
                detail="客服工单 #9 办理取消订单",
            )

        self.assertEqual(result["outcome"], "completed")
        self.assertFalse(result["idempotent"])
        self.assertEqual(result["order"]["status"], "已取消")
        self.assertEqual(result["request"]["status"], "completed")
        self.assertEqual(result["request"]["request_no"], "AS-CANCEL-DB")
        self.assertEqual(len(cursor.calls), 4)
        self.assertIn("FOR UPDATE", cursor.calls[0][0])
        self.assertIn("UPDATE orders SET status = '已取消'", cursor.calls[2][0])
        self.assertIn("INSERT INTO after_sale_requests", cursor.calls[3][0])


if __name__ == "__main__":
    unittest.main()
