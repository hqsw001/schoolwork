# -*- coding: utf-8 -*-
"""标签管理器（UC-11 / 对应 TieZ 的 TagManager.tsx）

左侧标签列表（颜色点 + 名称 + 数量），右侧是打了该标签的记录，
支持多选批量删除与批量复制。

三个设计点：

* **内置标签禁止改名与删除。** ``sensitive`` / ``密码`` / ``password``
  关联着"敏感信息保护"：改名之后新采集的敏感内容就不会再被打上标记，
  保护会**静默失效**。所以界面上直接把按钮禁掉并说明原因，而不是等点了
  才弹错误。
* **改名遇到重名按合并处理。** 用户想把"待用"并进"重要"，报错只会让他
  回去一条条手动改。仓储层已经按合并实现，这里只负责提示"已合并 N 条"。
* **用原生取色器。** ``tkinter.colorchooser`` 调的是系统颜色对话框，
  比自绘色板省事也更符合习惯。
"""

import tkinter as tk
from tkinter import colorchooser, messagebox, simpledialog

from app.constants import TAG_NAME_MAX_LEN, TOAST_ERROR, TOAST_INFO, TOAST_SUCCESS

#: 标签没有自定义颜色时的兜底色
DEFAULT_TAG_COLOR = "#888888"

#: 每个标签最多列出多少条记录
TAG_ITEMS_LIMIT = 200


def tag_color_for(name, color):
    """标签颜色：优先用用户设的，没设就按名字哈希出一个稳定颜色。

    哈希色要**稳定**：同一个标签每次打开颜色都一样，否则用户会以为
    自己设置丢了。
    """
    if color:
        return color
    total = 0
    for char in str(name):
        total = (total * 131 + ord(char)) & 0xFFFFFF
    hue = total % 360
    return _hsl_to_hex(hue, 0.62, 0.52)


def _hsl_to_hex(hue, saturation, lightness):
    import colorsys
    red, green, blue = colorsys.hls_to_rgb(hue / 360.0, lightness, saturation)
    return "#%02x%02x%02x" % (int(red * 255), int(green * 255), int(blue * 255))


class TagManager(tk.Toplevel):
    """标签管理窗口。"""

    def __init__(self, panel):
        super().__init__(panel.window)
        self.panel = panel
        self.kernel = panel.kernel
        self.tokens = panel._tokens
        self.toast = panel.toast

        self._tags = []
        self._current_tag = None
        self._items = []
        self._checked = set()
        self._row_widgets = {}

        self.title("标签管理")
        self.configure(bg=self.tokens["bg"])
        self.transient(panel.window)
        self.resizable(True, True)
        self._build()
        self._refresh_tags()
        self._center(panel.window)
        self.bind("<Escape>", lambda e: self.destroy())

    # ==================================================================
    # 布局
    # ==================================================================
    def _font(self, size=10, bold=False):
        family = self.tokens["font_family"]
        return (family, size, "bold") if bold else (family, size)

    def _build(self):
        tokens = self.tokens
        root = tk.Frame(self, bg=tokens["bg"])
        root.pack(fill="both", expand=True, padx=12, pady=10)

        header = tk.Frame(root, bg=tokens["bg"])
        header.pack(fill="x")
        tk.Label(header, text="标签管理", bg=tokens["bg"], fg=tokens["text"],
                 font=self._font(14, True)).pack(side="left")
        tk.Label(header,
                 text="给记录打标签后不会被容量上限自动清理",
                 bg=tokens["bg"], fg=tokens["text_faint"],
                 font=self._font(8)).pack(side="left", padx=(8, 0))
        tk.Button(header, text="关闭", font=self._font(9), width=8,
                  command=self.destroy).pack(side="right")

        body = tk.Frame(root, bg=tokens["bg"])
        body.pack(fill="both", expand=True, pady=(8, 0))
        self._build_sidebar(body)
        self._build_content(body)

    def _build_sidebar(self, parent):
        tokens = self.tokens
        side = tk.Frame(parent, bg=tokens["surface"], width=190)
        side.pack(side="left", fill="y", padx=(0, 10))
        side.pack_propagate(False)

        tk.Label(side, text="标签", bg=tokens["surface"], fg=tokens["text_muted"],
                 font=self._font(9, True)).pack(anchor="w", padx=8, pady=(8, 4))

        create_row = tk.Frame(side, bg=tokens["surface"])
        create_row.pack(fill="x", padx=8)
        self.new_tag_var = tk.StringVar()
        entry = tk.Entry(create_row, textvariable=self.new_tag_var,
                         font=self._font(9), bg=tokens["bg"], fg=tokens["text"],
                         bd=1, relief="solid")
        entry.pack(side="left", fill="x", expand=True)
        entry.bind("<Return>", lambda e: self._create_tag())
        tk.Button(create_row, text="+", font=self._font(9), width=2,
                  command=self._create_tag).pack(side="left", padx=(3, 0))
        tk.Label(side, text="新建标签（回车）", bg=tokens["surface"],
                 fg=tokens["text_faint"], font=self._font(8)).pack(anchor="w", padx=8)

        self.tag_list = tk.Frame(side, bg=tokens["surface"])
        self.tag_list.pack(fill="both", expand=True, padx=4, pady=(8, 4))

    def _build_content(self, parent):
        tokens = self.tokens
        content = tk.Frame(parent, bg=tokens["bg"])
        content.pack(side="left", fill="both", expand=True)

        toolbar = tk.Frame(content, bg=tokens["bg"])
        toolbar.pack(fill="x")
        self.breadcrumb = tk.Label(toolbar, text="请选择左侧的标签",
                                   bg=tokens["bg"], fg=tokens["text"],
                                   font=self._font(11, True))
        self.breadcrumb.pack(side="left")

        self.rename_btn = tk.Button(toolbar, text="重命名", font=self._font(9),
                                    command=self._rename_tag, state="disabled")
        self.rename_btn.pack(side="right")
        self.color_btn = tk.Button(toolbar, text="改颜色", font=self._font(9),
                                   command=self._pick_color, state="disabled")
        self.color_btn.pack(side="right", padx=4)
        self.delete_tag_btn = tk.Button(toolbar, text="删除标签", font=self._font(9),
                                        command=self._delete_tag, state="disabled")
        self.delete_tag_btn.pack(side="right")

        self.hint = tk.Label(content, text="", bg=tokens["bg"], fg=tokens["text_faint"],
                             font=self._font(8), anchor="w", justify="left")
        self.hint.pack(fill="x", pady=(4, 0))

        bulk = tk.Frame(content, bg=tokens["bg"])
        bulk.pack(fill="x", pady=(6, 4))
        self.select_all_var = tk.BooleanVar(value=False)
        tk.Checkbutton(bulk, text="全选", variable=self.select_all_var,
                       command=self._toggle_all, font=self._font(9),
                       bg=tokens["bg"], fg=tokens["text"],
                       activebackground=tokens["bg"],
                       selectcolor=tokens["surface"]).pack(side="left")
        self.sel_label = tk.Label(bulk, text="未选中", bg=tokens["bg"],
                                  fg=tokens["text_muted"], font=self._font(9))
        self.sel_label.pack(side="left", padx=(8, 0))
        self.copy_btn = tk.Button(bulk, text="复制选中内容", font=self._font(9),
                                  command=self._copy_selected, state="disabled")
        self.copy_btn.pack(side="right")
        self.delete_btn = tk.Button(bulk, text="删除选中", font=self._font(9),
                                    command=self._delete_selected, state="disabled")
        self.delete_btn.pack(side="right", padx=4)

        wrap = tk.Frame(content, bg=tokens["bg"])
        wrap.pack(fill="both", expand=True)
        self.canvas = tk.Canvas(wrap, bg=tokens["bg"], highlightthickness=0)
        scrollbar = tk.Scrollbar(wrap, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.items_frame = tk.Frame(self.canvas, bg=tokens["bg"])
        self._list_window = self.canvas.create_window(
            (0, 0), window=self.items_frame, anchor="nw")
        self.items_frame.bind(
            "<Configure>",
            lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind(
            "<Configure>",
            lambda e: self.canvas.itemconfigure(self._list_window, width=e.width))

    def _center(self, parent):
        self.update_idletasks()
        width = max(720, self.winfo_reqwidth())
        height = min(620, max(440, self.winfo_reqheight()))
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + 30
        x = max(0, x)
        y = max(0, min(y, self.winfo_screenheight() - height - 30))
        self.geometry("%dx%d+%d+%d" % (width, height, x, y))

    # ==================================================================
    # 标签列表
    # ==================================================================
    def _refresh_tags(self, keep_selection=True):
        result = self.kernel.list_tags()
        self._tags = result["data"]["items"] if result["ok"] else []
        for child in self.tag_list.winfo_children():
            child.destroy()

        if not self._tags:
            tk.Label(self.tag_list, text="还没有标签\n\n复制内容后右键「添加标签…」",
                     bg=self.tokens["surface"], fg=self.tokens["text_faint"],
                     font=self._font(8), justify="left").pack(anchor="w", padx=8, pady=6)
            self._current_tag = None
            self._refresh_items()
            return

        for item in self._tags:
            self._build_tag_row(item)

        names = [item["name"] for item in self._tags]
        if not keep_selection or self._current_tag not in names:
            self._current_tag = names[0]
        self._refresh_items()

    def _build_tag_row(self, item):
        tokens = self.tokens
        name = item["name"]
        selected = name == self._current_tag
        row = tk.Frame(self.tag_list,
                       bg=tokens["surface_sel"] if selected else tokens["surface"],
                       highlightthickness=1,
                       highlightbackground=tokens["accent"] if selected else tokens["surface"])
        row.pack(fill="x", pady=1)

        dot = tk.Label(row, text="  ", bg=tag_color_for(name, item.get("color")),
                       font=self._font(6))
        dot.pack(side="left", padx=(6, 4))
        label = tk.Label(row, text=name, bg=row["bg"], fg=tokens["text"],
                         font=self._font(9), anchor="w")
        label.pack(side="left", fill="x", expand=True)
        if item.get("protected"):
            tk.Label(row, text="内置", bg=row["bg"], fg=tokens["text_faint"],
                     font=self._font(7)).pack(side="right", padx=(0, 6))
        tk.Label(row, text=str(item["count"]), bg=row["bg"], fg=tokens["text_faint"],
                 font=self._font(8)).pack(side="right", padx=(0, 4))

        for widget in (row, dot, label):
            widget.bind("<Button-1>", lambda e, n=name: self._select_tag(n))

    def _select_tag(self, name):
        self._current_tag = name
        for child in self.tag_list.winfo_children():
            child.destroy()
        for item in self._tags:
            self._build_tag_row(item)
        self._refresh_items()

    # ==================================================================
    # 记录列表
    # ==================================================================
    def _refresh_items(self):
        tokens = self.tokens
        self._checked = set()
        self._row_widgets = {}
        for child in self.items_frame.winfo_children():
            child.destroy()
        self.select_all_var.set(False)

        if not self._current_tag:
            self.breadcrumb.configure(text="请选择左侧的标签")
            self.hint.configure(text="")
            self._set_tag_buttons(None)
            self._update_selection_state()
            return

        self.breadcrumb.configure(text="# " + self._current_tag)
        current = next((t for t in self._tags if t["name"] == self._current_tag), None)
        self._set_tag_buttons(current)

        result = self.kernel.get_tag_items(self._current_tag, limit=TAG_ITEMS_LIMIT)
        self._items = result["data"]["items"] if result["ok"] else []

        # 提示行要把"内置标签不能改名"和"共几条"一起说清楚。
        # 只留后者的话，用户看不到按钮为什么是灰的。
        notes = []
        if current is not None and current.get("protected"):
            notes.append("「%s」是内置标签，关联着敏感信息保护，不能改名或删除"
                         % current["name"])
        if not result["ok"]:
            notes.append(result["error"]["message"])
        elif result["data"]["count"]:
            notes.append("共 %d 条（最多显示 %d 条）"
                         % (result["data"]["count"], TAG_ITEMS_LIMIT))
        else:
            notes.append("这个标签下还没有记录")
        self.hint.configure(text=" · ".join(notes))

        for entry in self._items:
            self._build_item_row(entry)
        self._update_selection_state()

    def _set_tag_buttons(self, item):
        protected = bool(item and item.get("protected"))
        state = "disabled" if (item is None or protected) else "normal"
        self.rename_btn.configure(state=state)
        self.delete_tag_btn.configure(state=state)
        self.color_btn.configure(state="disabled" if item is None else "normal")

    def _build_item_row(self, entry):
        tokens = self.tokens
        entry_id = entry["id"]
        row = tk.Frame(self.items_frame, bg=tokens["surface"],
                       highlightthickness=1, highlightbackground=tokens["border"])
        row.pack(fill="x", pady=2)
        self._row_widgets[entry_id] = row

        var = tk.BooleanVar(value=False)
        check = tk.Checkbutton(
            row, variable=var, bg=tokens["surface"], activebackground=tokens["surface"],
            selectcolor=tokens["bg"], command=lambda i=entry_id, v=var: self._toggle_one(i, v))
        check.pack(side="left", padx=(6, 0))
        row._check_var = var

        text = tk.Frame(row, bg=tokens["surface"])
        text.pack(side="left", fill="x", expand=True, padx=(2, 8), pady=4)
        tk.Label(text, text=entry["typeLabel"], bg=tokens["surface"],
                 fg=tokens["text_faint"], font=self._font(8)).pack(anchor="w")
        preview = (entry.get("preview") or "").replace("\n", " ")
        tk.Label(text, text=preview[:120] or "（无预览）", bg=tokens["surface"],
                 fg=tokens["text"], font=self._font(9), anchor="w",
                 justify="left", wraplength=420).pack(anchor="w")

        actions = tk.Frame(row, bg=tokens["surface"])
        actions.pack(side="right", padx=(0, 6))
        tk.Button(actions, text="×", font=self._font(9), width=2, bd=0, relief="flat",
                  bg=tokens["surface"], fg=tokens["text_muted"],
                  activebackground=tokens["accent"], activeforeground="#FFFFFF",
                  command=lambda i=entry_id: self._remove_from_tag(i)).pack(side="right")
        tk.Button(actions, text="打开", font=self._font(8), bd=0, relief="flat",
                  bg=tokens["surface"], fg=tokens["text_muted"],
                  activebackground=tokens["accent"], activeforeground="#FFFFFF",
                  command=lambda i=entry_id: self._open_item(i)).pack(side="right", padx=2)

    # ==================================================================
    # 选择与批量操作
    # ==================================================================
    def _toggle_one(self, entry_id, var):
        if var.get():
            self._checked.add(entry_id)
        else:
            self._checked.discard(entry_id)
        self._update_selection_state()

    def _toggle_all(self):
        want = bool(self.select_all_var.get())
        for entry_id, row in self._row_widgets.items():
            var = getattr(row, "_check_var", None)
            if var is None:
                continue
            var.set(want)
            if want:
                self._checked.add(entry_id)
            else:
                self._checked.discard(entry_id)
        self._update_selection_state()

    def _update_selection_state(self):
        count = len(self._checked)
        self.sel_label.configure(text="已选中 %d 条" % count if count else "未选中")
        state = "normal" if count else "disabled"
        self.delete_btn.configure(state=state)
        self.copy_btn.configure(state=state)

    def _copy_selected(self):
        """把选中记录的内容拼起来复制（每条一行）。"""
        if not self._checked:
            return
        lines = []
        for entry in self._items:
            if entry["id"] in self._checked:
                lines.append(entry.get("content") or entry.get("preview") or "")
        text = "\n".join(line for line in lines if line)
        result = self.kernel.write_text_to_clipboard(text)
        if result["ok"]:
            self.toast.show("已复制 %d 条内容" % len(lines), TOAST_SUCCESS)
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _delete_selected(self):
        ids = sorted(self._checked)
        if not ids:
            return
        if not messagebox.askyesno(
                "删除记录",
                "确认删除选中的 %d 条记录？删除后不可恢复。\n\n"
                "（置顶与带标签的记录不会被容量上限清理，但手动删除仍会删掉。）" % len(ids),
                parent=self):
            return
        result = self.kernel.delete_entries(ids)
        if result["ok"]:
            self.toast.show("已删除 %d 条" % len(result["data"]["deleted"]), TOAST_SUCCESS)
            self._refresh_tags()
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _remove_from_tag(self, entry_id):
        result = self.kernel.remove_tag(entry_id, self._current_tag)
        if result["ok"]:
            self._refresh_tags()
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _open_item(self, entry_id):
        result = self.kernel.open_content(entry_id)
        if not result["ok"]:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    # ==================================================================
    # 标签本身的增删改
    # ==================================================================
    def _create_tag(self):
        name = (self.new_tag_var.get() or "").strip()
        if not name:
            self.toast.show("请输入标签名", TOAST_INFO)
            return
        if len(name) > TAG_NAME_MAX_LEN:
            self.toast.show("标签名不超过 %d 个字" % TAG_NAME_MAX_LEN, TOAST_ERROR)
            return
        # 标签只有挂到记录上才有意义，这里先建定义，再让用户去添加
        result = self.kernel.upsert_tag_color(name, None)
        if result["ok"]:
            self.new_tag_var.set("")
            self._current_tag = name
            self._refresh_tags()
            self.toast.show("已创建标签「%s」，可在记录上右键添加" % name,
                            TOAST_SUCCESS, 2400)
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _rename_tag(self):
        if not self._current_tag:
            return
        new_name = simpledialog.askstring(
            "重命名标签", "把「%s」改成：" % self._current_tag, parent=self)
        if not new_name:
            return
        result = self.kernel.rename_tag(self._current_tag, new_name)
        if not result["ok"]:
            self.toast.show(result["error"]["message"], TOAST_ERROR)
            return
        data = result["data"]
        self._current_tag = data["tag"]
        if data["merged"]:
            self.toast.show("已合并到「%s」，影响 %d 条记录"
                            % (data["tag"], data["affected"]), TOAST_SUCCESS, 2600)
        else:
            self.toast.show("已重命名，影响 %d 条记录" % data["affected"], TOAST_SUCCESS)
        self._refresh_tags()

    def _delete_tag(self):
        if not self._current_tag:
            return
        if not messagebox.askyesno(
                "删除标签",
                "确认删除标签「%s」？\n\n记录本身不会被删除，只是不再带这个标签。"
                % self._current_tag, parent=self):
            return
        result = self.kernel.delete_tag(self._current_tag)
        if result["ok"]:
            self.toast.show("已删除标签，影响 %d 条记录" % result["data"]["affected"],
                            TOAST_SUCCESS)
            self._current_tag = None
            self._refresh_tags()
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)

    def _pick_color(self):
        if not self._current_tag:
            return
        current = next((t for t in self._tags if t["name"] == self._current_tag), None)
        initial = (current or {}).get("color") or tag_color_for(self._current_tag, None)
        _rgb, hex_color = colorchooser.askcolor(
            color=initial, title="选择标签颜色", parent=self)
        if not hex_color:
            return
        result = self.kernel.upsert_tag_color(self._current_tag, hex_color)
        if result["ok"]:
            self._refresh_tags()
        else:
            self.toast.show(result["error"]["message"], TOAST_ERROR)
