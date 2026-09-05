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

    # ---- 大模型（默认 DeepSeek，可切换任意 OpenAI 兼容服务）----
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY", "")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    # LLM_* 是通用覆盖项；未配置时完全兼容旧版 DEEPSEEK_* 配置。
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "")
    LLM_BASE_URL = os.getenv("LLM_BASE_URL", "")
    MODEL = LLM_MODEL or os.getenv("DEEPSEEK_MODEL", "deepseek-chat")
    # 图片识别使用独立视觉模型；留空时走人工降级。
    VISION_MODEL = os.getenv("VISION_MODEL", "")
    VISION_BASE_URL = os.getenv("VISION_BASE_URL") or LLM_BASE_URL or DEEPSEEK_BASE_URL
    VISION_API_KEY = os.getenv("VISION_API_KEY") or LLM_API_KEY or DEEPSEEK_API_KEY
    MAX_IMAGE_BYTES = int(os.getenv("MAX_IMAGE_BYTES", str(8 * 1024 * 1024)))

    # 企业部署基础设施；留空 Redis 时继续使用单机内存降级。
    REDIS_URL = os.getenv("REDIS_URL", "")
    REDIS_KEY_PREFIX = os.getenv("REDIS_KEY_PREFIX", "ai_ticket:")
    DB_POOL_SIZE = int(os.getenv("DB_POOL_SIZE", "10"))
    DB_POOL_TIMEOUT_SECONDS = float(os.getenv("DB_POOL_TIMEOUT_SECONDS", "5"))
    DEFAULT_SLA_MINUTES = int(os.getenv("DEFAULT_SLA_MINUTES", "30"))
    SLA_SCAN_INTERVAL_SECONDS = float(os.getenv("SLA_SCAN_INTERVAL_SECONDS", "30"))
    # 客户超过该时长没有发送新消息时，后台自动结束人工会话；设为 0 可关闭。
    HUMAN_IDLE_TIMEOUT_MINUTES = int(os.getenv("HUMAN_IDLE_TIMEOUT_MINUTES", "30"))

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

    # 登录会话 TTL，默认 8 小时；后续迁移 Redis 时沿用该配置。
    SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "28800"))
    SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "ai_ticket_session")
    # 本地 HTTP 演示需为 false；生产 HTTPS 必须设为 true。
    SESSION_COOKIE_SECURE = os.getenv("SESSION_COOKIE_SECURE", "false").lower() in {
        "1", "true", "yes", "on"
    }
    PHONE_VERIFICATION_TTL_SECONDS = int(os.getenv("PHONE_VERIFICATION_TTL_SECONDS", "300"))
    PHONE_VERIFICATION_MAX_ATTEMPTS = int(os.getenv("PHONE_VERIFICATION_MAX_ATTEMPTS", "5"))

    # ---- 业务参数 ----
    # 分类置信度低于该值时，才升级给人工处理（人工是最后保证）。
    # 按产品规格定为 0.85：更严格地把「拿不准」的样本交给人，配合「极端负面 /
    # 多诉求」强制转人工一起，避免投诉/纠纷被自动误处理。
    # （历史上 0.8 是阈值扫描的最优平衡点；0.85 会更保守、自动处理率略降。）
    CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.85"))

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

    # RAG 向量召回的候选条数（召回 Top5 → LLM 重排 Top3）
    RAG_TOP_K = 5

    # 用户实体画像后台提取线程数（当前服务实例内有界执行）
    PROFILE_MAX_WORKERS = int(os.getenv("PROFILE_MAX_WORKERS", "2"))

    # 本地只读 MCP：发现/调用失败时由 Agent 回退到内置工具。
    MCP_ENABLED = os.getenv("MCP_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    MCP_SERVER_PATH = os.getenv(
        "MCP_SERVER_PATH",
        str(BASE_DIR / "mcp_servers" / "business_server.py"),
    )
    MCP_TIMEOUT_SECONDS = float(os.getenv("MCP_TIMEOUT_SECONDS", "8"))

    # 大模型调用治理：有界队列避免并发请求无限堆积，失败时由路由层转人工。
    LLM_MAX_WORKERS = int(os.getenv("LLM_MAX_WORKERS", "4"))
    LLM_QUEUE_SIZE = int(os.getenv("LLM_QUEUE_SIZE", "32"))
    LLM_REQUEST_TIMEOUT_SECONDS = float(os.getenv("LLM_REQUEST_TIMEOUT_SECONDS", "30"))
    LLM_TOTAL_TIMEOUT_SECONDS = float(os.getenv("LLM_TOTAL_TIMEOUT_SECONDS", "45"))
    LLM_MAX_RETRIES = int(os.getenv("LLM_MAX_RETRIES", "2"))
    LLM_RETRY_BACKOFF_SECONDS = float(os.getenv("LLM_RETRY_BACKOFF_SECONDS", "0.5"))
    LLM_CIRCUIT_FAILURE_THRESHOLD = int(os.getenv("LLM_CIRCUIT_FAILURE_THRESHOLD", "5"))
    LLM_CIRCUIT_RECOVERY_SECONDS = float(os.getenv("LLM_CIRCUIT_RECOVERY_SECONDS", "30"))

    # 开发/演示环境自动准备测试账号和订单；生产环境可设为 false。
    DEMO_DATA_ENABLED = os.getenv("DEMO_DATA_ENABLED", "true").lower() in {
        "1", "true", "yes", "on"
    }
    # 只在演示模式返回测试码；生产环境应接入短信服务且不配置此项。
    PHONE_VERIFICATION_TEST_CODE = os.getenv(
        "PHONE_VERIFICATION_TEST_CODE", "123456" if DEMO_DATA_ENABLED else ""
    )
    PHONE_VERIFICATION_SECRET = os.getenv(
        "PHONE_VERIFICATION_SECRET", "local-phone-verification-secret"
    )

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
