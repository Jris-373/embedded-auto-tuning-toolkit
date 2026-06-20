"""Test FlashBackend factory, command construction, capability declarations."""
import sys
from pathlib import Path
from unittest.mock import MagicMock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.backends import (
    FlashContext, VerifyMode, ResetMode,
    create_flash_backend, UnsupportedOperationError,
    OpenOCDBackend, JLinkBackend, STLinkBackend, DFUBackend, CustomFlashBackend,
    _jlink_validate_and_quote, _tcl_escape,
)
from lib.commands import CommandRunner

PROJECT_ROOT = Path("/tmp/project")
LOG_DIR = Path("/tmp/logs")


@pytest.fixture
def ctx():
    return FlashContext(round_number=1, project_root=PROJECT_ROOT, log_dir=LOG_DIR)


@pytest.fixture
def runner():
    r = MagicMock(spec=CommandRunner)
    r.run.return_value.command = []
    r.run.return_value.cwd = Path.cwd()
    r.run.return_value.returncode = 0
    return r


def test_openocd_inline_verify_and_reset():
    b = OpenOCDBackend("openocd", _min_config("openocd"), MagicMock(), MagicMock())
    assert b.verify_mode == VerifyMode.INLINE
    assert b.reset_mode == ResetMode.INLINE


def test_dfubackend_has_no_verify_or_reset():
    b = DFUBackend("dfu-util", _min_config("dfu"), MagicMock(), MagicMock())
    assert b.verify_mode == VerifyMode.NONE
    assert b.reset_mode == ResetMode.NONE


def test_jlink_loadbin_syntax(ctx, runner):
    cfg = _min_config("jlink")
    b = JLinkBackend("JLinkExe", cfg, runner, ctx)
    p = Path("fw.bin")
    script = b._generate_script(p)
    assert f'loadbin "{p}", 0x08000000' in script


def test_jlink_elf_syntax(ctx, runner):
    cfg = _min_config("jlink")
    b = JLinkBackend("JLinkExe", cfg, runner, ctx)
    p = Path("fw.elf")
    script = b._generate_script(p)
    assert f'loadfile "{p}"' in script


def test_jlink_rejects_unsafe_chars():
    with pytest.raises(ValueError, match="unsafe"):
        _jlink_validate_and_quote('/path/with"quote')


def test_jlink_rejects_newline():
    with pytest.raises(ValueError, match="unsafe"):
        _jlink_validate_and_quote("/path/with\nnewline")


def test_jlink_windows_space_path(ctx, runner):
    cfg = _min_config("jlink")
    b = JLinkBackend("JLinkExe", cfg, runner, ctx)
    script = b._generate_script(Path(r"C:\My Docs\fw.bin"))
    assert r'loadbin "C:\My Docs\fw.bin", 0x08000000' in script


def test_tcl_escape_braces():
    result = _tcl_escape("/path/{to}/file")
    assert result.startswith("{")
    assert result.endswith("}")
    assert "\\{" in result


def test_stlink_separate_verify():
    b = STLinkBackend("st-flash", _min_config("stlink"), MagicMock(), MagicMock())
    assert b.verify_mode == VerifyMode.SEPARATE
    assert b.reset_mode == ResetMode.SUPPORTED


def test_custom_placeholder_resolution(ctx, runner):
    cfg = {
        "flash": {
            "backend": "custom",
            "custom": {"flash_command": ["flasher", "--image", "{binary}"]},
        },
        "project": {"root": "."},
    }
    b = CustomFlashBackend("custom", cfg, runner, ctx)
    p = Path("fw.bin")
    args = b._resolve_command(p)
    assert args == ["flasher", "--image", str(p)]


def test_custom_rejects_unknown_placeholder(ctx, runner):
    cfg = {
        "flash": {
            "backend": "custom",
            "custom": {"flash_command": ["flasher", "{unknown}"]},
        },
        "project": {"root": "."},
    }
    b = CustomFlashBackend("custom", cfg, runner, ctx)
    with pytest.raises(ValueError, match="Unknown placeholder"):
        b._resolve_command(Path("/tmp/fw.bin"))


def test_backend_default_verify_raises(ctx, runner):
    b = DFUBackend("dfu-util", _min_config("dfu"), runner, ctx)
    with pytest.raises(UnsupportedOperationError):
        b.verify(Path("/tmp/fw.bin"))


def test_backend_default_reset_raises(ctx, runner):
    b = DFUBackend("dfu-util", _min_config("dfu"), runner, ctx)
    with pytest.raises(UnsupportedOperationError):
        b.reset()


def test_openocd_bin_requires_address(ctx, runner):
    cfg = _min_config("openocd")
    cfg["flash"]["openocd"]["address"] = None
    b = OpenOCDBackend("openocd", cfg, runner, ctx)
    with pytest.raises(ValueError, match="address"):
        b._flash_args(Path("/tmp/fw.bin"))


def test_jlink_bin_requires_address(ctx, runner):
    cfg = _min_config("jlink")
    cfg["flash"]["jlink"]["address"] = None
    b = JLinkBackend("JLinkExe", cfg, runner, ctx)
    with pytest.raises(ValueError, match="address"):
        b._generate_script(Path("/tmp/fw.bin"))


def _min_config(backend: str) -> dict:
    base = {
        "flash": {"verify": True, "allow_unverified": False, "allow_no_reset": False},
        "project": {"root": "."},
    }
    if backend == "openocd":
        base["flash"]["openocd"] = {
            "interface": "if.cfg", "target": "tgt.cfg",
            "address": "0x08000000", "extra_args": [],
        }
    elif backend == "jlink":
        base["flash"]["jlink"] = {
            "device": "STM32F407VG", "interface": "SWD",
            "speed": 4000, "address": "0x08000000",
        }
    elif backend == "stlink":
        base["flash"]["stlink"] = {"address": "0x08000000"}
    elif backend == "dfu":
        base["flash"]["dfu"] = {"vid": "0483", "pid": "df11", "alt": 0}
    return base
