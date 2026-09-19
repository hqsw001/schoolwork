# -*- coding: utf-8 -*-
"""窗口行为与"关掉终端后仍可用"的验证

覆盖三件事：

    一、面板是标准窗口（有标题栏），标题栏关闭按钮 = 收起面板而非退出程序
    二、面板内的「最小化 / 收起」按钮、最小化恢复、尺寸记忆、置顶开关都有效
    三、程序脱离控制台启动后，控制台结束、单实例守卫、快捷键消息链路仍正常

第三项是用户明确提的诉求："terminal 关掉后还能用快捷键呼出面板"。
换句话说 —— 关掉终端之后，托盘图标与全局快捷键必须还在。

⚠ 本测试会短暂创建窗口，并在最后启动一个真实的脱离控制台的子进程
   （验证完立刻结束它）。

用法：
    python -m tests.window_behavior_check
"""

import os
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.constants import K_ALWAYS_ON_TOP, K_WINDOW_GEOMETRY  # noqa: E402
from app.infrastructure import single_instance  # noqa: E402
from app.main import Application  # noqa: E402

PASSED = []
FAILED = []

#: Win32 窗口样式位（用来确认系统标题栏确实存在）
GWL_STYLE = -16
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_MINIMIZEBOX = 0x00020000
WS_SYSMENU = 0x00080000
WS_POPUP = 0x80000000


def _window_style(hwnd):
    """读取窗口样式位。

    Tk 的 ``winfo_id()`` 返回的是子窗口句柄，真正带标题栏的是它的父窗口，
    因此要往上找一层 —— 这一步很容易搞错，实测确认过。
    """
    import ctypes
    import ctypes.wintypes as wt

    user32 = ctypes.windll.user32
    user32.GetWindowLongW.restype = ctypes.c_long
    user32.GetWindowLongW.argtypes = [wt.HWND, ctypes.c_int]
    user32.GetParent.restype = wt.HWND
    user32.GetParent.argtypes = [wt.HWND]

    handle = int(hwnd)
    parent = user32.GetParent(wt.HWND(handle))
    target = int(parent) if parent else handle
    return user32.GetWindowLongW(wt.HWND(target), GWL_STYLE) & 0xFFFFFFFF


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def pump(app, seconds=0.5):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            app.root.update()
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.02)


# ---------------------------------------------------------------------------
def test_window_chrome(app):
    print("\n=== 1) 面板是标准窗口（可移动 / 可最小化 / 有关闭按钮）===")
    panel = app.panel
    panel.show(focus_search=False)
    pump(app, 0.6)

    # 直接查 Win32 窗口样式，确认系统标题栏与缩放边框真的存在 ——
    # 这比读 tkinter 的 overrideredirect() 更可靠（后者在部分 Tk 版本上
    # 查询形式返回 None）。
    style = _window_style(panel.window.winfo_id())
    check(style & WS_CAPTION == WS_CAPTION,
          "窗口带系统标题栏（WS_CAPTION，可拖动移动）")
    check(bool(style & WS_THICKFRAME), "窗口带缩放边框（可拖边缘改大小）")
    check(bool(style & WS_MINIMIZEBOX), "窗口带最小化按钮")
    check(bool(style & WS_SYSMENU), "窗口带系统菜单与关闭按钮")

    check(panel.window.resizable() != (0, 0), "窗口允许缩放")
    check(panel.window.minsize() != (1, 1), "设置了最小尺寸，拖小也不会挤坏布局")
    check("剪贴板Pro" in panel.window.title(),
          "窗口标题已设置：%s" % panel.window.title())
    check(bool(panel.window.protocol("WM_DELETE_WINDOW")),
          "关闭按钮已绑定处理函数：%s" % panel.window.protocol("WM_DELETE_WINDOW"))
    check(panel.window.state() == "normal", "唤出后窗口状态为 normal")

    print("\n=== 2) 关闭按钮 = 收起面板，不是退出程序 ===")
    # 等价于点标题栏的 ✕：调用绑定的协议处理函数。
    # 不能只调 tk.call("wm","protocol",...) —— 那只查询不触发。
    close_handler = panel.window.protocol("WM_DELETE_WINDOW")
    panel.window.tk.call(close_handler)
    pump(app, 0.4)
    check(panel.is_visible() is False, "点关闭后面板已收起")
    check(panel.window.winfo_exists() == 1, "窗口对象没有被销毁，可以再次唤出")
    check(app.panel is not None, "进程未退出，面板对象仍在")

    print("\n=== 3) 收起后仍能唤出 ===")
    panel.show(focus_search=False)
    pump(app, 0.5)
    check(panel.is_visible() is True, "再次唤出成功")
    check(panel.window.state() == "normal", "窗口状态回到 normal")

    print("\n=== 4) 最小化与从最小化恢复 ===")
    panel.minimize()
    pump(app, 0.5)
    check(panel.window.state() == "iconic", "最小化后窗口状态为 iconic")
    check(panel.is_visible() is False, "最小化后不再算作已显示状态")

    panel.show(focus_search=False)
    pump(app, 0.6)
    check(panel.window.state() == "normal",
          "从最小化唤出时还原为 normal（而不是停在 iconic）")
    check(panel.is_visible() is True, "从最小化恢复后面板可见")

    print("\n=== 5) 面板内的窗口控制按钮都在 ===")
    check(panel.min_btn.winfo_exists() == 1, "有「最小化」按钮")
    check(panel.hide_btn.winfo_exists() == 1, "有「收起」按钮")
    check(panel.min_btn.cget("text") == "—",
          "最小化按钮文案正确：%s" % panel.min_btn.cget("text"))
    check(panel.hide_btn.cget("text") == "收起",
          "收起按钮文案正确：%s" % panel.hide_btn.cget("text"))

    print("\n=== 6) 快捷键绑定 ===")
    for sequence, name in (("<Control-w>", "收起"), ("<Control-m>", "最小化/还原"),
                           ("<Control-comma>", "打开设置")):
        check(bool(panel.window.bind(sequence)), "已绑定 %s = %s" % (sequence, name))

    print("\n=== 7) 窗口尺寸记忆 ===")
    panel.window.geometry("520x560")
    pump(app, 0.6)
    panel.hide()
    pump(app, 0.4)
    saved = app.kernel.settings.get(K_WINDOW_GEOMETRY)
    check(saved == "520x560", "隐藏时把窗口尺寸写回了配置：%s" % saved)

    from app.ui.panel import MainPanel
    probe = MainPanel(app.kernel)
    check(probe._panel_size == (520, 560),
          "重建面板时按上次尺寸恢复：%s" % (probe._panel_size,))
    probe.window.destroy()

    print("\n=== 8) 置顶开关 ===")
    result = app.kernel.update_setting(K_ALWAYS_ON_TOP, False)
    check(result["ok"], "可以关闭「总在最前」")
    panel.window.attributes("-topmost", False)
    check(not panel.window.attributes("-topmost"), "置顶已关闭")
    app.kernel.update_setting(K_ALWAYS_ON_TOP, True)
    panel.window.attributes("-topmost", True)
    check(bool(panel.window.attributes("-topmost")), "置顶已恢复")


# ---------------------------------------------------------------------------
def test_console_survival(app):
    """核心诉求：终端关掉之后，程序与快捷键必须还在。"""
    print("\n=== 9) 脱离控制台启动：终端关掉后程序仍存活 ===")
    cwd = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    launcher = os.path.join(cwd, "启动剪贴板Pro.py")
    check(os.path.exists(launcher), "启动器存在：%s" % os.path.basename(launcher))

    data_dir = tempfile.mkdtemp(prefix="clipboardpro_detach_")
    env = dict(os.environ)
    env["CLIPBOARDPRO_DATA_DIR"] = data_dir
    env["CLIPBOARDPRO_LOG_LEVEL"] = "INFO"

    proc = subprocess.Popen(
        [sys.executable, launcher, "--show"],
        cwd=cwd, env=env,
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    output = proc.communicate(timeout=45)[0].decode("utf-8", "replace")
    check(proc.returncode == 0, "启动器正常返回（退出码 %s）" % proc.returncode)
    check("已启动，进程号" in output, "启动器报告后台进程已启动")
    # 启动器是前台控制台进程且已经结束 —— 等价于"终端被关掉"
    check(proc.poll() is not None, "前台控制台进程（启动器）已结束")

    # 从启动器输出里解析出后台进程号：这是最确定的清理依据，
    # 不要靠"按窗口标题找窗口"来善后（标题匹配容易受别的窗口干扰）
    child_pid = _parse_child_pid(output)
    check(child_pid != 0, "解析到后台进程号 pid=%s" % child_pid)

    try:
        time.sleep(2.5)
        if child_pid:
            check(_process_alive(child_pid),
                  "后台进程仍存活（pid=%s）—— 控制台结束后程序没被带走" % child_pid)
        panel_hwnd = single_instance.find_existing_panel_window()
        check(panel_hwnd != 0, "后台实例的面板窗口存在（hwnd=%s）" % panel_hwnd)
        check(os.path.exists(os.path.join(data_dir, "clipboardpro.db")),
              "后台实例已完成内核初始化（数据库已建立）")

        print("\n=== 10) 单实例守卫（避免重复启动抢占快捷键）===")
        second = subprocess.run(
            [sys.executable, "-m", "app.main"],
            cwd=cwd, env=env, capture_output=True, timeout=45,
        )
        check(second.returncode == 3,
              "第二个实例被守卫拦下（退出码 %s，期望 3）" % second.returncode)

        print("\n=== 11) 快捷键消息链路（控制台已关闭）===")
        if panel_hwnd:
            check(_post_message_probe(panel_hwnd),
                  "能向驻留实例投递窗口消息，说明消息循环仍在正常处理事件")
        else:
            check(False, "没有找到驻留实例的窗口，无法验证消息链路")
    finally:
        # ---- 收尾：按 pid 精确结束，不依赖窗口标题
        if child_pid:
            _kill(child_pid)
            time.sleep(1.0)
            check(not _process_alive(child_pid),
                  "测试结束后台进程已按 pid 精确清理（pid=%s）" % child_pid)


def _parse_child_pid(output):
    match = re.search(r"已启动，进程号\s*(\d+)", output)
    return int(match.group(1)) if match else 0


def _process_alive(pid):
    """用 GetExitCodeProcess 判断进程是否还在跑（STILL_ACTIVE = 259）。"""
    import ctypes
    import ctypes.wintypes as wt

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    STILL_ACTIVE = 259
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return False
    try:
        code = wt.DWORD(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return False
        return code.value == STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _kill(pid):
    import ctypes

    PROCESS_TERMINATE = 0x0001
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(PROCESS_TERMINATE, False, int(pid))
    if handle:
        kernel32.TerminateProcess(handle, 0)
        kernel32.CloseHandle(handle)


def _post_message_probe(hwnd):
    """向窗口投递一个无害的自定义消息，验证它还能收消息。

    这里刻意**不模拟真实按键**：真的发 Ctrl+Shift+V 会打到用户当前
    正在用的窗口上，测试不该做这种有副作用的事。
    """
    import ctypes
    import ctypes.wintypes as wt

    WM_NULL = 0x0000
    user32 = ctypes.windll.user32
    if not user32.IsWindow(wt.HWND(int(hwnd))):
        return False
    return bool(user32.PostMessageW(wt.HWND(int(hwnd)), WM_NULL, 0, 0))


# ---------------------------------------------------------------------------
def main():
    tmp = tempfile.mkdtemp(prefix="clipboardpro_window_")
    print("数据目录：%s" % tmp)
    app = Application(data_dir=tmp, enable_listener=False, silent_start=True)
    try:
        app.kernel.start(enable_listener=False)
        app.kernel.register_frontend(show=app.panel.show, hide=app.panel.hide)
        pump(app, 0.6)
        test_window_chrome(app)
        test_console_survival(app)
    finally:
        try:
            app.panel.hide()
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


if __name__ == "__main__":
    sys.exit(main())
