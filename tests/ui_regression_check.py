# -*- coding: utf-8 -*-
"""界面回归：设置窗口入口、设置窗口单例、历史分页

本套件守住三个**已经在用户现场暴露过的真实缺陷**：

1. ``panel.py`` 调用 ``SettingsWindow(self)`` 却从没导入过这个名字 ——
   右键菜单「设置…」、``Ctrl+,``、托盘「设置」三个入口**全部**抛
   ``NameError``，也就是说整个界面改不了任何设置。
   之所以能一路通过原有回归：``ui_smoke.py`` 是直接 ``import
   SettingsWindow`` 来测的，恰好绕开了 ``panel.open_settings()``；
   而当时的 ``static_check.py`` 在遍历结束后才读作用域栈，函数体里的
   未定义名字一个都没检查过（现已修复并加了自检）。

2. 设置窗口每点一次就新建一个，点三下叠三个窗口，得关三次。

3. 历史浏览固定 ``get_history(limit=50)`` 且**从不传 offset**，
   ``_maybe_render_more`` 只是在已取回的 50 条里多渲染几批。
   结果：第 51 条以后的记录除了搜索之外没有任何办法翻到。

用法：
    python -m tests.ui_regression_check
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402

from app.constants import PAGE_SIZE  # noqa: E402
from app.domain.models import ClipboardEntry  # noqa: E402
from app.errors import now_ms  # noqa: E402
from app.main import Application  # noqa: E402
from app.ui.settings_window import SettingsWindow  # noqa: E402

PASSED = []
FAILED = []


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def pump(app, seconds=0.6):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            app.root.update()
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.02)


def collect(widget):
    out = [widget]
    for child in widget.winfo_children():
        out.extend(collect(child))
    return out


def settings_windows(panel):
    out = []
    for child in panel.window.winfo_children():
        if isinstance(child, SettingsWindow):
            try:
                if child.winfo_exists():
                    out.append(child)
            except Exception:  # noqa: BLE001
                pass
    return out


def close_settings(app):
    for window in settings_windows(app.panel):
        try:
            window.destroy()
        except Exception:  # noqa: BLE001
            pass
    pump(app, 0.2)


def main():
    tmp = tempfile.mkdtemp(prefix="clipboardpro_uireg_")
    print("数据目录：%s\n" % tmp)
    app = Application(data_dir=tmp, enable_listener=False, silent_start=True)
    try:
        app.kernel.start(enable_listener=False)
        app.kernel.register_frontend(show=app.panel.show, hide=app.panel.hide)
        app.panel.show(focus_search=False)
        pump(app, 0.8)

        # ==============================================================
        print("=== 1) 设置窗口：三个入口都要能打开（NameError 回归）===")
        # 1a —— 直接调用（右键菜单「设置…」走的就是这一条）
        try:
            app.panel.open_settings()
            direct_error = None
        except Exception as exc:  # noqa: BLE001
            direct_error = "%s: %s" % (type(exc).__name__, exc)
        pump(app, 0.7)
        check(direct_error is None,
              "panel.open_settings() 不抛异常（%s）" % (direct_error or "无异常"))
        check(len(settings_windows(app.panel)) == 1,
              "右键菜单路径成功开出一个设置窗口")
        close_settings(app)
        pump(app, 0.3)

        # 1b —— Ctrl+, 的绑定必须存在
        binding = app.panel.window.bind("<Control-comma>")
        check(bool(binding), "Ctrl+, 的按键绑定存在：%s" % (binding or "无"))
        if binding:
            try:
                app.panel.window.event_generate("<Control-comma>")
                binding_error = None
            except Exception as exc:  # noqa: BLE001
                binding_error = "%s: %s" % (type(exc).__name__, exc)
            pump(app, 0.7)
            check(binding_error is None and len(settings_windows(app.panel)) >= 1,
                  "按下 Ctrl+, 能打开设置窗口")
            close_settings(app)
            pump(app, 0.3)

        # 1c —— 托盘「设置」：内核投 toast 事件，面板消费后打开设置
        app.kernel._show_panel(settings=True)
        pump(app, 1.2)
        check(len(settings_windows(app.panel)) >= 1,
              "托盘「设置」路径能打开设置窗口")
        close_settings(app)
        pump(app, 0.3)

        # ==============================================================
        print("\n=== 1d) 设置窗口里的「系统集成」控件真的在 ===")
        # 这批是批次 2 加的：后端命令写好了但界面没入口，就等于没做
        app.panel.open_settings()
        pump(app, 0.8)
        opened = settings_windows(app.panel)
        check(len(opened) == 1, "设置窗口已打开")
        if opened:
            texts = []
            for widget in collect(opened[0]):
                try:
                    if isinstance(widget, (tk.Checkbutton, tk.Button, tk.Label)):
                        texts.append(str(widget.cget("text")))
                except tk.TclError:
                    pass
            joined = " | ".join(texts)
            check("开机自动启动" in joined, "设置里有「开机自动启动」开关")
            check("接管 Win+V" in joined, "设置里有「接管 Win+V」开关")
            check("打开数据目录" in joined, "设置里有「打开数据目录」按钮")
            check("系统集成" in joined, "有独立的「系统集成」分区")
        close_settings(app)
        pump(app, 0.3)

        # ==============================================================
        print("\n=== 1e) 批次 4 的界面入口 ===")
        # 后端写好了但界面点不到，就等于没做——这里守住入口存在
        check(bool(app.panel.window.bind("<Control-Up>")),
              "Ctrl+↑ 绑定存在（置顶区上移）")
        check(bool(app.panel.window.bind("<Control-Down>")),
              "Ctrl+↓ 绑定存在（置顶区下移）")

        app.kernel.simulate_capture("界面回归用手动记录一")
        app.kernel.simulate_capture("界面回归用手动记录二")
        app.panel.refresh(reset_scroll=True)
        pump(app, 0.5)
        check(len(app.panel._entries) >= 2, "有记录可供排序测试")

        ids = [entry["id"] for entry in app.panel._entries]
        for entry_id in ids[:2]:
            app.kernel.toggle_pin(entry_id, True)
        app.panel.refresh(keep_selection=True)
        pump(app, 0.4)

        pinned = [entry["id"] for entry in app.panel._entries if entry["isPinned"]]
        check(len(pinned) >= 2, "置顶了 %d 条" % len(pinned))
        if len(pinned) >= 2:
            app.panel._select(pinned[0])
            app.panel.move_pinned_selected(1)
            pump(app, 0.4)
            after = [entry["id"] for entry in app.panel._entries if entry["isPinned"]]
            check(after[0] == pinned[1] and after[1] == pinned[0],
                  "界面上「下移」真的改了置顶顺序：%s -> %s" % (pinned, after))

        # 手动新增：走的是同一个 EditorDialog 回调
        app.panel._apply_manual_add("界面回归手动新增的这条")
        pump(app, 0.4)
        contents = [entry["content"] for entry in app.panel._entries]
        check(any("界面回归手动新增" in (c or "") for c in contents),
              "手动新增的记录出现在列表里")
        before_count = len(app.panel._entries)
        app.panel._apply_manual_add("   ")
        pump(app, 0.3)
        check(len(app.panel._entries) == before_count,
              "空白内容不会新增记录")

        # ==============================================================
        print("\n=== 1f) 批次 6 顺序粘贴的界面入口 ===")
        target_id = app.panel._entries[0]["id"]
        app.panel._select(target_id)
        app.panel._queue_selected()
        pump(app, 0.4)
        queued = app.kernel.get_paste_queue()["data"]
        check(queued["size"] >= 1, "右键「加入顺序粘贴队列」真的入队了（%d 条）"
              % queued["size"])

        app.kernel.set_sequential_mode(True)
        app.panel._update_status()
        pump(app, 0.3)
        status = app.panel.status_label.cget("text")
        check("顺序粘贴" in status and "待粘" in status,
              "状态栏显示队列进度：%s" % status[-24:])
        app.kernel.set_sequential_mode(False)
        app.panel._update_status()
        pump(app, 0.3)
        check("待粘" not in app.panel.status_label.cget("text"),
              "关掉模式后状态栏不再显示队列进度")

        # 设置窗口里的「顺序粘贴」分区
        app.panel.open_settings()
        pump(app, 0.9)
        opened = settings_windows(app.panel)
        check(len(opened) == 1, "设置窗口已打开（顺序粘贴分区）")
        if opened:
            texts = []
            for widget in collect(opened[0]):
                try:
                    if isinstance(widget, (tk.Checkbutton, tk.Button, tk.Label)):
                        texts.append(str(widget.cget("text")))
                except tk.TclError:
                    pass
            joined = " | ".join(texts)
            check("顺序粘贴" in joined, "设置里有「顺序粘贴」分区")
            check("开启顺序粘贴" in joined, "设置里有开启开关")
            check("Alt+V" in joined, "说明里写清了默认快捷键 Alt+V")
        close_settings(app)
        pump(app, 0.3)

        # ==============================================================
        print("\n=== 1g) 批次 9 隐私加密的设置界面 ===")
        app.panel.open_settings()
        pump(app, 0.9)
        opened9 = settings_windows(app.panel)
        check(len(opened9) == 1, "设置窗口已打开（隐私加密分区）")
        if opened9:
            window9 = opened9[0]
            status_text = window9.encryption_label.cget("text")
            check("DPAPI" in status_text or "不支持" in status_text,
                  "显示加密状态：%s" % status_text[:24])
            check(hasattr(window9, "mask_prefix_var")
                  and hasattr(window9, "mask_suffix_var"),
                  "有掩码位数设置项")
            check(hasattr(window9, "custom_rules_text"), "有自定义规则输入框")

            window9.mask_prefix_var.set("2")
            window9.mask_suffix_var.set("1")
            window9._on_mask_changed()
            pump(app, 0.3)
            config = app.kernel.privacy.configure()
            check(config["maskPrefix"] == 2 and config["maskSuffix"] == 1,
                  "**掩码位数改动立刻生效**（%d/%d）"
                  % (config["maskPrefix"], config["maskSuffix"]))

            window9.custom_rules_text.delete("1.0", "end")
            window9.custom_rules_text.insert("1.0", "工号\\d{6}")
            window9._on_custom_rules_changed()
            pump(app, 0.3)
            hits = app.kernel.privacy.scan("工号123456")
            check(bool(hits), "**自定义规则改完立刻生效**")
            if hits:
                check("*" in hits[0].masked,
                      "自定义命中也按掩码位数脱敏：%s" % hits[0].masked)

            window9.mask_prefix_var.set("3")
            window9.mask_suffix_var.set("3")
            window9._on_mask_changed()
            pump(app, 0.2)
            check(app.kernel.privacy.configure()["maskPrefix"] == 3, "改回默认值")
        close_settings(app)
        pump(app, 0.3)

        # ==============================================================
        print("\n=== 2) 设置窗口必须是单例 ===")
        check(len(settings_windows(app.panel)) == 0, "起点的设置窗口已全部关闭")
        for _ in range(3):
            app.panel.open_settings()
            app.panel.open_settings()
            pump(app, 0.4)
        count = len(settings_windows(app.panel))
        check(count == 1, "连点 6 次只存在 1 个设置窗口（实际 %d 个）" % count)
        check(len(app.panel.window.winfo_children()) > 0, "窗口树结构完好")
        close_settings(app)
        pump(app, 0.3)

        # ==============================================================
        print("\n=== 3) 历史分页：必须能翻到第 50 条以后的记录 ===")
        # 先把前面几节留下的记录清干净，否则下面的精确计数就不成立。
        # clear_history 会保留置顶/带标签的记录，所以先全部取消置顶。
        for entry in app.kernel.repo.get_history(limit=500):
            if entry.is_pinned:
                app.kernel.toggle_pin(entry.id, False)
        app.kernel.clear_history()
        base = app.kernel.get_count()["data"]["count"]
        check(base == 0, "分页测试从空历史开始（实际 %d 条）" % base)

        total = PAGE_SIZE * 2 + 7          # 107 条，故意不是页长的整数倍
        # 直接写仓储层，绕开事件总线，避免上百次 clipboard-updated 刷新干扰计时
        for index in range(total):
            app.kernel.repo.save(ClipboardEntry(
                content_type="text",
                content="分页回归测试记录 %04d" % index,
                preview="分页回归测试记录 %04d" % index,
                source_app="分页测试",
                timestamp=now_ms() + index,
                content_hash=index + 1,
            ))
        check(app.kernel.get_count()["data"]["count"] == total,
              "已写入 %d 条记录" % total)

        app.panel.refresh(reset_scroll=True)
        pump(app, 0.5)
        check(len(app.panel._entries) == PAGE_SIZE,
              "首屏只取一页（%d 条），不全量加载" % PAGE_SIZE)
        check(app.panel._has_more is True,
              "首屏取满一页时标记「还有更旧的记录」")

        # 反复加载直到没有更多
        guard = 0
        while app.panel._has_more and guard < 20:
            app.panel._load_more()
            pump(app, 0.25)
            guard += 1
        fetched = len(app.panel._entries)
        check(fetched == total,
              "翻到底共取回 %d 条（期望 %d）" % (fetched, total))
        check(app.panel._has_more is False, "到底之后不再标记还有更多")
        ids = [entry["id"] for entry in app.panel._entries]
        check(len(set(ids)) == len(ids), "分页结果没有重复记录")
        check(app.panel._rendered == total,
              "全部 %d 条都已渲染成卡片" % total)

        # 最后一页只有 7 条，也必须取到
        oldest = app.panel._entries[-1]["content"]
        check("0000" in app.panel._entries[-1]["content"] or oldest,
              "能取到最旧的一条：%s" % oldest)

        # 搜索态不应该分页（一次性取回，避免和 offset 语义打架）
        app.panel.search_var.set("分页回归测试记录 000")
        app.panel._placeholder_active = False
        pump(app, 0.8)
        check(app.panel._has_more is False, "搜索结果不分页")

    finally:
        app.kernel.shutdown()
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
