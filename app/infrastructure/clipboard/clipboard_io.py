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
from app.infrastructure.clipboard import rich_text
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
                # 顺手把命名格式（RTF / 电子表格私有格式）打包进 HTML 注释，
                # 跟着记录一起走；粘回去时再取出来还原
                formats = read_named_formats(
                    format_ids, options.get("source_app"), options.get("logger"))
                html = rich_text.embed_named_formats(html, formats)
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
    """读取 CF_HTML 并取出 HTML 片段。"""
    try:
        raw = win32clipboard.GetClipboardData(html_id)
    except Exception:  # noqa: BLE001
        return None
    # 原始字节**直接**交给解析器：CF_HTML 可能是 UTF-8 也可能是 UTF-16LE。
    # 这里若先 decode("utf-8") 一次，宽字符会被解成夹着一堆 NUL 的字符串，
    # 后面的 UTF-16 识别就再也没机会生效了（这个坑踩过）。
    return extract_html_fragment(raw)


def extract_html_fragment(cf_html_text):
    """从 CF_HTML 全文中抠出真正的 HTML 片段。

    实现搬到 ``rich_text.parse_cf_html``：那里做了 UTF-16 识别、优先整篇
    偏移（保住 Word 的 CSS）、以及 WPS 被吃掉 ``<`` 的修复。
    """
    return rich_text.parse_cf_html(cf_html_text)


def read_named_formats(format_ids, source_app=None, logger=None):
    """读取值得保留的命名剪贴板格式（要求剪贴板已打开）。

    只保留两类：

    * **Rich Text Format** —— 任何来源都留。它是"复制 Word/网页 → 粘回
      支持 RTF 的程序"保真的关键。
    * **电子表格类来源的私有格式**（Excel / WPS / ET / Calc）—— Biff 之类
      带着公式与单元格格式，只存 HTML 的话粘回 Excel 会掉格式。

    其余格式（各种 OLE / Shell 内部格式）留着没用，还会把数据库撑大，
    一律丢弃。
    """
    out = []
    total = 0
    spreadsheet = looks_like_spreadsheet(source_app)
    for fmt in format_ids:
        try:
            name = win32clipboard.GetClipboardFormatName(fmt)
        except Exception:  # noqa: BLE001 - 标准格式没有名字，跳过
            continue
        if not name:
            continue
        keep = name.lower() == rich_text.RTF_FORMAT_NAME.lower() or spreadsheet
        if not keep:
            continue
        if name.lower() in _NAMED_FORMAT_IGNORE:
            continue
        try:
            data = win32clipboard.GetClipboardData(fmt)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data, str):
            data = data.encode("utf-8", "ignore")
        if not isinstance(data, (bytes, bytearray)) or not data:
            continue
        if len(data) > rich_text.MAX_FORMAT_BYTES:
            continue
        if total + len(data) > rich_text.MAX_TOTAL_FORMAT_BYTES:
            break
        total += len(data)
        out.append({"name": name, "data": bytes(data)})
        if len(out) >= rich_text.MAX_FORMAT_COUNT:
            break
    return out


#: 这些命名格式没必要留（体积大且粘回去也没用）
_NAMED_FORMAT_IGNORE = frozenset((
    "html format", "png", "image/png", "jpeg", "jfif", "image/jpeg",
    "gif", "animated gif", "image/gif", "bitmap", "dib", "dibv5",
    "dataobject", "ole private data", "embed source", "link source",
    "object descriptor", "ownerlink", "shell idlist array",
    "preferred dropeffect", "performed dropeffect", "filegroupdescriptorw",
    "uniformresourcelocator", "uniformresourcelocatorw", "inetturl",
))

#: 看起来像电子表格的进程名 / 标题关键字
_SPREADSHEET_HINTS = ("excel", "wps", "et.exe", "spreadsheet", "calc",
                      "numbers", "wps.exe", "kingsoft")


def looks_like_spreadsheet(source_app):
    """来源是不是电子表格程序（决定要不要保留它的私有格式）。"""
    if not source_app:
        return False
    lowered = str(source_app).lower()
    return any(hint in lowered for hint in _SPREADSHEET_HINTS)


def _append_named_formats(formats):
    """在**不清空**剪贴板的前提下追加命名格式。

    必须在 write_text/write_image 之后调用——那些函数开头会
    ``EmptyClipboard()``，先追加会被清掉。
    """
    if not formats:
        return 0
    _require_pywintypes()
    written = 0
    with clipboard_session(write=True) as ok:
        if not ok:
            return 0
        for item in formats:
            name = item.get("name")
            data = item.get("data")
            if not name or not data:
                continue
            try:
                fmt = win32clipboard.RegisterClipboardFormat(name)
                win32clipboard.SetClipboardData(fmt, data)
                written += 1
            except Exception:  # noqa: BLE001 - 单个格式失败不影响其他格式
                continue
    return written


def write_text_html_and_formats(text, html):
    """写富文本：纯文本 + HTML + 随记录一起走的命名格式。

    TieZ 的做法也是"先写正文与 HTML，再追加命名格式"——顺序不能反。
    """
    clean = html or ""
    clean, formats = rich_text.extract_named_formats(clean)
    clean, _image = rich_text.extract_image_fallback(clean)
    write_text_and_html(text or "", clean)
    _append_named_formats(formats)
    return True


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


def clear_clipboard():
    """清空剪贴板。"""
    _require_pywintypes()
    with clipboard_session(write=True) as ok:
        if not ok:
            return False
        win32clipboard.EmptyClipboard()
    return True


#: 快照哨兵：剪贴板上确实有内容，但**没有我们能识别的格式**。
#: 这种情况下还原时什么都不做——总比把用户的原始内容清掉要好。
SNAPSHOT_UNSUPPORTED = object()


def snapshot_clipboard(options=None):
    """抓一份当前剪贴板的快照，供"临时粘贴"之后还原。

    返回值三态：

    * ``ClipboardData``        —— 正常快照，可以还原
    * ``SNAPSHOT_UNSUPPORTED`` —— 有内容但读不懂，**不要动它**
    * ``None``                 —— 剪贴板本来就是空的，还原时清空即可
    """
    try:
        data = read_clipboard_data(options)
    except Exception:  # noqa: BLE001 - 快照失败不该影响粘贴本身
        return SNAPSHOT_UNSUPPORTED
    if data is not None:
        return data
    try:
        if enum_formats():
            return SNAPSHOT_UNSUPPORTED
    except Exception:  # noqa: BLE001
        return SNAPSHOT_UNSUPPORTED
    return None


def restore_clipboard(snapshot):
    """把快照写回剪贴板。

    只写回我们**读得懂**的那一种格式。剪贴板里原本并存的其他格式
    （Excel 私有格式之类）在快照时就丢了——这是 Windows 剪贴板模型
    本身的限制，如实记在这里，不假装能完整还原。
    """
    if snapshot is SNAPSHOT_UNSUPPORTED:
        return False
    if snapshot is None:
        return clear_clipboard()
    try:
        if snapshot.kind == "image" and snapshot.image_bytes:
            return write_image(snapshot.image_bytes)
        if snapshot.kind == "files" and snapshot.files:
            return write_files(snapshot.files)
        if snapshot.kind == "rich_text" and snapshot.html:
            return write_text_and_html(snapshot.text or "", snapshot.html)
        return write_text(snapshot.text or "")
    except AppError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise AppError(ERR_INTERNAL, "还原剪贴板失败：%s" % exc)


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
