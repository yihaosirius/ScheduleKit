"""两阶段录入的草稿状态机。

**最重要的一条不变量：草稿阶段 ``items`` 表零写入。**

这条不是洁癖，而是需求本身——"识别后进入草稿，用户确认并二次修改后才入库"。
如果识别时就写库，用户在确认页点"取消"就会留下一条已经存在的任务，
而且我们无法区分"用户还没确认的"和"用户确认过的"。

状态机::

    pending ──confirm──> confirmed      （同时批量写入 items，同事务）
       │
       └─────discard───> discarded      （保留行，便于统计与排查）

到期（``expires_at``）的 pending 草稿由 housekeeping 删除；图片按
``[backup].upload_retention_days`` 单独清理。**confirmed 草稿不自动删**——
它是"这条任务是从哪来的"的唯一痕迹。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from app.db import transaction
from app.jsonfield import dumps, dumps_int_list, loads_dict_list, loads_int_list
from app.logging import get_logger, kv
from app.services import tasks as tasks_service
from app.timeutil import now_utc, now_utc_iso, parse_utc

log = get_logger("drafts")

STATUSES = ("pending", "confirmed", "discarded")

#: 草稿条目的字段顺序 —— 确认页按这个顺序渲染，接口也按这个顺序返回。
#: 下划线开头的 ``_source_quote`` 不在这里：它是内部排查字段，不是可编辑任务字段。
EDITABLE_FIELDS = ("title", "category", "due_at", "priority", "notes")


class DraftError(ValueError):
    """草稿状态机被违反，或确认数据不合法。"""


@dataclass
class Draft:
    id: int
    status: str
    channel: str
    text_input: str
    image_path: str | None
    image_mime: str | None
    image_bytes: int | None
    image_sha256: str | None
    llm_path: str
    fallback_note: str | None
    llm_model: str
    llm_elapsed_ms: float
    items: list[dict[str, Any]] = field(default_factory=list)
    memory_ids: list[int] = field(default_factory=list)
    overrides: str = ""
    created_at: str = ""
    expires_at: str = ""
    confirmed_at: str | None = None

    # ------------------------------------------------------------------ 派生
    @property
    def is_pending(self) -> bool:
        return self.status == "pending"

    @property
    def has_image(self) -> bool:
        return bool(self.image_path)

    def seconds_left(self, now=None) -> int:
        moment = now or now_utc()
        return int((parse_utc(self.expires_at) - moment).total_seconds())

    def to_dict(self, *, include_internal: bool = False) -> dict[str, Any]:
        items = [
            {k: v for k, v in item.items() if include_internal or not k.startswith("_")}
            for item in self.items
        ]
        payload: dict[str, Any] = {
            "id": self.id,
            "status": self.status,
            "channel": self.channel,
            "text_input": self.text_input,
            "has_image": self.has_image,
            "image_url": f"/api/drafts/{self.id}/image" if self.has_image else None,
            "llm_path": self.llm_path,
            "fallback_note": self.fallback_note,
            "llm_model": self.llm_model,
            "llm_elapsed_ms": round(self.llm_elapsed_ms, 1),
            "items": items,
            "item_count": len(items),
            "memory_ids": list(self.memory_ids),
            "overrides": [part for part in self.overrides.split("；") if part],
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "seconds_left": self.seconds_left(),
            "confirmed_at": self.confirmed_at,
        }
        return payload


def _row_to_draft(row: sqlite3.Row) -> Draft:
    return Draft(
        id=row["id"],
        status=row["status"],
        channel=row["channel"],
        text_input=row["text_input"],
        image_path=row["image_path"],
        image_mime=row["image_mime"],
        image_bytes=row["image_bytes"],
        image_sha256=row["image_sha256"],
        llm_path=row["llm_path"],
        fallback_note=row["fallback_note"],
        llm_model=row["llm_model"],
        llm_elapsed_ms=row["llm_elapsed_ms"] or 0.0,
        items=loads_dict_list(row["items_json"]),
        memory_ids=loads_int_list(row["memory_ids"]),
        overrides=row["overrides"],
        created_at=row["created_at"],
        expires_at=row["expires_at"],
        confirmed_at=row["confirmed_at"],
    )


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def get_draft(con: sqlite3.Connection, draft_id: int) -> Draft | None:
    row = con.execute("SELECT * FROM ingest_drafts WHERE id = ?", (draft_id,)).fetchone()
    return _row_to_draft(row) if row else None


def get_pending(con: sqlite3.Connection, draft_id: int) -> Draft:
    draft = get_draft(con, draft_id)
    if draft is None:
        raise DraftError(f"草稿不存在：id={draft_id}")
    if draft.status != "pending":
        raise DraftError(
            f"草稿 {draft_id} 已是 {draft.status} 状态，不能再操作"
            f"（{'已确认入库' if draft.status == 'confirmed' else '已被丢弃'}）"
        )
    return draft


def list_drafts(
    con: sqlite3.Connection, *, status: str | None = "pending", limit: int = 100
) -> list[Draft]:
    if status is not None and status not in STATUSES:
        raise DraftError(f"状态不合法：{status!r}")
    if status:
        rows = con.execute(
            "SELECT * FROM ingest_drafts WHERE status = ? ORDER BY created_at DESC, id DESC LIMIT ?",
            (status, int(limit)),
        )
    else:
        rows = con.execute(
            "SELECT * FROM ingest_drafts ORDER BY created_at DESC, id DESC LIMIT ?",
            (int(limit),),
        )
    return [_row_to_draft(row) for row in rows]


def counts(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute(
        "SELECT status, COUNT(*) AS n FROM ingest_drafts GROUP BY status"
    ).fetchall()
    result = {status: 0 for status in STATUSES}
    for row in rows:
        result[row["status"]] = row["n"]
    return result


# --------------------------------------------------------------------------- #
# 写：创建
# --------------------------------------------------------------------------- #
def create_draft(
    con: sqlite3.Connection,
    *,
    channel: str,
    text_input: str,
    items: list[dict[str, Any]],
    memory_ids: list[int],
    llm_path: str,
    llm_model: str,
    llm_elapsed_ms: float,
    raw_output: str,
    fallback_note: str | None = None,
    overrides: str = "",
    image_path: str | None = None,
    image_mime: str | None = None,
    image_bytes: int | None = None,
    image_sha256: str | None = None,
    ttl_hours: int = 48,
) -> Draft:
    now = now_utc()
    expires = now + timedelta(hours=max(1, int(ttl_hours)))
    with transaction(con):
        cursor = con.execute(
            "INSERT INTO ingest_drafts (status, channel, text_input, image_path, image_mime,"
            " image_bytes, image_sha256, llm_path, fallback_note, llm_model, llm_elapsed_ms,"
            " raw_output, items_json, memory_ids, overrides, created_at, expires_at)"
            " VALUES ('pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                channel,
                text_input,
                image_path,
                image_mime,
                image_bytes,
                image_sha256,
                llm_path,
                fallback_note,
                llm_model,
                float(llm_elapsed_ms),
                raw_output,
                dumps(items),
                dumps_int_list(memory_ids),
                overrides,
                now_utc_iso(),
                expires.replace(microsecond=0).isoformat(),
            ),
        )
    draft = get_draft(con, int(cursor.lastrowid))
    assert draft is not None
    log.info(
        "draft.created %s",
        kv(
            id=draft.id,
            channel=channel,
            llm_path=llm_path,
            items=len(items),
            memories=len(memory_ids),
            has_image=draft.has_image,
            degraded=llm_path != "tool_call",
        ),
    )
    return draft


# --------------------------------------------------------------------------- #
# 写：确认
# --------------------------------------------------------------------------- #
def confirm_draft(
    con: sqlite3.Connection,
    draft_id: int,
    *,
    items: list[dict[str, Any]] | None = None,
) -> tuple[Draft, list[tasks_service.Task]]:
    """确认草稿并入库。

    ``items`` 为 None 表示"直接用草稿里的解析结果"；否则用用户二次修改过的版本。

    **整个操作在一个事务里**：草稿内的条目是 1:1 映射到任务的，
    部分成功会让用户看到"草稿已确认，但只进去 2 条"这种无法理解的状态。
    """
    draft = get_pending(con, draft_id)
    payload = _validated_items(draft, items)

    if not payload:
        raise DraftError("草稿里没有任何可入库的条目；没有识别到事项时请直接丢弃草稿。")

    with transaction(con):
        created: list[tasks_service.Task] = []
        for entry in payload:
            created.append(
                tasks_service.create_task(
                    con,
                    title=entry["title"],
                    category=entry["category"],
                    due_at=entry["due_at"],
                    priority=entry["priority"],
                    notes=entry.get("notes") or "",
                    source="llm",
                    memory_ids=draft.memory_ids,
                    # 草稿里的条目没有 client_uuid：它们还没经过任何客户端。
                    # 幂等保护交给 status 状态机（confirmed 后不能再确认）。
                    in_transaction=True,
                )
            )
        con.execute(
            "UPDATE ingest_drafts SET status = 'confirmed', confirmed_at = ?, items_json = ?"
            " WHERE id = ?",
            (now_utc_iso(), dumps(payload), draft_id),
        )

    updated = get_draft(con, draft_id)
    assert updated is not None
    log.info(
        "draft.confirmed %s",
        kv(id=draft_id, tasks=len(created), edited=items is not None),
    )
    return updated, created


def _validated_items(
    draft: Draft, items: list[dict[str, Any]] | None
) -> list[dict[str, Any]]:
    """校验确认时提交的条目。

    确认接口接受的是**用户改过的**数据，所以必须重跑一遍业务校验
    （二选一、类别、标题长度）。把校验放在这里而不是 router 里，
    是为了让"确认"这条路径与"直接建任务"共用同一套规则。
    """
    source = items if items is not None else draft.items
    if not isinstance(source, list):
        raise DraftError("items 必须是数组")

    prepared: list[dict[str, Any]] = []
    for index, raw in enumerate(source):
        if not isinstance(raw, dict):
            raise DraftError(f"第 {index + 1} 条不是对象")
        try:
            title = tasks_service.validate_title(raw.get("title"))
            category = tasks_service.validate_category(
                (raw.get("category") or "other")
            )
            due_at, priority = tasks_service.validate_shape(
                due_at=raw.get("due_at"),
                priority=raw.get("priority"),
                context=f"第 {index + 1} 条",
            )
            notes, _ = tasks_service.normalize_notes(raw.get("notes"))
        except tasks_service.TaskError as exc:
            raise DraftError(str(exc)) from exc
        prepared.append(
            {
                "title": title,
                "category": category,
                "due_at": due_at,
                "priority": priority,
                "notes": notes,
            }
        )
    return prepared


def replace_items(
    con: sqlite3.Connection, draft_id: int, items: list[dict[str, Any]]
) -> Draft:
    """保存二次修改，但不入库。

    给确认页的"暂存"用：用户在手机上改了几条、被打断，回来时不该从头再填。
    """
    draft = get_pending(con, draft_id)
    prepared = _validated_items(draft, items)
    with transaction(con):
        con.execute(
            "UPDATE ingest_drafts SET items_json = ? WHERE id = ?",
            (dumps(prepared), draft_id),
        )
    updated = get_draft(con, draft_id)
    assert updated is not None
    log.info("draft.items_replaced %s", kv(id=draft_id, items=len(prepared)))
    return updated


# --------------------------------------------------------------------------- #
# 写：丢弃与清理
# --------------------------------------------------------------------------- #
def discard_draft(con: sqlite3.Connection, draft_id: int) -> Draft:
    get_pending(con, draft_id)
    with transaction(con):
        con.execute(
            "UPDATE ingest_drafts SET status = 'discarded' WHERE id = ?", (draft_id,)
        )
    updated = get_draft(con, draft_id)
    assert updated is not None
    log.info("draft.discarded %s", kv(id=draft_id))
    return updated


def link_image(
    con: sqlite3.Connection,
    draft_id: int,
    *,
    image_path: str,
    image_mime: str,
    image_bytes: int,
    image_sha256: str,
) -> None:
    """补写图片信息。

    为什么分两步：``create_draft`` 需要知道"有没有图"来拼提示词，
    而图片是在 ``store_image`` 之前就上传了的——顺序上先落盘再建草稿更自然。
    这个方法的存在是为了让"先建草稿、后补图片引用"也可以，但目前不用于反序。
    """
    with transaction(con):
        con.execute(
            "UPDATE ingest_drafts SET image_path = ?, image_mime = ?, image_bytes = ?,"
            " image_sha256 = ? WHERE id = ?",
            (image_path, image_mime, image_bytes, image_sha256, draft_id),
        )


@dataclass(frozen=True)
class PurgeResult:
    """一次清理的结果。

    刻意分成"删了几条"与"这些草稿引用了哪些图片"两项：只要图片路径的话，
    一条**没有图片**的草稿被删掉后会报告 0 条已清理——计数就成了假的。
    这个 bug 真的发生过（housekeeping 的 drafts_purged 一直显示 0），
    而"清理了 0 条"与"清理了 5 条"在排查磁盘问题时含义完全不同。
    """

    drafts: list[int]
    image_paths: list[str]

    @property
    def count(self) -> int:
        return len(self.drafts)


def purge_expired(con: sqlite3.Connection) -> PurgeResult:
    """删除到期的 pending 草稿。

    只删 pending：``confirmed`` 是溯源痕迹，``discarded`` 保留用于统计
    "用户丢掉了多少识别结果"——这个比例是判断 prompt 质量的直接指标。
    """
    now = now_utc_iso()
    rows = con.execute(
        "SELECT id, image_path FROM ingest_drafts WHERE status = 'pending' AND expires_at <= ?",
        (now,),
    ).fetchall()
    if not rows:
        return PurgeResult(drafts=[], image_paths=[])

    ids = [int(row["id"]) for row in rows]
    image_paths = [row["image_path"] for row in rows if row["image_path"]]
    with transaction(con):
        con.executemany("DELETE FROM ingest_drafts WHERE id = ?", [(i,) for i in ids])
    log.info("draft.purged %s", kv(count=len(ids), images=len(image_paths)))
    return PurgeResult(drafts=ids, image_paths=image_paths)


def purge_all_pending(con: sqlite3.Connection) -> PurgeResult:
    """草稿箱"一键清空"。"""
    rows = con.execute(
        "SELECT id, image_path FROM ingest_drafts WHERE status = 'pending'"
    ).fetchall()
    if not rows:
        return PurgeResult(drafts=[], image_paths=[])

    ids = [int(row["id"]) for row in rows]
    image_paths = [row["image_path"] for row in rows if row["image_path"]]
    with transaction(con):
        con.executemany("DELETE FROM ingest_drafts WHERE id = ?", [(i,) for i in ids])
    log.info("draft.purged_all %s", kv(count=len(ids), images=len(image_paths)))
    return PurgeResult(drafts=ids, image_paths=image_paths)


def referenced_image_paths(con: sqlite3.Connection) -> set[str]:
    """仍在被引用的图片路径。housekeeping 据此判断哪些图片成了孤儿。"""
    rows = con.execute(
        "SELECT image_path FROM ingest_drafts WHERE image_path IS NOT NULL"
    ).fetchall()
    return {row["image_path"] for row in rows}
