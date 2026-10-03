"""录入接口：上传图片 / 文字 → 生成待确认草稿。

**这是整条链路的入口，也是唯一会被 iPhone 快捷指令调用的接口**，
所以它的错误信息要为"在手机小屏幕上阅读"优化：短、说清原因、给出下一步。

三条实现要点：

1. 图片先过 :func:`app.media.store_image` 真解一次，再交给 LLM。
   不信 multipart 里的 content-type —— iPhone 会把 HEIC 标成 image/jpeg。
2. ``client_uuid`` 只在**直接建任务**时用于幂等；录入接口的幂等靠"草稿"本身
   （重复上传会生成两条草稿，用户能看见并丢弃），因为识别结果不确定，
   用 uuid 去重会掩盖"我传了两张不同的图"。
3. 渠道判定：带 API Key 的算 shortcut，网页会话算 web。这个区分会进状态监测。
"""

from __future__ import annotations

import sqlite3
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile, status

from app.config import Config
from app.deps import Principal, get_config, get_db, require_write
from app.logging import get_logger, kv
from app.media import ImageRejected, store_image
from app.schemas import DraftOut, IngestResultOut
from app.services import drafts as drafts_service
from app.services import ingest as ingest_service
from app.services.ingest import IngestError, Intake

log = get_logger("api.ingest")

router = APIRouter()

#: 文字上限。识别的是"一件事的截图或几句话"，不是长文档；
#: 超出这个量级通常意味着用户传错了东西。
TEXT_MAX = 4000


def draft_to_out(draft: drafts_service.Draft) -> DraftOut:
    return DraftOut(**draft.to_dict())


@router.post(
    "",
    response_model=IngestResultOut,
    status_code=status.HTTP_201_CREATED,
    summary="上传图片或文本，生成待确认草稿",
)
async def ingest(
    principal: Annotated[Principal, Depends(require_write)],
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
    image: Annotated[UploadFile | None, File(description="图片；与 text 至少给一个")] = None,
    text: Annotated[str, Form(description="文字备注，可选")] = "",
) -> IngestResultOut:
    if text and len(text) > TEXT_MAX:
        raise HTTPException(
            status_code=413,
            detail=f"文字过长（{len(text)} 字，上限 {TEXT_MAX} 字）。请只发需要识别的那段。",
        )

    raw = b""
    if image is not None:
        raw = await image.read()
        if not raw:
            raise HTTPException(status_code=422, detail="上传的图片是空的。")

    stored = None
    if raw:
        try:
            stored = store_image(cfg, raw)
        except ImageRejected as exc:
            # 图片问题是用户能自己修的（重新拍、转格式），所以文案要具体
            log.warning("ingest.image_rejected %s", kv(error=str(exc), bytes=len(raw)))
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    if not text.strip() and stored is None:
        raise HTTPException(
            status_code=422,
            detail="请提供图片或文字中的至少一项。",
        )

    intake = Intake(
        text=text,
        image_path=stored.relative_path if stored else None,
        image_mime=stored.mime if stored else None,
        image_bytes=raw or None,
        image_size=stored.size_bytes if stored else None,
        image_sha256=stored.sha256 if stored else None,
        channel="shortcut" if principal.kind == "apikey" else "web",
    )

    try:
        result = await ingest_service.run_intake(cfg, con, intake)
    except IngestError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    draft = result.draft
    confirm_url = f"{cfg.server.public_url.rstrip('/')}/#/drafts/{draft.id}"

    if result.items:
        message = f"识别到 {len(result.items)} 条事项，请在确认页核对后入库。"
    elif draft.fallback_note and "识别失败" in draft.fallback_note:
        message = "识别失败，图片与文字已保存在草稿箱，可稍后重试。"
    else:
        message = "没有识别到可执行的事项。图片与文字已保留在草稿箱。"

    log.info(
        "ingest.done %s",
        kv(
            draft=draft.id,
            channel=intake.channel,
            items=len(result.items),
            llm_path=draft.llm_path,
            context_chars=result.context_chars,
        ),
    )
    return IngestResultOut(draft=draft_to_out(draft), confirm_url=confirm_url, message=message)


@router.get("/context", summary="查看当前会被注入的上下文（排查用）")
async def current_context(
    principal: Annotated[Principal, Depends(require_write)],
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> dict[str, object]:
    """识别结果不对时的第一排查入口：上下文对不对。

    没有这个接口，用户只能看着"识别错了"猜原因：是没读到图，还是读到了但
    上下文里的日期算错了？这两件事的修法完全不同。
    """
    text = ingest_service.preview_context(cfg, con)
    return {"context": text, "chars": len(text)}
