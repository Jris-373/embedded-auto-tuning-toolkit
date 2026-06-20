"""
commands.py — Subprocess execution primitives.

Shared by BuildRunner and FlashBackend.  Command construction (pure
functions returning List[str]) is separated from execution (run())
so that tests can verify command arrays without spawning processes.
"""

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List


class CommandTimeoutError(TimeoutError):
    """Command exceeded the configured timeout."""
    def __init__(self, command: list[str], timeout_s: float):
        self.command = command
        self.timeout_s = timeout_s
        super().__init__(f"Command timed out after {timeout_s}s: {' '.join(command)}")


@dataclass(frozen=True)
class CommandResult:
    """Result of a successfully-launched process.

    Process-startup failures (FileNotFoundError) and timeouts
    (CommandTimeoutError) are raised, not returned.
    """
    command: list[str]
    cwd: Path
    returncode: int
    stdout: str
    stderr: str
    log_path: Path | None
    duration_ms: float

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class CommandRunner:
    """Inject-able subprocess runner.

    Usage:
        runner = CommandRunner(timeout_s=120.0)
        result = runner.run(["make", "-j8"], cwd=project_root,
                            log_path=log_dir / "build.log")
    """

    def __init__(self, timeout_s: float = 120.0):
        self._timeout = timeout_s

    def run(self, args: list[str], cwd: Path,
            log_path: Path | None = None) -> CommandResult:
        """Execute *args* in *cwd*.  Raises FileNotFoundError or
        CommandTimeoutError on failure to launch / complete."""
        t0 = time.monotonic()
        try:
            proc = subprocess.run(
                args, cwd=cwd, capture_output=True, text=True,
                timeout=self._timeout, check=False,
            )
        except subprocess.TimeoutExpired:
            raise CommandTimeoutError(args, self._timeout)

        duration_ms = (time.monotonic() - t0) * 1000.0

        if log_path:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(log_path, "w") as f:
                f.write(f"# command: {' '.join(args)}\n")
                f.write(f"# cwd: {cwd}\n")
                f.write(f"# returncode: {proc.returncode}\n")
                f.write(f"# duration_ms: {duration_ms:.0f}\n\n")
                f.write(proc.stdout)
                if proc.stderr:
                    f.write("\n# STDERR:\n")
                    f.write(proc.stderr)

        return CommandResult(
            command=args,
            cwd=cwd,
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
            log_path=log_path,
            duration_ms=duration_ms,
        )
