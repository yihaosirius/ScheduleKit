"""前端源码的静态约定检查。

这一层测试的存在理由：前端的**视觉与交互**只能靠浏览器验证，但有些问题是
纯静态的、可以在 pytest 里一次拦住。我实测踩到过两个：

1. **直接把 UTC 时间戳切一段显示**（`due_at.slice(0, 16)`）。
   服务端按约定只存 UTC（见 docs/decisions.md D-005），任何直接截断显示的做法
   都会让用户看到差 8 小时的时间——而在界面上它看起来完全正常。
   实测确实出现了 `2026-10-04 15:59`（本应是 23:59）。

2. **时区/格式化逻辑重复实现**。所有时间展示必须走 `utils/format.ts`，
   否则"哪些地方转、哪些地方没转"会变成需要逐个回忆的东西。

这两条都是"改起来容易、忘掉更容易"的，所以用测试钉住。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WEB = PROJECT_ROOT / "web"
SRC = WEB / "src"

VUE_FILES = sorted(SRC.rglob("*.vue")) if SRC.exists() else []
TS_FILES = sorted(SRC.rglob("*.ts")) if SRC.exists() else []

pytestmark = pytest.mark.skipif(
    not SRC.exists(), reason="web/ 源码不存在（前端里程碑未开始）"
)


# --------------------------------------------------------------------------- #
# 时间显示
# --------------------------------------------------------------------------- #
def test_no_raw_timestamp_slicing_in_views() -> None:
    """视图层不许直接切 ISO 时间戳。

    服务端只存 UTC，直接 `slice(0, 16)` 会显示 UTC 时间。
    必须走 `utils/format.ts` 里的 `formatDateTime` / `formatDateShort` /
    `formatRelative`——它们用 `Date` 做本地时区换算。
    """
    offenders: list[str] = []
    pattern = re.compile(r"(due_at|created_at|updated_at|completed_at|last_used_at|expires_at|confirmed_at)\s*\??\.\s*slice\(")
    for path in VUE_FILES + TS_FILES:
        if path.name == "format.ts":
            continue  # 格式化模块本身当然会操作时间
        text = path.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{path.relative_to(PROJECT_ROOT)}:{lineno}  {line.strip()}")

    assert not offenders, (
        "直接截断 ISO 时间戳会显示 UTC 时间（差 8 小时），必须用 utils/format.ts：\n  "
        + "\n  ".join(offenders)
    )


def test_format_module_exports_the_time_helpers() -> None:
    assert (SRC / "utils" / "format.ts").exists()
    text = (SRC / "utils" / "format.ts").read_text(encoding="utf-8")
    for helper in ("formatDateTime", "formatDateShort", "formatRelative", "formatBytes"):
        assert f"export function {helper}" in text, f"format.ts 缺少 {helper}"


def test_format_datetime_uses_local_conversion() -> None:
    """格式化函数必须走 Date（会做本地换算），而不是字符串处理。"""
    text = (SRC / "utils" / "format.ts").read_text(encoding="utf-8")
    # 找到 formatDateTime 的实现体
    match = re.search(
        r"export function formatDateTime\(.*?\n\}", text, flags=re.DOTALL
    )
    assert match, "找不到 formatDateTime 的实现"
    body = match.group(0)
    assert "new Date(" in body, "formatDateTime 必须用 new Date() 做本地换算"
    assert "slice(" not in body, "formatDateTime 不该用字符串切片"


# --------------------------------------------------------------------------- #
# 二选一约束在界面上的表达
# --------------------------------------------------------------------------- #
def test_confirm_page_clears_the_other_field() -> None:
    """确认页必须在填了截止时间时清掉优先级（反之亦然）。

    服务端会强制这件事并返回 422，但如果前端不联动，用户会先看到"两个都填了"
    再被打回——那体验比不让填差得多。
    """
    text = (SRC / "pages" / "DraftConfirmPage.vue").read_text(encoding="utf-8")
    assert "item.due_at = iso" in text and "item.priority = null" in text, (
        "确认页设置截止时间时必须清掉优先级"
    )
    assert "item.priority = value" in text and "item.due_at = null" in text, (
        "确认页设置优先级时必须清掉截止时间"
    )


def test_composer_uses_explicit_mode_switch() -> None:
    """新建表单用"模式切换"表达二选一，而不是两个输入框 + 报错。"""
    text = (SRC / "components" / "TaskComposer.vue").read_text(encoding="utf-8")
    assert "mode === 'ordered'" in text and "mode === 'unordered'" in text
    # 两个输入框不能同时渲染
    assert "v-if=\"mode === 'ordered'\"" in text
    assert "v-else" in text


def test_sticky_action_bar_clears_the_dock() -> None:
    """确认页的动作条不能贴在 `bottom: 0`。

    实测踩过：`bottom: 0` 会让动作条停在视口底部、被固定的底部 dock 遮住一半。
    原因是外层 `.shell__main` 的 padding-bottom 只保证"滚动到底时"内容不被挡，
    而 sticky 是相对滚动容器的 padding box —— 两者不是一回事。

    移动端必须让出 dock 的高度；PC 端 dock 变侧栏，可以贴底。
    """
    text = (SRC / "pages" / "DraftConfirmPage.vue").read_text(encoding="utf-8")
    match = re.search(r"\.actions\s*\{[^}]*\}", text, flags=re.DOTALL)
    assert match is not None, "找不到 .actions 的样式"
    body = match.group(0)
    assert "position: sticky" in body
    assert "var(--dock-h)" in body or "var(--safe-bottom)" in body, (
        "动作条必须让出 dock 的高度，否则会被底部导航遮住"
    )


def test_home_tiles_ordered_and_unordered_on_desktop() -> None:
    """PC（≥900px）上有序表与无序表要**平铺并排**，而不是标签切换。

    这条是用户明确要求的布局：PC 端不用标签，两列直接铺开。
    实现方式是"同一份 DOM + 断点切换 grid/flex"，所以这里断言的是
    *两份列结构都在 DOM 里*，并靠 CSS 在移动端隐藏其一。
    如果哪天有人改成 `v-if="desktop"` 渲染两套模板，这条测试会提醒他
    那种做法会让逻辑分叉（改一处忘另一处）。
    """
    text = (SRC / "pages" / "HomePage.vue").read_text(encoding="utf-8")

    # 两份列结构都在（不是按 desktop 条件渲染）
    assert 'data-col="ordered"' in text
    assert 'data-col="unordered"' in text

    # 移动端靠显隐切换，而不是 v-if 掉整个列
    assert "col--hidden-mobile" in text

    # PC 断点必须是 grid 两列。媒体查询后面还有别的内容，
    # 所以不能用 `\}\s*$` 去锚定文件末尾——只截到该块结束即可。
    media = re.search(r"@media \(min-width: 900px\)\s*\{(.*?)\n\}", text, flags=re.DOTALL)
    assert media is not None, "找不到 PC 断点"
    body = media.group(1)
    assert "grid-template-columns: 1fr 1fr" in body, "PC 上两列必须并排平铺"


def test_home_has_no_done_tab() -> None:
    """「已完成」不能是个标签页 —— 它改成了下方可展开区。

    如果标签列表里又冒出第三个 'done'，说明有人只改了标签没改折叠区，
    结果会出现"点了标签、下面还有一块可展开"的重复。
    """
    text = (SRC / "pages" / "HomePage.vue").read_text(encoding="utf-8")
    tabs = text[text.index("const TABS"):text.index("const TABS") + 400]
    assert "'ordered'" in tabs and "'unordered'" in tabs
    assert "'done'" not in tabs, "已完成不该出现在标签里"


def test_done_section_is_collapsible_and_collapsed_by_default() -> None:
    """已完成必须是可展开的，且默认收起。"""
    home = (SRC / "pages" / "HomePage.vue").read_text(encoding="utf-8")
    assert "CollapsibleSection" in home
    assert 'title="已完成"' in home
    assert "@expand=" in home, "展开时才去拉数据（默认收起时不该请求）"

    section = (SRC / "components" / "CollapsibleSection.vue").read_text(encoding="utf-8")
    assert "const open = ref(false)" in section, "默认必须是收起状态"
    # 高度动画用 grid-template-rows（0fr→1fr），不用猜 max-height
    assert "grid-template-rows: 0fr" in section
    assert "grid-template-rows: 1fr" in section

    # 只在**样式块**里检查 max-height —— 文件头的文档里会提到这个词
    # （解释为什么不用它），按整文件搜会把注释误判成实现。
    style = re.search(r"<style scoped>(.*?)</style>", section, flags=re.DOTALL)
    assert style is not None, "找不到样式块"
    css = style.group(1)
    # 去掉 CSS 注释后再查，否则注释里的说明也会命中
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)
    assert "max-height" not in css, (
        "用 max-height 做展开动画需要猜一个上限：猜小了下拉不到位，"
        "猜大了动画会提前结束，看起来像卡顿"
    )


def test_desktop_detection_uses_matchmedia() -> None:
    """断点判定要用 matchMedia，而不是自己监听 resize 再量宽度。

    matchMedia 能在跨过断点时被动收到回调（包括窗口拖动、
    以及某些浏览器里"缩放级别变化"触发的情况），不必自己重算与去抖。
    """
    text = (SRC / "pages" / "HomePage.vue").read_text(encoding="utf-8")
    assert "window.matchMedia('(min-width: 900px)')" in text
    assert "addEventListener('change'" in text


def test_loads_all_views_only_on_desktop() -> None:
    """只有平铺布局才一次拉三个视图；移动端一次拉一个。

    否则移动端会白白多两次请求——而它在标签版里只看得到其中一个，
    已完成那节默认还收起。
    """
    text = (SRC / "state" / "store.ts").read_text(encoding="utf-8")
    assert "Promise.all(" in text, "loadTasks('all') 要并行拉三个视图"
    home = (SRC / "pages" / "HomePage.vue").read_text(encoding="utf-8")
    assert "loadTasks(desktop.value ? 'all' : tab.value)" in home


def test_done_toggle_updates_counts_locally() -> None:
    """勾选完成后本地要立刻同步列表与计数。

    等重新拉取才更新会让用户看到"点了但没反应"的那一帧；
    而完全不更新计数会让角标长期不准。
    """
    text = (SRC / "state" / "store.ts").read_text(encoding="utf-8")
    assert "dropLocal" in text
    assert "insertLocal" in text
    assert "refreshCounts" in text


def test_done_card_shows_absolute_time_not_countdown() -> None:
    """已完成的任务只显示绝对时间，不显示"几小时后 / 逾期"这类倒计时。

    用户明确提过这一点：已经做完的事没有再显示时间压力的道理，
    红色逾期标记和倒计时只会干扰阅读。同时显示「21 小时前」+「10月2日 23:59」
    也是同一份信息的两次表达。

    另外已完成时显示的是**完成时间**而不是原截止时间——对一个做完的事，
    更相关的是"什么时候做完的"；当两者相差很大时（提前一周做完），
    只显示截止时间会让人误以为它早该交了。
    """
    text = (SRC / "components" / "TaskCard.vue").read_text(encoding="utf-8")

    # 倒计时只在"有序表且未完成"时出现
    assert "isOrdered && !isDone" in text, "倒计时要排除已完成的任务"
    assert "due_in_human" in text, "未完成的有序任务仍要显示倒计时"

    # 已完成走完成时间分支
    assert "completed_at" in text
    assert "完成于" in text, "已完成任务的时间要标成完成时间"

    # 已完成不再显示优先级档位
    assert "!isDone" in text

    # 已完成不该带上紧急度状态（逾期/今天/即将）。
    # 逾期后完成的任务在后端看仍然有过去的 due_at，不排除它就会一直带着
    # 红色"逾期"左边条——而那是给"还没做且已经晚了"用的。
    assert "isDone.value ? 'done' : dueState" in text, (
        "已完成的任务要跳出 dueState，否则逾期后完成的条目会一直显示逾期标记"
    )
    assert "border-left-color: transparent" in text, (
        "已完成要显式清掉左边条，不能只靠'状态不命中'"
    )

    # 不再需要冗余的"已完成"文字 chip（绿色对勾已经表达了）
    assert 'class="sk-chip sk-chip--ok">已完成' not in text, (
        "已完成条目不需要额外的文字标签，绿色对勾已经足够"
    )


def test_priority_labels_match_backend() -> None:
    """优先级文案必须与后端 system prompt 里的说明一致。"""
    client = (SRC / "api" / "client.ts").read_text(encoding="utf-8")
    assert "'Ⅰ'" in client and "'Ⅴ'" in client
    # 档位含义要与 app/config.py 的 DEFAULT_SYSTEM_PROMPT 对齐
    assert "24 小时内" in client
    assert "本周关键" in client


# --------------------------------------------------------------------------- #
# PWA 与构建配置
# --------------------------------------------------------------------------- #
def test_index_html_declares_both_capable_metas() -> None:
    """两个 capable meta 都要有。

    `apple-mobile-web-app-capable` 被 Chrome 标为 deprecated，标准名是
    `mobile-web-app-capable`；但 iOS 至今只认 apple- 那个。
    只留一个就会出现"iOS 上像普通书签"或"Chrome 刷警告"。
    """
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'name="mobile-web-app-capable"' in html
    assert 'name="apple-mobile-web-app-capable"' in html
    assert 'rel="manifest"' in html
    assert "viewport-fit=cover" in html, "没有 viewport-fit 时 safe-area 变量全为 0"


def test_vite_outputs_into_the_app_static_dir() -> None:
    """产物必须落进 app/static/spa/，否则服务端托管不到。"""
    config = (WEB / "vite.config.ts").read_text(encoding="utf-8")
    assert "'../app/static/spa'" in config or '"../app/static/spa"' in config


def test_password_field_has_hidden_username() -> None:
    """登录表单要有一个（视觉隐藏的）username 字段。

    Chromium 会因此报可访问性警告，密码管理器也更愿意保存。
    注意不能用 display:none —— 那会把元素从可访问性树里摘掉，等于没加。
    """
    text = (SRC / "pages" / "LoginPage.vue").read_text(encoding="utf-8")
    assert 'autocomplete="username"' in text
    assert "sr-only" in text, "隐藏 username 要用 .sr-only 而不是 display:none"

    css = (SRC / "styles" / "base.css").read_text(encoding="utf-8")
    assert ".sr-only" in css
    # 断言它不是用 display:none 实现的
    match = re.search(r"\.sr-only\s*\{[^}]*\}", css, flags=re.DOTALL)
    assert match is not None
    body = match.group(0)
    assert "display: none" not in body, ".sr-only 不能用 display:none（会从可访问性树消失）"
    assert "clip-path" in body or "clip:" in body


def test_service_worker_never_caches_api() -> None:
    """sw.js 绝不能缓存 /api/。

    缓存了会出现"勾选完刷新又变回来"这类极难排查的错觉——
    问题不在勾选逻辑，而在于读到了旧数据。
    """
    sw = (PROJECT_ROOT / "app" / "static" / "sw.js").read_text(encoding="utf-8")
    assert "startsWith('/api/')" in sw, "sw.js 必须显式排除 /api/"
    assert "network" in sw.lower()
    # 不允许把 /api/ 放进预缓存列表
    match = re.search(r"const SHELL = \[(.*?)\]", sw, flags=re.DOTALL)
    assert match is not None
    assert "/api/" not in match.group(1), "预缓存列表里不能有 /api/"
