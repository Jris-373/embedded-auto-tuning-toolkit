"""
protocol.py — Python-side frame parser for the tracepoint binary protocol.

Frame format (little-endian):
    ┌──────┬──────┬──────┬──────┬──────────────────…──────────────────┬──────┐
    │ 0xAA │ 0x55 │ seq  │count │ [id(2B)|type(1B)|val(4B)]*N         │ crc8 │
    └──────┴──────┴──────┴──────┴──────────────────…──────────────────┴──────┘

Special variable IDs:
    0x0001 — BOOT_DONE
    0x0002 — ERROR
    0x0003 — HEARTBEAT
"""

import struct
from dataclasses import dataclass, field
from enum import IntEnum
from typing import List, Optional, Tuple


class VarType(IntEnum):
    INT32  = 0x01
    UINT32 = 0x02
    FLOAT  = 0x03
    UINT16 = 0x04
    INT16  = 0x05

    @property
    def fmt_char(self) -> str:
        """struct.unpack format character."""
        return {
            VarType.INT32:  'i',
            VarType.UINT32: 'I',
            VarType.FLOAT:  'f',
            VarType.UINT16: 'H',
            VarType.INT16:  'h',
        }[self]

    @property
    def byte_width(self) -> int:
        """Number of bytes the value occupies (before padding)."""
        return 2 if self in (VarType.UINT16, VarType.INT16) else 4


class SpecialFrame(IntEnum):
    """VarID values that carry special semantics."""
    BOOT_DONE = 0x0001
    ERROR     = 0x0002
    HEARTBEAT = 0x0003


@dataclass
class VariableValue:
    """One decoded variable entry from a frame."""
    id: int
    type: VarType
    raw_bytes: bytes
    value: float   # all numeric types promoted to float for uniform analysis

    def __repr__(self) -> str:
        return f"Var(0x{self.id:04X}, {self.type.name}, {self.value})"


@dataclass
class ParsedFrame:
    """One fully decoded frame."""
    seq: int
    variables: List[VariableValue] = field(default_factory=list)
    special: Optional[int] = None      # SpecialFrame value or None
    error_code: Optional[int] = None   # for ERROR special frames
    raw_crc: int = 0
    computed_crc: int = 0
    crc_valid: bool = False

    @property
    def is_boot_done(self) -> bool:
        return self.special == SpecialFrame.BOOT_DONE

    @property
    def is_error(self) -> bool:
        return self.special == SpecialFrame.ERROR

    @property
    def is_heartbeat(self) -> bool:
        return self.special == SpecialFrame.HEARTBEAT

    @property
    def is_special(self) -> bool:
        return self.special is not None

    @property
    def is_data(self) -> bool:
        return self.special is None and len(self.variables) > 0

    def get(self, var_id: int) -> Optional[VariableValue]:
        """Get a variable by its ID."""
        for v in self.variables:
            if v.id == var_id:
                return v
        return None


# ---------------------------------------------------------------------------
# CRC-8-ATM (matches firmware tracepoint.h)
# ---------------------------------------------------------------------------
def crc8(data: bytes, poly: int = 0x07) -> int:
    """Compute CRC-8-ATM over bytes."""
    crc = 0x00
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 0x80:
                crc = ((crc << 1) ^ poly) & 0xFF
            else:
                crc = (crc << 1) & 0xFF
    return crc


# ---------------------------------------------------------------------------
# Decode helpers
# ---------------------------------------------------------------------------
def _decode_value(raw: bytes, vtype: VarType) -> float:
    """Decode raw bytes to a float.  All int types are widened."""
    if vtype == VarType.FLOAT:
        val, = struct.unpack('<f', raw)
        return float(val)
    elif vtype == VarType.INT32:
        val, = struct.unpack('<i', raw)
        return float(val)
    elif vtype == VarType.UINT32:
        val, = struct.unpack('<I', raw)
        return float(val)
    elif vtype == VarType.INT16:
        val, = struct.unpack('<h', raw[:2])
        return float(val)
    elif vtype == VarType.UINT16:
        val, = struct.unpack('<H', raw[:2])
        return float(val)
    raise ValueError(f"Unknown type: {vtype}")


# ---------------------------------------------------------------------------
# Frame-level constants
# ---------------------------------------------------------------------------
SYNC_1 = 0xAA
SYNC_2 = 0x55
HEADER_SIZE = 4          # sync1 + sync2 + seq + count
VAR_ENTRY_SIZE = 7       # id(2) + type(1) + value(4)
CRC_SIZE = 1
MIN_FRAME_SIZE = HEADER_SIZE + CRC_SIZE  # a frame with 0 variables
MAX_VARS = 16


# ---------------------------------------------------------------------------
# Parser state machine
# ---------------------------------------------------------------------------
class FrameParser:
    """
    Streaming frame parser with sync detection and CRC validation.

    Usage:
        parser = FrameParser()
        for chunk in serial_port:
            frames = parser.feed(chunk)
            for f in frames:
                handle(f)
    """

    def __init__(self, max_vars: int = MAX_VARS):
        self._buf = bytearray()
        self.max_vars = max_vars
        self.max_frame_size = HEADER_SIZE + max_vars * VAR_ENTRY_SIZE + CRC_SIZE

        # Stats
        self.total_bytes = 0
        self.good_frames = 0
        self.bad_crc_frames = 0
        self.sync_lost = 0

    def feed(self, chunk: bytes) -> List[ParsedFrame]:
        """
        Feed a chunk of bytes.  Returns any fully-parsed valid frames.
        Frames with bad CRC are counted in stats but NOT returned.
        """
        self._buf.extend(chunk)
        self.total_bytes += len(chunk)

        frames: List[ParsedFrame] = []

        while True:
            # Find sync header
            sync_idx = self._find_sync()
            if sync_idx < 0:
                # No sync found — keep last byte in case it's a partial sync_1
                if len(self._buf) > 0:
                    self._buf = self._buf[-1:]
                break

            # Discard bytes before sync
            if sync_idx > 0:
                self._buf = self._buf[sync_idx:]

            # Do we have enough data for a header?
            if len(self._buf) < HEADER_SIZE:
                break

            # Read count and compute expected frame size
            count = self._buf[3]
            if count > self.max_vars:
                # Corrupted count — skip this sync byte and re-scan
                self._buf = self._buf[2:]
                self.sync_lost += 1
                continue

            expected_size = HEADER_SIZE + count * VAR_ENTRY_SIZE + CRC_SIZE
            if len(self._buf) < expected_size:
                break   # wait for more data

            # Extract frame bytes
            frame_bytes = bytes(self._buf[:expected_size])

            # Parse
            frame = self._parse_frame(frame_bytes)
            self._buf = self._buf[expected_size:]

            if frame.crc_valid:
                self.good_frames += 1
                frames.append(frame)
            else:
                self.bad_crc_frames += 1
                # Don't append — silently drop bad frames

        return frames

    def _find_sync(self) -> int:
        """Return index of the sync header (0xAA 0x55) or -1."""
        for i in range(len(self._buf) - 1):
            if self._buf[i] == SYNC_1 and self._buf[i + 1] == SYNC_2:
                return i
        return -1

    def _parse_frame(self, raw: bytes) -> ParsedFrame:
        """Parse a single frame (already extracted from buffer)."""
        seq = raw[2]
        count = raw[3]
        payload = raw[:HEADER_SIZE + count * VAR_ENTRY_SIZE]
        raw_crc = raw[-1]
        computed_crc = crc8(payload)
        crc_valid = (raw_crc == computed_crc)

        frame = ParsedFrame(
            seq=seq,
            raw_crc=raw_crc,
            computed_crc=computed_crc,
            crc_valid=crc_valid,
        )

        if not crc_valid:
            return frame

        # Parse variable entries
        for i in range(count):
            offset = HEADER_SIZE + i * VAR_ENTRY_SIZE
            var_id = raw[offset] | (raw[offset + 1] << 8)
            var_type_byte = raw[offset + 2]
            var_type_raw = VarType(var_type_byte) if var_type_byte in (1, 2, 3, 4, 5) else None
            raw_val = raw[offset + 3 : offset + 7]

            # Special frame?
            if var_id in (SpecialFrame.BOOT_DONE, SpecialFrame.ERROR, SpecialFrame.HEARTBEAT):
                frame.special = var_id
                if var_id == SpecialFrame.ERROR:
                    # error_code is encoded in the low 16 bits of the value
                    frame.error_code = raw_val[0] | (raw_val[1] << 8)
            elif var_type_raw is not None:
                try:
                    value = _decode_value(raw_val, var_type_raw)
                    frame.variables.append(VariableValue(
                        id=var_id,
                        type=var_type_raw,
                        raw_bytes=raw_val,
                        value=value,
                    ))
                except (struct.error, ValueError):
                    pass   # skip malformed entry

        return frame

    @property
    def error_rate(self) -> float:
        total = self.good_frames + self.bad_crc_frames
        if total == 0:
            return 0.0
        return self.bad_crc_frames / total


# ---------------------------------------------------------------------------
# Frame builder (host → target commands)
# ---------------------------------------------------------------------------
def build_set_param_frame(param_byte_offset: int, value_bytes: bytes) -> bytes:
    """
    Build a host-to-target SET_PARAM command frame.
    Uses the same binary framing so the target can reuse its parser.

    Command ID 0x0100 = SET_PARAM.
    """
    cmd_id = 0x0100
    data = bytearray()
    data.append(SYNC_1)
    data.append(SYNC_2)
    data.append(0x00)                             # seq (host sets 0)
    data.append(0x01)                             # count = 1
    data.append(cmd_id & 0xFF)                    # var_id lo
    data.append((cmd_id >> 8) & 0xFF)             # var_id hi
    data.append(0x01)                             # type = custom
    data.append(param_byte_offset & 0xFF)         # value[0]: offset lo
    data.append(0x00)                             # value[1]: offset hi
    data.append(value_bytes[0])                   # value[2]: payload start
    # pad or fill remaining bytes as needed
    for i in range(1, 4):
        data.append(value_bytes[i] if i < len(value_bytes) else 0x00)
    crc = crc8(bytes(data))
    data.append(crc)
    return bytes(data)
