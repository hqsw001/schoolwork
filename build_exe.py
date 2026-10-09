# -*- coding: utf-8 -*-
"""把剪贴板Pro 打包成 Windows exe（PyInstaller，文件夹版 / onedir）。

用法：
    python build_exe.py                 # 构建 + 自动做一次启动验证
    python build_exe.py --no-verify     # 只构建，不启动验证（CI/快速迭代用）
    python build_exe.py --clean-only    # 只清掉 build/ 与 dist/

产物（文件夹版）：
    dist/剪贴板Pro/剪贴板Pro.exe        主程序，双击即用、无控制台窗口
    dist/剪贴板Pro/_internal/          运行时依赖，**必须跟 exe 一起拷贝**
    dist/剪贴板Pro/.data/              首次运行后自动生成（历史、配置、附件）

为什么用"文件夹版"而不是单文件：
    单文件 exe 每次启动都要把整个运行时解压到 %TEMP%，首次启动要等好几秒，
    而这个程序是常驻托盘的，启动慢会被用户当成"没启动起来"。文件夹版
    启动快、报错信息完整，代价是分发时要整个文件夹一起拷。

分发时请注意：**不能只拷 exe**，`_internal` 一起拷才跑得起来。
用户数据在 `.data` 里，整个文件夹拷走即完成迁移（绿色便携）。

打包相关的三处路径适配见 `app/infrastructure/paths.py`：数据目录按 exe 所在
目录算、自启命令指向 exe 自己、随包资源按解包目录算。
"""

import argparse
import os
import shutil
import subprocess
import sys
import time

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
ENTRY_SCRIPT = os.path.join(PROJECT_ROOT, "app", "main.py")
EMOJI_DATA = os.path.join(PROJECT_ROOT, "app", "data", "emoji_data.json")

DIST_DIR = os.path.join(PROJECT_ROOT, "dist")
BUILD_DIR = os.path.join(PROJECT_ROOT, "build")
APP_NAME = "剪贴板Pro"
EXE_PATH = os.path.join(DIST_DIR, APP_NAME, APP_NAME + ".exe")

#: 明确排除的大块头。它们没有被本项目导入过，列出来是为了防止某个 hook
#: 顺手把它们拖进来，把几十 MB 的无用依赖打进包。
EXCLUDED_MODULES = (
    "numpy", "matplotlib", "scipy", "pandas",
    "PyQt5", "PyQt6", "PySide2", "PySide6",
    "IPython", "pytest", "pydoc_data",
)

#: 兜底的隐藏导入：这几个是"在函数体里 import"的（PIL/pywin32 的懒加载入口），
#: 静态分析通常找得到，但漏一个的后果是运行到某个按钮才崩，代价太大。
HIDDEN_IMPORTS = (
    "win32gui", "win32ui", "win32api", "win32con",
    "PIL.Image", "PIL.ImageTk", "PIL.ImageGrab",
)


def _setup_output():
    """让中文输出在 GBK 控制台里也不炸。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def clean():
    """清掉上一次的构建产物。"""
    for path in (BUILD_DIR, DIST_DIR):
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
            print("已清理 %s" % os.path.relpath(path, PROJECT_ROOT))


def check_environment():
    """构建前的环境自检：缺东西就早点报，别等跑到一半失败。"""
    problems = []
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        problems.append("没有安装 PyInstaller。先执行：python -m pip install pyinstaller")
    for name in ("PIL", "win32gui"):
        try:
            __import__(name)
        except ImportError:
            problems.append("缺少运行依赖 %s。先执行：python -m pip install pillow pywin32" % name)
    if not os.path.isfile(ENTRY_SCRIPT):
        problems.append("找不到入口脚本：%s" % ENTRY_SCRIPT)
    if not os.path.isfile(EMOJI_DATA):
        problems.append("找不到随包资源：%s" % EMOJI_DATA)
    return problems


def build_arguments():
    """PyInstaller 的命令行参数（唯一事实来源，spec 由它生成到 build/）。

    注意：``--hidden-import`` / ``--exclude-module`` 在 PyInstaller 里是
    append 型参数，**一个模块一个参数**，写成逗号分隔的一长串会被当成
    单个模块名，等于没写。
    """
    args = [
        ENTRY_SCRIPT,
        "--name", APP_NAME,
        "--onedir",                 # 文件夹版：启动快，失败信息完整
        "--windowed",               # 不带控制台窗口（程序自己常驻托盘）
        "--noconfirm",              # 覆盖旧产物不再问
        "--clean",                  # 清掉 PyInstaller 自己的缓存
        "--distpath", DIST_DIR,
        "--workpath", os.path.join(BUILD_DIR, "pyinstaller"),
        "--specpath", BUILD_DIR,
        # 打包后 import app 要靠它：入口脚本在 app/ 子目录里，
        # 只把 app/ 加进搜索路径是找不到 app 包的
        "--paths", PROJECT_ROOT,
        # 随包资源：落到解包目录的 app/data/ 下，与
        # app/infrastructure/emoji.py 里 DATA_FILE 的推算位置一致
        "--add-data", EMOJI_DATA + os.pathsep + "app/data",
        "--log-level", "WARN",
    ]
    for name in HIDDEN_IMPORTS:
        args += ["--hidden-import", name]
    for name in EXCLUDED_MODULES:
        args += ["--exclude-module", name]
    return args


def run_pyinstaller():
    print("=" * 72)
    print("开始打包：%s（文件夹版）" % APP_NAME)
    print("=" * 72)
    from PyInstaller.__main__ import run as pyinstaller_run
    started = time.time()
    pyinstaller_run(build_arguments())
    print("\nPyInstaller 用时 %.1f 秒" % (time.time() - started))


def report_artifacts():
    """打印产物清单与体积，帮用户判断该拷哪些东西。"""
    if not os.path.isfile(EXE_PATH):
        print("[FAIL] 没有生成 %s" % os.path.relpath(EXE_PATH, PROJECT_ROOT))
        return False
    total = 0
    for dirpath, _dirnames, filenames in os.walk(os.path.dirname(EXE_PATH)):
        for name in filenames:
            try:
                total += os.path.getsize(os.path.join(dirpath, name))
            except OSError:
                pass
    print("\n" + "=" * 72)
    print("打包完成")
    print("=" * 72)
    print("  主程序    : %s" % os.path.relpath(EXE_PATH, PROJECT_ROOT))
    print("  依赖目录  : %s" % os.path.relpath(os.path.join(DIST_DIR, APP_NAME, "_internal"),
                                               PROJECT_ROOT))
    print("  整个文件夹: %.1f MB" % (total / 1024.0 / 1024.0))
    print("\n分发时把整个 %s 文件夹拷走；只拷 exe 是跑不起来的。"
          % os.path.relpath(os.path.join(DIST_DIR, APP_NAME), PROJECT_ROOT))
    return True


def verify():
    """构建完立刻做一次真机启动验证（复用测试脚本里的同一套断言）。"""
    print("\n" + "=" * 72)
    print("启动验证")
    print("=" * 72)
    script = os.path.join(PROJECT_ROOT, "tests", "packaging_check.py")
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    completed = subprocess.run(
        [sys.executable, "-u", script, "--exe", EXE_PATH],
        cwd=PROJECT_ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    print(completed.stdout.decode("utf-8", "replace"))
    return completed.returncode == 0


def main():
    _setup_output()
    parser = argparse.ArgumentParser(description="把剪贴板Pro 打包成 exe")
    parser.add_argument("--no-verify", action="store_true",
                        help="构建完不启动 exe 做验证")
    parser.add_argument("--clean-only", action="store_true",
                        help="只清理构建产物，不打包")
    args = parser.parse_args()

    if args.clean_only:
        clean()
        return 0

    problems = check_environment()
    if problems:
        print("打包前的环境检查没通过：")
        for item in problems:
            print("    - %s" % item)
        return 1

    clean()
    run_pyinstaller()
    if not report_artifacts():
        return 1
    if args.no_verify:
        return 0
    return 0 if verify() else 1


if __name__ == "__main__":
    sys.exit(main())
