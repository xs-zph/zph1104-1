"""离线路由评测：python scripts/evaluate_router.py

读取 data/eval_router_cases.jsonl，在固定 mock 依赖下运行真实路由逻辑。
用于回归测试路由优先级、人工升级边界和故障降级，不调用真实 LLM、MySQL 或向量模型。
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import router  # noqa: E402

DEFAULT_CASES = ROOT / "data" / "eval_router_cases.jsonl"


def load_cases(path: Path = DEFAULT_CASES) -> list[dict]:
    """加载 JSONL 样例，跳过空行，给出明确的行号错误。"""
    cases = []
    for line_no, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            case = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{line_no} JSON 格式错误: {exc}") from exc
        if not case.get("id") or not case.get("text") or not case.get("expected"):
            raise ValueError(f"{path}:{line_no} 缺少 id/text/expected 字段")
        cases.append(case)
    return cases


def _classify_result(case: dict) -> dict:
    return case.get("classify") or {
        "category": "商品咨询",
        "confidence": 0.95,
        "reason": "评测默认分类",
        "emotion": "中性",
        "emotion_intensity": "normal",
        "multi_intent": False,
    }


def run_case(case: dict) -> tuple[dict, list[str]]:
    """在隔离的依赖环境中执行单条样例，返回结果和失败原因。"""
    mock_kind = case.get("mock", "none")
    cached_record = {
        "category": "商品咨询",
        "confidence": 1.0,
        "status": "auto",
        "reply": "缓存答案",
        "reply_source": "template",
        "emotion": "中性",
        "emotion_intensity": "",
        "multi_intent": 0,
        "route_trace": "[]",
        "latency_ms": 0,
    }
    cache_get = (lambda _key: cached_record) if mock_kind == "cache" else (lambda _key: None)
    rag_hit = None
    if mock_kind == "rag":
        rag_hit = {"question": "退货政策", "answer": "支持七天无理由退货。", "distance": 0.12}
    agent_result = None if mock_kind in {"agent_error", "data_query_error"} else "模拟 Agent 回复"
    data_query_result = None if mock_kind in {"agent_error", "data_query_error"} else "模拟实时查询结果"
    classify_result = None if mock_kind == "classifier_error" else _classify_result(case)
    chat_result = "模拟闲聊回复"

    with patch.object(router.daily, "is_daily", return_value=False), \
            patch.object(router.cache, "get", side_effect=cache_get), \
            patch.object(router.cache, "set"), \
            patch.object(router.memory, "get_history", return_value=[]), \
            patch.object(router.profile, "get_context_for_question", return_value=""), \
            patch.object(router, "_remember_and_schedule"), \
            patch.object(router.rag, "best_answer", return_value=rag_hit), \
            patch.object(router.responder, "chat_reply", return_value=chat_result), \
            patch.object(router, "_classify_safe", return_value=classify_result), \
            patch.object(router, "_run_agent_safe", return_value=agent_result), \
            patch.object(router, "_run_data_query_safe", return_value=data_query_result):
        result = router.process_ticket(case["text"], username="eval-user")

    trace = json.loads(result.get("route_trace") or "[]")
    failures = []
    expected = case["expected"]
    for field in ("category", "status", "reply_source"):
        if field in expected and result.get(field) != expected[field]:
            failures.append(f"{field}: expected={expected[field]!r}, actual={result.get(field)!r}")
    if expected.get("trace") and not any(expected["trace"] in item for item in trace):
        failures.append(f"trace: expected substring={expected['trace']!r}, actual={trace!r}")
    return result, failures


def evaluate(cases: list[dict]) -> dict:
    passed = []
    failed = []
    for case in cases:
        result, failures = run_case(case)
        item = {"id": case["id"], "text": case["text"], "result": result, "failures": failures}
        (passed if not failures else failed).append(item)
    total = len(cases)
    return {
        "total": total,
        "passed": len(passed),
        "failed": len(failed),
        "accuracy": round(len(passed) / total, 4) if total else 0.0,
        "passed_cases": passed,
        "failed_cases": failed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="运行离线路由测试集")
    parser.add_argument("--file", type=Path, default=DEFAULT_CASES, help="JSONL 测试集路径")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON 结果")
    args = parser.parse_args()

    report = evaluate(load_cases(args.file))
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(f"路由评测: {report['passed']}/{report['total']} 通过，准确率 {report['accuracy']:.2%}")
        for item in report["failed_cases"]:
            print(f"[失败] {item['id']}: {'; '.join(item['failures'])}")
            print(f"       输入: {item['text']}")
            print(f"       轨迹: {item['result'].get('route_trace')}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
