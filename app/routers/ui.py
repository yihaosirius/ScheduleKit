"""页面外壳路由：SPA 宿主、manifest、Service Worker。

这个项目**没有服务端模板**——前端是 Vue SPA，构建产物放在 ``app/static/spa/``。
所以这里只做三件事：

1. 把 ``/`` 与任意前端路由（``/drafts/1`` 之类的 history 路由）都交给
   ``index.html``，由前端自己做路由。
2. 出 ``/manifest.webmanifest``（PWA 安装信息）。
3. 出 ``/sw.js``（Service Worker），并**确保它不被缓存**。

为什么 SW 与 manifest 由后端出而不是放静态文件：它们的 ``Content-Type``
必须精确（``application/manifest+json``、``text/javascript``），
而且必须带 ``no-cache``。交给 Caddy 按扩展名猜不如显式写死可靠。

**深链接的关键**：确认页地址是 ``/#/drafts/{id}`` 这种带 ``#`` 的形式
（见 app/routers/ingest.py 的 ``confirm_url``）。用 hash 路由而不是
history 路由，是为了让快捷指令生成的链接在任何静态托管/反代配置下都能直接打开——
history 路由要求服务端把未知路径都回退到 index.html，多一个会配错的地方。
"""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse, JSONResponse, Response

from app.logging import get_logger, kv

log = get_logger("ui")

router = APIRouter()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"
SPA_DIR = STATIC_DIR / "spa"
ICONS_DIR = STATIC_DIR / "icons"

MANIFEST = {
    "name": "ScheduleKit 任务管理",
    "short_name": "ScheduleKit",
    "description": "围绕学业构建的待办事项系统：有序表 / 无序表、拍照录入、课表上下文",
    "lang": "zh-CN",
    "dir": "ltr",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "orientation": "portrait",
    "background_color": "#f5f5f7",
    "theme_color": "#1c1e21",
    "icons": [
        {"src": "/static/icons/icon-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
        {"src": "/static/icons/icon-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
        {
            "src": "/static/icons/icon-maskable-512.png",
            "sizes": "512x512",
            "type": "image/png",
            "purpose": "maskable",
        },
        {"src": "/static/icons/apple-touch-icon.png", "sizes": "180x180", "type": "image/png"},
    ],
}


@router.get("/manifest.webmanifest", summary="PWA manifest")
async def manifest() -> Response:
    return JSONResponse(
        MANIFEST,
        media_type="application/manifest+json",
        # manifest 改了必须立刻生效：缓存住会让"名称/图标改了没反应"
        headers={"Cache-Control": "no-cache"},
    )


@router.get("/sw.js", summary="Service Worker")
async def service_worker() -> Response:
    path = STATIC_DIR / "sw.js"
    if not path.exists():
        raise HTTPException(status_code=404, detail="sw.js 尚未部署")
    return FileResponse(
        path,
        media_type="text/javascript",
        headers={
            # 绝不缓存 sw.js：缓存住之后新版本永远装不上，
            # 而用户看到的是"改了没反应"，这是 PWA 最难排查的一类问题。
            "Cache-Control": "no-cache",
            # 允许 SW 控制根路径下的所有页面
            "Service-Worker-Allowed": "/",
        },
    )


@router.get("/", include_in_schema=False)
async def index() -> Response:
    return _spa_index()


#: 前端 history 路由的前缀。用 hash 路由后其实用不到，
#: 但保留回退可以让"用户手敲了 /drafts"这种直链也能打开而不是 404。
SPA_PREFIXES = ("/drafts", "/settings", "/courses", "/status", "/memories", "/login")


@router.get("/{full_path:path}", include_in_schema=False)
async def spa_fallback(full_path: str) -> Response:
    """把未匹配的路径交给前端。

    两条排除规则很重要：
    * ``/api/`` 与 ``/healthz`` 绝不能回退成 HTML——否则接口 404 会变成
      "返回了 index.html"，前端解析 JSON 失败后报一个完全无关的错。
    * 带文件扩展名的路径（``/foo.js``）也不回退，直接 404，
      否则一个写错的资源路径会静默返回 HTML，浏览器报的错会指向别处。
    """
    if full_path.startswith(("api/", "healthz")):
        raise HTTPException(status_code=404, detail="接口不存在")

    target = SPA_DIR / full_path
    if full_path and target.is_file():
        return FileResponse(target)

    if Path(full_path).suffix:
        raise HTTPException(status_code=404, detail=f"静态资源不存在：/{full_path}")

    if not any(full_path == prefix.lstrip("/") or full_path.startswith(prefix.lstrip("/") + "/")
               for prefix in SPA_PREFIXES):
        # 未知路径也交给前端：前端有自己的 404 页面，比后端的纯文本好看
        log.info("ui.spa_fallback %s", kv(path=f"/{full_path}"))
    return _spa_index()


def _spa_index() -> Response:
    index = SPA_DIR / "index.html"
    if not index.exists():
        # 未构建前端时的提示要具体，否则会让人以为是路由写错了
        return Response(
            content=(
                "<!doctype html><meta charset='utf-8'>"
                "<title>ScheduleKit</title>"
                "<h1>前端尚未构建</h1>"
                "<p>服务端已在运行，但 <code>app/static/spa/index.html</code> 不存在。</p>"
                "<p>本地构建：<code>cd web &amp;&amp; pnpm install &amp;&amp; pnpm build</code>"
                "（产物会写到 <code>app/static/spa/</code>）。</p>"
                "<p>接口仍然可用：<code>GET /healthz</code>、<code>GET /api/tasks</code>。</p>"
            ),
            media_type="text/html; charset=utf-8",
            status_code=503,
            headers={"Cache-Control": "no-cache"},
        )
    return FileResponse(
        index,
        media_type="text/html",
        # index.html 绝不缓存：缓存住会让用户一直看到旧的外壳，
        # 而里面的 JS 已经换了版本，可能直接白屏。
        headers={"Cache-Control": "no-cache"},
    )


def manifest_json() -> str:
    """给测试用：确认 manifest 可序列化且字段齐全。"""
    return json.dumps(MANIFEST, ensure_ascii=False)
