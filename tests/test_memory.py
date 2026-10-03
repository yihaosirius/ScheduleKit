"""记忆条目与清理任务的测试。

记忆这块最容易出错的地方不是 CRUD，而是**注入选择**：
注入了不该注入的（减少干扰失败），或者该注入的没注入（用户以为开关失灵）。
所以重点在 ``select_for_injection`` 与 pin 的一次性语义。
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import timedelta

import pytest

from app.config import load_config
from app.db import connect
from app.housekeeping import run_once
from app.migrations import runner
from app.paths import ensure_dirs
from app.services import drafts as drafts_service
from app.services import memory as memory_service
from app.services import timetable as timetable_service
from app.services.memory import MemoryError
from app.services.timetable import TimetableError
from app.timeutil import now_utc_iso


@pytest.fixture
def con(work_dir) -> sqlite3.Connection:
    connection = connect(work_dir / "memory.db")
    runner.run(connection)
    yield connection
    connection.close()


def add_course(con, name: str = "电子电路基础") -> int:
    courses = timetable_service.replace_timetable(con, [{"name": name}])
    return courses[0].id


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def test_create_and_read(con) -> None:
    memory = memory_service.create_memory(
        con, title="电子电路基础", content="周三交作业", tags="课业,惯例"
    )
    assert memory.id > 0
    assert memory.scope == "global"
    assert memory.enabled is True
    assert memory.pin_single is False
    assert memory.sort_order == 10, "首次创建应当排在 10"

    reloaded = memory_service.get_memory(con, memory.id)
    assert reloaded is not None
    assert reloaded.to_dict()["tags"] == ["课业", "惯例"]


def test_sort_order_auto_increments(con) -> None:
    first = memory_service.create_memory(con, title="A", content="a")
    second = memory_service.create_memory(con, title="B", content="b")
    assert second.sort_order > first.sort_order


def test_list_orders_by_sort_order(con) -> None:
    memory_service.create_memory(con, title="后", content="x", sort_order=20)
    memory_service.create_memory(con, title="先", content="x", sort_order=10)
    titles = [m.title for m in memory_service.list_memories(con)]
    assert titles == ["先", "后"]


def test_update_and_delete(con) -> None:
    memory = memory_service.create_memory(con, title="A", content="a")
    updated = memory_service.update_memory(con, memory.id, content="改过了", enabled=False)
    assert updated.content == "改过了"
    assert updated.enabled is False

    assert memory_service.delete_memory(con, memory.id) is True
    assert memory_service.delete_memory(con, memory.id) is False
    assert memory_service.get_memory(con, memory.id) is None


def test_update_missing_memory_rejected(con) -> None:
    with pytest.raises(MemoryError):
        memory_service.update_memory(con, 999, content="x")


def test_update_with_no_fields_is_noop(con) -> None:
    memory = memory_service.create_memory(con, title="A", content="a")
    assert memory_service.update_memory(con, memory.id).updated_at == memory.updated_at


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("title", ["", "   ", "\u200b"])
def test_empty_title_rejected(con, title: str) -> None:
    with pytest.raises(MemoryError):
        memory_service.create_memory(con, title=title, content="内容")


@pytest.mark.parametrize("content", ["", "   ", "\n\n"])
def test_empty_content_rejected(con, content: str) -> None:
    with pytest.raises(MemoryError):
        memory_service.create_memory(con, title="标题", content=content)


def test_content_multiline_preserved(con) -> None:
    """记忆允许分行：它表达的是"约定"，分行比一句话更好读。"""
    memory = memory_service.create_memory(
        con, title="标题", content="第一行\n\n第二行"
    )
    assert "\n" in memory.content
    assert "第一行" in memory.content and "第二行" in memory.content


def test_invalid_scope_rejected(con) -> None:
    with pytest.raises(MemoryError):
        memory_service.create_memory(con, title="A", content="a", scope="session")


def test_course_scope_requires_existing_course(con) -> None:
    with pytest.raises(MemoryError) as excinfo:
        memory_service.create_memory(
            con, title="A", content="a", scope="course", course_id=999
        )
    assert "课程不存在" in str(excinfo.value)


def test_course_scope_requires_course_id(con) -> None:
    with pytest.raises(MemoryError) as excinfo:
        memory_service.create_memory(con, title="A", content="a", scope="course")
    assert "必须指定课程" in str(excinfo.value)


def test_global_scope_clears_course_id(con) -> None:
    course_id = add_course(con)
    memory = memory_service.create_memory(
        con, title="A", content="a", scope="global", course_id=course_id
    )
    assert memory.course_id is None, "全局记忆不该挂课程，否则 UI 上自相矛盾"


def test_switching_scope_requires_new_course(con) -> None:
    memory = memory_service.create_memory(con, title="A", content="a")
    with pytest.raises(MemoryError):
        memory_service.update_memory(con, memory.id, scope="course")


def test_switching_to_global_drops_course(con) -> None:
    course_id = add_course(con)
    memory = memory_service.create_memory(
        con, title="A", content="a", scope="course", course_id=course_id
    )
    updated = memory_service.update_memory(con, memory.id, scope="global")
    assert updated.course_id is None


# --------------------------------------------------------------------------- #
# 注入选择
# --------------------------------------------------------------------------- #
def test_global_enabled_memories_are_selected(con) -> None:
    kept = memory_service.create_memory(con, title="保留", content="x")
    disabled = memory_service.create_memory(con, title="关闭", content="x", enabled=False)
    selected = memory_service.select_for_injection(con)
    ids = [m.id for m in selected]
    assert kept.id in ids
    assert disabled.id not in ids


def test_course_scope_only_when_course_in_context(con) -> None:
    """识别英语作业时不该带上复变的约定——这是记忆功能的全部意义。"""
    course_id = add_course(con)
    memory = memory_service.create_memory(
        con, title="复变作业", content="周三交", scope="course", course_id=course_id
    )

    without = memory_service.select_for_injection(con, course_ids_in_context=set())
    assert memory.id not in [m.id for m in without]

    with_course = memory_service.select_for_injection(con, course_ids_in_context={course_id})
    assert memory.id in [m.id for m in with_course]


def test_manual_ids_are_included_even_if_disabled(con) -> None:
    """用户显式点选优先于默认开关。"""
    memory = memory_service.create_memory(con, title="关掉的", content="x", enabled=False)
    selected = memory_service.select_for_injection(con, manual_ids=[memory.id])
    assert memory.id in [m.id for m in selected]


def test_manual_missing_id_is_ignored(con) -> None:
    selected = memory_service.select_for_injection(con, manual_ids=[999])
    assert selected == []


def test_pinned_memory_selected_and_ordered_first(con) -> None:
    memory_service.create_memory(con, title="普通", content="x", sort_order=1)
    pinned = memory_service.create_memory(con, title="钉住的", content="x", sort_order=99, pin_single=True)

    selected = memory_service.select_for_injection(con)
    assert selected[0].id == pinned.id, "pin 的条目应当排在最前（预算不够时先保住它）"


def test_pinned_disabled_memory_is_still_selected(con) -> None:
    memory = memory_service.create_memory(con, title="关掉但钉住", content="x", enabled=False)
    memory_service.update_memory(con, memory.id, pin_single=True)
    selected = memory_service.select_for_injection(con)
    assert memory.id in [m.id for m in selected]


def test_pinned_memory_deduplicated(con) -> None:
    memory = memory_service.create_memory(con, title="A", content="x", pin_single=True)
    selected = memory_service.select_for_injection(con, manual_ids=[memory.id])
    assert [m.id for m in selected].count(memory.id) == 1


# --------------------------------------------------------------------------- #
# pin 的一次性语义
# --------------------------------------------------------------------------- #
def test_clear_pin_clears_all(con) -> None:
    first = memory_service.create_memory(con, title="A", content="x", pin_single=True)
    second = memory_service.create_memory(con, title="B", content="x", pin_single=True)
    assert memory_service.clear_pin(con) == 2
    assert memory_service.get_memory(con, first.id).pin_single is False  # type: ignore[union-attr]
    assert memory_service.get_memory(con, second.id).pin_single is False  # type: ignore[union-attr]


def test_clear_pin_can_keep_one(con) -> None:
    keep = memory_service.create_memory(con, title="保留", content="x", pin_single=True)
    drop = memory_service.create_memory(con, title="清掉", content="x", pin_single=True)
    assert memory_service.clear_pin(con, except_id=keep.id) == 1
    assert memory_service.get_memory(con, keep.id).pin_single is True  # type: ignore[union-attr]
    assert memory_service.get_memory(con, drop.id).pin_single is False  # type: ignore[union-attr]


def test_clear_pin_when_nothing_pinned(con) -> None:
    memory_service.create_memory(con, title="A", content="x")
    assert memory_service.clear_pin(con) == 0


def test_counts(con) -> None:
    memory_service.create_memory(con, title="A", content="x")
    memory_service.create_memory(con, title="B", content="x", enabled=False)
    memory_service.create_memory(con, title="C", content="x", pin_single=True)
    counts = memory_service.counts(con)
    assert counts == {"total": 3, "enabled": 2, "pinned": 1}


def test_course_delete_keeps_memory_but_clears_link(con) -> None:
    """删课程是排课调整，不该连带把记忆忘掉。"""
    course_id = add_course(con)
    memory = memory_service.create_memory(
        con, title="A", content="周三交", scope="course", course_id=course_id
    )
    con.execute("DELETE FROM courses WHERE id = ?", (course_id,))
    after = memory_service.get_memory(con, memory.id)
    assert after is not None
    assert after.course_id is None
    assert after.content == "周三交"


# --------------------------------------------------------------------------- #
# housekeeping
# --------------------------------------------------------------------------- #
def test_housekeeping_purges_expired_drafts(configured, bare_con) -> None:
    draft = drafts_service.create_draft(
        bare_con,
        channel="web",
        text_input="x",
        items=[],
        memory_ids=[],
        llm_path="tool_call",
        llm_model="mock",
        llm_elapsed_ms=1.0,
        raw_output="",
        ttl_hours=48,
    )
    bare_con.execute(
        "UPDATE ingest_drafts SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (draft.id,),
    )
    stats = run_once(configured)
    assert stats["drafts_purged"] == 1


def test_housekeeping_removes_orphan_images(configured) -> None:
    ensure_dirs(configured)
    orphan = configured.uploads_dir / "zz" / "orphan.jpg"
    orphan.parent.mkdir(parents=True, exist_ok=True)
    orphan.write_bytes(b"x" * 100)
    # 把 mtime 推到 2 小时前，越过"刚上传"的缓冲窗口
    old = time.time() - 7200
    os.utime(orphan, (old, old))

    stats = run_once(configured)
    assert stats["images_removed"] == 1
    assert not orphan.exists()


def test_housekeeping_keeps_referenced_images(configured, bare_con) -> None:
    ensure_dirs(configured)
    keep = configured.uploads_dir / "ab" / "keep.jpg"
    keep.parent.mkdir(parents=True, exist_ok=True)
    keep.write_bytes(b"x" * 10)
    old = time.time() - 7200
    os.utime(keep, (old, old))

    drafts_service.create_draft(
        bare_con,
        channel="web",
        text_input="",
        items=[],
        memory_ids=[],
        llm_path="tool_call",
        llm_model="mock",
        llm_elapsed_ms=1.0,
        raw_output="",
        image_path="uploads/ab/keep.jpg",
        image_mime="image/jpeg",
        image_bytes=10,
    )
    stats = run_once(configured)
    assert stats["images_removed"] == 0
    assert keep.exists()


def test_housekeeping_keeps_fresh_orphan_images(configured) -> None:
    """刚落盘还没建草稿的图片必须留着，否则 store_image 与 create_draft
    之间的一个清理轮次就会把它删掉。"""
    ensure_dirs(configured)
    fresh = configured.uploads_dir / "cd" / "fresh.jpg"
    fresh.parent.mkdir(parents=True, exist_ok=True)
    fresh.write_bytes(b"x" * 10)

    stats = run_once(configured)
    assert stats["images_removed"] == 0
    assert fresh.exists()


def test_housekeeping_is_idempotent(configured) -> None:
    ensure_dirs(configured)
    assert run_once(configured)["drafts_purged"] == 0
    assert run_once(configured)["images_removed"] == 0
