# -*- coding: utf-8 -*-
"""敏感内容加密落盘（F9-3）：Windows DPAPI

用 ``CryptProtectData`` / ``CryptUnprotectData``（crypt32.dll）：

* 加密密钥由 Windows 按**当前用户**派生，程序自己不留密钥；
* 换用户、换机器都解不开——这正是我们要的；
* 不需要用户设密码，也就不存在"忘记密码导致数据全丢"。

密文写成 ``dpapi:<base64>``。这个前缀是**自描述**的：读到一条记录先看前缀
就知道要不要解密，不需要额外的标志位，也就不会出现"标志位和实际内容对不上"
的烂账（那种情况要么解密失败、要么明文被当成密文）。

**范围上刻意的取舍**：只加密文本类记录（text / code / url / rich_text）。
图片与文件类记录的 ``content`` 是磁盘路径，加密之后附件清理、打开内容、
换数据目录时的路径重写就全对不上了。这两类本来也不承载手机号/身份证这类
文本隐私。
"""

import base64
import ctypes
import ctypes.wintypes as wt

#: 密文前缀
ENCRYPT_PREFIX = "dpapi:"

#: 只对这些类型加密（见模块开头的说明）
ENCRYPTABLE_TYPES = ("text", "code", "url", "rich_text")

#: CryptProtectData 的标志：不允许弹 UI（后台服务里弹窗会卡死）
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _crypt32():
    lib = ctypes.WinDLL("crypt32", use_last_error=True)
    lib.CryptProtectData.restype = wt.BOOL
    lib.CryptProtectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), wt.LPCWSTR, ctypes.POINTER(DATA_BLOB),
        ctypes.c_void_p, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    lib.CryptUnprotectData.restype = wt.BOOL
    lib.CryptUnprotectData.argtypes = [
        ctypes.POINTER(DATA_BLOB), ctypes.POINTER(wt.LPWSTR),
        ctypes.POINTER(DATA_BLOB), ctypes.c_void_p, ctypes.c_void_p,
        wt.DWORD, ctypes.POINTER(DATA_BLOB),
    ]
    return lib


def _kernel32():
    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.LocalFree.restype = ctypes.c_void_p
    lib.LocalFree.argtypes = [ctypes.c_void_p]
    return lib


try:  # pragma: no cover - 非 Windows 平台直接降级
    _CRYPT32 = _crypt32()
    _KERNEL32 = _kernel32()
except OSError:  # pragma: no cover
    _CRYPT32 = None
    _KERNEL32 = None


def is_available():
    """当前平台是否支持 DPAPI 加密。"""
    return _CRYPT32 is not None and _KERNEL32 is not None


def is_encrypted(value):
    """这个值是不是 DPAPI 密文。"""
    return bool(value) and str(value).startswith(ENCRYPT_PREFIX)


def _to_blob(data):
    buffer = ctypes.create_string_buffer(data, len(data))
    return DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def encrypt(plain):
    """加密一段文本。失败返回 None（调用方应当按原文落库并记日志）。

    返回 None 而不是抛异常：加密是"尽力而为"的保护，不该因为它失败
    就让用户连复制都用不了。
    """
    if not is_available():
        return None
    if plain is None:
        return None
    try:
        raw = str(plain).encode("utf-8")
    except (UnicodeError, ValueError):
        return None
    if not raw:
        return None
    in_blob, keepalive = _to_blob(raw)
    out_blob = DATA_BLOB()
    ok = _CRYPT32.CryptProtectData(
        ctypes.byref(in_blob), "clipboardpro", None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob))
    del keepalive
    if not ok:
        return None
    try:
        payload = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _KERNEL32.LocalFree(out_blob.pbData)
    return ENCRYPT_PREFIX + base64.b64encode(payload).decode("ascii")


def decrypt(value):
    """解密一段 DPAPI 密文。不是密文、或解不开都返回 None。"""
    if not is_available() or not is_encrypted(value):
        return None
    try:
        payload = base64.b64decode(str(value)[len(ENCRYPT_PREFIX):])
    except Exception:  # noqa: BLE001
        return None
    if not payload:
        return None
    in_blob, keepalive = _to_blob(payload)
    out_blob = DATA_BLOB()
    ok = _CRYPT32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None,
        CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob))
    del keepalive
    if not ok:
        # 换用户/换机器之后就是这个结果——如实返回 None，让上层决定怎么提示
        return None
    try:
        plain = ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        _KERNEL32.LocalFree(out_blob.pbData)
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError:
        return None


def should_encrypt(entry):
    """这条记录该不该加密。"""
    if entry is None or not getattr(entry, "is_sensitive", 0):
        return False
    if entry.content_type not in ENCRYPTABLE_TYPES:
        return False
    return not is_encrypted(entry.content)


def protect_entry(entry):
    """返回**要写进数据库**的 (content, html_content, preview)。

    敏感文本类记录返回密文；其余原样返回。加密失败时退回明文，
    绝不因为加密出问题就把内容丢掉。
    """
    if not should_encrypt(entry):
        return entry.content, entry.html_content, entry.preview
    content = encrypt(entry.content)
    if content is None:
        return entry.content, entry.html_content, entry.preview
    html = encrypt(entry.html_content) if entry.html_content else entry.html_content
    preview = encrypt(entry.preview) if entry.preview else entry.preview
    return content, html, preview


def unprotect_entry(entry, logger=None):
    """把记录里的密文就地解回明文，返回同一条记录。

    解密失败（比如换了 Windows 用户）时把字段换成一个**明确的占位说明**，
    而不是留一串 base64 让用户对着发呆。
    """
    if entry is None:
        return entry
    for field in ("content", "html_content", "preview"):
        value = getattr(entry, field, None)
        if not is_encrypted(value):
            continue
        plain = decrypt(value)
        if plain is None:
            if field == "content":
                plain = "[无法解密：该记录由其他 Windows 用户加密]"
            elif field == "preview":
                plain = "[无法解密的敏感内容]"
            else:
                plain = None
            if logger:
                logger.warning("记录 #%s 的 %s 解密失败", getattr(entry, "id", "?"), field)
        setattr(entry, field, plain)
    return entry


def encrypt_settings_value(value):
    """设置项里的敏感值（如 AI API Key）加密。失败返回原文。"""
    if not value or is_encrypted(value):
        return value
    return encrypt(value) or value


def decrypt_settings_value(value):
    """设置项里的敏感值解密。"""
    if not is_encrypted(value):
        return value
    return decrypt(value) or ""
