# -*- coding: utf-8 -*-
"""DedupService：内容指纹与重复判定（F6 重复内容合并 / UC-06）

两块职责：

    1. 回声抑制（F1-6 / UC-09）：本程序自己写回剪贴板产生的内容变化，
       10 秒内不允许再次入库，否则历史会被自己污染。
       判定条件是"指纹一致 **且** 在时间窗内"，两者缺一不可（UC-09 业务规则 R2）。
       这里采用三重判定：指纹、宽松文本比对、原始字符串比对。
       之所以不止用指纹，是因为"写进去的文本"和"读回来的文本"可能因为
       系统的换行符转换而指纹不同（\\n 被写成 \\r\\n），只比指纹会漏判。

    2. 重复合并（UC-06）：命中已有记录时不新增行，只刷新时间戳与使用次数。
"""

import time

from app.constants import ECHO_WINDOW_MS, TYPE_IMAGE
from app.services import content_utils


class EchoMarker(object):
    """一次"本程序写回剪贴板"的标记。"""

    __slots__ = ("fingerprint", "created_at", "kind", "content_type", "raw_text",
                 "normalized_text", "entry_id")

    def __init__(self, fingerprint, kind, content_type, raw_text="", normalized_text="",
                 entry_id=None):
        self.fingerprint = fingerprint
        self.kind = kind
        self.content_type = content_type
        self.raw_text = raw_text
        self.normalized_text = normalized_text
        self.entry_id = entry_id
        self.created_at = time.time()

    def age_ms(self):
        return int((time.time() - self.created_at) * 1000)

    def is_expired(self, window_ms=ECHO_WINDOW_MS):
        return self.age_ms() > window_ms


class DedupService(object):
    def __init__(self, logger=None, window_ms=ECHO_WINDOW_MS):
        self.logger = logger
        self.window_ms = window_ms
        self._markers = []

    # ================================================================ 回声抑制
    def mark_paste(self, fingerprint, kind, content_type="text", raw_text="",
                   normalized_text="", entry_id=None):
        """写回剪贴板之前登记粘贴标记（UC-09 主事件流第 4 步）。"""
        self._prune()
        marker = EchoMarker(
            fingerprint=fingerprint, kind=kind, content_type=content_type,
            raw_text=raw_text, normalized_text=normalized_text, entry_id=entry_id,
        )
        self._markers.append(marker)
        if self.logger:
            self.logger.debug(
                "登记粘贴标记 kind=%s hash=%s entry=%s（共 %d 个有效标记）",
                kind, fingerprint, entry_id, len(self._markers),
            )
        return marker

    def is_echo(self, fingerprint=None, kind=None, raw_text=None, normalized_text=None,
                content_type=None):
        """判断本次采集是否为本程序自己的回声（UC-09 主事件流第 9 步）。

        命中后清除对应标记：一次粘贴只应该被抑制一次，标记留着会误伤
        用户随后真正复制同样内容的操作（UC-09 业务规则 R2 的窗口语义）。
        """
        self._prune()
        for marker in list(self._markers):
            if kind and marker.kind != kind:
                continue
            if content_type and marker.content_type != content_type:
                continue
            matched = False
            if fingerprint is not None and marker.fingerprint == fingerprint:
                matched = True
            elif raw_text is not None and marker.raw_text and marker.raw_text == raw_text:
                # 原始字符串完全一致（最可靠的一种）
                matched = True
            elif (normalized_text and marker.normalized_text
                  and marker.normalized_text == normalized_text):
                matched = True
            if matched:
                self._markers.remove(marker)
                if self.logger:
                    self.logger.info(
                        "判定为自粘贴回声，丢弃本次采集（kind=%s，%dms 前写入）",
                        marker.kind, marker.age_ms(),
                    )
                return True
        return False

    def clear_markers(self):
        self._markers = []

    def marker_count(self):
        self._prune()
        return len(self._markers)

    def _prune(self):
        """清理过期标记，避免长时间运行后列表无限增长。"""
        self._markers = [m for m in self._markers if not m.is_expired(self.window_ms)]

    # ================================================================ 重复合并
    @staticmethod
    def fingerprint(content_type, content, image=None):
        return content_utils.fingerprint_of(content_type, content, image=image)

    @staticmethod
    def is_same_content(content_type, existing, content, html=None):
        """指纹之外的二次比对（UC-06 主事件流第 4~5 步）。

        文本类：规范化后比对，覆盖"同一段文字因复制来源不同而携带不同换行符"；
        图片类：指纹已经基于缩放后的像素，无需再比；
        富文本：HTML 不同但纯文本相同时，由调用方结合 10 秒窗口决定是否合并
                （UC-06 备选流 A1），这里只回答"纯文本是否相同"。
        """
        if content_type == TYPE_IMAGE:
            return True
        return content_utils.same_text(existing.content, content)
