"""记忆条目的业务规则。

记忆的定位（写在最前面，避免后来者误解）：**它是给模型的参考素材，不是待办事项。**

它解决的具体问题是消歧。例如用户长期有"电子电路基础的作业周三交"这条约定，
那么一张只写着"这次作业"的群聊截图，模型需要这条记忆才能补出课程名与日期。
反过来，如果模型把记忆内容本身当成待办提交，就会凭空多出一条任务——
那条"不得把记忆内容当作待办本身提交"的约束就是这个原因。

注入选择（``select_for_injection``）的三个来源，优先级从高到低：

1. ``pin_single=1`` —— 用户在 UI 上对某一条点开"下次识别带上它"
2. ``manual_ids`` —— 本次请求显式指定（快捷指令可以用 extra 字段传）
3. ``scope='global'`` 且 ``enabled=1`` —— 默认全量注入

``scope='course'`` 的条目**只在它关联的课程出现在上下文里时才注入**，
否则"复变的作业周三交"会在识别英语作业时也被塞进去，反而增加干扰。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

from app.db import transaction
from app.logging import get_logger, kv
from app.text import clean_multiline, clean_text
from app.timeutil import now_utc_iso

log = get_logger("memory")

TITLE_MAX = 40
CONTENT_MAX = 500
TAGS_MAX = 120

SCOPES = ("global", "course")

#: 记忆条目的默认字符预算。真实值从配置读，见 ``app/services/drafts.py``。
DEFAULT_CHAR_BUDGET = 2000


class MemoryError(ValueError):
    """记忆条目数据不合法。"""


@dataclass
class Memory:
    id: int
    title: str
    content: str
    tags: str = ""
    scope: str = "global"
    course_id: int | None = None
    enabled: bool = True
    #: 用户在 UI 上标记"下次识别单条注入这条"
    pin_single: bool = False
    sort_order: int = 0
    created_at: str = ""
    updated_at: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "content": self.content,
            "tags": [t for t in self.tags.split(",") if t],
            "scope": self.scope,
            "course_id": self.course_id,
            "enabled": self.enabled,
            "pin_single": self.pin_single,
            "sort_order": self.sort_order,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


def _row_to_memory(row: sqlite3.Row) -> Memory:
    return Memory(
        id=row["id"],
        title=row["title"],
        content=row["content"],
        tags=row["tags"],
        scope=row["scope"],
        course_id=row["course_id"],
        enabled=bool(row["enabled"]),
        pin_single=bool(row["pin_single"]),
        sort_order=row["sort_order"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


# --------------------------------------------------------------------------- #
# 读
# --------------------------------------------------------------------------- #
def list_memories(
    con: sqlite3.Connection, *, enabled_only: bool = False
) -> list[Memory]:
    sql = "SELECT * FROM memories"
    if enabled_only:
        sql += " WHERE enabled = 1"
    sql += " ORDER BY sort_order, id"
    return [_row_to_memory(row) for row in con.execute(sql)]


def get_memory(con: sqlite3.Connection, memory_id: int) -> Memory | None:
    row = con.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
    return _row_to_memory(row) if row else None


def select_for_injection(
    con: sqlite3.Connection,
    *,
    course_ids_in_context: set[int] | None = None,
    manual_ids: list[int] | None = None,
) -> list[Memory]:
    """挑出本次要注入的记忆条目。返回顺序即注入顺序。

    ``course_ids_in_context`` 为 None 表示"本次没有课程上下文"，
    此时 scope='course' 的条目一律不注入——没有上下文就无从判断相关性。
    """
    course_ids = course_ids_in_context or set()
    selected: dict[int, Memory] = {}

    for memory in list_memories(con, enabled_only=True):
        if memory.scope == "global":
            selected[memory.id] = memory
        elif memory.scope == "course" and memory.course_id in course_ids:
            selected[memory.id] = memory

    # 用户显式点选的单条注入：即使 disabled 也要带上（用户意图优先于默认开关），
    # 但要检查它是否真的存在。
    for memory_id in manual_ids or []:
        if memory_id in selected:
            continue
        memory = get_memory(con, memory_id)
        if memory is None:
            log.warning("memory.manual_missing %s", kv(id=memory_id))
            continue
        selected[memory_id] = memory

    for memory in list_memories(con):
        if memory.pin_single:
            selected[memory.id] = memory

    result = sorted(
        selected.values(), key=lambda m: (0 if m.pin_single else 1, m.sort_order, m.id)
    )
    log.info(
        "memory.selected %s",
        kv(count=len(result), pinned=sum(1 for m in result if m.pin_single), course_ids=len(course_ids)),
    )
    return result


# --------------------------------------------------------------------------- #
# 写
# --------------------------------------------------------------------------- #
def validate_title(title: str) -> str:
    try:
        return clean_text(title, limit=TITLE_MAX, allow_empty=False)
    except ValueError as exc:
        raise MemoryError("记忆标题不能为空。") from exc


def validate_content(content: str) -> str:
    cleaned = clean_multiline(content, limit=CONTENT_MAX)
    if not cleaned:
        raise MemoryError("记忆内容不能为空。")
    return cleaned


def validate_scope(scope: str) -> str:
    if scope not in SCOPES:
        raise MemoryError(f"scope 不合法：{scope!r}。可选：{'、'.join(SCOPES)}")
    return scope


def _validate_course(con: sqlite3.Connection, course_id: int | None) -> int | None:
    if course_id is None:
        return None
    row = con.execute("SELECT id FROM courses WHERE id = ?", (course_id,)).fetchone()
    if row is None:
        raise MemoryError(f"课程不存在：id={course_id}")
    return int(row["id"])


def create_memory(
    con: sqlite3.Connection,
    *,
    title: str,
    content: str,
    tags: str = "",
    scope: str = "global",
    course_id: int | None = None,
    enabled: bool = True,
    pin_single: bool = False,
    sort_order: int | None = None,
) -> Memory:
    clean_title = validate_title(title)
    clean_content = validate_content(content)
    validate_scope(scope)

    # scope='global' 时不该挂课程；scope='course' 时必须有课程。
    # 这两条如果不强制，UI 上会出现"全局记忆却标着某门课"这种自相矛盾的数据。
    if scope == "global":
        course_id = None
    else:
        if course_id is None:
            raise MemoryError("scope='course' 的记忆必须指定课程。")
        course_id = _validate_course(con, course_id)

    if sort_order is None:
        row = con.execute("SELECT COALESCE(MAX(sort_order), 0) AS n FROM memories").fetchone()
        sort_order = int(row["n"]) + 10

    now = now_utc_iso()
    with transaction(con):
        cursor = con.execute(
            "INSERT INTO memories (title, content, tags, scope, course_id, enabled,"
            " pin_single, sort_order, created_at, updated_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                clean_title,
                clean_content,
                clean_text(tags, limit=TAGS_MAX),
                scope,
                course_id,
                int(enabled),
                int(pin_single),
                sort_order,
                now,
                now,
            ),
        )
    memory = get_memory(con, int(cursor.lastrowid))
    assert memory is not None
    log.info(
        "memory.created %s",
        kv(id=memory.id, scope=memory.scope, course_id=memory.course_id, enabled=memory.enabled),
    )
    return memory


def update_memory(
    con: sqlite3.Connection,
    memory_id: int,
    *,
    title: str | None = None,
    content: str | None = None,
    tags: str | None = None,
    scope: str | None = None,
    course_id: int | None = None,
    course_id_provided: bool = False,
    enabled: bool | None = None,
    pin_single: bool | None = None,
    sort_order: int | None = None,
) -> Memory:
    current = get_memory(con, memory_id)
    if current is None:
        raise MemoryError(f"记忆不存在：id={memory_id}")

    fields: dict[str, object] = {}
    if title is not None:
        fields["title"] = validate_title(title)
    if content is not None:
        fields["content"] = validate_content(content)
    if tags is not None:
        fields["tags"] = clean_text(tags, limit=TAGS_MAX)
    if enabled is not None:
        fields["enabled"] = int(enabled)
    if pin_single is not None:
        fields["pin_single"] = int(pin_single)
    if sort_order is not None:
        fields["sort_order"] = int(sort_order)

    next_scope = validate_scope(scope) if scope is not None else current.scope
    next_course = current.course_id
    if course_id_provided or scope is not None:
        next_course = _validate_course(con, course_id)
    if next_scope == "global":
        next_course = None
    elif next_course is None:
        raise MemoryError("scope='course' 的记忆必须指定课程。")
    if scope is not None or course_id_provided:
        fields["scope"] = next_scope
        fields["course_id"] = next_course

    if not fields:
        return current

    fields["updated_at"] = now_utc_iso()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with transaction(con):
        con.execute(
            f"UPDATE memories SET {assignments} WHERE id = ?",
            list(fields.values()) + [memory_id],
        )
    updated = get_memory(con, memory_id)
    assert updated is not None
    log.info("memory.updated %s", kv(id=memory_id, fields=",".join(fields)))
    return updated


def delete_memory(con: sqlite3.Connection, memory_id: int) -> bool:
    with transaction(con):
        cursor = con.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    deleted = cursor.rowcount > 0
    log.info("memory.deleted %s", kv(id=memory_id, deleted=deleted))
    return deleted


def clear_pin(con: sqlite3.Connection, *, except_id: int | None = None) -> int:
    """清除"单条注入"标记。

    单条注入是"这一次"的意图，不是持久偏好：识别完成后必须清掉，
    否则下次识别又会莫名其妙带上它，用户会以为开关失灵了。
    """
    with transaction(con):
        if except_id is None:
            cursor = con.execute("UPDATE memories SET pin_single = 0 WHERE pin_single = 1")
        else:
            cursor = con.execute(
                "UPDATE memories SET pin_single = 0 WHERE pin_single = 1 AND id != ?",
                (except_id,),
            )
    if cursor.rowcount:
        log.info("memory.pins_cleared %s", kv(count=cursor.rowcount))
    return cursor.rowcount


def counts(con: sqlite3.Connection) -> dict[str, int]:
    row = con.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS enabled,"
        " SUM(CASE WHEN pin_single = 1 THEN 1 ELSE 0 END) AS pinned"
        " FROM memories"
    ).fetchone()
    return {
        "total": int(row["total"] or 0),
        "enabled": int(row["enabled"] or 0),
        "pinned": int(row["pinned"] or 0),
    }
