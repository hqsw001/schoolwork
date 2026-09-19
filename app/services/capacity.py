# -*- coding: utf-8 -*-
"""CapacityService：容量上限检查与裁剪（F3 容量管理与存储策略 / UC-04 / UC-05）

这是最容易"误删用户东西"的模块，因此规则写得非常死：

    R1 裁剪顺序严格按时间从旧到新，禁止随机删除或删除最新记录；
    R2 置顶记录与已打标签的记录永不参与自动裁剪；
    R3 裁剪必须与附件清理联动，不留下"数据库没记录、磁盘有孤儿文件"的状态；
    R4 一次入库最多触发一次裁剪。
"""

from app.constants import MIN_STORAGE_LIMIT, TYPE_IMAGE
from app.infrastructure.repository.attachments import attachment_usage
from app.infrastructure.repository.clipboard_repo import human_size


class CapacityService(object):
    def __init__(self, repo, settings, attachment_dir=None, db=None, logger=None):
        self.repo = repo
        self.settings = settings
        self.attachment_dir = attachment_dir
        self.db = db
        self.logger = logger

    # ------------------------------------------------------------ 查询
    def is_limit_enabled(self):
        from app.constants import K_PERSISTENT_LIMIT_ENABLED
        return bool(self.settings.get(K_PERSISTENT_LIMIT_ENABLED))

    def limit(self):
        from app.constants import K_PERSISTENT_LIMIT
        try:
            return int(self.settings.get(K_PERSISTENT_LIMIT))
        except (TypeError, ValueError):
            return 500

    def protected_count(self):
        """已豁免的记录条数（置顶或已打标签）。"""
        row = self.db.conn.execute(
            "SELECT COUNT(*) FROM clipboard_history"
            " WHERE is_pinned = 1 OR (tags <> '[]' AND tags <> '')"
        ).fetchone()
        return row[0]

    def countable(self):
        """参与上限计量的条数 N = 总数 - 豁免数（UC-04 业务规则 R1）。"""
        return self.repo.count() - self.protected_count()

    # ------------------------------------------------------------ 裁剪
    def enforce(self, reason="入库"):
        """执行一次容量检查与裁剪，返回被删除的 id 列表。

        对应 UC-05 主事件流第 2~8 步。
        """
        if not self.is_limit_enabled():
            return []
        limit = self.limit()
        total = self.repo.count()
        protected = self.protected_count()
        countable = total - protected

        if countable <= limit:
            return []

        to_delete = countable - limit
        if to_delete <= 0:
            return []

        # 按时间升序取最旧的 D 条"不参与豁免"的记录（R1）
        rows = self.db.conn.execute(
            "SELECT id, content_type, content FROM clipboard_history"
            " WHERE is_pinned = 0 AND (tags = '[]' OR tags = '')"
            " ORDER BY timestamp ASC, id ASC LIMIT ?",
            (to_delete,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        if not ids:
            return []

        # 逐条删除：先记下附件路径，删记录，再清理独占附件（R3）
        attachment_paths = [
            row["content"] for row in rows if row["content_type"] == TYPE_IMAGE
        ]
        with self.db.transaction() as conn:
            for entry_id in ids:
                conn.execute("DELETE FROM clipboard_history WHERE id = ?", (entry_id,))
                conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
            conn.execute(
                "DELETE FROM saved_tags WHERE name NOT IN (SELECT DISTINCT tag FROM entry_tags)"
            )

        self._cleanup_attachments(attachment_paths)
        if self.logger:
            self.logger.info(
                "容量裁剪（%s）：上限 %d，原可计数 %d 条，删除最旧 %d 条",
                reason, limit, countable, len(ids),
            )
        return ids

    def enforce_on_limit_change(self, new_limit):
        """上限调小后的裁剪（UC-04 主事件流第 7~9 步）。

        返回 (将要删除的条数, 删除后的 id 列表)。界面上"新的上限将删除 N 条"
        的确认框用的是第一个返回值，确认后再调用 apply_limit_change。
        """
        if not self.is_limit_enabled():
            return 0, []
        countable = self.countable()
        pending = max(0, countable - int(new_limit))
        return pending, []

    def apply_limit_change(self, new_limit):
        if self.logger:
            self.logger.info("存储上限调整为 %d，立即执行一次容量检查", new_limit)
        return self.enforce(reason="调整上限")

    def _cleanup_attachments(self, paths):
        """删除附件文件，但只删不再被任何记录引用的（UC-05 备选流 A3）。"""
        if not self.attachment_dir:
            return
        from app.infrastructure.repository import attachments
        for path in paths:
            still_used = self.db.conn.execute(
                "SELECT COUNT(*) FROM clipboard_history WHERE content = ?", (path,)
            ).fetchone()[0]
            if still_used == 0:
                attachments.delete_attachment(path, self.attachment_dir, self.logger)

    # ------------------------------------------------------------ 统计
    def stats(self):
        """容量统计（F3-5 容量统计与占用提示）。"""
        total_bytes, file_count = attachment_usage(self.attachment_dir or "")
        limit = self.limit()
        enabled = self.is_limit_enabled()
        countable = self.countable()
        protected = self.protected_count()
        usage_ratio = (countable / limit) if (enabled and limit) else 0.0
        warning = None
        if enabled and usage_ratio >= 0.9:
            warning = "已使用 %d/%d 条，接近上限，最旧的普通记录将被自动清理" % (countable, limit)
        if enabled and protected > 0 and countable == 0:
            warning = "记录均已豁免（置顶或已打标签），当前占用未受上限约束"
        return {
            "limitEnabled": enabled,
            "limit": limit,
            "minLimit": MIN_STORAGE_LIMIT,
            "totalCount": self.repo.count(),
            "countable": countable,
            "protectedCount": protected,
            "attachmentCount": file_count,
            "attachmentBytes": total_bytes,
            "attachmentHuman": human_size(total_bytes),
            "usageRatio": round(usage_ratio, 4),
            "warning": warning,
        }
