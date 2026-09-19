# -*- coding: utf-8 -*-
"""Win32 API 绑定（ctypes）

为什么用 ctypes 而不是 pywin32：本机 pywin32 没有导出
``AddClipboardFormatListener`` / ``RegisterClipboardFormat`` 之外的监听入口，
而"事件驱动、不轮询"是 UC-01 业务规则 R1 的硬要求。ctypes 直接绑定
user32.dll / kernel32.dll，零额外依赖，也便于把每个调用的参数类型写清楚。

线程约定（最容易踩坑的地方）：
    - ``GetForegroundWindow`` / ``SetForegroundWindow`` / ``RegisterHotKey``
      都必须在**已经建立消息循环的那个线程**上调用，否则行为未定义；
      因此监听线程负责建消息窗口，来源应用识别与快捷键注册都在该线程上完成。
    - ``SendInput`` 可以在任意线程调用，但发键前必须先把自己的窗口隐藏，
      否则按键会打到本程序自己的窗口里（UC-09 业务规则 R4）。
"""

import ctypes
import ctypes.wintypes as wt
import sys

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
else:  # pragma: no cover - 本项目仅面向 Windows，非 Windows 下给出明确报错
    user32 = kernel32 = shell32 = None


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
WM_CLIPBOARDUPDATE = 0x031D
WM_DESTROY = 0x0002
WM_HOTKEY = 0x0312
WM_APP = 0x8000
WM_TRAYICON = WM_APP + 1
WM_LBUTTONUP = 0x0202
WM_LBUTTONDBLCLK = 0x0203
WM_RBUTTONUP = 0x0205
WM_CONTEXTMENU = 0x007B

HWND_MESSAGE = -3

#: SendInput 的 cbSize 必须等于 Win32 头文件里 INPUT 结构的大小，
#: 这个大小由**最大的联合成员 MOUSEINPUT**（32 字节）决定，全部 64 位
#: 字段对齐后为 40。用 ctypes.sizeof(INPUT) 得到的是 32（Python 这边
#: 只实例化了 KEYBDINPUT 成员，自然对齐后更小），传 32 会被 Windows
#: 以 ERROR_INVALID_PARAMETER(87) 拒绝，**一个按键都发不出去**。
#: 这个坑实测踩过，所以把常量写死在这里而不是现算。
INPUT_SIZE_X64 = 40
INPUT_SIZE_X86 = 28

# ShowWindow 命令
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_SHOWMAXIMIZED = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_RESTORE = 9

# RegisterHotKey 修饰键
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# 虚拟键
VK_CONTROL = 0x11
VK_SHIFT = 0x10
VK_MENU = 0x12      # Alt
VK_LWIN = 0x5B
VK_ESCAPE = 0x1B
VK_RETURN = 0x0D
VK_V = 0x56
VK_F = 0x46
VK_DELETE = 0x2E
VK_UP = 0x26
VK_DOWN = 0x28
VK_P = 0x50

# SendInput
INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004

# 剪贴板格式
CF_TEXT = 1
CF_BITMAP = 2
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIBV5 = 17

# 剪贴板打开重试
OPEN_CLIPBOARD_RETRIES = 3
OPEN_CLIPBOARD_DELAY = 0.05

# 托盘图标
NIM_ADD = 0
NIM_MODIFY = 1
NIM_DELETE = 2
NIF_MESSAGE = 0x01
NIF_ICON = 0x02
NIF_TIP = 0x04
IDI_APPLICATION = 32512

# 弹出菜单
MF_STRING = 0x00000000
MF_SEPARATOR = 0x00000800
TPM_LEFTALIGN = 0x0000
TPM_RIGHTBUTTON = 0x0002
TPM_RETURNCMD = 0x0100


# ---------------------------------------------------------------------------
# 结构体
# ---------------------------------------------------------------------------
if IS_WINDOWS:
    LRESULT = ctypes.c_ssize_t
    WNDPROCTYPE = ctypes.WINFUNCTYPE(
        LRESULT, wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM
    )
    #: EnumWindows / EnumChildWindows 的回调签名是
    #: (HWND, LPARAM) -> BOOL，和窗口过程不是一回事，必须分开定义
    WNDENUMPROC = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", ctypes.c_uint),
            ("lpfnWndProc", WNDPROCTYPE),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wt.HINSTANCE),
            ("hIcon", wt.HICON),
            ("hCursor", wt.HANDLE),
            ("hbrBackground", wt.HBRUSH),
            ("lpszMenuName", wt.LPCWSTR),
            ("lpszClassName", wt.LPCWSTR),
        ]

    class POINT(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    class MSG(ctypes.Structure):
        _fields_ = [
            ("hwnd", wt.HWND),
            ("message", ctypes.c_uint),
            ("wParam", wt.WPARAM),
            ("lParam", wt.LPARAM),
            ("time", wt.DWORD),
            ("pt", POINT),
        ]

    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", wt.WORD),
            ("wScan", wt.WORD),
            ("dwFlags", wt.DWORD),
            ("time", wt.DWORD),
            ("dwExtraInfo", ULONG_PTR),
        ]

    class INPUT(ctypes.Structure):
        class _INPUTUNION(ctypes.Union):
            _fields_ = [("ki", KEYBDINPUT)]

        _anonymous_ = ("u",)
        _fields_ = [("type", wt.DWORD), ("u", _INPUTUNION)]

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wt.DWORD),
            ("hWnd", wt.HWND),
            ("uID", ctypes.c_uint),
            ("uFlags", ctypes.c_uint),
            ("uCallbackMessage", ctypes.c_uint),
            ("hIcon", wt.HICON),
            ("szTip", wt.WCHAR * 128),
            ("dwState", wt.DWORD),
            ("dwStateMask", wt.DWORD),
            ("szInfo", wt.WCHAR * 256),
            ("uVersion", ctypes.c_uint),
            ("szInfoTitle", wt.WCHAR * 64),
            ("dwInfoFlags", wt.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wt.HICON),
        ]

    # ---------------------------------------------------------------- 原型
    user32.DefWindowProcW.restype = LRESULT
    user32.DefWindowProcW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]

    user32.RegisterClassW.restype = wt.ATOM
    user32.RegisterClassW.argtypes = [ctypes.POINTER(WNDCLASSW)]

    user32.CreateWindowExW.restype = wt.HWND
    user32.CreateWindowExW.argtypes = [
        wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD,
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        wt.HWND, wt.HMENU, wt.HINSTANCE, wt.LPVOID,
    ]

    user32.DestroyWindow.restype = wt.BOOL
    user32.DestroyWindow.argtypes = [wt.HWND]

    user32.AddClipboardFormatListener.restype = wt.BOOL
    user32.AddClipboardFormatListener.argtypes = [wt.HWND]

    user32.RemoveClipboardFormatListener.restype = wt.BOOL
    user32.RemoveClipboardFormatListener.argtypes = [wt.HWND]

    user32.GetMessageW.restype = ctypes.c_int
    user32.GetMessageW.argtypes = [ctypes.POINTER(MSG), wt.HWND, ctypes.c_uint, ctypes.c_uint]

    user32.PeekMessageW.restype = wt.BOOL
    user32.PeekMessageW.argtypes = [
        ctypes.POINTER(MSG), wt.HWND, ctypes.c_uint, ctypes.c_uint, ctypes.c_uint
    ]

    user32.TranslateMessage.restype = wt.BOOL
    user32.TranslateMessage.argtypes = [ctypes.POINTER(MSG)]

    user32.DispatchMessageW.restype = LRESULT
    user32.DispatchMessageW.argtypes = [ctypes.POINTER(MSG)]

    user32.PostMessageW.restype = wt.BOOL
    user32.PostMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]

    user32.PostQuitMessage.restype = None
    user32.PostQuitMessage.argtypes = [ctypes.c_int]

    user32.RegisterHotKey.restype = wt.BOOL
    user32.RegisterHotKey.argtypes = [wt.HWND, ctypes.c_int, ctypes.c_uint, ctypes.c_uint]

    user32.UnregisterHotKey.restype = wt.BOOL
    user32.UnregisterHotKey.argtypes = [wt.HWND, ctypes.c_int]

    user32.GetForegroundWindow.restype = wt.HWND
    user32.GetForegroundWindow.argtypes = []

    user32.SetForegroundWindow.restype = wt.BOOL
    user32.SetForegroundWindow.argtypes = [wt.HWND]

    user32.ShowWindow.restype = wt.BOOL
    user32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]

    user32.IsWindow.restype = wt.BOOL
    user32.IsWindow.argtypes = [wt.HWND]

    user32.GetCursorPos.restype = wt.BOOL
    user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]

    user32.SendInput.restype = ctypes.c_uint
    user32.SendInput.argtypes = [ctypes.c_uint, ctypes.POINTER(INPUT), ctypes.c_int]

    #: 焦点交还的最后手段：先补一次"用户按过 Alt"的动作，
    #: 让系统认为本进程刚收到过用户输入，SetForegroundWindow 才会放行
    user32.keybd_event.restype = None
    user32.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wt.DWORD, ULONG_PTR]

    user32.GetWindowThreadProcessId.restype = wt.DWORD
    user32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]

    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextLengthW.argtypes = [wt.HWND]

    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]

    user32.AttachThreadInput.restype = wt.BOOL
    user32.AttachThreadInput.argtypes = [wt.DWORD, wt.DWORD, wt.BOOL]

    user32.GetClassNameW.restype = ctypes.c_int
    user32.GetClassNameW.argtypes = [wt.HWND, wt.LPWSTR, ctypes.c_int]

    user32.EnumWindows.restype = wt.BOOL
    user32.EnumWindows.argtypes = [WNDENUMPROC, wt.LPARAM]

    user32.EnumChildWindows.restype = wt.BOOL
    user32.EnumChildWindows.argtypes = [wt.HWND, WNDENUMPROC, wt.LPARAM]

    user32.IsWindowVisible.restype = wt.BOOL
    user32.IsWindowVisible.argtypes = [wt.HWND]

    user32.SendMessageW.restype = LRESULT
    user32.SendMessageW.argtypes = [wt.HWND, ctypes.c_uint, wt.WPARAM, wt.LPARAM]

    kernel32.GetModuleHandleW.restype = wt.HMODULE
    kernel32.GetModuleHandleW.argtypes = [wt.LPCWSTR]

    kernel32.OpenProcess.restype = wt.HANDLE
    kernel32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]

    kernel32.CloseHandle.restype = wt.BOOL
    kernel32.CloseHandle.argtypes = [wt.HANDLE]

    kernel32.QueryFullProcessImageNameW.restype = wt.BOOL
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wt.HANDLE, wt.DWORD, wt.LPWSTR, ctypes.POINTER(wt.DWORD)
    ]

    shell32.Shell_NotifyIconW.restype = wt.BOOL
    shell32.Shell_NotifyIconW.argtypes = [wt.DWORD, ctypes.POINTER(NOTIFYICONDATAW)]

    user32.LoadIconW.restype = wt.HICON
    user32.LoadIconW.argtypes = [wt.HINSTANCE, wt.LPCWSTR]

    user32.CreatePopupMenu.restype = wt.HMENU
    user32.CreatePopupMenu.argtypes = []

    # 注意：AppendMenuW 的最后一个参数是"字符串指针或菜单句柄"，
    # 分隔符时必须传 NULL，因此按 c_void_p 绑定，由调用方自己准备缓冲。
    user32.AppendMenuW.restype = wt.BOOL
    user32.AppendMenuW.argtypes = [wt.HMENU, ctypes.c_uint, ctypes.c_size_t, ctypes.c_void_p]

    user32.DestroyMenu.restype = wt.BOOL
    user32.DestroyMenu.argtypes = [wt.HMENU]

    # 带 TPM_RETURNCMD 时返回值是"被选中的菜单项 ID"（0 表示没有选择），
    # 因此返回类型必须是整数而不是 BOOL
    user32.TrackPopupMenu.restype = ctypes.c_int
    user32.TrackPopupMenu.argtypes = [
        wt.HMENU, ctypes.c_uint, ctypes.c_int, ctypes.c_int,
        ctypes.c_int, wt.HWND, ctypes.c_void_p,
    ]

    user32.SetMenuDefaultItem.restype = wt.BOOL
    user32.SetMenuDefaultItem.argtypes = [wt.HMENU, ctypes.c_uint, ctypes.c_uint]


def append_menu(menu, flags, item_id, text):
    """向弹出菜单追加一项；text 为 None 时按分隔符处理。

    自己维护一份宽字符缓冲区并保持引用，避免 ctypes 传参后缓冲被回收
    （这是 ctypes 调 Win32 菜单 API 最常见的崩溃原因）。
    """
    if text is None:
        return bool(user32.AppendMenuW(menu, flags, item_id, None))
    buffer = ctypes.create_unicode_buffer(text)
    ok = bool(user32.AppendMenuW(menu, flags, item_id, ctypes.cast(buffer, ctypes.c_void_p)))
    _MENU_TEXT_BUFFERS.append(buffer)
    # 菜单项文本不多，缓冲不会无限增长；这里做个软上限
    if len(_MENU_TEXT_BUFFERS) > 64:
        del _MENU_TEXT_BUFFERS[:32]
    return ok


_MENU_TEXT_BUFFERS = []


# ---------------------------------------------------------------------------
# 便捷函数
# ---------------------------------------------------------------------------
def is_windows():
    return IS_WINDOWS


def input_struct_size():
    """SendInput 需要的 cbSize（见 INPUT_SIZE_X64 的说明）。"""
    import struct as _struct
    return INPUT_SIZE_X64 if _struct.calcsize("P") == 8 else INPUT_SIZE_X86


def last_error():
    return ctypes.get_last_error()


def set_dpi_aware():
    """声明进程 DPI 感知，避免高分屏下面板与字体发虚。

    必须在创建任何窗口之前调用。失败不致命：老系统没有这个 API 时
    界面会由系统拉伸，功能不受影响。
    """
    if not IS_WINDOWS:
        return False
    try:
        # -4 = DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2
        return bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    except AttributeError:
        try:
            return bool(ctypes.WinDLL("shcore").SetProcessDpiAwareness(2) == 0)
        except Exception:
            try:
                return bool(user32.SetProcessDPIAware())
            except Exception:
                return False


def get_cursor_pos():
    pt = POINT()
    if user32.GetCursorPos(ctypes.byref(pt)):
        return pt.x, pt.y
    return 0, 0
