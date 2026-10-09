# -*- coding: utf-8 -*-
"""ClipboardRepository：记录表与标签表的增删改查（仓储类）

对应 Rust 版的 ``SqliteClipboardRepository``。所有写操作走事务，
读操作直接查（WAL 模式下读不阻塞写）。

列表排序规则固定为（UC-10 业务规则 R2，所有查询路径一致）：

    置顶优先 → 置顶顺序倒序 → 时间倒序 → id 倒序
"""

import json
import sqlite3

from app.constants import (
    ERR_NOT_FOUND,
    PAGE_SIZE,
    SEARCH_RESULT_LIMIT,
    TAG_NAME_MAX_LEN,
    TYPE_IMAGE,
)
from app.domain.models import ClipboardEntry
from app.errors import AppError, now_ms
from app.infrastructure import encryption
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
        # 敏感文本类记录加密落盘（F9-3）。content_hash 仍然基于**明文**计算，
        # 所以加密不会影响去重。
        content, html_content, preview = encryption.protect_entry(entry)
        with self.db.transaction() as conn:
            if is_new:
                cursor = conn.execute(
                    "INSERT INTO clipboard_history ("
                    "  content_type, content, html_content, source_app, source_app_path,"
                    "  timestamp, preview, is_pinned, pinned_order, tags, use_count,"
                    "  content_hash, is_external, is_sensitive"
                    ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entry.content_type, content, html_content,
                        entry.source_app, entry.source_app_path, entry.timestamp,
                        preview, entry.is_pinned, entry.pinned_order,
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
                        entry.content_type, content, html_content,
                        entry.source_app, entry.source_app_path, entry.timestamp,
                        preview, entry.is_pinned, entry.pinned_order,
                        json.dumps(entry.tags, ensure_ascii=False), entry.use_count,
                        entry.content_hash, entry.is_external, entry.is_sensitive,
                        entry.id,
                    ),
                )
            self._sync_tags(conn, entry)
        return entry.id

    def _row_to_entry(self, row):
        """行 → 实体，并把敏感记录的密文解回明文。

        解密集中在这一处：所有读路径都从这里走，就不会出现
        "某个查询忘了解密、界面上显示一串 base64"的情况。
        """
        return encryption.unprotect_entry(ClipboardEntry.from_row(row), self.logger)

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

    def increment_use_count(self, entry_id, delta=1):
        """只增加使用次数，**不动时间戳**。

        "打开内容"只是用了一次，不应该把它顶到列表最前面——那会让用户
        正在翻找的列表突然重排。粘贴走的是 touch_entry（会重排，符合预期）。
        """
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT id FROM clipboard_history WHERE id = ?", (entry_id,)
            ).fetchone()
            if row is None:
                raise AppError(ERR_NOT_FOUND, "记录不存在", str(entry_id))
            conn.execute(
                "UPDATE clipboard_history SET use_count = use_count + ? WHERE id = ?",
                (delta, entry_id),
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
        """设置或取消敏感标记（UC-17 备选流 A4 / A5）。

        改标记必须**同时改落盘形态**：打上敏感就把明文加密，取消敏感就把
        密文解回明文。只改标志位的话，会出现"标着敏感但库里是明文"或者
        "标着不敏感但库里是密文解不开"的烂账。
        """
        is_sensitive = 1 if is_sensitive else 0
        entry = self.find_by_id(entry_id)
        if entry is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在", str(entry_id))
        entry.is_sensitive = is_sensitive
        if is_sensitive:
            content, html_content, preview = encryption.protect_entry(entry)
        else:
            content, html_content, preview = self._decrypted_fields(entry)
        with self.db.transaction() as conn:
            conn.execute(
                "UPDATE clipboard_history SET is_sensitive = ?, content = ?,"
                " html_content = ?, preview = ? WHERE id = ?",
                (is_sensitive, content, html_content, preview, entry_id),
            )
        return self.get_entry(entry_id)

    def _decrypted_fields(self, entry):
        """把记录字段还原成明文（取消敏感标记时用）。"""
        out = []
        for field in ("content", "html_content", "preview"):
            value = getattr(entry, field, None)
            if encryption.is_encrypted(value):
                value = encryption.decrypt(value)
            out.append(value)
        return out[0], out[1], out[2]

    def alignment_stats(self):
        """统计"标记与实际落盘形态对不上"的记录数（自愈用）。

        两种不一致：标着敏感却是明文、标着不敏感却是密文。
        """
        rows = self.db.conn.execute(
            "SELECT id, content, is_sensitive FROM clipboard_history"
            " WHERE content_type IN (%s)"
            % ",".join("?" * len(encryption.ENCRYPTABLE_TYPES)),
            tuple(encryption.ENCRYPTABLE_TYPES),
        ).fetchall()
        missing = []
        stale = []
        for row in rows:
            encrypted = encryption.is_encrypted(row["content"])
            if row["is_sensitive"] and not encrypted:
                missing.append(row["id"])
            elif not row["is_sensitive"] and encrypted:
                stale.append(row["id"])
        return {"total": len(rows), "missingEncryption": missing,
                "staleEncryption": stale}

    def realign_encryption(self, limit=500, logger=None):
        """把"标记与落盘形态对不上"的记录修正过来（F9-3 自愈）。

        为什么需要：加密是后加的，之前存下来的敏感记录全是明文；
        用户手动打上/取消 sensitive 标签时也要跟着变。
        """
        stats = self.alignment_stats()
        pending = stats["missingEncryption"] + stats["staleEncryption"]
        fixed = 0
        for entry_id in pending[:limit]:
            entry = self.find_by_id(entry_id)
            if entry is None:
                continue
            try:
                self.mark_sensitive(entry_id, entry.is_sensitive)
                fixed += 1
            except AppError as exc:
                if logger:
                    logger.warning("记录 #%s 加密对齐失败：%s", entry_id, exc.message)
        return {"scanned": stats["total"], "fixed": fixed,
                "pending": max(0, len(pending) - fixed)}

    # ================================================================ 读取
    def get_entry(self, entry_id):
        row = self.db.conn.execute(
            "SELECT %s FROM clipboard_history WHERE id = ?" % _SELECT_FIELDS, (entry_id,)
        ).fetchone()
        if row is None:
            raise AppError(ERR_NOT_FOUND, "记录不存在", str(entry_id))
        return self._row_to_entry(row)

    def find_by_id(self, entry_id):
        row = self.db.conn.execute(
            "SELECT %s FROM clipboard_history WHERE id = ?" % _SELECT_FIELDS, (entry_id,)
        ).fetchone()
        return self._row_to_entry(row) if row else None

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
        return [self._row_to_entry(r) for r in rows]

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
        entries = [self._row_to_entry(r) for r in rows]

        # 敏感记录的正文在库里是**密文**，SQL 的 LIKE 天然匹配不到——
        # 只走 SQL 的话，"搜自己的手机号却搜不到"会让人以为记录丢了。
        # 所以补一遍内存匹配：把敏感记录解密后比对，**带上限**避免全表解密。
        if not tag_only:
            entries = self._merge_encrypted_matches(
                entries, query, limit, content_type, source_app)
        return entries

    #: 补扫敏感记录时的最大解密条数（每次检索的代价上限）
    ENCRYPTED_SCAN_LIMIT = 500

    def _merge_encrypted_matches(self, entries, query, limit, content_type, source_app):
        """把"密文里其实匹配"的敏感记录并进结果集。"""
        known = {entry.id for entry in entries}
        sql = ("SELECT %s FROM clipboard_history WHERE is_sensitive = 1"
               " AND content LIKE ? ESCAPE '\\'" % _SELECT_FIELDS)
        params = [encryption.ENCRYPT_PREFIX + "%"]
        if content_type:
            sql += " AND content_type = ?"
            params.append(content_type)
        if source_app:
            sql += " AND source_app = ?"
            params.append(source_app)
        sql += _ORDER_BY + " LIMIT ?"
        params.append(self.ENCRYPTED_SCAN_LIMIT)
        try:
            rows = self.db.conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return entries

        needle = query.lower()
        extra = []
        for row in rows:
            if row["id"] in known:
                continue
            entry = self._row_to_entry(row)
            haystack = " ".join([
                entry.content or "", entry.preview or "",
                entry.source_app or "", " ".join(entry.tags or []),
            ]).lower()
            if needle in haystack:
                extra.append(entry)
        if not extra:
            return entries
        merged = entries + extra
        merged.sort(key=lambda e: (e.is_pinned, e.pinned_order, e.timestamp, e.id),
                    reverse=True)
        return merged[:limit]

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
        return self._row_to_entry(row) if row else None

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

    def rename_tag(self, old, new):
        """全局重命名标签（对应命令 rename_tag_globally / UC-11 备选流 A2）。

        目标标签**已存在时按合并处理**，而不是报错：用户想把"待用"并进
        "重要"，报错只会让他去手动一条条改。

        返回受影响条数，界面据此提示"已更新 N 条记录"。
        """
        source = self.validate_tag_name(old)
        target = self.validate_tag_name(new)
        if source == target:
            return {"tag": target, "affected": 0, "merged": False}
        with self.db.transaction() as conn:
            affected = [row["entry_id"] for row in conn.execute(
                "SELECT entry_id FROM entry_tags WHERE tag = ?", (source,)
            ).fetchall()]
            target_exists = conn.execute(
                "SELECT 1 FROM saved_tags WHERE name = ?", (target,)
            ).fetchone() is not None

            for entry_id in affected:
                # 先把新标签挂上（已存在就跳过），再摘掉旧的
                conn.execute(
                    "INSERT OR IGNORE INTO entry_tags (entry_id, tag) VALUES (?, ?)",
                    (entry_id, target),
                )
            conn.execute("DELETE FROM entry_tags WHERE tag = ?", (source,))

            # 颜色：目标已经有颜色就保留目标的，否则把源的颜色搬过去
            if not target_exists:
                row = conn.execute(
                    "SELECT color FROM saved_tags WHERE name = ?", (source,)
                ).fetchone()
                conn.execute(
                    "INSERT INTO saved_tags (name, color) VALUES (?, ?)"
                    " ON CONFLICT(name) DO UPDATE SET color = excluded.color",
                    (target, row["color"] if row else None),
                )
            conn.execute("DELETE FROM saved_tags WHERE name = ?", (source,))

            # 冗余的 tags 列要跟着重建，否则列表里的标签芯片还是旧的
            for entry_id in affected:
                members = [r["tag"] for r in conn.execute(
                    "SELECT tag FROM entry_tags WHERE entry_id = ? ORDER BY tag",
                    (entry_id,)
                ).fetchall()]
                conn.execute(
                    "UPDATE clipboard_history SET tags = ? WHERE id = ?",
                    (json.dumps(members, ensure_ascii=False), entry_id),
                )
        return {"tag": target, "affected": len(affected), "merged": target_exists}

    def items_by_tag(self, tag, limit=200, offset=0):
        """按标签取记录（对应命令 get_tag_items）。"""
        rows = self.db.conn.execute(
            "SELECT %s FROM clipboard_history h WHERE EXISTS ("
            "  SELECT 1 FROM entry_tags e WHERE e.entry_id = h.id AND e.tag = ?"
            ") ORDER BY h.timestamp DESC, h.id DESC LIMIT ? OFFSET ?"
            % _SELECT_FIELDS.replace("id,", "h.id,", 1),
            (tag, limit, offset),
        ).fetchall()
        return [self._row_to_entry(row) for row in rows]

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
