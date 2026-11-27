"""Install the packaged plugin or link the MCP runtime to a development checkout."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from pathlib import Path


def _unlink_managed_link(path: Path) -> None:
    try:
        entry = path.lstat()
    except FileNotFoundError:
        return
    if stat.S_ISLNK(entry.st_mode):
        path.unlink()
    elif os.name == "nt" and entry.st_reparse_tag == stat.IO_REPARSE_TAG_MOUNT_POINT:
        path.rmdir()


def _write_mcp_config(plugin_root: Path, runtime_root: str, python_relative: str) -> None:
    config = {
        "mcpServers": {
            "codex_orbit": {
                "command": f"{runtime_root}/{python_relative}",
                "args": ["-u", f"{runtime_root}/server/main.py", "--data-dir=${PLUGIN_DATA}"],
                "env": {
                    "PYTHONUTF8": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "CODEX_ORBIT_APP_HTML": f"{runtime_root}/dist/index.html",
                },
            },
        },
    }
    stream = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=plugin_root, delete=False)
    temp_path = Path(stream.name)
    try:
        with stream:
            json.dump(config, stream, indent=2)
            stream.write("\n")
        temp_path.replace(plugin_root / "mcp_config.json")
    finally:
        temp_path.unlink(missing_ok=True)


def setup_plugin(repo_root: Path, plugin_root: Path, *, dev_link: bool = False) -> int:
    if plugin_root == repo_root:
        raise ValueError("Run the installer from a checkout separate from the installed plugin.")
    python_relative = ".venv/Scripts/python.exe" if os.name == "nt" else ".venv/bin/python"
    files = ["plugin.json"]
    directories = ["assets", "skills"]
    if not dev_link:
        files.append("pyproject.toml")
        directories.extend(("server", "dist"))
    for path in [*files, *directories, "server/main.py", "dist/index.html"]:
        if not (repo_root / path).exists():
            raise FileNotFoundError(f"Missing plugin artifact: {repo_root / path}")
    if dev_link and not (repo_root / python_relative).is_file():
        raise FileNotFoundError(f"Create the development virtual environment at {repo_root / '.venv'} first.")

    _unlink_managed_link(plugin_root)
    plugin_root.mkdir(parents=True, exist_ok=True)
    for name in files:
        destination = plugin_root / name
        _unlink_managed_link(destination)
        shutil.copy2(repo_root / name, destination)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in directories:
        destination = plugin_root / name
        _unlink_managed_link(destination)
        shutil.copytree(repo_root / name, destination, dirs_exist_ok=True, ignore=ignore)

    if dev_link:
        _write_mcp_config(plugin_root, repo_root.as_posix(), python_relative)
        print(f"Linked {plugin_root} to {repo_root}.")
        return 0

    venv = plugin_root / ".venv"
    _unlink_managed_link(venv)
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    installed_python = plugin_root / python_relative
    subprocess.run([str(installed_python), "-m", "pip", "install", "--upgrade", "."], cwd=plugin_root, check=True)
    _write_mcp_config(plugin_root, "${PLUGIN_ROOT}", python_relative)
    print(f"Installed Codex Orbit at {plugin_root}.", flush=True)
    environment = {
        **os.environ,
        "CODEX_ORBIT_PLUGIN_DIR": str(plugin_root),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    doctor = subprocess.run(
        [str(installed_python), "-u", str(plugin_root / "server" / "delegate_cli.py"), "--doctor"],
        env=environment,
        cwd=plugin_root,
        check=False,
    )
    return doctor.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plugin-name", default="agy-codex-orbit")
    parser.add_argument("--dev-link", action="store_true")
    args = parser.parse_args()
    if sys.version_info < (3, 11):
        parser.exit(1, "Python 3.11 or newer is required.\n")
    if not re.fullmatch(r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*", args.plugin_name):
        parser.error("--plugin-name must be a single directory name containing letters, digits, dots, underscores, or hyphens")

    repo_root = Path(__file__).resolve().parents[1]
    plugins_dir = (Path.home() / ".gemini" / "config" / "plugins").resolve()
    try:
        return setup_plugin(repo_root, plugins_dir / args.plugin_name, dev_link=args.dev_link)
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        parser.exit(1, f"Codex Orbit installation failed: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
