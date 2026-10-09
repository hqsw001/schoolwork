# -*- coding: utf-8 -*-
"""窗口行为的**纯几何逻辑**（贴边停靠 / 跑出屏幕修复 / 多屏比例重映射）

这里全是纯函数，不碰 Tk、不碰 Win32——所有"窗口该放哪儿"的判断都能
单测覆盖。真正的窗口操作在 ``ui/panel.py`` 里。

三个场景：

* **贴边停靠**：窗口拖到屏幕边缘时吸附过去，只留一条窄边露在外面。
  鼠标碰一下边缘就展开。
* **跑出屏幕修复**：换了显示器、拔了外接屏之后，上次记下的位置可能
  落在已经不存在的区域里，窗口就再也看不见了。
* **比例重映射**：在小屏上调好的位置，拿到大屏上应当按比例放大，
  而不是死板地复制坐标。
"""

#: 距离屏幕边缘多少像素以内触发停靠
DOCK_TRIGGER = 5

#: 停靠后露出多少像素（鼠标能碰到的那一条）
DOCK_PEEK = 3

#: 边缘名
EDGE_LEFT = "left"
EDGE_RIGHT = "right"
EDGE_TOP = "top"
EDGES = (EDGE_LEFT, EDGE_RIGHT, EDGE_TOP)

#: 窗口至少要有多少像素留在屏幕内（修复时的可见性下限）
MIN_VISIBLE = 48


def clamp_to_screens(x, y, width, height, monitors):
    """把窗口位置修到"至少有一部分在某块屏幕上看得见"。

    返回 ``(x, y, repaired)``。``repaired`` 为 True 表示确实动过位置，
    调用方据此决定要不要记日志——静默改动用户的位置会让人莫名其妙。
    """
    if not monitors:
        return x, y, False

    # 先看窗口矩形与任意一块屏有没有足够的交集
    for monitor in monitors:
        overlap_x = (min(x + width, monitor["x"] + monitor["width"])
                     - max(x, monitor["x"]))
        overlap_y = (min(y + height, monitor["y"] + monitor["height"])
                     - max(y, monitor["y"]))
        if overlap_x >= MIN_VISIBLE and overlap_y >= MIN_VISIBLE:
            return x, y, False

    # 完全看不见：挪到最近那块屏的可见区域里
    primary = next((m for m in monitors if m.get("primary")), monitors[0])
    work_x = primary.get("workX", primary["x"])
    work_y = primary.get("workY", primary["y"])
    work_w = primary.get("workWidth", primary["width"])
    work_h = primary.get("workHeight", primary["height"])
    new_x = max(work_x, min(x, work_x + work_w - MIN_VISIBLE))
    new_y = max(work_y, min(y, work_y + work_h - MIN_VISIBLE))
    # 位置本身就完全不靠谱时，直接回到主屏中间偏上——这是最不容易再出错的落点
    if new_x + MIN_VISIBLE < work_x or new_y + MIN_VISIBLE < work_y:
        new_x = work_x + max(0, (work_w - width) // 2)
        new_y = work_y + 30
    return new_x, new_y, True


def nearest_edge(x, y, width, height, monitor, trigger=DOCK_TRIGGER):
    """窗口贴近哪条边；都不贴返回 None。

    只支持左/右/上三条边：**下边缘不吸附**——任务栏在那里，
    吸下去会把面板压在任务栏底下。
    """
    if monitor is None:
        return None
    left = monitor["x"]
    right = monitor["x"] + monitor["width"]
    top = monitor["y"]

    if x <= left + trigger:
        return EDGE_LEFT
    if x + width >= right - trigger:
        return EDGE_RIGHT
    if y <= top + trigger:
        return EDGE_TOP
    return None


def docked_position(edge, monitor, width, height, peek=DOCK_PEEK):
    """停靠后窗口应当在哪儿（大部分藏在屏幕外，只露 peek 像素）。"""
    if monitor is None or edge not in EDGES:
        return None
    left = monitor["x"]
    right = monitor["x"] + monitor["width"]
    top = monitor["y"]

    if edge == EDGE_LEFT:
        return left - width + peek, top
    if edge == EDGE_RIGHT:
        return right - peek, top
    return left, top - height + peek


def expanded_position(edge, monitor, width, height):
    """停靠状态下鼠标碰上来之后，窗口完全展开的位置。"""
    if monitor is None or edge not in EDGES:
        return None
    work_x = monitor.get("workX", monitor["x"])
    work_y = monitor.get("workY", monitor["y"])
    work_w = monitor.get("workWidth", monitor["width"])
    work_h = monitor.get("workHeight", monitor["height"])

    if edge == EDGE_LEFT:
        return work_x, min(work_y + 30, work_y + work_h - height)
    if edge == EDGE_RIGHT:
        return work_x + work_w - width, min(work_y + 30, work_y + work_h - height)
    return work_x + max(0, (work_w - width) // 2), work_y


def remap_between_monitors(x, y, width, height, source, target, window_size):
    """把窗口位置从一块屏**按比例**映射到另一块屏。

    为什么按比例而不是照搬坐标：21:9 的副屏上靠右的位置拿到 16:9 的
    主屏上，照搬会跑到屏幕外面；按比例映射才符合直觉。
    """
    if source is None or target is None:
        return x, y
    src_w = max(1, source["width"])
    src_h = max(1, source["height"])
    dst_w = max(1, target["width"])
    dst_h = max(1, target["height"])

    ratio_x = (x - source["x"]) / float(src_w)
    ratio_y = (y - source["y"]) / float(src_h)
    new_x = target["x"] + int(round(ratio_x * dst_w))
    new_y = target["y"] + int(round(ratio_y * dst_h))

    # 映射完还要夹一次：比例映射在小屏→大屏时不会越界，
    # 反过来（大屏→小屏）就可能把窗口推出可见区域
    win_w, win_h = window_size
    max_x = target["x"] + max(0, dst_w - win_w)
    max_y = target["y"] + max(0, dst_h - win_h)
    return max(target["x"], min(new_x, max_x)), max(target["y"], min(new_y, max_y))


def is_fully_visible(x, y, width, height, monitors):
    """窗口是否完整落在某块屏的可见范围内。"""
    for monitor in monitors:
        if (x >= monitor["x"] and y >= monitor["y"]
                and x + width <= monitor["x"] + monitor["width"]
                and y + height <= monitor["y"] + monitor["height"]):
            return True
    return False
