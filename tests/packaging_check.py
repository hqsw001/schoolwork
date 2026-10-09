# -*- coding: utf-8 -*-
"""打包回归：exe（frozen）形态下的路径、自启命令与产物结构

**为什么需要这个套件**：打包之后有三处和源码运行不一样，算错了都不会报错，
只会"表现得很怪"，等用户发现时已经过去很久：

1. **数据目录按 ``__file__`` 算** → 写进 ``_internal\\.data``。用户把文件夹
   换个位置、或者用新版整个覆盖旧版，历史记录看起来就"丢了"。
2. **自启命令还拼"解释器 + 启动脚本"** → 注册表 ``Run`` 里那条命令指向一个
   打包后根本不存在的 ``.py``，开机自启**静默失效**（界面上还显示"已开启"）。
3. **随包资源按 exe 所在目录拼** → ``emoji_data.json`` 永远读不到，表情面板
   静默降级成两张兜底分组，功能"看起来有、其实是残的"。

所以这里同时钉住三件事：

    A. 源码形态的行为**一个字都没变**——不能为了打包把原来的路径改坏；
    B. frozen 形态下三种"根"（程序根 / 资源根 / 可执行文件）各自算对；
    C. 打包脚本的参数正确，且（如果已经构建过）真实产物里资源确实在预期位置。

用法：
    python -m tests.packaging_check                 只做静态部分
    python -m tests.packaging_check --exe <exe>     额外做一次真机启动验证
"""

import argparse
import ctypes
import ctypes.wintypes as wt
import importlib
import os
import shutil
import subprocess
import sys
import tempfile
import time

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from app import APP_NAME, VERSION  # noqa: E402
from app.infrastructure import emoji, paths  # noqa: E402
from app.infrastructure import database  # noqa: E402
from app.infrastructure import os_integration as oi  # noqa: E402

PASSED = []
FAILED = []

#: 启动验证的等待上限：打包版冷启动要解包运行时，给足时间但不无限等
LAUNCH_TIMEOUT_SECONDS = 90


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


# ---------------------------------------------------------------------------
# 工具：临时伪造 frozen 环境
# ---------------------------------------------------------------------------
class fake_frozen(object):
    """上下文管理器：临时把进程伪装成"PyInstaller 打包后"的样子。

    退出时**必须**把所有改动还回去（交接说明里明确记着一条教训：
    测试改了全局状态不还回去，后面的用例会在离现场很远的地方莫名其妙失败）。
    """

    def __init__(self, executable, meipass):
        self.executable = executable
        self.meipass = meipass
        self._saved = {}

    def __enter__(self):
        for name, value in (("frozen", True),
                            ("executable", self.executable),
                            ("_MEIPASS", self.meipass)):
            self._saved[name] = (hasattr(sys, name), getattr(sys, name, None))
            setattr(sys, name, value)
        return self

    def __exit__(self, exc_type, exc, tb):
        for name, (existed, value) in self._saved.items():
            if existed:
                setattr(sys, name, value)
            else:
                try:
                    delattr(sys, name)
                except AttributeError:
                    pass
        return False


#: 主面板的窗口标题（app/ui/panel.py 里是 "%s %s" % (APP_NAME, VERSION)）。
#: 必须精确匹配，不能"标题里含剪贴板Pro 就算" —— 记事本里做过粘贴验证后，
#: 会留下一个标题形如 "*剪贴板Pro 记事本验证-1234 - Notepad" 的窗口，
#: 宽松匹配会把它当成"程序已经在运行"，直接跳过整个启动验证。
PANEL_TITLE = "%s %s" % (APP_NAME, VERSION)


def visible_panel_windows():
    """枚举"当前真的显示在屏幕上"的主面板窗口标题。

    刻意要求 ``IsWindowVisible``：面板窗口是 ``Toplevel``，但程序还有一个
    ``withdraw()`` 过的根窗口也叫这个名字。不筛可见性的话，"面板弹出来了"
    这条断言即使面板没弹出来也会通过——等于恒真断言，交接说明里点过名。
    """
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)
    def enum_proc(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length > 0:
            buffer = ctypes.create_unicode_buffer(length + 2)
            user32.GetWindowTextW(hwnd, buffer, length + 1)
            if buffer.value == PANEL_TITLE:
                found.append(buffer.value)
        return True

    user32.EnumWindows(enum_proc, 0)
    return found


# ---------------------------------------------------------------------------
# A. 源码形态：行为必须和打包之前完全一致
# ---------------------------------------------------------------------------
def check_source_mode_is_unchanged():
    print("\n=== A) 源码形态：路径行为没有被打包改动带坏 ===")

    check(paths.is_frozen() is False, "源码运行时 is_frozen() 为 False")

    program_root = paths.program_root()
    check(os.path.abspath(program_root) == os.path.abspath(PROJECT_ROOT),
          "program_root() = 项目根：%s" % program_root)

    check(os.path.abspath(paths.bundle_root()) == os.path.abspath(PROJECT_ROOT),
          "bundle_root() = 项目根（源码形态）")

    check(paths.executable_path().lower().endswith(("python.exe", "pythonw.exe")),
          "可执行文件是 Python 解释器：%s" % os.path.basename(paths.executable_path()))

    # 数据目录：必须还是 <项目根>/.data，不能因为打包改动而搬家
    data_dir = database.default_data_dir()
    check(os.path.abspath(data_dir) == os.path.join(os.path.abspath(PROJECT_ROOT), ".data"),
          "数据目录仍是 <项目根>/.data：%s" % data_dir)

    # 随包资源：源码形态能真读到，且分组表不是兜底表
    check(os.path.isfile(emoji.DATA_FILE),
          "emoji 数据文件在源码形态下存在：%s"
          % os.path.relpath(emoji.DATA_FILE, PROJECT_ROOT))
    groups = emoji.load_groups()
    check(len(groups) > len(emoji.FALLBACK_GROUPS),
          "emoji 读到的是完整分组表（%d 组 > 兜底 %d 组）"
          % (len(groups), len(emoji.FALLBACK_GROUPS)))

    # 自启命令：源码形态仍然是"解释器 + 启动器"，且这个启动器真实存在
    command = oi.autostart_command()
    check(command.count('"') >= 4 and "python" in command.lower(),
          "自启命令仍带引号包住解释器与启动器：%s" % command)
    launcher = command.split('"')[-2]
    check(os.path.isfile(launcher), "自启命令指向的启动器真实存在")

    # 重启命令：源码形态必须带上入口脚本，否则重启出来是个空解释器
    executable, params = oi.relaunch_command()
    check(os.path.isfile(executable) and "main.py" in params,
          "重启命令仍带入口脚本：%s %s" % (os.path.basename(executable), params))
    check(os.path.isfile(params.strip('"')), "重启命令里的入口脚本真实存在")


# ---------------------------------------------------------------------------
# B. frozen 形态：三种"根"各自算对
# ---------------------------------------------------------------------------
def check_frozen_mode():
    print("\n=== B) frozen 形态：程序根 / 资源根 / 可执行文件 ===")

    sandbox = tempfile.mkdtemp(prefix="clipboardpro_frozen_")
    fake_exe_dir = os.path.join(sandbox, "剪贴板Pro 便携版")
    fake_meipass = os.path.join(fake_exe_dir, "_internal")
    os.makedirs(fake_meipass)
    fake_exe = os.path.join(fake_exe_dir, APP_NAME + ".exe")
    with open(fake_exe, "wb") as handle:
        handle.write(b"MZ")          # 内容不重要，只要路径存在
    # 解包目录里放一份"随包资源"，验证资源路径确实是按 _MEIPASS 拼的
    os.makedirs(os.path.join(fake_meipass, "app", "data"))
    with open(os.path.join(fake_meipass, "app", "data", "emoji_data.json"), "w",
              encoding="utf-8") as handle:
        handle.write('{"groups": []}')

    saved_data_file = emoji.DATA_FILE
    try:
        with fake_frozen(fake_exe, fake_meipass):
            check(paths.is_frozen() is True, "is_frozen() 认出打包形态")

            check(os.path.abspath(paths.program_root()) == os.path.abspath(fake_exe_dir),
                  "program_root() = exe 所在目录（而不是 _internal）")

            check(os.path.abspath(paths.bundle_root()) == os.path.abspath(fake_meipass),
                  "bundle_root() = 解包目录 sys._MEIPASS")

            check(os.path.abspath(paths.executable_path()) == os.path.abspath(fake_exe),
                  "可执行文件 = exe 自己")

            # 核心回归：数据目录必须挨着 exe，绝不能落进 _internal
            data_dir = database.default_data_dir()
            check(os.path.abspath(data_dir) == os.path.join(os.path.abspath(fake_exe_dir), ".data"),
                  "**数据目录挨着 exe**：%s" % data_dir)
            check(not os.path.exists(os.path.join(fake_meipass, ".data")),
                  "**没有在 _internal 里建数据目录**（否则换目录就像丢数据）")

            # 自启命令：打包后要指向 exe 自己，不能再拼解释器与 .py 启动器
            autostart = oi.autostart_command()
            check(autostart == '"%s"' % fake_exe,
                  "**自启命令指向 exe 自己**：%s" % autostart)
            check(os.path.isfile(autostart.strip('"')),
                  "自启命令里的目标文件真实存在（自启不会静默失效）")

            # 重启命令：打包后不能带参数（带 main.py 会变成"参数不识别"）
            executable, params = oi.relaunch_command()
            check(executable == fake_exe and params == "",
                  "重启命令是 exe 本身且不带参数：%r %r" % (executable, params))

            # 随包资源：按解包目录拼（真实断言要重载模块，见下）
            expected = os.path.join(fake_meipass, "app", "data", "emoji_data.json")
            check(os.path.abspath(paths.bundle_file("app", "data", "emoji_data.json")) ==
                  os.path.abspath(expected),
                  "bundle_file() 指向解包目录里的随包资源")
            check(os.path.isfile(expected), "（测试夹具）解包目录里确实放了那份资源")

            # emoji 的 DATA_FILE 是模块级常量，必须在 frozen 环境里重新求值一次，
            # 否则测的还是导入时算好的源码路径 —— 那样就成了恒真断言。
            importlib.reload(emoji)
            check(os.path.abspath(emoji.DATA_FILE) == os.path.abspath(expected),
                  "**frozen 下 emoji.DATA_FILE 指向解包目录**（否则只能吃兜底表）")
    finally:
        importlib.reload(emoji)
        shutil.rmtree(sandbox, ignore_errors=True)

    check(emoji.DATA_FILE == saved_data_file,
          "重载之后 emoji.DATA_FILE 已还原成源码路径（全局状态还回去了）")
    check(not hasattr(sys, "frozen"),
          "退出后 sys.frozen 已删除（全局状态还回去了）")
    check(not hasattr(sys, "_MEIPASS"),
          "退出后 sys._MEIPASS 已删除（全局状态还回去了）")


# ---------------------------------------------------------------------------
# C. 打包脚本本身
# ---------------------------------------------------------------------------
def check_build_script():
    print("\n=== C) 打包脚本：参数与资源声明 ===")

    try:
        import build_exe
    except ImportError as exc:
        check(False, "能导入 build_exe.py：%s" % exc)
        return

    args = build_exe.build_arguments()
    check(build_exe.ENTRY_SCRIPT == os.path.join(PROJECT_ROOT, "app", "main.py"),
          "入口脚本是 app/main.py")

    check("--onedir" in args, "用文件夹版（--onedir）")
    check("--windowed" in args, "不带控制台窗口（--windowed）")
    check(PROJECT_ROOT in args, "把项目根加进搜索路径（否则打包时 import app 失败）")

    # 随包资源：必须声明 app/data/emoji_data.json，且落到 app/data
    data_pairs = [args[i + 1] for i, item in enumerate(args) if item == "--add-data"]
    check(any(pair.endswith(os.pathsep + "app/data") and "emoji_data.json" in pair
              for pair in data_pairs),
          "把 emoji_data.json 声明为随包资源，落到 app/data：%s" % data_pairs)

    check(os.path.isfile(build_exe.EMOJI_DATA), "随包资源文件真实存在")

    # 这一条是防一个我自己写出来过的真 bug：--hidden-import / --exclude-module
    # 在 PyInstaller 里是 append 型参数，逗号分隔的一长串会被当成单个模块名，
    # 等于这两个参数完全没生效，而且不报任何错。
    for flag in ("--hidden-import", "--exclude-module"):
        values = [args[i + 1] for i, item in enumerate(args) if item == flag]
        check(values and all("," not in value for value in values),
              "%s 一个模块一个参数、没有逗号拼接（%d 项）" % (flag, len(values)))

    check(build_exe.EXE_PATH.endswith(os.path.join(build_exe.APP_NAME,
                                                   build_exe.APP_NAME + ".exe")),
          "产物路径是 dist/<名字>/<名字>.exe")


# ---------------------------------------------------------------------------
# D. 真实产物 + 真机启动（可选）
# ---------------------------------------------------------------------------
def check_artifacts(exe_path):
    print("\n=== D) 真实产物与真机启动 ===")

    if not os.path.isfile(exe_path):
        print("  [跳过] 还没有构建产物：%s" % exe_path)
        print("         先执行 python build_exe.py 再回来跑这一段。")
        return

    dist_app_dir = os.path.dirname(exe_path)
    internal = os.path.join(dist_app_dir, "_internal")
    check(os.path.isdir(internal), "存在 _internal 依赖目录（文件夹版）")

    bundled_emoji = os.path.join(internal, "app", "data", "emoji_data.json")
    check(os.path.isfile(bundled_emoji),
          "**随包 emoji 数据在解包目录里的预期位置**：%s"
          % os.path.relpath(bundled_emoji, dist_app_dir))

    # 拷到临时目录再启动：既验证"数据目录挨着 exe"，也不弄脏 dist/
    sandbox = tempfile.mkdtemp(prefix="clipboardpro_launch_")
    work = os.path.join(sandbox, APP_NAME)
    process = None
    try:
        shutil.copytree(dist_app_dir, work)
        exe = os.path.join(work, os.path.basename(exe_path))
        check(os.path.isfile(exe), "拷贝出来的 exe 存在：%s" % os.path.relpath(exe, sandbox))

        before = visible_panel_windows()
        if before:
            print("  [提示] 现在已经有 %d 个主面板窗口在显示（用户自己开的实例）。"
                  "本次验证用 --allow-multiple 另起一个，按「新增了几个窗口」判定，"
                  "不会去动那个实例。" % len(before))

        # --allow-multiple：绕开单实例守卫 —— 否则一旦用户自己开着程序，
        # 打包版会弹一个**模态**消息框"已经在运行"，把测试卡死在这里。
        # --no-listener：不注册全局快捷键与托盘，验证过程对用户无副作用。
        # 日志级别显式给 INFO：run_tests.py 会给子进程设 CLIPBOARDPRO_LOG_LEVEL=WARNING
        # （为了不刷屏），而下面要靠日志断言路径与启动结果，必须拿回 INFO 那几行。
        child_env = dict(os.environ)
        child_env["CLIPBOARDPRO_LOG_LEVEL"] = "INFO"
        process = subprocess.Popen([exe, "--show", "--no-listener", "--allow-multiple"],
                                   cwd=work, close_fds=True, env=child_env)

        deadline = time.time() + LAUNCH_TIMEOUT_SECONDS
        titles = []
        while time.time() < deadline:
            titles = visible_panel_windows()
            if len(titles) > len(before):
                break
            if process.poll() is not None:
                break
            time.sleep(0.5)

        check(len(titles) > len(before),
              "打包版真的把主面板显示出来了（可见面板 %d 个 → %d 个）"
              % (len(before), len(titles)))
        check(process.poll() is None, "面板显示后进程仍在运行（没有启动即退出）")

        data_dir = os.path.join(work, ".data")
        check(os.path.isfile(os.path.join(data_dir, "clipboardpro.db")),
              "**数据库建在 exe 旁边的 .data 里**：%s" % os.path.relpath(data_dir, sandbox))
        check(not os.path.exists(os.path.join(work, "_internal", ".data")),
              "**没有把数据建进 _internal**（这正是打包最容易踩坏的一条）")

        log_path = os.path.join(data_dir, "clipboardpro.log")
        log_text = ""
        if os.path.isfile(log_path):
            with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
                log_text = handle.read()
        check("已就绪" in log_text, "日志里出现了「已就绪」（内核启动成功）")
        check("%s v%s" % (APP_NAME, VERSION) in log_text,
              "日志里的程序名与版本号正确：%s v%s" % (APP_NAME, VERSION))

        # 这一条是"随包 emoji 数据真的被读到了"的闭环证据：
        # 日志里的资源根 == <exe 目录>\_internal，而上面已经断言那份
        # emoji_data.json 就在 <exe 目录>\_internal\app\data\ 下；
        # emoji.DATA_FILE = 资源根 + app/data/emoji_data.json（frozen 段已证），
        # 所以打包版读到的必然是完整分组表，不是兜底的两组。
        check(("程序根：%s" % os.path.abspath(work)) in log_text,
              "日志里的程序根确实是 exe 所在目录（不是 _internal）")
        check(("资源根：%s" % os.path.join(os.path.abspath(work), "_internal")) in log_text,
              "日志里的资源根确实是 exe 同级的 _internal（emoji 数据就取在这里）")
        check("打包形态：True" in log_text, "程序自己认出这是打包形态")

        check("Traceback" not in log_text, "日志里没有 Python 异常回溯")
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
        shutil.rmtree(sandbox, ignore_errors=True)


def main():
    parser = argparse.ArgumentParser(description="打包回归检查")
    parser.add_argument("--exe", default=None,
                        help="要验证的 exe 路径（省略则用 build_exe 里的默认产物）")
    args = parser.parse_args()

    print("=" * 72)
    print("打包回归：%s v%s" % (APP_NAME, VERSION))
    print("=" * 72)

    check_source_mode_is_unchanged()
    check_frozen_mode()
    check_build_script()

    if args.exe:
        exe_path = args.exe
    else:
        import build_exe
        exe_path = build_exe.EXE_PATH
    check_artifacts(exe_path)

    print("\n" + "=" * 72)
    print("通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
    if FAILED:
        print("失败清单：")
        for item in FAILED:
            print("    - %s" % item)
    print("=" * 72)
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
