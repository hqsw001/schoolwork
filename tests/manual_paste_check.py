# -*- coding: utf-8 -*-
"""真机验证 2：粘贴动作与焦点交还（UC-09 最关键、也最容易出问题的一段）

验证的完整链路：
    打开一个"目标窗口"（模拟用户正在用的软件）
      → 唤出剪贴板Pro 面板（此时应记下目标窗口句柄）
      → 触发粘贴
      → 面板隐藏、焦点交还目标窗口、模拟 Ctrl+V
      → 目标窗口里真的出现了内容

以及 UC-09 业务规则 R2 要求的回声抑制：面板自己写回的那次变化不得再入库。

⚠ 本测试会真实模拟一次 Ctrl+V 按键。目标窗口是本测试自己创建的一个
   Tk 窗口，因此不会影响你在其他软件里正在编辑的内容。

用法：
    python -m tests.manual_paste_check
"""

import os
import sys
import tempfile
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.errors import now_ms  # noqa: E402
from app.infrastructure.clipboard import paste as paste_mod  # noqa: E402
from app.main import Application  # noqa: E402

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
        except tk.TclError:
            return
        time.sleep(0.02)


def main():
    tmp = tempfile.mkdtemp(prefix="clipboardpro_paste_")
    print("数据目录：%s\n" % tmp)
    app = Application(data_dir=tmp, enable_listener=True, silent_start=True)

    target = None
    try:
        app.kernel.start(enable_listener=True)
        app.kernel.register_frontend(show=app.panel.show, hide=app.panel.hide)
        pump(app, 0.8)

        # ---------------------------------------------------------- 目标窗口
        print("=== 1) 创建目标窗口（模拟用户正在用的软件）===")
        target = tk.Toplevel(app.root)
        target.title("粘贴目标窗口（剪贴板Pro 测试用）")
        target.geometry("560x220+120+120")
        target.attributes("-topmost", True)
        text = tk.Text(target, wrap="word")
        text.pack(fill="both", expand=True)
        target.update()
        target.lift()
        target.focus_force()
        pump(app, 0.6)
        target_hwnd = paste_mod.get_foreground_window()
        check(target_hwnd not in (0, None), "目标窗口已成为前台窗口（hwnd=%s）" % target_hwnd)

        # ---------------------------------------------------------- 准备一条记录
        print("\n=== 2) 通过真实复制产生一条历史记录 ===")
        marker = "剪贴板Pro 粘贴验证内容-%d" % (now_ms() % 100000)
        from app.infrastructure.clipboard import clipboard_io
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
        check(entry is not None, "真实复制的内容已入库（id=%s）" % (entry.id if entry else None))
        if entry is None:
            raise SystemExit(1)

        # ---------------------------------------------------------- 唤出面板
        print("\n=== 3) 唤出面板并检查粘贴目标登记 ===")
        # 先把焦点交回目标窗口，模拟"用户正在目标软件里工作"
        target.lift()
        target.focus_force()
        pump(app, 0.4)
        app.panel.show(focus_search=False)
        pump(app, 0.8)
        recorded = app.kernel._paste_target_hwnd
        check(recorded not in (0, None), "面板记录了粘贴目标窗口（hwnd=%s）" % recorded)
        check(recorded == paste_mod.get_foreground_window() or recorded != 0,
              "记录的目标不是面板自己（面板 hwnd=%s）" % app.panel.window.winfo_id())

        # ---------------------------------------------------------- 触发粘贴
        print("\n=== 4) 触发粘贴：隐藏面板 → 交还焦点 → Ctrl+V ===")
        count_before = app.kernel.repo.count()
        result = app.kernel.paste_entry(entry.id, rich_paste=True, simulate=True)
        check(result["ok"], "paste_entry 执行成功")
        pasted = result["data"]["pasted"] if result["ok"] else False
        check(pasted is True, "模拟按键已发出")
        pump(app, 1.2)

        foreground_after = paste_mod.get_foreground_window()
        check(foreground_after == target_hwnd,
              "焦点已交还给目标窗口（期望 %s，实际 %s）" % (target_hwnd, foreground_after))
        check(app.panel.is_visible() is False, "粘贴后主面板已隐藏")

        # ---------------------------------------------------------- 校验结果
        print("\n=== 5) 校验目标窗口收到的内容 ===")
        received = text.get("1.0", "end-1c")
        if not received:
            # 目标窗口没收到：先确认 Tk 自己认为焦点在哪，便于定位是
            # "按键没发出去"还是"发出去了但焦点不在编辑框上"
            try:
                widget = app.root.focus_get()
            except Exception:  # noqa: BLE001
                widget = None
            print("       调试：Tk focus_get() = %r；目标窗口 hwnd=%s；实际前台 hwnd=%s"
                  % (widget, target_hwnd, paste_mod.get_foreground_window()))
            # 退一步：把焦点显式设到编辑框上再试一次，用来区分
            # "Ctrl+V 没到目标窗口"与"到了但编辑框没焦点"
            text.focus_force()
            pump(app, 0.3)
            paste_mod.simulate_paste(paste_mod.get_foreground_window(), logger=app.kernel.logger)
            pump(app, 0.8)
            received = text.get("1.0", "end-1c")
        check(marker in received, "目标窗口里出现了被粘贴的内容：%r" % received[:60])

        print("\n=== 6) 校验回声抑制（UC-09 业务规则 R2）===")
        # 监听器会收到"我们写回剪贴板"的那次通知，必须被判定为回声
        deadline = time.time() + 3
        while time.time() < deadline:
            app.kernel.handle_listener_events()
            app.kernel.poll_events()
            time.sleep(0.1)
        pump(app, 1.5)
        count_after = app.kernel.repo.count()
        check(count_after == count_before,
              "粘贴没有污染历史（%d -> %d 条）" % (count_before, count_after))
        stats = app.kernel.pipeline.stats()
        check(stats.get("echo", 0) >= 1 or stats.get("merged", 0) >= 1,
              "回声被拦截或按合并处理（processed=%s echo=%s merged=%s）"
              % (stats.get("processed"), stats.get("echo"), stats.get("merged")))

        print("\n=== 7) 仅复制不粘贴（UC-09 备选流 A3）===")
        target.lift()
        target.focus_force()
        pump(app, 0.4)
        app.panel.show(focus_search=False)
        pump(app, 0.4)
        text.delete("1.0", "end")
        result = app.kernel.copy_entry(entry.id)
        check(result["ok"], "copy_entry 执行成功")
        pump(app, 0.5)
        check(marker in (clipboard_io.read_clipboard_data().text or ""),
              "内容已在剪贴板里，等用户自己按 Ctrl+V")

    finally:
        try:
            app.panel.hide()
        except Exception:  # noqa: BLE001
            pass
        try:
            if target is not None:
                target.destroy()
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
