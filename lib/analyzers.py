"""
analyzers.py — Analysis strategy plugins.

UNVERIFIED: DeviationAnalyzer assumes monotonic negative correlation
between parameter changes and variable deviations.  ThresholdAnalyzer
flag-detection heuristic is untested on real hardware.

Concrete implementations:
    DeviationAnalyzer — Compare variables against expected/warn/emergency ranges
    ThresholdAnalyzer — Detect flag variables (scenario B: stage diagnostics)
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, List, Optional


@dataclass
class AnalysisResult:
    """Output from any Analyzer."""
    overall_status: str = "ok"          # ok | warn | bad | emergency
    convergence: str = "unknown"        # converging | diverging | stalled | oscillating | unknown
    termination: Optional[str] = None   # success:<reason> | emergency:<reason> | None
    unverified_assumptions: List[str] = field(default_factory=list)
    details: dict = field(default_factory=dict)   # analyzer-specific data


class Analyzer(ABC):
    """Abstract analysis strategy."""

    @abstractmethod
    def analyze(self, var_data: Dict[int, List[float]],
                var_configs: Dict[int, dict],
                history: List[dict]) -> AnalysisResult:
        """
        Parameters:
            var_data:    {var_id: [time-series values]}
            var_configs: {var_id: {name, expected, warn, emergency, ...}}
            history:     list of prior AnalysisResult.details dicts
        """


class DeviationAnalyzer(Analyzer):
    """
    Range-based deviation analysis (Scenario A: parameter optimization).

    UNVERIFIED: assumes monotonic negative correlation between parameter
    and variable.  Non-monotonic systems (e.g. resonant peaks) need a
    per-parameter direction map in config.yaml.

    UNVERIFIED: 0.05 normalized-deviation threshold for trend detection
    is arbitrary.  For noisy variables, use per-variable thresholds based
    on measured noise floor, or a statistical test (Mann-Kendall).
    """

    def analyze(self, var_data, var_configs, history):
        from math import sqrt
        import json

        variables = []
        unverified = [
            "param_dir_heuristic: assumes monotonic negative correlation",
            "trend_threshold: fixed 0.05, no per-variable noise calibration",
            "convergence_rounds: default 3, not tuned to system dynamics",
            "var_weighting: equal weight, no sensitivity matrix",
        ]

        for var_id, values in var_data.items():
            cfg = var_configs.get(var_id, {})
            n = len(values)
            if n == 0:
                continue
            mean = sum(values) / n
            variance = sum((v - mean) ** 2 for v in values) / n
            std = sqrt(variance)
            min_v = min(values)
            max_v = max(values)

            exp = cfg.get("expected", {})
            emin, emax = exp.get("min"), exp.get("max")
            in_range = 0
            deviation = 0.0
            deviation_norm = 0.0
            if emin is not None and emax is not None:
                center = (emin + emax) / 2.0
                deviation = mean - center
                rw = emax - emin
                deviation_norm = abs(deviation) / (rw / 2.0) if rw > 0 else abs(deviation)
                for v in values:
                    if emin <= v <= emax:
                        in_range += 1
            in_range_pct = (in_range / n * 100.0) if n > 0 else 0.0

            # Status
            status = "ok"
            emergency = cfg.get("emergency", {})
            eemin, eemax = emergency.get("min"), emergency.get("max")
            if (eemin is not None and min_v < eemin) or (eemax is not None and max_v > eemax):
                status = "emergency"
            else:
                warn = cfg.get("warn", {})
                wmin, wmax = warn.get("min"), warn.get("max")
                if (wmin is not None and min_v < wmin) or (wmax is not None and max_v > wmax):
                    status = "bad"
                elif in_range_pct < 90.0:
                    status = "warn"

            # Trend
            trend = "unknown"
            if history:
                prev_vars = history[-1].get("variables", [])
                prev_map = {v["var_id"]: v for v in prev_vars}
                prev = prev_map.get(var_id)
                if prev and "deviation_norm" in prev:
                    delta = deviation_norm - prev["deviation_norm"]
                    if abs(delta) < 0.05:
                        trend = "stable_in_range" if in_range_pct >= 95.0 else "stalled"
                    elif delta < -0.05:
                        trend = "improving"
                    else:
                        trend = "worsening"

            variables.append({
                "var_id": var_id,
                "name": cfg.get("name", f"0x{var_id:04X}"),
                "unit": cfg.get("unit", ""),
                "count": n,
                "mean": round(mean, 6),
                "std": round(std, 6),
                "min": round(min_v, 6),
                "max": round(max_v, 6),
                "in_range_pct": round(in_range_pct, 2),
                "deviation": round(deviation, 6),
                "deviation_norm": round(deviation_norm, 4),
                "trend": trend,
                "status": status,
            })

        # Overall status: worst of all variables
        rank = {"ok": 0, "warn": 1, "bad": 2, "emergency": 3}
        worst = max(variables, key=lambda v: rank.get(v["status"], 0), default=None)
        overall = worst["status"] if worst else "ok"

        # Convergence
        improving = sum(1 for v in variables if v["trend"] == "improving")
        worsening = sum(1 for v in variables if v["trend"] == "worsening")
        stalled = sum(1 for v in variables if v["trend"] == "stalled")
        if worsening > improving:
            conv = "diverging"
        elif improving > worsening:
            conv = "converging"
        elif stalled >= len(variables) * 0.7:
            conv = "stalled"
        else:
            conv = "oscillating"

        # Termination
        term = None
        for v in variables:
            if v["status"] == "emergency":
                term = f"emergency: {v['name']} outside emergency bounds"
                break
        if term is None and all(v["status"] == "ok" for v in variables):
            term = "success: all variables within expected range"

        return AnalysisResult(
            overall_status=overall,
            convergence=conv,
            termination=term,
            unverified_assumptions=unverified,
            details={"variables": variables},
        )


class ThresholdAnalyzer(Analyzer):
    """
    Flag-based diagnosis (Scenario B: stage diagnostics).

    UNVERIFIED: assumes flag value > 0 means error.  Real hardware may
    use different error-coding conventions.
    """

    def analyze(self, var_data, var_configs, history):
        unverified = [
            "flag_convention: assumes >0 means error",
        ]
        variables = []
        failed_stages = []

        for var_id, values in var_data.items():
            cfg = var_configs.get(var_id, {})
            max_v = max(values) if values else 0
            name = cfg.get("name", f"0x{var_id:04X}")
            failed = max_v > 0
            variables.append({
                "var_id": var_id,
                "name": name,
                "max_value": max_v,
                "failed": failed,
                "status": "bad" if failed else "ok",
            })
            if failed:
                failed_stages.append(name)

        if failed_stages:
            overall = "bad"
            term = f"diagnosis: failures at {', '.join(failed_stages)}"
        else:
            overall = "ok"
            term = "success: all stages passed"

        return AnalysisResult(
            overall_status=overall,
            convergence="unknown",
            termination=term,
            unverified_assumptions=unverified,
            details={"variables": variables, "failed_stages": failed_stages},
        )
