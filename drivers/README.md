# DVS Camera USB Driver Installation

本目录包含 Prophesee EVK4 / IMX636 的 WinUSB 驱动安装工具。

---

## 为什么需要安装驱动

Windows 不会自动为 Prophesee 相机加载 WinUSB 驱动。若未安装，`DeviceDiscovery.open("")` 会抛出异常，设备管理器中相机显示为"未知设备"或带感叹号。

---

## 文件说明

| 文件 | 说明 |
|------|------|
| `installer_x64.exe` | WinUSB 驱动一键安装程序（推荐） |
| `wdi-simple.exe` | 底层 WinUSB Device Installer，供高级用户使用 |

---

## 安装步骤

### 方法一：一键安装（推荐）

1. 用 USB 线连接 EVK4 相机，等待 Windows 识别设备（约 5 秒）
2. 以**管理员身份**运行 `installer_x64.exe`
3. 安装完成后，打开**设备管理器**，确认相机出现在 **通用串行总线设备** 下，名称类似 `EVK4` 或 `Prophesee`
4. 运行 `python tools/test_camera.py` 验证连接

### 方法二：wdi-simple（命令行）

```cmd
wdi-simple.exe --vid 0x04B4 --pid 0x00F4 --name "EVK4" --dest "."
```

> VID/PID 以你的设备管理器中显示的为准。

---

## 验证安装

```bash
python tools/test_camera.py
```

输出示例：

```
[camera] 发现相机: serial=000001, 1280x720
[camera] 事件流正常，5 秒内收到 12,345,678 个事件
```

---

## 卸载 / 切换驱动

若需要卸载 WinUSB 驱动（例如切换回厂商驱动）：

1. 打开**设备管理器**
2. 右键相机 → **卸载设备** → 勾选"删除此设备的驱动程序软件"
3. 拔插 USB，Windows 将重新枚举设备

---

## 常见问题

| 现象 | 原因 | 解决 |
|------|------|------|
| `DeviceDiscovery` 找不到相机 | 驱动未安装或安装失败 | 以管理员身份重新运行 `installer_x64.exe` |
| 设备管理器显示感叹号 | 驱动版本不匹配 | 卸载后重新安装 |
| 安装后仍无法连接 | USB 口供电不足 | 换用主板直出 USB 3.0 口，避免使用 Hub |
| 连接时立即断开 | FPGA 固件异常 | 运行 `python tools/fpga_reset.py` |

---

版权所有 © 2025 烟台码睿智能科技有限公司 (Marui Tech Co., Ltd.)
