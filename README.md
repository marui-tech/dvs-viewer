<div align="center">

# DVS Viewer

**高性能 DVS 相机实时可视化工具 · High-performance DVS Camera Viewer · DVS カメラビューア**

[![License](https://img.shields.io/badge/License-GPLv3%20%2F%20Commercial-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.9%2B-green.svg)](https://python.org)
[![OpenGL](https://img.shields.io/badge/OpenGL-3.3%20Core-orange.svg)](https://opengl.org)
[![Platform](https://img.shields.io/badge/Platform-Windows-lightgrey.svg)](https://microsoft.com/windows)

[中文](#中文) | [English](#english) | [日本語](#日本語)

</div>

---

<a name="中文"></a>
## 中文

### 项目简介

DVS Viewer 是针对 Prophesee IMX636（EVK4）等 DVS（动态视觉传感器）相机开发的高性能实时可视化与控制软件。基于 **PyQt5 + OpenGL 3.3 Core 独立渲染线程**架构，事件到显示延迟 < 3ms，渲染帧率可达 500–2000fps。

### 功能特性

**可视化**
- 5 种渲染模式：`event_frame`（ON=绿/OFF=红，衰减） · `time_surface`（热力图） · `on_only` · `off_only` · `accumulated`（累积不衰减）
- 衰减系数实时调节（0.01–0.99）

**硬件控制**（通过 OpenEB HAL）
- **偏置调节**：bias_fo · bias_hpf · bias_diff_on · bias_diff · bias_diff_off · bias_refr
- **ERC**：硬件事件率限制，默认 5 Mev/s（适配 IMX636）
- **ROI**：传感器级硬件裁剪
- **Anti-Flicker**：抑制 50/60 Hz 电源频率噪声
- **Activity Filter**：像素级事件率门控
- **Trigger In/Out**：外部同步信号

**监控与录制**
- 实时温度与照度显示
- 事件录制为 `.npy` 格式，支持离线分析

**界面**
- 4 列可折叠面板 + 中央 OpenGL 渲染区
- 所有标签中英双语
- 渲染性能实时面板（FPS · 帧耗时 · 包数/帧 · VBO 带宽）

### 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11 x64 |
| Python | 3.9+ |
| GPU | OpenGL 3.3 Core Profile |
| OpenEB | 从源码编译（见下方） |
| 硬件 | Prophesee EVK4 / IMX636 或其他 OpenEB 兼容相机 |

### 快速开始

```bash
# 1. 安装 Python 依赖
pip install -r requirements.txt

# 2. 连接 DVS 相机（USB）

# 3. 启动
python dvs_viewer.py
```

> **首次使用**：请先阅读 [docs/openeb_install_windows.md](docs/openeb_install_windows.md) 完成 OpenEB 编译和 USB 驱动安装，再阅读 [drivers/README.md](drivers/README.md) 安装 WinUSB 驱动。

### 使用说明

1. 点击 **连接相机 Connect** — 自动发现并连接第一个可用相机
2. 点击 **开始采集 Start** — 启动事件流，画面开始渲染
3. 左侧面板查看实时统计和渲染性能；右侧面板调节 ERC / 偏置 / ROI 等参数
4. 点击 **停止 Stop** → **断开 Disconnect**

### 演示

> Demo 文件位于 `demo/` 目录。

#### 场景一：人 · 摩托车 · 汽车

`demo/人摩托车汽车.mp4`

街道场景录制。DVS 相机仅在亮度变化处产生事件，静止背景完全静默，运动目标（行人、摩托车、汽车）以极低延迟的事件流精确勾勒。相比传统帧相机，无运动模糊，轮廓清晰，且数据量远小于完整帧图像。

#### 场景二：海螺

`demo/海螺.mp4`

近距离拍摄海螺表面纹理。螺旋纹路在 DVS 事件流中以高对比度呈现，展示传感器对微弱亮度梯度的高灵敏度响应能力。

#### 场景三：红外激光（普通相机不可见）

| 普通相机 | DVS 相机 |
|----------|----------|
| `demo/激光红外(本身红外不可见).jpg` | `demo/激光红外红外.png` |
| 蓝色桌面，激光点几乎不可见 | 黑色背景，激光扫过轨迹以红绿事件清晰显现 |

Prophesee IMX636 对近红外波段（NIR）具有较高响应，普通 RGB 相机会用滤光片截止该波段。左图为普通相机拍摄：场景中激光枪正对桌面，但光点几乎不可见；右图为同一场景下 DVS Viewer 的渲染输出：激光扫过轨迹形成明亮的红（OFF）绿（ON）事件串，在纯黑背景上清晰可见。此特性可用于：
- 隐蔽光源检测与追踪
- 工业激光标记/打孔在线监控
- 夜视与安防感知

---

### 目录结构

```
dvs-viewer/
├── dvs_viewer.py               # 主程序（PyQt5 + OpenGL 渲染线程）
├── camera_manager.py           # OpenEB HAL 封装（相机控制 + 事件回调）
├── requirements.txt
├── LICENSE
├── README.md
├── docs/
│   └── openeb_install_windows.md
├── tools/
│   ├── test_camera.py          # 相机连接测试脚本
│   └── fpga_reset.py           # USB/FPGA 诊断与复位工具
├── drivers/
│   ├── README.md
│   ├── installer_x64.exe       # WinUSB 驱动安装程序
│   └── wdi-simple.exe
└── demo/                       # 演示视频与图片
```

### 技术架构

```
采集线程 (HAL)                      渲染线程 (QThread)
──────────────────────────          ─────────────────────────────────
get_latest_raw_data()               threading.Event.wait(1ms)
  └─ decode()                         └─ 取出全部积累包
       └─ on_cd(evs)                       └─ np.concatenate (>1包时)
            └─ push_events(evs)                └─ 直接上传原始结构体到 GPU
                 └─ _pending_list.append            └─ Decay Pass  (ping-pong FBO)
                                                    └─ Event Pass  (GL_POINTS)
                                                    └─ glBlitFramebuffer (1:1)
                                                    └─ swapBuffers (VSync=OFF)
```

**延迟优化一览**

| 优化项 | 实现 | 效果 |
|--------|------|------|
| 关闭垂直同步 | `SwapInterval=0` | 不等显示器刷新 |
| VBO 孤立化 | `glBufferData(NULL)` | 消除 GPU 读写停顿 |
| 1ms 系统定时器 | `timeBeginPeriod(1)` | 15ms → 1ms 睡眠精度 |
| 无拷贝事件上传 | `uint16/int16` 顶点属性 | 省去 CPU float32 转换 |
| 关闭 GL 错误检查 | `OpenGL.ERROR_CHECKING=False` | GL 调用 2–3x 提速 |
| 缓存 Uniform 位置 | `_init_gl` 时查询一次 | 省去每帧字符串哈希 |
| 显示尺寸 FBO | FBO 跟随窗口物理像素 | 消除缩放模糊 |
| 事件累积队列 | 锁保护列表，每帧消费全部 | 消除约 95% 的事件丢弃 |

### 授权说明

本项目采用**双协议授权**：

- **开源版**：[GNU GPL v3](LICENSE) — 免费使用，衍生产品须以 GPL v3 开源
- **商业版**：如需集成到闭源产品中，请联系购买商业授权

**商业授权咨询：sjfdtxz@yeah.net**

版权所有 © 2025 烟台码睿智能科技有限公司 (Marui Tech Co., Ltd.)

---

<a name="english"></a>
## English

### Overview

DVS Viewer is a high-performance real-time visualization and control application for DVS (Dynamic Vision Sensor) cameras, targeting the Prophesee IMX636 (EVK4) and other OpenEB-compatible sensors. It achieves **event-to-display latency under 3ms** and render rates of 500–2000fps via a dedicated PyQt5 + OpenGL 3.3 Core render thread.

### Features

**Visualization**
- 5 rendering modes: `event_frame` (ON=green / OFF=red, decay) · `time_surface` (heatmap) · `on_only` · `off_only` · `accumulated`
- Real-time decay factor slider (0.01–0.99)

**Hardware Control** (via OpenEB HAL)
- **Biases**: bias_fo · bias_hpf · bias_diff_on · bias_diff · bias_diff_off · bias_refr
- **ERC**: Hardware event rate limiter, default 5 Mev/s (tuned for IMX636)
- **ROI**: Sensor-level hardware crop window
- **Anti-Flicker**: Suppress 50/60 Hz power-line noise
- **Activity Filter**: Per-pixel event rate gating
- **Trigger In/Out**: External synchronization signals

**Monitoring & Recording**
- Live temperature and illuminance readout
- Event recording to `.npy` for offline analysis (`x`, `y`, `p`, `t` fields)

**UI**
- 4-column collapsible panel layout, central OpenGL render view
- Bilingual labels (Chinese / English)
- Live render performance panel (FPS · paint_ms · Batches/frm · VBO BW)

### Requirements

| Item | Requirement |
|------|-------------|
| OS | Windows 10/11 x64 |
| Python | 3.9+ |
| GPU | OpenGL 3.3 Core Profile |
| OpenEB | Built from source (see below) |
| Camera | Prophesee EVK4 / IMX636 or any OpenEB-compatible DVS camera |

### Quick Start

```bash
# 1. Install Python dependencies
pip install -r requirements.txt

# 2. Connect your DVS camera via USB

# 3. Launch
python dvs_viewer.py
```

> **First time?** Follow [docs/openeb_install_windows.md](docs/openeb_install_windows.md) to build OpenEB and install the WinUSB driver from [drivers/](drivers/).

### Demo

> Demo files are located in the `demo/` directory.

#### Scene 1 — People, Motorcycles, Cars

`demo/人摩托车汽车.mp4`

Street scene captured with DVS Viewer. The static background produces zero events; moving targets (pedestrians, motorcycles, cars) are outlined with microsecond-precision event streams. No motion blur, crisp edges, and orders-of-magnitude less data than full-frame video.

#### Scene 2 — Conch Shell

`demo/海螺.mp4`

Close-up of a conch shell surface. The spiral ridges are rendered with high contrast in the event stream, demonstrating the sensor's sensitivity to subtle brightness gradients.

#### Scene 3 — Infrared Laser (invisible to normal cameras)

| Normal camera | DVS camera |
|---------------|------------|
| `demo/激光红外(本身红外不可见).jpg` | `demo/激光红外红外.png` |
| Blue tabletop, laser spot nearly invisible | Black background, laser trail clearly visible as red/green events |

The Prophesee IMX636 responds well to near-infrared (NIR) wavelengths that standard RGB cameras block with an IR-cut filter. Left: a laser gun aimed at the table is nearly invisible to a regular camera. Right: the same scene in DVS Viewer — the laser sweep leaves a vivid trail of red (OFF) and green (ON) events against a pitch-black background. Use cases include:
- Covert light source detection and tracking
- Industrial laser marking / drilling inline monitoring
- Night-vision and security sensing

---

### Architecture

```
Capture Thread (HAL)                Render Thread (QThread)
────────────────────────────        ─────────────────────────────────
get_latest_raw_data()               threading.Event.wait(1ms)
  └─ decode()                         └─ drain all pending batches
       └─ on_cd(evs)                       └─ np.concatenate (if > 1)
            └─ push_events(evs)                └─ upload raw struct to GPU
                 └─ _pending_list.append            └─ Decay Pass  (ping-pong FBO)
                                                    └─ Event Pass  (GL_POINTS)
                                                    └─ glBlitFramebuffer (1:1)
                                                    └─ swapBuffers (VSync OFF)
```

### Latency Optimizations

| Optimization | Implementation | Effect |
|---|---|---|
| VSync disabled | `SwapInterval=0` | No display-refresh stall |
| VBO orphaning | `glBufferData(NULL)` | Eliminates GPU R/W stall |
| 1ms system timer | `timeBeginPeriod(1)` | Windows 15ms → 1ms precision |
| Zero-copy event upload | `uint16/int16` vertex attribs | No CPU float32 conversion |
| GL error checking off | `OpenGL.ERROR_CHECKING=False` | 2–3× GL call throughput |
| Cached uniform locations | Queried once in `_init_gl` | No per-frame string lookup |
| Display-size FBO | FBO tracks window physical pixels | Eliminates downscale blur |
| Accumulating event queue | Locked list, drained per frame | Eliminates ~95% event drop |

### License

This project is **dual-licensed**:

- **Open Source**: [GNU GPL v3](LICENSE) — free to use; derivative works must be released under the same license
- **Commercial**: For closed-source integration, a commercial license is available

**Commercial licensing: sjfdtxz@yeah.net**

Copyright © 2025 Marui Tech Co., Ltd. (烟台码睿智能科技有限公司)

---

<a name="日本語"></a>
## 日本語

### 概要

DVS Viewer は Prophesee IMX636（EVK4）などの DVS（動的視覚センサー）カメラ向けの高性能リアルタイム可視化・制御ソフトウェアです。PyQt5 + OpenGL 3.3 Core の専用レンダリングスレッドにより、**イベントから表示までのレイテンシ 3ms 未満**を実現します。

### 主な機能

**可視化**
- 5種類のレンダリングモード：`event_frame` / `time_surface` / `on_only` / `off_only` / `accumulated`
- リアルタイム減衰係数スライダー

**ハードウェア制御**（OpenEB HAL 経由）
- バイアス調整 · ERC（イベントレート制限、デフォルト 5 Mev/s） · ROI · アンチフリッカー · アクティビティフィルタ · トリガー入出力

**モニタリング & 録画**
- リアルタイム温度・照度表示
- `.npy` 形式でのイベント録画（オフライン解析対応）

**UI**
- 4列折りたたみパネル + OpenGL レンダリングエリア
- 中英バイリンガルラベル

### デモ

> デモファイルは `demo/` ディレクトリにあります。

#### シーン 1 — 人・バイク・自動車

`demo/人摩托车汽车.mp4`

街中のシーンを DVS Viewer で録画。静止した背景はイベントを発生させず、動くターゲット（歩行者・バイク・自動車）がマイクロ秒精度のイベントストリームで輪郭されます。モーションブラーなし、データ量も通常フレーム映像より大幅に少ないです。

#### シーン 2 — 巻き貝

`demo/海螺.mp4`

巻き貝の表面テクスチャをクローズアップ撮影。螺旋状の溝がイベントストリームで高コントラストに表現され、センサーが微細な輝度変化に対して高感度に反応することを示しています。

#### シーン 3 — 赤外線レーザー（通常カメラには不可視）

| 通常カメラ | DVS カメラ |
|-----------|-----------|
| `demo/激光红外(本身红外不可见).jpg` | `demo/激光红外红外.png` |
| 青いテーブル、レーザー点がほぼ不可視 | 黒背景、レーザー軌跡が赤・緑イベントとして鮮明に表示 |

Prophesee IMX636 は通常の RGB カメラが IRカットフィルターで遮断する近赤外（NIR）波長にも高感度です。左は通常カメラの映像でレーザー点はほぼ見えませんが、右の DVS Viewer 出力では同じシーンのレーザー走査軌跡が真っ黒の背景に赤（OFF）と緑（ON）のイベント列として明確に映し出されます。応用例：
- 不可視光源の検出・追跡
- 工業用レーザーマーキング・穿孔のインライン監視
- 夜間視覚・セキュリティセンシング

---

### 動作環境

- Windows 10/11 x64 · Python 3.9+ · OpenGL 3.3 Core
- OpenEB（ソースからビルド）· WinUSB ドライバ

### クイックスタート

```bash
pip install -r requirements.txt
python dvs_viewer.py
```

### ライセンス

本プロジェクトは**デュアルライセンス**を採用しています。

- **オープンソース版**：[GNU GPL v3](LICENSE)
- **商用版**：クローズドソース製品への組み込みには商用ライセンスが必要です

商用ライセンスのお問い合わせ：**sjfdtxz@yeah.net**

Copyright © 2025 煙台マルイインテリジェントテクノロジー株式会社 (Marui Tech Co., Ltd.)
