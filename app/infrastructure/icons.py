# -*- coding: utf-8 -*-
"""图标提取：可执行文件图标、文件类型图标

卡片上要显示"来源应用图标"和"文件类型图标"，Windows 里这两个来源不同：

* **可执行文件图标** → ``ExtractIconExW`` 取出 HICON，画进一块 32 位 DIB，
  再按 BGRA→RGBA 拆成 PNG。pywin32 提供了 ``ExtractIconEx`` 与 DC 操作，
  比手写一遍 ctypes 的 ``CreateDIBSection`` + ``DrawIconEx`` 稳得多。
* **文件类型图标** → ``SHGetFileInfoW``。关键是带 ``SHGFI_USEFILEATTRIBUTES``：
  这样**文件即使不存在**也能按扩展名拿到图标（历史记录里的文件多半早被删了）。

GDI 句柄必须显式释放（``DestroyIcon`` / ``DeleteDC`` / ``DeleteObject``），
否则列表刷几十次就会把进程的 GDI 句柄耗尽——这是这类代码最常见的泄漏。

所有函数失败一律返回 None，绝不抛异常：图标是装饰，不能挡住列表渲染。
"""

import base64
import ctypes
import ctypes.wintypes as wt
import io
import os
import threading

#: 卡片上常用的两种尺寸
SMALL_ICON_SIZE = 16
LARGE_ICON_SIZE = 32

#: SHGetFileInfo 标志
SHGFI_ICON = 0x000000100
SHGFI_LARGEICON = 0x000000000
SHGFI_SMALLICON = 0x000000001
SHGFI_USEFILEATTRIBUTES = 0x000000010
SHGFI_TYPENAME = 0x000000400
SHGFI_DISPLAYNAME = 0x000000200

FILE_ATTRIBUTE_NORMAL = 0x80

MAX_PATH = 260


class SHFILEINFOW(ctypes.Structure):
    _fields_ = [
        ("hIcon", wt.HICON),
        ("iIcon", ctypes.c_int),
        ("dwAttributes", wt.DWORD),
        ("szDisplayName", wt.WCHAR * MAX_PATH),
        ("szTypeName", wt.WCHAR * 80),
    ]


def _shlwapi():
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.SHGetFileInfoW.restype = ctypes.c_void_p
    shell32.SHGetFileInfoW.argtypes = [
        wt.LPCWSTR, wt.DWORD, ctypes.POINTER(SHFILEINFOW), ctypes.c_uint, ctypes.c_uint,
    ]
    return shell32


try:  # pragma: no cover - 非 Windows 直接降级
    _SHELL32 = _shlwapi()
except OSError:  # pragma: no cover
    _SHELL32 = None


# ---------------------------------------------------------------------------
# 进程内缓存
# ---------------------------------------------------------------------------
_lock = threading.RLock()
_icon_cache = {}       # (规范路径, 尺寸, 是否按扩展名) -> base64 字符串或 None


def normalize_key(path):
    """缓存键：统一分隔符、小写、尽量取真实路径。

    同一份文件用 ``C:\\A\\B.exe`` 和 ``c:/a/b.exe`` 引用了两次不该抠两遍图标。
    """
    if not path:
        return ""
    text = str(path).strip().strip('"')
    try:
        text = os.path.abspath(text)
    except (OSError, ValueError):
        pass
    return text.replace("/", "\\").lower()


def clear_cache():
    """清空图标缓存，返回清掉的条目数。"""
    with _lock:
        count = len(_icon_cache)
        _icon_cache.clear()
    return count


def cache_usage():
    with _lock:
        return {"count": len(_icon_cache),
                "bytes": sum(len(v) for v in _icon_cache.values() if v)}


# ---------------------------------------------------------------------------
# 抠图标
# ---------------------------------------------------------------------------
def extract_icon_handle(hicon, size):
    """把 HICON 画成 PIL 图像。失败返回 None。"""
    try:
        import win32gui
        import win32ui
        from PIL import Image
    except ImportError:  # pragma: no cover
        return None

    hdc = None
    memdc = None
    bmp = None
    try:
        hdc = win32ui.CreateDCFromHandle(win32gui.GetDC(0))
        memdc = hdc.CreateCompatibleDC()
        bmp = win32ui.CreateBitmap()
        bmp.CreateCompatibleBitmap(hdc, size, size)
        memdc.SelectObject(bmp)
        memdc.DrawIcon((0, 0), hicon)
        bits = bmp.GetBitmapBits(True)
        image = Image.frombuffer("RGBA", (size, size), bits, "raw", "BGRA", 0, 1)
        image.load()
        return image
    except Exception:  # noqa: BLE001
        return None
    finally:
        # GDI 句柄必须显式释放，否则刷几十次列表就把句柄耗光了
        for obj, method in ((bmp, "DeleteObject"), (memdc, "DeleteDC"), (hdc, "DeleteDC")):
            if obj is None:
                continue
            try:
                getattr(obj, method)()
            except Exception:  # noqa: BLE001
                pass


def extract_executable_icon(path, size=LARGE_ICON_SIZE):
    """取可执行文件的图标。文件不存在或没有图标时返回 None。"""
    if not path or not os.path.isfile(path):
        return None
    try:
        import win32gui
    except ImportError:  # pragma: no cover
        return None
    handles = []
    try:
        large, small = win32gui.ExtractIconEx(path, 0)
        handles = list(small) + list(large)
        if not handles:
            return None
        # 优先用小图标：任务栏/标题栏用的那套在 16px 下更清晰
        preferred = (small or large)[0]
        return extract_icon_handle(preferred, size)
    except Exception:  # noqa: BLE001
        return None
    finally:
        for handle in handles:
            try:
                win32gui.DestroyIcon(handle)
            except Exception:  # noqa: BLE001
                pass


def extract_file_icon(path, size=LARGE_ICON_SIZE, use_attributes=None):
    """取文件的类型图标。

    ``use_attributes`` 为 None 时自动判断：路径存在就用真实图标，
    不存在则带 ``SHGFI_USEFILEATTRIBUTES`` 按扩展名取通用图标——
    历史记录里的文件经常早被删了，那时候仍然应该显示出"这是个 PDF"。
    """
    if _SHELL32 is None or not path:
        return None
    exists = os.path.exists(path)
    if use_attributes is None:
        use_attributes = not exists

    flags = SHGFI_ICON | SHGFI_USEFILEATTRIBUTES if use_attributes else SHGFI_ICON
    flags |= SHGFI_SMALLICON if size <= SMALL_ICON_SIZE else SHGFI_LARGEICON

    info = SHFILEINFOW()
    try:
        _SHELL32.SHGetFileInfoW(
            str(path), FILE_ATTRIBUTE_NORMAL, ctypes.byref(info),
            ctypes.sizeof(info), flags,
        )
    except Exception:  # noqa: BLE001
        return None
    if not info.hIcon:
        return None
    try:
        return extract_icon_handle(info.hIcon, size)
    except Exception:  # noqa: BLE001
        return None
    finally:
        try:
            ctypes.WinDLL("user32", use_last_error=True).DestroyIcon(info.hIcon)
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------------------
# 对外的缓存接口
# ---------------------------------------------------------------------------
def icon_base64(path, size=LARGE_ICON_SIZE, kind="auto"):
    """返回 base64 PNG，取不到返回 None（调用方应当退回文字/类型图标）。

    ``kind``：``exe`` 强制按可执行文件取；``file`` 按文件类型取；
    ``auto`` 由扩展名判断。
    """
    key = (normalize_key(path), size, kind)
    with _lock:
        if key in _icon_cache:
            return _icon_cache[key]

    image = None
    if not path:
        image = None
    elif kind == "exe" or (kind == "auto" and str(path).lower().endswith(".exe")):
        image = extract_executable_icon(path, size)
        if image is None:
            image = extract_file_icon(path, size)
    else:
        image = extract_file_icon(path, size)

    data = None
    if image is not None:
        try:
            buffer = io.BytesIO()
            image.save(buffer, "PNG")
            data = base64.b64encode(buffer.getvalue()).decode("ascii")
        except Exception:  # noqa: BLE001
            data = None

    with _lock:
        _icon_cache[key] = data
    return data


def source_app_icon_base64(process_path, size=SMALL_ICON_SIZE):
    """来源应用图标（进程 exe 路径）。"""
    return icon_base64(process_path, size=size, kind="exe")


def file_type_icon_base64(path, size=SMALL_ICON_SIZE):
    """文件类型图标（按扩展名，文件不存在也能取到）。"""
    return icon_base64(path, size=size, kind="file")
