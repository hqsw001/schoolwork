# -*- coding: utf-8 -*-
"""ClipboardListener：系统剪贴板监听 + 全局快捷键 + 托盘入口（F1-1 / UC-19）

设计要点（对照 UC-01 业务规则 R1）：
    用 ``AddClipboardFormatListener`` 注册消息窗口，由 WM_CLIPBOARDUPDATE
    驱动采集，**不使用定时轮询**，空闲时零 CPU 占用。

线程模型：
    本模块内部起一条消息线程，消息窗口、快捷键注册、来源应用识别
    全部发生在该线程上（Win32 对这三件事都有"必须在建消息循环的线程上调用"的约束）。
    消息线程只负责"发现变化 + 取出来源快照"，然后立刻把事件抛给上层；
    真正的剪贴板读取与入库由上层的工作线程完成，避免阻塞消息循环——
    消息循环一旦被卡住，后面的剪贴板变化通知就会丢。

    回调抛出的任何异常都被吞掉并记日志：监听线程一旦死掉，软件就变成
    "看起来在跑但什么都不记录"，这是最不可接受的故障模式。
"""

import ctypes
import ctypes.wintypes as wt
import queue
import threading
import time

from app.constants import ERR_INTERNAL, ERR_INVALID_ARG
from app.errors import AppError
from app.infrastructure.clipboard import paste as paste_mod
from app.infrastructure.clipboard import source_app as source_mod
from app.infrastructure.clipboard import win32 as w32

# 事件类型
EVENT_CLIPBOARD_UPDATE = "clipboard_update"
EVENT_HOTKEY = "hotkey"
EVENT_TRAY = "tray"
EVENT_ERROR = "error"
EVENT_STARTED = "started"
#: 配置里的快捷键被占用、已自动改用替代键
EVENT_HOTKEY_CHANGED = "hotkey_changed"
#: 全部候选都被占用，快捷键彻底不可用（此时只剩托盘入口）
EVENT_HOTKEY_FAILED = "hotkey_failed"

#: 唤醒消息线程处理外部命令的自定义消息
WM_RUN_COMMANDS = w32.WM_APP + 2

# 托盘图标 uID
TRAY_UID = 1

#: 快捷键动作名（与配置项 app.main_hotkey / app.search_hotkey 对应）
ACTION_MAIN_PANEL = "main_panel"
ACTION_SEARCH = "search"
ACTION_TOGGLE_LISTEN = "toggle_listen"

HOTKEY_IDS = {
    ACTION_MAIN_PANEL: 1,
    ACTION_SEARCH: 2,
    ACTION_TOGGLE_LISTEN: 3,
}

#: 每个动作的候选快捷键（第一个就是配置里的默认值）。
#: 为什么要一串候选：RegisterHotKey 是**先到先得**的，而 Ctrl+Shift+V
#: 这类组合非常抢手（输入法、截图工具、IDE、浏览器扩展都爱用）。
#: 实测本机 Ctrl+Shift+V 与 Ctrl+Shift+F 就已经被别的程序占用（err=1409），
#: 只试一个就放弃的话，用户拿到的体验就是"快捷键完全没反应"。
#: 这里按"既不扰民、又尽量好按"的顺序往下试，全部失败才真放弃。
HOTKEY_CANDIDATES = {
    ACTION_MAIN_PANEL: (
        "Ctrl+Shift+V", "Ctrl+Alt+V", "Ctrl+Shift+F9", "Ctrl+Shift+F10",
        "Ctrl+Alt+Q", "Ctrl+Shift+Alt+V", "Ctrl+F10",
    ),
    ACTION_SEARCH: (
        "Ctrl+Shift+F", "Ctrl+Shift+S", "Ctrl+Alt+F", "Ctrl+Shift+F8",
        "Ctrl+Alt+D", "Ctrl+F9",
    ),
}

#: 托盘菜单命令
MENU_SHOW = 1001
MENU_TOGGLE_LISTEN = 1002
MENU_CLEAR = 1003
MENU_SETTINGS = 1004
MENU_EXIT = 1005

MENU_LABELS = [
    (MENU_SHOW, "显示主面板(&O)"),
    (MENU_TOGGLE_LISTEN, "暂停监听(&P)"),
    (MENU_CLEAR, "清空历史(&C)"),
    (MENU_SETTINGS, "设置(&S)"),
    (MENU_EXIT, "退出(&X)"),
]

#: 托盘菜单命令 -> 内核动作名，由 Kernel 注册回调
TRAY_COMMANDS = {
    MENU_SHOW: "show",
    MENU_TOGGLE_LISTEN: "toggle_listen",
    MENU_CLEAR: "clear_history",
    MENU_SETTINGS: "settings",
    MENU_EXIT: "exit",
}


class ListenerEvent(object):
    """监听线程抛出的事件。"""

    __slots__ = ("kind", "payload", "timestamp")

    def __init__(self, kind, payload=None):
        self.kind = kind
        self.payload = payload
        self.timestamp = time.time()

    def __repr__(self):
        return "<ListenerEvent %s %s>" % (self.kind, self.payload)


class ClipboardListener(object):
    """事件驱动的剪贴板监听器。"""

    def __init__(self, logger=None, hotkeys=None, tray_tooltip="剪贴板Pro 0.1.0"):
        self.logger = logger
        self._events = queue.Queue()
        self._thread = None
        self._hwnd = None
        self._running = False
        self._paused = False
        self._wndproc_ref = None   # 必须持有引用，否则回调对象被回收会导致进程崩溃
        self._hotkeys = dict(hotkeys or {})
        self._registered = {}
        #: 各动作**实际生效**的快捷键（可能与 _hotkeys 里的配置值不同：
        #: 配置里的键被占用时会自动降级到候选列表里的下一个）
        self._effective = {}
        #: 发生过替换的动作 -> {"configured": 配置值, "effective": 实际值}
        self._substituted = {}
        #: 彻底注册失败的动作 -> 尝试过的组合列表
        self._failed = {}
        self._tray_tooltip = tray_tooltip
        self._tray_added = False
        self._icon = None
        self._pending_hotkey_commands = queue.Queue()
        self._last_update_at = 0.0
        #: 托盘菜单点击回调：签名 (action_name) -> None，在消息线程中被调用
        self.tray_callback = None

    # ================================================================ 生命周期
    def start(self):
        """启动监听线程（幂等）。"""
        if self._running:
            return True
        if not w32.is_windows():
            raise AppError(ERR_INTERNAL, "剪贴板监听仅支持 Windows 系统")
        self._running = True
        self._thread = threading.Thread(
            target=self._run, name="clipboard-listener", daemon=True
        )
        self._thread.start()
        return True

    def stop(self, timeout=2.0):
        """停止监听：注销快捷键、移除托盘图标、销毁消息窗口（UC-19 第 8 步）。"""
        if not self._running:
            return
        self._running = False
        hwnd = self._hwnd
        if hwnd:
            try:
                w32.user32.PostMessageW(wt.HWND(hwnd), w32.WM_DESTROY, 0, 0)
            except Exception:  # noqa: BLE001
                pass
        if self._thread:
            self._thread.join(timeout)
            self._thread = None

    def is_running(self):
        return bool(self._running and self._thread is not None and self._thread.is_alive())

    def is_paused(self):
        return self._paused

    def set_paused(self, paused):
        """暂停/恢复监听（UC-14）。

        暂停时仍然接收系统通知但立即丢弃（主事件流第 3 步）；
        恢复后不补录暂停期间的内容（业务规则 R2）。
        """
        self._paused = bool(paused)
        if self.logger:
            self.logger.info("剪贴板监听已%s", "暂停" if self._paused else "恢复")
        return self._paused

    # ================================================================ 事件
    def poll_events(self, max_items=64):
        """取出待处理事件（供界面的定时回调消费）。"""
        out = []
        for _ in range(max_items):
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        return out

    def _emit(self, kind, payload=None):
        self._events.put(ListenerEvent(kind, payload))

    def _wake(self):
        """唤醒消息线程去消费排队的命令。"""
        if self._hwnd:
            try:
                w32.user32.PostMessageW(wt.HWND(self._hwnd), WM_RUN_COMMANDS, 0, 0)
            except Exception:  # noqa: BLE001
                pass

    # ================================================================ 消息线程
    def _run(self):
        hinst = w32.kernel32.GetModuleHandleW(None)
        class_name = "ClipboardProMessageWindow"
        wndclass = w32.WNDCLASSW()
        self._wndproc_ref = w32.WNDPROCTYPE(self._wnd_proc)
        wndclass.lpfnWndProc = self._wndproc_ref
        wndclass.hInstance = hinst
        wndclass.lpszClassName = class_name

        atom = w32.user32.RegisterClassW(ctypes.byref(wndclass))
        if not atom and self.logger:
            # 类名已被本进程注册过（例如重启监听）不算致命错误
            self.logger.debug("RegisterClassW 返回 0：%s", w32.last_error())

        hwnd = w32.user32.CreateWindowExW(
            0, class_name, "clipboardpro", 0, 0, 0, 0, 0,
            wt.HWND(w32.HWND_MESSAGE), None, hinst, None,
        )
        if not hwnd:
            self._running = False
            self._emit(EVENT_ERROR, {"message": "监听窗口创建失败", "code": w32.last_error()})
            if self.logger:
                self.logger.error("消息窗口创建失败：%s", w32.last_error())
            return
        self._hwnd = int(hwnd)

        if not w32.user32.AddClipboardFormatListener(hwnd):
            self._running = False
            self._emit(EVENT_ERROR, {
                "message": "剪贴板监听启动失败，可在设置中重新注册",
                "code": w32.last_error(),
            })
            if self.logger:
                self.logger.error("AddClipboardFormatListener 失败：%s", w32.last_error())
            return

        self._register_all_hotkeys_on_thread()
        self._add_tray_icon_on_thread()
        if self.logger:
            self.logger.info("剪贴板监听已启动（事件驱动，hwnd=%s）", self._hwnd)
        self._emit(EVENT_STARTED, {"hwnd": self._hwnd})

        msg = w32.MSG()
        while self._running:
            ret = w32.user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
            if ret in (0, -1):          # WM_QUIT 或出错
                break
            w32.user32.TranslateMessage(ctypes.byref(msg))
            w32.user32.DispatchMessageW(ctypes.byref(msg))

        # ---- 退出清理
        self._unregister_all_hotkeys_on_thread()
        self._remove_tray_icon_on_thread()
        try:
            w32.user32.RemoveClipboardFormatListener(hwnd)
        except Exception:  # noqa: BLE001
            pass
        try:
            w32.user32.DestroyWindow(hwnd)
        except Exception:  # noqa: BLE001
            pass
        self._hwnd = None
        if self.logger:
            self.logger.info("剪贴板监听已停止")

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == w32.WM_CLIPBOARDUPDATE:
                self._on_clipboard_update()
                return 0
            if msg == w32.WM_HOTKEY:
                self._on_hotkey(int(wparam))
                return 0
            if msg == w32.WM_TRAYICON:
                self._on_tray_message(int(lparam))
                return 0
            if msg == WM_RUN_COMMANDS:
                self._drain_hotkey_commands()
                return 0
            if msg == w32.WM_DESTROY:
                w32.user32.PostQuitMessage(0)
                return 0
        except Exception as exc:  # noqa: BLE001 - 消息回调里绝不能让异常冒出去
            if self.logger:
                self.logger.exception("消息处理异常 msg=%s：%s", msg, exc)
        return w32.user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    # ---------------------------------------------------------- 剪贴板变化
    def _on_clipboard_update(self):
        if self._paused:
            # 暂停期间收到通知直接丢弃，不产生记录（UC-14 主事件流第 3 步）
            return
        now = time.time()
        # 部分软件复制一次会连发两次通知，50ms 内的重复通知合并为一次，
        # 保证"同一次复制只产生一次采集"（UC-01 业务规则 R2）
        if now - self._last_update_at < 0.05:
            return
        self._last_update_at = now
        # 来源必须在变化通知的同一刻立刻取，晚一步前台窗口就可能变了
        info = source_mod.get_source_app_info()
        self._emit(EVENT_CLIPBOARD_UPDATE, info)

    def _on_hotkey(self, hotkey_id):
        for action, ident in HOTKEY_IDS.items():
            if ident == hotkey_id:
                self._emit(EVENT_HOTKEY, {"action": action})
                return

    def _on_tray_message(self, lparam):
        event = lparam & 0xFFFF
        if event in (w32.WM_LBUTTONUP, w32.WM_LBUTTONDBLCLK):
            self._dispatch_tray(MENU_SHOW)
        elif event in (w32.WM_RBUTTONUP, w32.WM_CONTEXTMENU):
            self._show_tray_menu_on_thread()

    def _dispatch_tray(self, command):
        action = TRAY_COMMANDS.get(command)
        if not action:
            return
        if callable(self.tray_callback):
            try:
                self.tray_callback(action)
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.warning("托盘命令 %s 执行失败：%s", action, exc)

    # ---------------------------------------------------------- 快捷键
    def _register_all_hotkeys_on_thread(self):
        for action, accelerator in list(self._hotkeys.items()):
            if accelerator:
                self._register_with_fallback(action, accelerator)

    def preferred_accelerator(self, action):
        """该动作实际生效的快捷键（可能是候选里的某一个，不一定是配置值）。"""
        return self._effective.get(action) or self._hotkeys.get(action)

    def registration_report(self):
        """返回一份"每个动作最终用了哪个键"的报告，供界面提示与诊断。"""
        return {
            "configured": dict(self._hotkeys),
            "effective": {a: self._effective.get(a, self._hotkeys.get(a))
                          for a in self._hotkeys},
            "substituted": dict(self._substituted),
        }

    def _register_with_fallback(self, action, accelerator):
        """按"配置值 → 候选列表"的顺序尝试注册，返回是否成功。

        MOD_NOREPEAT 的说明：按住不放时只触发一次，
        否则用户按住 Ctrl+Shift+V 会让面板疯狂开关。
        """
        hotkey_id = HOTKEY_IDS.get(action)
        if hotkey_id is None or not self._hwnd:
            return False

        tried = []
        candidates = [accelerator]
        for candidate in HOTKEY_CANDIDATES.get(action, ()):
            if candidate not in candidates:
                candidates.append(candidate)

        for candidate in candidates:
            try:
                modifiers, vk = paste_mod.parse_accelerator(candidate)
            except AppError as exc:
                if self.logger:
                    self.logger.warning("快捷键 %s=%s 非法，跳过：%s", action, candidate, exc)
                continue
            tried.append(candidate)
            ok = w32.user32.RegisterHotKey(
                wt.HWND(self._hwnd), hotkey_id, modifiers | w32.MOD_NOREPEAT, vk
            )
            if ok:
                self._registered[action] = candidate
                self._effective[action] = candidate
                if candidate != accelerator:
                    # 配置里的键被别人占了，用了替代键：必须告诉用户，
                    # 否则他按配置里那个键没反应，只会觉得"快捷键坏了"
                    self._substituted[action] = {
                        "configured": accelerator, "effective": candidate,
                    }
                    message = ("快捷键 %s 已被其他程序占用，已自动改用 %s"
                               "（可在设置里自行更换）" % (accelerator, candidate))
                    self._emit(EVENT_HOTKEY_CHANGED, dict(self._substituted[action]))
                    self._emit(EVENT_ERROR, {"message": message, "level": "warning"})
                    if self.logger:
                        self.logger.warning(message)
                elif self.logger:
                    self.logger.info("已注册全局快捷键 %s = %s", action, candidate)
                return True
            if self.logger:
                self.logger.info("快捷键 %s=%s 注册失败（err=%s），尝试下一个候选",
                                 action, candidate, w32.last_error())

        # 全部候选都失败：给出明确的失败信号，而不是静默无声
        self._failed[action] = tried
        message = ("快捷键 %s 及其全部备选组合都被其他程序占用，"
                   "暂时无法用键盘唤出面板 —— 请点击托盘图标，"
                   "并在「设置 → 快捷键」里换一个组合。" % accelerator)
        self._emit(EVENT_HOTKEY_FAILED, {"action": action, "tried": tried})
        self._emit(EVENT_ERROR, {"message": message, "level": "error"})
        if self.logger:
            self.logger.error(message)
        return False

    def _register_one(self, action, accelerator):
        """兼容旧调用：注册单个快捷键（失败不抛异常）。"""
        return self._register_with_fallback(action, accelerator)

    def _unregister_action(self, action):
        hotkey_id = HOTKEY_IDS.get(action)
        if hotkey_id is None or not self._hwnd:
            return
        try:
            w32.user32.UnregisterHotKey(wt.HWND(self._hwnd), hotkey_id)
        except Exception:  # noqa: BLE001
            pass
        self._registered.pop(action, None)
        self._effective.pop(action, None)

    def _unregister_all_hotkeys_on_thread(self):
        if not self._hwnd:
            return
        for action in list(HOTKEY_IDS):
            self._unregister_action(action)

    def _drain_hotkey_commands(self):
        while True:
            try:
                command, action, accelerator = self._pending_hotkey_commands.get_nowait()
            except queue.Empty:
                return
            try:
                if command == "__tooltip__":
                    self._update_tray_tooltip_on_thread(accelerator)
                elif command == "set":
                    # 用户主动在设置里改的快捷键：只认这一个，
                    # **不做候选降级** —— 用户明确要这个键，
                    # 悄悄换成别的组合会让人以为设置没生效
                    previous = self._registered.get(action)
                    self._unregister_action(action)
                    if self._register_exact(action, accelerator):
                        self._hotkeys[action] = accelerator
                        self._substituted.pop(action, None)
                        self._failed.pop(action, None)
                    else:
                        # 注册失败必须保留原快捷键（UC-16 业务规则 R2）
                        if previous:
                            self._register_exact(action, previous)
                            self._hotkeys[action] = previous
                        self._emit(EVENT_ERROR, {
                            "message": "快捷键 %s 注册失败（可能被其他程序占用），"
                                       "已%s" % (accelerator,
                                               "保留原快捷键 %s" % previous if previous
                                               else "暂时没有可用快捷键，请换一个组合"),
                            "level": "error",
                        })
                elif command == "reapply":
                    self._unregister_all_hotkeys_on_thread()
                    self._hotkeys = dict(accelerator or {})
                    self._substituted.clear()
                    self._failed.clear()
                    self._register_all_hotkeys_on_thread()
            except Exception as exc:  # noqa: BLE001
                if self.logger:
                    self.logger.warning("处理快捷键命令失败：%s", exc)

    def _register_exact(self, action, accelerator):
        """只注册指定的这一个组合，不做候选降级。"""
        hotkey_id = HOTKEY_IDS.get(action)
        if hotkey_id is None or not self._hwnd:
            return False
        try:
            modifiers, vk = paste_mod.parse_accelerator(accelerator)
        except AppError:
            return False
        ok = w32.user32.RegisterHotKey(
            wt.HWND(self._hwnd), hotkey_id, modifiers | w32.MOD_NOREPEAT, vk
        )
        if ok:
            self._registered[action] = accelerator
            self._effective[action] = accelerator
            if self.logger:
                self.logger.info("已注册全局快捷键 %s = %s", action, accelerator)
        elif self.logger:
            self.logger.warning("注册快捷键失败 %s=%s（err=%s）",
                                action, accelerator, w32.last_error())
        return bool(ok)

    # ------------------------------------------------- 供其他线程调用的入口
    def set_hotkey(self, action, accelerator):
        """重新注册快捷键（UC-16）。必须转回消息线程执行。"""
        if action not in HOTKEY_IDS:
            raise AppError(ERR_INVALID_ARG, "未知的快捷键动作", action)
        # 先做参数校验，让调用方立刻拿到错误，而不是异步失败
        paste_mod.validate_accelerator(accelerator)
        self._pending_hotkey_commands.put(("set", action, accelerator))
        self._wake()

    def reapply_hotkeys(self, hotkeys):
        self._pending_hotkey_commands.put(("reapply", None, dict(hotkeys or {})))
        self._wake()

    def update_tray_tooltip(self, text):
        self._tray_tooltip = text
        self._pending_hotkey_commands.put(("__tooltip__", None, text))
        self._wake()

    # ---------------------------------------------------------- 托盘
    def _build_notify_data(self, flags):
        data = w32.NOTIFYICONDATAW()
        data.cbSize = ctypes.sizeof(w32.NOTIFYICONDATAW)
        data.hWnd = wt.HWND(self._hwnd)
        data.uID = TRAY_UID
        data.uFlags = flags
        data.uCallbackMessage = w32.WM_TRAYICON
        data.hIcon = self._icon
        data.szTip = self._tray_tooltip[:127]
        return data

    def _add_tray_icon_on_thread(self):
        if not self._hwnd:
            return False
        try:
            self._icon = w32.user32.LoadIconW(None, wt.LPCWSTR(w32.IDI_APPLICATION))
            data = self._build_notify_data(w32.NIF_MESSAGE | w32.NIF_ICON | w32.NIF_TIP)
            ok = w32.shell32.Shell_NotifyIconW(w32.NIM_ADD, ctypes.byref(data))
            self._tray_added = bool(ok)
            if not ok and self.logger:
                self.logger.warning("托盘图标创建失败（不影响快捷键与主窗口使用）")
            return self._tray_added
        except Exception as exc:  # noqa: BLE001 - 托盘失败不能影响启动（UC-19 异常流 E2）
            if self.logger:
                self.logger.warning("托盘初始化异常：%s", exc)
            return False

    def _update_tray_tooltip_on_thread(self, text):
        if not self._tray_added:
            return
        try:
            data = self._build_notify_data(w32.NIF_TIP)
            w32.shell32.Shell_NotifyIconW(w32.NIM_MODIFY, ctypes.byref(data))
        except Exception:  # noqa: BLE001
            pass

    def _remove_tray_icon_on_thread(self):
        if not self._tray_added or not self._hwnd:
            return
        try:
            data = self._build_notify_data(0)
            w32.shell32.Shell_NotifyIconW(w32.NIM_DELETE, ctypes.byref(data))
        except Exception:  # noqa: BLE001
            pass
        self._tray_added = False

    def _show_tray_menu_on_thread(self):
        """弹出托盘右键菜单。

        TrackPopupMenu 要求调用窗口是前台窗口，否则菜单一点就消失，
        因此先 SetForegroundWindow；用 TPM_RETURNCMD 直接拿到命令号，
        比在 WM_COMMAND 里绕一圈更可靠。
        """
        if not self._hwnd:
            return
        menu = None
        try:
            menu = w32.user32.CreatePopupMenu()
            for command, label in MENU_LABELS:
                if command == MENU_EXIT:
                    w32.append_menu(menu, w32.MF_SEPARATOR, 0, None)
                text = label
                if command == MENU_TOGGLE_LISTEN and self._paused:
                    text = "恢复监听(&R)"
                w32.append_menu(menu, w32.MF_STRING, command, text)
            w32.user32.SetMenuDefaultItem(menu, MENU_SHOW, 0)

            x, y = w32.get_cursor_pos()
            hwnd = wt.HWND(self._hwnd)
            w32.user32.SetForegroundWindow(hwnd)
            selected = w32.user32.TrackPopupMenu(
                menu, w32.TPM_RIGHTBUTTON | w32.TPM_RETURNCMD, x, y, 0, hwnd, None
            )
            # 菜单关闭后补一条空消息，避免菜单残影（MSDN 明确要求）
            w32.user32.PostMessageW(hwnd, 0, 0, 0)
            if selected:
                self._dispatch_tray(int(selected))
        except Exception as exc:  # noqa: BLE001
            if self.logger:
                self.logger.warning("托盘菜单异常：%s", exc)
        finally:
            if menu:
                try:
                    w32.user32.DestroyMenu(menu)
                except Exception:  # noqa: BLE001
                    pass

    # ---------------------------------------------------------- 诊断
    def diagnostics(self):
        return {
            "running": self.is_running(),
            "paused": self._paused,
            "hwnd": self._hwnd,
            "registeredHotkeys": dict(self._registered),
            "trayAdded": self._tray_added,
        }
