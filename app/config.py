"""配置模块：读取 .env 环境变量，并初始化日志系统。

日志统一写入项目根目录下的 logs/ 文件夹，方便随时查看系统运行情况：
  - logs/app.log      主日志（按大小自动轮转，最多保留 5 个文件）
  - logs/tickets.log  工单处理日志（每张工单的分类、路由、回复记录）
"""
import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录 = 本文件所在目录的上一级
BASE_DIR = Path(__file__).resolve().parent.parent

# 读取项目根目录下的 .env 文件（如果存在）
load_dotenv(BASE_DIR / ".env")


class Config:
    """集中管理所有配置项，方便统一修改。"""

    # ---- 大模型（DeepSeek，OpenAI 兼容接口）----
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    MODEL = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    # ---- 路径 ----
    DATA_DIR = BASE_DIR / "data"
    FAQ_PATH = DATA_DIR / "faq.md"
    DOCS_DIR = DATA_DIR / "docs"      # 产品手册 / 政策文档（文档切块向量化用）
    CHROMA_DIR = DATA_DIR / "chroma"
    LOG_DIR = BASE_DIR / "logs"

    # ---- MySQL 数据库 ----
    MYSQL_HOST = os.getenv("MYSQL_HOST", "localhost")
    MYSQL_PORT = int(os.getenv("MYSQL_PORT", "3308"))
    MYSQL_USER = os.getenv("MYSQL_USER", "root")
    MYSQL_PASSWORD = os.getenv("MYSQL_PASSWORD", "")
    MYSQL_DB = os.getenv("MYSQL_DB", "ai_ticket")

    # ---- 业务参数 ----
    # 分类置信度低于该值时，才升级给人工处理（人工是最后保证）。
    # 0.8 经带真实标签的评测集「阈值扫描」选定（scripts/tune_threshold.py）：
    # 低于 0.8 能兜住「分错且低置信」的难样本，避免投诉/纠纷被自动处理。
    CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.8"))

    # RAG 优先检索：知识库 top-1 的 L2 距离 ≤ 此值才视为「命中」，直接返回知识库答案（不调大模型）。
    # 距离越小越相似（0=完全相同，2=完全相反）。实测：0.4 强相关 / 0.6 相关 / 0.8 弱相关(易答非所问) / 1.1+ 无关。
    RAG_MAX_DISTANCE = float(os.getenv("RAG_MAX_DISTANCE", "0.6"))

    # Agent 的 search_faq 工具检索阈值：比 RAG 优先更宽松。
    # 因为 RAG 优先是「原样返回答案」必须高置信；而 Agent 拿到候选后由大模型自行判断
    # 相关性，可以多给一点弱相关条目，避免「空气炸锅怎么用」这类近似问法被误判为「查不到」。
    RAG_AGENT_MAX_DISTANCE = float(os.getenv("RAG_AGENT_MAX_DISTANCE", "0.8"))

    # 天气查询的默认城市（客户没指定城市时使用）
    DEFAULT_CITY = os.getenv("DEFAULT_CITY", "邵阳")

    # ---- 微信公众号 ----
    # 公众号后台「服务器配置」里的 Token，需与此处一致（用于签名校验）
    WECHAT_TOKEN = os.getenv("WECHAT_TOKEN", "ai_ticket_wechat_token")

    # RAG 检索返回的最相关文档条数
    RAG_TOP_K = 3

    @classmethod
    def ensure_dirs(cls):
        """确保运行时需要的目录都存在。"""
        for d in (cls.DATA_DIR, cls.CHROMA_DIR, cls.DOCS_DIR, cls.LOG_DIR):
            d.mkdir(parents=True, exist_ok=True)


_logging_configured = False


def setup_logging():
    """初始化日志：控制台 + 文件双输出。"""
    global _logging_configured
    if _logging_configured:
        return  # 避免重复添加 handler
    _logging_configured = True

    Config.ensure_dirs()

    # 统一日志格式：时间 | 级别 | 模块 | 内容
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ---- 根日志（app.log，轮转文件，单个最大 2MB，保留 5 个）----
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    file_handler = RotatingFileHandler(
        Config.LOG_DIR / "app.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # 控制台也打印一份，方便本地调试
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    root.addHandler(console)

    # ---- 工单专用日志（tickets.log）----
    ticket_logger = logging.getLogger("tickets")
    ticket_logger.setLevel(logging.INFO)
    ticket_logger.propagate = False  # 避免重复输出到根日志

    ticket_handler = RotatingFileHandler(
        Config.LOG_DIR / "tickets.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    ticket_handler.setFormatter(fmt)
    ticket_logger.addHandler(ticket_handler)

    logging.getLogger("app").info("日志系统初始化完成，日志目录：%s", Config.LOG_DIR)
