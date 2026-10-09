# -*- coding: utf-8 -*-
"""紧凑预览浮窗的**定位逻辑**（对应 TieZ 的 ``pickPreviewPosition``）

纯函数，不碰 Tk 也不碰 Win32——所有"浮窗该摆哪儿"的判断都能单测覆盖。
真正的窗口操作在 ``ui/preview_window.py`` 里。

算法与参考实现一致，按优先级依次尝试：

1. 锚点右下 / 右上 / 左下 / 左上四个候选位；
2. 都不合适时，改放到**主面板外侧**（右、左、上、下）——
   浮窗盖住面板本身是最糟的结果，用户就没法边看预览边操作了；
3. 再不行就把默认候选位夹进屏幕范围。

最后还有一道兜底：实在没地方放就夹到屏幕内，**宁可盖住面板也不能跑到屏幕外**。
"""

#: 浮窗与屏幕边缘之间至少留多少像素
SCREEN_MARGIN = 8

#: 浮窗与锚点之间的间隔
ANCHOR_OFFSET = 12

#: 与主面板重叠多少算"盖住了"
AVOID_RECT = "avoid"

#: 浮窗尺寸范围
MIN_WIDTH = 260
MAX_WIDTH = 560
MIN_HEIGHT = 120
MAX_HEIGHT = 520


def _fits(point, width, height, bounds, margin=SCREEN_MARGIN):
    x, y = point
    return (x >= bounds["x"] + margin and y >= bounds["y"] + margin
            and x + width <= bounds["x"] + bounds["width"] - margin
            and y + height <= bounds["y"] + bounds["height"] - margin)


def _intersects(point, width, height, rect):
    """矩形相交判断（边界相接不算相交）。"""
    if not rect:
        return False
    x, y = point
    return not (x + width <= rect["left"] or x >= rect["right"]
                or y + height <= rect["top"] or y >= rect["bottom"])


def _clamp(point, width, height, bounds, margin=SCREEN_MARGIN):
    x, y = point
    max_x = bounds["x"] + bounds["width"] - width - margin
    max_y = bounds["y"] + bounds["height"] - height - margin
    return (min(max(x, bounds["x"] + margin), max(max_x, bounds["x"] + margin)),
            min(max(y, bounds["y"] + margin), max(max_y, bounds["y"] + margin)))


def pick_preview_position(anchor_x, anchor_y, width, height, monitor,
                          avoid_rect=None, margin=SCREEN_MARGIN,
                          offset=ANCHOR_OFFSET):
    """算出浮窗该放在哪儿，返回 ``(x, y)``。

    anchor 是鼠标（或卡片）在屏幕坐标系里的位置；
    avoid_rect 是主面板的矩形（``{left, top, right, bottom}``）。
    """
    if monitor is None:
        return anchor_x + offset, anchor_y + offset

    candidates = [
        (anchor_x + offset, anchor_y + offset),                  # 右下
        (anchor_x + offset, anchor_y - height - offset),         # 右上
        (anchor_x - width - offset, anchor_y + offset),          # 左下
        (anchor_x - width - offset, anchor_y - height - offset),  # 左上
    ]

    for point in candidates:
        if _fits(point, width, height, monitor, margin) \
                and not _intersects(point, width, height, avoid_rect):
            return point

    # 四个锚点候选位都不行：改放到主面板外侧。
    # 浮窗盖住面板本身是最糟的结果——用户就没法边看预览边操作了。
    if avoid_rect:
        outside = [
            (avoid_rect["right"] + offset, anchor_y - height // 4),
            (avoid_rect["left"] - width - offset, anchor_y - height // 4),
            (anchor_x - width // 5, avoid_rect["top"] - height - offset),
            (anchor_x - width // 5, avoid_rect["bottom"] + offset),
        ]
        for point in outside:
            clamped = _clamp(point, width, height, monitor, margin)
            if not _intersects(clamped, width, height, avoid_rect):
                return clamped

    for point in candidates:
        clamped = _clamp(point, width, height, monitor, margin)
        if not _intersects(clamped, width, height, avoid_rect):
            return clamped

    # 兜底：夹进屏幕。**宁可盖住面板也不能跑到屏幕外**——
    # 跑到屏幕外的浮窗用户根本看不见，还会以为程序出问题了。
    return _clamp(candidates[0], width, height, monitor, margin)


def preview_size(content_type, text_length=0, image_size=None,
                 base_width=380, base_height=220):
    """按内容类型估算浮窗该多大。

    图片要按原图比例算，长文本要按字数给高度——统一用固定尺寸会让
    "一行字的记录"和"五千字的记录"看起来一样大，很浪费屏幕。
    """
    width = max(MIN_WIDTH, min(MAX_WIDTH, base_width))
    height = max(MIN_HEIGHT, min(MAX_HEIGHT, base_height))

    if content_type == "image" and image_size:
        image_w, image_h = image_size
        if image_w > 0 and image_h > 0:
            # 等比缩放到宽度上限内
            scale = min(1.0, (MAX_WIDTH - 24) / float(image_w))
            width = max(MIN_WIDTH, int(image_w * scale) + 24)
            height = max(MIN_HEIGHT, int(image_h * scale) + 48)
    elif text_length:
        # 每行大约 42 个字符，行高约 18px
        lines = max(1, (text_length + 41) // 42)
        height = max(MIN_HEIGHT, min(MAX_HEIGHT, lines * 18 + 64))

    return width, height


def anchor_rect(left, top, right, bottom):
    """把四个边界打包成 avoid_rect。"""
    return {"left": left, "top": top, "right": right, "bottom": bottom}


def rect_from_window(x, y, width, height):
    """窗口位置尺寸 → avoid_rect。"""
    return {"left": x, "top": y, "right": x + width, "bottom": y + height}
