# Changelog

All notable changes to DVS Viewer are documented here.

---

## [1.3.0] — 2026-04-13

### Added
- **instant（瞬时帧）可视化模式** — 每帧只显示当前 Δt 窗口内的事件，不做衰减叠加，彻底消除超高速目标的水平横纹拖影
- Δt 最小值从 1000µs 降至 **100µs**，步进改为 100µs，配合 instant 模式可设置 200–500µs 超细切片

### Fixed
- 修复退出时卡死问题
- 修复 `QSpinBox` / `QLineEdit` / `QComboBox` 在深色系统主题下黑底黑字、文字不可见的问题

---

## [1.2.0] — 2026-03-xx

### Added
- **高速目标偏置预设** — 一键切换为适合高速场景的 bias 组合，减少像素电路不稳定
- **录制 ROI 过滤** — 录制时仅保存 ROI 区域内事件，降低文件体积
- **密度抽样** — 高速模式下对过密事件按比例抽样，降低渲染负载

### Fixed
- 高速预设满屏白问题（bias 默认值修正）
- Trigger In Channel 枚举错误
- 高速预设未自动启用 Activity Filter，导致热像素方块噪音
- 录制 IO 改为独立线程，消除连接/录制时采集滞后与卡死
- 慢放空帧不再清屏，消除黑屏闪烁

---

## [1.1.0] — 2026-02-xx

### Added
- **.npy 离线回放** — 支持 0.0001×–10× 变速播放
- **可拖拽进度条** — VLC 风格底部播放条，可跳转到任意位置
- 回放 UI 简化：单一播放/暂停切换按钮，状态提示清晰

### Fixed
- 回放闪烁与细节丢失
- `_p_batches` 属性名笔误（原 `_p_queue`）导致断连时 AttributeError

---

## [1.0.0] — 2026-01-xx

### Initial Release
- PyQt5 + OpenGL 3.3 Core 独立渲染线程，事件到显示延迟 < 3ms
- 5 种渲染模式：`event_frame` · `time_surface` · `on_only` · `off_only` · `accumulated`
- 硬件控制：偏置 · ERC · ROI · Anti-Flicker · Activity Filter · Trigger In/Out
- 实时温度与照度显示
- 事件录制为 `.npy` 格式
- WinUSB 驱动 + OpenEB 安装文档
