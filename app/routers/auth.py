"""鉴权接口。

三条设计要点：

1. **登录失败限流按 IP + 滑动窗口**，且只在失败时消耗配额——成功登录不会
   把自己锁出去。
2. **``/api/auth/me`` 同时充当"给我一个 CSRF token"**。前端首次加载时
   如果还没有 CSRF Cookie，就调这个接口拿到它。
3. **登出永远是 200**。Cookie 已失效时重复登出不该报错——那是用户的正常操作，
   报错只会让人以为出了问题。
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response

from app.config import Config
from app.deps import (
    OptionalAuth,
    ReadAuth,
    SessionOnly,
    clear_session_cookies,
    client_ip,
    get_config,
    get_db,
    set_session_cookies,
)
from app.logging import get_logger, kv
from app.ratelimit import SlidingWindow
from app.schemas import LoginRequest, OkBody, SessionInfo
from app.security import issue_csrf, issue_session, verify_password

log = get_logger("auth")

router = APIRouter()

#: 进程内限流器。键是客户端 IP。窗口与上限来自配置，见 app/deps 的说明。
_login_limiter: SlidingWindow | None = None


def _limiter(cfg: Config) -> SlidingWindow:
    """懒初始化限流器。

    不能在模块导入时构造：那时还没有配置（窗口与上限都来自配置）。
    """
    global _login_limiter
    if _login_limiter is None or (
        _login_limiter.limit != cfg.auth.login_max_attempts
        or _login_limiter.window != cfg.auth.login_window_seconds
    ):
        _login_limiter = SlidingWindow(
            limit=cfg.auth.login_max_attempts,
            window_seconds=cfg.auth.login_window_seconds,
        )
    return _login_limiter


def reset_limiter_for_tests() -> None:
    """测试用：清空限流状态。"""
    global _login_limiter
    _login_limiter = None


@router.post("/login", response_model=SessionInfo, summary="登录")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    cfg: Annotated[Config, Depends(get_config)],
) -> SessionInfo:
    ip = client_ip(request)
    limiter = _limiter(cfg)
    allowed, retry_after = limiter.check(ip)
    if not allowed:
        log.warning(
            "auth.login_rate_limited %s", kv(ip=ip, retry_after_s=round(retry_after, 1))
        )
        raise HTTPException(
            status_code=429,
            detail=f"登录尝试过于频繁，请 {int(retry_after) + 1} 秒后再试。",
            headers={"Retry-After": str(int(retry_after) + 1)},
        )

    if not verify_password(payload.password, cfg.auth.password_hash):
        count = limiter.hit(ip)
        log.warning("auth.login_failed %s", kv(ip=ip, attempts_in_window=count))
        raise HTTPException(status_code=401, detail="密码不正确。")

    limiter.reset(ip)
    token = issue_session(cfg.auth.secret_key, cfg.auth.session_epoch)
    csrf = issue_csrf(cfg.auth.secret_key)
    set_session_cookies(response, cfg, token, csrf)
    log.info("auth.login_ok %s", kv(ip=ip))

    return SessionInfo(
        authenticated=True, csrf_token=csrf, session_epoch=cfg.auth.session_epoch
    )


@router.post("/logout", response_model=OkBody, summary="登出")
async def logout(response: Response) -> OkBody:
    clear_session_cookies(response)
    return OkBody(ok=True, message="已登出")


@router.get("/me", response_model=SessionInfo, summary="当前会话状态")
async def me(
    request: Request,
    response: Response,
    principal: OptionalAuth,
    cfg: Annotated[Config, Depends(get_config)],
) -> SessionInfo:
    """未登录也返回 200，``authenticated=False``。

    用 401 表达"未登录"会让前端无法区分"没登录"和"鉴权服务坏了"，
    而首屏本来就需要在两种情况下都拿到 CSRF token。
    """
    authenticated = principal is not None
    existing = request.cookies.get("sk_csrf") or ""
    csrf = existing or issue_csrf(cfg.auth.secret_key)

    if not existing:
        # 补一个 CSRF Cookie。这不涉及任何权限，只是一个随机值 + 签名。
        secure = cfg.server.public_url.startswith("https://")
        response.set_cookie(
            "sk_csrf",
            csrf,
            max_age=cfg.auth.session_ttl_days * 86400,
            httponly=False,
            samesite="lax",
            secure=secure,
            path="/",
        )

    return SessionInfo(
        authenticated=authenticated,
        csrf_token=csrf,
        session_epoch=cfg.auth.session_epoch if authenticated else 0,
    )


@router.get("/whoami", summary="我的身份（用于排查 Key 是否生效）")
async def whoami(principal: ReadAuth, con: Annotated[sqlite3.Connection, Depends(get_db)]):
    """给快捷指令 / 小组件排查用的：能一眼看出这把 Key 是什么 scope。"""
    payload: dict[str, object] = {
        "kind": principal.kind,
        "scope": principal.scope,
        "label": principal.label,
    }
    if principal.kind == "apikey" and principal.label.startswith("key:"):
        try:
            key_id = int(principal.label.split(":")[1])
        except (IndexError, ValueError):
            key_id = None
        if key_id is not None:
            row = con.execute(
                "SELECT name, prefix, created_at, last_used_at FROM api_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
            if row is not None:
                payload["key"] = dict(row)
    return payload
