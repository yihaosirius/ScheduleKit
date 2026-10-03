"""任务接口。

分层：本文件只做鉴权、参数转换、把 :class:`TaskError` 映射成 4xx。
所有业务规则（二选一、排序、幂等）都在 :mod:`app.services.tasks`。
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, status

from app.config import Config
from app.deps import ReadAuth, WriteAuth, get_config, get_db
from app.logging import get_logger, kv
from app.schemas import (
    TaskCreateRequest,
    TaskDeleteResult,
    TaskListOut,
    TaskOut,
    TaskUpdateRequest,
)
from app.services import tasks as tasks_service
from app.services.tasks import Task, TaskError
from app.timeutil import now_utc_iso

log = get_logger("api.tasks")

router = APIRouter()

#: 视图 → 中文名。给错误信息与 UI 用。
VIEW_LABELS = {
    "ordered": "有序表（有截止时间）",
    "unordered": "无序表（有优先级）",
    "done": "已完成",
    "all": "全部",
}


def task_to_out(task: Task, cfg: Config) -> TaskOut:
    payload = task.to_dict(timezone_name=cfg.server.timezone)
    return TaskOut(**payload)


def _handle(exc: TaskError) -> HTTPException:
    """业务异常 → 422。

    用 422 而不是 400：错误是"提交的值不满足业务规则"，
    422 是 FastAPI 校验失败时用的同一个状态码，前端只需要处理一种。
    """
    return HTTPException(status_code=422, detail=str(exc))


@router.get("", response_model=TaskListOut, summary="按视图列出任务")
async def list_tasks(
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
    view: Annotated[str, Query(description="ordered | unordered | done | all")] = "ordered",
    limit: Annotated[int | None, Query(ge=1, le=500)] = None,
) -> TaskListOut:
    try:
        items = tasks_service.list_tasks(con, view=view, limit=limit)
    except TaskError as exc:
        raise _handle(exc) from exc
    return TaskListOut(
        view=view,
        items=[task_to_out(task, cfg) for task in items],
        counts=tasks_service.counts(con),
        server_time=now_utc_iso(),
    )


@router.get("/{task_id}", response_model=TaskOut, summary="读取单条任务")
async def get_task(
    task_id: int,
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> TaskOut:
    task = tasks_service.get_task(con, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail=f"任务不存在：id={task_id}")
    return task_to_out(task, cfg)


@router.post(
    "", response_model=TaskOut, status_code=status.HTTP_201_CREATED, summary="新建任务"
)
async def create_task(
    payload: TaskCreateRequest,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> TaskOut:
    try:
        task = tasks_service.create_task(
            con,
            title=payload.title,
            category=payload.category,
            due_at=payload.due_at,
            priority=payload.priority,
            notes=payload.notes,
            source="shortcut" if principal.kind == "apikey" else "web",
            client_uuid=payload.client_uuid,
        )
    except TaskError as exc:
        raise _handle(exc) from exc
    return task_to_out(task, cfg)


@router.patch("/{task_id}", response_model=TaskOut, summary="局部更新任务")
async def update_task(
    task_id: int,
    payload: TaskUpdateRequest,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> TaskOut:
    try:
        task = tasks_service.update_task(
            con,
            task_id,
            title=payload.title,
            category=payload.category,
            notes=payload.notes,
            status=payload.status,
            due_at=payload.due_at,
            due_at_provided=payload.due_at_set,
            priority=payload.priority,
            priority_provided=payload.priority_set,
        )
    except TaskError as exc:
        # "任务不存在"是 404，其余是 422。靠文案区分不够稳，
        # 这里显式再查一次，避免把 404 报成 422。
        if tasks_service.get_task(con, task_id) is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：id={task_id}") from exc
        raise _handle(exc) from exc
    return task_to_out(task, cfg)


@router.post("/{task_id}/toggle", response_model=TaskOut, summary="切换完成状态")
async def toggle_task(
    task_id: int,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> TaskOut:
    """单独一个端点，因为"勾选完成"是使用频率最高的写操作。

    让客户端自己算"下一个状态"也可以，但那要求客户端知道"已完成"的语义，
    而这是服务端的事（同时要写 completed_at）。
    """
    try:
        task = tasks_service.toggle_done(con, task_id)
    except TaskError as exc:
        if tasks_service.get_task(con, task_id) is None:
            raise HTTPException(status_code=404, detail=f"任务不存在：id={task_id}") from exc
        raise _handle(exc) from exc
    return task_to_out(task, cfg)


@router.delete("/{task_id}", response_model=TaskDeleteResult, summary="删除任务")
async def delete_task(
    task_id: int,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> TaskDeleteResult:
    deleted = tasks_service.delete_task(con, task_id)
    if not deleted:
        raise HTTPException(status_code=404, detail=f"任务不存在：id={task_id}")
    return TaskDeleteResult(ok=True, id=task_id)
