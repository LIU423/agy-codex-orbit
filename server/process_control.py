"""Windows-safe process tree termination for active Codex Orbit delegations."""

from __future__ import annotations

import ctypes
import os
import signal
import subprocess
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass, field

from server.delegation_store import DelegationStore, is_process_alive


@dataclass
class CancelResult:
    cancelled: bool
    already_finished: bool = False
    session_id: str | None = None
    run_id: str | None = None
    terminated_pids: list[int] = field(default_factory=list)
    message: str = ""


def _win32_terminate_tree(pid: int) -> None:
    """Use a process snapshot when taskkill cannot enumerate the tree."""
    class ProcessEntry(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD),
            ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD),
            ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD),
            ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD),
            ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD),
            ("szExeFile", wintypes.WCHAR * 260),
        ]

    kernel32 = ctypes.WinDLL("kernel32")  # type: ignore[attr-defined]
    kernel32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.Process32NextW.argtypes = (wintypes.HANDLE, ctypes.POINTER(ProcessEntry))
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)

    children: dict[int, list[int]] = {}
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)  # TH32CS_SNAPPROCESS
    if snapshot and snapshot != ctypes.c_void_p(-1).value:
        try:
            entry = ProcessEntry(dwSize=ctypes.sizeof(ProcessEntry))
            has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
            while has_entry:
                children.setdefault(entry.th32ParentProcessID, []).append(entry.th32ProcessID)
                has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        finally:
            kernel32.CloseHandle(snapshot)

    targets = [pid]
    seen = {pid, os.getpid()}
    for parent in targets:
        for child in children.get(parent, ()):
            if child not in seen:
                seen.add(child)
                targets.append(child)
    for target in reversed(targets):
        handle = kernel32.OpenProcess(0x0001, False, target)  # PROCESS_TERMINATE
        if handle:
            try:
                kernel32.TerminateProcess(handle, 1)
            finally:
                kernel32.CloseHandle(handle)


def _send_posix_signal(pid: int, sig: signal.Signals) -> None:
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
    except ProcessLookupError:
        pass


def terminate_process_tree(pid: int | None, *, wait_seconds: float = 2.0) -> bool:
    """Safely terminate a process and its descendants on Windows or POSIX.

    Returns True if the target process is no longer alive afterwards.
    """
    if pid is None or pid <= 0 or pid == os.getpid():
        return False
    if sys.platform == "win32" and pid == 4:
        return False
    if not is_process_alive(pid):
        return True

    if sys.platform == "win32":
        try:
            result = subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=8,
                check=False,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            if result.returncode != 0:
                _win32_terminate_tree(pid)
        except (OSError, subprocess.TimeoutExpired):
            _win32_terminate_tree(pid)

        deadline = time.monotonic() + max(0.2, wait_seconds)
        while time.monotonic() < deadline:
            if not is_process_alive(pid):
                return True
            time.sleep(0.05)

        if is_process_alive(pid):
            _win32_terminate_tree(pid)
            time.sleep(0.1)
        return not is_process_alive(pid)

    try:
        _send_posix_signal(pid, signal.SIGTERM)
    except OSError:
        return not is_process_alive(pid)

    deadline = time.monotonic() + max(0.2, wait_seconds)
    while time.monotonic() < deadline:
        if not is_process_alive(pid):
            return True
        time.sleep(0.05)

    try:
        _send_posix_signal(pid, signal.SIGKILL)
    except OSError:
        pass
    return not is_process_alive(pid)


def cancel_delegation(
    store: DelegationStore,
    *,
    session_id: str | None = None,
    run_id: str | None = None,
    reason: str = "Cancelled from Codex Orbit.",
) -> CancelResult:
    """Cancel a running delegation round and terminate its process tree."""
    rec = None
    if run_id:
        rec = store.get_by_run_id(run_id)
    if rec is None and session_id:
        rec = store.get_by_session_id(session_id, verify_liveness=True)
    if rec is None:
        return CancelResult(
            cancelled=False,
            already_finished=True,
            session_id=session_id,
            run_id=run_id,
            message="No delegation record found.",
        )

    target_round = None
    if run_id:
        target_round = next((r for r in rec.rounds if r.run_id == run_id), None)
    else:
        for rnd in reversed(rec.rounds):
            if rnd.status == "running":
                target_round = rnd
                break

    if target_round is None or target_round.status != "running":
        return CancelResult(
            cancelled=False,
            already_finished=True,
            session_id=rec.session_id or rec.record_id,
            run_id=target_round.run_id if target_round else run_id,
            message="Delegation is not currently running.",
        )

    # Mark cancelled BEFORE terminating child_pid so delegate_cli emits a clean cancelled summary.
    store.update_round(
        target_round.run_id,
        status="cancelled",
        error_summary=reason,
        ended=True,
    )

    terminated: list[int] = []
    child_pid = target_round.child_pid
    wrapper_pid = target_round.pid

    if child_pid and child_pid != os.getpid() and is_process_alive(child_pid):
        if terminate_process_tree(child_pid, wait_seconds=1.5):
            terminated.append(child_pid)

    # Allow delegate_cli wrapper a short window to flush its compact cancelled JSON summary cleanly.
    if wrapper_pid and wrapper_pid != os.getpid():
        deadline = time.monotonic() + 1.2
        while time.monotonic() < deadline and is_process_alive(wrapper_pid):
            time.sleep(0.05)
        if is_process_alive(wrapper_pid):
            if terminate_process_tree(wrapper_pid, wait_seconds=1.5):
                terminated.append(wrapper_pid)

    return CancelResult(
        cancelled=True,
        already_finished=False,
        session_id=rec.session_id or rec.record_id,
        run_id=target_round.run_id,
        terminated_pids=terminated,
        message="Delegation cancelled.",
    )
