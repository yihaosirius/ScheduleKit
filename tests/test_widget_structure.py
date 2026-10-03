"""Scriptable 小组件的**结构**测试（离线，不访问外网）。

为什么需要这一层：iOS 只在小组件被放到主屏/锁屏之后才渲染它，所以布局写错
在代码里完全读不出来。`clients/scriptable/schedulekit-widget.js` 里恰好出过一个
把布局彻底搞坏的 bug：函数创建了「一行」的 stack，却把子元素加到了父容器上，
于是所有"行"被摊平成垂直流——看起来就是标题、圆点、时间各占一行地错位。

做法：用 Node + 假的 Scriptable API 真的**执行**那个脚本，把元素树 dump 成
JSON，然后断言结构。数据来自一个本地 HTTP 服务（`http.server`），
所以不碰外网、不依赖网络。

跑不了 Node 时整体跳过，而不是失败：后端开发不该被"没装 Node"卡住。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WIDGET = PROJECT_ROOT / "clients" / "scriptable" / "schedulekit-widget.js"
HARNESS = Path(__file__).resolve().parent / "_widget_harness.cjs"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    not WIDGET.exists() or NODE is None,
    reason="需要 node 与 clients/scriptable/schedulekit-widget.js",
)

# 固定数据：包含逾期、今天、未来、以及优先级任务，覆盖各种显示分支
_ORDERED = {
    "items": [
        {"id": 1, "title": "交第三章习题", "category": "homework",
         "due_at": None, "status": "pending", "priority": None, "notes": "",
         "memory_ids": [], "due_in_human": "2 天后"},
        {"id": 2, "title": "逾期的旧作业", "category": "homework",
         "due_at": None, "status": "pending", "priority": None, "notes": "",
         "memory_ids": [], "due_in_human": "逾期 2 天"},
        {"id": 3, "title": "电子电路实验报告", "category": "homework",
         "due_at": None, "status": "pending", "priority": None, "notes": "",
         "memory_ids": [], "due_in_human": "5 天后"},
    ],
    "counts": {"ordered": 3, "unordered": 2, "done": 1, "all": 6},
    "server_time": "2026-10-03T14:00:00+00:00",
}
_UNORDERED = {
    "items": [
        {"id": 4, "title": "背单词", "category": "practice", "due_at": None,
         "status": "pending", "priority": 2, "notes": "", "memory_ids": [],
         "due_in_human": None},
        {"id": 5, "title": "预约研讨间", "category": "appointment", "due_at": None,
         "status": "pending", "priority": 3, "notes": "", "memory_ids": [],
         "due_in_human": None},
    ],
    "counts": {"ordered": 3, "unordered": 2, "done": 1, "all": 6},
    "server_time": "2026-10-03T14:00:00+00:00",
}
_STATUS = {
    "week": 3, "week_total": 16, "term_start": "2026-09-14", "timezone": "Asia/Shanghai",
    "warnings": [], "around": [], "today": [],
}


def _with_due(tasks: list[dict], base_days: float) -> list[dict]:
    """给任务填上相对当前时间的 due_at，让倒计时分支真的被走到。"""
    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    offsets = [base_days, -base_days, base_days * 2]
    for task, days in zip(tasks, offsets):
        task["due_at"] = (now + timedelta(days=days)).isoformat()
    return tasks


_with_due(_ORDERED["items"], 2)


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802
        if self.headers.get("Authorization", "") != "Bearer TEST-KEY":
            self.send_response(401)
            self.end_headers()
            self.wfile.write(b'{"detail":"unauthorized"}')
            return
        if self.path.startswith("/api/tasks?view=ordered"):
            payload = _ORDERED
        elif self.path.startswith("/api/tasks?view=unordered"):
            payload = _UNORDERED
        elif self.path.startswith("/api/status"):
            payload = _STATUS
        else:
            self.send_response(404)
            self.end_headers()
            return
        body = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args) -> None:  # 静默
        pass


@pytest.fixture(scope="module")
def api_server() -> str:
    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    yield f"http://{host}:{port}"
    server.shutdown()


def run_widget(family: str, base_url: str) -> dict:
    # 两个 Windows 专属的坑，都会表现成"stdout 是 None"：
    #
    # 1. 直接传 "node" 时 subprocess 不会补 .exe，会静默失败 →
    #    用 shutil.which 解析出的完整路径。
    # 2. **不能只写 text=True**：那会用系统默认编码（中文 Windows 上是 GBK）
    #    去解码，而 harness 输出的是含中文的 UTF-8 JSON →
    #    读取线程抛 UnicodeDecodeError，stdout 变成 None。
    #    必须显式指定 encoding="utf-8"。
    proc = subprocess.run(  # noqa: S603
        [NODE, str(HARNESS), family, base_url, "TEST-KEY"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=60,
        check=False,
    )
    assert proc.stdout and proc.stdout.strip(), (
        f"harness 没有输出；returncode={proc.returncode} stderr={proc.stderr[:500]}"
    )
    return json.loads(proc.stdout)


def walk(node: dict) -> list[dict]:
    out = [node]
    for child in node.get("children", []):
        out.extend(walk(child))
    return out


def texts_of(tree: dict) -> list[str]:
    return [n["text"] for n in walk(tree) if n["kind"] == "text"]


def rows_of(tree: dict) -> list[dict]:
    """横向 stack = 视觉上的"一行"。"""
    return [n for n in walk(tree) if n["kind"] == "stack" and n["layout"] == "horizontal"]


TASK_TITLES = {"交第三章习题", "逾期的旧作业", "电子电路实验报告", "背单词", "预约研讨间"}


# --------------------------------------------------------------------------- #
# 主屏
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("family", ["small", "medium", "large"])
def test_home_widget_runs(family: str, api_server: str) -> None:
    result = run_widget(family, api_server)
    assert result["ok"], f"{family} 执行失败：{result.get('error')}"


@pytest.mark.parametrize("family", ["small", "medium", "large"])
def test_task_title_and_countdown_share_one_row(family: str, api_server: str) -> None:
    """任务标题必须挂在**横向 stack** 下，倒计时也要与它同一行。

    这条正是为了锁住那个真实 bug：子元素被加到父容器而不是行 stack 上时，
    标题会掉出任何横向行，断言立刻失败。
    """
    tree = run_widget(family, api_server)["tree"]
    row_texts = {t for row in rows_of(tree) for t in texts_of(row)}

    titles = TASK_TITLES & set(texts_of(tree))
    assert titles, "没有渲染出任何任务标题"
    orphans = titles - row_texts
    assert not orphans, f"{family} 这些标题不在横向行里（会错位）：{orphans}"

    due_texts = {t for t in texts_of(tree) if t and (t.endswith(("分", "时", "天")))}
    due_orphans = due_texts - row_texts
    assert not due_orphans, f"{family} 倒计时不在行里：{due_orphans}"


@pytest.mark.parametrize("family", ["small", "medium", "large"])
def test_home_widget_uses_dynamic_background(family: str, api_server: str) -> None:
    """主屏要用 Color.dynamic 跟随深浅色，而不是硬编码一种底色。"""
    tree = run_widget(family, api_server)["tree"]
    assert tree["dynamicBg"] is True


@pytest.mark.parametrize("family", ["small", "medium", "large"])
def test_home_widget_does_not_use_lock_screen_white(family: str, api_server: str) -> None:
    """主屏不该用锁屏那套纯白配色。"""
    tree = run_widget(family, api_server)["tree"]
    colors = {n["color"] for n in walk(tree) if n["color"]}
    assert "white" not in colors, f"{family} 用了锁屏的纯白：{colors}"


def test_small_limits_to_three_tasks(api_server: str) -> None:
    tree = run_widget("small", api_server)["tree"]
    titles = TASK_TITLES & set(texts_of(tree))
    assert len(titles) <= 3, f"small 放了 {len(titles)} 条：{titles}"


def test_large_has_both_sections(api_server: str) -> None:
    text = texts_of(run_widget("large", api_server)["tree"])
    assert "待截止" in text
    assert "待排序" in text


def test_medium_has_two_columns(api_server: str) -> None:
    tree = run_widget("medium", api_server)["tree"]
    labelled = [
        row for row in rows_of(tree)
        if {"待截止", "待排序"} & set(texts_of(row))
    ]
    assert len(labelled) >= 2, f"只找到 {len(labelled)} 栏"


# --------------------------------------------------------------------------- #
# 锁屏
# --------------------------------------------------------------------------- #
def test_accessory_inline_returns_widget_not_element(api_server: str) -> None:
    """`accessoryInline` 必须返回**组件**，不是文本元素。

    `w.addText(...)` 返回的是文本元素；直接把它当 widget 交给
    Script.setWidget() 在真机上是错的（一个元素不是组件）。
    这个缺陷就是靠这个 harness 发现的。
    """
    tree = run_widget("accessoryInline", api_server)["tree"]
    assert tree["kind"] == "list", f"返回了 {tree['kind']}，应当是 widget"


def test_accessory_inline_is_one_short_line(api_server: str) -> None:
    tree = run_widget("accessoryInline", api_server)["tree"]
    lines = texts_of(tree)
    assert len(lines) == 1, f"inline 只能有一行，实际 {len(lines)}：{lines}"
    assert "\n" not in lines[0]
    assert len(lines[0]) <= 40, f"inline 文本过长（锁屏会被截断）：{lines[0]!r}"


@pytest.mark.parametrize("family", ["accessoryCircular", "accessoryRectangular"])
def test_accessory_uses_white_and_no_dynamic_background(family: str, api_server: str) -> None:
    """锁屏是单色渲染：字色用白，且不该用跟随深浅色的动态背景。"""
    tree = run_widget(family, api_server)["tree"]
    colors = {n["color"] for n in walk(tree) if n["color"]}
    assert "white" in colors, f"{family} 没有用白色文字：{colors}"
    assert tree["dynamicBg"] is not True, "锁屏没有深浅色概念，不该用 Color.dynamic"


@pytest.mark.parametrize("family", ["accessoryCircular", "accessoryRectangular"])
def test_accessory_fits_in_a_few_lines(family: str, api_server: str) -> None:
    """锁屏空间极小，行数必须克制（超了会被系统裁掉）。

    判据是**行数**而不是文本节点数——一行里可以放多个文本元素
    （标记 + 标题 + 倒计时），它们不额外占高度。
    """
    tree = run_widget(family, api_server)["tree"]
    line_count = len(rows_of(tree))
    limit = 2
    assert line_count <= limit, f"{family} 有 {line_count} 行，超过 {limit}"


def test_accessory_rectangular_shows_a_specific_time(api_server: str) -> None:
    """矩形要给出具体时刻（"10月4日 23:59"），而不只是"2 天后"。

    锁屏上用户要的是一眼可核对的时间；单纯倒计时无法核对。
    """
    tree = run_widget("accessoryRectangular", api_server)["tree"]
    text = texts_of(tree)
    assert any(":" in t for t in text), f"没有具体时刻：{text}"


def test_accessory_rectangular_mentions_remaining(api_server: str) -> None:
    tree = run_widget("accessoryRectangular", api_server)["tree"]
    text = texts_of(tree)
    assert any(t.endswith("条") for t in text), f"没有剩余条数提示：{text}"


def test_accessory_circular_is_minimal(api_server: str) -> None:
    """圆形里只放一条任务：多了根本放不下。"""
    tree = run_widget("accessoryCircular", api_server)["tree"]
    titles = TASK_TITLES & set(texts_of(tree))
    assert len(titles) <= 1, f"圆形里放了 {len(titles)} 条：{titles}"


# --------------------------------------------------------------------------- #
# 源码层的约束
# --------------------------------------------------------------------------- #
def test_widget_covers_every_family() -> None:
    """六个尺寸族都要有分支，且锁屏族不能被主屏分支兜住。"""
    source = WIDGET.read_text(encoding="utf-8")
    for family in ["small", "medium", "large", "accessoryCircular",
                   "accessoryRectangular", "accessoryInline"]:
        assert family in source, f"没有处理 {family}"
    # 必须有"是否为锁屏"的判断，否则锁屏会落进主屏分支
    assert "accessory" in source
    assert "runsInAccessoryWidget" in source, (
        "锁屏要用 config.runsInAccessoryWidget 判断，只用 runsInWidget 会在锁屏上不渲染"
    )


def test_widget_previews_the_right_family() -> None:
    """在 Scriptable 里直接运行时的预览要按实际尺寸族选，否则调锁屏样式看不到效果。"""
    source = WIDGET.read_text(encoding="utf-8")
    assert "presentAccessoryRectangular" in source
    assert "presentSmall" in source and "presentLarge" in source


def test_widget_reads_credentials_from_parameter_not_hardcoded() -> None:
    """凭据必须来自 Parameter/Keychain，不能写死在脚本里。"""
    source = WIDGET.read_text(encoding="utf-8")
    assert "args.widgetParameter" in source
    assert "Keychain" in source
    # 不能出现形如 sk_xxx 的字面量
    import re

    assert not re.search(r"sk_[A-Za-z0-9_-]{10,}", source), "脚本里出现了硬编码的 API Key"
