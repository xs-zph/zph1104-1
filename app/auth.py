"""用户认证模块：登录、会话管理、密码校验。

现有数据库中的旧 SHA-256 密码仍可登录；登录成功后会自动升级为
带随机盐的 bcrypt 哈希。会话默认保存在进程内并带有可配置 TTL；配置 Redis 后由 Redis
作为共享会话源，Redis 故障时自动回退本地实现。
"""
from dataclasses import dataclass
import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import uuid
import re

from fastapi import Cookie, Depends, Header, HTTPException

from app import db, redis_store
from app.config import Config

logger = logging.getLogger("app.auth")

_SALT = "ai_ticket_demo_salt"
_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,32}$")
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_ENTERPRISE_ROLES = {"admin", "manager", "agent"}

# token -> session 的内存会话表
@dataclass(frozen=True)
class Session:
    username: str
    expires_at: float


SESSIONS: dict[str, Session] = {}


@dataclass
class PhoneChallenge:
    username: str
    role: str
    phone: str | None
    expires_at: float
    code_digest: str = ""
    attempts: int = 0
    sent: bool = False


PHONE_CHALLENGES: dict[str, PhoneChallenge] = {}
_PHONE_CHALLENGES_LOCK = threading.Lock()


@dataclass
class PasswordResetChallenge:
    username: str
    phone: str
    valid: bool
    expires_at: float
    code_digest: str = ""
    attempts: int = 0


PASSWORD_RESET_CHALLENGES: dict[str, PasswordResetChallenge] = {}
_PASSWORD_RESET_LOCK = threading.Lock()

PERMISSIONS = {
    "ticket.view": "查看转人工工单",
    "ticket.reply": "回复转人工工单",
    "ticket.claim": "接单",
    "ticket.transfer": "转派工单",
    "ticket.resolve": "结束工单",
}

DEFAULT_AGENT_PERMISSIONS = tuple(PERMISSIONS)
MANAGER_PERMISSIONS = ("dashboard.view", "stats.view")


def _bcrypt_payload(password: str) -> bytes:
    """先做固定长度预哈希，避免 bcrypt 的 72 字节截断问题。"""
    return hashlib.sha256(password.encode("utf-8")).digest()


def hash_password(password: str) -> str:
    """生成带随机盐的 bcrypt 密码哈希。"""
    import bcrypt

    return bcrypt.hashpw(_bcrypt_payload(password), bcrypt.gensalt()).decode("ascii")


def verify_password(password: str, hashed: str) -> bool:
    """校验 bcrypt 或旧版固定盐 SHA-256 密码哈希。"""
    if hashed.startswith(("$2a$", "$2b$", "$2y$")):
        import bcrypt

        try:
            return bcrypt.checkpw(_bcrypt_payload(password), hashed.encode("ascii"))
        except (ValueError, UnicodeEncodeError):
            return False

    legacy = hashlib.sha256((_SALT + password).encode("utf-8")).hexdigest()
    return hmac.compare_digest(legacy, hashed)


def _is_legacy_hash(hashed: str) -> bool:
    """判断是否为固定盐 SHA-256 旧格式。"""
    return len(hashed) == 64 and all(c in "0123456789abcdef" for c in hashed.lower())


def seed_users():
    """初始化演示账号（仅当账号不存在时创建）。"""
    demo_phones = {
        "admin": "13800000001",
        "manager": "13800000002",
        "agent": "13800000003",
    }
    if db.get_user_by_username("admin") is None:
        db.create_user("admin", hash_password("123456"), role="admin", phone=demo_phones["admin"])
    else:
        db.set_user_phone_if_empty("admin", demo_phones["admin"])
    if db.get_user_by_username("manager") is None:
        db.create_user(
            "manager", hash_password("123456"), role="manager",
            permissions=list(MANAGER_PERMISSIONS), phone=demo_phones["manager"],
        )
    else:
        db.set_user_phone_if_empty("manager", demo_phones["manager"])
    if db.get_user_by_username("agent") is None:
        db.create_user(
            "agent",
            hash_password("123456"),
            role="agent",
            permissions=list(DEFAULT_AGENT_PERMISSIONS),
            phone=demo_phones["agent"],
        )
    else:
        db.set_user_phone_if_empty("agent", demo_phones["agent"])
    if db.get_user_by_username("user") is None:
        db.create_user("user", hash_password("123456"), role="customer")
    if db.get_user_by_username("demo_user") is None:
        db.create_user("demo_user", hash_password("123456"), role="customer")


def authenticate(username: str, password: str) -> dict | None:
    """校验用户名密码，成功返回用户信息 dict，失败返回 None。"""
    user = db.get_user_by_username(username)
    if user is None:
        return None
    if not user.get("active", 1):
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    if _is_legacy_hash(user["password_hash"]):
        upgraded = hash_password(password)
        db.update_user_password(username, upgraded)
        user = dict(user)
        user["password_hash"] = upgraded
    return user


def validate_username(username: str) -> bool:
    """校验公开注册使用的用户名格式。"""
    return bool(_USERNAME_RE.fullmatch(username.strip()))


def validate_password(password: str) -> bool:
    """P0 密码规则：至少 8 位，同时包含字母和数字。"""
    return (
        len(password) >= 8
        and any(char.isalpha() for char in password)
        and any(char.isdigit() for char in password)
    )


def normalize_phone(phone: str | None, required: bool = True) -> str | None:
    """校验并规范中国大陆手机号；管理员绑定和首次核验共用此规则。"""
    value = (phone or "").strip()
    if not value and not required:
        return None
    if not _PHONE_RE.fullmatch(value):
        raise ValueError("请输入有效的 11 位手机号")
    return value


def mask_phone(phone: str | None) -> str:
    """只展示手机号首三位和末四位。"""
    value = phone or ""
    return f"{value[:3]}****{value[-4:]}" if len(value) == 11 else "未绑定"


def is_phone_verified(user: dict) -> bool:
    """手机号核验时间是建立正式会话的唯一门槛。"""
    return bool(user.get("phone_verified_at"))


def create_phone_challenge(user: dict) -> str:
    """创建仅存在于服务端的登录挑战，不创建正式会话。"""
    role = user.get("role", "customer")
    phone = normalize_phone(user.get("phone"), required=False)
    if role in _ENTERPRISE_ROLES and not phone:
        raise ValueError("企业账号尚未绑定手机号，请联系管理员")
    challenge_id = uuid.uuid4().hex
    with _PHONE_CHALLENGES_LOCK:
        PHONE_CHALLENGES[challenge_id] = PhoneChallenge(
            username=user["username"], role=role, phone=phone,
            expires_at=time.time() + Config.PHONE_VERIFICATION_TTL_SECONDS,
        )
    return challenge_id


def _phone_code_digest(code: str) -> str:
    return hmac.new(
        Config.PHONE_VERIFICATION_SECRET.encode("utf-8"),
        code.encode("ascii"), hashlib.sha256,
    ).hexdigest()


def send_phone_code(challenge_id: str, phone: str | None = None) -> dict:
    """为挑战生成验证码；演示模式可返回测试码，生产模式只返回脱敏手机号。"""
    with _PHONE_CHALLENGES_LOCK:
        challenge = PHONE_CHALLENGES.get(challenge_id)
        if challenge is None or challenge.expires_at <= time.time():
            PHONE_CHALLENGES.pop(challenge_id, None)
            raise ValueError("验证码挑战不存在或已过期")
        normalized = normalize_phone(phone, required=challenge.phone is None)
        if challenge.phone and normalized and normalized != challenge.phone:
            raise ValueError("手机号与账号绑定信息不一致")
        challenge.phone = challenge.phone or normalized
        code = Config.PHONE_VERIFICATION_TEST_CODE
        if not code:
            code = f"{secrets.randbelow(1000000):06d}"
        if not code.isdigit() or len(code) != 6:
            raise ValueError("服务端验证码配置无效")
        challenge.code_digest = _phone_code_digest(code)
        challenge.sent = True
        result = {
            "status": "code_sent",
            "phone_masked": mask_phone(challenge.phone),
            "expires_in": max(0, int(challenge.expires_at - time.time())),
        }
        if Config.DEMO_DATA_ENABLED and Config.PHONE_VERIFICATION_TEST_CODE:
            result["demo_code"] = code
        return result


def verify_phone_challenge(challenge_id: str, phone: str, code: str) -> dict:
    """校验并一次性消费挑战，成功后写入手机号核验时间。"""
    normalized = normalize_phone(phone)
    submitted_code = (code or "").strip()
    if len(submitted_code) != 6 or not submitted_code.isdigit():
        raise ValueError("验证码格式错误")
    with _PHONE_CHALLENGES_LOCK:
        challenge = PHONE_CHALLENGES.get(challenge_id)
        if challenge is None or challenge.expires_at <= time.time() or not challenge.sent:
            PHONE_CHALLENGES.pop(challenge_id, None)
            raise ValueError("验证码挑战不存在、未发送或已过期")
        if normalized != challenge.phone:
            raise ValueError("手机号与验证码不匹配")
        if not hmac.compare_digest(challenge.code_digest, _phone_code_digest(submitted_code)):
            challenge.attempts += 1
            if challenge.attempts >= Config.PHONE_VERIFICATION_MAX_ATTEMPTS:
                PHONE_CHALLENGES.pop(challenge_id, None)
                raise ValueError("验证码错误次数过多，请重新登录")
            raise ValueError("验证码错误")
        PHONE_CHALLENGES.pop(challenge_id, None)

    if challenge.role in _ENTERPRISE_ROLES:
        current = db.get_user_by_username(challenge.username)
        if not current or current.get("phone") != normalized:
            raise ValueError("手机号与账号绑定信息不一致")
    if not db.mark_user_phone_verified(challenge.username, normalized):
        raise ValueError("账号不存在或已停用")
    return db.get_user_by_username(challenge.username) or {
        "username": challenge.username, "role": challenge.role,
        "phone": normalized, "phone_verified_at": True,
    }


def register_customer(username: str, password: str, confirm_password: str) -> dict:
    """创建普通客户账号。角色只在后端决定，永远不会从请求读取。"""
    username = username.strip()
    if not validate_username(username):
        raise ValueError("用户名须为 3-32 位字母、数字或下划线")
    if not validate_password(password):
        raise ValueError("密码至少 8 位，且必须包含字母和数字")
    if password != confirm_password:
        raise ValueError("两次输入的密码不一致")
    if db.get_user_by_username(username) is not None:
        raise ValueError("用户名已存在")
    try:
        db.create_user(username, hash_password(password), role="customer")
    except Exception as exc:  # noqa: BLE001
        # 以数据库唯一约束为最终防线，避免并发注册绕过预检查。
        if db.get_user_by_username(username) is not None:
            raise ValueError("用户名已存在") from exc
        raise
    return db.get_user_by_username(username) or {"username": username, "role": "customer"}


def request_password_reset(username: str, phone: str) -> dict:
    """创建密码重置挑战；不因账号不存在而返回不同的错误。"""
    username = (username or "").strip()
    normalized_phone = normalize_phone(phone)
    user = db.get_user_by_username(username)
    valid = bool(
        user and user.get("active", 1) and user.get("phone_verified_at")
        and user.get("phone") == normalized_phone
    )
    code = Config.PHONE_VERIFICATION_TEST_CODE or f"{secrets.randbelow(1000000):06d}"
    challenge_id = uuid.uuid4().hex
    challenge = PasswordResetChallenge(
        username=username,
        phone=normalized_phone,
        valid=valid,
        expires_at=time.time() + Config.PHONE_VERIFICATION_TTL_SECONDS,
        code_digest=_phone_code_digest(code),
    )
    with _PASSWORD_RESET_LOCK:
        PASSWORD_RESET_CHALLENGES[challenge_id] = challenge

    result = {
        "status": "code_sent",
        "challenge_id": challenge_id,
        "phone_masked": mask_phone(normalized_phone) if valid else "已绑定手机号",
        "expires_in": Config.PHONE_VERIFICATION_TTL_SECONDS,
    }
    if Config.DEMO_DATA_ENABLED and Config.PHONE_VERIFICATION_TEST_CODE:
        result["demo_code"] = code
    return result


def verify_password_reset(
    challenge_id: str, code: str, password: str, confirm_password: str,
) -> bool:
    """校验一次性重置挑战并更新密码；成功或失败后均不接受重复消费。"""
    if not validate_password(password) or password != confirm_password:
        raise ValueError("重置失败，请检查密码格式和两次输入内容")
    submitted_code = (code or "").strip()
    if len(submitted_code) != 6 or not submitted_code.isdigit():
        raise ValueError("验证码格式错误")

    with _PASSWORD_RESET_LOCK:
        challenge = PASSWORD_RESET_CHALLENGES.get(challenge_id)
        if challenge is None or challenge.expires_at <= time.time():
            PASSWORD_RESET_CHALLENGES.pop(challenge_id, None)
            raise ValueError("验证码挑战不存在或已过期")
        if not hmac.compare_digest(
            challenge.code_digest, _phone_code_digest(submitted_code)
        ):
            challenge.attempts += 1
            if challenge.attempts >= Config.PHONE_VERIFICATION_MAX_ATTEMPTS:
                PASSWORD_RESET_CHALLENGES.pop(challenge_id, None)
                raise ValueError("验证码错误次数过多，请重新申请")
            raise ValueError("验证码错误")
        PASSWORD_RESET_CHALLENGES.pop(challenge_id, None)

    if not challenge.valid:
        raise ValueError("重置失败，请检查账号和手机号")
    user = db.get_user_by_username(challenge.username)
    if not user or not user.get("active", 1) or user.get("phone") != challenge.phone:
        raise ValueError("重置失败，请检查账号和手机号")
    if not db.update_user_password(challenge.username, hash_password(password)):
        raise ValueError("重置失败，请稍后重试")
    try:
        db.insert_account_audit(
            challenge.username,
            "password_reset",
            "用户通过已绑定手机号重置密码",
            "self-service",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("密码重置审计写入失败 username=%s: %s", challenge.username, exc)
    delete_sessions_for_user(challenge.username)
    return True


def create_session(username: str) -> str:
    """创建带过期时间的会话，返回 token。"""
    token = uuid.uuid4().hex
    ttl = Config.SESSION_TTL_SECONDS
    SESSIONS[token] = Session(
        username=username,
        expires_at=time.time() + ttl,
    )
    try:
        redis_store.save_session(token, username, ttl)
    except Exception as exc:  # noqa: BLE001
        # Redis 故障不能阻断当前登录；本地会话仍可用。
        logger.warning("Redis 保存会话失败，使用本地会话：%s", exc)
    return token


def get_username(token: str) -> str | None:
    """根据 token 查用户名；无效或过期 token 返回 None。"""
    try:
        redis_username = redis_store.load_session(token)
        if redis_username:
            return redis_username
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 读取会话失败，使用本地会话：%s", exc)
    session = SESSIONS.get(token)
    if session is None:
        return None
    if session.expires_at <= time.time():
        SESSIONS.pop(token, None)
        return None
    return session.username


def is_admin(username: str) -> bool:
    """判断用户是否具有系统管理员角色。"""
    user = db.get_user_by_username(username)
    return user is not None and user.get("role") == "admin"


def is_manager(username: str) -> bool:
    """判断用户是否为启用中的经理账号。"""
    user = db.get_user_by_username(username)
    return user is not None and user.get("role") == "manager" and bool(user.get("active", 1))


def is_agent(username: str) -> bool:
    """判断用户是否为启用中的客服账号。"""
    user = db.get_user_by_username(username)
    return user is not None and user.get("role") == "agent" and bool(user.get("active", 1))


def user_permissions(user: dict) -> set[str]:
    """读取账号权限；系统管理员与经理权限边界固定，客服使用账号级权限。"""
    if user.get("role") == "admin":
        return {"account.manage"}
    if user.get("role") == "manager":
        return set(MANAGER_PERMISSIONS)
    value = user.get("permissions")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = []
    if not isinstance(value, (list, tuple, set)):
        value = DEFAULT_AGENT_PERMISSIONS if user.get("role") == "agent" else []
    return {item for item in value if item in PERMISSIONS}


def has_permission(username: str, permission: str) -> bool:
    user = db.get_user_by_username(username)
    return bool(user and user.get("active", 1) and permission in user_permissions(user))


def delete_session(token: str):
    """删除会话（登出）。"""
    SESSIONS.pop(token, None)
    try:
        redis_store.delete_session(token)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 删除会话失败：%s", exc)


def delete_sessions_for_user(username: str) -> None:
    """密码重置成功后撤销当前进程中该用户的旧会话。"""
    tokens = [token for token, session in SESSIONS.items() if session.username == username]
    for token in tokens:
        delete_session(token)


def extract_token(authorization: str = "", session_token: str = "") -> str:
    """优先从后端 HttpOnly Cookie 取会话；Bearer 仅保留给脚本客户端兼容。"""
    if session_token:
        return session_token
    if authorization.startswith("Bearer "):
        return authorization[7:]
    return ""


def require_user(
    authorization: str = Header(default=""),
    session_token: str = Cookie(default="", alias=Config.SESSION_COOKIE_NAME),
):
    """FastAPI 依赖：校验请求头里的 Bearer token，返回当前用户名。

    未登录或 token 失效时抛出 401。
    """
    username = get_username(extract_token(authorization, session_token))
    if not username:
        raise HTTPException(status_code=401, detail="未登录或登录已失效")
    user = db.get_user_by_username(username)
    if user is None or not user.get("active", 1):
        raise HTTPException(status_code=401, detail="账号已停用或登录已失效")
    return username


def require_admin(
    authorization: str = Header(default=""),
    session_token: str = Cookie(default="", alias=Config.SESSION_COOKIE_NAME),
):
    """FastAPI 依赖：仅系统管理员可访问账号管理接口。返回当前用户名。

    未登录抛 401，非管理员抛 403。
    """
    username = get_username(extract_token(authorization, session_token))
    if not username:
        raise HTTPException(status_code=401, detail="未登录或登录已失效")
    user = db.get_user_by_username(username)
    if user is None or user.get("role") != "admin" or not user.get("active", 1):
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return username


def require_manager(
    authorization: str = Header(default=""),
    session_token: str = Cookie(default="", alias=Config.SESSION_COOKIE_NAME),
):
    """FastAPI 依赖：仅启用中的经理可访问运营数据接口。"""
    username = get_username(extract_token(authorization, session_token))
    if not username:
        raise HTTPException(status_code=401, detail="未登录或登录已失效")
    user = db.get_user_by_username(username)
    if user is None or user.get("role") != "manager" or not user.get("active", 1):
        raise HTTPException(status_code=403, detail="需要经理权限")
    return username


def require_agent(
    authorization: str = Header(default=""),
    session_token: str = Cookie(default="", alias=Config.SESSION_COOKIE_NAME),
):
    """仅启用中的客服账号可访问人工工作台。"""
    username = get_username(extract_token(authorization, session_token))
    if not username:
        raise HTTPException(status_code=401, detail="未登录或登录已失效")
    user = db.get_user_by_username(username)
    if user is None or user.get("role") != "agent" or not user.get("active", 1):
        raise HTTPException(status_code=403, detail="需要客服权限")
    return username


def require_agent_permission(permission: str):
    """构造带账号级权限检查的客服接口依赖。"""
    def dependency(username: str = Depends(require_agent)):
        if not has_permission(username, permission):
            raise HTTPException(status_code=403, detail="当前账号没有该操作权限")
        return username
    return dependency


def get_current_user(
    authorization: str = Header(default=""),
    session_token: str = Cookie(default="", alias=Config.SESSION_COOKIE_NAME),
) -> dict | None:
    """根据 token 返回完整用户信息（含角色），无效返回 None。"""
    username = get_username(extract_token(authorization, session_token))
    if not username:
        return None
    user = db.get_user_by_username(username)
    return user if user and user.get("active", 1) else None
