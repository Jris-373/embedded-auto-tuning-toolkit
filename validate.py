#!/usr/bin/env python3
"""
validate.py — Pre-flight precondition checker.

Checks config integrity, toolchain availability, file paths, serial port
access, and protocol consistency between config.yaml and tracepoint.h.

Exit 0: all checks passed (warnings are non-fatal)
Exit 1: errors found — fix before running loop_runner.py
"""

import argparse
import re
import shutil
import sys
from pathlib import Path
from typing import List, Tuple

import yaml

# Add tools/lib to path
sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


class Checker:
    def __init__(self, config_dir: Path | None = None):
        self.errors: List[str] = []
        self.warnings: List[str] = []
        self._config_dir = config_dir

    def error(self, msg: str):
        self.errors.append(f"  ERROR: {msg}")

    def warn(self, msg: str):
        self.warnings.append(f"  WARN:  {msg}")

    def _resolve_root(self, cfg: dict) -> Path:
        """Resolve project.root relative to the config file directory."""
        root_str = cfg.get("project", {}).get("root", ".")
        if self._config_dir:
            return (self._config_dir / root_str).resolve()
        return Path(root_str).resolve()

    def check_config_exists(self, path: str):
        if not Path(path).exists():
            self.error(f"Config file not found: {path}")
            return False
        try:
            load_config(path)
        except yaml.YAMLError as e:
            self.error(f"Invalid YAML in {path}: {e}")
            return False
        return True

    def check_variable_ids_unique(self, cfg: dict):
        ids = [v["id"] for v in cfg.get("variables", [])]
        seen = set()
        for vid in ids:
            if vid in seen:
                self.error(f"Duplicate variable id: 0x{vid:04X}")
            seen.add(vid)

    def check_parameter_depends_on(self, cfg: dict):
        var_ids = {v["id"] for v in cfg.get("variables", [])}
        for p in cfg.get("parameters", []):
            for dep_id in p.get("depends_on", []):
                if dep_id not in var_ids:
                    self.error(
                        f"Parameter '{p['name']}' depends_on 0x{dep_id:04X} "
                        f"which is not defined in variables"
                    )

    def check_flash_backend(self, cfg: dict):
        valid = {"openocd", "jlink", "stlink", "dfu", "custom"}
        backend = cfg.get("flash", {}).get("backend", "")
        if backend not in valid:
            self.error(f"flash.backend '{backend}' not in {valid}")

    def check_toolchain(self, cfg: dict):
        build_system = cfg.get("build", {}).get("system", "")
        if build_system == "make":
            explicit = cfg.get("build", {}).get("make_executable")
            if explicit:
                if not Path(explicit).exists():
                    self.error(f"make executable not found at configured path: {explicit}")
            elif not (shutil.which("make") or shutil.which("mingw32-make.exe")):
                self.error("make not found in PATH (checked: make, mingw32-make.exe)")
        elif build_system == "cmake":
            explicit = cfg.get("build", {}).get("cmake_executable")
            if explicit:
                if not Path(explicit).exists():
                    self.error(f"cmake executable not found at configured path: {explicit}")
            elif not shutil.which("cmake"):
                self.error("cmake not found in PATH")

        backend = cfg.get("flash", {}).get("backend", "")
        backend_cfg = cfg.get("flash", {}).get(backend, {})
        explicit = backend_cfg.get("executable") if backend_cfg else None
        tool_map = {
            "openocd": ("openocd", None),
            "jlink": ("JLinkExe", "JLink.exe"),
            "stlink": ("st-flash", None),
            "dfu": ("dfu-util", None),
        }
        entry = tool_map.get(backend)
        if explicit:
            if not Path(explicit).exists():
                self.error(
                    f"Flash backend '{backend}' executable not found "
                    f"at configured path: {explicit}"
                )
        elif entry and backend != "custom":
            primary, alt = entry
            if not (shutil.which(primary) or (alt and shutil.which(alt))):
                names = f"'{primary}'" if not alt else f"'{primary}' / '{alt}'"
                self.error(f"Flash backend '{backend}' requires {names} in PATH")

    def check_python_deps(self):
        for mod in ["serial", "yaml"]:
            try:
                __import__(mod)
            except ImportError:
                self.error(f"Python module '{mod}' not installed (pip install pyserial pyyaml)")

    def check_project_root(self, cfg: dict):
        root = self._resolve_root(cfg)
        if not root.exists():
            self.error(f"Project root not found: {root}")

    def check_parameter_files(self, cfg: dict):
        root = self._resolve_root(cfg)
        for p in cfg.get("parameters", []):
            fpath = root / p["file"]
            if not fpath.exists():
                self.warn(f"Parameter file not found: {fpath}")
                continue
            content = fpath.read_text(encoding="utf-8")
            if not re.search(p["pattern"], content):
                self.warn(f"Pattern for '{p['name']}' matches nothing in {fpath}")

    def check_bin_address(self, cfg: dict):
        binary = Path(cfg["build"]["binary"])
        if binary.suffix.lower() != ".bin":
            return
        backend = cfg["flash"]["backend"]
        if backend not in {"openocd", "jlink", "stlink"}:
            return
        addr = cfg["flash"].get(backend, {}).get("address")
        if not addr:
            self.error(f"BIN image requires flash.{backend}.address")

    def check_serial_port(self, cfg: dict):
        port = cfg.get("serial", {}).get("port", "")
        if not port:
            self.warn("serial.port is empty")
            return
        if not Path(port).exists():
            self.warn(f"Serial port device not found: {port}")
            return
        try:
            import serial
            ser = serial.Serial(port=port, baudrate=cfg["serial"].get("baudrate", 115200),
                                timeout=0.1)
            ser.close()
        except Exception as e:
            self.warn(f"Cannot open serial port {port}: {e}")

    def check_protocol_consistency(self, cfg: dict):
        """Verify config.yaml protocol section matches tracepoint.h."""
        proto = cfg.get("protocol", {})
        tp_header = Path(__file__).resolve().parent / "firmware" / "tracepoint.h"
        if not tp_header.exists():
            self.warn(f"tracepoint.h not found at {tp_header} — skipping consistency check")
            return
        content = tp_header.read_text(encoding="utf-8")

        sync1 = proto.get("sync_byte_1", 0xAA)
        sync2 = proto.get("sync_byte_2", 0x55)
        if f"TP_SYNC_1 {sync1:#04X}" not in content.replace("0X", "0x").replace("0xAA", "0xAA"):
            # Check if the hex values match
            m = re.search(r'TP_SYNC_1\s+(\w+)', content)
            if m:
                tp_val = int(m.group(1), 16) if m.group(1).startswith("0x") else int(m.group(1))
                if tp_val != sync1:
                    self.error(
                        f"Protocol sync_byte_1 mismatch: config={sync1:#04x}, "
                        f"tracepoint.h={tp_val:#04x}"
                    )

        crc_poly = proto.get("crc_poly", 0x07)
        m = re.search(r'\^\s*(0x[0-9a-fA-F]+)', content)
        if m:
            tp_poly = int(m.group(1), 16)
            if tp_poly != crc_poly:
                self.error(
                    f"CRC polynomial mismatch: config={crc_poly:#04x}, "
                    f"tracepoint.h={tp_poly:#04x}"
                )


def main():
    ap = argparse.ArgumentParser(description="Pre-flight validation for auto-tuning toolkit")
    ap.add_argument("--config", default="tools/config.yaml")
    args = ap.parse_args()

    config_path = Path(args.config).resolve()
    ck = Checker(config_dir=config_path.parent)

    print("[validate] Checking...")

    # 1. Config file integrity
    if not ck.check_config_exists(str(config_path)):
        print("\n".join(ck.errors))
        sys.exit(1)

    cfg = load_config(str(config_path))

    # 2. Variable IDs
    ck.check_variable_ids_unique(cfg)

    # 3. Parameter dependencies
    ck.check_parameter_depends_on(cfg)

    # 4. Flash backend
    ck.check_flash_backend(cfg)

    # 5. Toolchain
    ck.check_toolchain(cfg)

    # 6. Python deps
    ck.check_python_deps()

    # 7. Project root
    ck.check_project_root(cfg)

    # 8. Parameter files
    ck.check_parameter_files(cfg)

    # 9. BIN address requirement
    ck.check_bin_address(cfg)

    # 10. Serial port (best-effort)
    ck.check_serial_port(cfg)

    # 11. Protocol consistency
    ck.check_protocol_consistency(cfg)

    # Report
    if ck.warnings:
        print("\n".join(ck.warnings))

    if ck.errors:
        print("\n".join(ck.errors))
        print(f"\n[validate] {len(ck.errors)} error(s) found. Fix before running loop_runner.py.")
        sys.exit(1)

    print("[validate] OK — all checks passed")
    sys.exit(0)


if __name__ == "__main__":
    main()
