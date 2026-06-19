#!/usr/bin/env python3
"""
monitor.py — Serial frame capture and CSV logging.

Connects to the target's serial port, syncs to the binary frame protocol,
decodes all frames, and writes timestamped readings to a per-round CSV.

Usage:
    python3 tools/monitor.py [--config tools/config.yaml] [--round 1]
    python3 tools/monitor.py --once          # capture once, print to stdout
    python3 tools/monitor.py --list-ports    # enumerate available serial ports

Output CSV columns:
    timestamp_ms, var_id, var_name, value, frame_seq, crc_valid
"""

import argparse
import csv
import os
import sys
import time
from pathlib import Path
from typing import Dict, Optional

import serial
import serial.tools.list_ports
import yaml

# Add tools/lib to import path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.protocol import FrameParser, ParsedFrame, SpecialFrame


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def build_var_index(cfg: dict) -> Dict[int, dict]:
    """Build a lookup table: var_id → {name, type, expected, warn, emergency}."""
    idx = {}
    for v in cfg.get("variables", []):
        idx[v["id"]] = v
    return idx


def list_ports():
    """Print available serial ports."""
    ports = serial.tools.list_ports.comports()
    if not ports:
        print("No serial ports found.")
        return
    for p in ports:
        print(f"  {p.device} — {p.description} [{p.hwid}]")


def open_serial(cfg: dict) -> serial.Serial:
    s = cfg["serial"]
    ser = serial.Serial(
        port=s["port"],
        baudrate=s["baudrate"],
        bytesize=s.get("data_bits", 8),
        parity=s.get("parity", "N"),
        stopbits=s.get("stop_bits", 1),
        timeout=s.get("timeout_ms", 1000) / 1000.0,
    )
    # Discard any stale data in the buffer
    ser.reset_input_buffer()
    return ser


def monitor_once(cfg: dict, var_index: Dict[int, dict]) -> None:
    """Capture a single frame stream and print to stdout."""
    parser = FrameParser()
    ser = open_serial(cfg)
    deadline = time.monotonic() + (cfg["monitor"]["duration_ms"] / 1000.0)

    print(f"{'timestamp_ms':>12}  {'var_id':>6}  {'var_name':>16}  {'value':>12}  {'seq':>4}  crc")
    print("-" * 70)

    try:
        while time.monotonic() < deadline:
            chunk = ser.read(256)
            if not chunk:
                continue
            frames = parser.feed(chunk)
            for f in frames:
                ts = int(time.monotonic() * 1000)
                if f.is_boot_done:
                    print(f"{ts:>12}  {'—':>6}  {'BOOT_DONE':>16}  {'—':>12}  {f.seq:>4}  ✓")
                elif f.is_error:
                    print(f"{ts:>12}  {'—':>6}  {'ERROR':>16}  {'—':>12}  {f.seq:>4}  ✓")
                elif f.is_data:
                    for v in f.variables:
                        name = var_index.get(v.id, {}).get("name", f"0x{v.id:04X}")
                        print(f"{ts:>12}  {v.id:#06x}  {name:>16}  {v.value:>12.4f}  {f.seq:>4}  {'✓' if f.crc_valid else '✗'}")
    finally:
        ser.close()

    print(f"\nFrames: {parser.good_frames} good  {parser.bad_crc_frames} bad  ({parser.error_rate*100:.1f}% error rate)")


def monitor_to_csv(cfg: dict, var_index: Dict[int, dict], round_num: int) -> str:
    """
    Capture monitoring data to a CSV file.
    Returns the path to the CSV file.
    """
    monitor_cfg = cfg["monitor"]
    duration_s = monitor_cfg["duration_ms"] / 1000.0
    warmup_s = monitor_cfg.get("warmup_ms", 0) / 1000.0
    csv_dir = Path(monitor_cfg["csv_dir"])
    csv_dir.mkdir(parents=True, exist_ok=True)

    csv_path = csv_dir / f"monitor_r{round_num:02d}.csv"

    parser = FrameParser()
    ser = open_serial(cfg)

    # Collect frames during warmup but discard them
    boot_done_seen = False
    boot_deadline = time.monotonic() + (cfg["flash"]["boot_timeout_ms"] / 1000.0)

    print(f"[monitor] Waiting for BOOT_DONE (timeout={cfg['flash']['boot_timeout_ms']}ms)...")

    try:
        # --- Boot wait ---
        while time.monotonic() < boot_deadline:
            chunk = ser.read(64)
            if not chunk:
                continue
            frames = parser.feed(chunk)
            for f in frames:
                if f.is_boot_done:
                    boot_done_seen = True
                    boot_ts = time.monotonic()
                    print(f"[monitor] BOOT_DONE received at t={boot_ts:.3f}")
                    break
            if boot_done_seen:
                break

        if not boot_done_seen:
            print("[monitor] WARNING: BOOT_DONE not received, capturing anyway", file=sys.stderr)

        # --- Warmup ---
        warmup_deadline = time.monotonic() + warmup_s
        if warmup_s > 0:
            print(f"[monitor] Warmup: discarding first {warmup_s}s of data...")
            while time.monotonic() < warmup_deadline:
                chunk = ser.read(256)
                if chunk:
                    parser.feed(chunk)  # parse but don't log
                else:
                    time.sleep(0.01)

        # --- Capture ---
        deadline = time.monotonic() + duration_s
        print(f"[monitor] Capturing {duration_s}s → {csv_path}")

        safety_violations = []

        with open(csv_path, "w", newline="") as csvfile:
            writer = csv.writer(csvfile)
            writer.writerow(["timestamp_ms", "var_id", "var_name", "value", "frame_seq", "crc_valid"])

            while time.monotonic() < deadline:
                chunk = ser.read(256)
                if not chunk:
                    time.sleep(0.01)
                    continue

                frames = parser.feed(chunk)
                for f in frames:
                    ts = int((time.monotonic() - boot_ts) * 1000) if boot_done_seen else int(time.monotonic() * 1000)

                    if f.is_data:
                        for v in f.variables:
                            var_info = var_index.get(v.id, {})
                            name = var_info.get("name", f"0x{v.id:04X}")
                            writer.writerow([ts, f"0x{v.id:04X}", name, f"{v.value:.6f}", f.seq, f.crc_valid])

                            # Safety check
                            emergency = var_info.get("emergency", {})
                            if emergency:
                                emin, emax = emergency.get("min"), emergency.get("max")
                                if (emin is not None and v.value < emin) or (emax is not None and v.value > emax):
                                    safety_violations.append(f"EMERGENCY: {name}={v.value} bounds=[{emin},{emax}]")

        # --- Post-capture summary ---
        frame_err_rate = parser.error_rate
        threshold = monitor_cfg.get("frame_error_threshold", 0.05)
        print(f"[monitor] Done.  Frames: {parser.good_frames} good  {parser.bad_crc_frames} bad  ({frame_err_rate*100:.1f}% err)")

        if frame_err_rate > threshold:
            print(f"[monitor] WARNING: frame error rate {frame_err_rate*100:.1f}% exceeds threshold {threshold*100:.1f}%")

        if safety_violations:
            print("[monitor] !!! SAFETY VIOLATIONS !!!")
            for sv in safety_violations:
                print(f"  {sv}")

    finally:
        ser.close()

    return str(csv_path)


def main():
    parser = argparse.ArgumentParser(description="Serial frame monitor for embedded auto-tuning")
    parser.add_argument("--config", default="tools/config.yaml", help="Path to config.yaml")
    parser.add_argument("--round", type=int, default=1, help="Round number (for CSV naming)")
    parser.add_argument("--once", action="store_true", help="Capture once and print to stdout")
    parser.add_argument("--list-ports", action="store_true", help="List available serial ports and exit")
    args = parser.parse_args()

    if args.list_ports:
        list_ports()
        return

    cfg = load_config(args.config)
    var_index = build_var_index(cfg)

    if args.once:
        monitor_once(cfg, var_index)
    else:
        csv_path = monitor_to_csv(cfg, var_index, args.round)
        print(f"CSV: {csv_path}")


if __name__ == "__main__":
    main()
