"""草稿接口：确认 / 丢弃 / 暂存 / 重试 / 原图。

``confirm`` 是整条链路唯一的入库点，也是**唯一会写 ``items`` 表的录入路径**。

几个刻意的选择：

* **原图读接口允许 API Key**，但**不允许未鉴权访问**。图片里可能有课程群名、
  学号这类信息，匿名可读是明确的风险。浏览器用会话 Cookie 即可；
  Scriptable 用 Bearer Key。
* **``discard`` 保留记录**（``status='discarded'``）而不是删行：
  "用户丢掉了多少识别结果"是判断 prompt 质量的直接指标。
* **``purge`` 需要网页会话**（``require_session``）：一次性删掉所有待确认草稿
  是不可逆的批量操作，API Key 不该能做。
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import FileResponse

from app.config import Config
from app.deps import ReadAuth, SessionOnly, WriteAuth, get_config, get_db
from app.logging import get_logger, kv
from app.paths import resolve_upload_path
from app.schemas import (
    ConfirmRequest,
    ConfirmResultOut,
    DraftListOut,
    DraftOut,
    OkBody,
    ReplaceItemsRequest,
)
from app.services import drafts as drafts_service
from app.services.drafts import DraftError
from app.timeutil import now_utc_iso

log = get_logger("api.drafts")

router = APIRouter()


def draft_to_out(draft: drafts_service.Draft) -> DraftOut:
    return DraftOut(**draft.to_dict())


def _handle(exc: DraftError) -> HTTPException:
    """草稿业务异常 → 4xx。

    "草稿不存在/已确认"是**状态冲突**，用 409 而不是 422：
    前端据此可以给出"这条已经确认过了，去任务列表看"这样的具体提示，
    而不是笼统的"参数不合法"。
    """
    text = str(exc)
    if "不存在" in text:
        return HTTPException(status_code=404, detail=text)
    return HTTPException(status_code=409, detail=text)


@router.get("", response_model=DraftListOut, summary="列出草稿（草稿箱）")
async def list_drafts(
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    status_filter: str = "pending",
    limit: int = 100,
) -> DraftListOut:
    try:
        items = drafts_service.list_drafts(
            con, status=None if status_filter == "all" else status_filter, limit=limit
        )
    except DraftError as exc:
        raise _handle(exc) from exc
    return DraftListOut(
        items=[draft_to_out(draft) for draft in items],
        counts=drafts_service.counts(con),
        server_time=now_utc_iso(),
    )


@router.get("/{draft_id}", response_model=DraftOut, summary="读取草稿")
async def get_draft(
    draft_id: int,
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> DraftOut:
    draft = drafts_service.get_draft(con, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"草稿不存在：id={draft_id}")
    return draft_to_out(draft)


@router.get("/{draft_id}/image", summary="读取草稿原图", response_class=FileResponse)
async def get_draft_image(
    draft_id: int,
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> FileResponse:
    draft = drafts_service.get_draft(con, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"草稿不存在：id={draft_id}")
    if not draft.image_path:
        raise HTTPException(status_code=404, detail="该草稿没有图片。")

    try:
        path = resolve_upload_path(cfg, draft.image_path)
    except ValueError as exc:
        # 库里的路径越出 data_dir：要么有人手改了库，要么是历史脏数据。
        # 这种情况必须拦住并留痕——它意味着一个任意文件读取的可能性。
        log.error("drafts.image_path_escape %s", kv(id=draft_id, path=draft.image_path))
        raise HTTPException(status_code=500, detail="图片路径异常，已记录日志。") from exc

    if not path.exists():
        log.warning("drafts.image_missing %s", kv(id=draft_id, path=str(path)))
        raise HTTPException(
            status_code=404, detail="图片文件已不存在（可能已被清理）。"
        )

    return FileResponse(
        path,
        media_type=draft.image_mime or "application/octet-stream",
        # 内容不变（文件名就是内容哈希），可以放心长缓存；private 表示仅本机缓存
        headers={"Cache-Control": "private, max-age=604800"},
    )


@router.post("/{draft_id}/confirm", response_model=ConfirmResultOut, summary="确认草稿并入库")
async def confirm_draft(
    draft_id: int,
    payload: ConfirmRequest,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> ConfirmResultOut:
    items = None
    if payload.items is not None:
        items = [item.model_dump() for item in payload.items]
    try:
        draft, created = drafts_service.confirm_draft(con, draft_id, items=items)
    except DraftError as exc:
        raise _handle(exc) from exc

    from app.routers.tasks import task_to_out

    return ConfirmResultOut(
        draft=draft_to_out(draft),
        created=[task_to_out(task, cfg) for task in created],
        message=f"已入库 {len(created)} 条任务。",
    )


@router.put("/{draft_id}/items", response_model=DraftOut, summary="暂存二次修改（不入库）")
async def replace_items(
    draft_id: int,
    payload: ReplaceItemsRequest,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> DraftOut:
    """给确认页用：用户在手机上改了一半被打断，回来不该从头再填。"""
    try:
        draft = drafts_service.replace_items(
            con, draft_id, [item.model_dump() for item in payload.items]
        )
    except DraftError as exc:
        raise _handle(exc) from exc
    return draft_to_out(draft)


@router.post("/{draft_id}/discard", response_model=OkBody, summary="丢弃草稿")
async def discard_draft(
    draft_id: int,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> OkBody:
    try:
        drafts_service.discard_draft(con, draft_id)
    except DraftError as exc:
        raise _handle(exc) from exc
    return OkBody(ok=True, message="草稿已丢弃（保留记录用于统计，不会出现在草稿箱）。")


@router.post("/{draft_id}/retry", response_model=DraftOut, summary="用原图重跑一次识别")
async def retry_draft(
    draft_id: int,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> DraftOut:
    """重试识别，**原地更新同一条草稿**而不是新建。

    这是刻意的：用户的意图是"再来一次"，不是"我又传了一遍"。
    新建草稿会让草稿箱里堆出同一张图的多个副本，清理起来很烦。
    """
    draft = drafts_service.get_pending(con, draft_id)
    if not draft.image_path and not draft.text_input:
        raise HTTPException(status_code=409, detail="该草稿既没有图片也没有文字，无法重试。")

    raw = b""
    mime = draft.image_mime or "image/jpeg"
    if draft.image_path:
        try:
            path = resolve_upload_path(cfg, draft.image_path)
        except ValueError as exc:
            raise HTTPException(status_code=500, detail="图片路径异常") from exc
        if not path.exists():
            raise HTTPException(status_code=409, detail="原图已被清理，无法重试识别。")
        raw = path.read_bytes()

    from app.services import ingest as ingest_service
    from app.services.ingest import IngestError, Intake

    intake = Intake(
        text=draft.text_input,
        image_path=draft.image_path,
        image_mime=mime,
        image_bytes=raw or None,
        image_size=len(raw) or None,
        image_sha256=draft.image_sha256,
        channel=draft.channel,
    )
    try:
        result = await ingest_service.run_intake(cfg, con, intake)
    except IngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    # 新草稿替换旧的：清掉旧行，避免草稿箱里出现两张一样的图
    drafts_service.discard_draft(con, draft_id)
    log.info("drafts.retried %s", kv(old=draft_id, new=result.draft.id))
    return draft_to_out(result.draft)


@router.post("/purge", response_model=OkBody, summary="清空草稿箱（仅网页会话）")
async def purge_drafts(
    principal: SessionOnly,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> OkBody:
    purged = drafts_service.purge_all_pending(con)
    from app.housekeeping import run_once

    stats = run_once(cfg)
    removed = stats.get("images_removed", 0)
    return OkBody(
        ok=True,
        message=(
            f"已清空 {purged.count} 条待确认草稿，"
            f"顺带清理了 {removed} 个无引用的图片文件。"
        ),
    )


@router.get("/{draft_id}/raw", summary="查看草稿的原始模型输出（排查用）")
async def raw_output(
    draft_id: int,
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> dict[str, object]:
    """排查"识别结果不对"时最有用的一条：模型到底返回了什么。

    刻意做成独立接口而不是塞进 DraftOut：raw 可能有几十 KB，
    列表里带上它会让草稿箱接口变得很重。
    """
    draft = drafts_service.get_draft(con, draft_id)
    if draft is None:
        raise HTTPException(status_code=404, detail=f"草稿不存在：id={draft_id}")
    row = con.execute(
        "SELECT raw_output, llm_path, fallback_note, overrides FROM ingest_drafts WHERE id = ?",
        (draft_id,),
    ).fetchone()
    return {
        "raw_output": row["raw_output"],
        "llm_path": row["llm_path"],
        "fallback_note": row["fallback_note"],
        "overrides": row["overrides"],
    }
