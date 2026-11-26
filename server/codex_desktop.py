"""Codex Desktop integration: live quota query, `agy:` thread naming, and project binding."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.codex_locator import CodexBinaryNotFoundError, locate_codex_binary
from server.session_store import default_codex_home

TITLE_PREFIX = "agy: "


def format_agy_title(raw_title: str) -> str:
    cleaned = " ".join((raw_title or "").strip().split())
    if not cleaned:
        cleaned = "Codex Task"
    if cleaned.lower().startswith("agy:"):
        rest = cleaned[4:].strip()
        cleaned = rest or "Codex Task"
    if len(cleaned) > 76:
        cleaned = cleaned[:73].rstrip() + "..."
    return f"{TITLE_PREFIX}{cleaned}"


def _norm_win_path(path: Path | str) -> str:
    s = str(Path(path).expanduser().resolve()).rstrip("\\/")
    # Keep drive root like C:\ intact
    if len(s) == 2 and s[1] == ":":
        s += "\\"
    return s


def _path_key(path: Path | str) -> str:
    s = str(path).strip().replace("/", "\\").rstrip("\\")
    return s.lower() if sys.platform == "win32" else s


def _format_reset_time(epoch_sec: int | float | None) -> tuple[str | None, str | None]:
    if epoch_sec is None:
        return None, None
    try:
        ts = float(epoch_sec)
        local_dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone()
        iso = local_dt.strftime("%Y-%m-%d %H:%M (%z)")
        delta = max(0, int(ts - time.time()))
        if delta < 60:
            rel = f"{delta}s"
        elif delta < 3600:
            rel = f"{delta // 60}m"
        elif delta < 86400:
            h, rem = divmod(delta, 3600)
            rel = f"{h}h {rem // 60}m"
        else:
            d, rem = divmod(delta, 86400)
            rel = f"{d}d {rem // 3600}h"
        return iso, rel
    except (ValueError, OSError, OverflowError):
        return None, None


def _call_app_server_rpc(
    codex_bin: Path,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Execute a single JSON-RPC request against a short-lived `codex app-server --stdio`."""
    proc = subprocess.Popen(
        [str(codex_bin), "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    assert proc.stdin is not None and proc.stdout is not None

    def _send(obj: dict[str, Any]) -> None:
        proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")  # type: ignore[union-attr]
        proc.stdin.flush()  # type: ignore[union-attr]

    def _recv_id(target_id: int, deadline: float) -> dict[str, Any]:
        while time.monotonic() < deadline:
            line = proc.stdout.readline()  # type: ignore[union-attr]
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, dict) and msg.get("id") == target_id:
                return msg
        raise TimeoutError(f"Timed out waiting for codex app-server response to {method}")

    deadline = time.monotonic() + timeout
    try:
        _send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex-orbit", "version": "0.4.0"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        init_resp = _recv_id(1, deadline)
        if "error" in init_resp:
            raise RuntimeError(f"app-server initialize failed: {init_resp['error']}")

        _send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        _send({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
        resp = _recv_id(2, deadline)
        if "error" in resp:
            raise RuntimeError(f"app-server {method} failed: {resp['error']}")
        result = resp.get("result")
        return result if isinstance(result, dict) else {"value": result}
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _format_window(win: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(win, dict):
        return None
    used = win.get("usedPercent")
    if used is None:
        used = win.get("used_percent")
    if not isinstance(used, (int, float)):
        return None
    remaining = max(0.0, min(100.0, round(100.0 - float(used), 1)))
    mins = win.get("windowDurationMins") or win.get("window_minutes")
    resets_at = win.get("resetsAt") or win.get("resets_at")
    reset_local, resets_in = _format_reset_time(resets_at)
    return {
        "used_percent": round(float(used), 1),
        "remaining_percent": remaining,
        "window_minutes": int(mins) if isinstance(mins, (int, float)) else None,
        "resets_at": int(resets_at) if isinstance(resets_at, (int, float)) else None,
        "resets_at_local": reset_local,
        "resets_in": resets_in,
    }


def _read_rollout_fallback_quota(codex_home: Path) -> dict[str, Any] | None:
    sessions_dir = codex_home / "sessions"
    if not sessions_dir.is_dir():
        return None
    files = sorted(
        sessions_dir.rglob("rollout-*.jsonl"),
        key=lambda p: p.stat().st_mtime_ns if p.exists() else 0,
        reverse=True,
    )
    for fp in files[:3]:
        try:
            with fp.open("rb") as fh:
                fh.seek(0, os.SEEK_END)
                size = fh.tell()
                fh.seek(max(0, size - 262144))
                tail = fh.read().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in reversed(tail):
            if '"token_count"' not in line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            pl = rec.get("payload")
            if isinstance(pl, dict) and pl.get("type") == "token_count" and isinstance(pl.get("rate_limits"), dict):
                rl = pl["rate_limits"]
                return {
                    "source": "rollout_cache",
                    "plan_type": rl.get("plan_type"),
                    "primary": _format_window(rl.get("primary")),
                    "secondary": _format_window(rl.get("secondary")),
                    "reset_credits_available": None,
                    "buckets": {},
                }
    return None


def query_codex_quota(
    *,
    codex_bin: Path | str | None = None,
    codex_home: Path | None = None,
) -> dict[str, Any]:
    """Query live Codex rate limits via `codex app-server --stdio` (with rollout fallback)."""
    home = codex_home or default_codex_home()
    antigravity_note = {
        "available": False,
        "note": "Antigravity 暂无公开的本机额度查询 CLI/API，请以 Antigravity 客户端账户面板为准。",
    }

    err_msg: str | None = None
    try:
        binary = locate_codex_binary(codex_bin)
        raw = _call_app_server_rpc(binary, "account/rateLimits/read", {}, timeout=8.0)
        rl = raw.get("rateLimits") if isinstance(raw.get("rateLimits"), dict) else {}
        buckets: dict[str, Any] = {}
        by_id = raw.get("rateLimitsByLimitId")
        if isinstance(by_id, dict):
            for bid, bval in by_id.items():
                if isinstance(bval, dict):
                    buckets[bid] = {
                        "limit_name": bval.get("limitName"),
                        "primary": _format_window(bval.get("primary")),
                        "secondary": _format_window(bval.get("secondary")),
                    }
        reset_credits = raw.get("rateLimitResetCredits")
        available_resets = (
            int(reset_credits.get("availableCount"))
            if isinstance(reset_credits, dict) and isinstance(reset_credits.get("availableCount"), int)
            else None
        )
        return {
            "codex": {
                "available": True,
                "source": "app_server",
                "plan_type": rl.get("planType"),
                "primary": _format_window(rl.get("primary")),
                "secondary": _format_window(rl.get("secondary")),
                "reset_credits_available": available_resets,
                "buckets": buckets,
            },
            "antigravity": antigravity_note,
        }
    except (CodexBinaryNotFoundError, OSError, TimeoutError, RuntimeError) as exc:
        err_msg = str(exc)

    fallback = _read_rollout_fallback_quota(home)
    if fallback is not None:
        return {
            "codex": {"available": True, "warning": err_msg, **fallback},
            "antigravity": antigravity_note,
        }

    return {
        "codex": {"available": False, "error": err_msg or "Unable to query Codex quota."},
        "antigravity": antigravity_note,
    }


def _upsert_session_index_title(codex_home: Path, session_id: str, title: str) -> None:
    index_path = codex_home / "session_index.jsonl"
    now_iso = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    entry = json.dumps({"id": session_id, "thread_name": title, "updated_at": now_iso}, ensure_ascii=False)
    try:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        with index_path.open("a", encoding="utf-8") as fh:
            fh.write(entry + "\n")
    except OSError:
        pass


def set_codex_thread_name(
    session_id: str,
    raw_title: str,
    *,
    codex_bin: Path | str | None = None,
    codex_home: Path | None = None,
    use_rpc: bool = True,
) -> dict[str, Any]:
    """Set the Codex thread display name to `agy: <title>` via official `thread/name/set`."""
    home = codex_home or default_codex_home()
    title = format_agy_title(raw_title)
    rpc_ok = False
    rpc_error: str | None = None

    if use_rpc:
        try:
            binary = locate_codex_binary(codex_bin)
            _call_app_server_rpc(
                binary,
                "thread/name/set",
                {"threadId": session_id, "name": title},
                timeout=8.0,
            )
            rpc_ok = True
        except (CodexBinaryNotFoundError, OSError, TimeoutError, RuntimeError) as exc:
            rpc_error = str(exc)

    if not rpc_ok:
        # Safe fallback: append to session_index.jsonl and update threads.name if present.
        _upsert_session_index_title(home, session_id, title)
        db_path = home / "state_5.sqlite"
        if db_path.is_file():
            try:
                conn = sqlite3.connect(str(db_path), timeout=3.0)
                cur = conn.cursor()
                cols = {r[1] for r in cur.execute('PRAGMA table_info("threads")')}
                if {"id", "name"}.issubset(cols):
                    cur.execute("UPDATE threads SET name = ? WHERE id = ?", (title, session_id))
                    conn.commit()
                conn.close()
            except sqlite3.Error:
                pass

    return {
        "title": title,
        "via_rpc": rpc_ok,
        "warning": rpc_error if not rpc_ok else None,
    }


def _is_codex_desktop_running() -> bool:
    if sys.platform != "win32":
        return False
    try:
        proc = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Codex.exe", "/NH"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=3,
            check=False,
        )
        return "codex.exe" in proc.stdout.lower()
    except (OSError, subprocess.TimeoutExpired):
        return False


def _ensure_sqlite_project(
    db_path: Path,
    norm_path: str,
    project_name: str,
    session_id: str | None,
    *,
    allow_create: bool,
    preferred_id: str | None = None,
) -> tuple[str | None, bool, str | None]:
    """Find or create a project in `state_5.sqlite`. Returns `(project_id, created, warning)`."""
    if not db_path.is_file():
        return None, False, None

    norm_key = _path_key(norm_path)
    try:
        conn = sqlite3.connect(str(db_path), timeout=3.0)
        cur = conn.cursor()
        proj_cols = {r[1] for r in cur.execute('PRAGMA table_info("projects")')}
        root_cols = {r[1] for r in cur.execute('PRAGMA table_info("project_roots")')}
        if not {"id", "name", "position", "created_at_ms", "updated_at_ms"}.issubset(proj_cols):
            conn.close()
            return None, False, "state_5.sqlite projects schema mismatch; skipped SQLite sync."
        if not {"project_id", "position", "path"}.issubset(root_cols):
            conn.close()
            return None, False, "state_5.sqlite project_roots schema mismatch; skipped SQLite sync."

        rows = cur.execute("SELECT project_id, path FROM project_roots").fetchall()
        matched_pid: str | None = None
        for pid, rpath in rows:
            if isinstance(rpath, str) and _path_key(rpath) == norm_key:
                matched_pid = str(pid)
                break

        created = False
        if matched_pid is None and allow_create:
            matched_pid = preferred_id or str(uuid.uuid4())
            now_ms = int(time.time() * 1000)
            max_pos = cur.execute("SELECT COALESCE(MAX(position), -1) FROM projects").fetchone()[0]
            cur.execute("BEGIN IMMEDIATE")
            if "metadata" in proj_cols:
                cur.execute(
                    "INSERT INTO projects (id, name, metadata, position, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                    (matched_pid, project_name, "{}", int(max_pos) + 1, now_ms, now_ms),
                )
            else:
                cur.execute(
                    "INSERT INTO projects (id, name, position, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?)",
                    (matched_pid, project_name, int(max_pos) + 1, now_ms, now_ms),
                )
            cur.execute(
                "INSERT INTO project_roots (project_id, position, path) VALUES (?, ?, ?)",
                (matched_pid, 0, norm_path),
            )
            created = True

        if matched_pid and session_id:
            thread_cols = {r[1] for r in cur.execute('PRAGMA table_info("threads")')}
            if {"id", "project_id"}.issubset(thread_cols):
                cur.execute(
                    "UPDATE threads SET project_id = ? WHERE id = ?",
                    (matched_pid, session_id),
                )

        conn.commit()
        conn.close()
        return matched_pid, created, None
    except sqlite3.Error as exc:
        return None, False, f"SQLite project sync degraded: {exc}"


def _ensure_global_state_project(
    gs_path: Path,
    norm_path: str,
    project_name: str,
    session_id: str | None,
    *,
    allow_create: bool,
    preferred_id: str | None = None,
) -> tuple[str | None, bool, str | None]:
    """Find or create a project in `.codex-global-state.json` with backup & schema validation."""
    if not gs_path.is_file():
        return None, False, None

    try:
        raw_text = gs_path.read_text(encoding="utf-8")
        data = json.loads(raw_text)
    except (OSError, json.JSONDecodeError) as exc:
        return None, False, f"Could not read .codex-global-state.json: {exc}"

    if not isinstance(data, dict):
        return None, False, "Unexpected .codex-global-state.json root type."

    local_projects = data.get("local-projects")
    if local_projects is None:
        local_projects = {}
        data["local-projects"] = local_projects
    if not isinstance(local_projects, dict):
        return None, False, "Unexpected local-projects schema in .codex-global-state.json."

    norm_key = _path_key(norm_path)
    matched_pid: str | None = None
    for pid, entry in local_projects.items():
        if not isinstance(entry, dict):
            continue
        roots = entry.get("rootPaths")
        if isinstance(roots, list) and any(isinstance(r, str) and _path_key(r) == norm_key for r in roots):
            matched_pid = str(entry.get("id") or pid)
            break

    changed = False
    created = False
    if matched_pid is None and allow_create:
        matched_pid = preferred_id or str(uuid.uuid4())
        now_ms = int(time.time() * 1000)
        local_projects[matched_pid] = {
            "id": matched_pid,
            "name": project_name,
            "rootPaths": [norm_path],
            "createdAt": now_ms,
            "updatedAt": now_ms,
        }
        order = data.get("project-order")
        if isinstance(order, list) and matched_pid not in order:
            order.append(matched_pid)
        changed = True
        created = True

    if matched_pid and session_id:
        assignments = data.get("thread-project-assignments")
        if assignments is None:
            assignments = {}
            data["thread-project-assignments"] = assignments
        if isinstance(assignments, dict):
            desired = {"projectKind": "local", "projectId": matched_pid}
            if assignments.get(session_id) != desired:
                assignments[session_id] = desired
                changed = True
        projectless = data.get("projectless-thread-ids")
        if isinstance(projectless, list) and session_id in projectless:
            projectless.remove(session_id)
            changed = True

    if changed:
        try:
            backup_path = gs_path.with_name(gs_path.name + ".agy-orbit.bak")
            shutil.copy2(gs_path, backup_path)
            tmp_path = gs_path.with_name(f".{gs_path.name}.{uuid.uuid4().hex[:8]}.tmp")
            tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp_path, gs_path)
        except OSError as exc:
            return matched_pid, created, f"Failed to write .codex-global-state.json: {exc}"

    return matched_pid, created, None


def ensure_codex_project_and_bind(
    workspace_dir: Path | str,
    session_id: str | None = None,
    *,
    codex_home: Path | None = None,
    allow_create: bool = True,
) -> dict[str, Any]:
    """Ensure `workspace_dir` exists as a Codex Desktop project and bind `session_id` to it.

    Backs up `.codex-global-state.json` before edits, validates schemas, and degrades gracefully
    without breaking delegation if Codex Desktop internal files change shape.
    """
    home = codex_home or default_codex_home()
    norm_path = _norm_win_path(workspace_dir)
    project_name = Path(norm_path).name or norm_path
    warnings: list[str] = []

    gs_pid, gs_created, gs_warn = _ensure_global_state_project(
        home / ".codex-global-state.json",
        norm_path,
        project_name,
        session_id,
        allow_create=allow_create,
    )
    if gs_warn:
        warnings.append(gs_warn)

    sq_pid, sq_created, sq_warn = _ensure_sqlite_project(
        home / "state_5.sqlite",
        norm_path,
        project_name,
        session_id,
        allow_create=allow_create,
        preferred_id=gs_pid,
    )
    if sq_warn:
        warnings.append(sq_warn)

    project_id = gs_pid or sq_pid
    created = gs_created or sq_created
    desktop_running = _is_codex_desktop_running()

    result: dict[str, Any] = {
        "workspace_dir": norm_path,
        "project_name": project_name,
        "project_id": project_id,
        "created": created,
        "bound": bool(project_id and session_id),
        "codex_desktop_running": desktop_running,
    }
    if warnings:
        result["warnings"] = warnings
    if created and desktop_running:
        result["note"] = "Codex Desktop is currently open; reload Codex Desktop if the new project does not appear immediately."
    return result
