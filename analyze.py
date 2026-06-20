#!/usr/bin/env python3
"""
analyze.py — Deviation analysis, convergence detection, and decision output.

Reads a per-round monitor CSV and compares each variable's behavior against
its expected range.  Outputs a structured JSON decision file with:
  - per-variable statistics (mean, std, min, max, % in range)
  - convergence trend (improving / worsening / stalled)
  - recommended parameter adjustments
  - termination signals (success / stall / divergence / emergency)

Usage:
    python3 tools/analyze.py --config tools/config.yaml --round 1
    python3 tools/analyze.py --config tools/config.yaml --csv tools/logs/monitor_r01.csv

Output:
    tools/logs/decision_r01.json
"""

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List

import yaml

# Add tools/lib to path (already done via sys.path in existing code)
from lib.analyzers import DeviationAnalyzer, ThresholdAnalyzer


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------
@dataclass
class VarStats:
    var_id: int
    name: str
    unit: str
    count: int
    mean: float
    std: float
    min_val: float
    max_val: float
    in_range_pct: float              # % of samples within expected bounds
    deviation: float                  # signed distance from expected center
    deviation_norm: float             # deviation normalized to expected range width
    trend: str = "flat"               # improving | worsening | flat | oscillating
    status: str = "ok"                # ok | warn | bad | emergency

@dataclass
class Decision:
    round_num: int
    csv_path: str
    variables: List[VarStats] = field(default_factory=list)
    overall_status: str = "ok"        # ok | warn | bad | emergency
    convergence: str = "unknown"      # converging | diverging | stalled | oscillating | unknown
    recommendations: List[dict] = field(default_factory=list)
    termination: Optional[str] = None  # success | stall | divergence | emergency
    unverified: List[str] = field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------
def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_csv(path: str) -> Dict[int, List[float]]:
    """Load a monitor CSV, returning {var_id: [values]}."""
    import csv
    data: Dict[int, List[float]] = defaultdict(list)
    with open(path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                var_id = int(row["var_id"], 16)
                value = float(row["value"])
                data[var_id].append(value)
            except (ValueError, KeyError):
                continue
    return data



def build_recommendations(variables: List[VarStats], params: List[dict],
                          var_index: Dict[int, dict]) -> List[dict]:
    """
    Build parameter adjustment recommendations based on variable deviations.

    For each adjustable parameter, look at its dependent variables and
    suggest a proportional adjustment.
    """
    recommendations = []
    var_map = {v.var_id: v for v in variables}

    for param in params:
        if not param.get("auto", False):
            continue

        param_name = param["name"]
        step = param.get("step", 0.1)
        param_range = param.get("range", {})
        pmin, pmax = param_range.get("min"), param_range.get("max")

        # Compute a weighted suggestion from all dependent variables
        total_weight = 0.0
        weighted_direction = 0.0
        details = []

        for dep_id in param.get("depends_on", []):
            var = var_map.get(dep_id)
            if var is None:
                continue

            # Direction: if var is above center, we likely need to decrease the gain
            #  if var is below center, increase.  This is a simple heuristic —
            #  real systems need a proper sensitivity model.
            direction = -1.0 if var.deviation > 0 else 1.0
            weight = abs(var.deviation_norm)
            weighted_direction += direction * weight
            total_weight += weight

            details.append({
                "variable": var.name,
                "deviation": round(var.deviation, 4),
                "direction": "decrease" if direction < 0 else "increase",
                "weight": round(weight, 3),
            })

        if total_weight == 0:
            continue

        # Normalize
        norm_direction = weighted_direction / total_weight
        delta = norm_direction * step

        # Clamp to parameter range
        # (We don't know the current value here — adjust.py reads it from the file.
        #  So we just provide the suggested delta.)
        delta_clamped = delta  # clamping happens in adjust.py

        recommendations.append({
            "parameter": param_name,
            "file": param["file"],
            "pattern": param["pattern"],
            "format": param.get("format", "%.4f"),
            "range": {"min": pmin, "max": pmax},
            "suggested_delta": round(delta_clamped, 6),
            "confidence": round(min(total_weight / len(param.get("depends_on", [1])), 1.0), 2),
            "details": details,
        })

    return recommendations



def analyze(cfg: dict, csv_path: str, round_num: int,
            history: List[Decision]) -> Decision:
    """
    Main analysis entry point.  Delegates to the configured analyzer plugin.

    Scenario auto-detection:
      - Has auto:true parameters → DeviationAnalyzer (scenario A)
      - No auto:true parameters   → ThresholdAnalyzer (scenario B/C)
    """
    var_cfgs = {v["id"]: v for v in cfg.get("variables", [])}
    has_auto = any(p.get("auto", False) for p in cfg.get("parameters", []))

    # Load raw data
    raw_data = load_csv(csv_path)

    # Select analyzer
    if has_auto:
        analyzer = DeviationAnalyzer()
    else:
        analyzer = ThresholdAnalyzer()

    # Run analysis
    # Decision objects have no 'details' field — always build from .variables
    history_details = [{
        "variables": [
            {"var_id": v.var_id, "name": v.name, "deviation_norm": v.deviation_norm,
             "trend": v.trend, "status": v.status}
            for v in h.variables
        ]
    } for h in history]

    result = analyzer.analyze(raw_data, var_cfgs, history_details)

    # Build Decision object
    decision = Decision(
        round_num=round_num,
        csv_path=csv_path,
        overall_status=result.overall_status,
        convergence=result.convergence,
        termination=result.termination,
    )

    # Populate variables list
    var_list = result.details.get("variables", [])
    for vd in var_list:
        decision.variables.append(VarStats(
            var_id=int(vd["var_id"]) if isinstance(vd["var_id"], str) else vd["var_id"],
            name=vd["name"],
            unit=vd.get("unit", ""),
            count=vd.get("count", 0),
            mean=vd.get("mean", 0.0),
            std=vd.get("std", 0.0),
            min_val=vd.get("min", 0.0),
            max_val=vd.get("max", vd.get("max_value", 0.0)),
            in_range_pct=vd.get("in_range_pct", 100.0 if vd.get("status") == "ok" else 0.0),
            deviation=vd.get("deviation", 0.0),
            deviation_norm=vd.get("deviation_norm", 0.0),
            trend=vd.get("trend", "unknown"),
            status=vd.get("status", "ok"),
        ))

    # Build recommendations (only for deviation analyzer with auto params)
    if has_auto:
        decision.recommendations = build_recommendations(
            decision.variables, cfg.get("parameters", []),
            {v["id"]: v for v in cfg.get("variables", [])}
        )

    # Attach UNVERIFIED assumptions
    decision.unverified = result.unverified_assumptions

    # Summary
    lines = [f"Round {round_num}: {decision.overall_status} ({decision.convergence})"]
    for v in decision.variables:
        lines.append(
            f"  {v.name:>16s}  μ={v.mean:>10.4f}  σ={v.std:.4f}  "
            f"in_range={v.in_range_pct:>5.1f}%  status={v.status}  trend={v.trend}"
        )
    decision.summary = "\n".join(lines)

    return decision


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(description="Analyze monitor CSV and produce a decision")
    ap.add_argument("--config", default="tools/config.yaml")
    ap.add_argument("--round", type=int, default=1)
    ap.add_argument("--csv", default=None, help="CSV path (default: tools/logs/monitor_r<N>.csv)")
    ap.add_argument("--history-dir", default="tools/logs",
                    help="Directory with previous decision JSON files")
    args = ap.parse_args()

    cfg = load_config(args.config)
    csv_path = args.csv or f"tools/logs/monitor_r{args.round:02d}.csv"

    if not Path(csv_path).exists():
        print(f"Error: CSV not found: {csv_path}", file=sys.stderr)
        sys.exit(1)

    # Load history
    history: List[Decision] = []
    history_dir = Path(args.history_dir)
    for r in range(1, args.round):
        hist_path = history_dir / f"decision_r{r:02d}.json"
        if hist_path.exists():
            with open(hist_path) as f:
                data = json.load(f)
                # Reconstruct a minimal Decision for trend comparison
                d = Decision(round_num=r, csv_path="")
                d.variables = [
                    VarStats(**v) if isinstance(v, dict) else v
                    for v in data.get("variables", [])
                ]
                history.append(d)

    decision = analyze(cfg, csv_path, args.round, history)

    # Write decision file
    out_path = f"tools/logs/decision_r{args.round:02d}.json"
    Path(out_path).parent.mkdir(parents=True, exist_ok=True)

    # Serialize
    output = {
        "round": decision.round_num,
        "csv_path": decision.csv_path,
        "overall_status": decision.overall_status,
        "convergence": decision.convergence,
        "termination": decision.termination,
        "unverified_assumptions": decision.unverified,
        "variables": [
            {
                "var_id": f"0x{v.var_id:04X}",
                "name": v.name,
                "unit": v.unit,
                "count": v.count,
                "mean": round(v.mean, 6),
                "std": round(v.std, 6),
                "min": round(v.min_val, 6),
                "max": round(v.max_val, 6),
                "in_range_pct": round(v.in_range_pct, 2),
                "deviation": round(v.deviation, 6),
                "deviation_norm": round(v.deviation_norm, 4),
                "trend": v.trend,
                "status": v.status,
            }
            for v in decision.variables
        ],
        "recommendations": decision.recommendations,
        "summary": decision.summary,
    }

    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)

    print(decision.summary)
    print(f"\nDecision → {out_path}")

    if decision.termination:
        print(f"TERMINATION: {decision.termination}")
        sys.exit(0)


if __name__ == "__main__":
    main()
