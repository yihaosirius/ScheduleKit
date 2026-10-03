"""数据目录布局。

集中在这里的原因：``data_dir`` 是配置里唯一可变的位置，而它下面有数据库、
上传图片、备份三类东西。路径拼错会导致"图片存了但读不到"这类难查的问题，
所以只允许从这一个模块取路径。
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from app.config import Config

#: 上传图片按 sha256 前两位分子目录。单目录堆上万张图时，目录项查找会明显变慢。
SHARD_LEN = 2


def ensure_dirs(cfg: Config) -> None:
    for path in (cfg.data_dir, cfg.uploads_dir, cfg.backups_dir):
        path.mkdir(parents=True, exist_ok=True)


def upload_path(cfg: Config, sha256_hex: str, suffix: str) -> Path:
    """图片落盘路径。文件名用内容哈希，天然去重且可校验。"""
    shard = sha256_hex[:SHARD_LEN]
    return cfg.uploads_dir / shard / f"{sha256_hex}{suffix}"


def relative_upload_path(cfg: Config, absolute: Path) -> str:
    """库里存相对路径，这样换机器（或把 data_dir 挂到别处）备份仍可搬。"""
    return absolute.relative_to(cfg.data_dir).as_posix()


def resolve_upload_path(cfg: Config, relative: str) -> Path:
    """还原为绝对路径，并**阻止越权路径**。

    库里的值理论上由我们写入，但它是字符串；一旦有人手改库或将来接口变更，
    ``../../etc/shadow`` 这类值会变成任意文件读取。这里显式挡住。
    """
    candidate = (cfg.data_dir / relative).resolve()
    root = cfg.data_dir.resolve()
    if root != candidate and root not in candidate.parents:
        raise ValueError(f"上传路径越出数据目录：{relative!r}")
    return candidate


def sha256_of(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()
