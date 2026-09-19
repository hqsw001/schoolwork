# -*- coding: utf-8 -*-
"""图片链路保真测试：复制进来的图片，入库后必须逐像素一致

背景（为什么单独有这个套件）
------------------------------------------------------------
`manual_clipboard_check.py` 第 4 节早就测过"图片能不能入库"，但它用的是
`Image.new("RGB", (60, 40), (30, 120, 200))` —— **纯色图**。纯色图哪怕
解码整体错位几十个字节，像素仍然一模一样，所以它一直是绿的。

真正的缺陷是 `dib_to_image` 里的 bfOffBits 写死成 14（漏算了位图头长度），
Pillow 会从位图头中间开始读像素。用 1666×1080 的用例图实测：CF_DIB 有
67% 的像素被改坏，CF_DIBV5 有 47%。所以本套件一律用**高频细节图**
（逐像素变化的图案 + 奇数宽度以覆盖行填充）来测。

⚠ 注意：会真实占用系统剪贴板。运行前请勿复制重要内容。

用法：
    python -m tests.image_fidelity_check
"""

import io
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import win32clipboard  # noqa: E402
from PIL import Image, ImageChops  # noqa: E402

from app.app.kernel import Kernel  # noqa: E402
from app.constants import ERR_INVALID_ARG, ERR_NOT_FOUND  # noqa: E402
from app.infrastructure.clipboard import clipboard_io  # noqa: E402
from app.infrastructure.clipboard import image_utils  # noqa: E402

PASSED = []
FAILED = []


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def same_pixels(a, b):
    """逐像素比对；getbbox() 返回 None 表示两图完全一致。"""
    if a is None or b is None or a.size != b.size:
        return False
    return ImageChops.difference(a.convert("RGB"), b.convert("RGB")).getbbox() is None


def diff_ratio(a, b):
    """返回 (不同像素数, 总像素数)，用于打印诊断信息。"""
    if a.size != b.size:
        return (a.size[0] * a.size[1], a.size[0] * a.size[1])
    grey = ImageChops.difference(a.convert("RGB"), b.convert("RGB")).convert("L")
    hist = grey.histogram()
    return (sum(hist[1:]), a.size[0] * a.size[1])


def pattern_image():
    """高频细节图案：宽度取奇数，顺带覆盖 BMP 行 4 字节填充。"""
    width, height = 97, 61
    image = Image.new("RGB", (width, height))
    pixels = image.load()
    for y in range(height):
        for x in range(width):
            pixels[x, y] = ((x * 7) % 256, (y * 11) % 256, ((x + y) * 13) % 256)
    return image


def png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def pump(kernel, seconds):
    deadline = time.time() + seconds
    while time.time() < deadline:
        kernel.handle_listener_events()
        kernel.poll_events()
        time.sleep(0.05)


def wait_for_capture(kernel, seconds):
    """等待采集工作线程把内容写进数据库。"""
    deadline = time.time() + seconds
    captured_before = kernel._stats["captured"]
    while time.time() < deadline:
        kernel.handle_listener_events()
        kernel.poll_events()
        time.sleep(0.08)
        if kernel._stats["captured"] > captured_before:
            time.sleep(0.5)
            kernel.handle_listener_events()
            kernel.poll_events()
            return True
    return False


def main():
    source = pattern_image()
    print("测试图：%dx%d 高频细节图案（含奇数宽度，覆盖 BMP 行填充）\n"
          % source.size)

    # ==================================================================
    print("=== 1) 单元：24 位 DIB 解码必须逐像素一致 ===")
    dib = image_utils.image_to_dib_bytes(source)
    decoded = image_utils.dib_to_image(dib, is_v5=False)
    check(decoded.size == source.size,
          "解码尺寸一致：%s" % (decoded.size,))
    check(same_pixels(source, decoded),
          "24 位 CF_DIB 解码无损（bfOffBits 必须等于 14 + 头长度）")
    if not same_pixels(source, decoded):
        bad, total = diff_ratio(source, decoded)
        print("       诊断：%d/%d 像素不同" % (bad, total))

    # ==================================================================
    print("\n=== 2) 单元：CF_DIBV5 解码必须逐像素一致 ===")
    # 把 24 位 DIB 的手工升级成 BITMAPV5HEADER（124 字节），模拟系统合成出来的 V5
    v5 = bytearray()
    v5 += (124).to_bytes(4, "little")          # bV5Size
    v5 += dib[4:52]                            # 尺寸/位深/压缩/掩码等原样保留
    v5 += (0).to_bytes(4, "little")            # bV5AlphaMask
    v5 += (0x73524742).to_bytes(4, "little")   # bV5CSType = LCS_sRGB
    v5 += bytes(36)                            # bV5Endpoints
    v5 += bytes(12)                            # gamma
    v5 += bytes(16)                            # intent / profile / reserved
    v5 += dib[40:]                             # 调色板 + 像素数据
    decoded_v5 = image_utils.dib_to_image(bytes(v5), is_v5=True)
    check(decoded_v5.size == source.size, "V5 解码尺寸一致：%s" % (decoded_v5.size,))
    check(same_pixels(source, decoded_v5),
          "CF_DIBV5 解码无损（不能手工删 bV5AlphaMask 那 4 个字节）")
    if not same_pixels(source, decoded_v5):
        bad, total = diff_ratio(source, decoded_v5)
        print("       诊断：%d/%d 像素不同" % (bad, total))

    # ==================================================================
    print("\n=== 3) 单元：8 位调色板 DIB 解码必须逐像素一致 ===")
    palette_image = source.convert("P", palette=Image.ADAPTIVE, colors=256)
    buffer = io.BytesIO()
    palette_image.save(buffer, "BMP")
    palette_dib = buffer.getvalue()[14:]
    bit_count = int.from_bytes(palette_dib[14:16], "little")
    if bit_count == 8:
        decoded_p = image_utils.dib_to_image(palette_dib, is_v5=False)
        check(same_pixels(palette_image.convert("RGB"), decoded_p),
              "8 位调色板 DIB 解码无损（bfOffBits 要把 1024 字节调色板算进去）")
        if not same_pixels(palette_image.convert("RGB"), decoded_p):
            bad, total = diff_ratio(palette_image.convert("RGB"), decoded_p)
            print("       诊断：%d/%d 像素不同" % (bad, total))
    else:
        check(False, "构造 8 位调色板 DIB 失败（位深=%s）" % bit_count)

    # ==================================================================
    print("\n=== 4) 端到端：真实剪贴板 → 采集 → 附件逐像素一致 ===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_fidelity_")
    print("       数据目录：%s" % tmp)
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=True)
    time.sleep(0.8)
    pump(kernel, 0.5)

    # 先把剪贴板弄成别的内容，确保后面的写入真的触发一次变化
    clipboard_io.write_text("图片保真测试占位-%d" % int(time.time()))
    wait_for_capture(kernel, 5.0)

    before = kernel.repo.count()
    clipboard_io.write_image(png_bytes(source))
    captured = wait_for_capture(kernel, 6.0)
    check(captured, "写入剪贴板触发了采集")

    rows = kernel.repo.get_history(limit=5)
    image_row = None
    for row in rows:
        if row.content_type == "image":
            image_row = row
            break
    check(image_row is not None, "图片被识别为 image 类型并入库")
    check(kernel.repo.count() == before + 1,
          "新增一条记录（%d -> %d）" % (before, kernel.repo.count()))

    if image_row is not None:
        check(os.path.isfile(image_row.content),
              "图片已外置为附件：%s" % os.path.basename(image_row.content))
        stored = Image.open(image_row.content)
        check(stored.size == source.size,
              "附件尺寸与源图一致：%s" % (stored.size,))
        check(same_pixels(source, stored),
              ">>> 入库的附件与复制进来的图逐像素一致 <<<")
        if not same_pixels(source, stored):
            bad, total = diff_ratio(source, stored)
            print("       诊断：%d/%d 像素不同（%.2f%%）"
                  % (bad, total, 100.0 * bad / total))

    # ==================================================================
    print("\n=== 5) copy_entry_as_file：把图片变成可粘贴的文件 ===")
    if image_row is not None:
        result = kernel.copy_entry_as_file(image_row.id)
        check(result["ok"], "copy_entry_as_file 执行成功")
        paths = result["data"]["paths"] if result["ok"] else []
        check(paths == [image_row.content], "返回的路径就是附件路径")

        win32clipboard.OpenClipboard()
        try:
            formats = []
            fmt = 0
            while True:
                fmt = win32clipboard.EnumClipboardFormats(fmt)
                if not fmt:
                    break
                formats.append(fmt)
            dropped = None
            if win32clipboard.IsClipboardFormatAvailable(win32clipboard.CF_HDROP):
                dropped = win32clipboard.GetClipboardData(win32clipboard.CF_HDROP)
        finally:
            win32clipboard.CloseClipboard()
        check(win32clipboard.CF_HDROP == 15, "CF_HDROP 常量正确")
        check(15 in formats,
              "剪贴板上是文件列表 CF_HDROP（资源管理器只认这个，格式=%s）" % formats)
        check(dropped and os.path.abspath(dropped[0]) == os.path.abspath(image_row.content),
              "CF_HDROP 里的路径与附件一致：%s" % (dropped,))

    # ==================================================================
    print("\n=== 6) copy_entry_as_file 的回声抑制 ===")
    if image_row is not None:
        count_before = kernel.repo.count()
        refreshed = kernel.repo.find_by_id(image_row.id)
        uses_before = refreshed.use_count
        # 第 5 节写的 CF_HDROP 会被自己采集一遍，等监听线程处理完
        captured_before = kernel._stats["captured"]
        time.sleep(1.0)
        pump(kernel, 3.0)
        check(kernel._stats["captured"] > captured_before,
              "CF_HDROP 确实被监听器采集了一次（否则本节测试没有意义）")
        check(kernel.repo.count() == count_before,
              "「复制为文件」没有新增记录（%d -> %d）"
              % (count_before, kernel.repo.count()))
        refreshed = kernel.repo.find_by_id(image_row.id)
        check(refreshed is not None and refreshed.use_count == uses_before,
              "也没有被当成一次「重复合并」刷高使用次数（%d -> %s）"
              % (uses_before, refreshed.use_count if refreshed else "记录消失"))

    # ==================================================================
    print("\n=== 7) 非图片记录不支持「复制为文件」 ===")
    text_row = None
    for row in kernel.repo.get_history(limit=20):
        if row.content_type == "text":
            text_row = row
            break
    if text_row is None:
        check(False, "没有可用于本节的文本记录")
    else:
        result = kernel.copy_entry_as_file(text_row.id)
        check(not result["ok"], "文本记录被拒绝")
        check(result["error"]["code"] == ERR_INVALID_ARG,
              "返回参数非法 %d（得到 %s）" % (ERR_INVALID_ARG, result["error"]["code"]))
        missing = kernel.copy_entry_as_file(999999)
        check(not missing["ok"] and missing["error"]["code"] == ERR_NOT_FOUND,
              "不存在的记录返回资源不存在 %d（得到 %s）"
              % (ERR_NOT_FOUND, missing["error"]["code"]))

    # ==================================================================
    print("\n=== 8) 清理 ===")
    kernel.shutdown()
    time.sleep(0.5)
    shutil.rmtree(tmp, ignore_errors=True)
    print("       内核已关闭，临时数据目录已删除")

    print("\n" + "=" * 72)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("\n失败清单：")
        for item in FAILED:
            print("  - %s" % item)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
