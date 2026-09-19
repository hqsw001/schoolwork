# -*- coding: utf-8 -*-
"""ClipboardPipeline：五阶段处理管线（F1 / F2 / F4 / F6 / F9）

对应《需求分析报告》3.2 节与 6.3.2 顺序图，阶段划分与职责严格照搬
开源参照项目 TieZ 的 ``services/clipboard/pipeline.rs``：

    ① DiscoveryStage       识别类型（UC-02）
    ② TransformationStage  规范化、生成预览、图片外置（UC-02 / F2-3）
    ③ ValidationStage      脱敏判定 + 回声检查 + 指纹查重（UC-17 / UC-06）
    ④ PersistenceStage     事务入库 / 合并刷新（UC-03 / UC-06）
    ⑤ DistributionStage    容量检查 + 推送刷新事件（UC-05 / 7.4 事件）

设计约定：
    - 每个阶段往同一个 PipelineContext 上写结果，任何阶段都可以用
      ``ctx.should_stop = True`` 终止后续阶段（例如判定为回声）；
    - 阶段本身不抛异常给调用方，异常统一收敛成 ctx.error 并终止管线，
      因为"复制了一下导致程序崩了"是最不可接受的体验（UC-01 异常流 E5）。
"""

from app.constants import (
    ERR_INTERNAL,
    K_CAPTURE_RICH_TEXT,
    K_DEDUPLICATE,
    K_PERSISTENT,
    MAX_CONTENT_LEN,
    PREVIEW_MAX_LEN,
    TYPE_IMAGE,
    TYPE_RICH_TEXT,
)
from app.domain.models import ClipboardEntry, make_preview
from app.errors import AppError, now_ms
from app.services import content_utils


class PipelineContext(object):
    """一次采集的处理上下文。"""

    __slots__ = (
        "data", "source_app", "source_app_path", "timestamp", "content_type",
        "content", "html_content", "preview", "content_hash", "entry",
        "should_stop", "outcome", "error", "image_object", "image_bytes",
        "attachment_path", "is_sensitive", "sensitive_kinds", "is_truncated",
        "fingerprint_new", "session_only", "is_echo", "merged",
    )

    def __init__(self, data, source_app=None, source_app_path=None, timestamp=None):
        self.data = data
        self.source_app = getattr(source_app, "app_name", None) or "未知"
        self.source_app_path = getattr(source_app, "process_path", None)
        self.timestamp = timestamp or now_ms()
        self.content_type = None
        self.content = None
        self.html_content = None
        self.preview = ""
        self.content_hash = 0
        self.entry = None
        self.should_stop = False
        #: 处理结果：inserted / merged / echo / skipped / failed
        self.outcome = None
        self.error = None
        self.image_object = None
        self.image_bytes = None
        self.attachment_path = None
        self.is_sensitive = 0
        self.sensitive_kinds = []
        self.is_truncated = 0
        self.fingerprint_new = False
        self.session_only = False
        self.is_echo = False
        self.merged = False

    def stop(self, outcome):
        self.should_stop = True
        self.outcome = outcome

    def __repr__(self):
        return "<PipelineContext %s %s>" % (self.outcome, self.content_type)


class PipelineStage(object):
    """阶段基类。"""

    name = "stage"

    def process(self, ctx):
        raise NotImplementedError


# ---------------------------------------------------------------------------
# ① 类型识别
# ---------------------------------------------------------------------------
class DiscoveryStage(PipelineStage):
    name = "discovery"

    def process(self, ctx):
        content_type, content, html = content_utils.detect_content_type(ctx.data)
        if content_type is None:
            # 系统私有格式或空内容：跳过，不产生记录（UC-01 异常流 E2）
            ctx.stop("skipped")
            return
        ctx.content_type = content_type
        ctx.html_content = html
        if content_type == TYPE_IMAGE:
            ctx.image_bytes = content
            ctx.content = ""      # 图片的 content 在转换阶段落成附件路径
        else:
            ctx.content = content or ""
        # 纯空白内容不产生记录（UC-02 备选流 A4）
        if content_type != TYPE_IMAGE and not ctx.content.strip():
            ctx.stop("skipped")


# ---------------------------------------------------------------------------
# ② 规范化与转换
# ---------------------------------------------------------------------------
class TransformationStage(PipelineStage):
    name = "transformation"

    def __init__(self, settings=None, privacy=None, attachment_dir=None, logger=None):
        self.settings = settings
        self.privacy = privacy
        self.attachment_dir = attachment_dir
        self.logger = logger

    def process(self, ctx):
        if ctx.content_type == TYPE_IMAGE:
            self._process_image(ctx)
        else:
            self._process_text(ctx)

    # ---------------------------------------------------------- 文本
    def _process_text(self, ctx):
        text = ctx.content
        # R3：采集到的原始内容在进入管线前不做任何修改，所有规范化都在这里完成
        text, truncated = _clamp(text, MAX_CONTENT_LEN)
        ctx.is_truncated = 1 if truncated else 0
        text = self._apply_cleanup_rules(text)
        ctx.content = text

        # 富文本：HTML 也做长度保护，并同步一份纯文本作为降级内容
        if ctx.content_type == TYPE_RICH_TEXT:
            if ctx.html_content:
                ctx.html_content, _ = _clamp(ctx.html_content, MAX_CONTENT_LEN)
            if not (ctx.content or "").strip() and ctx.html_content:
                # HTML 解析不出可读文字时，用固定文案兜底（UC-02 异常流 E3）
                ctx.content = content_utils.strip_html(ctx.html_content) or "富文本内容"
            if not self.settings.get(K_CAPTURE_RICH_TEXT, True):
                # 关闭富文本采集：降级为纯文本，内容不丢
                ctx.content_type = content_utils.detect_text_type(ctx.content)
                ctx.html_content = None

        # 预览：先脱敏再折叠截断（敏感内容绝不能以明文出现在列表里）
        preview_source = ctx.content
        if self.privacy is not None and self.privacy.enabled:
            hits = self.privacy.scan(ctx.content)
            if hits:
                ctx.is_sensitive = 1
                ctx.sensitive_kinds = _unique([h.kind for h in hits])
                preview_source = self.privacy.mask_preview(ctx.content)
        ctx.preview = make_preview(preview_source, PREVIEW_MAX_LEN)
        if ctx.is_truncated:
            ctx.preview = (ctx.preview + " …（内容过长已截断）")[:PREVIEW_MAX_LEN + 20]
        if not ctx.preview and ctx.content:
            ctx.preview = ctx.content[:100]

    # ---------------------------------------------------------- 图片
    def _process_image(self, ctx):
        """图片处理：解码 → 计算像素指纹 → 外置为附件 → content 存路径（F2-3）。"""
        from io import BytesIO
        from PIL import Image
        from app.infrastructure.clipboard import image_utils
        from app.infrastructure.repository import attachments

        raw = bytes(ctx.image_bytes or b"")
        if not raw:
            raise AppError(ERR_INTERNAL, "剪贴板位图为空")
        if _looks_like_png(raw):
            image = Image.open(BytesIO(raw))
            image.load()
        else:
            # 采集阶段交进来的就是原始 DIB（CF_DIB / CF_DIBV5）
            image = image_utils.dib_to_image(raw)
        ctx.image_object = image
        ctx.content_hash = image_utils.image_hash(image)
        png_bytes = image_utils.image_to_png_bytes(image)
        ctx.image_bytes = png_bytes
        if self.attachment_dir:
            path, _created = attachments.save_image_attachment(
                self.attachment_dir, png_bytes, ctx.content_hash, self.logger
            )
            ctx.attachment_path = path
            ctx.content = path
        else:
            ctx.content = ""
        ctx.preview = "[图片] %d × %d" % image.size

    def _apply_cleanup_rules(self, text):
        """入库前的文本清理规则（配置项 app.cleanup_rules）。

        格式为多条 "正则=>替换" 规则，用换行或 || 分隔，默认配置为空。
        规则写错时忽略该条并记日志，绝不让一次错误配置挡住所有复制。
        """
        raw = (self.settings.get("app.cleanup_rules") or "").strip() if self.settings else ""
        if not raw:
            return text
        import re
        result = text
        for line in re.split(r"[\n|]{2,}|\n", raw):
            line = line.strip()
            if not line or "=>" not in line:
                continue
            pattern, _, replacement = line.partition("=>")
            try:
                result = re.sub(pattern.strip(), replacement.strip(), result)
            except re.error as exc:
                if self.logger:
                    self.logger.warning("清理规则 %r 无效，已忽略：%s", line, exc)
        return result


# ---------------------------------------------------------------------------
# ③ 校验：回声抑制 + 指纹查重
# ---------------------------------------------------------------------------
class ValidationStage(PipelineStage):
    name = "validation"

    def __init__(self, dedup, repo, settings=None, logger=None):
        self.dedup = dedup
        self.repo = repo
        self.settings = settings
        self.logger = logger

    def process(self, ctx):
        # 计算指纹（F6-1 / F6-2）
        if ctx.content_type == TYPE_IMAGE:
            if not ctx.content_hash:
                ctx.content_hash = self.dedup.fingerprint(
                    TYPE_IMAGE, ctx.content, image=ctx.image_object
                )
        else:
            ctx.content_hash = self.dedup.fingerprint(ctx.content_type, ctx.content)

        # ---- 回声抑制（F1-6）：必须在查重之前判定，
        # 因为"自己粘贴出来的内容"往往正好和库里那条重复，
        # 如果先查重就会把它当成一次合并刷新，使用次数被自己刷高。
        normalized = content_utils.normalize_for_compare(ctx.content) \
            if ctx.content_type != TYPE_IMAGE else None
        if self.dedup.is_echo(
            fingerprint=ctx.content_hash,
            kind="image" if ctx.content_type == TYPE_IMAGE else "text",
            raw_text=ctx.content if ctx.content_type != TYPE_IMAGE else None,
            normalized_text=normalized,
            content_type=ctx.content_type,
        ):
            ctx.is_echo = True
            ctx.stop("echo")
            return

        # ---- 重复判定（UC-06 主事件流第 1~6 步）
        if not self.settings.get(K_DEDUPLICATE, True):
            return
        existing = self.repo.find_duplicate(ctx.content_type, ctx.content_hash)
        if existing is None:
            ctx.fingerprint_new = True
            return
        # 文本类额外做一次规范化比对（UC-06 第 4 步）；
        # 富文本 HTML 不同但纯文本相同时，只在 10 秒窗口内判定为重复
        # （UC-06 备选流 A1），避免把用户刻意保留的两种排版误合并。
        if ctx.content_type != TYPE_IMAGE and not self.dedup.is_same_content(
            ctx.content_type, existing, ctx.content
        ):
            ctx.fingerprint_new = True
            return
        ctx.entry = existing
        ctx.merged = True


# ---------------------------------------------------------------------------
# ④ 入库
# ---------------------------------------------------------------------------
class PersistenceStage(PipelineStage):
    name = "persistence"

    def __init__(self, repo, settings=None, session_store=None, logger=None):
        self.repo = repo
        self.settings = settings
        self.session_store = session_store
        self.logger = logger

    def process(self, ctx):
        persistent = bool(self.settings.get(K_PERSISTENT, True)) if self.settings else True
        ctx.session_only = not persistent

        if ctx.merged and ctx.entry is not None:
            # 合并刷新：不新增行，刷新时间戳 + 使用次数 + 来源（UC-06 第 7 步）
            if persistent:
                updated = self.repo.touch_entry(
                    ctx.entry.id, timestamp=ctx.timestamp,
                    use_count_delta=1, source_app=ctx.source_app,
                    source_app_path=ctx.source_app_path,
                )
                # 敏感标记在合并时也要保留
                ctx.entry = updated
            else:
                self._merge_in_session(ctx)
            ctx.stop("merged")
            return

        entry = ClipboardEntry(
            id=0,
            content_type=ctx.content_type,
            content=ctx.content,
            html_content=ctx.html_content,
            source_app=ctx.source_app,
            source_app_path=ctx.source_app_path,
            timestamp=ctx.timestamp,
            preview=ctx.preview,
            is_pinned=0,
            pinned_order=0,
            tags=[],
            use_count=0,
            content_hash=ctx.content_hash,
            is_external=1 if ctx.content_type == TYPE_IMAGE else 0,
            is_sensitive=ctx.is_sensitive,
            is_truncated=ctx.is_truncated,
            attachment_path=ctx.attachment_path,
        )
        if persistent:
            entry.id = self.repo.save(entry)
        else:
            # 未开启持久化：只进会话内存列表，上限 500 条（UC-03 备选流 A1）
            entry.id = self.session_store.add(entry) if self.session_store else -(len(ctx.content) + 1)
        ctx.entry = entry
        ctx.stop("inserted")

    def _merge_in_session(self, ctx):
        if self.session_store is None:
            return
        self.session_store.touch(ctx.entry.id, ctx.timestamp, ctx.source_app)


# ---------------------------------------------------------------------------
# ⑤ 分发：容量检查 + 事件
# ---------------------------------------------------------------------------
class DistributionStage(PipelineStage):
    name = "distribution"

    def __init__(self, capacity=None, emit=None, logger=None):
        self.capacity = capacity
        self.emit = emit
        self.logger = logger

    def process(self, ctx):
        session_only = ctx.session_only
        if ctx.outcome == "inserted" and not session_only and self.capacity is not None:
            # 入库完成后触发一次容量检查（UC-03 第 9 步 / UC-05 第 1 步）
            removed = self.capacity.enforce(reason="入库")
            if removed and self.emit:
                self.emit("clipboard-removed", {"ids": removed, "reason": "capacity"})
        if self.emit and ctx.entry is not None:
            self.emit("clipboard-updated", {
                "entry": ctx.entry.to_dict(),
                "outcome": ctx.outcome,
                "merged": ctx.merged,
                "sessionOnly": session_only,
            })


# ---------------------------------------------------------------------------
# 管线
# ---------------------------------------------------------------------------
class ClipboardPipeline(object):
    """五阶段管线。

    阶段序列里**不包含**分发阶段：分发必须在"任何结果下都执行一次"，
    而 should_stop 的语义是"跳过后续处理阶段"。把它放进 stages 列表里，
    入库阶段一旦 stop，容量检查与事件推送就永远不会发生——
    这是本项目实测踩到过的一个真实缺陷，故在此固定下来。
    """

    def __init__(self, stages=None, distribution=None):
        self.stages = list(stages or [])
        #: 分发阶段：容量检查 + 事件推送，无论前面结果如何都执行
        self.distribution = distribution
        self._stats = {"processed": 0, "inserted": 0, "merged": 0, "echo": 0,
                       "skipped": 0, "failed": 0}

    def execute(self, ctx):
        """执行全部阶段，返回 ctx。任何阶段异常都收敛为 ctx.error。"""
        self._stats["processed"] += 1
        for stage in self.stages:
            if ctx.should_stop:
                break
            try:
                stage.process(ctx)
            except AppError as exc:
                ctx.error = exc
                ctx.stop("failed")
                if self.logger:
                    self.logger.error("管线阶段 %s 失败：%s", stage.name, exc)
                break
            except Exception as exc:  # noqa: BLE001
                ctx.error = AppError(ERR_INTERNAL, "处理失败：%s" % exc)
                ctx.stop("failed")
                if self.logger:
                    self.logger.exception("管线阶段 %s 未预期异常", stage.name)
                break
        ctx.outcome = ctx.outcome or "processed"

        # 分发：容量检查与事件推送（UC-03 第 9~11 步 / UC-05 第 1 步 / 7.4 事件）
        if self.distribution is not None and ctx.outcome in ("inserted", "merged"):
            try:
                self.distribution.process(ctx)
            except Exception as exc:  # noqa: BLE001 - 推送失败不应回滚已入库的数据
                if self.logger:
                    self.logger.exception("分发阶段异常：%s", exc)

        self._stats[ctx.outcome] = self._stats.get(ctx.outcome, 0) + 1
        return ctx

    @property
    def logger(self):
        for stage in self.stages:
            if getattr(stage, "logger", None) is not None:
                return stage.logger
        return None

    def stats(self):
        return dict(self._stats)


# ---------------------------------------------------------------------------
# 会话内存列表（未开启持久化时使用）
# ---------------------------------------------------------------------------
class SessionStore(object):
    """会话内内存记录列表，上限 500 条（UC-03 备选流 A1）。"""

    MAX_ITEMS = 500

    def __init__(self):
        self._items = []
        self._next_id = -1

    def add(self, entry):
        entry.id = self._next_id
        self._next_id -= 1
        self._items.insert(0, entry)
        if len(self._items) > self.MAX_ITEMS:
            del self._items[self.MAX_ITEMS:]
        return entry.id

    def touch(self, entry_id, timestamp, source_app=None):
        for entry in self._items:
            if entry.id == entry_id:
                entry.timestamp = timestamp
                entry.use_count += 1
                if source_app:
                    entry.source_app = source_app
                self._items.remove(entry)
                self._items.insert(0, entry)
                return entry
        return None

    def find_duplicate(self, content_type, content_hash):
        for entry in self._items:
            if entry.content_type == content_type and entry.content_hash == content_hash:
                return entry
        return None

    def list(self, limit=50, offset=0, content_type=None):
        items = self._items
        if content_type:
            items = [e for e in items if e.content_type == content_type]
        return items[offset:offset + limit]

    def count(self):
        return len(self._items)

    def clear(self):
        removed = [e.id for e in self._items]
        self._items = []
        return removed

    def remove(self, entry_id):
        for entry in list(self._items):
            if entry.id == entry_id:
                self._items.remove(entry)
                return True
        return False


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _clamp(text, limit):
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _unique(items):
    out = []
    for item in items:
        if item not in out:
            out.append(item)
    return out


def _looks_like_png(data):
    return bool(data) and bytes(data[:8]) == b"\x89PNG\r\n\x1a\n"


def _looks_like_dib(data):
    """DIB 头长度特征：前 4 字节是一个合理的结构体长度。"""
    if not data or len(data) < 40:
        return False
    import struct
    header_size = struct.unpack_from("<I", bytes(data[:4]), 0)[0]
    return header_size in (12, 40, 52, 56, 108, 124)
