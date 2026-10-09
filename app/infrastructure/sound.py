# -*- coding: utf-8 -*-
"""提示音（对应 TieZ 的 ``useSoundEffects.ts``）

参考实现用 Web Audio **现场合成**一个短促的"嘀"声，不是播放音频文件。
这里照搬它的参数在 Python 里合成同样的波形，再用 ``winsound`` 播放。

波形构成（与参考实现逐项对应）：

* **三角波**振荡器，频率从 ``baseFreq × 1.25`` 指数滑到 ``baseFreq × 0.92``
* **带通滤波器**（中心 ``baseFreq × 1.3``，Q=6）——这是"清脆"的来源
* **增益包络**：4ms 快速起音 → 降到 35% → 指数衰减到听不见
* **一小段高通噪声**（1500Hz）当作起音的"齿音"

为什么不直接放一个 wav 文件：合成出来的声音只有一个几百字节的缓存，
不用往用户磁盘上塞素材，也不会有"文件被杀软删了就报错"的问题。

**播放失败一律静默。** 没有声卡、被远程桌面占着、winsound 不可用——
这些都不该影响复制粘贴这件正事。
"""

import io
import math
import struct
import threading
import wave

#: 采样率（与参考实现的 AudioContext 默认值一致）
SAMPLE_RATE = 44100

#: 事件类型
SOUND_COPY = "copy"
SOUND_PASTE = "paste"
SOUND_TYPES = (SOUND_COPY, SOUND_PASTE)

#: 每种事件的合成参数：(时长秒, 基频Hz, 音量系数)
#: 数值直接取自参考实现，改这里就等于改音色
EVENT_PARAMS = {
    SOUND_COPY: (0.06, 500, 0.8),
    # 粘贴是**两声**：第二声在 110ms 后，频率更高，听起来像"落定"
    SOUND_PASTE: (0.09, 950, 0.9),
}
PASTE_ECHO_PARAMS = (0.075, 1150, 0.75)
PASTE_ECHO_DELAY_MS = 110

#: 缓存上限（不同音量组合不会太多，防的是被异常调用刷爆）
MAX_CACHE = 32

_cache = {}
_cache_lock = threading.Lock()
_winsound = None
_winsound_checked = False


def _load_winsound():
    global _winsound, _winsound_checked
    if _winsound_checked:
        return _winsound
    _winsound_checked = True
    try:
        import winsound
        _winsound = winsound
    except ImportError:  # pragma: no cover - 非 Windows
        _winsound = None
    return _winsound


def is_available():
    """能不能放声音。"""
    return _load_winsound() is not None


# ---------------------------------------------------------------------------
# 合成
# ---------------------------------------------------------------------------
def _triangle(phase):
    """相位（0~1）→ 三角波，取值 -1~1。"""
    x = phase % 1.0
    return 4.0 * abs(x - 0.5) - 1.0


def _biquad(samples, sample_rate, kind, freq, q):
    """双二阶滤波器（RBJ cookbook 公式）。

    参考实现用的是 Web Audio 的 BiquadFilterNode，系数规则一致，
    所以出来的音色也对得上。
    """
    omega = 2.0 * math.pi * freq / sample_rate
    sin_w, cos_w = math.sin(omega), math.cos(omega)
    alpha = sin_w / (2.0 * q)

    if kind == "bandpass":
        # 常数峰值增益（peak gain = Q），与 Web Audio 的 bandpass 一致
        b0, b1, b2 = alpha, 0.0, -alpha
        a0, a1, a2 = 1.0 + alpha, -2.0 * cos_w, 1.0 - alpha
    elif kind == "highpass":
        b0, b1, b2 = (1.0 + cos_w) / 2.0, -(1.0 + cos_w), (1.0 + cos_w) / 2.0
        a0, a1, a2 = 1.0 + alpha, -2.0 * cos_w, 1.0 - alpha
    else:
        return list(samples)

    b0, b1, b2 = b0 / a0, b1 / a0, b2 / a0
    a1, a2 = a1 / a0, a2 / a0

    out = [0.0] * len(samples)
    x1 = x2 = y1 = y2 = 0.0
    for index, value in enumerate(samples):
        y0 = b0 * value + b1 * x1 + b2 * x2 - a1 * y1 - a2 * y2
        out[index] = y0
        x2, x1 = x1, value
        y2, y1 = y1, y0
    return out


def _exp_ramp(start, end, position, total):
    """指数插值。两端都不能是 0（指数曲线到不了 0），所以夹一个极小值。"""
    start = max(start, 1e-5)
    end = max(end, 1e-5)
    if total <= 0:
        return end
    ratio = max(0.0, min(1.0, position / float(total)))
    return start * ((end / start) ** ratio)


def _envelope_gain(index, total, sample_rate, volume):
    """增益包络：4ms 起音 → 降到 35% → 指数衰减。"""
    t = index / float(sample_rate)
    duration = total / float(sample_rate)
    if t < 0.004:
        return _exp_ramp(0.0001, volume, t, 0.004)
    mid = min(0.08, duration * 0.2)
    if t < mid:
        return _exp_ramp(volume, volume * 0.35, t - 0.004, max(1e-6, mid - 0.004))
    return _exp_ramp(volume * 0.35, 0.0001, t - mid, max(1e-6, duration - mid))


def synthesize(duration_sec=0.1, base_freq=1400, volume=0.35):
    """合成一段"嘀"声，返回 16 位单声道 PCM 的 float 列表。"""
    duration_sec = max(0.05, float(duration_sec))
    volume = max(0.0001, min(1.0, float(volume)))
    total = int(SAMPLE_RATE * duration_sec)
    if total <= 0:
        return []

    glide = min(0.18, duration_sec * 0.25)
    glide_samples = max(1, int(SAMPLE_RATE * glide))

    # ① 三角波 + 频率滑落
    osc = []
    phase = 0.0
    for index in range(total):
        if index < glide_samples:
            freq = _exp_ramp(base_freq * 1.25, max(80.0, base_freq * 0.92),
                             index, glide_samples)
        else:
            freq = max(80.0, base_freq * 0.92)
        phase += freq / SAMPLE_RATE
        osc.append(_triangle(phase))

    # ② 带通
    osc = _biquad(osc, SAMPLE_RATE, "bandpass",
                  min(4000.0, base_freq * 1.3), 6.0)

    # ③ 增益包络
    tone = [osc[i] * _envelope_gain(i, total, SAMPLE_RATE, volume)
            for i in range(total)]

    # ④ 起音噪声（高通 1500Hz，指数衰减）
    noise_dur = min(0.03, duration_sec * 0.1)
    noise_total = max(1, int(SAMPLE_RATE * noise_dur))
    # 用固定种子的伪随机，保证同样的参数合成出同样的字节 —— 缓存才有效
    seed = 0x2545F491
    noise = []
    for index in range(noise_total):
        seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
        value = (seed / float(0x3FFFFFFF)) - 1.0
        noise.append(value * (1.0 - index / float(noise_total)))
    noise = _biquad(noise, SAMPLE_RATE, "highpass", 1500.0, 0.707)
    for index in range(min(noise_total, total)):
        noise_gain = _exp_ramp(volume * 0.25, 0.0001, index, noise_total)
        tone[index] += noise[index] * noise_gain

    # 削顶：合成结果可能略微超过 ±1，直接转 16 位会溢出成刺耳的爆音
    return [max(-1.0, min(1.0, value)) for value in tone]


def to_wav_bytes(samples, sample_rate=SAMPLE_RATE):
    """float 列表 → 内存里的 WAV 字节（winsound 的 SND_MEMORY 要这个）。"""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        frames = b"".join(
            struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
            for value in samples)
        handle.writeframes(frames)
    return buffer.getvalue()


def wav_for(duration_sec, base_freq, volume):
    """带缓存的合成：同样的参数只算一次。"""
    key = (round(float(duration_sec), 4), round(float(base_freq), 2),
           round(float(volume), 4))
    with _cache_lock:
        cached = _cache.get(key)
    if cached is not None:
        return cached
    data = to_wav_bytes(synthesize(duration_sec, base_freq, volume))
    with _cache_lock:
        if len(_cache) >= MAX_CACHE:
            _cache.clear()
        _cache[key] = data
    return data


# ---------------------------------------------------------------------------
# 播放
# ---------------------------------------------------------------------------
#: 最近一次播放的结果（诊断用）。``None`` 表示还没播过。
_last_result = None
_last_error = None
_result_lock = threading.Lock()

#: 待播的最新一段（合并策略：永远只留最新的一段）
_pending = None
_pending_lock = threading.Lock()
_worker_started = False


def last_result():
    """最近一次播放是否成功；没播过返回 None。"""
    with _result_lock:
        return _last_result


def last_error():
    """最近一次播放失败的原因（字符串），成功时为 None。"""
    with _result_lock:
        return _last_error


def _record(ok, error=None):
    global _last_result, _last_error
    with _result_lock:
        _last_result = ok
        _last_error = error


def play_sync(data):
    """**同步**播放一段内存 WAV，返回是否成功。

    ``winsound`` 明确不支持 ``SND_MEMORY | SND_ASYNC``（会抛
    "Cannot play asynchronously from memory"），所以想不阻塞调用方
    就只能自己开线程——写临时文件再异步播的做法会带来
    "文件正被占用删不掉"的麻烦（实测复现过）。
    """
    winsound = _load_winsound()
    if winsound is None:
        _record(False, "winsound 不可用")
        return False
    if not data:
        _record(False, "没有可播放的数据")
        return False
    try:
        winsound.PlaySound(data, winsound.SND_MEMORY)
        _record(True)
        return True
    except Exception as exc:  # noqa: BLE001 - 没声卡/被占用都不该影响复制
        _record(False, str(exc))
        return False


def _play_wav(data):
    """交给后台的播放线程，立刻返回。

    为什么必须异步：``winsound.PlaySound`` 即使播 60ms 的声音也会**阻塞
    200~600ms**（音频设备初始化/驱动延迟，实测本机就是这样）。粘贴路径
    跑在界面线程上，同步播会让界面明显卡一下；采集路径卡住会拖慢后续的
    剪贴板事件。

    为什么要**合并**而不是排队：连按几次复制粘贴时，排队会让声音越来越
    滞后于操作——一个迟到一秒的提示音比没有提示音更让人困惑。所以始终
    只保留"最新的一段待播"，正在播的那段不打断。

    为什么只用一个常驻工作线程：``PlaySound`` 是**进程级**的，
    同时开多个线程只会互相打断，还会把线程数堆起来。
    """
    global _pending, _worker_started
    if not data:
        return False
    with _pending_lock:
        _pending = data
        if not _worker_started:
            _worker_started = True
            threading.Thread(target=_worker_loop, name="sound-play",
                             daemon=True).start()
    return True


def _worker_loop():
    """播放线程：有活就播，没活就退出（下次播放再起）。"""
    global _pending, _worker_started
    while True:
        with _pending_lock:
            data = _pending
            _pending = None
            if data is None:
                # 退出与"是否已有线程"必须在同一把锁里改，
                # 否则会漏播：新请求看到 _worker_started=True 就不起线程，
                # 而这边正要退出
                _worker_started = False
                return
        play_sync(data)


def pending():
    """当前是否有一段还没开始播的声音（诊断用）。"""
    with _pending_lock:
        return _pending is not None


def beep(duration_sec=0.06, base_freq=500, volume=0.35):
    """放一声（不阻塞）。"""
    return _play_wav(wav_for(duration_sec, base_freq, volume))


def _play_event(kind, enabled, volume_percent):
    """放某类事件的声音（copy 一声、paste 两声）。"""
    if not enabled or kind not in EVENT_PARAMS:
        return False
    master = max(0.0, min(1.0, float(volume_percent) / 100.0))
    if master <= 0.0:
        return False
    duration, freq, factor = EVENT_PARAMS[kind]
    played = beep(duration, freq, min(1.0, master * factor))
    if kind == SOUND_PASTE:
        echo_duration, echo_freq, echo_factor = PASTE_ECHO_PARAMS

        def echo():
            beep(echo_duration, echo_freq, min(1.0, master * echo_factor))

        timer = threading.Timer(PASTE_ECHO_DELAY_MS / 1000.0, echo)
        timer.daemon = True
        timer.start()
    return played


def play_copy(volume_percent=35):
    """复制的提示音（短、低）。"""
    return _play_event(SOUND_COPY, True, volume_percent)


def play_paste(volume_percent=35):
    """粘贴的提示音（两声、更高）。"""
    return _play_event(SOUND_PASTE, True, volume_percent)


def play(kind, volume_percent=35):
    """按事件类型播放。"""
    if kind == SOUND_COPY:
        return play_copy(volume_percent)
    if kind == SOUND_PASTE:
        return play_paste(volume_percent)
    return False


def preview(volume_percent=35):
    """设置界面里的"试听"：直接放一遍粘贴音（两声最好认）。"""
    return play_paste(volume_percent)
