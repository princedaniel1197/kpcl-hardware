"""CPU time consumed by another process, precisely.

T-12 used to read `ps -o time=`, which has one-second resolution. Over a
twenty-second window that is +-5 % of one core of noise on a quantity of about
1 %: a measurement that could not tell the collector's load from nothing.

macOS: proc_pid_rusage() from libproc, whose user and system times are in Mach
absolute-time units (nanoseconds on Intel, 41.67 ns ticks on Apple silicon) and
are converted with mach_timebase_info. Linux: /proc/<pid>/stat, in clock ticks
(usually 10 ms) -- coarser, and said so by `resolution_s`.
"""

from __future__ import annotations

import ctypes
import os
import sys


class _RusageInfoV2(ctypes.Structure):
    _fields_ = [
        ("ri_uuid", ctypes.c_uint8 * 16),
        ("ri_user_time", ctypes.c_uint64),
        ("ri_system_time", ctypes.c_uint64),
        ("ri_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_interrupt_wkups", ctypes.c_uint64),
        ("ri_pageins", ctypes.c_uint64),
        ("ri_wired_size", ctypes.c_uint64),
        ("ri_resident_size", ctypes.c_uint64),
        ("ri_phys_footprint", ctypes.c_uint64),
        ("ri_proc_start_abstime", ctypes.c_uint64),
        ("ri_proc_exit_abstime", ctypes.c_uint64),
        ("ri_child_user_time", ctypes.c_uint64),
        ("ri_child_system_time", ctypes.c_uint64),
        ("ri_child_pkg_idle_wkups", ctypes.c_uint64),
        ("ri_child_interrupt_wkups", ctypes.c_uint64),
        ("ri_child_pageins", ctypes.c_uint64),
        ("ri_child_elapsed_abstime", ctypes.c_uint64),
        ("ri_diskio_bytesread", ctypes.c_uint64),
        ("ri_diskio_byteswritten", ctypes.c_uint64),
    ]


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


_RUSAGE_INFO_V2 = 2


def _darwin_reader():
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    libsystem = ctypes.CDLL("/usr/lib/libSystem.dylib")
    timebase = _Timebase()
    libsystem.mach_timebase_info(ctypes.byref(timebase))
    to_s = timebase.numer / timebase.denom / 1e9

    def read(pid: int) -> float:
        info = _RusageInfoV2()
        if libproc.proc_pid_rusage(pid, _RUSAGE_INFO_V2, ctypes.byref(info)) != 0:
            raise OSError(ctypes.get_errno(), f"proc_pid_rusage({pid}) failed")
        return (info.ri_user_time + info.ri_system_time) * to_s

    return read, to_s


def _linux_reader():
    ticks = os.sysconf("SC_CLK_TCK")

    def read(pid: int) -> float:
        with open(f"/proc/{pid}/stat") as f:
            fields = f.read().rsplit(")", 1)[1].split()
        return (int(fields[11]) + int(fields[12])) / ticks   # utime, stime

    return read, 1.0 / ticks


if sys.platform == "darwin":
    cpu_seconds, resolution_s = _darwin_reader()
elif sys.platform.startswith("linux"):
    cpu_seconds, resolution_s = _linux_reader()
else:                                                 # pragma: no cover
    raise ImportError(f"no precise CPU reader for {sys.platform}")
