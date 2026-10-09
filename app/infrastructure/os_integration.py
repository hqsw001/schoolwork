# -*- coding: utf-8 -*-
"""系统集成：打开内容、开机自启、Win+V 接管、提权检测、平台信息

这一层只碰操作系统，不理解"剪贴板记录"这种概念——上层把内容类型和内容
传进来，这里负责变成一次具体的系统动作。

**设计约束：导入本模块不得产生任何副作用。** 注册表读写只发生在显式调用
的 set_* 函数里。特别是 Win+V 接管：它会真的把 Windows 自带的 Win+V 关掉，
绝不能顺手就做。

可测试性：真正"执行"的出口只有 :func:`shell_execute` 一个函数，
测试可以替换它来断言"打算做什么"，而不必真的弹出资源管理器。
纯逻辑（注册表值的增删、打开方式的判定）都拆成了无副作用的函数。
"""

import ctypes
import ctypes.wintypes as wt
import os
import subprocess
import sys
import tempfile
import time

from app.infrastructure import paths

#: 开机自启在注册表 Run 键下的值名
AUTOSTART_VALUE = "剪贴板Pro"

#: 旧的注册表值名（早期版本用过，升级时要一并清掉，否则会出现两条自启）
LEGACY_AUTOSTART_VALUES = ("tie-z", "TieZ")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
EXPLORER_ADVANCED_KEY = r"Software\Microsoft\Windows\CurrentVersion\Explorer\Advanced"
CLIPBOARD_POLICY_KEY = r"Software\Microsoft\Windows\CurrentVersion\Policies\Explorer"
SYSTEM_POLICY_KEY = r"Software\Policies\Microsoft\Windows\System"

#: ShellExecuteW 返回值 <= 32 表示失败（Windows 的约定）
SHELL_EXECUTE_MIN_SUCCESS = 32

#: 临时文件前缀——打开文本类内容时会落到 %TEMP%
TEMP_PREFIX = "剪贴板Pro_"


# ---------------------------------------------------------------------------
# Win32 绑定
# ---------------------------------------------------------------------------
def _bind():
    shell32 = ctypes.WinDLL("shell32", use_last_error=True)
    shell32.ShellExecuteW.restype = wt.HINSTANCE
    shell32.ShellExecuteW.argtypes = [
        wt.HWND, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, wt.LPCWSTR, ctypes.c_int,
    ]
    return shell32


try:  # pragma: no cover - 非 Windows 平台直接降级
    _SHELL32 = _bind()
except OSError:  # pragma: no cover
    _SHELL32 = None


def shell_execute(target, params=None, verb=None, logger=None):
    """调用 ShellExecuteW 打开目标。成功返回 True。

    这是本模块**唯一的执行出口**，测试替换它即可断言行为而不产生副作用。
    返回值 <= 32 是 Windows 约定的失败码，必须显式判掉——只判断"没抛异常"
    会把"文件关联缺失"这类失败当成成功。
    """
    if _SHELL32 is None:  # pragma: no cover
        return False
    if not target:
        return False
    try:
        result = _SHELL32.ShellExecuteW(
            None, verb, str(target),
            str(params) if params else None, None, 1,   # SW_SHOWNORMAL
        )
    except Exception as exc:  # noqa: BLE001
        if logger:
            logger.warning("ShellExecute 失败（%s）：%s", target, exc)
        return False
    ok = int(result) > SHELL_EXECUTE_MIN_SUCCESS
    if not ok and logger:
        logger.warning("ShellExecute 返回失败码 %s（目标：%s）", int(result), target)
    return ok


# ---------------------------------------------------------------------------
# 重启 / 提权重启（对应 TieZ 的 relaunch / restart_as_admin）
# ---------------------------------------------------------------------------
def relaunch_command():
    """返回"重新启动本程序"要用到的 (可执行文件, 参数)。

    分成"算命令"和"执行"两步是为了可测：测试断言命令拼得对，
    而不会真的再启动一个进程。
    """
    executable = paths.executable_path()
    # 打包成 exe 时 sys.executable 就是本程序；源码运行时要在参数里
    # 带上入口脚本，否则重启出来的是一个空白的 Python 解释器
    if paths.is_frozen():
        return executable, ""
    entry = os.path.join(paths.program_root(), "app", "main.py")
    return executable, '"%s"' % entry


def relaunch(extra_args=None, logger=None):
    """重新启动本程序（新进程起来之后再退出当前进程）。

    **必须先 spawn 成功再返回**：先退出的话新进程可能来不及起来，
    用户会看到"程序自己关了"。
    """
    executable, params = relaunch_command()
    if extra_args:
        params = (params + " " + " ".join(str(a) for a in extra_args)).strip()
    try:
        # DETACHED_PROCESS：让新进程脱离当前控制台，
        # 否则关掉旧进程时新进程会跟着一起被收走
        subprocess.Popen([executable] + ([params] if params else []),
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0),
                         close_fds=True)
        if logger:
            logger.info("已启动新实例，准备退出当前进程")
        return True
    except OSError as exc:
        if logger:
            logger.warning("重新启动失败：%s", exc)
        return False


def restart_as_admin(logger=None):
    """以管理员身份重新启动本程序（对应命令 restart_as_admin）。

    走 ShellExecuteW 的 ``runas`` 动词——这会弹 UAC。
    用户点"否"时 ShellExecuteW 返回失败码，如实返回 False，
    不能当成"已经提权了"。
    """
    executable, params = relaunch_command()
    ok = shell_execute(executable, params, verb="runas", logger=logger)
    if not ok and logger:
        logger.info("提权重启未完成（用户可能取消了 UAC）")
    return ok


# ---------------------------------------------------------------------------
# 打开内容
# ---------------------------------------------------------------------------
def plan_open(content_type, content):
    """决定"打开这条记录"应该做什么（纯函数，不产生任何副作用）。

    返回 ``{"action", "target", "suffix"}``：

        url      直接用浏览器打开 target
        path     用系统默认程序打开 target（已存在的文件/目录）
        tempfile 先把 target 写进临时文件（后缀 suffix）再打开
    """
    text = (content or "").strip()
    if content_type == "url":
        return {"action": "url", "target": text, "suffix": None}

    if content_type in ("file", "video"):
        paths = [line.strip() for line in text.split("\n") if line.strip()]
        return {"action": "path", "target": paths[0] if paths else "", "suffix": None}

    if content_type == "image":
        # 图片记录的 content 就是附件绝对路径
        return {"action": "path", "target": text, "suffix": None}

    if content_type == "rich_text":
        return {"action": "tempfile", "target": text, "suffix": ".html"}

    return {"action": "tempfile", "target": text, "suffix": ".txt"}


def cleanup_temp_files(max_age_seconds=24 * 3600, logger=None):
    """清掉打开内容时留下的临时文件（启动时调用一次）。

    不抛异常：临时目录清理失败不该影响启动。
    """
    removed = 0
    try:
        root = tempfile.gettempdir()
        deadline = time.time() - max_age_seconds
        for name in os.listdir(root):
            if not name.startswith(TEMP_PREFIX):
                continue
            full = os.path.join(root, name)
            try:
                if os.path.isfile(full) and os.path.getmtime(full) < deadline:
                    os.remove(full)
                    removed += 1
            except OSError:
                continue
    except OSError as exc:
        if logger:
            logger.warning("清理临时文件失败：%s", exc)
    return removed


def write_temp_content(content, suffix):
    """把内容写进临时文件，返回路径。"""
    stamp = time.strftime("%Y%m%d-%H%M%S")
    handle, path = tempfile.mkstemp(prefix=TEMP_PREFIX, suffix=suffix or ".txt")
    os.close(handle)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(content or "")
    # mkstemp 的名字带随机串，重新命名成带时间戳的，方便用户辨认
    friendly = os.path.join(os.path.dirname(path),
                            "%s%s%s" % (TEMP_PREFIX, stamp, suffix or ".txt"))
    try:
        os.replace(path, friendly)
        return friendly
    except OSError:
        return path


def open_content(content_type, content, logger=None):
    """按 plan_open 的结论真的打开内容。返回 plan（多一个 opened 字段）。"""
    plan = plan_open(content_type, content)
    target = plan["target"]
    plan["opened"] = False

    if not target:
        return plan
    if plan["action"] == "url":
        plan["opened"] = shell_execute(target, logger=logger)
        return plan
    if plan["action"] == "path":
        if os.path.exists(target):
            plan["opened"] = shell_execute(target, logger=logger)
        elif logger:
            logger.warning("要打开的目标不存在：%s", target)
        return plan
    # tempfile
    try:
        path = write_temp_content(target, plan["suffix"])
    except OSError as exc:
        if logger:
            logger.warning("写临时文件失败：%s", exc)
        return plan
    plan["tempPath"] = path
    plan["opened"] = shell_execute(path, logger=logger)
    return plan


def open_folder(path, logger=None):
    """在资源管理器里打开一个目录。"""
    if not path or not os.path.isdir(path):
        return False
    return shell_execute(path, logger=logger)


def open_file_location(path, logger=None):
    """打开文件所在目录并选中它。"""
    if not path or not os.path.exists(path):
        return False
    return shell_execute("explorer.exe", '/select,"%s"' % path, logger=logger)


def restart_explorer(logger=None):
    """重启资源管理器（托盘图标丢失或注册表改动需要生效时用）。"""
    try:
        subprocess.Popen("taskkill /F /IM explorer.exe & start explorer.exe",
                         shell=True)
        return True
    except OSError as exc:
        if logger:
            logger.warning("重启资源管理器失败：%s", exc)
        return False


# ---------------------------------------------------------------------------
# 提权与平台信息
# ---------------------------------------------------------------------------
def is_admin():
    """当前进程是否以管理员身份运行。"""
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        TOKEN_QUERY = 0x0008
        TOKEN_ELEVATION = 20

        token = wt.HANDLE()
        if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(),
                                         TOKEN_QUERY, ctypes.byref(token)):
            return False
        try:
            elevation = ctypes.c_ulong(0)
            returned = ctypes.c_ulong(0)
            ok = advapi32.GetTokenInformation(
                token, TOKEN_ELEVATION, ctypes.byref(elevation),
                ctypes.sizeof(elevation), ctypes.byref(returned))
            return bool(ok) and bool(elevation.value)
        finally:
            kernel32.CloseHandle(token)
    except Exception:  # noqa: BLE001 - 提权检测失败一律按"否"处理
        return False


def platform_info():
    """平台信息：Windows 版本与是否为 Win11。

    Win11 判定用 build >= 22000，这个阈值决定云母/亚克力背景能不能用。
    """
    info = {"platform": sys.platform, "isWindows": sys.platform == "win32"}
    if sys.platform != "win32":
        return info
    try:
        version = sys.getwindowsversion()
    except Exception:  # noqa: BLE001
        return info
    build = int(getattr(version, "build", 0))
    info.update({
        "major": int(version.major),
        "minor": int(version.minor),
        "build": build,
        "release": "%d.%d.%d" % (version.major, version.minor, build),
        "isWindows11": build >= 22000,
        "isWindows10": 10240 <= build < 22000,
    })
    return info


# ---------------------------------------------------------------------------
# 开机自启
# ---------------------------------------------------------------------------
def autostart_command():
    """开机自启要执行的命令行。

    源码运行：用"解释器 + 启动器"而不是 .bat —— .bat 会闪一个黑窗口。
    启动器本身就是分离进程的，主进程退出后它仍在后台运行。

    **打包成 exe 之后**：exe 自己就是自启目标，再拼解释器和启动器是错的
    （那条命令里的 `启动剪贴板Pro.py` 根本不存在，自启会静默失效）。
    """
    if paths.is_frozen():
        # 路径可能带空格（例如放在"Paste PRO"这种目录下），必须加引号
        return '"%s"' % paths.executable_path()
    root = paths.program_root()
    launcher = os.path.join(root, "启动剪贴板Pro.py")
    if os.path.isfile(launcher):
        return '"%s" "%s"' % (paths.executable_path(), launcher)
    return '"%s" -m app.main' % paths.executable_path()


def is_autostart_enabled(value_name=AUTOSTART_VALUE):
    """检查注册表里有没有自启项（含早期版本用过的旧名字）。"""
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return False
    names = (value_name,) + LEGACY_AUTOSTART_VALUES
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            for name in names:
                try:
                    winreg.QueryValueEx(key, name)
                    return True
                except FileNotFoundError:
                    continue
    except OSError:
        return False
    return False


def set_autostart(enabled, value_name=AUTOSTART_VALUE, command=None):
    """开启/关闭开机自启。返回 (是否成功, 说明)。"""
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return False, "当前平台不支持注册表自启"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            if enabled:
                winreg.SetValueEx(key, value_name, 0, winreg.REG_SZ,
                                  command or autostart_command())
                # 清掉旧值名，避免同一次开机被拉起两次
                for legacy in LEGACY_AUTOSTART_VALUES:
                    try:
                        winreg.DeleteValue(key, legacy)
                    except FileNotFoundError:
                        pass
                return True, "已开启开机自启"
            removed = 0
            for name in (value_name,) + LEGACY_AUTOSTART_VALUES:
                try:
                    winreg.DeleteValue(key, name)
                    removed += 1
                except FileNotFoundError:
                    continue
            return True, ("已关闭开机自启" if removed else "开机自启本来就是关闭的")
    except OSError as exc:
        return False, "写注册表失败：%s" % exc


# ---------------------------------------------------------------------------
# Win+V 接管
# ---------------------------------------------------------------------------
def merge_disabled_hotkey(current, letter="V"):
    """把某个字母加进 DisabledHotkeys（纯函数，便于测试）。"""
    letter = (letter or "").strip().upper()
    if not letter:
        return current or ""
    existing = [ch for ch in (current or "") if ch.isalpha()]
    if letter in [ch.upper() for ch in existing]:
        return "".join(existing)
    return "".join(existing) + letter


def remove_disabled_hotkey(current, letter="V"):
    """把某个字母从 DisabledHotkeys 里拿掉（纯函数）。"""
    letter = (letter or "").strip().upper()
    return "".join(ch for ch in (current or "") if ch.isalpha() and ch.upper() != letter)


def _read_disabled_hotkeys():
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return ""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, EXPLORER_ADVANCED_KEY) as key:
            return winreg.QueryValueEx(key, "DisabledHotkeys")[0] or ""
    except OSError:
        return ""


def _write_disabled_hotkeys(value):
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, EXPLORER_ADVANCED_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
        if value:
            winreg.SetValueEx(key, "DisabledHotkeys", 0, winreg.REG_SZ, value)
        else:
            try:
                winreg.DeleteValue(key, "DisabledHotkeys")
            except FileNotFoundError:
                pass


def _read_clipboard_history_flags():
    """读取系统剪贴板历史相关的两个 DWORD。返回 (EnableClipboardHistory, EnableCloudClipboard)。"""
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return None, None
    values = []
    for name in ("EnableClipboardHistory", "EnableCloudClipboard"):
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Clipboard") as key:
                values.append(winreg.QueryValueEx(key, name)[0])
        except OSError:
            values.append(None)
    return values[0], values[1]


def is_win_v_disabled():
    """Win+V 是否已被我们接管（DisabledHotkeys 里含 V）。"""
    return "V" in [ch.upper() for ch in _read_disabled_hotkeys()]


def win_v_status():
    """Win+V 与系统剪贴板历史的完整状态。"""
    history, cloud = _read_clipboard_history_flags()
    return {
        "disabledHotkeys": _read_disabled_hotkeys(),
        "vDisabled": is_win_v_disabled(),
        "historyEnabled": history,
        "cloudClipboardEnabled": cloud,
        # 用户实际能不能用系统 Win+V：历史开着**并且** V 没被禁用
        "windowsWinVAvailable": (history == 1) and not is_win_v_disabled(),
    }


def set_win_v_optimized(enabled, logger=None):
    """接管 / 归还 Win+V。

    开启时把 V 加进 DisabledHotkeys 并关掉系统剪贴板历史与云剪贴板；
    关闭时反向操作，并清掉可能让 Win+V 一直失效到重启的策略锁。
    返回 (是否成功, 说明, 状态字典)。
    """
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return False, "当前平台不支持注册表操作", win_v_status()
    try:
        current = _read_disabled_hotkeys()
        _write_disabled_hotkeys(
            merge_disabled_hotkey(current, "V") if enabled
            else remove_disabled_hotkey(current, "V"))

        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Clipboard", 0,
                                winreg.KEY_SET_VALUE) as key:
            flag = 0 if enabled else 1
            winreg.SetValueEx(key, "EnableClipboardHistory", 0, winreg.REG_DWORD, flag)
            winreg.SetValueEx(key, "EnableCloudClipboard", 0, winreg.REG_DWORD, flag)

        if not enabled:
            # 归还时必须清策略锁，否则系统 Win+V 会一直不可用
            _delete_policy_value(winreg, CLIPBOARD_POLICY_KEY, "DisallowClipboardHistory")
            _delete_policy_value(winreg, SYSTEM_POLICY_KEY, "AllowClipboardHistory")
            _delete_policy_value(winreg, SYSTEM_POLICY_KEY, "AllowCrossDeviceClipboard")

        return True, ("已接管 Win+V（系统剪贴板历史已关闭）" if enabled
                      else "已归还 Win+V 给 Windows"), win_v_status()
    except OSError as exc:
        if logger:
            logger.warning("切换 Win+V 接管失败：%s", exc)
        return False, "写注册表失败：%s" % exc, win_v_status()


def _delete_policy_value(winreg, key_path, name):
    """删掉一个策略值（不存在就跳过）。"""
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, name)
            return True
    except (FileNotFoundError, OSError):
        return False


def set_windows_clipboard_history(enabled, logger=None):
    """开启 / 关闭 Windows 自带的剪贴板历史（Win+V）。

    这个是"把系统功能还回去"的入口，与 set_win_v_optimized 互为镜像。
    """
    try:
        import winreg
    except ImportError:  # pragma: no cover
        return False, "当前平台不支持注册表操作"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Clipboard", 0,
                                winreg.KEY_SET_VALUE) as key:
            flag = 1 if enabled else 0
            winreg.SetValueEx(key, "EnableClipboardHistory", 0, winreg.REG_DWORD, flag)
            winreg.SetValueEx(key, "EnableCloudClipboard", 0, winreg.REG_DWORD, flag)

        if enabled:
            # 还原时把 V 从禁用列表里摘掉，否则按了还是没反应
            current = _read_disabled_hotkeys()
            _write_disabled_hotkeys(remove_disabled_hotkey(current, "V"))
            _delete_policy_value(winreg, CLIPBOARD_POLICY_KEY, "DisallowClipboardHistory")
        return True, ("已开启系统剪贴板历史" if enabled else "已关闭系统剪贴板历史")
    except OSError as exc:
        if logger:
            logger.warning("切换系统剪贴板历史失败：%s", exc)
        return False, "写注册表失败：%s" % exc
