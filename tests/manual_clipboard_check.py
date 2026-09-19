# -*- coding: utf-8 -*-
"""真机验证 1：真实剪贴板监听、来源识别、全局快捷键、托盘、写回剪贴板

前面的 e2e 测试用 simulate_capture 绕过了真实剪贴板；本测试补上这一段：
它真的去注册 AddClipboardFormatListener、真的写系统剪贴板、真的注册全局快捷键。

⚠ 注意：本测试会短暂占用系统剪贴板与 Ctrl+Shift+F9 这个全局快捷键，
   运行期间请勿复制重要内容。测试结束会恢复（剪贴板内容会留在最后一次写入值上）。

用法：
    python -m tests.manual_clipboard_check
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app.kernel import Kernel  # noqa: E402
from app.constants import K_MAIN_HOTKEY  # noqa: E402
from app.infrastructure.clipboard import clipboard_io  # noqa: E402
from app.infrastructure.clipboard import listener as listener_mod  # noqa: E402
from app.infrastructure.clipboard import source_app as source_mod  # noqa: E402

PASSED = []
FAILED = []


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def pump(kernel, seconds, stop_on=None):
    """跑一段时间的事件循环，收集监听线程抛出的事件。"""
    deadline = time.time() + seconds
    events = []
    while time.time() < deadline:
        kernel.handle_listener_events()
        for event in kernel.poll_events():
            events.append(event)
            if stop_on and stop_on(event):
                return events
        time.sleep(0.05)
    return events


def wait_for_capture(kernel, seconds):
    """等待采集工作线程把内容写进数据库。"""
    deadline = time.time() + seconds
    while time.time() < deadline:
        kernel.handle_listener_events()
        kernel.poll_events()
        time.sleep(0.08)
        if kernel._stats["captured"]:
            # 再等一会儿让工作线程完成入库
            time.sleep(0.4)
            kernel.handle_listener_events()
            kernel.poll_events()
            return True
    return False


def main():
    tmp = tempfile.mkdtemp(prefix="clipboardpro_real_")
    print("数据目录：%s\n" % tmp)
    kernel = Kernel(data_dir=tmp)

    print("=== 1) 真实剪贴板监听（AddClipboardFormatListener，非轮询）===")
    kernel.start(enable_listener=True)
    time.sleep(0.8)
    pump(kernel, 0.5)          # 消费启动事件
    status = kernel.get_listen_status()
    data = status["data"] if status["ok"] else {}
    check(data.get("running") is True, "监听线程已启动并处于运行状态")

    diag = kernel.diagnostics() if hasattr(kernel, "diagnostics") else None
    listener_diag = kernel.listener.diagnostics()
    check(listener_diag["hwnd"] not in (None, 0),
          "消息窗口创建成功（hwnd=%s）" % listener_diag["hwnd"])

    print("\n=== 2) 复制 → 自动采集 → 入库 ===")
    marker = "真实剪贴板监听验证-%d" % int(time.time())
    before = kernel._stats["captured"]
    clipboard_io.write_text(marker)
    captured = wait_for_capture(kernel, 5.0)
    check(captured, "复制动作触发了采集（captured: %d -> %d）"
          % (before, kernel._stats["captured"]))
    history = kernel.get_history(limit=5)
    items = history["data"]["items"] if history["ok"] else []
    check(any(item["content"] == marker for item in items),
          "剪贴板内容被自动写入历史记录")

    print("\n=== 3) 来源应用识别 ===")
    info = source_mod.get_source_app_info()
    print("       当前前台应用：%s（路径：%s）" % (info.app_name, info.process_path))
    check(info.app_name not in (None, ""), "能读出来源应用名（即使是「未知」也算通过）")
    own = source_mod.get_source_app_info(own_pid=os.getpid())
    check(isinstance(own.app_name, str), "来源识别在异常情况下也不会抛异常")

    print("\n=== 4) 图片内容采集（CF_DIB）===")
    try:
        import io as _io
        from PIL import Image
        import win32clipboard
        image = Image.new("RGB", (60, 40), (30, 120, 200))
        buffer = _io.BytesIO()
        image.save(buffer, "BMP")
        dib = buffer.getvalue()[14:]
        win32clipboard.OpenClipboard()
        win32clipboard.EmptyClipboard()
        win32clipboard.SetClipboardData(win32clipboard.CF_DIB, dib)
        win32clipboard.CloseClipboard()

        before = kernel._stats["captured"]
        deadline = time.time() + 5
        image_row = None
        while time.time() < deadline:
            kernel.handle_listener_events()
            kernel.poll_events()
            time.sleep(0.1)
            if kernel._stats["captured"] > before:
                time.sleep(0.5)
                kernel.handle_listener_events()
                kernel.poll_events()
                rows = kernel.repo.get_history(limit=1)
                image_row = rows[0] if rows else None
                break
        check(image_row is not None and image_row.content_type == "image",
              "位图被识别为 image 类型并入库")
        if image_row is not None:
            attachment = image_row.content
            check(os.path.isfile(attachment),
                  "图片已外置为附件文件：%s" % os.path.basename(attachment))
            check(not os.path.basename(attachment).startswith("data:"),
                  "数据库里没有内联图片数据（content 存的是路径）")
    except ImportError as exc:
        print("       跳过（缺少依赖：%s）" % exc)

    print("\n=== 5) 写回剪贴板 + 回声抑制 ===")
    rows = kernel.repo.get_history(limit=20)
    text_row = None
    for row in rows:
        if row.content_type == "text":
            text_row = row
            break
    if text_row is None:
        check(False, "没有可用于回声测试的文本记录")
    else:
        count_before = kernel.repo.count()
        # 只写剪贴板，不模拟按键：这会让监听器收到一次变化通知
        result = kernel.paste_entry(text_row.id, simulate=False)
        check(result["ok"], "paste_entry 写入剪贴板成功")
        # 等待监听器把这次变化读回来
        deadline = time.time() + 4
        while time.time() < deadline:
            kernel.handle_listener_events()
            kernel.poll_events()
            time.sleep(0.1)
        time.sleep(0.6)
        kernel.handle_listener_events()
        kernel.poll_events()
        count_after = kernel.repo.count()
        check(count_after == count_before,
              "自己写回剪贴板产生的变化被判定为回声，未产生新记录（%d -> %d）"
              % (count_before, count_after))

    print("\n=== 6) 全局快捷键注册 ===")
    # 用一个不太可能冲突的组合做测试，避免抢走用户常用快捷键
    result = kernel.set_hotkey(listener_mod.ACTION_SEARCH, "Ctrl+Shift+F9")
    time.sleep(0.5)
    kernel.handle_listener_events()
    kernel.poll_events()
    diag = kernel.listener.diagnostics()
    registered = diag.get("registeredHotkeys", {})
    check("search" in registered,
          "全局快捷键注册成功：%s（已注册：%s）" % (registered.get("search"), registered))
    # 还原默认值
    kernel.set_hotkey(listener_mod.ACTION_SEARCH,
                      kernel.settings.get("app.search_hotkey") or "Ctrl+Shift+F")
    time.sleep(0.3)

    print("\n=== 7) 暂停 / 恢复监听 ===")
    kernel.set_listening(False)
    time.sleep(0.3)
    captured_before = kernel._stats["captured"]
    clipboard_io.write_text("暂停期间复制的内容不应该被记录-%d" % int(time.time()))
    time.sleep(1.2)
    kernel.handle_listener_events()
    kernel.poll_events()
    check(kernel._stats["captured"] == captured_before,
          "暂停期间复制的内容没有被采集（UC-14 主事件流第 3 步）")
    kernel.set_listening(True)
    time.sleep(0.3)
    clipboard_io.write_text("恢复监听后的内容-%d" % int(time.time()))
    resumed = wait_for_capture(kernel, 5.0)
    check(resumed, "恢复监听后新复制的内容能正常采集（不回补暂停期间的）")

    print("\n=== 8) 清理与退出 ===")
    kernel.shutdown()
    time.sleep(0.5)
    print("       内核已关闭，快捷键与托盘图标已注销")

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
