# -*- coding: utf-8 -*-
"""窗口行为回归（差距清单批次 11）

对应 TieZ 的窗口控制命令（``activate_window_focus`` / ``restore_last_focus``
/ ``hide_window_cmd`` / ``toggle_window_cmd`` / ``set_navigation_*``）与
贴边停靠、失焦隐藏、窗口固定、多屏重映射。

本套件盯住六个容易写错的地方：

1. **跑出屏幕必须能修回来。** 拔掉外接屏、改分辨率之后，上次记下的位置
   可能落在已经不存在的区域里。不修的话用户按快捷键"什么都没发生"，
   只会认为程序坏了。
2. **贴边停靠默认关闭，且只在用户拖动时生效。** 程序自己摆放窗口
   （跟随鼠标、位置修复）时不该顺手把它吸到边上。
3. **下边缘不吸附。** 任务栏在那里，吸下去会把面板压在任务栏底下。
4. **窗口固定 = 置顶 + 失焦不隐藏。** 它是"我要它一直在"的表达，
   不该要求用户再去改另外两个开关。
5. **面板内部控件之间移动焦点不能触发隐藏。** 点一下搜索框面板就没了，
   这是失焦隐藏最经典的翻车方式。
6. **关掉方向键要解绑，不能只"不响应"。** 留着空绑定照样吃按键。

用法：python -m tests.window_docking_check
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402

from app.app.kernel import Kernel  # noqa: E402
from app.constants import ERR_INVALID_ARG  # noqa: E402
from app.infrastructure import monitors as mo  # noqa: E402
from app.main import Application  # noqa: E402
from app.services import window_behavior as wb  # noqa: E402

PASSED = []
FAILED = []

#: 假想的双屏：左 1920×1080（主）+ 右 2560×1080
DUAL = [
    {"x": 0, "y": 0, "width": 1920, "height": 1080, "primary": True,
     "workX": 0, "workY": 0, "workWidth": 1920, "workHeight": 1040},
    {"x": 1920, "y": 0, "width": 2560, "height": 1080, "primary": False,
     "workX": 1920, "workY": 0, "workWidth": 2560, "workHeight": 1080},
]


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


def main():
    # ==================================================================
    print("=== 1) 显示器枚举 ===")
    screens = mo.list_monitors()
    check(isinstance(screens, list), "list_monitors 返回列表")
    if screens:
        first = screens[0]
        check(all(k in first for k in ("x", "y", "width", "height", "primary")),
              "每块屏都带位置尺寸与主屏标记")
        check(first["width"] > 0 and first["height"] > 0,
              "主屏尺寸合理：%d×%d" % (first["width"], first["height"]))
        check(any(m["primary"] for m in screens), "至少有一块主屏")
        virtual = mo.virtual_screen(screens)
        check(virtual["width"] >= first["width"],
              "虚拟屏幕不小于单块主屏：%s" % virtual)
    else:
        print("       （当前环境枚举不到显示器，跳过尺寸断言）")
    check(mo.virtual_screen([]) is None, "空列表时 virtual_screen 返回 None")
    check(mo.monitor_from_point(0, 0, []) is None, "空列表时不报错")
    check(mo.monitor_index_of(0, 0, []) == 0, "空列表时索引退回 0")

    # ==================================================================
    print("\n=== 2) 跑出屏幕修复 ===")
    x, y, repaired = wb.clamp_to_screens(100, 100, 400, 600, DUAL)
    check((x, y, repaired) == (100, 100, False), "正常位置原样不动")
    check(wb.clamp_to_screens(1900, 100, 400, 600, DUAL)[2] is False,
          "跨在两块屏之间也算可见，不修")
    far_x, far_y, far_fixed = wb.clamp_to_screens(9999, 9999, 400, 600, DUAL)
    check(far_fixed is True, "离谱位置被修正")
    check(far_x < 1920 + 2560 and far_y < 1080,
          "修正后落在屏幕范围内：(%d,%d)" % (far_x, far_y))
    # 拔掉副屏：原来在副屏上的位置要收回来
    lone_x, lone_y, lone_fixed = wb.clamp_to_screens(2200, 200, 400, 600, [DUAL[0]])
    check(lone_fixed is True and lone_x <= 1920,
          "**拔掉副屏后位置被收回主屏**：(%d,%d)" % (lone_x, lone_y))
    check(wb.clamp_to_screens(0, 0, 400, 600, []) == (0, 0, False),
          "没有显示器信息时不动窗口")

    print("\n=== 3) 贴边停靠 ===")
    check(wb.nearest_edge(0, 300, 400, 600, DUAL[0]) == wb.EDGE_LEFT, "贴左边缘")
    check(wb.nearest_edge(1520, 300, 400, 600, DUAL[0]) == wb.EDGE_RIGHT, "贴右边缘")
    check(wb.nearest_edge(700, 0, 400, 600, DUAL[0]) == wb.EDGE_TOP, "贴上边缘")
    check(wb.nearest_edge(700, 300, 400, 600, DUAL[0]) is None, "中间不吸附")
    check(wb.nearest_edge(700, 480, 400, 600, DUAL[0]) is None,
          "**下边缘不吸附**（任务栏在那里）")
    check(wb.nearest_edge(0, 0, 400, 600, None) is None, "没有显示器信息时不吸附")

    left = wb.docked_position(wb.EDGE_LEFT, DUAL[0], 400, 600)
    check(left == (0 - 400 + wb.DOCK_PEEK, 0),
          "停靠左侧只露出一条边：%s" % (left,))
    right = wb.docked_position(wb.EDGE_RIGHT, DUAL[0], 400, 600)
    check(right == (1920 - wb.DOCK_PEEK, 0), "停靠右侧只露出一条边：%s" % (right,))
    check(wb.docked_position("bottom", DUAL[0], 400, 600) is None,
          "不认识的边缘返回 None")

    expanded = wb.expanded_position(wb.EDGE_RIGHT, DUAL[0], 400, 600)
    check(expanded[0] + 400 == 1920, "展开后贴住右边缘：%s" % (expanded,))
    check(wb.expanded_position(wb.EDGE_TOP, DUAL[0], 400, 600)[1] == 0,
          "从上边展开后回到屏幕内")

    print("\n=== 4) 多屏比例重映射 ===")
    remapped = wb.remap_between_monitors(0, 0, 400, 600, DUAL[0], DUAL[1], (400, 600))
    check(remapped == (1920, 0), "左屏左上角映射到右屏左上角：%s" % (remapped,))
    corner = wb.remap_between_monitors(1520, 400, 400, 600, DUAL[0], DUAL[1], (400, 600))
    check(corner[0] > 3000, "靠右的位置映射到右边：%s" % (corner,))
    shrunk = wb.remap_between_monitors(4000, 800, 400, 600, DUAL[1], DUAL[0], (400, 600))
    check(shrunk[0] + 400 <= 1920 and shrunk[1] + 600 <= 1080,
          "**大屏 → 小屏时被夹回可见范围**：%s" % (shrunk,))
    check(wb.remap_between_monitors(5, 6, 400, 600, None, DUAL[0], (400, 600))
          == (5, 6), "缺少源屏信息时原样返回")

    check(wb.is_fully_visible(100, 100, 400, 600, DUAL) is True, "完整可见判定")
    check(wb.is_fully_visible(1900, 100, 400, 600, DUAL) is False,
          "跨界不算完整可见")

    # ==================================================================
    print("\n=== 5) 内核命令 ===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_wb_")
    print("       数据目录：%s" % tmp)
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    try:
        behavior = kernel.get_window_behavior()["data"]
        check(behavior["monitorCount"] >= 0, "报告显示器数量：%d"
              % behavior["monitorCount"])
        check(behavior["edgeDocking"] is False, "**贴边停靠默认关闭**")
        check(behavior["ignoreBlur"] is False, "失焦隐藏默认生效（ignoreBlur=False）")
        check(behavior["windowPinned"] is False, "窗口固定默认关闭")
        check(behavior["arrowKeySelection"] is True, "方向键选择默认开启")
        check(behavior["navigationMode"] == "list", "默认导航模式 list")

        applied = kernel.set_window_behavior(
            edgeDocking=True, ignoreBlur=True, trayVisible=False)["data"]
        check(set(applied["applied"]) == {"edgeDocking", "ignoreBlur", "trayVisible"},
              "批量设置只改传进来的项：%s" % sorted(applied["applied"]))
        after = kernel.get_window_behavior()["data"]
        check(after["edgeDocking"] is True and after["ignoreBlur"] is True,
              "批量设置已生效")
        check(after["followMouse"] is False, "**没传的项保持原值**")

        check(kernel.set_navigation_enabled(False)["data"]["enabled"] is False,
              "set_navigation_enabled 关闭方向键")
        check(kernel.get_window_behavior()["data"]["arrowKeySelection"] is False,
              "配置已同步")
        check(kernel.set_navigation_mode("grid")["data"]["mode"] == "grid",
              "set_navigation_mode 切到 grid")
        check(kernel.settings.get("app.arrow_key_selection") is True,
              "切到 grid 会顺带打开方向键")
        none_mode = kernel.set_navigation_mode("none")["data"]
        check(none_mode["mode"] == "none"
              and kernel.settings.get("app.arrow_key_selection") is False,
              "模式 none 等于关掉方向键")
        bad_mode = kernel.set_navigation_mode("乱写")
        check(not bad_mode["ok"] and bad_mode["error"]["code"] == ERR_INVALID_ARG,
              "非法模式被拒绝（%s）" % ERR_INVALID_ARG)

        # 命令式显隐：没有界面时也必须安全返回
        for name in ("hide_window_cmd", "toggle_window_cmd",
                     "activate_window_focus", "restore_last_focus"):
            result = getattr(kernel, name)()
            check(result["ok"], "%s 在无界面时也安全返回" % name)

        # ==============================================================
        print("\n=== 6) 面板上的实际行为 ===")
        app = Application(data_dir=tmp, enable_listener=False, silent_start=True)
        try:
            app.kernel.start(enable_listener=False)
            # 上一节把 ignoreBlur 打开了，这里复位——否则"失焦会隐藏"的
            # 断言会因为残留配置而假失败
            app.kernel.update_setting("app.ignore_blur", False)
            app.kernel.update_setting("app.window_pinned", False)
            app.panel.show(focus_search=False)
            pump(app, 0.8)
            panel = app.panel

            # 贴边停靠：关闭时不动
            app.kernel.update_setting("app.edge_docking", False)
            panel._maybe_dock(0, 300, 400, 600)
            pump(app, 0.3)
            check(panel._docked_edge is None, "**关闭时拖到边缘也不吸附**")

            app.kernel.update_setting("app.edge_docking", True)
            panel._maybe_dock(0, 300, 400, 600)
            pump(app, 0.4)
            check(panel._docked_edge == wb.EDGE_LEFT,
                  "开启后贴左边缘吸附：%s" % panel._docked_edge)
            check(panel.window.winfo_x() < 0,
                  "停靠后窗口大部分在屏幕外（x=%d）" % panel.window.winfo_x())
            check(panel.undock() is True, "undock 展开")
            pump(app, 0.3)
            check(panel.window.winfo_x() >= 0,
                  "展开后回到屏幕内（x=%d）" % panel.window.winfo_x())
            check(panel.undock() is False, "没有停靠时 undock 返回 False")

            # 跑出屏幕修复
            panel.window.geometry("+9000+9000")
            pump(app, 0.3)
            check(panel._repair_position() is True, "**离谱位置被修复**")
            pump(app, 0.3)
            check(panel.window.winfo_x() < 9000,
                  "修复后 x=%d" % panel.window.winfo_x())

            # 窗口固定
            check(panel.pinned() is False, "初始未固定")
            check(panel._should_stay_on_blur() is False, "未固定时失焦会隐藏")
            panel.set_pinned(True)
            pump(app, 0.3)
            check(panel.pinned() is True, "固定已生效")
            check(panel._should_stay_on_blur() is True,
                  "**固定后失焦不隐藏**（不用再去改另一个开关）")
            check(bool(panel.window.attributes("-topmost")),
                  "固定后自动置顶")
            panel.set_pinned(False)
            pump(app, 0.2)
            check(panel._should_stay_on_blur() is False, "取消固定后恢复")

            app.kernel.update_setting("app.ignore_blur", True)
            check(panel._should_stay_on_blur() is True,
                  "单独打开「忽略失焦」也生效")
            app.kernel.update_setting("app.ignore_blur", False)

            # 面板内部控件之间移动焦点不该隐藏
            panel.show(focus_search=False)
            pump(app, 0.4)
            check(hasattr(panel, "_on_focus_out"), "面板绑定了 FocusOut 处理")
            check(panel._has_child_window() is False, "没有子窗口时不误判")
            panel.open_settings()
            pump(app, 0.8)
            check(panel._has_child_window() is True,
                  "**开着设置窗口时不会因为失焦而收起面板**")
            from app.ui.settings_window import SettingsWindow
            for child in panel.window.winfo_children():
                if isinstance(child, SettingsWindow):
                    child.destroy()
            pump(app, 0.4)

            # 方向键：Tk 会把 lambda 渲染成 <lambda>，绑定的 Tcl 脚本里
            # 看不到 Python 函数名，所以用"绑定是否为空"来判断解绑与否。
            # 注意上一节把导航模式设成了 none，这里要先显式打开再比。
            panel.set_navigation_enabled(True)
            pump(app, 0.2)
            enabled_script = str(panel.window.bind("<Up>"))
            check(enabled_script != "", "打开方向键后 <Up> 有绑定")
            panel.set_navigation_enabled(False)
            pump(app, 0.2)
            disabled_script = str(panel.window.bind("<Up>"))
            check(disabled_script == "",
                  "**关掉方向键后绑定被解绑**（%d → %d 字符）"
                  % (len(enabled_script), len(disabled_script)))
            panel.set_navigation_enabled(True)
            pump(app, 0.2)
            # 不能比字符串相等：Tk 会把 lambda 的内存地址写进绑定脚本，
            # 重新绑定必然是新地址。只要"又绑上了"就够。
            check(str(panel.window.bind("<Up>")) != "",
                  "重新打开后绑定恢复（重新绑上即可）")

        finally:
            try:
                app.kernel.shutdown()
            except Exception:  # noqa: BLE001
                pass
            try:
                app.root.destroy()
            except Exception:  # noqa: BLE001
                pass
    finally:
        try:
            kernel.shutdown()
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
