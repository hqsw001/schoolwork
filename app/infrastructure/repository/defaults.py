# -*- coding: utf-8 -*-
"""配置项登记表：键、类型、默认值（《需求分析报告》7.2 主要配置项）

配置值在数据库中统一以字符串存放，读取时按这里的类型解析。
新增配置项只改本文件即可，缺失项会自动补齐并写回（UC-18 业务规则 R4）。
"""

from app.constants import (
    DENSITY_NORMAL,
    K_ALWAYS_ON_TOP,
    K_CAPTURE_FILES,
    K_CAPTURE_RICH_TEXT,
    K_CLEANUP_RULES,
    K_DEDUPLICATE,
    K_DELETE_AFTER_PASTE,
    K_EDGE_DOCKING,
    K_FOLLOW_MOUSE,
    K_LIST_DENSITY,
    K_MAIN_HOTKEY,
    K_PERSISTENT,
    K_PERSISTENT_LIMIT,
    K_PERSISTENT_LIMIT_ENABLED,
    K_PRIVACY_KINDS,
    K_PRIVACY_PROTECTION,
    K_SEARCH_HOTKEY,
    K_SILENT_START,
    K_SOUND_ENABLED,
    K_THEME,
    K_THEME_MODE,
    K_WINDOW_GEOMETRY,
    MODE_SYSTEM,
    THEME_MICA,
)

#: 布尔 / 整数 / 文本 / 枚举 / 字符串数组
TYPE_BOOL = "bool"
TYPE_INT = "int"
TYPE_TEXT = "text"
TYPE_ENUM = "enum"
TYPE_LIST = "list"

#: 键 -> (类型, 默认值, 含义, 合法取值)
SPECS = {
    K_PERSISTENT: (
        TYPE_BOOL, True, "是否持久化保存（关闭后仅会话内保留）", None),
    K_PERSISTENT_LIMIT_ENABLED: (
        TYPE_BOOL, True, "是否启用存储上限", None),
    K_PERSISTENT_LIMIT: (
        TYPE_INT, 500, "存储上限条数", None),
    K_DEDUPLICATE: (
        TYPE_BOOL, True, "是否启用重复内容合并", None),
    K_CAPTURE_RICH_TEXT: (
        TYPE_BOOL, True, "是否采集富文本", None),
    K_CAPTURE_FILES: (
        TYPE_BOOL, True, "是否采集文件与目录路径", None),
    K_DELETE_AFTER_PASTE: (
        TYPE_BOOL, False, "粘贴后是否自动删除该条记录", None),
    K_CLEANUP_RULES: (
        TYPE_TEXT, "", "入库前的文本清理规则（正则替换）", None),
    K_PRIVACY_PROTECTION: (
        TYPE_BOOL, True, "是否启用敏感信息识别与脱敏", None),
    K_PRIVACY_KINDS: (
        TYPE_LIST, ["身份证", "手机号", "邮箱", "密钥"], "启用识别的敏感类型集合", None),
    K_THEME: (
        TYPE_ENUM, THEME_MICA, "当前外观主题", None),
    K_THEME_MODE: (
        TYPE_ENUM, MODE_SYSTEM, "深浅色模式", ("light", "dark", "system")),
    K_LIST_DENSITY: (
        TYPE_ENUM, DENSITY_NORMAL, "列表密度", ("compact", "normal", "loose")),
    K_MAIN_HOTKEY: (
        TYPE_TEXT, "Ctrl+Shift+V", "唤起主面板的全局快捷键", None),
    K_SEARCH_HOTKEY: (
        TYPE_TEXT, "Ctrl+Shift+F", "唤起并聚焦搜索框的全局快捷键", None),
    K_EDGE_DOCKING: (
        TYPE_BOOL, False, "是否启用窗口贴边停靠", None),
    K_FOLLOW_MOUSE: (
        TYPE_BOOL, False, "面板是否在鼠标位置弹出", None),
    K_SILENT_START: (
        TYPE_BOOL, True, "是否静默启动（不显示主窗口）", None),
    K_SOUND_ENABLED: (
        TYPE_BOOL, False, "复制时是否播放提示音", None),
    K_ALWAYS_ON_TOP: (
        TYPE_BOOL, True, "面板是否总在最前（关闭后会被其他窗口盖住）", None),
    K_WINDOW_GEOMETRY: (
        TYPE_TEXT, "", "上次的面板尺寸，如 470x640（只记大小不记位置）", None),
}

DEFAULTS = {key: spec[1] for key, spec in SPECS.items()}


def coerce(key, raw):
    """把字符串解析成该键声明的类型；非法值回退默认值（UC-18 异常流 E2）。"""
    spec = SPECS.get(key)
    if spec is None:
        return raw
    kind, default = spec[0], spec[1]
    if raw is None:
        return default
    try:
        if kind == TYPE_BOOL:
            if isinstance(raw, bool):
                return raw
            return str(raw).strip().lower() in ("1", "true", "yes", "on")
        if kind == TYPE_INT:
            value = int(str(raw).strip())
            if value < 0:
                return default
            return value
        if kind == TYPE_LIST:
            if isinstance(raw, (list, tuple)):
                return [str(x) for x in raw]
            text = str(raw).strip()
            if not text:
                return []
            return [x.strip() for x in text.split(",") if x.strip()]
        if kind == TYPE_ENUM:
            value = str(raw).strip()
            allowed = spec[3]
            if allowed and value not in allowed:
                return default
            return value
        return str(raw)
    except (TypeError, ValueError):
        return default


def serialize(value):
    """类型 -> 数据库字符串。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(x) for x in value)
    return str(value)
