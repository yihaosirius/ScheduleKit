"""FastAPI 依赖：会话 / API Key 鉴权、CSRF、管理员门禁。

鉴权有且只有两条路径，且**互不叠加**：

============ ========================================== ==========================
路径          凭据                                        能做什么
============ ========================================== ==========================
Web 会话      ``sk_session`` 签名 Cookie + ``X-CSRF-Token`` 全部操作（写要 CSRF）
API Key      ``Authorization: Bearer sk_...``             读；写需要 write scope
============ ========================================== ==========================

设计取舍：

* **API Key 不走 CSRF**。CSRF 攻击的前提是浏览器会自动携带凭据，而
  ``Authorization`` 头不会被自动携带，所以 JSON API 天然免疫 CSRF。
  反过来给 API Key 加 CSRF 会让快捷指令无法使用。
* **Cookie 会话的写操作强制 CSRF**。这是唯一会被浏览器自动携带的凭据。
* 会话 Cookie 用 ``HttpOnly`` + ``SameSite=Lax`` + ``Secure``，并且**只在 HTTPS 下带 Secure**
  （本地 http 开发时带 Secure 会导致 Cookie 存不下来，让人以为"登录不生效"）。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Cookie, Depends, Header, HTTPException, Request, Response, status

from app.config import Config
from app.logging import get_logger, kv
from app.security import (
    looks_like_api_key,
    parse_session,
    should_refresh,
    verify_csrf,
    hash_api_key,
    issue_session,
)
from app.timeutil import now_utc_iso

log = get_logger("auth")

SESSION_COOKIE = "sk_session"
CSRF_COOKIE = "sk_csrf"

AuthKind = Literal["session", "apikey"]
Scope = Literal["read", "write"]


@dataclass(frozen=True)
class Principal:
    """当前请求的身份。"""

    kind: AuthKind
    scope: Scope
    label: str = ""

    @property
    def can_write(self) -> bool:
        return self.scope == "write"


def get_config(request: Request) -> Config:
    """从 app.state 取配置。测试里可以替换掉。"""
    config = getattr(request.app.state, "config", None)
    if config is None:  # pragma: no cover - 装配错误
        raise HTTPException(status_code=500, detail="服务未正确初始化：缺少配置")
    return config


def get_db(request: Request) -> sqlite3.Connection:
    """请求级连接。

    每个请求一条连接（SQLite 打开成本极低，且避免了跨线程共享连接的问题）。
    由中间件负责关闭，见 app/main.py。
    """
    con = getattr(request.state, "db", None)
    if con is None:  # pragma: no cover - 装配错误
        raise HTTPException(status_code=500, detail="数据库连接缺失")
    return con


def client_ip(request: Request) -> str:
    """真实客户端 IP。

    Caddy 会用 ``X-Real-IP`` 覆盖该头（见 deploy/Caddyfile），所以这里信它；
    但 ``listen_host`` 只绑回环，外部无法绕过 Caddy 直连伪造该头。
    """
    forwarded = request.headers.get("x-real-ip")
    if forwarded:
        return forwarded.strip()
    if request.client:
        return request.client.host
    return "unknown"


# --------------------------------------------------------------------------- #
# 身份解析
# --------------------------------------------------------------------------- #
def _resolve_api_key(con: sqlite3.Connection, token: str) -> Principal | None:
    row = con.execute(
        "SELECT id, name, scope FROM api_keys WHERE key_hash = ? AND revoked_at IS NULL",
        (hash_api_key(token),),
    ).fetchone()
    if row is None:
        log.warning("auth.apikey_unknown %s", kv(prefix=token[:10]))
        return None

    # last_used_at 是给控制台看的"这个 key 还在用吗"。
    # 失败不影响鉴权结果，所以吞掉异常。
    try:
        con.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now_utc_iso(), row["id"]))
    except sqlite3.Error as exc:  # pragma: no cover
        log.warning("auth.apikey_touch_failed %s", exc)

    scope: Scope = "write" if row["scope"] == "write" else "read"
    return Principal(kind="apikey", scope=scope, label=f"key:{row['id']}:{row['name']}")


async def current_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    sk_session: Annotated[str | None, Cookie()] = None,
) -> Principal:
    """解析身份。两条路径都失败时抛 401。"""
    cfg = get_config(request)
    con = get_db(request)

    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        if not looks_like_api_key(token):
            raise HTTPException(status_code=401, detail="API Key 格式不正确")
        principal = _resolve_api_key(con, token)
        if principal is None:
            raise HTTPException(status_code=401, detail="API Key 无效或已吊销")
        return principal

    session = parse_session(
        sk_session or "",
        cfg.auth.secret_key,
        ttl_days=cfg.auth.session_ttl_days,
        epoch=cfg.auth.session_epoch,
    )
    if session is None:
        raise HTTPException(status_code=401, detail="未登录或会话已失效")

    # 滑动续期：老会话换新票。这里只改 state，真正的写入在中间件里做，
    # 因为依赖里拿不到响应对象。
    if should_refresh(session):
        request.state.refresh_session = issue_session(
            cfg.auth.secret_key, cfg.auth.session_epoch
        )
    return Principal(kind="session", scope="write", label="session")


async def optional_principal(
    request: Request,
    authorization: Annotated[str | None, Header()] = None,
    sk_session: Annotated[str | None, Cookie()] = None,
) -> Principal | None:
    """不强制登录。给登录页/健康检查这类端点用。"""
    try:
        return await current_principal(request, authorization, sk_session)
    except HTTPException as exc:
        if exc.status_code == 401:
            return None
        raise


# --------------------------------------------------------------------------- #
# 门禁
# --------------------------------------------------------------------------- #
async def require_session(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    """仅网页会话。用于"必须有人在浏览器前操作"的动作（如永久删除草稿）。"""
    if principal.kind != "session":
        raise HTTPException(status_code=403, detail="该操作仅允许在网页上执行")
    return principal


async def require_write(
    principal: Annotated[Principal, Depends(current_principal)],
) -> Principal:
    if not principal.can_write:
        raise HTTPException(status_code=403, detail="该 API Key 是只读的")
    return principal


async def require_csrf(
    request: Request,
    principal: Annotated[Principal, Depends(require_write)],
    x_csrf_token: Annotated[str | None, Header()] = None,
) -> Principal:
    """写操作门禁。

    API Key 调用跳过 CSRF（不会被浏览器自动携带，天然免疫）；
    Cookie 会话必须带有效 token。
    """
    if principal.kind == "apikey":
        return principal

    cfg = get_config(request)
    if not verify_csrf(x_csrf_token or "", cfg.auth.secret_key):
        log.warning("auth.csrf_rejected %s", kv(path=request.url.path))
        raise HTTPException(status_code=403, detail="CSRF 校验失败，请刷新页面重试")
    return principal


ReadAuth = Annotated[Principal, Depends(current_principal)]
WriteAuth = Annotated[Principal, Depends(require_csrf)]
SessionOnly = Annotated[Principal, Depends(require_session)]
OptionalAuth = Annotated[Principal | None, Depends(optional_principal)]


# --------------------------------------------------------------------------- #
# Cookie 读写
# --------------------------------------------------------------------------- #
def set_session_cookies(response: Response, cfg: Config, session_token: str, csrf_token: str) -> None:
    from app.security import issue_csrf  # 局部导入避免循环

    secure = cfg.server.public_url.startswith("https://")
    max_age = cfg.auth.session_ttl_days * 86400
    response.set_cookie(
        SESSION_COOKIE,
        session_token,
        max_age=max_age,
        httponly=True,
        samesite="lax",
        secure=secure,
        path="/",
    )
    # CSRF token 必须能被 JS 读取（前端把它放进 X-CSRF-Token 头），
    # 所以刻意不加 HttpOnly。
    response.set_cookie(
        CSRF_COOKIE,
        csrf_token or issue_csrf(cfg.auth.secret_key),
        max_age=max_age,
        httponly=False,
        samesite="lax",
        secure=secure,
        path="/",
    )


def clear_session_cookies(response: Response) -> None:
    for name in (SESSION_COOKIE, CSRF_COOKIE):
        response.delete_cookie(name, path="/")


def unauthorized(detail: str = "未登录") -> HTTPException:
    return HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=detail)
