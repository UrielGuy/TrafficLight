"""The C transmit loop and packer (native.c), when they are built for this machine.

scripts/install_pi.sh builds _native-<machine>.so next to this file; on Windows, or wherever there is
no build, ``load()`` returns None and the driver keeps its Python loop and numpy packer. ctypes lets go
of the interpreter lock for the length of each call, which is the point.
"""
from __future__ import annotations

import ctypes
import os
import platform
import socket
import sys
from pathlib import Path
from typing import Optional

import numpy as np

ABI = 4
SOURCE = Path(__file__).with_name("native.c")


def library_path() -> Path:
    return SOURCE.with_name(f"_native-{platform.machine()}.so")


class Native:
    def __init__(self, lib: ctypes.CDLL):
        f = lib.tl_send_paced
        f.restype = ctypes.c_int
        f.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int64, ctypes.c_void_p, ctypes.c_int,
                      ctypes.c_uint32, ctypes.c_uint16, ctypes.c_int, ctypes.c_int64,
                      ctypes.POINTER(ctypes.c_int64), ctypes.POINTER(ctypes.c_int64), ctypes.POINTER(ctypes.c_int64)]
        self._send = f
        self._min_gap = ctypes.c_int64(-1)
        self._last = ctypes.c_int64(0)          # when the last group went, for the next call's first gap
        self._backlogs = ctypes.c_int64(0)
        p = lib.tl_pack
        p.restype = None
        p.argtypes = [ctypes.c_void_p] * 4 + [ctypes.c_int] * 3 + [ctypes.c_void_p]
        self._pack = p

    @property
    def backlogs(self) -> int:
        """How often a group waited for the one before to leave the system (native.c)."""
        return self._backlogs.value

    def pack(self, image: np.ndarray, index: np.ndarray, lut: np.ndarray, err: Optional[np.ndarray],
             rows: int, cols: int, planes: int, out: np.ndarray) -> None:
        """Packer.pack's work; index int32 (pack.lane_index, every entry < image.size: the caller checks
        once), err None for no dithering."""
        for a, dtype in ((image, np.uint8), (index, np.int32), (lut, np.uint16), (out, np.uint8)):
            assert a.dtype == dtype and a.flags.c_contiguous
        assert index.size == rows * cols * 48 and lut.size == 256 and out.size == planes * rows * cols * 6
        if err is not None:
            assert err.dtype == np.uint16 and err.flags.c_contiguous and err.size == index.size
        self._pack(image.ctypes.data, index.ctypes.data, lut.ctypes.data,
                   err.ctypes.data if err is not None else None, rows, cols, planes, out.ctypes.data)

    def send_paced(self, fd: int, rows: np.ndarray, lengths: np.ndarray, addr: tuple[str, int],
                   burst: int, gap_s: float) -> Optional[float]:
        """Datagram i is rows[i, :lengths[i]]. The first group waits out the gap after the last group
        of the call before. Returns the shortest gap kept (seconds), None if no gap was waited. Raises
        OSError for a failed send."""
        assert rows.flags.c_contiguous and lengths.dtype == np.uint32 and lengths.flags.c_contiguous
        ip = int.from_bytes(socket.inet_aton(addr[0]), sys.byteorder)      # in_addr: network order in memory
        port = int.from_bytes(addr[1].to_bytes(2, "big"), sys.byteorder)
        self._min_gap.value = -1
        r = self._send(fd, rows.ctypes.data, rows.strides[0], lengths.ctypes.data, len(lengths), ip, port,
                       burst, round(gap_s * 1e9), ctypes.byref(self._last), ctypes.byref(self._min_gap),
                       ctypes.byref(self._backlogs))
        if r < 0:
            raise OSError(-r, os.strerror(-r))
        return self._min_gap.value / 1e9 if self._min_gap.value >= 0 else None


def load(path: Optional[Path] = None) -> Optional[Native]:
    """The built library, or None (not Linux, not built, or built for another ABI)."""
    if not sys.platform.startswith("linux"):
        return None
    path = path or library_path()
    try:
        lib = ctypes.CDLL(str(path))
        if lib.tl_abi() != ABI:
            return None
    except (OSError, AttributeError):
        return None
    return Native(lib)
