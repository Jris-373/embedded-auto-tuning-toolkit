#!/usr/bin/env python3
"""
loop_runner.py — Closed-loop auto-tuning orchestrator.

Scenarios:
    A — Parameter optimization (flash→monitor→analyze→adjust loop)
    B — Stage diagnosis    (flash→monitor→analyze→report, one-shot)
    C — Long-term monitoring (flash→monitor→analyze, continuous)

Usage:
    python3 tools/loop_runner.py [--config <path>] [--max-rounds N]

Exit codes:
    0   — success
    1   — scenario A max rounds reached
    2   — scenario A stalled
    3   — emergency stop
    4   — step execution error
    5   — config error
    130 — interrupted by user
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

import yaml


EXIT_SUCCESS       = 0
EXIT_MAX_ROUNDS    = 1
EXIT_STALLED       = 2
EXIT_EMERGENCY     = 3
EXIT_STEP_ERROR    = 4
EXIT_CONFIG_ERROR  = 5


class StepError(Exception):
    def __init__(self, step: str, exit_code: int, message: str):
        self.step = step
        self.exit_code = exit_code
        self.message = message


def run_step(cmd: list, *, cwd: Path, retry_on: int | None = None,
             timeout_s: float = 300.0, failure_exit_code: int = 4) -> int:
    try:
        result = subprocess.run(cmd, cwd=cwd, timeout=timeout_s, check=False)
    except subprocess.TimeoutExpired:
        raise StepError(
            Path(cmd[1]).stem, failure_exit_code,
            f"Timeout after {timeout_s}s: {' '.join(cmd)}",
        )
    if result.returncode == 0:
        return 0
    if retry_on is not None and result.returncode == retry_on:
        return result.returncode
    # Exit 5 is always a configuration error regardless of caller
    effective_exit = 5 if result.returncode == 5 else failure_exit_code
    raise StepError(
        Path(cmd[1]).stem, effective_exit,
        f"{Path(cmd[1]).name} exit {result.returncode}",
    )


def load_decision(log_dir: Path, round_num: int) -> dict:
    path = log_dir / f"decision_r{round_num:02d}.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def compute_score(decision: dict) -> float:
    return sum(abs(v.get("deviation_norm", 0)) for v in decision.get("variables", []))


def print_diagnosis(decision: dict):
    failed = [v["name"] for v in decision.get("variables", [])
              if v.get("failed") or v.get("status") == "bad"]
    print("Failed stages:")
    if failed:
        for s in failed:
            print(f"  ✗ {s}")
    else:
        print("  (none) — all stages passed")


def generate_report(config: dict, log_dir: Path, round_num: int,
                    best_round: int, best_score: float):
    report_path = log_dir / "final_report.md"
    lines = [
        f"# Auto-Tuning Report",
        f"",
        f"- **Project:** {config.get('project', {}).get('name', 'unknown')}",
        f"- **Total rounds:** {round_num}",
        f"- **Best round:** {best_round} (score={best_score:.4f})",
    ]
    report_path.write_text("\n".join(lines) + "\n")
    print(f"[loop] Report → {report_path}")


def load_config(path: Path) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _run(args) -> int:
    tool_dir = Path(__file__).resolve().parent
    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    project_root = (config_path.parent / config["project"]["root"]).resolve()
    log_dir = tool_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    effective_max = args.max_rounds or config["loop"]["max_rounds"]

    # ── 0. Validate ──
    try:
        run_step(
            [sys.executable, str(tool_dir / "validate.py"),
             "--config", str(config_path)],
            cwd=project_root, failure_exit_code=5,
        )
    except StepError:
        return EXIT_CONFIG_ERROR

    # ── Scene detection ──
    has_auto = any(p.get("auto", False) for p in config.get("parameters", []))
    has_vars = bool(config.get("variables"))
    if has_auto:
        scenario = "A"
    elif has_vars:
        scenario = "B" if effective_max <= 3 else "C"
    else:
        print("[loop] No variables configured")
        return EXIT_CONFIG_ERROR
    print(f"[loop] Scenario {scenario} (max_rounds={effective_max})")

    # ── State ──
    convergence_needed = config["loop"]["convergence_rounds"]
    stall_limit = config["loop"]["stall_rounds"]
    best_score = float("inf")
    best_round = 0
    consecutive_ok = 0
    no_improvement = 0

    # ── Main loop ──
    round_num = 0
    try:
        while round_num < effective_max:
            round_num += 1
            print(f"\n==== ROUND {round_num}/{effective_max} ====")

            # 1. Flash
            flash_args = [
                sys.executable, str(tool_dir / "flash.py"),
                "--config", str(config_path),
                "--round", str(round_num),
                "--skip-boot-wait",
            ]
            rc = run_step(flash_args, cwd=project_root, retry_on=2)
            if rc == 2:
                print("[loop] Flash failed — retrying once with --skip-build")
                flash_args.append("--skip-build")
                run_step(flash_args, cwd=project_root)

            # 2. Monitor
            run_step([
                sys.executable, str(tool_dir / "monitor.py"),
                "--config", str(config_path),
                "--round", str(round_num),
                "--require-boot-done",
            ], cwd=project_root)

            # 3. Analyze
            run_step([
                sys.executable, str(tool_dir / "analyze.py"),
                "--config", str(config_path),
                "--round", str(round_num),
            ], cwd=project_root)
            decision = load_decision(log_dir, round_num)

            # 4. Score
            score = compute_score(decision)
            if score < best_score - 0.001:
                best_score = score
                best_round = round_num
                no_improvement = 0
            elif abs(score - best_score) < 0.001:
                no_improvement += 1
            else:
                no_improvement += 1

            # 5. Termination
            if decision.get("overall_status") == "emergency":
                print("[loop] EMERGENCY STOP")
                return EXIT_EMERGENCY

            if scenario == "A":
                if (decision.get("termination") or "").startswith("success"):
                    consecutive_ok += 1
                    if consecutive_ok >= convergence_needed:
                        print(f"[loop] TUNING SUCCESSFUL ({round_num} rounds)")
                        generate_report(config, log_dir, round_num, best_round, best_score)
                        return EXIT_SUCCESS
                else:
                    consecutive_ok = 0
                if no_improvement >= stall_limit:
                    print(f"[loop] STALLED (best was round {best_round})")
                    generate_report(config, log_dir, round_num, best_round, best_score)
                    return EXIT_STALLED
                if round_num < effective_max:
                    run_step([
                        sys.executable, str(tool_dir / "adjust.py"),
                        "--config", str(config_path),
                        "--round", str(round_num),
                    ], cwd=project_root)
                else:
                    print("[loop] Final round — skipping adjust")

            elif scenario == "B":
                print_diagnosis(decision)
                generate_report(config, log_dir, round_num, best_round, best_score)
                return EXIT_SUCCESS

            # scenario C: monitor only, continue loop
    except KeyboardInterrupt:
        print(f"\n[loop] Interrupted at round {round_num}")
        generate_report(config, log_dir, round_num, best_round, best_score)
        return 130

    # ── Max rounds ──
    if scenario == "C":
        print(f"[loop] Monitoring complete ({effective_max} rounds)")
        generate_report(config, log_dir, effective_max, best_round, best_score)
        return EXIT_SUCCESS
    print(f"[loop] MAX ROUNDS ({effective_max}) reached")
    generate_report(config, log_dir, effective_max, best_round, best_score)
    return EXIT_MAX_ROUNDS


def main() -> int:
    ap = argparse.ArgumentParser(description="Closed-loop auto-tuning orchestrator")
    ap.add_argument("--config", default="tools/config.yaml")
    ap.add_argument("--max-rounds", type=int, default=None)
    args = ap.parse_args()
    try:
        return _run(args)
    except StepError as e:
        print(f"[loop] {e.step} failed: {e.message}", file=sys.stderr)
        return e.exit_code
    except (ValueError, FileNotFoundError, yaml.YAMLError) as e:
        print(f"[loop] Configuration error: {e}", file=sys.stderr)
        return EXIT_CONFIG_ERROR


if __name__ == "__main__":
    sys.exit(main())
