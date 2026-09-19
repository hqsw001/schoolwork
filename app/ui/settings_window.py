# -*- coding: utf-8 -*-
"""设置窗口（UC-04 容量上限 / UC-15 外观主题 / UC-16 快捷键 / UC-17 隐私 / UC-18 配置持久化）

本期实现范围（用户已确认"只做核心链路、其余留接口占位"）：
    ✅ 存储上限与开关（含"新的上限将删除 N 条"的二次确认，UC-04 第 8 步）
    ✅ 去重开关、持久化开关、富文本与文件采集开关、粘贴后删除
    ✅ 隐私保护开关与敏感类型勾选（F9-1 / F9-2）
    ✅ 主面板快捷键设置（录制式，含"必须带修饰键"校验与冲突提示）
    ✅ 外观主题与深浅色、列表密度
    ✅ 容量统计、运行诊断
    ⏳ 顺序粘贴、贴边停靠、自定义背景：界面留位，逻辑第三周补
"""

import tkinter as tk
from tkinter import messagebox

from app.constants import (
    DENSITY_COMPACT,
    DENSITY_LOOSE,
    DENSITY_NORMAL,
    K_ALWAYS_ON_TOP,
    K_CAPTURE_FILES,
    K_CAPTURE_RICH_TEXT,
    K_DEDUPLICATE,
    K_DELETE_AFTER_PASTE,
    K_LIST_DENSITY,
    K_MAIN_HOTKEY,
    K_PERSISTENT,
    K_PERSISTENT_LIMIT,
    K_PERSISTENT_LIMIT_ENABLED,
    K_PRIVACY_KINDS,
    K_PRIVACY_PROTECTION,
    K_SEARCH_HOTKEY,
    K_THEME,
    K_THEME_MODE,
    MIN_STORAGE_LIMIT,
    MODE_DARK,
    MODE_LIGHT,
    MODE_SYSTEM,
    TOAST_ERROR,
    TOAST_INFO,
    TOAST_SUCCESS,
)
from app.infrastructure.clipboard import listener as listener_mod
from app.services.privacy import ALL_KINDS
from app.ui import theme as theme_mod

LIMIT_PRESETS = (100, 200, 500, 1000, 2000)

DENSITY_LABELS = [
    (DENSITY_COMPACT, "紧凑"),
    (DENSITY_NORMAL, "标准"),
    (DENSITY_LOOSE, "宽松"),
]

MODE_LABELS = [
    (MODE_LIGHT, "浅色"),
    (MODE_DARK, "深色"),
    (MODE_SYSTEM, "跟随系统"),
]


class SettingsWindow(tk.Toplevel):
    """设置面板。"""

    def __init__(self, panel):
        super().__init__(panel.window)
        self.panel = panel
        self.kernel = panel.kernel
        self.tokens = panel._tokens
        self.title("剪贴板Pro 设置")
        self.configure(bg=self.tokens["bg"])
        self.resizable(False, True)
        self.transient(panel.window)

        self._build()
        self._center(panel.window)
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.bind("<Escape>", lambda e: self.destroy())

    # ==================================================================
    def _build(self):
        tokens = self.tokens
        container = tk.Frame(self, bg=tokens["bg"])
        container.pack(fill="both", expand=True, padx=16, pady=14)

        tk.Label(container, text="设置", bg=tokens["bg"], fg=tokens["text"],
                 font=(tokens["font_family"], 14, "bold")).pack(anchor="w")
        tk.Label(container, text="全部设置仅保存在本机数据库，程序不联网、不上传任何内容。",
                 bg=tokens["bg"], fg=tokens["text_faint"],
                 font=(tokens["font_family"], 9)).pack(anchor="w", pady=(2, 10))

        self._build_storage(container)
        self._build_capture(container)
        self._build_privacy(container)
        self._build_hotkeys(container)
        self._build_appearance(container)
        self._build_window_behavior(container)
        self._build_footer(container)

    # ---------------------------------------------------------------- 数据
    def _build_storage(self, parent):
        section = self._section(parent, "数据与容量")
        usage = self.kernel.get_storage_usage()
        data = usage["data"] if usage["ok"] else {}

        self.limit_enabled = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_PERSISTENT_LIMIT_ENABLED)))
        self._check(section, "启用存储上限（超出后自动清理最旧的普通记录）",
                    self.limit_enabled, command=self._on_limit_toggle)

        row = tk.Frame(section, bg=self.tokens["bg"])
        row.pack(fill="x", pady=3)
        tk.Label(row, text="上限条数", bg=self.tokens["bg"], fg=self.tokens["text"],
                 font=self._font()).pack(side="left")
        self.limit_var = tk.StringVar(value=str(self.kernel.settings.get(K_PERSISTENT_LIMIT)))
        entry = tk.Entry(row, textvariable=self.limit_var, width=8, justify="right",
                         font=self._font(), bg=self.tokens["surface"],
                         fg=self.tokens["text"], bd=1, relief="solid")
        entry.pack(side="left", padx=(8, 8))
        for preset in LIMIT_PRESETS:
            tk.Button(row, text=str(preset), font=self._font(8),
                      command=lambda p=preset: self.limit_var.set(str(p)),
                      padx=6, pady=0).pack(side="left", padx=1)
        tk.Label(row, text="（不小于 %d 条）" % MIN_STORAGE_LIMIT,
                 bg=self.tokens["bg"], fg=self.tokens["text_faint"],
                 font=self._font(8)).pack(side="left", padx=(6, 0))

        tk.Button(section, text="保存上限", command=self._save_limit,
                  font=self._font(9)).pack(anchor="w", pady=(4, 0))

        stats = ("当前共 %d 条，其中受保护（置顶 / 已打标签）%d 条，计入上限 %d 条；"
                 "图片附件 %d 个，占用 %s"
                 % (data.get("totalCount", 0), data.get("protectedCount", 0),
                    data.get("countable", 0), data.get("attachmentCount", 0),
                    data.get("attachmentHuman", "0 B")))
        tk.Label(section, text=stats, bg=self.tokens["bg"], fg=self.tokens["text_muted"],
                 font=self._font(8), wraplength=430, justify="left").pack(anchor="w", pady=(6, 0))
        if data.get("warning"):
            tk.Label(section, text="⚠ " + data["warning"], bg=self.tokens["bg"],
                     fg=self.tokens["pin"], font=self._font(8), wraplength=430,
                     justify="left").pack(anchor="w", pady=(2, 0))

        self.persistent_var = tk.BooleanVar(value=bool(self.kernel.settings.get(K_PERSISTENT)))
        self._check(section, "持久化保存历史（关闭后仅本次运行内保留，退出即清空）",
                    self.persistent_var,
                    command=lambda: self._set(K_PERSISTENT, self.persistent_var.get()))

        self.delete_after_paste_var = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_DELETE_AFTER_PASTE)))
        self._check(section, "粘贴后删除该条记录",
                    self.delete_after_paste_var,
                    command=lambda: self._set(K_DELETE_AFTER_PASTE,
                                              self.delete_after_paste_var.get()))

    # ---------------------------------------------------------------- 采集
    def _build_capture(self, parent):
        section = self._section(parent, "采集与去重")
        self.dedup_var = tk.BooleanVar(value=bool(self.kernel.settings.get(K_DEDUPLICATE)))
        self._check(section, "重复内容合并（相同内容只留一条，刷新时间与次数并浮到顶部）",
                    self.dedup_var,
                    command=lambda: self._set(K_DEDUPLICATE, self.dedup_var.get()))

        self.rich_var = tk.BooleanVar(value=bool(self.kernel.settings.get(K_CAPTURE_RICH_TEXT)))
        self._check(section, "采集富文本（保留 HTML 原文，粘贴时可选择格式）",
                    self.rich_var,
                    command=lambda: self._set(K_CAPTURE_RICH_TEXT, self.rich_var.get()))

        self.files_var = tk.BooleanVar(value=bool(self.kernel.settings.get(K_CAPTURE_FILES)))
        self._check(section, "采集文件与目录路径",
                    self.files_var,
                    command=lambda: self._set(K_CAPTURE_FILES, self.files_var.get()))

    # ---------------------------------------------------------------- 隐私
    def _build_privacy(self, parent):
        section = self._section(parent, "隐私与安全")
        self.privacy_var = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_PRIVACY_PROTECTION)))
        self._check(section, "识别敏感信息并在列表中脱敏显示（点开仍可看原文）",
                    self.privacy_var, command=self._on_privacy_toggle)

        kinds_row = tk.Frame(section, bg=self.tokens["bg"])
        kinds_row.pack(fill="x", pady=(2, 0))
        current_kinds = self.kernel.settings.get(K_PRIVACY_KINDS) or []
        self.kind_vars = {}
        for kind in ALL_KINDS:
            var = tk.BooleanVar(value=kind in current_kinds)
            self.kind_vars[kind] = var
            tk.Checkbutton(
                kinds_row, text=kind, variable=var, font=self._font(9),
                bg=self.tokens["bg"], fg=self.tokens["text"],
                activebackground=self.tokens["bg"], selectcolor=self.tokens["surface"],
                command=self._on_kinds_changed,
            ).pack(side="left", padx=(0, 10))

        # 如实说明本期边界（UC-17 异常流 E2 的定位：降低风险而不是保证拦截）
        tk.Label(
            section,
            text="说明：本期实现「识别 + 脱敏显示」，敏感记录的加密落盘安排在第三周；"
                 "当前敏感内容在数据库中仍是明文，请勿在共用电脑上存放密钥。",
            bg=self.tokens["bg"], fg=self.tokens["pin"], font=self._font(8),
            wraplength=430, justify="left",
        ).pack(anchor="w", pady=(4, 0))

    # ---------------------------------------------------------------- 快捷键
    def _build_hotkeys(self, parent):
        section = self._section(parent, "快捷键")
        status = self.kernel.get_listen_status()
        data = status["data"] if status["ok"] else {}
        registered = data.get("registered") or {}
        substituted = data.get("substituted") or {}

        self.hotkey_labels = {}
        for action, label, key in (
            (listener_mod.ACTION_MAIN_PANEL, "唤起主面板", K_MAIN_HOTKEY),
            (listener_mod.ACTION_SEARCH, "唤起并聚焦搜索", K_SEARCH_HOTKEY),
        ):
            row = tk.Frame(section, bg=self.tokens["bg"])
            row.pack(fill="x", pady=2)
            tk.Label(row, text=label, width=14, anchor="w", bg=self.tokens["bg"],
                     fg=self.tokens["text"], font=self._font()).pack(side="left")
            # 显示**实际生效**的键，而不是配置里的值：
            # 配置的键若被别的程序占用，程序启动时会自动降级成备选组合，
            # 这里继续显示配置值会让人按了没反应却找不到原因
            effective = registered.get(action) or self.kernel.settings.get(key)
            value = tk.Label(row, text=effective, width=16,
                             anchor="w", bg=self.tokens["surface"], fg=self.tokens["accent"],
                             font=self._font(9), padx=6, pady=3, cursor="hand2")
            value.pack(side="left")
            value.bind("<Button-1>", lambda e, a=action, k=key, l=value:
                       self._record_hotkey(a, k, l))
            self.hotkey_labels[action] = value
            tk.Label(row, text="点击后按下新组合键", bg=self.tokens["bg"],
                     fg=self.tokens["text_faint"], font=self._font(8)).pack(side="left", padx=8)
            if action in substituted:
                tk.Label(
                    row, text="（%s 被占用，已自动改用此键）" % substituted[action].get("configured"),
                    bg=self.tokens["bg"], fg=self.tokens["pin"], font=self._font(8),
                ).pack(side="left")

        tk.Button(section, text="重新注册快捷键", font=self._font(9),
                  command=self._reapply_hotkeys).pack(anchor="w", pady=(6, 0))
        tk.Label(section,
                 text="快捷键必须包含 Ctrl / Alt / Shift / Win 中的至少一个。\n"
                      "Ctrl+Shift+V / Ctrl+Shift+F 常被输入法、截图工具、浏览器扩展占用；"
                      "被占用时程序会自动换用备选组合并提示，也可以在这里手动指定。",
                 bg=self.tokens["bg"], fg=self.tokens["text_faint"], font=self._font(8),
                 wraplength=430, justify="left").pack(anchor="w", pady=(3, 0))

    # ---------------------------------------------------------------- 外观
    def _build_appearance(self, parent):
        section = self._section(parent, "外观")
        row = tk.Frame(section, bg=self.tokens["bg"])
        row.pack(fill="x", pady=2)
        tk.Label(row, text="主题", bg=self.tokens["bg"], fg=self.tokens["text"],
                 font=self._font()).pack(side="left", padx=(0, 8))
        self.theme_var = tk.StringVar(value=self.kernel.settings.get(K_THEME))
        for item in theme_mod.list_themes():
            tk.Radiobutton(
                row, text=item["label"], value=item["id"], variable=self.theme_var,
                font=self._font(9), bg=self.tokens["bg"], fg=self.tokens["text"],
                activebackground=self.tokens["bg"], selectcolor=self.tokens["surface"],
                command=self._on_theme_change,
            ).pack(side="left", padx=(0, 6))

        row2 = tk.Frame(section, bg=self.tokens["bg"])
        row2.pack(fill="x", pady=2)
        tk.Label(row2, text="深浅色", bg=self.tokens["bg"], fg=self.tokens["text"],
                 font=self._font()).pack(side="left", padx=(0, 8))
        self.mode_var = tk.StringVar(value=self.kernel.settings.get(K_THEME_MODE))
        for value, label in MODE_LABELS:
            tk.Radiobutton(
                row2, text=label, value=value, variable=self.mode_var,
                font=self._font(9), bg=self.tokens["bg"], fg=self.tokens["text"],
                activebackground=self.tokens["bg"], selectcolor=self.tokens["surface"],
                command=self._on_mode_change,
            ).pack(side="left", padx=(0, 10))

        row3 = tk.Frame(section, bg=self.tokens["bg"])
        row3.pack(fill="x", pady=2)
        tk.Label(row3, text="列表密度", bg=self.tokens["bg"], fg=self.tokens["text"],
                 font=self._font()).pack(side="left", padx=(0, 8))
        self.density_var = tk.StringVar(value=self.kernel.settings.get(K_LIST_DENSITY))
        for value, label in DENSITY_LABELS:
            tk.Radiobutton(
                row3, text=label, value=value, variable=self.density_var,
                font=self._font(9), bg=self.tokens["bg"], fg=self.tokens["text"],
                activebackground=self.tokens["bg"], selectcolor=self.tokens["surface"],
                command=self._on_density_change,
            ).pack(side="left", padx=(0, 10))

    # ---------------------------------------------------------------- 窗口行为
    def _build_window_behavior(self, parent):
        section = self._section(parent, "窗口行为")
        self.topmost_var = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_ALWAYS_ON_TOP)))
        self._check(section, "面板总在最前（关闭后会被其他窗口盖住，适合边看资料边翻剪贴板）",
                    self.topmost_var, command=self._on_topmost_change)
        tk.Label(
            section,
            text="面板是标准 Windows 窗口：拖标题栏移动、右上角可最小化。\n"
                 "关闭按钮与 Esc 都只是“收起面板”，程序仍在托盘运行，"
                 "全局快捷键随时可以再唤出来。",
            bg=self.tokens["bg"], fg=self.tokens["text_faint"], font=self._font(8),
            wraplength=430, justify="left",
        ).pack(anchor="w", pady=(4, 0))

    # ---------------------------------------------------------------- 底部
    def _build_footer(self, parent):
        tokens = self.tokens
        diag = self.kernel.get_diagnostics()
        data = diag["data"] if diag["ok"] else {}
        listener = data.get("listener") or {}
        info = ("版本 %s · 监听：%s · 数据库结构：v%s\n数据目录：%s"
                % (data.get("version", "-"),
                   "运行中" if listener.get("running") else "未运行",
                   data.get("schemaVersion", "-"), data.get("dataDir", "-")))
        tk.Label(parent, text=info, bg=tokens["bg"], fg=tokens["text_faint"],
                 font=self._font(8), justify="left", wraplength=460).pack(anchor="w",
                                                                          pady=(12, 0))

        buttons = tk.Frame(parent, bg=tokens["bg"])
        buttons.pack(fill="x", pady=(10, 0))
        tk.Button(buttons, text="清空历史…", font=self._font(9),
                  command=self._clear_history).pack(side="left")
        tk.Button(buttons, text="整理数据库", font=self._font(9),
                  command=self._vacuum).pack(side="left", padx=6)
        tk.Button(buttons, text="恢复默认设置", font=self._font(9),
                  command=self._restore_defaults).pack(side="left")
        tk.Button(buttons, text="关闭", font=self._font(9), width=10,
                  command=self.destroy).pack(side="right")

    # ==================================================================
    # 控件工厂
    # ==================================================================
    def _font(self, size=10):
        return (self.tokens["font_family"], size)

    def _section(self, parent, title):
        tokens = self.tokens
        wrapper = tk.Frame(parent, bg=tokens["bg"])
        wrapper.pack(fill="x", pady=(6, 4))
        tk.Label(wrapper, text=title, bg=tokens["bg"], fg=tokens["accent"],
                 font=(tokens["font_family"], 10, "bold")).pack(anchor="w")
        frame = tk.Frame(wrapper, bg=tokens["bg"])
        frame.pack(fill="x", padx=(4, 0), pady=(3, 0))
        return frame

    def _check(self, parent, text, variable, command=None):
        return tk.Checkbutton(
            parent, text=text, variable=variable, command=command,
            font=self._font(9), bg=self.tokens["bg"], fg=self.tokens["text"],
            activebackground=self.tokens["bg"], selectcolor=self.tokens["surface"],
            anchor="w", justify="left", wraplength=430,
        ).pack(anchor="w", pady=1)

    def _center(self, parent):
        self.update_idletasks()
        width = max(500, self.winfo_reqwidth())
        height = min(760, self.winfo_reqheight())
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + 20
        x = max(0, x)
        y = max(0, min(y, self.winfo_screenheight() - height - 20))
        self.geometry("%dx%d+%d+%d" % (width, height, x, y))

    # ==================================================================
    # 动作
    # ==================================================================
    def _toast(self, message, level=TOAST_INFO):
        try:
            self.panel.toast.show(message, level)
        except Exception:  # noqa: BLE001
            pass

    def _set(self, key, value):
        result = self.kernel.update_setting(key, value)
        if not result["ok"]:
            self._toast(result["error"]["message"], TOAST_ERROR)
        return result

    def _on_limit_toggle(self):
        self._set(K_PERSISTENT_LIMIT_ENABLED, self.limit_enabled.get())

    def _save_limit(self):
        raw = self.limit_var.get().strip()
        try:
            limit = int(raw)
        except ValueError:
            messagebox.showwarning("输入非法", "上限必须是整数，且不小于 %d。" % MIN_STORAGE_LIMIT,
                                   parent=self)
            return
        if limit < MIN_STORAGE_LIMIT:
            messagebox.showwarning("输入非法",
                                   "上限不能小于 %d（防止历史形同虚设）。" % MIN_STORAGE_LIMIT,
                                   parent=self)
            return
        enabled = self.limit_enabled.get()
        # UC-04 主事件流第 8 步：会删除数据时必须二次确认
        preview = self.kernel.preview_storage_limit(limit, enabled)
        pending = preview["data"]["pendingDelete"] if preview["ok"] else 0
        if enabled and pending > 0:
            if not messagebox.askyesno(
                "确认调整上限",
                "新的上限将删除 %d 条较早的记录，其中不包含置顶与已打标签的记录。\n\n"
                "删除后不可恢复，是否继续？" % pending,
                parent=self,
            ):
                return
        result = self.kernel.set_storage_limit(limit, enabled)
        if result["ok"]:
            self.limit_var.set(str(limit))
            self._toast("上限已设为 %d 条%s"
                        % (limit, "，本次删除 %d 条" % result["data"]["deleted"]
                           if result["data"]["deleted"] else ""), TOAST_SUCCESS)
            self.panel.refresh(reset_scroll=True)
        else:
            self._toast(result["error"]["message"], TOAST_ERROR)

    def _on_privacy_toggle(self):
        self._set(K_PRIVACY_PROTECTION, self.privacy_var.get())

    def _on_kinds_changed(self):
        kinds = [kind for kind, var in self.kind_vars.items() if var.get()]
        self._set(K_PRIVACY_KINDS, kinds)

    # ---------------------------------------------------------------- 快捷键录制
    def _record_hotkey(self, action, key, label):
        label.configure(text="请按新组合键…", fg=self.tokens["pin"])
        recorder = _HotkeyRecorder(self, self.tokens)

        def finish(accelerator):
            label.configure(text=self.kernel.settings.get(key), fg=self.tokens["accent"])
            if not accelerator:
                return
            result = self.kernel.set_hotkey(action, accelerator)
            if result["ok"]:
                label.configure(text=result["data"]["accelerator"])
                self._toast("快捷键已设为 %s" % result["data"]["accelerator"], TOAST_SUCCESS)
            else:
                self._toast(result["error"]["message"], TOAST_ERROR)

        recorder.capture(finish)

    def _reapply_hotkeys(self):
        result = self.kernel.reapply_hotkeys()
        if result["ok"]:
            self._toast("已按配置重新注册快捷键", TOAST_SUCCESS)
        else:
            self._toast(result["error"]["message"], TOAST_ERROR)

    # ---------------------------------------------------------------- 外观
    def _on_theme_change(self):
        result = self.kernel.set_theme(self.theme_var.get())
        if result["ok"]:
            self.panel.apply_theme()

    def _on_mode_change(self):
        result = self.kernel.set_theme_mode(self.mode_var.get())
        if result["ok"]:
            self.panel.apply_theme()

    def _on_density_change(self):
        result = self.kernel.set_list_density(self.density_var.get())
        if result["ok"]:
            self.panel.apply_theme()

    def _on_topmost_change(self):
        result = self.kernel.update_setting(K_ALWAYS_ON_TOP, self.topmost_var.get())
        if not result["ok"]:
            self._toast(result["error"]["message"], TOAST_ERROR)
            return
        try:
            self.panel.window.attributes("-topmost", self.topmost_var.get())
        except Exception:  # noqa: BLE001
            pass
        self._toast("面板已%s总在最前" % ("" if self.topmost_var.get() else "取消"), TOAST_SUCCESS)

    # ---------------------------------------------------------------- 维护
    def _clear_history(self):
        self.panel.clear_history()
        self.destroy()

    def _vacuum(self):
        result = self.kernel.vacuum_database()
        if result["ok"]:
            self._toast("数据库已整理", TOAST_SUCCESS)
        else:
            self._toast(result["error"]["message"], TOAST_ERROR)

    def _restore_defaults(self):
        if not messagebox.askyesno(
            "恢复默认设置",
            "将把所有设置恢复为默认值（不影响已保存的历史记录）。是否继续？",
            parent=self,
        ):
            return
        self.kernel.settings.restore_defaults()
        self.kernel.privacy.configure(
            enabled=self.kernel.settings.get(K_PRIVACY_PROTECTION),
            kinds=self.kernel.settings.get(K_PRIVACY_KINDS),
        )
        self.panel.apply_theme()
        self._toast("设置已恢复默认", TOAST_SUCCESS)


class _HotkeyRecorder(object):
    """快捷键录制器：捕获一次按键组合并转换成加速器字符串。

    校验规则（UC-16 主事件流第 5 步）：必须包含修饰键 + 必须有一个非修饰键。

    同样是**回调式**而不是 ``wait_window`` 阻塞式：本类可能在窗口尚未映射
    时被调用，阻塞式会死锁。
    """

    MODIFIER_KEYSYMS = {"Control_L", "Control_R", "Alt_L", "Alt_R",
                        "Shift_L", "Shift_R", "Super_L", "Super_R", "Win_L", "Win_R"}

    def __init__(self, parent, tokens):
        self.parent = parent
        self.tokens = tokens

    def capture(self, callback):
        window = tk.Toplevel(self.parent)
        window.title("录制快捷键")
        window.configure(bg=self.tokens["bg"])
        window.transient(self.parent)
        window.resizable(False, False)
        tk.Label(window, text="请按下新的组合键\n（必须包含 Ctrl / Alt / Shift / Win，Esc 取消）",
                 bg=self.tokens["bg"], fg=self.tokens["text"],
                 font=(self.tokens["font_family"], 10), justify="center",
                 padx=28, pady=20).pack()
        window.focus_force()

        def finish(value):
            try:
                window.destroy()
            except tk.TclError:
                pass
            callback(value)

        def on_key(event):
            keysym = event.keysym
            if keysym == "Escape":
                finish(None)
                return "break"
            if keysym in self.MODIFIER_KEYSYMS:
                return "break"
            # 有些布局下 Mod4 (Win) 会落到 state 位；Tk 在 Windows 上通常
            # 拿不到，所以这里以 state 位为准，取不到就不认 Win
            parts = []
            if event.state & 0x0004:
                parts.append("Ctrl")
            if event.state & 0x0008:      # Mod1 = Alt
                parts.append("Alt")
            if event.state & 0x0001:
                parts.append("Shift")
            if keysym.startswith("Super") or keysym.startswith("Win"):
                parts.append("Win")
            if not parts:
                # 只按了普通键：提示必须带修饰键，不关闭窗口，让用户重按
                try:
                    window.bell()
                except tk.TclError:
                    pass
                return "break"
            key = _keysym_to_key(keysym)
            if key is None:
                return "break"
            parts.append(key)
            finish("+".join(parts))
            return "break"

        window.bind("<KeyPress>", on_key)


def _keysym_to_key(keysym):
    """Tk keysym -> 加速器里的主键名。"""
    if len(keysym) == 1 and keysym.isalnum():
        return keysym.upper()
    mapping = {
        "Return": "Enter", "KP_Enter": "Enter", "space": "Space",
        "Delete": "Delete", "Insert": "Insert", "Home": "Home", "End": "End",
        "Prior": "PageUp", "Next": "PageDown",
        "Up": "Up", "Down": "Down", "Left": "Left", "Right": "Right",
    }
    if keysym in mapping:
        return mapping[keysym]
    if keysym.startswith("F") and keysym[1:].isdigit():
        return keysym
    return None
