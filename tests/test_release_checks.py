from scripts.check_release import check_required_files, check_source_secrets, check_tracked_files


def test_release_required_files_are_present():
    assert check_required_files() == []


def test_release_check_does_not_flag_configuration_references():
    assert check_source_secrets(["app/llm.py", ".env.example", "README.md"]) == []


def test_release_check_rejects_tracked_runtime_secrets():
    assert check_tracked_files(["README.md", ".env", "data/tickets.db"])
