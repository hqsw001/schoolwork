# -*- coding: utf-8 -*-
"""剪贴板Pro —— 单机剪贴板管理器（内核包）

分层结构（对应《需求分析报告》6.3 分析类图）：

    domain/          实体类与值对象：ClipboardEntry / Tag / Setting / ClipboardData
    infrastructure/  与操作系统和本地资源打交道：Win32 剪贴板、SQLite 仓储
    services/        服务类：处理管线、去重、容量、检索、隐私、粘贴
    app/             应用装配层：内核门面、事件总线
    ui/              界面（tkinter）：主面板、设置、托盘

设计约束（硬性）：
    1. 全部能力均为单机离线能力，任何模块都不得发起网络请求；
    2. 内核与界面之间只通过 Kernel 门面与事件总线交互，界面不直接碰数据库；
    3. 采集由系统通知驱动，不使用定时轮询。
"""

APP_NAME = "剪贴板Pro"
APP_ID = "clipboardpro"
VERSION = "0.1.0"

__all__ = ["APP_NAME", "APP_ID", "VERSION"]
