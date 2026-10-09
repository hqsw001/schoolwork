# -*- coding: utf-8 -*-
"""卡片能力回归：缩略图、颜色色块、逐条操作按钮

对应 TieZ 卡片（`ClipboardItem.tsx`）里最直观的三样东西。补这批的直接
动机是：现有卡片只有"类型徽标 + 一段文本"，图片记录也只显示 `[图片] W × H`，
而缩略图函数（`image_to_thumbnail_png`）其实**早就写好了、从来没人调用**。

本套件会短暂弹出一个真实窗口。

用法：
    python -m tests.card_ui_check
"""

import base64
import io
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402
from PIL import Image  # noqa: E402

from app.domain.models import ClipboardData  # noqa: E402
from app.main import Application  # noqa: E402
from app.ui.panel import _normalize_color  # noqa: E402

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


def png_of(size, color):
    image = Image.new("RGB", size, color)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def main():
    tmp = tempfile.mkdtemp(prefix="clipboardpro_card_")
    print("数据目录：%s\n" % tmp)
    app = Application(data_dir=tmp, enable_listener=False, silent_start=True)
    try:
        app.kernel.start(enable_listener=False)

        # ==============================================================
        print("=== 1) 缩略图：采集时就生成，且有磁盘缓存 ===")
        app.kernel.capture(ClipboardData("image", image_bytes=png_of((900, 500), (40, 120, 200))))
        image_row = app.kernel.repo.get_history(limit=1)[0]
        check(image_row.content_type == "image", "图片已入库")

        thumb_dir = app.kernel.db.thumb_dir
        thumbs = os.listdir(thumb_dir) if os.path.isdir(thumb_dir) else []
        check(len(thumbs) == 1,
              "采集时顺手生成了缩略图：%s" % (thumbs or "无"))

        result = app.kernel.get_thumbnail(image_row.id)
        check(result["ok"], "get_thumbnail 返回成功")
        if result["ok"]:
            raw = base64.b64decode(result["data"]["data"])
            decoded = Image.open(io.BytesIO(raw))
            check(max(decoded.size) <= 160,
                  "缩略图长边不超过 160：%s" % (decoded.size,))
            check(decoded.size == (160, 89),
                  "缩略图按原图比例缩放（900×500 → 160×89），实际 %s" % (decoded.size,))
            check(len(raw) < 50 * 1024,
                  "缩略图体积很小（%d 字节），列表加载不会卡" % len(raw))

        # 缓存复用：再取一次不应重写文件（mtime 不变 = 没有重新解码）
        thumb_path = os.path.join(thumb_dir, thumbs[0])
        before = os.path.getmtime(thumb_path)
        app.kernel.get_thumbnail(image_row.id)
        check(os.path.getmtime(thumb_path) == before,
              "第二次取缩略图直接命中缓存，没有重新生成")

        # ==============================================================
        print("\n=== 2) 缩略图的错误分支 ===")
        text_row = None
        app.kernel.simulate_capture("一段普通文本，不该有缩略图")
        for row in app.kernel.repo.get_history(limit=5):
            if row.content_type != "image":
                text_row = row
                break
        check(text_row is not None, "已写入一条文本记录")
        if text_row is not None:
            bad = app.kernel.get_thumbnail(text_row.id)
            check(not bad["ok"] and bad["error"]["code"] == 4001,
                  "非图片记录被拒绝（4001）")
        missing = app.kernel.get_thumbnail(999999)
        check(not missing["ok"] and missing["error"]["code"] == 4004,
              "不存在的记录被拒绝（4004）")

        # 附件被删掉之后，缩略图应当失效而不是抛异常
        os.remove(image_row.content)
        gone = app.kernel.get_thumbnail(image_row.id)
        check(not gone["ok"], "附件被删除后取缩略图返回失败而不是抛异常")

        # ==============================================================
        print("\n=== 3) 颜色字面量归一（tk 只认 #rrggbb）===")
        cases = [
            ("#1E90FF", "#1e90ff"),
            ("#fff", "#ffffff"),
            ("#11223344", "#112233"),
            ("rgb(255, 0, 0)", "#ff0000"),
            ("rgba(255, 0, 0, 0.5)", "#ff0000"),
            ("rgb(100%, 0%, 0%)", "#ff0000"),
            ("hsl(0, 100%, 50%)", "#ff0000"),
            ("hsl(210, 100%, 50%)", "#007fff"),
        ]
        for raw, expected in cases:
            got = _normalize_color(raw)
            check(got == expected, "%-22s -> %s" % (raw, got))
        for raw in ["#这是一段中文", "not a color", "#12345", "", "#gggggg"]:
            check(_normalize_color(raw) is None,
                  "%-22s 不被当成颜色" % (repr(raw),))

        # ==============================================================
        print("\n=== 4) 卡片真的渲染出了缩略图 / 色块 / 操作按钮 ===")
        # 复用同一个 Application：中途 shutdown + destroy 再重建，会让
        # 已经排队的 after 回调打到销毁后的控件上，刷一屏无意义的 Tcl 报错。
        app.kernel.clear_history()
        app.kernel.capture(ClipboardData("image", image_bytes=png_of((640, 360), (200, 80, 40))))
        for text in ["#1E90FF", "rgba(255, 0, 0, 0.5)",
                     "这是一段用来对照的普通文本内容",
                     "#这是一段带井号的中文，不该被当成颜色"]:
            app.kernel.simulate_capture(text)
        app.panel.show(focus_search=False)
        pump(app, 1.2)

        cards = list(app.panel._card_map.values())
        check(len(cards) == 5, "渲染出 5 张卡片，实际 %d" % len(cards))
        check(len(app.panel._thumb_refs) == 1,
              "有 1 张图片卡片持有缩略图引用（引用不丢才不会显示空白）")

        buttons = 0
        swatches = 0
        for card in cards:
            for widget in collect(card):
                if isinstance(widget, tk.Button):
                    buttons += 1
                elif isinstance(widget, tk.Label):
                    try:
                        if widget.cget("relief") == "solid":
                            swatches += 1
                    except tk.TclError:
                        pass
        check(buttons == len(cards) * 6,
              "每张卡片 6 个操作按钮，共 %d 个" % buttons)
        check(swatches == 2,
              "两条颜色记录渲染成色块，共 %d 个（中文井号那条不算）" % swatches)

        labels = []
        for card in cards:
            for widget in collect(card):
                if isinstance(widget, tk.Button):
                    try:
                        labels.append(widget.cget("text"))
                    except tk.TclError:
                        pass
        for expected in ("粘贴", "复制", "置顶", "标签", "删除", "打开"):
            check(expected in labels, "卡片上有「%s」按钮" % expected)

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
