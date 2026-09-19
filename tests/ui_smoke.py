# -*- coding: utf-8 -*-
"""界面冒烟测试：真实创建 tkinter 窗口、渲染列表、切换主题、注入事件

说明：本测试会**短暂弹出一个真实窗口**（约几秒后自动关闭），
因为 tkinter 的空状态、主题切换、右键菜单这些路径只有在真实窗口映射之后
才会走到。测试末尾自动销毁窗口，不会残留进程。

用法：
    python -m tests.ui_smoke
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.constants import (  # noqa: E402
    K_LIST_DENSITY,
    K_THEME,
    K_THEME_MODE,
    TOAST_INFO,
)
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


def pump(app, seconds=0.6):
    """跑一小段事件循环，让 tkinter 真正完成布局与重绘。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            app.root.update()
        except Exception:  # noqa: BLE001
            return
        time.sleep(0.02)


def main():
    tmp = tempfile.mkdtemp(prefix="clipboardpro_ui_")
    print("数据目录：%s" % tmp)
    app = Application(data_dir=tmp, enable_listener=False, silent_start=True)
    try:
        # ---- 启动内核（不开真实监听，避免测试期间干扰本机剪贴板与快捷键）
        app.kernel.start(enable_listener=False)
        app.kernel.register_frontend(show=app.panel.show, hide=app.panel.hide)

        print("\n=== 空状态渲染 ===")
        app.panel.show(focus_search=False)
        pump(app)
        check(app.panel.window.winfo_exists() == 1, "主面板窗口创建成功")
        check(app.panel.empty_label.winfo_ismapped() == 1, "无记录时显示空状态引导")
        check("还没有任何记录" in app.panel.empty_label.cget("text"),
              "空状态文案给出了「复制任意内容即可开始记录」的引导")

        print("\n=== 注入记录并渲染列表 ===")
        samples = [
            ("这是一段普通的中文文本，用来验证列表卡片渲染是否正常。", "记事本"),
            ("https://github.com/jimuzhe/tiez-clipboard", "Edge 浏览器"),
            ("import os\n\n\ndef main():\n    return os.getcwd()\n", "VS Code"),
            ("我的手机号是13812345678，请联系我", "微信"),
        ]
        for text, source in samples:
            result = app.kernel.simulate_capture(text, source_name=source)
            assert result["ok"], result
        app.panel.refresh(reset_scroll=True)
        pump(app)
        check(len(app.panel._entries) == 4, "列表读取到 4 条记录")
        check(len(app.panel._card_map) >= 4, "4 张卡片都已渲染（实际 %d）"
              % len(app.panel._card_map))
        types = sorted(e["contentType"] for e in app.panel._entries)
        check("url" in types and "code" in types and "text" in types,
              "类型识别结果在列表里生效：%s" % types)
        sensitive = [e for e in app.panel._entries if e["isSensitive"]]
        check(len(sensitive) == 1, "敏感记录被标记")
        check("13812345678" not in sensitive[0]["preview"], "敏感记录预览已脱敏")

        print("\n=== 检索与命中高亮 ===")
        app.panel._clear_placeholder()
        app.panel.search_var.set("中文文本")
        app.panel._run_search()
        pump(app)
        check(len(app.panel._entries) == 1, "检索「中文文本」命中 1 条")
        app.panel.search_var.set("")
        app.panel._run_search()
        pump(app)
        check(len(app.panel._entries) == 4, "清除关键字后回到完整列表")

        print("\n=== 类型筛选 ===")
        app.panel._set_type_filter("code")
        pump(app)
        check(len(app.panel._entries) == 1, "按类型筛选出 1 条代码记录")
        app.panel._set_type_filter(None)
        pump(app)

        print("\n=== 键盘导航与选中 ===")
        app.panel._select_first()
        first_id = app.panel._selected_id
        app.panel._move_selection(1)
        pump(app, 0.3)
        check(app.panel._selected_id != first_id, "↓ 键切换了选中项")
        app.panel._move_selection(-1)
        pump(app, 0.3)
        check(app.panel._selected_id == first_id, "↑ 键回到上一条")
        check(app.panel._card_map.get(first_id) is not None, "选中项在已渲染的卡片里")

        print("\n=== 置顶与标签 ===")
        app.panel._toggle_pin_selected()
        pump(app)
        pinned = [e for e in app.panel._entries if e["id"] == first_id][0]
        check(pinned["isPinned"] is True, "置顶按钮生效并即时刷新列表")
        check(app.panel._entries[0]["id"] == first_id, "置顶记录浮到列表最前")

        print("\n=== 主题与密度切换 ===")
        result = app.kernel.set_theme("sakura")
        check(result["ok"], "切换到樱花主题成功")
        app.panel.apply_theme()
        pump(app)
        check(app.panel._tokens["theme"] == "sakura", "面板令牌已更新为樱花主题")
        result = app.kernel.set_theme_mode("dark")
        check(result["ok"], "切换到深色模式成功")
        app.panel.apply_theme()
        pump(app)
        check(app.panel._tokens["mode"] == "dark", "深浅色模式解析为 dark")
        app.kernel.set_theme("mica")
        app.kernel.set_theme_mode("light")
        app.panel.apply_theme()
        pump(app)
        applied = []
        for density in ("compact", "normal", "loose"):
            result = app.kernel.set_list_density(density)
            app.panel.apply_theme()
            pump(app, 0.3)
            applied.append(app.panel.current_density() == density
                           and app.panel._metrics()["pad_y"]
                           == {"compact": 4, "normal": 7, "loose": 11}[density])
        check(all(applied), "三档列表密度切换后都正确生效（含行内边距）：%s" % applied)

        print("\n=== 设置窗口 ===")
        from app.ui.settings_window import SettingsWindow
        settings = SettingsWindow(app.panel)
        pump(app, 0.5)
        check(settings.winfo_exists() == 1, "设置窗口创建成功")
        check(settings.limit_var.get() == "500", "设置窗口正确回显当前上限")
        settings.destroy()
        pump(app, 0.2)

        print("\n=== 轻提示 ===")
        app.panel.toast.show("测试提示", TOAST_INFO, duration=800)
        pump(app, 0.3)
        check(app.panel.toast._frame is not None, "轻提示控件已创建")
        app.panel.toast.hide()

        print("\n=== 编辑对话框（程序化填值并保存） ===")
        from app.ui.panel import EditorDialog
        saved = {}
        editor = EditorDialog(
            app.panel.window, app.panel._tokens, "原始内容",
            on_save=lambda content: saved.update({"content": content}),
        )
        pump(app, 0.3)
        check(editor.get_content() == "原始内容", "编辑框带出了原始内容")
        editor.set_content("改写后的内容")
        editor._save()
        pump(app, 0.3)
        check(saved.get("content") == "改写后的内容", "保存回调拿到了新内容")
        result = app.kernel.update_entry_content(first_id, saved["content"])
        check(result["ok"] and result["data"]["content"] == "改写后的内容",
              "编辑结果落库成功")
        app.panel.refresh()
        pump(app)

        print("\n=== 隐藏面板 ===")
        app.panel.hide()
        pump(app, 0.3)
        check(app.panel.is_visible() is False, "面板已隐藏")
        check(app.panel.window.state() == "withdrawn", "窗口状态为 withdrawn（焦点可交还）")

        print("\n=== 事件推送驱动刷新 ===")
        app.panel.show(focus_search=False)
        pump(app, 0.4)
        before = len(app.panel._entries)
        app.kernel.simulate_capture("通过事件刷新出来的新记录", source_name="测试")
        # 手动走一次事件轮询，等价于面板可见时 after 回调的效果
        app.kernel.handle_listener_events()
        for event in app.kernel.poll_events():
            app.panel._handle_event(event)
        pump(app, 0.4)
        check(len(app.panel._entries) == before + 1,
              "clipboard-updated 事件驱动列表插入新卡片（%d -> %d）"
              % (before, len(app.panel._entries)))

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
