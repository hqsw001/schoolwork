# -*- coding: utf-8 -*-
"""全局常量：内容类型、错误码、事件名、默认配置键。

这些常量是冻结项（见《00-项目概况》3.1 节）：前端与后端并行开发时，
双方都以本文件的取值为准，不得各写一套字面量。
"""

# --------------------------------------------------------------------------
# 内容类型：六类（《需求分析报告》6.1.4 F4-1）
# --------------------------------------------------------------------------
TYPE_TEXT = "text"
TYPE_RICH_TEXT = "rich_text"
TYPE_CODE = "code"
TYPE_URL = "url"
TYPE_IMAGE = "image"
TYPE_FILE = "file"

ALL_TYPES = (TYPE_TEXT, TYPE_RICH_TEXT, TYPE_CODE, TYPE_URL, TYPE_IMAGE, TYPE_FILE)
TEXT_TYPES = (TYPE_TEXT, TYPE_RICH_TEXT, TYPE_CODE, TYPE_URL)

# 类型中文名：界面标签与提示文案统一从这里取
TYPE_LABELS = {
    TYPE_TEXT: "文本",
    TYPE_RICH_TEXT: "富文本",
    TYPE_CODE: "代码",
    TYPE_URL: "网址",
    TYPE_IMAGE: "图片",
    TYPE_FILE: "文件",
}


def is_text_type(content_type):
    """该类型是否为可读文本类型（决定能否编辑、按正文检索）。"""
    return content_type in TEXT_TYPES


# --------------------------------------------------------------------------
# 统一返回格式的错误码（《需求分析报告》7.3）
# --------------------------------------------------------------------------
ERR_INVALID_ARG = 4001   # 参数非法
ERR_NOT_FOUND = 4004     # 资源不存在
ERR_CONFLICT = 4009      # 数据冲突
ERR_INTERNAL = 5000      # 内部错误

ERROR_MESSAGES = {
    ERR_INVALID_ARG: "参数非法",
    ERR_NOT_FOUND: "资源不存在",
    ERR_CONFLICT: "数据冲突",
    ERR_INTERNAL: "内部错误",
}

# --------------------------------------------------------------------------
# 事件名（《需求分析报告》7.4）：内核 → 界面单向推送
# --------------------------------------------------------------------------
EVT_CLIPBOARD_UPDATED = "clipboard-updated"
EVT_CLIPBOARD_REMOVED = "clipboard-removed"
EVT_LISTEN_STATUS_CHANGED = "listen-status-changed"
EVT_THEME_CHANGED = "theme-changed"
EVT_TOAST = "toast"

# --------------------------------------------------------------------------
# 提示类型（toast 事件的载荷）
# --------------------------------------------------------------------------
TOAST_INFO = "info"
TOAST_SUCCESS = "success"
TOAST_WARNING = "warning"
TOAST_ERROR = "error"

# --------------------------------------------------------------------------
# 配置键（《需求分析报告》7.2）
# --------------------------------------------------------------------------
K_PERSISTENT = "app.persistent"
K_PERSISTENT_LIMIT_ENABLED = "app.persistent_limit_enabled"
K_PERSISTENT_LIMIT = "app.persistent_limit"
K_DEDUPLICATE = "app.deduplicate"
K_CAPTURE_RICH_TEXT = "app.capture_rich_text"
K_CAPTURE_FILES = "app.capture_files"
K_DELETE_AFTER_PASTE = "app.delete_after_paste"
K_CLEANUP_RULES = "app.cleanup_rules"
K_PRIVACY_PROTECTION = "app.privacy_protection"
K_PRIVACY_KINDS = "app.privacy_protection_kinds"
K_THEME = "app.theme"
K_THEME_MODE = "app.theme_mode"
K_LIST_DENSITY = "app.list_density"
K_MAIN_HOTKEY = "app.main_hotkey"
K_SEARCH_HOTKEY = "app.search_hotkey"
K_EDGE_DOCKING = "app.edge_docking"
K_FOLLOW_MOUSE = "app.follow_mouse"
K_SILENT_START = "app.silent_start"
K_SOUND_ENABLED = "app.sound_enabled"
#: 面板是否总在最前。关掉之后面板会像普通窗口一样被其他窗口盖住，
#: 适合"想一边看资料一边翻剪贴板"的用法。
K_ALWAYS_ON_TOP = "app.always_on_top"
#: 上次的面板尺寸，格式 "宽x高"。只记大小不记位置，理由见 panel._restore_geometry
K_WINDOW_GEOMETRY = "app.window_geometry"

# --------------------------------------------------------------------------
# 主题与列表密度（本期只落地默认主题的完整令牌，其余主题留占位）
# --------------------------------------------------------------------------
THEME_MICA = "mica"
THEME_3D = "3d"
THEME_GLASS = "glass"
THEME_STICKY = "sticky"
THEME_BOOK = "book"
THEME_SAKURA = "sakura"

ALL_THEMES = (THEME_MICA, THEME_3D, THEME_GLASS, THEME_STICKY, THEME_BOOK, THEME_SAKURA)
THEME_LABELS = {
    THEME_MICA: "云母",
    THEME_3D: "3D 复古",
    THEME_GLASS: "毛玻璃",
    THEME_STICKY: "便利贴",
    THEME_BOOK: "纸质书感",
    THEME_SAKURA: "樱花",
}

MODE_LIGHT = "light"
MODE_DARK = "dark"
MODE_SYSTEM = "system"

DENSITY_COMPACT = "compact"
DENSITY_NORMAL = "normal"
DENSITY_LOOSE = "loose"

# --------------------------------------------------------------------------
# 业务规则常量
# --------------------------------------------------------------------------
#: 回声抑制时间窗（UC-09 R2）：写入标记后 10 秒内的相同指纹判定为自身回声
ECHO_WINDOW_MS = 10_000

#: 存储上限最小值（UC-04 R2）
MIN_STORAGE_LIMIT = 10

#: 预览文本最大长度（UC-02 主事件流第 6 步）
PREVIEW_MAX_LEN = 500

#: 超大内容截断阈值（UC-01 备选流 A2）
MAX_CONTENT_LEN = 100_000

#: 列表默认分页大小（UC-20 主事件流第 6 步）
PAGE_SIZE = 50

#: 检索结果条数上限（UC-08 主事件流第 10 步）
SEARCH_RESULT_LIMIT = 200

#: 标签名最大长度（UC-11 主事件流第 4 步）
TAG_NAME_MAX_LEN = 20

#: 预置标签（UC-11 备选流 A3）
PRESET_TAGS = ("重要", "待用", "密码")
