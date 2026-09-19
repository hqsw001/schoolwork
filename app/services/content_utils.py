# -*- coding: utf-8 -*-
"""内容识别与指纹（F4-1 内容类型自动识别 / F6-1 文本指纹）

类型判定规则完整照搬《02-用例图与用例说明》UC-02 主事件流第 5 步与
《需求分析报告》6.1.4 的约定，逐条对齐，不做自由发挥：

    ① 位图 / 图片文件      -> image
    ② 文件路径列表        -> file
    ③ HTML 富文本         -> rich_text（同时保留 HTML 原文）
    ④ 纯文本启发式判定：
        4.1 以 www. 开头，或含 :// 且协议部分合法 -> url
        4.2 统计代码特征得分 >= 2 -> code
        4.3 以 { 开头、以 } 结尾且同时含冒号与双引号 -> code（覆盖 JSON 片段）
        4.4 其余 -> text
"""

import hashlib
import re
import unicodedata

from app.constants import (
    TYPE_CODE,
    TYPE_FILE,
    TYPE_IMAGE,
    TYPE_RICH_TEXT,
    TYPE_TEXT,
    TYPE_URL,
)

# ---------------------------------------------------------------------------
# URL 判定
# ---------------------------------------------------------------------------
#: 常见协议；判定时要求协议后紧跟 ://
_URL_SCHEME_RE = re.compile(
    r"^(?:https?|ftp|ftps|file|mailto|ssh|git|ws|wss|tel|magnet)://", re.IGNORECASE
)
_URL_LOOSE_RE = re.compile(r"://")
_WWW_RE = re.compile(r"^www\.", re.IGNORECASE)


def looks_like_url(text):
    """URL 判定（UC-02 主事件流 5.1）。

    两点约束，避免把普通文本误判成网址：
        1. 整段内容不含空白字符（一条网址中间不会出现空格）；
        2. 以 www. 开头，或带 :// 且协议部分合法。
    """
    if not text:
        return False
    candidate = text.strip()
    if not candidate or len(candidate) > 2048:
        return False
    if any(ch.isspace() for ch in candidate):
        return False
    if _WWW_RE.match(candidate):
        return True
    if _URL_LOOSE_RE.search(candidate):
        return bool(_URL_SCHEME_RE.match(candidate))
    return False


# ---------------------------------------------------------------------------
# 代码判定
# ---------------------------------------------------------------------------
#: 代码特征关键字，命中一个加 1 分
CODE_KEYWORDS = (
    "import ", "from ", "const ", "let ", "var ", "function ", "class ",
    "def ", "pub fn", "fn ", "impl ", "#include", "package ", "interface ",
    "namespace ", "void ", "return ", "public ", "private ", "static ",
    "SELECT ", "INSERT INTO", "UPDATE ", "DELETE FROM", "console.log",
    "printf(", "std::", "struct ", "enum ", "async ", "await ", "lambda ",
    "elif ", "<?php", "#!/",
)

#: 明显不是代码的"人话"特征：命中就直接否决代码判定。
#: 这一条是需求文档没有写、但实测必须加的规则——
#: "请把 return 的结果发我" 这类正常中文句子会命中关键字，
#: 不加否决条件会被误判为代码（UC-02 业务规则 R1 要求判定与实际内容一致）。
_PROSE_MARKERS = ("。", "，", "、", "；", "？", "！", "“", "”", "《", "》")


def code_score(text):
    """统计代码特征得分（UC-02 主事件流 5.2）。

    命中关键字各加 1 分；包含分号加 1 分；同时包含花括号加 1 分；
    同时包含 </ 与 > 加 2 分（HTML/XML 特征）。
    """
    if not text:
        return 0
    score = 0
    lower = text.lower()
    for keyword in CODE_KEYWORDS:
        if keyword.lower() in lower:
            score += 1
    if ";" in text:
        score += 1
    if "{" in text and "}" in text:
        score += 1
    if "</" in text and ">" in text:
        score += 2
    return score


def looks_like_code(text):
    """代码判定（UC-02 主事件流 5.2 / 5.3）。"""
    if not text:
        return False
    stripped = text.strip()

    # 5.3 JSON 片段：以 { 开头、以 } 结尾且同时包含冒号与双引号
    if stripped.startswith("{") and stripped.endswith("}"):
        if ":" in stripped and '"' in stripped:
            return True

    # 一段正常中文/英文句子即使含 return 之类的词，也不该被判成代码
    if any(marker in stripped for marker in _PROSE_MARKERS):
        return False
    if len(stripped) > 40 and " " in stripped and "\n" not in stripped and ";" not in stripped:
        # 单行、有空格、没有分号的长句基本是自然语言
        if not any(k in stripped.lower() for k in ("{", "}", "</", "def ", "fn ", "class ")):
            return False

    return code_score(stripped) >= 2


# ---------------------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------------------
def detect_content_type(data):
    """按 ClipboardData 的形态直接判定类型；返回 (类型, 正文, HTML)。

    对应 UC-02 的 ① ② ③ 步，不需要再做启发式判断的情况都走这里。
    """
    kind = getattr(data, "kind", None)
    if kind == "image":
        return TYPE_IMAGE, data.image_bytes, None
    if kind == "files":
        content = "\n".join(data.files or [])
        if not data.files:
            return None, None, None
        if len(data.files) == 1 and looks_like_url(data.files[0]):
            return TYPE_FILE, content, None
        return TYPE_FILE, content, None
    if kind == "rich_text":
        html = data.html or ""
        plain = data.text or strip_html(html)
        # UC-02 备选流 A1：富文本与纯文本同时存在且内容实质相同，判定为
        # rich_text，纯文本作为 content 的降级内容保留
        return TYPE_RICH_TEXT, plain, html
    # 纯文本走启发式判定
    text = data.text or ""
    return detect_text_type(text), text, None


def detect_text_type(text):
    """纯文本的启发式类型判定（UC-02 主事件流第 5 步）。"""
    if looks_like_url(text):
        return TYPE_URL
    if looks_like_code(text):
        return TYPE_CODE
    return TYPE_TEXT


# ---------------------------------------------------------------------------
# 文本规范化与指纹
# ---------------------------------------------------------------------------
def normalize_text(text):
    """文本规范化：统一换行符、统一空白（UC-03 主事件流第 3 步 / F6-1）。

    规则：
        - 换行统一为 \\n（覆盖 Windows \\r\\n 与老式 Mac \\r）；
        - 全角空格（U+3000）与不换行空格（U+00A0）归一为普通空格；
        - 去除首尾空白。

    注意：这里**不折叠中间的多余空格**。因为代码类内容里缩进是有意义的，
    折叠会破坏指纹的判别力（UC-02 业务规则 R2：判定为 code 的内容不得被自动改写）。
    """
    if text is None:
        return ""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = text.replace("\u3000", " ").replace("\u00a0", " ")
    return text.strip()


def text_hash(text):
    """文本内容指纹（F6-1）。基于规范化后的**完整内容**计算。

    业务规则（UC-06 R1）：禁止只凭预览文本或内容前若干字符判断，
    避免两条内容前半段相同、后半段不同却被误合并。
    """
    normalized = normalize_text(text)
    digest = hashlib.blake2b(normalized.encode("utf-8", "ignore"), digest_size=8).digest()
    return int.from_bytes(digest, "big") & 0x7FFFFFFFFFFFFFFF


def fingerprint_of(content_type, content, image=None):
    """按类型分派指纹计算。"""
    if content_type == TYPE_IMAGE:
        from app.infrastructure.clipboard import image_utils
        if image is None:
            raise ValueError("图片类型计算指纹必须传入 image")
        return image_utils.image_hash(image)
    if isinstance(content, (bytes, bytearray)):
        return text_hash(content.decode("utf-8", "ignore"))
    return text_hash(content)


# ---------------------------------------------------------------------------
# HTML 处理
# ---------------------------------------------------------------------------
_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_BLOCK_RE = re.compile(r"</(p|div|tr|li|h[1-6]|table|section)>", re.IGNORECASE)
_BR_RE = re.compile(r"<br\s*/?>", re.IGNORECASE)
_ENTITY_MAP = {
    "&nbsp;": " ", "&lt;": "<", "&gt;": ">", "&amp;": "&",
    "&quot;": '"', "&#39;": "'", "&apos;": "'",
}


def strip_html(html):
    """从 HTML 提取可读纯文本（用于富文本记录的内容降级与预览）。

    不是完整的 HTML 解析器：剪贴板里的 HTML 片段结构简单，
    用正则足够，而且不会因为畸形标签把整段内容丢掉（UC-02 异常流 E3）。
    """
    if not html:
        return ""
    text = _SCRIPT_RE.sub("", html)
    text = _BR_RE.sub("\n", text)
    text = _BLOCK_RE.sub("\n", text)
    text = _TAG_RE.sub("", text)
    for entity, char in _ENTITY_MAP.items():
        text = text.replace(entity, char)
    # 折叠多余空行，但保留段落结构
    lines = [line.strip() for line in text.split("\n")]
    return "\n".join(line for line in lines if line).strip()


def normalize_for_compare(text):
    """用于"同一段文字不同换行符"场景的宽松比对（UC-06 主事件流第 4 步）。"""
    if text is None:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return " ".join(text.split()).strip()


def same_text(a, b):
    """两条文本是否实质相同。"""
    return normalize_for_compare(a) == normalize_for_compare(b)
