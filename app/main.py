# -*- coding: utf-8 -*-
"""剪贴板Pro 单机版入口

用法：
    python -m app.main                以正常方式启动（键盘监听 + 托盘常驻）
    python -m app.main --show         启动后立刻显示主面板
    python -m app.main --no-listener  只开界面，不注册剪贴板监听（调试界面用）
    python -m app.main --console      保留控制台输出（默认在无控制台环境下自我适配）
    python -m app.main --data-dir "D:\\clipboardpro-data"   指定数据目录

## 关于"关掉终端程序会不会退出"

会。这是 Windows 的规则：控制台程序（python.exe）与控制台是**绑定**的，
关掉控制台窗口时系统会向该控制台里的所有进程发 CTRL_CLOSE_EVENT，
进程随后被结束 —— 快捷键自然也就失效了。

所以正式启动方式是**无控制台模式**（由 `启动剪贴板Pro.bat` 负责）：
启动器用 `DETACHED_PROCESS` 把本程序脱离控制台启动，控制台立刻关闭，
程序独立存活，托盘图标与全局快捷键都还在。

本文件在 `pythonw.exe` 下也能正常工作，为此做了两点适配：
    1. 启动最开始就把 stdout / stderr 兜成哑对象 —— pythonw 下它们是 None，
       任何一处 print 或 logging 打屏都会抛 AttributeError 把程序带崩；
    2. 单实例检查放在最前面，避免重复启动导致快捷键被抢占。

设计约束（用户确认的唯一硬性标准）：
    本程序是**纯单机**工具。全部代码不发起任何网络请求，不依赖任何云端服务。
"""

import argparse
import io
import os
import sys
import tkinter as tk
from tkinter import messagebox

# 允许以 "python app/main.py" 的方式直接运行
if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class _NullStream(object):
    """哑输出流：pythonw 下 sys.stdout / sys.stderr 是 None，写它们会崩。"""

    encoding = "utf-8"

    def write(self, _text):
        return 0

    def flush(self):
        return None

    def isatty(self):
        return False

    def writable(self):
        return False

    def readable(self):
        return False


def _harden_streams():
    """保证 stdout / stderr 可用。必须在任何 print / logging 之前调用。"""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is None:
            setattr(sys, name, _NullStream())
            continue
        try:
            stream.write("")
            stream.flush()
        except Exception:  # noqa: BLE001 - 控制台已关闭或句柄失效
            setattr(sys, name, _NullStream())
    if getattr(sys, "stdin", None) is None:
        sys.stdin = io.StringIO("")


_harden_streams()

from app import APP_NAME, VERSION                      # noqa: E402
from app.app.kernel import Kernel                      # noqa: E402
from app.errors import AppError                        # noqa: E402
from app.infrastructure import single_instance         # noqa: E402
from app.infrastructure.clipboard import win32 as w32  # noqa: E402
from app.ui.panel import MainPanel                     # noqa: E402

#: 事件轮询之外的兜底保护：Kernel 无法启动时也要给出可读的提示
EXIT_OK = 0
EXIT_ERROR = 1
EXIT_ALREADY_RUNNING = 3


class Application(object):
    """把内核与界面装配在一起。"""

    def __init__(self, data_dir=None, enable_listener=True, silent_start=True):
        self.enable_listener = enable_listener
        self.silent_start = silent_start

        # ---- 1. 数据目录（UC-20 主事件流第 1 步）
        from app.infrastructure.database import default_data_dir
        from app.infrastructure.logger import setup_logger
        self.data_dir = data_dir or default_data_dir()
        self.logger = setup_logger(self.data_dir)

        # ---- 2~4. 配置 → 数据库 → 结构升级
        self.kernel = Kernel(data_dir=self.data_dir, logger=self.logger)

        # ---- 5~9. tkinter 根窗口、主面板、系统能力
        w32.set_dpi_aware()
        self.root = tk.Tk()
        self.root.withdraw()                    # 根窗口本身不显示，只做事件循环
        self.root.title(APP_NAME)
        self.kernel.ui_root = self.root

        self.panel = MainPanel(self.kernel)
        self.kernel.register_frontend(
            show=self.panel.show, hide=self.panel.hide
        )
        self.root.bind("<<ClipboardProExit>>", lambda e: self.quit())

    def run(self):
        try:
            self.kernel.start(enable_listener=self.enable_listener)
        except AppError as exc:
            self.logger.error("内核启动失败：%s", exc)
            self._fatal("启动失败：%s" % exc.message)
            return EXIT_ERROR

        if not self.silent_start:
            self.panel.show(focus_search=True)

        self.logger.info("%s v%s 已就绪（数据目录：%s）", APP_NAME, VERSION, self.data_dir)
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass
        finally:
            self.quit()
        return EXIT_OK

    def _fatal(self, message):
        """启动失败时把话说清楚：有控制台就打印，没有就弹窗。"""
        self.logger.error(message)
        try:
            if sys.stderr is not None and not isinstance(sys.stderr, _NullStream):
                print(message, file=sys.stderr)
        except Exception:  # noqa: BLE001
            pass
        try:
            messagebox.showerror(
                APP_NAME, "%s\n\n详情见日志：\n%s" % (message, os.path.join(self.data_dir, "clipboardpro.log"))
            )
        except Exception:  # noqa: BLE001
            pass

    def quit(self):
        """正常退出：注销快捷键、移除托盘、关闭数据库（UC-19 第 8 步）。"""
        try:
            self.panel.hide()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.kernel.shutdown()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("退出清理异常：%s", exc)
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:  # noqa: BLE001
            pass


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="clipboardpro", description="%s —— 单机剪贴板管理器" % APP_NAME
    )
    parser.add_argument("--data-dir", help="指定数据目录（默认放在程序包同级的 .data）")
    parser.add_argument("--no-listener", action="store_true",
                        help="不注册剪贴板监听与全局快捷键（调试界面用）")
    parser.add_argument("--show", action="store_true",
                        help="启动后立刻显示主面板（否则按配置静默驻留托盘）")
    parser.add_argument("--allow-multiple", action="store_true",
                        help="允许同时运行多个实例（仅用于调试，会导致快捷键抢占）")
    parser.add_argument("--version", action="version", version="%s %s" % (APP_NAME, VERSION))
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if not w32.is_windows():
        print("剪贴板Pro 依赖 Windows 剪贴板 API，当前系统不受支持。", file=sys.stderr)
        return EXIT_ERROR

    # ---- 单实例检查：必须放在最前面，早于读配置与注册快捷键
    guard = single_instance.SingleInstanceGuard()
    if not guard.acquire() and not args.allow_multiple:
        # 已有实例在跑：尽量把它的面板叫到前台，然后自己退出
        activated = single_instance.activate_existing_instance()
        # 给一点时间让前台切换生效，再提示（面板可能已经冒出来了）
        import time
        time.sleep(0.4)
        if not activated:
            single_instance.notify_already_running()
        return EXIT_ALREADY_RUNNING

    try:
        # 静默启动由配置决定：先读配置再决定要不要显示窗口
        app = Application(
            data_dir=args.data_dir,
            enable_listener=not args.no_listener,
            silent_start=True,
        )
        silent = bool(app.kernel.settings.get("app.silent_start", True))
        app.silent_start = silent and not args.show
        return app.run()
    finally:
        guard.release()


if __name__ == "__main__":
    sys.exit(main())
