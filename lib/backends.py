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


# ========================================================================
# FlashBackend — build/flash/verify/reset abstraction
# ========================================================================

from enum import Enum
from dataclasses import dataclass
from pathlib import Path
import shutil
import sys
from abc import ABC, abstractmethod
from .commands import CommandRunner, CommandResult


class VerifyMode(Enum):
    SEPARATE = "separate"   # Independent readback compare
    INLINE   = "inline"     # Flash command includes verify
    NONE     = "none"       # Cannot verify


class ResetMode(Enum):
    SUPPORTED = "supported" # Independent reset command available
    INLINE    = "inline"    # Flash command already resets
    NONE      = "none"      # Cannot reset


class UnsupportedOperationError(RuntimeError):
    """Backend does not support this operation."""
    pass


@dataclass(frozen=True)
class FlashContext:
    round_number: int
    project_root: Path
    log_dir: Path

    def log_path(self, stem: str) -> Path:
        return self.log_dir / f"flash_r{self.round_number:02d}_{stem}.log"


def _resolve_exe(explicit: str | None, primary: str,
                 windows_alt: str | None = None) -> str:
    if explicit:
        return explicit
    found = shutil.which(primary)
    if found:
        return found
    if sys.platform == "win32" and windows_alt:
        found = shutil.which(windows_alt)
        if found:
            return found
    raise FileNotFoundError(f"{primary} not found in PATH")


def _tcl_escape(path: str) -> str:
    """Escape a path for OpenOCD Tcl string literal."""
    return "{" + path.replace("{", "\\{").replace("}", "\\}") + "}"


def _jlink_validate_and_quote(path: str) -> str:
    """Quote a path for J-Link Commander script.

    Rejects paths containing double-quote or newline characters.
    """
    if '"' in path or '\n' in path or '\r' in path:
        raise ValueError(f"Path contains unsafe characters: {path}")
    return f'"{path}"'


ALLOWED_PLACEHOLDERS = {"{binary}", "{project_root}"}


class FlashBackend(ABC):
    verify_mode: VerifyMode = VerifyMode.NONE
    reset_mode: ResetMode = ResetMode.NONE

    def __init__(self, executable: str, config: dict,
                 runner: CommandRunner, context: FlashContext):
        self._exe = executable
        self._cfg = config
        self._runner = runner
        self._ctx = context

    @abstractmethod
    def flash(self, binary: Path) -> CommandResult: ...

    def verify(self, binary: Path) -> CommandResult:
        raise UnsupportedOperationError(
            f"{self.__class__.__name__} does not support separate verify. "
            f"Set flash.allow_unverified: true to proceed."
        )

    def reset(self) -> CommandResult:
        raise UnsupportedOperationError(
            f"{self.__class__.__name__} does not support reset. "
            f"Set flash.allow_no_reset: true to proceed."
        )


class OpenOCDBackend(FlashBackend):
    verify_mode = VerifyMode.INLINE
    reset_mode  = ResetMode.INLINE

    def _flash_args(self, binary: Path) -> list[str]:
        cfg = self._cfg["flash"]["openocd"]
        verify = "verify" if self._cfg["flash"].get("verify", True) else ""
        escaped = _tcl_escape(str(binary))
        if binary.suffix.lower() == ".bin":
            addr = cfg.get("address")
            if not addr:
                raise ValueError("BIN image requires flash.openocd.address")
            program_cmd = f"program {escaped} {addr} {verify} reset exit"
        else:
            program_cmd = f"program {escaped} {verify} reset exit"
        return [
            self._exe,
            "-f", cfg["interface"],
            "-f", cfg["target"],
            *cfg.get("extra_args", []),
            "-c", program_cmd,
        ]

    def flash(self, binary: Path) -> CommandResult:
        args = self._flash_args(binary)
        return self._runner.run(args, cwd=self._ctx.project_root,
                                log_path=self._ctx.log_path("openocd"))


class JLinkBackend(FlashBackend):
    verify_mode = VerifyMode.INLINE
    reset_mode  = ResetMode.INLINE

    def _generate_script(self, binary: Path) -> str:
        cfg = self._cfg["flash"]["jlink"]
        qpath = _jlink_validate_and_quote(str(binary))
        ext = binary.suffix.lower()
        if ext in (".elf", ".axf", ".hex"):
            load_cmd = f"loadfile {qpath}"
        elif ext == ".bin":
            addr = cfg.get("address")
            if not addr:
                raise ValueError("BIN image requires flash.jlink.address")
            load_cmd = f"loadbin {qpath}, {addr}"
        else:
            raise ValueError(f"Unsupported image format: {ext}")
        return (
            f"device {cfg['device']}\n"
            f"si {cfg['interface']}\n"
            f"speed {cfg['speed']}\n"
            f"r\nh\n{load_cmd}\nr\ng\nexit\n"
        )

    def flash(self, binary: Path) -> CommandResult:
        script_path = self._ctx.log_dir / f"flash_r{self._ctx.round_number:02d}.jlink"
        script_path.parent.mkdir(parents=True, exist_ok=True)
        script_path.write_text(self._generate_script(binary))
        return self._runner.run(
            [self._exe, "-CommanderScript", str(script_path)],
            cwd=self._ctx.project_root,
            log_path=self._ctx.log_path("jlink"),
        )


class STLinkBackend(FlashBackend):
    verify_mode = VerifyMode.SEPARATE
    reset_mode  = ResetMode.SUPPORTED

    def _flash_args(self, binary: Path) -> list[str]:
        addr = self._cfg["flash"]["stlink"]["address"]
        return [self._exe, "write", str(binary), addr]

    def flash(self, binary: Path) -> CommandResult:
        args = self._flash_args(binary)
        return self._runner.run(args, cwd=self._ctx.project_root,
                                log_path=self._ctx.log_path("stlink_flash"))

    def verify(self, binary: Path) -> CommandResult:
        addr = self._cfg["flash"]["stlink"]["address"]
        return self._runner.run(
            [self._exe, "verify", str(binary), addr],
            cwd=self._ctx.project_root,
            log_path=self._ctx.log_path("stlink_verify"),
        )

    def reset(self) -> CommandResult:
        return self._runner.run(
            [self._exe, "reset"],
            cwd=self._ctx.project_root,
            log_path=self._ctx.log_path("stlink_reset"),
        )


class DFUBackend(FlashBackend):
    verify_mode = VerifyMode.NONE
    reset_mode  = ResetMode.NONE

    def _flash_args(self, binary: Path) -> list[str]:
        cfg = self._cfg["flash"]["dfu"]
        return [
            self._exe,
            "-d", f"{cfg['vid']}:{cfg['pid']}",
            "-a", str(cfg["alt"]),
            "-D", str(binary),
        ]

    def flash(self, binary: Path) -> CommandResult:
        args = self._flash_args(binary)
        return self._runner.run(args, cwd=self._ctx.project_root,
                                log_path=self._ctx.log_path("dfu"))


class CustomFlashBackend(FlashBackend):
    verify_mode = VerifyMode.NONE
    reset_mode  = ResetMode.NONE

    def _resolve_command(self, binary: Path) -> list[str]:
        raw = self._cfg["flash"]["custom"]["flash_command"]
        if not raw:
            raise ValueError("flash.custom.flash_command must not be empty")
        if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
            raise ValueError("flash.custom.flash_command must be a string array")
        resolved: list[str] = []
        for token in raw:
            if token == "{binary}":
                resolved.append(str(binary))
            elif token == "{project_root}":
                resolved.append(str(self._ctx.project_root))
            elif token.startswith("{") and token.endswith("}"):
                raise ValueError(
                    f"Unknown placeholder '{token}'. "
                    f"Allowed: {', '.join(sorted(ALLOWED_PLACEHOLDERS))}"
                )
            else:
                resolved.append(token)
        return resolved

    def flash(self, binary: Path) -> CommandResult:
        args = self._resolve_command(binary)
        return self._runner.run(args, cwd=self._ctx.project_root,
                                log_path=self._ctx.log_path("custom"))


def create_flash_backend(config: dict, runner: CommandRunner,
                         context: FlashContext) -> FlashBackend:
    backend_name = config["flash"]["backend"]
    backend_cfg = config["flash"].get(backend_name, {})

    if backend_name == "openocd":
        exe = _resolve_exe(backend_cfg.get("executable"), "openocd")
        return OpenOCDBackend(exe, config, runner, context)
    elif backend_name == "jlink":
        exe = _resolve_exe(backend_cfg.get("executable"), "JLinkExe",
                           windows_alt="JLink.exe")
        return JLinkBackend(exe, config, runner, context)
    elif backend_name == "stlink":
        exe = _resolve_exe(backend_cfg.get("executable"), "st-flash")
        return STLinkBackend(exe, config, runner, context)
    elif backend_name == "dfu":
        exe = _resolve_exe(backend_cfg.get("executable"), "dfu-util")
        return DFUBackend(exe, config, runner, context)
    elif backend_name == "custom":
        exe = backend_cfg.get("executable", "custom")
        return CustomFlashBackend(exe, config, runner, context)
    else:
        raise ValueError(f"Unknown flash backend: {backend_name}")
