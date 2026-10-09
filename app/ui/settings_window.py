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

import json
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
    K_ALWAYS_ON_TOP,
    K_ARROW_KEY_SELECTION,
    K_EDGE_DOCKING,
    K_FOLLOW_MOUSE,
    K_IGNORE_BLUR,
    K_MASK_EMAIL_DOMAIN,
    K_MASK_PREFIX,
    K_MASK_SUFFIX,
    K_PREVIEW_ENABLED,
    K_PRIVACY_CUSTOM_RULES,
    K_PRIVACY_KINDS,
    K_PRIVACY_PROTECTION,
    K_RICH_PASTE_HOTKEY,
    K_SEARCH_HOTKEY,
    K_SEQUENTIAL_HOTKEY,
    K_SEQUENTIAL_MODE,
    K_THEME,
    K_THEME_MODE,
    K_TRAY_VISIBLE,
    K_WINDOW_PINNED,
    MIN_STORAGE_LIMIT,
    MODE_DARK,
    MODE_LIGHT,
    MODE_SYSTEM,
    TOAST_ERROR,
    TOAST_INFO,
    TOAST_SUCCESS,
)
from app.infrastructure.clipboard import listener as listener_mod
from app.services import app_rules as app_rules_mod
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
        self._build_app_rules(container)
        self._build_hotkeys(container)
        self._build_sequential(container)
        self._build_appearance(container)
        self._build_window_behavior(container)
        self._build_system(container)
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

        # 掩码位数
        mask_row = tk.Frame(section, bg=self.tokens["bg"])
        mask_row.pack(fill="x", pady=(6, 0))
        tk.Label(mask_row, text="脱敏时前后保留可见位数：", bg=self.tokens["bg"],
                 fg=self.tokens["text_muted"], font=self._font(9)).pack(side="left")
        self.mask_prefix_var = tk.StringVar(
            value=str(self.kernel.settings.get(K_MASK_PREFIX)))
        self.mask_suffix_var = tk.StringVar(
            value=str(self.kernel.settings.get(K_MASK_SUFFIX)))
        for label, var in (("前", self.mask_prefix_var), ("后", self.mask_suffix_var)):
            tk.Label(mask_row, text=label, bg=self.tokens["bg"],
                     fg=self.tokens["text_muted"], font=self._font(9)).pack(side="left")
            spin = tk.Spinbox(mask_row, from_=0, to=20, width=3, textvariable=var,
                              font=self._font(9), bg=self.tokens["surface"],
                              fg=self.tokens["text"], buttonbackground=self.tokens["surface"],
                              command=self._on_mask_changed)
            spin.pack(side="left", padx=(2, 8))
            spin.bind("<FocusOut>", lambda e: self._on_mask_changed())
            spin.bind("<Return>", lambda e: self._on_mask_changed())

        self.mask_email_domain_var = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_MASK_EMAIL_DOMAIN)))
        self._check(section, "邮箱脱敏时连域名一起打码（默认只打码用户名）",
                    self.mask_email_domain_var, command=self._on_mask_changed)

        # 自定义规则
        tk.Label(section, text="自定义识别规则（每行一条正则，# 开头为注释）：",
                 bg=self.tokens["bg"], fg=self.tokens["text_muted"],
                 font=self._font(9)).pack(anchor="w", pady=(6, 2))
        self.custom_rules_text = tk.Text(
            section, height=3, font=("Consolas", 9), bg=self.tokens["surface"],
            fg=self.tokens["text"], insertbackground=self.tokens["text"],
            relief="solid", bd=1, wrap="none")
        self.custom_rules_text.pack(fill="x")
        self.custom_rules_text.insert(
            "1.0", self.kernel.settings.get(K_PRIVACY_CUSTOM_RULES) or "")
        self.custom_rules_text.bind("<FocusOut>", lambda e: self._on_custom_rules_changed())

        # 加密状态
        self.encryption_label = tk.Label(
            section, text="", bg=self.tokens["bg"], fg=self.tokens["text_faint"],
            font=self._font(8), wraplength=430, justify="left")
        self.encryption_label.pack(anchor="w", pady=(6, 0))
        self.realign_btn = tk.Button(section, text="立即对齐加密", font=self._font(9),
                                     command=self._on_realign)
        self.realign_btn.pack(anchor="w", pady=(4, 0))
        self._refresh_encryption_status()

    def _refresh_encryption_status(self):
        status = self.kernel.get_encryption_status()
        if not status["ok"]:
            self.encryption_label.configure(text="加密状态读取失败")
            return
        data = status["data"]
        if not data["available"]:
            self.encryption_label.configure(
                text="当前平台不支持 DPAPI，敏感记录不会被加密。", fg=self.tokens["pin"])
            self.realign_btn.configure(state="disabled")
            return
        if data["aligned"]:
            text = ("✅ 敏感记录已用 %s 加密落盘：%d 条文本记录全部对齐。\n"
                    "手机号/身份证这类内容在数据库里是密文，换 Windows 用户也解不开。"
                    % (data["algorithm"], data["scanned"]))
        else:
            text = ("⚠ 有 %d 条敏感记录还是明文、%d 条标记与实际不符，点下面的按钮对齐。"
                    % (data["missingEncryption"], data["staleEncryption"]))
        self.encryption_label.configure(text=text, fg=self.tokens["text_faint"])
        self.realign_btn.configure(
            state="disabled" if data["aligned"] else "normal")

    def _on_realign(self):
        result = self.kernel.realign_encryption()
        if result["ok"]:
            self._toast("已加密 %d 条历史敏感记录" % result["data"]["fixed"],
                        TOAST_SUCCESS)
        else:
            self._toast(result["error"]["message"], TOAST_ERROR)
        self._refresh_encryption_status()

    def _on_mask_changed(self):
        self._set(K_MASK_PREFIX, self.mask_prefix_var.get())
        self._set(K_MASK_SUFFIX, self.mask_suffix_var.get())
        self._set(K_MASK_EMAIL_DOMAIN, self.mask_email_domain_var.get())

    def _on_custom_rules_changed(self):
        self._set(K_PRIVACY_CUSTOM_RULES,
                  self.custom_rules_text.get("1.0", "end").strip())

    # ---------------------------------------------------------------- 应用规则
    def _build_app_rules(self, parent):
        section = self._section(parent, "应用规则")
        tk.Label(
            section,
            text="按来源应用「忽略采集」或「自动清洗」。典型用法："
                 "密码管理器不入库；从 IDE 复制代码时自动去掉行号。",
            bg=self.tokens["bg"], fg=self.tokens["text_muted"], font=self._font(8),
            wraplength=430, justify="left",
        ).pack(anchor="w")

        self.app_rules_frame = tk.Frame(section, bg=self.tokens["bg"])
        self.app_rules_frame.pack(fill="x", pady=(4, 0))
        tk.Button(section, text="+ 添加规则…", font=self._font(9),
                  command=lambda: self._edit_policy(None)).pack(anchor="w", pady=(4, 0))
        self._refresh_app_rules()

        # 全局清洗规则（此前只能改数据库，没有界面）
        tk.Label(section, text="全局清洗规则（每行一条「正则 => 替换」，# 为注释）：",
                 bg=self.tokens["bg"], fg=self.tokens["text_muted"],
                 font=self._font(9)).pack(anchor="w", pady=(8, 2))
        self.global_rules_text = tk.Text(
            section, height=3, font=("Consolas", 9), bg=self.tokens["surface"],
            fg=self.tokens["text"], insertbackground=self.tokens["text"],
            relief="solid", bd=1, wrap="none")
        self.global_rules_text.pack(fill="x")
        self.global_rules_text.insert(
            "1.0", self.kernel.get_global_cleanup_rules()["data"]["raw"])
        self.global_rules_text.bind(
            "<FocusOut>", lambda e: self._on_global_rules_changed())
        self.global_rules_hint = tk.Label(
            section, text="", bg=self.tokens["bg"], fg=self.tokens["text_faint"],
            font=self._font(8), anchor="w")
        self.global_rules_hint.pack(fill="x")

    def _refresh_app_rules(self):
        for child in self.app_rules_frame.winfo_children():
            child.destroy()
        result = self.kernel.get_app_cleanup_policies()
        policies = result["data"]["items"] if result["ok"] else []
        if not policies:
            tk.Label(self.app_rules_frame, text="（还没有规则）", bg=self.tokens["bg"],
                     fg=self.tokens["text_faint"], font=self._font(8)).pack(anchor="w")
            return
        for index, policy in enumerate(policies):
            row = tk.Frame(self.app_rules_frame, bg=self.tokens["bg"])
            row.pack(fill="x", pady=1)
            enabled = tk.BooleanVar(value=bool(policy.get("enabled", True)))
            tk.Checkbutton(
                row, variable=enabled, bg=self.tokens["bg"],
                activebackground=self.tokens["bg"], selectcolor=self.tokens["surface"],
                command=lambda i=index, v=enabled: self._toggle_policy(i, v),
            ).pack(side="left")
            color = self.tokens["text_muted"] if policy.get("enabled", True) \
                else self.tokens["text_faint"]
            tk.Label(row, text=policy.get("summary") or "（未指定）", bg=self.tokens["bg"],
                     fg=color, font=self._font(9), anchor="w").pack(
                side="left", fill="x", expand=True)
            tk.Button(row, text="删除", font=self._font(8), bd=0, relief="flat",
                      bg=self.tokens["bg"], fg=self.tokens["text_muted"],
                      activebackground=self.tokens["surface"],
                      command=lambda i=index: self._delete_policy(i)).pack(side="right")
            tk.Button(row, text="编辑", font=self._font(8), bd=0, relief="flat",
                      bg=self.tokens["bg"], fg=self.tokens["text_muted"],
                      activebackground=self.tokens["surface"],
                      command=lambda i=index: self._edit_policy(i)).pack(side="right")

    def _save_policies(self, policies):
        result = self.kernel.set_app_cleanup_policies(policies)
        if result["ok"]:
            self._refresh_app_rules()
        else:
            self._toast(result["error"]["message"], TOAST_ERROR)
        return result["ok"]

    def _toggle_policy(self, index, var):
        policies = self.kernel.get_app_cleanup_policies()["data"]["items"]
        for policy in policies:
            policy.pop("summary", None)
        if 0 <= index < len(policies):
            policies[index]["enabled"] = bool(var.get())
            self._save_policies(policies)

    def _delete_policy(self, index):
        result = self.kernel.delete_app_cleanup_policy(index)
        if result["ok"]:
            self._refresh_app_rules()
            self._toast("已删除该规则", TOAST_SUCCESS, 1400)
        else:
            self._toast(result["error"]["message"], TOAST_ERROR)

    def _edit_policy(self, index):
        policies = self.kernel.get_app_cleanup_policies()["data"]["items"]
        for policy in policies:
            policy.pop("summary", None)
        current = dict(policies[index]) if (index is not None
                                            and 0 <= index < len(policies)) else {}
        dialog = PolicyEditor(self, current)
        self.wait_window(dialog)
        if not dialog.result:
            return
        if index is None:
            policies.append(dialog.result)
        else:
            policies[index] = dialog.result
        if self._save_policies(policies):
            self._toast("规则已保存", TOAST_SUCCESS, 1400)

    def _on_global_rules_changed(self):
        raw = self.global_rules_text.get("1.0", "end").strip()
        result = self.kernel.set_cleanup_rules(raw)
        if result["ok"]:
            self.global_rules_hint.configure(
                text="已保存，%d 条规则生效" % result["data"]["count"])
        else:
            self.global_rules_hint.configure(text=result["error"]["message"])

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
            (listener_mod.ACTION_RICH_PASTE, "带格式粘贴", K_RICH_PASTE_HOTKEY),
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
                      "被占用时程序会自动换用备选组合并提示，也可以在这里手动指定。\n"
                      "⚠ 带格式粘贴默认是 Ctrl+Shift+Z —— 这个组合在 Photoshop / "
                      "VS Code 里是“重做”，全局注册会把它抢走；用得上重做的话"
                      "建议改成别的组合。",
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

        # 批次 11 补上的四项
        self.edge_docking_var = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_EDGE_DOCKING)))
        self._check(section, "贴边停靠（拖到屏幕边缘自动吸附，只留一条窄边，鼠标碰到就展开）",
                    self.edge_docking_var,
                    command=lambda: self._set(K_EDGE_DOCKING, self.edge_docking_var.get()))

        self.follow_mouse_var = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_FOLLOW_MOUSE)))
        self._check(section, "在鼠标位置弹出（而不是固定在屏幕上方）",
                    self.follow_mouse_var,
                    command=lambda: self._set(K_FOLLOW_MOUSE, self.follow_mouse_var.get()))

        self.ignore_blur_var = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_IGNORE_BLUR)))
        self._check(section, "失焦时不自动隐藏（点别处面板也留着）",
                    self.ignore_blur_var,
                    command=lambda: self._set(K_IGNORE_BLUR, self.ignore_blur_var.get()))

        self.window_pinned_var = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_WINDOW_PINNED)))
        self._check(section, "窗口固定（始终置顶、失焦也不隐藏；等价于“我要它一直在”）",
                    self.window_pinned_var,
                    command=self._on_pinned_change)

        self.arrow_keys_var = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_ARROW_KEY_SELECTION)))
        self._check(section, "方向键参与列表选择（关闭后上下键不再移动选中项）",
                    self.arrow_keys_var, command=self._on_arrow_keys_change)

        preview_enabled, preview_delay = self._build_preview_options(section)

        self.tray_visible_var = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_TRAY_VISIBLE)))
        self._check(section, "显示托盘图标（关掉之后只剩全局快捷键这一个入口）",
                    self.tray_visible_var,
                    command=lambda: self._set(K_TRAY_VISIBLE, self.tray_visible_var.get()))

        # 显示器信息：让用户知道程序看到几块屏（多屏问题多半是这里对不上）
        behavior = self.kernel.get_window_behavior()["data"]
        tk.Label(
            section,
            text="检测到 %d 块显示器：%s" % (
                behavior["monitorCount"],
                "、".join("%d×%d" % (m["width"], m["height"])
                          for m in behavior["monitors"]) or "（读取失败，将按主屏处理）"),
            bg=self.tokens["bg"], fg=self.tokens["text_faint"], font=self._font(8),
        ).pack(anchor="w", pady=(4, 0))

        tk.Label(
            section,
            text="面板是标准 Windows 窗口：拖标题栏移动、右上角可最小化。\n"
                 "关闭按钮与 Esc 都只是“收起面板”，程序仍在托盘运行，"
                 "全局快捷键随时可以再唤出来。\n"
                 "换了显示器或拔掉外接屏后，面板会自动回到可见区域。",
            bg=self.tokens["bg"], fg=self.tokens["text_faint"], font=self._font(8),
            wraplength=430, justify="left",
        ).pack(anchor="w", pady=(4, 0))

    def _build_preview_options(self, section):
        """悬停预览的两个设置项（批次 14）。"""
        settings = self.kernel.get_preview_settings()["data"]
        self.preview_var = tk.BooleanVar(value=settings["enabled"])
        self._check(section, "鼠标停在卡片上时弹出预览浮窗（移开就收起，不抢焦点）",
                    self.preview_var, command=self._on_preview_toggle)

        row = tk.Frame(section, bg=self.tokens["bg"])
        row.pack(fill="x", pady=(2, 0))
        tk.Label(row, text="悬停", bg=self.tokens["bg"], fg=self.tokens["text_muted"],
                 font=self._font(9)).pack(side="left")
        self.preview_delay_var = tk.IntVar(value=int(settings["delayMs"]))
        tk.Scale(row, from_=0, to=1500, resolution=50, orient="horizontal",
                 variable=self.preview_delay_var, showvalue=True, length=150,
                 font=self._font(8), bg=self.tokens["bg"], fg=self.tokens["text"],
                 troughcolor=self.tokens["surface"], highlightthickness=0,
                 activebackground=self.tokens["accent"],
                 command=self._on_preview_delay).pack(side="left", padx=(6, 2))
        tk.Label(row, text="毫秒后弹出（0 = 立刻）", bg=self.tokens["bg"],
                 fg=self.tokens["text_faint"], font=self._font(8)).pack(side="left")
        return self.preview_var, self.preview_delay_var

    def _on_preview_toggle(self):
        self._set(K_PREVIEW_ENABLED, self.preview_var.get())

    def _on_preview_delay(self, _value=None):
        self.kernel.set_preview_settings(delay_ms=self.preview_delay_var.get())

    def _on_pinned_change(self):
        self._set(K_WINDOW_PINNED, self.window_pinned_var.get())

    def _on_arrow_keys_change(self):
        self.kernel.set_navigation_enabled(self.arrow_keys_var.get())

    # ---------------------------------------------------------------- 顺序粘贴
    def _build_sequential(self, parent):
        section = self._section(parent, "顺序粘贴")
        self.sequential_var = tk.BooleanVar(
            value=bool(self.kernel.settings.get(K_SEQUENTIAL_MODE)))
        self._check(section, "开启顺序粘贴（每复制一条自动进队，之后逐条粘出）",
                    self.sequential_var, command=self._on_sequential_change)
        hotkey = self.kernel.settings.get(K_SEQUENTIAL_HOTKEY)
        tk.Label(
            section,
            text="开启后才会注册快捷键「%s」：按住 Alt 连按 V，按一下粘一条。\n"
                 "没开模式时不注册该快捷键（Alt+V 很常用，不该平白占着）。\n"
                 "队列只存在内存里，关掉模式或退出程序即清空。" % hotkey,
            bg=self.tokens["bg"], fg=self.tokens["text_faint"], font=self._font(8),
            wraplength=430, justify="left",
        ).pack(anchor="w", pady=(4, 0))

    def _on_sequential_change(self):
        enabled = bool(self.sequential_var.get())
        result = self.kernel.set_sequential_mode(enabled)
        if not result["ok"]:
            self.sequential_var.set(not enabled)
            messagebox.showerror("顺序粘贴", result["error"]["message"], parent=self)

    # ---------------------------------------------------------------- 系统集成
    # ---------------------------------------------------------------- 提示音
    def _build_sound(self, section):
        """提示音：开关 + 音量 + 试听。

        声音是**现场合成**的（三角波 + 带通 + 一小段噪声起音），
        不依赖任何音频文件——参考实现也是这么做的。
        """
        sound = self.kernel.get_sound_settings()["data"]
        self.sound_var = tk.BooleanVar(value=sound["enabled"])
        self._check(section, "复制时播放提示音（短促的“嘀”声，现场合成，不带音频文件）",
                    self.sound_var, command=self._on_sound_toggle)

        row = tk.Frame(section, bg=self.tokens["bg"])
        row.pack(fill="x", pady=(2, 0))
        tk.Label(row, text="音量", bg=self.tokens["bg"], fg=self.tokens["text_muted"],
                 font=self._font(9)).pack(side="left")
        self.volume_var = tk.IntVar(value=int(sound["volume"]))
        self.volume_scale = tk.Scale(
            row, from_=0, to=100, orient="horizontal", variable=self.volume_var,
            showvalue=True, length=180, font=self._font(8),
            bg=self.tokens["bg"], fg=self.tokens["text"],
            troughcolor=self.tokens["surface"], highlightthickness=0,
            activebackground=self.tokens["accent"],
            command=self._on_volume_change)
        self.volume_scale.pack(side="left", padx=(6, 8))

        self.paste_sound_var = tk.BooleanVar(value=sound["pasteSoundEnabled"])
        self._check(row, "粘贴时也响（两声，音调更高）", self.paste_sound_var,
                    command=self._on_paste_sound_change)

        preview_row = tk.Frame(section, bg=self.tokens["bg"])
        preview_row.pack(fill="x", pady=(2, 0))
        tk.Button(preview_row, text="试听", font=self._font(9),
                  command=self._on_preview_sound).pack(side="left")
        hint = "声音在后台播放，不会拖慢复制粘贴。" if sound["available"] \
            else "⚠ 当前系统不支持播放提示音。"
        tk.Label(preview_row, text=hint, bg=self.tokens["bg"],
                 fg=self.tokens["text_faint"], font=self._font(8)).pack(
            side="left", padx=(8, 0))

    def _on_sound_toggle(self):
        enabled = bool(self.sound_var.get())
        result = self.kernel.set_sound_enabled(enabled)
        if not result["ok"]:
            self.sound_var.set(not enabled)
            self._toast(result["error"]["message"], TOAST_ERROR)
        elif enabled:
            self._toast("已开启提示音", TOAST_SUCCESS)

    def _on_volume_change(self, _value=None):
        result = self.kernel.set_sound_volume(self.volume_var.get())
        if result["ok"]:
            self.volume_var.set(result["data"]["volume"])

    def _on_paste_sound_change(self):
        self.kernel.set_paste_sound_enabled(self.paste_sound_var.get())

    def _on_preview_sound(self):
        result = self.kernel.preview_sound("paste")
        if not result["ok"]:
            self._toast(result["error"]["message"], TOAST_ERROR)

    def _build_system(self, parent):
        section = self._section(parent, "系统集成")

        self._build_sound(section)

        # 提权重启：Win+V 接管要写 HKLM，没有管理员权限时给用户一条出路，
        # 而不是只告诉他"权限不够"
        platform = self.kernel.check_is_admin()
        is_admin = bool(platform["data"]["isAdmin"]) if platform["ok"] else False
        if not is_admin:
            admin_row = tk.Frame(section, bg=self.tokens["bg"])
            admin_row.pack(fill="x", pady=(4, 0))
            tk.Button(admin_row, text="以管理员身份重启", font=self._font(9),
                      command=self._on_restart_as_admin).pack(side="left")
            tk.Label(admin_row, text="（接管 Win+V 需要管理员权限）",
                     bg=self.tokens["bg"], fg=self.tokens["text_faint"],
                     font=self._font(8)).pack(side="left", padx=(8, 0))

        autostart = self.kernel.get_autostart()
        self.autostart_var = tk.BooleanVar(
            value=bool(autostart["data"]["enabled"]) if autostart["ok"] else False)
        self._check(section, "开机自动启动（在注册表 HKCU\\...\\Run 下写一项，随时可取消）",
                    self.autostart_var, command=self._on_autostart_change)

        winv = self.kernel.get_win_v_status()
        status = winv["data"] if winv["ok"] else {}
        self.winv_var = tk.BooleanVar(value=bool(status.get("vDisabled")))
        self._check(section, "接管 Win+V（关掉 Windows 自带的剪贴板历史，把该快捷键让给本程序）",
                    self.winv_var, command=self._on_winv_change)
        tk.Label(
            section,
            text="系统剪贴板历史：%s · Win+V：%s。\n"
                 "接管后需要重启资源管理器（或注销一次）才完全生效；"
                 "取消接管时会一并把系统功能还原。"
                 % ("已开启" if status.get("historyEnabled") == 1 else "已关闭",
                    "已被本程序接管" if status.get("vDisabled") else "仍归 Windows"),
            bg=self.tokens["bg"], fg=self.tokens["text_faint"], font=self._font(8),
            wraplength=430, justify="left",
        ).pack(anchor="w", pady=(4, 0))

        row = tk.Frame(section, bg=self.tokens["bg"])
        row.pack(fill="x", pady=(6, 0))
        tk.Button(row, text="打开数据目录", font=self._font(9),
                  command=self._open_data_folder).pack(side="left")
        platform = self.kernel.get_platform_info()
        info = platform["data"] if platform["ok"] else {}
        admin = self.kernel.check_is_admin()
        is_admin = bool(admin["data"].get("isAdmin")) if admin["ok"] else False
        tk.Label(
            row,
            text="Windows %s%s · %s" % (
                info.get("release", "-"),
                "（Win11）" if info.get("isWindows11") else "",
                "管理员权限" if is_admin else "普通权限"),
            bg=self.tokens["bg"], fg=self.tokens["text_muted"],
            font=self._font(8),
        ).pack(side="left", padx=(10, 0))

    def _on_restart_as_admin(self):
        if not messagebox.askyesno(
                "以管理员身份重启",
                "会用管理员权限重新启动本程序，届时会弹出 UAC 提示。\n\n"
                "当前未保存的内容（比如还没入库的剪贴板记录）不受影响。\n继续？",
                parent=self):
            return
        result = self.kernel.restart_as_admin()
        if not result["ok"]:
            self._toast(result["error"]["message"], TOAST_ERROR)

    def _on_autostart_change(self):
        enabled = bool(self.autostart_var.get())
        result = self.kernel.set_autostart(enabled)
        if not result["ok"]:
            # 写注册表失败要立刻把勾去掉，否则界面在撒谎
            self.autostart_var.set(not enabled)
            messagebox.showerror("开机自启", result["error"]["message"], parent=self)

    def _on_winv_change(self):
        enabled = bool(self.winv_var.get())
        if enabled and not messagebox.askyesno(
                "接管 Win+V",
                "这会把 Windows 自带的剪贴板历史关掉，Win+V 交给剪贴板Pro。\n\n"
                "需要重启资源管理器（或注销一次）才完全生效。\n"
                "随时可以在这里取消接管，取消时会一并还原。\n\n确认接管？",
                parent=self):
            self.winv_var.set(False)
            return
        result = self.kernel.set_win_v_optimized(enabled)
        if not result["ok"]:
            self.winv_var.set(not enabled)
            messagebox.showerror("Win+V", result["error"]["message"], parent=self)

    def _open_data_folder(self):
        result = self.kernel.open_data_folder()
        if not result["ok"]:
            messagebox.showerror("打开数据目录", result["error"]["message"], parent=self)

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

        # **录制期间必须把全局快捷键全部让出去。** 否则用户想录的组合
        # 一旦是当前已注册的（比如想改 Ctrl+Shift+V 本身），
        # Windows 会先把它截走，录制窗口根本收不到那个按键。
        self.kernel.set_recording_mode(True)

        def finish(accelerator):
            # 无论录成没录成都要恢复，否则这段时间按快捷键没反应
            self.kernel.set_recording_mode(False)
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


class PolicyEditor(tk.Toplevel):
    """应用规则的编辑对话框。

    带一个「试跑」区：把一段样例内容丢进去，立刻看到规则会怎么处理。
    规则写错是**静默失效**的（坏正则被跳过），不给用户一个当场验证的机会，
    他只能靠"复制了半天发现没生效"来发现。
    """

    def __init__(self, parent, policy):
        super().__init__(parent)
        self.parent_window = parent
        self.kernel = parent.kernel
        self.tokens = parent.tokens
        self.result = None
        self._policy = dict(policy or {})

        self.title("编辑应用规则" if policy else "添加应用规则")
        self.configure(bg=self.tokens["bg"])
        self.transient(parent)
        self.resizable(False, False)
        self._build()
        self._center(parent)
        self.grab_set()

    def _font(self, size=9, bold=False):
        family = self.tokens["font_family"]
        return (family, size, "bold") if bold else (family, size)

    def _build(self):
        tokens = self.tokens
        root = tk.Frame(self, bg=tokens["bg"])
        root.pack(fill="both", expand=True, padx=14, pady=12)

        tk.Label(root, text="应用名（如 KeePass.exe 或 /银行|支付/）", bg=tokens["bg"],
                 fg=tokens["text_muted"], font=self._font(8)).pack(anchor="w")
        self.name_var = tk.StringVar(value=self._policy.get("appName") or "")
        tk.Entry(root, textvariable=self.name_var, font=self._font(10), width=44,
                 bg=tokens["surface"], fg=tokens["text"], relief="solid", bd=1).pack(
            fill="x", pady=(1, 6))

        tk.Label(root, text="应用路径（可留空；支持完整路径、exe 名、/正则/、MSIX AUMID）",
                 bg=tokens["bg"], fg=tokens["text_muted"], font=self._font(8)).pack(anchor="w")
        self.path_var = tk.StringVar(value=self._policy.get("appPath") or "")
        tk.Entry(root, textvariable=self.path_var, font=self._font(10), width=44,
                 bg=tokens["surface"], fg=tokens["text"], relief="solid", bd=1).pack(
            fill="x", pady=(1, 6))

        action_row = tk.Frame(root, bg=tokens["bg"])
        action_row.pack(fill="x")
        self.action_var = tk.StringVar(
            value=self._policy.get("action") or app_rules_mod.ACTION_CLEAN)
        for value, label in ((app_rules_mod.ACTION_IGNORE, "忽略采集（不入库）"),
                             (app_rules_mod.ACTION_CLEAN, "自动清洗（正则替换）")):
            tk.Radiobutton(
                action_row, text=label, value=value, variable=self.action_var,
                font=self._font(9), bg=tokens["bg"], fg=tokens["text"],
                activebackground=tokens["bg"], selectcolor=tokens["surface"],
                command=self._sync_action,
            ).pack(side="left", padx=(0, 14))

        tk.Label(root, text="生效的内容类型（忽略采集时不勾 = 全部类型）",
                 bg=tokens["bg"], fg=tokens["text_muted"], font=self._font(8)).pack(
            anchor="w", pady=(6, 0))
        kinds_row = tk.Frame(root, bg=tokens["bg"])
        kinds_row.pack(fill="x")
        current = self._policy.get("contentTypes") or list(app_rules_mod.DEFAULT_CONTENT_TYPES)
        self.kind_vars = {}
        for kind in app_rules_mod.DEFAULT_CONTENT_TYPES:
            var = tk.BooleanVar(value=kind in current)
            self.kind_vars[kind] = var
            tk.Checkbutton(
                kinds_row, text=app_rules_mod.TYPE_LABELS.get(kind, kind),
                variable=var, font=self._font(9), bg=tokens["bg"], fg=tokens["text"],
                activebackground=tokens["bg"], selectcolor=tokens["surface"],
            ).pack(side="left", padx=(0, 10))

        self.rules_label = tk.Label(
            root, text="清洗规则（每行一条「正则 => 替换」，# 为注释）",
            bg=tokens["bg"], fg=tokens["text_muted"], font=self._font(8))
        self.rules_label.pack(anchor="w", pady=(6, 0))
        self.rules_text = tk.Text(root, height=5, width=56, font=("Consolas", 9),
                                  bg=tokens["surface"], fg=tokens["text"],
                                  insertbackground=tokens["text"], relief="solid",
                                  bd=1, wrap="none")
        self.rules_text.pack(fill="x")
        self.rules_text.insert("1.0", self._policy.get("cleanupRules") or "")

        # 试跑
        tk.Label(root, text="试跑：贴一段样例内容，看看规则会怎么处理",
                 bg=tokens["bg"], fg=tokens["text_muted"], font=self._font(8)).pack(
            anchor="w", pady=(8, 0))
        self.sample_text = tk.Text(root, height=3, width=56, font=self._font(9),
                                   bg=tokens["surface"], fg=tokens["text"],
                                   insertbackground=tokens["text"], relief="solid",
                                   bd=1)
        self.sample_text.pack(fill="x")
        self.sample_text.insert("1.0", "1\tdef hello():\n2\t    return 1")

        test_row = tk.Frame(root, bg=tokens["bg"])
        test_row.pack(fill="x", pady=(4, 0))
        tk.Button(test_row, text="试跑", font=self._font(9),
                  command=self._run_test).pack(side="left")
        self.test_result = tk.Label(test_row, text="", bg=tokens["bg"],
                                    fg=tokens["text_faint"], font=self._font(8),
                                    anchor="w", justify="left", wraplength=380)
        self.test_result.pack(side="left", padx=(8, 0))

        buttons = tk.Frame(root, bg=tokens["bg"])
        buttons.pack(fill="x", pady=(10, 0))
        tk.Button(buttons, text="取消", font=self._font(9), width=10,
                  command=self.destroy).pack(side="right")
        tk.Button(buttons, text="保存", font=self._font(9), width=10,
                  command=self._save).pack(side="right", padx=(0, 6))

        self._sync_action()

    def _sync_action(self):
        """忽略采集时清洗规则没有意义，直接禁用，别让用户白填。"""
        editing_clean = self.action_var.get() == app_rules_mod.ACTION_CLEAN
        state = "normal" if editing_clean else "disabled"
        self.rules_text.configure(state=state, bg=self.tokens["surface"]
                                  if editing_clean else self.tokens["bg"])

    def _collect(self):
        kinds = [k for k, v in self.kind_vars.items() if v.get()]
        action = self.action_var.get()
        if action == app_rules_mod.ACTION_IGNORE and not kinds:
            # 不勾任何类型 = 全部忽略，这是参考实现的口径
            kinds = []
        return app_rules_mod.normalize_policy({
            "id": self._policy.get("id") or "",
            "enabled": self._policy.get("enabled", True),
            "appName": self.name_var.get().strip(),
            "appPath": self.path_var.get().strip(),
            "action": action,
            "contentTypes": kinds,
            "cleanupRules": self.rules_text.get("1.0", "end").strip(),
        })

    def _run_test(self):
        policy = self._collect()
        sample = self.sample_text.get("1.0", "end").rstrip("\n")
        action, cleaned, _matched = app_rules_mod.AppRuleEngine(
            json.dumps([policy], ensure_ascii=False)).evaluate(
            policy["appName"], policy["appPath"], "code", sample)
        if action == app_rules_mod.ACTION_IGNORE:
            self.test_result.configure(text="→ 这段内容会被忽略，不会入库",
                                       fg=self.tokens["pin"])
        elif cleaned != sample:
            self.test_result.configure(
                text="→ 清洗后：%s" % cleaned.replace("\n", "⏎")[:60],
                fg=self.tokens["accent"])
        else:
            self.test_result.configure(text="→ 规则没有改变这段内容（检查正则是否写对）",
                                       fg=self.tokens["text_faint"])

    def _save(self):
        policy = self._collect()
        if not policy["appPath"] and not policy["appName"]:
            messagebox.showwarning("应用规则", "应用名与应用路径至少填一个", parent=self)
            return
        self.result = policy
        self.destroy()

    def _center(self, parent):
        self.update_idletasks()
        width, height = self.winfo_reqwidth(), self.winfo_reqheight()
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + 40
        self.geometry("%dx%d+%d+%d" % (width, height, max(0, x), max(0, y)))
