# -*- coding: utf-8 -*-
"""粘贴执行与快捷键加速器解析（UC-09 粘贴 / UC-16 快捷键）

这是整个项目最容易出问题的地方，因此把规则写死在这里：

    顺序（UC-09 业务规则 R1）：① 写粘贴标记 → ② 写剪贴板 → ③ 模拟按键
    焦点交还（UC-09 业务规则 R4）：面板必须先把焦点还给用户原本在用的窗口，
        否则 Ctrl+V 会打在本程序自己的窗口里，这是最影响体验的缺陷。

模拟按键用 SendInput 而不是 keybd_event：SendInput 是当前推荐接口，
并且一次提交整组按键，能减少"按下 Ctrl 之后目标应用还没反应过来"的竞态。
"""

import ctypes
import time

from app.constants import ERR_INVALID_ARG, ERR_INTERNAL
from app.errors import AppError
from app.infrastructure.clipboard import win32 as w32

try:
    import win32con
except ImportError:  # pragma: no cover
    win32con = None

#: 面板隐藏后等待焦点交还的时长（毫秒）
FOCUS_SETTLE_MS = 60
#: 模拟粘贴前的等待时长，给目标应用一点时间完成激活
PASTE_DELAY_MS = 90

# ---------------------------------------------------------------------------
# 快捷键加速器解析（"Ctrl+Shift+V" <-> RegisterHotKey 参数）
# ---------------------------------------------------------------------------
_MODIFIER_ALIASES = {
    "ctrl": w32.MOD_CONTROL,
    "control": w32.MOD_CONTROL,
    "alt": w32.MOD_ALT,
    "shift": w32.MOD_SHIFT,
    "win": w32.MOD_WIN,
    "super": w32.MOD_WIN,
    "meta": w32.MOD_WIN,
}

_KEY_ALIASES = {
    "enter": w32.VK_RETURN,
    "return": w32.VK_RETURN,
    "esc": w32.VK_ESCAPE,
    "escape": w32.VK_ESCAPE,
    "space": 0x20,
    "tab": 0x09,
    "del": w32.VK_DELETE,
    "delete": w32.VK_DELETE,
    "up": w32.VK_UP,
    "down": w32.VK_DOWN,
    "left": 0x25,
    "right": 0x27,
}


def parse_accelerator(accelerator):
    """把 "Ctrl+Shift+V" 解析成 (modifiers, vk)。

    校验规则（UC-16 主事件流第 5 步）：
        必须包含修饰键，且必须包含且仅包含一个非修饰键。
    """
    if not accelerator or not isinstance(accelerator, str):
        raise AppError(ERR_INVALID_ARG, "快捷键为空")
    parts = [p.strip() for p in accelerator.replace("＋", "+").split("+") if p.strip()]
    if not parts:
        raise AppError(ERR_INVALID_ARG, "快捷键为空")

    modifiers = 0
    keys = []
    for part in parts:
        lower = part.lower()
        if lower in _MODIFIER_ALIASES:
            modifiers |= _MODIFIER_ALIASES[lower]
        else:
            keys.append(lower)

    if modifiers == 0:
        raise AppError(ERR_INVALID_ARG, "快捷键必须包含 Ctrl / Alt / Shift / Win 中的至少一个")
    if len(keys) != 1:
        raise AppError(ERR_INVALID_ARG, "快捷键必须包含且仅包含一个主键")

    key = keys[0]
    if key in _KEY_ALIASES:
        vk = _KEY_ALIASES[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise AppError(ERR_INVALID_ARG, "无法识别的按键：%s" % key)
    return modifiers, vk


def format_accelerator(modifiers, vk):
    """RegisterHotKey 参数 -> 展示用字符串（供界面回显）。"""
    parts = []
    if modifiers & w32.MOD_CONTROL:
        parts.append("Ctrl")
    if modifiers & w32.MOD_ALT:
        parts.append("Alt")
    if modifiers & w32.MOD_SHIFT:
        parts.append("Shift")
    if modifiers & w32.MOD_WIN:
        parts.append("Win")
    if 0x30 <= vk <= 0x5A:
        parts.append(chr(vk))
    elif 0x70 <= vk <= 0x87:
        parts.append("F%d" % (vk - 0x70 + 1))
    else:
        parts.append("VK_%02X" % vk)
    return "+".join(parts)


def validate_accelerator(accelerator):
    """只做校验，返回规范化后的字符串；不合法则抛 AppError。"""
    modifiers, vk = parse_accelerator(accelerator)
    return format_accelerator(modifiers, vk)


# ---------------------------------------------------------------------------
# 焦点与模拟按键
# ---------------------------------------------------------------------------
def get_foreground_window():
    return int(w32.user32.GetForegroundWindow() or 0)


def same_window(a, b):
    try:
        return int(a or 0) == int(b or 0)
    except (TypeError, ValueError):
        return False


def is_window(hwnd):
    return bool(hwnd) and bool(w32.user32.IsWindow(ctypes.wintypes.HWND(int(hwnd))))


def restore_foreground_window(hwnd, logger=None):
    """把焦点交还给指定窗口（UC-09 主事件流第 5 步）。

    SetForegroundWindow 有一条很烦的限制：只有"调用进程当前拥有前台窗口"
    等少数情况下才会成功，否则即使返回成功也可能只是把窗口闪一下任务栏。
    面板刚隐藏时前台窗口还属于本进程，但如果隐藏动作尚未被系统处理完，
    直接调用就会失败，结果 Ctrl+V 打到别处（UC-09 业务规则 R4 说的就是它）。

    按下面的顺序逐级加强，任一步成功即返回（实测这四步能覆盖
    普通应用、浏览器、记事本、资源管理器这些常见目标）：

        ① 直接 SetForegroundWindow
        ② 把本线程输入队列附加到前台线程再调（AttachThreadInput）
        ③ 先补一次 Alt 键的按下/抬起，让系统认账"本进程刚有用户输入"
        ④ 最小化再还原，强制把它顶到最前

    每一步都要成对清理（AttachThreadInput 必须解除），否则会挂住两边的输入。
    """
    import ctypes.wintypes as wt
    if not hwnd or not w32.is_windows():
        return False
    target = wt.HWND(int(hwnd))

    def _activate():
        try:
            if same_window(get_foreground_window(), hwnd):
                return True
            w32.user32.ShowWindow(target, w32.SW_RESTORE)
            return bool(w32.user32.SetForegroundWindow(target))
        except Exception:  # noqa: BLE001
            return False

    def _settle(seconds=0.06):
        """给系统一点时间处理前台窗口切换，顺便顺手再确认一次。"""
        time.sleep(seconds)
        return same_window(get_foreground_window(), hwnd)

    try:
        if not w32.user32.IsWindow(target):
            return False

        # ① 直接切
        if _activate() and _settle():
            return True

        # ② Alt 键技巧：补一次"用户按过 Alt"的动作，让系统认账本进程刚有用户输入。
        #    这条放在 AttachThreadInput 之前，因为它更轻、副作用更小。
        try:
            w32.user32.keybd_event(w32.VK_MENU, 0, 0, 0)
            w32.user32.keybd_event(w32.VK_MENU, 0, w32.KEYEVENTF_KEYUP, 0)
            if _activate() and _settle():
                return True
        except Exception:  # noqa: BLE001
            pass

        # ③ 把本线程输入队列附加到目标线程再切
        current_thread = w32.kernel32.GetCurrentThreadId()
        target_thread = w32.user32.GetWindowThreadProcessId(target, None)
        if target_thread and target_thread != current_thread:
            attached = bool(
                w32.user32.AttachThreadInput(current_thread, target_thread, True)
            )
            try:
                if _activate() and _settle():
                    return True
            finally:
                if attached:
                    w32.user32.AttachThreadInput(current_thread, target_thread, False)

        # ④ 最小化再还原，强制把它顶到最前
        try:
            w32.user32.ShowWindow(target, w32.SW_MINIMIZE)
            time.sleep(0.08)
            if _activate() and _settle(0.12):
                return True
        except Exception:  # noqa: BLE001
            pass

        if logger:
            logger.warning(
                "焦点未能交还给 hwnd=%s（当前前台 hwnd=%s），粘贴可能落到其他窗口",
                hwnd, get_foreground_window(),
            )
        return False
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.debug("恢复前台窗口异常：%s", exc)
        return False


def send_key_combo(vk, modifiers=w32.MOD_CONTROL, logger=None):
    """模拟一次"修饰键 + 主键"的组合按键。

    这里用 keybd_event 而不是 SendInput，理由是**实测结论**：
    本机（Windows + 当前 Python 3.12 / ctypes）用 SendInput 发 Ctrl+V 时，
    返回值报告 4 个事件全部发出、GetLastError 为 0，但记事本、浏览器等
    目标窗口收不到任何内容；换成 keybd_event 立刻正常。
    两套 API 的 INPUT 结构与调用参数都反复核对过（cbSize 也已按
    MOUSEINPUT 对齐到 40），因此判断问题出在 SendInput 与 ctypes
    联合体布局的配合上，而不是参数写错。

    作为工程取舍：粘贴是这个软件最核心的一条链路，"能不能粘上"
    比"用哪个 API"重要得多，所以这里选择实测可用的 keybd_event。
    SendInput 的实现保留在 send_key_combo_sendinput 里，方便后续验证与切换。
    """
    if not w32.is_windows():
        raise AppError(ERR_INTERNAL, "当前系统不支持模拟按键")
    modifier_vks = []
    if modifiers & w32.MOD_CONTROL:
        modifier_vks.append(w32.VK_CONTROL)
    if modifiers & w32.MOD_ALT:
        modifier_vks.append(w32.VK_MENU)
    if modifiers & w32.MOD_SHIFT:
        modifier_vks.append(w32.VK_SHIFT)
    if modifiers & w32.MOD_WIN:
        modifier_vks.append(w32.VK_LWIN)

    try:
        for key in modifier_vks:
            w32.user32.keybd_event(key, 0, 0, 0)
        w32.user32.keybd_event(vk, 0, 0, 0)
        w32.user32.keybd_event(vk, 0, w32.KEYEVENTF_KEYUP, 0)
        for key in reversed(modifier_vks):
            w32.user32.keybd_event(key, 0, w32.KEYEVENTF_KEYUP, 0)
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("模拟按键失败：%s", exc)
        return False
    return True


def send_key_combo_sendinput(vk, modifiers=w32.MOD_CONTROL, logger=None):
    """SendInput 版本（当前不用于粘贴，保留供后续验证与切换）。"""
    if not w32.is_windows():
        raise AppError(ERR_INTERNAL, "当前系统不支持模拟按键")
    modifier_vks = []
    if modifiers & w32.MOD_CONTROL:
        modifier_vks.append(w32.VK_CONTROL)
    if modifiers & w32.MOD_ALT:
        modifier_vks.append(w32.VK_MENU)
    if modifiers & w32.MOD_SHIFT:
        modifier_vks.append(w32.VK_SHIFT)
    if modifiers & w32.MOD_WIN:
        modifier_vks.append(w32.VK_LWIN)

    events = []
    for key in modifier_vks:
        events.append(_key_event(key, up=False))
    events.append(_key_event(vk, up=False))
    events.append(_key_event(vk, up=True))
    for key in reversed(modifier_vks):
        events.append(_key_event(key, up=True))

    array = (w32.INPUT * len(events))(*events)
    sent = w32.user32.SendInput(len(events), array, w32.input_struct_size())
    if sent != len(events):
        error = w32.last_error()
        if logger:
            logger.warning("SendInput 只发出 %d/%d 个按键事件（err=%s）", sent, len(events), error)
        return False
    return True


def _key_event(vk, up=False):
    event = w32.INPUT()
    event.type = w32.INPUT_KEYBOARD
    event.ki = w32.KEYBDINPUT()
    event.ki.wVk = vk
    event.ki.wScan = 0
    event.ki.dwFlags = w32.KEYEVENTF_KEYUP if up else 0
    event.ki.time = 0
    # dwExtraInfo 必须是整数（ULONG_PTR），传 None 会让 ctypes 抛类型错误
    event.ki.dwExtraInfo = 0
    return event


def simulate_paste(target_hwnd, rich=False, logger=None):
    """隐藏面板之后的完整粘贴动作：交还焦点 → 等待 → 发 Ctrl+V。

    返回 True 表示按键已发出；目标应用是否真的接收，系统层面无法确认，
    因此界面上对失败情况固定提示"已复制到剪贴板，请手动按 Ctrl+V"
    （UC-09 异常流 E1）。
    """
    if target_hwnd:
        restore_foreground_window(target_hwnd, logger)
    time.sleep(FOCUS_SETTLE_MS / 1000.0)
    # 再确认一次：目标窗口确实拿到前台了，否则 Ctrl+V 会打到别的地方
    if target_hwnd and not same_window(get_foreground_window(), target_hwnd):
        restored = restore_foreground_window(target_hwnd, logger)
        if not restored and logger:
            logger.warning(
                "焦点未能交还给目标窗口（期望 hwnd=%s，实际 hwnd=%s），粘贴可能落到其他窗口",
                target_hwnd, get_foreground_window(),
            )
    time.sleep(PASTE_DELAY_MS / 1000.0)
    return send_key_combo(w32.VK_V, w32.MOD_CONTROL, logger)


def simulate_key(accelerator, logger=None):
    """按配置的加速器发一次按键（顺序粘贴等场景备用）。"""
    modifiers, vk = parse_accelerator(accelerator)
    return send_key_combo(vk, modifiers, logger)
