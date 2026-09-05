import unittest
from unittest.mock import patch

from app import llm


class FakeResponse:
    def __init__(self, status_code, payload=None):
        self.status_code = status_code
        self.text = "upstream"
        self._payload = payload or {"choices": [{"message": {"content": "ok"}}]}

    def json(self):
        return self._payload


class LLMResilienceTests(unittest.TestCase):
    def tearDown(self):
        llm.reset_runtime_state()

    def test_transient_rate_limit_retries_then_succeeds(self):
        responses = [FakeResponse(429), FakeResponse(200)]
        with patch.object(llm.Config, "DEEPSEEK_API_KEY", "test-key"), \
             patch.object(llm.Config, "LLM_MAX_RETRIES", 1), \
             patch.object(llm.Config, "LLM_RETRY_BACKOFF_SECONDS", 0), \
             patch.object(llm._session, "post", side_effect=responses) as post:
            result = llm.complete("system", "question")

        self.assertEqual(result, "ok")
        self.assertEqual(post.call_count, 2)

    def test_queue_full_returns_overloaded_error_without_waiting(self):
        with patch.object(llm.Config, "DEEPSEEK_API_KEY", "test-key"), \
             patch.object(llm._executor, "submit", side_effect=llm.QueueFullError):
            with self.assertRaises(llm.LLMOverloadedError):
                llm.complete("system", "question")

    def test_repeated_upstream_failures_open_circuit(self):
        with patch.object(llm.Config, "DEEPSEEK_API_KEY", "test-key"), \
             patch.object(llm.Config, "LLM_MAX_RETRIES", 0), \
             patch.object(llm.Config, "LLM_CIRCUIT_FAILURE_THRESHOLD", 2), \
             patch.object(llm.Config, "LLM_CIRCUIT_RECOVERY_SECONDS", 60), \
             patch.object(llm._session, "post", return_value=FakeResponse(503)) as post:
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    llm.complete("system", "question")
            with self.assertRaises(llm.LLMCircuitOpenError):
                llm.complete("system", "question")

        self.assertEqual(post.call_count, 2)

    def test_openai_compatible_endpoint_can_run_with_local_model(self):
        with patch.object(llm.Config, "LLM_API_KEY", ""), \
             patch.object(llm.Config, "LLM_BASE_URL", "http://127.0.0.1:11434/v1"), \
             patch.object(llm.Config, "MODEL", "qwen2.5:7b"), \
             patch.object(llm.Config, "LLM_MAX_RETRIES", 0), \
             patch.object(llm._session, "post", return_value=FakeResponse(200)) as post:
            result = llm.complete("system", "question")

        self.assertEqual(result, "ok")
        self.assertEqual(post.call_args.args[0], "http://127.0.0.1:11434/v1/chat/completions")
        self.assertEqual(post.call_args.kwargs["json"]["model"], "qwen2.5:7b")


if __name__ == "__main__":
    unittest.main()
