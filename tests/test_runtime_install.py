from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import setup_plugin
from server import codex_desktop, codex_locator


def test_catalog_filters_hidden_models_and_sorts_by_priority(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({
        "models": [
            {"slug": "later", "priority": 10, "visibility": "list"},
            {"slug": "hidden", "priority": 0, "visibility": "hide"},
            {"slug": "also-hidden", "priority": 0, "visibility": "hidden"},
            {"slug": "unranked"},
            {
                "slug": "first",
                "display_name": "First",
                "description": "Preferred model",
                "default_reasoning_level": "high",
                "supported_reasoning_levels": [{"effort": "low"}, "high", {}],
                "priority": 1,
            },
            {"visibility": "list"},
            None,
        ]
    }), encoding="utf-8")

    catalog = codex_desktop.read_codex_model_catalog()

    assert [model["slug"] for model in catalog] == ["first", "later", "unranked"]
    assert catalog[0] == {
        "slug": "first",
        "display_name": "First",
        "description": "Preferred model",
        "efforts": ["low", "high"],
        "default_effort": "high",
    }
    assert catalog[2] == {
        "slug": "unranked", "display_name": "unranked", "description": "", "efforts": [], "default_effort": None
    }


@pytest.mark.parametrize("contents", [None, b"{oops", b"\xff", b"[]", b'{"models": {}}'])
def test_catalog_handles_missing_or_corrupt_cache(tmp_path, contents):
    if contents is not None:
        (tmp_path / "models_cache.json").write_bytes(contents)

    assert codex_desktop.read_codex_model_catalog(tmp_path) == []


@pytest.mark.parametrize(("platform_name", "machine", "location"), [
    ("darwin", "arm64", "Applications/Codex.app/Contents/Resources/codex"),
    ("linux", "x86_64", ".npm-global/bin/codex"),
    ("linux", "aarch64", ".vscode/extensions/openai.chatgpt-1.2.3/bin/linux-aarch64/codex"),
    ("darwin", "arm64", ".cursor/extensions/openai.chatgpt-1.2.3/bin/darwin-arm64/codex"),
    ("linux", "x86_64", ".antigravity/extensions/openai.chatgpt-1.2.3/bin/linux-x86_64/codex"),
])
def test_codex_locator_discovers_posix_installations(tmp_path, monkeypatch, platform_name, machine, location):
    candidate = tmp_path / location
    candidate.parent.mkdir(parents=True)
    candidate.write_text("#!/bin/sh\n", encoding="utf-8")
    candidate.chmod(0o755)

    with monkeypatch.context() as patch:
        patch.setattr(codex_locator.sys, "platform", platform_name)
        patch.setattr(codex_locator.platform, "machine", lambda: machine)
        patch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
        assert candidate in codex_locator._discover_local_fallback_codex()


@pytest.mark.parametrize("platform_name", ["darwin", "linux"])
def test_posix_paths_and_pgrep(monkeypatch, platform_name):
    def pgrep(command, **kwargs):
        assert command == ["pgrep", "-x", "Codex|codex-desktop"]
        assert kwargs["timeout"] == 3
        return subprocess.CompletedProcess(command, 0)

    def missing(*args, **kwargs):
        raise FileNotFoundError("pgrep")

    with monkeypatch.context() as patch:
        patch.setattr(codex_desktop.sys, "platform", platform_name)
        assert codex_desktop._path_key("/") == "/"
        assert codex_desktop._path_key(r"/Work/With\Backslash/") == r"/Work/With\Backslash"
        assert codex_desktop._path_key("/Work") != codex_desktop._path_key("/work")
        patch.setattr(codex_desktop.subprocess, "run", pgrep)
        assert codex_desktop._is_codex_desktop_running()
        patch.setattr(codex_desktop.subprocess, "run", missing)
        assert not codex_desktop._is_codex_desktop_running()

    if os.name != "nt":
        assert codex_desktop._norm_win_path("/") == "/"


def test_quota_rpc_passes_codex_home_and_client_version(tmp_path, monkeypatch):
    stub = tmp_path / "codex_stub.py"
    stub.write_text(
        """
import json
import os
import sys

initialize = json.loads(sys.stdin.readline())
assert initialize["params"]["clientInfo"]["version"] == "0.1.0"
print(json.dumps({"id": initialize["id"], "result": {}}), flush=True)
assert json.loads(sys.stdin.readline())["method"] == "initialized"
request = json.loads(sys.stdin.readline())
assert request["method"] == "account/rateLimits/read"
print(json.dumps({
    "id": request["id"],
    "result": {"rateLimits": {
        "planType": os.environ["CODEX_HOME"],
        "primary": {"usedPercent": 95, "resetsAt": []},
    }},
}), flush=True)
""",
        encoding="utf-8",
    )
    real_popen = subprocess.Popen
    monkeypatch.setattr(codex_desktop.subprocess, "Popen", lambda cmd, **kwargs: real_popen(
        [sys.executable, "-u", str(stub)], **kwargs
    ))
    quota = codex_desktop.query_codex_quota(codex_bin=sys.executable, codex_home=tmp_path)

    assert quota["codex"]["source"] == "app_server"
    assert quota["codex"]["plan_type"] == str(tmp_path)
    assert quota["codex"]["primary"]["remaining_percent"] == 5
    assert quota["codex"]["primary"]["resets_at"] is None
    assert quota["antigravity"]["note"] == "Check the Antigravity account panel for quota details."


def test_quota_rpc_timeout_applies_when_process_sends_no_lines(monkeypatch):
    real_popen = subprocess.Popen
    monkeypatch.setattr(codex_desktop.subprocess, "Popen", lambda cmd, **kwargs: real_popen(
        [sys.executable, "-c", "import time; time.sleep(2)"], **kwargs
    ))

    with pytest.raises(TimeoutError, match="response"):
        codex_desktop._call_app_server_rpc(Path(sys.executable), "account/rateLimits/read", {}, timeout=0.2)


def test_quota_fallback_skips_malformed_windows_and_non_objects(tmp_path):
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "rollout-synthetic.jsonl").write_text("\n".join([
        json.dumps({"payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": 90},
        }}}),
        json.dumps({"payload": {"type": "token_count", "rate_limits": {
            "primary": {"used_percent": float("nan")},
        }}}),
        json.dumps(["token_count"]),
    ]), encoding="utf-8")

    quota = codex_desktop.query_codex_quota(codex_bin=tmp_path / "missing-codex", codex_home=tmp_path)

    assert quota["codex"]["primary"]["remaining_percent"] == 10


@pytest.fixture
def installer_source(tmp_path):
    repo = tmp_path / "source checkout"
    for name in ("plugin.json", "pyproject.toml", "assets/logo.svg", "skills/skill.md",
                 "server/main.py", "server/delegate_cli.py", "dist/index.html"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name, encoding="utf-8")
    return repo


@pytest.mark.parametrize(("os_name", "python_relative"), [
    ("nt", ".venv/Scripts/python.exe"),
    ("posix", ".venv/bin/python"),
])
def test_installer_copies_complete_plugin_and_runs_doctor(
    tmp_path, monkeypatch, installer_source, os_name, python_relative
):
    plugin_root = tmp_path / "config" / "plugins" / "custom-name"
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1 if command[-1] == "--doctor" else 0)

    monkeypatch.setattr(setup_plugin.subprocess, "run", run)
    monkeypatch.setattr(setup_plugin, "os", SimpleNamespace(name=os_name, environ=os.environ))

    assert setup_plugin.setup_plugin(installer_source, plugin_root) == 1
    for source in installer_source.rglob("*"):
        if source.is_file():
            assert (plugin_root / source.relative_to(installer_source)).read_bytes() == source.read_bytes()

    config = json.loads((plugin_root / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"]["codex_orbit"]
    assert config["command"] == "${PLUGIN_ROOT}/" + python_relative
    assert config["args"] == ["-u", "${PLUGIN_ROOT}/server/main.py", "--data-dir=${PLUGIN_DATA}"]
    assert config["env"]["CODEX_ORBIT_APP_HTML"] == "${PLUGIN_ROOT}/dist/index.html"
    assert calls[0][0] == [sys.executable, "-m", "venv", str(plugin_root / ".venv")]
    assert calls[1][0] == [str(plugin_root / python_relative), "-m", "pip", "install", "--upgrade", "."]
    assert calls[1][1]["cwd"] == plugin_root
    assert calls[2][0] == [str(plugin_root / python_relative), "-u", str(plugin_root / "server/delegate_cli.py"), "--doctor"]
    assert calls[2][1]["env"]["CODEX_ORBIT_PLUGIN_DIR"] == str(plugin_root)


def test_dev_link_replaces_stale_link_without_touching_source(tmp_path, installer_source):
    repo = installer_source
    python_relative = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    runtime = repo / python_relative
    runtime.parent.mkdir(parents=True)
    runtime.touch()
    target = tmp_path / "installed-plugin"
    if os.name == "nt":
        escaped_repo = str(repo).replace("'", "''")
        escaped_target = str(target).replace("'", "''")
        subprocess.run([
            "powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
            f"New-Item -ItemType Junction -Path '{escaped_target}' -Target '{escaped_repo}'",
        ], check=True, capture_output=True)
    else:
        target.symlink_to(repo, target_is_directory=True)

    assert setup_plugin.setup_plugin(repo, target, dev_link=True) == 0

    assert target.resolve() == target
    assert not (repo / "mcp_config.json").exists()
    assert (repo / "server/main.py").read_text(encoding="utf-8") == "server/main.py"
    assert (target / "assets/logo.svg").is_file()
    assert (target / "skills/skill.md").is_file()
    assert not (target / "dist").exists()
    config = json.loads((target / "mcp_config.json").read_text(encoding="utf-8"))["mcpServers"]["codex_orbit"]
    assert config["command"] == runtime.as_posix()
    assert config["args"][1] == (repo / "server/main.py").as_posix()
    assert config["env"]["CODEX_ORBIT_APP_HTML"] == (repo / "dist/index.html").as_posix()
