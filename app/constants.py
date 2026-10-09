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
#: 视频文件单独成一类（TieZ 同款）。存储形态与文件类一样是路径列表，
#: 区别只在类型标记与界面呈现——所以凡是判断"文件类"的地方都要
#: 用 is_file_type() 而不是直接比 "file"。
TYPE_VIDEO = "video"

ALL_TYPES = (TYPE_TEXT, TYPE_RICH_TEXT, TYPE_CODE, TYPE_URL,
             TYPE_IMAGE, TYPE_FILE, TYPE_VIDEO)
TEXT_TYPES = (TYPE_TEXT, TYPE_RICH_TEXT, TYPE_CODE, TYPE_URL)
#: 路径列表形态的类型（粘贴时走 CF_HDROP、可以"打开所在文件夹"）
FILE_TYPES = (TYPE_FILE, TYPE_VIDEO)

# 类型中文名：界面标签与提示文案统一从这里取
TYPE_LABELS = {
    TYPE_TEXT: "文本",
    TYPE_RICH_TEXT: "富文本",
    TYPE_CODE: "代码",
    TYPE_URL: "网址",
    TYPE_IMAGE: "图片",
    TYPE_FILE: "文件",
    TYPE_VIDEO: "视频",
}

#: 视频扩展名。判"这批文件是不是视频"时用。
VIDEO_EXTENSIONS = (
    ".mp4", ".mkv", ".avi", ".mov", ".wmv", ".flv", ".webm", ".m4v",
    ".mpg", ".mpeg", ".ts", ".3gp", ".rmvb", ".rm", ".vob", ".ogv",
)


def is_text_type(content_type):
    """该类型是否为可读文本类型（决定能否编辑、按正文检索）。"""
    return content_type in TEXT_TYPES


def is_file_type(content_type):
    """该类型的内容是不是**磁盘路径列表**。

    文件与视频在存储上完全一样，判断时要用这个而不是直接比 ``"file"``，
    否则新增类型时很容易漏掉某个分支。
    """
    return content_type in FILE_TYPES


def looks_like_video(paths):
    """这批路径是不是视频。**全部**是视频才算，混合的不算。"""
    items = [p for p in (paths or []) if str(p).strip()]
    if not items:
        return False
    for path in items:
        name = str(path).strip().strip('"').lower()
        # 允许带查询串的 URL（有些视频源是链接）
        name = name.split("?", 1)[0]
        if not name.endswith(VIDEO_EXTENSIONS):
            return False
    return True


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
#: 应用级规则（JSON）：按来源应用"忽略采集"或"自动清洗"
K_APP_CLEANUP_POLICIES = "app.app_cleanup_policies"
K_PRIVACY_PROTECTION = "app.privacy_protection"
K_PRIVACY_KINDS = "app.privacy_protection_kinds"
#: 用户自定义的敏感识别规则，每行一条正则
K_PRIVACY_CUSTOM_RULES = "app.privacy_protection_custom_rules"
#: 脱敏时前后各保留几位可见（TieZ 的掩码设置）
K_MASK_PREFIX = "app.sensitive_mask_prefix_visible"
K_MASK_SUFFIX = "app.sensitive_mask_suffix_visible"
#: 邮箱脱敏时是否连域名一起打码
K_MASK_EMAIL_DOMAIN = "app.sensitive_mask_email_domain"
K_THEME = "app.theme"
K_THEME_MODE = "app.theme_mode"
K_LIST_DENSITY = "app.list_density"
K_MAIN_HOTKEY = "app.main_hotkey"
K_SEARCH_HOTKEY = "app.search_hotkey"
#: 顺序粘贴模式：开启后每复制一条就自动进队，"顺序粘贴"快捷键逐条粘出
K_SEQUENTIAL_MODE = "app.sequential_mode"
#: 顺序粘贴快捷键（只在顺序模式开启时注册）
K_SEQUENTIAL_HOTKEY = "app.sequential_hotkey"
#: 带格式粘贴快捷键：不开面板，直接把最新一条带格式粘到当前窗口
K_RICH_PASTE_HOTKEY = "app.rich_paste_hotkey"

#: 内置标签。它们关联着"敏感信息保护"，因此**禁止改名与删除**：
#: 改了名之后新采集的敏感内容就不会再被打上标记，保护会静默失效。
PROTECTED_TAGS = ("sensitive", "密码", "password")

#: 给记录打上这几个标签，就等同于把它标记为敏感 —— 于是加密落盘。
#: 与 PROTECTED_TAGS 是同一组：受保护的标签也正是"触发保护"的标签。
SENSITIVE_TAGS = PROTECTED_TAGS


def is_protected_tag(tag):
    """是不是内置保护标签（大小写不敏感）。"""
    return bool(tag) and str(tag).strip().lower() in PROTECTED_TAGS


def is_sensitive_tag(tag):
    """这个标签是否意味着"这条记录该加密"。"""
    return bool(tag) and str(tag).strip().lower() in SENSITIVE_TAGS
K_EDGE_DOCKING = "app.edge_docking"
K_FOLLOW_MOUSE = "app.follow_mouse"
#: 失焦时不要自动隐藏（默认关：多数人希望点别处面板就收起来）
K_IGNORE_BLUR = "app.ignore_blur"
#: 窗口固定：一直留在最前且失焦也不隐藏（临时查资料时用）
K_WINDOW_PINNED = "app.window_pinned"
#: 是否显示托盘图标
K_TRAY_VISIBLE = "app.tray_visible"
#: 方向键是否参与列表选择
K_ARROW_KEY_SELECTION = "app.arrow_key_selection"
#: 方向键的选择模式：list（列表上下）/ grid（卡片网格）/ none（关闭）
K_NAVIGATION_MODE = "app.navigation_mode"

#: 是否开启悬停预览浮窗
K_PREVIEW_ENABLED = "app.preview_enabled"
#: 悬停多久才弹出（毫秒）。太短会"鼠标划过就闪一下"，太长又显得迟钝
K_PREVIEW_DELAY_MS = "app.preview_delay_ms"

#: 预览浮窗最多显示多少字符（超出部分明确标注已截断）
PREVIEW_MAX_CHARS = 4000

K_SILENT_START = "app.silent_start"
K_SOUND_ENABLED = "app.sound_enabled"
#: 提示音音量（0~100）
K_SOUND_VOLUME = "app.sound_volume"
#: 粘贴时是否也响（复制音与粘贴音可以分开关）
K_PASTE_SOUND_ENABLED = "app.paste_sound_enabled"
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
