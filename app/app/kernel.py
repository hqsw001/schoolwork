# -*- coding: utf-8 -*-
"""内核门面（Kernel）：界面唯一的对话对象

对应《需求分析报告》7.3 命令接口 + 7.4 事件推送。界面**不允许**直接访问
数据库、剪贴板或管线，一切通过本类的方法调用，一切变化通过事件订阅回来。

统一返回格式（与需求文档 7.3 节完全一致）：

    {"ok": true, "data": {...}, "error": null, "timestamp": 1757000000000}

线程模型：
    界面（tkinter）在主线程；剪贴板监听在监听线程；入库处理在工作线程。
    Kernel 对数据库的访问全部经由仓储内部的事务锁串行化；
    对界面只推事件，不直接改控件（UI 在自己的 after 回调里消费事件队列）。
"""

import os
import queue
import threading
import time

from app import APP_NAME, VERSION
from app.constants import (
    ERR_INTERNAL,
    ERR_INVALID_ARG,
    ERR_NOT_FOUND,
    EVT_CLIPBOARD_REMOVED,
    EVT_CLIPBOARD_UPDATED,
    EVT_LISTEN_STATUS_CHANGED,
    EVT_THEME_CHANGED,
    EVT_TOAST,
    K_ALWAYS_ON_TOP,
    K_APP_CLEANUP_POLICIES,
    K_ARROW_KEY_SELECTION,
    K_CAPTURE_FILES,
    K_CLEANUP_RULES,
    K_CAPTURE_RICH_TEXT,
    K_DELETE_AFTER_PASTE,
    K_EDGE_DOCKING,
    K_FOLLOW_MOUSE,
    K_IGNORE_BLUR,
    K_LIST_DENSITY,
    K_MAIN_HOTKEY,
    K_MASK_EMAIL_DOMAIN,
    K_MASK_PREFIX,
    K_MASK_SUFFIX,
    K_NAVIGATION_MODE,
    K_PERSISTENT,
    K_PERSISTENT_LIMIT,
    K_PERSISTENT_LIMIT_ENABLED,
    K_PREVIEW_DELAY_MS,
    K_PREVIEW_ENABLED,
    K_PRIVACY_CUSTOM_RULES,
    K_PRIVACY_KINDS,
    K_PRIVACY_PROTECTION,
    K_SEARCH_HOTKEY,
    K_SEQUENTIAL_HOTKEY,
    K_SEQUENTIAL_MODE,
    K_RICH_PASTE_HOTKEY,
    K_SOUND_ENABLED,
    K_SOUND_VOLUME,
    K_PASTE_SOUND_ENABLED,
    K_THEME,
    K_THEME_MODE,
    K_TRAY_VISIBLE,
    K_WINDOW_PINNED,
    PAGE_SIZE,
    PREVIEW_MAX_CHARS,
    SEARCH_RESULT_LIMIT,
    TOAST_ERROR,
    TOAST_INFO,
    TOAST_SUCCESS,
    TOAST_WARNING,
    TYPE_IMAGE,
    TYPE_LABELS,
    TYPE_RICH_TEXT,
    is_protected_tag,
    is_sensitive_tag,
    is_file_type,
    is_text_type,
)
from app.domain.models import ClipboardData, make_preview
from app.errors import AppError, guard, now_ms
from app.infrastructure.clipboard import clipboard_io
from app.infrastructure.clipboard import image_utils
from app.infrastructure.clipboard import listener as listener_mod
from app.infrastructure.clipboard import paste as paste_mod
from app.infrastructure.clipboard import source_app as source_mod
from app.infrastructure.database import Database, default_data_dir
from app.infrastructure import apps
from app.infrastructure import emoji
from app.infrastructure import encryption
from app.infrastructure import icons
from app.infrastructure import monitors as monitors_mod
from app.infrastructure import sound as sound_mod
from app.infrastructure import os_integration
from app.infrastructure import thumbnails
from app.infrastructure.logger import setup_logger
from app.infrastructure.repository import migrations
from app.infrastructure.repository.clipboard_repo import ClipboardRepository
from app.infrastructure.repository.settings_repo import SettingsRepository
from app.services import content_utils
from app.services.capacity import CapacityService
from app.services import app_rules as app_rules_mod
from app.services.dedup import DedupService
from app.services.paste_queue import PasteQueue
from app.services.pipeline import (
    ClipboardPipeline,
    DiscoveryStage,
    DistributionStage,
    PersistenceStage,
    PipelineContext,
    SessionStore,
    TransformationStage,
    ValidationStage,
)
from app.services.privacy import PrivacyService

#: 手动新增记录的来源标记（与剪贴板采集区分开）
MANUAL_SOURCE_NAME = "手动添加"

#: 临时粘贴后、还原剪贴板之前的等待时间。
#: 必须给目标应用留出把内容取走的时间，否则还原太快会粘出上一次的内容。
TRANSIENT_RESTORE_DELAY_MS = 250


class Kernel(object):
    """单机剪贴板内核。"""

    def __init__(self, data_dir=None, logger=None):
        self.started_at = now_ms()
        self.data_dir = data_dir or default_data_dir()
        self.logger = logger or setup_logger(self.data_dir)

        # ---- 持久化（UC-20 主事件流第 1~4 步）
        self.db = Database(data_dir=self.data_dir)
        executed = migrations.run_migrations(self.db.conn, self.logger)
        if executed:
            self.logger.info("数据库结构升级完成：%s -> v%s", executed, migrations.CURRENT_VERSION)

        self.settings = SettingsRepository(self.db, self.logger)
        self.settings.load_all()
        self.repo = ClipboardRepository(self.db, self.logger)
        self.session_store = SessionStore()

        # ---- 服务
        self.privacy = PrivacyService(
            enabled=self.settings.get(K_PRIVACY_PROTECTION, True),
            kinds=self.settings.get(K_PRIVACY_KINDS),
            logger=self.logger,
            custom_rules=self.settings.get(K_PRIVACY_CUSTOM_RULES, ""),
            mask_prefix=self.settings.get(K_MASK_PREFIX, 3),
            mask_suffix=self.settings.get(K_MASK_SUFFIX, 3),
            mask_email_domain=self.settings.get(K_MASK_EMAIL_DOMAIN, False),
        )
        self.dedup = DedupService(self.logger)
        # 顺序粘贴队列：纯内存态，进程退出即清空（见 services/paste_queue.py）
        self.paste_queue = PasteQueue(self.logger)
        # 应用级规则引擎：按来源应用"忽略采集"或"自动清洗"
        self.app_rules = app_rules_mod.AppRuleEngine(
            self.settings.get(K_APP_CLEANUP_POLICIES, ""), self.logger)
        self.capacity = CapacityService(
            repo=self.repo, settings=self.settings,
            attachment_dir=self.db.attachment_dir, db=self.db, logger=self.logger,
        )

        # ---- 管线
        self.pipeline = ClipboardPipeline(
            stages=[
                DiscoveryStage(),
                TransformationStage(
                    settings=self.settings, privacy=self.privacy,
                    attachment_dir=self.db.attachment_dir,
                    thumb_dir=self.db.thumb_dir, logger=self.logger,
                    app_rules=self.app_rules,
                ),
                ValidationStage(self.dedup, self.repo, self.settings, self.logger),
                PersistenceStage(self.repo, self.settings, self.session_store, self.logger),
            ],
            distribution=DistributionStage(
                self.capacity, self._emit, self.logger,
                on_captured=self._on_entry_captured),
        )

        # ---- 事件总线：内核 → 界面
        self._events = queue.Queue()
        self._subscribers = []

        # ---- 采集工作线程：监听线程只投递，真正的读取与入库在这里做
        self._work_queue = queue.Queue()
        self._worker = None
        self._worker_running = False

        # ---- 监听器
        self.listener = None
        self._last_source = source_mod.SourceAppInfo("未知")
        self._paste_target_hwnd = 0
        self._frontend_hide_callback = None
        self._frontend_show_callback = None
        self._stats = {"captured": 0, "inserted": 0, "merged": 0, "echo": 0,
                       "skipped": 0, "failed": 0}

    # ==================================================================
    # 启动 / 停止
    # ==================================================================
    def start(self, enable_listener=True):
        """启动内核（UC-20 主事件流）。"""
        self._start_worker()
        if enable_listener:
            self.start_listener()
        # 第 5 步：扫描孤儿附件（数据库里已无对应记录）
        try:
            self.repo.cleanup_orphan_attachments(self.db.attachment_dir)
            self.cleanup_orphan_thumbnails()
            # 打开内容时落下的临时文件也顺手清一遍（超过 24 小时的）
            os_integration.cleanup_temp_files(logger=self.logger)
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响启动
            self.logger.warning("孤儿附件清理失败：%s", exc)
        # 第 6 步：加密形态对齐（后台跑）
        self._start_encryption_alignment()
        self.logger.info("%s v%s 内核已启动，数据目录 %s", APP_NAME, VERSION, self.data_dir)
        return True

    def _start_encryption_alignment(self):
        """后台把历史敏感记录的加密形态对齐一次（F9-3 自愈）。

        为什么放后台：加密是后加的功能，历史库里可能攒着不少明文敏感记录，
        同步对齐要一条条读出来重新加密，会把启动拖慢好几秒。
        为什么要自愈：用户手动打上/取消 ``sensitive`` 标签、或者从旧版本
        升上来，都会让"标记"与"实际落盘形态"对不上。
        """
        if not encryption.is_available():
            return

        def worker():
            try:
                result = self.repo.realign_encryption(logger=self.logger)
                if result["fixed"]:
                    self.logger.info("加密对齐完成：%s", result)
            except Exception as exc:  # noqa: BLE001 - 对齐失败不影响使用
                self.logger.warning("加密对齐失败：%s", exc)

        thread = threading.Thread(target=worker, name="encryption-align", daemon=True)
        thread.start()

    def shutdown(self):
        """退出前收尾：注销监听、落盘会话记录（UC-19 主事件流第 8 步）。"""
        self.session_store.clear()
        if self.listener:
            self.listener.stop()
            self.listener = None
        self._stop_worker()
        try:
            self.db.close()
        except Exception:  # noqa: BLE001
            pass
        self.logger.info("内核已退出")

    def _configured_hotkeys(self):
        """当前应当注册的快捷键集合。

        顺序粘贴的快捷键**只在顺序模式开启时注册**：没开还占着一个全局
        组合（Alt+V 本身很常用）会平白影响别的软件。

        带格式粘贴则是**一直注册**的——它是"不开面板直接粘"的快捷方式，
        属于常用路径。
        """
        hotkeys = {
            listener_mod.ACTION_MAIN_PANEL: self.settings.get(K_MAIN_HOTKEY),
            listener_mod.ACTION_SEARCH: self.settings.get(K_SEARCH_HOTKEY),
            listener_mod.ACTION_RICH_PASTE: self.settings.get(K_RICH_PASTE_HOTKEY),
        }
        if self.settings.get(K_SEQUENTIAL_MODE, False):
            hotkeys[listener_mod.ACTION_SEQUENTIAL] = self.settings.get(
                K_SEQUENTIAL_HOTKEY)
        return hotkeys

    # ------------------------------------------------------------ 监听
    def start_listener(self):
        if self.listener and self.listener.is_running():
            return True
        hotkeys = self._configured_hotkeys()
        self.listener = listener_mod.ClipboardListener(
            logger=self.logger, hotkeys=hotkeys,
            tray_tooltip="%s %s" % (APP_NAME, VERSION),
        )
        self.listener.tray_callback = self._on_tray_command
        self.listener.start()
        self._emit_listen_status()
        return True

    def _on_tray_command(self, action):
        """托盘菜单动作（在监听线程中被调用，因此只投事件不碰界面）。"""
        mapping = {
            "show": lambda: self._show_panel(),
            "settings": lambda: self._show_panel(settings=True),
            "toggle_listen": self.set_listening_toggle,
            "clear_history": self.clear_history,
            "exit": self._request_exit,
        }
        handler = mapping.get(action)
        if handler:
            try:
                handler()
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("托盘动作 %s 失败：%s", action, exc)

    def _request_exit(self):
        """托盘退出：通知界面走正常退出流程（保存状态、销毁窗口）。"""
        self._emit(EVT_TOAST, {"type": TOAST_INFO, "message": "正在退出…", "action": "exit"})

    # ==================================================================
    # 采集工作线程
    # ==================================================================
    def _start_worker(self):
        if self._worker_running:
            return
        self._worker_running = True
        self._worker = threading.Thread(target=self._worker_loop, name="pipeline-worker", daemon=True)
        self._worker.start()

    def _stop_worker(self):
        self._worker_running = False
        self._work_queue.put(None)
        if self._worker:
            self._worker.join(timeout=1.5)
            self._worker = None

    def _worker_loop(self):
        while self._worker_running:
            try:
                task = self._work_queue.get(timeout=0.5)
            except queue.Empty:
                continue
            if task is None:
                break
            try:
                task()
            except Exception as exc:  # noqa: BLE001 - 工作线程绝不允许退出
                self.logger.exception("采集任务失败：%s", exc)

    def _schedule(self, func):
        self._work_queue.put(func)

    # ------------------------------------------------------------ 监听事件
    def handle_listener_events(self):
        """在界面的定时回调里调用：把监听线程的事件转成内核动作。

        这是"监听线程 → 界面线程"的唯一通道，中间不共享任何可变状态。
        """
        if not self.listener:
            return
        for event in self.listener.poll_events():
            kind = event.kind
            if kind == listener_mod.EVENT_CLIPBOARD_UPDATE:
                info = event.payload
                # 剪贴板读取与入库放到工作线程，避免阻塞消息循环
                self._schedule(lambda info=info: self._on_clipboard_update(info))
            elif kind == listener_mod.EVENT_HOTKEY:
                self._handle_hotkey(event.payload.get("action"))
            elif kind == listener_mod.EVENT_HOTKEY_CHANGED:
                # 配置里的快捷键被别人占了、已自动改用替代键：
                # 必须主动告诉用户实际生效的是哪个键
                self._on_hotkey_substituted(event.payload)
            elif kind == listener_mod.EVENT_HOTKEY_FAILED:
                self._emit(EVT_TOAST, {
                    "type": TOAST_ERROR,
                    "message": "快捷键全部被其他程序占用，暂时请点托盘图标唤出面板",
                })
            elif kind == listener_mod.EVENT_ERROR:
                self.notify(event.payload.get("message", "监听异常"),
                            event.payload.get("level", TOAST_WARNING))
            elif kind == listener_mod.EVENT_STARTED:
                self._emit_listen_status()

    def _handle_hotkey(self, action):
        if action == listener_mod.ACTION_MAIN_PANEL:
            self._emit(EVT_TOAST, {"type": TOAST_INFO, "message": "hotkey",
                                   "action": "toggle_panel"})
        elif action == listener_mod.ACTION_SEARCH:
            self._emit(EVT_TOAST, {"type": TOAST_INFO, "message": "hotkey",
                                   "action": "show_search"})
        elif action == listener_mod.ACTION_TOGGLE_LISTEN:
            self.set_listening_toggle()
        elif action == listener_mod.ACTION_SEQUENTIAL:
            self._paste_next_step_safe()
        elif action == listener_mod.ACTION_RICH_PASTE:
            self._paste_latest_rich_safe()

    def _paste_latest_rich_safe(self):
        """带格式粘贴的快捷键入口：失败只提示，不抛。

        快捷键回调里抛异常没地方接，而且用户按一下没反应会以为程序坏了。
        """
        try:
            result = self.paste_latest_rich()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("带格式粘贴失败：%s", exc)
            return
        if not result.get("ok"):
            message = (result.get("error") or {}).get("message") or "带格式粘贴失败"
            self.notify(message, TOAST_WARNING)

    def _on_hotkey_substituted(self, payload):
        """快捷键被自动替换后：把新键写回配置，并提示用户。

        为什么必须写回配置：面板状态栏与设置界面读的都是配置里的值。
        如果只改监听器内部状态，界面会继续显示那个**按了没反应**的旧键，
        用户只会得出"快捷键坏了"的结论。
        """
        configured = payload.get("configured")
        effective = payload.get("effective")
        if not effective:
            return
        # **所有可能被降级的快捷键都要在这里**。漏掉一个的后果是：
        # 那个动作实际用的是备选键，界面上却还显示配置里的旧键，
        # 用户按了没反应、也看不出为什么（本缺陷在同一份代码里犯过第二次）。
        for key in (K_MAIN_HOTKEY, K_SEARCH_HOTKEY,
                    K_RICH_PASTE_HOTKEY, K_SEQUENTIAL_HOTKEY):
            if self.settings.get(key) == configured:
                try:
                    self.settings.set(key, effective)
                except Exception as exc:  # noqa: BLE001
                    self.logger.warning("写回替代快捷键失败：%s", exc)
                break
        self._emit(EVT_TOAST, {
            "type": TOAST_WARNING,
            "message": "快捷键 %s 已被其他程序占用，已自动改用 %s"
                       % (configured, effective),
        })
        self._emit_listen_status()

    # ------------------------------------------------------------ 采集
    def _on_clipboard_update(self, source_info):
        """监听线程收到剪贴板变化（本方法在工作线程中执行）。"""
        self._last_source = source_info or source_mod.SourceAppInfo("未知")
        options = {
            "capture_rich_text": self.settings.get(K_CAPTURE_RICH_TEXT, True),
            "capture_files": self.settings.get(K_CAPTURE_FILES, True),
            # 来源决定要不要保留电子表格的私有格式（Excel/WPS 才留 Biff 之类）
            "source_app": getattr(source_info, "app_name", None),
            "logger": self.logger,
        }
        try:
            data = clipboard_io.read_clipboard_data(options)
        except AppError as exc:
            # UC-01 异常流 E1：读取失败重试后放弃，不打扰用户（只记日志）
            self.logger.warning("剪贴板读取失败：%s", exc)
            return
        except Exception as exc:  # noqa: BLE001
            self.logger.exception("剪贴板读取未预期异常：%s", exc)
            return
        if data is None:
            # 空内容或系统私有格式：跳过（UC-01 异常流 E2）
            self._stats["skipped"] += 1
            return
        self._stats["captured"] += 1
        ctx = self.capture(data, self._last_source)
        return ctx

    def capture(self, data, source_info=None, timestamp=None):
        """把一份 ClipboardData 走完整条管线（也供测试直接调用）。"""
        ctx = PipelineContext(
            data, source_app=source_info or self._last_source, timestamp=timestamp
        )
        self.pipeline.execute(ctx)
        outcome = ctx.outcome or "processed"
        self._stats[outcome] = self._stats.get(outcome, 0) + 1
        if outcome == "failed" and ctx.error:
            self._emit(EVT_TOAST, {
                "type": TOAST_ERROR,
                "message": "保存失败：%s" % ctx.error.message,
            })
        # 复制提示音：只在**真正入库**时响。合并、被忽略、跳过都不响——
        # 否则用户会被一连串"其实什么都没存"的提示音搞糊涂。
        if outcome == "inserted":
            self._play_sound(sound_mod.SOUND_COPY)
        return ctx

    def _play_sound(self, kind):
        """按配置播放提示音。任何失败都静默（声音不该影响复制粘贴）。"""
        try:
            if not self.settings.get(K_SOUND_ENABLED, False):
                return False
            if kind == sound_mod.SOUND_PASTE \
                    and not self.settings.get(K_PASTE_SOUND_ENABLED, True):
                return False
            volume = self.settings.get(K_SOUND_VOLUME, 35)
            return sound_mod.play(kind, volume)
        except Exception as exc:  # noqa: BLE001
            self.logger.debug("提示音播放失败：%s", exc)
            return False

    # ==================================================================
    # 事件总线
    # ==================================================================
    def _emit(self, event_name, payload):
        """内核内部事件出口（DistributionStage 也用它）。"""
        event = {"name": event_name, "payload": payload, "timestamp": now_ms()}
        self._events.put(event)
        for callback in list(self._subscribers):
            try:
                callback(event)
            except Exception as exc:  # noqa: BLE001
                self.logger.warning("事件订阅者异常（%s）：%s", event_name, exc)

    def subscribe(self, callback):
        """订阅事件；返回取消订阅的函数。"""
        self._subscribers.append(callback)

        def unsubscribe():
            if callback in self._subscribers:
                self._subscribers.remove(callback)

        return unsubscribe

    def poll_events(self, max_items=64):
        """界面 after 回调中调用：取出待处理事件。"""
        out = []
        for _ in range(max_items):
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        return out

    def _emit_listen_status(self):
        paused = bool(self.listener and self.listener.is_paused())
        running = bool(self.listener and self.listener.is_running())
        self._emit(EVT_LISTEN_STATUS_CHANGED, {"paused": paused, "running": running})

    def _show_panel(self, settings=False):
        self._emit(EVT_TOAST, {
            "type": TOAST_INFO, "message": "show",
            "action": "show_settings" if settings else "show_panel",
        })

    def notify(self, message, level=TOAST_INFO):
        self._emit(EVT_TOAST, {"type": level, "message": message})

    # ==================================================================
    # 界面回调注册（让内核能指挥面板显隐，但由界面决定怎么实现）
    # ==================================================================
    def register_frontend(self, show=None, hide=None):
        self._frontend_show_callback = show
        self._frontend_hide_callback = hide

    def request_show_panel(self, focus_search=False):
        if self._frontend_show_callback:
            self._frontend_show_callback(focus_search)
        return True

    def request_hide_panel(self):
        if self._frontend_hide_callback:
            self._frontend_hide_callback()
        return True

    # ==================================================================
    # 命令接口：历史与检索
    # ==================================================================
    @guard
    def get_history(self, limit=PAGE_SIZE, offset=0, content_type=None, source_app=None,
                    pinned_only=False):
        """分页读取历史（对应命令 get_history）。"""
        if not self.settings.get(K_PERSISTENT, True):
            items = self.session_store.list(limit, offset, content_type)
            return {"items": [e.to_dict() for e in items],
                    "total": self.session_store.count(), "sessionOnly": True}
        entries = self.repo.get_history(
            limit=limit, offset=offset, content_type=content_type,
            source_app=source_app, pinned_only=pinned_only,
        )
        return {
            "items": [e.to_dict() for e in entries],
            "total": self.repo.count(),
            "offset": offset,
            "limit": limit,
            "sessionOnly": False,
        }

    @guard
    def search_history(self, query, limit=SEARCH_RESULT_LIMIT, tag_only=False,
                       content_type=None, source_app=None):
        """按关键字检索（对应命令 search_history，UC-08）。"""
        if not self.settings.get(K_PERSISTENT, True):
            entries = [e for e in self.session_store.list(SEARCH_RESULT_LIMIT)
                       if _session_match(e, query, tag_only)]
        else:
            entries = self.repo.search(
                query, limit=limit, tag_only=tag_only,
                content_type=content_type, source_app=source_app,
            )
        normalized = content_utils.normalize_for_compare(query or "")
        return {
            "items": [e.to_dict() for e in entries],
            "total": len(entries),
            "query": query or "",
            "tagOnly": bool(tag_only),
            "highlight": normalized,
            "truncated": len(entries) >= limit,
        }

    @guard
    def get_entry(self, entry_id):
        """读取单条记录完整内容（对应命令 get_entry）。"""
        if entry_id is None:
            raise AppError(ERR_INVALID_ARG, "缺少记录 id")
        entry = self.repo.find_by_id(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        data = entry.to_dict()
        data["contentFull"] = self._full_content(entry)
        data["sensitiveKinds"] = self.privacy.describe_hits(entry.content or "")
        return data

    def _full_content(self, entry):
        """取记录的完整内容（图片返回附件路径，文本返回正文）。"""
        if entry.content_type == TYPE_IMAGE:
            return entry.content
        return entry.content

    @guard
    def get_thumbnail(self, entry_id, size=thumbnails.DEFAULT_THUMB_SIZE):
        """取图片记录的列表缩略图（base64 PNG，界面直接喂给 PhotoImage）。

        统一走这里而不是让界面自己开附件：缩略图带磁盘缓存，采集时已经
        生成好，列表刷新时只是读一个小文件，不会每次都解码原图。
        """
        if entry_id is None:
            raise AppError(ERR_INVALID_ARG, "缺少记录 id")
        entry = self.repo.find_by_id(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        if entry.content_type != TYPE_IMAGE:
            raise AppError(ERR_INVALID_ARG, "该记录不是图片，没有缩略图")
        data = thumbnails.thumbnail_base64(
            self.db.thumb_dir, entry.content, size=size, logger=self.logger)
        if not data:
            raise AppError(ERR_NOT_FOUND, "缩略图不可用（附件已删除或无法解码）")
        return {"data": data, "size": size, "contentType": entry.content_type}

    @guard
    def get_count(self):
        """记录总数（对应命令 get_count）。"""
        return {"count": self.repo.count(), "sessionOnly": not self.settings.get(K_PERSISTENT, True)}

    @guard
    def get_type_counts(self):
        """各类型条数（F4-5）。"""
        counts = self.repo.count_by_type()
        out = {key: 0 for key in TYPE_LABELS}
        out.update(counts)
        out["all"] = self.repo.count()
        return {"counts": out}

    @guard
    def list_sources(self):
        """来源应用列表（F4-3）。"""
        return {"items": self.repo.list_sources()}

    @guard
    def get_storage_usage(self):
        """条数与附件占用空间（对应命令 get_storage_usage）。"""
        return self.capacity.stats()

    # ==================================================================
    # 命令接口：组织（置顶 / 标签 / 编辑 / 删除）
    # ==================================================================
    @guard
    def toggle_pin(self, entry_id, is_pinned=None):
        """置顶 / 取消置顶（UC-10）。"""
        entry = self.repo.find_by_id(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        target = (not entry.is_pinned) if is_pinned is None else bool(is_pinned)
        updated = self.repo.toggle_pin(int(entry_id), target)
        self._emit(EVT_CLIPBOARD_UPDATED, {"entry": updated.to_dict(), "outcome": "pinned"})
        return updated.to_dict()

    @guard
    def update_entry_content(self, entry_id, content, preview=None):
        """编辑记录内容并重算指纹（UC-12）。"""
        entry = self.repo.find_by_id(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        if not is_text_type(entry.content_type):
            raise AppError(ERR_INVALID_ARG, "图片与文件类记录不支持内容编辑")
        new_content = (content or "").strip() if content is not None else ""
        if not new_content:
            raise AppError(ERR_INVALID_ARG, "内容不能为空")
        new_hash = content_utils.text_hash(new_content)
        new_type = entry.content_type
        clear_html = False
        if entry.content_type == "rich_text":
            # 富文本被改成纯文本后必须清空原 HTML（UC-12 业务规则 R3）
            new_type = content_utils.detect_text_type(new_content)
            clear_html = True
        updated = self.repo.update_content(
            int(entry_id), new_content,
            preview=preview or make_preview(self.privacy.mask_preview(new_content)),
            content_hash=new_hash, content_type=new_type, clear_html=clear_html,
        )
        self._emit(EVT_CLIPBOARD_UPDATED, {"entry": updated.to_dict(), "outcome": "edited"})
        return updated.to_dict()

    @guard
    def add_tag(self, entry_id, tag):
        """为记录添加标签（UC-11）。

        打上 ``sensitive`` / ``密码`` 这类内置标签时，**顺带把记录标记为敏感**，
        于是它会加密落盘（F9-3）。用户的心智模型是"我标了敏感它就该被保护"，
        只打标签不加密会让人以为已经保护了。
        """
        updated = self.repo.add_tag(int(entry_id), tag)
        if is_sensitive_tag(tag) and not updated.is_sensitive:
            updated = self.repo.mark_sensitive(int(entry_id), True)
        self._emit(EVT_CLIPBOARD_UPDATED, {"entry": updated.to_dict(), "outcome": "tagged"})
        return updated.to_dict()

    @guard
    def remove_tag(self, entry_id, tag):
        updated = self.repo.remove_tag(int(entry_id), tag)
        # 去掉最后一个敏感标签还不够：内容本身若**仍被识别为敏感**，
        # 就不能撤掉保护——否则"删个标签"就把身份证号变回明文了。
        if is_sensitive_tag(tag) and updated.is_sensitive \
                and not any(is_sensitive_tag(t) for t in updated.tags) \
                and not self.privacy.is_sensitive(updated.content or ""):
            updated = self.repo.mark_sensitive(int(entry_id), False)
        self._emit(EVT_CLIPBOARD_UPDATED, {"entry": updated.to_dict(), "outcome": "untagged"})
        return updated.to_dict()

    @guard
    def list_tags(self):
        """列出全部标签及颜色（对应命令 list_tags）。"""
        items = self.repo.list_tags()
        for item in items:
            # 界面据此禁用"改名/删除"按钮，而不是等点了才报错
            item["protected"] = is_protected_tag(item["name"])
        return {"items": items}

    @guard
    def get_tag_items(self, tag, limit=SEARCH_RESULT_LIMIT):
        """按标签取记录（对应命令 get_tag_items）。"""
        name = self.repo.validate_tag_name(tag)
        entries = self.repo.items_by_tag(name, limit=limit)
        return {"tag": name, "items": [e.to_dict() for e in entries],
                "count": len(entries)}

    @guard
    def rename_tag(self, old, new):
        """全局重命名标签（对应命令 rename_tag_globally）。

        内置标签（sensitive / 密码 / password）不允许改名：它们是
        "敏感信息保护"与加密落盘的依据，改了名整个机制就失效了。
        """
        source = self.repo.validate_tag_name(old)
        if is_protected_tag(source):
            raise AppError(ERR_INVALID_ARG,
                           "「%s」是内置标签，不能改名（它关联着敏感信息保护）" % source)
        result = self.repo.rename_tag(source, new)
        self._emit(EVT_CLIPBOARD_UPDATED, {"entry": None, "outcome": "tag-renamed"})
        return result

    @guard
    def upsert_tag_color(self, tag, color):
        return self.repo.set_tag_color(tag, color)

    @guard
    def delete_tag(self, tag):
        """删除标签（对应命令 delete_tag_from_all）。

        内置标签同样不允许删除——删掉之后新采集的敏感内容就不会再被标记。
        """
        name = self.repo.validate_tag_name(tag)
        if is_protected_tag(name):
            raise AppError(ERR_INVALID_ARG,
                           "「%s」是内置标签，不能删除（它关联着敏感信息保护）" % name)
        result = self.repo.delete_tag(name)
        self._emit(EVT_CLIPBOARD_UPDATED, {"entry": None, "outcome": "tag-deleted"})
        return result

    @guard
    def delete_entry(self, entry_id):
        """删除单条记录及其附件（UC-13）。"""
        if not self.repo.delete(int(entry_id), self.db.attachment_dir):
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        self._emit(EVT_CLIPBOARD_REMOVED, {"ids": [int(entry_id)], "reason": "manual"})
        return {"deleted": [int(entry_id)]}

    @guard
    def delete_entries(self, ids):
        """批量删除（UC-13 备选流 A2）。"""
        removed = self.repo.delete_many([int(i) for i in (ids or [])], self.db.attachment_dir)
        if removed:
            self._emit(EVT_CLIPBOARD_REMOVED, {"ids": removed, "reason": "manual"})
        return {"deleted": removed}

    @guard
    def clear_history(self):
        """清空未置顶且无标签的记录（UC-13 主事件流第 8 步）。"""
        removed = self.repo.clear(keep_protected=True, attachment_dir=self.db.attachment_dir)
        if removed:
            self._emit(EVT_CLIPBOARD_REMOVED, {"ids": removed, "reason": "clear"})
        kept = self.repo.count()
        return {"deleted": len(removed), "kept": kept}

    @guard
    def cleanup_by_age(self, age_seconds):
        """按时间清理历史（对应命令 cleanup_by_age，F3-4）。"""
        removed = self.repo.cleanup_by_age(int(age_seconds), self.db.attachment_dir)
        if removed:
            self._emit(EVT_CLIPBOARD_REMOVED, {"ids": removed, "reason": "age"})
        return {"deleted": len(removed)}

    @guard
    def vacuum_database(self):
        """整理数据库（对应命令 vacuum_database）。"""
        self.repo.vacuum()
        return {"ok": True}

    def cleanup_orphan_thumbnails(self):
        """删掉已经没有附件引用的缩略图（附件删除后留下的缓存）。

        不抛异常：缓存清理失败不该影响启动或任何主流程。
        """
        try:
            referenced = self.repo.all_attachment_paths()
            orphans = thumbnails.find_orphan_thumbnails(self.db.thumb_dir, referenced)
            for path in orphans:
                thumbnails.delete_thumbnail(path, self.db.thumb_dir, self.logger)
            return len(orphans)
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("清理孤儿缩略图失败：%s", exc)
            return 0

    @guard
    def get_thumbnail_cache_usage(self):
        """缩略图缓存占用（设置面板展示用）。"""
        total, count = thumbnails.cache_usage(self.db.thumb_dir)
        return {"bytes": total, "count": count}

    @guard
    def clear_thumbnail_cache(self):
        """清空缩略图缓存；下次渲染会按需重建。"""
        removed = 0
        for path in thumbnails.find_orphan_thumbnails(self.db.thumb_dir, []):
            if thumbnails.delete_thumbnail(path, self.db.thumb_dir, self.logger):
                removed += 1
        return {"removed": removed}

    # ==================================================================
    # 命令接口：系统集成（对应 TieZ 的 system_cmd / content_handler）
    # ==================================================================
    @guard
    def open_content(self, entry_id):
        """用系统默认程序打开这条记录的内容（UC-09 备选流 A5）。

        网址走浏览器；文件/视频/图片直接用默认程序打开（图片的 content
        本来就是附件路径）；纯文本与富文本先落到临时文件再打开，
        这样用户可以用外部编辑器改，改完的文件也是他自己拿着的。
        """
        entry = self.repo.find_by_id(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        plan = os_integration.open_content(
            entry.content_type, entry.content, self.logger)
        if not plan.get("opened"):
            raise AppError(ERR_INTERNAL, "打开失败：目标不存在，或系统没有关联程序")
        # 只加使用次数，**不重排列表**——用户正在翻找时列表突然跳动很难受
        try:
            self.repo.increment_use_count(entry.id)
        except AppError:
            pass
        return {
            "action": plan["action"],
            "contentType": entry.content_type,
            "target": (plan.get("target") or "")[:200],
            "tempPath": plan.get("tempPath"),
        }

    @guard
    def open_data_folder(self):
        """在资源管理器里打开数据目录。"""
        if not os_integration.open_folder(self.db.data_dir, self.logger):
            raise AppError(ERR_INTERNAL, "打开数据目录失败")
        return {"path": self.db.data_dir}

    @guard
    def open_entry_location(self, entry_id):
        """打开这条记录对应文件所在的目录并选中它。"""
        entry = self.repo.find_by_id(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        target = entry.content or ""
        if entry.content_type in ("file", "video"):
            paths = [line.strip() for line in target.split("\n") if line.strip()]
            target = paths[0] if paths else ""
        if not target or not os.path.exists(target):
            raise AppError(ERR_NOT_FOUND, "该记录没有可定位的文件")
        if not os_integration.open_file_location(target, self.logger):
            raise AppError(ERR_INTERNAL, "打开文件位置失败")
        return {"path": target}

    @guard
    def get_platform_info(self):
        """平台信息（含 Win10 / Win11 判定）。"""
        return os_integration.platform_info()

    @guard
    def check_is_admin(self):
        """当前进程是否以管理员身份运行。"""
        return {"isAdmin": os_integration.is_admin()}

    @guard
    def get_autostart(self):
        """开机自启状态（对应命令 is_autostart_enabled）。"""
        return {"enabled": os_integration.is_autostart_enabled(),
                "command": os_integration.autostart_command()}

    @guard
    def set_autostart(self, enabled):
        """开启 / 关闭开机自启（对应命令 toggle_autostart）。"""
        ok, message = os_integration.set_autostart(bool(enabled))
        if not ok:
            raise AppError(ERR_INTERNAL, message)
        self.notify(message, TOAST_SUCCESS)
        return {"enabled": bool(enabled), "message": message}

    @guard
    def get_win_v_status(self):
        """Win+V 接管状态 + 系统剪贴板历史状态。"""
        return os_integration.win_v_status()

    @guard
    def set_win_v_optimized(self, enabled):
        """接管 / 归还 Win+V（对应命令 trigger_registry_win_v_optimization）。

        注意这是**真的改注册表**：开启会把系统 Win+V 关掉。
        """
        ok, message, status = os_integration.set_win_v_optimized(
            bool(enabled), self.logger)
        if not ok:
            raise AppError(ERR_INTERNAL, message)
        self.notify(message, TOAST_SUCCESS)
        return status

    @guard
    def set_system_clipboard_history(self, enabled):
        """开启 / 关闭 Windows 自带的剪贴板历史。"""
        ok, message = os_integration.set_windows_clipboard_history(
            bool(enabled), self.logger)
        if not ok:
            raise AppError(ERR_INTERNAL, message)
        self.notify(message, TOAST_SUCCESS)
        return {"enabled": bool(enabled), "message": message}

    def send_system_notification(self, title, body, level="info"):
        """弹一个系统托盘气泡通知。

        比面板内的 toast 更容易看到——面板收起时 toast 是看不见的。
        没有监听器（未启动托盘）时静默返回 False。
        """
        if self.listener is None:
            return False
        try:
            self.listener.show_balloon(title, body, level)
            return True
        except Exception as exc:  # noqa: BLE001 - 通知失败不影响主流程
            self.logger.warning("发送系统通知失败：%s", exc)
            return False

    @guard
    def get_os_diagnostics(self):
        """系统集成状态汇总（设置面板展示用）。"""
        status = os_integration.win_v_status()
        status.update({
            "isAdmin": os_integration.is_admin(),
            "autostart": os_integration.is_autostart_enabled(),
            "dataDir": self.db.data_dir,
            "platform": os_integration.platform_info(),
        })
        return status

    # ==================================================================
    # 命令接口：图标与关联应用（对应 TieZ 的 apps.rs）
    # ==================================================================
    @guard
    def get_source_app_icon(self, entry_id, size=icons.SMALL_ICON_SIZE):
        """取这条记录的来源应用图标（base64 PNG）。

        来源应用的 exe 路径在采集时就存下来了（source_app_path），
        所以这里不需要再去查前台窗口。
        """
        entry = self.repo.find_by_id(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        data = icons.source_app_icon_base64(entry.source_app_path, size=size)
        if not data:
            raise AppError(ERR_NOT_FOUND, "没有可用的来源应用图标")
        return {"data": data, "size": size, "path": entry.source_app_path or ""}

    @guard
    def get_file_type_icon(self, entry_id, size=icons.SMALL_ICON_SIZE):
        """取文件类记录的类型图标（base64 PNG）。

        文件多半早被删了，所以按扩展名取通用图标——这也是
        ``SHGFI_USEFILEATTRIBUTES`` 存在的意义。
        """
        entry = self.repo.find_by_id(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        target = entry.content or ""
        if entry.content_type in ("file", "video"):
            paths = [line.strip() for line in target.split("\n") if line.strip()]
            target = paths[0] if paths else ""
        if not target:
            raise AppError(ERR_NOT_FOUND, "该记录没有可识别类型的路径")
        data = icons.file_type_icon_base64(target, size=size)
        if not data:
            raise AppError(ERR_NOT_FOUND, "没有可用的文件图标")
        return {"data": data, "size": size, "path": target}

    @guard
    def get_icon_cache_usage(self):
        """图标缓存占用（内存里，进程退出即释放）。"""
        return icons.cache_usage()

    @guard
    def clear_icon_cache(self):
        """清空图标缓存；下次渲染重新抠一遍。"""
        return {"removed": icons.clear_cache()}

    @guard
    def get_system_default_app(self, content_type):
        """某个内容类型的系统默认程序。"""
        return apps.system_default_app(content_type)

    @guard
    def get_associated_apps(self, extension):
        """某个扩展名在"打开方式"里出现过的程序。"""
        return {"items": apps.associated_apps(extension)}

    @guard
    def scan_installed_apps(self):
        """扫描已安装应用（会起一次 PowerShell，约 1~2 秒，按需调用）。"""
        return {"items": apps.scan_installed_apps()}

    # ==================================================================
    # 命令接口：顺序粘贴（对应 TieZ 的 paste_queue.rs）
    # ==================================================================
    # ==================================================================
    # 命令接口：加密落盘（F9-3）
    # ==================================================================
    @guard
    def get_encryption_status(self):
        """加密能力与对齐状态（设置面板展示用）。"""
        stats = self.repo.alignment_stats()
        return {
            "available": encryption.is_available(),
            "algorithm": "Windows DPAPI",
            "encryptableTypes": list(encryption.ENCRYPTABLE_TYPES),
            "scanned": stats["total"],
            "missingEncryption": len(stats["missingEncryption"]),
            "staleEncryption": len(stats["staleEncryption"]),
            "aligned": not stats["missingEncryption"] and not stats["staleEncryption"],
        }

    @guard
    def realign_encryption(self):
        """把"标记与实际落盘形态对不上"的记录修正过来（F9-3 自愈）。"""
        if not encryption.is_available():
            raise AppError(ERR_INTERNAL, "当前平台不支持 DPAPI 加密")
        result = self.repo.realign_encryption(logger=self.logger)
        if result["fixed"]:
            self.notify("已加密 %d 条历史敏感记录" % result["fixed"], TOAST_SUCCESS)
        return result

    @guard
    def mark_sensitive(self, entry_id, is_sensitive):
        """手动设置/取消敏感标记（UC-17 备选流 A4/A5）。"""
        if is_sensitive and not encryption.is_available():
            raise AppError(ERR_INTERNAL, "当前平台不支持 DPAPI 加密")
        updated = self.repo.mark_sensitive(int(entry_id), bool(is_sensitive))
        self._emit(EVT_CLIPBOARD_UPDATED, {"entry": updated.to_dict(),
                                           "outcome": "sensitive-changed"})
        return {"id": updated.id, "isSensitive": bool(updated.is_sensitive)}

    def _on_entry_captured(self, entry):
        """DistributionStage 的回调：顺序模式下把新记录追加进队列。"""
        if not self.settings.get(K_SEQUENTIAL_MODE, False):
            return
        if entry is None:
            return
        self.paste_queue.append(entry.id)
        self._emit(EVT_CLIPBOARD_UPDATED, {
            "entry": entry.to_dict(), "outcome": "queued",
            "queueSize": self.paste_queue.size(),
        })

    def _resolve_queue_entry(self, entry_id):
        """按 id 取记录：正数查数据库，负数查会话内存。"""
        if int(entry_id) > 0:
            return self.repo.find_by_id(int(entry_id))
        return self.session_store.find_by_id(int(entry_id))

    @guard
    def get_paste_queue(self):
        """读取顺序粘贴队列（对应命令 get_paste_queue）。"""
        items = []
        for entry_id in self.paste_queue.items():
            entry = self._resolve_queue_entry(entry_id)
            items.append({
                "id": entry_id,
                "missing": entry is None,
                "preview": (entry.preview if entry is not None else "（已删除）"),
                "contentType": entry.content_type if entry is not None else "",
            })
        return {"items": items, "size": len(items),
                "sequentialMode": bool(self.settings.get(K_SEQUENTIAL_MODE, False))}

    @guard
    def set_paste_queue(self, item_ids):
        """整体设置队列内容（对应命令 set_paste_queue）。传空列表即清空。"""
        ids = [int(i) for i in (item_ids or [])]
        self.paste_queue.set_items(ids)
        return {"items": self.paste_queue.items(), "size": self.paste_queue.size()}

    @guard
    def clear_paste_queue(self):
        """清空顺序粘贴队列。"""
        self.paste_queue.clear()
        return {"size": 0}

    @guard
    def add_to_paste_queue(self, entry_id):
        """把一条记录加入顺序粘贴队列（界面上右键就能加）。"""
        entry = self._resolve_queue_entry(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        self.paste_queue.append(entry.id)
        return {"size": self.paste_queue.size(), "items": self.paste_queue.items()}

    @guard
    def set_sequential_mode(self, enabled):
        """开关顺序粘贴模式（对应命令 set_sequential_mode）。

        开与关都要**重新注册快捷键**：这个模式下才注册顺序粘贴快捷键，
        关掉就该把它还回去，不能一直占着 Alt+V。
        """
        enabled = bool(enabled)
        self.settings.set(K_SEQUENTIAL_MODE, enabled)
        self.reapply_hotkeys()
        if not enabled:
            self.paste_queue.clear()
        return {"sequentialMode": enabled,
                "hotkey": self.settings.get(K_SEQUENTIAL_HOTKEY),
                "queueSize": self.paste_queue.size()}

    @guard
    def paste_next_step(self, simulate=True):
        """弹出一条并粘出去（对应命令 paste_next_step）。

        遇到已经失效的队首（用户把那条删了）自动跳过，不打断连续粘贴。
        """
        target = None
        skipped = 0
        while True:
            entry_id = self.paste_queue.pop()
            if entry_id is None:
                raise AppError(ERR_NOT_FOUND, "顺序粘贴队列已空")
            entry = self._resolve_queue_entry(entry_id)
            if entry is not None:
                target = entry
                break
            skipped += 1
            if skipped > 200:      # 防御：队列被写坏时不至于死循环
                raise AppError(ERR_NOT_FOUND, "队列里的记录都已失效")

        # "按住 Alt 连按 V"：热键触发时 Alt 会被系统吃掉，粘完补按一次，
        # 否则第二次 V 就不再构成 Alt+V，连续粘贴会断在第一下。
        alt_was_down = paste_mod.alt_is_down()
        result = self.paste_entry(target.id, rich_paste=True, simulate=simulate)
        paste_mod.restore_alt(alt_was_down)
        # paste_entry 已经是 @guard 命令，直接返回它的信封（guard 不会重复包）
        if isinstance(result, dict) and "data" in result and result.get("ok"):
            result["data"]["skipped"] = skipped
            result["data"]["remaining"] = self.paste_queue.size()
        return result

    def _paste_next_step_safe(self):
        """快捷键入口：失败只提示，不抛（快捷键回调里抛异常没地方接）。"""
        try:
            result = self.paste_next_step()
        except Exception as exc:  # noqa: BLE001
            self.logger.warning("顺序粘贴失败：%s", exc)
            return
        if not result.get("ok"):
            message = (result.get("error") or {}).get("message") or "顺序粘贴失败"
            self.notify(message, TOAST_WARNING)

    # ==================================================================
    # 命令接口：粘贴（最核心的一条链路）
    # ==================================================================
    @guard
    def paste_entry(self, entry_id, rich_paste=True, simulate=True, target_hwnd=None):
        """把记录写回系统剪贴板并模拟粘贴（UC-09）。

        顺序严格按业务规则 R1：① 写粘贴标记 → ② 写剪贴板 → ③ 模拟按键。
        面板隐藏与焦点交还由界面在 hide 回调中完成（业务规则 R4）。
        """
        entry = self.repo.find_by_id(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")

        # ---- 记住目标窗口：调用方没给就用当前前台窗口
        if target_hwnd:
            self._paste_target_hwnd = int(target_hwnd)
        elif not self._paste_target_hwnd:
            self._paste_target_hwnd = paste_mod.get_foreground_window()

        # ---- ① 写粘贴标记（必须在写剪贴板之前）
        fingerprint = None
        kind = "text"
        if entry.content_type == TYPE_IMAGE:
            kind = "image"
            try:
                fingerprint = image_utils.image_hash_from_bytes(open(entry.content, "rb").read())
            except Exception as exc:  # noqa: BLE001 - 附件丢失时给出明确提示
                self.logger.warning("图片附件读取失败：%s", exc)
                raise AppError(ERR_NOT_FOUND, "该图片附件已不存在，无法粘贴")
        else:
            fingerprint = content_utils.text_hash(entry.content)
        self.dedup.mark_paste(
            fingerprint=fingerprint, kind=kind, content_type=entry.content_type,
            raw_text=entry.content if kind == "text" else "",
            normalized_text=content_utils.normalize_for_compare(entry.content)
            if kind == "text" else "",
            entry_id=entry.id,
        )

        # ---- ② 写剪贴板
        try:
            self._write_entry_to_clipboard(entry, rich_paste=rich_paste)
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AppError(ERR_INTERNAL, "写入剪贴板失败：%s" % exc)

        # ---- ③ 模拟粘贴
        pasted = False
        if simulate:
            if self._frontend_hide_callback:
                self._frontend_hide_callback()
            target = self._paste_target_hwnd or paste_mod.get_foreground_window()
            pasted = paste_mod.simulate_paste(target, logger=self.logger)
            self._paste_target_hwnd = 0

        # ---- 粘贴后的收尾（UC-09 主事件流第 10~11 步）
        if self.settings.get(K_DELETE_AFTER_PASTE, False):
            self.repo.delete(entry.id, self.db.attachment_dir)
            self._emit(EVT_CLIPBOARD_REMOVED, {"ids": [entry.id], "reason": "after_paste"})
        else:
            self.repo.touch_entry(entry.id, use_count_delta=1)
            refreshed = self.repo.find_by_id(entry.id)
            if refreshed:
                self._emit(EVT_CLIPBOARD_UPDATED, {
                    "entry": refreshed.to_dict(), "outcome": "pasted",
                })

        if simulate and not pasted:
            self.notify("已复制到剪贴板，请手动按 Ctrl+V 粘贴", TOAST_WARNING)
        # 粘贴提示音：只有真粘出去了才响，降级成"已复制请手动粘贴"时不响——
        # 那时候粘贴还没发生，响一声反而误导
        if pasted:
            self._play_sound(sound_mod.SOUND_PASTE)
        return {"id": entry.id, "pasted": bool(pasted), "simulated": bool(simulate),
                "targetHwnd": target_hwnd or 0}

    @guard
    def copy_entry(self, entry_id, rich_paste=True):
        """仅复制到剪贴板，不模拟粘贴（UC-09 备选流 A3）。"""
        return self.paste_entry(entry_id, rich_paste=rich_paste, simulate=False)

    @guard
    def copy_entry_as_file(self, entry_id):
        """把图片记录以「文件」形式放进剪贴板（CF_HDROP）。

        为什么需要它：资源管理器只认文件列表，不认位图。图片记录走
        paste_entry 时写的是 CF_DIB，粘到文件夹里不会有任何反应——
        这是 Windows 的行为，不是程序缺陷。想让图片能直接粘进文件夹，
        只能把附件文件的路径放上剪贴板。
        """
        entry = self.repo.find_by_id(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        if entry.content_type != TYPE_IMAGE:
            raise AppError(ERR_INVALID_ARG, "该记录不是图片，无法按文件复制")

        path = entry.content
        try:
            with open(path, "rb") as fh:
                fingerprint = image_utils.image_hash_from_bytes(fh.read())
        except Exception as exc:  # noqa: BLE001 - 附件丢失时给出明确提示
            self.logger.warning("图片附件读取失败：%s", exc)
            raise AppError(ERR_NOT_FOUND, "该图片附件已不存在，无法复制为文件")

        # CF_HDROP 里的 .png 会被自己重新采集一遍，先登记回声标记
        self.dedup.mark_paste(
            fingerprint=fingerprint, kind="image", content_type=TYPE_IMAGE,
            entry_id=entry.id,
        )
        try:
            clipboard_io.write_files([path])
        except AppError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise AppError(ERR_INTERNAL, "写入剪贴板失败：%s" % exc)
        return {"paths": [path], "count": 1}

    # ==================================================================
    # 命令接口：手动新增 / 置顶排序 / 临时粘贴 / 快捷粘贴
    # ==================================================================
    @guard
    def add_manual_item(self, content, content_type=None, tags=None):
        """手动新增一条记录（对应命令 add_manual_item）。

        不经过剪贴板：直接构造 ClipboardData 走**同一条管线**，这样类型判定、
        指纹、脱敏、容量裁剪与正常采集完全一致，不会出现"手动加的记录行为
        跟别的不一样"。来源标记为「手动添加」，方便日后区分。
        """
        text = content or ""
        if not text.strip():
            raise AppError(ERR_INVALID_ARG, "内容不能为空")
        kind = "rich_text" if content_type == TYPE_RICH_TEXT else "text"
        ctx = self.capture(
            ClipboardData(kind, text=text),
            source_info=source_mod.SourceAppInfo(MANUAL_SOURCE_NAME),
        )
        if ctx.outcome == "failed":
            raise AppError(ERR_INTERNAL, "保存失败：%s"
                           % (ctx.error.message if ctx.error else "未知原因"))
        entry = ctx.entry
        if entry is None:
            raise AppError(ERR_INTERNAL, "记录未能保存")
        applied = []
        for tag in (tags or []):
            try:
                self.repo.add_tag(entry.id, tag)
                applied.append(tag)
            except AppError as exc:
                self.logger.warning("手动新增记录打标签失败（%s）：%s", tag, exc.message)
        refreshed = self.repo.find_by_id(entry.id) or entry
        return {"id": refreshed.id, "outcome": ctx.outcome,
                "tags": applied, "entry": refreshed.to_dict()}

    @guard
    def set_pinned_order(self, entry_ids):
        """按给定顺序重排置顶项（对应命令 update_pinned_order）。

        列表顺序即显示顺序；置顶区按 ``pinned_order`` **降序**排，
        所以这里倒着赋值——第一个 id 拿到最大的 order。
        """
        ids = [int(i) for i in (entry_ids or [])]
        if not ids:
            raise AppError(ERR_INVALID_ARG, "缺少置顶记录 id")
        total = len(ids)
        orders = [(entry_id, total - index) for index, entry_id in enumerate(ids)]
        self.repo.update_pinned_order(orders)
        return {"orders": orders}

    @guard
    def move_pinned(self, entry_id, delta):
        """把某条置顶记录在置顶区里上移 / 下移一格（delta = -1 / +1）。"""
        delta = int(delta)
        if delta == 0:
            raise AppError(ERR_INVALID_ARG, "位移不能为 0")
        result = self.get_history(limit=SEARCH_RESULT_LIMIT, pinned_only=True)
        pinned = [item["id"] for item in (result["data"]["items"] if result["ok"] else [])]
        if int(entry_id) not in pinned:
            raise AppError(ERR_NOT_FOUND, "该记录不在置顶区")
        index = pinned.index(int(entry_id))
        target = index + delta
        if target < 0 or target >= len(pinned):
            # 到边界了不算错误，界面只是不该再让用户按
            return {"moved": False, "index": index, "count": len(pinned)}
        pinned[index], pinned[target] = pinned[target], pinned[index]
        self.repo.update_pinned_order(
            [(pid, len(pinned) - i) for i, pid in enumerate(pinned)])
        return {"moved": True, "index": target, "count": len(pinned)}

    def _transient_paste(self, write_payload, target_hwnd=None):
        """先快照剪贴板 → 写入 → 模拟粘贴 → 还原。

        ``write_payload`` 是个无参可调用，负责把内容写进剪贴板。抽出来是因为
        文本表情与图片表情都要走这条流程，只有写剪贴板那一步不同。
        """
        snapshot = clipboard_io.snapshot_clipboard()
        pasted = False
        try:
            write_payload()
            if self._frontend_hide_callback:
                self._frontend_hide_callback()
            target = (target_hwnd or self._paste_target_hwnd
                      or paste_mod.get_foreground_window())
            pasted = paste_mod.simulate_paste(target, logger=self.logger)
        finally:
            self._paste_target_hwnd = 0
            # 必须等目标应用把内容取走再还原，太早会粘出上一次的内容
            time.sleep(TRANSIENT_RESTORE_DELAY_MS / 1000.0)
            try:
                clipboard_io.restore_clipboard(snapshot)
            except Exception as exc:  # noqa: BLE001 - 还原失败不该让粘贴算失败
                self.logger.warning("临时粘贴后还原剪贴板失败：%s", exc)
        return pasted

    @guard
    def paste_transient(self, text, target_hwnd=None):
        """临时粘贴：粘完把剪贴板还原回去（对应命令 paste_content_transiently）。

        "顺手粘一下，别弄脏我的剪贴板"——表情面板、快捷回复这类场景。
        还原失败不影响已完成的粘贴，只记日志。
        """
        pasted = self._transient_paste(
            lambda: clipboard_io.write_text(text or ""), target_hwnd)
        return {"pasted": bool(pasted)}

    @guard
    def paste_text_directly(self, text, target_hwnd=None):
        """把一段文本直接粘到目标窗口（对应命令 paste_text_directly）。"""
        pasted = self._transient_paste(
            lambda: clipboard_io.write_text(text or ""), target_hwnd)
        return {"pasted": bool(pasted)}

    @guard
    def paste_image_file(self, path, target_hwnd=None):
        """把一个图片文件当**图片**粘出去（图片表情用），粘完还原剪贴板。"""
        if not path or not os.path.isfile(path):
            raise AppError(ERR_NOT_FOUND, "图片文件不存在")
        with open(path, "rb") as handle:
            payload = handle.read()
        if not payload:
            raise AppError(ERR_NOT_FOUND, "图片文件是空的")
        pasted = self._transient_paste(
            lambda: clipboard_io.write_image(payload), target_hwnd)
        return {"pasted": bool(pasted)}

    @guard
    def paste_latest_rich(self, simulate=True):
        """粘贴最新一条富文本记录（对应命令 paste_latest_rich）。

        绑定到"带格式粘贴"快捷键。历史里没有富文本时退回最新一条记录，
        避免用户按了快捷键却毫无反应。
        """
        items = self.repo.get_history(limit=20, content_type=TYPE_RICH_TEXT)
        if not items:
            items = self.repo.get_history(limit=1)
        if not items:
            raise AppError(ERR_NOT_FOUND, "历史里还没有任何记录")
        entry = items[0]
        if entry.content_type == TYPE_RICH_TEXT:
            return self.paste_entry(entry.id, rich_paste=True, simulate=simulate)
        return self.paste_entry(entry.id, rich_paste=False, simulate=simulate)

    @guard
    def test_hotkey_available(self, accelerator):
        """探测某个快捷键能不能注册（对应命令 test_hotkey_available）。

        设置界面在保存前用它提前告诉用户"这个键被占了"，
        而不是等保存完才发现按了没反应。
        """
        if not accelerator:
            raise AppError(ERR_INVALID_ARG, "快捷键不能为空")
        try:
            modifiers, vk = paste_mod.parse_accelerator(accelerator)
        except AppError as exc:
            raise AppError(ERR_INVALID_ARG, "快捷键格式无效：%s" % exc.message)
        if self.listener is None:
            raise AppError(ERR_INTERNAL, "监听器未启动，无法探测快捷键")
        return self.listener.probe_hotkey(accelerator, modifiers, vk)

    # ==================================================================
    # 命令接口：紧凑预览浮窗
    # ==================================================================
    @guard
    def get_preview_settings(self):
        """预览浮窗的配置。"""
        return {
            "enabled": bool(self.settings.get(K_PREVIEW_ENABLED, True)),
            "delayMs": int(self.settings.get(K_PREVIEW_DELAY_MS, 350)),
            "maxChars": PREVIEW_MAX_CHARS,
        }

    @guard
    def set_preview_settings(self, enabled=None, delay_ms=None):
        """开/关悬停预览、调整悬停多久才弹。"""
        if enabled is not None:
            self.settings.set(K_PREVIEW_ENABLED, bool(enabled))
        if delay_ms is not None:
            try:
                value = int(delay_ms)
            except (TypeError, ValueError):
                raise AppError(ERR_INVALID_ARG, "延迟必须是数字")
            self.settings.set(K_PREVIEW_DELAY_MS, max(0, min(3000, value)))
        return self.get_preview_settings()["data"]

    @guard
    def get_preview_content(self, entry_id):
        """浮窗要显示的内容（对应 TieZ 的 compact-preview 载荷）。

        图片类的正文是**磁盘路径**，浮窗直接读文件；其余类型给文本。
        敏感记录照常返回明文 —— 用户主动悬停就是"我要看"，
        与点开记录同义（UC-17 业务规则 R3）。
        """
        entry = self._resolve_queue_entry(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")

        payload = {
            "id": entry.id,
            "contentType": entry.content_type,
            "preview": entry.preview or "",
            "sourceApp": entry.source_app or "",
            "timestamp": entry.timestamp,
            "isSensitive": bool(entry.is_sensitive),
            "tags": list(entry.tags or []),
            "isPinned": bool(entry.is_pinned),
            "isTruncated": False,
            "content": "",
            "imagePath": None,
            "files": [],
        }

        if entry.content_type == TYPE_IMAGE:
            payload["imagePath"] = entry.content
        elif is_file_type(entry.content_type):
            payload["files"] = self._describe_files(entry.content)
        else:
            text = entry.content or ""
            # 浮窗不需要几万字：截断显示，并如实标注"已截断"
            if len(text) > PREVIEW_MAX_CHARS:
                payload["content"] = text[:PREVIEW_MAX_CHARS]
                payload["isTruncated"] = True
            else:
                payload["content"] = text
            if entry.content_type == TYPE_RICH_TEXT and entry.html_content:
                # 富文本按约定降级为纯文本预览（见差距清单"需要确认的点"第 2 条）
                payload["plainFromHtml"] = content_utils.strip_html(entry.html_content)
        return payload

    def _describe_files(self, content):
        """给路径列表补上大小与修改时间。

        **只回原始数值**（字节数、毫秒时间戳），格式化成
        "1.2 MB / 3 分钟前"是界面层的事——内核不该管怎么显示。
        """
        out = []
        for line in (content or "").split("\n"):
            path = line.strip()
            if not path:
                continue
            item = {"path": path, "name": os.path.basename(path) or path,
                    "exists": False, "size": 0, "mtime": 0, "isDir": False}
            try:
                if os.path.isfile(path):
                    info = os.stat(path)
                    item["exists"] = True
                    item["size"] = info.st_size
                    item["mtime"] = int(info.st_mtime * 1000)
                elif os.path.isdir(path):
                    item["exists"] = True
                    item["isDir"] = True
                    item["mtime"] = int(os.stat(path).st_mtime * 1000)
            except OSError:
                pass
            out.append(item)
        return out

    # ==================================================================
    # 命令接口：提示音
    # ==================================================================
    @guard
    def get_sound_settings(self):
        """提示音配置与可用性。"""
        return {
            "enabled": bool(self.settings.get(K_SOUND_ENABLED, False)),
            "volume": int(self.settings.get(K_SOUND_VOLUME, 35)),
            "pasteSoundEnabled": bool(
                self.settings.get(K_PASTE_SOUND_ENABLED, True)),
            "available": sound_mod.is_available(),
            "synthesized": True,
        }

    @guard
    def set_sound_enabled(self, enabled):
        """开关提示音（对应命令 set_sound_enabled）。"""
        enabled = bool(enabled)
        self.settings.set(K_SOUND_ENABLED, enabled)
        # 打开时立刻响一声，让用户知道"就是这个声音"
        if enabled:
            self._play_sound(sound_mod.SOUND_COPY)
        return self.get_sound_settings()["data"]

    @guard
    def set_sound_volume(self, volume):
        """设置音量（0~100）。越界会被夹到范围内而不是报错。"""
        try:
            value = int(volume)
        except (TypeError, ValueError):
            raise AppError(ERR_INVALID_ARG, "音量必须是数字")
        value = max(0, min(100, value))
        self.settings.set(K_SOUND_VOLUME, value)
        return {"volume": value}

    @guard
    def set_paste_sound_enabled(self, enabled):
        """粘贴时是否也响（对应 TieZ 的 pasteSoundEnabled）。"""
        self.settings.set(K_PASTE_SOUND_ENABLED, bool(enabled))
        return {"pasteSoundEnabled": bool(enabled)}

    @guard
    def preview_sound(self, kind=None):
        """试听（设置界面的"试听"按钮）。**不受开关限制** ——
        试听的意义就是"打开之前先听听"，被开关挡住就没法用了。"""
        kind = kind or sound_mod.SOUND_PASTE
        if kind not in sound_mod.SOUND_TYPES:
            raise AppError(ERR_INVALID_ARG, "未知的提示音类型：%s" % kind)
        if not sound_mod.is_available():
            raise AppError(ERR_INTERNAL, "当前系统不支持播放提示音")
        volume = int(self.settings.get(K_SOUND_VOLUME, 35))
        played = sound_mod.play(kind, volume)
        if not played:
            raise AppError(ERR_INTERNAL, "播放失败：" + (sound_mod.last_error() or "未知原因"))
        return {"kind": kind, "volume": volume}

    # ==================================================================
    # 命令接口：窗口行为（对应 TieZ 的窗口控制命令）
    # ==================================================================
    @guard
    def hide_window_cmd(self):
        """命令式隐藏窗口（对应命令 hide_window_cmd）。

        为什么不直接调面板：内核不该依赖界面。这里只投一个事件，
        由界面自己决定怎么隐藏——没有界面时（比如测试、纯内核用法）
        这个命令也不会炸。
        """
        self._emit(EVT_TOAST, {"type": TOAST_INFO, "message": "hotkey",
                               "action": "hide_panel"})
        return {"hidden": True}

    @guard
    def toggle_window_cmd(self):
        """命令式显隐窗口（对应命令 toggle_window_cmd）。"""
        self._emit(EVT_TOAST, {"type": TOAST_INFO, "message": "hotkey",
                               "action": "toggle_panel"})
        return {"toggled": True}

    @guard
    def activate_window_focus(self):
        """显式把焦点给面板（对应命令 activate_window_focus）。"""
        self._emit(EVT_TOAST, {"type": TOAST_INFO, "message": "hotkey",
                               "action": "activate_focus"})
        return {"activated": True}

    @guard
    def restore_last_focus(self):
        """把焦点还给唤出面板之前的那个窗口（对应命令 restore_last_focus）。"""
        self._emit(EVT_TOAST, {"type": TOAST_INFO, "message": "hotkey",
                               "action": "restore_last_focus"})
        return {"target": self.paste_target()}

    @guard
    def set_navigation_enabled(self, enabled):
        """开/关方向键选择（对应命令 set_navigation_enabled）。"""
        self.settings.set(K_ARROW_KEY_SELECTION, bool(enabled))
        self._emit(EVT_TOAST, {"type": TOAST_INFO, "message": "hotkey",
                               "action": "navigation_changed"})
        return {"enabled": bool(enabled)}

    @guard
    def set_navigation_mode(self, mode):
        """方向键的选择模式（对应命令 set_navigation_mode）。

        当前只支持 ``list``（在列表里上下移动）；保留这个接口是为了
        让配置能表达意图，将来加"按卡片网格移动"时不用再改配置结构。
        """
        mode = str(mode or "list").strip().lower()
        if mode not in ("list", "grid", "none"):
            raise AppError(ERR_INVALID_ARG, "未知的导航模式：%s" % mode)
        self.settings.set(K_NAVIGATION_MODE, mode)
        self.settings.set(K_ARROW_KEY_SELECTION, mode != "none")
        return {"mode": mode}

    @guard
    def get_window_behavior(self):
        """窗口行为的当前状态（界面与诊断面板用）。"""
        monitors = monitors_mod.list_monitors()
        return {
            "monitors": monitors,
            "monitorCount": len(monitors),
            "edgeDocking": bool(self.settings.get(K_EDGE_DOCKING, False)),
            "followMouse": bool(self.settings.get(K_FOLLOW_MOUSE, False)),
            "ignoreBlur": bool(self.settings.get(K_IGNORE_BLUR, False)),
            "windowPinned": bool(self.settings.get(K_WINDOW_PINNED, False)),
            "trayVisible": bool(self.settings.get(K_TRAY_VISIBLE, True)),
            "arrowKeySelection": bool(
                self.settings.get(K_ARROW_KEY_SELECTION, True)),
            "navigationMode": self.settings.get(K_NAVIGATION_MODE, "list"),
            "alwaysOnTop": bool(self.settings.get(K_ALWAYS_ON_TOP, True)),
        }

    @guard
    def set_window_behavior(self, **options):
        """批量设置窗口行为（对应 TieZ 那一串 set_xxx 命令）。

        合成一个命令是因为界面上这些开关在同一个分区里，
        逐个调用会写很多次数据库、也会触发很多次事件。
        """
        mapping = {
            "edgeDocking": K_EDGE_DOCKING,
            "followMouse": K_FOLLOW_MOUSE,
            "ignoreBlur": K_IGNORE_BLUR,
            "windowPinned": K_WINDOW_PINNED,
            "trayVisible": K_TRAY_VISIBLE,
            "arrowKeySelection": K_ARROW_KEY_SELECTION,
            "alwaysOnTop": K_ALWAYS_ON_TOP,
        }
        applied = {}
        for name, key in mapping.items():
            if name in options and options[name] is not None:
                self.settings.set(key, bool(options[name]))
                applied[name] = bool(options[name])
        return {"applied": applied}

    # ==================================================================
    # 命令接口：应用级规则引擎
    # ==================================================================
    @guard
    def get_app_cleanup_policies(self):
        """读取应用级规则（对应命令 get_app_cleanup_policies）。"""
        policies = self.app_rules.policies()
        for policy in policies:
            policy["summary"] = self._describe_policy(policy)
        return {"items": policies, "count": len(policies)}

    @staticmethod
    def _describe_policy(policy):
        """给人看的一句话摘要，界面上直接显示。"""
        target = policy.get("appName") or policy.get("appPath") or "（未指定）"
        if policy["action"] == app_rules_mod.ACTION_IGNORE:
            kinds = policy.get("contentTypes") or []
            scope = "全部内容" if not kinds else app_rules_mod.describe_content_types(kinds)
            return "忽略 %s 的%s" % (target, scope)
        count = len(app_rules_mod.parse_cleanup_rules(policy.get("cleanupRules")))
        return "清洗 %s 的内容（%d 条规则）" % (target, count)

    @guard
    def set_app_cleanup_policies(self, policies):
        """整体设置应用级规则（对应命令 set_app_cleanup_policies）。"""
        if not isinstance(policies, (list, tuple)):
            raise AppError(ERR_INVALID_ARG, "规则必须是一个列表")
        normalized = [app_rules_mod.normalize_policy(p) for p in policies
                      if isinstance(p, dict)]
        for policy in normalized:
            if not policy["appPath"] and not policy["appName"]:
                raise AppError(ERR_INVALID_ARG, "每条规则都要填应用名或应用路径")
        raw = app_rules_mod.dump_app_cleanup_policies(normalized)
        self.settings.set(K_APP_CLEANUP_POLICIES, raw)
        self.app_rules.reload(raw)
        return {"items": self.app_rules.policies(), "count": self.app_rules.count()}

    @guard
    def test_app_rule(self, source_app, source_app_path=None, content_type="text",
                      text=None):
        """试跑一次规则（对应命令 test_app_rule），让用户在保存前就看到效果。"""
        action, cleaned, policy = self.app_rules.evaluate(
            source_app, source_app_path, content_type, text if text is not None else "")
        return {
            "action": action or "none",
            "ignored": action == app_rules_mod.ACTION_IGNORE,
            "matched": policy is not None,
            "policy": policy,
            "summary": self._describe_policy(policy) if policy else "",
            "before": text,
            "after": cleaned,
            "changed": text is not None and cleaned != text,
        }

    @guard
    def add_app_rule_for_entry(self, entry_id, action="ignore", content_types=None):
        """给某条记录的来源应用**一键加规则**（右键菜单用）。

        比让用户手打 exe 名靠谱得多：应用名与进程路径直接从记录里取。
        """
        entry = self.repo.find_by_id(int(entry_id))
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在")
        app_name = entry.source_app or ""
        app_path = getattr(entry, "source_app_path", None) or ""
        if not app_name and not app_path:
            raise AppError(ERR_INVALID_ARG, "这条记录没有来源应用信息")
        if action not in app_rules_mod.ACTIONS:
            raise AppError(ERR_INVALID_ARG, "未知的动作：%s" % action)

        policies = self.app_rules.policies()
        for policy in policies:
            if (policy.get("appName") or "") == app_name \
                    and (policy.get("appPath") or "") == app_path:
                raise AppError(ERR_INVALID_ARG,
                               "「%s」已经有规则了，请在设置里修改" % app_name)
        policies.append(app_rules_mod.normalize_policy({
            "id": "rule-%d" % (len(policies) + 1),
            "enabled": True,
            "appName": app_name,
            "appPath": app_path,
            "action": action,
            "contentTypes": list(content_types or app_rules_mod.DEFAULT_CONTENT_TYPES),
            "cleanupRules": "",
        }))
        raw = app_rules_mod.dump_app_cleanup_policies(policies)
        self.settings.set(K_APP_CLEANUP_POLICIES, raw)
        self.app_rules.reload(raw)
        return {"count": self.app_rules.count(), "appName": app_name}

    @guard
    def delete_app_cleanup_policy(self, index):
        """按序号删除一条规则。"""
        policies = self.app_rules.policies()
        index = int(index)
        if index < 0 or index >= len(policies):
            raise AppError(ERR_NOT_FOUND, "规则不存在")
        removed = policies.pop(index)
        raw = app_rules_mod.dump_app_cleanup_policies(policies)
        self.settings.set(K_APP_CLEANUP_POLICIES, raw)
        self.app_rules.reload(raw)
        return {"removed": removed, "count": self.app_rules.count()}

    @guard
    def get_global_cleanup_rules(self):
        """读取全局清洗规则原文（对应命令 set_cleanup_rules 的读侧）。"""
        raw = self.settings.get(K_CLEANUP_RULES, "") or ""
        return {"raw": raw,
                "count": len(app_rules_mod.parse_cleanup_rules(raw, self.logger))}

    @guard
    def set_cleanup_rules(self, rules):
        """设置全局清洗规则（对应命令 set_cleanup_rules）。

        保存前先解析一遍：写错的正则在这里就告诉用户，
        而不是等他复制了半天才发现规则没生效。
        """
        raw = rules if isinstance(rules, str) else "\n".join(rules or [])
        parsed = app_rules_mod.parse_cleanup_rules(raw, self.logger)
        self.settings.set(K_CLEANUP_RULES, raw)
        return {"raw": raw, "count": len(parsed)}

    # ==================================================================
    # 命令接口：表情（对应 TieZ 的 EmojiPanel / file_cmd）
    # ==================================================================
    @guard
    def get_emoji_groups(self):
        """内置 Emoji 分组表（完全离线，随程序带一份）。"""
        groups = emoji.load_groups()
        return {"groups": groups,
                "count": sum(len(g["emojis"]) for g in groups)}

    @guard
    def paste_emoji(self, text, target_hwnd=None):
        """点一个 Emoji 就把它粘到目标窗口（粘完还原剪贴板）。"""
        if not text:
            raise AppError(ERR_INVALID_ARG, "表情内容为空")
        pasted = self._transient_paste(
            lambda: clipboard_io.write_text(text), target_hwnd)
        return {"pasted": bool(pasted), "emoji": text}

    @guard
    def list_emoji_favorites(self):
        """列出图片表情收藏。"""
        items = emoji.list_favorites(self.db.data_dir)
        total, count = emoji.favorite_usage(self.db.data_dir)
        return {"items": items, "count": count, "bytes": total}

    @guard
    def save_emoji_favorite(self, path):
        """把本地图片文件收进表情收藏（对应命令 save_emoji_favorite）。"""
        try:
            saved = emoji.save_file(self.db.data_dir, path, self.logger)
        except ValueError as exc:
            raise AppError(ERR_INVALID_ARG, str(exc))
        return {"path": saved, "alreadySaved": saved == path}

    @guard
    def save_emoji_favorite_data_url(self, data_url, file_name=None):
        """把 data URL 收进收藏（对应命令 save_emoji_favorite_data_url）。"""
        try:
            saved = emoji.save_data_url(
                self.db.data_dir, data_url, file_name, self.logger)
        except ValueError as exc:
            raise AppError(ERR_INVALID_ARG, str(exc))
        return {"path": saved}

    @guard
    def remove_emoji_favorite(self, path):
        """删除一个收藏（对应命令 remove_emoji_favorite）。

        路径来自界面，所以 emoji 模块会校验它确实在收藏目录之内——
        否则一个构造出来的相对路径就能删掉磁盘上任意文件。
        """
        if not emoji.remove_favorite(self.db.data_dir, path, self.logger):
            raise AppError(ERR_INVALID_ARG, "删除失败：文件不存在，或不在收藏目录内")
        return {"removed": path}

    @guard
    def get_emoji_favorite_icon(self, path, size=44):
        """收藏图片的网格缩略图（base64 PNG）。

        直接塞原图会让网格卡住——收藏里可能有几 MB 的动图。
        """
        if not emoji.is_inside(path, emoji.favorite_dir(self.db.data_dir)):
            raise AppError(ERR_INVALID_ARG, "路径不在收藏目录内")
        data = thumbnails.thumbnail_base64(
            self.db.thumb_dir, path, size=size, logger=self.logger)
        if not data:
            raise AppError(ERR_NOT_FOUND, "缩略图不可用")
        return {"data": data, "size": size, "path": path}

    def _write_entry_to_clipboard(self, entry, rich_paste=True):
        """按内容类型写入对应格式（UC-09 主事件流第 3 步）。"""
        content_type = entry.content_type
        if content_type == TYPE_IMAGE:
            with open(entry.content, "rb") as fh:
                clipboard_io.write_image(fh.read())
            return
        if is_file_type(content_type):
            paths = [line for line in (entry.content or "").split("\n") if line.strip()]
            if not paths:
                raise AppError(ERR_INVALID_ARG, "该记录没有可粘贴的文件路径")
            clipboard_io.write_files(paths)
            return
        if content_type == "rich_text" and rich_paste and entry.html_content:
            # 带上随记录保存的命名格式（RTF / 电子表格私有格式），
            # 这样"复制 Excel → 粘回 Excel"不会掉公式与单元格格式
            clipboard_io.write_text_html_and_formats(entry.content, entry.html_content)
            return
        clipboard_io.write_text(entry.content)

    @guard
    def write_text_to_clipboard(self, text):
        """把任意文本写入剪贴板（供"固定到历史"等入口使用）。"""
        clipboard_io.write_text(text or "")
        return {"ok": True}

    def set_paste_target(self, hwnd):
        """界面在唤出面板之前登记"要粘回哪个窗口"。"""
        self._paste_target_hwnd = int(hwnd or 0)

    def paste_target(self):
        """当前登记的粘贴目标窗口句柄（0 表示还没登记）。"""
        return int(getattr(self, "_paste_target_hwnd", 0) or 0)

    # ==================================================================
    # 命令接口：监听状态
    # ==================================================================
    @guard
    def set_listening(self, enabled):
        """暂停 / 恢复监听（对应命令 set_listening，UC-14）。

        暂停只影响采集，不影响浏览、检索与粘贴（UC-14 业务规则 R3）。
        """
        if self.listener is None:
            self.start_listener()
        paused = not bool(enabled)
        self.listener.set_paused(paused)
        self._emit_listen_status()
        self._emit(EVT_TOAST, {
            "type": TOAST_INFO,
            "message": "剪贴板监听已暂停" if paused else "剪贴板监听已恢复",
        })
        return {"paused": paused, "running": self.listener.is_running()}

    def set_listening_toggle(self):
        """切换监听开关（托盘菜单与快捷键共用）。"""
        currently_paused = bool(self.listener and self.listener.is_paused())
        return self.set_listening(currently_paused)

    @guard
    def get_listen_status(self):
        """读取监听状态（对应命令 get_listen_status）。"""
        if self.listener is None:
            return {"running": False, "paused": False, "registered": {},
                    "configured": {}, "substituted": {}}
        diag = self.listener.diagnostics()
        report = self.listener.registration_report()
        return {
            "running": diag["running"],
            "paused": diag["paused"],
            # 实际生效的快捷键（可能已因占用而自动降级）
            "registered": diag["registeredHotkeys"],
            # 配置里写的值（用户意图）
            "configured": report["configured"],
            # 哪些动作发生了降级
            "substituted": report["substituted"],
            "trayAdded": diag["trayAdded"],
        }

    # ==================================================================
    # 命令接口：配置与外观
    # ==================================================================
    @guard
    def get_settings(self):
        """读取全部配置项（对应命令 get_settings）。"""
        return {"settings": self.settings.snapshot(), "items": self.settings.describe()}

    @guard
    def update_setting(self, key, value):
        """更新单项配置（对应命令 update_setting，UC-18）。"""
        result = self.settings.set(key, value)
        self._apply_setting_side_effects(key, result)
        return {"key": key, "value": result}

    @guard
    def set_storage_limit(self, limit, enabled=True):
        """设置容量上限与开关（对应命令 set_storage_limit，UC-04）。

        返回体里的 pendingDelete 告诉界面"新的上限将删除多少条"，
        由界面弹二次确认后再调用 apply_storage_limit。
        """
        pending, _ = self.capacity.enforce_on_limit_change(limit)
        self.settings.set(K_PERSISTENT_LIMIT, int(limit))
        self.settings.set(K_PERSISTENT_LIMIT_ENABLED, bool(enabled))
        removed = self.capacity.apply_limit_change(int(limit)) if enabled else []
        if removed:
            self._emit(EVT_CLIPBOARD_REMOVED, {"ids": removed, "reason": "capacity"})
        return {"limit": int(limit), "enabled": bool(enabled),
                "deleted": len(removed), "pendingDelete": pending}

    @guard
    def preview_storage_limit(self, limit, enabled=True):
        """只计算"改到这个上限会删多少条"，不改动任何数据（UC-04 第 8 步）。"""
        if not enabled:
            return {"pendingDelete": 0, "countable": self.capacity.countable()}
        pending, _ = self.capacity.enforce_on_limit_change(limit)
        return {"pendingDelete": pending, "countable": self.capacity.countable()}

    @guard
    def set_privacy(self, enabled=None, kinds=None):
        """开关隐私保护与敏感类型（对应命令 set_privacy，UC-17）。"""
        state = self.privacy.configure(enabled=enabled, kinds=kinds)
        if enabled is not None:
            self.settings.set(K_PRIVACY_PROTECTION, bool(enabled))
        if kinds is not None:
            self.settings.set(K_PRIVACY_KINDS, list(kinds))
        return state

    @guard
    def set_theme(self, theme_id):
        """切换外观主题（对应命令 set_theme，UC-15）。"""
        self.settings.set(K_THEME, theme_id)
        self._emit(EVT_THEME_CHANGED, {"theme": theme_id,
                                       "mode": self.settings.get(K_THEME_MODE)})
        return {"theme": theme_id}

    @guard
    def set_theme_mode(self, mode):
        """切换深浅色模式（对应命令 set_theme_mode）。"""
        self.settings.set(K_THEME_MODE, mode)
        self._emit(EVT_THEME_CHANGED, {"theme": self.settings.get(K_THEME), "mode": mode})
        return {"mode": mode}

    @guard
    def set_list_density(self, density):
        """设置列表密度（对应命令 set_list_density）。"""
        self.settings.set(K_LIST_DENSITY, density)
        return {"density": density}

    @guard
    def set_hotkey(self, action, accelerator):
        """设置全局快捷键（对应命令 set_hotkey，UC-16）。"""
        mapping = {
            listener_mod.ACTION_MAIN_PANEL: K_MAIN_HOTKEY,
            listener_mod.ACTION_SEARCH: K_SEARCH_HOTKEY,
            listener_mod.ACTION_SEQUENTIAL: K_SEQUENTIAL_HOTKEY,
            listener_mod.ACTION_RICH_PASTE: K_RICH_PASTE_HOTKEY,
        }
        key = mapping.get(action)
        if key is None:
            raise AppError(ERR_INVALID_ARG, "未知的快捷键动作：%s" % action)
        normalized = paste_mod.validate_accelerator(accelerator)
        self.settings.set(key, normalized)
        if self.listener:
            self.listener.set_hotkey(action, normalized)
        return {"action": action, "accelerator": normalized}

    @guard
    def set_recording_mode(self, enabled):
        """开/关"快捷键录制模式"（对应命令 set_recording_mode）。

        录制期间临时注销全部全局快捷键：一旦某个组合被注册，
        Windows 会先把它截走，录制窗口根本收不到那个按键——
        "想录一个当前已注册的快捷键"就永远录不上。
        """
        enabled = bool(enabled)
        if self.listener:
            self.listener.set_recording_mode(enabled)
        return {"recording": enabled}

    @guard
    def relaunch_command(self):
        """只看"重启会执行什么命令"，不真的重启（界面与测试用）。"""
        executable, params = os_integration.relaunch_command()
        return {"executable": executable, "params": params}

    @guard
    def relaunch(self):
        """重新启动本程序（对应命令 relaunch）。

        新进程起来之后再通知界面退出——先退的话新进程可能来不及起来，
        用户会看到"程序自己关了"。
        """
        if not os_integration.relaunch(logger=self.logger):
            raise AppError(ERR_INTERNAL, "重新启动失败，请手动打开程序")
        self._emit(EVT_TOAST, {"type": TOAST_INFO, "message": "正在重启…",
                               "action": "exit"})
        return {"relaunched": True}

    @guard
    def restart_as_admin(self):
        """以管理员身份重启（对应命令 restart_as_admin）。

        Win+V 接管要写 HKLM，必须有管理员权限；没有的话这里给用户
        一条"提权重启"的路，而不是只告诉他"权限不够"。
        """
        if os_integration.is_admin():
            raise AppError(ERR_INVALID_ARG, "当前已经是管理员权限，无需重启")
        if not os_integration.restart_as_admin(logger=self.logger):
            raise AppError(ERR_INTERNAL, "提权重启未完成（可能取消了 UAC 提示）")
        self._emit(EVT_TOAST, {"type": TOAST_INFO, "message": "正在提权重启…",
                               "action": "exit"})
        return {"restarting": True}

    @guard
    def reapply_hotkeys(self):
        """按配置重新注册全部快捷键（UC-16 异常流 E2 的"重新注册"按钮）。"""
        hotkeys = self._configured_hotkeys()
        if self.listener:
            self.listener.reapply_hotkeys(hotkeys)
        return {"hotkeys": hotkeys}

    @guard
    def restart_listener(self):
        """重新注册剪贴板监听（UC-01 异常流 E4 的"重新注册"按钮）。"""
        if self.listener:
            self.listener.stop()
            self.listener = None
        self.start_listener()
        return self.get_listen_status()["data"]

    def _apply_setting_side_effects(self, key, value):
        """配置变更后立刻生效（UC-18 主事件流第 4 步）。"""
        if key == K_PRIVACY_PROTECTION:
            self.privacy.configure(enabled=bool(value))
        elif key == K_PRIVACY_KINDS:
            self.privacy.configure(kinds=value)
        elif key == K_PRIVACY_CUSTOM_RULES:
            self.privacy.configure(custom_rules=value)
        elif key == K_MASK_PREFIX:
            self.privacy.configure(mask_prefix=value)
        elif key == K_MASK_SUFFIX:
            self.privacy.configure(mask_suffix=value)
        elif key == K_MASK_EMAIL_DOMAIN:
            self.privacy.configure(mask_email_domain=value)
        elif key == K_APP_CLEANUP_POLICIES:
            self.app_rules.reload(value)
        elif key == K_PERSISTENT_LIMIT_ENABLED or key == K_PERSISTENT_LIMIT:
            removed = self.capacity.enforce(reason="配置变更")
            if removed:
                self._emit(EVT_CLIPBOARD_REMOVED, {"ids": removed, "reason": "capacity"})

    # ==================================================================
    # 诊断
    # ==================================================================
    @guard
    def get_diagnostics(self):
        """运行状态自检（供"关于/诊断"面板与自动化测试使用）。"""
        return {
            "version": VERSION,
            "dataDir": self.data_dir,
            "dbPath": self.db.db_path,
            "attachmentDir": self.db.attachment_dir,
            "schemaVersion": migrations.CURRENT_VERSION,
            "uptimeMs": now_ms() - self.started_at,
            "pipeline": self.pipeline.stats(),
            "capture": dict(self._stats),
            "listener": self.listener.diagnostics() if self.listener else None,
            "storage": self.capacity.stats(),
            "echoMarkers": self.dedup.marker_count(),
        }

    @guard
    def simulate_capture(self, text, source_name="测试来源", content_type=None):
        """测试辅助：把一段文本当作剪贴板内容走完整条管线。

        供自动化测试使用——测试环境里真实剪贴板往往被别的程序占用，
        且真实复制无法精确控制来源与时间戳。
        """
        from app.domain.models import ClipboardData
        from app.infrastructure.clipboard.source_app import SourceAppInfo
        data = ClipboardData("text", text=text, clipboard_formats=[13])
        info = SourceAppInfo(app_name=source_name)
        ctx = self.capture(data, info)
        return {
            "outcome": ctx.outcome,
            "entry": ctx.entry.to_dict() if ctx.entry else None,
            "reason": ctx.error.message if ctx.error else None,
            "contentType": ctx.content_type,
        }


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _session_match(entry, query, tag_only):
    if not query:
        return True
    needle = query.lower()
    if tag_only:
        return any(needle in tag.lower() for tag in entry.tags)
    haystacks = [entry.content or "", entry.source_app or ""] + list(entry.tags)
    return any(needle in h.lower() for h in haystacks)
