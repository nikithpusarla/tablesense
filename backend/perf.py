"""Process-level performance settings for running on ordinary (often laptop) PCs."""
from __future__ import annotations

import logging
import sys

log = logging.getLogger(__name__)


def disable_windows_power_throttling() -> bool:
    """Opt this process out of Windows "EcoQoS" power throttling (best effort).

    On battery, Windows 11 moves background processes (a server with no visible window)
    onto efficiency cores at low clock speed, which can make YOLO inference 5-10x slower.
    Returns True when the opt-out was applied.
    """
    if sys.platform != "win32":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        class ProcessPowerThrottlingState(ctypes.Structure):
            _fields_ = [("Version", wintypes.ULONG), ("ControlMask", wintypes.ULONG),
                        ("StateMask", wintypes.ULONG)]

        process_power_throttling = 4                 # PROCESS_INFORMATION_CLASS value
        execution_speed = 0x1                        # PROCESS_POWER_THROTTLING_EXECUTION_SPEED
        state = ProcessPowerThrottlingState(1, execution_speed, 0)  # control it, state = off
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        # Declare types: without them ctypes truncates the 64-bit process handle and the call fails.
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        kernel32.SetProcessInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        kernel32.SetProcessInformation.restype = wintypes.BOOL
        ok = bool(kernel32.SetProcessInformation(kernel32.GetCurrentProcess(), process_power_throttling,
                                                 ctypes.byref(state), ctypes.sizeof(state)))
        if ok:
            log.info("Windows power throttling disabled for TableSense")
        return ok
    except Exception:  # pragma: no cover - depends on Windows version
        log.debug("Could not change power throttling", exc_info=True)
        return False


def limit_opencv_threads(n: int = 2) -> None:
    """Keep OpenCV from spawning a thread per core next to PyTorch's own pool."""
    try:
        import cv2

        cv2.setNumThreads(n)
    except Exception:  # pragma: no cover
        pass
