# -*- coding: utf-8 -*-
"""无控制台启动器

## 为什么需要它

控制台程序（python.exe）与控制台窗口是绑定的：关掉控制台时，
Windows 会向该控制台里的所有进程发 CTRL_CLOSE_EVENT，进程随即被结束。
用户希望"关掉终端之后快捷键还能用"，所以必须让程序**脱离控制台**启动。

## 实测过的三种方式（详见 文档/设计说明.md）

| 方式 | cmd 关闭后存活 | 备注 |
|---|---|---|
| 普通 subprocess 启动 python.exe | ❌ | 随控制台一起被杀 |
| `DETACHED_PROCESS` 启动 python.exe | ✅ | **本模块采用的方式** |
| 启动 pythonw.exe | ⚠️ | 需要脚本路径全 ASCII，对中文路径会失败 |

pythonw 的坑（当时项目目录还叫「开发」时实测到的）：
`pythonw.exe "G:\\soft\\Paste PRO\\开发\\app\\main.py"` 会以退出码 2
直接失败 —— 命令行里的非 ASCII 路径解析出问题；
而同样路径下 `DETACHED_PROCESS` 方式工作正常。

> 项目目录已经改名为 ASCII 的 `1st`，`pythonw` 现在也能用了，
> 但**仍然采用 DETACHED_PROCESS**：它对路径不做任何假设，
> 将来谁把目录改成中文名也不会突然启动失败。

## 用法

    python 启动剪贴板Pro.py          # 启动（控制台立刻返回）
    python 启动剪贴板Pro.py --wait   # 前台运行，用于排查启动失败
"""

import os
import subprocess
import sys
import time

#: Windows 进程创建标志
DETACHED_PROCESS = 0x00000008
CREATE_NEW_PROCESS_GROUP = 0x00000200
CREATE_NO_WINDOW = 0x08000000

#: 启动后等待的秒数，用来确认进程没有立刻挂掉
STARTUP_GRACE_SECONDS = 2.5

DEVNULL = getattr(subprocess, "DEVNULL", os.devnull)


def find_python():
    """找出解释器路径。优先用当前解释器（保证与开发环境一致）。"""
    return sys.executable


def build_command(python, extra_args):
    """构造启动命令。

    用 ``-m app.main`` 而不是直接跑 main.py：模块方式下 ``app`` 包的父目录
    会自动进入 sys.path，不依赖脚本参数里的路径写法。
    """
    command = [python, "-m", "app.main"]
    command.extend(extra_args)
    return command


def launch_detached(command, cwd):
    """脱离控制台启动子进程，返回 Popen 对象。"""
    env = dict(os.environ)
    env.setdefault("PYTHONIOENCODING", "utf-8")
    env.setdefault("CLIPBOARDPRO_LOG_LEVEL", "INFO")
    # 让子进程的日志不被父进程的退出影响
    env["PYTHONUNBUFFERED"] = "1"

    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=DEVNULL,
        stdout=DEVNULL,
        stderr=DEVNULL,
        creationflags=flags,
        close_fds=True,
    )


def data_dir_of(cwd):
    override = os.environ.get("CLIPBOARDPRO_DATA_DIR")
    return os.path.abspath(override) if override else os.path.join(cwd, ".data")


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    cwd = os.path.dirname(os.path.abspath(__file__))
    python = find_python()

    # --wait：前台运行，把输出直接打在终端上，方便看启动失败原因
    if "--wait" in argv:
        argv.remove("--wait")
        print("以调试方式前台启动（Ctrl+C 结束）...")
        print("命令：%s" % " ".join(build_command(python, argv)))
        print("-" * 60)
        return subprocess.call(build_command(python, argv), cwd=cwd)

    # 默认给面板一个"启动即显示"的观感
    if not any(a in argv for a in ("--show", "--no-listener")):
        argv.append("--show")

    print("=" * 52)
    print("  剪贴板Pro - 单机剪贴板管理器")
    print("=" * 52)
    print()
    print("  解释器: %s" % python)
    print("  目录  : %s" % cwd)
    print()

    try:
        process = launch_detached(build_command(python, argv), cwd)
    except OSError as exc:
        print("[错误] 启动失败：%s" % exc)
        print()
        print("可以改用前台方式启动，看具体报错：")
        print("    python 启动剪贴板Pro.py --wait")
        return 1

    # 确认它没有立刻退出（最常见的失败是缺依赖或已被单实例守卫拦下）
    deadline = time.time() + STARTUP_GRACE_SECONDS
    while time.time() < deadline:
        if process.poll() is not None:
            break
        time.sleep(0.2)

    exit_code = process.poll()
    if exit_code is None:
        print("  已启动，进程号 %d" % process.pid)
        print("  本窗口可以直接关闭，程序会继续在后台运行。")
        print()
        print("  唤出面板： Ctrl+Shift+V")
        print("  托盘图标： 右键可暂停监听、清空历史或退出")
        print("  退出程序： 托盘图标右键 -> 退出")
        print()
        print("  提示：如果按快捷键没反应，可能是被其他软件占用了，")
        print("        可以单击托盘图标唤出面板，在设置里换一组快捷键。")
        return 0

    # 进程立刻退出了，说明启动有问题
    print("  [警告] 程序启动后立刻退出（退出码 %s）。" % exit_code)
    if exit_code == 3:
        print()
        print("  含义：已经有一个剪贴板Pro 在运行了（单实例保护）。")
        print("        请用快捷键或托盘图标唤出它，不要重复启动。")
        return 0
    print()
    print("  常见原因与排查：")
    print("    1. 缺少依赖：python -m pip install pillow pywin32")
    print("    2. 想看具体报错：python 启动剪贴板Pro.py --wait")
    print("    3. 查看日志：%s" % os.path.join(data_dir_of(cwd), "clipboardpro.log"))
    return exit_code or 1


if __name__ == "__main__":
    sys.exit(main())
