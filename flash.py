#!/usr/bin/env python3
"""
flash.py — Build → Flash → Verify → Reset → Boot-wait pipeline.

Usage:
    python3 tools/flash.py [--config <path>] [--skip-build] [--skip-verify]
                            [--skip-boot-wait] [--round N]

Exit codes:
    0 — success
    1 — build / clean failed
    2 — flash failed (including inline verify failure)
    3 — separate verify failed
    4 — boot timeout
    5 — config / precondition error
"""

import argparse
import hashlib
import sys
import time
from pathlib import Path

import yaml

# Ensure tools/lib is importable
sys.path.insert(0, str(Path(__file__).resolve().parent))

from lib.commands import CommandRunner, CommandTimeoutError
from lib.builders import create_builder
from lib.backends import (
    create_flash_backend, FlashContext,
    VerifyMode, ResetMode, UnsupportedOperationError,
)
from lib.protocol import FrameParser


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def ensure_binary_exists(binary: Path):
    if not binary.exists():
        raise FileNotFoundError(f"Binary not found: {binary}")


def record_binary_metadata(binary: Path, ctx: FlashContext):
    size = binary.stat().st_size
    sha = hashlib.sha256(binary.read_bytes()).hexdigest()
    print(f"[flash] Binary: {binary}  size={size}  sha256={sha[:16]}...")
    log = ctx.log_dir / f"flash_r{ctx.round_number:02d}_build.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    log.write_text(f"size={size}\nsha256={sha}\npath={binary}\n")


def verify_flash(backend, binary: Path, config: dict) -> bool:
    if not config["flash"].get("verify", True):
        print("[flash] Verify disabled by configuration (flash.verify: false)")
        return True
    if backend.verify_mode == VerifyMode.INLINE:
        print("[flash] Verify: INLINE (completed during flash)")
        return True
    if backend.verify_mode == VerifyMode.SEPARATE:
        try:
            result = backend.verify(binary)
            if result.ok:
                print("[flash] Verify OK (separate readback)")
                return True
            print(f"[flash] Verify FAILED: {result.stderr[:200]}")
            return False
        except UnsupportedOperationError:
            if config["flash"].get("allow_unverified", False):
                print("[flash] WARNING: verify unsupported, proceeding")
                return True
            raise
    if backend.verify_mode == VerifyMode.NONE:
        if config["flash"].get("allow_unverified", False):
            print("[flash] WARNING: no verification available")
            return True
        raise UnsupportedOperationError(
            "Set flash.allow_unverified: true to proceed"
        )
    return False


def wait_for_boot_done(config: dict, timeout_ms: int) -> bool:
    from lib.backends import SerialBackend
    parser = FrameParser()
    backend = SerialBackend()
    try:
        backend.open(config)
    except Exception as e:
        print(f"[flash] Cannot open serial: {e}", file=sys.stderr)
        return False
    deadline = time.monotonic() + timeout_ms / 1000.0
    try:
        while time.monotonic() < deadline:
            chunk = backend.read(100)
            if not chunk:
                time.sleep(0.05)
                continue
            frames = parser.feed(chunk)
            for f in frames:
                if f.is_boot_done:
                    print("[flash] BOOT_DONE received")
                    return True
    finally:
        backend.close()
    return False


def _run(args) -> int:
    tool_dir = Path(__file__).resolve().parent
    config_path = Path(args.config).resolve()
    config = load_config(config_path)

    project_root = (config_path.parent / config["project"]["root"]).resolve()
    binary = (project_root / config["build"]["binary"]).resolve()
    log_dir = tool_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    if args.skip_verify:
        config["flash"]["verify"] = False

    ctx = FlashContext(
        round_number=args.round,
        project_root=project_root,
        log_dir=log_dir,
    )

    try:
        # ── 1. Build ──
        if args.skip_build:
            ensure_binary_exists(binary)
            print("[flash] Skipping build (--skip-build)")
        else:
            runner = CommandRunner()
            builder = create_builder(config, runner)
            if config["build"].get("clean_first", False):
                result = builder.clean(config, project_root)
                if result is not None and not result.ok:
                    print(f"[flash] CLEAN FAILED (exit {result.returncode})")
                    return 1
            result = builder.build(config, project_root)
            if not result.ok:
                print(f"[flash] BUILD FAILED (exit {result.returncode})")
                return 1
            print("[flash] Build OK")
    except CommandTimeoutError as e:
        print(f"[flash] BUILD TIMEOUT: {e}")
        return 1

    # ── 2. Binary metadata ──
    record_binary_metadata(binary, ctx)

    try:
        # ── 3. Flash ──
        runner = CommandRunner()
        backend = create_flash_backend(config, runner, ctx)
        result = backend.flash(binary)
        if not result.ok:
            print(f"[flash] FLASH FAILED (exit {result.returncode})")
            return 2
        print("[flash] Flash OK")

        # ── 4. Verify ──
        try:
            if not verify_flash(backend, binary, config):
                return 3
        except UnsupportedOperationError as e:
            print(f"[flash] {e}")
            return 5

        # ── 5. Reset ──
        if backend.reset_mode == ResetMode.SUPPORTED:
            result = backend.reset()
            if not result.ok:
                print(f"[flash] RESET FAILED (exit {result.returncode})")
                return 2
        elif backend.reset_mode == ResetMode.NONE:
            if not config["flash"].get("allow_no_reset", False):
                print("[flash] Backend cannot reset target. "
                      "Set flash.allow_no_reset: true to proceed.")
                return 5
    except CommandTimeoutError as e:
        print(f"[flash] FLASH/RESET TIMEOUT: {e}")
        return 2

    # ── 6. Boot wait ──
    if args.skip_boot_wait:
        print("[flash] Skipping BOOT_DONE wait (--skip-boot-wait)")
    else:
        try:
            if not wait_for_boot_done(config, config["flash"]["boot_timeout_ms"]):
                print("[flash] BOOT TIMEOUT")
                return 4
        except CommandTimeoutError as e:
            print(f"[flash] BOOT_WAIT TIMEOUT: {e}")
            return 4

    print(f"[flash] Flash cycle complete (round {ctx.round_number})")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Build → Flash → Verify pipeline")
    ap.add_argument("--config", default="tools/config.yaml")
    ap.add_argument("--skip-build", action="store_true")
    ap.add_argument("--skip-verify", action="store_true")
    ap.add_argument("--skip-boot-wait", action="store_true")
    ap.add_argument("--round", type=int, default=1)
    args = ap.parse_args()
    try:
        return _run(args)
    except (ValueError, FileNotFoundError, yaml.YAMLError) as e:
        print(f"[flash] Configuration error: {e}", file=sys.stderr)
        return 5


if __name__ == "__main__":
    sys.exit(main())
