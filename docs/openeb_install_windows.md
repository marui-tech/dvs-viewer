# OpenEB on Windows from Sources - 安装操作记录

- 文档来源：https://docs.prophesee.ai/stable/installation/windows_openeb.html
- 记录日期：2026-03-16
- 版本：OpenEB 5.2.0
- 编译完成：2026-03-16 ✓

## 实际安装路径

| 项目 | 路径 |
|------|------|
| OpenEB 源码 | `C:\openeb` |
| Vcpkg | `C:\vcpkg` |
| Python | `C:\Users\sjfdt\anaconda3\envs\dvscamera\python.exe` (conda env) |
| 编译输出 | `C:\openeb\build\bin\Release\` |
| Python 模块 | `C:\openeb\build\py3\Release\` |

## 实际编译命令（已验证可用）

```bat
set PATH=C:\Program Files\Microsoft Visual Studio\2022\Professional\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin;%PATH%

cd C:\openeb\build

cmake .. -A x64 ^
  -DCMAKE_BUILD_TYPE=Release ^
  -DCMAKE_TOOLCHAIN_FILE=C:\openeb\cmake\toolchains\vcpkg.cmake ^
  -DVCPKG_DIRECTORY=C:\vcpkg ^
  -DBUILD_TESTING=OFF ^
  -DPython3_ARTIFACTS_INTERACTIVE=TRUE ^
  -DPython3_EXECUTABLE=C:\Users\sjfdt\anaconda3\envs\dvscamera\python.exe ^
  -DPython3_INCLUDE_DIR=C:\Users\sjfdt\anaconda3\envs\dvscamera\include ^
  -DPython3_LIBRARY=C:\Users\sjfdt\anaconda3\envs\dvscamera\libs\python310.lib ^
  -DBOOST_ROOT=C:\vcpkg\installed\x64-windows ^
  -DBoost_NO_SYSTEM_PATHS=ON ^
  -DBoost_NO_BOOST_CMAKE=ON

cmake --build . --config Release --parallel 4
```

**注意事项（踩过的坑）：**
1. cmake 必须在 PATH 中，否则 `_add_global_alias_library` 内部调用 `cmake --help-property-list` 失败，导致 Python include 路径不写入 vcxproj
2. Python 必须用含头文件的 conda env（`dvscamera`），不能用 venv
3. 必须强制 `Boost_NO_BOOST_CMAKE=ON`，否则会找到 Anaconda 的 boost（GCC 编译，MSVC 不兼容）

---

## 一、系统要求

- Windows 10 / Windows 11（64位，amd64 架构）
- 显卡支持 OpenGL 3.0 及以上
- 文件系统：NTFS（不支持 FAT32 / exFAT）
- 需要开启长路径支持（gpedit.msc）

### 开启长路径支持

```
Win + R → gpedit.msc → 计算机配置 → 管理模板 → 系统 → 文件系统
→ 启用"启用 Win32 长路径"
```

---

## 二、安装开发工具

按顺序安装以下工具：

| 工具 | 版本 | 备注 |
|------|------|------|
| Git for Windows | 最新 | https://git-scm.com/download/win |
| CMake | 3.26 | 注意：不兼容过新版本 |
| Visual Studio 2022 | 17.14 | 选 MSVC 64-bit 编译器 + Windows 11 SDK + English Language Pack |
| Python | 3.10 / 3.11 / 3.12（64位）| 安装时勾选"Add to PATH" |

### Visual Studio 2022 安装组件要求

在 Visual Studio Installer 中勾选：
- **使用 C++ 的桌面开发**工作负载
- MSVC v143 - VS 2022 C++ x64/x86 生成工具
- Windows 11 SDK
- English Language Pack（英语语言包）

---

## 三、安装 Vcpkg 及依赖库

### 3.1 下载并解压 Vcpkg

从官方下载 Vcpkg 版本 **2024.11.16**（不要用最新版，保持兼容性）：

```
https://github.com/microsoft/vcpkg/releases/tag/2024.11.16
```

假设解压到 `C:\vcpkg`（即 `<VCPKG_SRC_DIR>`）。

### 3.2 Bootstrap Vcpkg

```bat
cd C:\vcpkg
bootstrap-vcpkg.bat
vcpkg update
```

### 3.3 复制 OpenEB 的依赖清单

将 OpenEB 源码目录中的依赖文件复制过来：

```bat
copy <OPENEB_SRC_DIR>\utils\windows\11\vcpkg-openeb.json C:\vcpkg\vcpkg.json
```

### 3.4 安装所有依赖库

```bat
cd C:\vcpkg
vcpkg install --triplet x64-windows --x-install-root installed
```

依赖库包括：libusb、boost、OpenCV、gtest、pybind11、GLEW、GLFW3、HDF5、protobuf 等（约需较长时间）。

### 3.5 安装 FFMPEG（单独安装）

1. 从 https://ffmpeg.org/download.html 下载 Windows 预编译版
2. 解压后将 `bin` 目录加入系统 PATH 环境变量

---

## 四、Python 环境配置

### 4.1 安装 Python

安装 Python 3.10 / 3.11 / 3.12（64位）。

PATH 顺序要求（重要）：
1. Python 主目录（如 `C:\Python311`）
2. Scripts 目录（如 `C:\Python311\Scripts`）
3. WindowsApps（必须在最后）

### 4.2 创建虚拟环境

```bat
python -m venv C:\tmp\prophesee\py3venv --system-site-packages
C:\tmp\prophesee\py3venv\Scripts\activate
```

### 4.3 安装 Python 依赖

```bat
pip install -r <OPENEB_SRC_DIR>\requirements_openeb.txt -r <OPENEB_SRC_DIR>\requirements_pytorch_cpu.txt
```

---

## 五、下载 OpenEB 源码

```bat
git clone https://github.com/prophesee-ai/openeb.git --branch 5.2.0
```

克隆完成后目录即为 `<OPENEB_SRC_DIR>`，例如 `C:\openeb`。

> 注意：不要从 GitHub 自动生成的 ZIP 下载，必须用 git clone 或官方提供的 Full Source Code 包。

---

## 六、编译 OpenEB

### 方式 A：命令行编译（推荐）

```bat
cd <OPENEB_SRC_DIR>
mkdir build
cd build

cmake .. -A x64 ^
  -DCMAKE_BUILD_TYPE=Release ^
  -DCMAKE_TOOLCHAIN_FILE=<OPENEB_SRC_DIR>\cmake\toolchains\vcpkg.cmake ^
  -DVCPKG_DIRECTORY=<VCPKG_SRC_DIR> ^
  -DBUILD_TESTING=OFF

cmake --build . --config Release --parallel 4
```

将 `<OPENEB_SRC_DIR>` 替换为实际路径，如 `C:\openeb`；
将 `<VCPKG_SRC_DIR>` 替换为 Vcpkg 路径，如 `C:\vcpkg`。

### 方式 B：Visual Studio GUI 编译

```bat
cd <OPENEB_SRC_DIR>
mkdir build
cd build

cmake .. -G "Visual Studio 17 2022" -A x64 ^
  -DCMAKE_BUILD_TYPE=Release ^
  -DCMAKE_TOOLCHAIN_FILE=<OPENEB_SRC_DIR>\cmake\toolchains\vcpkg.cmake ^
  -DVCPKG_DIRECTORY=<VCPKG_SRC_DIR> ^
  -DBUILD_TESTING=OFF
```

然后用 Visual Studio 打开 `build\metavision.sln`，选择 Release 配置，构建 ALL_BUILD 项目。

---

## 七、部署 / 安装

### 方式一：直接从 build 目录运行（开发调试用）

```bat
<OPENEB_SRC_DIR>\build\utils\scripts\setup_env.bat
```

每次打开新命令行都需执行此脚本来设置环境变量。

### 方式二：安装到默认位置（需管理员权限）

以管理员身份打开命令提示符：

```bat
cmake --build . --config Release --target install
```

### 方式三：安装到自定义目录

重新运行 CMake，追加参数：

```bat
cmake .. -A x64 ^
  -DCMAKE_BUILD_TYPE=Release ^
  -DCMAKE_TOOLCHAIN_FILE=<OPENEB_SRC_DIR>\cmake\toolchains\vcpkg.cmake ^
  -DVCPKG_DIRECTORY=<VCPKG_SRC_DIR> ^
  -DBUILD_TESTING=OFF ^
  -DCMAKE_INSTALL_PREFIX=<OPENEB_INSTALL_DIR> ^
  -DPYTHON3_SITE_PACKAGES=<PYTHON3_PACKAGES_INSTALL_DIR>

cmake --build . --config Release --target install
```

手动添加以下环境变量：

| 环境变量 | 值 |
|----------|-----|
| PATH | 追加 `<OPENEB_INSTALL_DIR>\bin` |
| MV_HAL_PLUGIN_PATH | `<OPENEB_INSTALL_DIR>\lib\metavision\hal\plugins` |
| HDF5_PLUGIN_PATH | `<OPENEB_INSTALL_DIR>\lib\hdf5\plugin` |
| PYTHONPATH | `<PYTHON3_PACKAGES_INSTALL_DIR>` |

---

## 八、安装相机驱动（EVK 系列）

下载 `wdi-simple.exe`（从 Prophesee 文档页面获取链接），以管理员身份执行：

```bat
wdi-simple.exe -n "EVK" -m "Prophesee" -v 0x04b4 -p 0x00f4
wdi-simple.exe -n "EVK" -m "Prophesee" -v 0x04b4 -p 0x00f5
wdi-simple.exe -n "EVK" -m "Prophesee" -v 0x04b4 -p 0x00f3
```

---

## 九、（可选）运行测试

1. 下载 OpenEB 测试数据（约 1.5 GB）
2. 解压到 `<OPENEB_SRC_DIR>/datasets`
3. 重新配置 CMake，将 `-DBUILD_TESTING=OFF` 改为 `-DBUILD_TESTING=ON`
4. 重新编译
5. 运行测试：

```bat
cd <OPENEB_SRC_DIR>\build
ctest -C Release
```

---

## 十、常见问题备注

- **CMake 版本**：建议使用 3.26，过新版本可能不兼容。
- **Vcpkg 版本**：固定使用 2024.11.16，保证依赖一致性。
- **Python PATH 顺序**：必须保证 Python 主目录在 WindowsApps 之前，否则 python 命令可能指向错误位置。
- **编译并行数**：`--parallel 4` 可根据 CPU 核心数调整，提升编译速度。
- **管理员权限**：安装驱动和执行 install target 时需要管理员权限。
