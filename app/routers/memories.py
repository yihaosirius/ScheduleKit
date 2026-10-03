"""记忆接口。

记忆是**给模型看的参考素材**，不是任务。所以这里的接口刻意不提供"转为任务"
这类快捷操作——那会强化"记忆就是待办"的误解，而这正是提示词里要费力纠正的事。
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.deps import ReadAuth, WriteAuth, get_db
from app.logging import get_logger
from app.schemas import (
    MemoryCreateRequest,
    MemoryListOut,
    MemoryOut,
    MemoryUpdateRequest,
    OkBody,
)
from app.services import memory as memory_service
from app.services.memory import Memory, MemoryError

log = get_logger("api.memories")

router = APIRouter()


def memory_to_out(memory: Memory) -> MemoryOut:
    payload = memory.to_dict()
    return MemoryOut(**payload)


def _handle(exc: MemoryError) -> HTTPException:
    text = str(exc)
    if "不存在" in text:
        return HTTPException(status_code=404, detail=text)
    return HTTPException(status_code=422, detail=text)


@router.get("", response_model=MemoryListOut, summary="列出记忆条目")
async def list_memories(
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    enabled_only: bool = False,
) -> MemoryListOut:
    items = memory_service.list_memories(con, enabled_only=enabled_only)
    return MemoryListOut(
        items=[memory_to_out(memory) for memory in items],
        counts=memory_service.counts(con),
    )


@router.get("/selected", summary="查看下一次识别会注入哪些记忆")
async def selected(
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> dict[str, object]:
    """排查"为什么这条记忆没进去"的直接入口。

    没有它，用户只能看到"识别时没参考这条"，然后怀疑是模型的问题。
    """
    from app.services import timetable as timetable_service
    from app.timeutil import now_utc

    courses = timetable_service.list_courses(con)
    course_ids = {course.id for course in courses}
    memories = memory_service.select_for_injection(con, course_ids_in_context=course_ids)
    return {
        "memory_ids": [m.id for m in memories],
        "items": [memory_to_out(m).model_dump() for m in memories],
        "note": "scope=course 的条目只在对应课程出现在上下文里时注入；"
                "pin_single 的条目用过一次后会自动清除。",
        "server_time": now_utc().isoformat(),
    }


@router.post("/clear-pins", response_model=OkBody, summary="清除全部单条注入标记")
async def clear_pins(
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> OkBody:
    """必须定义在 ``/{memory_id}`` 之前，否则 "clear-pins" 会被当成 memory_id 匹配掉。"""
    count = memory_service.clear_pin(con)
    return OkBody(ok=True, message=f"已清除 {count} 条单条注入标记。")


@router.get("/{memory_id}", response_model=MemoryOut, summary="读取单条记忆")
async def get_memory(
    memory_id: int,
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> MemoryOut:
    memory = memory_service.get_memory(con, memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail=f"记忆不存在：id={memory_id}")
    return memory_to_out(memory)


@router.post(
    "", response_model=MemoryOut, status_code=status.HTTP_201_CREATED, summary="新建记忆条目"
)
async def create_memory(
    payload: MemoryCreateRequest,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> MemoryOut:
    try:
        memory = memory_service.create_memory(
            con,
            title=payload.title,
            content=payload.content,
            tags=payload.tags,
            scope=payload.scope,
            course_id=payload.course_id,
            enabled=payload.enabled,
            pin_single=payload.pin_single,
            sort_order=payload.sort_order,
        )
    except MemoryError as exc:
        raise _handle(exc) from exc
    return memory_to_out(memory)


@router.patch("/{memory_id}", response_model=MemoryOut, summary="修改记忆条目")
async def update_memory(
    memory_id: int,
    payload: MemoryUpdateRequest,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> MemoryOut:
    try:
        memory = memory_service.update_memory(
            con,
            memory_id,
            title=payload.title,
            content=payload.content,
            tags=payload.tags,
            scope=payload.scope,
            course_id=payload.course_id,
            course_id_provided=payload.course_id_set,
            enabled=payload.enabled,
            pin_single=payload.pin_single,
            sort_order=payload.sort_order,
        )
    except MemoryError as exc:
        raise _handle(exc) from exc
    return memory_to_out(memory)


@router.post("/{memory_id}/pin", response_model=MemoryOut, summary="标记单条注入（下次识别带上它）")
async def pin_memory(
    memory_id: int,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> MemoryOut:
    """单条注入是"下一次识别"的意图，用掉后自动清除（见 app/services/ingest.py）。"""
    try:
        memory = memory_service.update_memory(con, memory_id, pin_single=True)
    except MemoryError as exc:
        raise _handle(exc) from exc
    return memory_to_out(memory)


@router.delete("/{memory_id}/pin", response_model=MemoryOut, summary="取消单条注入")
async def unpin_memory(
    memory_id: int,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> MemoryOut:
    try:
        memory = memory_service.update_memory(con, memory_id, pin_single=False)
    except MemoryError as exc:
        raise _handle(exc) from exc
    return memory_to_out(memory)


@router.delete("/{memory_id}", response_model=OkBody, summary="删除记忆条目")
async def delete_memory(
    memory_id: int,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> OkBody:
    deleted = memory_service.delete_memory(con, memory_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"记忆不存在：id={memory_id}")
    return OkBody(ok=True, message="记忆条目已删除。")
