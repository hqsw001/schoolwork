# -*- coding: utf-8 -*-
"""顺序粘贴回归（差距清单批次 6）

对应 TieZ 的 ``services/paste_queue.rs`` 与 README 里的
"为高频办公场景设计的顺序拷贝/顺序粘贴工作流程"。

用法：从几个地方依次复制一堆片段，然后到目标文档里**按一次键粘一条**。

本套件盯住四个容易写错的地方：

1. **合并的记录不该重复进队** —— 连续复制两次同样的内容只会合并成一条，
   队列里塞两条一样的会让用户粘出重复内容。
2. **失效项要自动跳过** —— 队列建好之后用户可能把某条删了，
   这时该跳下一条，而不是报错打断连续粘贴。
3. **快捷键只在模式开启时注册** —— Alt+V 是很常用的组合，
   没开顺序粘贴还占着它会平白影响别的软件。
4. **关闭模式要清空队列** —— 留着上次没粘完的队列会让用户莫名其妙。

⚠ 会真实注册全局快捷键。用法：
    python -m tests.sequential_paste_check
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app.kernel import Kernel  # noqa: E402
from app.constants import ERR_INVALID_ARG, ERR_NOT_FOUND  # noqa: E402
from app.infrastructure.clipboard import listener as listener_mod  # noqa: E402
from app.infrastructure.clipboard import paste as paste_mod  # noqa: E402
from app.services.paste_queue import PasteQueue  # noqa: E402

PASSED = []
FAILED = []


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def main():
    # ==================================================================
    print("=== 1) 队列服务本身（纯逻辑，无副作用）===")
    queue = PasteQueue()
    check(queue.pop() is None, "空队列 pop 返回 None")
    check(queue.size() == 0 and len(queue) == 0, "空队列长度为 0")

    queue.set_items([3, 1, 2])
    check(queue.items() == [3, 1, 2], "set_items 保持给定顺序（FIFO）")
    check(queue.peek() == 3, "peek 看队首但不弹出")
    check(queue.items() == [3, 1, 2], "peek 之后内容没变")
    check(queue.pop() == 3, "pop 弹出队首")
    check(queue.items() == [1, 2], "弹出后队首后移")

    queue.append(9)
    check(queue.items() == [1, 2, 9], "append 追加到队尾")
    check(queue.remove(1) is True and queue.items() == [2, 9], "remove 删掉指定项")
    check(queue.remove(12345) is False, "删不存在的项返回 False")
    queue.clear()
    check(queue.size() == 0, "clear 清空")

    dirty = PasteQueue()
    dirty.set_items(["bad", 5, None, 7.9])
    check(dirty.items() == [5, 7], "脏数据被过滤，只留能转成整数的")
    check(dirty.append("x") is False, "append 非法值返回 False 而不是抛异常")

    # ==================================================================
    print("\n=== 2) 顺序模式：采集自动进队 ===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_seq_")
    print("       数据目录：%s" % tmp)
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=True)
    time.sleep(0.8)
    kernel.handle_listener_events()
    kernel.poll_events()

    try:
        before = kernel.listener.diagnostics().get("registeredHotkeys") or {}
        check(listener_mod.ACTION_SEQUENTIAL not in before,
              "**默认不注册顺序粘贴快捷键**（没开模式就别占着 Alt+V）")
        check(kernel.get_paste_queue()["data"]["size"] == 0, "起始队列是空的")

        result = kernel.set_sequential_mode(True)
        check(result["ok"] and result["data"]["sequentialMode"] is True,
              "开启顺序粘贴模式")
        time.sleep(0.5)
        kernel.handle_listener_events()
        kernel.poll_events()
        after = kernel.listener.diagnostics().get("registeredHotkeys") or {}
        check(listener_mod.ACTION_SEQUENTIAL in after,
              "开启后顺序粘贴快捷键已注册：%s" % after.get(
                  listener_mod.ACTION_SEQUENTIAL))

        for text in ("第一条内容", "第二条内容", "第三条内容"):
            kernel.simulate_capture(text)
        payload = kernel.get_paste_queue()["data"]
        check(payload["size"] == 3, "三次复制进了三条（实际 %d）" % payload["size"])
        check([item["preview"] for item in payload["items"]]
              == ["第一条内容", "第二条内容", "第三条内容"],
              "队列按复制顺序排列（FIFO）")
        check(all(not item["missing"] for item in payload["items"]),
              "三条都能解析到记录")
        check(payload["sequentialMode"] is True, "返回值里带上模式状态")

        # ==============================================================
        print("\n=== 3) 合并的记录不重复进队 ===")
        kernel.simulate_capture("第一条内容")      # 与队首内容相同 → 合并
        check(kernel.get_paste_queue()["data"]["size"] == 3,
              "重复内容只合并、不重复进队（仍是 3 条）")

        # ==============================================================
        print("\n=== 4) 逐条粘出 ===")
        first = kernel.paste_next_step(simulate=False)
        check(first["ok"], "第一次弹出成功")
        check(first["data"]["remaining"] == 2,
              "剩余 %d 条" % first["data"]["remaining"])
        check(first["data"]["skipped"] == 0, "没有跳过任何项")
        popped_ids = [first["data"]["id"]]

        second = kernel.paste_next_step(simulate=False)
        popped_ids.append(second["data"]["id"])
        check(popped_ids[0] != popped_ids[1], "第二次弹出的是另一条")
        check(len(set(popped_ids)) == 2, "不会重复弹出同一条")

        third = kernel.paste_next_step(simulate=False)
        check(third["data"]["remaining"] == 0, "弹完后队列为空")

        empty = kernel.paste_next_step(simulate=False)
        check(not empty["ok"] and empty["error"]["code"] == ERR_NOT_FOUND,
              "空队列弹出返回 %s 而不是抛异常" % ERR_NOT_FOUND)

        guarded = kernel._paste_next_step_safe()
        check(guarded is None, "快捷键入口在队列为空时静默返回（不抛）")

        # ==============================================================
        print("\n=== 5) 失效项自动跳过 ===")
        for text in ("A 片段", "B 片段", "C 片段"):
            kernel.simulate_capture(text)
        items = kernel.get_paste_queue()["data"]["items"]
        check(len(items) == 3, "重新攒了 3 条")
        victim = items[1]["id"]
        kernel.delete_entry(victim)
        still = kernel.get_paste_queue()["data"]["items"]
        check(any(item["missing"] for item in still),
              "被删掉的那条在队列里标记为 missing")

        kernel.paste_next_step(simulate=False)          # 弹出第一条
        skipped = kernel.paste_next_step(simulate=False)
        check(skipped["ok"] and skipped["data"]["skipped"] == 1,
              "**失效的队首被自动跳过**（skipped=%s）" % skipped["data"]["skipped"])
        check(skipped["data"]["remaining"] == 0, "跳过后队列清空")

        # ==============================================================
        print("\n=== 6) 手动管理队列 ===")
        kernel.clear_paste_queue()
        kernel.simulate_capture("X 内容")
        kernel.simulate_capture("Y 内容")
        ids = [item["id"] for item in kernel.get_paste_queue()["data"]["items"]]
        reversed_result = kernel.set_paste_queue(list(reversed(ids)))
        check(reversed_result["data"]["items"] == list(reversed(ids)),
              "set_paste_queue 按给定顺序重排")

        added = kernel.add_to_paste_queue(ids[0])
        check(added["data"]["size"] == 3, "add_to_paste_queue 追加一条")
        check(kernel.add_to_paste_queue(999999)["ok"] is False,
              "加入不存在的记录被拒绝")
        check(kernel.clear_paste_queue()["data"]["size"] == 0, "清空队列")

        # ==============================================================
        print("\n=== 7) 关闭模式：清队列 + 还回快捷键 ===")
        kernel.simulate_capture("关闭前的残留")
        check(kernel.get_paste_queue()["data"]["size"] == 1, "关闭前队列里有 1 条")
        off = kernel.set_sequential_mode(False)
        check(off["data"]["queueSize"] == 0,
              "**关闭模式会清空队列**（残留会让用户莫名其妙）")
        time.sleep(0.5)
        kernel.handle_listener_events()
        kernel.poll_events()
        final = kernel.listener.diagnostics().get("registeredHotkeys") or {}
        check(listener_mod.ACTION_SEQUENTIAL not in final,
              "关闭后顺序粘贴快捷键已注销：%s" % sorted(final))
        check(listener_mod.ACTION_MAIN_PANEL in final,
              "主面板快捷键不受影响，仍然注册着")

        # ==============================================================
        print("\n=== 8) 顺序粘贴快捷键可改，且校验格式 ===")
        kernel.set_sequential_mode(True)
        changed = kernel.set_hotkey(listener_mod.ACTION_SEQUENTIAL, "Ctrl+Alt+9")
        check(changed["ok"] and changed["data"]["accelerator"] == "Ctrl+Alt+9",
              "顺序粘贴快捷键改成了 %s" % changed["data"]["accelerator"])
        bad = kernel.set_hotkey(listener_mod.ACTION_SEQUENTIAL, "这不是快捷键")
        check(not bad["ok"] and bad["error"]["code"] == ERR_INVALID_ARG,
              "非法格式被拒绝（%s）" % ERR_INVALID_ARG)
        unknown = kernel.set_hotkey("不存在的动作", "Ctrl+Alt+8")
        check(not unknown["ok"] and unknown["error"]["code"] == ERR_INVALID_ARG,
              "未知动作被拒绝")
        kernel.set_sequential_mode(False)

        # ==============================================================
        print("\n=== 9) Alt 辅助函数（按住 Alt 连按 V 的前提）===")
        check(isinstance(paste_mod.alt_is_down(), bool),
              "alt_is_down 返回布尔值")
        check(paste_mod.restore_alt(False) is False,
              "粘贴前 Alt 没按住就不补按")
        check(hasattr(paste_mod, "VK_ALT") and paste_mod.VK_ALT == 0x12,
              "VK_ALT 是 Alt 的虚拟键码 0x12")

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
