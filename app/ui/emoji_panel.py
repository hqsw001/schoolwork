# -*- coding: utf-8 -*-
"""表情面板（对应 TieZ 的 EmojiPanel.tsx）

两个 Tab：

* **Emoji** —— 内置分组表，点一下就粘到目标窗口
* **收藏** —— 图片表情包，从本地文件添加，点一下当**图片**粘出去

三个设计点：

* **点一下就粘，不是复制。** 走的是"临时粘贴"：先快照剪贴板、粘完再还原，
  用户原来的剪贴板内容不会被表情覆盖。
* **收藏显示的是缩略图不是原图。** 收藏里可能有几 MB 的动图，直接塞进
  网格会把面板卡死。
* **删除收藏要防目录穿越。** 路径来自界面，内核与 emoji 模块都会校验它
  确实在收藏目录之内。
"""

import tkinter as tk
from tkinter import filedialog

from app.constants import TOAST_ERROR, TOAST_SUCCESS

#: Emoji 网格每行几个
EMOJI_COLUMNS = 12
#: 收藏网格每行几个
FAVORITE_COLUMNS = 7
#: 收藏缩略图边长
FAVORITE_ICON_SIZE = 44

#: Windows 上显示彩色 Emoji 的字体
EMOJI_FONT = "Segoe UI Emoji"


class EmojiPanel(tk.Toplevel):
    """表情面板窗口。"""

    def __init__(self, panel):
        super().__init__(panel.window)
        self.panel = panel
        self.kernel = panel.kernel
        self.tokens = panel._tokens
        self.toast = panel.toast

        self._tab = "emoji"
        self._groups = []
        self._favorites = []
        self._icon_refs = {}
        self._favorite_widgets = {}
        self._delete_target = None

        self.title("表情")
        self.configure(bg=self.tokens["bg"])
        self.transient(panel.window)
        self.resizable(True, True)
        self._build()
        self._load_groups()
        self._refresh_favorites()
        self._center(panel.window)
        self.bind("<Escape>", lambda e: self.destroy())

    # ==================================================================
    # 布局
    # ==================================================================
    def _font(self, size=10, bold=False, emoji=False):
        if emoji:
            return (EMOJI_FONT, size)
        family = self.tokens["font_family"]
        return (family, size, "bold") if bold else (family, size)

    def _build(self):
        tokens = self.tokens
        root = tk.Frame(self, bg=tokens["bg"])
        root.pack(fill="both", expand=True, padx=10, pady=8)

        header = tk.Frame(root, bg=tokens["bg"])
        header.pack(fill="x")
        self.emoji_tab = tk.Button(header, text="Emoji", font=self._font(10),
                                   relief="flat", bd=0, padx=10,
                                   command=lambda: self._switch_tab("emoji"))
        self.emoji_tab.pack(side="left")
        self.fav_tab = tk.Button(header, text="收藏", font=self._font(10),
                                 relief="flat", bd=0, padx=10,
                                 command=lambda: self._switch_tab("favorite"))
        self.fav_tab.pack(side="left", padx=(4, 0))
        self.count_label = tk.Label(header, text="", bg=tokens["bg"],
                                    fg=tokens["text_faint"], font=self._font(8))
        self.count_label.pack(side="left", padx=(10, 0))
        tk.Button(header, text="关闭", font=self._font(9), width=8,
                  command=self.destroy).pack(side="right")

        self.hint = tk.Label(root, text="", bg=tokens["bg"], fg=tokens["text_faint"],
                             font=self._font(8), anchor="w", justify="left")
        self.hint.pack(fill="x", pady=(4, 4))

        wrap = tk.Frame(root, bg=tokens["bg"])
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=tokens["bg"], highlightthickness=0,
                                width=520, height=360)
        scrollbar = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.body = tk.Frame(self.canvas, bg=tokens["bg"])
        self._body_window = self.canvas.create_window((0, 0), window=self.body,
                                                      anchor="nw")
        self.body.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._body_window, width=e.width))
        self.canvas.bind("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, event):
        self.canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")

    def _center(self, parent):
        self.update_idletasks()
        width = max(560, self.winfo_reqwidth())
        height = min(560, max(380, self.winfo_reqheight()))
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + 40
        x = max(0, x)
        y = max(0, min(y, self.winfo_screenheight() - height - 40))
        self.geometry("%dx%d+%d+%d" % (width, height, x, y))

    # ==================================================================
    # Tab
    # ==================================================================
    def _switch_tab(self, tab):
        self._tab = tab
        self._paint_tabs()
        if tab == "emoji":
            self._render_groups()
        else:
            # 必须**先重新查询**再渲染：收藏目录可能被外部改动
            # （用户自己往里放文件，或者别的窗口刚加过），
            # 只渲染内存里那份旧列表会显示不出新收藏。
            self._refresh_favorites()
            self._render_favorites()

    def _paint_tabs(self):
        tokens = self.tokens
        active = tokens["surface_sel"]
        idle = tokens["bg"]
        for name, widget in (("emoji", self.emoji_tab), ("favorite", self.fav_tab)):
            selected = name == self._tab
            widget.configure(
                bg=active if selected else idle,
                fg=tokens["accent"] if selected else tokens["text_muted"],
                activebackground=active,
                highlightthickness=1,
                highlightbackground=tokens["accent"] if selected else idle,
            )

    def _clear_body(self):
        for child in self.body.winfo_children():
            child.destroy()
        self._icon_refs = {}
        self._favorite_widgets = {}

    # ==================================================================
    # Emoji
    # ==================================================================
    def _load_groups(self):
        result = self.kernel.get_emoji_groups()
        self._groups = result["data"]["groups"] if result["ok"] else []
        self._paint_tabs()
        self._render_groups()

    def _render_groups(self):
        tokens = self.tokens
        self._clear_body()
        total = sum(len(g["emojis"]) for g in self._groups)
        self.count_label.configure(text="%d 个表情" % total)
        self.hint.configure(text="点一下就粘到刚才那个窗口；不会覆盖你的剪贴板内容")

        for group in self._groups:
            tk.Label(self.body, text=group["name"], bg=tokens["bg"],
                     fg=tokens["accent"], font=self._font(9, True),
                     anchor="w").pack(fill="x", pady=(8, 2))
            grid = tk.Frame(self.body, bg=tokens["bg"])
            grid.pack(fill="x")
            for index, char in enumerate(group["emojis"]):
                cell = tk.Label(grid, text=char, bg=tokens["bg"],
                                font=self._font(16, emoji=True), width=2, cursor="hand2")
                cell.grid(row=index // EMOJI_COLUMNS, column=index % EMOJI_COLUMNS,
                          padx=1, pady=1)
                cell.bind("<Button-1>", lambda e, c=char: self._send_emoji(c))
                cell.bind("<Enter>",
                          lambda e, w=cell: w.configure(bg=tokens["surface_hover"]))
                cell.bind("<Leave>",
                          lambda e, w=cell: w.configure(bg=tokens["bg"]))

    def _send_emoji(self, char):
        # 面板自己要被收起/隐藏，否则 Ctrl+V 会打回面板里
        if self.kernel._frontend_hide_callback:
            try:
                self.kernel._frontend_hide_callback()
            except Exception:  # noqa: BLE001
                pass
        result = self.kernel.paste_emoji(char)
        if result["ok"]:
            self.toast.show("已粘贴 %s" % char, TOAST_SUCCESS, 1200)
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    # ==================================================================
    # 收藏
    # ==================================================================
    def _refresh_favorites(self):
        result = self.kernel.list_emoji_favorites()
        self._favorites = result["data"]["items"] if result["ok"] else []

    def _render_favorites(self):
        tokens = self.tokens
        self._clear_body()
        self.count_label.configure(text="%d 个收藏" % len(self._favorites))
        self.hint.configure(
            text="点一下当图片粘出去；右键点卡片可以删除。图片存在数据目录的"
                 " emoji_favorites 下，可以自己往里放文件。")

        grid = tk.Frame(self.body, bg=tokens["bg"])
        grid.pack(fill="x", pady=(4, 0))
        for index, item in enumerate(self._favorites):
            self._build_favorite_card(grid, item, index)
        # 末尾的"+"卡片
        self._build_add_card(grid, len(self._favorites))

        if not self._favorites:
            tk.Label(self.body,
                     text="还没有图片表情。\n点下面的「+」从本地选一张图，"
                          "或者直接把图片文件复制到收藏目录。",
                     bg=tokens["bg"], fg=tokens["text_faint"],
                     font=self._font(9), justify="left").pack(anchor="w", pady=(10, 0))

    def _build_favorite_card(self, parent, item, index):
        tokens = self.tokens
        path = item["path"]
        card = tk.Frame(parent, bg=tokens["surface"], width=74, height=74,
                        highlightthickness=1, highlightbackground=tokens["border"])
        card.grid(row=index // FAVORITE_COLUMNS, column=index % FAVORITE_COLUMNS,
                  padx=3, pady=3)
        card.grid_propagate(False)
        self._favorite_widgets[path] = card

        icon = self.kernel.get_emoji_favorite_icon(path, size=FAVORITE_ICON_SIZE)
        if icon["ok"]:
            try:
                photo = tk.PhotoImage(data=icon["data"]["data"])
                # 必须留引用，否则图片被 GC 掉，卡片变空白
                self._icon_refs[path] = photo
                tk.Label(card, image=photo, bg=tokens["surface"], bd=0).pack(
                    expand=True)
            except tk.TclError:
                tk.Label(card, text="?", bg=tokens["surface"],
                         fg=tokens["text_muted"], font=self._font(14)).pack(expand=True)
        else:
            tk.Label(card, text="已失效", bg=tokens["surface"],
                     fg=tokens["text_faint"], font=self._font(8)).pack(expand=True)

        for widget in (card,):
            widget.bind("<Button-1>", lambda e, p=path: self._send_favorite(p))
            widget.bind("<Button-3>", lambda e, p=path: self._show_delete(p))

    def _build_add_card(self, parent, index):
        tokens = self.tokens
        card = tk.Frame(parent, bg=tokens["bg"], width=74, height=74,
                        highlightthickness=1, highlightbackground=tokens["text_faint"],
                        cursor="hand2")
        card.grid(row=index // FAVORITE_COLUMNS, column=index % FAVORITE_COLUMNS,
                  padx=3, pady=3)
        card.grid_propagate(False)
        tk.Label(card, text="+", bg=tokens["bg"], fg=tokens["text_muted"],
                 font=self._font(18)).pack(expand=True)
        card.bind("<Button-1>", lambda e: self._add_favorite())

    def _show_delete(self, path):
        """右键：在卡片上显示一个删除按钮（对应 TieZ 的 deleteTarget）。"""
        card = self._favorite_widgets.get(path)
        if card is None:
            return
        if self._delete_target == path:
            return
        self._delete_target = path
        tokens = self.tokens
        button = tk.Label(card, text="删", bg=tokens["pin"], fg="#FFFFFF",
                          font=self._font(8), cursor="hand2")
        button.place(relx=1.0, rely=1.0, anchor="se")
        button.bind("<Button-1>", lambda e, p=path: self._delete_favorite(p))

    def _add_favorite(self):
        path = filedialog.askopenfilename(
            parent=self, title="选择表情图片",
            filetypes=[("图片", "*.png *.jpg *.jpeg *.webp *.gif"),
                       ("所有文件", "*.*")])
        if not path:
            return
        result = self.kernel.save_emoji_favorite(path)
        if result["ok"]:
            self.toast.show("已添加表情", TOAST_SUCCESS)
            self._refresh_favorites()
            self._render_favorites()
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _delete_favorite(self, path):
        result = self.kernel.remove_emoji_favorite(path)
        if result["ok"]:
            self._delete_target = None
            self.toast.show("已删除该收藏", TOAST_SUCCESS, 1400)
            self._refresh_favorites()
            self._render_favorites()
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _send_favorite(self, path):
        if self._delete_target == path:
            # 删除按钮已经显示出来了，这一下点击不当成"发送"
            return
        if self.kernel._frontend_hide_callback:
            try:
                self.kernel._frontend_hide_callback()
            except Exception:  # noqa: BLE001
                pass
        result = self.kernel.paste_image_file(path)
        if result["ok"]:
            self.toast.show("已粘贴图片表情", TOAST_SUCCESS, 1200)
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)
