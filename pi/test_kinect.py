"""Test de faisabilité Kinect 1 sur le Pi : cadence de la profondeur (libfreenect_sync via ctypes, sans compilation)."""
import ctypes, ctypes.util, sys, time

import numpy as np

import os
lib = ctypes.CDLL(os.environ.get("FREENECT_SYNC") or ctypes.util.find_library("freenect_sync") or "libfreenect_sync.so.0")
FREENECT_DEPTH_11BIT, FREENECT_DEPTH_MM = 0, 5
lib.freenect_sync_get_depth.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_uint32), ctypes.c_int, ctypes.c_int]
lib.freenect_sync_get_depth.restype = ctypes.c_int


def depth(fmt=FREENECT_DEPTH_MM):
    ptr, ts = ctypes.c_void_p(), ctypes.c_uint32()
    if lib.freenect_sync_get_depth(ctypes.byref(ptr), ctypes.byref(ts), 0, fmt) != 0:
        return None, 0
    buf = (ctypes.c_uint16 * (640 * 480)).from_address(ptr.value)
    return np.frombuffer(buf, np.uint16).reshape(480, 640).copy(), ts.value


if __name__ == "__main__":
    dur = float(sys.argv[1]) if len(sys.argv) > 1 else 60
    d, _ = depth()
    if d is None:
        print("ÉCHEC : impossible de lire la profondeur"); sys.exit(1)
    t0, n, last_ts, dup, per_s, sec, cnt = time.time(), 0, None, 0, [], int(time.time()), 0
    while time.time() - t0 < dur:
        d, ts = depth()
        if d is None: print("perte du flux"); break
        if ts == last_ts: dup += 1; continue
        last_ts, n, cnt = ts, n + 1, cnt + 1
        if int(time.time()) != sec:
            per_s.append(cnt); cnt, sec = 0, int(time.time())
    valid = d[d > 0]
    print(f"images : {n} en {time.time() - t0:.0f} s → {n / (time.time() - t0):.1f} ips ; doublons ignorés : {dup}")
    print(f"ips par seconde : min {min(per_s[1:] or [0])}, max {max(per_s or [0])}")
    print(f"dernière image : {100 * valid.size / d.size:.0f} % de pixels valides, distance médiane {np.median(valid) if valid.size else 0:.0f} mm")
    lib.freenect_sync_stop()
