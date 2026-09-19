# -*- coding: utf-8 -*-
"""来源应用识别（F1-5）

记录内容来自哪个进程，用于展示来源名称与按来源筛选。

识别方式：取当前前台窗口的进程可执行文件路径，再取文件名。
必须在**建立了消息循环的线程**上调用（见 win32 模块的线程约定），
且必须在采集的那一刻立刻取——晚一步用户可能已经切到别的窗口了。
"""

import ctypes
import ctypes.wintypes as wt
import os

from app.infrastructure.clipboard import win32 as w32

#: 进程路径查询权限
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

#: 可执行文件名 -> 友好名称。这里只收录常见软件，命中不了就显示文件名，
#: 不影响功能，只是列表里的"来源"更好认。
FRIENDLY_NAMES = {
    "msedge.exe": "Edge 浏览器",
    "chrome.exe": "Chrome 浏览器",
    "firefox.exe": "Firefox 浏览器",
    "explorer.exe": "文件资源管理器",
    "notepad.exe": "记事本",
    "notepad++.exe": "Notepad++",
    "code.exe": "VS Code",
    "pycharm64.exe": "PyCharm",
    "idea64.exe": "IntelliJ IDEA",
    "winword.exe": "Word",
    "excel.exe": "Excel",
    "powerpnt.exe": "PowerPoint",
    "wps.exe": "WPS 文字",
    "et.exe": "WPS 表格",
    "wpp.exe": "WPS 演示",
    "outlook.exe": "Outlook",
    "wechat.exe": "微信",
    "weixin.exe": "微信",
    "qq.exe": "QQ",
    "dingtalk.exe": "钉钉",
    "feishu.exe": "飞书",
    "telegram.exe": "Telegram",
    "windowsterminal.exe": "Windows 终端",
    "cmd.exe": "命令提示符",
    "powershell.exe": "PowerShell",
    "pwsh.exe": "PowerShell",
    "acrobat.exe": "Acrobat",
    "acrord32.exe": "Acrobat Reader",
    "sumatrapdf.exe": "SumatraPDF",
    "mstsc.exe": "远程桌面",
    "devenv.exe": "Visual Studio",
    "java.exe": "Java 程序",
    "python.exe": "Python",
    "pythonw.exe": "Python",
    "snippingtool.exe": "截图工具",
    "screensketch.exe": "截图和草图",
    "mspaint.exe": "画图",
    "searchapp.exe": "Windows 搜索",
    "applicationframehost.exe": "Windows 应用",
}

UNKNOWN_APP = "未知"


class SourceAppInfo(object):
    """一次采集的来源信息快照。"""

    __slots__ = ("app_name", "process_path", "window_title", "hwnd")

    def __init__(self, app_name=UNKNOWN_APP, process_path=None, window_title="", hwnd=0):
        self.app_name = app_name or UNKNOWN_APP
        self.process_path = process_path
        self.window_title = window_title or ""
        self.hwnd = hwnd

    def to_dict(self):
        return {
            "appName": self.app_name,
            "processPath": self.process_path,
            "windowTitle": self.window_title,
            "hwnd": int(self.hwnd or 0),
        }

    def __repr__(self):
        return "<SourceAppInfo %s>" % self.app_name


def get_foreground_window():
    """当前前台窗口句柄。"""
    if not w32.is_windows():
        return 0
    return int(w32.user32.GetForegroundWindow() or 0)


def get_source_app_info(hwnd=None, own_pid=None):
    """识别给定窗口（默认前台窗口）所属的应用。

    识别失败一律返回"未知"，不抛异常（UC-01 业务规则 R4）。
    """
    if not w32.is_windows():
        return SourceAppInfo()
    own_pid = own_pid if own_pid is not None else os.getpid()
    try:
        hwnd = int(hwnd or get_foreground_window() or 0)
        if not hwnd:
            return SourceAppInfo()
        pid = wt.DWORD(0)
        w32.user32.GetWindowThreadProcessId(wt.HWND(hwnd), ctypes.byref(pid))
        if not pid.value:
            return SourceAppInfo(hwnd=hwnd)
        if pid.value == own_pid:
            # 前台窗口就是本程序自己：这次内容变化很可能是我们自己的回声，
            # 来源标成"剪贴板Pro"，便于在历史里一眼看出是自粘贴残留。
            return SourceAppInfo(app_name="剪贴板Pro", hwnd=hwnd)
        path = _query_process_path(pid.value)
        title = _get_window_title(hwnd)
        return SourceAppInfo(
            app_name=friendly_name(path), process_path=path,
            window_title=title, hwnd=hwnd,
        )
    except Exception:  # noqa: BLE001 - 来源识别绝不阻塞采集
        return SourceAppInfo()


def friendly_name(process_path):
    """可执行文件路径 -> 界面展示用的应用名。"""
    if not process_path:
        return UNKNOWN_APP
    base = os.path.basename(process_path)
    return FRIENDLY_NAMES.get(base.lower(), base)


def _query_process_path(pid):
    """用 QueryFullProcessImageNameW 取进程路径。

    用 LIMITED_INFORMATION 权限而不是 VM_READ：对普通权限进程足够，
    对更高完整性级别的进程也不会因权限不足直接失败。
    """
    handle = w32.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        size = wt.DWORD(1024)
        buffer = ctypes.create_unicode_buffer(size.value)
        if w32.kernel32.QueryFullProcessImageNameW(
            handle, 0, buffer, ctypes.byref(size)
        ):
            return buffer.value
    finally:
        w32.kernel32.CloseHandle(handle)
    return None


def _get_window_title(hwnd):
    try:
        length = w32.user32.GetWindowTextLengthW(wt.HWND(hwnd))
        if length <= 0:
            return ""
        buffer = ctypes.create_unicode_buffer(length + 1)
        w32.user32.GetWindowTextW(wt.HWND(hwnd), buffer, length + 1)
        return buffer.value
    except Exception:  # noqa: BLE001
        return ""
