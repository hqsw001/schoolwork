# -*- coding: utf-8 -*-
"""剪贴板读写（UC-01 采集 / UC-09 粘贴）

读取格式优先级（UC-01 主事件流第 4 步）：

    文件列表(CF_HDROP) → 位图(CF_DIBV5 / CF_DIB) → HTML 富文本(HTML Format) → 纯文本(CF_UNICODETEXT)

业务规则：
    R2 同一次复制只产生一次采集，不允许因为多格式读取而产生多条记录
       —— 因此这里是"挑一个最合适的格式"，不是"把能读的都读出来"。
    R4 来源应用识别失败时来源字段填"未知"，不阻塞采集。

剪贴板被其他程序独占是常态（浏览器、Office 都会短暂占用），
因此所有打开操作都会重试 3 次（UC-01 异常流 E1 / UC-09 异常流 E2）。
"""

import struct
import time

from app.constants import ERR_INTERNAL, MAX_CONTENT_LEN
from app.domain.models import ClipboardData
from app.errors import AppError
from app.infrastructure.clipboard import image_utils
from app.infrastructure.clipboard import win32 as w32

try:
    import win32clipboard
    import win32con
except ImportError:  # pragma: no cover
    win32clipboard = None
    win32con = None

#: 剪贴板格式号（直接写字面量，避免依赖 win32con 的版本差异）
CF_TEXT = 1
CF_BITMAP = 2
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIBV5 = 17

#: HTML 格式名（需要用 RegisterClipboardFormat 换格式号）
HTML_FORMAT_NAME = "HTML Format"

#: 图片类文件的扩展名（单个图片文件被复制时按图片处理，而不是按文件处理）
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".bmp", ".gif", ".webp", ".tiff", ".tif")


# ---------------------------------------------------------------------------
# 低层：打开/关闭剪贴板
# ---------------------------------------------------------------------------
class clipboard_session(object):
    """剪贴板打开上下文管理器，带重试。

    用法::

        with clipboard_session(write=False) as ok:
            if ok:
                ...
    """

    def __init__(self, write=False, retries=w32.OPEN_CLIPBOARD_RETRIES):
        self.write = write
        self.retries = retries
        self.opened = False
        self.last_error = None

    def __enter__(self):
        for attempt in range(self.retries):
            try:
                win32clipboard.OpenClipboard()
                self.opened = True
                return True
            except Exception as exc:  # noqa: BLE001 - pywintypes.error 才会抛出
                self.last_error = exc
                time.sleep(w32.OPEN_CLIPBOARD_DELAY * (attempt + 1))
        return False

    def __exit__(self, exc_type, exc, tb):
        if self.opened:
            try:
                win32clipboard.CloseClipboard()
            except Exception:  # noqa: BLE001
                pass
            self.opened = False
        return False


def enum_formats():
    """列出剪贴板上当前存在的格式号与名称（排查多格式竞争用）。

    要求调用方已经打开剪贴板；为方便使用，这里自己开一次。
    """
    with clipboard_session() as ok:
        if not ok:
            return []
        formats = []
        fmt = win32clipboard.EnumClipboardFormats(0)
        while fmt:
            try:
                name = win32clipboard.GetClipboardFormatName(fmt)
            except Exception:  # noqa: BLE001 - 标准格式没有名字
                name = ""
            formats.append((fmt, name))
            fmt = win32clipboard.EnumClipboardFormats(fmt)
        return formats


def html_format_id():
    """取得 "HTML Format" 的格式号（未注册过会顺带注册）。"""
    with clipboard_session() as ok:
        if not ok:
            return 0
        try:
            return win32clipboard.RegisterClipboardFormat(HTML_FORMAT_NAME)
        except Exception:  # noqa: BLE001
            return 0


# ---------------------------------------------------------------------------
# 采集：把剪贴板内容读成一个 ClipboardData
# ---------------------------------------------------------------------------
def read_clipboard_data(options=None):
    """读取剪贴板，返回 ClipboardData；无可用内容时返回 None。

    options 支持：
        capture_rich_text  是否采集富文本，默认 True
        capture_files      是否采集文件与目录路径，默认 True
    """
    options = options or {}
    with clipboard_session() as ok:
        if not ok:
            return None
        formats = _enum_formats_opened()
        format_ids = {f for f, _ in formats}
        html_id = _register_html_opened()

        # ① 文件列表优先：从资源管理器复制文件时，剪贴板里同时有 CF_HDROP
        #    和一份文字路径，按文件处理才符合用户预期。
        if options.get("capture_files", True) and CF_HDROP in format_ids:
            files = _read_files_opened()
            if files:
                data = _build_files_data(files, format_ids)
                if data is not None:
                    return data

        # ② 位图次之：截图工具复制后一般只有位图
        for fmt, is_v5 in ((CF_DIBV5, True), (CF_DIB, False)):
            if fmt in format_ids:
                image_data = _read_image_opened(fmt, is_v5)
                if image_data is not None:
                    data = ClipboardData(
                        "image", image_bytes=image_data[0],
                        clipboard_formats=sorted(format_ids),
                    )
                    # 位图旁边若还有纯文本（例如 Excel 复制单元格），
                    # 仍然按图片入库：用户看到的是图，粘出来的也应该是图。
                    return data

        # ③ HTML 富文本
        if options.get("capture_rich_text", True) and html_id and html_id in format_ids:
            html = _read_html_opened(html_id)
            if html:
                plain = _read_text_opened() or ""
                return ClipboardData(
                    "rich_text", text=plain, html=html,
                    clipboard_formats=sorted(format_ids),
                )

        # ④ 纯文本兜底
        text = _read_text_opened()
        if text is not None and text.strip():
            return ClipboardData("text", text=text, clipboard_formats=sorted(format_ids))
    return None


def _build_files_data(files, format_ids):
    """单个图片文件按图片采集，其余按文件列表采集（UC-01 备选流）。"""
    if len(files) == 1:
        lower = files[0].lower()
        if lower.endswith(IMAGE_EXTENSIONS):
            try:
                image = image_utils.open_image_file(files[0])
                return ClipboardData(
                    "image",
                    image_bytes=image_utils.image_to_png_bytes(image),
                    files=files,
                    clipboard_formats=sorted(format_ids),
                )
            except AppError:
                # 图片打不开就降级为文件记录，内容不能丢（业务规则 R4 精神）
                pass
    return ClipboardData("files", files=files, clipboard_formats=sorted(format_ids))


# ---------------------------------------------------------------------------
# 各格式的具体读取（均要求剪贴板已打开）
# ---------------------------------------------------------------------------
def _enum_formats_opened():
    out = []
    fmt = win32clipboard.EnumClipboardFormats(0)
    while fmt:
        try:
            name = win32clipboard.GetClipboardFormatName(fmt)
        except Exception:  # noqa: BLE001
            name = ""
        out.append((fmt, name))
        fmt = win32clipboard.EnumClipboardFormats(fmt)
    return out


def _register_html_opened():
    try:
        return win32clipboard.RegisterClipboardFormat(HTML_FORMAT_NAME)
    except Exception:  # noqa: BLE001
        return 0


def _read_text_opened():
    try:
        if win32clipboard.IsClipboardFormatAvailable(CF_UNICODETEXT):
            value = win32clipboard.GetClipboardData(CF_UNICODETEXT)
            return value if isinstance(value, str) else str(value)
    except Exception:  # noqa: BLE001
        pass
    try:
        if win32clipboard.IsClipboardFormatAvailable(CF_TEXT):
            value = win32clipboard.GetClipboardData(CF_TEXT)
            if isinstance(value, bytes):
                for encoding in ("utf-8", "gbk", "latin-1"):
                    try:
                        return value.decode(encoding)
                    except UnicodeDecodeError:
                        continue
            return value
    except Exception:  # noqa: BLE001
        pass
    return None


def _read_html_opened(html_id):
    """读取 CF_HTML，按 StartFragment/EndFragment 偏移取出片段。"""
    try:
        raw = win32clipboard.GetClipboardData(html_id)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(raw, bytes):
        text = raw.decode("utf-8", "ignore")
    else:
        text = str(raw)
    return extract_html_fragment(text)


def extract_html_fragment(cf_html_text):
    """从 CF_HTML 全文中抠出真正的 HTML 片段。

    CF_HTML 的头部是 "键:值" 文本行，其中 StartHTML/StartFragment/EndFragment
    是**字节**偏移。中文内容下字符数与字节数不一致，因此必须按字节切片，
    否则富文本会出现截断或乱码。
    """
    if not cf_html_text:
        return None
    offsets = {}
    for line in cf_html_text.splitlines()[:12]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in ("starthtml", "endhtml", "startfragment", "endfragment"):
            try:
                offsets[key] = int(value.strip())
            except ValueError:
                continue
    data = cf_html_text.encode("utf-8", "ignore")
    start = offsets.get("startfragment")
    end = offsets.get("endfragment")
    if start is None or end is None or start >= end or end > len(data):
        start = offsets.get("starthtml", 0)
        end = offsets.get("endhtml", len(data))
        if start >= end or end > len(data):
            return cf_html_text.strip() or None
    return data[start:end].decode("utf-8", "ignore").strip() or None


def _read_files_opened():
    """读取 CF_HDROP 路径列表。"""
    try:
        data = win32clipboard.GetClipboardData(CF_HDROP)
    except Exception:  # noqa: BLE001
        # pywin32 偶尔解析不出 DROPFILES，退一步用 PIL 的 grabclipboard
        try:
            from PIL import ImageGrab
            data = ImageGrab.grabclipboard()
        except Exception:  # noqa: BLE001
            return []
    if isinstance(data, (list, tuple)):
        return [str(p) for p in data if p]
    return []


def _read_image_opened(fmt, is_v5):
    """读取位图，返回 (PNG 字节, 原始 DIB 字节)。"""
    try:
        dib = win32clipboard.GetClipboardData(fmt)
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(dib, bytes) or not dib:
        return None
    image = image_utils.dib_to_image(dib, is_v5=is_v5)
    return image_utils.image_to_png_bytes(image), dib


# ---------------------------------------------------------------------------
# 写回剪贴板（UC-09 主事件流第 3 步）
# ---------------------------------------------------------------------------
def write_text(text):
    """写纯文本（先清空剪贴板，保证粘出来的是干净的一条内容）。"""
    _require_pywintypes()
    with clipboard_session(write=True) as ok:
        if not ok:
            raise AppError(ERR_INTERNAL, "写入剪贴板失败（被其他程序独占）")
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(CF_UNICODETEXT, text or "")
    return True


def write_text_and_html(text, html):
    """同时写纯文本与 CF_HTML，供富文本记录粘贴（UC-09 主事件流第 3 步）。

    两个格式必须在**同一次**打开期间写入，否则第二次 EmptyClipboard
    会把第一次写的内容清掉。
    """
    _require_pywintypes()
    cf_html = build_cf_html(html)
    html_id = 0
    with clipboard_session(write=True) as ok:
        if not ok:
            raise AppError(ERR_INTERNAL, "写入剪贴板失败（被其他程序独占）")
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(CF_UNICODETEXT, text or "")
        try:
            html_id = win32clipboard.RegisterClipboardFormat(HTML_FORMAT_NAME)
            win32clipboard.SetClipboardData(html_id, cf_html.encode("utf-8"))
        except Exception:  # noqa: BLE001 - 富文本写失败时纯文本仍然可用
            pass
    return True


def write_image(png_bytes):
    """写位图（CF_DIB）。"""
    _require_pywintypes()
    from PIL import Image
    import io as _io
    image = Image.open(_io.BytesIO(png_bytes))
    image.load()
    dib = image_utils.image_to_dib_bytes(image)
    with clipboard_session(write=True) as ok:
        if not ok:
            raise AppError(ERR_INTERNAL, "写入剪贴板失败（被其他程序独占）")
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(CF_DIB, dib)
    return True


def write_files(paths):
    """写文件路径列表（CF_HDROP），供粘贴到资源管理器或支持文件拖放的软件。"""
    _require_pywintypes()
    paths = [str(p) for p in (paths or []) if p]
    if not paths:
        raise AppError(ERR_INTERNAL, "文件列表为空，无法粘贴")
    data = build_dropfiles(paths)
    with clipboard_session(write=True) as ok:
        if not ok:
            raise AppError(ERR_INTERNAL, "写入剪贴板失败（被其他程序独占）")
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(CF_HDROP, data)
    return True


def _require_pywintypes():
    if win32clipboard is None:
        raise AppError(ERR_INTERNAL, "缺少 pywin32，无法读写剪贴板", "pip install pywin32")


def build_dropfiles(paths):
    """构造 CF_HDROP 需要的 DROPFILES 结构。

    结构：DROPFILES(20 字节) + 以 \\0 分隔、以 \\0\\0 结尾的宽字符路径列表。
    """
    encoded = ("\0".join(paths) + "\0\0").encode("utf-16-le")
    header = struct.pack("<IiiII", 20, 0, 0, 0, 1)  # pFiles=20, pt=(0,0), fNC=0, fWide=1
    return header + encoded


def build_cf_html(fragment, source_url=None):
    """把 HTML 片段包装成 CF_HTML 全文（含头部偏移）。

    偏移必须按**字节**计算：头部本身长度会影响后续偏移，因此先按占位符
    算一次头部长度，再回填真实偏移。
    """
    fragment = fragment or ""
    template = (
        "Version:0.9\r\n"
        "StartHTML:{start_html:010d}\r\n"
        "EndHTML:{end_html:010d}\r\n"
        "StartFragment:{start_fragment:010d}\r\n"
        "EndFragment:{end_fragment:010d}\r\n"
    )
    if source_url:
        template += "SourceURL:%s\r\n" % source_url
    placeholder = template.format(
        start_html=0, end_html=0, start_fragment=0, end_fragment=0
    )
    base = len(placeholder.encode("utf-8"))

    head = "<html><body>\r\n<!--StartFragment-->"
    tail = "<!--EndFragment-->\r\n</body></html>"
    fragment_bytes = fragment.encode("utf-8")
    start_fragment = base + len(head.encode("utf-8"))
    end_fragment = start_fragment + len(fragment_bytes)
    start_html = base
    end_html = end_fragment + len(tail.encode("utf-8"))

    header = template.format(
        start_html=start_html, end_html=end_html,
        start_fragment=start_fragment, end_fragment=end_fragment,
    )
    return header + head + fragment + tail


# ---------------------------------------------------------------------------
# 内容长度保护
# ---------------------------------------------------------------------------
def clamp_content(text, limit=MAX_CONTENT_LEN):
    """超大内容截断（UC-01 备选流 A2）。返回 (内容, 是否被截断)。"""
    if text is None:
        return "", False
    if len(text) <= limit:
        return text, False
    return text[:limit], True
