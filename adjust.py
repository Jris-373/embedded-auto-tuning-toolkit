#!/usr/bin/env python3
"""
adjust.py — Apply parameter changes to source files based on a decision.

Reads a decision JSON from analyze.py and modifies the source files
(typically C header #defines) according to the recommendations.

Safety:
  - Parameters marked auto=false are NEVER modified.
  - Every modification is logged to a per-round changelog.
  - Values are clamped to their configured [min, max] range.
  - If the current value in the file doesn't match the expected pattern,
    the parameter is skipped with a warning (human must intervene).

Usage:
    python3 tools/adjust.py --config tools/config.yaml --round 1
    python3 tools/adjust.py --config tools/config.yaml --decision tools/logs/decision_r01.json
    python3 tools/adjust.py --rollback --round 5    # revert round 5 changes

Output:
    tools/logs/adjust_r01.log   — what was changed and why
    Modified source file(s)     — in-place edits with backup as <file>.bak
"""

import argparse
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List

import yaml

from lib.adjusters import MacroAdjuster


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def load_decision(path: str) -> dict:
    with open(path) as f:
        return json.load(f)



# ---------------------------------------------------------------------------
# Rollback
# ---------------------------------------------------------------------------
def rollback_round(round_num: int) -> bool:
    """
    Restore .bak files for a given round's adjustments.

    This is a simple approach: for each .bak file in the project,
    restore it.  More sophisticated implementations would track
    exactly which files were modified per round.
    """
    log_path = Path(f"tools/logs/adjust_r{round_num:02d}.log")
    if not log_path.exists():
        print(f"No adjust log for round {round_num}")
        return False

    restored = 0
    log_content = log_path.read_text()

    # Parse log to find modified files
    for line in log_content.splitlines():
        if ".bak" in line:
            continue
        # Extract file path from log line: "  path/to/file: old → new"
        match = re.match(r'\s+(\S+):', line)
        if match:
            file_path = Path(match.group(1))
            backup = file_path.with_suffix(file_path.suffix + ".bak")
            if backup.exists():
                backup.rename(file_path)
                restored += 1
                print(f"  Restored: {file_path}")

    print(f"Rollback round {round_num}: {restored} file(s) restored")
    return True


# ---------------------------------------------------------------------------
# Main adjustment logic
# ---------------------------------------------------------------------------
def apply_decision(cfg: dict, decision: dict, round_num: int) -> bool:
    """
    Apply all auto-adjustable recommendations from a decision.
    Generates unified diff and saves to tools/logs/adjust_r{N}.diff.
    Returns True if any changes were made.
    """
    params_cfg = {p["name"]: p for p in cfg.get("parameters", [])}
    recommendations = decision.get("recommendations", [])

    log_dir = Path("tools/logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    project_root = Path(cfg.get("project", {}).get("root", "."))

    log_lines = [
        f"# Adjust Round {round_num} — {datetime.now().isoformat()}",
        f"# Decision: {decision.get('overall_status', '?')} / {decision.get('convergence', '?')}",
        "",
    ]
    all_diffs: List[str] = []

    if not recommendations:
        log_lines.append("No recommendations to apply.")
        _write_log(round_num, log_lines, all_diffs)
        return False

    auto_recs = [r for r in recommendations if params_cfg.get(r["parameter"], {}).get("auto", False)]
    manual_recs = [r for r in recommendations if not params_cfg.get(r["parameter"], {}).get("auto", False)]

    if manual_recs:
        log_lines.append("## Manual intervention required (auto=false):")
        for r in manual_recs:
            log_lines.append(f"  - {r['parameter']}: delta={r['suggested_delta']:+.6f} "
                           f"(confidence={r['confidence']})")
        log_lines.append("")

    if not auto_recs:
        log_lines.append("## No auto-adjustable parameters.")
        _write_log(round_num, log_lines, all_diffs)
        return False

    log_lines.append("## Applied adjustments:")
    adjuster = MacroAdjuster()
    changed = False

    for r in auto_recs:
        param_name = r["parameter"]
        param_cfg = params_cfg.get(param_name)
        if param_cfg is None:
            log_lines.append(f"  SKIP {param_name}: not found in config")
            continue

        delta = r["suggested_delta"]

        log_lines.append(f"\n### {param_name} (delta={delta:+.6f}, confidence={r['confidence']:.0%})")
        for d in r.get("details", []):
            log_lines.append(f"    {d['variable']}: dev={d['deviation']:+.4f} → {d['direction']}")

        current_val = adjuster.read_current(param_cfg, project_root)
        if current_val is None:
            log_lines.append(f"  SKIP: cannot read current value from "
                           f"{project_root / param_cfg['file']}")
            continue

        log_lines.append(f"  Current: {current_val:.6f}")

        new_val = current_val + delta

        success, diff_or_error = adjuster.apply(param_cfg, new_val, project_root)
        if success:
            changed = True
            log_lines.append(f"  {current_val:.6f} → {new_val:.6f}")
            if diff_or_error:
                all_diffs.append(f"=== file: {param_cfg['file']} ===\n{diff_or_error}")
                print(f"[adjust] Diff for {param_name}:")
                for line in diff_or_error.splitlines()[:10]:
                    print(f"  {line}")
                if len(diff_or_error.splitlines()) > 10:
                    print(f"  ... ({len(diff_or_error.splitlines())} lines total)")
        else:
            log_lines.append(f"  FAILED: {diff_or_error}")

    _write_log(round_num, log_lines, all_diffs)
    return changed


def _write_log(round_num: int, lines: List[str], diffs: List[str]):
    """Write adjust log and diff file."""
    log_dir = Path("tools/logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    log_path = log_dir / f"adjust_r{round_num:02d}.log"
    log_path.write_text("\n".join(lines) + "\n")
    print(f"[adjust] Log → {log_path}")

    if diffs:
        diff_path = log_dir / f"adjust_r{round_num:02d}.diff"
        diff_path.write_text("\n".join(diffs) + "\n")
        print(f"[adjust] Diff → {diff_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Apply parameter adjustments from a decision")
    ap.add_argument("--config", default="tools/config.yaml")
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--decision", default=None,
                    help="Decision JSON path (default: tools/logs/decision_r<N>.json)")
    ap.add_argument("--rollback", action="store_true",
                    help="Rollback changes from a given round")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print what would change but don't modify files")
    args = ap.parse_args()

    cfg = load_config(args.config)

    if args.rollback:
        rollback_round(args.round)
        return

    decision_path = args.decision or f"tools/logs/decision_r{args.round:02d}.json"
    if not Path(decision_path).exists():
        print(f"Decision file not found: {decision_path}")
        sys.exit(1)

    decision = load_decision(decision_path)

    if args.dry_run:
        print("[adjust] DRY RUN — no files will be modified")
        # Just print recommendations
        for r in decision.get("recommendations", []):
            print(f"  {r['parameter']}: delta={r['suggested_delta']:+.6f} (confidence={r['confidence']:.0%})")
        return

    changed = apply_decision(cfg, decision, args.round)
    if changed:
        print("[adjust] Changes applied.  Rebuild and reflash required.")
    else:
        print("[adjust] No changes made.")


if __name__ == "__main__":
    main()
