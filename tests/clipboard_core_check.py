# -*- coding: utf-8 -*-
"""剪贴板核心补强回归（差距清单批次 4）

覆盖这一批新加的五件事，外加一个顺带修掉的老毛病：

1. ``add_manual_item`` —— 手动新增记录，走的是**同一条管线**
2. ``set_pinned_order`` / ``move_pinned`` —— 置顶区重排
3. ``paste_transient`` —— 临时粘贴后把剪贴板还原回去
4. ``paste_latest_rich`` —— 粘贴最新富文本
5. ``test_hotkey_available`` —— 快捷键占用探测
6. **``@guard`` 不再套两层信封** —— 之前 ``copy_entry`` 转发到
   ``paste_entry`` 时，调用方拿到的是
   ``{"ok":true,"data":{"ok":true,"data":{...}}}``，得剥两层才能用。
   这是本轮顺手修掉的既有缺陷，本套件守住它。

⚠ 会短暂占用系统剪贴板。用法：
    python -m tests.clipboard_core_check
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app.kernel import Kernel, MANUAL_SOURCE_NAME  # noqa: E402
from app.constants import ERR_INVALID_ARG, ERR_NOT_FOUND  # noqa: E402
from app.infrastructure.clipboard import clipboard_io  # noqa: E402
from app.infrastructure.clipboard import paste as paste_mod  # noqa: E402

PASSED = []
FAILED = []
ENVELOPE_KEYS = {"ok", "data", "error", "timestamp"}


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def main():
    tmp = tempfile.mkdtemp(prefix="clipboardpro_core_")
    print("数据目录：%s\n" % tmp)
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=True)
    time.sleep(0.8)
    kernel.handle_listener_events()
    kernel.poll_events()

    try:
        # ==============================================================
        print("=== 1) 手动新增记录 ===")
        result = kernel.add_manual_item("手动加的一条记录")
        check(result["ok"], "add_manual_item 成功")
        data = result["data"]
        check(data["outcome"] == "inserted", "走的是正常插入路径（%s）" % data["outcome"])
        check(data["entry"]["sourceApp"] == MANUAL_SOURCE_NAME,
              "来源标记为「%s」" % MANUAL_SOURCE_NAME)
        check(data["entry"]["contentType"] == "text",
              "类型判定由管线决定：%s" % data["entry"]["contentType"])

        # 类型判定必须与正常采集一致：URL 就该被判成 url
        url_result = kernel.add_manual_item("https://example.com/page")
        check(url_result["ok"] and url_result["data"]["entry"]["contentType"] == "url",
              "手动加的网址也被判成 url（与采集路径一致）")

        tagged = kernel.add_manual_item("带标签的手动记录", tags=["重要", "待用"])
        check(tagged["ok"] and tagged["data"]["tags"] == ["重要", "待用"],
              "标签打上了：%s" % (tagged["data"]["tags"] if tagged["ok"] else "无"))
        row = kernel.repo.find_by_id(tagged["data"]["id"])
        check(row is not None and "重要" in row.tags, "标签落库了")

        blank = kernel.add_manual_item("   \n  ")
        check(not blank["ok"] and blank["error"]["code"] == ERR_INVALID_ARG,
              "全空白内容被拒绝（%s）" % ERR_INVALID_ARG)

        # 再加一条**不置顶**的记录：否则下面"非置顶项不能排序"那条断言
        # 会因为挑不出非置顶项而静默跳过，变成永远通过的假绿灯。
        spare = kernel.add_manual_item("这条不置顶，专门用来验证非置顶项不能排序")
        check(spare["ok"], "准备了一条非置顶记录")

        # ==============================================================
        print("\n=== 2) @guard 不再套两层信封（老缺陷回归）===")
        history = kernel.get_history(limit=5)
        check(set(history.keys()) == ENVELOPE_KEYS,
              "get_history 恰好返回 4 个信封键")
        entry_id = history["data"]["items"][0]["id"]
        copied = kernel.copy_entry(entry_id)
        check(set(copied.keys()) == ENVELOPE_KEYS, "copy_entry 也是 4 个键")
        check(isinstance(copied["data"], dict) and "ok" not in copied["data"],
              "copy_entry 的 data 是真的业务数据，不是又一层信封")
        check("pasted" in copied["data"],
              "data 里有业务字段：%s" % sorted(copied["data"].keys()))

        # ==============================================================
        print("\n=== 3) 置顶排序 ===")
        ids = [item["id"] for item in kernel.get_history(limit=5)["data"]["items"]]
        for entry_id in ids[:3]:
            kernel.toggle_pin(entry_id, True)
        pinned = [item["id"] for item in
                  kernel.get_history(limit=50, pinned_only=True)["data"]["items"]]
        check(len(pinned) == 3, "置顶了 3 条：%s" % pinned)

        moved = kernel.move_pinned(pinned[0], +1)
        check(moved["ok"] and moved["data"]["moved"] is True, "下移成功")
        after = [item["id"] for item in
                 kernel.get_history(limit=50, pinned_only=True)["data"]["items"]]
        check(after[0] == pinned[1] and after[1] == pinned[0],
              "顺序真的换了：%s -> %s" % (pinned, after))

        edge = kernel.move_pinned(after[-1], +1)
        check(edge["ok"] and edge["data"]["moved"] is False,
              "已经在末尾再下移，返回 moved=False 而不是报错")

        # 显式排序：给定顺序即显示顺序
        explicit = list(reversed(after))
        ordered = kernel.set_pinned_order(explicit)
        check(ordered["ok"], "set_pinned_order 成功")
        final = [item["id"] for item in
                 kernel.get_history(limit=50, pinned_only=True)["data"]["items"]]
        check(final == explicit, "显式排序生效：%s" % final)

        not_pinned = [item["id"] for item in
                      kernel.get_history(limit=50)["data"]["items"]
                      if item["id"] not in explicit]
        check(bool(not_pinned), "确实存在非置顶记录可用于本节断言（%s）" % not_pinned)
        if not_pinned:
            bad = kernel.move_pinned(not_pinned[0], -1)
            check(not bad["ok"] and bad["error"]["code"] == ERR_NOT_FOUND,
                  "非置顶项不能排序（%s）" % ERR_NOT_FOUND)
        empty = kernel.set_pinned_order([])
        check(not empty["ok"] and empty["error"]["code"] == ERR_INVALID_ARG,
              "空 id 列表被拒绝")

        # ==============================================================
        print("\n=== 4) 剪贴板快照 / 还原 ===")
        clipboard_io.write_text("还原前的原始内容")
        snapshot = clipboard_io.snapshot_clipboard()
        check(getattr(snapshot, "kind", None) == "text", "快照是文本")
        clipboard_io.write_text("临时粘贴的内容")
        check(clipboard_io.read_clipboard_data().text == "临时粘贴的内容",
              "临时内容已写入")
        clipboard_io.restore_clipboard(snapshot)
        check(clipboard_io.read_clipboard_data().text == "还原前的原始内容",
              "剪贴板被还原回原始内容")

        # 读不懂的格式：不还原也不清空（宁可不动，也不要误删用户内容）
        real_read = clipboard_io.read_clipboard_data
        real_enum = clipboard_io.enum_formats
        try:
            clipboard_io.write_text("不该被清掉的内容")
            clipboard_io.read_clipboard_data = lambda options=None: None
            clipboard_io.enum_formats = lambda: [49999]
            sentinel = clipboard_io.snapshot_clipboard()
            check(sentinel is clipboard_io.SNAPSHOT_UNSUPPORTED,
                  "读不懂的剪贴板返回 SNAPSHOT_UNSUPPORTED 哨兵")
            check(clipboard_io.restore_clipboard(sentinel) is False,
                  "哨兵还原时不做任何事")
        finally:
            clipboard_io.read_clipboard_data = real_read
            clipboard_io.enum_formats = real_enum
        check(clipboard_io.read_clipboard_data().text == "不该被清掉的内容",
              "读不懂的情况下原内容完好无损")

        # ==============================================================
        print("\n=== 5) 临时粘贴会还原剪贴板 ===")
        # 替换掉真实的模拟按键，免得往当前前台窗口里打字
        sent = []
        real_paste = paste_mod.simulate_paste
        try:
            paste_mod.simulate_paste = (
                lambda target, rich=False, logger=None: (sent.append(target), True)[1])
            clipboard_io.write_text("用户原来的剪贴板")
            result = kernel.paste_transient("临时要粘的内容", target_hwnd=0)
            check(result["ok"] and result["data"]["pasted"] is True,
                  "临时粘贴执行成功")
            check(len(sent) == 1, "模拟按键发了一次")
            check(clipboard_io.read_clipboard_data().text == "用户原来的剪贴板",
                  "粘完把剪贴板还原成了用户原来的内容")
        finally:
            paste_mod.simulate_paste = real_paste

        # ==============================================================
        print("\n=== 6) 快捷键占用探测 ===")
        free = kernel.test_hotkey_available("Ctrl+Alt+Shift+F12")
        check(free["ok"] and free["data"]["available"] is True,
              "空闲组合报告可用")
        diag = kernel.listener.diagnostics()
        registered = diag.get("registeredHotkeys") or {}
        if registered:
            own_key = list(registered.values())[0]
            own = kernel.test_hotkey_available(own_key)
            check(own["ok"] and own["data"]["available"] is True,
                  "自己已注册的 %s 报告可用（不能误报被占用）" % own_key)
            check(own["data"].get("ownedByUs") is True,
                  "并标记 ownedByUs")
        check(kernel.listener.diagnostics().get("registeredHotkeys"),
              "探测之后原有快捷键仍然注册着（没被挤掉）")

        illegal = kernel.test_hotkey_available("这根本不是快捷键")
        check(not illegal["ok"] and illegal["error"]["code"] == ERR_INVALID_ARG,
              "非法格式被拒绝（%s）" % ERR_INVALID_ARG)
        blank = kernel.test_hotkey_available("")
        check(not blank["ok"], "空快捷键被拒绝")

        # ==============================================================
        print("\n=== 7) 粘贴最新富文本 ===")
        latest = kernel.paste_latest_rich(simulate=False)
        check(latest["ok"], "paste_latest_rich 成功")
        check("id" in latest["data"], "返回了被粘贴的记录 id")
        # 历史里只有普通文本时，应当退回最新一条而不是报"没有富文本"
        check(latest["data"]["id"] > 0, "没有富文本时退回最新一条记录")

    finally:
        kernel.shutdown()

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
