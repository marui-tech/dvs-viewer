"""
DVS Camera — 渲染线程 + OpenGL 超低延迟可视化

线程架构:
  Capture Thread  → push_events() → _pending (原子赋值, 无锁)
                                          ↓ threading.Event.set()
  Render Thread   ← event.wait(4ms) ←───┘ → paintGL → swapBuffers
  Main Thread     → Qt UI 控件（完全不涉及 OpenGL）

延迟路径:
  on_cd 回调 → _pending 赋值(~0.01ms) → Event唤醒渲染线程(~0.1ms)
  → GPU 渲染(~0.5ms) → 显示 ≈ 1ms 以内
"""
import sys
import os
import ctypes
import threading
import time
from typing import Optional

import numpy as np

# ── OpenEB 路径 ────────────────────────────────────────────────────────────────
sys.path.insert(0, r"C:\openeb\build\py3\Release")
for _d in [r"C:\openeb\build\bin\Release", r"C:\vcpkg\installed\x64-windows\bin"]:
    try:
        os.add_dll_directory(_d)
    except Exception:
        pass
os.environ.setdefault("MV_HAL_PLUGIN_PATH", r"C:\openeb\build\lib\metavision\hal\plugins")
os.environ.setdefault("HDF5_PLUGIN_PATH",   r"C:\openeb\build\lib\hdf5\plugin")

from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QSlider, QGroupBox,
    QSplitter, QScrollArea, QSizePolicy, QSpinBox, QFrame,
    QCheckBox, QLineEdit, QDoubleSpinBox, QFormLayout, QTabWidget,
    QLayout,
)
from PyQt5.QtCore  import Qt, QTimer, QThread, pyqtSignal
from PyQt5.QtGui   import QOpenGLContext, QWindow, QPalette, QColor, QSurfaceFormat

import OpenGL
OpenGL.ERROR_CHECKING = False   # 关闭每次 GL 调用后的错误验证，GL 调用速度 2-3x
OpenGL.ERROR_ON_COPY  = False   # 允许 PyOpenGL 内部拷贝，不抛出警告
from OpenGL import GL
from OpenGL.GL import shaders

# 事件结构体步长：x(u16,2)+y(u16,2)+p(i16,2)+t(i64,8) = 14 字节
# 直接上传原始结构体内存到 GPU，无需中间 float32 拷贝
_EVT_DTYPE  = np.dtype([('x','<u2'),('y','<u2'),('p','<i2'),('t','<i8')])
_EVT_STRIDE = _EVT_DTYPE.itemsize  # 14

sys.path.insert(0, os.path.dirname(__file__))
from camera_manager import CameraManager, CameraState, BIAS_DEFS

# ═══════════════════════════════════════════════════════════════════════════════
#  GLSL Shaders
# ═══════════════════════════════════════════════════════════════════════════════
_VERT_EVENTS = """
#version 330 core
// 直接接受原始事件结构体布局（uint16 x,y + int16 p），GPU 自动转 float，零 CPU 拷贝
layout(location=0) in vec2  a_pos;   // glVertexAttribPointer GL_UNSIGNED_SHORT → float
layout(location=1) in float a_pol;   // glVertexAttribPointer GL_SHORT          → float
uniform vec2 u_sensor;
uniform vec2 u_vp;
out vec4 v_color;
void main() {
    float s      = min(u_vp.x / u_sensor.x, u_vp.y / u_sensor.y);
    vec2  offset = (u_vp - u_sensor * s) * 0.5;
    vec2  disp   = a_pos * s + offset;
    vec2  ndc    = disp / u_vp * 2.0 - 1.0;
    ndc.y = -ndc.y;
    gl_Position  = vec4(ndc, 0.0, 1.0);
    gl_PointSize = 1.0;
    v_color = (a_pol > 0.5)
        ? vec4(0.0,  0.9,  0.3,  1.0)
        : vec4(0.85, 0.12, 0.08, 1.0);
}
"""
_FRAG_EVENTS = """
#version 330 core
in  vec4 v_color;
out vec4 fragColor;
void main() { fragColor = v_color; }
"""
_VERT_QUAD = """
#version 330 core
out vec2 v_uv;
void main() {
    const vec2 P[4] = vec2[4](vec2(-1,-1),vec2(1,-1),vec2(-1,1),vec2(1,1));
    const vec2 U[4] = vec2[4](vec2(0,0),  vec2(1,0), vec2(0,1), vec2(1,1));
    gl_Position = vec4(P[gl_VertexID], 0.0, 1.0);
    v_uv = U[gl_VertexID];
}
"""
_FRAG_DECAY = """
#version 330 core
in  vec2 v_uv;
uniform sampler2D u_tex;
uniform float     u_decay;
out vec4 fragColor;
void main() { fragColor = texture(u_tex, v_uv) * u_decay; }
"""
_VERT_BLIT = """
#version 330 core
out vec2 v_uv;
void main() {
    // FBO 已是显示尺寸，1:1 拷贝，无缩放
    const vec2 P[4] = vec2[4](vec2(-1,-1),vec2(1,-1),vec2(-1,1),vec2(1,1));
    const vec2 U[4] = vec2[4](vec2(0,0),  vec2(1,0), vec2(0,1), vec2(1,1));
    gl_Position = vec4(P[gl_VertexID], 0.0, 1.0);
    v_uv = U[gl_VertexID];
}
"""
_FRAG_BLIT = """
#version 330 core
in  vec2 v_uv;
uniform sampler2D u_tex;
out vec4 fragColor;
void main() { fragColor = texture(u_tex, v_uv); }
"""


def _compile(vs: str, fs: str) -> int:
    return shaders.compileProgram(
        shaders.compileShader(vs, GL.GL_VERTEX_SHADER),
        shaders.compileShader(fs, GL.GL_FRAGMENT_SHADER),
    )


# ═══════════════════════════════════════════════════════════════════════════════
#  DVSRenderWindow — 仅作 OpenGL 表面，不做任何渲染逻辑
# ═══════════════════════════════════════════════════════════════════════════════
class DVSRenderWindow(QWindow):
    """Qt 窗口表面，在主线程创建，渲染线程通过它交换缓冲区."""
    def __init__(self):
        super().__init__()
        fmt = QSurfaceFormat()
        fmt.setVersion(3, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        fmt.setSwapInterval(0)          # 关闭垂直同步
        self.setFormat(fmt)
        self.setSurfaceType(QWindow.OpenGLSurface)
        self.setTitle("DVS Stream")


# ═══════════════════════════════════════════════════════════════════════════════
#  RenderThread — 独立线程，拥有全部 OpenGL 对象
# ═══════════════════════════════════════════════════════════════════════════════
class RenderThread(QThread):
    """
    完全独立的渲染线程：
    - 自建 QOpenGLContext，与主线程 Qt 事件循环无关
    - 用 threading.Event 实现"有事件立即唤醒，无事件 4ms 超时做衰减"
    - push_events() 可从任意线程安全调用（GIL 原子赋值）
    """
    fps_updated  = pyqtSignal(float)
    perf_updated = pyqtSignal(dict)   # 每秒推送一次详细性能数据

    def __init__(self, window: DVSRenderWindow, sensor_w: int, sensor_h: int):
        super().__init__()
        self._window    = window
        self.sensor_w   = sensor_w
        self.sensor_h   = sensor_h

        # 事件累积队列：on_cd 每个 USB 包都 append，渲染线程一次性消费全部
        # 修复原有 _pending 覆盖赋值导致的大量事件丢弃（IMX636 高速时丢弃率 95%+）
        self._pending_lock = threading.Lock()
        self._pending_list: list = []
        self._stopping  = False
        self._new_evt   = threading.Event()   # 新事件到达通知

        # 可视化参数（主线程写，渲染线程读，Python int/float 赋值原子）
        self.decay_factor = 0.72
        self.viz_mode     = "event_frame"

        # 直接上传原始事件结构体，无需 float32 中间缓冲
        self._MAX_EVS   = 10_000_000

        # GL 对象（在 run() 中初始化，仅渲染线程访问）
        self._ctx       = None
        self._prog_evt  = None
        self._prog_dec  = None
        self._prog_blit = None
        self._vao_evt   = None
        self._vbo_evt   = None
        self._vao_quad  = None
        self._fbos      = [None, None]
        self._textures  = [None, None]
        self._read_idx  = 0
        self._fbo_size  = (0, 0)   # FBO 当前尺寸，首帧按显示尺寸创建

        # 缓存 uniform 位置（初始化时查一次，避免每帧字符串哈希查找）
        self._uloc_dec_tex    = -1
        self._uloc_dec_decay  = -1
        self._uloc_evt_sensor = -1
        self._uloc_evt_vp     = -1
        self._uloc_blit_tex   = -1
        self._last_vp = (-1, -1)   # 上次 u_vp 值，只在 resize 时才更新

        # GL 初始化完成标志（主线程可等待）
        self._gl_ready  = threading.Event()

        # FPS & 性能计时
        self._frames      = 0
        self._t0          = time.monotonic()
        # 滑动窗口累积量（每秒归零）
        self._sum_paint_ms = 0.0   # GPU渲染+swap耗时之和
        self._sum_wait_ms  = 0.0   # event.wait 等待耗时之和
        self._sum_evs      = 0     # 处理事件总数
        self._sum_vbo_kb   = 0.0   # VBO上传量之和（KB）
        self._sum_batches  = 0     # 每帧累积的 USB 包数（修复前固定为 1，修复后反映真实包数）

    # ── 公共 API（线程安全）────────────────────────────────────────────────────
    def push_events(self, evs: np.ndarray):
        """从 capture 线程直接调用。
        累积所有 USB 包到列表，渲染帧一次性消费全部，消除原有 95%+ 事件丢弃。"""
        with self._pending_lock:
            self._pending_list.append(evs)
        self._new_evt.set()

    def clear_frame(self):
        empty = np.array([], dtype=[('x','<u2'),('y','<u2'),('p','<i2'),('t','<i8')])
        with self._pending_lock:
            self._pending_list = [empty]
        self._new_evt.set()

    def stop_render(self):
        self._stopping = True
        self._new_evt.set()          # 唤醒以便退出 wait

    def wait_gl_ready(self, timeout: float = 5.0) -> bool:
        return self._gl_ready.wait(timeout)

    # ── 渲染线程主体 ───────────────────────────────────────────────────────────
    def run(self):
        # 主线程已在 processEvents() 后启动此线程，窗口应已 expose；
        # 给系统 50ms 缓冲以保证 native window handle 就绪
        time.sleep(0.05)

        # 在渲染线程创建并绑定 OpenGL 上下文
        self._ctx = QOpenGLContext()
        self._ctx.setFormat(self._window.requestedFormat())
        if not self._ctx.create():
            print("[RenderThread] 无法创建 OpenGL 上下文")
            return
        if not self._ctx.makeCurrent(self._window):
            print("[RenderThread] makeCurrent 失败")
            return

        self._init_gl()
        self._gl_ready.set()
        print("[RenderThread] GL 初始化完成，进入渲染循环")

        # ── 渲染循环 ──────────────────────────────────────────────────────────
        # 设计：有事件 → 立即渲染（不阻塞）；无事件 → sleep 1ms 做衰减帧
        # 突破原有 wait(4ms)+GPU(3ms) = ~333fps 上限，有事件时可达 1000fps+
        while not self._stopping:
            # ① 非阻塞取队列：先检查是否有积累的事件
            _t_wait = time.monotonic()
            with self._pending_lock:
                pending_list, self._pending_list = self._pending_list, []

            if not pending_list:
                # 无新事件：等最多 1ms（timeBeginPeriod(1) 保证精度），做衰减帧
                self._new_evt.wait(timeout=0.001)
                self._new_evt.clear()
                with self._pending_lock:
                    pending_list, self._pending_list = self._pending_list, []
            else:
                self._new_evt.clear()

            wait_ms = (time.monotonic() - _t_wait) * 1000.0

            if self._stopping:
                break

            # 必须用物理像素：QWindow.width/height 返回逻辑像素
            dpr = self._window.devicePixelRatio()
            ww  = int(self._window.width()  * dpr)
            wh  = int(self._window.height() * dpr)

            # 合并所有待渲染包；1 个包时直接使用，避免 concatenate 开销
            n_batches = len(pending_list)
            if n_batches == 0:
                evs = None
            elif n_batches == 1:
                evs = pending_list[0]          # 零拷贝，直接引用
            else:
                evs = np.concatenate(pending_list)

            # ② 计时：GPU渲染 + buffer swap
            _t_paint = time.monotonic()
            n_evs = self._paint(evs, ww, wh)   # _paint 返回实际渲染事件数
            self._ctx.swapBuffers(self._window)
            paint_ms = (time.monotonic() - _t_paint) * 1000.0

            # ③ 累积统计
            self._frames       += 1
            self._sum_paint_ms += paint_ms
            self._sum_wait_ms  += wait_ms
            self._sum_evs      += n_evs
            self._sum_vbo_kb   += n_evs * 12 / 1024.0   # float32 x,y,p = 12 bytes/ev
            self._sum_batches  += n_batches

            # ④ 每秒发射一次汇总
            now = time.monotonic()
            if now - self._t0 >= 1.0:
                elapsed = now - self._t0
                fps = self._frames / elapsed
                self.fps_updated.emit(fps)
                self.perf_updated.emit({
                    "fps":              fps,
                    "paint_ms_avg":     self._sum_paint_ms / max(self._frames, 1),
                    "wait_ms_avg":      self._sum_wait_ms  / max(self._frames, 1),
                    "evs_per_frame":    self._sum_evs      / max(self._frames, 1),
                    "vbo_kb_per_s":     self._sum_vbo_kb   / elapsed,
                    "batches_per_frame":self._sum_batches  / max(self._frames, 1),
                    "vsync_off":    True,
                    "vbo_orphan":   True,
                    "timer_1ms":    True,
                })
                self._frames = self._sum_paint_ms = self._sum_wait_ms = 0
                self._sum_evs = 0; self._sum_vbo_kb = 0.0; self._sum_batches = 0
                self._t0 = now

        self._ctx.doneCurrent()
        print("[RenderThread] 渲染线程已退出")

    # ── OpenGL 初始化（仅在渲染线程调用）──────────────────────────────────────
    def _init_gl(self):
        GL.glClearColor(0.03, 0.04, 0.06, 1.0)
        GL.glEnable(GL.GL_PROGRAM_POINT_SIZE)

        self._prog_evt  = _compile(_VERT_EVENTS, _FRAG_EVENTS)
        self._prog_dec  = _compile(_VERT_QUAD,   _FRAG_DECAY)
        self._prog_blit = _compile(_VERT_BLIT,   _FRAG_BLIT)

        # ── 缓存 uniform 位置（每帧调 glGetUniformLocation 有字符串哈希开销）──
        self._uloc_dec_decay  = GL.glGetUniformLocation(self._prog_dec,  "u_decay")
        self._uloc_evt_sensor = GL.glGetUniformLocation(self._prog_evt,  "u_sensor")
        self._uloc_evt_vp     = GL.glGetUniformLocation(self._prog_evt,  "u_vp")

        # ── 常量 uniform 只设一次（u_tex 永远绑定纹理单元 0，u_sensor 传感器分辨率固定）
        GL.glUseProgram(self._prog_dec)
        GL.glUniform1i(GL.glGetUniformLocation(self._prog_dec,  "u_tex"), 0)
        GL.glUseProgram(self._prog_blit)
        GL.glUniform1i(GL.glGetUniformLocation(self._prog_blit, "u_tex"), 0)
        GL.glUseProgram(self._prog_evt)
        GL.glUniform2f(self._uloc_evt_sensor,
                       float(self.sensor_w), float(self.sensor_h))
        GL.glUseProgram(0)

        # ── VAO：直接使用原始事件结构体布局，无需 CPU 拷贝到 float32 缓冲 ──────
        # 事件结构: x(u16,off=0) y(u16,off=2) p(i16,off=4) t(i64,off=6)，stride=14
        self._vao_evt = int(GL.glGenVertexArrays(1))
        self._vbo_evt = int(GL.glGenBuffers(1))
        GL.glBindVertexArray(self._vao_evt)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo_evt)
        GL.glBufferData(GL.GL_ARRAY_BUFFER,
                        self._MAX_EVS * _EVT_STRIDE, None, GL.GL_STREAM_DRAW)
        GL.glEnableVertexAttribArray(0)
        # a_pos: 2×uint16，GPU 自动转 float（0→0.0，65535→65535.0）
        GL.glVertexAttribPointer(0, 2, GL.GL_UNSIGNED_SHORT, GL.GL_FALSE,
                                 _EVT_STRIDE, ctypes.c_void_p(0))
        GL.glEnableVertexAttribArray(1)
        # a_pol: 1×int16，GPU 自动转 float（0→0.0，1→1.0）
        GL.glVertexAttribPointer(1, 1, GL.GL_SHORT, GL.GL_FALSE,
                                 _EVT_STRIDE, ctypes.c_void_p(4))
        GL.glBindVertexArray(0)

        self._vao_quad = int(GL.glGenVertexArrays(1))
        # FBO 在首帧 _paint 中按实际显示尺寸创建（不再固定传感器分辨率）

    def _create_fbos(self, w: int, h: int):
        """创建/重建 ping-pong FBO，尺寸为显示区物理像素（不再是传感器分辨率）."""
        if self._fbos[0] is not None:
            GL.glDeleteFramebuffers(2, self._fbos)
            GL.glDeleteTextures(2, self._textures)
        self._fbos     = [int(x) for x in GL.glGenFramebuffers(2)]
        self._textures = [int(x) for x in GL.glGenTextures(2)]
        for i in range(2):
            GL.glBindTexture(GL.GL_TEXTURE_2D, self._textures[i])
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, GL.GL_RGB8,
                            w, h, 0, GL.GL_RGB, GL.GL_UNSIGNED_BYTE, None)
            # FBO 已是显示尺寸，blit 为 1:1，NEAREST 即可保持像素锐利
            GL.glTexParameteri(GL.GL_TEXTURE_2D,
                               GL.GL_TEXTURE_MIN_FILTER, GL.GL_NEAREST)
            GL.glTexParameteri(GL.GL_TEXTURE_2D,
                               GL.GL_TEXTURE_MAG_FILTER, GL.GL_NEAREST)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._fbos[i])
            GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER,
                                      GL.GL_COLOR_ATTACHMENT0,
                                      GL.GL_TEXTURE_2D, self._textures[i], 0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        self._fbo_size = (w, h)

    # ── 单帧渲染（仅在渲染线程调用）返回本帧实际渲染事件数 ─────────────────────
    def _paint(self, evs, vw: int, vh: int) -> int:
        if vw <= 0 or vh <= 0:
            return 0

        # FBO 跟随显示区尺寸：首次或窗口 resize 时重建
        if (vw, vh) != self._fbo_size:
            self._create_fbos(vw, vh)

        n = 0
        if evs is not None and len(evs) > 0:
            n = min(len(evs), self._MAX_EVS)
            if self.viz_mode == "on_only":
                evs = evs[evs['p'] == 1];  n = min(len(evs), self._MAX_EVS)
            elif self.viz_mode == "off_only":
                evs = evs[evs['p'] == 0];  n = min(len(evs), self._MAX_EVS)

            if n > 0:
                # 直接上传原始事件结构体（14 字节/事件），GPU 在着色器中完成类型转换
                # 省去原有 3 次 numpy 字段拷贝到 float32 缓冲的开销
                nbytes = n * _EVT_STRIDE
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self._vbo_evt)
                GL.glBufferData(GL.GL_ARRAY_BUFFER, nbytes, None, GL.GL_STREAM_DRAW)
                GL.glBufferSubData(GL.GL_ARRAY_BUFFER, 0, nbytes, evs[:n])
                GL.glBindBuffer(GL.GL_ARRAY_BUFFER, 0)

        ri = self._read_idx
        wi = 1 - ri

        # Decay pass → FBO_write
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self._fbos[wi])
        GL.glViewport(0, 0, vw, vh)
        GL.glUseProgram(self._prog_dec)
        GL.glActiveTexture(GL.GL_TEXTURE0)
        GL.glBindTexture(GL.GL_TEXTURE_2D, self._textures[ri])
        GL.glUniform1f(self._uloc_dec_decay,
                       1.0 if self.viz_mode == "accumulated" else self.decay_factor)
        GL.glBindVertexArray(self._vao_quad)
        GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)

        # Event pass → FBO_write（letterbox 映射在着色器内完成）
        if n > 0:
            GL.glUseProgram(self._prog_evt)
            # u_vp 只在窗口尺寸变化时更新，避免每帧 glUniform2f 调用
            if (vw, vh) != self._last_vp:
                GL.glUniform2f(self._uloc_evt_vp, float(vw), float(vh))
                self._last_vp = (vw, vh)
            GL.glBindVertexArray(self._vao_evt)
            GL.glDrawArrays(GL.GL_POINTS, 0, n)
            GL.glBindVertexArray(0)

        self._read_idx = wi

        # Blit → 窗口（glBlitFramebuffer 硬件路径，比 shader blit 少 5 次 GL 调用）
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, self._fbos[wi])
        GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, 0)
        GL.glBlitFramebuffer(0, 0, vw, vh, 0, 0, vw, vh,
                             GL.GL_COLOR_BUFFER_BIT, GL.GL_NEAREST)
        return n


# ═══════════════════════════════════════════════════════════════════════════════
#  UI 工具：可折叠区块 + 可折叠列面板
# ═══════════════════════════════════════════════════════════════════════════════
def _muted(text: str) -> QLabel:
    lb = QLabel(text)
    lb.setStyleSheet("font-size:10px;color:#8b949e;")
    lb.setWordWrap(True)
    return lb


# ─── CollapsibleSection ────────────────────────────────────────────────────────
class CollapsibleSection(QWidget):
    """
    可折叠区块：标题栏显示中英双语，点击展开/折叠内容区。
    Collapsible section with bilingual header (ZH / EN).
    """
    _HDR = ("QPushButton{text-align:left;padding:5px 8px;"
            "background:#21262d;border:none;border-top:1px solid #30363d;"
            "color:#e6edf3;font-size:11px;font-weight:600;}"
            "QPushButton:hover{background:#282e36;}")

    def __init__(self, zh: str, en: str, expanded: bool = True, parent=None):
        super().__init__(parent)
        self._zh = zh
        self._en = en
        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(0)

        self._btn = QPushButton()
        self._btn.setCheckable(True)
        self._btn.setChecked(expanded)
        self._btn.setFlat(True)
        self._btn.setStyleSheet(self._HDR)
        self._btn.clicked.connect(self._toggle)
        vb.addWidget(self._btn)

        self._body = QWidget()
        self._body.setStyleSheet("background:#161b22;")
        self._bvb = QVBoxLayout(self._body)
        self._bvb.setContentsMargins(8, 6, 8, 8)
        self._bvb.setSpacing(5)
        self._body.setVisible(expanded)
        vb.addWidget(self._body)

        self._update_header()

    def _update_header(self):
        arrow = "▾" if self._btn.isChecked() else "▸"
        self._btn.setText(f"  {arrow}  {self._zh}  /  {self._en}")

    def _toggle(self, checked: bool):
        self._body.setVisible(checked)
        self._update_header()

    def add(self, item) -> None:
        """添加 QWidget 或 QLayout 到内容区 / Add widget or layout to body."""
        if isinstance(item, QWidget):
            self._bvb.addWidget(item)
        else:
            self._bvb.addLayout(item)


# ─── ColumnPanel ──────────────────────────────────────────────────────────────
class ColumnPanel(QWidget):
    """
    可折叠列面板：列头可点击展开/折叠整列。
    Collapsible column panel — click header to collapse/expand.
    折叠后宽度收缩为 22px，只显示展开箭头。
    """
    _COL_HDR = ("QPushButton{text-align:left;padding:5px 8px;"
                "background:#0d1117;border:none;"
                "border-right:1px solid #30363d;border-bottom:2px solid #1f6feb;"
                "color:#58a6ff;font-size:11px;font-weight:700;}"
                "QPushButton:hover{background:#161b22;}")

    def __init__(self, zh: str, en: str, width: int = 245, parent=None):
        super().__init__(parent)
        self._zh = zh
        self._en = en
        self._full_w = width
        self._expanded = True

        vb = QVBoxLayout(self)
        vb.setContentsMargins(0, 0, 0, 0)
        vb.setSpacing(0)

        self._col_btn = QPushButton()
        self._col_btn.setFlat(True)
        self._col_btn.setFixedHeight(28)
        self._col_btn.setStyleSheet(self._COL_HDR)
        self._col_btn.clicked.connect(self._toggle_col)
        vb.addWidget(self._col_btn)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._scroll.setStyleSheet(
            "QScrollArea{border:none;border-right:1px solid #30363d;background:#0d1117;}"
            "QScrollBar:vertical{width:6px;background:#0d1117;}"
            "QScrollBar::handle:vertical{background:#30363d;border-radius:3px;}"
        )
        self._inner = QWidget()
        self._inner.setStyleSheet("background:#0d1117;")
        self._ivb = QVBoxLayout(self._inner)
        self._ivb.setContentsMargins(0, 0, 0, 4)
        self._ivb.setSpacing(0)
        self._ivb.setAlignment(Qt.AlignTop)
        self._scroll.setWidget(self._inner)
        vb.addWidget(self._scroll, 1)

        self.setFixedWidth(width)
        self._update_col_btn()

    def _update_col_btn(self):
        if self._expanded:
            self._col_btn.setText(f"  ◀  {self._zh}  /  {self._en}")
            self._col_btn.setToolTip("")
        else:
            self._col_btn.setText("▶")
            self._col_btn.setToolTip(f"{self._zh} / {self._en}")

    def _toggle_col(self):
        self._expanded = not self._expanded
        self._scroll.setVisible(self._expanded)
        self.setFixedWidth(self._full_w if self._expanded else 22)
        self._update_col_btn()

    def section(self, zh: str, en: str, expanded: bool = True) -> CollapsibleSection:
        """创建并添加一个区块，返回区块对象 / Create & add section, return it."""
        s = CollapsibleSection(zh, en, expanded)
        self._ivb.addWidget(s)
        return s


# ═══════════════════════════════════════════════════════════════════════════════
#  主窗口 MainWindow
# ═══════════════════════════════════════════════════════════════════════════════
class MainWindow(QMainWindow):

    _VIZ_DESC = {
        "event_frame":  "事件帧 Event Frame — ON=绿/green, OFF=红/red, 衰减/decay",
        "time_surface": "时间面 Time Surface — 像素颜色=事件年龄/pixel age heatmap",
        "on_only":      "仅ON Only ON — 只显示亮度增加事件/brightness-increase events",
        "off_only":     "仅OFF Only OFF — 只显示亮度降低事件/brightness-decrease events",
        "accumulated":  "累积 Accumulated — 不衰减,按清空重置/no decay, clear to reset",
    }

    def __init__(self):
        super().__init__()
        self.setWindowTitle("DVS Camera — Render Thread OpenGL")
        self.resize(1520, 860)

        self.camera       = CameraManager()
        self._render_win: Optional[DVSRenderWindow] = None
        self._render_thr: Optional[RenderThread]    = None
        self._container:  Optional[QWidget]         = None

        self._build_ui()
        self._update_buttons()

        self._poll = QTimer(self)
        self._poll.setInterval(500)
        self._poll.timeout.connect(self._on_poll)
        self._poll.start()

    # ── 构建多列界面 ───────────────────────────────────────────────────────────
    def _build_ui(self):
        root = QWidget(self)
        self.setCentralWidget(root)
        hl = QHBoxLayout(root)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(0)
        self._main_hl = hl

        # ── Column 1: 相机 / Camera ──────────────────────────────────────────
        col1 = ColumnPanel("相机", "Camera", 215)

        # 相机控制 Camera Control
        sc = col1.section("相机控制", "Camera Control")
        self._btn_conn  = QPushButton("⚡ 连接相机  Connect")
        self._btn_start = QPushButton("▶ 开始采集  Start")
        self._btn_stop  = QPushButton("■ 停止采集  Stop")
        self._btn_disc  = QPushButton("⏏ 断开  Disconnect")
        self._btn_conn.clicked.connect(self._on_connect)
        self._btn_start.clicked.connect(self._on_start)
        self._btn_stop.clicked.connect(self._on_stop)
        self._btn_disc.clicked.connect(self._on_disconnect)
        for b in (self._btn_conn, self._btn_start, self._btn_stop, self._btn_disc):
            sc.add(b)

        # 相机信息 Camera Info
        si = col1.section("相机信息", "Camera Info")
        self._lbl_serial = QLabel("序列号 Serial:  —")
        self._lbl_res    = QLabel("分辨率 Resolution:  —")
        self._lbl_fac    = QLabel("Facilities:  —")
        self._lbl_fac.setWordWrap(True)
        for lb in (self._lbl_serial, self._lbl_res, self._lbl_fac):
            lb.setStyleSheet("font-size:10px;")
            si.add(lb)

        # 实时统计 Live Stats
        ss = col1.section("实时统计", "Live Stats")
        self._lbl_state      = QLabel("状态 State:  —")
        self._lbl_rate       = QLabel("事件率 Event Rate:  —")
        self._lbl_total      = QLabel("总量 Total:  —")
        self._lbl_rec_status = QLabel("录制 Recording:  —")
        for lb in (self._lbl_state, self._lbl_rate, self._lbl_total, self._lbl_rec_status):
            lb.setWordWrap(True)
            ss.add(lb)

        # 渲染性能 Render Performance
        sp = col1.section("渲染性能", "Render Perf")
        self._lbl_fps = QLabel("FPS: —")
        self._lbl_fps.setStyleSheet("font-size:14px;font-weight:bold;color:#3fb950;")
        sp.add(self._lbl_fps)

        _perf_defs = [
            ("_p_paint",   "帧耗时",   "Paint"),
            ("_p_wait",    "等待耗时", "Wait"),
            ("_p_epf",     "事件/帧",  "Evs/frm"),
            ("_p_batches", "包数/帧",  "Batches/f"),
            ("_p_vbo",     "VBO带宽",  "VBO BW"),
            ("_p_vsync",   "垂直同步", "VSync"),
            ("_p_orphan",  "VBO孤立",  "Orphan"),
            ("_p_timer",   "定时器",   "Timer"),
        ]
        for attr, zh, en in _perf_defs:
            row = QHBoxLayout()
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(3)
            lbl_n = QLabel(f"{zh}/{en}:")
            lbl_n.setStyleSheet("font-size:9px;color:#8b949e;")
            lbl_n.setFixedWidth(82)
            lbl_v = QLabel("—")
            lbl_v.setStyleSheet("font-size:9px;font-weight:bold;color:#e6edf3;")
            setattr(self, attr, lbl_v)
            row.addWidget(lbl_n)
            row.addWidget(lbl_v)
            row.addStretch()
            sp.add(row)

        hl.addWidget(col1)

        # ── Column 2: 可视化/偏置  Visualization / Biases ───────────────────
        col2 = ColumnPanel("可视化/偏置", "Viz / Biases", 250)

        # 可视化设置 Viz Settings
        sv = col2.section("可视化设置", "Viz Settings")
        sv.add(QLabel("模式  Mode"))
        self._cmb_viz = QComboBox()
        self._cmb_viz.addItems(["event_frame", "time_surface", "on_only",
                                 "off_only", "accumulated"])
        self._cmb_viz.currentTextChanged.connect(self._on_viz_mode)
        sv.add(self._cmb_viz)
        self._lbl_viz_desc = _muted("")
        sv.add(self._lbl_viz_desc)

        sv.add(QLabel("衰减系数  Decay Factor"))
        decay_row = QHBoxLayout()
        self._sld_decay = QSlider(Qt.Horizontal)
        self._sld_decay.setRange(1, 99); self._sld_decay.setValue(72)
        self._sld_decay.valueChanged.connect(self._on_decay)
        self._lbl_decay = QLabel("0.72"); self._lbl_decay.setFixedWidth(30)
        decay_row.addWidget(self._sld_decay); decay_row.addWidget(self._lbl_decay)
        sv.add(decay_row)

        sv.add(QLabel("事件切片  Event Slice  Δt (µs)"))
        self._spn_dt = QSpinBox()
        self._spn_dt.setRange(1000, 100_000); self._spn_dt.setValue(10_000)
        self._spn_dt.setSingleStep(1000)
        self._spn_dt.valueChanged.connect(lambda v: self.camera.set_delta_t(v))
        sv.add(self._spn_dt)
        sv.add(_muted("1000µs=1ms → max 1000Hz detection; restart to take effect"))

        btn_clr = QPushButton("🗑 清空帧缓冲  Clear Frame Buffer")
        btn_clr.clicked.connect(self._on_clear)
        sv.add(btn_clr)

        # 偏置调节 Bias Tuning (默认折叠)
        sb = col2.section("偏置调节", "Bias Tuning", expanded=False)
        self._bias_sliders: dict = {}
        self._bias_labels:  dict = {}
        for bname, bmeta in BIAS_DEFS.items():
            hdr = QHBoxLayout()
            name_lb = QLabel(f"<b>{bname}</b>")
            name_lb.setStyleSheet("font-size:10px;")
            hdr.addWidget(name_lb); hdr.addStretch()
            sb.add(hdr)
            sb.add(_muted(bmeta["desc"]))
            sld_row = QHBoxLayout()
            sld = QSlider(Qt.Horizontal)
            sld.setRange(bmeta["min"], bmeta["max"]); sld.setValue(bmeta["default"])
            val = QLabel(str(bmeta["default"])); val.setFixedWidth(26)
            val.setStyleSheet("font-size:10px;font-weight:bold;color:#58a6ff;")
            sld.valueChanged.connect(lambda v, n=bname, lv=val: self._on_bias(n, v, lv))
            sld_row.addWidget(sld); sld_row.addWidget(val)
            sb.add(sld_row)
            self._bias_sliders[bname] = sld
            self._bias_labels[bname]  = val
        btn_rst = QPushButton("↺ 恢复默认  Reset Defaults")
        btn_rst.clicked.connect(self._on_bias_reset)
        sb.add(btn_rst)

        hl.addWidget(col2)

        # ── Center: render window / placeholder ─────────────────────────────
        self._placeholder = QWidget()
        self._placeholder.setStyleSheet("background:#080b0f;")
        ph_lbl = QLabel("连接相机后开始采集\nConnect camera to start streaming",
                         self._placeholder)
        ph_lbl.setAlignment(Qt.AlignCenter)
        ph_lbl.setStyleSheet("color:#555;font-size:16px;")
        ph_vb = QVBoxLayout(self._placeholder)
        ph_vb.addWidget(ph_lbl)
        hl.addWidget(self._placeholder, 1)   # stretch=1

        # ── Column 3: 硬件控制  Hardware Control ────────────────────────────
        col3 = ColumnPanel("硬件控制", "Hardware", 252)

        # ERC 事件率控制 Event Rate Controller
        se = col3.section("事件率控制", "ERC")
        se.add(_muted("硬件限速 Hardware rate limiter — 超过阈值的事件在传感器内丢弃\n"
                       "Events above threshold are dropped inside the sensor"))
        erc_en = QHBoxLayout()
        erc_en.addWidget(QLabel("启用  Enable ERC"))
        self._chk_erc = QCheckBox(); erc_en.addWidget(self._chk_erc); erc_en.addStretch()
        se.add(erc_en)
        se.add(QLabel("阈值  Threshold  (events/s)"))
        self._spn_erc_thresh = QSpinBox()
        self._spn_erc_thresh.setRange(100_000, 100_000_000)
        self._spn_erc_thresh.setSingleStep(100_000); self._spn_erc_thresh.setValue(5_000_000)
        se.add(self._spn_erc_thresh)
        btn_erc = QPushButton("应用  Apply ERC"); btn_erc.clicked.connect(self._on_erc_apply)
        se.add(btn_erc)
        self._lbl_erc_status = _muted("—"); se.add(self._lbl_erc_status)

        # ROI 感兴趣区域 Region of Interest
        sr = col3.section("感兴趣区域", "ROI")
        sr.add(_muted("传感器级裁剪 Sensor-level crop — 只有区域内的像素会产生事件\n"
                       "Only pixels inside ROI generate events"))
        roi_en = QHBoxLayout()
        roi_en.addWidget(QLabel("启用  Enable ROI"))
        self._chk_roi = QCheckBox(); roi_en.addWidget(self._chk_roi); roi_en.addStretch()
        sr.add(roi_en)
        roi_form = QFormLayout()
        self._spn_roi_x = QSpinBox(); self._spn_roi_x.setRange(0, 9999)
        self._spn_roi_y = QSpinBox(); self._spn_roi_y.setRange(0, 9999)
        self._spn_roi_w = QSpinBox(); self._spn_roi_w.setRange(1, 9999); self._spn_roi_w.setValue(640)
        self._spn_roi_h = QSpinBox(); self._spn_roi_h.setRange(1, 9999); self._spn_roi_h.setValue(480)
        roi_form.addRow("X", self._spn_roi_x)
        roi_form.addRow("Y", self._spn_roi_y)
        roi_form.addRow("宽 Width", self._spn_roi_w)
        roi_form.addRow("高 Height", self._spn_roi_h)
        sr.add(roi_form)
        btn_roi = QPushButton("应用  Apply ROI"); btn_roi.clicked.connect(self._on_roi_apply)
        sr.add(btn_roi)
        self._lbl_roi_status = _muted("—"); sr.add(self._lbl_roi_status)

        # 抗闪烁 Anti-Flicker
        saf = col3.section("抗闪烁", "Anti-Flicker")
        saf.add(_muted("过滤电源频率噪声 Filter power-line flicker noise\n"
                        "50Hz / 60Hz 周期性事件噪声"))
        af_en = QHBoxLayout()
        af_en.addWidget(QLabel("启用  Enable"))
        self._chk_af = QCheckBox(); af_en.addWidget(self._chk_af); af_en.addStretch()
        saf.add(af_en)
        af_form = QFormLayout()
        self._spn_af_low  = QSpinBox(); self._spn_af_low.setRange(1, 1000); self._spn_af_low.setValue(50)
        self._spn_af_high = QSpinBox(); self._spn_af_high.setRange(1, 1000); self._spn_af_high.setValue(70)
        af_form.addRow("下限 Low (Hz)", self._spn_af_low)
        af_form.addRow("上限 High (Hz)", self._spn_af_high)
        saf.add(af_form)
        btn_af = QPushButton("应用  Apply Anti-Flicker"); btn_af.clicked.connect(self._on_af_apply)
        saf.add(btn_af)
        self._lbl_af_status = _muted("—"); saf.add(self._lbl_af_status)

        # 活动滤波 Activity Filter
        sact = col3.section("活动滤波", "Activity Filter")
        sact.add(_muted("按像素事件率门控 Per-pixel event-rate gate\n"
                         "范围外的像素被抑制 / pixels outside range are suppressed"))
        act_en = QHBoxLayout()
        act_en.addWidget(QLabel("启用  Enable"))
        self._chk_act = QCheckBox(); act_en.addWidget(self._chk_act); act_en.addStretch()
        sact.add(act_en)
        act_form = QFormLayout()
        self._spn_act_lower = QSpinBox()
        self._spn_act_lower.setRange(0, 10_000_000); self._spn_act_lower.setValue(100)
        self._spn_act_upper = QSpinBox()
        self._spn_act_upper.setRange(1, 100_000_000)
        self._spn_act_upper.setSingleStep(100_000); self._spn_act_upper.setValue(10_000_000)
        act_form.addRow("下限 Lower (ev/s/px)", self._spn_act_lower)
        act_form.addRow("上限 Upper (ev/s/px)", self._spn_act_upper)
        sact.add(act_form)
        btn_act = QPushButton("应用  Apply Activity Filter"); btn_act.clicked.connect(self._on_act_apply)
        sact.add(btn_act)
        self._lbl_act_status = _muted("—"); sact.add(self._lbl_act_status)

        hl.addWidget(col3)

        # ── Column 4: 触发/监控/录制  Trigger / Monitor / Record ────────────
        col4 = ColumnPanel("触发/监控/录制", "Trigger/Monitor/Rec", 252)

        # 触发输入 Trigger In
        sti = col4.section("触发输入", "Trigger In")
        sti.add(_muted("接收外部脉冲 Receive external pulse — 产生 EventExtTrigger\n"
                        "用于多设备时间同步 / multi-device time sync"))
        tin_form = QFormLayout()
        self._spn_trig_in_ch = QSpinBox(); self._spn_trig_in_ch.setRange(0, 7)
        tin_form.addRow("通道 Channel", self._spn_trig_in_ch)
        sti.add(tin_form)
        tin_btns = QHBoxLayout()
        btn_tin_on  = QPushButton("启用 Enable"); btn_tin_on.clicked.connect(lambda: self._on_trig_in(True))
        btn_tin_off = QPushButton("禁用 Disable"); btn_tin_off.clicked.connect(lambda: self._on_trig_in(False))
        tin_btns.addWidget(btn_tin_on); tin_btns.addWidget(btn_tin_off)
        sti.add(tin_btns)
        self._lbl_trig_in_status = _muted("—"); sti.add(self._lbl_trig_in_status)

        # 触发输出 Trigger Out
        sto = col4.section("触发输出", "Trigger Out")
        sto.add(_muted("输出周期性脉冲 Output periodic pulses\n"
                        "驱动外设同步 / drive external device sync"))
        tout_form = QFormLayout()
        self._spn_trig_out_period = QSpinBox()
        self._spn_trig_out_period.setRange(1000, 100_000_000)
        self._spn_trig_out_period.setValue(1_000_000); self._spn_trig_out_period.setSingleStep(1000)
        self._dsb_trig_out_duty = QDoubleSpinBox()
        self._dsb_trig_out_duty.setRange(0.01, 0.99)
        self._dsb_trig_out_duty.setValue(0.5); self._dsb_trig_out_duty.setSingleStep(0.05)
        tout_form.addRow("周期 Period (µs)", self._spn_trig_out_period)
        tout_form.addRow("占空比 Duty Cycle", self._dsb_trig_out_duty)
        sto.add(tout_form)
        tout_btns = QHBoxLayout()
        btn_tout_on  = QPushButton("启用 Enable"); btn_tout_on.clicked.connect(lambda: self._on_trig_out(True))
        btn_tout_off = QPushButton("停止 Stop"); btn_tout_off.clicked.connect(lambda: self._on_trig_out(False))
        tout_btns.addWidget(btn_tout_on); tout_btns.addWidget(btn_tout_off)
        sto.add(tout_btns)
        self._lbl_trig_out_status = _muted("—"); sto.add(self._lbl_trig_out_status)

        # 传感器监控 Monitoring
        smon = col4.section("传感器监控", "Monitoring")
        mon_cards = QHBoxLayout()
        tg = QGroupBox("温度 Temp"); tgl = QVBoxLayout(tg)
        self._lbl_temp = QLabel("—")
        self._lbl_temp.setAlignment(Qt.AlignCenter)
        self._lbl_temp.setStyleSheet("font-size:20px;font-weight:bold;color:#e3b341;")
        tgl.addWidget(self._lbl_temp); tgl.addWidget(_muted("°C"))
        mon_cards.addWidget(tg)
        lg = QGroupBox("光照 Lux"); lgl = QVBoxLayout(lg)
        self._lbl_lux = QLabel("—")
        self._lbl_lux.setAlignment(Qt.AlignCenter)
        self._lbl_lux.setStyleSheet("font-size:20px;font-weight:bold;color:#e3b341;")
        lgl.addWidget(self._lbl_lux); lgl.addWidget(_muted("lux"))
        mon_cards.addWidget(lg)
        smon.add(mon_cards)
        btn_ref = QPushButton("🔄 刷新  Refresh"); btn_ref.clicked.connect(self._on_mon_refresh)
        smon.add(btn_ref)
        auto_row = QHBoxLayout()
        auto_row.addWidget(QLabel("自动刷新 Auto-refresh (2s)"))
        self._chk_mon_auto = QCheckBox()
        self._chk_mon_auto.stateChanged.connect(self._on_mon_auto)
        auto_row.addWidget(self._chk_mon_auto); auto_row.addStretch()
        smon.add(auto_row)
        self._mon_timer = QTimer(self)
        self._mon_timer.setInterval(2000)
        self._mon_timer.timeout.connect(self._on_mon_refresh)

        # 录制 Recording
        srec = col4.section("录制", "Recording")
        srec.add(_muted("保存为 NumPy .npy 文件  Save as NumPy array file"))
        srec.add(QLabel("路径  Path"))
        self._txt_rec_path = QLineEdit("recording.npy")
        srec.add(self._txt_rec_path)
        self._btn_rec_start = QPushButton("⏺ 开始录制  Start Recording")
        self._btn_rec_start.clicked.connect(self._on_rec_start)
        self._btn_rec_stop  = QPushButton("⏹ 停止录制  Stop Recording")
        self._btn_rec_stop.clicked.connect(self._on_rec_stop)
        self._btn_rec_stop.setEnabled(False)
        srec.add(self._btn_rec_start)
        srec.add(self._btn_rec_stop)
        self._lbl_rec_result = QLabel("")
        self._lbl_rec_result.setWordWrap(True)
        self._lbl_rec_result.setStyleSheet("font-size:10px;color:#8b949e;")
        srec.add(self._lbl_rec_result)
        code_lbl = QLabel("import numpy as np\nevs = np.load('rec.npy', allow_pickle=True)\n"
                           "# fields: x, y, p(0=OFF/1=ON), t(µs)")
        code_lbl.setStyleSheet("font-family:Consolas,monospace;font-size:9px;"
                                "color:#8b949e;background:#0d1117;padding:6px;"
                                "border-radius:4px;")
        srec.add(code_lbl)

        hl.addWidget(col4)

    # ── 相机操作 ───────────────────────────────────────────────────────────────
    def _on_connect(self):
        try:
            info = self.camera.connect()
            w, h = info["width"], info["height"]
            self._lbl_serial.setText(f"序列号: {info.get('serial','—')}")
            self._lbl_res.setText(f"分辨率: {w}×{h}")
            # Facilities 可用性
            fac = info.get("facilities", {})
            enabled = [k for k, v in fac.items() if v]
            self._lbl_fac.setText("Facilities:\n" + ("  ".join(enabled) if enabled else "—"))
            # ROI 默认大小
            self._spn_roi_w.setValue(w); self._spn_roi_h.setValue(h)
            # IMX636：连接后自动启用 ERC（5 Mev/s），防止高事件率压垮渲染线程
            if fac.get("erc"):
                self._chk_erc.setChecked(True)
                self._on_erc_apply()
            self._start_render(w, h)
            self._update_buttons()
        except Exception as e:
            self._lbl_state.setText(f"连接失败: {e}")

    def _start_render(self, w: int, h: int):
        """创建渲染窗口和渲染线程，替换中央占位区域 / Replace center placeholder."""
        self._stop_render()

        self._render_win = DVSRenderWindow()
        self._render_win.resize(w, h)

        self._container = QWidget.createWindowContainer(self._render_win, self)
        self._container.setMinimumSize(320, 240)
        self._container.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        # 在 QHBoxLayout 中把 placeholder 替换成 render container
        hl = self._main_hl
        idx = hl.indexOf(self._placeholder)
        hl.insertWidget(idx, self._container, 1)   # stretch=1，让其充满
        self._placeholder.hide()
        self._container.show()
        # 确保 expose 事件在渲染线程启动前已经派发到 QWindow
        QApplication.processEvents()

        self._render_thr = RenderThread(self._render_win, w, h)
        self._render_thr.fps_updated.connect(
            lambda fps: self._lbl_fps.setText(f"FPS: {fps:.1f}"))
        self._render_thr.perf_updated.connect(self._on_perf)
        self._render_thr.start()

        # 等待 GL 初始化完成再绑定回调（最多 5s）
        if not self._render_thr.wait_gl_ready(5.0):
            print("[Main] GL 初始化超时")
            return

        self.camera.add_event_callback(self._render_thr.push_events)

    def _stop_render(self):
        if self._render_thr:
            self.camera.remove_event_callback(self._render_thr.push_events)
            self._render_thr.stop_render()
            self._render_thr.wait(3000)
            self._render_thr = None
            # 清空性能面板
            self._lbl_fps.setText("FPS: —")
            gray = "font-size:9px;font-weight:bold;color:#8b949e;"
            for attr in ("_p_paint","_p_wait","_p_epf","_p_vbo",
                         "_p_vsync","_p_orphan","_p_timer","_p_queue"):
                getattr(self, attr).setText("—")
                getattr(self, attr).setStyleSheet(gray)
        if self._container:
            hl = self._main_hl
            hl.removeWidget(self._container)
            self._container.deleteLater()
            self._container = None
            # 恢复占位符（确保在布局中仍有 stretch=1 的中央元素）
            idx_ph = hl.indexOf(self._placeholder)
            if idx_ph < 0:
                # placeholder 已被移出布局，重新插入到中间位置（列2之后）
                hl.insertWidget(2, self._placeholder, 1)
            self._placeholder.show()
        if self._render_win:
            self._render_win.destroy()
            self._render_win = None

    def _on_start(self):
        try:
            self.camera.start()
            self._update_buttons()
        except Exception as e:
            self._lbl_state.setText(f"启动失败: {e}")

    def _on_stop(self):
        try:
            self.camera.stop()
            self._update_buttons()
        except Exception as e:
            self._lbl_state.setText(f"停止失败: {e}")

    def _on_disconnect(self):
        self._stop_render()
        self.camera.disconnect()
        self._update_buttons()

    # ── 可视化控制 ─────────────────────────────────────────────────────────────
    def _on_viz_mode(self, mode: str):
        if self._render_thr:
            self._render_thr.viz_mode = mode
        self._lbl_viz_desc.setText(self._VIZ_DESC.get(mode, ""))

    def _on_decay(self, v: int):
        f = v / 100.0
        self._lbl_decay.setText(f"{f:.2f}")
        if self._render_thr:
            self._render_thr.decay_factor = f

    def _on_clear(self):
        if self._render_thr:
            self._render_thr.clear_frame()

    def _on_bias(self, name: str, value: int, label: QLabel):
        label.setText(str(value))
        if self.camera.state.value in ("connected", "streaming"):
            try:
                self.camera.set_bias(name, value)
            except Exception:
                pass

    def _on_bias_reset(self):
        for name, meta in BIAS_DEFS.items():
            self._bias_sliders[name].setValue(meta["default"])

    # ── ERC ────────────────────────────────────────────────────────────────────
    def _on_erc_apply(self):
        try:
            self.camera.set_erc(self._chk_erc.isChecked(),
                                 self._spn_erc_thresh.value())
            self._lbl_erc_status.setText("✓ 已应用")
            self._lbl_erc_status.setStyleSheet("font-size:10px;color:#3fb950;")
        except Exception as e:
            self._lbl_erc_status.setText(f"✗ {e}")
            self._lbl_erc_status.setStyleSheet("font-size:10px;color:#f85149;")

    # ── ROI ────────────────────────────────────────────────────────────────────
    def _on_roi_apply(self):
        try:
            self.camera.set_roi(self._chk_roi.isChecked(),
                                 self._spn_roi_x.value(), self._spn_roi_y.value(),
                                 self._spn_roi_w.value(), self._spn_roi_h.value())
            self._lbl_roi_status.setText("✓ 已应用")
            self._lbl_roi_status.setStyleSheet("font-size:10px;color:#3fb950;")
        except Exception as e:
            self._lbl_roi_status.setText(f"✗ {e}")
            self._lbl_roi_status.setStyleSheet("font-size:10px;color:#f85149;")

    # ── Anti-Flicker ───────────────────────────────────────────────────────────
    def _on_af_apply(self):
        try:
            self.camera.set_antiflicker(self._chk_af.isChecked(),
                                         self._spn_af_low.value(),
                                         self._spn_af_high.value())
            self._lbl_af_status.setText("✓ 已应用")
            self._lbl_af_status.setStyleSheet("font-size:10px;color:#3fb950;")
        except Exception as e:
            self._lbl_af_status.setText(f"✗ {e}")
            self._lbl_af_status.setStyleSheet("font-size:10px;color:#f85149;")

    # ── Activity Filter ────────────────────────────────────────────────────────
    def _on_act_apply(self):
        try:
            self.camera.set_activity_filter(self._chk_act.isChecked(),
                                             self._spn_act_lower.value(),
                                             self._spn_act_upper.value())
            self._lbl_act_status.setText("✓ 已应用")
            self._lbl_act_status.setStyleSheet("font-size:10px;color:#3fb950;")
        except Exception as e:
            self._lbl_act_status.setText(f"✗ {e}")
            self._lbl_act_status.setStyleSheet("font-size:10px;color:#f85149;")

    # ── Trigger ────────────────────────────────────────────────────────────────
    def _on_trig_in(self, enabled: bool):
        try:
            self.camera.set_trigger_in(self._spn_trig_in_ch.value(), enabled)
            self._lbl_trig_in_status.setText(f"✓ {'已启用' if enabled else '已禁用'}")
            self._lbl_trig_in_status.setStyleSheet("font-size:10px;color:#3fb950;")
        except Exception as e:
            self._lbl_trig_in_status.setText(f"✗ {e}")
            self._lbl_trig_in_status.setStyleSheet("font-size:10px;color:#f85149;")

    def _on_trig_out(self, enabled: bool):
        try:
            self.camera.set_trigger_out(enabled,
                                         self._spn_trig_out_period.value(),
                                         self._dsb_trig_out_duty.value())
            self._lbl_trig_out_status.setText(f"✓ {'已启用' if enabled else '已停止'}")
            self._lbl_trig_out_status.setStyleSheet("font-size:10px;color:#3fb950;")
        except Exception as e:
            self._lbl_trig_out_status.setText(f"✗ {e}")
            self._lbl_trig_out_status.setStyleSheet("font-size:10px;color:#f85149;")

    # ── Monitoring ─────────────────────────────────────────────────────────────
    def _on_mon_refresh(self):
        try:
            mon = self.camera.get_monitoring()
            temp = mon.get("temperature")
            lux  = mon.get("illuminance")
            self._lbl_temp.setText(f"{temp:.1f} °C"   if temp is not None else "— °C")
            self._lbl_lux.setText(f"{lux:.0f} lux"    if lux  is not None else "— lux")
        except Exception:
            pass

    def _on_mon_auto(self, state: int):
        if state:
            self._mon_timer.start()
        else:
            self._mon_timer.stop()

    # ── Recording ──────────────────────────────────────────────────────────────
    def _on_rec_start(self):
        try:
            path = self._txt_rec_path.text().strip() or "recording.npy"
            self.camera.start_recording(path)
            self._btn_rec_start.setEnabled(False)
            self._btn_rec_stop.setEnabled(True)
            self._lbl_rec_result.setText(f"录制中: {path}")
            self._lbl_rec_result.setStyleSheet("font-size:11px;color:#3fb950;")
            self._lbl_rec_status.setText("录制: 🔴 录制中")
        except Exception as e:
            self._lbl_rec_result.setText(f"✗ {e}")
            self._lbl_rec_result.setStyleSheet("font-size:11px;color:#f85149;")

    def _on_rec_stop(self):
        try:
            result = self.camera.stop_recording()
            self._btn_rec_start.setEnabled(True)
            self._btn_rec_stop.setEnabled(False)
            events = result.get("events", 0)
            path   = result.get("path",   "")
            self._lbl_rec_result.setText(f"✓ 已保存 {events:,} 个事件\n→ {path}")
            self._lbl_rec_result.setStyleSheet("font-size:11px;color:#3fb950;")
            self._lbl_rec_status.setText("录制: 未录制")
        except Exception as e:
            self._lbl_rec_result.setText(f"✗ {e}")
            self._lbl_rec_result.setStyleSheet("font-size:11px;color:#f85149;")

    # ── 渲染性能回调（每秒一次，来自 RenderThread）────────────────────────────
    def _on_perf(self, d: dict):
        paint   = d["paint_ms_avg"]
        wait    = d["wait_ms_avg"]
        epf     = d["evs_per_frame"]
        vbo_kb  = d["vbo_kb_per_s"]
        batches = d.get("batches_per_frame", 0)

        self._p_paint.setText(f"{paint:.2f} ms")
        self._p_paint.setStyleSheet(
            f"font-size:9px;font-weight:bold;"
            f"color:{'#3fb950' if paint < 2.0 else '#e3b341' if paint < 5.0 else '#f85149'};")

        self._p_wait.setText(f"{wait:.2f} ms")
        # 等待时间接近 4ms = 空闲（纯衰减帧）；接近 0 = 事件驱动立即唤醒
        self._p_wait.setStyleSheet(
            f"font-size:9px;font-weight:bold;"
            f"color:{'#58a6ff' if wait > 2.0 else '#3fb950'};")

        if epf >= 1_000_000:
            self._p_epf.setText(f"{epf/1e6:.2f} M/帧")
        elif epf >= 1000:
            self._p_epf.setText(f"{epf/1000:.1f} k/帧")
        else:
            self._p_epf.setText(f"{int(epf)}/帧")
        self._p_epf.setStyleSheet("font-size:9px;font-weight:bold;color:#e6edf3;")

        # 包数/帧：修复前固定为 0-1，修复后显示真实 USB 包累积数
        self._p_batches.setText(f"{batches:.1f} pkts")
        # >10 包/帧 = 修复生效（绿）；≤1 = 仍在丢弃（红）
        self._p_batches.setStyleSheet(
            f"font-size:9px;font-weight:bold;"
            f"color:{'#3fb950' if batches > 10 else '#e3b341' if batches > 1 else '#f85149'};")

        if vbo_kb >= 1024:
            self._p_vbo.setText(f"{vbo_kb/1024:.1f} MB/s")
        else:
            self._p_vbo.setText(f"{vbo_kb:.0f} KB/s")
        self._p_vbo.setStyleSheet("font-size:9px;font-weight:bold;color:#e6edf3;")

        tick = "✓"
        green = "font-size:9px;font-weight:bold;color:#3fb950;"
        self._p_vsync .setText(f"{tick} 关闭 (SwapInt=0)");  self._p_vsync.setStyleSheet(green)
        self._p_orphan.setText(f"{tick} 已启用");              self._p_orphan.setStyleSheet(green)
        self._p_timer .setText(f"{tick} 1ms 精度");           self._p_timer.setStyleSheet(green)

    # ── 状态轮询 ───────────────────────────────────────────────────────────────
    def _on_poll(self):
        try:
            st = self.camera.get_status()
        except Exception:
            return
        self._lbl_state.setText(f"状态: {st['state']}")
        rate  = st["event_rate_kevs"]
        total = st["total_events"]
        self._lbl_rate.setText(
            f"事件率: {rate/1000:.2f} Mev/s" if rate >= 1000
            else f"事件率: {rate:.1f} kev/s")
        self._lbl_total.setText(
            f"总事件: {total/1e9:.2f}G" if total >= 1e9
            else f"总事件: {total/1e6:.1f}M" if total >= 1e6
            else f"总事件: {total}")
        if st.get("is_recording"):
            self._lbl_rec_status.setText("录制: 🔴 录制中")

    def _update_buttons(self):
        s = self.camera.state.value
        self._btn_conn.setEnabled(s == "disconnected")
        self._btn_start.setEnabled(s == "connected")
        self._btn_stop.setEnabled(s == "streaming")
        self._btn_disc.setEnabled(s in ("connected", "streaming"))

    def closeEvent(self, event):
        self._stop_render()
        self.camera.disconnect()
        super().closeEvent(event)


# ═══════════════════════════════════════════════════════════════════════════════
#  入口
# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    # Windows 定时器精度：15ms → 1ms
    try:
        ctypes.windll.winmm.timeBeginPeriod(1)
        import atexit
        atexit.register(lambda: ctypes.windll.winmm.timeEndPeriod(1))
    except Exception:
        pass

    fmt = QSurfaceFormat()
    fmt.setVersion(3, 3)
    fmt.setProfile(QSurfaceFormat.CoreProfile)
    fmt.setSwapInterval(0)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.Window,          QColor(22, 27, 34))
    pal.setColor(QPalette.WindowText,      QColor(230, 237, 243))
    pal.setColor(QPalette.Base,            QColor(13, 17, 23))
    pal.setColor(QPalette.AlternateBase,   QColor(28, 33, 40))
    pal.setColor(QPalette.Button,          QColor(33, 38, 45))
    pal.setColor(QPalette.ButtonText,      QColor(230, 237, 243))
    pal.setColor(QPalette.Highlight,       QColor(31, 111, 235))
    pal.setColor(QPalette.HighlightedText, QColor(255, 255, 255))
    app.setPalette(pal)

    win = MainWindow()
    win.show()
    sys.exit(app.exec_())
