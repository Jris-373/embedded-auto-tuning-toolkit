"""
backends.py — Abstract monitor and flash backends.

UNVERIFIED: All backend registries are placeholder.  Future backends
(RTT, SWO, CAN, TCP, CMSIS-DAP, Bootloader, picoprobe, ESP32) are
named in comments but not implemented.

Concrete implementations:
    SerialBackend — UART / USB CDC via pyserial
    FileBackend  — Replay pre-recorded binary log files (for simulate.py)
"""

from abc import ABC, abstractmethod
from typing import Optional
import time


class MonitorBackend(ABC):
    """Abstract interface for telemetry data sources."""

    @abstractmethod
    def open(self, config: dict) -> None:
        """Open the monitor channel.  Raise on failure."""

    @abstractmethod
    def read(self, timeout_ms: int) -> bytes:
        """Read up to timeout_ms milliseconds.  Return empty bytes on timeout."""

    @abstractmethod
    def close(self) -> None:
        """Release the channel."""


class SerialBackend(MonitorBackend):
    """Monitor via UART / USB CDC serial port."""

    def __init__(self):
        self._ser = None

    def open(self, config: dict) -> None:
        import serial
        s = config["serial"]
        self._ser = serial.Serial(
            port=s["port"],
            baudrate=s["baudrate"],
            bytesize=s.get("data_bits", 8),
            parity=s.get("parity", "N"),
            stopbits=s.get("stop_bits", 1),
            timeout=s.get("timeout_ms", 1000) / 1000.0,
        )
        self._ser.reset_input_buffer()

    def read(self, timeout_ms: int) -> bytes:
        if self._ser is None:
            return b""
        self._ser.timeout = timeout_ms / 1000.0
        chunk = self._ser.read(256)
        return chunk if chunk else b""

    def close(self) -> None:
        if self._ser is not None:
            self._ser.close()
            self._ser = None


class FileBackend(MonitorBackend):
    """Replay binary log for offline simulation."""

    def __init__(self):
        self._fh = None
        self._start_time: float = 0.0
        self._bytes_read: int = 0

    def open(self, config: dict) -> None:
        path = config.get("_file_path", "")
        if not path:
            raise ValueError("FileBackend requires _file_path in config")
        self._fh = open(path, "rb")
        self._start_time = time.monotonic()

    def read(self, timeout_ms: int) -> bytes:
        if self._fh is None:
            return b""
        # Simulate real-time pacing: read up to what would have arrived
        elapsed = time.monotonic() - self._start_time
        # Simple approach: read a chunk
        data = self._fh.read(256)
        return data if data else b""

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


class FlashBackend(ABC):
    """Abstract interface for build + flash + verify.  Not yet used by
    Python code (flash.sh remains shell), but defined for future migration."""

    @abstractmethod
    def build(self, config: dict) -> bool: ...

    @abstractmethod
    def flash(self, config: dict, binary_path: str) -> bool: ...

    @abstractmethod
    def verify(self, config: dict, binary_path: str) -> bool: ...

    @abstractmethod
    def reset(self) -> bool: ...
