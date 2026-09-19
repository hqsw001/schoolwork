# -*- coding: utf-8 -*-
"""启动脚本 (.bat / .cmd) 的格式检查

**这个检查是被一个真实事故逼出来的**：

最初的 ``启动剪贴板Pro.bat`` 被写成了 LF 换行 + UTF-8 编码 + 中文提示文字，
结果双击之后窗口一闪就没了，程序根本没启动。原因：

    cmd.exe 解析 .bat 时**不认孤立的 LF（0x0A）行尾**。文件里只要有中文
    这类多字节字符，cmd 就会把行尾判断错，继续往后读，把下一行"粘"到
    上一行上。于是 ``python -m app.main`` 这一行被吞进了上一条命令的
    参数里 —— 程序从未被启动，脚本自己走到 ``exit /b`` 结束，
    表现就是"黑窗口闪一下就没"。

实测矩阵（4 种编码/代码页组合 × 2 种行尾）：

    crlf + gbk/utf8 + chcp 开/关   → 全部正常
    lf   + gbk/utf8 + chcp 开/关   → 全部报 "'xxx' is not recognized"

结论：**行尾必须是 CRLF，这与编码和 chcp 无关。**

本脚本做三条硬检查：
    A. 不能有孤立 LF（必须全部是 CRLF）  ← 唯一确证的致命项
    B. 不能有 UTF-8 BOM
    C. 非 ASCII 行必须落在 echo / rem / title 等"整行都是数据"的命令里，
       不能是一条裸命令（那种情况下中文会被当成命令名去执行）

用法：
    python -m tests.check_launchers
"""

import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

UTF8_BOM = b"\xef\xbb\xbf"

#: 这些命令的整行都是"数据"，出现中文是安全的
SAFE_PREFIXES = (
    "echo", "rem", "::", "title", "@echo", "@rem",
    "set ", "if ", "for ", "goto", "call", "exit", "pause",
    "cd ", "chcp", "python", "pip", "start", "del", "copy", "md ", "mkdir",
)


def find_launchers(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".data", ".git")]
        for name in sorted(filenames):
            if name.lower().endswith((".bat", ".cmd")):
                yield os.path.join(dirpath, name)


def check_launcher(path):
    """返回问题清单（空列表表示没问题）。"""
    problems = []
    with open(path, "rb") as fh:
        data = fh.read()

    # ---- B. BOM
    if data.startswith(UTF8_BOM):
        problems.append("文件带 UTF-8 BOM：cmd 会把 BOM 当成命令的一部分，请去掉")
    payload = data[3:] if data.startswith(UTF8_BOM) else data

    # ---- A. 换行符（确证的致命项）
    lone_lf = 0
    crlf = 0
    for index, byte in enumerate(payload):
        if byte == 0x0A:
            if index > 0 and payload[index - 1] == 0x0D:
                crlf += 1
            else:
                lone_lf += 1
    if lone_lf:
        problems.append(
            "存在 %d 处孤立 LF 换行（CRLF 只有 %d 处）：cmd 不认 LF 行尾，"
            "会把下一行粘到上一行，导致启动命令被吞掉。"
            "请把整个文件转成 CRLF 换行" % (lone_lf, crlf)
        )

    # ---- C. 非 ASCII 行的位置
    text = payload.decode("utf-8", "replace")
    for number, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or all(ord(ch) < 128 for ch in stripped):
            continue
        lowered = stripped.lower()
        if not lowered.startswith(SAFE_PREFIXES):
            problems.append(
                "第 %d 行含中文但不在 echo/rem/title 等命令里，"
                "cmd 会把中文当成命令名去执行：%r" % (number, stripped[:60])
            )
    return problems


def main():
    launchers = list(find_launchers(PROJECT_ROOT))
    if not launchers:
        print("没有找到 .bat / .cmd 启动脚本，跳过。")
        return 0

    failed = 0
    for path in launchers:
        relative = os.path.relpath(path, PROJECT_ROOT)
        problems = check_launcher(path)
        if problems:
            failed += 1
            print("[有问题] %s" % relative)
            for problem in problems:
                print("    - %s" % problem)
        else:
            print("[OK]     %s" % relative)

    print()
    if failed:
        print("%d 个启动脚本存在格式问题。" % failed)
        return 1
    print("启动脚本格式检查通过（%d 个）：换行符全为 CRLF、无 BOM、中文位置安全。"
          % len(launchers))
    return 0


if __name__ == "__main__":
    sys.exit(main())
