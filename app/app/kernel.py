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

import queue
import threading

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
    K_CAPTURE_FILES,
    K_CAPTURE_RICH_TEXT,
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
    PAGE_SIZE,
    SEARCH_RESULT_LIMIT,
    TOAST_ERROR,
    TOAST_INFO,
    TOAST_WARNING,
    TYPE_IMAGE,
    TYPE_LABELS,
    is_text_type,
)
from app.domain.models import make_preview
from app.errors import AppError, guard, now_ms
from app.infrastructure.clipboard import clipboard_io
from app.infrastructure.clipboard import image_utils
from app.infrastructure.clipboard import listener as listener_mod
from app.infrastructure.clipboard import paste as paste_mod
from app.infrastructure.clipboard import source_app as source_mod
from app.infrastructure.database import Database, default_data_dir
from app.infrastructure.logger import setup_logger
from app.infrastructure.repository import migrations
from app.infrastructure.repository.clipboard_repo import ClipboardRepository
from app.infrastructure.repository.settings_repo import SettingsRepository
from app.services import content_utils
from app.services.capacity import CapacityService
from app.services.dedup import DedupService
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
        )
        self.dedup = DedupService(self.logger)
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
                    attachment_dir=self.db.attachment_dir, logger=self.logger,
                ),
                ValidationStage(self.dedup, self.repo, self.settings, self.logger),
                PersistenceStage(self.repo, self.settings, self.session_store, self.logger),
            ],
            distribution=DistributionStage(self.capacity, self._emit, self.logger),
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
        except Exception as exc:  # noqa: BLE001 - 清理失败不影响启动
            self.logger.warning("孤儿附件清理失败：%s", exc)
        self.logger.info("%s v%s 内核已启动，数据目录 %s", APP_NAME, VERSION, self.data_dir)
        return True

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

    # ------------------------------------------------------------ 监听
    def start_listener(self):
        if self.listener and self.listener.is_running():
            return True
        hotkeys = {
            listener_mod.ACTION_MAIN_PANEL: self.settings.get(K_MAIN_HOTKEY),
            listener_mod.ACTION_SEARCH: self.settings.get(K_SEARCH_HOTKEY),
        }
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
        for key in (K_MAIN_HOTKEY, K_SEARCH_HOTKEY):
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
        return ctx

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
        """为记录添加标签（UC-11）。"""
        updated = self.repo.add_tag(int(entry_id), tag)
        self._emit(EVT_CLIPBOARD_UPDATED, {"entry": updated.to_dict(), "outcome": "tagged"})
        return updated.to_dict()

    @guard
    def remove_tag(self, entry_id, tag):
        updated = self.repo.remove_tag(int(entry_id), tag)
        self._emit(EVT_CLIPBOARD_UPDATED, {"entry": updated.to_dict(), "outcome": "untagged"})
        return updated.to_dict()

    @guard
    def list_tags(self):
        """列出全部标签及颜色（对应命令 list_tags）。"""
        return {"items": self.repo.list_tags()}

    @guard
    def upsert_tag_color(self, tag, color):
        return self.repo.set_tag_color(tag, color)

    @guard
    def delete_tag(self, tag):
        return self.repo.delete_tag(tag)

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

    def _write_entry_to_clipboard(self, entry, rich_paste=True):
        """按内容类型写入对应格式（UC-09 主事件流第 3 步）。"""
        content_type = entry.content_type
        if content_type == TYPE_IMAGE:
            with open(entry.content, "rb") as fh:
                clipboard_io.write_image(fh.read())
            return
        if content_type == "file":
            paths = [line for line in (entry.content or "").split("\n") if line.strip()]
            if not paths:
                raise AppError(ERR_INVALID_ARG, "该记录没有可粘贴的文件路径")
            clipboard_io.write_files(paths)
            return
        if content_type == "rich_text" and rich_paste and entry.html_content:
            clipboard_io.write_text_and_html(entry.content, entry.html_content)
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
        key = K_MAIN_HOTKEY if action == listener_mod.ACTION_MAIN_PANEL else K_SEARCH_HOTKEY
        normalized = paste_mod.validate_accelerator(accelerator)
        self.settings.set(key, normalized)
        if self.listener:
            self.listener.set_hotkey(action, normalized)
        return {"action": action, "accelerator": normalized}

    @guard
    def reapply_hotkeys(self):
        """按配置重新注册全部快捷键（UC-16 异常流 E2 的"重新注册"按钮）。"""
        hotkeys = {
            listener_mod.ACTION_MAIN_PANEL: self.settings.get(K_MAIN_HOTKEY),
            listener_mod.ACTION_SEARCH: self.settings.get(K_SEARCH_HOTKEY),
        }
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
