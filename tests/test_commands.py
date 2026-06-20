"""Test CommandResult and CommandRunner."""
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.commands import CommandRunner, CommandResult, CommandTimeoutError


@pytest.fixture
def work_dir():
    """A writable temporary directory that works cross-platform."""
    tmp = tempfile.mkdtemp(prefix="cmdtool_test_")
    yield Path(tmp)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def test_command_result_ok():
    r = CommandResult(["echo"], Path.cwd(), 0, "out", "", None, 100.0)
    assert r.ok is True
    r2 = CommandResult(["false"], Path.cwd(), 1, "", "err", None, 50.0)
    assert r2.ok is False


def test_runner_returns_command_result(work_dir):
    runner = CommandRunner(timeout_s=30)
    result = runner.run([sys.executable, "-c", "print('hello')"], cwd=work_dir)
    assert result.ok
    assert "hello" in result.stdout
    assert result.cwd == work_dir
    assert result.duration_ms > 0


def test_runner_writes_log(work_dir):
    runner = CommandRunner(timeout_s=30)
    log = work_dir / "mylog.txt"
    result = runner.run([sys.executable, "-c", "print('x')"], cwd=work_dir, log_path=log)
    assert log.exists()
    content = log.read_text()
    assert "x" in content
    assert "returncode: 0" in content


def test_runner_raises_on_timeout():
    runner = CommandRunner(timeout_s=0.01)
    with pytest.raises(CommandTimeoutError):
        runner.run([sys.executable, "-c", "import time; time.sleep(10)"], cwd=Path.cwd())


def test_command_result_frozen():
    r = CommandResult(["x"], Path.cwd(), 0, "", "", None, 0)
    with pytest.raises(Exception):
        r.returncode = 1
