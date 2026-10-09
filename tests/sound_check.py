# -*- coding: utf-8 -*-
"""提示音回归（差距清单批次 12）

对应 TieZ 的 ``useSoundEffects.ts``：用 Web Audio **现场合成**一个短促的
"嘀"声（三角波 + 带通 + 一小段高通噪声起音），不是播放音频文件。
这里照搬同样的参数在 Python 里合成，再用 ``winsound`` 播放。

本套件盯住六个容易写错的地方：

1. **播放绝不能阻塞调用方。** ``winsound.PlaySound`` 即使播 60ms 的声音
   也会阻塞 200~600ms（音频设备初始化延迟，实测如此）。粘贴路径跑在
   界面线程上，同步播会让界面明显卡一下。
2. **连发要合并，不能排队。** 一个迟到一秒的提示音比没有提示音更让人困惑。
3. **只能有一个播放线程。** ``PlaySound`` 是进程级的，多线程只会互相打断。
4. **合成结果不能溢出。** 波形叠加后可能超过 ±1，直接转 16 位会变成爆音。
5. **只在真正入库/真正粘出去时响。** 合并、被规则忽略、降级成"请手动粘贴"
   都不该响——那会让用户以为操作成功了。
6. **声音失败不能影响复制粘贴。** 没声卡、被占用，都只记日志。

用法：python -m tests.sound_check
"""

import os
import sys
import tempfile
import threading
import time
import wave
import io

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.app.kernel import Kernel  # noqa: E402
from app.constants import ERR_INVALID_ARG  # noqa: E402
from app.infrastructure import sound as snd  # noqa: E402
from app.main import Application  # noqa: E402
from app.services import app_rules as ar  # noqa: E402

PASSED = []
FAILED = []


def check(condition, message):
    if condition:
        PASSED.append(message)
        print("  [OK]   %s" % message)
    else:
        FAILED.append(message)
        print("  [FAIL] %s" % message)


def workers():
    """当前还活着的播放线程。"""
    return [t for t in threading.enumerate() if t.name == "sound-play"]


def settle(seconds=3.0):
    """等后台把声音真的播完。

    只等 ``pending()`` 变 False 是不够的——那只说明**取走了**待播项，
    这时 ``play_sync`` 还在音频设备上阻塞着（实测 200~600ms）。
    """
    deadline = time.time() + seconds
    while time.time() < deadline:
        if not snd.pending() and not workers():
            return
        time.sleep(0.02)


def main():
    # ==================================================================
    print("=== 1) 合成 ===")
    check(snd.is_available() is True, "winsound 可用")

    for duration, freq, volume in ((0.06, 500, 0.8), (0.09, 950, 0.9),
                                   (0.075, 1150, 0.75)):
        pcm = snd.synthesize(duration, freq, volume)
        expected = int(snd.SAMPLE_RATE * duration)
        check(len(pcm) == expected,
              "%.3fs → %d 个采样（期望 %d）" % (duration, len(pcm), expected))
        check(all(-1.0 <= v <= 1.0 for v in pcm),
              "**全部采样在 ±1 内**（不然转 16 位会变爆音）")
        check(any(abs(v) > 0.01 for v in pcm), "确实有声音，不是一片静音")

    short = snd.synthesize(0.05, 1400, 0.35)
    check(len(short) > 0, "最短时长也能合成")
    check(snd.synthesize(0.0001, 500, 0.5) != [], "极短时长被夹到最小值")

    # 包络：应当先冲高再衰减
    pcm = snd.synthesize(0.1, 950, 0.9)
    tenth = max(1, len(pcm) // 10)
    head = max(abs(v) for v in pcm[:tenth])
    tail = max(abs(v) for v in pcm[-tenth:])
    check(head > tail, "包络先冲高后衰减（头 %.3f > 尾 %.3f）" % (head, tail))

    # ==================================================================
    print("\n=== 2) WAV 封装 ===")
    data = snd.wav_for(0.06, 500, 0.8)
    check(data[:4] == b"RIFF" and data[8:12] == b"WAVE", "RIFF/WAVE 头正确")
    handle = wave.open(io.BytesIO(data))
    check(handle.getnchannels() == 1, "单声道")
    check(handle.getsampwidth() == 2, "16 位")
    check(handle.getframerate() == snd.SAMPLE_RATE,
          "采样率 %d" % handle.getframerate())
    check(handle.getnframes() == int(snd.SAMPLE_RATE * 0.06), "帧数与时长对得上")
    handle.close()

    print("\n=== 3) 缓存 ===")
    first = snd.wav_for(0.09, 950, 0.9)
    second = snd.wav_for(0.09, 950, 0.9)
    check(first is second, "同样参数返回同一份字节（走了缓存）")
    check(snd.wav_for(0.06, 500, 0.8) != first, "不同参数是不同结果")
    started = time.perf_counter()
    snd.wav_for(0.09, 950, 0.9)
    check((time.perf_counter() - started) < 0.01,
          "缓存命中耗时 < 10ms")

    # ==================================================================
    print("\n=== 4) 播放不阻塞 ===")
    snd.play_sync(snd.wav_for(0.06, 500, 0.8))     # 先热一次设备
    time.sleep(0.1)
    started = time.perf_counter()
    played = snd.beep(0.06, 500, 0.8)
    elapsed_ms = (time.perf_counter() - started) * 1000
    check(played is True, "beep 返回 True")
    check(elapsed_ms < 50,
          "**beep 立刻返回（%.1f ms）**，没有等音频设备" % elapsed_ms)
    settle()
    check(snd.last_result() is True, "后台确实播成功了")
    check(snd.last_error() is None, "没有错误：%s" % snd.last_error())

    print("\n=== 5) 连发合并 + 只有一个播放线程 ===")
    for index in range(6):
        snd.beep(0.06, 500 + index * 50, 0.8)
    check(len(workers()) <= 1,
          "**最多只有一个播放线程**（实际 %d 个）" % len(workers()))
    check(snd.pending() is True, "连发之后只留最新的一段待播（合并生效）")
    settle()
    check(snd.pending() is False, "播完之后没有残留")
    check(len(workers()) == 0, "空闲时播放线程已退出")

    print("\n=== 6) 异常输入 ===")
    check(snd.play_sync(b"") is False, "空数据返回 False")
    check("没有可播放的数据" in (snd.last_error() or ""),
          "空数据给出明确原因：%s" % snd.last_error())
    check(snd.play("乱写") is False, "未知事件类型返回 False")
    check(snd._play_event("copy", False, 50) is False, "开关关闭时不播")
    check(snd._play_event("copy", True, 0) is False, "音量 0 时不播")

    # ==================================================================
    print("\n=== 7) 内核命令 ===")
    tmp = tempfile.mkdtemp(prefix="clipboardpro_sound_")
    print("       数据目录：%s" % tmp)
    kernel = Kernel(data_dir=tmp)
    kernel.start(enable_listener=False)
    try:
        settings = kernel.get_sound_settings()["data"]
        check(settings["enabled"] is False, "提示音默认关闭（不打扰用户）")
        check(settings["volume"] == 35, "默认音量 35")
        check(settings["pasteSoundEnabled"] is True, "默认粘贴也响")
        check(settings["available"] is True, "报告系统可用")
        check(settings["synthesized"] is True, "报告是现场合成而不是音频文件")

        check(kernel.set_sound_enabled(True)["data"]["enabled"] is True,
              "set_sound_enabled 打开")
        settle()
        check(kernel.get_sound_settings()["data"]["enabled"] is True,
              "配置已保存")

        check(kernel.set_sound_volume(80)["data"]["volume"] == 80, "设置音量 80")
        check(kernel.set_sound_volume(999)["data"]["volume"] == 100,
              "**越界 999 夹到 100**（不报错）")
        check(kernel.set_sound_volume(-5)["data"]["volume"] == 0,
              "**越界 -5 夹到 0**")
        bad = kernel.set_sound_volume("响一点")
        check(not bad["ok"] and bad["error"]["code"] == ERR_INVALID_ARG,
              "非数字被拒绝（%s）" % ERR_INVALID_ARG)

        # 音量在上一段被夹成了 0，测播放行为前先恢复——否则"复制音不受影响"
        # 会因为音量为 0 而假失败
        kernel.set_sound_volume(35)
        check(kernel.set_paste_sound_enabled(False)["data"]["pasteSoundEnabled"]
              is False, "关闭粘贴音")
        check(kernel._play_sound("paste") is False, "**关掉之后粘贴音确实不播**")
        check(kernel._play_sound("copy") is True, "复制音不受影响")
        settle()

        kernel.set_sound_volume(35)
        check(kernel.preview_sound("paste")["ok"] is True, "试听 paste")
        settle()
        bad_kind = kernel.preview_sound("乱写")
        check(not bad_kind["ok"] and bad_kind["error"]["code"] == ERR_INVALID_ARG,
              "试听非法类型被拒（%s）" % ERR_INVALID_ARG)

        # ==============================================================
        print("\n=== 8) 只在恰当的时机响 ===")
        kernel.set_sound_enabled(False)     # 先关掉，避免干扰计数
        kernel.simulate_capture("声音测试第一条")
        settle()
        before = snd.last_result()
        check(before is True, "（上一轮的结果还在）")

        # 真正入库时会播：直接数 _play_sound 的调用
        calls = []
        original = kernel._play_sound
        kernel._play_sound = lambda kind: calls.append(kind) or True
        try:
            kernel.simulate_capture("一条全新内容")
            check(calls == [snd.SOUND_COPY],
                  "**新内容入库响一声复制音**（实际 %s）" % calls)

            calls.clear()
            kernel.simulate_capture("一条全新内容")     # 重复 → 合并
            check(calls == [],
                  "**重复内容被合并时不响**（实际 %s）" % calls)

            # 被应用规则忽略时不响
            kernel.set_app_cleanup_policies([{
                "id": "r1", "enabled": True, "appName": "Secret.exe",
                "action": "ignore", "contentTypes": ["text"], "cleanupRules": "",
            }])
            calls.clear()
            kernel.simulate_capture("密码内容", source_name="Secret.exe")
            check(calls == [],
                  "**被应用规则忽略时不响**（实际 %s）" % calls)
            kernel.set_app_cleanup_policies([])
        finally:
            kernel._play_sound = original

        # ==============================================================
        print("\n=== 9) 声音失败不影响主流程 ===")
        broken = kernel._play_sound
        try:
            def explode(kind):
                raise RuntimeError("声卡炸了")
            kernel._play_sound = explode
            # _play_sound 自己的 try/except 在方法内部，这里直接测采集不受影响
            kernel.simulate_capture("声卡坏掉时的一条内容")
            check(True, "采集过程没有因为声音异常而中断")
        finally:
            kernel._play_sound = broken
        check(kernel.repo.count() >= 2,
              "内容照常入库（%d 条）" % kernel.repo.count())

    finally:
        kernel.shutdown()

    # ==================================================================
    print("\n=== 10) 设置界面 ===")
    tmp2 = tempfile.mkdtemp(prefix="clipboardpro_soundui_")
    app = Application(data_dir=tmp2, enable_listener=False, silent_start=True)
    try:
        app.kernel.start(enable_listener=False)
        app.panel.show(focus_search=False)
        deadline = time.time() + 0.8
        while time.time() < deadline:
            try:
                app.root.update()
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.02)

        app.panel.open_settings()
        deadline = time.time() + 1.2
        while time.time() < deadline:
            try:
                app.root.update()
            except Exception:  # noqa: BLE001
                break
            time.sleep(0.02)

        from app.ui.settings_window import SettingsWindow
        windows = [w for w in app.panel.window.winfo_children()
                   if isinstance(w, SettingsWindow)]
        check(len(windows) == 1, "设置窗口已打开")
        if windows:
            window = windows[0]
            check(hasattr(window, "sound_var"), "有提示音开关")
            check(hasattr(window, "volume_var"), "有音量滑块")
            check(hasattr(window, "paste_sound_var"), "有粘贴音开关")

            window.sound_var.set(True)
            window._on_sound_toggle()
            app.root.update()
            check(app.kernel.get_sound_settings()["data"]["enabled"] is True,
                  "开关写进了配置")

            window.volume_var.set(70)
            window._on_volume_change()
            app.root.update()
            check(app.kernel.get_sound_settings()["data"]["volume"] == 70,
                  "音量写进了配置")

            window.volume_var.set(999)
            window._on_volume_change()
            app.root.update()
            check(window.volume_var.get() == 100,
                  "**越界值被夹回并同步到控件**（%s）" % window.volume_var.get())

            window.paste_sound_var.set(False)
            window._on_paste_sound_change()
            app.root.update()
            check(app.kernel.get_sound_settings()["data"]["pasteSoundEnabled"]
                  is False, "粘贴音开关写进了配置")

            window._on_preview_sound()
            app.root.update()
            check(True, "试听不抛异常")
            settle()
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
