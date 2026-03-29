import sys, os, time

sys.path.insert(0, r'C:\openeb\build\py3\Release')

_dll_dirs = [
    r'C:\openeb\build\bin\Release',
    r'C:\vcpkg\installed\x64-windows\bin',
    r'C:\openeb\build\lib\Release',
]
for d in _dll_dirs:
    try:
        os.add_dll_directory(d)
    except Exception:
        pass

os.environ['PATH'] = ';'.join(_dll_dirs) + ';' + os.environ.get('PATH', '')
os.environ['MV_HAL_PLUGIN_PATH'] = r'C:\openeb\build\lib\metavision\hal\plugins'
os.environ['MV_LOG_LEVEL'] = 'TRACE'

import metavision_sdk_stream as mv

RETRIES = 5
DELAY   = 3.0

cam = None
for attempt in range(RETRIES):
    try:
        cam = mv.Camera.from_first_available()
        break
    except RuntimeError as e:
        print(f"[attempt {attempt+1}/{RETRIES}] 连接失败: {e}")
        if attempt < RETRIES - 1:
            print(f"  等待 {DELAY}s 后重试...")
            time.sleep(DELAY)

if cam is None:
    print("无法连接相机，请检查 USB 连接或执行 FPGA 复位。")
    sys.exit(1)

print('OK serial:', cam.from_serial())
del cam
