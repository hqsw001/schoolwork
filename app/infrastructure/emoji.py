# -*- coding: utf-8 -*-
"""表情：内置 Emoji 分组表 + 图片表情收藏（对应 TieZ 的 EmojiPanel / file_cmd）

两块完全离线的东西：

1. **Emoji 分组表**：随程序带一份 ``app/data/emoji_data.json``，读不到就退回
   内置的精简表——**绝不允许因为数据文件缺失就整个面板打不开**。
2. **图片表情收藏**：存在 ``<数据目录>/emoji_favorites/``，文件名按内容哈希，
   同一张图重复收藏不会产生第二份。

安全上有一条硬要求：删除收藏时**必须校验目标路径在收藏目录之内**。
这个路径来自界面，不校验的话一个构造出来的相对路径就能删掉用户磁盘上
任意文件。
"""

import base64
import hashlib
import json
import os

from app.infrastructure import paths

#: 收藏目录名
FAVORITE_DIR_NAME = "emoji_favorites"

#: 收藏允许的图片扩展名
ALLOWED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".gif")

#: 单个收藏文件最大字节数（防止有人把几百 MB 的图拖进来）
MAX_FAVORITE_BYTES = 8 * 1024 * 1024

#: 数据文件位置。源码运行时是 ``<项目>/app/data/emoji_data.json``；
#: 打包成 exe 之后是解包目录里的 ``app/data/emoji_data.json``（打包脚本会把
#: 这份 json 放到同样的相对位置），所以按 ``bundle_root()`` 拼而不是按
#: exe 所在目录拼 —— 按 exe 目录拼的话打包后永远读不到，只能吃兜底表。
DATA_FILE = paths.bundle_file("app", "data", "emoji_data.json")

#: 兜底分组表：数据文件读不到时用它，保证面板永远有内容
FALLBACK_GROUPS = [
    {"name": "常用", "emojis": ["😀", "😂", "😊", "😍", "😎", "🤔", "👍", "👎",
                                "🙏", "👏", "🎉", "🔥", "💯", "✨", "👌", "😭"]},
    {"name": "符号", "emojis": ["❤️", "✅", "❌", "⚠️", "⭐", "❗", "❓", "➕"]},
]

#: 按扩展名推断 MIME（data URL 用）
_MIME_BY_EXT = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

#: 常见图片的魔数，用于 data URL 没写扩展名时判断类型
_MAGIC = (
    (b"\x89PNG\r\n\x1a\n", ".png"),
    (b"\xff\xd8\xff", ".jpg"),
    (b"GIF87a", ".gif"),
    (b"GIF89a", ".gif"),
    (b"RIFF", ".webp"),
)


# ---------------------------------------------------------------------------
# Emoji 分组表
# ---------------------------------------------------------------------------
def load_groups():
    """读取内置 Emoji 分组表；任何异常都退回兜底表。"""
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, ValueError):
        return list(FALLBACK_GROUPS)

    groups = []
    for item in payload.get("groups") or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        emojis = [str(e) for e in (item.get("emojis") or []) if e]
        if name and emojis:
            groups.append({"name": name, "emojis": emojis})
    return groups or list(FALLBACK_GROUPS)


# ---------------------------------------------------------------------------
# 收藏
# ---------------------------------------------------------------------------
def favorite_dir(data_dir):
    return os.path.join(data_dir, FAVORITE_DIR_NAME)


def extension_for(data, hint=None):
    """决定收藏文件用什么扩展名：文件名 → MIME 提示 → 魔数 → 默认 png。"""
    if hint:
        ext = os.path.splitext(str(hint))[1].lower()
        if ext in ALLOWED_EXTENSIONS:
            return ext
    for magic, ext in _MAGIC:
        if data.startswith(magic):
            return ext
    return ".png"


def _safe_name(data):
    """按内容哈希命名：同一张图重复收藏不会产生第二份。"""
    return "fav_%s" % hashlib.blake2b(data, digest_size=8).hexdigest()


def save_bytes(data_dir, data, hint=None, logger=None):
    """把一段图片字节存进收藏目录，返回文件路径。

    内容相同就直接复用已有文件，不重复落盘。
    """
    if not data:
        raise ValueError("图片内容为空")
    if len(data) > MAX_FAVORITE_BYTES:
        raise ValueError("图片太大（超过 %d MB）" % (MAX_FAVORITE_BYTES // 1024 // 1024))
    directory = favorite_dir(data_dir)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, _safe_name(data) + extension_for(data, hint))
    if os.path.isfile(path):
        return path
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "wb") as handle:
        handle.write(data)
    os.replace(tmp, path)
    if logger:
        logger.info("已收藏表情：%s", os.path.basename(path))
    return path


def save_file(data_dir, source_path, logger=None):
    """把本地图片文件收进收藏。"""
    if not source_path or not os.path.isfile(source_path):
        raise ValueError("文件不存在")
    ext = os.path.splitext(source_path)[1].lower()
    if ext not in ALLOWED_EXTENSIONS:
        raise ValueError("只支持 %s" % "、".join(ALLOWED_EXTENSIONS))
    with open(source_path, "rb") as handle:
        data = handle.read()
    return save_bytes(data_dir, data, hint=source_path, logger=logger)


def parse_data_url(data_url):
    """解析 ``data:image/xxx;base64,....``，返回 (字节, 扩展名提示)。"""
    if not data_url or not str(data_url).startswith("data:"):
        raise ValueError("不是合法的 data URL")
    header, _, payload = str(data_url).partition(",")
    if not payload:
        raise ValueError("data URL 里没有内容")
    if ";base64" in header.lower():
        try:
            data = base64.b64decode(payload, validate=False)
        except Exception as exc:  # noqa: BLE001
            raise ValueError("data URL 解码失败：%s" % exc)
    else:
        data = payload.encode("utf-8", "ignore")
    mime = header[5:].split(";")[0].strip().lower()
    hint = None
    for ext, known in _MIME_BY_EXT.items():
        if known == mime:
            hint = ext
            break
    return data, hint


def save_data_url(data_dir, data_url, file_name=None, logger=None):
    """把 data URL 收进收藏。"""
    data, hint = parse_data_url(data_url)
    return save_bytes(data_dir, data, hint=file_name or hint, logger=logger)


def list_favorites(data_dir):
    """列出收藏（按文件名排序，顺序稳定）。"""
    directory = favorite_dir(data_dir)
    if not os.path.isdir(directory):
        return []
    out = []
    for name in sorted(os.listdir(directory)):
        if name.endswith(".tmp"):
            continue
        full = os.path.join(directory, name)
        if not os.path.isfile(full):
            continue
        if os.path.splitext(name)[1].lower() not in ALLOWED_EXTENSIONS:
            continue
        try:
            out.append({"path": full, "name": name, "size": os.path.getsize(full)})
        except OSError:
            continue
    return out


def is_inside(path, directory):
    """目标路径是否真的在目录之内（防目录穿越）。"""
    if not path or not directory:
        return False
    try:
        target = os.path.abspath(path)
        root = os.path.abspath(directory)
    except (OSError, ValueError):
        return False
    if target == root:
        return False
    return os.path.dirname(target) == root or target.startswith(root + os.sep)


def remove_favorite(data_dir, path, logger=None):
    """删除一个收藏。

    **必须**校验路径在收藏目录内：这个路径来自界面，不校验的话一个
    构造出来的相对路径就能删掉磁盘上任意文件。
    """
    directory = favorite_dir(data_dir)
    if not is_inside(path, directory):
        if logger:
            logger.warning("拒绝删除收藏目录之外的文件：%s", path)
        return False
    if not os.path.isfile(path):
        return False
    try:
        os.remove(path)
        return True
    except OSError as exc:
        if logger:
            logger.warning("删除收藏失败：%s", exc)
        return False


def favorite_usage(data_dir):
    """返回 (总字节数, 文件数)。"""
    favorites = list_favorites(data_dir)
    return sum(item["size"] for item in favorites), len(favorites)
