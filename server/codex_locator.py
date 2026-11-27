"""Locate the OpenAI Codex CLI on Windows, macOS, and Linux."""

from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import sys
from pathlib import Path

_VERSION_RE = re.compile(r"(\d+)\.(\d+)\.(\d+)")


class CodexBinaryNotFoundError(FileNotFoundError):
    """Raised when no usable local `codex` CLI binary can be located."""


def _version_sort_key(path: Path) -> tuple[int, int, int, float]:
    match = next((m for part in reversed(path.parts) if (m := _VERSION_RE.search(part))), None)
    if match:
        ver = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    else:
        ver = (0, 0, 0)
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    return (*ver, mtime)


def _discover_windows_appx_codex() -> list[Path]:
    if sys.platform != "win32":
        return []
    found: list[Path] = []
    try:
        proc = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                "(Get-AppxPackage -Name 'OpenAI.Codex*' -ErrorAction SilentlyContinue).InstallLocation",
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            for line in proc.stdout.splitlines():
                loc = line.strip()
                if not loc:
                    continue
                candidate = Path(loc) / "app" / "resources" / "codex.exe"
                if candidate.is_file():
                    found.append(candidate)
    except (OSError, subprocess.TimeoutExpired):
        pass

    windows_apps = Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "WindowsApps"
    try:
        if windows_apps.exists():
            for pkg in windows_apps.glob("OpenAI.Codex_*"):
                candidate = pkg / "app" / "resources" / "codex.exe"
                if candidate.is_file() and candidate not in found:
                    found.append(candidate)
    except OSError:
        pass

    found.sort(key=_version_sort_key, reverse=True)
    return found


def _discover_local_fallback_codex() -> list[Path]:
    home = Path.home()
    windows = sys.platform == "win32"
    executable = "codex.exe" if windows else "codex"
    direct = [
        home / ".codex" / "plugins" / ".plugin-appserver" / executable,
        home / ".codex" / ".sandbox-bin" / executable,
    ]
    if not windows:
        if sys.platform == "darwin":
            app_binary = Path("Codex.app/Contents/Resources/codex")
            direct.extend((Path("/Applications") / app_binary, home / "Applications" / app_binary))
        direct.extend((
            Path("/opt/homebrew/bin/codex"),
            Path("/usr/local/bin/codex"),
            home / ".npm-global" / "bin" / "codex",
            home / ".local" / "bin" / "codex",
        ))
    candidates = [p for p in direct if p.is_file() and (windows or os.access(p, os.X_OK))]

    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    platform_name = "windows" if windows else sys.platform
    alias = {"aarch64": "arm64", "x86_64": "x64"}.get(arch)
    targets = [f"{platform_name}-{arch}"]
    if alias:
        targets.append(f"{platform_name}-{alias}")
    if windows and arch != "x86_64":
        targets.append("windows-x86_64")

    for extension_dir in (".vscode", ".vscode-insiders", ".cursor", ".antigravity"):
        ext_root = home / extension_dir / "extensions"
        try:
            ext_bins = sorted(
                (p for target in targets for p in ext_root.glob(f"openai.chatgpt-*/bin/{target}/{executable}")),
                key=_version_sort_key,
                reverse=True,
            )
            candidates.extend(p for p in ext_bins if p.is_file() and (windows or os.access(p, os.X_OK)))
        except OSError:
            continue

    return candidates


def locate_codex_binary(explicit: str | Path | None = None) -> Path:
    """Locate the best available local `codex` CLI executable.

    Resolution order:
    1. Explicit `explicit` argument
    2. `CODEX_ORBIT_CODEX_BIN` environment variable
    3. `PATH` lookup (`codex` / `codex.exe` / `codex.cmd`)
    4. Windows Store `OpenAI.Codex*\\app\\resources\\codex.exe`
    5. Local app bundles, package manager paths, and editor extensions
    """
    checked: list[str] = []

    if explicit:
        p = Path(explicit).expanduser()
        if p.is_file():
            return p.resolve()
        raise CodexBinaryNotFoundError(f"Configured Codex binary not found: {p}")

    env_bin = os.environ.get("CODEX_ORBIT_CODEX_BIN")
    if env_bin:
        p = Path(env_bin).expanduser()
        if p.is_file():
            return p.resolve()
        raise CodexBinaryNotFoundError(
            f"CODEX_ORBIT_CODEX_BIN points to a missing file: {p}"
        )

    names = ("codex", "codex.exe", "codex.cmd") if sys.platform == "win32" else ("codex",)
    for cmd_name in names:
        hit = shutil.which(cmd_name)
        if hit:
            return Path(hit).resolve()
    checked.append("PATH (codex)")

    appx_bins = _discover_windows_appx_codex()
    if appx_bins:
        return appx_bins[0]
    if sys.platform == "win32":
        checked.append("WindowsApps/OpenAI.Codex*/app/resources/codex.exe")

    fallbacks = _discover_local_fallback_codex()
    if fallbacks:
        return fallbacks[0]
    checked.append("Codex Desktop, local install paths, and editor extensions")

    raise CodexBinaryNotFoundError(
        "Unable to locate local Codex CLI executable. Checked: "
        + ", ".join(checked)
        + ". Install Codex CLI (`npm i -g @openai/codex`) or set CODEX_ORBIT_CODEX_BIN."
    )


def probe_codex_version(binary: Path) -> str | None:
    try:
        proc = subprocess.run(
            [str(binary), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            check=False,
        )
        if proc.returncode == 0:
            line = (proc.stdout or proc.stderr).strip().splitlines()
            return line[0].strip() if line else None
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None
