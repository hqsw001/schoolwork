-- 剪贴板Pro 建表脚本（结构版本 v1）
-- 对应《需求分析报告》7.1 数据库表结构。所有时间字段为毫秒级 Unix 时间戳。
-- 本文件是给人看的"结构基线"；程序启动时执行的结构升级脚本在
-- app/infrastructure/repository/migrations.py，两者必须保持一致。

PRAGMA foreign_keys = ON;

-- 表 1：clipboard_history（剪贴板历史记录表）
CREATE TABLE IF NOT EXISTS clipboard_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    content_type    TEXT    NOT NULL,
    content         TEXT    NOT NULL,
    html_content    TEXT,
    source_app      TEXT    NOT NULL DEFAULT '未知',
    source_app_path TEXT,
    timestamp       INTEGER NOT NULL,
    preview         TEXT    NOT NULL DEFAULT '',
    is_pinned       INTEGER NOT NULL DEFAULT 0,
    pinned_order    INTEGER NOT NULL DEFAULT 0,
    tags            TEXT    NOT NULL DEFAULT '[]',
    use_count       INTEGER NOT NULL DEFAULT 0,
    content_hash    INTEGER NOT NULL DEFAULT 0,
    is_external     INTEGER NOT NULL DEFAULT 0,
    -- v1 追加：F9 隐私标记。v1 只做识别与脱敏预览，密文存储留给第三周，
    -- 因此这里先落标记位，避免第三周再加字段又要走一次结构升级。
    is_sensitive    INTEGER NOT NULL DEFAULT 0
);

-- 表 2：saved_tags（标签定义表）
CREATE TABLE IF NOT EXISTS saved_tags (
    name  TEXT PRIMARY KEY,
    color TEXT
);

-- 表 3：entry_tags（记录与标签的关联表）
CREATE TABLE IF NOT EXISTS entry_tags (
    entry_id INTEGER NOT NULL,
    tag      TEXT    NOT NULL,
    PRIMARY KEY (entry_id, tag),
    FOREIGN KEY (entry_id) REFERENCES clipboard_history (id) ON DELETE CASCADE
);

-- 表 4：settings（配置表）
CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 表 5：schema_migrations（结构版本表）
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- 关键索引
CREATE INDEX IF NOT EXISTS idx_history_pinned_order_time
    ON clipboard_history (is_pinned, pinned_order, timestamp);
CREATE INDEX IF NOT EXISTS idx_history_type_hash
    ON clipboard_history (content_type, content_hash);
CREATE INDEX IF NOT EXISTS idx_history_timestamp
    ON clipboard_history (timestamp);
CREATE INDEX IF NOT EXISTS idx_entry_tags_tag
    ON entry_tags (tag);
CREATE INDEX IF NOT EXISTS idx_entry_tags_entry
    ON entry_tags (entry_id);
