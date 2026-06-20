"""Test MakeBuilder, CMakeBuilder, CustomBuilder command construction."""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from lib.builders import MakeBuilder, CMakeBuilder, CustomBuilder, create_builder
from lib.commands import CommandRunner


@pytest.fixture
def runner():
    r = MagicMock(spec=CommandRunner)
    r.run.return_value.command = []
    r.run.return_value.cwd = Path.cwd()
    r.run.return_value.returncode = 0
    return r


@patch("lib.builders._resolve_exe", return_value="/usr/bin/make")
def test_make_build_args(mock_resolve):
    config = {"build": {"target": "all", "parallel": 8, "flags": ["-k"]}}
    b = MakeBuilder(MagicMock(), config)
    args = b._build_args(config)
    assert args[0] == "/usr/bin/make"
    assert "all" in args
    assert "-j8" in args
    assert "-k" in args


@patch("lib.builders._resolve_exe", return_value="/usr/bin/cmake")
def test_cmake_build_args(mock_resolve):
    config = {"build": {"directory": "build", "target": "firmware", "parallel": 4, "flags": ["-DDEBUG=1"]}}
    b = CMakeBuilder(MagicMock(), config)
    args = b._build_args(config)
    assert args[0] == "/usr/bin/cmake"
    assert "--build" in args
    assert "build" in args
    assert "--target" in args
    assert "firmware" in args
    assert "--parallel" in args
    assert "4" in args
    assert "--" in args
    assert "-DDEBUG=1" in args


@patch("lib.builders._resolve_exe", return_value="/usr/bin/cmake")
def test_cmake_no_flags_no_dashdash(mock_resolve):
    config = {"build": {"directory": "build", "target": "all", "parallel": 0, "flags": []}}
    b = CMakeBuilder(MagicMock(), config)
    args = b._build_args(config)
    assert "--" not in args


def test_custom_builder_rejects_empty():
    b = CustomBuilder(MagicMock())
    config = {"build": {"command": []}}
    with pytest.raises(ValueError, match="must not be empty"):
        b.build(config, Path.cwd())


def test_custom_builder_rejects_non_list():
    b = CustomBuilder(MagicMock())
    config = {"build": {"command": "make"}}
    with pytest.raises(ValueError, match="string array"):
        b.build(config, Path.cwd())


@patch("lib.builders._resolve_exe", return_value="/usr/bin/make")
def test_create_builder_make(mock_resolve):
    b = create_builder({"build": {"system": "make"}}, MagicMock())
    assert isinstance(b, MakeBuilder)


def test_create_builder_unknown():
    with pytest.raises(ValueError, match="Unknown build.system"):
        create_builder({"build": {"system": "bazel"}}, MagicMock())
