"""
adjusters.py — Parameter modification plugins.

UNVERIFIED: MacroAdjuster assumes C preprocessor macro format.
Struct-field and runtime-protocol adjusters are not implemented.

Concrete implementations:
    MacroAdjuster — Modify #define values in C headers via regex
"""

from abc import ABC, abstractmethod
import re
from pathlib import Path
from typing import Optional, Tuple


class Adjuster(ABC):
    """Abstract parameter modifier."""

    @abstractmethod
    def read_current(self, param_config: dict, project_root: Path) -> Optional[float]:
        """Read current parameter value from the source file."""

    @abstractmethod
    def apply(self, param_config: dict, new_value: float,
              project_root: Path) -> Tuple[bool, str]:
        """Modify the source file.  Returns (success, diff_or_error_message)."""


class MacroAdjuster(Adjuster):
    """
    Adjust C preprocessor #define macros via regex replacement.

    UNVERIFIED: assumes the pattern captures exactly one numeric group
    and that the macro is only defined once.  Multi-definition patterns
    (e.g. #ifdef platform-specific blocks) will silently miss the second
    definition.
    """

    def read_current(self, param_config, project_root):
        file_path = project_root / param_config["file"]
        if not file_path.exists():
            return None
        content = file_path.read_text()
        match = re.search(param_config["pattern"], content)
        if not match:
            return None
        try:
            return float(match.group(1))
        except (ValueError, IndexError):
            return None

    def apply(self, param_config, new_value, project_root):
        file_path = project_root / param_config["file"]
        pattern = param_config["pattern"]
        fmt = param_config.get("format", "%.4f")

        if not file_path.exists():
            return False, f"File not found: {file_path}"

        content = file_path.read_text()
        match = re.search(pattern, content)
        if not match:
            return False, f"Pattern not found in {file_path}"

        old_str = match.group(0)
        old_val_str = match.group(1)

        pmin = param_config.get("range", {}).get("min")
        pmax = param_config.get("range", {}).get("max")
        clamped = ""
        if pmin is not None and new_value < pmin:
            new_value = pmin
            clamped = f" (clamped to min={pmin})"
        elif pmax is not None and new_value > pmax:
            new_value = pmax
            clamped = f" (clamped to max={pmax})"

        if "d" in fmt:
            new_val_str = str(int(round(new_value)))
        else:
            new_val_str = fmt % new_value

        new_str = old_str.replace(old_val_str, new_val_str, 1)
        new_content = content.replace(old_str, new_str, 1)

        if new_content == content:
            return False, "Replacement produced no change"

        # Backup
        backup_path = file_path.with_suffix(file_path.suffix + ".bak")
        file_path.rename(backup_path)
        file_path.write_text(new_content)

        # Generate diff
        import subprocess
        import shutil
        diff_cmd = shutil.which("diff")
        if diff_cmd:
            result = subprocess.run(
                [diff_cmd, "-u", str(backup_path), str(file_path)],
                capture_output=True, text=True
            )
            diff_text = result.stdout
        else:
            diff_text = f"--- {backup_path}\n+++ {file_path}\n@@ change @@\n-{old_val_str}\n+{new_val_str}\n"

        return True, diff_text
