"""控制台接口：LLM 配置（热加载）、API Key 管理、运行状态。

三条要点：

1. **API Key 明文只在创建时返回一次**。库里只有 sha256；列表接口只给前缀。
2. **改 ``[llm]`` 立即生效**（下一次识别就用新配置），因为配置是每次请求现读的。
   改 ``[server]``/``[auth]`` 需要重启，接口会明确说出来——静默不生效比报错更糟。
3. **状态接口是自己写的**，没有引 psutil 之类的依赖：服务器只有 1.6G 内存，
   能用 stdlib 读的就不引进新包。
"""

from __future__ import annotations

import os
import shutil
import sqlite3
import time
from datetime import timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException

from app import __version__
from app.config import Config, load_config, update_config_section
from app.deps import ReadAuth, WriteAuth, get_config, get_db
from app.logging import get_logger, kv
from app.schemas import (
    ApiKeyCreated,
    ApiKeyOut,
    CreateApiKeyRequest,
    LlmSettingsIn,
    LlmSettingsOut,
    OkBody,
    SettingsOut,
    StatusOut,
    TermSettingsIn,
)
from app.security import generate_api_key
from app.services import drafts as drafts_service
from app.services import memory as memory_service
from app.services import tasks as tasks_service
from app.services import timetable as timetable_service
from app.timeutil import now_utc, now_utc_iso, parse_utc, term_week

log = get_logger("api.settings")

router = APIRouter()

#: 进程启动时刻，用于算 uptime。模块级常量在这里是合适的：
#: uptime 的语义就是"这个进程活了多久"。
_STARTED_AT = time.monotonic()

#: 需要重启才能生效的配置段。接口会把这件事说出来。
RESTART_REQUIRED = ("server", "auth", "tls")

#: LLM 配置里可以热加载的键。`api_key` 不在这个列表里是因为它需要单独处理
#: "不传表示不改"的语义。
_HOT_KEYS = (
    "provider",
    "base_url",
    "model",
    "temperature",
    "timeout_seconds",
    "max_tokens",
    "thinking",
    "reasoning_effort",
    "retry_count",
    "retry_backoff_seconds",
    "system_prompt",
)


@router.get("/settings", response_model=SettingsOut, summary="读取控制台设置")
async def get_settings(
    principal: ReadAuth,
    cfg: Annotated[Config, Depends(get_config)],
) -> SettingsOut:
    return _settings_payload(cfg)


def _settings_payload(cfg: Config) -> SettingsOut:
    return SettingsOut(
        llm=LlmSettingsOut(
            provider=cfg.llm.provider,  # type: ignore[arg-type]
            base_url=cfg.llm.base_url,
            model=cfg.llm.model,
            has_api_key=bool(cfg.llm.api_key),
            temperature=cfg.llm.temperature,
            timeout_seconds=cfg.llm.timeout_seconds,
            max_tokens=cfg.llm.max_tokens,
            thinking=cfg.llm.thinking,
            reasoning_effort=cfg.llm.reasoning_effort,
            retry_count=cfg.llm.retry_count,
            retry_backoff_seconds=cfg.llm.retry_backoff_seconds,
            system_prompt=cfg.llm.system_prompt,
        ),
        term={
            "start_date": cfg.term.start_date,
            "total_weeks": cfg.term.total_weeks,
            "current_week": _current_week(cfg),
        },
        server={
            "public_url": cfg.server.public_url,
            "timezone": cfg.server.timezone,
            "listen_host": cfg.server.listen_host,
            "listen_port": cfg.server.listen_port,
            "data_dir": str(cfg.data_dir),
            # 不返回 config.toml 的完整路径：它出现在错误信息里就够了，
            # 从接口暴露路径没有收益
        },
    )


def _reload(cfg: Config) -> Config:
    """写回配置后重新读一遍，用于构造响应。

    不这样做的话，``put_settings`` 会拿"写之前"的内存配置去渲染响应，
    于是用户看到"我改了但没有生效"——而他刷新一下又发现其实生效了。
    这种"看起来没生效"的假象会直接导致重复操作。
    """
    try:
        return load_config(cfg.path)
    except Exception:  # noqa: BLE001 - 读不回来就退回旧配置，不把写成功变成 500
        log.warning("settings.reload_failed %s", kv(path=str(cfg.path)))
        return cfg


@router.put("/settings", response_model=SettingsOut, summary="保存 LLM / 学期配置")
async def put_settings(
    payload: LlmSettingsIn,
    principal: WriteAuth,
    cfg: Annotated[Config, Depends(get_config)],
) -> SettingsOut:
    llm_values: dict[str, Any] = {}
    for key in _HOT_KEYS:
        value = getattr(payload, key)
        if value is not None:
            llm_values[key] = value

    # api_key 的语义：不传 = 不改；传空串 = 清空。
    # 用 None 表示"不改"而不是"清空"，是因为控制台表单里那个输入框
    # 平时是空的（我们不回显密钥），如果空值等于清空，用户每次改别的设置
    # 都会顺手把 Key 清掉。
    if payload.api_key is not None:
        llm_values["api_key"] = payload.api_key.strip()

    if llm_values:
        try:
            update_config_section(cfg.path, "llm", llm_values)
        except OSError as exc:
            raise HTTPException(
                status_code=500, detail=f"无法写入配置文件：{exc}"
            ) from exc
        log.info("settings.llm_updated %s", kv(keys=",".join(sorted(llm_values))))

    return _settings_payload(_reload(cfg))


@router.put("/settings/term", response_model=SettingsOut, summary="保存学期设置")
async def put_term(
    payload: TermSettingsIn,
    principal: WriteAuth,
    cfg: Annotated[Config, Depends(get_config)],
) -> SettingsOut:
    values: dict[str, Any] = {}
    if payload.start_date is not None:
        try:
            parse_utc(payload.start_date)
        except Exception as exc:
            raise HTTPException(
                status_code=422,
                detail=f"学期起始日无法解析：{payload.start_date!r}，请用 YYYY-MM-DD",
            ) from exc
        values["start_date"] = payload.start_date
    if payload.total_weeks is not None:
        values["total_weeks"] = payload.total_weeks

    if values:
        update_config_section(cfg.path, "term", values)
        log.info("settings.term_updated %s", kv(**values))

    return _settings_payload(_reload(cfg))


# --------------------------------------------------------------------------- #
# API Key
# --------------------------------------------------------------------------- #
@router.get("/keys", response_model=list[ApiKeyOut], summary="列出 API Key（不含明文）")
async def list_keys(
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> list[ApiKeyOut]:
    rows = con.execute(
        "SELECT id, name, scope, prefix, created_at, last_used_at, revoked_at"
        " FROM api_keys ORDER BY id"
    ).fetchall()
    return [ApiKeyOut(**dict(row)) for row in rows]


@router.post("/keys", response_model=ApiKeyCreated, summary="创建 API Key（明文只返回一次）")
async def create_key(
    payload: CreateApiKeyRequest,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> ApiKeyCreated:
    plain, digest, prefix = generate_api_key()
    cursor = con.execute(
        "INSERT INTO api_keys (name, key_hash, scope, prefix, created_at) VALUES (?, ?, ?, ?, ?)",
        (payload.name, digest, "read" if payload.read_only else "write", prefix, now_utc_iso()),
    )
    log.info("api.key_created %s", kv(id=cursor.lastrowid, scope="read" if payload.read_only else "write"))
    return ApiKeyCreated(
        id=int(cursor.lastrowid),
        name=payload.name,
        scope="read" if payload.read_only else "write",
        api_key=plain,
    )


@router.delete("/keys/{key_id}", response_model=OkBody, summary="吊销 API Key")
async def revoke_key(
    key_id: int,
    principal: WriteAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
) -> OkBody:
    cursor = con.execute(
        "UPDATE api_keys SET revoked_at = ? WHERE id = ? AND revoked_at IS NULL",
        (now_utc_iso(), key_id),
    )
    if cursor.rowcount == 0:
        raise HTTPException(status_code=404, detail=f"没有找到未吊销的 Key：id={key_id}")
    log.info("api.key_revoked %s", kv(id=key_id))
    return OkBody(ok=True, message="Key 已吊销，立即失效。")


# --------------------------------------------------------------------------- #
# 运行状态
# --------------------------------------------------------------------------- #
@router.get("/status", response_model=StatusOut, summary="运行状态")
async def status(
    principal: ReadAuth,
    con: Annotated[sqlite3.Connection, Depends(get_db)],
    cfg: Annotated[Config, Depends(get_config)],
) -> StatusOut:
    warnings = _warnings(cfg)
    return StatusOut(
        version=__version__,
        uptime_seconds=round(time.monotonic() - _STARTED_AT, 1),
        server_time=now_utc_iso(),
        week=_current_week(cfg),
        counts={
            "tasks": tasks_service.counts(con),
            "drafts": drafts_service.counts(con),
            "memories": memory_service.counts(con),
            "courses": len(timetable_service.list_courses(con)),
        },
        storage=_storage(cfg),
        llm={
            "provider": cfg.llm.provider,
            "model": cfg.llm.model,
            "base_url": cfg.llm.base_url,
            "thinking": cfg.llm.thinking,
            "has_api_key": bool(cfg.llm.api_key),
        },
        timezone=cfg.server.timezone,
        warnings=warnings,
    )


def _current_week(cfg: Config) -> int:
    try:
        start = parse_utc(cfg.term.start_date).date()
    except Exception:
        return 0
    from app.timeutil import tz

    return term_week(now_utc().astimezone(tz(cfg.server.timezone)).date(), start)


def _storage(cfg: Config) -> dict[str, Any]:
    """数据目录占用。自己走目录，不引 psutil：服务器只有 1.6G 内存，
    能用 stdlib 读的东西就不值得新增一个依赖。"""
    db_bytes = cfg.db_path.stat().st_size if cfg.db_path.exists() else 0
    uploads = 0
    upload_bytes = 0
    if cfg.uploads_dir.exists():
        for size in _iter_file_sizes(cfg.uploads_dir):
            uploads += 1
            upload_bytes += size

    probe = cfg.data_dir if cfg.data_dir.exists() else cfg.path.parent
    disk = shutil.disk_usage(str(probe))
    return {
        "data_dir": str(cfg.data_dir),
        "db_bytes": db_bytes,
        "upload_files": uploads,
        "upload_bytes": upload_bytes,
        "disk_total_bytes": disk.total,
        "disk_free_bytes": disk.free,
        "disk_used_percent": round((disk.used / disk.total) * 100, 1) if disk.total else 0.0,
    }


def _iter_file_sizes(root):
    """逐个 yield 文件的字节数。

    自己用 ``os.scandir`` 走而不是 ``os.walk`` + ``stat``：scandir 的
    DirEntry 自带缓存的 stat 结果，在图片目录（上万个小文件）上差别明显。
    容错：单个目录/文件读不到就跳过，不让统计接口整个失败。
    """
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    yield entry.stat(follow_symlinks=False).st_size
            except OSError:
                continue


def _warnings(cfg: Config) -> list[str]:
    """把"能跑但有问题"的状态说出来。

    这些正是用户不会主动检查、但会在出问题后回头怀疑的东西，
    所以在状态页上直接列出来比等他来问有用。
    """
    messages: list[str] = []
    if cfg.llm.provider != "mock" and not cfg.llm.api_key:
        messages.append("LLM 未配置 API Key，识别功能不可用。")
    try:
        start = parse_utc(cfg.term.start_date).date()
        week = _current_week(cfg)
        if week > cfg.term.total_weeks:
            messages.append(
                f"今天是第 {week} 周，已超过配置的总周数 {cfg.term.total_weeks}。"
                "请在设置里更新学期起始日或总周数，否则提示词里的课表上下文会不准。"
            )
        elif week <= 0:
            messages.append(
                f"按当前学期起始日（{start.isoformat()}）计算，学期尚未开始。"
                "如果这不符实，请更新学期起始日。"
            )
    except Exception:
        messages.append(f"学期起始日无法解析：{cfg.term.start_date!r}，请改成 YYYY-MM-DD。")

    if not cfg.uploads_dir.exists():
        messages.append("上传目录不存在（还没有上传过图片）。")
    return messages
