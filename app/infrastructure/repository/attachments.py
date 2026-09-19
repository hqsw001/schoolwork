# -*- coding: utf-8 -*-
"""图片附件外置存储（F2-3 图片附件外置存储；UC-03 备选流 A3）

规则（UC-03 业务规则 R3）：图片一律外置为文件，数据库内不保存二进制数据，
content 字段只存放附件的绝对路径。

文件命名用内容指纹，因此同一张图重复入库时天然复用同一个文件，
不需要额外的去重逻辑。
"""

import os

from app.errors import AppError
from app.constants import ERR_INTERNAL

_EXT_BY_FORMAT = {
    "PNG": ".png",
    "JPEG": ".jpg",
    "GIF": ".gif",
    "BMP": ".bmp",
    "WEBP": ".webp",
}


def save_image_attachment(attachment_dir, image_bytes, content_hash, logger=None):
    """把图片字节写入附件目录，返回 (绝对路径, 是否新建)。

    image_bytes 可以是 PNG/JPEG/... 原始字节，也可以是 Windows DIB 字节，
    由调用方（image_utils）统一转成 PNG 后再交进来。
    """
    fmt = _detect_format(image_bytes)
    ext = _EXT_BY_FORMAT.get(fmt, ".png")
    filename = "%016x%s" % (content_hash & 0xFFFFFFFFFFFFFFFF, ext)
    target = os.path.join(attachment_dir, filename)
    if os.path.exists(target):
        return os.path.abspath(target), False
    try:
        os.makedirs(attachment_dir, exist_ok=True)
        # 先写临时文件再改名，避免写到一半被中断留下半张图
        tmp = target + ".tmp"
        with open(tmp, "wb") as fh:
            fh.write(image_bytes)
        os.replace(tmp, target)
    except OSError as exc:
        raise AppError(ERR_INTERNAL, "图片附件写入失败", str(exc))
    if logger:
        logger.debug("图片附件已写入 %s（%d 字节）", filename, len(image_bytes))
    return os.path.abspath(target), True


def delete_attachment(path, attachment_dir, logger=None):
    """删除附件文件，但只删本程序附件目录内的文件（安全边界）。

    返回 True 表示文件已删除或本来就不存在；False 表示删除失败
    （被占用或权限不足），由调用方记入待清理列表（UC-05 异常流 E1）。
    """
    if not path:
        return True
    abs_path = os.path.abspath(path)
    abs_dir = os.path.abspath(attachment_dir)
    if not abs_path.lower().startswith(abs_dir.lower() + os.sep):
        # 位于程序目录之外的文件（例如用户从桌面复制的原图）一律不动
        return True
    try:
        os.remove(abs_path)
        if logger:
            logger.debug("已删除附件 %s", os.path.basename(abs_path))
        return True
    except FileNotFoundError:
        return True
    except OSError as exc:
        if logger:
            logger.warning("附件删除失败 %s：%s", abs_path, exc)
        return False


def attachment_usage(attachment_dir):
    """统计附件目录占用的字节数与文件数（F3-5 容量统计）。"""
    total = 0
    count = 0
    if not os.path.isdir(attachment_dir):
        return 0, 0
    for name in os.listdir(attachment_dir):
        full = os.path.join(attachment_dir, name)
        if os.path.isfile(full):
            try:
                total += os.path.getsize(full)
                count += 1
            except OSError:
                continue
    return total, count


def find_orphan_attachments(attachment_dir, referenced_paths):
    """找出孤儿附件（数据库里已无对应记录），供启动时清理（UC-20 第 5 步）。"""
    referenced = {os.path.abspath(p).lower() for p in referenced_paths if p}
    orphans = []
    if not os.path.isdir(attachment_dir):
        return orphans
    for name in os.listdir(attachment_dir):
        full = os.path.abspath(os.path.join(attachment_dir, name))
        if os.path.isfile(full) and full.lower() not in referenced:
            orphans.append(full)
    return orphans


def _detect_format(data):
    """按文件头判断图片格式（不依赖扩展名，也不解码整张图）。"""
    if not data or len(data) < 12:
        return "PNG"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "PNG"
    if data[:3] == b"\xff\xd8\xff":
        return "JPEG"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "GIF"
    if data[:2] == b"BM":
        return "BMP"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "WEBP"
    return "PNG"
