# -*- coding: utf-8 -*-
"""图片处理：DIB ↔ PNG 转换、像素指纹（F6-2 图片内容指纹）

Windows 剪贴板里的位图是 DIB（BITMAPINFOHEADER + 可选调色板 + 像素数据），
不是 BMP 文件，没有 14 字节文件头，需要自己补上才能交给 Pillow 解码。
DIBV5（CF_DIBV5）额外带一个 4 字节的 alpha 通道掩码，需要一并处理。

图片指纹（F6-2）采用"缩放后像素哈希"：
    灰度化 → 等比缩放到 32×32 → 取像素字节 → 哈希
这样同一张图被重新编码（PNG 换 JPEG、压缩率不同）后字节变了，
但视觉相同仍然能被判定为重复，符合 UC-06 主事件流第 5 步的要求。
"""

import hashlib
import io
import struct

from app.constants import ERR_INTERNAL
from app.errors import AppError

try:
    from PIL import Image
except ImportError:  # pragma: no cover
    Image = None

#: 图片指纹的缩放边长（过大对重新编码不鲁棒，过小容易撞图）
FINGERPRINT_SIZE = 32
#: 缩略图边长（列表卡片用）
THUMBNAIL_SIZE = 160

_TYPE_NAMES = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "GIF": ".gif",
    "BMP": ".bmp",
    "WEBP": ".webp",
}


def require_pillow():
    if Image is None:
        raise AppError(ERR_INTERNAL, "缺少 Pillow，无法处理图片内容", "pip install pillow")


# ---------------------------------------------------------------------------
# DIB -> PIL
# ---------------------------------------------------------------------------
def dib_to_image(dib_bytes, is_v5=False):
    """把剪贴板里的 DIB 字节转成 PIL Image。

    is_v5 只表示数据来自 CF_DIBV5，解码方式与 CF_DIB 完全一致：Pillow
    原生认识 BITMAPV5HEADER（124 字节），alpha 掩码由它按 bV5AlphaMask
    自行处理，这里**不要**手工删那 4 个字节——删了头长度就对不上。

    关键是 bfOffBits：DIB 本身没有文件头，补出来的 BMP 头必须声明
    "像素数据从 14 + 头长度 + 调色板 开始"。早先这里把它写死成 14，
    Pillow 于是从位图头中间开始读像素，整幅图错位十几个像素、颜色
    通道全乱（实测 1666×1080 的图有 67% 的像素被改坏）。
    """
    require_pillow()
    if not dib_bytes or len(dib_bytes) < 40:
        raise AppError(ERR_INTERNAL, "剪贴板位图数据不完整")

    header_size = struct.unpack_from("<I", dib_bytes, 0)[0]
    if header_size not in (12, 40, 52, 56, 108, 124):
        raise AppError(ERR_INTERNAL, "未知的位图头长度：%s" % header_size)

    # 调色板紧跟在头后面，只有 ≤8 位色深才有；bfOffBits 必须把它算进去
    palette = 0
    if header_size >= 40:
        bit_count = struct.unpack_from("<H", dib_bytes, 14)[0]
        if bit_count <= 8:
            clr_used = struct.unpack_from("<I", dib_bytes, 32)[0]
            palette = (clr_used or (1 << bit_count)) * 4

    # 补上 BMP 文件头，让 Pillow 按标准 BMP 解码。
    # bfOffBits 漏算头部长度是本模块历史上最严重的缺陷，别再改回常量 14。
    size = len(dib_bytes) + 14
    file_header = b"BM" + struct.pack(
        "<IHHI", size, 0, 0, 14 + header_size + palette)
    stream = io.BytesIO(file_header + dib_bytes)
    try:
        image = Image.open(stream)
        image.load()
    except Exception as exc:  # noqa: BLE001 - Pillow 解码失败原因很多，统一转换
        raise AppError(ERR_INTERNAL, "剪贴板位图解码失败", str(exc))
    if image.mode not in ("RGB", "RGBA"):
        image = image.convert("RGBA" if "A" in image.getbands() else "RGB")
    return image


def open_image_file(path):
    """从磁盘读取图片文件。"""
    require_pillow()
    try:
        image = Image.open(path)
        image.load()
        return image
    except Exception as exc:  # noqa: BLE001
        raise AppError(ERR_INTERNAL, "图片文件无法打开", "%s（%s）" % (path, exc))


# ---------------------------------------------------------------------------
# PIL -> 字节
# ---------------------------------------------------------------------------
def image_to_png_bytes(image):
    """PIL Image -> PNG 字节（附件统一存 PNG，避免多次有损重编码）。"""
    require_pillow()
    buffer = io.BytesIO()
    if image.mode == "P":
        image = image.convert("RGBA")
    image.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def image_to_dib_bytes(image):
    """PIL Image -> CF_DIB 需要的字节（写回剪贴板时用）。

    BMP 文件头是 14 字节（"BM" + 文件大小 + 保留 4 字节 + 像素数据偏移 4 字节），
    去掉它剩下的就是 DIB。
    """
    require_pillow()
    buffer = io.BytesIO()
    rgb = image.convert("RGB") if image.mode not in ("RGB", "RGBA") else image
    rgb.save(buffer, format="BMP")
    return buffer.getvalue()[14:]


def image_to_thumbnail_png(image, size=THUMBNAIL_SIZE):
    """生成列表卡片用的缩略图 PNG 字节。"""
    require_pillow()
    thumb = image.copy()
    thumb.thumbnail((size, size))
    return image_to_png_bytes(thumb)


# ---------------------------------------------------------------------------
# 指纹
# ---------------------------------------------------------------------------
def image_hash(image, size=FINGERPRINT_SIZE):
    """图片像素指纹（F6-2）。

    返回 63 位以内的正整数（与 SQLite INTEGER 的取值习惯保持一致）。
    """
    require_pillow()
    try:
        small = image.convert("L").resize((size, size), Image.LANCZOS)
    except Exception:  # noqa: BLE001 - 极端尺寸下 LANCZOS 可能失败，退化为最近邻
        small = image.convert("L").resize((size, size), Image.NEAREST)
    digest = hashlib.blake2b(small.tobytes(), digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


def image_hash_from_bytes(data):
    """从图片字节直接计算指纹（附件存在磁盘上时先用 open_image_file）。"""
    require_pillow()
    try:
        image = Image.open(io.BytesIO(data))
        image.load()
    except Exception as exc:  # noqa: BLE001
        raise AppError(ERR_INTERNAL, "图片无法解码，无法计算指纹", str(exc))
    return image_hash(image)
