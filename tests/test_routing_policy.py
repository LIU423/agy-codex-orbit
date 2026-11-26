"""Unit tests for the 3-mode model routing policy and CLI integration."""

from __future__ import annotations

from pathlib import Path

import pytest

from server.delegate_cli import parse_args, run_delegation
from server.routing_policy import (
    DEFAULT_ROUTING_MODE,
    MODEL_ASTRA,
    MODEL_SOL,
    load_routing_mode,
    resolve_route,
    save_routing_mode,
)


def test_routing_mode_persistence(tmp_path: Path) -> None:
    data_dir = tmp_path / "plugin-data"
    assert load_routing_mode(data_dir) == DEFAULT_ROUTING_MODE

    assert save_routing_mode("silent_best", data_dir) == "silent_best"
    assert load_routing_mode(data_dir) == "silent_best"

    assert save_routing_mode("silent_low_cost", data_dir) == "silent_low_cost"
    assert load_routing_mode(data_dir) == "silent_low_cost"

    with pytest.raises(ValueError):
        save_routing_mode("unknown_mode", data_dir)


def test_three_routing_policies_matrix() -> None:
    # 1. silent_low_cost: never uses Astra, never requires confirmation
    for tier, expected_effort in (("light", "medium"), ("standard", "medium"), ("complex", "high")):
        d = resolve_route(routing_mode="silent_low_cost", task_tier=tier)
        assert d.model == MODEL_SOL
        assert d.reasoning_effort == expected_effort
        assert d.requires_confirmation is False

    # 2. ask (default): silent Sol on light/standard; asks user with Astra recommendation on complex
    d_light = resolve_route(routing_mode="ask", task_tier="light")
    assert d_light.model == MODEL_SOL and d_light.requires_confirmation is False

    d_std = resolve_route(routing_mode="ask", task_tier="standard", isolation_mode="workspace-write")
    assert d_std.model == MODEL_SOL and d_std.reasoning_effort == "high" and d_std.requires_confirmation is False

    d_complex = resolve_route(routing_mode="ask", task_tier="complex")
    assert d_complex.requires_confirmation is True
    assert d_complex.recommended_model == MODEL_ASTRA
    assert d_complex.fallback_model == MODEL_SOL

    # 3. silent_best: silently picks Astra on standard/complex without asking
    b_light = resolve_route(routing_mode="silent_best", task_tier="light")
    assert b_light.model == MODEL_SOL and b_light.requires_confirmation is False

    b_std = resolve_route(routing_mode="silent_best", task_tier="standard")
    assert b_std.model == MODEL_ASTRA and b_std.reasoning_effort == "high" and b_std.requires_confirmation is False

    b_complex = resolve_route(routing_mode="silent_best", task_tier="complex")
    assert b_complex.model == MODEL_ASTRA and b_complex.reasoning_effort == "xhigh" and b_complex.requires_confirmation is False


def test_delegate_cli_routing_mode_subcommands(tmp_path: Path) -> None:
    data_dir = tmp_path / "plugin-data"

    # Default mode is ask
    code, out = run_delegation(parse_args(["--get-routing-mode", "--data-dir", str(data_dir)]))
    assert code == 0
    assert out["routing_mode"] == "ask"

    # Persist silent_best and plan a complex route -> silent Astra xhigh, no confirmation
    code, out = run_delegation(parse_args(["--set-routing-mode", "silent_best", "--data-dir", str(data_dir)]))
    assert code == 0
    assert out["routing_mode"] == "silent_best"

    code, plan = run_delegation(
        parse_args(["--plan-route", "--task-tier", "complex", "--data-dir", str(data_dir)])
    )
    assert code == 0
    assert plan["routing_mode"] == "silent_best"
    assert plan["model"] == MODEL_ASTRA
    assert plan["reasoning_effort"] == "xhigh"
    assert plan["requires_confirmation"] is False

    # Override to ask on complex -> requires_confirmation + quota field present
    code, plan_ask = run_delegation(
        parse_args(
            [
                "--plan-route",
                "--routing-mode",
                "ask",
                "--task-tier",
                "complex",
                "--data-dir",
                str(data_dir),
                "--codex-bin",
                str(tmp_path / "missing-codex.exe"),
            ]
        )
    )
    assert code == 0
    assert plan_ask["requires_confirmation"] is True
    assert plan_ask["recommended_model"] == MODEL_ASTRA
    assert "quota" in plan_ask
