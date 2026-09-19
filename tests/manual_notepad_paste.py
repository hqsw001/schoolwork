# -*- coding: utf-8 -*-
"""真机验证 3：用真实记事本验证"双击记录 → 内容出现在目标软件里"

前两轮验证分别覆盖了"监听链路"与"焦点交还"，但目标窗口是测试自己建的
Tk 窗口——而 Tk 有自己的焦点模型，Windows 的 Ctrl+V 打进去不一定落到
Text 控件上，因此那种验证方式说服力不足。这一轮直接用系统记事本：
它是最标准的 Win32 编辑控件，粘没粘上一目了然（用 WM_GETTEXT 读回来）。

⚠ 本测试会启动一个记事本进程并在里面输入内容，结束后自动关闭该记事本
   （只关自己启动的那个，不影响你已打开的记事本）。

用法：
    python -m tests.manual_notepad_paste
"""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.errors import now_ms  # noqa: E402
from app.infrastructure.clipboard import clipboard_io  # noqa: E402
from app.infrastructure.clipboard import paste as paste_mod  # noqa: E402
from app.infrastructure.clipboard import win32 as w32  # noqa: E402
from app.main import Application  # noqa: E402

WM_GETTEXT = 0x000D
WM_GETTEXTLENGTH = 0x000E

PASSED = []
FAILED = []


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def pump(app, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            app.root.update()
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.02)


def find_child_window(parent, class_name):
    """找指定类名的子窗口（记事本的编辑区是 "Edit" 或 "RichEditD2DPT"）。"""
    result = []

    @w32.WNDENUMPROC
    def enum_proc(hwnd, _lparam):
        buffer = ctypes.create_unicode_buffer(256)
        w32.user32.GetClassNameW(hwnd, buffer, 256)
        if buffer.value == class_name:
            result.append(int(hwnd))
            return False           # 返回 False 结束枚举
        return True

    w32.user32.EnumChildWindows(wt.HWND(int(parent)), enum_proc, 0)
    return result[0] if result else 0


def read_window_text(hwnd):
    """用 WM_GETTEXT 把目标窗口的文字读回来。

    注意 lParam 传的是缓冲区指针的整数值：SendMessageW 的 argtypes 里
    第 4 个参数是 LPARAM（整数），直接传 c_void_p 会被 ctypes 拒绝。
    """
    length = w32.user32.SendMessageW(wt.HWND(int(hwnd)), WM_GETTEXTLENGTH, 0, 0)
    if not length:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 2)
    w32.user32.SendMessageW(
        wt.HWND(int(hwnd)), WM_GETTEXT, length + 1,
        ctypes.cast(buffer, ctypes.c_void_p).value,
    )
    return buffer.value


def main():
    tmp = tempfile.mkdtemp(prefix="clipboardpro_notepad_")
    print("数据目录：%s\n" % tmp)

    app = Application(data_dir=tmp, enable_listener=True, silent_start=True)
    notepad = None
    try:
        app.kernel.start(enable_listener=True)
        app.kernel.register_frontend(show=app.panel.show, hide=app.panel.hide)
        pump(app, 0.8)

        # ---------------------------------------------------------- 启动记事本
        print("=== 1) 启动系统记事本 ===")
        # Win11 的记事本是多进程的 UWP 打包应用，Popen 拿到的 pid 不一定是
        # 真正持有窗口的那个进程，因此用"启动前后窗口集合的差集"来找它。
        before_windows = _list_top_windows()
        notepad = subprocess.Popen(["notepad.exe"])
        notepad_hwnd = 0
        for _ in range(60):
            time.sleep(0.2)
            new_windows = _list_top_windows() - before_windows
            candidates = [h for h in new_windows if _window_title(h)]
            if candidates:
                notepad_hwnd = candidates[0]
                break
        check(notepad_hwnd != 0,
              "找到新出现的记事本窗口（hwnd=%s，标题=%r）"
              % (notepad_hwnd, _window_title(notepad_hwnd)))
        if not notepad_hwnd:
            raise SystemExit(1)

        edit_hwnd = find_child_window(notepad_hwnd, "Edit") or \
            find_child_window(notepad_hwnd, "RichEditD2DPT") or \
            find_child_window(notepad_hwnd, "Windows.UI.Core.CoreWindow")
        check(edit_hwnd != 0, "找到记事本编辑区（hwnd=%s，类名=%s）"
              % (edit_hwnd, _class_name(edit_hwnd)))

        print("\n=== 2) 把内容放进剪贴板（走真实复制链路）===")
        marker = "剪贴板Pro 记事本验证-%d" % (now_ms() % 100000)
        clipboard_io.write_text(marker)
        deadline = time.time() + 5
        entry = None
        while time.time() < deadline:
            app.kernel.handle_listener_events()
            app.kernel.poll_events()
            time.sleep(0.1)
            rows = app.kernel.repo.get_history(limit=1)
            if rows and rows[0].content == marker:
                entry = rows[0]
                break
        check(entry is not None, "复制的内容已自动入库")

        print("\n=== 3) 切到记事本，然后唤出剪贴板Pro 面板 ===")
        paste_mod.restore_foreground_window(notepad_hwnd, app.kernel.logger)
        time.sleep(0.5)
        foreground = paste_mod.get_foreground_window()
        check(foreground == notepad_hwnd,
              "记事本成为前台窗口（期望 %s，实际 %s）" % (notepad_hwnd, foreground))
        app.panel.show(focus_search=False)
        pump(app, 1.0)
        recorded = app.kernel._paste_target_hwnd
        check(recorded == notepad_hwnd,
              "面板登记了正确的粘贴目标（期望记事本 %s，记录 %s）" % (notepad_hwnd, recorded))

        print("\n=== 4) 触发粘贴（等价于界面上双击记录）===")
        result = app.kernel.paste_entry(entry.id, rich_paste=True, simulate=True)
        check(result["ok"] and result["data"]["pasted"] is True, "粘贴命令执行成功并发出按键")
        pump(app, 1.5)

        foreground_after = paste_mod.get_foreground_window()
        check(foreground_after == notepad_hwnd,
              "焦点回到了记事本（期望 %s，实际 %s）" % (notepad_hwnd, foreground_after))
        check(app.panel.is_visible() is False, "面板已隐藏")

        print("\n=== 5) 读回记事本内容，确认真的粘上了 ===")
        received = read_window_text(edit_hwnd) if edit_hwnd else ""
        if not received:
            # 新版记事本（Win11 的 UWP 版本）子窗口类名不同，退一步用标题栏判断不了，
            # 这里再枚举一次所有子窗口
            for class_name in ("Edit", "RichEditD2DPT", "Windows.UI.Core.CoreWindow"):
                child = find_child_window(notepad_hwnd, class_name)
                if child:
                    received = read_window_text(child)
                    if received:
                        break
        check(marker in received,
              "记事本里出现了被粘贴的内容：%r" % received[:80])

        print("\n=== 6) 校验粘贴没有污染历史（回声抑制）===")
        count_after = app.kernel.repo.count()
        check(count_after == 1, "本次粘贴没有产生重复记录（当前 %d 条）" % count_after)

    finally:
        try:
            app.panel.hide()
        except Exception:  # noqa: BLE001
            pass
        if notepad is not None:
            try:
                notepad.terminate()
                notepad.wait(timeout=5)
                print("       已关闭测试用的记事本进程")
            except Exception:  # noqa: BLE001
                pass
        try:
            app.kernel.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            app.root.destroy()
        except Exception:  # noqa: BLE001
            pass

    print("\n" + "=" * 72)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\n失败清单：")
        for item in FAILED:
            print("  - %s" % item)
        return 1
    return 0


def _list_top_windows():
    """列出当前所有可见的顶层窗口句柄。"""
    windows = set()

    @w32.WNDENUMPROC
    def enum_proc(hwnd, _lparam):
        if w32.user32.IsWindowVisible(hwnd):
            windows.add(int(hwnd))
        return True

    w32.user32.EnumWindows(enum_proc, 0)
    return windows


def _window_title(hwnd):
    if not hwnd:
        return ""
    length = w32.user32.GetWindowTextLengthW(wt.HWND(int(hwnd)))
    if length <= 0:
        return ""
    buffer = ctypes.create_unicode_buffer(length + 1)
    w32.user32.GetWindowTextW(wt.HWND(int(hwnd)), buffer, length + 1)
    return buffer.value


def _class_name(hwnd):
    if not hwnd:
        return ""
    buffer = ctypes.create_unicode_buffer(256)
    w32.user32.GetClassNameW(wt.HWND(int(hwnd)), buffer, 256)
    return buffer.value


if __name__ == "__main__":
    sys.exit(main())
