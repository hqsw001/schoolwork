# -*- coding: utf-8 -*-
"""紧凑预览浮窗（对应 TieZ 的 ``CompactPreviewWindow``）

鼠标在卡片上停一会儿，就在旁边弹一个更大的预览：完整文本、原图、
文件信息。参考实现是开一个独立的 Tauri 窗口，这里用无边框 Toplevel。

**这是一块"体验"功能，最怕的是帮倒忙**，所以四条纪律：

1. **绝不吃焦点。** 用 ``overrideredirect`` 去掉边框并**不调用 focus_force**；
   浮窗抢走焦点的话用户接着敲键盘就没反应了。
2. **绝不挡住主面板。** 定位算法会优先挑面板外侧的位置——盖住面板等于
   逼用户先移开鼠标才能继续操作。
3. **绝不在鼠标已经移开之后才弹出来。** 悬停计时器带**请求号**，
   移开就作废。不然会出现"鼠标早走了，浮窗才冒出来"的诡异体验。
4. **内容只读。** 浮窗里不做任何修改操作，点到就关掉。
"""

import os
import re
import time
import tkinter as tk

from app.constants import TYPE_CODE, TYPE_IMAGE, TYPE_URL
from app.infrastructure import monitors as monitors_mod
from app.services import preview_layout

#: 图片预览的最大边长
IMAGE_MAX_EDGE = 320

#: 浮窗消失后多久内不允许再弹（防止鼠标划过列表时连续闪）
REOPEN_COOLDOWN_MS = 120


def format_size(size):
    """字节数 → 人话。"""
    try:
        value = float(size)
    except (TypeError, ValueError):
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return "%d B" % int(value)
            return "%.1f %s" % (value, unit)
        value /= 1024.0
    return ""


def format_relative_time(timestamp_ms):
    """毫秒时间戳 → "刚刚 / 3 分钟前 / 昨天 14:30 / 2026-09-19"。"""
    try:
        stamp = int(timestamp_ms)
    except (TypeError, ValueError):
        return ""
    if stamp <= 0:
        return ""
    seconds = max(0, int(time.time() - stamp / 1000.0))
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return "%d 分钟前" % (seconds // 60)
    if seconds < 86400:
        return "%d 小时前" % (seconds // 3600)
    local = time.localtime(stamp / 1000.0)
    if seconds < 172800:
        return "昨天 %02d:%02d" % (local.tm_hour, local.tm_min)
    if seconds < 604800:
        return "%d 天前" % (seconds // 86400)
    return time.strftime("%Y-%m-%d %H:%M", local)


#: 十六进制颜色（#rgb / #rrggbb）
_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")
_RGB_RE = re.compile(r"^rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})")


def detect_color(text):
    """内容本身是不是一个颜色值；是就返回 ``#rrggbb``。"""
    raw = (text or "").strip()
    if len(raw) > 32:
        return None
    if _COLOR_RE.match(raw):
        if len(raw) == 4:
            # #abc → #aabbcc。**要一并转小写**：接口返回的颜色值大小写
            # 混用会让"看起来一样的两个色块"在比较时判不相等
            return "#" + "".join(ch * 2 for ch in raw[1:]).lower()
        return raw.lower()
    match = _RGB_RE.match(raw)
    if match:
        try:
            parts = [max(0, min(255, int(g))) for g in match.groups()]
        except ValueError:
            return None
        return "#%02x%02x%02x" % tuple(parts)
    return None


class PreviewPopup(object):
    """被主面板复用的**单例**浮窗（不是每次都新建窗口）。

    复用而不是每次 destroy/create：悬停会频繁触发，反复建窗口在
    Windows 上会有肉眼可见的闪烁。
    """

    def __init__(self, panel):
        self.panel = panel
        self.kernel = panel.kernel
        self.tokens = panel._tokens
        self._window = None
        self._visible = False
        self._request_id = 0
        self._timer = None
        self._entry_id = None
        self._last_hidden_at = 0.0
        self._image_ref = None

    # ------------------------------------------------------------------
    def _font(self, size=9, bold=False, mono=False):
        if mono:
            return ("Consolas", size)
        family = self.tokens["font_family"]
        return (family, size, "bold") if bold else (family, size)

    def _ensure_window(self):
        if self._window is not None:
            try:
                if self._window.winfo_exists():
                    return self._window
            except tk.TclError:
                pass
        window = tk.Toplevel(self.panel.window)
        # 去掉标题栏：浮窗不该有边框
        window.overrideredirect(True)
        window.withdraw()
        window.configure(bg=self.tokens["border"])
        # 置顶但不抢焦点
        window.attributes("-topmost", True)
        try:
            window.attributes("-toolwindow", True)
        except tk.TclError:
            pass
        self._window = window
        return window

    # ------------------------------------------------------------------
    def schedule(self, entry_id, event=None):
        """鼠标停到卡片上：延迟这么久再弹。

        用**请求号**而不是简单的 after_cancel：鼠标快速划过很多卡片时，
        取消/重建定时器容易漏掉，请求号能让过期的那次彻底作废。
        """
        settings = self.kernel.get_preview_settings()["data"]
        if not settings["enabled"]:
            return
        if time.time() - self._last_hidden_at < REOPEN_COOLDOWN_MS / 1000.0:
            return
        self._request_id += 1
        request = self._request_id
        self.cancel()
        self._request_id = request
        delay = max(0, int(settings["delayMs"]))
        try:
            self._timer = self.panel.window.after(
                delay, lambda: self._show_if_current(request, entry_id))
        except tk.TclError:
            self._timer = None

    def cancel(self):
        """鼠标移开 / 滚动 / 按键：立刻取消并隐藏。"""
        self._request_id += 1
        if self._timer is not None:
            try:
                self.panel.window.after_cancel(self._timer)
            except (tk.TclError, ValueError):
                pass
            self._timer = None
        self.hide()

    def _show_if_current(self, request, entry_id):
        """延迟到了：确认这次请求还是最新的，而且鼠标仍在卡片上。"""
        self._timer = None
        if request != self._request_id:
            return                      # 已经过期，鼠标早移开了
        if not self._pointer_still_inside():
            return
        self.show(entry_id)

    def _pointer_still_inside(self):
        """鼠标是不是还在主面板里。

        没有这一步就会出现"鼠标早走了，浮窗才冒出来"——这是延迟弹出
        最经典的翻车方式。
        """
        try:
            x = self.panel.window.winfo_pointerx()
            y = self.panel.window.winfo_pointery()
            left = self.panel.window.winfo_rootx()
            top = self.panel.window.winfo_rooty()
            right = left + self.panel.window.winfo_width()
            bottom = top + self.panel.window.winfo_height()
        except tk.TclError:
            return False
        return left <= x < right and top <= y < bottom

    # ------------------------------------------------------------------
    def show(self, entry_id):
        result = self.kernel.get_preview_content(entry_id)
        if not result["ok"]:
            return
        payload = result["data"]
        window = self._ensure_window()
        self._entry_id = entry_id
        self._clear(window)
        self._render(window, payload)
        self._position(window, payload)
        try:
            window.deiconify()
            window.lift()
        except tk.TclError:
            return
        self._visible = True

    def hide(self):
        if not self._visible:
            return
        self._visible = False
        self._entry_id = None
        self._last_hidden_at = time.time()
        if self._window is not None:
            try:
                self._window.withdraw()
            except tk.TclError:
                pass

    def destroy(self):
        self.cancel()
        if self._window is not None:
            try:
                self._window.destroy()
            except tk.TclError:
                pass
            self._window = None

    @property
    def visible(self):
        return self._visible

    def current_entry(self):
        return self._entry_id

    # ------------------------------------------------------------------
    def _clear(self, window):
        for child in window.winfo_children():
            child.destroy()
        self._image_ref = None

    def _position(self, window, payload):
        """按定位算法摆放，并把尺寸定下来。"""
        width, height = self._measure(window, payload)
        window.geometry("%dx%d" % (width, height))

        try:
            pointer_x = window.winfo_pointerx()
            pointer_y = window.winfo_pointery()
            panel_x = self.panel.window.winfo_rootx()
            panel_y = self.panel.window.winfo_rooty()
            panel_w = self.panel.window.winfo_width()
            panel_h = self.panel.window.winfo_height()
        except tk.TclError:
            return
        monitor = monitors_mod.monitor_from_point(pointer_x, pointer_y,
                                                  self.panel._monitors())
        avoid = preview_layout.rect_from_window(panel_x, panel_y, panel_w, panel_h)
        x, y = preview_layout.pick_preview_position(
            pointer_x, pointer_y, width, height, monitor, avoid)
        window.geometry("+%d+%d" % (int(x), int(y)))

    def _measure(self, window, payload):
        """按内容类型估算尺寸（图片按原图比例）。"""
        content_type = payload["contentType"]
        image_size = None
        if content_type == TYPE_IMAGE and payload.get("imagePath"):
            image_size = self._image_pixel_size(payload["imagePath"])
        text_length = len(payload.get("content") or payload.get("preview") or "")
        if payload.get("files"):
            text_length = max(text_length, len(payload["files"]) * 60)
        base_w, base_h = 400, 240
        if content_type == TYPE_CODE:
            base_w = 460
        window.update_idletasks()
        return preview_layout.preview_size(content_type, text_length, image_size,
                                           base_w, base_h)

    @staticmethod
    def _image_pixel_size(path):
        try:
            from PIL import Image
            with Image.open(path) as image:
                return image.size
        except Exception:  # noqa: BLE001
            return None

    # ------------------------------------------------------------------
    def _render(self, window, payload):
        tokens = self.tokens
        window.configure(bg=tokens["border"])
        body = tk.Frame(window, bg=tokens["bg"])
        body.pack(fill="both", expand=True, padx=1, pady=1)

        self._render_header(body, payload)
        content_type = payload["contentType"]
        if content_type == TYPE_IMAGE:
            self._render_image(body, payload)
        elif payload.get("files"):
            self._render_files(body, payload)
        else:
            self._render_text(body, payload)

        self._render_footer(body, payload)

    def _render_header(self, parent, payload):
        tokens = self.tokens
        header = tk.Frame(parent, bg=tokens["surface"])
        header.pack(fill="x")

        from app.constants import TYPE_LABELS
        label = TYPE_LABELS.get(payload["contentType"], payload["contentType"])
        tk.Label(header, text=label, bg=tokens["surface"], fg=tokens["accent"],
                 font=self._font(9, True)).pack(side="left", padx=(8, 6), pady=4)
        source = payload.get("sourceApp") or "未知来源"
        tk.Label(header, text=source, bg=tokens["surface"], fg=tokens["text_muted"],
                 font=self._font(8)).pack(side="left")
        if payload.get("isPinned"):
            tk.Label(header, text="📌", bg=tokens["surface"],
                     fg=tokens["pin"], font=self._font(8)).pack(side="left", padx=4)
        if payload.get("isSensitive"):
            tk.Label(header, text="敏感", bg=tokens["surface"], fg=tokens["pin"],
                     font=self._font(8)).pack(side="left", padx=4)
        tk.Label(header, text=format_relative_time(payload.get("timestamp")),
                 bg=tokens["surface"], fg=tokens["text_faint"],
                 font=self._font(8)).pack(side="right", padx=8)

    def _render_text(self, parent, payload):
        tokens = self.tokens
        text = payload.get("content") or payload.get("plainFromHtml") \
            or payload.get("preview") or "（无内容）"
        color = detect_color(text)
        mono = payload["contentType"] in (TYPE_CODE, TYPE_URL)

        wrap = tk.Frame(parent, bg=tokens["bg"])
        wrap.pack(fill="both", expand=True, padx=8, pady=(6, 0))

        if color:
            # 内容是一个颜色值：直接给色块，比给一串十六进制直观得多
            swatch = tk.Frame(wrap, bg=color, height=52,
                              highlightthickness=1,
                              highlightbackground=tokens["border"])
            swatch.pack(fill="x", pady=(0, 4))
            swatch.pack_propagate(False)

        if payload.get("isTruncated"):
            tk.Label(wrap, text="内容较长，以下为前 %d 个字符"
                     % len(text), bg=tokens["bg"], fg=tokens["pin"],
                     font=self._font(8)).pack(anchor="w")

        box = tk.Text(wrap, wrap="word" if not mono else "none",
                      font=self._font(10, mono=mono), bg=tokens["bg"],
                      fg=tokens["text"], relief="flat", bd=0,
                      highlightthickness=0, height=6)
        box.pack(fill="both", expand=True)
        box.insert("1.0", text)
        # 只读：浮窗里不做任何修改操作
        box.configure(state="disabled")
        box.bind("<MouseWheel>",
                 lambda e: box.yview_scroll(int(-1 * (e.delta / 120)), "units"))

    def _render_image(self, parent, payload):
        tokens = self.tokens
        path = payload.get("imagePath")
        wrap = tk.Frame(parent, bg=tokens["bg"])
        wrap.pack(fill="both", expand=True, padx=8, pady=6)
        if not path or not os.path.isfile(path):
            tk.Label(wrap, text="图片文件已不存在", bg=tokens["bg"],
                     fg=tokens["pin"], font=self._font(10)).pack(expand=True)
            return
        try:
            from PIL import Image, ImageTk
            with Image.open(path) as image:
                image = image.convert("RGB")
                image.thumbnail((IMAGE_MAX_EDGE, IMAGE_MAX_EDGE))
                photo = ImageTk.PhotoImage(image)
                size = image.size
        except Exception as exc:  # noqa: BLE001
            tk.Label(wrap, text="无法显示图片：%s" % exc, bg=tokens["bg"],
                     fg=tokens["pin"], font=self._font(9), wraplength=340,
                     justify="left").pack(expand=True)
            return
        # **必须留引用**：不留的话 PhotoImage 被回收，图片变空白
        self._image_ref = photo
        tk.Label(wrap, image=photo, bg=tokens["bg"], bd=0).pack(expand=True)
        try:
            bytesize = os.path.getsize(path)
            tk.Label(wrap, text="%d × %d · %s" % (size[0], size[1],
                                                  format_size(bytesize)),
                     bg=tokens["bg"], fg=tokens["text_faint"],
                     font=self._font(8)).pack(pady=(4, 0))
        except OSError:
            pass

    def _render_files(self, parent, payload):
        tokens = self.tokens
        wrap = tk.Frame(parent, bg=tokens["bg"])
        wrap.pack(fill="both", expand=True, padx=8, pady=6)
        for item in payload["files"][:12]:
            row = tk.Frame(wrap, bg=tokens["bg"])
            row.pack(fill="x", pady=1)
            icon = "📁" if item.get("isDir") else "📄"
            tk.Label(row, text=icon, bg=tokens["bg"],
                     font=self._font(10)).pack(side="left", padx=(0, 4))
            info = tk.Frame(row, bg=tokens["bg"])
            info.pack(side="left", fill="x", expand=True)
            tk.Label(info, text=item["name"], bg=tokens["bg"], fg=tokens["text"],
                     font=self._font(9), anchor="w").pack(anchor="w")
            detail = item["path"]
            if not item.get("exists"):
                detail += "（文件已不存在）"
            elif item.get("size"):
                detail += " · " + format_size(item["size"])
            tk.Label(info, text=self._ellipsize(detail, 62), bg=tokens["bg"],
                     fg=tokens["text_faint"], font=self._font(8),
                     anchor="w").pack(anchor="w")
        if len(payload["files"]) > 12:
            tk.Label(wrap, text="…还有 %d 个" % (len(payload["files"]) - 12),
                     bg=tokens["bg"], fg=tokens["text_faint"],
                     font=self._font(8)).pack(anchor="w", pady=(4, 0))

    def _render_footer(self, parent, payload):
        tokens = self.tokens
        footer = tk.Frame(parent, bg=tokens["bg"])
        footer.pack(fill="x", padx=8, pady=(0, 6))
        tags = payload.get("tags") or []
        if tags:
            tk.Label(footer, text="、".join("#" + t for t in tags[:5]),
                     bg=tokens["bg"], fg=tokens["accent"],
                     font=self._font(8)).pack(side="left")
        tk.Label(footer, text="双击卡片粘贴 · 移开鼠标关闭", bg=tokens["bg"],
                 fg=tokens["text_faint"], font=self._font(8)).pack(side="right")

    @staticmethod
    def _ellipsize(text, limit):
        text = str(text or "")
        return text if len(text) <= limit else text[:limit - 1] + "…"
