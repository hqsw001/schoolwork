# -*- coding: utf-8 -*-
"""标签管理器回归（差距清单批次 7）

对应 TieZ 的 ``TagManager.tsx`` 与 UC-11（管理标签）。

本套件盯住四个容易写错的地方：

1. **内置标签必须禁止改名与删除。** ``sensitive`` / ``密码`` / ``password``
   关联着"敏感信息保护"：改名之后新采集的敏感内容就不会再被打上标记，
   保护会**静默失效**——这种失效用户根本察觉不到，所以必须挡在入口。
2. **改名遇到重名要按合并处理。** 用户想把"待用"并进"重要"，报错只会让
   他回去一条条手动改。
3. **标签颜色要稳定。** 没设自定义色时按名字哈希出一个颜色，同一个标签
   每次打开必须一样，否则用户会以为自己设置丢了。
4. **改完标签要同步冗余列。** ``clipboard_history.tags`` 是 ``entry_tags``
   的冗余，不同步的话列表里的标签芯片还是旧的。

⚠ 会短暂弹出窗口。用法：
    python -m tests.tag_manager_check
"""

import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tkinter as tk  # noqa: E402

from app.app.kernel import Kernel  # noqa: E402
from app.constants import ERR_INVALID_ARG, ERR_NOT_FOUND, PROTECTED_TAGS  # noqa: E402
from app.main import Application  # noqa: E402
from app.ui.tag_manager import TagManager, tag_color_for  # noqa: E402

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
    print("=== 1) 标签哈希色（纯函数）===")
    first = tag_color_for("重要", None)
    second = tag_color_for("重要", None)
    check(first == second, "同一标签两次取色一致：%s" % first)
    check(tag_color_for("重要", None) != tag_color_for("待用", None),
          "不同标签颜色不同")
    check(tag_color_for("重要", "#ff0000") == "#ff0000",
          "设了自定义色就用自定义色")
    check(first.startswith("#") and len(first) == 7,
          "中文标签也能生成合法的 #rrggbb：%s" % first)
    check(tag_color_for("", None).startswith("#"), "空名字不炸")

    # ==================================================================
    print("\n=== 2) 仓储层：按标签取条目 + 全局重命名 ===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_tag_")
    print("       数据目录：%s" % tmp)
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    try:
        for text in ("标签内容甲", "标签内容乙", "标签内容丙"):
            kernel.simulate_capture(text)
        rows = kernel.repo.get_history(limit=10)
        kernel.add_tag(rows[0].id, "重要")
        kernel.add_tag(rows[1].id, "重要")
        kernel.add_tag(rows[2].id, "待用")
        kernel.repo.set_tag_color("重要", "#ff8800")

        items = kernel.repo.items_by_tag("重要")
        check(len(items) == 2, "items_by_tag 取到 2 条")
        check(all("重要" in entry.tags for entry in items),
              "取回的每条都真的带这个标签")
        check(kernel.repo.items_by_tag("不存在的标签") == [],
              "取不存在的标签返回空列表而不是报错")

        # 重命名成**已存在**的标签 → 合并
        merged = kernel.repo.rename_tag("待用", "重要")
        check(merged["affected"] == 1 and merged["merged"] is True,
              "重命名到已存在的标签按合并处理（affected=%d）" % merged["affected"])
        tags = {t["name"]: t for t in kernel.repo.list_tags()}
        check("待用" not in tags, "旧标签名已消失")
        check(tags["重要"]["count"] == 3, "合并后计数为 3（实际 %d）"
              % tags["重要"]["count"])
        check(tags["重要"]["color"] == "#ff8800", "目标标签原有颜色被保留")

        # 冗余列必须同步
        entry = kernel.repo.find_by_id(rows[2].id)
        check(entry.tags == ["重要"],
              "**冗余的 tags 列同步了**（%s）" % entry.tags)

        # 重命名成全新的名字
        kernel.add_tag(rows[0].id, "临时")
        kernel.repo.set_tag_color("临时", "#123456")
        fresh = kernel.repo.rename_tag("临时", "归档")
        check(fresh["merged"] is False, "重命名到新名字不算合并")
        tags = {t["name"]: t for t in kernel.repo.list_tags()}
        check("归档" in tags and tags["归档"]["color"] == "#123456",
              "新名字继承了原来的颜色")
        check("临时" not in tags, "旧名字被清掉")

        same = kernel.repo.rename_tag("归档", "归档")
        check(same["affected"] == 0, "改成同一个名字是空操作，不报错")

        # ==================================================================
        print("\n=== 3) 内核命令：内置标签保护 ===")
        kernel.repo.add_tag(rows[0].id, "sensitive")
        listed = kernel.list_tags()["data"]["items"]
        by_name = {t["name"]: t for t in listed}
        check(by_name["sensitive"].get("protected") is True,
              "list_tags 给内置标签打上 protected 标记（界面据此禁用按钮）")
        check(by_name["重要"].get("protected") is False, "普通标签不带 protected")
        check(all("protected" in t for t in listed), "每个标签都有 protected 字段")

        for name in PROTECTED_TAGS:
            renamed = kernel.rename_tag(name, "改个名")
            check(not renamed["ok"] and renamed["error"]["code"] == ERR_INVALID_ARG,
                  "内置标签「%s」禁止改名" % name)
            deleted = kernel.delete_tag(name)
            check(not deleted["ok"] and deleted["error"]["code"] == ERR_INVALID_ARG,
                  "内置标签「%s」禁止删除" % name)

        # 大小写不敏感
        upper = kernel.rename_tag("SENSITIVE", "改个名")
        check(not upper["ok"], "内置标签判断大小写不敏感")

        # 普通标签可以正常改名与删除
        ok_rename = kernel.rename_tag("归档", "存档")
        check(ok_rename["ok"] and ok_rename["data"]["tag"] == "存档",
              "普通标签可以改名")
        ok_delete = kernel.delete_tag("存档")
        check(ok_delete["ok"], "普通标签可以删除")

        # ==================================================================
        print("\n=== 4) 内核命令：get_tag_items ===")
        # 期望值从 list_tags 取，不要写死：前面第 2 节做过一次合并，
        # 写死数字会在改动测试顺序后变成假失败。
        expected = next(t["count"] for t in kernel.list_tags()["data"]["items"]
                        if t["name"] == "重要")
        payload = kernel.get_tag_items("重要")["data"]
        check(payload["count"] == expected,
              "get_tag_items 条数与 list_tags 一致（都是 %d）" % expected)
        check(len(payload["items"]) == expected, "items 长度也对得上")
        check(all("contentType" in item for item in payload["items"]),
              "返回的是完整的记录字典")
        missing = kernel.get_tag_items("根本没有这个标签")
        check(missing["ok"] and missing["data"]["count"] == 0,
              "取不存在的标签返回空而不是报错")

    finally:
        kernel.shutdown()

    # ==================================================================
    print("\n=== 5) 界面：窗口、按钮状态、批量操作 ===")
    tmp2 = tempfile.mkdtemp(prefix="clipboardpro_tagui_")
    app = Application(data_dir=tmp2, enable_listener=False, silent_start=True)
    try:
        app.kernel.start(enable_listener=False)
        for text in ("界面标签甲", "界面标签乙"):
            app.kernel.simulate_capture(text)
        rows = app.kernel.repo.get_history(limit=10)
        app.kernel.add_tag(rows[0].id, "重要")
        app.kernel.add_tag(rows[1].id, "重要")
        app.kernel.repo.add_tag(rows[0].id, "sensitive")

        app.panel.show(focus_search=False)
        deadline = time.time() + 0.8
        while time.time() < deadline:
            try:
                app.root.update()
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.02)

        app.panel.open_tag_manager()
        deadline = time.time() + 1.0
        while time.time() < deadline:
            try:
                app.root.update()
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.02)

        windows = [w for w in app.panel.window.winfo_children()
                   if isinstance(w, TagManager)]
        check(len(windows) == 1, "标签管理器窗口打开了")

        app.panel.open_tag_manager()
        deadline = time.time() + 0.6
        while time.time() < deadline:
            try:
                app.root.update()
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.02)
        again = [w for w in app.panel.window.winfo_children()
                 if isinstance(w, TagManager)]
        check(len(again) == 1, "**标签管理器是单例**（点两次只有一个窗口）")

        manager = windows[0]
        check(len(manager._tags) >= 2, "侧栏列出了 %d 个标签" % len(manager._tags))
        check(manager._current_tag is not None, "默认选中了第一个标签：%s"
              % manager._current_tag)

        # 普通标签：按钮可用
        manager._select_tag("重要")
        app.root.update()
        check(manager.rename_btn.cget("state") == "normal", "普通标签可以重命名")
        check(manager.delete_tag_btn.cget("state") == "normal", "普通标签可以删除")
        check(len(manager._items) == 2, "右侧列出 %d 条记录" % len(manager._items))

        # 内置标签：按钮禁用 + 说明原因
        manager._select_tag("sensitive")
        app.root.update()
        check(manager.rename_btn.cget("state") == "disabled",
              "**内置标签的重命名按钮被禁用**")
        check(manager.delete_tag_btn.cget("state") == "disabled",
              "**内置标签的删除按钮被禁用**")
        hint = manager.hint.cget("text")
        check("内置标签" in hint, "提示里说明了原因：%s" % hint[:28])

        # 全选 + 批量复制
        manager._select_tag("重要")
        app.root.update()
        check(len(manager._checked) == 0, "初始没有选中项")
        check(manager.delete_btn.cget("state") == "disabled",
              "没选中时批量删除是灰的")

        manager.select_all_var.set(True)
        manager._toggle_all()
        app.root.update()
        check(len(manager._checked) == len(manager._items),
              "全选选中了全部 %d 条" % len(manager._checked))
        check(manager.delete_btn.cget("state") == "normal",
              "选中后批量删除可用了")

        manager._copy_selected()
        time.sleep(0.2)
        from app.infrastructure.clipboard import clipboard_io
        copied = clipboard_io.read_clipboard_data()
        check(copied is not None and "界面标签" in (copied.text or ""),
              "批量复制把选中内容写进了剪贴板")

        manager.select_all_var.set(False)
        manager._toggle_all()
        app.root.update()
        check(len(manager._checked) == 0, "取消全选清空了选中项")

        # 从标签里移除一条
        before = len(manager._items)
        manager._remove_from_tag(manager._items[0]["id"])
        app.root.update()
        check(len(manager._items) == before - 1,
              "「×」把记录从标签里移除了（%d -> %d）" % (before, len(manager._items)))

    finally:
        try:
            app.kernel.shutdown()
        except Exception:  # noqa: BLE001
            pass
        try:
            app.root.destroy()
        except Exception:  # noqa: BLE001
            pass

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
