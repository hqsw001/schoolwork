# -*- coding: utf-8 -*-
"""领域模型（实体类与值对象）

对应《需求分析报告》6.3.1 分析类图中的实体类：

    ClipboardEntry   一条剪贴板历史记录
    Tag              一个自定义标签
    Setting          一项配置
    ClipboardData    从系统剪贴板读到的原始数据（值对象）
"""

import json

from app.constants import (
    ALL_TYPES,
    PREVIEW_MAX_LEN,
    TYPE_IMAGE,
    TYPE_LABELS,
    TYPE_TEXT,
)


class ClipboardData(object):
    """值对象：一次采集从系统剪贴板读到的原始数据。

    对应 Rust 版的枚举 ``ClipboardData { Text, RichText, Image, Files }``。
    这里用 kind 字段区分四种形态，字段含义固定：

        kind="text"      text=纯文本
        kind="rich_text" text=纯文本降级内容, html=HTML 片段
        kind="image"     image_bytes=PNG 或 DIB 编码后的字节
        kind="files"     files=路径列表
    """

    __slots__ = ("kind", "text", "html", "image_bytes", "files", "clipboard_formats")

    def __init__(self, kind, text=None, html=None, image_bytes=None, files=None,
                 clipboard_formats=None):
        self.kind = kind
        self.text = text
        self.html = html
        self.image_bytes = image_bytes
        self.files = files or []
        #: 采集时剪贴板上实际存在的格式号列表，用于排查多格式竞争问题
        self.clipboard_formats = clipboard_formats or []

    def __repr__(self):
        if self.kind == "image":
            size = len(self.image_bytes) if self.image_bytes else 0
            return "<ClipboardData image %d bytes>" % size
        if self.kind == "files":
            return "<ClipboardData files %d 项>" % len(self.files)
        body = (self.text or "")[:40].replace("\n", "\\n")
        return "<ClipboardData %s %r>" % (self.kind, body)


class ClipboardEntry(object):
    """实体类：一条剪贴板历史记录（对应表 clipboard_history 的一行）。"""

    __slots__ = (
        "id", "content_type", "content", "html_content", "source_app",
        "source_app_path", "timestamp", "preview", "is_pinned", "pinned_order",
        "tags", "use_count", "content_hash", "is_external", "is_sensitive",
        "is_truncated", "attachment_path",
    )

    def __init__(
        self,
        id=0,
        content_type=TYPE_TEXT,
        content="",
        html_content=None,
        source_app="未知",
        source_app_path=None,
        timestamp=0,
        preview="",
        is_pinned=0,
        pinned_order=0,
        tags=None,
        use_count=0,
        content_hash=0,
        is_external=0,
        is_sensitive=0,
        is_truncated=0,
        attachment_path=None,
    ):
        self.id = id
        self.content_type = content_type
        self.content = content
        self.html_content = html_content
        self.source_app = source_app or "未知"
        self.source_app_path = source_app_path
        self.timestamp = timestamp
        self.preview = preview
        self.is_pinned = int(is_pinned or 0)
        self.pinned_order = int(pinned_order or 0)
        self.tags = list(tags or [])
        self.use_count = int(use_count or 0)
        self.content_hash = int(content_hash or 0)
        self.is_external = int(is_external or 0)
        # 以下两项由 F9 隐私模块与 UC-01 备选流使用，本期只做识别与标记
        self.is_sensitive = int(is_sensitive or 0)
        self.is_truncated = int(is_truncated or 0)
        #: 图片类记录的附件绝对路径（由 content 字段冗余出来，便于清理文件）
        self.attachment_path = attachment_path

    # ---------------------------------------------------------------- 转换
    @classmethod
    def from_row(cls, row):
        """由 sqlite3.Row 构造实体。"""
        tags_raw = row["tags"] if "tags" in row.keys() else "[]"
        tags = cls._parse_tags(tags_raw)
        content = row["content"]
        html_content = row["html_content"]
        is_sensitive = row["is_sensitive"] if "is_sensitive" in row.keys() else 0
        attachment = None
        if (row["content_type"] if "content_type" in row.keys() else "") == TYPE_IMAGE:
            attachment = content
        return cls(
            id=row["id"],
            content_type=row["content_type"],
            content=content,
            html_content=html_content,
            source_app=row["source_app"],
            source_app_path=row["source_app_path"],
            timestamp=row["timestamp"],
            preview=row["preview"],
            is_pinned=row["is_pinned"],
            pinned_order=row["pinned_order"],
            tags=tags,
            use_count=row["use_count"],
            content_hash=row["content_hash"],
            is_external=row["is_external"],
            is_sensitive=is_sensitive,
            attachment_path=attachment,
        )

    @staticmethod
    def _parse_tags(tags_raw):
        if not tags_raw:
            return []
        if isinstance(tags_raw, (list, tuple)):
            return list(tags_raw)
        try:
            parsed = json.loads(tags_raw)
            if isinstance(parsed, list):
                return [str(t) for t in parsed]
        except (ValueError, TypeError):
            # 冗余字段被外部改坏时不阻塞读取，退化为按逗号切分
            return [t.strip() for t in str(tags_raw).split(",") if t.strip()]
        return []

    def to_dict(self):
        """序列化为界面可直接使用的字典（事件的载荷格式）。"""
        return {
            "id": self.id,
            "contentType": self.content_type,
            "typeLabel": TYPE_LABELS.get(self.content_type, self.content_type),
            "content": self.content,
            "htmlContent": self.html_content,
            "sourceApp": self.source_app,
            "sourceAppPath": self.source_app_path,
            "timestamp": self.timestamp,
            "preview": self.preview,
            "isPinned": bool(self.is_pinned),
            "pinnedOrder": self.pinned_order,
            "tags": list(self.tags),
            "useCount": self.use_count,
            "contentHash": self.content_hash,
            "isExternal": bool(self.is_external),
            "isSensitive": bool(self.is_sensitive),
            "isTruncated": bool(self.is_truncated),
            "attachmentPath": self.attachment_path,
        }

    # ---------------------------------------------------------------- 校验
    def validate(self):
        """入库前自检：类型合法、正文非空（UC-03 R2）。"""
        if self.content_type not in ALL_TYPES:
            raise ValueError("未知内容类型：%s" % self.content_type)
        if self.content == "" and self.content_type != TYPE_IMAGE:
            raise ValueError("内容不能为空")
        return True

    def __repr__(self):
        return "<ClipboardEntry #%s %s %r>" % (
            self.id, self.content_type, (self.preview or "")[:24]
        )


class Tag(object):
    """实体类：一个自定义标签（对应表 saved_tags）。"""

    __slots__ = ("name", "color")

    def __init__(self, name, color=None):
        self.name = name
        self.color = color

    @classmethod
    def from_row(cls, row):
        return cls(row["name"], row["color"])

    def to_dict(self):
        return {"name": self.name, "color": self.color}


class Setting(object):
    """实体类：一项配置（对应表 settings，值统一以字符串存放）。"""

    __slots__ = ("key", "value")

    def __init__(self, key, value):
        self.key = key
        self.value = value

    def to_dict(self):
        return {"key": self.key, "value": self.value}


def make_preview(text, limit=PREVIEW_MAX_LEN):
    """生成列表预览文本：折叠空白后截断（UC-02 主事件流第 6 步）。

    折叠规则：连续空白（含换行、制表符）压成一个空格，去掉首尾空白。
    这样多行内容在单行卡片里也能读得清楚，且不会因为换行把卡片撑破。
    """
    if not text:
        return ""
    collapsed = " ".join(str(text).split())
    if len(collapsed) > limit:
        return collapsed[:limit]
    return collapsed
