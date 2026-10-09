# -*- coding: utf-8 -*-
"""表情面板回归（差距清单批次 8）

对应 TieZ 的 ``EmojiPanel.tsx`` 与 ``file_cmd.rs`` 里的收藏相关命令。

本套件盯住五个容易写错的地方：

1. **Emoji 数据文件缺失不能让面板打不开。** 读不到就退回内置精简表——
   "因为一个数据文件就整个功能不可用"是最不该犯的错。
2. **收藏按内容哈希命名。** 同一张图重复收藏不该产生第二份。
3. **删除收藏必须防目录穿越。** 这个路径来自界面，不校验的话一个构造出来
   的相对路径就能删掉磁盘上任意文件。这是本批次最要紧的一条断言。
4. **收藏网格显示缩略图而不是原图。** 收藏里可能有几 MB 的动图。
5. **切到收藏 Tab 要先重新查询。** 收藏目录可能被外部改动（用户自己往里
   放文件），只渲染内存里那份旧列表会显示不出新收藏。

⚠ 会短暂弹出窗口。用法：
    python -m tests.emoji_panel_check
"""

import base64
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402

from app.app.kernel import Kernel  # noqa: E402
from app.constants import ERR_INVALID_ARG  # noqa: E402
from app.infrastructure import emoji as em  # noqa: E402
from app.main import Application  # noqa: E402
from app.ui.emoji_panel import EmojiPanel  # noqa: E402

PASSED = []
FAILED = []

#: 1×1 的 PNG，用来当"图片表情"
TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGA"
    "hKmMIQAAAABJRU5ErkJggg==")


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


def main():
    # ==================================================================
    print("=== 1) 内置 Emoji 分组表 ===")
    groups = em.load_groups()
    check(len(groups) >= 10, "读到 %d 个分组" % len(groups))
    total = sum(len(g["emojis"]) for g in groups)
    check(total >= 300, "共 %d 个表情" % total)
    check(all(g["name"] and g["emojis"] for g in groups),
          "每个分组都有名字和内容")
    names = [g["name"] for g in groups]
    check("常用" in names, "含「常用」分组：%s" % names[:3])

    # 数据文件缺失时必须退回内置表，而不是抛异常
    real_data_file = em.DATA_FILE
    try:
        em.DATA_FILE = os.path.join(tempfile.gettempdir(), "definitely_missing.json")
        fallback = em.load_groups()
        check(len(fallback) >= 1 and all(g["emojis"] for g in fallback),
              "**数据文件缺失时退回内置表**（%d 个分组）" % len(fallback))
    finally:
        em.DATA_FILE = real_data_file
    check(em.load_groups() is not fallback, "数据文件恢复后又能读到完整表")

    # ==================================================================
    print("\n=== 2) 扩展名与 data URL ===")
    check(em.extension_for(TINY_PNG) == ".png", "按魔数认出 PNG")
    check(em.extension_for(TINY_PNG, "a.gif") == ".gif", "文件名提示优先于魔数")
    check(em.extension_for(b"????") == ".png", "认不出时默认 .png")
    check(em.extension_for(b"\xff\xd8\xff\xe0") == ".jpg", "认出 JPEG")
    check(em.extension_for(b"GIF89a....") == ".gif", "认出 GIF")

    url = "data:image/png;base64," + base64.b64encode(TINY_PNG).decode()
    data, hint = em.parse_data_url(url)
    check(data == TINY_PNG and hint == ".png", "解析 data URL 得到原始字节")
    for bad in ("", "不是 data url", "data:image/png;base64,"):
        try:
            em.parse_data_url(bad)
            check(False, "非法 data URL 应当被拒绝：%r" % bad)
        except ValueError:
            check(True, "非法 data URL 被拒绝：%r" % (bad[:20],))

    # ==================================================================
    print("\n=== 3) 收藏：按内容哈希去重 ===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_emoji_")
    first = em.save_bytes(tmp, TINY_PNG)
    second = em.save_bytes(tmp, TINY_PNG)
    check(first == second, "同一张图重复收藏复用同一个文件")
    check(os.path.isfile(first), "文件真的落盘了")
    favorites = em.list_favorites(tmp)
    check(len(favorites) == 1, "列表里只有 1 个收藏")
    check(em.favorite_usage(tmp) == (len(TINY_PNG), 1),
          "占用统计正确：%s" % (em.favorite_usage(tmp),))

    other = TINY_PNG + b"\x00"      # 内容不同 → 应当是另一个文件
    third = em.save_bytes(tmp, other)
    check(third != first, "不同内容存成不同文件")
    check(len(em.list_favorites(tmp)) == 2, "现在有 2 个收藏")

    saved_url = em.save_data_url(tmp, url)
    check(saved_url == first, "data URL 保存到与原文件相同的路径")

    try:
        em.save_bytes(tmp, b"x" * (em.MAX_FAVORITE_BYTES + 1))
        check(False, "超大文件应当被拒绝")
    except ValueError as exc:
        check(True, "超大文件被拒绝：%s" % str(exc)[:24])

    # ==================================================================
    print("\n=== 4) 目录穿越防护（本批次最要紧的一条）===")
    directory = em.favorite_dir(tmp)
    outside = os.path.join(tempfile.gettempdir(), "clipboardpro_do_not_delete.txt")
    with open(outside, "w", encoding="utf-8") as handle:
        handle.write("这个文件绝不能被删掉")

    check(em.is_inside(first, directory) is True, "收藏目录内的路径判定为 True")
    check(em.is_inside(outside, directory) is False, "目录外的路径判定为 False")
    check(em.is_inside(directory, directory) is False, "目录本身不算「在目录内」")

    check(em.remove_favorite(tmp, outside) is False,
          "**删除目录外的文件被拒绝**")
    check(os.path.isfile(outside), "那个文件确实还在")

    traversal = os.path.join(directory, "..", os.path.basename(outside))
    check(em.remove_favorite(tmp, traversal) is False,
          "用 .. 构造的相对路径同样被拒绝")
    check(os.path.isfile(outside), "构造路径也没能删掉它")

    check(em.remove_favorite(tmp, first) is True, "目录内的收藏可以正常删除")
    check(not os.path.exists(first), "确实删掉了")
    try:
        os.remove(outside)
    except OSError:
        pass

    # ==================================================================
    print("\n=== 5) 内核命令 ===")
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    try:
        payload = kernel.get_emoji_groups()["data"]
        check(payload["count"] == total, "内核返回的表情数与模块一致")

        # 上一步把收藏删了，这里重新存一份用来测命令
        source = os.path.join(tmp, "source.png")
        with open(source, "wb") as handle:
            handle.write(TINY_PNG)
        saved = kernel.save_emoji_favorite(source)
        check(saved["ok"], "save_emoji_favorite 成功")
        path = saved["data"]["path"]

        listed = kernel.list_emoji_favorites()["data"]
        check(listed["count"] >= 1, "list_emoji_favorites 返回 %d 个" % listed["count"])

        icon = kernel.get_emoji_favorite_icon(path)
        check(icon["ok"] and icon["data"]["data"], "取到收藏缩略图（base64）")
        if icon["ok"]:
            raw = base64.b64decode(icon["data"]["data"])
            check(raw.startswith(b"\x89PNG"), "缩略图是合法 PNG")
            check(len(raw) < 4096, "缩略图很小（%d 字节）" % len(raw))

        bad_icon = kernel.get_emoji_favorite_icon(r"C:\Windows\notepad.exe")
        check(not bad_icon["ok"] and bad_icon["error"]["code"] == ERR_INVALID_ARG,
              "**目录外取缩略图被拒绝**（%s）" % ERR_INVALID_ARG)
        bad_remove = kernel.remove_emoji_favorite(r"C:\Windows\notepad.exe")
        check(not bad_remove["ok"] and bad_remove["error"]["code"] == ERR_INVALID_ARG,
              "**目录外删除被拒绝**（%s）" % ERR_INVALID_ARG)
        check(os.path.exists(r"C:\Windows\notepad.exe"),
              "系统的 notepad.exe 完好无损")

        missing = kernel.save_emoji_favorite(os.path.join(tmp, "nope.png"))
        check(not missing["ok"] and missing["error"]["code"] == ERR_INVALID_ARG,
              "收藏不存在的文件被拒绝")

        url_saved = kernel.save_emoji_favorite_data_url(url)
        check(url_saved["ok"] and url_saved["data"]["path"] == path,
              "data URL 收藏复用同一文件")

        removed = kernel.remove_emoji_favorite(path)
        check(removed["ok"], "remove_emoji_favorite 成功")

        # ==============================================================
        print("\n=== 6) 表情面板窗口 ===")
        app = Application(data_dir=tmp, enable_listener=False, silent_start=True)
        try:
            app.kernel.start(enable_listener=False)
            app.panel.show(focus_search=False)
            pump(app, 0.8)

            app.panel.open_emoji_panel()
            pump(app, 1.2)
            windows = [w for w in app.panel.window.winfo_children()
                       if isinstance(w, EmojiPanel)]
            check(len(windows) == 1, "表情面板打开了")
            app.panel.open_emoji_panel()
            pump(app, 0.6)
            again = [w for w in app.panel.window.winfo_children()
                     if isinstance(w, EmojiPanel)]
            check(len(again) == 1, "**表情面板是单例**")

            panel = windows[0]
            check(len(panel._groups) == len(groups),
                  "面板拿到 %d 个分组" % len(panel._groups))
            cells = [w for w in collect(panel.body)
                     if isinstance(w, tk.Label)
                     and "Segoe UI Emoji" in str(w.cget("font"))]
            check(len(cells) == total,
                  "网格渲染出 %d 个表情格子（与数据一致）" % len(cells))

            # 收藏 Tab 必须**重新查询**，不能只渲染内存里的旧列表。
            # 期望值从前面的实际数量推，不要写死——这个临时目录里
            # 已经攒了几个收藏。
            before = len(app.kernel.list_emoji_favorites()["data"]["items"])
            app.kernel.save_emoji_favorite(source)
            panel._switch_tab("favorite")
            pump(app, 0.8)
            check(len(panel._favorites) == before + 1,
                  "**切到收藏 Tab 会重新查询**（%d -> %d）"
                  % (before, len(panel._favorites)))
            check(len(panel._icon_refs) == len(panel._favorites),
                  "每个收藏卡片都持有缩略图引用（不留引用会显示空白）：%d 个"
                  % len(panel._icon_refs))

            favorite_path = panel._favorites[0]["path"]
            panel._show_delete(favorite_path)
            pump(app, 0.3)
            check(panel._delete_target == favorite_path,
                  "右键卡片进入删除态")
            panel._delete_favorite(favorite_path)
            pump(app, 0.5)
            check(len(panel._favorites) == before,
                  "删除后少了一个（剩下 %d 个）" % len(panel._favorites))
            check(not os.path.exists(favorite_path), "文件也真的删掉了")

            panel._switch_tab("emoji")
            pump(app, 0.4)
            check(panel._tab == "emoji" and panel.count_label.cget("text").endswith("个表情"),
                  "切回 Emoji Tab：%s" % panel.count_label.cget("text"))
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
