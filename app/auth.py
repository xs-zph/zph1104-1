"""用户认证模块：登录、会话管理、密码校验。

演示版采用：
  - 密码用 sha256 + 固定盐 哈希后存储（生产环境建议换成 bcrypt/passlib）
  - 会话用内存字典保存 token（服务重启后失效，演示场景足够）
"""
import hashlib
import uuid

from fastapi import Header, HTTPException

from app import db

_SALT = "ai_ticket_demo_salt"

# token -> username 的内存会话表
SESSIONS: dict[str, str] = {}


def hash_password(password: str) -> str:
    """对密码做加盐哈希。"""
    return hashlib.sha256((_SALT + password).encode("utf-8")).hexdigest()


def verify_password(password: str, hashed: str) -> bool:
    """校验密码是否匹配。"""
    return hash_password(password) == hashed


def seed_users():
    """初始化演示账号（仅当账号不存在时创建）。"""
    if db.get_user_by_username("admin") is None:
        db.create_user("admin", hash_password("123456"), role="admin")
    if db.get_user_by_username("user") is None:
        db.create_user("user", hash_password("123456"), role="customer")


def authenticate(username: str, password: str) -> dict | None:
    """校验用户名密码，成功返回用户信息 dict，失败返回 None。"""
    user = db.get_user_by_username(username)
    if user is None:
        return None
    if not verify_password(password, user["password_hash"]):
        return None
    return user


def create_session(username: str) -> str:
    """创建会话，返回 token。"""
    token = uuid.uuid4().hex
    SESSIONS[token] = username
    return token


def get_username(token: str) -> str | None:
    """根据 token 查用户名，token 无效返回 None。"""
    return SESSIONS.get(token)


def delete_session(token: str):
    """删除会话（登出）。"""
    SESSIONS.pop(token, None)


def _extract_token(authorization: str) -> str:
    """从 Authorization 请求头里取出 Bearer token。"""
    token = ""
    if authorization.startswith("Bearer "):
        token = authorization[7:]
    return token


def require_user(authorization: str = Header(default="")):
    """FastAPI 依赖：校验请求头里的 Bearer token，返回当前用户名。

    未登录或 token 失效时抛出 401。
    """
    username = get_username(_extract_token(authorization))
    if not username:
        raise HTTPException(status_code=401, detail="未登录或登录已失效")
    return username


def require_admin(authorization: str = Header(default="")):
    """FastAPI 依赖：仅管理员可访问（客服后台）。返回当前用户名。

    未登录抛 401，非管理员抛 403。
    """
    username = get_username(_extract_token(authorization))
    if not username:
        raise HTTPException(status_code=401, detail="未登录或登录已失效")
    user = db.get_user_by_username(username)
    if user is None or user.get("role") != "admin":
        raise HTTPException(status_code=403, detail="需要管理员权限")
    return username


def get_current_user(authorization: str = Header(default="")) -> dict | None:
    """根据 token 返回完整用户信息（含角色），无效返回 None。"""
    username = get_username(_extract_token(authorization))
    if not username:
        return None
    return db.get_user_by_username(username)
