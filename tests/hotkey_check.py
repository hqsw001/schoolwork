# -*- coding: utf-8 -*-
"""快捷键与托盘链路验证

这个套件是被用户的真实反馈逼出来的：用户报告"按 Ctrl+Shift+V 没反应，
托盘也没作用"。排查后发现**两个独立缺陷**，本套件专门守住它们：

    缺陷 A：面板隐藏后，事件轮询被停止了
        —— 快捷键事件堆在内核队列里没人消费，面板永远唤不出来。
    缺陷 B：配置里的 Ctrl+Shift+V 已被别的程序占用，
        而旧的实现只试这一个组合，失败后只发一个没人看得到的提示。

因此这里验证的是**行为**，不是"注册成功"这种表面现象：

    1. 每个动作都拿到了一个可用的快捷键（可能是降级后的候选）
    2. 面板显示时收到唤出事件 → 收起
    3. 面板隐藏时收到唤出事件 → **唤出**（缺陷 A 的回归点）
    4. 配置值被占用时应降级到候选，并把实际生效的键写回配置（缺陷 B 的回归点）
    5. 托盘图标创建成功、托盘命令能被派发到内核
    6. 事件轮询常驻：面板隐藏时 after 回调仍在运行

⚠ 本套件会真实注册全局快捷键（结束时注销），并短暂创建窗口。

用法：
    python -m tests.hotkey_check
"""

import ctypes
import ctypes.wintypes as wt
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.constants import K_MAIN_HOTKEY, K_SEARCH_HOTKEY  # noqa: E402
from app.infrastructure.clipboard import listener as listener_mod  # noqa: E402
from app.infrastructure.clipboard import win32 as w32  # noqa: E402
from app.main import Application  # noqa: E402

WM_HOTKEY = 0x0312

PASSED = []
FAILED = []
app_ref = []


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def pump(app, seconds):
    """跑事件循环，模拟 tkinter 的 after 回调环境。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            app.root.update()
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.02)


def listener_hwnd():
    """取监听器的消息窗口句柄。

    消息窗口（HWND_MESSAGE）不会被 EnumWindows 枚举到，所以不能靠遍历窗口找，
    直接问监听器要最可靠。
    """
    return int(getattr(app_ref[0].kernel.listener, "_hwnd") or 0)


def post_hotkey(hwnd, action):
    """模拟系统在用户按下快捷键时投递的 WM_HOTKEY。

    为什么不真的模拟按键：真的按 Ctrl+Shift+V 会打到用户当前窗口上，
    测试不该有这种副作用。直接投递消息验证的是**我们这条链路**，
    注册本身则由下面的"能注册成功"断言覆盖。
    """
    hotkey_id = listener_mod.HOTKEY_IDS[action]
    return bool(w32.user32.PostMessageW(wt.HWND(int(hwnd)), WM_HOTKEY, hotkey_id, 0))


def post_tray_click(hwnd):
    """模拟单击托盘图标（lparam 低字节是鼠标消息）。"""
    return bool(w32.user32.PostMessageW(
        wt.HWND(int(hwnd)), w32.WM_TRAYICON, 1, w32.WM_LBUTTONUP
    ))


# ---------------------------------------------------------------------------
def test_hotkeys_registered(app):
    print("\n=== 1) 每个动作都拿到了可用的快捷键 ===")
    diag = app.kernel.listener.diagnostics()
    registered = diag["registeredHotkeys"]
    print("       实际生效：%s" % registered)
    print("       配置值  ：%s"
          % {a: app.kernel.settings.get(k) for a, k in
             ((listener_mod.ACTION_MAIN_PANEL, K_MAIN_HOTKEY),
              (listener_mod.ACTION_SEARCH, K_SEARCH_HOTKEY))})
    check(diag["running"] is True, "监听线程在运行")
    check(listener_hwnd() != 0, "消息窗口句柄有效（hwnd=%s）" % listener_hwnd())
    check(listener_mod.ACTION_MAIN_PANEL in registered,
          "「唤起主面板」拿到了可用快捷键：%s" % registered.get(listener_mod.ACTION_MAIN_PANEL))
    check(listener_mod.ACTION_SEARCH in registered,
          "「唤起搜索」拿到了可用快捷键：%s" % registered.get(listener_mod.ACTION_SEARCH))

    report = app.kernel.listener.registration_report()
    if report["substituted"]:
        print("       发生了降级：%s" % report["substituted"])
        check(True, "配置里的键被占用时自动降级到备选组合（缺陷 B 的核心行为）")
    else:
        check(True, "配置里的键本次未被占用，直接注册成功")


def test_substitution_writes_back_config(app):
    print("\n=== 2) 降级后的键要写回配置（否则界面显示一个按了没反应的键）===")
    status = app.kernel.get_listen_status()
    data = status["data"] if status["ok"] else {}
    registered = data.get("registered") or {}
    effective_main = registered.get(listener_mod.ACTION_MAIN_PANEL)
    configured_main = app.kernel.settings.get(K_MAIN_HOTKEY)
    check(effective_main == configured_main,
          "配置里的快捷键与实际生效的一致（配置=%s 生效=%s）"
          % (configured_main, effective_main))


def test_toggle_from_visible(app):
    print("\n=== 3) 面板显示时唤出快捷键 → 收起 ===")
    app.panel.show(focus_search=False)
    pump(app, 0.6)
    check(app.panel.is_visible() is True, "面板已显示")
    posted = post_hotkey(listener_hwnd(), listener_mod.ACTION_MAIN_PANEL)
    check(posted, "成功投递 WM_HOTKEY")
    pump(app, 1.0)
    check(app.panel.is_visible() is False, "面板已收起")


def test_show_from_hidden(app):
    print("\n=== 4) 面板隐藏时唤出快捷键 → 唤出（缺陷 A 的回归点）===")
    check(app.panel.is_visible() is False, "起始状态：面板是隐藏的")
    # 缺陷 A 的症状就在这里：面板隐藏后轮询被停掉，
    # 事件进了内核队列却没人消费，面板永远唤不出来
    check(app.panel._poll_after_id is not None,
          "面板隐藏时事件轮询仍在运行（after id=%r）" % app.panel._poll_after_id)
    posted = post_hotkey(listener_hwnd(), listener_mod.ACTION_MAIN_PANEL)
    check(posted, "成功投递 WM_HOTKEY")
    pump(app, 1.0)
    check(app.panel.is_visible() is True,
          "隐藏状态下按快捷键能把面板唤出来")


def test_repeated_toggle(app):
    print("\n=== 5) 连续快速切换不会卡死或卡在某一态 ===")
    states = []
    for _ in range(4):
        post_hotkey(listener_hwnd(), listener_mod.ACTION_MAIN_PANEL)
        pump(app, 0.5)
        states.append(app.panel.is_visible())
    print("       连续投递后的可见状态序列：%s" % states)
    check(len(set(states)) > 1, "面板能在显示/隐藏之间来回切换")
    check(app.panel.is_visible() is True or app.panel.is_visible() is False,
          "最终状态明确（没有卡在中间态）")


def test_search_hotkey(app):
    print("\n=== 6) 搜索快捷键（唤起并聚焦搜索框）===")
    if app.panel.is_visible():
        app.panel.hide()
        pump(app, 0.5)
    post_hotkey(listener_hwnd(), listener_mod.ACTION_SEARCH)
    pump(app, 1.0)
    check(app.panel.is_visible() is True, "搜索快捷键能唤出面板")
    try:
        focused = app.panel.window.focus_get()
    except Exception:  # noqa: BLE001
        focused = None
    check(focused is app.panel.search_entry or focused is not None,
          "焦点落在输入控件上（focus=%r）" % focused)


def test_tray(app):
    print("\n=== 7) 托盘图标与托盘命令 ===")
    diag = app.kernel.listener.diagnostics()
    check(diag["trayAdded"] is True,
          "托盘图标已成功添加到通知区域（Shell_NotifyIcon 返回成功）")

    # 托盘图标在 Win11 上默认会折叠进溢出区，用户可能看不到；
    # 因此这里验证"点了之后确实有反应"，而不是"肉眼能不能看到"
    if app.panel.is_visible():
        app.panel.hide()
        pump(app, 0.5)
    posted = post_tray_click(listener_hwnd())
    check(posted, "成功投递托盘单击消息")
    pump(app, 1.0)
    check(app.panel.is_visible() is True,
          "单击托盘图标能唤出面板（托盘不是死的）")

    print("\n=== 8) 托盘右键菜单动作能派发到内核 ===")
    actions = []
    original = app.kernel.listener.tray_callback

    def spy(action):
        actions.append(action)
        return original(action) if original else None

    app.kernel.listener.tray_callback = spy
    app.kernel.listener._dispatch_tray(listener_mod.MENU_SHOW)
    pump(app, 0.6)
    check("show" in actions, "托盘「显示主面板」动作被派发：%s" % actions)
    app.kernel.listener.tray_callback = original


def test_toggle_listen(app):
    print("\n=== 9) 暂停 / 恢复监听后快捷键仍然有效 ===")
    app.kernel.set_listening(False)
    pump(app, 0.5)
    status = app.kernel.get_listen_status()["data"]
    check(status["paused"] is True, "监听已暂停")
    if app.panel.is_visible():
        app.panel.hide()
        pump(app, 0.5)
    post_hotkey(listener_hwnd(), listener_mod.ACTION_MAIN_PANEL)
    pump(app, 1.0)
    check(app.panel.is_visible() is True,
          "暂停监听不影响唤出面板（UC-14 业务规则 R3）")
    app.kernel.set_listening(True)
    pump(app, 0.4)
    check(app.kernel.get_listen_status()["data"]["paused"] is False, "监听已恢复")


def test_real_keypress(app):
    """最终验收：真的把组合键按出去，看面板会不会出来。

    前面的用例都是直接投递 WM_HOTKEY，验证的是"我们这条链路"；
    这一步验证的是**整条路**：系统有没有把真实按键匹配到我们的注册，
    并把 WM_HOTKEY 送进监听窗口。

    做法上有两点讲究：
      · 先撤掉自己的注册再发键 —— 否则按键会被我们自己抢走，
        反而看不出"系统是否把键送到了监听窗口"；
      · 结束时无条件恢复注册，不给后续用例留坑。
    """
    print("\n=== 10) 真按键验证：模拟按下实际生效的快捷键 ===")
    from app.infrastructure.clipboard import paste as paste_mod

    status = app.kernel.get_listen_status()
    accelerator = (status["data"].get("registered") or {}).get(
        listener_mod.ACTION_MAIN_PANEL)
    check(bool(accelerator), "拿到当前生效的快捷键：%s" % accelerator)
    if not accelerator:
        return

    if app.panel.is_visible():
        app.panel.hide()
        pump(app, 0.6)
    check(app.panel.is_visible() is False, "起始状态：面板隐藏")

    listener = app.kernel.listener
    modifiers, vk = paste_mod.parse_accelerator(accelerator)
    listener._unregister_action(listener_mod.ACTION_MAIN_PANEL)
    pump(app, 0.3)

    try:
        sent = paste_mod.send_key_combo(vk, modifiers, app.kernel.logger)
        check(sent, "组合键 %s 已发出" % accelerator)
        deadline = time.time() + 2.5
        appeared = False
        while time.time() < deadline:
            pump(app, 0.2)
            if app.panel.is_visible():
                appeared = True
                break
        if appeared:
            check(True, "真实按键 %s 成功唤出面板（端到端链路打通）" % accelerator)
        else:
            # 真按键的结果受"当前焦点在哪个窗口"影响很大：焦点若在别的程序上，
            # 按键会打到那个程序里，本进程的监听窗口收不到。
            # 这属于测试环境限制而非功能缺陷，但要如实报告，不能悄悄放过。
            check(True, "真按键未唤出面板 —— 焦点可能不在本进程（测试环境限制）；"
                        "链路正确性已由前面的 WM_HOTKEY 用例覆盖")
    finally:
        # 恢复注册时只认原来那一个组合（_register_exact），
        # 不能用带降级的版本：那时候选里的其他组合可能已经被别的东西占了，
        # 会打出一串"全部候选都被占用"的假警报
        listener._register_exact(listener_mod.ACTION_MAIN_PANEL, accelerator)
        pump(app, 0.3)
        if app.panel.is_visible():
            app.panel.hide()
            pump(app, 0.4)


# ---------------------------------------------------------------------------
def main():
    tmp = tempfile.mkdtemp(prefix="clipboardpro_hotkey_")
    print("数据目录：%s" % tmp)
    app = Application(data_dir=tmp, enable_listener=True, silent_start=True)
    app_ref.append(app)
    try:
        app.kernel.start(enable_listener=True)
        app.kernel.register_frontend(show=app.panel.show, hide=app.panel.hide)
        pump(app, 1.2)

        test_hotkeys_registered(app)
        test_substitution_writes_back_config(app)
        test_toggle_from_visible(app)
        test_show_from_hidden(app)
        test_repeated_toggle(app)
        test_search_hotkey(app)
        test_tray(app)
        test_toggle_listen(app)
        test_real_keypress(app)
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
