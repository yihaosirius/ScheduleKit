"""任务业务规则。

**所有排序、筛选、"二选一"约束都在这里**，``routers/tasks.py`` 只做鉴权与参数转发。
这样这些规则可以脱离 HTTP 单测——它们是最容易写错、也最值得测的部分。

三条视图的语义（与 UI 的"有序表/无序表/已完成"一一对应）：

* ``ordered``   —— ``due_at`` 非空，按截止时间**升序**（最近要交的在最上）
* ``unordered`` —— ``priority`` 非空，按 Ⅰ→Ⅴ 升序，同档位内按创建时间升序
* ``done``      —— ``status='done'``，按完成时间**降序**

``cancelled`` 不出现在任何默认视图里：它是"我自己删掉了但保留痕迹"的状态，
只在显式查询时可见。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from typing import Any, Literal

from app.db import transaction
from app.logging import get_logger, kv
from app.text import clean_text, truncated
from app.timeutil import now_utc_iso, to_utc_iso, parse_utc, human_delta, now_utc

log = get_logger("tasks")

View = Literal["ordered", "unordered", "done", "all"]

CATEGORIES = ("homework", "practice", "exam", "appointment", "other")
SOURCES = ("web", "shortcut", "llm", "api")
STATUSES = ("open", "done", "cancelled")

TITLE_MAX = 60
NOTES_MAX = 500

#: 每张视图的排序 SQL。集中成常量，避免"接口改了排序但列表没改"。
_ORDER_BY: dict[str, str] = {
    "ordered": "due_at ASC, priority ASC, id ASC",
    "unordered": "priority ASC, created_at ASC, id ASC",
    "done": "completed_at DESC, id DESC",
    "all": "status ASC, due_at ASC, priority ASC, id ASC",
}

_WHERE: dict[str, str] = {
    "ordered": "status = 'open' AND due_at IS NOT NULL",
    "unordered": "status = 'open' AND priority IS NOT NULL",
    "done": "status = 'done'",
    "all": "status IN ('open', 'done')",
}


class TaskError(ValueError):
    """业务规则被违反。调用方映射成 4xx。"""


@dataclass
class Task:
    id: int
    title: str
    notes: str = ""
    category: str = "other"
    due_at: str | None = None
    priority: int | None = None
    status: str = "open"
    source: str = "web"
    client_uuid: str | None = None
    memory_ids: list[int] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    completed_at: str | None = None

    # ------------------------------------------------------------------ 派生
    @property
    def view(self) -> Literal["ordered", "unordered"]:
        return "ordered" if self.due_at else "unordered"

    @property
    def is_done(self) -> bool:
        return self.status == "done"

    def to_dict(self, *, timezone_name: str = "Asia/Shanghai") -> dict[str, Any]:
        """接口输出形态。

        ``due_in_human`` / ``is_overdue`` 由服务端算好给前端：倒计时要在
        同一个时间基准上算，前端各自算会与服务端排序结果不一致。
        """
        payload: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "notes": self.notes,
            "category": self.category,
            "due_at": self.due_at,
            "priority": self.priority,
            "status": self.status,
            "source": self.source,
            "view": self.view,
            "memory_ids": list(self.memory_ids),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "due_in_human": None,
            "is_overdue": False,
        }
        if self.due_at:
            due = parse_utc(self.due_at)
            now = now_utc()
            payload["due_in_human"] = human_delta(due, now)
            payload["is_overdue"] = due < now and self.status == "open"
        return payload


def _row_to_task(row: sqlite3.Row) -> Task:
    from app.jsonfield import loads_int_list

    return Task(
        id=row["id"],
        title=row["title"],
        notes=row["notes"],
        category=row["category"],
        due_at=row["due_at"],
        priority=row["priority"],
        status=row["status"],
        source=row["source"],
        client_uuid=row["client_uuid"],
        memory_ids=loads_int_list(row["memory_ids"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        completed_at=row["completed_at"],
    )


# --------------------------------------------------------------------------- #
# 校验
# --------------------------------------------------------------------------- #
def validate_shape(
    *, due_at: str | None, priority: int | None, context: str = "任务"
) -> tuple[str | None, int | None]:
    """强制"截止时间与优先级二选一"。

    库层也有同名 CHECK，但服务层必须先给出能读懂的中文错误：
    CHECK 抛出来的是 ``CHECK constraint failed: items``，用户看不明白。

    返回值是**规范化后**的一对值：``due_at`` 统一成 UTC ISO8601。
    """
    has_due = due_at is not None and str(due_at).strip() != ""
    has_priority = priority is not None

    if has_due and has_priority:
        raise TaskError(
            f"{context}的截止时间与优先级只能填一个："
            "有明确截止时间的进有序表，没有的才用 Ⅰ–Ⅴ 优先级。"
        )
    if not has_due and not has_priority:
        raise TaskError(f"{context}必须填截止时间或优先级之一。")

    if has_due:
        raw = str(due_at).strip()
        try:
            normalized = to_utc_iso(parse_utc(raw))
        except ValueError as exc:
            raise TaskError(f"截止时间无法解析：{raw!r}。需要 ISO8601，例如 2026-10-10T23:59:00+08:00") from exc
        return normalized, None

    value = int(priority)  # type: ignore[arg-type]
    if not 1 <= value <= 5:
        raise TaskError(f"优先级必须在 1–5 之间（对应 Ⅰ–Ⅴ），收到 {value}。")
    return None, value


def validate_category(category: str) -> str:
    if category not in CATEGORIES:
        raise TaskError(f"类别不合法：{category!r}。可选：{'、'.join(CATEGORIES)}")
    return category


def validate_source(source: str) -> str:
    if source not in SOURCES:
        raise TaskError(f"来源不合法：{source!r}。可选：{'、'.join(SOURCES)}")
    return source


def validate_title(title: str) -> str:
    """清洗标题。

    ``clean_text`` 对空串抛 ``ValueError``，这里统一转成 :class:`TaskError`：
    调用方（router）只需要处理一种业务异常类型，不必同时 catch 两种。
    """
    try:
        cleaned = clean_text(title, limit=TITLE_MAX, allow_empty=False)
    except ValueError as exc:
        raise TaskError("标题不能为空。") from exc
    if not cleaned:
        raise TaskError("标题不能为空。")
    return cleaned


def normalize_notes(notes: str | None) -> tuple[str, bool]:
    """返回 ``(清洗后的备注, 是否被截断)``。截断必须让调用方能上报。"""
    cleaned = clean_text(notes, limit=NOTES_MAX)
    if len(cleaned) <= NOTES_MAX:
        return cleaned, False
    return truncated(cleaned, NOTES_MAX)


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def list_tasks(
    con: sqlite3.Connection,
    *,
    view: str = "ordered",
    include_cancelled: bool = False,
    limit: int | None = None,
) -> list[Task]:
    if view not in _WHERE:
        raise TaskError(f"未知视图：{view!r}。可选：{'、'.join(_WHERE)}")

    where = _WHERE[view]
    if include_cancelled:
        where = where.replace("status = 'open'", "status IN ('open', 'cancelled')")

    sql = f"SELECT * FROM items WHERE {where} ORDER BY {_ORDER_BY[view]}"
    params: tuple[Any, ...] = ()
    if limit is not None:
        sql += " LIMIT ?"
        params = (int(limit),)
    return [_row_to_task(row) for row in con.execute(sql, params)]


def get_task(con: sqlite3.Connection, task_id: int) -> Task | None:
    row = con.execute("SELECT * FROM items WHERE id = ?", (task_id,)).fetchone()
    return _row_to_task(row) if row else None


def counts(con: sqlite3.Connection) -> dict[str, int]:
    """各视图计数。首页徽标与状态监测都用它。"""
    result: dict[str, int] = {}
    for view, where in _WHERE.items():
        result[view] = con.execute(f"SELECT COUNT(*) AS n FROM items WHERE {where}").fetchone()["n"]
    return result


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def create_task(
    con: sqlite3.Connection,
    *,
    title: str,
    category: str = "other",
    due_at: str | None = None,
    priority: int | None = None,
    notes: str | None = None,
    source: str = "web",
    client_uuid: str | None = None,
    memory_ids: list[int] | None = None,
    in_transaction: bool = False,
) -> Task:
    """新建任务。

    ``client_uuid`` 命中已有记录时**返回已有任务**而不是报错：
    快捷指令在网络超时后会重试，报错会让用户以为没录上而再传一次。
    """
    from app.jsonfield import dumps_int_list

    clean_title = validate_title(title)
    validate_category(category)
    validate_source(source)
    normalized_due, normalized_priority = validate_shape(due_at=due_at, priority=priority)
    clean_notes, notes_truncated = normalize_notes(notes)
    if notes_truncated:
        log.warning("tasks.notes_truncated %s", kv(limit=NOTES_MAX))

    if client_uuid:
        existing = con.execute(
            "SELECT * FROM items WHERE client_uuid = ?", (client_uuid,)
        ).fetchone()
        if existing is not None:
            log.info("tasks.client_uuid_replay %s", kv(client_uuid=client_uuid, id=existing["id"]))
            return _row_to_task(existing)

    now = now_utc_iso()
    params = (
        clean_title,
        clean_notes,
        category,
        normalized_due,
        normalized_priority,
        source,
        client_uuid,
        dumps_int_list(memory_ids or []),
        now,
        now,
    )
    sql = (
        "INSERT INTO items (title, notes, category, due_at, priority, status, source,"
        " client_uuid, memory_ids, created_at, updated_at)"
        " VALUES (?, ?, ?, ?, ?, 'open', ?, ?, ?, ?, ?)"
    )

    if in_transaction:
        cursor = con.execute(sql, params)
    else:
        with transaction(con):
            cursor = con.execute(sql, params)

    task = get_task(con, int(cursor.lastrowid))
    assert task is not None  # 刚插入必然存在
    log.info(
        "tasks.created %s",
        kv(id=task.id, view=task.view, category=task.category, source=task.source),
    )
    return task


def update_task(
    con: sqlite3.Connection,
    task_id: int,
    *,
    title: str | None = None,
    category: str | None = None,
    due_at: str | None = None,
    priority: int | None = None,
    notes: str | None = None,
    status: str | None = None,
    due_at_provided: bool = False,
    priority_provided: bool = False,
    in_transaction: bool = False,
) -> Task:
    """局部更新。

    ``due_at_provided`` / ``priority_provided`` 与值本身分开传，是为了区分
    "没改这个字段"和"显式清空它"——``None`` 在这两种语义下是一样的，
    而我们的二选一约束恰好要求"清空一个、填上另一个"。
    """
    current = get_task(con, task_id)
    if current is None:
        raise TaskError(f"任务不存在：id={task_id}")

    fields: dict[str, Any] = {}

    if title is not None:
        fields["title"] = validate_title(title)
    if category is not None:
        fields["category"] = validate_category(category)
    if notes is not None:
        clean_notes, _ = normalize_notes(notes)
        fields["notes"] = clean_notes
    if status is not None:
        if status not in STATUSES:
            raise TaskError(f"状态不合法：{status!r}。可选：{'、'.join(STATUSES)}")
        fields["status"] = status

    next_due = current.due_at
    next_priority = current.priority
    if due_at_provided:
        next_due = due_at if (due_at or "").strip() else None
        # 显式填了截止时间就意味着放弃优先级，否则会撞上二选一约束
        next_priority = None
    if priority_provided:
        next_priority = priority
        next_due = None

    if due_at_provided or priority_provided:
        normalized_due, normalized_priority = validate_shape(
            due_at=next_due, priority=next_priority
        )
        fields["due_at"] = normalized_due
        fields["priority"] = normalized_priority

    # 状态与完成时间必须同步，否则"已完成"列表会按 NULL 排序而乱序
    if "status" in fields:
        if fields["status"] == "done" and current.status != "done":
            fields["completed_at"] = now_utc_iso()
        elif fields["status"] != "done":
            fields["completed_at"] = None

    if not fields:
        return current

    fields["updated_at"] = now_utc_iso()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    values = list(fields.values()) + [task_id]

    if in_transaction:
        con.execute(f"UPDATE items SET {assignments} WHERE id = ?", values)
    else:
        with transaction(con):
            con.execute(f"UPDATE items SET {assignments} WHERE id = ?", values)

    updated = get_task(con, task_id)
    assert updated is not None
    log.info("tasks.updated %s", kv(id=task_id, fields=",".join(fields)))
    return updated


def toggle_done(con: sqlite3.Connection, task_id: int) -> Task:
    current = get_task(con, task_id)
    if current is None:
        raise TaskError(f"任务不存在：id={task_id}")
    target = "open" if current.status == "done" else "done"
    return update_task(con, task_id, status=target)


def delete_task(con: sqlite3.Connection, task_id: int) -> bool:
    with transaction(con):
        cursor = con.execute("DELETE FROM items WHERE id = ?", (task_id,))
    deleted = cursor.rowcount > 0
    log.info("tasks.deleted %s", kv(id=task_id, deleted=deleted))
    return deleted


def create_many(
    con: sqlite3.Connection, payloads: list[dict[str, Any]], *, source: str = "llm"
) -> list[Task]:
    """确认草稿时批量入库。整批一个事务：要么全成，要么一条都不写。"""
    created: list[Task] = []
    with transaction(con):
        for payload in payloads:
            created.append(
                create_task(con, source=source, in_transaction=True, **payload)
            )
    log.info("tasks.created_many %s", kv(count=len(created), source=source))
    return created
