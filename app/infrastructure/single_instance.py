# -*- coding: utf-8 -*-
"""单实例守卫

为什么需要它：程序现在以"无控制台模式"启动（终端关掉也不影响），
用户看不到任何窗口，很容易以为没启动成功而反复双击图标。
每启动一次就会多注册一份全局快捷键与托盘图标，而系统里
RegisterHotKey 是"先到先得"，第二个实例拿不到快捷键却又占着托盘，
表现就是"点了几次之后快捷键时灵时不灵"。

做法：用一个**进程级命名互斥体**（CreateMutexW）。这是 Windows 上
最轻、最可靠的单实例方案——互斥体随进程结束自动释放，不存在
"程序崩了留下锁文件导致再也起不来"的问题。

不联网、不写端口、不需要 IPC：第二个实例发现已有实例后，
只是把自己知道的窗口句柄找回来并尝试激活，然后退出。
"""

import ctypes
import ctypes.wintypes as wt
import os

from app import APP_ID, APP_NAME

ERROR_ALREADY_EXISTS = 183

#: 互斥体名：用 Global\ 前缀保证跨会话可见（同一用户多开也拦得住）
MUTEX_NAME = "Global\\%s.single_instance" % APP_ID


class SingleInstanceGuard(object):
    """持有则说明本进程是唯一实例；未持有则说明已经有实例在跑。"""

    def __init__(self, name=MUTEX_NAME):
        self.name = name
        self.handle = None
        self.already_running = False

    def acquire(self):
        """尝试获取互斥体，返回 True 表示本进程是唯一实例。"""
        if os.name != "nt":
            return True
        kernel32 = ctypes.windll.kernel32
        kernel32.CreateMutexW.restype = wt.HANDLE
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.LPCWSTR]
        handle = kernel32.CreateMutexW(None, False, self.name)
        if not handle:
            # 拿不到互斥体（极罕见）时不要拦住用户，放行启动
            return True
        self.handle = handle
        self.already_running = ctypes.get_last_error() == ERROR_ALREADY_EXISTS or \
            kernel32.GetLastError() == ERROR_ALREADY_EXISTS
        return not self.already_running

    def release(self):
        if self.handle:
            try:
                ctypes.windll.kernel32.CloseHandle(self.handle)
            except Exception:  # noqa: BLE001
                pass
            self.handle = None

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.release()
        return False


def find_existing_panel_window():
    """找出已经运行的实例的面板窗口句柄。

    按窗口标题匹配（标题里带 APP_NAME）。找不到返回 0。
    """
    if os.name != "nt":
        return 0
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def enum_proc(hwnd, _lparam):
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 2)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if APP_NAME in buffer.value:
                found.append(int(hwnd))
                return False
        return True

    user32.EnumWindows(enum_proc, 0)
    return found[0] if found else 0


def activate_existing_instance(logger=None):
    """尝试把已有实例的面板唤到前台。

    注意：跨进程 SetForegroundWindow 大概率会被系统拒绝（这正是
    "焦点不能随便抢"的保护机制），因此这里**不承诺一定能唤起**，
    失败了会返回 False，由调用方提示用户自己按快捷键。
    """
    hwnd = find_existing_panel_window()
    if not hwnd:
        return False
    try:
        from app.infrastructure.clipboard import paste as paste_mod
        return paste_mod.restore_foreground_window(hwnd, logger)
    except Exception:  # noqa: BLE001
        return False


def notify_already_running():
    """用系统消息框告知"已经在运行"（不依赖 tkinter，最早期也能弹）。"""
    text = ("%s 已经在运行了。\n\n"
            "面板已收起或在托盘里：\n"
            "  · 按全局快捷键唤出面板\n"
            "  · 或单击任务栏右下角的托盘图标\n\n"
            "如果没有反应，可能是快捷键被其他软件占用了，"
            "可以从托盘菜单进入设置更换。" % APP_NAME)
    try:
        ctypes.windll.user32.MessageBoxW(
            None, text, "%s 已在运行" % APP_NAME, 0x00000040  # MB_ICONINFORMATION
        )
    except Exception:  # noqa: BLE001
        pass
