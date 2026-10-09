# -*- coding: utf-8 -*-
"""富文本：CF_HTML 解析/构造、Office 噪声清洗、预览截断、附加格式往返

对应 TieZ 的 ``services/clipboard/utils.rs`` 里那一大块富文本处理。要解决
四个真实问题：

1. **CF_HTML 的偏移量是字节不是字符。** 中文内容下两者不等，按字符切会
   截断或乱码。而且 ``StartHTML/EndHTML``（整篇）比
   ``StartFragment/EndFragment``（片段）信息更全——**优先用整篇**，
   否则 Word 复制过来的 CSS 全丢，粘回去样式就没了。
2. **WPS 会把片段开头的 ``<`` 吃掉**，发出 ``table border=0 ...>`` 这种
   半截标签，直接塞进浏览器就是一团文本。需要按"看起来像标签"补回 ``<``。
3. **Office 的 HTML 里塞满了 ``mso-*`` 样式和注释**，体积能占一多半，
   预览时毫无用处。按特征签名识别后再删，不能无差别删 ``<style>``
   （用户自己写的样式还要留着）。
4. **Excel 复制出来的不只是 HTML**，还有 RTF / Biff 等私有格式。
   只存 HTML 的话，"复制 Excel → 粘回 Excel"会丢掉公式与格式。
   所以把这些格式原样打包，藏在 HTML 注释里跟着记录一起走。

标记的载体是 HTML 注释，因此 ``html_content`` 的 schema 不用改：

    <!--CLIPBOARDPRO_RICH_FORMATS:<base64(JSON)>-->
    <!--CLIPBOARDPRO_RICH_IMAGE:<payload>-->

任何要展示 HTML 的地方（预览、摘要）都必须先过 :func:`strip_markers`，
否则用户会在预览里看到一大串 base64。
"""

import base64
import json
import re

#: 命名格式的载体标记
NAMED_FORMATS_MARK = "CLIPBOARDPRO_RICH_FORMATS"
#: 位图回退的载体标记（Excel 复制时 HTML 之外还有一张图，预览用图更准）
IMAGE_FALLBACK_MARK = "CLIPBOARDPRO_RICH_IMAGE"

#: RTF 格式在 Windows 里的注册名。任何来源都值得保留它。
RTF_FORMAT_NAME = "Rich Text Format"

_MARKER_RE = re.compile(
    r"<!--\s*(?:%s|%s)\s*:.*?-->" % (NAMED_FORMATS_MARK, IMAGE_FALLBACK_MARK),
    re.DOTALL,
)

#: 单个命名格式的最大字节数
MAX_FORMAT_BYTES = 512 * 1024
#: 全部命名格式加起来的最大字节数
MAX_TOTAL_FORMAT_BYTES = 1024 * 1024
#: 最多保留几个命名格式
MAX_FORMAT_COUNT = 8

#: 预览用 HTML 的长度上限
HTML_PREVIEW_MAX_CHARS = 5000
#: 预览用表格最多保留几行
HTML_PREVIEW_MAX_ROWS = 10

#: Office 专有样式的特征签名。命中才删，避免误删用户自己的样式。
_OFFICE_NOISE_SIGNATURES = (
    "mso-style-name", "mso-", "table.mso", "microsoftinternetexplorer",
    "documentnotspecified", "wps office", "office word", "msonormal",
    "/* style definitions */", "mso-bidi", "mso-fareast",
)

#: 片段可能以这些标签开头（WPS 吃掉 ``<`` 之后剩下的部分）
_FRAGMENT_TAG_RE = re.compile(
    r"^(table|tbody|thead|tfoot|tr|td|th|colgroup|col|div|span|p|ul|ol|li|"
    r"blockquote|pre|h[1-6]|meta|style|img|a|b|i|u|em|strong|font|br)\b[^>]*>",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# CF_HTML 解析
# ---------------------------------------------------------------------------
def parse_cf_html(raw):
    """从 CF_HTML 全文里抠出 HTML 片段。认不出就返回清洗过的原文。

    解析顺序（每一步都是上一步失败后的退路）：

    1. 是 UTF-16LE 就转成 UTF-8（有些程序按宽字符写）
    2. 读头部偏移，**优先 StartHTML/EndHTML**（整篇，保住 CSS），
       再退回 StartFragment/EndFragment
    3. 没有可用偏移就砍掉 ``版本:/StartHTML:`` 这些头行
    4. 再不行就找第一个像标签的 ``<`` 当起点
    """
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = _decode_cf_html_bytes(raw)
    elif isinstance(raw, str) and "\x00" in raw[:64]:
        # pywin32 有时把 UTF-16 的 HGLOBAL 当单字节解码，交回来一个夹着
        # NUL 的字符串。转回字节再按 UTF-16 解一遍就能救回来。
        try:
            raw = raw.encode("latin-1", "ignore").decode("utf-16-le", "ignore")
        except (UnicodeDecodeError, UnicodeEncodeError):
            pass
    if not raw.strip():
        return None

    offsets = _parse_offsets(raw)
    data = raw.encode("utf-8", "ignore")

    for start_key, end_key in (("starthtml", "endhtml"),
                               ("startfragment", "endfragment")):
        start = offsets.get(start_key)
        end = offsets.get(end_key)
        if start is None or end is None:
            continue
        if 0 <= start < end <= len(data):
            piece = data[start:end].decode("utf-8", "ignore")
            if start_key == "starthtml" and _document_carries_styling(piece):
                # 整篇确实带着样式：按整篇保留，别把 <head> 里的 CSS 丢掉
                cleaned = _clean_fragment(piece, keep_document=True)
            else:
                # 多数复制外面只是一层空壳 <html><body>。这时取干净的片段：
                # 既不给每条记录白包一层壳，也让"建 → 解"保持对称。
                cleaned = _clean_fragment(piece)
            if cleaned:
                return cleaned

    # 退路一：把头部的键值行砍掉
    body = _strip_header_lines(raw)
    cleaned = _clean_fragment(body)
    if cleaned:
        return cleaned

    # 退路二：找第一个像标签的 <
    match = re.search(r"<(?=[a-zA-Z!/])", raw)
    if match:
        cleaned = _clean_fragment(raw[match.start():])
        if cleaned:
            return cleaned
    return raw.strip() or None


def _decode_cf_html_bytes(raw):
    """CF_HTML 可能是 UTF-8 也可能是 UTF-16LE，按 BOM 与零字节分布判断。"""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        try:
            return raw.decode("utf-16")
        except UnicodeDecodeError:
            pass
    # 没有 BOM：ASCII 字符的宽字符编码会在奇数位出现大量 0x00
    sample = raw[:64]
    if len(sample) >= 4 and sample[1] == 0 and sample[3] == 0:
        try:
            return raw.decode("utf-16-le", "ignore")
        except UnicodeDecodeError:
            pass
    return raw.decode("utf-8", "ignore")


def _parse_offsets(text):
    offsets = {}
    for line in text.splitlines()[:16]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        if key in ("starthtml", "endhtml", "startfragment", "endfragment"):
            try:
                offsets[key] = int(value.strip())
            except ValueError:
                continue
    return offsets


def _strip_header_lines(text):
    """把 ``Version:`` / ``StartHTML:`` 这类头行去掉，剩下正文。"""
    lines = text.splitlines()
    index = 0
    for index, line in enumerate(lines[:16]):
        if not re.match(r"^\s*[A-Za-z]+:", line):
            break
    else:
        index = len(lines)
    return "\n".join(lines[index:])


def _document_carries_styling(document):
    """整篇里、**片段标记之外**是否带着样式信息。

    决定用整篇还是用片段：只有真的带样式（``<style>``、``<link>``、
    ``style=`` 属性）才值得为它多包一层 ``<html><head>``。
    """
    start = document.find("<!--StartFragment-->")
    end = document.find("<!--EndFragment-->")
    if start < 0 or end < 0:
        return bool(_STYLING_RE.search(document))
    outside = document[:start] + document[end:]
    return bool(_STYLING_RE.search(outside))


_STYLING_RE = re.compile(r"<(style|link)\b|style\s*=", re.IGNORECASE)


def _clean_fragment(piece, keep_document=False):
    """去掉片段两端的空白与 BOM，必要时补回被吃掉的 ``<``。

    ``keep_document=True`` 表示切出来的已经是整篇（含 ``<head>``），
    这时**不能**再缩到 StartFragment/EndFragment 之间，否则 CSS 全丢。
    """
    if not piece:
        return None
    text = piece.replace("\ufeff", "").strip()
    if not text:
        return None
    if not keep_document:
        start_mark = text.find("<!--StartFragment-->")
        end_mark = text.find("<!--EndFragment-->")
        if 0 <= start_mark < end_mark:
            text = text[start_mark + len("<!--StartFragment-->"):end_mark].strip()
    return repair_html_fragment(text)


def repair_html_fragment(html):
    """修复"开头少了一个 ``<``"的片段（WPS 的经典毛病）。

    WPS 有时发出来的是 ``table border=0 ...>`` —— 开头的 ``<`` 没了。
    这时要塞进浏览器只会显示成一团文本，所以按"看起来像标签就补回来"。
    """
    if not html:
        return html
    text = html.lstrip()
    if text.startswith("<"):
        return html
    if _FRAGMENT_TAG_RE.match(text):
        return "<" + text
    return html


# ---------------------------------------------------------------------------
# Office 噪声清洗
# ---------------------------------------------------------------------------
def strip_office_noise(html):
    """删掉 Office 特有的样式块与注释，保留用户自己的样式。

    只删**同时满足**"是 style/注释"且"内容命中 Office 签名"的块。
    无差别删 ``<style>`` 会把用户网页里真正有用的样式也删掉。
    """
    if not html:
        return html
    text = html

    # XML 声明与 Office 命名空间块，删了不影响渲染
    text = re.sub(r"<\?xml[^>]*\?>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"<xml>.*?</xml>", "", text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r"<o:p\s*/?>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?o:[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?w:[^>]*>", "", text, flags=re.IGNORECASE)
    text = re.sub(r"</?v:[^>]*>", "", text, flags=re.IGNORECASE)

    def drop_style(match):
        body = match.group(0)
        lowered = body.lower()
        if len(body) > 24 and any(sig in lowered for sig in _OFFICE_NOISE_SIGNATURES):
            return ""
        return body

    text = re.sub(r"<style[^>]*>.*?</style>", drop_style, text,
                  flags=re.DOTALL | re.IGNORECASE)

    def drop_comment(match):
        body = match.group(0)
        lowered = body.lower()
        if len(body) > 24 and any(sig in lowered for sig in _OFFICE_NOISE_SIGNATURES):
            return ""
        return body

    text = re.sub(r"<!--(?!\[if).*?-->", drop_comment, text, flags=re.DOTALL)

    # 没被 <table> 包住的裸 <tr>/<td>（Excel 常见）补一层壳，否则表格会散架
    if "<table" not in text.lower() and re.search(r"<(tr|td|th)\b", text, re.IGNORECASE):
        text = '<table border="0" cellspacing="0" cellpadding="0">%s</table>' % text
    return text


# ---------------------------------------------------------------------------
# 预览截断
# ---------------------------------------------------------------------------
def truncate_html_for_preview(html, max_chars=HTML_PREVIEW_MAX_CHARS,
                              max_rows=HTML_PREVIEW_MAX_ROWS):
    """把超长 HTML 截到适合预览的长度。返回 (html, 是否截断)。

    表格走单独策略：在**第 N 个 ``</tr>`` 之后**切，并补上闭合标签。
    按字符数硬切会把一行切成两半，预览里出现半个表格。
    """
    if not html or len(html) <= max_chars:
        return html, False

    lowered = html.lower()
    if "<table" in lowered or "<tr" in lowered:
        cut = _cut_after_row(html, max_rows)
        if cut is not None and cut < len(html):
            return (html[:cut]
                    + "</table>"), True

    cut = html.rfind(">", 0, max_chars)
    if cut < max_chars // 2:
        # 找不到像样的闭合点：典型情况是整段超长文本被一个 <p> 包着，
        # 在它前面找 ">" 只会找到 "<p>" 自己，截完就剩三个字符。
        # 这时直接在字符上限处切，并标注已截断。
        return html[:max_chars] + "... [HTML 已截断]", True
    return html[:cut + 1], True


def _cut_after_row(html, max_rows):
    """返回第 max_rows 个 ``</tr>`` 结束处的下标；找不到返回 None。"""
    found = 0
    for match in re.finditer(r"</tr\s*>", html, re.IGNORECASE):
        found += 1
        if found >= max_rows:
            return match.end()
    return None


# ---------------------------------------------------------------------------
# 附加格式 / 位图回退：打包进 HTML 注释
# ---------------------------------------------------------------------------
def strip_markers(html):
    """去掉全部附加标记，返回纯 HTML。

    任何要展示 HTML 的地方都必须先调这个，否则预览里会冒出一大串 base64。
    """
    if not html:
        return html
    return _MARKER_RE.sub("", html)


def has_markers(html):
    return bool(html) and bool(_MARKER_RE.search(html))


def embed_named_formats(html, formats):
    """把命名格式打包成注释追加到 HTML 末尾。

    ``formats`` 形如 ``[{"name": "Rich Text Format", "data": b"..."}]``。
    """
    if not formats:
        return html
    payload = []
    total = 0
    for item in formats[:MAX_FORMAT_COUNT]:
        raw = item.get("data") or b""
        if not raw or len(raw) > MAX_FORMAT_BYTES:
            continue
        if total + len(raw) > MAX_TOTAL_FORMAT_BYTES:
            break
        total += len(raw)
        payload.append({
            "name": item.get("name") or "",
            "data": base64.b64encode(raw).decode("ascii"),
        })
    if not payload:
        return html
    blob = base64.b64encode(
        json.dumps(payload, ensure_ascii=False).encode("utf-8")).decode("ascii")
    return "%s<!--%s:%s-->" % (html or "", NAMED_FORMATS_MARK, blob)


def extract_named_formats(html):
    """取出命名格式，返回 (去掉标记的 HTML, formats)。"""
    formats = []
    clean = html or ""
    for match in re.finditer(
            r"<!--\s*%s\s*:(.*?)-->" % NAMED_FORMATS_MARK, html or "", re.DOTALL):
        clean = clean.replace(match.group(0), "")
        try:
            decoded = base64.b64decode(match.group(1).strip())
            items = json.loads(decoded.decode("utf-8"))
        except Exception:  # noqa: BLE001 - 坏掉的标记直接忽略，不能让整条记录打不开
            continue
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            try:
                data = base64.b64decode(item.get("data") or "")
            except Exception:  # noqa: BLE001
                continue
            if data:
                formats.append({"name": item.get("name") or "", "data": data})
    return clean, formats


def embed_image_fallback(html, payload):
    """记下"这次复制还带了一张位图"。

    用途是预览：Excel 复制出来的 HTML 渲染出来跟实际表格差很远，
    而随附的位图才是用户真正看到的样子。
    """
    if not payload:
        return html
    return "%s<!--%s:%s-->" % (html or "", IMAGE_FALLBACK_MARK, payload)


def extract_image_fallback(html):
    """取出位图回退载荷，返回 (去掉标记的 HTML, payload 或 None)。"""
    payload = None
    clean = html or ""
    for match in re.finditer(
            r"<!--\s*%s\s*:(.*?)-->" % IMAGE_FALLBACK_MARK, html or "", re.DOTALL):
        clean = clean.replace(match.group(0), "")
        payload = match.group(1).strip() or payload
    return clean, payload
