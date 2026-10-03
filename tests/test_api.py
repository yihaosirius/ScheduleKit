"""端到端接口测试：走真实的 HTTP 层。

这里不 mock 任何东西（LLM 用 ``provider = "mock"``，它本身是确定性的），
所以它测的是"整条链路能不能用"：鉴权 → 上传 → 识别 → 草稿 → 确认 → 入库。

相比服务层单测，这一层专门负责抓**装配问题**：路由前缀、依赖注入、
CSRF 与 Cookie 的配合、状态码映射。这类问题在单测里看不见。
"""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from PIL import Image


def make_png(color: tuple[int, int, int] = (200, 180, 160)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buffer, format="PNG")
    return buffer.getvalue()


# --------------------------------------------------------------------------- #
# 健康检查与外壳
# --------------------------------------------------------------------------- #
async def test_healthz(anon) -> None:
    response = await anon.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert "X-Trace-Id" in response.headers


async def test_manifest_is_pwa_ready(anon) -> None:
    response = await anon.get("/manifest.webmanifest")
    assert response.status_code == 200
    assert "manifest+json" in response.headers["content-type"]
    body = response.json()
    assert body["display"] == "standalone"
    assert body["lang"] == "zh-CN"
    assert body["start_url"] == "/"
    purposes = {icon.get("purpose") for icon in body["icons"]}
    assert "maskable" in purposes, "缺少 maskable 图标，Android 上会显示成方框"
    assert response.headers["cache-control"] == "no-cache"


async def test_api_404_is_json_not_html(anon) -> None:
    """``/api/*`` 的 404 必须是 JSON。

    如果被 SPA 回退吃掉变成 index.html，前端解析 JSON 失败后会报一个
    与真实原因完全无关的错——这是最难查的一类问题。
    """
    response = await anon.get("/api/does-not-exist")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert "detail" in response.json()


async def test_icons_are_served(anon) -> None:
    """图标必须能取到。

    这条测试的由来：`/static/*` 一开始**根本没有挂载**——前端引用了图标，
    但服务端没有任何路由处理它，于是浏览器拿到 404。
    这是纯装配问题（代码都对，只是少了一行 mount），单测看不见，
    只有真的去取一次才会发现。
    """
    for path in (
        "/static/icons/icon-192.png",
        "/static/icons/icon-512.png",
        "/static/icons/apple-touch-icon.png",
        "/static/icons/favicon-32.png",
        "/static/icons/icon-maskable-512.png",
    ):
        response = await anon.get(path)
        assert response.status_code == 200, f"{path} 取不到"
        assert response.headers["content-type"].startswith("image/png")
        assert response.content.startswith(b"\x89PNG"), f"{path} 不是 PNG"


async def test_unknown_static_file_404s(anon) -> None:
    """不存在的静态资源要 404，**不能**回退成 index.html。

    否则一个写错的资源路径会静默返回 HTML，浏览器报的错会指向别处。
    """
    response = await anon.get("/static/icons/nope.png")
    assert response.status_code == 404


async def test_spa_assets_are_served_when_built(anon, configured) -> None:
    """Vite 产物挂着 `/assets/` 前缀，必须能取到。

    产物不存在时跳过（还没构建过前端），而不是让测试失败——
    后端开发过程中不该被"前端没构建"卡住。
    """
    spa = Path(__file__).resolve().parent.parent / "app" / "static" / "spa"
    assets = spa / "assets"
    if not assets.exists():
        pytest.skip("前端产物尚未构建（app/static/spa/assets 不存在）")

    files = [p for p in assets.glob("*.js")]
    assert files, "assets 目录里没有 js 产物"

    response = await anon.get(f"/assets/{files[0].name}")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


async def test_spa_index_is_served_when_built(anon) -> None:
    spa = Path(__file__).resolve().parent.parent / "app" / "static" / "spa" / "index.html"
    response = await anon.get("/")
    if spa.exists():
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        # SPA 外壳必须不缓存：缓存住会让用户一直看到旧外壳 + 新 JS
        assert response.headers.get("cache-control") == "no-cache"
    else:
        # 没构建时给的是 503 的说明页，而不是空白 404 —— 更好排查
        assert response.status_code == 503
        assert "前端尚未构建" in response.text


# --------------------------------------------------------------------------- #
# 鉴权边界
# --------------------------------------------------------------------------- #
async def test_tasks_require_auth(anon) -> None:
    response = await anon.get("/api/tasks")
    assert response.status_code == 401


async def test_ingest_requires_auth(anon) -> None:
    response = await anon.post("/api/ingest", data={"text": "写作业"})
    assert response.status_code == 401


async def test_write_requires_csrf(configured) -> None:
    """Cookie 会话的写操作必须带 CSRF token。

    这是唯一会被浏览器自动携带的凭据，所以它是 CSRF 的唯一边界。
    """
    import httpx

    from app.main import create_app
    from tests._support import VALID_PASSWORD

    app = create_app(configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        await client.post("/api/auth/login", json={"password": VALID_PASSWORD})
        # 故意不带 X-CSRF-Token
        response = await client.post("/api/tasks", json={"title": "x", "priority": 1})
        assert response.status_code == 403
        assert "CSRF" in response.json()["detail"]


async def test_wrong_password_rejected(anon) -> None:
    response = await anon.post("/api/auth/login", json={"password": "nope"})
    assert response.status_code == 401


async def test_login_sets_both_cookies(anon) -> None:
    from tests._support import VALID_PASSWORD

    response = await anon.post("/api/auth/login", json={"password": VALID_PASSWORD})
    assert response.status_code == 200
    cookies = response.headers.get_list("set-cookie")
    assert any(cookie.startswith("sk_session=") for cookie in cookies)
    assert any(cookie.startswith("sk_csrf=") for cookie in cookies)
    # 会话 cookie 必须 HttpOnly；CSRF 必须能被 JS 读，所以不能 HttpOnly
    assert any("sk_session=" in c and "HttpOnly" in c for c in cookies)
    assert any("sk_csrf=" in c and "HttpOnly" not in c for c in cookies)


async def test_me_reports_unauthenticated_without_401(anon) -> None:
    """未登录返回 200 + authenticated=false，让前端首屏能拿到 CSRF。"""
    response = await anon.get("/api/auth/me")
    assert response.status_code == 200
    body = response.json()
    assert body["authenticated"] is False
    assert body["csrf_token"]


async def test_logout_is_idempotent(anon) -> None:
    assert (await anon.post("/api/auth/logout")).status_code == 200
    assert (await anon.post("/api/auth/logout")).status_code == 200


# --------------------------------------------------------------------------- #
# 任务
# --------------------------------------------------------------------------- #
async def test_create_ordered_and_unordered(api) -> None:
    ordered = await api.post(
        "/api/tasks", json={"title": "交作业", "due_at": "2026-10-10T23:59:00+08:00"}
    )
    assert ordered.status_code == 201, ordered.text
    assert ordered.json()["view"] == "ordered"

    unordered = await api.post("/api/tasks", json={"title": "背单词", "priority": 3})
    assert unordered.status_code == 201
    assert unordered.json()["view"] == "unordered"

    listing = await api.get("/api/tasks", params={"view": "ordered"})
    assert [item["title"] for item in listing.json()["items"]] == ["交作业"]

    listing = await api.get("/api/tasks", params={"view": "unordered"})
    assert [item["title"] for item in listing.json()["items"]] == ["背单词"]


async def test_both_due_and_priority_is_422(api) -> None:
    response = await api.post(
        "/api/tasks",
        json={"title": "x", "due_at": "2026-10-10T23:59:00+08:00", "priority": 2},
    )
    assert response.status_code == 422
    assert "只能填一个" in response.json()["detail"]


async def test_listing_carries_counts_and_server_time(api) -> None:
    await api.post("/api/tasks", json={"title": "a", "priority": 1})
    body = (await api.get("/api/tasks")).json()
    assert set(body["counts"]) >= {"ordered", "unordered", "done"}
    assert body["server_time"].endswith("+00:00")


async def test_toggle_done_moves_view(api) -> None:
    created = (await api.post("/api/tasks", json={"title": "x", "priority": 1})).json()
    toggled = await api.post(f"/api/tasks/{created['id']}/toggle")
    assert toggled.status_code == 200
    assert toggled.json()["status"] == "done"
    assert toggled.json()["completed_at"] is not None

    done = (await api.get("/api/tasks", params={"view": "done"})).json()
    assert [item["id"] for item in done["items"]] == [created["id"]]


async def test_patch_can_switch_view(api) -> None:
    created = (await api.post("/api/tasks", json={"title": "x", "priority": 2})).json()
    patched = await api.patch(
        f"/api/tasks/{created['id']}",
        json={"due_at": "2026-10-10T23:59:00+08:00", "due_at_set": True},
    )
    assert patched.status_code == 200
    body = patched.json()
    assert body["view"] == "ordered"
    assert body["priority"] is None


async def test_delete_task(api) -> None:
    created = (await api.post("/api/tasks", json={"title": "x", "priority": 1})).json()
    assert (await api.delete(f"/api/tasks/{created['id']}")).status_code == 200
    assert (await api.delete(f"/api/tasks/{created['id']}")).status_code == 404


async def test_missing_task_is_404_not_422(api) -> None:
    response = await api.patch("/api/tasks/99999", json={"title": "x"})
    assert response.status_code == 404


async def test_unknown_view_is_422(api) -> None:
    response = await api.get("/api/tasks", params={"view": "someday"})
    assert response.status_code == 422


async def test_client_uuid_is_idempotent(api) -> None:
    first = (
        await api.post("/api/tasks", json={"title": "作业", "priority": 1, "client_uuid": "u1"})
    ).json()
    second = (
        await api.post("/api/tasks", json={"title": "作业", "priority": 1, "client_uuid": "u1"})
    ).json()
    assert first["id"] == second["id"]


# --------------------------------------------------------------------------- #
# 录入 → 草稿 → 确认
# --------------------------------------------------------------------------- #
async def test_ingest_text_creates_draft_without_writing_tasks(api, bare_con) -> None:
    response = await api.post("/api/ingest", data={"text": "明天交数学作业"})
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["draft"]["status"] == "pending"
    assert body["draft"]["item_count"] >= 1
    assert body["confirm_url"].endswith(f"/#/drafts/{body['draft']['id']}")

    # 关键不变量：草稿阶段 items 表零写入
    assert bare_con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0


async def test_ingest_image_only(api) -> None:
    response = await api.post(
        "/api/ingest", files={"image": ("photo.png", make_png(), "image/png")}
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["draft"]["has_image"] is True
    assert body["draft"]["image_url"] == f"/api/drafts/{body['draft']['id']}/image"


async def test_ingest_image_and_text_mixed(api) -> None:
    response = await api.post(
        "/api/ingest",
        data={"text": "第三章习题，下周一交"},
        files={"image": ("photo.png", make_png(), "image/png")},
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["draft"]["has_image"] is True
    assert body["draft"]["text_input"] == "第三章习题，下周一交"


async def test_ingest_requires_some_input(api) -> None:
    response = await api.post("/api/ingest", data={"text": "   "})
    assert response.status_code == 422
    assert "至少" in response.json()["detail"]


async def test_ingest_rejects_non_image(api) -> None:
    """不信 content-type：一个文本文件标成 image/png 也必须被拒。"""
    response = await api.post(
        "/api/ingest", files={"image": ("fake.png", b"not an image at all", "image/png")}
    )
    assert response.status_code == 422
    assert "图片" in response.json()["detail"]


async def test_draft_image_is_authenticated(anon, api) -> None:
    created = (
        await api.post("/api/ingest", files={"image": ("p.png", make_png(), "image/png")})
    ).json()
    draft_id = created["draft"]["id"]

    assert (await anon.get(f"/api/drafts/{draft_id}/image")).status_code == 401

    served = await api.get(f"/api/drafts/{draft_id}/image")
    assert served.status_code == 200
    assert served.headers["content-type"].startswith("image/png")
    assert served.content.startswith(b"\x89PNG")


async def test_confirm_draft_creates_tasks(api, bare_con) -> None:
    draft = (
        await api.post("/api/ingest", data={"text": "下周交复习资料"})
    ).json()["draft"]

    confirmed = await api.post(f"/api/drafts/{draft['id']}/confirm", json={})
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert len(body["created"]) >= 1
    assert body["draft"]["status"] == "confirmed"

    # 现在 items 表里应该有东西了
    assert bare_con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] >= 1


async def test_confirm_with_edited_items(api) -> None:
    draft = (await api.post("/api/ingest", data={"text": "写实验报告"})).json()["draft"]
    confirmed = await api.post(
        f"/api/drafts/{draft['id']}/confirm",
        json={"items": [{"title": "我改过的标题", "category": "homework", "priority": 1}]},
    )
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["created"][0]["title"] == "我改过的标题"


async def test_confirm_rejects_bad_edited_item(api) -> None:
    """二次修改也要过业务校验：二选一不能只在前端拦。"""
    draft = (await api.post("/api/ingest", data={"text": "写实验报告"})).json()["draft"]
    response = await api.post(
        f"/api/drafts/{draft['id']}/confirm",
        json={
            "items": [
                {
                    "title": "两个都给",
                    "category": "homework",
                    "due_at": "2026-10-10T23:59:00+08:00",
                    "priority": 1,
                }
            ]
        },
    )
    assert response.status_code == 409
    assert "只能填一个" in response.json()["detail"]


async def test_confirm_twice_is_409(api) -> None:
    draft = (await api.post("/api/ingest", data={"text": "写实验报告"})).json()["draft"]
    assert (await api.post(f"/api/drafts/{draft['id']}/confirm", json={})).status_code == 200
    second = await api.post(f"/api/drafts/{draft['id']}/confirm", json={})
    assert second.status_code == 409, "同一条草稿不能确认两次"


async def test_discard_then_confirm_is_409(api) -> None:
    draft = (await api.post("/api/ingest", data={"text": "写实验报告"})).json()["draft"]
    assert (await api.post(f"/api/drafts/{draft['id']}/discard")).status_code == 200
    assert (await api.post(f"/api/drafts/{draft['id']}/confirm", json={})).status_code == 409


async def test_replace_items_stores_without_writing(api, bare_con) -> None:
    draft = (await api.post("/api/ingest", data={"text": "写实验报告"})).json()["draft"]
    stored = await api.put(
        f"/api/drafts/{draft['id']}/items",
        json={"items": [{"title": "暂存的标题", "category": "other", "priority": 4}]},
    )
    assert stored.status_code == 200
    assert stored.json()["items"][0]["title"] == "暂存的标题"
    assert bare_con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0


async def test_draft_list_shows_pending_only(api) -> None:
    first = (await api.post("/api/ingest", data={"text": "第一件事"})).json()["draft"]
    await api.post("/api/ingest", data={"text": "第二件事"})
    await api.post(f"/api/drafts/{first['id']}/discard")

    listing = (await api.get("/api/drafts")).json()
    ids = [item["id"] for item in listing["items"]]
    assert first["id"] not in ids
    assert listing["counts"]["discarded"] >= 1


async def test_raw_output_endpoint_exposes_llm_result(api) -> None:
    draft = (await api.post("/api/ingest", data={"text": "写实验报告"})).json()["draft"]
    raw = await api.get(f"/api/drafts/{draft['id']}/raw")
    assert raw.status_code == 200
    body = raw.json()
    assert body["llm_path"] in ("tool_call", "json_schema", "json_object")
    assert "items" in body["raw_output"]


async def test_ingest_context_endpoint(api) -> None:
    response = await api.get("/api/ingest/context")
    assert response.status_code == 200
    body = response.json()
    assert "【时间上下文】" in body["context"]
    assert body["chars"] > 0


# --------------------------------------------------------------------------- #
# 记忆
# --------------------------------------------------------------------------- #
async def test_memory_crud(api) -> None:
    created = await api.post(
        "/api/memories",
        json={"title": "电子电路基础", "content": "周三交作业", "scope": "global"},
    )
    assert created.status_code == 201, created.text
    memory_id = created.json()["id"]

    assert (await api.get("/api/memories")).json()["counts"]["total"] == 1

    patched = await api.patch(f"/api/memories/{memory_id}", json={"content": "周五交作业"})
    assert patched.json()["content"] == "周五交作业"

    assert (await api.delete(f"/api/memories/{memory_id}")).status_code == 200
    assert (await api.delete(f"/api/memories/{memory_id}")).status_code == 404


async def test_memory_course_scope_requires_course(api) -> None:
    response = await api.post(
        "/api/memories",
        json={"title": "x", "content": "y", "scope": "course"},
    )
    assert response.status_code == 422
    assert "必须指定课程" in response.json()["detail"]


async def test_global_memory_ignores_course_id(api) -> None:
    response = await api.post(
        "/api/memories",
        json={"title": "x", "content": "y", "scope": "global", "course_id": 123},
    )
    assert response.status_code == 201
    assert response.json()["course_id"] is None


async def test_pin_endpoint_marks_single_injection(api) -> None:
    memory_id = (
        await api.post("/api/memories", json={"title": "a", "content": "b"})
    ).json()["id"]
    pinned = await api.post(f"/api/memories/{memory_id}/pin")
    assert pinned.json()["pin_single"] is True

    unpinned = await api.delete(f"/api/memories/{memory_id}/pin")
    assert unpinned.json()["pin_single"] is False


async def test_pin_is_consumed_by_next_ingest(api) -> None:
    """单条注入是"下一次"的意图：用掉就该清掉，否则用户会以为开关失灵。"""
    memory_id = (
        await api.post("/api/memories", json={"title": "长期偏好", "content": "作业周三交"})
    ).json()["id"]
    await api.post(f"/api/memories/{memory_id}/pin")

    draft = (await api.post("/api/ingest", data={"text": "交作业"})).json()["draft"]
    assert memory_id in draft["memory_ids"], "被 pin 的记忆必须出现在本次注入里"

    after = (await api.get(f"/api/memories/{memory_id}")).json()
    assert after["pin_single"] is False, "用过一次后必须清除 pin"


async def test_clear_pins_endpoint(api) -> None:
    memory_id = (
        await api.post("/api/memories", json={"title": "a", "content": "b"})
    ).json()["id"]
    await api.post(f"/api/memories/{memory_id}/pin")
    response = await api.post("/api/memories/clear-pins")
    assert response.status_code == 200
    assert (await api.get(f"/api/memories/{memory_id}")).json()["pin_single"] is False


async def test_selected_endpoint_lists_injection(api) -> None:
    await api.post("/api/memories", json={"title": "a", "content": "b"})
    body = (await api.get("/api/memories/selected")).json()
    assert len(body["memory_ids"]) == 1


# --------------------------------------------------------------------------- #
# 课表
# --------------------------------------------------------------------------- #
TIMETABLE = {
    "courses": [
        {
            "name": "电子电路基础",
            "teacher": "张老师",
            "location": "三教 201",
            "sessions": [
                {"weekday": 3, "start_time": "08:00", "end_time": "09:40", "weeks": "1-16"}
            ],
        }
    ]
}


async def test_put_and_get_timetable(api) -> None:
    put = await api.put("/api/timetable", json=TIMETABLE)
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["courses"][0]["name"] == "电子电路基础"
    assert body["matrix"]["周三"][0]["start_time"] == "08:00"

    got = await api.get("/api/timetable")
    assert got.json()["courses"][0]["sessions"][0]["weeks"] == "1-16"


async def test_timetable_replaces_wholesale(api) -> None:
    await api.put("/api/timetable", json=TIMETABLE)
    await api.put("/api/timetable", json={"courses": []})
    assert (await api.get("/api/timetable")).json()["courses"] == []


async def test_timetable_rejects_reversed_time(api) -> None:
    bad = {
        "courses": [
            {
                "name": "x",
                "sessions": [{"weekday": 1, "start_time": "10:00", "end_time": "09:00"}],
            }
        ]
    }
    response = await api.put("/api/timetable", json=bad)
    assert response.status_code == 422
    assert "晚于" in response.json()["detail"]


async def test_timetable_rejects_duplicate_name(api) -> None:
    dup = {"courses": [{"name": "同一门"}, {"name": "同一门"}]}
    response = await api.put("/api/timetable", json=dup)
    assert response.status_code == 422
    assert "重复" in response.json()["detail"]


async def test_timetable_now_endpoint(api) -> None:
    await api.put("/api/timetable", json=TIMETABLE)
    body = (await api.get("/api/timetable/now")).json()
    assert "week" in body
    assert isinstance(body["around"], list)


async def test_memory_course_scope_injected_when_course_in_context(api, bare_con) -> None:
    """scope=course 的记忆只在对应课程出现在上下文里时才注入。

    这条约束的价值是减少干扰：识别英语作业时不该带上复变的约定。
    """
    put = await api.put("/api/timetable", json=TIMETABLE)
    course_id = put.json()["courses"][0]["id"]

    course_memory = (
        await api.post(
            "/api/memories",
            json={"title": "电动作业", "content": "周三交", "scope": "course", "course_id": course_id},
        )
    ).json()["id"]

    # 手动把课表调成"此刻正在上课"，让课程进入上下文
    from app.timeutil import now_utc, tz

    local = now_utc().astimezone(tz("Asia/Shanghai"))
    weekday = local.isoweekday()
    start = local.strftime("%H:%M")
    end_hour = (local.hour + 1) % 24
    end = f"{end_hour:02d}:{local.minute:02d}"
    await api.put(
        "/api/timetable",
        json={
            "courses": [
                {
                    "name": "电子电路基础",
                    "sessions": [
                        {"weekday": weekday, "start_time": "00:00", "end_time": "23:59", "weeks": ""}
                    ],
                }
            ]
        },
    )
    # 课程被整体替换后 id 会变，重新拿一次
    courses = (await api.get("/api/timetable")).json()["courses"]
    new_course_id = courses[0]["id"]
    await api.patch(f"/api/memories/{course_memory}", json={"course_id": new_course_id, "course_id_set": True})

    draft = (await api.post("/api/ingest", data={"text": "交作业"})).json()["draft"]
    assert course_memory in draft["memory_ids"], (
        f"课程正在上课时，其 scope=course 的记忆应当被注入（start={start} end={end}）"
    )


# --------------------------------------------------------------------------- #
# 控制台
# --------------------------------------------------------------------------- #
async def test_settings_never_leaks_api_key(api) -> None:
    from tests._support import FAKE_API_KEY

    raw = (await api.get("/api/settings")).text
    assert FAKE_API_KEY not in raw, "配置接口泄漏了 LLM API Key 明文"
    body = (await api.get("/api/settings")).json()
    assert body["llm"]["has_api_key"] is True, "应当只告诉你「有没有配」，而不是值"


async def test_settings_update_is_hot(api) -> None:
    response = await api.put("/api/settings", json={"model": "deepseek-v4-pro", "thinking": True})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["llm"]["model"] == "deepseek-v4-pro"
    assert body["llm"]["thinking"] is True
    # 再读一次确认真的落盘了
    assert (await api.get("/api/settings")).json()["llm"]["model"] == "deepseek-v4-pro"


async def test_settings_api_key_can_be_cleared_but_not_by_omission(api) -> None:
    await api.put("/api/settings", json={"temperature": 0.5})
    assert (await api.get("/api/settings")).json()["llm"]["has_api_key"] is True

    await api.put("/api/settings", json={"api_key": ""})
    assert (await api.get("/api/settings")).json()["llm"]["has_api_key"] is False


async def test_term_settings_validation(api) -> None:
    bad = await api.put("/api/settings/term", json={"start_date": "下周一"})
    assert bad.status_code == 422

    ok = await api.put("/api/settings/term", json={"start_date": "2026-09-14", "total_weeks": 18})
    assert ok.status_code == 200
    assert ok.json()["term"]["total_weeks"] == 18


async def test_api_key_lifecycle(api) -> None:
    created = await api.post("/api/keys", json={"name": "iphone", "read_only": False})
    assert created.status_code == 200
    body = created.json()
    assert body["api_key"].startswith("sk_")

    listing = (await api.get("/api/keys")).json()
    assert listing[0]["prefix"] == body["api_key"][:9]
    assert "api_key" not in listing[0], "列表不能包含明文"

    assert (await api.delete(f"/api/keys/{body['id']}")).status_code == 200
    assert (await api.delete(f"/api/keys/{body['id']}")).status_code == 404


async def test_api_key_can_write_without_csrf(configured) -> None:
    """API Key 不走 CSRF——Authorization 头不会被浏览器自动携带。

    这也是快捷指令能工作的前提。
    """
    import httpx

    from app.main import create_app
    from app.security import generate_api_key
    from app.timeutil import now_utc_iso

    plain, digest, prefix = generate_api_key()
    from app.db import connect

    con = connect(configured.db_path)
    con.execute(
        "INSERT INTO api_keys (name, key_hash, scope, prefix, created_at) VALUES (?, ?, ?, ?, ?)",
        ("shortcut", digest, "write", prefix, now_utc_iso()),
    )
    con.close()

    app = create_app(configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        headers = {"Authorization": f"Bearer {plain}"}
        created = await client.post(
            "/api/tasks", json={"title": "快捷指令建的", "priority": 1}, headers=headers
        )
        assert created.status_code == 201, created.text
        assert created.json()["source"] == "shortcut"


async def test_read_only_api_key_cannot_write(configured) -> None:
    import httpx

    from app.main import create_app
    from app.security import generate_api_key
    from app.timeutil import now_utc_iso
    from app.db import connect

    plain, digest, prefix = generate_api_key()
    con = connect(configured.db_path)
    con.execute(
        "INSERT INTO api_keys (name, key_hash, scope, prefix, created_at) VALUES (?, ?, ?, ?, ?)",
        ("widget", digest, "read", prefix, now_utc_iso()),
    )
    con.close()

    app = create_app(configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        headers = {"Authorization": f"Bearer {plain}"}
        assert (await client.get("/api/tasks", headers=headers)).status_code == 200
        denied = await client.post(
            "/api/tasks", json={"title": "x", "priority": 1}, headers=headers
        )
        assert denied.status_code == 403
        assert "只读" in denied.json()["detail"]


async def test_revoked_api_key_rejected(configured) -> None:
    import httpx

    from app.main import create_app
    from app.security import generate_api_key
    from app.timeutil import now_utc_iso
    from app.db import connect

    plain, digest, prefix = generate_api_key()
    con = connect(configured.db_path)
    con.execute(
        "INSERT INTO api_keys (name, key_hash, scope, prefix, created_at, revoked_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        ("old", digest, "write", prefix, now_utc_iso(), now_utc_iso()),
    )
    con.close()

    app = create_app(configured)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8000"
    ) as client:
        response = await client.get("/api/tasks", headers={"Authorization": f"Bearer {plain}"})
        assert response.status_code == 401


async def test_status_endpoint(api) -> None:
    body = (await api.get("/api/status")).json()
    assert body["version"]
    assert "tasks" in body["counts"]
    assert "db_bytes" in body["storage"]
    assert isinstance(body["warnings"], list)


async def test_status_warns_when_term_expired(api) -> None:
    """学期已过总周数时必须警告——否则提示词里的课表上下文会静默不准。"""
    await api.put("/api/settings/term", json={"start_date": "2020-01-06", "total_weeks": 16})
    body = (await api.get("/api/status")).json()
    assert any("超过配置的总周数" in warning for warning in body["warnings"])


# --------------------------------------------------------------------------- #
# trace id 贯穿
# --------------------------------------------------------------------------- #
async def test_trace_id_header_present_everywhere(api) -> None:
    for path in ("/healthz", "/api/tasks", "/api/settings", "/api/status"):
        response = await api.get(path)
        trace_id = response.headers.get("X-Trace-Id", "")
        assert len(trace_id) == 8, f"{path} 缺少可用的 X-Trace-Id"
