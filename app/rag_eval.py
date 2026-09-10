"""离线 RAG 评测：用带标准 FAQ id 的问题集计算召回和精确率。"""
from __future__ import annotations

import json
import threading
from pathlib import Path

from app import config, rag

_evaluation_cache = {}
_evaluation_lock = threading.Lock()


def load_cases(path: str | Path | None = None) -> list[dict]:
    eval_path = Path(path or (config.Config.DATA_DIR / "rag_eval.jsonl"))
    if not eval_path.exists():
        return []
    cases = []
    for line in eval_path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            cases.append(json.loads(line))
    return cases


def evaluate(path: str | Path | None = None, recall_k: int = 5, precision_k: int = 3) -> dict:
    """运行离线评测，结果不写数据库，便于重复运行和对比参数。"""
    cases = load_cases(path)
    eval_path = Path(path or (config.Config.DATA_DIR / "rag_eval.jsonl"))
    try:
        version = (str(eval_path), eval_path.stat().st_mtime_ns, recall_k, precision_k)
    except FileNotFoundError:
        version = (str(eval_path), 0, recall_k, precision_k)
    with _evaluation_lock:
        if version in _evaluation_cache:
            return _evaluation_cache[version]
    if not cases:
        result = {
            "case_count": 0,
            "faq_case_count": 0,
            "guard_case_count": 0,
            "recall_at_k": None,
            "precision_at_1": None,
            "precision_at_k": None,
            "hit_rate": None,
            "guard_pass_rate": None,
            "recall_k": recall_k,
            "precision_k": precision_k,
            "details": [],
        }
        with _evaluation_lock:
            _evaluation_cache[version] = result
        return result

    recall_hits = 0
    precision_at_1_sum = 0.0
    precision_sum = 0.0
    faq_case_count = 0
    guard_case_count = 0
    guard_passes = 0
    failure_reason_counts = {}
    guard_reason_counts = {}
    details = []
    with rag.suspend_cache_stats():
        for case in cases:
            question = case.get("question", "")
            if case.get("expect_faq", True) is False:
                guard_case_count += 1
                answer = rag.best_answer(question)
                guard_passed = answer is None
                guard_passes += int(guard_passed)
                guard_reason = case.get("guard_reason", "unspecified")
                guard_reason_counts[guard_reason] = guard_reason_counts.get(guard_reason, 0) + 1
                if not guard_passed:
                    reason = "faq_answer_returned"
                    failure_reason_counts[reason] = failure_reason_counts.get(reason, 0) + 1
                details.append({
                    "id": case.get("id", question),
                    "question": question,
                    "expected": [],
                    "recalled": [],
                    "retrieved": [],
                    "recall_hit": None,
                    "precision": None,
                    "guard_passed": guard_passed,
                    "guard_reason": guard_reason,
                    "guard_failure_reason": None if guard_passed else "faq_answer_returned",
                    "answer_question": str(answer.get("question", "")) if answer else None,
                })
                continue
            faq_case_count += 1
            expected = set(str(item) for item in case.get("expected_questions", []))
            if case.get("expected_question"):
                expected.add(str(case["expected_question"]))
            recalled = rag.retrieve(question, top_k=recall_k)
            hits = rag.rerank(question, recalled, top_n=precision_k)
            recalled_questions = [str(item.get("question", "")) for item in recalled]
            retrieved = [str(item.get("question", "")) for item in hits]
            normalized_expected = {rag.normalize_question(item) for item in expected}
            normalized_recalled = [rag.normalize_question(item) for item in recalled_questions]
            normalized_retrieved = [rag.normalize_question(item) for item in retrieved]
            recall_match = any(item in normalized_expected for item in normalized_recalled)
            top_one_relevant = int(bool(normalized_retrieved) and normalized_retrieved[0] in normalized_expected)
            relevant_count = sum(item in normalized_expected for item in normalized_retrieved)
            recall_hits += int(recall_match)
            precision_at_1_sum += top_one_relevant
            precision_sum += relevant_count / max(1, len(normalized_retrieved))
            details.append({
                "id": case.get("id", question),
                "question": question,
                "expected": sorted(expected),
                "recalled": recalled_questions,
                "retrieved": retrieved,
                "recall_hit": recall_match,
                "precision_at_1": float(top_one_relevant),
                "precision": round(relevant_count / max(1, len(normalized_retrieved)), 4),
                "top1_question": retrieved[0] if retrieved else None,
                "top1_distance": hits[0].get("distance") if hits else None,
                "failure_reason": (
                    None
                    if recall_match and top_one_relevant
                    else "recall_miss"
                    if not recall_match
                    else "top1_wrong"
                ),
            })
            failure_reason = details[-1]["failure_reason"]
            if failure_reason:
                failure_reason_counts[failure_reason] = failure_reason_counts.get(failure_reason, 0) + 1

    count = len(cases)
    result = {
        "case_count": count,
        "faq_case_count": faq_case_count,
        "guard_case_count": guard_case_count,
        "recall_k": recall_k,
        "precision_k": precision_k,
        "recall_at_k": round(recall_hits / faq_case_count, 4) if faq_case_count else None,
        "precision_at_1": round(precision_at_1_sum / faq_case_count, 4) if faq_case_count else None,
        "precision_at_k": round(precision_sum / faq_case_count, 4) if faq_case_count else None,
        "hit_rate": round(recall_hits / faq_case_count, 4) if faq_case_count else None,
        "guard_pass_rate": round(guard_passes / guard_case_count, 4) if guard_case_count else None,
        "failure_reason_counts": failure_reason_counts,
        "guard_reason_counts": guard_reason_counts,
        "details": details,
    }
    with _evaluation_lock:
        _evaluation_cache[version] = result
    return result
