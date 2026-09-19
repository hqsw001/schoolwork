# -*- coding: utf-8 -*-
"""剪贴板基础设施导出。"""

from app.infrastructure.clipboard.listener import (  # noqa: F401
    ACTION_MAIN_PANEL,
    ACTION_SEARCH,
    ACTION_TOGGLE_LISTEN,
    EVENT_CLIPBOARD_UPDATE,
    EVENT_ERROR,
    EVENT_HOTKEY,
    EVENT_STARTED,
    EVENT_TRAY,
    ClipboardListener,
)
from app.infrastructure.clipboard.source_app import (  # noqa: F401
    SourceAppInfo,
    get_foreground_window,
    get_source_app_info,
)

__all__ = [
    "ACTION_MAIN_PANEL",
    "ACTION_SEARCH",
    "ACTION_TOGGLE_LISTEN",
    "ClipboardListener",
    "EVENT_CLIPBOARD_UPDATE",
    "EVENT_ERROR",
    "EVENT_HOTKEY",
    "EVENT_STARTED",
    "EVENT_TRAY",
    "SourceAppInfo",
    "get_foreground_window",
    "get_source_app_info",
]
