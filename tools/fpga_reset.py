"""
EVK4 diagnostic - use libusb1 directly via ctypes with correct function signatures.
"""
import ctypes, ctypes.util, struct, time, sys

# Load libusb
libusb = ctypes.CDLL(r'C:\openeb\build\bin\Release\libusb-1.0.dll')

# Define types
class libusb_context(ctypes.Structure):
    pass

class libusb_device_handle(ctypes.Structure):
    pass

ctx_p = ctypes.POINTER(libusb_context)
handle_p = ctypes.POINTER(libusb_device_handle)

# Set function signatures
libusb.libusb_init.argtypes = [ctypes.POINTER(ctx_p)]
libusb.libusb_init.restype = ctypes.c_int

libusb.libusb_exit.argtypes = [ctx_p]
libusb.libusb_exit.restype = None

libusb.libusb_open_device_with_vid_pid.argtypes = [ctx_p, ctypes.c_uint16, ctypes.c_uint16]
libusb.libusb_open_device_with_vid_pid.restype = handle_p

libusb.libusb_close.argtypes = [handle_p]
libusb.libusb_close.restype = None

libusb.libusb_claim_interface.argtypes = [handle_p, ctypes.c_int]
libusb.libusb_claim_interface.restype = ctypes.c_int

libusb.libusb_release_interface.argtypes = [handle_p, ctypes.c_int]
libusb.libusb_release_interface.restype = ctypes.c_int

libusb.libusb_set_interface_alt_setting.argtypes = [handle_p, ctypes.c_int, ctypes.c_int]
libusb.libusb_set_interface_alt_setting.restype = ctypes.c_int

libusb.libusb_clear_halt.argtypes = [handle_p, ctypes.c_ubyte]
libusb.libusb_clear_halt.restype = ctypes.c_int

libusb.libusb_bulk_transfer.argtypes = [
    handle_p, ctypes.c_ubyte, ctypes.POINTER(ctypes.c_ubyte),
    ctypes.c_int, ctypes.POINTER(ctypes.c_int), ctypes.c_uint
]
libusb.libusb_bulk_transfer.restype = ctypes.c_int

# Init
ctx = ctx_p()
r = libusb.libusb_init(ctypes.byref(ctx))
if r < 0:
    print(f'libusb_init failed: {r}')
    sys.exit(1)

# Open device
handle = libusb.libusb_open_device_with_vid_pid(ctx, 0x04b4, 0x00f5)
if not handle:
    print('Device not found')
    libusb.libusb_exit(ctx)
    sys.exit(1)
print('Device opened')

# Claim interface
r = libusb.libusb_claim_interface(handle, 0)
print(f'Claim interface: {r} (0=OK)')

# Set alt setting
r = libusb.libusb_set_interface_alt_setting(handle, 0, 0)
print(f'Set alt setting: {r} (0=OK)')

# From device: [0]=0x82 IN, [1]=0x02 OUT, [2]=0x81 IN
EP_CTRL_IN = 0x82
EP_CTRL_OUT = 0x02

# Clear halt
r = libusb.libusb_clear_halt(handle, EP_CTRL_IN)
print(f'Clear halt 0x{EP_CTRL_IN:02x}: {r}')
r = libusb.libusb_clear_halt(handle, EP_CTRL_OUT)
print(f'Clear halt 0x{EP_CTRL_OUT:02x}: {r}')

ERROR_NAMES = {0:'OK', -1:'IO', -2:'INVALID_PARAM', -3:'ACCESS', -4:'NO_DEVICE',
               -5:'NOT_FOUND', -6:'BUSY', -7:'TIMEOUT', -8:'OVERFLOW', -9:'PIPE',
               -10:'INTERRUPTED', -11:'NO_MEM', -12:'NOT_SUPPORTED', -99:'OTHER'}

def errname(code):
    return ERROR_NAMES.get(code, str(code))

def tz_transfer(prop, payload=b''):
    frame_data = struct.pack('<II', 8 + len(payload), prop) + payload
    buf_out = (ctypes.c_ubyte * len(frame_data))(*frame_data)
    sent = ctypes.c_int(0)

    r = libusb.libusb_bulk_transfer(handle, EP_CTRL_OUT, buf_out, len(frame_data), ctypes.byref(sent), 1000)
    if r != 0:
        return r, 0, 0, b''

    buf_in = (ctypes.c_ubyte * 4096)()
    received = ctypes.c_int(0)
    r = libusb.libusb_bulk_transfer(handle, EP_CTRL_IN, buf_in, 4096, ctypes.byref(received), 10000)
    if r != 0:
        return r, 0, 0, b''

    resp = bytes(buf_in[:received.value])
    if len(resp) >= 8:
        s, p = struct.unpack('<II', resp[:8])
        data = resp[8:min(s, len(resp))] if s > 8 else b''
        return 0, s, p, data
    return 0, 0, 0, resp

print('\n=== Treuzell Protocol Queries ===')

queries = [
    ('Serial',      0x72),
    ('Version',     0x79),
    ('Build date',  0x7A),
    ('FPGA state',  0x71),
    ('Devices',     0x10000),
]

for name, prop in queries:
    err, s, p, d = tz_transfer(prop)
    if err != 0:
        print(f'  {name}: ERROR {errname(err)} ({err})')
        libusb.libusb_clear_halt(handle, EP_CTRL_IN)
        libusb.libusb_clear_halt(handle, EP_CTRL_OUT)
        continue

    failed = (p & 0x80000000) != 0
    dh = d.hex() if d else 'empty'
    tag = 'FAIL' if failed else 'OK'
    print(f'  {name}: [{tag}] size=0x{s:08x} prop=0x{p:08x} data={dh}')

    if not failed:
        if name == 'Version' and len(d) >= 4:
            ver = struct.unpack('<I', d[:4])[0]
            print(f'    -> {(ver>>16)&0xFF}.{(ver>>8)&0xFF}.{ver&0xFF} (0x{ver:08x})')
        if name == 'Serial' and len(d) >= 8:
            ser = struct.unpack('<Q', d[:8])[0]
            print(f'    -> 0x{ser:016x}')
        if name == 'Devices' and len(d) >= 4:
            cnt = struct.unpack('<I', d[:4])[0]
            print(f'    -> count = {cnt}')
        if name == 'FPGA state' and len(d) >= 4:
            st = struct.unpack('<I', d[:4])[0]
            print(f'    -> {"BOOTED" if st else "NOT BOOTED"}')

# FPGA Reset
print('\n=== FPGA Reset Attempt ===')
libusb.libusb_clear_halt(handle, EP_CTRL_IN)
libusb.libusb_clear_halt(handle, EP_CTRL_OUT)

magic = struct.pack('<I', 0xB007F26A)
err, s, p, d = tz_transfer(0x71 | 0x40000000, magic)
if err != 0:
    print(f'  Reset error: {errname(err)}')
else:
    tag = 'FAIL' if (p & 0x80000000) else 'OK'
    print(f'  Reset: [{tag}] prop=0x{p:08x}')
    if not (p & 0x80000000):
        print('  Waiting 5s...')
        time.sleep(5)
        err, s, p, d = tz_transfer(0x10000)
        if err == 0 and len(d) >= 4 and not (p & 0x80000000):
            cnt = struct.unpack('<I', d[:4])[0]
            print(f'  Devices after reset: {cnt}')
        else:
            print(f'  Check failed: err={errname(err)} prop=0x{p:08x}')

libusb.libusb_release_interface(handle, 0)
libusb.libusb_close(handle)
libusb.libusb_exit(ctx)
print('\nDone.')