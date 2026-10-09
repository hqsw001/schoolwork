# -*- coding: utf-8 -*-
"""紧凑预览浮窗回归（差距清单批次 14）

对应 TieZ 的 ``CompactPreviewWindow``：鼠标在卡片上停一会儿，
旁边弹一个更大的预览。

浮窗是"体验"功能，**最怕帮倒忙**，所以本套件盯住四条纪律：

1. **绝不吃焦点。** 浮窗抢走焦点的话，用户接着敲键盘就没反应了。
2. **绝不挡住主面板。** 盖住面板等于逼用户先移开鼠标才能继续操作。
3. **绝不在鼠标已经移开之后才弹出来。** 延迟弹出最经典的翻车方式。
4. **绝不能跑到屏幕外。** 看不见的浮窗会让人以为程序坏了。

另外还盯住内容侧的几个点：图片按原图比例给尺寸、长文本如实标注已截断、
文件显示大小与"已不存在"、颜色值直接给色块。

⚠ 会短暂弹出窗口。用法：
    python -m tests.preview_check
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402

from app.app.kernel import Kernel  # noqa: E402
from app.constants import (  # noqa: E402
    ERR_INVALID_ARG,
    ERR_NOT_FOUND,
    PREVIEW_MAX_CHARS,
)
from app.main import Application  # noqa: E402
from app.services import preview_layout as pl  # noqa: E402
from app.ui.preview_window import (  # noqa: E402
    PreviewPopup,
    detect_color,
    format_relative_time,
    format_size,
)

PASSED = []
FAILED = []

#: 假想的 1920×1080 屏幕
MONITOR = {"x": 0, "y": 0, "width": 1920, "height": 1080,
           "workX": 0, "workY": 0, "workWidth": 1920, "workHeight": 1040}
#: 假想的主面板：480×640，摆在屏幕上方中间
PANEL_RECT = pl.rect_from_window(720, 100, 480, 640)


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
    print("=== 1) 定位算法（纯逻辑）===")
    point = pl.pick_preview_position(1250, 300, 380, 220, MONITOR, PANEL_RECT)
    check(not pl._intersects(point, 380, 220, PANEL_RECT),
          "**右侧空白区：浮窗不压面板** %s" % (point,))
    check(pl._fits(point, 380, 220, MONITOR), "而且在屏幕内")

    corner = pl.pick_preview_position(1880, 1040, 380, 220, MONITOR, PANEL_RECT)
    check(pl._fits(corner, 380, 220, MONITOR),
          "**右下角：自动翻到能放下的位置** %s" % (corner,))

    on_panel = pl.pick_preview_position(900, 400, 380, 220, MONITOR, PANEL_RECT)
    check(not pl._intersects(on_panel, 380, 220, PANEL_RECT),
          "**锚点在面板上：挪到面板外侧** %s" % (on_panel,))

    for anchor in ((0, 0), (0, 1080), (1920, 0), (1920, 1080), (960, 540)):
        result = pl.pick_preview_position(anchor[0], anchor[1], 380, 220,
                                          MONITOR, PANEL_RECT)
        check(pl._fits(result, 380, 220, MONITOR),
              "锚点 %s 时浮窗仍在屏幕内 %s" % (anchor, result))

    huge = pl.pick_preview_position(500, 400, 3000, 2000, MONITOR, None)
    check(pl._fits(huge, 3000, 2000, MONITOR) or huge == (8, 8),
          "**浮窗比屏幕还大时夹到左上角**（至少看得见一部分）%s" % (huge,))

    no_monitor = pl.pick_preview_position(100, 100, 380, 220, None)
    check(no_monitor == (112, 112), "拿不到显示器信息时就近放置 %s" % (no_monitor,))

    print("\n=== 2) 尺寸估算 ===")
    small = pl.preview_size("text", 20)
    large = pl.preview_size("text", 900)
    check(large[1] > small[1], "**长文本给更高**（%d > %d）" % (large[1], small[1]))
    check(pl.preview_size("text", 999999)[1] <= pl.MAX_HEIGHT,
          "再长也不超过最大高度 %d" % pl.MAX_HEIGHT)
    check(pl.preview_size("text", 1)[1] >= pl.MIN_HEIGHT, "再短也不低于最小高度")
    wide = pl.preview_size("image", 0, (1200, 400))
    check(wide[0] <= pl.MAX_WIDTH,
          "**超宽图片被限制在最大宽度内**（%d）" % wide[0])
    check(pl.preview_size("image", 0, (100, 100))[0] >= pl.MIN_WIDTH,
          "小图也有最小宽度")
    check(pl.preview_size("file", 0)[0] >= pl.MIN_WIDTH, "文件类型有合理尺寸")

    # ==================================================================
    print("\n=== 3) 格式化与颜色识别 ===")
    check(format_size(512) == "512 B", "512 字节：%s" % format_size(512))
    check(format_size(2048) == "2.0 KB", "2048 字节：%s" % format_size(2048))
    check(format_size(5 * 1024 * 1024) == "5.0 MB", "5MB：%s" % format_size(5 * 1024 * 1024))
    check(format_size("乱写") == "", "非法输入返回空串")

    check(format_relative_time(int(time.time() * 1000)) == "刚刚", "刚采集显示「刚刚」")
    check("分钟" in format_relative_time(int((time.time() - 600) * 1000)),
          "10 分钟前")
    check("小时" in format_relative_time(int((time.time() - 7200) * 1000)),
          "2 小时前")
    check("天" in format_relative_time(int((time.time() - 3 * 86400) * 1000)),
          "3 天前")
    check(format_relative_time(0) == "", "时间戳 0 返回空串")
    check(format_relative_time(None) == "", "None 返回空串")

    check(detect_color("#fff") == "#ffffff", "三位十六进制补全并转小写")
    check(detect_color("#0F6CBD") == "#0f6cbd", "六位转小写")
    check(detect_color("  #ABC  ") == "#aabbcc", "**带空白也能认，且是小写**")
    check(detect_color("rgb(15, 108, 189)") == "#0f6cbd", "认 rgb()")
    check(detect_color("rgba(255,0,0,0.5)") == "#ff0000", "认 rgba()")
    check(detect_color("这不是颜色") is None, "普通文本不是颜色")
    check(detect_color("#" + "a" * 40) is None, "过长的不是颜色")
    check(detect_color("") is None, "空串不是颜色")

    # ==================================================================
    print("\n=== 4) 内核：预览内容 ===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_preview_")
    print("       数据目录：%s" % tmp)
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    try:
        settings = kernel.get_preview_settings()["data"]
        check(settings["enabled"] is True, "默认开启悬停预览")
        check(settings["delayMs"] == 350, "默认延迟 350ms：%d" % settings["delayMs"])
        check(settings["maxChars"] == PREVIEW_MAX_CHARS,
              "报告最大字符数 %d" % settings["maxChars"])

        check(kernel.set_preview_settings(delay_ms=9999)["data"]["delayMs"] == 3000,
              "**延迟上限夹到 3000ms**（不然悬停半天没反应）")
        check(kernel.set_preview_settings(delay_ms=-5)["data"]["delayMs"] == 0,
              "负数夹到 0")
        bad_delay = kernel.set_preview_settings(delay_ms="很快")
        check(not bad_delay["ok"] and bad_delay["error"]["code"] == ERR_INVALID_ARG,
              "非数字延迟被拒（%s）" % ERR_INVALID_ARG)
        check(kernel.set_preview_settings(enabled=False)["data"]["enabled"] is False,
              "能关掉")
        kernel.set_preview_settings(enabled=True, delay_ms=350)

        kernel.simulate_capture("一段用来预览的普通文本内容")
        kernel.simulate_capture("x" * (PREVIEW_MAX_CHARS + 2000))
        rows = kernel.repo.get_history(limit=10)
        normal = next(r for r in rows if "普通文本" in (r.content or ""))
        long_one = next(r for r in rows if len(r.content or "") > PREVIEW_MAX_CHARS)

        payload = kernel.get_preview_content(normal.id)["data"]
        check(payload["content"] == "一段用来预览的普通文本内容", "常规文本原样返回")
        check(payload["isTruncated"] is False, "没截断时不标截断")
        check(payload["contentType"] == "text", "带上内容类型")
        check(isinstance(payload["timestamp"], int) and payload["timestamp"] > 0,
              "带上时间戳（**原始数值**，格式化是界面的事）")
        check("sourceApp" in payload and "tags" in payload, "带上来源与标签")

        long_payload = kernel.get_preview_content(long_one.id)["data"]
        check(len(long_payload["content"]) == PREVIEW_MAX_CHARS,
              "**超长内容截断到 %d 字符**" % PREVIEW_MAX_CHARS)
        check(long_payload["isTruncated"] is True, "**并如实标注已截断**")

        missing = kernel.get_preview_content(999999)
        check(not missing["ok"] and missing["error"]["code"] == ERR_NOT_FOUND,
              "不存在的记录返回 %s" % ERR_NOT_FOUND)

        # 文件类：带大小与是否存在
        from app.domain.models import ClipboardEntry
        real = os.path.join(tmp, "real.txt")
        with open(real, "w", encoding="utf-8") as handle:
            handle.write("hello")
        missing_path = os.path.join(tmp, "missing.txt")
        entry = ClipboardEntry(
            content_type="file",
            content="\n".join([real, missing_path]),
            preview="[文件] 2 个", timestamp=1)
        entry.validate()
        saved = kernel.repo.save(entry)
        files_payload = kernel.get_preview_content(saved)["data"]
        check(len(files_payload["files"]) == 2, "文件类返回 %d 个文件" % len(files_payload["files"]))
        present = next(f for f in files_payload["files"] if f["path"] == real)
        absent = next(f for f in files_payload["files"] if f["path"] == missing_path)
        check(present["exists"] is True and present["size"] == 5,
              "**存在的文件带大小**（%d 字节）" % present["size"])
        check(absent["exists"] is False,
              "**不存在的文件标 exists=False**（界面据此提示已不存在）")
        check(present["name"] == "real.txt", "带上文件名")
        kernel.delete_entry(saved)

    finally:
        kernel.shutdown()

    # ==================================================================
    print("\n=== 5) 浮窗实际行为 ===")
    tmp2 = tempfile.mkdtemp(prefix="clipboardpro_previewui_")
    app = Application(data_dir=tmp2, enable_listener=False, silent_start=True)
    try:
        app.kernel.start(enable_listener=False)
        app.panel.show(focus_search=False)
        # 面板默认出现在"水平居中、纵向 13%"处，而鼠标**很可能正好停在那里**
        # （本机实测：鼠标停在 (1120,318)，面板 x∈[789,1259] y∈[149,789]）。
        # 窗口在鼠标底下映射出来的那一刻就会触发卡片 hover，延迟到点后
        # 预览浮窗自己弹出来，下面"初始不可见"就成了一条**看鼠标脸色的假失败**。
        # 所以先把面板挪到"鼠标所在位置的对面"，让这条断言只反映代码行为。
        # （已验证：同样代码只把面板挪开，本套件 77/78 → 78/78。）
        pointer_x = app.panel.window.winfo_pointerx()
        pointer_y = app.panel.window.winfo_pointery()
        screen_w = app.panel.window.winfo_screenwidth()
        screen_h = app.panel.window.winfo_screenheight()
        panel_w, panel_h = 470, 640
        margin = 60          # 留出边距，避免贴边停靠逻辑再把窗口挪回来
        away_x = margin if pointer_x > screen_w // 2 else screen_w - panel_w - margin
        away_y = margin if pointer_y > screen_h // 2 else screen_h - panel_h - margin
        app.panel.window.geometry("%dx%d+%d+%d"
                                  % (panel_w, panel_h, max(0, away_x), max(0, away_y)))
        pump(app, 0.8)
        panel_left = app.panel.window.winfo_rootx()
        panel_top = app.panel.window.winfo_rooty()
        check(not (panel_left <= pointer_x <= panel_left + panel_w
                   and panel_top <= pointer_y <= panel_top + panel_h),
              "（前置条件）面板已挪开、鼠标不在面板上，下面不受悬停干扰")
        app.kernel.simulate_capture("浮窗预览测试内容")
        app.panel.refresh()
        pump(app, 0.5)

        popup = app.panel._preview()
        check(isinstance(popup, PreviewPopup), "面板创建了预览浮窗")
        check(app.panel._preview() is popup,
              "**浮窗是单例**（反复建窗口会闪）")
        check(popup.visible is False, "初始不可见")

        entry_id = app.panel._entries[0]["id"]
        popup.show(entry_id)
        pump(app, 0.6)
        check(popup.visible is True, "show 之后可见")
        check(popup.current_entry() == entry_id, "记住当前预览的是哪条")

        window = popup._window
        check(bool(window.overrideredirect()),
              "**无边框（overrideredirect）**，不会变成一个有标题栏的窗口")
        check(bool(window.attributes("-topmost")), "置顶显示")
        check(window.winfo_width() >= pl.MIN_WIDTH,
              "宽度合理：%d" % window.winfo_width())
        check(len(window.winfo_children()) >= 1, "渲染出了内容")

        # 不抢焦点：直接查源码，确认整个模块里没有**调用** focus_force /
        # grab_set。写成 check(True, ...) 那种"永远通过"的断言等于没测；
        # 但也不能只搜字符串——文档字符串里正好写着"不调用 focus_force"。
        import re as _re
        import app.ui.preview_window as preview_mod
        source = open(preview_mod.__file__, encoding="utf-8").read()
        for name in ("focus_force", "focus_set", "grab_set"):
            check(not _re.search(r"\.%s\s*\(" % name, source),
                  "**浮窗代码里没有调用 .%s(**（绝不抢焦点）" % name)
        check("overrideredirect(True)" in source, "确实用了无边框窗口")

        popup.hide()
        pump(app, 0.3)
        check(popup.visible is False, "hide 之后不可见")
        check(popup.current_entry() is None, "隐藏后清空当前记录")

        # 请求号：过期请求必须作废
        popup.schedule(entry_id)
        first_request = popup._request_id
        popup.schedule(entry_id)
        second_request = popup._request_id
        check(second_request > first_request,
              "**每次 schedule 都递增请求号**（%d → %d）"
              % (first_request, second_request))

        # 过期请求不该弹出
        popup.cancel()
        popup._show_if_current(first_request, entry_id)
        check(popup.visible is False,
              "**过期请求不弹出**（鼠标早移开了就不该冒出来）")

        # 鼠标不在面板上时也不弹
        popup._request_id += 1
        current = popup._request_id
        saved_pointer = popup._pointer_still_inside
        popup._pointer_still_inside = lambda: False
        popup._show_if_current(current, entry_id)
        check(popup.visible is False, "鼠标不在面板上时不弹")
        popup._pointer_still_inside = saved_pointer

        # 各种操作都要收起浮窗
        popup.show(entry_id)
        pump(app, 0.4)
        check(popup.visible is True, "重新显示")
        app.panel.hide_preview()
        check(popup.visible is False, "hide_preview() 能收起")
        popup.show(entry_id)
        pump(app, 0.4)
        app.panel._on_mousewheel(type("E", (), {"delta": -120})())
        check(popup.visible is False, "**滚动时自动收起**（滚动后位置就对不上了）")
        popup.show(entry_id)
        pump(app, 0.4)
        app.panel.hide()
        pump(app, 0.4)
        check(popup.visible is False, "收起面板时浮窗也收起")

        # 开关生效
        app.kernel.set_preview_settings(enabled=False)
        app.panel.show(focus_search=False)
        pump(app, 0.5)
        before = popup._request_id
        popup.schedule(entry_id)
        check(popup._request_id == before,
              "**关掉开关后 schedule 不做任何事**")

        popup.destroy()
        check(popup.visible is False, "destroy 之后不可见")

    finally:
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
