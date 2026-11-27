"""Read-only diagnostics for the Codex Orbit runtime and installation."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from server.codex_desktop import query_codex_quota, read_codex_model_catalog
from server.codex_locator import locate_codex_binary, probe_codex_version
from server.delegation_store import runtime_pointer_path
from server.routing_policy import load_settings
from server.session_store import default_codex_home


def _runtime_pointer_check(path: Path) -> tuple[str, str, str]:
    try:
        pointer = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return "warning", "Runtime pointer will be created when the MCP server starts.", str(path)
    except (OSError, ValueError) as exc:
        return "error", "Runtime pointer could not be read.", f"{path}: {exc}"

    if isinstance(pointer, dict):
        invalid = []
        for key in ("data_dir", "python_bin", "delegate_cli"):
            value = pointer.get(key)
            if not isinstance(value, str) or not value.strip():
                invalid.append(key)
                continue
            try:
                target = Path(value).expanduser()
                exists = target.is_dir() if key == "data_dir" else target.is_file()
            except (OSError, ValueError, RuntimeError):
                exists = False
            if not exists:
                invalid.append(key)
        if not invalid:
            return "ok", "Runtime pointer targets exist.", str(path)
        return "error", "Runtime pointer has missing or invalid targets.", f"{path}: {', '.join(invalid)}"
    return "error", "Runtime pointer must contain a JSON object.", str(path)


def run_doctor(
    *,
    data_dir: Path | str | None = None,
    codex_bin: Path | str | None = None,
    codex_home: Path | str | None = None,
    plugin_dir: Path | str | None = None,
    check_quota: bool = True,
) -> dict[str, Any]:
    """Report required runtime failures and optional capability warnings."""
    home = Path(codex_home).expanduser() if codex_home is not None else default_codex_home()
    plugin_root = plugin_dir or os.environ.get("CODEX_ORBIT_PLUGIN_DIR") or os.environ.get("PLUGIN_ROOT")
    install = Path(plugin_root).expanduser() if plugin_root else (
        Path.home() / ".gemini" / "config" / "plugins" / "agy-codex-orbit"
    )
    settings = load_settings(data_dir)
    models = read_codex_model_catalog(home)
    checks: list[dict[str, str]] = []

    def add(check_id: str, status: str, summary: str, detail: str) -> None:
        checks.append({"id": check_id, "status": status, "summary": summary, "detail": detail})

    python_version = ".".join(str(part) for part in sys.version_info[:3])
    python_ok = sys.version_info >= (3, 11)
    add("python", "ok" if python_ok else "error", f"Python {python_version} (requires 3.11+)", sys.executable)

    binary = None
    try:
        binary = locate_codex_binary(codex_bin)
    except (OSError, RuntimeError, ValueError) as exc:
        add("codex_cli", "error", "Codex CLI could not be located.", str(exc))
    else:
        version = probe_codex_version(binary)
        if version:
            add("codex_cli", "ok", version, str(binary))
        else:
            add("codex_cli", "error", "Codex CLI did not return a version.", str(binary))

    sessions = home / "sessions"
    try:
        if sessions.is_dir():
            count = sum(path.is_file() for path in sessions.rglob("rollout-*.jsonl"))
            add("codex_sessions", "ok", f"{count} local session rollouts.", str(sessions))
        else:
            add("codex_sessions", "warning", "Codex sessions directory is missing.", str(sessions))
    except OSError as exc:
        add("codex_sessions", "warning", "Codex sessions directory could not be read.", f"{sessions}: {exc}")

    if check_quota:
        quota = query_codex_quota(codex_bin=binary or codex_bin, codex_home=home)["codex"]
        source = quota.get("source")
        if not quota.get("available") or not (quota.get("primary") or quota.get("secondary")):
            add("codex_quota", "warning", "Codex quota is unavailable.", quota.get("error") or "No quota windows returned.")
        elif source == "app_server":
            add("codex_quota", "ok", "Live Codex quota is available.", "Codex app-server")
        else:
            add("codex_quota", "warning", "Using quota from a local Codex rollout.", quota.get("warning") or str(source))
    else:
        add("codex_quota", "skipped", "Codex quota check was skipped.", "Disabled by the caller.")

    catalog_path = home / "models_cache.json"
    missing = sorted({settings.default_model, settings.escalation_model} - {model["slug"] for model in models})
    catalog_detail = f"{catalog_path}."
    if missing:
        catalog_detail += f" Configured models absent from the visible catalog: {', '.join(missing)}."
    add("model_catalog", "warning" if missing else "ok", f"{len(models)} visible models in the Codex catalog.", catalog_detail)

    required = ("plugin.json", "mcp_config.json")
    missing_files = [name for name in required if not (install / name).is_file()]
    has_ui_bundle = (install / "dist" / "index.html").is_file()
    if not has_ui_bundle and (install / "mcp_config.json").is_file():
        try:
            mcp_cfg = json.loads((install / "mcp_config.json").read_text(encoding="utf-8"))
            env_cfg = mcp_cfg.get("mcpServers", {}).get("codex_orbit", {}).get("env", {})
            raw_html = env_cfg.get("CODEX_ORBIT_APP_HTML") if isinstance(env_cfg, dict) else None
            if isinstance(raw_html, str) and raw_html.strip():
                resolved_html = raw_html.replace("${PLUGIN_ROOT}", str(install))
                has_ui_bundle = Path(resolved_html).expanduser().is_file()
        except (OSError, ValueError, AttributeError):
            has_ui_bundle = False
    if not has_ui_bundle:
        missing_files.append("dist/index.html")
    if missing_files:
        add("plugin_install", "error", "Plugin installation is incomplete.", f"{install}. Missing: {', '.join(missing_files)}.")
    else:
        add("plugin_install", "ok", "Plugin manifest, MCP configuration, and UI bundle are present.", str(install))

    add("runtime_pointer", *_runtime_pointer_check(runtime_pointer_path()))

    return {
        "ok": all(check["status"] != "error" for check in checks),
        "platform": sys.platform,
        "checks": checks,
        "settings": settings.to_dict(),
        "available_models": models,
    }
