# -*- coding: utf-8 -*-
"""基础设施层导出。"""

from app.infrastructure.database import Database, default_data_dir  # noqa: F401

__all__ = ["Database", "default_data_dir"]
