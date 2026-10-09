# -*- coding: utf-8 -*-
"""显示器枚举（多屏窗口行为的底座）

只做一件事：把系统里每块显示器的**物理像素**工作区列出来。
贴边停靠、跑出屏幕修复、比例重映射都靠它。

坐标系：全部是虚拟屏幕坐标（主屏左上角为原点，副屏可以是负坐标）。
因为程序启动时已经 ``SetProcessDpiAwarenessContext``，Tk 的窗口坐标与
这里的坐标是同一个空间，可以直接比较——不这么做的话在 125% 缩放下
两个坐标系会差一个缩放倍率，窗口会跑到屏幕外面。
"""

import ctypes
import ctypes.wintypes as wt

#: 显示器信息结构体
MONITOR_DEFAULTTONEAREST = 2


class RECT(ctypes.Structure):
    _fields_ = [("left", wt.LONG), ("top", wt.LONG),
                ("right", wt.LONG), ("bottom", wt.LONG)]


class MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", wt.DWORD), ("rcMonitor", RECT),
                ("rcWork", RECT), ("dwFlags", wt.DWORD)]


def _user32():
    lib = ctypes.WinDLL("user32", use_last_error=True)
    lib.EnumDisplayMonitors.restype = wt.BOOL
    lib.EnumDisplayMonitors.argtypes = [wt.HDC, ctypes.c_void_p,
                                        ctypes.c_void_p, wt.LPARAM]
    lib.GetMonitorInfoW.restype = wt.BOOL
    lib.GetMonitorInfoW.argtypes = [wt.HMONITOR, ctypes.POINTER(MONITORINFO)]
    return lib


try:  # pragma: no cover - 非 Windows 平台直接降级为空列表
    _USER32 = _user32()
except OSError:  # pragma: no cover
    _USER32 = None

#: 一次枚举最多认多少块屏（防御回调被异常数据刷爆）
MAX_MONITORS = 32


def list_monitors():
    """返回全部显示器：``[{x, y, width, height, primary, work...}, ...]``。

    枚举失败时返回空列表，调用方应当退回"用主屏尺寸"的行为——
    **多屏是增强，不是前提**，没有它程序也得能跑。
    """
    if _USER32 is None:
        return []
    monitors = []

    def callback(hmonitor, _hdc, _rect, _data):
        info = MONITORINFO()
        info.cbSize = ctypes.sizeof(MONITORINFO)
        if not _USER32.GetMonitorInfoW(hmonitor, ctypes.byref(info)):
            return True
        rect = info.rcMonitor
        work = info.rcWork
        monitors.append({
            "x": rect.left, "y": rect.top,
            "width": rect.right - rect.left,
            "height": rect.bottom - rect.top,
            # 1 = 主显示器（MONITORINFOF_PRIMARY）
            "primary": bool(info.dwFlags & 1),
            "workX": work.left, "workY": work.top,
            "workWidth": work.right - work.left,
            "workHeight": work.bottom - work.top,
        })
        return len(monitors) < MAX_MONITORS

    proc = ctypes.WINFUNCTYPE(wt.BOOL, wt.HMONITOR, wt.HDC,
                              ctypes.POINTER(RECT), wt.LPARAM)(callback)
    try:
        _USER32.EnumDisplayMonitors(None, None, proc, 0)
    except OSError:  # pragma: no cover
        return []
    # 主屏排在最前，其余按位置从左到右、从上到下——顺序稳定，
    # 否则"第 2 块屏"每次启动指的可能是不同的显示器
    monitors.sort(key=lambda m: (not m["primary"], m["x"], m["y"]))
    return monitors


def virtual_screen(monitors=None):
    """所有显示器合起来的外接矩形。"""
    monitors = monitors if monitors is not None else list_monitors()
    if not monitors:
        return None
    left = min(m["x"] for m in monitors)
    top = min(m["y"] for m in monitors)
    right = max(m["x"] + m["width"] for m in monitors)
    bottom = max(m["y"] + m["height"] for m in monitors)
    return {"x": left, "y": top, "width": right - left, "height": bottom - top}


def monitor_from_point(x, y, monitors=None):
    """点落在哪块屏上；都不在（比如屏幕被拔了）返回最近的一块。"""
    monitors = monitors if monitors is not None else list_monitors()
    if not monitors:
        return None
    for monitor in monitors:
        if (monitor["x"] <= x < monitor["x"] + monitor["width"]
                and monitor["y"] <= y < monitor["y"] + monitor["height"]):
            return monitor
    # 不在任何屏上：选中心点距离最近的
    def distance(monitor):
        cx = monitor["x"] + monitor["width"] / 2.0
        cy = monitor["y"] + monitor["height"] / 2.0
        return (cx - x) ** 2 + (cy - y) ** 2

    return min(monitors, key=distance)


def monitor_index_of(x, y, monitors=None):
    """点在哪块屏上（返回序号）；找不到返回 0。"""
    monitors = monitors if monitors is not None else list_monitors()
    if not monitors:
        return 0
    target = monitor_from_point(x, y, monitors)
    try:
        return monitors.index(target)
    except ValueError:  # pragma: no cover
        return 0
