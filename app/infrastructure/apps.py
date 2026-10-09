# -*- coding: utf-8 -*-
"""关联应用：系统默认程序、按扩展名查"打开方式"列表、已安装应用扫描

对应 TieZ 的 ``infrastructure/windows_api/apps.rs``。用途是"默认应用"设置页——
让用户给文本/图片/视频等类型各指定一个打开程序。

三条数据来源，可靠性依次下降，都要处理取不到的情况：

1. ``AssocQueryStringW``（shlwapi）：系统为某个扩展名登记的友好名与可执行文件。
2. 注册表 ``HKCU\\...\\FileExts\\<ext>\\OpenWithList``：用户"打开方式"用过的程序。
3. ``Get-StartApps``（PowerShell）：开始菜单里能搜到的应用，含 UWP。

所有函数失败都返回空值，不抛异常——设置页拿不到数据应当显示"未配置"，
而不是弹一个错误框。
"""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess

#: 内容类型 → 用来查默认程序的扩展名（与 TieZ 的映射一致）
CONTENT_EXTENSIONS = {
    "image": ".png",
    "video": ".mp4",
    "code": ".txt",
    "text": ".txt",
    "file": ".txt",
    "rich_text": ".html",
    "url": ".html",     # 网址没有扩展名，退回 html 关联
    "link": ".html",
}

#: AssocQueryString 常量
ASSOCF_NONE = 0x00000000
ASSOCF_VERIFY = 0x00000040
ASSOCSTR_EXECUTABLE = 2
ASSOCSTR_FRIENDLYAPPNAME = 4

#: 扫描结果里要过滤掉的噪声（安装器、卸载器、帮助文档之类）
SKIP_KEYWORDS = (
    "uninstall", "卸载", "setup", "install", "config", "help",
    "readme", "update", "修复", "remove", "debug", "crash",
)


def _shlwapi():
    lib = ctypes.WinDLL("shlwapi", use_last_error=True)
    lib.AssocQueryStringW.restype = ctypes.c_long
    lib.AssocQueryStringW.argtypes = [
        ctypes.c_uint, ctypes.c_uint, wt.LPCWSTR, wt.LPCWSTR,
        wt.LPWSTR, ctypes.POINTER(wt.DWORD),
    ]
    return lib


try:  # pragma: no cover
    _SHLWAPI = _shlwapi()
except OSError:  # pragma: no cover
    _SHLWAPI = None


def _assoc_query(assoc, extra, assoc_str):
    """调用 AssocQueryStringW，返回字符串或 None。"""
    if _SHLWAPI is None:
        return None
    length = wt.DWORD(0)
    # 第一次调用只为拿长度（pszOut 传 NULL）
    _SHLWAPI.AssocQueryStringW(ASSOCF_VERIFY, assoc_str, assoc, extra, None,
                               ctypes.byref(length))
    if not length.value:
        return None
    buffer = ctypes.create_unicode_buffer(length.value + 1)
    result = _SHLWAPI.AssocQueryStringW(ASSOCF_VERIFY, assoc_str, assoc, extra,
                                        buffer, ctypes.byref(length))
    if result != 0:
        return None
    return buffer.value or None


def system_default_app(content_type):
    """查某内容类型的系统默认程序。返回 {"friendlyName", "executable", "extension"}。"""
    extension = CONTENT_EXTENSIONS.get(content_type, ".txt")
    assoc = "http" if content_type in ("url", "link") else extension
    executable = _assoc_query(assoc, None, ASSOCSTR_EXECUTABLE)
    friendly = _assoc_query(assoc, None, ASSOCSTR_FRIENDLYAPPNAME)
    if not friendly and executable:
        friendly = os.path.splitext(os.path.basename(executable))[0]
    return {
        "extension": extension,
        "friendlyName": friendly or "系统默认",
        "executable": executable or "",
        "configured": bool(executable),
    }


def _resolve_exe_path(name):
    """把 exe 文件名解析成完整路径（先 Get-Command 那种，再 App Paths）。"""
    if not name:
        return ""
    if os.path.isabs(name) and os.path.isfile(name):
        return name
    if not name.lower().endswith(".exe"):
        name += ".exe"
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return ""
    try:
        with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\%s" % name) as key:
            return winreg.QueryValueEx(key, None)[0] or ""
    except OSError:
        return ""


def _looks_like_app_id(value):
    """判断 OpenWithList 里的值是不是真的程序标识。

    注册表里会残留垃圾值——实测本机 ``.txt`` 的 OpenWithList 里就有
    ``b = "ab"`` 和 ``c = "adebc"``（像是 MRUList 写坏留下的）。
    真正的值只有三种形态：``xxx.exe``、UWP 的 ``包名!App``、完整路径。
    """
    if not value:
        return False
    lowered = value.lower()
    return (lowered.endswith(".exe") or "!" in value
            or "\\" in value or "/" in value)


def _friendly_name(value):
    """把程序标识变成好读的名字。"""
    if "!" in value:                      # UWP AppID：Microsoft.WindowsNotepad_xxx!App
        head = value.split("!")[0].split("_")[0]
        return head.split(".")[-1] or value
    return os.path.splitext(os.path.basename(value))[0]


def associated_apps(extension, limit=12):
    """列出"打开方式"里用过的程序（HKCU 的 OpenWithList）。"""
    if not extension:
        return []
    if not extension.startswith("."):
        extension = "." + extension
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return []
    out = []
    seen = set()
    try:
        with winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Explorer"
                r"\FileExts\%s\OpenWithList" % extension) as key:
            index = 0
            while index < 64:
                try:
                    name, value, _kind = winreg.EnumValue(key, index)
                except OSError:
                    break
                index += 1
                # MRUList 是排序索引（"bac" 这种），不是程序名
                if name.lower() == "mrulist" or not isinstance(value, str):
                    continue
                if not _looks_like_app_id(value):
                    continue
                lowered = value.lower()
                if lowered in seen:
                    continue
                seen.add(lowered)
                out.append({
                    "name": _friendly_name(value),
                    "executable": _resolve_exe_path(value),
                    "source": "openWithList",
                })
    except OSError:
        return []
    return out[:limit]


def _run_powershell(script, timeout=25):
    """跑一段 PowerShell 并返回 stdout（失败返回空串）。"""
    try:
        completed = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
             "-Command", "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
            capture_output=True, timeout=timeout,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if completed.returncode != 0:
        return ""
    return completed.stdout.decode("utf-8", "replace").strip()


def scan_installed_apps(limit=200):
    """扫描"已安装应用"（开始菜单能搜到的 + 几个系统常用程序）。

    走 PowerShell 的 ``Get-StartApps``，启动一次约 1 秒，因此**只在设置页
    按需调用**，绝不放启动路径上。
    """
    import json

    apps = []
    seen = set()

    def add(name, path, source):
        name = (name or "").strip()
        if not name or not path:
            return
        lowered = name.lower()
        if lowered in seen:
            return
        if any(word in lowered for word in SKIP_KEYWORDS):
            return
        seen.add(lowered)
        apps.append({"name": name, "executable": path, "source": source})

    raw = _run_powershell(
        "Get-StartApps | Select-Object Name,AppID | ConvertTo-Json -Compress")
    if raw:
        try:
            parsed = json.loads(raw)
        except ValueError:
            parsed = []
        if isinstance(parsed, dict):
            parsed = [parsed]
        for item in parsed if isinstance(parsed, list) else []:
            if isinstance(item, dict):
                add(item.get("Name"), item.get("AppID"), "startApps")

    # 系统自带的常用程序不一定会出现在 Get-StartApps 里
    system_root = os.environ.get("SystemRoot", r"C:\Windows")
    builtin = [
        ("记事本", os.path.join(system_root, "System32", "notepad.exe")),
        ("画图", os.path.join(system_root, "System32", "mspaint.exe")),
        ("计算器", os.path.join(system_root, "System32", "calc.exe")),
        ("命令提示符", os.path.join(system_root, "System32", "cmd.exe")),
        ("截图工具", os.path.join(system_root, "System32", "SnippingTool.exe")),
        ("PowerShell", os.path.join(
            system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe")),
        ("注册表编辑器", os.path.join(system_root, "regedit.exe")),
        ("文件资源管理器", os.path.join(system_root, "explorer.exe")),
    ]
    for name, path in builtin:
        if os.path.isfile(path):
            add(name, path, "builtin")

    apps.sort(key=lambda item: item["name"].lower())
    return apps[:limit]
