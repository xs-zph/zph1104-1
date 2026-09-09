from scripts.evaluate_router import DEFAULT_CASES, evaluate, load_cases


def test_router_evaluation_dataset_is_green():
    report = evaluate(load_cases(DEFAULT_CASES))
    assert report["total"] >= 15
    assert report["failed"] == 0, report["failed_cases"]
