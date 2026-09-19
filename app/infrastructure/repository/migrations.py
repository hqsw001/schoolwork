# -*- coding: utf-8 -*-
"""结构版本与升级脚本（F2-1 自动建库建表；UC-20 主事件流第 4 步）

规则（UC-20 业务规则 R2）：
    1. 每个版本一段 SQL，按版本号从小到大依次执行；
    2. 每执行完一步立刻写入版本号，保证升级过程可中断可续；
    3. 已应用的版本不重复执行。

新增结构时：在 MIGRATIONS 末尾追加 (版本号, 说明, SQL)，不要修改历史版本。
"""

VERSION_1 = """
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
    is_sensitive    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS saved_tags (
    name  TEXT PRIMARY KEY,
    color TEXT
);

CREATE TABLE IF NOT EXISTS entry_tags (
    entry_id INTEGER NOT NULL,
    tag      TEXT    NOT NULL,
    PRIMARY KEY (entry_id, tag),
    FOREIGN KEY (entry_id) REFERENCES clipboard_history (id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

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
"""

#: (版本号, 说明, SQL)
MIGRATIONS = [
    (1, "建库建表：历史记录、标签、关联、配置、结构版本", VERSION_1),
]

CURRENT_VERSION = MIGRATIONS[-1][0]


def ensure_schema_table(conn):
    """schema_migrations 表本身必须先存在，才能记录版本。"""
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "  version    INTEGER PRIMARY KEY,"
        "  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
        ")"
    )


def applied_versions(conn):
    ensure_schema_table(conn)
    rows = conn.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
    return [row[0] for row in rows]


def run_migrations(conn, logger=None):
    """执行缺失的结构升级，返回本次实际执行的版本号列表。"""
    done = set(applied_versions(conn))
    executed = []
    for version, description, sql in MIGRATIONS:
        if version in done:
            continue
        for statement in _split_statements(sql):
            conn.execute(statement)
        conn.execute("INSERT INTO schema_migrations (version) VALUES (?)", (version,))
        conn.commit()
        executed.append(version)
        if logger:
            logger.info("结构升级到 v%s：%s", version, description)
    return executed


def _split_statements(sql):
    """按分号拆分 SQL 脚本。

    升级脚本里没有触发器、没有字符串内分号，因此简单拆分是安全的；
    这里仍然加一道断言，避免以后有人往脚本里塞进带分号的字面量却浑然不觉。
    """
    statements = []
    for chunk in sql.split(";"):
        text = chunk.strip()
        if not text:
            continue
        if "'" in text and text.count("'") % 2 != 0:
            raise ValueError("升级脚本存在未闭合的字符串字面量，无法安全拆分：%r" % text[:60])
        statements.append(text)
    return statements
