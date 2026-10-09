# -*- coding: utf-8 -*-
"""图标与关联应用回归（差距清单批次 3）

覆盖三件事：

1. **可执行文件图标** —— ``ExtractIconExW`` 抠出来 → 画进 DIB → BGRA 拆成 PNG。
2. **文件类型图标** —— ``SHGetFileInfoW`` + ``SHGFI_USEFILEATTRIBUTES``：
   **文件不存在也要有图标**，因为历史记录里的文件多半早被删了。
3. **关联应用** —— 系统默认程序、打开方式列表、已安装应用扫描。

两个容易写错的地方，本套件专门盯：

* **GDI 句柄泄漏**：每次抠图标都会 CreateDC/DrawIcon/DestroyIcon，
  漏掉释放刷几十次列表就会把手柄耗光。这里靠"连续抠 60 次不崩"来兜。
* **注册表垃圾值**：实测本机 ``.txt`` 的 OpenWithList 里躺着
  ``b = "ab"`` 和 ``c = "adebc"``（MRUList 写坏留下的残留），
  天真地枚举会把它们当成程序名显示出来。

用法：
    python -m tests.icons_apps_check
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

from app.constants import ERR_NOT_FOUND  # noqa: E402
from app.domain.models import ClipboardData  # noqa: E402
from app.infrastructure import apps  # noqa: E402
from app.infrastructure import icons  # noqa: E402
from app.main import Application  # noqa: E402

PASSED = []
FAILED = []

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
NOTEPAD = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"),
                       "System32", "notepad.exe")


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def decode(data):
    """把 base64 PNG 解成 PIL 图像；不是合法 PNG 就返回 None。"""
    if not data:
        return None
    raw = base64.b64decode(data)
    if not raw.startswith(PNG_MAGIC):
        return None
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
        return image
    except Exception:  # noqa: BLE001
        return None


def is_blank(image):
    """整张图只有一种颜色 = 空白图标。"""
    if image is None:
        return True
    colors = image.convert("RGB").getcolors(maxcolors=200000)
    return not colors or len(colors) <= 1


def collect(widget):
    out = [widget]
    for child in widget.winfo_children():
        out.extend(collect(child))
    return out


def main():
    icons.clear_cache()

    # ==================================================================
    print("=== 1) 可执行文件图标 ===")
    data = icons.source_app_icon_base64(NOTEPAD, size=16)
    image = decode(data)
    check(image is not None, "notepad.exe 抠出了合法 PNG")
    check(image is not None and image.size == (16, 16),
          "图标尺寸是 16×16（实际 %s）" % ((image.size if image else "无"),))
    check(not is_blank(image), "图标不是空白方块（说明真的画上了内容）")
    check(data is not None and len(data) < 8192,
          "base64 体积很小（%d 字节），列表加载无压力" % (len(data or "")))

    large = decode(icons.source_app_icon_base64(NOTEPAD, size=32))
    check(large is not None and large.size == (32, 32),
          "32px 尺寸也能取到（实际 %s）" % ((large.size if large else "无"),))

    # ==================================================================
    print("\n=== 2) 文件类型图标：文件不存在也要有 ===")
    for name in ("报告.pdf", "data.xlsx", "photo.png", "a.zip", "clip.mp4"):
        fake = os.path.join(r"C:\definitely_not_here", name)
        icon = decode(icons.file_type_icon_base64(fake, size=16))
        check(icon is not None and not is_blank(icon),
              "不存在的 %-12s 仍取到图标" % name)

    unknown = decode(icons.file_type_icon_base64(
        r"C:\definitely_not_here\weird.zzz9", size=16))
    check(unknown is not None, "未知扩展名也能拿到系统通用图标")

    no_ext = decode(icons.file_type_icon_base64(
        r"C:\definitely_not_here\noextension", size=16))
    check(no_ext is not None, "没有扩展名也能拿到通用图标")

    # ==================================================================
    print("\n=== 3) 缓存与异常输入 ===")
    icons.clear_cache()
    icons.source_app_icon_base64(NOTEPAD, size=16)
    first = icons.cache_usage()["count"]
    icons.source_app_icon_base64(NOTEPAD.lower().replace("\\", "/").upper(), size=16)
    second = icons.cache_usage()["count"]
    check(first == 1 and second == 1,
          "同一文件的不同写法命中同一个缓存键（%d -> %d）" % (first, second))
    icons.source_app_icon_base64(NOTEPAD, size=32)
    check(icons.cache_usage()["count"] == 2, "不同尺寸算不同的缓存键")
    cleared = icons.clear_cache()
    check(cleared == 2 and icons.cache_usage()["count"] == 0,
          "清空缓存删掉 %d 条" % cleared)

    for bad in (None, "", "   ", "not a path", r"C:\no\such\file.exe"):
        try:
            icons.icon_base64(bad, 16, "exe")
            icons.icon_base64(bad, 16, "file")
            icons.source_app_icon_base64(bad, 16)
            icons.file_type_icon_base64(bad, 16)
            survived = True
        except Exception as exc:  # noqa: BLE001
            survived = False
            print("       异常输入 %r 抛了 %s" % (bad, exc))
        check(survived, "异常输入 %r 不抛异常" % (bad,))

    # GDI 句柄泄漏的粗测：连续抠 60 次不该出问题
    icons.clear_cache()
    started = time.time()
    for index in range(60):
        icons.icon_base64(NOTEPAD, 16, "exe")
        icons.icon_base64(r"C:\x\y%d.pdf" % index, 16, "file")
    check(True, "连续抠 120 次图标没有崩（耗时 %.2fs）" % (time.time() - started))

    # ==================================================================
    print("\n=== 4) 关联应用 ===")
    default = apps.system_default_app("text")
    check(isinstance(default, dict) and "friendlyName" in default,
          "系统默认程序结构完整")
    check(bool(default.get("friendlyName")),
          ".txt 的默认程序：%s" % default.get("friendlyName"))
    check(apps.system_default_app("url")["extension"] == ".html",
          "url 类型映射到 .html 关联")
    for content_type in ("image", "video", "rich_text", "code", "file"):
        item = apps.system_default_app(content_type)
        check("configured" in item, "%-10s 返回 configured 字段" % content_type)

    check(apps._looks_like_app_id("WeChat.exe") is True, "识别 xxx.exe")
    check(apps._looks_like_app_id("Microsoft.X_abc!App") is True, "识别 UWP AppID")
    check(apps._looks_like_app_id(r"C:\a\b.exe") is True, "识别完整路径")
    check(apps._looks_like_app_id("ab") is False, "过滤注册表垃圾值 ab")
    check(apps._looks_like_app_id("adebc") is False, "过滤注册表垃圾值 adebc")
    check(apps._looks_like_app_id("") is False, "空值不算程序标识")
    check(apps._friendly_name("Microsoft.WindowsNotepad_8wekyb3d8bbwe!App")
          == "WindowsNotepad", "UWP AppID 转成可读名")
    check(apps._friendly_name("WeChat.exe") == "WeChat", "exe 去掉扩展名")

    text_apps = apps.associated_apps(".txt")
    check(isinstance(text_apps, list), "打开方式列表返回列表")
    names = [item["name"] for item in text_apps]
    check("ab" not in names and "adebc" not in names,
          "列表里没有注册表垃圾值：%s" % (names or "空"))
    check(all(item["name"] for item in text_apps), "每项都有名字")

    scan = apps.scan_installed_apps()
    check(isinstance(scan, list), "已安装应用扫描返回列表（%d 个）" % len(scan))
    if scan:
        check(all(item.get("name") and item.get("executable") for item in scan),
              "扫描结果每项都有名字与路径")
        lowered = " ".join(item["name"].lower() for item in scan)
        check("uninstall" not in lowered, "过滤掉了卸载器类噪声")

    # ==================================================================
    print("\n=== 5) 内核命令 + 卡片渲染 ===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_icons_")
    app = Application(data_dir=tmp, enable_listener=False, silent_start=True)
    try:
        app.kernel.start(enable_listener=False)

        sample = os.path.join(tmp, "报告.pdf")
        with open(sample, "wb") as fh:
            fh.write(b"%PDF-1.4 fake")
        app.kernel.capture(ClipboardData("files", files=[sample]))
        app.kernel.simulate_capture("一条普通文本记录")

        rows = app.kernel.repo.get_history(limit=10)
        file_row = next((r for r in rows if r.content_type == "file"), None)
        text_row = next((r for r in rows if r.content_type == "text"), None)
        check(file_row is not None, "文件记录已入库")

        if file_row is not None:
            result = app.kernel.get_file_type_icon(file_row.id, size=16)
            check(result["ok"] and decode(result["data"]["data"]) is not None,
                  "内核返回的文件图标是合法 PNG")

            app.kernel.repo.touch_entry(file_row.id, source_app="记事本",
                                        source_app_path=NOTEPAD)
            result = app.kernel.get_source_app_icon(file_row.id, size=16)
            check(result["ok"] and decode(result["data"]["data"]) is not None,
                  "内核返回的来源应用图标是合法 PNG")

        if text_row is not None:
            bad = app.kernel.get_source_app_icon(text_row.id, size=16)
            check(not bad["ok"],
                  "没有来源路径的记录返回失败而不是抛异常")

        missing = app.kernel.get_file_type_icon(999999, size=16)
        check(not missing["ok"] and missing["error"]["code"] == ERR_NOT_FOUND,
              "不存在的记录返回 %s" % ERR_NOT_FOUND)

        app.panel.show(focus_search=False)
        deadline = time.time() + 1.2
        while time.time() < deadline:
            try:
                app.root.update()
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.02)

        check(len(app.panel._card_map) == 2, "渲染出 2 张卡片")
        check(len(app.panel._icon_refs) >= 1,
              "卡片上真的挂了图标引用（%d 个）" % len(app.panel._icon_refs))
        with_image = 0
        for card in app.panel._card_map.values():
            for widget in collect(card):
                if isinstance(widget, tk.Label):
                    try:
                        if widget.cget("image"):
                            with_image += 1
                    except tk.TclError:
                        pass
        check(with_image >= 1, "卡片树里有 %d 个带图标的 Label" % with_image)

        usage = app.kernel.get_icon_cache_usage()
        check(usage["ok"] and usage["data"]["count"] >= 1,
              "图标缓存里有 %d 条" % usage["data"]["count"])
        check(app.kernel.clear_icon_cache()["ok"], "清空图标缓存成功")
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
