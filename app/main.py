"""FastAPI 装配。

请求生命周期（每一步都在这里，别处不再包一层）：

1. 生成 trace id，写 ``request.state`` 与 contextvar；响应头回写 ``X-Trace-Id``。
2. 打开请求级 SQLite 连接（鉴权与业务共用同一条，避免同一请求两次开库）。
3. 路由处理。
4. 若鉴权时判定会话需要滑动续期，在这里下发新 Cookie。
5. 补安全响应头、关闭连接、打 ``request.end`` 日志。

**请求体大小限制在这里做**，而不是在 Caddy 里：应用要给出可读的 413 与中文说明，
而且 Caddyfile 的 ``request_body`` 指令是 2.10 才有的，赌版本不划算。
"""

from __future__ import annotations

import json
import time
import traceback
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Config, load_config
from app.db import connect
from app.logging import current_trace_id, get_logger, kv, new_trace_id, reset_trace_id, set_trace_id, setup_logging
from app.paths import ensure_dirs
from app.timeutil import now_utc_iso

log = get_logger("http")

#: 这些路径不记 request.start/end，避免静态资源与健康检查刷屏
QUIET_PATHS = ("/healthz", "/favicon.ico")


def _wants_json(request: Request) -> bool:
    """判断客户端是否期待 JSON 错误体。

    ``/api/*`` 一律 JSON；页面请求走 JSON 也无害（前端统一按 JSON 解析错误）。
    """
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get(
        "accept", ""
    )


def create_app(config: Config | None = None) -> FastAPI:
    cfg = config or load_config()
    setup_logging("INFO")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.config.validate_for_startup()
        ensure_dirs(app.state.config)
        log.info(
            "app.start %s",
            kv(
                config=str(app.state.config.path),
                data_dir=str(app.state.config.data_dir),
                timezone=app.state.config.server.timezone,
                provider=app.state.config.llm.provider,
                host=app.state.config.server.listen_host,
                port=app.state.config.server.listen_port,
            ),
        )
        yield
        log.info("app.stop %s", kv())

    app = FastAPI(
        title="ScheduleKit",
        version="0.1.0",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.config = cfg

    @app.middleware("http")
    async def pipeline(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        trace_id = new_trace_id()
        token = set_trace_id(trace_id)
        request.state.trace_id = trace_id
        quiet = request.url.path in QUIET_PATHS
        started = time.perf_counter()

        # 每次都从磁盘重新读配置。
        #
        # 这不是"图省事"，而是**热加载的正确实现方式**：控制台改 [llm] 之后
        # 必须立刻生效，而把"新值"同步到内存里的 Config 对象需要知道改了哪些字段
        # （漏一个就会出现"改了没反应"）。重新读一遍文件是小开销（几 KB 的 TOML）
        # 换来的是不可能漏字段。
        #
        # 读失败（文件被编辑器写坏等）时保留上一份内存配置：宁可让旧配置继续服务，
        # 也不要因为一次配置写坏就让整个服务开始报 500。
        try:
            fresh = load_config(app.state.config.path)
            app.state.config = fresh
        except Exception as exc:  # noqa: BLE001
            fresh = app.state.config
            log.warning("config.reload_failed %s", kv(error=str(exc)))

        if not quiet:
            log.info(
                "request.start %s",
                kv(
                    method=request.method,
                    path=request.url.path,
                    query=request.url.query or None,
                    client=request.headers.get("x-real-ip") or (request.client.host if request.client else None),
                ),
            )

        # 请求体大小：先看 Content-Length 快速拒绝，避免把大 body 读进内存再判断。
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > fresh.server.max_request_bytes:
            return _error_response(
                request,
                status_code=413,
                detail=(
                    f"请求体过大：{int(declared)} 字节，"
                    f"上限 {fresh.server.max_request_bytes} 字节"
                ),
                trace_id=trace_id,
            )

        con = connect(fresh.db_path)
        request.state.db = con
        response: Response
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001 - 兜底，必须保证连接被关闭
            log.error(
                "request.unhandled %s",
                kv(path=request.url.path, error=type(exc).__name__, detail=str(exc)),
            )
            log.error("%s", traceback.format_exc().strip().splitlines()[-1])
            response = _error_response(
                request, status_code=500, detail="服务端内部错误", trace_id=trace_id
            )
        finally:
            con.close()

        # 滑动续期：依赖里只把新票放进 state，这里才写得进响应
        refresh = getattr(request.state, "refresh_session", None)
        if refresh:
            from app.deps import CSRF_COOKIE, SESSION_COOKIE

            secure = fresh.server.public_url.startswith("https://")
            response.set_cookie(
                SESSION_COOKIE,
                refresh,
                max_age=fresh.auth.session_ttl_days * 86400,
                httponly=True,
                samesite="lax",
                secure=secure,
                path="/",
            )
            # 会话续期时如果 CSRF Cookie 丢了，一并补上——否则用户下一次写操作
            # 会被 403 拦住，而且看起来完全没有原因。
            if CSRF_COOKIE not in request.cookies:
                from app.security import issue_csrf

                response.set_cookie(
                    CSRF_COOKIE,
                    issue_csrf(fresh.auth.secret_key),
                    max_age=fresh.auth.session_ttl_days * 86400,
                    httponly=False,
                    samesite="lax",
                    secure=secure,
                    path="/",
                )

        response.headers["X-Trace-Id"] = trace_id
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")

        if not quiet:
            log.info(
                "request.end %s",
                kv(
                    status=response.status_code,
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 1),
                ),
            )
        reset_trace_id(token)
        return response

    _install_error_handlers(app)
    _install_routes(app, cfg)
    return app


def _error_response(request: Request, *, status_code: int, detail: Any, trace_id: str) -> JSONResponse:
    payload: dict[str, Any] = {"detail": detail, "trace_id": trace_id}
    if _wants_json(request):
        return JSONResponse(payload, status_code=status_code)
    # 页面请求：也给 JSON——前端统一解析错误体，不做两套。
    return JSONResponse(payload, status_code=status_code)


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", current_trace_id())
        if exc.status_code >= 500:
            log.error("http.error %s", kv(status=exc.status_code, detail=exc.detail))
        else:
            log.warning("http.rejected %s", kv(status=exc.status_code, detail=exc.detail))
        return JSONResponse(
            {"detail": exc.detail, "trace_id": trace_id}, status_code=exc.status_code
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        trace_id = getattr(request.state, "trace_id", current_trace_id())
        # 把 pydantic 的错误压成一句人话。前端只展示 detail。
        first = exc.errors()[0] if exc.errors() else {}
        location = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
        message = first.get("msg", "参数不合法")
        detail = f"{location}: {message}" if location else message
        log.warning("http.validation_failed %s", kv(path=request.url.path, detail=detail))
        return JSONResponse({"detail": detail, "trace_id": trace_id}, status_code=422)


def _install_routes(app: FastAPI, cfg: Config) -> None:
    from fastapi.staticfiles import StaticFiles

    from app.routers import auth as auth_router
    from app.routers import courses as courses_router
    from app.routers import drafts as drafts_router
    from app.routers import ingest as ingest_router
    from app.routers import memories as memories_router
    from app.routers import settings as settings_router
    from app.routers import tasks as tasks_router
    from app.routers import ui as ui_router

    # 静态资源：图标与 Vite 产物。
    #
    # `/static/spa/` 必须挂载：Vite 产物的 `index.html` 里引用的是
    # `/assets/...`（绝对路径），但**页面路由可能是深层 hash 路由**——
    # 只要引用是绝对的，`/assets/` 也能被下面的 mount 命中，所以产物与图标
    # 用两个挂载点分别暴露，语义更清楚：
    #   /static/icons/...  → 手工维护的图标
    #   /static/spa/...    → 构建产物
    #   /assets/...        → 同上（Vite 默认的产物引用路径）
    static_dir = Path(__file__).resolve().parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    spa_dir = static_dir / "spa"
    assets_dir = spa_dir / "assets"
    if assets_dir.exists():
        # 带内容哈希，可以长缓存；Caddy 侧也有同样的规则（双层保险不冲突）
        app.mount("/assets", StaticFiles(directory=assets_dir), name="assets")

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "version": app.version, "time": now_utc_iso()}

    app.include_router(auth_router.router, prefix="/api/auth", tags=["auth"])
    app.include_router(tasks_router.router, prefix="/api/tasks", tags=["tasks"])
    app.include_router(ingest_router.router, prefix="/api/ingest", tags=["ingest"])
    app.include_router(drafts_router.router, prefix="/api/drafts", tags=["drafts"])
    app.include_router(memories_router.router, prefix="/api/memories", tags=["memories"])
    app.include_router(courses_router.router, prefix="/api/courses", tags=["courses"])
    app.include_router(courses_router.timetable_router, prefix="/api/timetable", tags=["timetable"])
    app.include_router(settings_router.router, prefix="/api", tags=["settings"])
    # SPA 回退必须**最后**注册：它带通配路径，注册早了会吃掉前面所有路由
    app.include_router(ui_router.router, include_in_schema=False)
