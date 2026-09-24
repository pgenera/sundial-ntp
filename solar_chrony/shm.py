"""NTP shared-memory refclock writer (the protocol ntpd, gpsd and chrony use).

chronyd creates the segment (`refclock SHM <unit>:perm=0660`); we attach to
it and post samples: "at system time T, the reference (the sun) said T'".
"""

import ctypes
import math

NTP_SHM_BASE = 0x4E545030          # "NTP0"
SEGMENT_SIZE = 96                  # sizeof(struct shmTime) on 64-bit Linux

# struct shmTime field offsets (x86-64 / aarch64).
_MODE, _COUNT = 0, 4
_CLOCK_SEC, _CLOCK_USEC = 8, 16
_RECV_SEC, _RECV_USEC = 24, 32
_LEAP, _PRECISION, _NSAMPLES, _VALID = 36, 40, 44, 48
_CLOCK_NSEC, _RECV_NSEC = 52, 56

_libc = ctypes.CDLL(None, use_errno=True)
_libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
_libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
_libc.shmat.restype = ctypes.c_void_p
_libc.shmdt.argtypes = [ctypes.c_void_p]


class SegmentMissing(OSError):
    """chronyd has not created the segment (not running, or wrong unit)."""


class SHMWriter:
    def __init__(self, unit: int):
        self.key = NTP_SHM_BASE + unit
        shmid = _libc.shmget(self.key, SEGMENT_SIZE, 0)
        if shmid < 0:
            err = ctypes.get_errno()
            raise SegmentMissing(err, f"no NTP SHM segment for unit {unit} (key {self.key:#x})")
        addr = _libc.shmat(shmid, None, 0)
        if addr in (None, ctypes.c_void_p(-1).value):
            err = ctypes.get_errno()
            raise OSError(err, f"cannot attach NTP SHM segment {self.key:#x}")
        self._base = addr

    def _int(self, off):
        return ctypes.c_int.from_address(self._base + off)

    def _i64(self, off):
        return ctypes.c_int64.from_address(self._base + off)

    def _uint(self, off):
        return ctypes.c_uint.from_address(self._base + off)

    def put(self, system_time: float, reference_time: float, precision: int = -1) -> None:
        """Post one sample using the mode-1 (count-bracketed) protocol."""
        self._int(_VALID).value = 0
        self._int(_COUNT).value += 1
        self._int(_MODE).value = 1
        for sec, usec, nsec, t in ((_CLOCK_SEC, _CLOCK_USEC, _CLOCK_NSEC, reference_time),
                                   (_RECV_SEC, _RECV_USEC, _RECV_NSEC, system_time)):
            whole = math.floor(t)
            frac = t - whole
            self._i64(sec).value = whole
            self._int(usec).value = int(frac * 1e6)
            self._uint(nsec).value = int(frac * 1e9)
        self._int(_LEAP).value = 0
        self._int(_PRECISION).value = precision
        self._int(_NSAMPLES).value = 3
        self._int(_COUNT).value += 1
        self._int(_VALID).value = 1

    def invalidate(self) -> None:
        self._int(_VALID).value = 0

    def close(self) -> None:
        if self._base:
            _libc.shmdt(self._base)
            self._base = None
