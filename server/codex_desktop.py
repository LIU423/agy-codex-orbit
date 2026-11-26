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


def _call_app_server_batch(
    codex_bin: Path,
    calls: list[tuple[str, dict[str, Any]]],
    *,
    timeout: float = 10.0,
) -> list[dict[str, Any]]:
    """Execute one or more JSON-RPC requests on a single short-lived `codex app-server --stdio`."""
    proc = subprocess.Popen(
        [str(codex_bin), "app-server", "--stdio"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
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
        raise TimeoutError(f"Timed out waiting for codex app-server response id={target_id}")

    deadline = time.monotonic() + timeout
    try:
        _send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "clientInfo": {"name": "codex-orbit", "version": "0.6.1"},
                    "capabilities": {"experimentalApi": True},
                },
            }
        )
        init_resp = _recv_id(1, deadline)
        if "error" in init_resp:
            raise RuntimeError(f"app-server initialize failed: {init_resp['error']}")

        _send({"jsonrpc": "2.0", "method": "initialized", "params": {}})
        results: list[dict[str, Any]] = []
        req_id = 2
        for method, params in calls:
            _send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
            resp = _recv_id(req_id, deadline)
            if "error" in resp:
                raise RuntimeError(f"app-server {method} failed: {resp['error']}")
            result = resp.get("result")
            results.append(result if isinstance(result, dict) else {"value": result})
            req_id += 1
        return results
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=3)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


def _call_app_server_rpc(
    codex_bin: Path,
    method: str,
    params: dict[str, Any],
    *,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Execute a single JSON-RPC request against a short-lived `codex app-server --stdio`."""
    return _call_app_server_batch(codex_bin, [(method, params)], timeout=timeout)[0]


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
            cur.execute("BEGIN IMMEDIATE")
            # Match Codex Desktop ordering: newest project sits at position 0 (top of sidebar).
            cur.execute("UPDATE projects SET position = position + 1")
            if "metadata" in proj_cols:
                cur.execute(
                    "INSERT INTO projects (id, name, metadata, position, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?, ?)",
                    (matched_pid, project_name, "{}", 0, now_ms, now_ms),
                )
            else:
                cur.execute(
                    "INSERT INTO projects (id, name, position, created_at_ms, updated_at_ms) VALUES (?, ?, ?, ?, ?)",
                    (matched_pid, project_name, 0, now_ms, now_ms),
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
            # Codex Desktop's default interactive `thread/list` hides `source='exec'` threads.
            # Promote this AGY-delegated thread to interactive `vscode` so it appears in Codex Desktop.
            if {"id", "source"}.issubset(thread_cols):
                cur.execute(
                    "UPDATE threads SET source = 'vscode' WHERE id = ? AND source = 'exec'",
                    (session_id,),
                )

        conn.commit()
        conn.close()
        return matched_pid, created, None
    except sqlite3.Error as exc:
        return None, False, f"SQLite project sync degraded: {exc}"


def _ensure_global_state_sqlite_project(
    gs_db_path: Path,
    codex_home: Path,
    norm_path: str,
    project_name: str,
    session_id: str | None,
    *,
    allow_create: bool,
    app_server_pid: str | None = None,
) -> tuple[str | None, bool, str | None]:
    """Sync project & thread assignment into Codex Desktop's `.codex-global-state.sqlite`."""
    if not gs_db_path.is_file():
        return None, False, None

    norm_key = _path_key(norm_path)
    try:
        conn = sqlite3.connect(str(gs_db_path), timeout=3.0)
        cur = conn.cursor()
        cols = {r[1] for r in cur.execute('PRAGMA table_info("settings")')}
        if not {"scope", "key", "value"}.issubset(cols):
            conn.close()
            return None, False, ".codex-global-state.sqlite settings schema mismatch; skipped."

        def _get_json(key: str, default: Any) -> Any:
            row = cur.execute(
                "SELECT value FROM settings WHERE scope = 'global' AND key = ?",
                (key,),
            ).fetchone()
            if not row or not isinstance(row[0], str):
                return default
            try:
                return json.loads(row[0])
            except json.JSONDecodeError:
                return default

        def _set_json(key: str, val: Any) -> None:
            encoded = json.dumps(val, ensure_ascii=False, separators=(",", ":"))
            cur.execute(
                "INSERT INTO settings (scope, key, value) VALUES ('global', ?, ?) "
                "ON CONFLICT(scope, key) DO UPDATE SET value = excluded.value",
                (key, encoded),
            )

        local_projects = _get_json("local-projects", {})
        if not isinstance(local_projects, dict):
            conn.close()
            return None, False, "Unexpected local-projects schema in .codex-global-state.sqlite."

        matched_legacy_pid: str | None = None
        for pid, entry in local_projects.items():
            if not isinstance(entry, dict):
                continue
            roots = entry.get("rootPaths")
            if isinstance(roots, list) and any(isinstance(r, str) and _path_key(r) == norm_key for r in roots):
                matched_legacy_pid = str(entry.get("id") or pid)
                break

        changed = False
        created = False
        if matched_legacy_pid is None and allow_create:
            matched_legacy_pid = app_server_pid or str(uuid.uuid4())
            now_ms = int(time.time() * 1000)
            local_projects[matched_legacy_pid] = {
                "id": matched_legacy_pid,
                "name": project_name,
                "rootPaths": [norm_path],
                "createdAt": now_ms,
                "updatedAt": now_ms,
            }
            changed = True
            created = True

        project_order = _get_json("project-order", [])
        if matched_legacy_pid and isinstance(project_order, list) and matched_legacy_pid not in project_order:
            project_order.insert(0, matched_legacy_pid)
            changed = True

        # Keep legacy-id -> app-server-id mapping aligned so Codex Desktop resolves the folder picker entry.
        id_map_by_host = _get_json("app-server-project-id-by-legacy-project-id-by-host", {})
        if matched_legacy_pid and app_server_pid and isinstance(id_map_by_host, dict):
            host_key = next(
                (k for k in id_map_by_host.keys() if isinstance(k, str) and k.lower().startswith("local:")),
                f"local:{_norm_win_path(codex_home)}",
            )
            host_map = id_map_by_host.get(host_key)
            if not isinstance(host_map, dict):
                host_map = {}
                id_map_by_host[host_key] = host_map
            if host_map.get(matched_legacy_pid) != app_server_pid:
                host_map[matched_legacy_pid] = app_server_pid
                changed = True

        assignments = _get_json("thread-project-assignments", {})
        membership_hosts = _get_json("thread-project-membership-host-ids", {})
        projectless = _get_json("projectless-thread-ids", [])
        if matched_legacy_pid and session_id:
            if isinstance(assignments, dict):
                desired = {"projectKind": "local", "projectId": matched_legacy_pid}
                if assignments.get(session_id) != desired:
                    assignments[session_id] = desired
                    changed = True
            if isinstance(membership_hosts, dict) and membership_hosts.get(session_id) != "local":
                membership_hosts[session_id] = "local"
                changed = True
            if isinstance(projectless, list) and session_id in projectless:
                projectless.remove(session_id)
                changed = True

        if changed:
            backup_path = gs_db_path.with_name(gs_db_path.name + ".agy-orbit.bak")
            try:
                bconn = sqlite3.connect(str(backup_path))
                conn.backup(bconn)
                bconn.close()
            except sqlite3.Error:
                pass

            cur.execute("BEGIN IMMEDIATE")
            _set_json("local-projects", local_projects)
            if isinstance(project_order, list):
                _set_json("project-order", project_order)
            if isinstance(id_map_by_host, dict) and id_map_by_host:
                _set_json("app-server-project-id-by-legacy-project-id-by-host", id_map_by_host)
            if isinstance(assignments, dict):
                _set_json("thread-project-assignments", assignments)
            if isinstance(membership_hosts, dict) and membership_hosts:
                _set_json("thread-project-membership-host-ids", membership_hosts)
            if isinstance(projectless, list):
                _set_json("projectless-thread-ids", projectless)
            conn.commit()

        conn.close()
        return matched_legacy_pid, created, None
    except sqlite3.Error as exc:
        return None, False, f".codex-global-state.sqlite sync degraded: {exc}"


def _ensure_thread_section_via_rpc(
    codex_bin: Path,
    section_name: str,
    session_id: str | None,
    *,
    allow_create: bool,
) -> tuple[str | None, bool, str | None]:
    """Ensure a Codex Desktop sidebar section (`threadSection`) exists and move `session_id` into it."""
    target_low = section_name.strip().lower()
    if not target_low:
        return None, False, None
    try:
        listed = _call_app_server_rpc(codex_bin, "threadSection/list", {}, timeout=8.0)
        raw_sections = listed.get("data") or listed.get("sections") or []
        matched_sid: str | None = None
        if isinstance(raw_sections, list):
            for entry in raw_sections:
                if not isinstance(entry, dict):
                    continue
                nm = entry.get("name")
                if isinstance(nm, str) and nm.strip().lower() == target_low:
                    matched_sid = str(entry.get("id") or "")
                    break

        created = False
        if not matched_sid and allow_create:
            created_res = _call_app_server_rpc(
                codex_bin,
                "threadSection/create",
                {"name": section_name},
                timeout=8.0,
            )
            sec_obj = created_res.get("section") if isinstance(created_res.get("section"), dict) else created_res
            sid_val = sec_obj.get("id") if isinstance(sec_obj, dict) else None
            if isinstance(sid_val, str) and sid_val:
                matched_sid = sid_val
                created = True

        if matched_sid and session_id:
            try:
                _call_app_server_rpc(
                    codex_bin,
                    "thread/section/move",
                    {"threadId": session_id, "sectionId": matched_sid},
                    timeout=8.0,
                )
            except (OSError, TimeoutError, RuntimeError):
                pass

        return matched_sid, created, None
    except (OSError, TimeoutError, RuntimeError) as exc:
        return None, False, f"threadSection RPC degraded: {exc}"


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
            order.insert(0, matched_pid)
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


def _ensure_project_via_rpc(
    codex_bin: Path,
    norm_path: str,
    project_name: str,
    session_id: str | None,
    *,
    allow_create: bool,
) -> tuple[str | None, bool, bool, str | None]:
    """Use official `codex app-server` project RPCs (`project/list`, `project/create`, `thread/metadata/update`).

    Returns `(project_id, created, bound, warning)`.
    """
    norm_key = _path_key(norm_path)
    try:
        listed = _call_app_server_rpc(codex_bin, "project/list", {}, timeout=8.0)
        raw_projects = listed.get("data") or listed.get("projects") or []
        matched_pid: str | None = None
        if isinstance(raw_projects, list):
            for entry in raw_projects:
                if not isinstance(entry, dict):
                    continue
                roots = entry.get("roots") or []
                if isinstance(roots, list):
                    for rt in roots:
                        rpath = rt.get("path") if isinstance(rt, dict) else rt
                        if isinstance(rpath, str) and _path_key(rpath) == norm_key:
                            matched_pid = str(entry.get("id") or "")
                            break
                if matched_pid:
                    break

        created = False
        if not matched_pid and allow_create:
            idem = f"agy-orbit:{norm_key}"
            created_res = _call_app_server_rpc(
                codex_bin,
                "project/create",
                {
                    "idempotencyKey": idem,
                    "name": project_name,
                    "roots": [{"path": norm_path}],
                },
                timeout=8.0,
            )
            proj_obj = created_res.get("project") if isinstance(created_res.get("project"), dict) else created_res
            pid_val = proj_obj.get("id") if isinstance(proj_obj, dict) else None
            if isinstance(pid_val, str) and pid_val:
                matched_pid = pid_val
                created = True

        bound = False
        if matched_pid and session_id:
            try:
                _call_app_server_rpc(
                    codex_bin,
                    "thread/metadata/update",
                    {"threadId": session_id, "projectId": matched_pid},
                    timeout=8.0,
                )
                bound = True
            except (OSError, TimeoutError, RuntimeError):
                bound = False

        return matched_pid, created, bound, None
    except (OSError, TimeoutError, RuntimeError) as exc:
        return None, False, False, f"app-server project RPC degraded: {exc}"


def ensure_codex_project_and_bind(
    workspace_dir: Path | str,
    session_id: str | None = None,
    *,
    codex_bin: Path | str | None = None,
    codex_home: Path | None = None,
    allow_create: bool = True,
    use_rpc: bool | None = None,
) -> dict[str, Any]:
    """Ensure `workspace_dir` exists as a Codex Desktop project and bind `session_id` to it.

    Prefers official `codex app-server` RPCs (`project/list`, `project/create`, `thread/metadata/update`)
    when operating against the default local Codex home, then keeps `.codex-global-state.json` and
    `state_5.sqlite` synchronized with backup, schema validation, and graceful degradation.
    """
    home = codex_home or default_codex_home()
    norm_path = _norm_win_path(workspace_dir)
    project_name = Path(norm_path).name or norm_path
    warnings: list[str] = []

    # Only invoke live `codex app-server` RPCs when targeting the real Codex home (not isolated test tmpdirs).
    should_use_rpc = (codex_home is None) if use_rpc is None else bool(use_rpc)
    rpc_pid: str | None = None
    rpc_created = False
    rpc_bound = False
    section_id: str | None = None
    section_created = False
    if should_use_rpc:
        try:
            binary = locate_codex_binary(codex_bin)
            rpc_pid, rpc_created, rpc_bound, rpc_warn = _ensure_project_via_rpc(
                binary,
                norm_path,
                project_name,
                session_id,
                allow_create=allow_create,
            )
            if rpc_warn:
                warnings.append(rpc_warn)
            section_id, section_created, sec_warn = _ensure_thread_section_via_rpc(
                binary,
                project_name,
                session_id,
                allow_create=allow_create,
            )
            if sec_warn:
                warnings.append(sec_warn)
        except CodexBinaryNotFoundError:
            pass

    sq_pid, sq_created, sq_warn = _ensure_sqlite_project(
        home / "state_5.sqlite",
        norm_path,
        project_name,
        session_id,
        allow_create=allow_create,
        preferred_id=rpc_pid,
    )
    if sq_warn:
        warnings.append(sq_warn)

    canonical_pid = rpc_pid or sq_pid

    gs_db_pid, gs_db_created, gs_db_warn = _ensure_global_state_sqlite_project(
        home / ".codex-global-state.sqlite",
        home,
        norm_path,
        project_name,
        session_id,
        allow_create=allow_create,
        app_server_pid=canonical_pid,
    )
    if gs_db_warn:
        warnings.append(gs_db_warn)

    gs_pid, gs_created, gs_warn = _ensure_global_state_project(
        home / ".codex-global-state.json",
        norm_path,
        project_name,
        session_id,
        allow_create=allow_create,
        preferred_id=gs_db_pid or canonical_pid,
    )
    if gs_warn:
        warnings.append(gs_warn)

    project_id = canonical_pid or gs_db_pid or gs_pid
    created = rpc_created or sq_created or gs_db_created or gs_created
    desktop_running = _is_codex_desktop_running()

    result: dict[str, Any] = {
        "workspace_dir": norm_path,
        "project_name": project_name,
        "project_id": project_id,
        "section_id": section_id,
        "section_created": section_created,
        "created": created,
        "bound": bool((rpc_bound or project_id) and session_id),
        "via_rpc": bool(rpc_pid),
        "codex_desktop_running": desktop_running,
    }
    if warnings:
        result["warnings"] = warnings
    if created and desktop_running:
        result["note"] = "Codex Desktop is currently open; restart Codex Desktop if the new project does not appear immediately."
    return result
