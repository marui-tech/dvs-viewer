"""
DVS Camera Manager — OpenEB HAL 封装

架构：
  采集线程通过 HAL get_latest_raw_data() / decode() 拉取事件，
  解码后经 add_event_callback() 注册的回调（如 OpenGL 渲染线程）推送给消费者。
  参数调节（Bias / ERC / ROI 等）通过 HAL Facility 方法直接写入硬件寄存器。
  所有共享状态通过 threading.Lock 保护。
"""
import threading
import time
import enum
import struct as _struct
import shutil
import queue  as _queue
import numpy as np
from typing import Optional, Dict, Any
import os, sys

# ── OpenEB 路径 ─────────────────────────────────────────────────────────────
OPENEB_PY   = r"C:\openeb\build\py3\Release"
OPENEB_BIN  = r"C:\openeb\build\bin\Release"
OPENEB_PLUG = r"C:\openeb\build\lib\metavision\hal\plugins"

if OPENEB_PY not in sys.path:
    sys.path.insert(0, OPENEB_PY)
try:
    os.add_dll_directory(OPENEB_BIN)
    os.add_dll_directory(r"C:\vcpkg\installed\x64-windows\bin")
except Exception:
    pass
os.environ.setdefault("MV_HAL_PLUGIN_PATH", OPENEB_PLUG)
os.environ.setdefault("HDF5_PLUGIN_PATH", r"C:\openeb\build\lib\hdf5\plugin")

# ── Bias 定义 ──────────────────────────────────────────────────────────────
BIAS_DEFS: Dict[str, Dict] = {
    "bias_fo":       {"default": 72,  "min": 52,  "max": 72,  "desc": "低通截止频率（越大噪声越少）"},
    "bias_hpf":      {"default": 0,   "min": 0,   "max": 120, "desc": "高通滤波（去背景漂移）"},
    "bias_diff_on":  {"default": 98,  "min": 18,  "max": 243, "desc": "ON 事件阈值（越大灵敏度越低）"},
    "bias_diff":     {"default": 77,  "min": 52,  "max": 100, "desc": "差分增益"},
    "bias_diff_off": {"default": 49,  "min": 19,  "max": 249, "desc": "OFF 事件阈值（越大灵敏度越低）"},
    "bias_refr":     {"default": 20,  "min": 0,   "max": 255, "desc": "不应期（越大事件率越低）"},
}

# 录制 IO 队列大小：采集线程最多预存多少个 10ms 切片再开始丢弃
# 200 × 10ms = 2s 缓冲；IO 线程跟不上时丢弃新帧而非阻塞采集线程
_REC_QUEUE_MAX = 200

# DVS 事件结构体 dtype（与 dvs_viewer.py 一致）
_EVT_DTYPE = np.dtype([('x','<u2'),('y','<u2'),('p','<i2'),('t','<i8')])


class CameraState(str, enum.Enum):
    DISCONNECTED = "disconnected"
    CONNECTED    = "connected"
    STREAMING    = "streaming"
    ERROR        = "error"


class CameraError(Exception):
    pass


class CameraManager:
    def __init__(self):
        self._lock = threading.Lock()

        # HAL Device 对象（由 DeviceDiscovery.open 获取）
        self._device = None
        self._state = CameraState.DISCONNECTED

        # 传感器参数
        self._width  = 0
        self._height = 0
        self._serial = ""

        # HAL facilities（best-effort，不支持则为 None）
        self._f_biases     = None
        self._f_erc        = None
        self._f_roi        = None
        self._f_monitoring = None
        self._f_aflicker   = None
        self._f_actfilter  = None
        self._f_trig_in    = None
        self._f_trig_out   = None

        # 流控
        self._stop_flag   = threading.Event()
        self._capture_thr: Optional[threading.Thread] = None
        self._delta_t_us  = 10_000   # 事件切片时间窗口（µs），默认 10ms

        # 录制（IO 线程解耦磁盘写入，采集线程只做非阻塞 queue.put_nowait）
        self._rec_path    : Optional[str]              = None
        self._rec_tmpfile : Optional[str]              = None
        self._rec_total   : int                        = 0
        self._rec_queue   : Optional[_queue.Queue]     = None   # None = 未录制
        self._rec_io_thr  : Optional[threading.Thread] = None
        self._rec_lock    = threading.Lock()
        # 录制过滤器（采集线程读，主线程写，GIL 保证赋值原子性）
        self._rec_roi     : Optional[tuple] = None   # (x0,y0,x1,y1) or None=全帧
        self._rec_keep_1in: int             = 1      # 保留 1/N 事件（1=全量）
        self._rec_skip_n  : int             = 0      # 内部计数器

        # 统计
        self._total_events    = 0
        self._event_rate_kevs = 0.0
        self._rate_window:    list = []
        self._stats_lock = threading.Lock()

        # 原始事件回调（供 OpenGL 渲染线程等外部消费者注册，在 capture 线程中调用）
        self._event_callbacks: list = []

        # ERC / ROI / Anti-flicker 缓存
        self._erc_enabled   = False
        self._erc_threshold = 10_000_000
        self._roi_cfg = {"enabled": False, "x": 0, "y": 0, "width": 0, "height": 0}
        self._aflicker_cfg = {"enabled": False, "freq_low": 50, "freq_high": 70}

    # ─────────────────────────────── 属性 ───────────────────────────────────
    @property
    def state(self) -> CameraState:
        return self._state

    @property
    def is_streaming(self) -> bool:
        return self._state == CameraState.STREAMING

    # ─────────────────────────────── 连接 ───────────────────────────────────
    def connect(self, retries: int = 5, retry_delay: float = 3.0) -> Dict[str, Any]:
        with self._lock:
            if self._state not in (CameraState.DISCONNECTED, CameraState.ERROR):
                raise CameraError(f"当前状态 {self._state} 不允许连接")
            try:
                from metavision_hal import DeviceDiscovery

                last_err = None
                for attempt in range(retries):
                    try:
                        self._device = DeviceDiscovery.open("")
                        if self._device is None:
                            raise RuntimeError("DeviceDiscovery 未找到相机")
                        break
                    except Exception as e:
                        last_err = e
                        if attempt < retries - 1:
                            time.sleep(retry_delay)
                else:
                    raise last_err

                # 读分辨率
                geo = self._device.get_i_geometry()
                self._width  = geo.get_width()
                self._height = geo.get_height()

                # 读序列号
                try:
                    hw_id = self._device.get_i_hw_identification()
                    self._serial = hw_id.get_serial() if hw_id else "unknown"
                except Exception:
                    self._serial = "unknown"

                # 加载所有 HAL Facilities
                self._load_facilities(self._device)

                self._state = CameraState.CONNECTED
                return self._build_info()

            except CameraError:
                raise
            except Exception as e:
                self._state = CameraState.ERROR
                raise CameraError(f"连接失败: {e}")

    def _load_facilities(self, device):
        # Python 绑定使用 get_i_*() 便捷方法
        mapping = [
            ("_f_biases",    "get_i_ll_biases"),
            ("_f_erc",       "get_i_erc_module"),
            ("_f_roi",       "get_i_roi"),
            ("_f_monitoring","get_i_monitoring"),
            ("_f_aflicker",  "get_i_antiflicker_module"),
            ("_f_actfilter", "get_i_event_rate_activity_filter_module"),
            ("_f_trig_in",   "get_i_trigger_in"),
            ("_f_trig_out",  "get_i_trigger_out"),
        ]
        for attr, method_name in mapping:
            facility = None
            try:
                getter = getattr(device, method_name, None)
                if getter is not None:
                    facility = getter()
            except Exception:
                pass
            setattr(self, attr, facility)

        # 读取 ERC 初始状态
        if self._f_erc:
            try:
                self._erc_enabled   = self._f_erc.is_enabled()
                self._erc_threshold = self._f_erc.get_cd_event_rate()
            except Exception:
                pass

    def _build_info(self) -> Dict[str, Any]:
        return {
            "serial":  self._serial,
            "width":   self._width,
            "height":  self._height,
            "facilities": {
                "biases":     self._f_biases     is not None,
                "erc":        self._f_erc        is not None,
                "roi":        self._f_roi        is not None,
                "monitoring": self._f_monitoring is not None,
                "antiflicker":self._f_aflicker   is not None,
                "actfilter":  self._f_actfilter  is not None,
                "trigger_in": self._f_trig_in    is not None,
                "trigger_out":self._f_trig_out   is not None,
            }
        }

    def disconnect(self):
        with self._lock:
            if self._state == CameraState.STREAMING:
                self._do_stop()
            # 中止正在进行的录制（不等待完整写入，临时文件随后清理）
            with self._rec_lock:
                q     = self._rec_queue
                io_thr = self._rec_io_thr
                tmp    = self._rec_tmpfile
                self._rec_queue   = None
                self._rec_io_thr  = None
                self._rec_path    = None
                self._rec_tmpfile = None
                self._rec_total   = 0
            self._device = None
            self._state  = CameraState.DISCONNECTED
            self._f_biases = self._f_erc = self._f_roi = None
            self._f_monitoring = self._f_aflicker = self._f_actfilter = None
            self._f_trig_in = self._f_trig_out = None

        # 锁外：停止 IO 线程，清理临时文件
        if q is not None:
            try: q.put_nowait(None)
            except Exception: pass
        if io_thr and io_thr.is_alive():
            io_thr.join(timeout=3)
        if tmp and os.path.exists(tmp):
            try: os.remove(tmp)
            except Exception: pass

    # ─────────────────────────────── 流控 ───────────────────────────────────
    def start(self):
        with self._lock:
            if self._state != CameraState.CONNECTED:
                raise CameraError(f"当前状态 {self._state} 不允许启动")
            with self._stats_lock:
                self._total_events    = 0
                self._event_rate_kevs = 0.0
                self._rate_window.clear()

            self._stop_flag.clear()
            self._capture_thr = threading.Thread(
                target=self._capture_loop, daemon=True)
            self._capture_thr.start()
            self._state = CameraState.STREAMING

    def stop(self):
        with self._lock:
            if self._state != CameraState.STREAMING:
                raise CameraError(f"当前状态 {self._state} 不允许停止")
            self._do_stop()
            self._state = CameraState.CONNECTED

    def _do_stop(self):
        self._stop_flag.set()
        if self._capture_thr and self._capture_thr.is_alive():
            self._capture_thr.join(timeout=6)
        self._capture_thr = None

    # ─────────────────────────────── 采集主循环 ─────────────────────────────
    def _capture_loop(self):
        """HAL 方式采集事件，通过注册的回调推送给渲染线程。

        流程：
          1. get_i_events_stream() → 启动 USB 事件流
          2. get_i_events_stream_decoder() → 解码原始字节
          3. get_i_event_cd_decoder() → 注册 CD 事件回调
          4. 循环 get_latest_raw_data() → decode() → 回调推送 + 统计 + 录制
        """
        import traceback

        try:
            evts_stream = self._device.get_i_events_stream()
            decoder     = self._device.get_i_events_stream_decoder()
            cd_decoder  = self._device.get_i_event_cd_decoder()

            accum = []
            _dbg_cb_count = [0]

            def on_cd(evs):
                # evs 是 HAL 内部缓冲区视图，回调返回后 HAL 可能立即复用该内存，
                # 必须先 copy 再传给渲染线程，否则可能读到被覆盖的数据
                evs_copy = evs.copy()
                for _cb in list(self._event_callbacks):
                    try:
                        _cb(evs_copy)
                    except Exception:
                        pass
                accum.append(evs_copy)
                _dbg_cb_count[0] += 1
                if _dbg_cb_count[0] <= 3:
                    print(f"[capture] on_cd #{_dbg_cb_count[0]}: {len(evs)} events")

            if cd_decoder:
                cd_decoder.add_event_buffer_callback(on_cd)

            evts_stream.start()
            print(f"[capture] HAL 事件流已启动 (Δt={self._delta_t_us}µs, "
                  f"cd_decoder={'OK' if cd_decoder else 'None'})")

            last_stats = time.monotonic()
            dt = self._delta_t_us / 1_000_000  # 秒

            while not self._stop_flag.is_set():
                raw = evts_stream.get_latest_raw_data()
                if raw is not None:
                    decoder.decode(raw)   # 同步解码，触发 on_cd 回调

                now = time.monotonic()
                if now - last_stats >= dt:
                    last_stats = now
                    if accum:
                        evs = np.concatenate(accum)
                        accum.clear()
                        if len(evs) > 0:
                            self._update_stats(evs)
                            # 录制：非阻塞投入 IO 队列，不再阻塞采集线程
                            q = self._rec_queue   # GIL 保证原子读
                            if q is not None:
                                batch = evs
                                # ① ROI 过滤
                                roi = self._rec_roi
                                if roi is not None:
                                    x0, y0, x1, y1 = roi
                                    m = ((batch['x'] >= x0) & (batch['x'] < x1) &
                                         (batch['y'] >= y0) & (batch['y'] < y1))
                                    batch = batch[m]
                                # ② 密度抽样（每 N 个取 1 个，保持时间顺序）
                                n = self._rec_keep_1in
                                if n > 1 and len(batch) > 0:
                                    batch = batch[::n]
                                if len(batch) > 0:
                                    try:
                                        q.put_nowait(batch)
                                        self._rec_total += len(batch)
                                    except _queue.Full:
                                        pass  # IO 跟不上：丢弃本帧，采集不卡顿
                else:
                    time.sleep(0.0005)  # 0.5ms，避免空转

            evts_stream.stop()
            print("[capture] 事件流已停止")

        except Exception as e:
            print(f"[capture] 异常退出: {e}")
            traceback.print_exc()
            with self._lock:
                self._state = CameraState.ERROR

    def _update_stats(self, evs: np.ndarray):
        n = len(evs)
        with self._stats_lock:
            self._total_events += n
            now = time.monotonic()
            self._rate_window.append((now, n))
            cutoff = now - 1.0
            self._rate_window = [(t, c) for t, c in self._rate_window if t >= cutoff]
            self._event_rate_kevs = sum(c for _, c in self._rate_window) / 1000.0

    # ─────────────────────────────── 参数设置 ───────────────────────────────
    def add_event_callback(self, cb):
        """注册原始事件回调，在 capture 线程中以 numpy 数组调用（勿在回调中做耗时操作）."""
        if cb not in self._event_callbacks:
            self._event_callbacks.append(cb)

    def remove_event_callback(self, cb):
        try:
            self._event_callbacks.remove(cb)
        except ValueError:
            pass

    def set_delta_t(self, delta_t_us: int):
        """调整事件切片时间窗口（仅在停止状态下生效，重启后有效）."""
        self._delta_t_us = max(1000, min(100_000, delta_t_us))

    # ── Biases ───────────────────────────────────────────────────────────────
    def get_all_biases(self) -> Dict[str, Any]:
        result = {}
        for name, meta in BIAS_DEFS.items():
            current = meta["default"]
            if self._f_biases:
                try:
                    current = self._f_biases.get(name)
                except Exception:
                    pass
            result[name] = {**meta, "current": current}
        return result

    def set_bias(self, name: str, value: int):
        if name not in BIAS_DEFS:
            raise CameraError(f"未知 bias: {name}")
        meta = BIAS_DEFS[name]
        if not (meta["min"] <= value <= meta["max"]):
            raise CameraError(f"{name} 超出范围 [{meta['min']}, {meta['max']}]")
        if not self._f_biases:
            raise CameraError("Bias facility 不可用")
        try:
            self._f_biases.set(name, value)
        except Exception as e:
            raise CameraError(f"设置 {name} 失败: {e}")

    # ── ERC ──────────────────────────────────────────────────────────────────
    def get_erc(self) -> Dict[str, Any]:
        return {
            "supported": self._f_erc is not None,
            "enabled":   self._erc_enabled,
            "threshold": self._erc_threshold,
        }

    def set_erc(self, enabled: bool, threshold: Optional[int] = None):
        if not self._f_erc:
            raise CameraError("ERC 不支持")
        try:
            if threshold is not None:
                self._f_erc.set_cd_event_rate(threshold)
                self._erc_threshold = threshold
            self._f_erc.enable(enabled)
            self._erc_enabled = enabled
        except Exception as e:
            raise CameraError(f"ERC 设置失败: {e}")

    # ── ROI ──────────────────────────────────────────────────────────────────
    def get_roi(self) -> Dict[str, Any]:
        return {"supported": self._f_roi is not None, **self._roi_cfg}

    def set_roi(self, enabled: bool, x=0, y=0, width=0, height=0):
        if not self._f_roi:
            raise CameraError("ROI 不支持")
        try:
            if not enabled:
                self._f_roi.enable(False)
                self._roi_cfg["enabled"] = False
            else:
                from metavision_hal import I_ROI
                win = I_ROI.Window()
                win.x, win.y, win.width, win.height = x, y, width, height
                self._f_roi.set_windows([win])
                self._f_roi.enable(True)
                self._roi_cfg = {"enabled": True, "x": x, "y": y,
                                 "width": width, "height": height}
        except Exception as e:
            raise CameraError(f"ROI 设置失败: {e}")

    # ── Monitoring ───────────────────────────────────────────────────────────
    def get_monitoring(self) -> Dict[str, Any]:
        result = {"supported": self._f_monitoring is not None,
                  "temperature": None, "illuminance": None}
        if not self._f_monitoring:
            return result
        try:
            result["temperature"] = self._f_monitoring.get_temperature()
        except Exception:
            pass
        try:
            result["illuminance"] = self._f_monitoring.get_illuminance()
        except Exception:
            pass
        return result

    # ── Anti-Flicker ─────────────────────────────────────────────────────────
    def get_antiflicker(self) -> Dict[str, Any]:
        return {"supported": self._f_aflicker is not None, **self._aflicker_cfg}

    def set_antiflicker(self, enabled: bool, freq_low: int = 50, freq_high: int = 70):
        if not self._f_aflicker:
            raise CameraError("Anti-flicker 不支持")
        try:
            if enabled:
                self._f_aflicker.set_frequency_band(freq_low, freq_high)
            self._f_aflicker.enable(enabled)
            self._aflicker_cfg = {"enabled": enabled,
                                  "freq_low": freq_low, "freq_high": freq_high}
        except Exception as e:
            raise CameraError(f"Anti-flicker 设置失败: {e}")

    # ── Activity Filter (Hardware) ───────────────────────────────────────────
    def get_activity_filter(self) -> Dict[str, Any]:
        return {"supported": self._f_actfilter is not None}

    def set_activity_filter(self, enabled: bool,
                            lower: int = 100, upper: int = 10_000_000):
        if not self._f_actfilter:
            raise CameraError("Activity Filter 不支持")
        try:
            if enabled:
                self._f_actfilter.set_thresholds(lower, upper)
            self._f_actfilter.enable(enabled)
        except Exception as e:
            raise CameraError(f"Activity Filter 设置失败: {e}")

    # ── Trigger In / Out ─────────────────────────────────────────────────────
    def get_triggers(self) -> Dict[str, Any]:
        return {
            "trigger_in":  self._f_trig_in  is not None,
            "trigger_out": self._f_trig_out is not None,
        }

    def set_trigger_in(self, channel: int, enabled: bool):
        if not self._f_trig_in:
            raise CameraError("Trigger In 不支持")
        try:
            if enabled:
                self._f_trig_in.enable(channel)
            else:
                self._f_trig_in.disable(channel)
        except Exception as e:
            raise CameraError(f"Trigger In 设置失败: {e}")

    def set_trigger_out(self, enabled: bool, period_us: int = 1_000_000,
                        duty: float = 0.5):
        if not self._f_trig_out:
            raise CameraError("Trigger Out 不支持")
        try:
            if enabled:
                self._f_trig_out.set_period(period_us)
                self._f_trig_out.set_duty_cycle(duty)
                self._f_trig_out.enable(True)
            else:
                self._f_trig_out.enable(False)
        except Exception as e:
            raise CameraError(f"Trigger Out 设置失败: {e}")

    # ── Recording filters ────────────────────────────────────────────────────
    def set_rec_roi(self, enabled: bool,
                    x0: int = 0, y0: int = 0,
                    x1: int = 1280, y1: int = 720):
        """录制 ROI 过滤：仅保存指定矩形内的事件，大幅缩小文件体积。"""
        self._rec_roi = (int(x0), int(y0), int(x1), int(y1)) if enabled else None

    def set_rec_decimation(self, keep_1in: int):
        """录制密度抽样：每 N 个事件保留 1 个（1=全量，2=1/2，4=1/4…）。"""
        self._rec_keep_1in = max(1, int(keep_1in))
        self._rec_skip_n   = 0

    # ── Recording ────────────────────────────────────────────────────────────
    def start_recording(self, path: str):
        with self._rec_lock:
            if self._rec_queue is not None:
                raise CameraError("录制已在进行中")
            self._rec_path    = path
            self._rec_tmpfile = path + ".tmp"
            self._rec_total   = 0
            if os.path.exists(self._rec_tmpfile):
                os.remove(self._rec_tmpfile)
            self._rec_queue  = _queue.Queue(maxsize=_REC_QUEUE_MAX)
            self._rec_io_thr = threading.Thread(
                target=self._rec_io_loop, daemon=True, name="RecIO")
            self._rec_io_thr.start()

    def _rec_io_loop(self):
        """IO 线程：从队列读取事件批，以原始字节追加写入临时文件。
        采集线程不再阻塞于磁盘 I/O，彻底消除录制导致的采集滞后。"""
        q       = self._rec_queue    # 本地引用，stop_recording 置 None 后仍有效
        tmpfile = self._rec_tmpfile
        try:
            with open(tmpfile, 'ab') as f:
                while True:
                    try:
                        item = q.get(timeout=0.5)
                    except _queue.Empty:
                        continue
                    if item is None:   # 哨兵：退出信号
                        break
                    f.write(item.tobytes())
        except Exception as e:
            print(f"[RecIO] 写入失败: {e}")

    def _binary_to_npy(self, binary_path: str, npy_path: str):
        """将原始二进制事件流（结构体逐字节堆叠）转换为 .npy，
        流式拷贝：O(1) 内存，无需将整个文件读入 RAM。"""
        file_size = os.path.getsize(binary_path)
        n = file_size // _EVT_DTYPE.itemsize
        # 手工写 NumPy 1.0 格式头（避免为 n 个事件分配内存）
        descr = _EVT_DTYPE.descr   # [('x','<u2'), ...]
        hdr   = (f"{{'descr': {descr!r}, "
                 f"'fortran_order': False, "
                 f"'shape': ({n},), }}")
        # 头部块需是 64 字节的整数倍（magic 6 + version 2 + hlen 2 = 10 字节前缀）
        pad = (64 - (10 + len(hdr) + 1) % 64) % 64
        hdr += ' ' * pad + '\n'
        with open(npy_path, 'wb') as fout:
            fout.write(b'\x93NUMPY\x01\x00')
            fout.write(_struct.pack('<H', len(hdr)))
            fout.write(hdr.encode('latin1'))
            with open(binary_path, 'rb') as fin:
                shutil.copyfileobj(fin, fout, 4 * 1024 * 1024)

    def stop_recording(self) -> Dict[str, Any]:
        with self._rec_lock:
            if self._rec_queue is None:
                raise CameraError("未在录制")
            q      = self._rec_queue
            io_thr = self._rec_io_thr
            path   = self._rec_path
            tmp    = self._rec_tmpfile
            total  = self._rec_total
            self._rec_queue   = None
            self._rec_io_thr  = None
            self._rec_path    = None
            self._rec_tmpfile = None
            self._rec_total   = 0

        # 锁外：发送哨兵，等待 IO 线程将剩余数据写完
        q.put(None)
        if io_thr:
            io_thr.join(timeout=30)

        try:
            if total == 0 or not os.path.exists(tmp):
                np.save(path, np.array([], dtype=_EVT_DTYPE))
            else:
                self._binary_to_npy(tmp, path)
            return {"path": path, "events": total}
        except Exception as e:
            raise CameraError(f"保存录制失败: {e}")
        finally:
            if tmp and os.path.exists(tmp):
                try: os.remove(tmp)
                except Exception: pass

    @property
    def is_recording(self) -> bool:
        return self._rec_queue is not None

    # ── 综合状态 ─────────────────────────────────────────────────────────────
    def get_status(self) -> Dict[str, Any]:
        with self._stats_lock:
            rate  = round(self._event_rate_kevs, 1)
            total = self._total_events
        return {
            "state":           self._state,
            "serial":          self._serial,
            "width":           self._width,
            "height":          self._height,
            "event_rate_kevs": rate,
            "total_events":    total,
            "delta_t_us":      self._delta_t_us,
            "is_recording":    self.is_recording,
            "facilities":      self._build_info().get("facilities", {}) if self._device else {},
        }
