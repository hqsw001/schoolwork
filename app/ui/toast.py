# -*- coding: utf-8 -*-
"""轻提示（toast）

《02-用例图与用例说明》明确了本系统不建独立的消息通知模块：
所有反馈都在用户眼前发生，因此用界面内的轻提示承担反馈职责。
这里实现成一个贴在主面板底部的小标签，不抢焦点、不阻塞操作。
"""

import tkinter as tk

DEFAULT_DURATION_MS = 2600

LEVEL_COLOR_KEY = {
    "info": "accent",
    "success": "badge_url",
    "warning": "pin",
    "error": "sensitive",
}


class Toast(object):
    """主面板内的轻提示条。"""

    def __init__(self, parent, theme_getter):
        self.parent = parent
        self.theme_getter = theme_getter
        self._frame = None
        self._label = None
        self._after_id = None

    def show(self, message, level="info", duration=DEFAULT_DURATION_MS):
        if not message:
            return
        tokens = self.theme_getter()
        self._ensure_widgets(tokens)
        color = tokens.get(LEVEL_COLOR_KEY.get(level, "accent"), tokens["accent"])
        self._label.configure(text=message, bg=color, fg="#FFFFFF")
        self._frame.place(relx=0.5, rely=1.0, anchor="s", y=-14)
        if self._after_id:
            try:
                self.parent.after_cancel(self._after_id)
            except Exception:  # noqa: BLE001
                pass
        self._after_id = self.parent.after(duration, self.hide)

    def hide(self):
        self._after_id = None
        if self._frame is not None:
            try:
                self._frame.place_forget()
            except Exception:  # noqa: BLE001
                pass

    def restyle(self, tokens=None):
        if self._frame is None:
            return
        tokens = tokens or self.theme_getter()
        try:
            self._frame.configure(bg=tokens["surface"])
            self._label.configure(font=self._font(tokens))
        except tk.TclError:
            pass

    def _ensure_widgets(self, tokens):
        if self._frame is not None:
            return
        self._frame = tk.Frame(self.parent, bd=0, highlightthickness=0,
                               bg=tokens["surface"])
        self._label = tk.Label(
            self._frame, bd=0, padx=14, pady=7, justify="left",
            font=self._font(tokens), bg=tokens["accent"], fg="#FFFFFF",
        )
        self._label.pack()

    @staticmethod
    def _font(tokens):
        return (tokens.get("font_family", "Microsoft YaHei UI"), 10)
