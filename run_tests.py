# -*- coding: utf-8 -*-
"""一次性跑完全部测试，输出汇总结果。

用法：
    python run_tests.py                 跑除"真机粘贴"以外的全部测试
    python run_tests.py --all           连真机粘贴一起跑（会操作系统剪贴板与记事本）
"""

import os
import subprocess
import sys
import time

ROOT = os.path.dirname(os.path.abspath(__file__))

#: (脚本, 说明, 是否会操作真实剪贴板/窗口)
SUITES = [
    ("tests/static_check.py", "静态自检：未定义名字与多余导入", False),
    ("tests/check_launchers.py", "启动脚本格式：CRLF 换行 / 无 BOM / 中文位置", False),
    ("tests/smoke_repository.py", "仓储层冒烟：建库、增删改查、检索、裁剪", False),
    ("tests/e2e_pipeline.py", "端到端链路：采集→识别→去重→入库→检索→粘贴写回", True),
    ("tests/ui_smoke.py", "界面冒烟：渲染、筛选、主题、事件刷新（会短暂弹窗）", True),
    ("tests/ui_regression_check.py", "界面回归：设置窗口入口/单例、历史分页（会短暂弹窗）", True),
    ("tests/card_ui_check.py", "卡片能力：缩略图、颜色色块、逐条操作按钮（会短暂弹窗）", True),
    ("tests/os_integration_check.py", "系统集成：打开内容、文件定位、自启、Win+V、平台", False),
    ("tests/icons_apps_check.py", "图标与关联应用：exe/文件类型图标、打开方式、应用扫描", False),
    ("tests/clipboard_core_check.py", "剪贴板核心：手动新增、置顶排序、临时粘贴、快捷键探测", True),
    ("tests/content_type_check.py", "内容类型：视频类型、文件类判定、类型表一致性", False),
    ("tests/rich_text_check.py", "富文本链路：CF_HTML 解析、Office 清洗、截断、命名格式往返", True),
    ("tests/sequential_paste_check.py", "顺序粘贴：队列、自动入队、逐条粘出、失效跳过", True),
    ("tests/tag_manager_check.py", "标签管理：全局重命名、内置标签保护、批量操作（会短暂弹窗）", True),
    ("tests/emoji_panel_check.py", "表情面板：Emoji 分组、收藏去重、目录穿越防护（会短暂弹窗）", True),
    ("tests/encryption_check.py", "隐私加密：DPAPI 落盘、密文可搜、敏感标签联动、掩码规则", False),
    ("tests/app_rules_check.py", "应用规则：忽略采集、自动清洗、四种匹配写法、全局规则", False),
    ("tests/window_behavior_check.py", "窗口行为：标题栏、收起/最小化、脱离控制台存活", True),
    ("tests/window_docking_check.py", "窗口停靠：贴边吸附、跑出屏幕修复、多屏重映射、失焦策略", True),
    ("tests/preview_check.py", "紧凑预览：定位算法、内容载荷、浮窗行为（会短暂弹窗）", True),
    ("tests/sound_check.py", "提示音：现场合成、非阻塞播放、连发合并、设置界面", False),
    ("tests/hotkey_check.py", "快捷键与托盘：降级注册、隐藏态唤出、真按键验证", True),
    ("tests/manual_clipboard_check.py", "真机剪贴板：监听、来源、图片、快捷键、托盘", True),
    ("tests/image_fidelity_check.py", "图片保真：DIB 解码与入库附件逐像素一致", True),
    ("tests/manual_paste_check.py", "真机粘贴：焦点交还 + 回声抑制", True),
    ("tests/manual_notepad_paste.py", "真机粘贴：内容真的进到记事本里", True),
    ("tests/packaging_check.py", "打包回归：exe 形态路径、自启命令、产物与启动验证", True),
]


def run(script, description):
    print("\n" + "=" * 72)
    print(">>> %s\n    %s" % (description, script))
    print("=" * 72)
    started = time.time()
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("CLIPBOARDPRO_LOG_LEVEL", "WARNING")
    completed = subprocess.run(
        [sys.executable, "-u", script],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    output = completed.stdout.decode("utf-8", "replace")
    # 只打印结论行，避免刷屏；完整输出在需要时单独跑对应脚本
    for line in output.splitlines():
        if any(marker in line for marker in
               ("[FAIL]", "[有问题]", "通过 ", "已通过", "失败清单", "静态自检",
                "全部通过", "格式检查", "  - ", "Traceback")):
            print(line)
    elapsed = time.time() - started
    return completed.returncode == 0, elapsed


def main():
    include_real_devices = "--all" in sys.argv
    results = []
    for script, description, touches_system in SUITES:
        if touches_system and not include_real_devices:
            # 仍然跑这些脚本：它们自己会创建目标窗口/记事本并在结束时清理。
            # 但如果调用方希望完全不动系统剪贴板，可以加 --safe 跳过。
            if "--safe" in sys.argv:
                print("\n[跳过] %s（--safe 模式）" % description)
                continue
        ok, elapsed = run(script, description)
        results.append((description, ok, elapsed))

    print("\n" + "=" * 72)
    print("汇总")
    print("=" * 72)
    failed = 0
    for description, ok, elapsed in results:
        print("  %s  %-52s %5.1fs" % ("[OK]  " if ok else "[FAIL]", description, elapsed))
        if not ok:
            failed += 1
    print("\n共 %d 个测试套件，失败 %d 个。" % (len(results), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
