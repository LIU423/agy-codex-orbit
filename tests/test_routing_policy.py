"""Unit tests for the 3-mode model routing policy and CLI integration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server import delegate_cli, delegation_store
from server.delegate_cli import parse_args, run_delegation
from server.routing_policy import (
    DEFAULT_ROUTING_MODE,
    MODEL_ASTRA,
    MODEL_SOL,
    ROUTING_MODE_LABELS,
    VALID_UI_LOCALES,
    OrbitSettings,
    load_routing_mode,
    load_settings,
    resolve_route,
    save_routing_mode,
    save_settings,
    save_ui_locale,
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
        parse_args([
            "--plan-route", "--task-tier", "complex", "--quota-remaining-5h", "100",
            "--data-dir", str(data_dir),
        ])
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


def test_settings_upgrades_legacy_file_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "settings.json"
    legacy = {"routing_mode": "silent_low_cost", "ui_locale": "ja_JP", "other": {"preserved": True}}
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert load_settings(tmp_path) == OrbitSettings(routing_mode="silent_low_cost", ui_locale="ja")
    assert VALID_UI_LOCALES == ("auto", "zh", "en", "ja")

    replace = delegation_store.os.replace
    replacements: list[dict] = []

    def observe_replace(src: Path, dest: Path) -> None:
        assert src.parent == dest.parent == tmp_path
        assert json.loads(dest.read_text(encoding="utf-8")) == legacy
        replacements.append(json.loads(src.read_text(encoding="utf-8")))
        replace(src, dest)

    with monkeypatch.context() as context:
        context.setattr(delegation_store.os, "replace", observe_replace)
        settings = save_settings(
            tmp_path,
            default_model="  custom-default ",
            escalation_model="custom-escalation",
            quota_guardrail_enabled=False,
            quota_guardrail_threshold_pct=80,
        )
    assert replacements == [{"other": legacy["other"], **settings.to_dict()}]
    assert settings == load_settings(tmp_path)
    assert settings.default_model == "custom-default"
    assert settings.escalation_model == "custom-escalation"
    assert settings.quota_guardrail_enabled is False
    assert settings.quota_guardrail_threshold_pct == 50
    assert list(tmp_path.iterdir()) == [path]

    save_ui_locale("zh-Hant", tmp_path)
    save_routing_mode("silent_best", tmp_path)
    assert load_settings(tmp_path) == OrbitSettings(
        routing_mode="silent_best",
        ui_locale="zh-TW",
        default_model="custom-default",
        escalation_model="custom-escalation",
        quota_guardrail_enabled=False,
        quota_guardrail_threshold_pct=50,
    )
    reset = save_settings(tmp_path, default_model=None, escalation_model=" ", quota_guardrail_threshold_pct=0)
    assert (reset.default_model, reset.escalation_model, reset.quota_guardrail_threshold_pct) == (
        MODEL_SOL, MODEL_ASTRA, 5,
    )


@pytest.mark.parametrize(
    "raw",
    [
        b"not-json",
        b"\xff",
        b"[]",
        b'{"routing_mode":"invalid","ui_locale":"fr","default_model":null,"escalation_model":false,'
        b'"quota_guardrail_enabled":"false","quota_guardrail_threshold_pct":true}',
    ],
)
def test_invalid_settings_load_defaults(tmp_path: Path, raw: bytes) -> None:
    (tmp_path / "settings.json").write_bytes(raw)
    assert load_settings(tmp_path) == OrbitSettings()


@pytest.mark.parametrize("updates", [{"ui_locale": "fr"}, {"quota_guardrail_threshold_pct": 9.5}])
def test_invalid_settings_update_keeps_previous_file(tmp_path: Path, updates: dict) -> None:
    original = save_settings(tmp_path, default_model="custom")
    with pytest.raises(ValueError):
        save_settings(tmp_path, default_model="changed", **updates)
    assert load_settings(tmp_path) == original


@pytest.mark.parametrize("tier,effort", [("standard", "high"), ("complex", "xhigh")])
def test_max_guardrail_uses_configured_default_and_preserves_effort(tier: str, effort: str) -> None:
    kwargs = {
        "routing_mode": "silent_best",
        "task_tier": tier,
        "default_model": "custom-default",
        "escalation_model": "custom-escalation",
        "quota_guardrail_threshold_pct": 20,
    }
    low = resolve_route(**kwargs, quota_remaining_5h=19.9)
    assert (low.model, low.reasoning_effort, low.recommended_model) == (
        "custom-default", effort, "custom-escalation",
    )
    assert low.requires_confirmation is False
    assert low.to_dict()["guardrail_triggered"] is True
    assert "5-hour quota" in low.to_dict()["guardrail_note"]

    for remaining in (None, 20, 40):
        normal = resolve_route(**kwargs, quota_remaining_5h=remaining)
        assert normal.model == "custom-escalation"
        assert normal.guardrail_triggered is False
        assert normal.guardrail_note is None
    disabled = resolve_route(**kwargs, quota_remaining_5h=0, quota_guardrail_enabled=False)
    assert disabled.model == "custom-escalation"
    assert disabled.guardrail_triggered is False


def test_low_quota_only_advises_balanced_complex_and_ignores_identical_max_models() -> None:
    assert ROUTING_MODE_LABELS == {"silent_low_cost": "Eco", "ask": "Balanced", "silent_best": "Max"}
    balanced = resolve_route(
        routing_mode="ask", task_tier="complex", isolation_mode="read-only",
        default_model="custom-default", escalation_model="custom-escalation", quota_remaining_5h=5,
    )
    assert balanced.requires_confirmation is True
    assert balanced.model == balanced.fallback_model == "custom-default"
    assert balanced.recommended_model == "custom-escalation"
    assert balanced.recommended_effort == "high"
    assert balanced.guardrail_triggered is True
    assert "Consider staying on custom-default" in balanced.guardrail_note

    for mode, tier in (("silent_low_cost", "complex"), ("silent_best", "light"), ("ask", "standard")):
        route = resolve_route(routing_mode=mode, task_tier=tier, quota_remaining_5h=0)
        assert route.model == MODEL_SOL
        assert route.guardrail_triggered is False
    same_model = resolve_route(
        routing_mode="silent_best", default_model=MODEL_ASTRA, quota_remaining_5h=0,
    )
    assert same_model.model == MODEL_ASTRA
    assert same_model.guardrail_triggered is False


def test_cli_settings_flags_persist_partial_updates(tmp_path: Path) -> None:
    def cli(*args: str) -> tuple[int, dict]:
        return run_delegation(parse_args([*args, "--data-dir", str(tmp_path)]))

    assert cli("--get-settings") == (0, OrbitSettings().to_dict())
    code, updated = cli(
        "--set-routing-mode", "silent_best", "--set-default-model", "custom-default",
        "--set-escalation-model", "custom-escalation", "--set-quota-guardrail", "off",
        "--set-quota-threshold", "31",
    )
    assert code == 0
    assert updated == OrbitSettings(
        routing_mode="silent_best", default_model="custom-default", escalation_model="custom-escalation",
        quota_guardrail_enabled=False, quota_guardrail_threshold_pct=31,
    ).to_dict()
    assert cli("--get-settings") == (0, updated)
    assert cli("--set-default-model", "", "--set-quota-guardrail", "on") == (
        0, {**updated, "default_model": MODEL_SOL, "quota_guardrail_enabled": True},
    )
    assert cli("--get-routing-mode") == (0, {"routing_mode": "silent_best", "routing_mode_label": "Max"})


def test_cli_plan_quota_probe_and_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    quota = {"codex": {"available": True, "source": "app_server", "primary": {"remaining_percent": 7}}}
    calls: list[str | None] = []

    def query(*, codex_bin: str | None) -> dict:
        calls.append(codex_bin)
        return quota

    monkeypatch.setattr(delegate_cli, "query_codex_quota", query)
    settings = save_settings(
        tmp_path, routing_mode="silent_best", default_model="custom-default", escalation_model="custom-escalation",
    )

    def plan(*args: str) -> dict:
        code, result = run_delegation(parse_args(["--plan-route", "--data-dir", str(tmp_path), *args]))
        assert code == 0
        return result

    light = plan("--task-tier", "light")
    assert "quota" not in light
    assert not calls

    result = plan("--task-tier", "standard")
    assert result["quota"] == quota
    assert result["guardrail_triggered"] is True
    assert result["model"] == settings.default_model
    assert calls == [None]

    override = plan("--routing-mode", "ask", "--task-tier", "complex", "--quota-remaining-5h", "0")
    assert override["quota"]["codex"]["source"] == "override"
    assert override["quota"]["codex"]["primary"]["remaining_percent"] == 0
    assert override["requires_confirmation"] is True
    assert override["guardrail_triggered"] is True
    assert calls == [None]

    quota["codex"] = {"available": False, "error": "Quota unavailable"}
    result = plan("--task-tier", "complex")
    assert result["quota"] == quota
    assert result["guardrail_triggered"] is False
    assert result["model"] == settings.escalation_model
    assert calls == [None, None]

    save_settings(tmp_path, quota_guardrail_enabled=False)
    assert "quota" not in plan("--task-tier", "complex")
    assert plan("--routing-mode", "ask", "--task-tier", "complex")["quota"] == quota
    assert calls == [None, None, None]


def test_cli_doctor_and_prune_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def cli(action: str) -> tuple[int, dict]:
        return run_delegation(parse_args([action, "--data-dir", str(tmp_path)]))

    report = {"ok": True}

    def doctor(*, data_dir: str, codex_bin: str | None) -> dict:
        assert data_dir == str(tmp_path)
        assert codex_bin is None
        return report

    monkeypatch.setattr(delegate_cli, "run_doctor", doctor)
    assert cli("--doctor") == (0, report)
    report["ok"] = False
    assert cli("--doctor") == (1, report)

    result = {"removed": [], "skipped_running": [], "errors": []}

    def prune(store: delegation_store.DelegationStore) -> dict:
        assert store.data_dir == tmp_path.resolve()
        return result

    monkeypatch.setattr(delegate_cli, "prune_idle_worktrees", prune)
    assert cli("--prune-worktrees") == (0, result)
    result["errors"].append("Unable to remove worktree")
    assert cli("--prune-worktrees") == (1, result)
