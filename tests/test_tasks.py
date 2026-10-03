"""任务业务规则测试。

这是整个项目里最值得测的一块：**双列表的正确性就是产品本身**。
所以这里覆盖的是排序语义、二选一约束的每一条边界，以及"客户端重放"这类
真实会发生但很容易忽略的场景。
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app.migrations import runner
from app.services import tasks

UTC = timezone.utc


@pytest.fixture
def con(work_dir) -> sqlite3.Connection:
    from app.db import connect

    connection = connect(work_dir / "tasks.db")
    runner.run(connection)
    yield connection
    connection.close()


def _iso(days_from_now: float) -> str:
    return (datetime.now(UTC) + timedelta(days=days_from_now)).replace(microsecond=0).isoformat()


# --------------------------------------------------------------------------- #
# 二选一
# --------------------------------------------------------------------------- #
def test_create_with_due_at_goes_to_ordered(con) -> None:
    task = tasks.create_task(con, title="交作业", due_at=_iso(3))
    assert task.view == "ordered"
    assert task.priority is None


def test_create_with_priority_goes_to_unordered(con) -> None:
    task = tasks.create_task(con, title="背单词", priority=3)
    assert task.view == "unordered"
    assert task.due_at is None


def test_both_given_is_rejected(con) -> None:
    with pytest.raises(tasks.TaskError) as excinfo:
        tasks.create_task(con, title="x", due_at=_iso(1), priority=2)
    assert "只能填一个" in str(excinfo.value)


def test_neither_given_is_rejected(con) -> None:
    with pytest.raises(tasks.TaskError) as excinfo:
        tasks.create_task(con, title="x")
    assert "必须填" in str(excinfo.value)


@pytest.mark.parametrize("bad", ["", "   "])
def test_blank_due_at_counts_as_missing(con, bad: str) -> None:
    """空串算"没给截止时间"，此时必须给优先级，否则报错。"""
    with pytest.raises(tasks.TaskError):
        tasks.create_task(con, title="x", due_at=bad)
    task = tasks.create_task(con, title="x", due_at=bad, priority=1)
    assert task.view == "unordered"


@pytest.mark.parametrize("priority", [0, 6, -1, 100])
def test_priority_range_enforced(con, priority: int) -> None:
    with pytest.raises(tasks.TaskError) as excinfo:
        tasks.create_task(con, title="x", priority=priority)
    assert "1–5" in str(excinfo.value) or "1-5" in str(excinfo.value)


def test_due_at_is_normalized_to_utc(con) -> None:
    """库内一律 UTC。传 +08:00 的时间必须被换算，否则差 8 小时。"""
    task = tasks.create_task(con, title="x", due_at="2026-10-10T23:59:00+08:00")
    assert task.due_at == "2026-10-10T15:59:00+00:00"


def test_unparseable_due_at_names_the_format(con) -> None:
    with pytest.raises(tasks.TaskError) as excinfo:
        tasks.create_task(con, title="x", due_at="下周三")
    assert "ISO8601" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 排序
# --------------------------------------------------------------------------- #
def test_ordered_view_sorts_by_due_at_ascending(con) -> None:
    tasks.create_task(con, title="三天后", due_at=_iso(3))
    tasks.create_task(con, title="明天", due_at=_iso(1))
    tasks.create_task(con, title="五天后", due_at=_iso(5))

    titles = [t.title for t in tasks.list_tasks(con, view="ordered")]
    assert titles == ["明天", "三天后", "五天后"]


def test_unordered_view_sorts_by_priority_1_to_5(con) -> None:
    tasks.create_task(con, title="Ⅲ", priority=3)
    tasks.create_task(con, title="Ⅰ", priority=1)
    tasks.create_task(con, title="Ⅴ", priority=5)

    titles = [t.title for t in tasks.list_tasks(con, view="unordered")]
    assert titles == ["Ⅰ", "Ⅲ", "Ⅴ"]


def test_same_priority_keeps_creation_order(con) -> None:
    first = tasks.create_task(con, title="先建", priority=2)
    second = tasks.create_task(con, title="后建", priority=2)
    assert first.id < second.id

    titles = [t.title for t in tasks.list_tasks(con, view="unordered")]
    assert titles == ["先建", "后建"]


def test_views_are_mutually_exclusive(con) -> None:
    tasks.create_task(con, title="有序", due_at=_iso(2))
    tasks.create_task(con, title="无序", priority=2)

    ordered = {t.title for t in tasks.list_tasks(con, view="ordered")}
    unordered = {t.title for t in tasks.list_tasks(con, view="unordered")}
    assert ordered == {"有序"}
    assert unordered == {"无序"}
    assert not ordered & unordered


def test_done_view_sorts_by_completed_at_descending(con) -> None:
    a = tasks.create_task(con, title="先完成", priority=1)
    b = tasks.create_task(con, title="后完成", priority=2)
    tasks.toggle_done(con, a.id)
    tasks.toggle_done(con, b.id)

    done = tasks.list_tasks(con, view="done")
    assert {t.title for t in done} == {"先完成", "后完成"}
    assert done[0].completed_at >= done[1].completed_at


def test_done_tasks_leave_open_views(con) -> None:
    task = tasks.create_task(con, title="x", priority=1)
    tasks.toggle_done(con, task.id)
    assert tasks.list_tasks(con, view="unordered") == []
    assert [t.title for t in tasks.list_tasks(con, view="done")] == ["x"]


def test_toggle_done_twice_returns_to_open(con) -> None:
    task = tasks.create_task(con, title="x", priority=1)
    tasks.toggle_done(con, task.id)
    back = tasks.toggle_done(con, task.id)
    assert back.status == "open"
    assert back.completed_at is None, "回到未完成时必须清掉完成时间，否则已完成列表排序会乱"


def test_unknown_view_rejected(con) -> None:
    with pytest.raises(tasks.TaskError):
        tasks.list_tasks(con, view="someday")


# --------------------------------------------------------------------------- #
# 更新
# --------------------------------------------------------------------------- #
def test_update_can_move_task_between_views(con) -> None:
    task = tasks.create_task(con, title="x", priority=2)
    moved = tasks.update_task(con, task.id, due_at=_iso(1), due_at_provided=True)
    assert moved.view == "ordered"
    assert moved.priority is None, "填了截止时间后优先级必须自动让位，否则撞约束"


def test_update_priority_clears_due_at(con) -> None:
    task = tasks.create_task(con, title="x", due_at=_iso(1))
    moved = tasks.update_task(con, task.id, priority=4, priority_provided=True)
    assert moved.view == "unordered"
    assert moved.due_at is None


def test_update_without_provided_flags_keeps_shape(con) -> None:
    """只改标题不该碰截止时间——`provided` 标志就是为了区分这两种语义。"""
    task = tasks.create_task(con, title="旧标题", due_at=_iso(2))
    updated = tasks.update_task(con, task.id, title="新标题")
    assert updated.title == "新标题"
    assert updated.due_at == task.due_at
    assert updated.priority is None


def test_update_clearing_both_is_rejected(con) -> None:
    task = tasks.create_task(con, title="x", due_at=_iso(1))
    with pytest.raises(tasks.TaskError):
        tasks.update_task(con, task.id, due_at=None, due_at_provided=True)


def test_update_missing_task_rejected(con) -> None:
    with pytest.raises(tasks.TaskError):
        tasks.update_task(con, 99999, title="x")


def test_update_with_no_fields_is_a_noop(con) -> None:
    task = tasks.create_task(con, title="x", priority=1)
    same = tasks.update_task(con, task.id)
    assert same.updated_at == task.updated_at


def test_invalid_status_rejected(con) -> None:
    task = tasks.create_task(con, title="x", priority=1)
    with pytest.raises(tasks.TaskError):
        tasks.update_task(con, task.id, status="finished")


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def test_invalid_category_rejected(con) -> None:
    with pytest.raises(tasks.TaskError) as excinfo:
        tasks.create_task(con, title="x", priority=1, category="HW")
    assert "类别" in str(excinfo.value)


def test_invalid_source_rejected(con) -> None:
    with pytest.raises(tasks.TaskError):
        tasks.create_task(con, title="x", priority=1, source="telegram")


@pytest.mark.parametrize("title", ["", "   ", "\u200b"])
def test_empty_title_rejected(con, title: str) -> None:
    with pytest.raises(tasks.TaskError):
        tasks.create_task(con, title=title, priority=1)


def test_title_is_cleaned_and_truncated(con) -> None:
    task = tasks.create_task(con, title="  多  空格\n标题  ", priority=1)
    assert task.title == "多 空格 标题"

    long_title = "字" * 200
    task = tasks.create_task(con, title=long_title, priority=1)
    assert len(task.title) == tasks.TITLE_MAX


def test_notes_are_truncated_to_protect_ui(con) -> None:
    task = tasks.create_task(con, title="x", priority=1, notes="备" * 5000)
    assert len(task.notes) == tasks.NOTES_MAX


# --------------------------------------------------------------------------- #
# 幂等
# --------------------------------------------------------------------------- #
def test_client_uuid_replay_returns_existing_task(con) -> None:
    """快捷指令超时重试会带同一个 client_uuid，必须去重而不是录两条。"""
    first = tasks.create_task(con, title="作业", priority=1, client_uuid="uuid-1")
    second = tasks.create_task(con, title="作业", priority=1, client_uuid="uuid-1")
    assert first.id == second.id
    assert con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 1


def test_null_client_uuid_allows_many(con) -> None:
    tasks.create_task(con, title="a", priority=1)
    tasks.create_task(con, title="b", priority=1)
    assert con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 2


# --------------------------------------------------------------------------- #
# 批量与删除
# --------------------------------------------------------------------------- #
def test_create_many_is_atomic(con) -> None:
    """确认草稿时要"全成或全不成"，否则用户会看到一半任务入库而草稿还在。"""
    payloads = [
        {"title": "ok-1", "priority": 1},
        {"title": "ok-2", "due_at": _iso(1)},
        {"title": "bad", "priority": 2, "due_at": _iso(1)},  # 二选一被违反
    ]
    with pytest.raises(tasks.TaskError):
        tasks.create_many(con, payloads)
    assert con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"] == 0


def test_create_many_success(con) -> None:
    created = tasks.create_many(
        con, [{"title": "a", "priority": 1}, {"title": "b", "due_at": _iso(1)}]
    )
    assert [t.title for t in created] == ["a", "b"]
    assert all(t.source == "llm" for t in created)


def test_delete_task(con) -> None:
    task = tasks.create_task(con, title="x", priority=1)
    assert tasks.delete_task(con, task.id) is True
    assert tasks.delete_task(con, task.id) is False
    assert tasks.get_task(con, task.id) is None


# --------------------------------------------------------------------------- #
# 输出形态
# --------------------------------------------------------------------------- #
def test_to_dict_marks_overdue(con) -> None:
    task = tasks.create_task(con, title="过期了", due_at=_iso(-2))
    payload = task.to_dict()
    assert payload["is_overdue"] is True
    assert payload["due_in_human"].endswith("前")


def test_to_dict_future_not_overdue(con) -> None:
    task = tasks.create_task(con, title="还没到", due_at=_iso(2))
    payload = task.to_dict()
    assert payload["is_overdue"] is False
    assert payload["due_in_human"].endswith("后")


def test_done_task_never_reported_overdue(con) -> None:
    task = tasks.create_task(con, title="做完了", due_at=_iso(-2))
    tasks.toggle_done(con, task.id)
    done = tasks.get_task(con, task.id)
    assert done is not None
    assert done.to_dict()["is_overdue"] is False


def test_counts_cover_all_views(con) -> None:
    tasks.create_task(con, title="有序", due_at=_iso(1))
    tasks.create_task(con, title="无序", priority=1)
    done = tasks.create_task(con, title="完成", priority=2)
    tasks.toggle_done(con, done.id)

    result = tasks.counts(con)
    assert result["ordered"] == 1
    assert result["unordered"] == 1
    assert result["done"] == 1


def test_memory_ids_roundtrip(con) -> None:
    task = tasks.create_task(con, title="x", priority=1, memory_ids=[3, 1, 2])
    reloaded = tasks.get_task(con, task.id)
    assert reloaded is not None
    assert reloaded.memory_ids == [3, 1, 2]
