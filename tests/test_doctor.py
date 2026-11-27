"""Diagnostics against isolated Codex data and plugin installations."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from server import doctor
from server.codex_locator import CodexBinaryNotFoundError
from server.routing_policy import MODEL_ASTRA, MODEL_SOL, save_settings


def test_doctor_healthy_installation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    settings = save_settings(data_dir, default_model="custom", ui_locale="ja")
    codex_home = tmp_path / "codex"
    sessions = codex_home / "sessions" / "2026" / "11" / "27"
    sessions.mkdir(parents=True)
    (sessions / "rollout-test.jsonl").touch()
    (sessions / "ignored.jsonl").touch()
    (codex_home / "models_cache.json").write_text(json.dumps({"models": [
        {"slug": "hidden", "visibility": "hide"},
        {"slug": "custom", "priority": 2},
        {"slug": MODEL_ASTRA, "priority": 1},
    ]}), encoding="utf-8")

    install = tmp_path / "plugin"
    (install / "dist").mkdir(parents=True)
    for name in ("plugin.json", "mcp_config.json", "dist/index.html", "delegate_cli.py"):
        (install / name).touch()
    pointer = tmp_path / "runtime.json"
    pointer.write_text(json.dumps({
        "data_dir": str(data_dir),
        "python_bin": sys.executable,
        "delegate_cli": str(install / "delegate_cli.py"),
    }), encoding="utf-8")
    binary = tmp_path / "codex-cli"

    def quota(*, codex_bin: Path, codex_home: Path) -> dict:
        assert codex_bin == binary
        assert codex_home == tmp_path / "codex"
        return {"codex": {"available": True, "source": "app_server", "primary": {"remaining_percent": 10.0}}}

    monkeypatch.setenv("CODEX_ORBIT_PLUGIN_DIR", str(install))
    monkeypatch.setattr(doctor, "runtime_pointer_path", lambda: pointer)
    monkeypatch.setattr(doctor, "locate_codex_binary", lambda explicit: Path(explicit))
    monkeypatch.setattr(doctor, "probe_codex_version", lambda path: "codex-cli test" if path == binary else None)
    monkeypatch.setattr(doctor, "query_codex_quota", quota)

    report = doctor.run_doctor(data_dir=data_dir, codex_home=codex_home, codex_bin=str(binary))
    assert report["ok"] is True
    assert report["platform"] == sys.platform
    assert report["settings"] == settings.to_dict()
    assert [model["slug"] for model in report["available_models"]] == [MODEL_ASTRA, "custom"]
    checks = {check["id"]: check for check in report["checks"]}
    assert set(checks) == {
        "python", "codex_cli", "codex_sessions", "codex_quota", "model_catalog", "plugin_install", "runtime_pointer",
    }
    assert all(check["status"] == "ok" for check in checks.values())
    assert checks["codex_sessions"]["summary"].startswith("1 local")


@pytest.mark.parametrize("quota_source", ["rollout_cache", None])
def test_doctor_reports_failures_and_optional_checks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, quota_source: str | None,
) -> None:
    pointer = tmp_path / "runtime.json"
    pointer.write_bytes(b"\xff")
    monkeypatch.setattr(doctor, "runtime_pointer_path", lambda: pointer)

    def missing_cli(_: object) -> Path:
        raise CodexBinaryNotFoundError("Missing CLI")

    quota_calls = []

    def quota(**kwargs: object) -> dict:
        quota_calls.append(kwargs)
        return {"codex": {
            "available": quota_source is not None,
            "source": quota_source,
            "primary": {"remaining_percent": 10.0} if quota_source else None,
        }}

    monkeypatch.setattr(doctor, "locate_codex_binary", missing_cli)
    monkeypatch.setattr(doctor, "query_codex_quota", quota)

    options = {
        "data_dir": tmp_path / "data",
        "codex_home": tmp_path / "codex",
        "plugin_dir": tmp_path / "plugin",
    }
    report = doctor.run_doctor(**options)
    assert report["ok"] is False
    assert report["available_models"] == []
    checks = {check["id"]: check for check in report["checks"]}
    for check_id in ("codex_cli", "plugin_install", "runtime_pointer"):
        assert checks[check_id]["status"] == "error"
    for check_id in ("codex_sessions", "codex_quota", "model_catalog"):
        assert checks[check_id]["status"] == "warning"
    assert MODEL_SOL in checks["model_catalog"]["detail"]
    assert MODEL_ASTRA in checks["model_catalog"]["detail"]
    expected_quota_summary = "rollout" if quota_source else "unavailable"
    assert expected_quota_summary in checks["codex_quota"]["summary"]

    pointer.unlink()
    report = doctor.run_doctor(**options, check_quota=False)
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["runtime_pointer"]["status"] == "warning"
    assert checks["codex_quota"]["status"] == "skipped"
    assert len(quota_calls) == 1



def test_doctor_accepts_dev_linked_app_html_from_mcp_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    codex_home = tmp_path / "codex"
    (codex_home / "sessions").mkdir(parents=True)
    repo_html = tmp_path / "repo" / "dist" / "index.html"
    repo_html.parent.mkdir(parents=True)
    repo_html.write_text("<html></html>", encoding="utf-8")

    install = tmp_path / "plugin"
    install.mkdir()
    (install / "plugin.json").write_text("{}", encoding="utf-8")
    (install / "mcp_config.json").write_text(
        json.dumps({"mcpServers": {"codex_orbit": {"env": {"CODEX_ORBIT_APP_HTML": str(repo_html)}}}}),
        encoding="utf-8",
    )

    monkeypatch.setattr(doctor, "locate_codex_binary", lambda explicit: Path(tmp_path / "codex"))
    monkeypatch.setattr(doctor, "probe_codex_version", lambda path: "codex-cli test")
    monkeypatch.setattr(doctor, "runtime_pointer_path", lambda: tmp_path / "missing-runtime.json")

    report = doctor.run_doctor(data_dir=data_dir, codex_home=codex_home, plugin_dir=install, check_quota=False)
    checks = {check["id"]: check for check in report["checks"]}
    assert checks["plugin_install"]["status"] == "ok"
