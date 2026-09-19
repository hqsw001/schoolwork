# -*- coding: utf-8 -*-
"""日志设施（UC-20 业务规则 R4：启动过程必须记录日志）

日志写在数据目录下，只落本地文件，不做任何上报。
默认级别 INFO，可用环境变量 CLIPBOARDPRO_LOG_LEVEL 覆盖（DEBUG 便于排障）。
"""

import logging
import os
import sys

_CONFIGURED = False
LOGGER_NAME = "clipboardpro"


def setup_logger(data_dir, level=None):
    """初始化日志：文件 + 控制台双输出。重复调用只生效一次。"""
    global _CONFIGURED
    logger = logging.getLogger(LOGGER_NAME)
    if _CONFIGURED:
        return logger

    level_name = (level or os.environ.get("CLIPBOARDPRO_LOG_LEVEL") or "INFO").upper()
    logger.setLevel(getattr(logging, level_name, logging.INFO))
    logger.propagate = False
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%Y-%m-%d %H:%M:%S"
    )

    try:
        os.makedirs(data_dir, exist_ok=True)
        file_handler = logging.FileHandler(
            os.path.join(data_dir, "clipboardpro.log"), encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
    except OSError as exc:  # 日志文件写不了也不能让程序起不来
        print("日志文件初始化失败：%s" % exc, file=sys.stderr)

    console = logging.StreamHandler(sys.stderr)
    console.setFormatter(formatter)
    logger.addHandler(console)

    _CONFIGURED = True
    return logger


def get_logger(suffix=None):
    return logging.getLogger(LOGGER_NAME if not suffix else "%s.%s" % (LOGGER_NAME, suffix))
