# -*- coding: utf-8 -*-
"""顺序粘贴队列（对应 TieZ 的 ``services/paste_queue.rs``）

场景：从几个地方依次复制一堆片段，再到目标文档里**按一次键粘一条**——
不用来回切窗口，也不用翻历史列表。

设计上的三个决定：

1. **不落库。** 队列描述的是一次连续操作，"重启之后还留着上次没粘完的
   队列"没有意义，反而会让用户莫名其妙。进程退出即清空。
2. **允许负数 id。** 关闭持久化时记录只存在内存里，id 是负的
   （见 ``pipeline.SessionStore``）。队列两种 id 都要能装。
3. **弹出后要能跳过失效项。** 队列建好之后用户可能把某条删掉了，
   这时应当自动跳到下一条，而不是报错让整条流程卡住。
"""

import threading


class PasteQueue(object):
    """先进先出队列，自带一把锁（会在工作线程与界面线程之间共享）。"""

    def __init__(self, logger=None):
        self.logger = logger
        self._lock = threading.RLock()
        self._items = []

    # ---------------------------------------------------------------- 写
    def set_items(self, item_ids):
        """整体替换队列内容。传空列表就是清空。"""
        cleaned = []
        for item in item_ids or []:
            try:
                cleaned.append(int(item))
            except (TypeError, ValueError):
                continue
        with self._lock:
            self._items = cleaned
        return list(cleaned)

    def append(self, entry_id):
        """把一条记录追加到队尾（顺序模式下每采集一条就追加）。"""
        try:
            value = int(entry_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            self._items.append(value)
        return True

    def pop(self):
        """弹出队首；空队列返回 None。"""
        with self._lock:
            return self._items.pop(0) if self._items else None

    def remove(self, entry_id):
        try:
            value = int(entry_id)
        except (TypeError, ValueError):
            return False
        with self._lock:
            if value in self._items:
                self._items.remove(value)
                return True
        return False

    def clear(self):
        with self._lock:
            self._items = []

    # ---------------------------------------------------------------- 读
    def items(self):
        with self._lock:
            return list(self._items)

    def peek(self):
        with self._lock:
            return self._items[0] if self._items else None

    def size(self):
        with self._lock:
            return len(self._items)

    def __len__(self):
        return self.size()
