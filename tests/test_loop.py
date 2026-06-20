"""Test loop_runner scene detection and exit code mapping."""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from loop_runner import (
    StepError, run_step, load_config,
    EXIT_SUCCESS, EXIT_MAX_ROUNDS, EXIT_STALLED,
    EXIT_EMERGENCY, EXIT_STEP_ERROR, EXIT_CONFIG_ERROR,
)


def _min_config(**overrides):
    """Minimal valid config for testing."""
    cfg = {
        "project": {"root": ".."},
        "loop": {"max_rounds": 10, "convergence_rounds": 3, "stall_rounds": 5},
        "parameters": [{"name": "Kp", "auto": True, "depends_on": [0x1004]}],
        "variables": [{"id": 0x1004, "name": "err"}],
        "build": {"system": "custom", "command": ["echo"], "binary": "firmware.bin"},
        "flash": {"backend": "custom", "custom": {"flash_command": ["echo"]},
                  "verify": False, "allow_no_reset": True,
                  "boot_timeout_ms": 1000},
        "serial": {"port": "/dev/null", "baudrate": 115200, "timeout_ms": 100},
    }
    cfg.update(overrides)
    return cfg


def test_step_error_carries_exit_code():
    e = StepError("flash", 4, "flash exit 2")
    assert e.step == "flash"
    assert e.exit_code == 4
    assert "flash exit 2" in e.message


def test_step_error_validate_maps_to_5():
    e = StepError("validate", 5, "validate exit 1")
    assert e.exit_code == 5


@patch("loop_runner.load_config")
@patch("loop_runner.run_step")
def test_no_variables_returns_config_error(mock_step, mock_load):
    mock_load.return_value = {
        "project": {"root": ".."},
        "loop": {"max_rounds": 10, "convergence_rounds": 1, "stall_rounds": 5},
        "parameters": [],
        "variables": [],
    }
    with patch("builtins.print"):
        from loop_runner import _run
        rc = _run(MagicMock(config="cfg.yaml", max_rounds=None))
        assert rc == EXIT_CONFIG_ERROR


@patch("loop_runner.load_config")
@patch("loop_runner.run_step")
def test_scene_b_exits_after_one_round(mock_step, mock_load):
    mock_load.return_value = {
        "project": {"root": ".."},
        "loop": {"max_rounds": 3, "convergence_rounds": 1, "stall_rounds": 5},
        "parameters": [],
        "variables": [{"id": 0x2001, "name": "stage1"}],
        "build": {"system": "custom", "command": ["echo"], "binary": "firmware.bin"},
        "flash": {"backend": "custom", "custom": {"flash_command": ["echo"]},
                  "verify": False, "allow_no_reset": True,
                  "boot_timeout_ms": 1000},
        "serial": {"port": "/dev/null", "baudrate": 115200, "timeout_ms": 100},
    }
    mock_step.return_value = 0
    with patch("loop_runner.load_decision") as mock_ld:
        mock_ld.return_value = {
            "overall_status": "bad",
            "variables": [{"var_id": 0x2001, "name": "stage1", "failed": True, "status": "bad"}],
            "termination": None,
        }
        with patch("builtins.print"):
            from loop_runner import _run
            rc = _run(MagicMock(config="cfg.yaml", max_rounds=1))
            assert rc == 0


@patch("loop_runner.load_config")
@patch("loop_runner.run_step")
def test_scene_a_convergence_exits_success(mock_step, mock_load):
    """After convergence_needed consecutive OK rounds, exit SUCCESS."""
    mock_load.return_value = _min_config()
    mock_step.return_value = 0
    decisions = [
        {"overall_status": "ok", "termination": "success: all within range",
         "variables": [{"deviation_norm": 0.01}]},
        {"overall_status": "ok", "termination": "success: all within range",
         "variables": [{"deviation_norm": 0.01}]},
        {"overall_status": "ok", "termination": "success: all within range",
         "variables": [{"deviation_norm": 0.01}]},
    ]
    with patch("loop_runner.load_decision", side_effect=decisions):
        with patch("builtins.print"):
            from loop_runner import _run
            rc = _run(MagicMock(config="cfg.yaml", max_rounds=3))
            assert rc == EXIT_SUCCESS


@patch("loop_runner.load_config")
@patch("loop_runner.run_step")
def test_scene_a_stall_exits_stalled(mock_step, mock_load):
    """After stall_limit rounds without improvement, exit STALLED."""
    mock_load.return_value = _min_config(
        loop={"max_rounds": 10, "convergence_rounds": 3, "stall_rounds": 2})
    mock_step.return_value = 0
    # Same score every round → no_improvement increments each round
    decision = {"overall_status": "bad", "termination": None,
                "variables": [{"deviation_norm": 0.5}]}
    with patch("loop_runner.load_decision", return_value=decision):
        with patch("builtins.print"):
            from loop_runner import _run
            rc = _run(MagicMock(config="cfg.yaml", max_rounds=4))
            assert rc == EXIT_STALLED


@patch("loop_runner.load_config")
@patch("loop_runner.run_step")
def test_scene_a_max_rounds_exits_1(mock_step, mock_load):
    """After max_rounds without convergence, exit MAX_ROUNDS."""
    mock_load.return_value = _min_config(
        loop={"max_rounds": 2, "convergence_rounds": 3, "stall_rounds": 10})
    mock_step.return_value = 0
    decision = {"overall_status": "warn", "termination": None,
                "variables": [{"deviation_norm": 0.3}]}
    with patch("loop_runner.load_decision", return_value=decision):
        with patch("builtins.print"):
            from loop_runner import _run
            rc = _run(MagicMock(config="cfg.yaml", max_rounds=1))
            assert rc == EXIT_MAX_ROUNDS


@patch("loop_runner.load_config")
@patch("loop_runner.run_step")
def test_scene_c_emergency_still_exits_3(mock_step, mock_load):
    """Scenario C still exits on emergency."""
    mock_load.return_value = _min_config(
        parameters=[],
        loop={"max_rounds": 100, "convergence_rounds": 1, "stall_rounds": 5},
    )
    mock_step.return_value = 0
    decision = {"overall_status": "emergency",
                "variables": [{"deviation_norm": 2.0}]}
    with patch("loop_runner.load_decision", return_value=decision):
        with patch("builtins.print"):
            from loop_runner import _run
            rc = _run(MagicMock(config="cfg.yaml", max_rounds=None))
            assert rc == EXIT_EMERGENCY


@patch("loop_runner.load_config")
@patch("loop_runner.run_step")
def test_last_round_skips_adjust(mock_step, mock_load):
    """Final round in scenario A must NOT execute adjust.py."""
    mock_load.return_value = _min_config(
        loop={"max_rounds": 2, "convergence_rounds": 10, "stall_rounds": 10})
    mock_step.return_value = 0
    decision = {"overall_status": "warn", "termination": None,
                "variables": [{"deviation_norm": 0.3}]}
    with patch("loop_runner.load_decision", return_value=decision):
        with patch("builtins.print") as mock_print:
            from loop_runner import _run
            rc = _run(MagicMock(config="cfg.yaml", max_rounds=1))
            assert rc == EXIT_MAX_ROUNDS
            # Check that "Final round" message was printed
            printed = " ".join(str(a[0]) for a in mock_print.call_args_list if a[0])
            assert "Final round" in printed or any(
                "skipping adjust" in str(a) for a in mock_print.call_args_list
            )


@patch("loop_runner.load_config")
@patch("loop_runner.run_step")
def test_flash_exit_5_maps_to_loop_exit_5(mock_step, mock_load):
    """flash.py exit 5 (config error) should produce loop exit 5, not 4."""
    mock_load.return_value = _min_config()
    from loop_runner import _run as run_fn
    with patch("builtins.print"):
        # run_step raises StepError with exit_code=5 for exit code 5
        mock_step.side_effect = StepError("flash", 5, "flash exit 5")
        rc = run_fn(MagicMock(config="cfg.yaml", max_rounds=1))
        assert rc == 5


@patch("loop_runner.load_config")
@patch("loop_runner.run_step")
def test_scene_c_completes_normally(mock_step, mock_load):
    """Scenario C runs max_rounds and exits SUCCESS."""
    mock_load.return_value = _min_config(
        parameters=[],
        loop={"max_rounds": 2, "convergence_rounds": 1, "stall_rounds": 5},
    )
    mock_step.return_value = 0
    decision = {"overall_status": "ok",
                "variables": [{"deviation_norm": 0.01}]}
    with patch("loop_runner.load_decision", return_value=decision):
        with patch("builtins.print"):
            from loop_runner import _run
            rc = _run(MagicMock(config="cfg.yaml", max_rounds=1))
            assert rc == EXIT_SUCCESS
