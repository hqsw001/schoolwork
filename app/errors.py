# -*- coding: utf-8 -*-
"""统一异常与服务结果封装。

内核对外（界面命令接口）统一返回：

    {"ok": true, "data": {...}, "error": null, "timestamp": 1757000000000}

出错时 ok 为 false，error 为 {"code": 4001, "message": "参数非法"}。
业务代码内部抛 AppError，由门面统一转换成上面的结构，避免每个命令
各写一套 try/except。
"""

import time

from app.constants import (
    ERR_INVALID_ARG,
    ERR_INTERNAL,
    ERR_NOT_FOUND,
    ERROR_MESSAGES,
)


class AppError(Exception):
    """带错误码的业务异常。"""

    def __init__(self, code=ERR_INTERNAL, message=None, detail=None):
        self.code = code
        self.detail = detail
        self.message = message or ERROR_MESSAGES.get(code, "内部错误")
        if detail:
            self.message = "%s：%s" % (self.message, detail)
        super().__init__(self.message)


class InvalidArgument(AppError):
    def __init__(self, detail=None):
        super().__init__(ERR_INVALID_ARG, detail=detail)


class NotFound(AppError):
    def __init__(self, detail=None):
        super().__init__(ERR_NOT_FOUND, detail=detail)


def now_ms():
    """当前毫秒级 Unix 时间戳（全系统统一时间口径）。"""
    return int(time.time() * 1000)


def ok(data=None):
    """成功返回结构。"""
    return {
        "ok": True,
        "data": data,
        "error": None,
        "timestamp": now_ms(),
    }


def fail(code=ERR_INTERNAL, message=None):
    """失败返回结构。"""
    return {
        "ok": False,
        "data": None,
        "error": {"code": code, "message": message or ERROR_MESSAGES.get(code, "内部错误")},
        "timestamp": now_ms(),
    }


def guard(func):
    """命令方法装饰器：把 AppError 与未预期异常统一收敛成返回结构。

    用法::

        @guard
        def get_history(self, limit=50, offset=0):
            ...
    """

    def wrapper(*args, **kwargs):
        try:
            return ok(func(*args, **kwargs))
        except AppError as exc:
            return fail(exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 - 兜底，避免界面因异常白屏
            return fail(ERR_INTERNAL, "内部错误：%s" % exc)

    wrapper.__name__ = func.__name__
    wrapper.__doc__ = func.__doc__
    return wrapper
