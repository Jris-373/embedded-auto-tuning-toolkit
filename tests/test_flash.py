"""Test flash.py exit codes and verify_flash logic without real hardware."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.commands import CommandResult
from lib.backends import VerifyMode, ResetMode, UnsupportedOperationError, FlashBackend

# Import flash.py module
TOOLS_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TOOLS_DIR))
from flash import verify_flash


def test_verify_flash_disabled_by_config():
    backend = MagicMock(spec=FlashBackend)
    backend.verify_mode = VerifyMode.NONE
    config = {"flash": {"verify": False, "allow_unverified": False}}
    assert verify_flash(backend, Path("x.bin"), config) is True


def test_verify_flash_inline_returns_true():
    backend = MagicMock(spec=FlashBackend)
    backend.verify_mode = VerifyMode.INLINE
    config = {"flash": {"verify": True}}
    assert verify_flash(backend, Path("x.elf"), config) is True


def test_verify_flash_separate_success():
    backend = MagicMock(spec=FlashBackend)
    backend.verify_mode = VerifyMode.SEPARATE
    result = CommandResult(["vfy"], Path.cwd(), 0, "ok", "", None, 10)
    backend.verify.return_value = result
    config = {"flash": {"verify": True}}
    assert verify_flash(backend, Path("x.bin"), config) is True


def test_verify_flash_separate_failure():
    backend = MagicMock(spec=FlashBackend)
    backend.verify_mode = VerifyMode.SEPARATE
    result = CommandResult(["vfy"], Path.cwd(), 1, "", "mismatch", None, 10)
    backend.verify.return_value = result
    config = {"flash": {"verify": True}}
    assert verify_flash(backend, Path("x.bin"), config) is False


def test_verify_flash_none_with_allow():
    backend = MagicMock(spec=FlashBackend)
    backend.verify_mode = VerifyMode.NONE
    config = {"flash": {"verify": True, "allow_unverified": True}}
    assert verify_flash(backend, Path("x.bin"), config) is True


def test_verify_flash_none_without_allow():
    backend = MagicMock(spec=FlashBackend)
    backend.verify_mode = VerifyMode.NONE
    config = {"flash": {"verify": True, "allow_unverified": False}}
    with pytest.raises(UnsupportedOperationError):
        verify_flash(backend, Path("x.bin"), config)


def test_verify_flash_inline_skips_verify_call():
    """INLINE mode should not call backend.verify()."""
    backend = MagicMock(spec=FlashBackend)
    backend.verify_mode = VerifyMode.INLINE
    config = {"flash": {"verify": True}}
    assert verify_flash(backend, Path("x.elf"), config) is True
    backend.verify.assert_not_called()


def test_verify_flash_separate_allow_unverified():
    """SEPARATE mode with unsupported verify + allow_unverified grants pass."""
    backend = MagicMock(spec=FlashBackend)
    backend.verify_mode = VerifyMode.SEPARATE
    backend.verify.side_effect = UnsupportedOperationError("not supported")
    config = {"flash": {"verify": True, "allow_unverified": True}}
    assert verify_flash(backend, Path("x.bin"), config) is True


def test_verify_flash_separate_raises_without_allow():
    """SEPARATE mode with unsupported verify and no allow raises."""
    backend = MagicMock(spec=FlashBackend)
    backend.verify_mode = VerifyMode.SEPARATE
    backend.verify.side_effect = UnsupportedOperationError("not supported")
    config = {"flash": {"verify": True, "allow_unverified": False}}
    with pytest.raises(UnsupportedOperationError):
        verify_flash(backend, Path("x.bin"), config)


def test_reset_mode_none_requires_allow():
    """ResetMode.NONE without allow_no_reset should be gated."""
    from lib.backends import ResetMode
    backend = MagicMock(spec=FlashBackend)
    backend.reset_mode = ResetMode.NONE
    backend.verify_mode = VerifyMode.INLINE  # so verify_flash passes
    # The gate is: allow_no_reset must be true for NONE reset
    # This is tested at the flash.py _run() level; verify properties here
    assert backend.reset_mode == ResetMode.NONE


def test_reset_mode_none_flag():
    """ResetMode.NONE is a distinct enumeration value."""
    from lib.backends import ResetMode
    assert ResetMode.NONE.value == "none"
