"""发布前检查：python scripts/check_release.py

只检查仓库内容和本地可验证的工程条件，不连接真实 LLM、数据库或外部服务。
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

FORBIDDEN_TRACKED = {
    ".env",
    "data/tickets.db",
}

SECRET_PATTERNS = (
    # DeepSeek/OpenAI 风格密钥：只匹配真实前缀，不匹配 .env.example 的中文占位符。
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    # 常见云厂商长密钥；排除变量引用、空值和明确的开发占位符。
    re.compile(
        r"(?i)(?:api[_-]?key|secret[_-]?key)\s*[:=]\s*['\"]?"
        r"(?!\$\{|\$\w|你的|test|example|dummy|changeme|空|config\.|os\.)"
        r"[A-Za-z0-9_./+=-]{20,}"
    ),
)


def tracked_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, check=True, capture_output=True, text=True,
    )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def check_tracked_files(files: list[str]) -> list[str]:
    failures = []
    for name in FORBIDDEN_TRACKED:
        if name in files:
            failures.append(f"禁止提交敏感/运行时文件: {name}")
    return failures


def check_source_secrets(files: list[str]) -> list[str]:
    failures = []
    ignored_names = {".env.example", "README.md"}
    for name in files:
        path = ROOT / name
        if not path.is_file() or path.name in ignored_names:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in SECRET_PATTERNS:
            if pattern.search(content):
                failures.append(f"疑似硬编码密钥/密码: {name}")
                break
    return failures


def check_required_files() -> list[str]:
    required = (
        "README.md",
        ".env.example",
        "pytest.ini",
        "requirements-dev.txt",
        "scripts/evaluate_router.py",
        "data/eval_router_cases.jsonl",
        ".github/workflows/ci.yml",
    )
    return [f"缺少发布文件: {name}" for name in required if not (ROOT / name).exists()]


def main() -> int:
    failures = []
    files = tracked_files()
    failures.extend(check_tracked_files(files))
    failures.extend(check_source_secrets(files))
    failures.extend(check_required_files())

    if failures:
        print("发布检查失败:")
        for failure in failures:
            print(f"- {failure}")
        return 1

    print(f"发布检查通过：已检查 {len(files)} 个 Git 跟踪文件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
