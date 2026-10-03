"""守门测试：开发 trace 与决策记录必须存在且格式完整。

用户明确要求"在项目级 AGENTS.md 中补充一条：开发时维护一个开发的 trace 文档"。
只写进文档的约定会在几轮之后被遗忘——所以这里把它变成**会失败的测试**。

这个文件测的不是业务逻辑，而是**约定的可执行性**：
`AGENTS.md` 里的字段表、`docs/dev-trace.md` 里的每一条记录、`docs/decisions.md`
的每条决策，三处必须互相吻合。少一个字段就红。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
AGENTS_MD = PROJECT_ROOT / "AGENTS.md"
DOCS_AGENTS_MD = PROJECT_ROOT / "docs" / "AGENTS.md"
DEV_TRACE = PROJECT_ROOT / "docs" / "dev-trace.md"
DECISIONS = PROJECT_ROOT / "docs" / "decisions.md"

#: trace 记录必须包含的字段，顺序固定（AGENTS.md §1.1）
TRACE_FIELDS = (
    "目标：",
    "决策与理由：",
    "触及文件：",
    "执行过的命令与结果：",
    "观察到的现象 / 反直觉之处 / 踩坑：",
    "未决问题与下一步：",
)

#: 记录的标题行，例如 "## 2026-10-03 18:21 · 拆除服务器上的旧 ScheduleKit 实例并归档"
_ENTRY_HEADING = re.compile(r"^## (\d{4}-\d{2}-\d{2} \d{2}:\d{2}) · (.+)$")

#: 运行 trace id 形如 [t=8f3a2b1c]
_TRACE_ID = re.compile(r"\[t=[0-9a-f]{8}\]")


def _split_entries(text: str) -> list[tuple[str, str, str]]:
    """按 ``## <时间> · <标题>`` 切分，返回 ``[(时间, 标题, 正文)]``。"""
    entries: list[tuple[str, str, str]] = []
    current: tuple[str, str] | None = None
    buffer: list[str] = []
    for line in text.splitlines():
        match = _ENTRY_HEADING.match(line)
        if match:
            if current is not None:
                entries.append((current[0], current[1], "\n".join(buffer)))
            current = (match.group(1), match.group(2))
            buffer = []
        elif current is not None:
            buffer.append(line)
    if current is not None:
        entries.append((current[0], current[1], "\n".join(buffer)))
    return entries


# --------------------------------------------------------------------------- #
# 文件存在性
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "path", [AGENTS_MD, DOCS_AGENTS_MD, DEV_TRACE, DECISIONS], ids=lambda p: p.name
)
def test_required_documents_exist(path: Path) -> None:
    assert path.exists(), f"缺少约定文档：{path.relative_to(PROJECT_ROOT)}"
    assert path.read_text(encoding="utf-8").strip(), f"{path.name} 是空文件"


# --------------------------------------------------------------------------- #
# AGENTS.md 里的条款
# --------------------------------------------------------------------------- #
def test_agents_md_states_trace_requirement() -> None:
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert "trace" in text.lower()
    assert "docs/dev-trace.md" in text, "AGENTS.md 必须指明 trace 文档的固定路径"
    assert "decisions.md" in text, "AGENTS.md 必须要求同时沉淀决策记录"
    # 用户的原话大意必须能在这份文件里找到，避免条款被悄悄弱化
    assert "同一次改动" in text, "必须写明 trace 与代码改动在同一次改动中完成"


def test_agents_md_field_list_matches_test() -> None:
    """AGENTS.md 里列的字段，必须与这个测试断言的一致。

    这条防的是"改了 AGENTS.md 的字段表但没改测试"——那样约定会静默失守。
    """
    text = AGENTS_MD.read_text(encoding="utf-8")
    for field in TRACE_FIELDS:
        assert field in text, f"AGENTS.md 的字段表缺少 {field!r}"


# --------------------------------------------------------------------------- #
# dev-trace.md 的每条记录
# --------------------------------------------------------------------------- #
def test_dev_trace_has_at_least_one_entry() -> None:
    entries = _split_entries(DEV_TRACE.read_text(encoding="utf-8"))
    assert entries, "docs/dev-trace.md 里没有任何记录"


def test_every_trace_entry_has_all_fields_in_order() -> None:
    text = DEV_TRACE.read_text(encoding="utf-8")
    entries = _split_entries(text)
    for timestamp, title, body in entries:
        assert title.strip(), f"{timestamp} 这条记录没有标题"
        positions: list[int] = []
        for field in TRACE_FIELDS:
            index = body.find(field)
            assert index >= 0, f"记录「{timestamp} {title}」缺少字段：{field}"
            positions.append(index)
        assert positions == sorted(positions), (
            f"记录「{timestamp} {title}」的字段顺序与 AGENTS.md §1.1 不一致"
        )
        # 每个字段都得有内容，不能留空壳
        for field, position in zip(TRACE_FIELDS, positions):
            tail = body[position + len(field):]
            first_line = tail.split("\n", 1)[0].strip()
            assert first_line or tail.strip(), (
                f"记录「{timestamp} {title}」的字段 {field} 是空的"
            )


def test_trace_entries_are_chronological() -> None:
    entries = _split_entries(DEV_TRACE.read_text(encoding="utf-8"))
    stamps = [entry[0] for entry in entries]
    assert stamps == sorted(stamps), "trace 记录必须按时间递增追加"


def test_trace_entry_documents_a_rejected_alternative() -> None:
    """决策字段里必须至少有一个被否决的替代方案。

    只写"我选了什么"的记录，事后无法判断当时是否考虑过别的路，
    也就无法判断这个选择在条件变化后是否还成立。
    """
    for timestamp, title, body in _split_entries(DEV_TRACE.read_text(encoding="utf-8")):
        assert "否决" in body, (
            f"记录「{timestamp} {title}」没有写被否决的替代方案（AGENTS.md §1.1 要求）"
        )


def test_trace_records_failures_not_only_successes() -> None:
    """「执行过的命令与结果」必须出现至少一次非零退出或报错原文。

    成功路径在代码里，失败路径只在这里——这不只是风格要求，是溯源的最后一道门。
    """
    text = DEV_TRACE.read_text(encoding="utf-8")
    assert re.search(r"exit 1|退出码 1|FAIL|FAILED|SyntaxError|Error:", text), (
        "trace 里没有任何失败记录；成功的命令不需要记在这，失败的需要"
    )


# --------------------------------------------------------------------------- #
# 运行时 trace id 与开发 trace 的关联口径
# --------------------------------------------------------------------------- #
def test_trace_id_format_is_documented() -> None:
    """AGENTS.md 约定的 id 形态必须与 app/logging.py 的实现一致。

    两边一旦分叉，"根据日志里的 t= 去 grep trace 文档"这条动线就断了。
    """
    text = AGENTS_MD.read_text(encoding="utf-8")
    assert _TRACE_ID.search(text), "AGENTS.md 没有写明 trace id 的形态 [t=xxxxxxxx]"

    sample = _TRACE_ID.search(text)
    assert sample is not None
    from app.logging import new_trace_id

    assert _TRACE_ID.fullmatch(f"[t={new_trace_id()}]"), (
        "app.logging.new_trace_id() 生成的 id 与 AGENTS.md 里写的形态不一致"
    )


# --------------------------------------------------------------------------- #
# decisions.md
# --------------------------------------------------------------------------- #
def test_decisions_use_numbered_adr_format() -> None:
    text = DECISIONS.read_text(encoding="utf-8")
    numbers = [int(m) for m in re.findall(r"^## D-(\d{3}) ", text, flags=re.MULTILINE)]
    assert numbers, "docs/decisions.md 里没有 D-xxx 格式的决策"
    assert numbers == sorted(numbers), "决策编号必须递增"
    assert len(numbers) == len(set(numbers)), "决策编号不能重复"


def test_every_decision_records_rejected_alternatives() -> None:
    """每条决策都要有"被否决"一节，否则它只是结论而非决策。"""
    text = DECISIONS.read_text(encoding="utf-8")
    blocks = re.split(r"^## D-\d{3} ", text, flags=re.MULTILINE)[1:]
    assert blocks, "没有解析到任何决策块"
    for block in blocks:
        title = block.split("\n", 1)[0].strip()
        assert "被否决" in block, f"决策「{title}」没有写被否决的替代方案"
        # 写成 `**状态**：` / `状态：` 都算数；`**理由**（若干条约束）：` 这种
        # "关键词 + 括号补充 + 冒号"的写法也算——所以在冒号前允许任意补充说明。
        assert re.search(r"\*{0,2}状态\*{0,2}[^\n：]{0,40}：", block), (
            f"决策「{title}」缺少状态标记"
        )
        assert re.search(r"\*{0,2}理由\*{0,2}[^\n：]{0,40}：", block), f"决策「{title}」缺少理由"
