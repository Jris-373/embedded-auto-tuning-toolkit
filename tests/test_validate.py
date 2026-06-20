"""Test validate.py BIN address check and exit code mapping."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from validate import Checker


def test_bin_address_check_skips_elf():
    ck = Checker()
    cfg = {"build": {"binary": "firmware.elf"}, "flash": {"backend": "openocd"}}
    ck.check_bin_address(cfg)
    assert len(ck.errors) == 0


def test_bin_address_check_skips_dfu():
    ck = Checker()
    cfg = {"build": {"binary": "firmware.bin"}, "flash": {"backend": "dfu"}}
    ck.check_bin_address(cfg)
    assert len(ck.errors) == 0


def test_bin_address_check_requires_openocd_addr():
    ck = Checker()
    cfg = {
        "build": {"binary": "firmware.bin"},
        "flash": {"backend": "openocd", "openocd": {}},
    }
    ck.check_bin_address(cfg)
    assert len(ck.errors) == 1
    assert "flash.openocd.address" in ck.errors[0]


def test_bin_address_check_requires_jlink_addr():
    ck = Checker()
    cfg = {
        "build": {"binary": "firmware.bin"},
        "flash": {"backend": "jlink", "jlink": {}},
    }
    ck.check_bin_address(cfg)
    assert len(ck.errors) == 1
    assert "flash.jlink.address" in ck.errors[0]


def test_custom_is_valid_flash_backend():
    ck = Checker()
    ck.check_flash_backend({"flash": {"backend": "custom"}})
    assert len(ck.errors) == 0
