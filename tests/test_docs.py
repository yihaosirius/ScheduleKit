"""文档与实现的一致性测试。

文档会腐烂，而且腐烂是静默的：没人会因为 `docs/api.md` 少写一个端点而收到报错，
但照文档接入快捷指令的人会撞上一个不存在的路径。

这里把"文档说了什么"与"代码实际提供什么"对上：
接口表里的路径必须都能在 FastAPI 的路由表里找到，反之新端点也必须出现在文档里。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS = PROJECT_ROOT / "docs"

API_MD = DOCS / "api.md"
DEPLOY_MD = DOCS / "deploy.md"
SHORTCUTS_MD = DOCS / "shortcuts.md"
WIDGET_MD = DOCS / "widget.md"
PUSH_MD = DOCS / "push-notes.md"


@pytest.fixture(scope="module")
def registered_paths() -> set[str]:
    """从真实应用里取已注册的路径（路径参数归一成 ``{id}``）。

    ⚠️ 不能只看 ``app.routes``。Starlette 1.7 起，``include_router`` 的结果是一个
    ``_IncludedRouter`` 包装对象，**子路由不再摊平到 ``app.routes`` 里**：
    直接遍历 ``app.routes`` 只能看到 ``/healthz``。

    这一点值得记下来，因为它会让人误判"路由丢了"——我第一版就是这么以为的，
    写了个探针去看，结果发现接口其实全都正常工作，只是枚举方式错了。
    真正权威的判据是"发请求能不能通"，而不是枚举出来的列表。
    """
    import tempfile

    from tests._support import write_config

    with tempfile.TemporaryDirectory() as tmp:
        cfg = write_config(Path(tmp))
        from app.config import load_config
        from app.main import create_app

        app = create_app(load_config(cfg))

        def join(prefix: str, path: str) -> str:
            """把挂载前缀与子路径拼成规范的绝对路径（无多余斜杠）。"""
            combined = (prefix.rstrip("/") + "/" + path.lstrip("/")).rstrip("/")
            return combined or "/"

        def collect(routes, prefix: str = "") -> set[str]:
            found: set[str] = set()
            for route in routes:
                # 被 include 的子路由：从 include_context 取挂载前缀
                context = getattr(route, "include_context", None)
                original = getattr(route, "original_router", None)
                if original is not None and hasattr(original, "routes"):
                    found |= collect(original.routes, prefix + (getattr(context, "prefix", "") or ""))
                    continue
                path = getattr(route, "path", None)
                if path is None:
                    continue
                found.add(join(prefix, path))
            return found

        raw = collect(app.routes)

    # /api/tasks/{task_id} -> /api/tasks/{id}
    return {re.sub(r"\{[^}]+\}", "{id}", path) for path in raw}


def test_route_enumeration_is_not_silently_empty(registered_paths: set[str]) -> None:
    """先确认枚举本身有效。

    如果哪天 Starlette 又换了内部结构，这个断言会先红——
    否则下面两条"文档与代码对齐"的测试会因为枚举不到任何东西而**假装通过**。
    """
    assert len(registered_paths) > 20, (
        f"只枚举到 {len(registered_paths)} 条路由，几乎肯定是枚举方式失效了"
        f"（Starlette 内部结构又变了？）而不是路由真的没了"
    )
    assert "/api/tasks" in registered_paths


# --------------------------------------------------------------------------- #
# 文件存在性
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path", [API_MD, DEPLOY_MD, SHORTCUTS_MD, WIDGET_MD, PUSH_MD],
    ids=lambda p: p.name,
)
def test_doc_exists_and_is_substantial(path: Path) -> None:
    assert path.exists(), f"缺少文档：{path.relative_to(PROJECT_ROOT)}"
    text = path.read_text(encoding="utf-8")
    assert len(text) > 500, f"{path.name} 太短（{len(text)} 字符），像是占位文件"
    # 每个文档都该有标题
    assert text.lstrip().startswith("#"), f"{path.name} 没有以一级标题开头"


# --------------------------------------------------------------------------- #
# api.md 与真实路由对齐
# --------------------------------------------------------------------------- #
def _documented_paths() -> set[str]:
    """从 api.md 的表格行里抽出路径。

    只认 ``| GET | ``/api/tasks`` |`` 这种表格形态，避免把正文里提到的
    路径片段（例如 ``/healthz`` 出现在说明里）也算进来。

    文档里的路径常常带查询串（``/api/drafts?status_filter=pending``），
    这里把 ``?`` 之后的部分去掉再比——**是否写了查询参数不是这份测试要管的事**，
    它管的是"端点有没有"。把查询串当成差异会让测试天天误报，
    而误报是文档测试最常见、也最致命的失效方式：红了就没人看了。
    """
    text = API_MD.read_text(encoding="utf-8")
    found: set[str] = set()
    for line in text.splitlines():
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        if cells[0] not in ("GET", "POST", "PUT", "PATCH", "DELETE"):
            continue
        match = re.search(r"`(/[^`]*)`", cells[1])
        if not match:
            continue
        raw = match.group(1)
        path = raw.split("?", 1)[0].rstrip("/") or "/"
        found.add(re.sub(r"\{[^}]+\}", "{id}", path))
    return found


def test_documented_paths_all_exist(registered_paths: set[str]) -> None:
    documented = _documented_paths()
    assert documented, "api.md 里没解析到任何端点（表格格式变了？）"

    missing = sorted(p for p in documented if p not in registered_paths)
    assert not missing, (
        "api.md 里写了但代码里没有的端点：\n  " + "\n  ".join(missing)
        + "\n（文档腐烂了，或者路由被删了）"
    )


def test_every_api_route_is_documented(registered_paths: set[str]) -> None:
    """新加的端点必须写进文档。

    排除页面外壳与 SPA 捕获路由：它们不是"接口"，
    写在接口文档里反而会让人以为可以拿来调。
    """
    documented = _documented_paths()
    excluded = {
        "/",
        "/{id}",                 # SPA 回退
        "/manifest.webmanifest",
        "/sw.js",
        "/healthz",
    }
    undocumented = sorted(
        p for p in registered_paths
        if p.startswith("/api") and p not in documented
    )
    assert not undocumented, (
        "代码里有但 api.md 没写的接口：\n  " + "\n  ".join(undocumented)
        + "\n（新接口要同步更新文档）"
    )
    assert documented - excluded


# --------------------------------------------------------------------------- #
# 文档内容的关键约定（防止被简化掉）
# --------------------------------------------------------------------------- #
def test_api_doc_states_the_core_invariants() -> None:
    text = API_MD.read_text(encoding="utf-8")
    # 二选一约束
    assert "恰好一个非空" in text, "api.md 必须写明 due_at 与 priority 的二选一约束"
    # 草稿零写入
    assert "零写入" in text, "api.md 必须写明「草稿阶段不写任务表」这条不变量"
    # API Key 不需要 CSRF
    assert "不需要 CSRF" in text or "不走 CSRF" in text, (
        "api.md 必须说明 API Key 不需要 CSRF——否则接快捷指令的人会去传它"
    )
    # 幂等键
    assert "client_uuid" in text, "api.md 必须说明 client_uuid 的幂等作用"
    # 错误码表
    assert "409" in text and "状态冲突" in text, "api.md 必须解释 409 与 422 的区别"


def test_deploy_doc_states_the_port_constraint() -> None:
    text = DEPLOY_MD.read_text(encoding="utf-8")
    assert "8443" in text
    assert "备案" in text, "deploy.md 必须写明端口与备案的关系（这是硬约束）"
    assert "restart" in text and "reload" in text, (
        "deploy.md 必须写出 admin off 与 reload 互斥这件事——否则改配置后必然踩"
    )
    # 不要碰别人的服务
    for name in ("cloudreve", "portainer"):
        assert name in text, f"deploy.md 必须写明这台机器上还有 {name}，不要动"


def test_deploy_doc_mentions_archive_of_legacy_instance() -> None:
    text = DEPLOY_MD.read_text(encoding="utf-8")
    assert "legacy-schedulekit" in text, "deploy.md 必须记录旧实例归档的位置"


def test_shortcuts_doc_covers_the_known_pitfalls() -> None:
    text = SHORTCUTS_MD.read_text(encoding="utf-8")
    # HEIC 是最容易踩的：iPhone 默认拍 HEIC，而服务端只收位图
    assert "HEIC" in text, "shortcuts.md 必须提醒 HEIC 需要转 JPEG"
    # 必须写清楚用哪个头带 Key
    assert "Authorization" in text
    # 确认链接
    assert "confirm_url" in text


def test_widget_doc_covers_sizes_and_secret_handling() -> None:
    text = WIDGET_MD.read_text(encoding="utf-8")
    for size in ("小", "中", "大"):
        assert size in text, f"widget.md 必须说明{size}尺寸显示什么"
    assert "Keychain" in text or "钥匙串" in text, (
        "widget.md 必须说明 API Key 存哪里，而不是硬编码在脚本里"
    )
    assert "只读" in text, "widget.md 应当建议用只读 Key（小组件只需要读）"
