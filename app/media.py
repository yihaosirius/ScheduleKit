"""图片校验与落盘。

**不信任上传侧的任何声明**：不看文件名的扩展名，不看 multipart 里的 content-type，
一律用 Pillow 真解一次。理由很实际——iPhone 快捷指令会把 HEIC 标成 image/jpeg，
把 PNG 标成 image/octet-stream，信 header 会导致后面的视觉调用随机失败。

落盘文件名是内容的 sha256，带来三个好处：
1. 天然去重（同一张截图被传两次只占一份）；
2. 可以按内容校验归档是否完整；
3. ``resolve_upload_path`` 的路径穿越防护变得可验证。
"""

from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, UnidentifiedImageError

from app.config import Config
from app.logging import get_logger, kv
from app.paths import ensure_dirs, relative_upload_path, sha256_of, upload_path

log = get_logger("media")

#: Pillow 支持的格式白名单。刻意不含 SVG/PDF：它们是"可执行/可解析"的文档，
#: 不是照片，而视觉模型要的是位图。
ALLOWED_FORMATS = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp", "GIF": ".gif"}

#: 解压炸弹防护：单张图最大像素数。8000×8000 已经远超任何手机截图。
MAX_PIXELS = 64_000_000


class ImageRejected(ValueError):
    """图片不可用。调用方应返回 4xx 并给出人话原因。"""


@dataclass(frozen=True)
class StoredImage:
    relative_path: str
    absolute_path: str
    mime: str
    size_bytes: int
    sha256: str
    width: int
    height: int
    pillow_format: str


def _mime_for(pillow_format: str) -> str:
    return {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp", "GIF": "image/gif"}[
        pillow_format
    ]


def inspect_image(data: bytes) -> tuple[str, int, int]:
    """真解一次，返回 ``(格式, 宽, 高)``。任何异常都统一抛 :class:`ImageRejected`。"""
    if not data:
        raise ImageRejected("图片为空")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.load()
            fmt = (probe.format or "").upper()
            width, height = probe.size
    except UnidentifiedImageError as exc:
        raise ImageRejected(
            "无法识别为图片。可能原因：文件被截断、格式是 HEIC（iPhone 需在快捷指令里转 JPEG）"
            "或根本不是图片。"
        ) from exc
    except Image.DecompressionBombError as exc:
        raise ImageRejected("图片像素过多，疑似解压炸弹") from exc
    except OSError as exc:
        raise ImageRejected(f"图片解码失败：{exc}") from exc

    if fmt not in ALLOWED_FORMATS:
        raise ImageRejected(
            f"不支持的图片格式 {fmt or '未知'}；支持：{'、'.join(sorted(ALLOWED_FORMATS))}"
        )
    if width * height > MAX_PIXELS:
        raise ImageRejected(f"图片像素过多（{width}×{height}）")
    return fmt, width, height


def store_image(cfg: Config, data: bytes) -> StoredImage:
    """校验并落盘。已存在同样内容时直接复用（幂等）。"""
    limit = cfg.server.max_image_bytes
    if len(data) > limit:
        raise ImageRejected(
            f"图片过大：{len(data)} 字节，上限 {limit} 字节"
            f"（{limit // (1024 * 1024)}MB）"
        )

    fmt, width, height = inspect_image(data)
    digest = sha256_of(data)
    ensure_dirs(cfg)
    absolute = upload_path(cfg, digest, ALLOWED_FORMATS[fmt])
    absolute.parent.mkdir(parents=True, exist_ok=True)

    if not absolute.exists():
        absolute.write_bytes(data)
        log.info(
            "media.stored %s",
            kv(sha256=digest[:12], bytes=len(data), fmt=fmt, w=width, h=height),
        )
    else:
        log.info("media.reused %s", kv(sha256=digest[:12], bytes=len(data)))

    return StoredImage(
        relative_path=relative_upload_path(cfg, absolute),
        absolute_path=str(absolute),
        mime=_mime_for(fmt),
        size_bytes=len(data),
        sha256=digest,
        width=width,
        height=height,
        pillow_format=fmt,
    )


def to_llm_data_url(data: bytes, mime: str) -> str:
    """视觉模型要的 ``data:`` URL。

    用 base64 内联而不是传 URL：模型侧拿不到我们的鉴权，
    传 URL 就得开一个匿名可读的图片端点，那是纯负担。
    """
    import base64

    return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
