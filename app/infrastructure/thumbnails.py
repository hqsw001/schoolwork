# -*- coding: utf-8 -*-
"""缩略图：给列表卡片生成小图，带磁盘缓存。

为什么必须缓存：列表里直接加载原始附件（动辄一两千像素、几百 KB）
会让滚动明显卡顿，50 张卡片就是 50 次大图解码。所以统一缩到 160px
长边的小 PNG，放在 ``<数据目录>/thumbs/`` 下，文件名沿用附件的哈希名。

缓存有效性用 mtime 判断：缩略图不比源文件旧就直接复用，不做重复解码。
采集时顺手生成一次，所以新复制的图片在列表里是秒出的。
"""

import base64
import os

#: 缩略图缓存目录名（挂在数据目录下）
THUMB_DIR_NAME = "thumbs"

#: 缩略图长边默认像素
DEFAULT_THUMB_SIZE = 160

#: 单张缩略图允许的最大字节数——超过就说明生成逻辑出了问题，不再往界面送
MAX_THUMB_BYTES = 512 * 1024


def thumb_path_for(cache_dir, source_path, size=DEFAULT_THUMB_SIZE):
    """缩略图的目标路径（按源文件名 + 尺寸区分，换尺寸不会互相覆盖）。"""
    stem = os.path.splitext(os.path.basename(source_path or ""))[0]
    return os.path.join(cache_dir, "%s_%d.png" % (stem, size))


def _is_fresh(target, source):
    try:
        return (os.path.isfile(target)
                and os.path.getmtime(target) >= os.path.getmtime(source))
    except OSError:
        return False


def ensure_thumbnail(cache_dir, source_path, size=DEFAULT_THUMB_SIZE, logger=None):
    """保证缩略图存在，返回它的路径。

    返回 None 表示"没有可用缩略图"（源文件不在、不是图片、或解码失败）——
    调用方应当退回纯文本预览，而不是报错。
    """
    if not cache_dir or not source_path or not os.path.isfile(source_path):
        return None

    target = thumb_path_for(cache_dir, source_path, size)
    if _is_fresh(target, source_path):
        return target

    try:
        from app.infrastructure.clipboard import image_utils

        image = image_utils.open_image_file(source_path)
        data = image_utils.image_to_thumbnail_png(image, size=size)
        os.makedirs(cache_dir, exist_ok=True)
        # 先写临时文件再原子替换：中途失败不会留下半张图，
        # 而半张图会让 Pillow/tk 之后一直解码失败。
        tmp = "%s.%d.tmp" % (target, os.getpid())
        with open(tmp, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
        return target
    except Exception as exc:  # noqa: BLE001 - 缩略图失败绝不能影响列表渲染
        if logger:
            logger.warning("生成缩略图失败（%s）：%s",
                           os.path.basename(source_path), exc)
        return None


def thumbnail_base64(cache_dir, source_path, size=DEFAULT_THUMB_SIZE, logger=None):
    """返回 base64 编码的缩略图 PNG（tkinter 的 PhotoImage 可以直接用）。"""
    path = ensure_thumbnail(cache_dir, source_path, size, logger)
    if not path:
        return None
    try:
        if os.path.getsize(path) > MAX_THUMB_BYTES:
            if logger:
                logger.warning("缩略图异常偏大，已忽略：%s", os.path.basename(path))
            return None
        with open(path, "rb") as handle:
            return base64.b64encode(handle.read()).decode("ascii")
    except OSError:
        return None


def find_orphan_thumbnails(cache_dir, referenced_paths):
    """找出没有任何附件引用的缩略图（源附件被删掉之后留下的）。"""
    if not cache_dir or not os.path.isdir(cache_dir):
        return []
    # 缩略图名是 "<附件名去扩展>_<尺寸>.png"，所以按"前缀"比对
    stems = set()
    for path in referenced_paths or []:
        stems.add(os.path.splitext(os.path.basename(str(path)))[0].lower())

    orphans = []
    for name in sorted(os.listdir(cache_dir)):
        if not name.lower().endswith(".png"):
            continue
        if name.endswith(".tmp"):
            continue
        stem = name.rsplit("_", 1)[0].lower()
        if stem not in stems:
            orphans.append(os.path.join(cache_dir, name))
    return orphans


def delete_thumbnail(path, cache_dir, logger=None):
    """删除一张缩略图；只允许删缓存目录内的文件。"""
    if not path or not cache_dir:
        return False
    target = os.path.abspath(path)
    root = os.path.abspath(cache_dir)
    if os.path.dirname(target) != root:
        return False
    try:
        os.remove(target)
        return True
    except OSError as exc:
        if logger:
            logger.warning("删除缩略图失败（%s）：%s", os.path.basename(target), exc)
        return False


def cache_usage(cache_dir):
    """返回 (总字节数, 文件数)，供设置面板展示占用。"""
    if not cache_dir or not os.path.isdir(cache_dir):
        return 0, 0
    total = 0
    count = 0
    for name in os.listdir(cache_dir):
        full = os.path.join(cache_dir, name)
        if os.path.isfile(full):
            try:
                total += os.path.getsize(full)
                count += 1
            except OSError:
                continue
    return total, count
