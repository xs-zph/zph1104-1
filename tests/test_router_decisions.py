from app import router


def test_smalltalk_only_matches_pure_greeting():
    assert router.is_smalltalk("你好")
    assert router.is_smalltalk("谢谢")
    assert not router.is_smalltalk("你好，我的耳机坏了")
    assert not router.is_smalltalk("查一下我的订单")


def test_escalation_intent_wins_for_complaint_signals():
    assert router._is_escalate_intent("我要投诉物流")
    assert router._is_escalate_intent("请帮我转人工")
    assert not router._is_escalate_intent("我的快递到哪了")


def test_data_query_detection_excludes_complaints():
    assert router._is_data_query("查一下我的订单")
    assert router._is_data_query("我的物流到哪了")
    assert not router._is_data_query("我要投诉我的订单")
    assert not router._is_data_query("商品质量太差了")
