# -*- coding: utf-8 -*-
"""ClipboardRepository：记录表与标签表的增删改查（仓储类）

对应 Rust 版的 ``SqliteClipboardRepository``。所有写操作走事务，
读操作直接查（WAL 模式下读不阻塞写）。

列表排序规则固定为（UC-10 业务规则 R2，所有查询路径一致）：

    置顶优先 → 置顶顺序倒序 → 时间倒序 → id 倒序
"""

import json

from app.constants import (
    ERR_NOT_FOUND,
    PAGE_SIZE,
    SEARCH_RESULT_LIMIT,
    TAG_NAME_MAX_LEN,
    TYPE_IMAGE,
)
from app.domain.models import ClipboardEntry
from app.errors import AppError, now_ms
from app.infrastructure.repository import attachments

_ORDER_BY = (
    " ORDER BY is_pinned DESC, pinned_order DESC, timestamp DESC, id DESC"
)

_SELECT_FIELDS = (
    "id, content_type, content, html_content, source_app, source_app_path, "
    "timestamp, preview, is_pinned, pinned_order, tags, use_count, "
    "content_hash, is_external, is_sensitive"
)


class ClipboardRepository(object):
    def __init__(self, db, logger=None):
        self.db = db
        self.logger = logger

    # ================================================================ 写入
    def save(self, entry, attachment_dir=None):
        """插入或更新一条记录，返回记录 id（UC-03 主事件流第 5~8 步）。

        entry.id == 0 时插入并回填自增主键；entry.id > 0 时更新
        （用于 UC-06 的合并场景）。
        """
        entry.validate()
        is_new = not entry.id
        with self.db.transaction() as conn:
            if is_new:
                cursor = conn.execute(
                    "INSERT INTO clipboard_history ("
                    "  content_type, content, html_content, source_app, source_app_path,"
                    "  timestamp, preview, is_pinned, pinned_order, tags, use_count,"
                    "  content_hash, is_external, is_sensitive"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry.content_type, entry.content, entry.html_content,
                        entry.source_app, entry.source_app_path, entry.timestamp,
                        entry.preview, entry.is_pinned, entry.pinned_order,
                        json.dumps(entry.tags, ensure_ascii=False), entry.use_count,
                        entry.content_hash, entry.is_external, entry.is_sensitive,
                    ),
                )
                entry.id = cursor.lastrowid
            else:
                conn.execute(
                    "UPDATE clipboard_history SET"
                    "  content_type = ?, content = ?, html_content = ?,"
                    "  source_app = ?, source_app_path = ?, timestamp = ?,"
                    "  preview = ?, is_pinned = ?, pinned_order = ?, tags = ?,"
                    "  use_count = ?, content_hash = ?, is_external = ?,"
                    "  is_sensitive = ?"
                    " WHERE id = ?",
                    (
                        entry.content_type, entry.content, entry.html_content,
                        entry.source_app, entry.source_app_path, entry.timestamp,
                        entry.preview, entry.is_pinned, entry.pinned_order,
                        json.dumps(entry.tags, ensure_ascii=False), entry.use_count,
                        entry.content_hash, entry.is_external, entry.is_sensitive,
                        entry.id,
                    ),
                )
            self._sync_tags(conn, entry)
        return entry.id

    def _sync_tags(self, conn, entry):
        """同步记录与标签的关联，并去除重复标签（UC-03 第 7 步）。"""
        seen = []
        for tag in entry.tags:
            name = (tag or "").strip()
            if name and name not in seen:
                seen.append(name)
        entry.tags = seen
        conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry.id,))
        for name in seen:
            conn.execute(
                "INSERT OR IGNORE INTO saved_tags (name, color) VALUES (?, NULL)", (name,)
            )
            conn.execute(
                "INSERT OR IGNORE INTO entry_tags (entry_id, tag) VALUES (?, ?)",
                (entry.id, name),
            )

    def touch_entry(self, entry_id, timestamp=None, use_count_delta=1, source_app=None,
                    source_app_path=None):
        """合并命中时刷新：更新时间戳、使用次数加一、更新来源（UC-06 第 7 步）。

        保留原有的置顶状态、置顶顺序与标签集合（UC-06 业务规则 R2）。
        """
        timestamp = timestamp or now_ms()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT use_count FROM clipboard_history WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                raise AppError(ERR_NOT_FOUND, "记录不存在", str(entry_id))
            if source_app:
                conn.execute(
                    "UPDATE clipboard_history SET timestamp = ?, use_count = use_count + ?,"
                    " source_app = ?, source_app_path = ? WHERE id = ?",
                    (timestamp, use_count_delta, source_app, source_app_path, entry_id),
                )
            else:
                conn.execute(
                    "UPDATE clipboard_history SET timestamp = ?, use_count = use_count + ?"
                    " WHERE id = ?",
                    (timestamp, use_count_delta, entry_id),
                )
        return self.get_entry(entry_id)

    def update_content(self, entry_id, content, preview=None, content_hash=None,
                       content_type=None, clear_html=False):
        """编辑记录内容并重算指纹（UC-12 主事件流第 4~7 步）。"""
        from app.domain.models import make_preview
        preview = preview if preview is not None else make_preview(content)
        fields = ["content = ?", "preview = ?"]
        params = [content, preview]
        if content_hash is not None:
            fields.append("content_hash = ?")
            params.append(content_hash)
        if content_type is not None:
            fields.append("content_type = ?")
            params.append(content_type)
        if clear_html:
            fields.append("html_content = NULL")
        params.append(entry_id)
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE clipboard_history SET %s WHERE id = ?" % ", ".join(fields),
                params,
            )
            if cursor.rowcount == 0:
                raise AppError(ERR_NOT_FOUND, "该记录已不存在", str(entry_id))
        return self.get_entry(entry_id)

    def mark_sensitive(self, entry_id, is_sensitive):
        """设置或取消敏感标记（UC-17 备选流 A4 / A5）。"""
        with self.db.transaction() as conn:
            cursor = conn.execute(
                "UPDATE clipboard_history SET is_sensitive = ? WHERE id = ?",
                (1 if is_sensitive else 0, entry_id),
            )
            if cursor.rowcount == 0:
                raise AppError(ERR_NOT_FOUND, "记录不存在", str(entry_id))

    # ================================================================ 读取
    def get_entry(self, entry_id):
        row = self.db.conn.execute(
            "SELECT %s FROM clipboard_history WHERE id = ?" % _SELECT_FIELDS, (entry_id,)
        ).fetchone()
        if row is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在", str(entry_id))
        return ClipboardEntry.from_row(row)

    def find_by_id(self, entry_id):
        row = self.db.conn.execute(
            "SELECT %s FROM clipboard_history WHERE id = ?" % _SELECT_FIELDS, (entry_id,)
        ).fetchone()
        return ClipboardEntry.from_row(row) if row else None

    def get_history(self, limit=PAGE_SIZE, offset=0, content_type=None, source_app=None,
                    pinned_only=False):
        """分页读取历史，可按类型/来源过滤（F4-2 / F4-3）。"""
        sql = "SELECT %s FROM clipboard_history WHERE 1 = 1" % _SELECT_FIELDS
        params = []
        if content_type:
            sql += " AND content_type = ?"
            params.append(content_type)
        if source_app:
            sql += " AND source_app = ?"
            params.append(source_app)
        if pinned_only:
            sql += " AND is_pinned = 1"
        sql += _ORDER_BY + " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset)])
        rows = self.db.conn.execute(sql, params).fetchall()
        return [ClipboardEntry.from_row(r) for r in rows]

    def search(self, query, limit=SEARCH_RESULT_LIMIT, tag_only=False, content_type=None,
               source_app=None):
        """检索（UC-08 主事件流第 5~10 步）。

        普通模式三段并集：正文 / 来源应用 / 标签；
        仅标签模式只匹配标签。
        """
        query = (query or "").strip()
        if not query:
            return self.get_history(limit=limit, content_type=content_type,
                                    source_app=source_app)
        # 转义 SQL 通配符，避免用户输入的 % 与 _ 把结果集异常放大（UC-08 异常流 E1）
        escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = "%" + escaped + "%"
        sql = "SELECT %s FROM clipboard_history WHERE (" % _SELECT_FIELDS
        params = []
        if tag_only:
            sql += ("tags LIKE ? ESCAPE '\\' OR id IN ("
                    "SELECT entry_id FROM entry_tags WHERE tag LIKE ? ESCAPE '\\'))")
            params.extend([like, like])
        else:
            sql += ("content LIKE ? ESCAPE '\\' OR source_app LIKE ? ESCAPE '\\'"
                    " OR tags LIKE ? ESCAPE '\\' OR id IN ("
                    "SELECT entry_id FROM entry_tags WHERE tag LIKE ? ESCAPE '\\'))")
            params.extend([like, like, like, like])
        if content_type:
            sql += " AND content_type = ?"
            params.append(content_type)
        if source_app:
            sql += " AND source_app = ?"
            params.append(source_app)
        sql += _ORDER_BY + " LIMIT ?"
        params.append(int(limit))
        rows = self.db.conn.execute(sql, params).fetchall()
        return [ClipboardEntry.from_row(r) for r in rows]

    def count(self):
        return self.db.conn.execute(
            "SELECT COUNT(*) FROM clipboard_history"
        ).fetchone()[0]

    def count_by_type(self):
        """各类型条数（F4-5 类型计数）。"""
        rows = self.db.conn.execute(
            "SELECT content_type, COUNT(*) AS n FROM clipboard_history GROUP BY content_type"
        ).fetchall()
        counts = {row["content_type"]: row["n"] for row in rows}
        return counts

    def list_sources(self):
        """来源应用去重列表，供来源筛选下拉（F4-3）。"""
        rows = self.db.conn.execute(
            "SELECT source_app, COUNT(*) AS n FROM clipboard_history"
            " GROUP BY source_app ORDER BY n DESC"
        ).fetchall()
        return [{"name": row["source_app"], "count": row["n"]} for row in rows]

    def find_duplicate(self, content_type, content_hash):
        """按"类型 + 指纹"查重复记录（UC-06 主事件流第 3 步）。

        返回最新的一条（时间倒序第一条），命中后由调用方决定是否合并。
        """
        row = self.db.conn.execute(
            "SELECT %s FROM clipboard_history WHERE content_type = ? AND content_hash = ?"
            % _SELECT_FIELDS + _ORDER_BY + " LIMIT 1",
            (content_type, int(content_hash)),
        ).fetchone()
        return ClipboardEntry.from_row(row) if row else None

    # ================================================================ 删除
    def delete(self, entry_id, attachment_dir=None):
        """删除单条记录并联动清理其独占的附件（UC-13 第 4~6 步）。"""
        entry = self.find_by_id(entry_id)
        if entry is None:
            return False
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM clipboard_history WHERE id = ?", (entry_id,))
            conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
            conn.execute(
                "DELETE FROM saved_tags WHERE name NOT IN (SELECT DISTINCT tag FROM entry_tags)"
            )
        self._cleanup_orphan_attachment(entry, attachment_dir)
        return True

    def delete_many(self, ids, attachment_dir=None):
        """批量删除（UC-13 备选流 A2）。"""
        removed = []
        for entry_id in ids or []:
            if self.delete(entry_id, attachment_dir):
                removed.append(entry_id)
        return removed

    def clear(self, keep_protected=True, attachment_dir=None):
        """清空历史：只清理未置顶且无标签的记录（UC-13 第 8 步 / 业务规则 R3）。

        返回被删除的 id 列表。
        """
        if keep_protected:
            rows = self.db.conn.execute(
                "SELECT id FROM clipboard_history"
                " WHERE is_pinned = 0 AND (tags = '[]' OR tags = '')"
            ).fetchall()
        else:
            rows = self.db.conn.execute("SELECT id FROM clipboard_history").fetchall()
        ids = [row["id"] for row in rows]
        with self.db.transaction() as conn:
            for entry_id in ids:
                conn.execute("DELETE FROM clipboard_history WHERE id = ?", (entry_id,))
                conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
            conn.execute(
                "DELETE FROM saved_tags WHERE name NOT IN (SELECT DISTINCT tag FROM entry_tags)"
            )
        # 事务提交后再清理附件：事务若回滚，文件不能已经没了
        if attachment_dir and ids:
            self.cleanup_orphan_attachments(attachment_dir)
        return ids

    def cleanup_by_age(self, age_seconds, attachment_dir=None):
        """按时间清理：删除早于 age_seconds 的记录，豁免置顶与已打标签（F3-4）。"""
        cutoff = now_ms() - int(age_seconds) * 1000
        rows = self.db.conn.execute(
            "SELECT id FROM clipboard_history"
            " WHERE timestamp < ? AND is_pinned = 0 AND (tags = '[]' OR tags = '')",
            (cutoff,),
        ).fetchall()
        ids = [row["id"] for row in rows]
        with self.db.transaction() as conn:
            for entry_id in ids:
                conn.execute("DELETE FROM clipboard_history WHERE id = ?", (entry_id,))
                conn.execute("DELETE FROM entry_tags WHERE entry_id = ?", (entry_id,))
            conn.execute(
                "DELETE FROM saved_tags WHERE name NOT IN (SELECT DISTINCT tag FROM entry_tags)"
            )
        if attachment_dir:
            self.cleanup_orphan_attachments(attachment_dir)
        return ids

    def _cleanup_orphan_attachment(self, entry, attachment_dir):
        """删除记录后，清理仅被本条引用的附件（UC-13 业务规则 R2）。"""
        if not attachment_dir or not entry.attachment_path:
            return
        still_used = self.db.conn.execute(
            "SELECT COUNT(*) FROM clipboard_history WHERE content = ?",
            (entry.attachment_path,),
        ).fetchone()[0]
        if still_used == 0:
            attachments.delete_attachment(entry.attachment_path, attachment_dir, self.logger)

    def cleanup_orphan_attachments(self, attachment_dir):
        """扫描并删除孤儿附件文件（UC-20 主事件流第 5 步）。"""
        rows = self.db.conn.execute(
            "SELECT content FROM clipboard_history WHERE content_type = ?", (TYPE_IMAGE,)
        ).fetchall()
        orphans = attachments.find_orphan_attachments(
            attachment_dir, [row["content"] for row in rows]
        )
        for path in orphans:
            attachments.delete_attachment(path, attachment_dir, self.logger)
        if orphans and self.logger:
            self.logger.info("清理孤儿附件 %d 个", len(orphans))
        return orphans

    def all_attachment_paths(self):
        rows = self.db.conn.execute(
            "SELECT content FROM clipboard_history WHERE content_type = ?", (TYPE_IMAGE,)
        ).fetchall()
        return [row["content"] for row in rows]

    # ================================================================ 组织
    def toggle_pin(self, entry_id, is_pinned):
        """置顶 / 取消置顶（UC-10 主事件流）。

        置顶顺序取当前置顶记录的最大值加一，使新置顶出现在置顶区最前面；
        取消置顶时顺序归零，记录回到普通区按时间戳落位。
        """
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM clipboard_history WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                raise AppError(ERR_NOT_FOUND, "记录不存在", str(entry_id))
            if is_pinned:
                max_order = conn.execute(
                    "SELECT COALESCE(MAX(pinned_order), 0) FROM clipboard_history"
                    " WHERE is_pinned = 1"
                ).fetchone()[0]
                conn.execute(
                    "UPDATE clipboard_history SET is_pinned = 1, pinned_order = ? WHERE id = ?",
                    (int(max_order) + 1, entry_id),
                )
            else:
                conn.execute(
                    "UPDATE clipboard_history SET is_pinned = 0, pinned_order = 0 WHERE id = ?",
                    (entry_id,),
                )
        return self.get_entry(entry_id)

    def update_pinned_order(self, orders):
        """保存置顶项的拖拽排序（UC-10 备选流 A1）。orders 为 [(id, order), ...]。"""
        with self.db.transaction() as conn:
            for entry_id, order in orders:
                conn.execute(
                    "UPDATE clipboard_history SET pinned_order = ? WHERE id = ? AND is_pinned = 1",
                    (int(order), int(entry_id)),
                )
        return len(orders)

    # ================================================================ 标签
    def add_tag(self, entry_id, tag, color=None):
        """为记录添加标签（UC-11 主事件流第 4~5 步）。"""
        name = self.validate_tag_name(tag)
        entry = self.get_entry(entry_id)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO saved_tags (name, color) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET color = COALESCE(excluded.color, saved_tags.color)",
                (name, color),
            )
            conn.execute(
                "INSERT OR IGNORE INTO entry_tags (entry_id, tag) VALUES (?, ?)",
                (entry_id, name),
            )
            members = [row["tag"] for row in conn.execute(
                "SELECT tag FROM entry_tags WHERE entry_id = ? ORDER BY tag", (entry_id,)
            ).fetchall()]
            conn.execute(
                "UPDATE clipboard_history SET tags = ? WHERE id = ?",
                (json.dumps(members, ensure_ascii=False), entry_id),
            )
        entry.tags = members
        return entry

    def remove_tag(self, entry_id, tag):
        """移除记录的标签（UC-11 主事件流第 7 步）。"""
        with self.db.transaction() as conn:
            conn.execute(
                "DELETE FROM entry_tags WHERE entry_id = ? AND tag = ?", (entry_id, tag)
            )
            members = [row["tag"] for row in conn.execute(
                "SELECT tag FROM entry_tags WHERE entry_id = ? ORDER BY tag", (entry_id,)
            ).fetchall()]
            conn.execute(
                "UPDATE clipboard_history SET tags = ? WHERE id = ?",
                (json.dumps(members, ensure_ascii=False), entry_id),
            )
            conn.execute(
                "DELETE FROM saved_tags WHERE name = ? AND name NOT IN"
                " (SELECT DISTINCT tag FROM entry_tags)",
                (tag,),
            )
        entry = self.get_entry(entry_id)
        return entry

    def list_tags(self):
        """列出全部标签及其使用次数（对应命令 list_tags）。"""
        rows = self.db.conn.execute(
            "SELECT t.name AS name, t.color AS color,"
            " (SELECT COUNT(*) FROM entry_tags e WHERE e.tag = t.name) AS n"
            " FROM saved_tags t ORDER BY n DESC, t.name ASC"
        ).fetchall()
        return [{"name": row["name"], "color": row["color"], "count": row["n"]} for row in rows]

    def set_tag_color(self, tag, color):
        name = self.validate_tag_name(tag)
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO saved_tags (name, color) VALUES (?, ?)"
                " ON CONFLICT(name) DO UPDATE SET color = excluded.color",
                (name, color),
            )
        return {"name": name, "color": color}

    def delete_tag(self, tag):
        """删除标签并解除全部关联（UC-11 主事件流第 9 步）。"""
        with self.db.transaction() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM entry_tags WHERE tag = ?", (tag,)
            ).fetchone()[0]
            affected = [row["entry_id"] for row in conn.execute(
                "SELECT entry_id FROM entry_tags WHERE tag = ?", (tag,)
            ).fetchall()]
            conn.execute("DELETE FROM entry_tags WHERE tag = ?", (tag,))
            conn.execute("DELETE FROM saved_tags WHERE name = ?", (tag,))
            for entry_id in affected:
                members = [row["tag"] for row in conn.execute(
                    "SELECT tag FROM entry_tags WHERE entry_id = ? ORDER BY tag", (entry_id,)
                ).fetchall()]
                conn.execute(
                    "UPDATE clipboard_history SET tags = ? WHERE id = ?",
                    (json.dumps(members, ensure_ascii=False), entry_id),
                )
        return {"tag": tag, "affected": n}

    @staticmethod
    def validate_tag_name(tag):
        """标签名校验（UC-11 主事件流第 4 步 / 业务规则 R1）。"""
        name = (tag or "").strip()
        if not name:
            raise AppError(4001, "标签名不能为空")
        if len(name) > TAG_NAME_MAX_LEN:
            raise AppError(4001, "标签名不能超过 %d 个字符" % TAG_NAME_MAX_LEN)
        return name

    # ================================================================ 维护
    def storage_usage(self, attachment_dir):
        """条数与附件占用统计（F3-5 容量统计）。"""
        total_bytes, file_count = attachments.attachment_usage(attachment_dir)
        return {
            "entryCount": self.count(),
            "attachmentCount": file_count,
            "attachmentBytes": total_bytes,
            "attachmentHuman": human_size(total_bytes),
        }

    def vacuum(self):
        """整理数据库，回收已删除记录占用的空间。

        注意：VACUUM 不能在事务内执行，因此这里只加写锁，不开启事务。
        """
        with self.db.write_lock:
            self.db.conn.execute("VACUUM")
        return True


def human_size(num_bytes):
    """字节数转可读文案。"""
    size = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return "%.1f %s" % (size, unit) if unit != "B" else "%d B" % int(size)
        size /= 1024
    return "%.1f GB" % size
