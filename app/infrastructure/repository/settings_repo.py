# -*- coding: utf-8 -*-
"""SettingsRepository：配置表读写（UC-04 / UC-15 / UC-16 / UC-18）

内存里持有一份配置快照，读配置不查库；写配置同时更新内存与数据库。
这样界面高频读取配置（列表密度、主题、开关状态）不会反复访问 SQLite。
"""

import threading

from app.constants import ERR_INVALID_ARG
from app.errors import AppError
from app.infrastructure.repository import defaults
from app.infrastructure.repository.defaults import SPECS


class SettingsRepository(object):
    def __init__(self, db, logger=None):
        self.db = db
        self.logger = logger
        self._lock = threading.RLock()
        self._cache = {}

    # ------------------------------------------------------------ 读取
    def load_all(self):
        """启动时读取全部配置；缺失项写入默认值并落库（UC-18 主事件流第 6 步）。"""
        with self._lock:
            rows = self.db.conn.execute("SELECT key, value FROM settings").fetchall()
            stored = {row["key"]: row["value"] for row in rows}
            missing = []
            for key, (_, default, _, _) in SPECS.items():
                if key in stored:
                    self._cache[key] = defaults.coerce(key, stored[key])
                else:
                    self._cache[key] = default
                    missing.append(key)
            # 外部写坏的值也要写回正确形式，避免每次启动都解析失败
            for key in missing:
                self._persist(key, self._cache[key])
            if missing and self.logger:
                self.logger.info("补齐缺失配置项 %d 项：%s", len(missing), ", ".join(missing))
            return dict(self._cache)

    def get(self, key, default=None):
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        spec = SPECS.get(key)
        if spec is not None:
            return spec[1]
        return default

    def get_bool(self, key):
        return bool(self.get(key))

    def get_int(self, key):
        try:
            return int(self.get(key))
        except (TypeError, ValueError):
            return int(SPECS[key][1])

    def snapshot(self):
        """返回全部配置的字典副本（对应命令 get_settings）。"""
        with self._lock:
            return dict(self._cache)

    def describe(self):
        """返回带元信息的配置清单，供设置界面渲染。"""
        with self._lock:
            out = []
            for key, (kind, default,含义, allowed) in SPECS.items():
                out.append({
                    "key": key,
                    "type": kind,
                    "value": self._cache.get(key, default),
                    "default": default,
                    "label": 含义,
                    "options": list(allowed) if allowed else None,
                })
            return out

    # ------------------------------------------------------------ 写入
    def set(self, key, value):
        """更新单项配置并立即生效（UC-18 主事件流第 2~4 步）。"""
        if key not in SPECS:
            raise AppError(ERR_INVALID_ARG, "未知配置项", key)
        kind = SPECS[key][0]
        if kind == defaults.TYPE_INT and key == "app.persistent_limit":
            value = self._validate_limit(value)
        elif kind == defaults.TYPE_BOOL and not isinstance(value, bool):
            value = defaults.coerce(key, value)
        with self._lock:
            self._cache[key] = value
            self._persist(key, value)
        return value

    def set_many(self, mapping):
        with self._lock:
            for key, value in mapping.items():
                if key in SPECS:
                    self._cache[key] = value
                    self._persist(key, value)
        return self.snapshot()

    def restore_defaults(self):
        """恢复默认设置（UC-18 备选流 A1）：不影响历史记录本身。"""
        with self._lock:
            for key, (_, default, _, _) in SPECS.items():
                self._cache[key] = default
                self._persist(key, default)
            return dict(self._cache)

    def _persist(self, key, value):
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, defaults.serialize(value)),
            )

    @staticmethod
    def _validate_limit(value):
        from app.constants import MIN_STORAGE_LIMIT
        try:
            limit = int(value)
        except (TypeError, ValueError):
            raise AppError(ERR_INVALID_ARG, "上限必须是整数")
        if limit < MIN_STORAGE_LIMIT:
            raise AppError(ERR_INVALID_ARG, "上限不能小于 %d" % MIN_STORAGE_LIMIT)
        return limit
