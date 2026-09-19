# -*- coding: utf-8 -*-
"""主面板（F4 分类浏览 / F5 检索 / F7 记录组织 / UC-07 / UC-08 / UC-09 / UC-10）

界面约定（用户已确认本周只做"复制 → 入库 → 列表 → 双击粘贴"这条主链路并做深做稳）：

    做通：类型筛选、关键字检索与命中高亮、置顶、编辑、删除、清空、
          右键菜单、双击粘贴、键盘操作、来源显示、密度与主题令牌
    占位：来源筛选下拉、标签管理面板、批量勾选（界面入口已留，逻辑第三周补）

键盘操作（对照 UC-19 主事件流第 7 步：按 Esc 隐藏面板）：
    ↑ / ↓        移动选中项
    Enter        粘贴选中项（UC-09 主事件流第 1 步的"选中后按回车"）
    双击         粘贴选中项
    Ctrl+数字    粘贴当前筛选结果中的第 N 条
    Delete       删除选中项（带二次确认）
    Ctrl+P       置顶 / 取消置顶
    Ctrl+E       编辑选中项
    Ctrl+F       聚焦搜索框
    Esc          隐藏面板
    F5           刷新列表
"""

import re
import time
import tkinter as tk
from tkinter import messagebox, simpledialog

from app import APP_NAME, VERSION
from app.constants import (
    ALL_TYPES,
    EVT_CLIPBOARD_REMOVED,
    EVT_CLIPBOARD_UPDATED,
    EVT_LISTEN_STATUS_CHANGED,
    EVT_THEME_CHANGED,
    EVT_TOAST,
    K_ALWAYS_ON_TOP,
    K_FOLLOW_MOUSE,
    K_LIST_DENSITY,
    K_MAIN_HOTKEY,
    K_THEME,
    K_THEME_MODE,
    K_WINDOW_GEOMETRY,
    PAGE_SIZE,
    TOAST_ERROR,
    TOAST_INFO,
    TOAST_SUCCESS,
    TOAST_WARNING,
    TYPE_IMAGE,
    TYPE_LABELS,
    is_text_type,
)
from app.errors import now_ms
from app.ui import theme as theme_mod
from app.ui.toast import Toast

#: 一次渲染的最大条数（F4 虚拟滚动的简化实现：分批加载，不一次性建上千个控件）
RENDER_BATCH = 60

#: 检索防抖时长（UC-08 主事件流第 3 步：约 200 毫秒）
SEARCH_DEBOUNCE_MS = 200

#: 事件轮询间隔
POLL_INTERVAL_MS = 120

#: "刚隐藏"的抑制窗口：这段时间内到达的唤出事件会被忽略，
#: 避免收起面板的瞬间又被上一轮积压的唤出事件弹回来
SHOW_SUPPRESS_MS = 350


class MainPanel(object):
    """主面板窗口。"""

    def __init__(self, kernel):
        self.kernel = kernel
        self.logger = kernel.logger
        self.root = kernel.ui_root
        self._tokens = theme_mod.get_tokens(
            kernel.settings.get(K_THEME), kernel.settings.get(K_THEME_MODE)
        )
        self._density = kernel.settings.get(K_LIST_DENSITY)
        self._entries = []
        self._rendered = 0
        self._selected_id = None
        self._card_map = {}
        self._counts = {}
        self._truncated = False
        self._render_batch_id = None
        self._search_after_id = None
        self._search_trace = None
        self._poll_after_id = None
        self._last_query = ""
        self._last_tag_only = False
        self._tag_only = False
        self._counts = {}
        self._truncated = False
        self._placeholder_active = False
        self._visible = False
        self._hidden_at = 0.0
        self._menu = None

        self._build_window()
        self._build_widgets()
        self._bind_keys()
        self.refresh(reset_scroll=True)
        # 事件轮询必须在构造时就启动并且永不停止：
        # 面板隐藏时也要能收到"按了快捷键要唤出面板"这类事件
        self._start_polling()

    # ==================================================================
    # 窗口
    # ==================================================================
    def _build_window(self):
        self.window = tk.Toplevel(self.root)
        self.window.withdraw()
        # 【窗口形态说明】
        # 这里刻意**不使用** overrideredirect(True)。无边框窗口虽然更像"呼出式面板"，
        # 但它没有标题栏 —— 用户既没法拖动它，也没法最小化或关闭它。
        # 所以采用系统原生窗口：标题栏自带拖动 / 最小化 / 最大化 / 关闭，
        # 同时额外在面板内提供"收起"按钮，让关闭按钮对应的语义是"隐藏面板"，
        # 而不是退出程序（隐藏之后托盘与全局快捷键仍然可用）。
        self.window.overrideredirect(False)
        self.window.resizable(True, True)
        self.window.minsize(360, 320)
        self.window.title("%s %s" % (APP_NAME, VERSION))
        self.window.protocol("WM_DELETE_WINDOW", self.hide)
        self.window.configure(bg=self._tokens["bg"])
        self.window.attributes("-topmost", bool(self.kernel.settings.get(K_ALWAYS_ON_TOP)))

        width, height = self._restore_geometry()
        self._panel_size = (width, height)
        screen_w = self.window.winfo_screenwidth()
        screen_h = self.window.winfo_screenheight()
        self._default_pos = (
            max(0, (screen_w - width) // 2),
            max(0, int(screen_h * 0.13)),
        )
        self.window.geometry("%dx%d+%d+%d" % (width, height, *self._default_pos))
        # 拖动或缩放结束后记下新位置（<Configure> 会在每次移动时触发，
        # 所以这里只做轻量的变量记录，真正写库在 hide() 里做一次）
        self.window.bind("<Configure>", self._on_window_configure)

    def _restore_geometry(self):
        """恢复上次的窗口大小（UC-16 窗口行为持久化）。

        只记大小不记位置：面板是"停靠式"工具，固定出现在屏幕中上部更好预期；
        用户如果把窗口拖到别处并关了，下次出现的位置飘忽反而更难用。
        """
        raw = self.kernel.settings.get(K_WINDOW_GEOMETRY) or ""
        match = re.match(r"^(\d{3,4})x(\d{3,4})$", str(raw).strip())
        if match:
            width, height = int(match.group(1)), int(match.group(2))
            screen_w = self.window.winfo_screenwidth()
            screen_h = self.window.winfo_screenheight()
            # 防止上次在更大分辨率下调好的尺寸把窗口撑出当前屏幕
            width = max(360, min(width, screen_w - 40))
            height = max(320, min(height, screen_h - 80))
            return width, height
        return 470, 640

    def _on_window_configure(self, event):
        if event.widget is not self.window:
            return
        # 窗口处于图标化状态时上报的尺寸是无效值，必须跳过，
        # 否则最小化一次就会把窗口尺寸记成 1x1
        try:
            if self.window.state() != "normal":
                return
        except tk.TclError:
            return
        if event.width > 100 and event.height > 100:
            self._last_geometry = "%dx%d" % (event.width, event.height)

    def _build_widgets(self):
        tokens = self._tokens
        self.window.configure(bg=tokens["bg"])

        # ---------------- 顶部：搜索框 + 筛选条
        header = tk.Frame(self.window, bg=tokens["bg"])
        header.pack(fill="x", padx=10, pady=(10, 6))

        search_row = tk.Frame(header, bg=tokens["surface"], highlightthickness=1,
                              highlightbackground=tokens["border"])
        search_row.pack(fill="x")

        self.search_var = tk.StringVar()
        self.search_entry = tk.Entry(
            search_row, textvariable=self.search_var, bd=0, relief="flat",
            bg=tokens["surface"], fg=tokens["text"], insertbackground=tokens["accent"],
            font=(tokens["font_family"], 12),
        )
        self.search_entry.pack(side="left", fill="x", expand=True, padx=(10, 4), pady=7)
        self.search_entry.insert(0, "")
        self._search_placeholder = "搜索内容、来源应用或标签…"
        self._show_placeholder()
        self.search_entry.bind("<FocusIn>", self._clear_placeholder)

        self.clear_btn = tk.Label(
            search_row, text="✕", bg=tokens["surface"], fg=tokens["text_faint"],
            cursor="hand2", font=(tokens["font_family"], 10), padx=8,
        )
        self.clear_btn.pack(side="right")
        self.clear_btn.bind("<Button-1>", lambda e: self._clear_search())

        # ---- 窗口控制按钮（在面板内也提供一份，光靠标题栏不够直观）
        self.hide_btn = tk.Label(
            search_row, text="收起", bg=tokens["surface"], fg=tokens["text_muted"],
            cursor="hand2", font=(tokens["font_family"], 9), padx=6,
        )
        self.hide_btn.pack(side="right")
        self.hide_btn.bind("<Button-1>", lambda e: self.hide())
        tk.Label(search_row, text="|", bg=tokens["surface"], fg=tokens["border"],
                 font=(tokens["font_family"], 9)).pack(side="right")
        self.min_btn = tk.Label(
            search_row, text="—", bg=tokens["surface"], fg=tokens["text_muted"],
            cursor="hand2", font=(tokens["font_family"], 10), padx=6,
        )
        self.min_btn.pack(side="right")
        self.min_btn.bind("<Button-1>", lambda e: self.minimize())

        # 筛选条：全部 + 六类，带条数（F4-2 / F4-5）
        self.filter_row = tk.Frame(header, bg=tokens["bg"])
        self.filter_row.pack(fill="x", pady=(8, 0))
        self._type_filter = None
        self._type_buttons = {}
        self._render_filter_buttons()

        # ---------------- 中部：列表
        list_wrap = tk.Frame(self.window, bg=tokens["bg"])
        list_wrap.pack(fill="both", expand=True, padx=10, pady=(0, 6))

        self.canvas = tk.Canvas(list_wrap, bg=tokens["bg"], bd=0, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(list_wrap, orient="vertical", command=self.canvas.yview,
                                      width=10, bd=0, relief="flat")
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)

        self.list_frame = tk.Frame(self.canvas, bg=tokens["bg"])
        self._list_window = self.canvas.create_window(
            (0, 0), window=self.list_frame, anchor="nw"
        )
        self.list_frame.bind("<Configure>", self._on_frame_configure)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind_all("<MouseWheel>", self._on_mousewheel)

        # 空状态
        self.empty_label = tk.Label(
            self.list_frame, text="", bg=tokens["bg"], fg=tokens["text_faint"],
            font=(tokens["font_family"], 11), justify="center", pady=40,
        )

        # ---------------- 底部：状态 + 操作提示
        footer = tk.Frame(self.window, bg=tokens["surface_alt"])
        footer.pack(fill="x", side="bottom")
        self.status_label = tk.Label(
            footer, text="", bg=tokens["surface_alt"], fg=tokens["text_muted"],
            font=(tokens["font_family"], 9), anchor="w", padx=10, pady=5,
        )
        self.status_label.pack(side="left", fill="x", expand=True)

        self.menu_btn = tk.Label(
            footer, text="☰ 操作", bg=tokens["surface_alt"], fg=tokens["accent"],
            cursor="hand2", font=(tokens["font_family"], 9), padx=10, pady=5,
        )
        self.menu_btn.pack(side="right")
        self.menu_btn.bind("<Button-1>", self._on_menu_button)

        self.toast = Toast(self.window, lambda: self._tokens)

    def _render_filter_buttons(self):
        tokens = self._tokens
        for child in self.filter_row.winfo_children():
            child.destroy()
        self._type_buttons = {}
        items = [(None, "全部")] + [(t, TYPE_LABELS[t]) for t in ALL_TYPES]
        for content_type, label in items:
            btn = tk.Label(
                self.filter_row, text=label, padx=8, pady=3, cursor="hand2",
                font=(tokens["font_family"], 9),
                bg=tokens["bg"], fg=tokens["text_muted"],
            )
            btn.pack(side="left", padx=(0, 4))
            btn.bind("<Button-1>", lambda e, t=content_type: self._set_type_filter(t))
            self._type_buttons[content_type] = btn
        self._paint_filter_buttons()

    def _paint_filter_buttons(self):
        tokens = self._tokens
        counts = self._counts or {}
        for content_type, btn in self._type_buttons.items():
            active = content_type == self._type_filter
            key = content_type or "all"
            count = counts.get(key, 0)
            label = "全部" if content_type is None else TYPE_LABELS[content_type]
            btn.configure(
                text="%s %d" % (label, count) if count else label,
                bg=tokens["surface_sel"] if active else tokens["bg"],
                fg=tokens["accent"] if active else tokens["text_muted"],
            )

    # ==================================================================
    # 键盘与滚动
    # ==================================================================
    def _bind_keys(self):
        # 面板自身按键
        self.window.bind("<Escape>", lambda e: self.hide())
        self.window.bind("<F5>", lambda e: self.refresh())
        self.window.bind("<Delete>", lambda e: self._delete_selected())
        self.window.bind("<Control-p>", lambda e: self._toggle_pin_selected())
        self.window.bind("<Control-e>", lambda e: self._edit_selected())
        self.window.bind("<Control-f>", lambda e: self._focus_search())
        self.window.bind("<Control-a>", lambda e: self._select_first())
        self.window.bind("<Control-w>", lambda e: self.hide())
        self.window.bind("<Control-m>", lambda e: self.toggle_minimize())
        self.window.bind("<Control-comma>", lambda e: self.open_settings())
        self.window.bind("<Return>", self._on_return)
        self.window.bind("<KP_Enter>", self._on_return)
        self.window.bind("<Up>", lambda e: self._move_selection(-1))
        self.window.bind("<Down>", lambda e: self._move_selection(1))
        self.window.bind("<Prior>", lambda e: self._move_selection(-5))
        self.window.bind("<Next>", lambda e: self._move_selection(5))
        self.search_entry.bind("<Down>", lambda e: self._move_selection(1))
        self.search_entry.bind("<Up>", lambda e: self._move_selection(-1))
        self.search_entry.bind("<Return>", self._on_return)
        self.search_entry.bind("<Escape>", lambda e: self.hide())
        # Ctrl+1~9：直接粘贴第 N 条，熟练之后基本不用鼠标
        for index in range(1, 10):
            self.window.bind("<Control-Key-%d>" % index,
                             lambda e, i=index: self._paste_by_index(i))
        # 检索防抖（UC-08 第 3 步）
        self._search_trace = self.search_var.trace_add("write", self._on_search_changed)

    def _on_mousewheel(self, event):
        if not self._visible:
            return
        try:
            self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        except tk.TclError:
            pass
        self._maybe_render_more()

    def _on_frame_configure(self, _event=None):
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))

    def _on_canvas_configure(self, event):
        self.canvas.itemconfigure(self._list_window, width=event.width)

    def _maybe_render_more(self):
        """滚动到接近底部时继续渲染下一批（列表分页渲染）。"""
        if self._rendered >= len(self._entries):
            return
        try:
            first, last = self.canvas.yview()
        except tk.TclError:
            return
        if last >= 0.88:
            self._render_batch()

    # ==================================================================
    # 检索
    # ==================================================================
    def _show_placeholder(self):
        self.search_entry.configure(fg=self._tokens["text_faint"])
        self.search_var.set(self._search_placeholder)
        self._placeholder_active = True

    def _clear_placeholder(self, _event=None):
        if getattr(self, "_placeholder_active", False):
            self._placeholder_active = False
            self.search_var.set("")
            self.search_entry.configure(fg=self._tokens["text"])

    def _focus_search(self):
        self._clear_placeholder()
        self.search_entry.focus_set()
        self.search_entry.icursor("end")

    def _clear_search(self):
        self._placeholder_active = False
        self.search_var.set("")
        self.search_entry.configure(fg=self._tokens["text"])
        self._selected_id = None
        self.refresh(reset_scroll=True)

    def _set_type_filter(self, content_type):
        """切换类型筛选（F4-2 按类型筛选）。"""
        self._type_filter = content_type
        self._selected_id = None
        self._paint_filter_buttons()
        self.refresh(reset_scroll=True)
        label = TYPE_LABELS.get(content_type, "全部") if content_type else "全部"
        self._update_status(filter_label=label)

    def _current_query(self):
        if getattr(self, "_placeholder_active", False):
            return ""
        return self.search_var.get().strip()

    def _on_search_changed(self, *_args):
        if getattr(self, "_placeholder_active", False):
            return
        if self._search_after_id:
            try:
                self.window.after_cancel(self._search_after_id)
            except Exception:  # noqa: BLE001
                pass
        self._search_after_id = self.window.after(
            SEARCH_DEBOUNCE_MS, self._run_search
        )

    def _run_search(self):
        self._search_after_id = None
        query = self._current_query()
        if query == self._last_query and self._tag_only == self._last_tag_only:
            return
        self._last_query = query
        self.refresh(reset_scroll=True)

    # ==================================================================
    # 数据与渲染
    # ==================================================================
    def refresh(self, reset_scroll=False, keep_selection=True):
        """重新查询并渲染列表（事件驱动的唯一刷新入口）。"""
        query = self._current_query()
        if query:
            result = self.kernel.search_history(
                query, content_type=self._type_filter
            )
            items = result["data"]["items"] if result["ok"] else []
            truncated = result["data"].get("truncated") if result["ok"] else False
        else:
            result = self.kernel.get_history(limit=PAGE_SIZE, content_type=self._type_filter)
            items = result["data"]["items"] if result["ok"] else []
            truncated = False
        self._entries = items
        self._truncated = truncated

        counts = self.kernel.get_type_counts()
        self._counts = counts["data"]["counts"] if counts["ok"] else {}
        self._paint_filter_buttons()

        self._render(reset_scroll=reset_scroll, keep_selection=keep_selection)
        self._update_status()

    def _render(self, reset_scroll=False, keep_selection=True):
        if self._render_batch_id:
            try:
                self.window.after_cancel(self._render_batch_id)
            except Exception:  # noqa: BLE001
                pass
            self._render_batch_id = None
        for child in self.list_frame.winfo_children():
            if child is not self.empty_label:
                child.destroy()
        self.empty_label.pack_forget()
        self._card_map = {}
        self._rendered = 0

        if not self._entries:
            self._show_empty()
            self.canvas.yview_moveto(0)
            return

        if not keep_selection or all(e["id"] != self._selected_id for e in self._entries):
            self._selected_id = self._entries[0]["id"]
        self._render_batch()
        self.canvas.yview_moveto(0 if reset_scroll else 0)
        self._highlight_selection()

    def _show_empty(self):
        query = self._current_query()
        if query:
            text = "没有找到包含「%s」的记录\n\n换个关键字，或点击右上角 ✕ 清除检索" % query
        elif self._type_filter:
            text = "当前类型（%s）下还没有记录\n\n复制任意内容即可开始记录" % TYPE_LABELS.get(
                self._type_filter, self._type_filter)
        else:
            text = "还没有任何记录\n\n复制任意内容（文本 / 图片 / 文件）即可自动记录"
        self.empty_label.configure(text=text)
        self.empty_label.pack(fill="x")

    def _render_batch(self):
        """渲染下一批卡片（分批渲染，避免上千个控件一次性建出来卡住界面）。"""
        start = self._rendered
        end = min(start + RENDER_BATCH, len(self._entries))
        query = self._current_query()
        for entry in self._entries[start:end]:
            card = self._build_card(entry, query)
            card.pack(fill="x", pady=(0, self._gap()))
        self._rendered = end
        self.list_frame.update_idletasks()
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        self._update_status()

    def _gap(self):
        return theme_mod.DENSITY_METRICS.get(self._density, theme_mod.DENSITY_METRICS["normal"])["gap"]

    def _metrics(self):
        return theme_mod.DENSITY_METRICS.get(
            self.current_density(), theme_mod.DENSITY_METRICS["normal"])

    def current_density(self):
        """当前列表密度。以配置为准实时读取——内存里缓存的那份只在
        apply_theme 时更新，若别处改了配置而没走 apply_theme，缓存就会过期。"""
        return self.kernel.settings.get(K_LIST_DENSITY)

    def _build_card(self, entry, query):
        tokens = self._tokens
        metrics = self._metrics()
        entry_id = entry["id"]
        selected = entry_id == self._selected_id

        card = tk.Frame(
            self.list_frame, bg=tokens["surface_sel"] if selected else tokens["surface"],
            highlightthickness=1,
            highlightbackground=tokens["accent"] if selected else tokens["border"],
        )
        self._card_map[entry_id] = card

        inner = tk.Frame(card, bg=card["bg"])
        inner.pack(fill="x", padx=9, pady=metrics["pad_y"])

        # ---- 第一行：类型徽标 + 时间 + 标记
        top = tk.Frame(inner, bg=card["bg"])
        top.pack(fill="x")
        badge = tk.Label(
            top, text=entry["typeLabel"], padx=6, pady=1,
            bg=self._badge_color(entry["contentType"]), fg="#FFFFFF",
            font=(tokens["font_family"], 8),
        )
        badge.pack(side="left")
        if entry["isPinned"]:
            tk.Label(top, text="📌 置顶", bg=card["bg"], fg=tokens["pin"],
                     font=(tokens["font_family"], 8)).pack(side="left", padx=(6, 0))
        if entry["isSensitive"]:
            tk.Label(top, text="🔒 已脱敏", bg=card["bg"], fg=tokens["sensitive"],
                     font=(tokens["font_family"], 8)).pack(side="left", padx=(6, 0))
        if entry["useCount"]:
            tk.Label(top, text="×%d" % entry["useCount"], bg=card["bg"],
                     fg=tokens["text_faint"],
                     font=(tokens["font_family"], 8)).pack(side="left", padx=(6, 0))
        tk.Label(
            top, text=_human_time(entry["timestamp"]), bg=card["bg"],
            fg=tokens["text_faint"], font=(tokens["font_family"], 8),
        ).pack(side="right")

        # ---- 第二行：预览（带命中高亮）
        preview = entry["preview"] or "（无预览）"
        if entry["contentType"] == TYPE_IMAGE:
            preview = preview or "[图片]"
        self._build_preview(inner, card, preview, query, metrics)

        # ---- 第三行：来源 + 标签
        bottom = tk.Frame(inner, bg=card["bg"])
        bottom.pack(fill="x", pady=(2, 0))
        tk.Label(
            bottom, text="来源：%s" % (entry["sourceApp"] or "未知"), bg=card["bg"],
            fg=tokens["text_muted"], font=(tokens["font_family"], 8),
        ).pack(side="left")
        if entry["tags"]:
            tk.Label(
                bottom, text="🏷 " + " ".join(entry["tags"]), bg=card["bg"],
                fg=tokens["badge_url"], font=(tokens["font_family"], 8),
            ).pack(side="left", padx=(8, 0))

        self._bind_card_events(card, entry)
        return card

    def _build_preview(self, parent, card, preview, query, metrics):
        """预览文本；检索时把命中片段高亮出来（F5-4 命中高亮）。"""
        tokens = self._tokens
        font = (tokens["font_family"], 10 + metrics["font_delta"])
        wrap = self._panel_size[0] - 60
        if not query:
            tk.Label(
                parent, text=preview, bg=card["bg"], fg=tokens["text"],
                font=font, justify="left", anchor="w", wraplength=wrap,
            ).pack(fill="x", pady=(3, 0))
            return

        # 把预览按关键字切成 命中/未命中 交替的片段，逐段渲染
        segments = _split_by_query(preview, query)
        row = tk.Frame(parent, bg=card["bg"])
        row.pack(fill="x", pady=(3, 0))
        current = tk.Frame(row, bg=card["bg"])
        current.pack(anchor="w", fill="x")
        used_width = 0
        for text, hit in segments:
            if not text:
                continue
            if hit:
                widget = tk.Label(
                    current, text=text, bg=tokens["hit"], fg=tokens["hit_text"],
                    font=font, padx=0, pady=0,
                )
            else:
                widget = tk.Label(
                    current, text=text, bg=card["bg"], fg=tokens["text"], font=font,
                )
            widget.pack(side="left")
            used_width += len(text) * 9
            if used_width > wrap:
                current = tk.Frame(row, bg=card["bg"])
                current.pack(anchor="w", fill="x")
                used_width = 0

    def _badge_color(self, content_type):
        tokens = self._tokens
        return {
            "text": tokens["badge_text"],
            "rich_text": tokens["badge_rich"],
            "code": tokens["badge_code"],
            "url": tokens["badge_url"],
            "image": tokens["badge_image"],
            "file": tokens["badge_file"],
        }.get(content_type, tokens["badge_text"])

    # ==================================================================
    # 交互
    # ==================================================================
    def _bind_card_events(self, card, entry):
        entry_id = entry["id"]

        def select(_event=None):
            self._select(entry_id)

        def paste(_event=None):
            self._select(entry_id)
            self.paste_selected()

        for widget in _walk(card):
            widget.bind("<Enter>", lambda e, c=card, i=entry_id: self._on_card_enter(c, i))
            widget.bind("<Leave>", lambda e, c=card, i=entry_id: self._on_card_leave(c, i))
            widget.bind("<Button-1>", select)
            widget.bind("<Double-Button-1>", paste)
            widget.bind("<Button-3>", lambda e, i=entry_id: self._show_context_menu(e, i))

    def _on_card_enter(self, card, entry_id):
        if entry_id != self._selected_id:
            card.configure(bg=self._tokens["surface_hover"])
            for child in _walk(card):
                if isinstance(child, tk.Frame) or isinstance(child, tk.Label):
                    try:
                        if child["bg"] == self._tokens["surface"]:
                            child.configure(bg=self._tokens["surface_hover"])
                    except tk.TclError:
                        pass

    def _on_card_leave(self, card, entry_id):
        if entry_id != self._selected_id:
            card.configure(bg=self._tokens["surface"])
            for child in _walk(card):
                try:
                    if child["bg"] == self._tokens["surface_hover"]:
                        child.configure(bg=self._tokens["surface"])
                except tk.TclError:
                    pass

    def _select(self, entry_id):
        previous = self._selected_id
        self._selected_id = entry_id
        for eid in (previous, entry_id):
            card = self._card_map.get(eid)
            if card is None:
                continue
            selected = eid == self._selected_id
            bg = self._tokens["surface_sel"] if selected else self._tokens["surface"]
            card.configure(bg=bg, highlightbackground=(
                self._tokens["accent"] if selected else self._tokens["border"]))
            for child in _walk(card):
                try:
                    if child["bg"] in (self._tokens["surface_sel"],
                                       self._tokens["surface"],
                                       self._tokens["surface_hover"]):
                        child.configure(bg=bg)
                except tk.TclError:
                    pass

    def _highlight_selection(self):
        self._select(self._selected_id)

    def _select_first(self):
        if self._entries:
            self._select(self._entries[0]["id"])

    def _select_index(self):
        for index, entry in enumerate(self._entries):
            if entry["id"] == self._selected_id:
                return index
        return -1

    def _move_selection(self, delta):
        if not self._entries:
            return "break"
        index = self._select_index()
        target = max(0, min(len(self._entries) - 1, index + delta))
        if target == index:
            return "break"
        self._select(self._entries[target]["id"])
        self._ensure_rendered(target)
        self._scroll_to(target)
        return "break"

    def _ensure_rendered(self, index):
        while self._rendered <= index and self._rendered < len(self._entries):
            self._render_batch()

    def _scroll_to(self, index):
        card = self._card_map.get(self._entries[index]["id"])
        if card is None:
            return
        self.list_frame.update_idletasks()
        try:
            total = max(1, self.list_frame.winfo_height())
            offset = card.winfo_y()
            self.canvas.yview_moveto(max(0.0, min(1.0, (offset - 40) / float(total))))
        except tk.TclError:
            pass

    def _on_return(self, _event=None):
        self.paste_selected()
        return "break"

    def _on_menu_button(self, event):
        self._show_context_menu(event, self._selected_id)

    def _paste_by_index(self, index):
        if 0 < index <= len(self._entries):
            self._select(self._entries[index - 1]["id"])
            self.paste_selected()

    # ==================================================================
    # 动作
    # ==================================================================
    def paste_selected(self):
        """粘贴选中记录（UC-09 主事件流第 1 步）。"""
        if self._selected_id is None:
            self.toast.show("请先选中一条记录", TOAST_INFO)
            return
        entry = self._find_entry(self._selected_id)
        if entry is None:
            return
        result = self.kernel.paste_entry(self._selected_id, rich_paste=True, simulate=True)
        if not result["ok"]:
            self.toast.show(result["error"]["message"], TOAST_ERROR)
            return
        data = result["data"]
        if data.get("pasted"):
            self.toast.show("已粘贴", TOAST_SUCCESS, 1200)
        self.refresh(keep_selection=False)

    def copy_selected(self):
        if self._selected_id is None:
            return
        result = self.kernel.copy_entry(self._selected_id)
        if result["ok"]:
            self.toast.show("已复制到剪贴板", TOAST_SUCCESS)
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _copy_as_file_selected(self):
        """把图片记录以文件形式放进剪贴板（UC-09 备选流 A4）。"""
        if self._selected_id is None:
            return
        result = self.kernel.copy_entry_as_file(self._selected_id)
        if result["ok"]:
            self.toast.show("已复制为文件，到文件夹里按 Ctrl+V 即可",
                            TOAST_SUCCESS, 2400)
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _toggle_pin_selected(self):
        if self._selected_id is None:
            return
        entry = self._find_entry(self._selected_id)
        result = self.kernel.toggle_pin(self._selected_id)
        if result["ok"]:
            self.toast.show("已取消置顶" if entry and entry["isPinned"] else "已置顶", TOAST_SUCCESS)
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _edit_selected(self):
        if self._selected_id is None:
            return
        entry = self._find_entry(self._selected_id)
        if entry is None:
            return
        if not is_text_type(entry["contentType"]):
            self.toast.show("图片与文件类记录不支持内容编辑", TOAST_WARNING)
            return
        full = self.kernel.get_entry(self._selected_id)
        if not full["ok"]:
            self.toast.show(full["error"]["message"], TOAST_ERROR)
            return
        entry_id = self._selected_id
        EditorDialog(
            self.window, self._tokens, full["data"]["content"],
            on_save=lambda content: self._apply_edit(entry_id, content),
        )

    def _apply_edit(self, entry_id, content):
        result = self.kernel.update_entry_content(entry_id, content)
        if result["ok"]:
            self.toast.show("已保存并重算指纹", TOAST_SUCCESS)
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _tag_selected(self):
        if self._selected_id is None:
            return
        entry = self._find_entry(self._selected_id)
        if entry is None:
            return
        name = simpledialog.askstring(
            "添加标签", "标签名（不超过 20 字，已打标签的记录不会被自动清理）：",
            parent=self.window,
        )
        if not name:
            return
        result = self.kernel.add_tag(self._selected_id, name)
        if result["ok"]:
            self.toast.show("已添加标签「%s」" % name.strip(), TOAST_SUCCESS)
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _delete_selected(self):
        if self._selected_id is None:
            return
        entry = self._find_entry(self._selected_id)
        preview = (entry["preview"][:40] + "…") if entry and len(entry["preview"]) > 40 \
            else (entry["preview"] if entry else "")
        if not messagebox.askyesno(
            "删除记录",
            "确认删除这条记录？删除后不可恢复。\n\n类型：%s\n预览：%s"
            % (entry["typeLabel"] if entry else "", preview),
            parent=self.window,
        ):
            return
        result = self.kernel.delete_entry(self._selected_id)
        if result["ok"]:
            self._selected_id = None
            self.toast.show("已删除", TOAST_SUCCESS)
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def clear_history(self):
        usage = self.kernel.get_storage_usage()
        countable = usage["data"]["countable"] if usage["ok"] else 0
        protected = usage["data"]["protectedCount"] if usage["ok"] else 0
        if not messagebox.askyesno(
            "清空历史",
            "将清空 %d 条记录，操作不可恢复。\n\n置顶记录与已打标签的记录将被保留"
            "（当前有 %d 条受保护）。\n\n是否继续？" % (countable, protected),
            parent=self.window,
        ):
            return
        result = self.kernel.clear_history()
        if result["ok"]:
            self.toast.show("已清理 %d 条，保留 %d 条"
                            % (result["data"]["deleted"], result["data"]["kept"]), TOAST_SUCCESS)
            self.refresh(reset_scroll=True)
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def toggle_listen(self):
        status = self.kernel.get_listen_status()
        paused = status["data"]["paused"] if status["ok"] else False
        self.kernel.set_listening(paused)

    def open_settings(self):
        SettingsWindow(self)

    # ==================================================================
    # 右键菜单
    # ==================================================================
    def _show_context_menu(self, event, entry_id):
        if entry_id is None:
            return
        self._select(entry_id)
        entry = self._find_entry(entry_id)
        if entry is None:
            return
        tokens = self._tokens
        if self._menu is not None:
            try:
                self._menu.destroy()
            except tk.TclError:
                pass
        menu = tk.Menu(self.window, tearoff=0, bg=tokens["surface"], fg=tokens["text"],
                       activebackground=tokens["accent"], activeforeground="#FFFFFF",
                       bd=0)
        menu.add_command(label="粘贴（回车）", command=self.paste_selected)
        menu.add_command(label="仅复制到剪贴板", command=self.copy_selected)
        if entry["contentType"] == "image":
            # 图片默认走 CF_DIB（位图），粘到文件夹里不会有任何反应；
            # 这一项改成把附件文件路径放上剪贴板，资源管理器才认得。
            menu.add_command(label="复制为文件（可粘贴到文件夹）",
                             command=self._copy_as_file_selected)
        menu.add_separator()
        menu.add_command(
            label="取消置顶" if entry["isPinned"] else "置顶",
            command=self._toggle_pin_selected,
        )
        menu.add_command(label="添加标签…", command=self._tag_selected)
        menu.add_command(
            label="编辑内容…" if is_text_type(entry["contentType"]) else "编辑内容（仅文本类）",
            command=self._edit_selected,
        )
        menu.add_separator()
        menu.add_command(label="删除（Delete）", command=self._delete_selected)
        menu.add_separator()
        menu.add_command(label="清空历史…", command=self.clear_history)
        menu.add_command(label="设置…", command=self.open_settings)
        self._menu = menu
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    # ==================================================================
    # 显示 / 隐藏 / 主题
    # ==================================================================
    def show(self, focus_search=True, near_cursor=None):
        """唤出面板并把焦点交给自己（UC-19 主事件流第 6 步）。

        **必须在面板拿到焦点之前**记下当前前台窗口：稍后粘贴时全靠它把焦点
        交还回去（UC-09 业务规则 R4）。晚一步记下来的就是面板自己，
        粘贴就会打回本程序里——这是本功能最容易出现的缺陷。

        从最小化状态唤出时要显式还原：withdraw 过的窗口再 deiconify 会
        保持图标化，必须先 state("normal") 才能真的显示出来。
        """
        if near_cursor is None:
            near_cursor = bool(self.kernel.settings.get(K_FOLLOW_MOUSE))
        if near_cursor:
            x, y = _cursor_pos()
            width, height = self._panel_size
            x = max(0, min(x - width // 2, self.window.winfo_screenwidth() - width))
            y = max(0, min(y + 12, self.window.winfo_screenheight() - height))
            self.window.geometry("+%d+%d" % (x, y))
        else:
            self.window.geometry("+%d+%d" % self._default_pos)

        self._capture_paste_target()
        try:
            self.window.deiconify()
            if self.window.state() == "iconic":
                self.window.state("normal")
        except tk.TclError:
            pass
        self.window.attributes("-topmost", bool(self.kernel.settings.get(K_ALWAYS_ON_TOP)))
        self.window.lift()
        self.window.focus_force()
        self._visible = True
        self.refresh(keep_selection=True)
        if focus_search:
            self._focus_search()
        self._start_polling()

    def _capture_paste_target(self):
        """登记"粘贴时要回到哪个窗口"。"""
        try:
            from app.infrastructure.clipboard import paste as paste_mod
            hwnd = paste_mod.get_foreground_window()
            if hwnd and not self._is_own_window(hwnd):
                self.kernel.set_paste_target(hwnd)
        except Exception:  # noqa: BLE001 - 取不到就退化为"粘贴时用当前前台窗口"
            pass

    def _is_own_window(self, hwnd):
        """排除"前台窗口就是本程序自己"的情况，否则会把焦点还给自己。"""
        try:
            own = int(self.window.winfo_id())
        except tk.TclError:
            return False
        return int(hwnd) == own

    def hide(self):
        """隐藏面板并把焦点交还给用户原本在用的应用（UC-09 业务规则 R4）。

        隐藏 ≠ 退出：托盘图标与全局快捷键都还在，随时可以再唤出来。
        标题栏的关闭按钮、面板里的"收起"、Esc 走的都是这里。

        注意：这里**不能**停掉事件轮询。停掉之后全局快捷键与托盘菜单
        的事件就没人消费了，等于把"随时唤出来"这条路封死。
        """
        if self._visible:
            self._persist_geometry()
        self._visible = False
        # 记下隐藏时刻：紧接着到来的"唤出"事件要忽略掉，
        # 否则用户刚按 Esc 收起面板，120ms 轮询就把上一轮积压的
        # 唤出事件应用上去，面板又弹回来了
        self._hidden_at = time.time()
        try:
            self.window.withdraw()
        except tk.TclError:
            pass

    def minimize(self):
        """最小化到任务栏。

        最小化之后**依然算"面板已显示"**，所以粘贴前的焦点交还逻辑
        要能看到它并把焦点转到目标窗口；这一点由 hide() 之外的路径保证：
        真正粘贴时界面会调用 hide()，而 hide() 对最小化的窗口同样有效。
        """
        try:
            self.window.iconify()
        except tk.TclError:
            return
        self._visible = False
        self._hidden_at = time.time()

    def toggle_minimize(self):
        try:
            if self.window.state() == "iconic":
                self.show(focus_search=False)
            else:
                self.minimize()
        except tk.TclError:
            pass

    def _persist_geometry(self):
        """把当前窗口大小写回配置（只在隐藏时写一次，避免拖动时反复落库）。"""
        geometry = getattr(self, "_last_geometry", None)
        if not geometry:
            return
        if self.kernel.settings.get(K_WINDOW_GEOMETRY) == geometry:
            return
        result = self.kernel.update_setting(K_WINDOW_GEOMETRY, geometry)
        if not result["ok"]:
            self.logger.debug("窗口尺寸保存失败：%s", result["error"]["message"])

    def toggle(self):
        if self._visible:
            self.hide()
        else:
            self.show()

    def is_visible(self):
        return self._visible

    def _start_polling(self):
        """启动事件轮询 —— **只启动一次，永不停止**。

        这一点是踩过坑的：最初写成"面板显示时启动、隐藏时停止"，
        结果面板一收起来，全局快捷键与托盘菜单的事件就再也没人消费了，
        按 Ctrl+Shift+V 完全没反应（事件堆在内核队列里，界面看不到）。
        所以轮询必须常驻：它就是"后台驻留"这个产品形态的心跳。
        """
        if self._poll_after_id is None:
            self._poll_events()

    def _stop_polling(self):
        """仅在程序退出时调用。窗口隐藏/最小化都**不能**停轮询。"""
        if self._poll_after_id is not None:
            try:
                self.window.after_cancel(self._poll_after_id)
            except Exception:  # noqa: BLE001
                pass
            self._poll_after_id = None

    def _poll_events(self):
        """把内核事件转成界面动作。

        队列**始终**要消费：即便面板当前是隐藏的，
        clipboard-updated / toast / 快捷键动作这些事件也必须被处理，
        否则"隐藏状态下按快捷键唤出面板"这条链路就断了。
        """
        self.kernel.handle_listener_events()
        for event in self.kernel.poll_events():
            self._handle_event(event)
        self._poll_after_id = self.window.after(POLL_INTERVAL_MS, self._poll_events)

    def _handle_event(self, event):
        name = event["name"]
        payload = event["payload"]
        # 面板隐藏时不重绘列表（省 CPU），但**事件本身照样要处理** ——
        # 否则隐藏状态下按快捷键就唤不出面板了
        if name == EVT_CLIPBOARD_UPDATED:
            if not self._visible:
                return
            entry = payload.get("entry") or {}
            outcome = payload.get("outcome")
            if outcome == "inserted":
                self._selected_id = entry.get("id")
                self.refresh(reset_scroll=True)
                if entry.get("isSensitive"):
                    self.toast.show("已记录（含敏感信息，列表按脱敏显示）", TOAST_WARNING)
            elif outcome == "merged":
                self.refresh(reset_scroll=True)
            else:
                self.refresh(keep_selection=True)
        elif name == EVT_CLIPBOARD_REMOVED:
            if self._visible:
                self.refresh(keep_selection=True)
        elif name == EVT_THEME_CHANGED:
            self.apply_theme()
        elif name == EVT_LISTEN_STATUS_CHANGED:
            if self._visible:
                self._update_status()
        elif name == EVT_TOAST:
            action = payload.get("action")
            message = payload.get("message", "")
            if action in ("toggle_panel", "show_panel", "show_search"):
                if self._suppress_show():
                    return
                if action == "toggle_panel":
                    self.toggle()
                else:
                    self.show(focus_search=(action == "show_search"))
            elif action == "show_settings":
                if self._suppress_show():
                    return
                self.show(focus_search=False)
                self.open_settings()
            elif action == "exit":
                self.root.event_generate("<<ClipboardProExit>>")
            elif message and message not in ("show", "hotkey"):
                self.toast.show(message, payload.get("type", TOAST_INFO))

    def _suppress_show(self):
        """判断"刚刚才隐藏"的抑制窗口是否还生效。

        背景：快捷键是开关式的，用户按一次收起面板，紧接着可能又按一次
        想唤出来。但如果收起动作本身还残留着一个未消费的"唤出"事件，
        面板会在 120ms 后自己弹回来，看起来像"关不掉"。
        这里用一个很短的时间窗把这种回声吃掉，正常的手速不受影响。
        """
        hidden_at = getattr(self, "_hidden_at", 0)
        if hidden_at and (time.time() - hidden_at) < SHOW_SUPPRESS_MS / 1000.0:
            return True
        return False

    def apply_theme(self):
        """主题或密度变化后重新套用令牌（UC-15 主事件流第 4~6 步）。"""
        self._tokens = theme_mod.get_tokens(
            self.kernel.settings.get(K_THEME), self.kernel.settings.get(K_THEME_MODE)
        )
        self._density = self.kernel.settings.get(K_LIST_DENSITY)
        tokens = self._tokens
        self.window.configure(bg=tokens["bg"])
        for widget in _walk(self.window):
            try:
                current = widget["bg"]
            except tk.TclError:
                continue
            if isinstance(widget, tk.Entry):
                widget.configure(bg=tokens["surface"], fg=tokens["text"],
                                 insertbackground=tokens["accent"])
            elif isinstance(widget, tk.Label) and widget is self.clear_btn:
                widget.configure(bg=tokens["surface"], fg=tokens["text_faint"])
            elif current in ("#F3F3F3",) or isinstance(widget, (tk.Frame, tk.Canvas)):
                pass
        self.canvas.configure(bg=tokens["bg"])
        self.list_frame.configure(bg=tokens["bg"])
        self.render_static_parts()
        self.refresh(keep_selection=True)

    def render_static_parts(self):
        """重新渲染不依赖数据的静态部分（筛选条、状态栏、提示）。"""
        tokens = self._tokens
        self._render_filter_buttons()
        self.search_entry.configure(font=(tokens["font_family"], 12))
        self.status_label.configure(bg=tokens["surface_alt"], fg=tokens["text_muted"],
                                    font=(tokens["font_family"], 9))
        self.menu_btn.configure(bg=tokens["surface_alt"], fg=tokens["accent"],
                                font=(tokens["font_family"], 9))
        self._update_status()

    def _update_status(self, filter_label=None):
        listen = self.kernel.get_listen_status()
        data = listen["data"] if listen["ok"] else {}
        paused = data.get("paused")
        state = "已暂停" if paused else "监听中"
        # 显示**实际生效**的快捷键：配置里的键可能被别的程序占用、
        # 启动时已自动降级到替代键，显示配置值会让用户按了没反应还找不到原因
        hotkey = data.get("registered", {}).get(K_MAIN_HOTKEY) \
            or self.kernel.settings.get(K_MAIN_HOTKEY)
        shown = min(self._rendered, len(self._entries))
        current_filter = filter_label
        if current_filter is None:
            current_filter = TYPE_LABELS.get(self._type_filter, "全部") \
                if self._type_filter else "全部"
        text = "%s · %s %d/%d 条 · %s 唤出 · 双击粘贴" % (
            state, current_filter, shown, len(self._entries), hotkey)
        if getattr(self, "_truncated", False):
            text += " · 结果过多，请补充关键字"
        try:
            self.status_label.configure(text=text,
                                        fg=self._tokens["pin"] if paused
                                        else self._tokens["text_muted"])
        except tk.TclError:
            pass

    def _find_entry(self, entry_id):
        for entry in self._entries:
            if entry["id"] == entry_id:
                return entry
        return None


# ======================================================================
# 编辑对话框
# ======================================================================
class EditorDialog(tk.Toplevel):
    """记录内容编辑框（UC-12 主事件流第 2 步）。

    采用**回调式**而不是 ``wait_window`` 阻塞式：阻塞式在"窗口尚未映射"
    的时刻调用会直接死锁（tkinter 的 wait_window 依赖事件循环分发
    Destroy 事件）。回调式还顺带避免了"对话框没关就点别的"这类焦点问题。
    """

    def __init__(self, parent, tokens, content, on_save=None):
        super().__init__(parent)
        self.on_save = on_save
        self.title("编辑记录内容")
        self.configure(bg=tokens["bg"])
        self.transient(parent)
        self.resizable(True, True)

        tk.Label(self, text="内容（保存后会重新计算指纹，重复判定按新内容生效）",
                 bg=tokens["bg"], fg=tokens["text_muted"],
                 font=(tokens["font_family"], 9)).pack(anchor="w", padx=12, pady=(10, 4))
        self.text = tk.Text(
            self, width=64, height=14, wrap="word", bd=1, relief="solid",
            bg=tokens["surface"], fg=tokens["text"], insertbackground=tokens["accent"],
            font=(tokens["font_family"], 10),
        )
        self.text.pack(fill="both", expand=True, padx=12)
        self.text.insert("1.0", content or "")

        buttons = tk.Frame(self, bg=tokens["bg"])
        buttons.pack(fill="x", padx=12, pady=10)
        tk.Button(buttons, text="取消", command=self._cancel, width=10).pack(side="right")
        tk.Button(buttons, text="保存", command=self._save, width=10).pack(side="right", padx=(0, 8))
        self.bind("<Escape>", lambda e: self._cancel())
        self.bind("<Control-Return>", lambda e: self._save())

        self.update_idletasks()
        x = parent.winfo_rootx() + 30
        y = parent.winfo_rooty() + 40
        self.geometry("+%d+%d" % (x, y))
        self.text.focus_set()

    def get_content(self):
        """读取当前编辑框内容（供自动化测试直接使用）。"""
        return self.text.get("1.0", "end-1c")

    def set_content(self, content):
        self.text.delete("1.0", "end")
        self.text.insert("1.0", content)

    def _save(self):
        content = self.get_content()
        if not content.strip():
            messagebox.showwarning("内容为空", "内容不能为空；如需删除该条，请使用删除功能。",
                                   parent=self)
            return
        callback = self.on_save
        self.destroy()
        if callback:
            callback(content)

    def _cancel(self):
        self.destroy()


# ======================================================================
# 工具
# ======================================================================
def _walk(widget):
    """深度遍历控件树（含自身）。"""
    stack = [widget]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(current.winfo_children())


def _split_by_query(text, query):
    """把文本按关键字切成 [(片段, 是否命中)] 列表（不区分大小写）。"""
    if not query:
        return [(text, False)]
    lower_text = text.lower()
    lower_query = query.lower()
    segments = []
    cursor = 0
    while True:
        index = lower_text.find(lower_query, cursor)
        if index < 0:
            break
        if index > cursor:
            segments.append((text[cursor:index], False))
        segments.append((text[index:index + len(query)], True))
        cursor = index + len(query)
    if cursor < len(text):
        segments.append((text[cursor:], False))
    return segments or [(text, False)]


def _human_time(timestamp_ms):
    """毫秒时间戳 -> 友好的相对时间（列表按今天/昨天/本周/更早分组的基础）。"""
    if not timestamp_ms:
        return ""
    now = now_ms()
    delta = now - int(timestamp_ms)
    if delta < 0:
        delta = 0
    seconds = delta // 1000
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return "%d 分钟前" % (seconds // 60)
    if seconds < 86400:
        return "%d 小时前" % (seconds // 3600)
    days = seconds // 86400
    if days == 1:
        return "昨天"
    if days < 7:
        return "%d 天前" % days
    import datetime
    return datetime.datetime.fromtimestamp(int(timestamp_ms) / 1000).strftime("%m-%d")


def _cursor_pos():
    try:
        from app.infrastructure.clipboard import win32 as w32
        return w32.get_cursor_pos()
    except Exception:  # noqa: BLE001
        return 0, 0
