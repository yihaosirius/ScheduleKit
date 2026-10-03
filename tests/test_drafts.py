"""草稿状态机的测试。

这里守的核心性质只有一条，但它是产品需求的直接体现：

**草稿阶段 items 表零写入。**

其余测试都围绕它展开：确认是唯一的入库点、确认必须原子、
状态机的非法迁移必须被拒（不能确认两次、不能确认已丢弃的）。
"""

from __future__ import annotations

import sqlite3

import pytest

from app.db import connect
from app.migrations import runner
from app.services import drafts as drafts_service
from app.services import tasks as tasks_service
from app.services.drafts import DraftError


@pytest.fixture
def con(work_dir) -> sqlite3.Connection:
    connection = connect(work_dir / "drafts.db")
    runner.run(connection)
    yield connection
    connection.close()


def item(title: str = "写作业", **overrides) -> dict:
    base = {"title": title, "category": "homework", "due_at": None, "priority": 3, "notes": ""}
    base.update(overrides)
    return base


def make(con, *, items=None, text="原始输入", channel="web", **kwargs) -> drafts_service.Draft:
    return drafts_service.create_draft(
        con,
        channel=channel,
        text_input=text,
        items=items if items is not None else [item()],
        memory_ids=kwargs.pop("memory_ids", []),
        llm_path=kwargs.pop("llm_path", "tool_call"),
        llm_model="mock",
        llm_elapsed_ms=12.5,
        raw_output='{"items":[]}',
        **kwargs,
    )


def item_count(con: sqlite3.Connection) -> int:
    return con.execute("SELECT COUNT(*) AS n FROM items").fetchone()["n"]


# --------------------------------------------------------------------------- #
# 零写入不变量
# --------------------------------------------------------------------------- #
def test_creating_draft_writes_no_tasks(con) -> None:
    make(con, items=[item("A"), item("B"), item("C")])
    assert item_count(con) == 0, "草稿阶段绝不能写 items 表"


def test_replacing_items_writes_no_tasks(con) -> None:
    draft = make(con, items=[item("A")])
    drafts_service.replace_items(con, draft.id, [item("改过的"), item("又加一条")])
    assert item_count(con) == 0


def test_confirm_is_the_only_write_path(con) -> None:
    draft = make(con, items=[item("A"), item("B")])
    assert item_count(con) == 0
    _, created = drafts_service.confirm_draft(con, draft.id)
    assert item_count(con) == 2
    assert len(created) == 2


# --------------------------------------------------------------------------- #
# 状态机
# --------------------------------------------------------------------------- #
def test_confirm_marks_confirmed_and_records_time(con) -> None:
    draft = make(con)
    updated, _ = drafts_service.confirm_draft(con, draft.id)
    assert updated.status == "confirmed"
    assert updated.confirmed_at is not None
    assert updated.seconds_left() != 0  # 时间字段仍可读，没被清掉


def test_double_confirm_is_rejected(con) -> None:
    draft = make(con)
    drafts_service.confirm_draft(con, draft.id)
    with pytest.raises(DraftError) as excinfo:
        drafts_service.confirm_draft(con, draft.id)
    assert "confirmed" in str(excinfo.value)


def test_confirm_after_discard_is_rejected(con) -> None:
    draft = make(con)
    drafts_service.discard_draft(con, draft.id)
    with pytest.raises(DraftError):
        drafts_service.confirm_draft(con, draft.id)


def test_discard_twice_is_rejected(con) -> None:
    draft = make(con)
    drafts_service.discard_draft(con, draft.id)
    with pytest.raises(DraftError):
        drafts_service.discard_draft(con, draft.id)


def test_discard_keeps_row_for_stats(con) -> None:
    """"用户丢掉了多少识别结果"是判断 prompt 质量的直接指标，行不能删。"""
    draft = make(con)
    drafts_service.discard_draft(con, draft.id)
    counts = drafts_service.counts(con)
    assert counts["discarded"] == 1
    assert drafts_service.get_draft(con, draft.id) is not None


def test_missing_draft_is_reported(con) -> None:
    with pytest.raises(DraftError) as excinfo:
        drafts_service.get_pending(con, 999)
    assert "不存在" in str(excinfo.value)


# --------------------------------------------------------------------------- #
# 确认时用编辑过的数据
# --------------------------------------------------------------------------- #
def test_confirm_uses_edited_items(con) -> None:
    draft = make(con, items=[item("原标题")])
    _, created = drafts_service.confirm_draft(con, draft.id, items=[item("我改的标题")])
    assert created[0].title == "我改的标题"
    # 草稿本身也更新成最终形态，便于事后核对"用户改了什么"
    refreshed = drafts_service.get_draft(con, draft.id)
    assert refreshed is not None
    assert refreshed.items[0]["title"] == "我改的标题"


def test_confirm_validates_edited_items(con) -> None:
    """二次修改必须重跑业务校验——不能只靠前端拦。"""
    draft = make(con)
    with pytest.raises(DraftError) as excinfo:
        drafts_service.confirm_draft(
            con,
            draft.id,
            items=[item(due_at="2026-10-10T23:59:00+08:00", priority=1)],
        )
    assert "只能填一个" in str(excinfo.value)
    assert item_count(con) == 0, "校验失败时不该有任何写入"


def test_confirm_rejects_empty_list(con) -> None:
    draft = make(con, items=[])
    with pytest.raises(DraftError) as excinfo:
        drafts_service.confirm_draft(con, draft.id)
    assert "没有任何可入库" in str(excinfo.value)


def test_confirm_is_atomic(con) -> None:
    """一条非法就让整批都不入库，避免"草稿已确认但只进去两条"。"""
    draft = make(con)
    with pytest.raises(DraftError):
        drafts_service.confirm_draft(
            con,
            draft.id,
            items=[item("合法"), item("非法", due_at="2026-10-10T23:59:00+08:00", priority=2)],
        )
    assert item_count(con) == 0
    still = drafts_service.get_draft(con, draft.id)
    assert still is not None and still.status == "pending", "失败的确认不该改状态"


def test_confirm_tasks_carry_memory_ids(con) -> None:
    """任务上留下"参考了哪些记忆"的痕迹，便于事后判断识别依据。"""
    draft = make(con, memory_ids=[3, 7])
    _, created = drafts_service.confirm_draft(con, draft.id)
    assert created[0].memory_ids == [3, 7]


def test_confirm_source_is_llm(con) -> None:
    draft = make(con)
    _, created = drafts_service.confirm_draft(con, draft.id)
    assert created[0].source == "llm"


def test_confirmed_tasks_land_in_correct_views(con) -> None:
    draft = make(
        con,
        items=[
            item("有截止", due_at="2026-10-10T23:59:00+08:00", priority=None),
            item("有优先级", due_at=None, priority=2),
        ],
    )
    _, created = drafts_service.confirm_draft(con, draft.id)
    assert {task.view for task in created} == {"ordered", "unordered"}
    assert tasks_service.counts(con)["ordered"] == 1
    assert tasks_service.counts(con)["unordered"] == 1


# --------------------------------------------------------------------------- #
# 列表与清理
# --------------------------------------------------------------------------- #
def test_list_filters_by_status(con) -> None:
    first = make(con, text="第一条")
    second = make(con, text="第二条")
    drafts_service.discard_draft(con, first.id)

    pending = drafts_service.list_drafts(con, status="pending")
    assert [draft.id for draft in pending] == [second.id]

    discarded = drafts_service.list_drafts(con, status="discarded")
    assert [draft.id for draft in discarded] == [first.id]

    everything = drafts_service.list_drafts(con, status=None)
    assert len(everything) == 2


def test_list_rejects_unknown_status(con) -> None:
    with pytest.raises(DraftError):
        drafts_service.list_drafts(con, status="weird")


def test_purge_expired_removes_only_pending(con) -> None:
    fresh = make(con, text="还没到期")
    old = make(con, text="已到期")
    # 手工把一条改成过期
    con.execute(
        "UPDATE ingest_drafts SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (old.id,),
    )

    purged = drafts_service.purge_expired(con)
    assert purged.drafts == [old.id]
    assert purged.count == 1, "计数必须按条数算，不能按图片数算"
    assert drafts_service.get_draft(con, old.id) is None
    assert drafts_service.get_draft(con, fresh.id) is not None


def test_purge_count_is_not_based_on_images(con) -> None:
    """一条没有图片的草稿被删掉后，计数不能是 0。

    这个 bug 真的出现过：purge 只返回图片路径，于是"清理了几条"长期显示 0，
    而排查磁盘问题时"0 条"与"5 条"的含义完全不同。
    """
    draft = make(con, text="没有图的草稿")
    con.execute(
        "UPDATE ingest_drafts SET expires_at = '2000-01-01T00:00:00+00:00' WHERE id = ?",
        (draft.id,),
    )
    purged = drafts_service.purge_expired(con)
    assert purged.count == 1
    assert purged.image_paths == []


def test_purge_expired_never_touches_confirmed(con) -> None:
    confirmed = make(con)
    drafts_service.confirm_draft(con, confirmed.id)
    con.execute("UPDATE ingest_drafts SET expires_at = '2000-01-01T00:00:00+00:00'")

    drafts_service.purge_expired(con)
    assert drafts_service.get_draft(con, confirmed.id) is not None, (
        "confirmed 是溯源痕迹，不该被过期清理删掉"
    )


def test_purge_all_pending_reports_ids_and_images(con) -> None:
    make(con, image_path="uploads/ab/sha.jpg", image_mime="image/jpeg", image_bytes=10)
    make(con, text="没有图")
    purged = drafts_service.purge_all_pending(con)
    assert purged.count == 2
    assert purged.image_paths == ["uploads/ab/sha.jpg"]
    assert drafts_service.list_drafts(con, status="pending") == []


def test_purge_all_pending_ignores_confirmed(con) -> None:
    confirmed = make(con)
    drafts_service.confirm_draft(con, confirmed.id)
    make(con, text="待确认")
    drafts_service.purge_all_pending(con)
    assert drafts_service.get_draft(con, confirmed.id) is not None


def test_output_shape_hides_internal_fields(con) -> None:
    """``_source_quote`` 是排查字段，不该出现在接口输出里。"""
    draft = make(con, items=[item("x", _source_quote="下周一交")])
    public = draft.to_dict()
    assert "_source_quote" not in public["items"][0]

    internal = draft.to_dict(include_internal=True)
    assert internal["items"][0]["_source_quote"] == "下周一交"


def test_to_dict_reports_seconds_left(con) -> None:
    draft = make(con, ttl_hours=48)
    payload = draft.to_dict()
    assert payload["seconds_left"] > 47 * 3600
    assert payload["image_url"] is None


def test_degraded_path_is_visible(con) -> None:
    """降级必须能被界面看见，否则用户不知道这次结果可不可信。"""
    draft = make(
        con,
        llm_path="json_object",
        fallback_note="tool_call 通道不可用：HTTP 400",
        overrides="第 1 条同时给了截止时间与优先级，已丢弃优先级",
    )
    payload = draft.to_dict()
    assert payload["llm_path"] == "json_object"
    assert "400" in payload["fallback_note"]
    assert payload["overrides"] and "丢弃优先级" in payload["overrides"][0]


def test_malformed_json_in_db_does_not_break_listing(con) -> None:
    """库里一条脏 JSON 不该让整个草稿箱接口 500。"""
    draft = make(con)
    con.execute("UPDATE ingest_drafts SET items_json = '{坏数据' WHERE id = ?", (draft.id,))
    listing = drafts_service.list_drafts(con)
    assert listing[0].items == []
