"""Process resource snapshot without external dependencies.

Reads current RSS from /proc on Linux, the working set via psapi on Windows,
and peak RSS via the resource module elsewhere; every field degrades to None
when the platform lacks the source.
"""

from __future__ import annotations

import os
import sys

_BYTES_PER_MIB = 1024 * 1024


def process_memory_snapshot() -> dict[str, float | int | None]:
    rss_bytes = _rss_from_proc()
    peak_bytes = _peak_rss_from_resource()
    return {
        "pid": os.getpid(),
        "rss_mb": _to_mib(rss_bytes),
        "peak_rss_mb": _to_mib(peak_bytes),
    }


def _to_mib(value: int | None) -> float | None:
    if value is None or value <= 0:
        return None
    return round(value / _BYTES_PER_MIB, 2)


def _rss_from_proc() -> int | None:
    if sys.platform == "win32":
        return _windows_working_set()[0]
    try:
        with open("/proc/self/statm", "rb") as file:
            fields = file.read().split()
        return int(fields[1]) * os.sysconf("SC_PAGE_SIZE")
    except (OSError, ValueError, IndexError, AttributeError):
        return None


def _peak_rss_from_resource() -> int | None:
    if sys.platform == "win32":
        return _windows_working_set()[1]
    try:
        import resource
    except ImportError:
        return None
    try:
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    except (OSError, ValueError):
        return None
    if peak <= 0:
        return None
    # ru_maxrss is KiB on Linux and bytes on macOS.
    return int(peak) if sys.platform == "darwin" else int(peak) * 1024


def _windows_working_set() -> tuple[int | None, int | None]:
    """Return (working set, peak working set) in bytes on Windows."""

    if sys.platform != "win32":
        return None, None
    try:
        import ctypes
        from ctypes import wintypes

        class _ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE,
            ctypes.POINTER(_ProcessMemoryCounters),
            wintypes.DWORD,
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL

        counters = _ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(),
            ctypes.byref(counters),
            counters.cb,
        ):
            return None, None
        return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)
    except (OSError, AttributeError, ValueError, TypeError):
        return None, None
