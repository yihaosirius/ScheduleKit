"""后台清理。

只做两件事，都是"不做会慢慢烂掉"的那类：

1. 删除到期的 ``pending`` 草稿（``[ingest].confirm_ttl_hours``）。
2. 删除没有任何草稿引用的上传图片（``[backup].upload_retention_days`` 之前的不动，
   留一点缓冲——用户可能刚删了草稿又想从截图里找回来）。

**不做**：删除 ``confirmed`` / ``discarded`` 草稿记录。
它们是"这条任务从哪来""识别质量如何"的唯一痕迹，代价只是一点行数。
"""

from __future__ import annotations

import os
from datetime import timedelta

from app.config import Config
from app.db import connect
from app.logging import get_logger, kv
from app.services import drafts as drafts_service
from app.timeutil import now_utc

log = get_logger("housekeeping")


def run_once(cfg: Config) -> dict[str, int]:
    """跑一轮清理。返回统计，便于 /api/status 展示与测试断言。

    没有 dry_run 开关：清理的两件事都是"超时后丢弃中间态"，
    对它们的唯一用途就是真的执行；提供一个不执行的模式只会让人误以为
    "预览过了就安全"，而真正的风险（删掉还在引用的图片）在预览里也看不出来。
    """
    con = connect(cfg.db_path)
    stats = {"drafts_purged": 0, "images_removed": 0, "images_kept": 0, "bytes_reclaimed": 0}
    try:
        purged = drafts_service.purge_expired(con)
        # 用 PurgeResult.count 而不是 image_paths 的长度：一条没有图片的草稿
        # 也会被删除，按图片数计数会让"清理了几条"这个数字长期显示 0。
        stats["drafts_purged"] = purged.count

        referenced = drafts_service.referenced_image_paths(con)
        removed, kept, reclaimed = _sweep_orphan_images(cfg, referenced)
        stats["images_removed"] = removed
        stats["images_kept"] = kept
        stats["bytes_reclaimed"] = reclaimed
    finally:
        con.close()

    if stats["drafts_purged"] or stats["images_removed"]:
        log.info("housekeeping.done %s", kv(**stats))
    return stats


def _sweep_orphan_images(cfg: Config, referenced: set[str]) -> tuple[int, int, int]:
    """删掉没人引用的图片文件。

    ``referenced`` 是**相对 data_dir 的 posix 路径**（与库里一致），
    这里把磁盘上的文件换算成同样的相对路径再比较——直接比绝对路径会因为
    Windows 与 Linux 的分隔符不同而全部误判成孤儿，那是灾难性的。
    """
    root = cfg.uploads_dir
    if not root.exists():
        return 0, 0, 0

    # 刚上传但还没建草稿的图片有个短暂的存在窗口（store_image 先落盘）。
    # 用"文件年龄超过 1 小时"作为缓冲，避免把这中间态误删。
    cutoff = now_utc() - timedelta(hours=1)
    removed = kept = reclaimed = 0

    for path in root.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(cfg.data_dir).as_posix()
        if relative in referenced:
            kept += 1
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        if stat.st_mtime > cutoff.timestamp():
            kept += 1
            continue
        try:
            size = stat.st_size
            os.unlink(path)
            removed += 1
            reclaimed += size
        except OSError as exc:
            log.warning("housekeeping.unlink_failed %s", kv(path=str(path), error=str(exc)))

    if removed:
        log.info("housekeeping.images_removed %s", kv(count=removed, bytes=reclaimed))
    return removed, kept, reclaimed
