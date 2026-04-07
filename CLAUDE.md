# DVS Viewer — CLAUDE.md

## 项目概览

Prophesee IMX636 (EVK4) DVS 相机高性能实时可视化工具。
- **主程序**：`dvs_viewer.py`（PyQt5 + OpenGL 3.3 Core 渲染线程）
- **相机封装**：`camera_manager.py`（OpenEB HAL，纯事件回调，无 web 方法）
- **GitHub**：`https://github.com/marui-tech/dvs-viewer`
- **授权**：GPL v3 / 商业双协议，联系邮箱 sjfdtxz@yeah.net
- **版权**：烟台码睿智能科技有限公司 (Marui Tech Co., Ltd.)

## 架构要点

### 渲染线程 `RenderThread(QThread)`
- 独立 OpenGL context，`threading.Event.wait(1ms)` 驱动循环
- 事件通过 `push_events(evs)` 写入 `_pending_list`（锁保护列表，非原子替换）
- 每帧一次性消费全部积累包（`np.concatenate`），消除 ~95% 事件丢弃
- FBO 跟随窗口物理像素（display-size FBO），消除缩放模糊
- letterbox 映射在 event vertex shader 中完成（`u_vp` + `u_sensor` uniform）

### 关键优化（不要回退）
| 优化 | 位置 |
|------|------|
| `OpenGL.ERROR_CHECKING = False` | 文件顶部，GL 调用 2-3x 提速 |
| VBO 孤立化 `glBufferData(NULL)` | `_paint` 方法 |
| `glBlitFramebuffer` 替代 shader blit | `_paint` 方法 |
| `uint16/int16` 直接上传事件结构体 | VAO 设置，stride=14 |
| uniform 位置缓存 | `_init_gl` 一次性查询 |
| `timeBeginPeriod(1)` 1ms 定时器 | 文件顶部 Windows 初始化 |
| `SwapInterval=0` 关闭垂直同步 | `_init_gl` |

### 事件数据类型
```python
_EVT_DTYPE  = np.dtype([('x','<u2'),('y','<u2'),('p','<i2'),('t','<i8')])
_EVT_STRIDE = 14  # bytes
```

### HAL 采集流程
`get_latest_raw_data()` → `decode()` → `on_cd(evs)` → `evs.copy()` → `push_events()`

**必须 `evs.copy()`**：HAL 回调返回后内部缓冲区可能立即被复用。

## 性能面板标签属性名

`_p_paint` · `_p_wait` · `_p_epf` · `_p_batches` · `_p_vbo` · `_p_vsync` · `_p_orphan` · `_p_timer`

注意：`_p_batches`（包数/帧），**不是** `_p_queue`（曾经的笔误，已修复）。

## IMX636 特定配置
- ERC 默认阈值：`5_000_000` ev/s（非通用默认 10M，IMX636 最高输出约 66M ev/s）
- HAL facility `get_i_event_rate_activity_filter_module()` 在 IMX636 上返回 `None`（不支持）

## 目录结构
```
dvs_viewer.py          # 主程序
camera_manager.py      # HAL 封装（仅事件回调 + 硬件控制，无渲染/web方法）
requirements.txt       # PyQt5 / PyOpenGL / numpy / opencv-python
demo/                  # 演示视频与图片（4个文件）
docs/                  # openeb_install_windows.md
drivers/               # WinUSB 驱动 + README
tools/                 # test_camera.py / fpga_reset.py
web/                   # 已废弃（gitignore 排除），仅剩 .claude 会话目录
```

## OpenEB 路径（Windows）
```
C:\openeb\build\py3\Release
C:\openeb\build\bin\Release
C:\openeb\build\lib\metavision\hal\plugins
```

## 开发注意事项
- `camera_manager.py` 已清理所有 web 专用方法（`_render`、`get_frame_jpeg_*`、`set_viz_mode`、`set_polarity_filter`、`clear_frame`）及 `cv2`/`base64` 导入，不要重新引入
- `web/` 目录不参与 git，不要提交其中内容
- demo 文件名含中文，GitHub README 中括号需 URL 编码：`%28` `%29`
