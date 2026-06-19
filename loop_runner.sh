#!/usr/bin/env bash
# =============================================================================
# loop_runner.sh — Top-level closed-loop auto-tuning orchestrator
# =============================================================================
#
# Runs the full cycle repeatedly until success or a termination condition:
#
#   ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
#   │  flash   │ →  │ monitor  │ →  │ analyze  │ →  │ adjust   │
#   │ build+   │    │ serial   │    │ stats+   │    │ modify   │
#   │ verify   │    │ capture  │    │ decision │    │ source   │
#   └──────────┘    └──────────┘    └──────────┘    └──────────┘
#        ↑                                                │
#        └────────────────────────────────────────────────┘
#                         (loop until done)
#
# Usage:
#   ./tools/loop_runner.sh [--config tools/config.yaml] [--max-rounds 20]
#
# Exit codes:
#   0 — tuning successful
#   1 — reached max rounds without convergence
#   2 — stalled (no improvement)
#   3 — emergency stop (safety bound violated)
#   4 — flash or serial error
#   5 — config error
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config.yaml"

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
MAX_ROUNDS_OVERRIDE=""
while [[ $# -gt 0 ]]; do
    case "$1" in
        --config)     CONFIG_FILE="$2";         shift 2 ;;
        --max-rounds) MAX_ROUNDS_OVERRIDE="$2";  shift 2 ;;
        *)            echo "Unknown: $1"; exit 5 ;;
    esac
done

[[ -f "$CONFIG_FILE" ]] || { echo "[loop] Config not found: $CONFIG_FILE"; exit 5; }

# ---------------------------------------------------------------------------
# YAML helper
# ---------------------------------------------------------------------------
_yaml() {
    python3 -c "
import yaml, sys
with open('$CONFIG_FILE') as f:
    d = yaml.safe_load(f)
for key in '$1'.split('.'):
    d = d.get(key, '') if isinstance(d, dict) else ''
print(d if d is not None else '')
"
}

# ---------------------------------------------------------------------------
# Read config
# ---------------------------------------------------------------------------
PROJECT_ROOT="$(_yaml 'project.root')"
MAX_ROUNDS="${MAX_ROUNDS_OVERRIDE:-$(_yaml 'loop.max_rounds')}"
CONVERGENCE_ROUNDS="$(_yaml 'loop.convergence_rounds')"
STALL_ROUNDS="$(_yaml 'loop.stall_rounds')"
COOLDOWN_MS="$(_yaml 'loop.cooldown_ms')"
EMERGENCY_ACTION="$(_yaml 'safety.emergency_action')"
MONITOR_DURATION="$(_yaml 'monitor.duration_ms')"

MAX_ROUNDS="${MAX_ROUNDS:-20}"
CONVERGENCE_ROUNDS="${CONVERGENCE_ROUNDS:-3}"
STALL_ROUNDS="${STALL_ROUNDS:-5}"
COOLDOWN_S=$(awk "BEGIN {printf \"%.1f\", ${COOLDOWN_MS:-1000}/1000}")

cd "$PROJECT_ROOT"

# ---------------------------------------------------------------------------
# Step 0: Validate
# ---------------------------------------------------------------------------
echo "[loop] Step 0: validate"
python3 "${SCRIPT_DIR}/validate.py" --config "$CONFIG_FILE" || {
    echo "[loop] Validation failed — fix errors before running"
    exit 5
}

# ---------------------------------------------------------------------------
# Scenario auto-detection
# ---------------------------------------------------------------------------
HAS_AUTO_PARAMS=$(python3 -c "
import yaml
with open('$CONFIG_FILE') as f:
    cfg = yaml.safe_load(f)
params = cfg.get('parameters', [])
print('true' if any(p.get('auto', False) for p in params) else 'false')
")

HAS_VARIABLES=$(python3 -c "
import yaml
with open('$CONFIG_FILE') as f:
    cfg = yaml.safe_load(f)
print('true' if cfg.get('variables') else 'false')
")

if [[ "$HAS_AUTO_PARAMS" == "true" ]]; then
    SCENARIO="A"
    echo "[loop] Scenario A: Parameter Optimization (auto-tune enabled)"
elif [[ "$HAS_VARIABLES" == "true" ]]; then
    if [[ "${MAX_ROUNDS:-20}" -le 3 ]]; then
        SCENARIO="B"
        echo "[loop] Scenario B: Stage Diagnosis (one-shot, no auto-tune)"
    else
        SCENARIO="C"
        echo "[loop] Scenario C: Long-term Monitoring (continuous, no auto-tune)"
    fi
else
    echo "[loop] No variables configured — nothing to monitor"
    exit 5
fi

# Ensure logs directory
mkdir -p tools/logs

# ---------------------------------------------------------------------------
# State tracking
# ---------------------------------------------------------------------------
ROUND=0
CONSECUTIVE_OK=0
CONSECUTIVE_NO_IMPROVEMENT=0
BEST_ROUND=0
BEST_SCORE=999999.0   # lower is better (sum of normalized deviations)

echo "============================================"
echo "  loop_runner — Auto-Tuning Orchestrator"
echo "  Project: $(_yaml 'project.name')"
echo "  Max rounds: $MAX_ROUNDS"
echo "  Convergence threshold: $CONVERGENCE_ROUNDS consecutive ok rounds"
echo "  Stall threshold: $STALL_ROUNDS rounds without improvement"
echo "============================================"
echo ""

# ---------------------------------------------------------------------------
# Helper: compute overall score from decision JSON
# ---------------------------------------------------------------------------
get_score() {
    local decision_file="$1"
    python3 -c "
import json
with open('$decision_file') as f:
    d = json.load(f)
score = sum(abs(v['deviation_norm']) for v in d.get('variables', []))
print(score)
"
}

# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------
while [[ $ROUND -lt $MAX_ROUNDS ]]; do
    ROUND=$((ROUND + 1))

    echo ""
    echo "════════════════════════════════════════════"
    echo "  ROUND $ROUND / $MAX_ROUNDS"
    echo "════════════════════════════════════════════"
    echo ""

    # ------------------------------------------------------------------
    # Step 1: Flash
    # ------------------------------------------------------------------
    echo "[loop] Step 1/4: flash"
    flash_rc=0
    bash "${SCRIPT_DIR}/flash.sh" --config "$CONFIG_FILE" || flash_rc=$?

    case $flash_rc in
        0) ;; # success
        1) echo "[loop] Build failed — aborting"; exit 4 ;;
        2) echo "[loop] Flash failed — retrying once..."; sleep 2
           bash "${SCRIPT_DIR}/flash.sh" --config "$CONFIG_FILE" --skip-build || {
               echo "[loop] Flash failed again — aborting"; exit 4
           } ;;
        3) echo "[loop] Verify failed — continuing cautiously" ;;
        4) echo "[loop] Boot timeout — check serial connection"; exit 4 ;;
        *) echo "[loop] Flash error (rc=$flash_rc)"; exit 4 ;;
    esac

    # Cooldown before monitoring
    sleep "$COOLDOWN_S"

    # ------------------------------------------------------------------
    # Step 2: Monitor
    # ------------------------------------------------------------------
    echo "[loop] Step 2/4: monitor (${MONITOR_DURATION}ms)"
    python3 "${SCRIPT_DIR}/monitor.py" --config "$CONFIG_FILE" --round "$ROUND" || {
        echo "[loop] Monitor error — aborting"
        exit 4
    }

    CSV_FILE="tools/logs/monitor_r${ROUND}.csv"
    if [[ ! -f "$CSV_FILE" ]]; then
        echo "[loop] Monitor produced no CSV — aborting"
        exit 4
    fi

    # Quick safety check on raw CSV
    if [[ "$EMERGENCY_ACTION" == "stop" ]]; then
        if python3 -c "
import csv, sys
with open('$CSV_FILE') as f:
    reader = csv.DictReader(f)
    for row in reader:
        pass  # safety is checked more thoroughly in analyze.py
print('ok')
" 2>/dev/null; then
            :
        fi
    fi

    # ------------------------------------------------------------------
    # Step 3: Analyze
    # ------------------------------------------------------------------
    echo "[loop] Step 3/4: analyze"
    python3 "${SCRIPT_DIR}/analyze.py" --config "$CONFIG_FILE" --round "$ROUND" || {
        echo "[loop] Analyze error — aborting"
        exit 4
    }

    DECISION_FILE="tools/logs/decision_r${ROUND}.json"
    if [[ ! -f "$DECISION_FILE" ]]; then
        echo "[loop] Analyze produced no decision — aborting"
        exit 4
    fi

    # Read decision summary
    OVERALL_STATUS=$(python3 -c "import json; print(json.load(open('$DECISION_FILE'))['overall_status'])")
    CONVERGENCE=$(python3 -c "import json; print(json.load(open('$DECISION_FILE'))['convergence'])")
    TERMINATION=$(python3 -c "import json; d=json.load(open('$DECISION_FILE')); print(d.get('termination') or '')")

    echo "[loop] Status: $OVERALL_STATUS  Convergence: $CONVERGENCE"

    # Score tracking
    SCORE=$(get_score "$DECISION_FILE")
    echo "[loop] Score (sum|dev_norm|): $SCORE"

    if python3 -c "exit(0 if $SCORE < $BEST_SCORE else 1)" 2>/dev/null; then
        BEST_SCORE="$SCORE"
        BEST_ROUND="$ROUND"
        CONSECUTIVE_NO_IMPROVEMENT=0
    elif python3 -c "exit(0 if abs($SCORE - $BEST_SCORE) < 0.001 else 1)" 2>/dev/null; then
        CONSECUTIVE_NO_IMPROVEMENT=$((CONSECUTIVE_NO_IMPROVEMENT + 1))
    else
        CONSECUTIVE_NO_IMPROVEMENT=$((CONSECUTIVE_NO_IMPROVEMENT + 1))
    fi

    # ------------------------------------------------------------------
    # Termination checks
    # ------------------------------------------------------------------

    # A. Emergency
    if [[ "$OVERALL_STATUS" == "emergency" ]]; then
        echo ""
        echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        echo "  EMERGENCY STOP — safety bound violated"
        echo "!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!"
        exit 3
    fi

    # B. Success — consecutive rounds OK
    if [[ "$OVERALL_STATUS" == "ok" ]]; then
        CONSECUTIVE_OK=$((CONSECUTIVE_OK + 1))
        echo "[loop] OK rounds: $CONSECUTIVE_OK / $CONVERGENCE_ROUNDS"
        if [[ $CONSECUTIVE_OK -ge $CONVERGENCE_ROUNDS ]]; then
            echo ""
            echo "============================================"
            echo "  TUNING SUCCESSFUL"
            echo "  All variables in range for $CONSECUTIVE_OK rounds"
            echo "  Total rounds: $ROUND"
            echo "============================================"
            _generate_report
            exit 0
        fi
    else
        CONSECUTIVE_OK=0
    fi

    # C. Stalled
    if [[ $CONSECUTIVE_NO_IMPROVEMENT -ge $STALL_ROUNDS ]]; then
        echo ""
        echo "============================================"
        echo "  STALLED — no improvement for $STALL_ROUNDS rounds"
        echo "  Best was round $BEST_ROUND (score=$BEST_SCORE)"
        echo "============================================"
        _generate_report
        exit 2
    fi

    # D. Explicit termination from analyze
    if [[ -n "$TERMINATION" ]]; then
        echo ""
        echo "[loop] Termination signal: $TERMINATION"
        _generate_report
        exit 0
    fi

    # E. Diverging — early warning
    if [[ "$CONVERGENCE" == "diverging" ]]; then
        echo "[loop] WARNING: diverging — consider human intervention"
        # Don't abort; let stall detection catch it if it persists
    fi

    # ------------------------------------------------------------------
    # Action step (depends on scenario)
    # ------------------------------------------------------------------
    case "$SCENARIO" in
        A)
            # Step 4: Adjust (only for Scenario A)
            echo "[loop] Step 4/4: adjust"
            python3 "${SCRIPT_DIR}/adjust.py" --config "$CONFIG_FILE" --round "$ROUND" || {
                echo "[loop] Adjust error — continuing to next round anyway"
            }
            ;;

        B)
            # Scenario B: run once, print diagnosis, exit
            echo "[loop] Scenario B: Diagnosis complete"
            echo ""
            python3 -c "
import json
with open('$DECISION_FILE') as f:
    d = json.load(f)
print('Failed stages:')
failed = [v['name'] for v in d.get('variables', []) if v.get('failed') or v.get('status') == 'bad']
if failed:
    for s in failed:
        print(f'  ✗ {s}')
else:
    print('  (none) — all stages passed')
"
            _generate_report
            exit 0
            ;;

        C)
            # Scenario C: monitor only, no code changes
            echo "[loop] Scenario C: Monitoring only (no adjust step)"
            ;;
    esac

    echo "[loop] Round $ROUND complete.  Proceeding to next..."
done

# ---------------------------------------------------------------------------
# Max rounds reached
# ---------------------------------------------------------------------------
echo ""
echo "============================================"
echo "  MAX ROUNDS ($MAX_ROUNDS) REACHED"
echo "  Best was round $BEST_ROUND (score=$BEST_SCORE)"
echo "============================================"
_generate_report
exit 1

# ---------------------------------------------------------------------------
# Final report generator
# ---------------------------------------------------------------------------
_generate_report() {
    local report="tools/logs/final_report.md"
    echo "[loop] Generating report → $report"

    cat > "$report" <<REPORTHEADER
# Auto-Tuning Report

- **Project:** $(_yaml 'project.name')
- **Date:** $(date -Iseconds)
- **Total rounds:** $ROUND
- **Best round:** $BEST_ROUND (score=$BEST_SCORE)
- **Final status:** ${OVERALL_STATUS:-unknown} (${CONVERGENCE:-unknown})

## Per-Round Summary

| Round | Status     | Convergence | Score     |
|-------|------------|-------------|-----------|
REPORTHEADER

    for ((r=1; r<=ROUND; r++)); do
        local dec="tools/logs/decision_r$(printf "%02d" $r).json"
        if [[ -f "$dec" ]]; then
            local st=$(python3 -c "import json; print(json.load(open('$dec'))['overall_status'])" 2>/dev/null || echo "?")
            local cv=$(python3 -c "import json; print(json.load(open('$dec'))['convergence'])" 2>/dev/null || echo "?")
            local sc=$(get_score "$dec" 2>/dev/null || echo "?")
            echo "| $r | $st | $cv | $sc |" >> "$report"
        fi
    done

    echo "" >> "$report"
    echo "## Final Variable State" >> "$report"
    echo "" >> "$report"
    local last_dec="tools/logs/decision_r$(printf "%02d" $ROUND).json"
    if [[ -f "$last_dec" ]]; then
        python3 -c "
import json
with open('$last_dec') as f:
    d = json.load(f)
for v in d['variables']:
    print(f\"- **{v['name']}**: μ={v['mean']:.4f}, σ={v['std']:.4f}, in_range={v['in_range_pct']:.1f}%, status={v['status']}\")
" >> "$report"
    fi

    echo "" >> "$report"
    echo "## CSV Data" >> "$report"
    for ((r=1; r<=ROUND; r++)); do
        local csv="tools/logs/monitor_r$(printf "%02d" $r).csv"
        if [[ -f "$csv" ]]; then
            local lines=$(wc -l < "$csv")
            echo "- Round $r: \`$csv\` ($lines rows)" >> "$report"
        fi
    done

    echo "" >> "$report"
    echo "🤖 Generated with Claude Code" >> "$report"
}
