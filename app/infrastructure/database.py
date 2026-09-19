# -*- coding: utf-8 -*-
"""SQLite 连接层：负责数据目录、连接参数、外键与 WAL 设置。

界面线程与剪贴板监听线程都会用到数据库，因此这里统一规定：
    - 使用 ``check_same_thread=False`` 打开连接，由上层用锁串行化写入；
    - 打开 WAL，避免读写互相阻塞（监听到新内容时不能卡住界面查询）；
    - 每条连接都开启外键约束，保证 entry_tags 的级联删除生效。
"""

import os
import sqlite3
import threading

from app.errors import AppError
from app.constants import ERR_INTERNAL

#: 附件目录名（图片外置存储，F2-3）
ATTACHMENT_DIR_NAME = "attachments"
DB_FILE_NAME = "clipboardpro.db"


def default_data_dir():
    """确定数据目录（UC-20 主事件流第 1 步）。

    默认放在程序包同级的 ``.data`` 目录，保证"绿色便携、删除即清空"，
    不使用 %APPDATA%，避免换机器/换目录后找不到自己的历史。
    可用环境变量 ``CLIPBOARDPRO_DATA_DIR`` 覆盖，便于测试与多实例。
    目录不存在则创建。
    """
    override = os.environ.get("CLIPBOARDPRO_DATA_DIR")
    if override:
        data_dir = os.path.abspath(override)
    else:
        app_root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        data_dir = os.path.join(app_root, ".data")
    try:
        os.makedirs(data_dir, exist_ok=True)
        os.makedirs(os.path.join(data_dir, ATTACHMENT_DIR_NAME), exist_ok=True)
    except OSError as exc:
        raise AppError(ERR_INTERNAL, "数据目录不可写", str(exc))
    return data_dir


class Database(object):
    """数据库句柄：持有连接、数据目录路径与一把写锁。"""

    def __init__(self, data_dir=None, db_path=None):
        self.data_dir = data_dir or default_data_dir()
        self.attachment_dir = os.path.join(self.data_dir, ATTACHMENT_DIR_NAME)
        os.makedirs(self.attachment_dir, exist_ok=True)
        self.db_path = db_path or os.path.join(self.data_dir, DB_FILE_NAME)
        self.write_lock = threading.RLock()
        self.conn = self._connect()

    # ------------------------------------------------------------- 连接
    def _connect(self):
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=5.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA synchronous = NORMAL")
        return conn

    def is_readonly(self):
        return False

    def close(self):
        try:
            self.conn.close()
        except sqlite3.Error:
            pass

    # ------------------------------------------------------------- 事务
    def transaction(self):
        """写事务上下文管理器：异常即回滚（F2-5 写入事务与异常回滚）。

        用法::

            with db.transaction() as conn:
                conn.execute(...)
        """
        return _Transaction(self)


class _Transaction(object):
    def __init__(self, db):
        self.db = db
        self.conn = db.conn
        self._depth = 0

    def __enter__(self):
        self.db.write_lock.acquire()
        self._depth = getattr(self.conn, "isolation_level", None)
        # 显式开启事务，保证写入原子性
        try:
            self.conn.execute("BEGIN IMMEDIATE")
            self._begun = True
        except sqlite3.OperationalError:
            # 已经在事务里（嵌套调用）时不再开启
            self._begun = False
        return self.conn

    def __exit__(self, exc_type, exc, tb):
        try:
            if exc_type is None:
                if self._begun:
                    self.conn.commit()
            else:
                if self._begun:
                    self.conn.rollback()
        finally:
            self.db.write_lock.release()
        return False
