"""生成 PWA 图标。

旧项目的图标没进归档（当时只归档了数据库与上传图片），所以这里用 Pillow 现生成。

设计取舍：**不做文字**。主屏图标在 32–60px 上显示，任何文字都会糊成一团；
用一个粗的白色对勾 + 深色圆角底，在深浅两种主屏壁纸上都清楚，
也一眼能认出"这是待办"。

maskable 版本要单独留安全区：Android 会按自己的形状（圆形/圆角方形）裁切，
内容落在中间约 80% 的区域内才不会被切掉。
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"

#: 与前端主题色一致（深色主色）。用同一个值是有意的：
#: 图标底色与 App 主题色不一致会让人以为是两个东西。
BG = (28, 30, 33, 255)
CHECK = (255, 255, 255, 255)
ACCENT = (94, 168, 255, 255)


def rounded_square(size: int, radius_ratio: float, bg: tuple[int, int, int, int]) -> Image.Image:
    image = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    radius = int(size * radius_ratio)
    draw.rounded_rectangle([(0, 0), (size - 1, size - 1)], radius=radius, fill=bg)
    return image


def draw_check(image: Image.Image, *, scale: float, offset_y: float = 0.0,
               color: tuple[int, int, int, int] = CHECK) -> None:
    size = image.width
    draw = ImageDraw.Draw(image)
    width = max(2, int(size * 0.085))
    cx, cy = size / 2, size / 2 + size * offset_y

    # 对勾的三个点：左起点、底部转折、右上终点
    p1 = (cx - size * 0.22 * scale, cy - size * 0.01 * scale)
    p2 = (cx - size * 0.06 * scale, cy + size * 0.16 * scale)
    p3 = (cx + size * 0.24 * scale, cy - size * 0.20 * scale)

    for a, b in ((p1, p2), (p2, p3)):
        draw.line([a, b], fill=color, width=width, joint="curve")
    # 两个端点补圆，避免方头看起来像被切断
    r = width / 2
    for point in (p1, p2, p3):
        draw.ellipse(
            [point[0] - r, point[1] - r, point[0] + r, point[1] + r], fill=color
        )


def build_icon(size: int, *, radius_ratio: float = 0.22, scale: float = 1.0,
               with_accent_dot: bool = False) -> Image.Image:
    image = rounded_square(size, radius_ratio, BG)
    draw_check(image, scale=scale)

    if with_accent_dot:
        # 右上角一个小圆点：暗示"有待办"。小尺寸上它会糊掉，所以只在大图标上画。
        draw = ImageDraw.Draw(image)
        r = size * 0.075
        cx, cy = size * 0.755, size * 0.255
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)

    return image


def build_maskable(size: int) -> Image.Image:
    """maskable 版：满幅底色 + 缩到中间的对勾。

    Android 会用各种形状裁切（最大可能切掉边缘 20%），
    所以底色铺满整个画布，而图形缩到约 60%。
    """
    image = Image.new("RGBA", (size, size), BG)
    draw_check(image, scale=0.62)
    draw = ImageDraw.Draw(image)
    r = size * 0.045
    cx, cy = size * 0.70, size * 0.30
    draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=ACCENT)
    return image


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)

    targets = {
        "icon-192.png": build_icon(192),
        "icon-512.png": build_icon(512, with_accent_dot=True),
        "apple-touch-icon.png": build_icon(180, radius_ratio=0.0, with_accent_dot=True),
        "favicon-32.png": build_icon(32, radius_ratio=0.25),
        "icon-maskable-512.png": build_maskable(512),
    }
    for name, image in targets.items():
        path = OUT / name
        image.save(path, format="PNG", optimize=True)
        print(f"  {name:26s} {image.width}x{image.height}  {path.stat().st_size:>7d} B")

    # apple-touch-icon 不要圆角（iOS 自己会加），所以单独重画一次不带圆角的底
    print(f"\n输出目录：{OUT}")


if __name__ == "__main__":
    main()
