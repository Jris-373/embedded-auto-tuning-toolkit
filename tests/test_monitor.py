"""Test --require-boot-done flag behavior."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_monitor_to_csv_signature_accepts_require_boot_done():
    """Verify monitor_to_csv can be called with require_boot_done kwarg."""
    from monitor import monitor_to_csv
    import inspect
    sig = inspect.signature(monitor_to_csv)
    params = list(sig.parameters.keys())
    assert "require_boot_done" in params
    assert sig.parameters["require_boot_done"].default is False
