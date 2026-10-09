# -*- coding: utf-8 -*-
"""系统集成回归：打开内容、文件定位、开机自启、Win+V、平台信息

对应差距清单里的批次 2。这一批全是"碰操作系统"的功能，所以测试策略是：

* **纯逻辑直接测**：打开方式的判定（``plan_open``）、注册表值的增删
  （``merge_disabled_hotkey`` / ``remove_disabled_hotkey``）都不产生副作用。
* **执行路径替换出口再测**：真正会弹窗口的只有 ``shell_execute`` 一个函数，
  测试把它换掉，只断言"打算打开什么"，不会真的弹出资源管理器或浏览器。
* **注册表用一次性值名**：自启测试写的是 ``剪贴板Pro_测试用`` 这个临时值名，
  测完立刻删掉，绝不动用户真实的 ``剪贴板Pro`` 自启项。
* **Win+V 只读不写**：接管 Win+V 会真的把系统 Win+V 关掉，套件里只读状态、
  只测纯函数；真正改注册表留给用户手动点。

用法：
    python -m tests.os_integration_check
"""

import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app.kernel import Kernel  # noqa: E402
from app.constants import ERR_INTERNAL, ERR_NOT_FOUND  # noqa: E402
from app.domain.models import ClipboardData  # noqa: E402
from app.infrastructure import os_integration as oi  # noqa: E402

PASSED = []
FAILED = []

#: 自启测试专用的值名，测完即删（不要用真实值名）
TEST_AUTOSTART_VALUE = "剪贴板Pro_测试用"


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


class FakeShell(object):
    """替换 shell_execute，记录"打算打开什么"。"""

    def __init__(self, result=True):
        self.calls = []
        self.result = result

    def __call__(self, target, params=None, verb=None, logger=None):
        self.calls.append((target, params))
        return self.result


def main():
    # ==================================================================
    print("=== 1) 打开方式判定 plan_open（纯函数）===")
    cases = [
        ("url", "https://example.com/a", "url"),
        ("url", "www.example.com", "url"),
        ("file", r"C:\Windows\notepad.exe", "path"),
        ("file", "C:\\a.txt\nC:\\b.txt", "path"),      # 多路径取第一个
        ("video", r"D:\clip.mp4", "path"),
        ("image", r"D:\pic.png", "path"),               # 图片 content 就是附件路径
        ("rich_text", "<b>hi</b>", "tempfile"),
        ("text", "hello", "tempfile"),
        ("code", "print(1)", "tempfile"),
    ]
    for content_type, content, expected in cases:
        plan = oi.plan_open(content_type, content)
        check(plan["action"] == expected,
              "%-10s -> %-8s（%s）" % (content_type, plan["action"], expected))

    check(oi.plan_open("file", "C:\\a.txt\nC:\\b.txt")["target"] == "C:\\a.txt",
          "多路径只取第一个")
    check(oi.plan_open("rich_text", "x")["suffix"] == ".html",
          "富文本落成 .html")
    check(oi.plan_open("text", "x")["suffix"] == ".txt",
          "纯文本落成 .txt")
    check(oi.plan_open("url", "")["target"] == "",
          "空内容不炸")

    # ==================================================================
    print("\n=== 2) DisabledHotkeys 增删（纯函数，幂等）===")
    check(oi.merge_disabled_hotkey("", "V") == "V", "空值加 V -> V")
    check(oi.merge_disabled_hotkey("X", "V") == "XV", "X 加 V -> XV")
    check(oi.merge_disabled_hotkey("VX", "V") == "VX", "已有 V 不重复添加")
    check(oi.merge_disabled_hotkey("vx", "v") == "vx", "大小写不敏感")
    check(oi.remove_disabled_hotkey("XV", "V") == "X", "XV 去掉 V -> X")
    check(oi.remove_disabled_hotkey("V", "V") == "", "只有 V 时去掉 -> 空")
    check(oi.remove_disabled_hotkey("", "V") == "", "空值去掉 V -> 空")
    check(oi.remove_disabled_hotkey("XYZ", "V") == "XYZ", "不含 V 时原样返回")
    doubled = oi.merge_disabled_hotkey(oi.merge_disabled_hotkey("X", "V"), "V")
    check(doubled == "XV", "连续加两次结果不变（幂等）")

    # ==================================================================
    print("\n=== 3) 平台信息与提权检测 ===")
    info = oi.platform_info()
    check(info.get("isWindows") is True, "识别为 Windows 平台")
    check(int(info.get("build", 0)) > 0,
          "读到系统 build 号：%s" % info.get("release"))
    check(isinstance(info.get("isWindows11"), bool), "Win11 判定是布尔值")
    check(info["isWindows11"] == (int(info["build"]) >= 22000),
          "Win11 判定阈值正确（build >= 22000）")
    check(not (info["isWindows11"] and info["isWindows10"]),
          "不可能同时是 Win10 和 Win11")
    check(isinstance(oi.is_admin(), bool), "提权检测返回布尔值")

    # ==================================================================
    print("\n=== 4) 打开内容（替换 shell_execute，不弹窗口）===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_os_")
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)

    real_shell = oi.shell_execute
    fake = FakeShell()
    oi.shell_execute = fake
    try:
        kernel.simulate_capture("https://github.com/jimuzhe/tiez-clipboard")
        kernel.simulate_capture("一段要被外部编辑器打开的文本")

        sample = os.path.join(tmp, "sample.txt")
        with open(sample, "w", encoding="utf-8") as fh:
            fh.write("hello")
        kernel.capture(ClipboardData("files", files=[sample]))

        rows = {}
        for entry in kernel.repo.get_history(limit=10):
            rows.setdefault(entry.content_type, entry)

        # 网址 → 交给 shell_execute 打开
        fake.calls = []
        result = kernel.open_content(rows["url"].id)
        check(result["ok"] and result["data"]["action"] == "url",
              "网址记录走 url 分支")
        check(fake.calls and fake.calls[0][0].startswith("https://"),
              "把网址交给了 shell_execute：%s" % (fake.calls[0][0] if fake.calls else "无",))

        # 纯文本 → 先落临时文件再打开
        fake.calls = []
        result = kernel.open_content(rows["text"].id)
        check(result["ok"] and result["data"]["action"] == "tempfile",
              "纯文本记录走 tempfile 分支")
        temp_path = result["data"]["tempPath"]
        check(bool(temp_path) and os.path.isfile(temp_path),
              "临时文件真的建出来了：%s" % os.path.basename(temp_path or ""))
        check(fake.calls and fake.calls[0][0] == temp_path,
              "把临时文件交给了 shell_execute")
        if temp_path and os.path.isfile(temp_path):
            with open(temp_path, encoding="utf-8") as fh:
                check(fh.read() == "一段要被外部编辑器打开的文本",
                      "临时文件内容与记录一致")

        # 文件 → 直接用原路径打开
        if "file" in rows:
            fake.calls = []
            result = kernel.open_content(rows["file"].id)
            check(result["ok"] and result["data"]["action"] == "path",
                  "文件记录走 path 分支")
            check(fake.calls and fake.calls[0][0] == sample,
                  "直接打开原文件，不复制到临时目录")

        # 打不开时要报错而不是假装成功
        fake_unavailable = FakeShell(result=False)
        oi.shell_execute = fake_unavailable
        failed = kernel.open_content(rows["url"].id)
        check(not failed["ok"] and failed["error"]["code"] == ERR_INTERNAL,
              "系统拒绝打开时返回明确错误（%s）" % ERR_INTERNAL)
        oi.shell_execute = fake

        missing = kernel.open_content(999999)
        check(not missing["ok"] and missing["error"]["code"] == ERR_NOT_FOUND,
              "不存在的记录返回 %s" % ERR_NOT_FOUND)

        # 打开不应该改变列表顺序（只加使用次数）
        before = [e.id for e in kernel.repo.get_history(limit=10)]
        kernel.open_content(rows["text"].id)
        after = [e.id for e in kernel.repo.get_history(limit=10)]
        check(before == after, "打开内容不会把记录顶到列表最前面")
        refreshed = kernel.repo.find_by_id(rows["text"].id)
        check(refreshed.use_count >= 1, "使用次数有增加（%d）" % refreshed.use_count)

        # ==============================================================
        print("\n=== 5) 打开文件所在位置 ===")
        if "file" in rows:
            fake.calls = []
            located = kernel.open_entry_location(rows["file"].id)
            check(located["ok"], "文件记录可以定位")
            check(fake.calls and fake.calls[0][0] == "explorer.exe"
                  and "/select," in (fake.calls[0][1] or ""),
                  "用 explorer /select 选中该文件")
        bad = kernel.open_entry_location(rows["text"].id)
        check(not bad["ok"] and bad["error"]["code"] == ERR_NOT_FOUND,
              "文本记录没有可定位的文件，返回 %s" % ERR_NOT_FOUND)

        # ==============================================================
        print("\n=== 6) Win+V 状态（只读，不改注册表）===")
        status = kernel.get_win_v_status()
        check(status["ok"], "读取 Win+V 状态成功")
        data = status["data"]
        for field in ("disabledHotkeys", "vDisabled", "historyEnabled",
                      "windowsWinVAvailable"):
            check(field in data, "状态里包含 %s" % field)
        check(isinstance(data["vDisabled"], bool), "vDisabled 是布尔值")

        diagnostics = kernel.get_os_diagnostics()
        check(diagnostics["ok"], "系统集成诊断可用")
        for field in ("isAdmin", "autostart", "dataDir", "platform"):
            check(field in diagnostics["data"], "诊断里包含 %s" % field)

    finally:
        oi.shell_execute = real_shell
        kernel.shutdown()

    # ==================================================================
    print("\n=== 7) 开机自启（真实注册表往返，用一次性值名）===")
    try:
        # 先确保测试值名是干净的
        oi.set_autostart(False, value_name=TEST_AUTOSTART_VALUE)
        check(not oi.is_autostart_enabled(TEST_AUTOSTART_VALUE),
              "起始状态：测试用自启项不存在")

        ok, message = oi.set_autostart(True, value_name=TEST_AUTOSTART_VALUE,
                                       command='"X:\\fake.exe"')
        check(ok, "写入自启项成功：%s" % message)
        check(oi.is_autostart_enabled(TEST_AUTOSTART_VALUE),
              "读回来确实是已启用")

        ok, message = oi.set_autostart(False, value_name=TEST_AUTOSTART_VALUE)
        check(ok, "删除自启项成功：%s" % message)
        check(not oi.is_autostart_enabled(TEST_AUTOSTART_VALUE),
              "删掉之后读回来是未启用")

        # 关掉一个本来就不存在的项也不该报错
        ok, message = oi.set_autostart(False, value_name=TEST_AUTOSTART_VALUE)
        check(ok, "重复关闭不报错：%s" % message)

        command = oi.autostart_command()
        check("python" in command.lower() and command.count('"') >= 4,
              "自启命令行带引号包住解释器与启动器")
    finally:
        oi.set_autostart(False, value_name=TEST_AUTOSTART_VALUE)

    # ==================================================================
    print("\n=== 8) 临时文件清理 ===")
    stale = oi.write_temp_content("要被清掉的内容", ".txt")
    check(os.path.isfile(stale), "临时文件已创建")
    # 把 mtime 拨回两天前，模拟过期
    old = os.path.getmtime(stale) - 2 * 24 * 3600
    os.utime(stale, (old, old))
    removed = oi.cleanup_temp_files(max_age_seconds=24 * 3600)
    check(removed >= 1, "清理掉了 %d 个过期临时文件" % removed)
    check(not os.path.exists(stale), "过期临时文件确实被删掉了")

    fresh = oi.write_temp_content("刚写的，不该被清", ".txt")
    oi.cleanup_temp_files(max_age_seconds=24 * 3600)
    check(os.path.exists(fresh), "刚写的临时文件不会被误删")
    try:
        os.remove(fresh)
    except OSError:
        pass

    print("\n=== 9) 重启与提权重启（只算命令，不真的执行）===")
    executable, params = oi.relaunch_command()
    check(bool(executable) and os.path.isfile(executable),
          "重启用的可执行文件存在：%s" % os.path.basename(executable))
    check("main.py" in params,
          "**源码运行时参数里带上入口脚本**（不带的话重启出来是个空解释器）")
    entry = params.strip('"')
    check(os.path.isfile(entry), "入口脚本真实存在：%s" % entry)

    # 提权重启走 runas 动词；把执行出口换掉，避免真的弹 UAC
    calls = []
    real_execute = oi.shell_execute
    real_admin = oi.is_admin
    oi.shell_execute = lambda t, p=None, verb=None, logger=None: (
        calls.append({"target": t, "params": p, "verb": verb}) or True)
    try:
        check(oi.restart_as_admin() is True, "提权重启返回成功")
        check(calls and calls[0]["verb"] == "runas",
              "**用的是 runas 动词**（这样才会弹 UAC）")
        check(calls[0]["target"] == executable, "目标就是本程序")

        # 注意：「已经是管理员就别再提权」这条**策略在 kernel 命令里**，
        # 不在这里——基础设施层只负责"以管理员身份启动"这一件机械的事。
        # 所以这里不断言它，避免把策略和机制混在一起。
    finally:
        oi.shell_execute = real_execute
        oi.is_admin = real_admin

    # 用户取消 UAC 时必须如实返回 False，不能当成"已提权"
    oi.shell_execute = lambda t, p=None, verb=None, logger=None: False
    try:
        check(oi.restart_as_admin() is False,
              "**用户取消 UAC 时如实返回失败**（返回码 <= 32 是失败码）")
    finally:
        oi.shell_execute = real_execute

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
