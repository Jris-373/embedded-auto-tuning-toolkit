"""
builders.py — Build-system abstraction.

Concrete implementations:
    MakeBuilder     — make <target> -j<parallel> <flags>
    CMakeBuilder    — cmake --build <directory> --target <target> [--parallel N] [-- <flags>]
    CustomBuilder   — execute build.command array directly

Factory:
    create_builder(config, runner) -> BuildRunner
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import List, Optional

from lib.commands import CommandRunner, CommandResult


def _resolve_exe(explicit: str | None, primary: str,
                 windows_alt: str | None = None) -> str:
    """Resolve an executable path.

    1. Use *explicit* if provided.
    2. shutil.which(*primary*).
    3. On Windows, try *windows_alt*.
    4. FileNotFoundError.
    """
    import shutil, sys
    if explicit:
        return explicit
    found = shutil.which(primary)
    if found:
        return found
    if sys.platform == "win32" and windows_alt:
        found = shutil.which(windows_alt)
        if found:
            return found
    raise FileNotFoundError(f"{primary} not found in PATH")


class BuildRunner(ABC):
    """Abstract build-system interface."""

    @abstractmethod
    def build(self, config: dict, project_root: Path) -> CommandResult: ...

    def clean(self, config: dict, project_root: Path) -> Optional[CommandResult]:
        """Optional clean step.  Returns None if not supported."""
        return None


class MakeBuilder(BuildRunner):
    def __init__(self, runner: CommandRunner, config: dict):
        self._runner = runner
        self._exe = _resolve_exe(
            config["build"].get("make_executable"), "make",
            windows_alt="mingw32-make.exe",
        )

    def _build_args(self, config: dict) -> list[str]:
        args = [self._exe, config["build"].get("target", "all")]
        parallel = config["build"].get("parallel", 0)
        if parallel > 0:
            args.append(f"-j{parallel}")
        flags = config["build"].get("flags", [])
        if isinstance(flags, list):
            args.extend(flags)
        return args

    def build(self, config: dict, project_root: Path) -> CommandResult:
        args = self._build_args(config)
        return self._runner.run(args, cwd=project_root)

    def clean(self, config: dict, project_root: Path) -> Optional[CommandResult]:
        return self._runner.run([self._exe, "clean"], cwd=project_root)


class CMakeBuilder(BuildRunner):
    def __init__(self, runner: CommandRunner, config: dict):
        self._runner = runner
        self._exe = _resolve_exe(config["build"].get("cmake_executable"), "cmake")

    def _build_args(self, config: dict) -> list[str]:
        directory = config["build"].get("directory", "build")
        target = config["build"].get("target", "all")
        args = [self._exe, "--build", directory, "--target", target]
        parallel = config["build"].get("parallel", 0)
        if parallel > 0:
            args.extend(["--parallel", str(parallel)])
        flags = config["build"].get("flags", [])
        if flags:
            args.append("--")
            args.extend(flags)
        return args

    def build(self, config: dict, project_root: Path) -> CommandResult:
        args = self._build_args(config)
        return self._runner.run(args, cwd=project_root)

    def clean(self, config: dict, project_root: Path) -> Optional[CommandResult]:
        exe = _resolve_exe(config["build"].get("cmake_executable"), "cmake")
        directory = config["build"].get("directory", "build")
        return self._runner.run(
            [exe, "--build", directory, "--target", "clean"],
            cwd=project_root,
        )


class CustomBuilder(BuildRunner):
    def __init__(self, runner: CommandRunner):
        self._runner = runner

    def build(self, config: dict, project_root: Path) -> CommandResult:
        cmd = config["build"].get("command")
        if not isinstance(cmd, list) or not all(isinstance(x, str) for x in cmd):
            raise ValueError(
                "build.system=custom requires build.command as a "
                "non-empty string array"
            )
        if not cmd:
            raise ValueError("build.command must not be empty")
        return self._runner.run(cmd, cwd=project_root)


def create_builder(config: dict, runner: CommandRunner) -> BuildRunner:
    system = config["build"].get("system", "make")
    if system == "make":
        return MakeBuilder(runner, config)
    elif system == "cmake":
        return CMakeBuilder(runner, config)
    elif system == "custom":
        return CustomBuilder(runner)
    raise ValueError(f"Unknown build.system: {system}")
